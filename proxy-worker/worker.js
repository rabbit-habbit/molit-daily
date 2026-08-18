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
// Cloudflare Cron Trigger(분 단위 정확)가 담당한다: 토 08:37 KST에
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

// ── 대기명단 원클릭 신청 ─────────────────────────────────────────────
// 뉴스레터 메일마다 수신자 전용 링크(/waitlist?t=<b64 email>&s=<hmac>)를 심는다.
// 클릭 → 완료 페이지가 뜨고, 페이지의 JS가 /waitlist/confirm으로 POST → KV 기록.
// (메일 보안 스캐너는 JS를 실행하지 않으므로 봇 클릭이 명단에 안 잡힌다)

async function hmacHex(secret, msg) {
  const key = await crypto.subtle.importKey(
    "raw", new TextEncoder().encode(secret),
    { name: "HMAC", hash: "SHA-256" }, false, ["sign"]
  );
  const sig = await crypto.subtle.sign("HMAC", key, new TextEncoder().encode(msg));
  return [...new Uint8Array(sig)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

function decodeToken(t) {
  let s = t.replace(/-/g, "+").replace(/_/g, "/");
  while (s.length % 4) s += "=";
  try { return atob(s); } catch { return null; }
}

async function verifyWaitlist(env, t, s) {
  const email = t ? decodeToken(t) : null;
  if (!email || !s || !email.includes("@")) return null;
  const expect = (await hmacHex(env.PROXY_TOKEN, email)).slice(0, 32);
  return s === expect ? email : null;
}

const WAITLIST_PAGE = (ok) => `<!DOCTYPE html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>래빗해빛 데일리 브리핑</title></head>
<body style="margin:0;font-family:'Apple SD Gothic Neo','Malgun Gothic',sans-serif;background:#FFF8F5;">
<div style="max-width:480px;margin:80px auto;padding:40px 32px;background:#fff;border:1.5px solid #FF6B35;border-radius:20px;text-align:center;">
  <div style="font-size:48px;">🐰</div>
  ${ok
    ? `<h2 style="margin:12px 0 8px;color:#24302A;">오픈 알림 신청 완료!</h2>
       <p style="color:#64716B;font-size:14.5px;line-height:1.7;">「데일리 경제 브리핑」이 오픈하면<br>
       <b style="color:#FF6B35;">가장 먼저, 가장 좋은 조건으로</b> 알려드릴게요.<br>매주 토요일 국토부 브리핑도 계속 만나요!</p>`
    : `<h2 style="margin:12px 0 8px;color:#24302A;">링크가 올바르지 않아요</h2>
       <p style="color:#64716B;font-size:14.5px;">받으신 메일의 버튼으로 다시 시도해주세요.</p>`}
</div>
${ok ? `<script>fetch("/waitlist/confirm",{method:"POST",headers:{"content-type":"application/json"},body:JSON.stringify({t:new URLSearchParams(location.search).get("t"),s:new URLSearchParams(location.search).get("s")})});</script>` : ""}
</body></html>`;

export default {
  // Cloudflare Cron Trigger (wrangler.toml [triggers]) — 토 08:37 KST 정각
  async scheduled(event, env, ctx) {
    const r = await dispatchWorkflow(env);
    if (r.status !== 204) {
      console.log("workflow dispatch 실패:", r.status, await r.text());
    }
  },

  async fetch(request, env) {
    const reqUrl = new URL(request.url);

    // 대기명단 경로는 공개 (수신자가 메일에서 직접 클릭)
    if (reqUrl.pathname === "/waitlist" && request.method === "GET") {
      const email = await verifyWaitlist(
        env, reqUrl.searchParams.get("t"), reqUrl.searchParams.get("s")
      );
      return new Response(WAITLIST_PAGE(!!email), {
        status: email ? 200 : 400,
        headers: { "content-type": "text/html; charset=utf-8" },
      });
    }
    if (reqUrl.pathname === "/waitlist/confirm" && request.method === "POST") {
      let body;
      try { body = await request.json(); } catch { body = {}; }
      const email = await verifyWaitlist(env, body.t, body.s);
      if (!email) return new Response("bad token", { status: 400 });
      const existing = await env.WAITLIST.get("sub:" + email);
      if (!existing) {
        await env.WAITLIST.put("sub:" + email, new Date().toISOString());
      }
      return new Response("ok");
    }

    // 대기명단 실시간 조회 (대표 전용 — ?key=<PROXY_TOKEN> 로 인증)
    if (reqUrl.pathname === "/waitlist/list" && request.method === "GET") {
      if (reqUrl.searchParams.get("key") !== env.PROXY_TOKEN) {
        return new Response("forbidden", { status: 403 });
      }
      const listed = await env.WAITLIST.list({ prefix: "sub:" });
      const entries = [];
      for (const k of listed.keys) {
        const when = await env.WAITLIST.get(k.name);
        entries.push({ email: k.name.slice(4), when: when || "" });
      }
      entries.sort((a, b) => (a.when < b.when ? 1 : -1));
      const rows = entries.map((e, i) => {
        const dt = e.when ? new Date(e.when).toLocaleString("ko-KR", { timeZone: "Asia/Seoul" }) : "-";
        return `<tr><td style="padding:8px 12px;color:#64716B;">${entries.length - i}</td>
          <td style="padding:8px 12px;font-weight:600;">${e.email}</td>
          <td style="padding:8px 12px;color:#64716B;">${dt}</td></tr>`;
      }).join("");
      const page = `<!DOCTYPE html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>데일리 브리핑 대기명단</title></head>
<body style="margin:0;font-family:'Apple SD Gothic Neo','Malgun Gothic',sans-serif;background:#FFF8F5;padding:24px;">
<div style="max-width:640px;margin:0 auto;">
  <h2 style="color:#24302A;">🔔 데일리 브리핑 오픈 알림 대기명단</h2>
  <p style="color:#FF6B35;font-weight:bold;font-size:18px;">${entries.length}명 신청</p>
  <table style="width:100%;background:#fff;border:1px solid #FFE0D1;border-radius:12px;border-collapse:separate;border-spacing:0;font-size:14px;">
    <tr style="background:#FFF3EE;"><th style="padding:10px 12px;text-align:left;">#</th>
      <th style="padding:10px 12px;text-align:left;">이메일</th><th style="padding:10px 12px;text-align:left;">신청 시각</th></tr>
    ${rows || '<tr><td colspan="3" style="padding:20px;text-align:center;color:#64716B;">아직 신청자가 없어요</td></tr>'}
  </table>
  <p style="color:#64716B;font-size:12px;margin-top:12px;">새로고침하면 실시간 반영 · 이 주소는 비공개로 관리하세요</p>
</div></body></html>`;
      return new Response(page, { headers: { "content-type": "text/html; charset=utf-8" } });
    }

    if (request.method !== "GET") {
      return new Response("method not allowed", { status: 405 });
    }
    if (request.headers.get("x-proxy-token") !== env.PROXY_TOKEN) {
      return new Response("forbidden", { status: 403 });
    }
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
