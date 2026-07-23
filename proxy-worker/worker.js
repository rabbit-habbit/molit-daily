/**
 * molit-proxy — 국토부 사이트 중계 Worker
 *
 * GitHub Actions(해외 IP, 국토부가 차단)가 molit.go.kr에 접근할 수 있도록
 * Cloudflare 네트워크를 경유시키는 단순 HTTP 릴레이.
 *
 *   GET https://<worker>/?url=<molit.go.kr URL>
 *   Header: x-proxy-token: <PROXY_TOKEN secret>
 *
 * 보안:
 *  - PROXY_TOKEN 불일치 시 403 (우리 파이프라인 외 사용 불가)
 *  - 대상 호스트는 molit.go.kr 계열만 허용 (오픈 프록시 방지)
 *
 * 국토부 WAF 대응:
 *  - 첫 요청에 307 + TMOSHCooKie 쿠키를 주고 같은 URL로 재접속시키므로
 *    redirect를 수동 처리하며 쿠키를 이어붙여 최대 6홉까지 따라간다.
 */

const UA =
  "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) " +
  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36";

// GitHub Actions cron은 수 시간씩 지연되는 best-effort라, 정시 발행은
// Cloudflare Cron Trigger(분 단위 정확)가 담당한다: 토 09:37 KST에
// GitHub API로 weekly.yml 워크플로를 직접 깨운다. GH_TOKEN 시크릿 필요
// (fine-grained PAT, molit-daily 저장소 Actions read/write 전용).
async function dispatchWorkflow(env) {
  const r = await fetch(
    "https://api.github.com/repos/rabbit-habbit/molit-daily/actions/workflows/weekly.yml/dispatches",
    {
      method: "POST",
      headers: {
        Authorization: `Bearer ${env.GH_TOKEN}`,
        Accept: "application/vnd.github+json",
        "User-Agent": "molit-proxy-cron",
        "X-GitHub-Api-Version": "2022-11-28",
      },
      body: JSON.stringify({ ref: "main" }),
    }
  );
  return r; // 성공 시 204 No Content
}

function setCookies(resp) {
  if (typeof resp.headers.getSetCookie === "function") {
    return resp.headers.getSetCookie();
  }
  const sc = resp.headers.get("set-cookie");
  return sc ? [sc] : [];
}

export default {
  // Cloudflare Cron Trigger (wrangler.toml [triggers]) — 토 09:37 KST 정각
  async scheduled(event, env, ctx) {
    const r = await dispatchWorkflow(env);
    if (r.status !== 204) {
      console.log("workflow dispatch 실패:", r.status, await r.text());
    }
  },

  async fetch(request, env) {
    if (request.method !== "GET") {
      return new Response("method not allowed", { status: 405 });
    }
    if (request.headers.get("x-proxy-token") !== env.PROXY_TOKEN) {
      return new Response("forbidden", { status: 403 });
    }
    const reqUrl = new URL(request.url);
    // 크론 디스패치 수동 테스트용 (프록시 토큰 인증 후)
    if (reqUrl.pathname === "/cron-test") {
      const r = await dispatchWorkflow(env);
      const body = r.status === 204 ? "dispatched" : await r.text();
      return new Response(`${r.status} ${body}`, { status: 200 });
    }
    const target = reqUrl.searchParams.get("url");
    if (!target) {
      return new Response("missing ?url=", { status: 400 });
    }
    let t;
    try {
      t = new URL(target);
    } catch {
      return new Response("bad url", { status: 400 });
    }
    if (t.protocol !== "https:" && t.protocol !== "http:") {
      return new Response("bad scheme", { status: 400 });
    }
    if (!(t.hostname === "molit.go.kr" || t.hostname.endsWith(".molit.go.kr"))) {
      return new Response("host not allowed", { status: 400 });
    }

    const cookies = [];
    let resp;
    for (let hop = 0; hop < 6; hop++) {
      resp = await fetch(t.toString(), {
        redirect: "manual",
        headers: {
          "User-Agent": UA,
          "Accept-Language": "ko-KR,ko;q=0.9",
          ...(cookies.length ? { Cookie: cookies.join("; ") } : {}),
        },
      });
      for (const sc of setCookies(resp)) {
        const pair = sc.split(";")[0].trim();
        if (pair && !cookies.includes(pair)) cookies.push(pair);
      }
      if (resp.status >= 300 && resp.status < 400) {
        const loc = resp.headers.get("location");
        if (!loc) break;
        const next = new URL(loc, t);
        if (
          !(next.hostname === "molit.go.kr" ||
            next.hostname.endsWith(".molit.go.kr"))
        ) {
          return new Response("redirect off-host: " + next.hostname, {
            status: 502,
          });
        }
        t = next;
        continue;
      }
      break;
    }

    return new Response(resp.body, {
      status: resp.status,
      headers: {
        "content-type":
          resp.headers.get("content-type") || "application/octet-stream",
        "x-proxy-final-url": t.toString(),
      },
    });
  },
};
