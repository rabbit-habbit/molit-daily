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


// ── 유료 브리핑 서명 링크 게이트 ─────────────────────────────────────
// 그룹톡에는 공개 Pages 링크 대신 /brief/{app}/{date}?e=<만료 unix초>&s=<hmac>
// 을 올린다. 날짜·만료를 조작하면 서명이 깨져 403, 만료 지나면 410.
// 서명 키는 LINK_SIGN_KEY secret (발신 스크립트 notify_publ.py와 공유).
// 콘텐츠는 GitHub raw에서 가져와 복사 방지 스니펫을 주입해 서빙한다.
// 한계: 화면 캡처·개발자도구까지 막지는 못한다 (억지력 수준).

const BRIEF_SOURCES = {
  k: (date) => `https://raw.githubusercontent.com/rabbit-habbit/kyungje-daily/main/docs/archive/${date}-share-inline.html`,
  m: (date) => `https://raw.githubusercontent.com/rabbit-habbit/molit-daily/main/exports/briefing-${date}-inline.html`,
  e: (date) => `https://raw.githubusercontent.com/rabbit-habbit/molit-daily/main/docs/mail/${date}.html`, // 이메일 전용 (얼리버드 카드 포함)
};

const GUARD_STYLE = `<style>html,body{-webkit-user-select:none!important;user-select:none!important;-webkit-touch-callout:none!important}</style>`;
const GUARD_SCRIPT = `<script>for(const t of["contextmenu","copy","cut","dragstart","selectstart"])document.addEventListener(t,function(e){e.preventDefault()});</script>`;

const BRIEF_ERROR_PAGE = (title, msg) => `<!DOCTYPE html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>래빗해빛 브리핑</title></head>
<body style="margin:0;font-family:'Apple SD Gothic Neo','Malgun Gothic',sans-serif;background:#FFF8F5;">
<div style="max-width:480px;margin:80px auto;padding:40px 32px;background:#fff;border:1.5px solid #FF6B35;border-radius:20px;text-align:center;">
  <div style="font-size:48px;">🐰</div>
  <h2 style="margin:12px 0 8px;color:#24302A;">${title}</h2>
  <p style="color:#64716B;font-size:14.5px;line-height:1.7;">${msg}</p>
</div></body></html>`;

async function serveBrief(reqUrl, env) {
  const m = reqUrl.pathname.match(/^\/brief\/([kme])\/(\d{4}-\d{2}-\d{2})$/);
  if (!m) return new Response(BRIEF_ERROR_PAGE("잘못된 주소예요", "받으신 링크를 그대로 눌러주세요."), {
    status: 404, headers: { "content-type": "text/html; charset=utf-8" },
  });
  const [, app, date] = m;
  const e = reqUrl.searchParams.get("e") || "";
  const sGiven = reqUrl.searchParams.get("s") || "";
  const expect = (await hmacHex(env.LINK_SIGN_KEY, `${app}/${date}/${e}`)).slice(0, 32);
  if (!/^\d{9,12}$/.test(e) || sGiven !== expect) {
    return new Response(BRIEF_ERROR_PAGE("링크가 올바르지 않아요", "멤버십 채팅방에 올라온 링크를 그대로 눌러주세요.<br>주소를 직접 수정하면 열리지 않아요."), {
      status: 403, headers: { "content-type": "text/html; charset=utf-8" },
    });
  }
  if (Date.now() / 1000 > Number(e)) {
    return new Response(BRIEF_ERROR_PAGE("링크 유효기간이 지났어요", "지난 브리핑은 publ 앱의 아티클에서<br>언제든 다시 보실 수 있어요 🐰"), {
      status: 410, headers: { "content-type": "text/html; charset=utf-8" },
    });
  }

  const src = await fetch(BRIEF_SOURCES[app](date), { headers: { "User-Agent": "rh-brief-gate" } });
  if (!src.ok) {
    return new Response(BRIEF_ERROR_PAGE("아직 준비 중이에요", "브리핑이 아직 발행 전이거나 주소가 달라요."), {
      status: 404, headers: { "content-type": "text/html; charset=utf-8" },
    });
  }
  let html = await src.text();
  if (/<\/body>/i.test(html)) {
    // 완전한 문서 (kyungje) → 가드 주입
    html = html.replace(/<head>/i, `<head>${GUARD_STYLE}`).replace(/<\/body>/i, `${GUARD_SCRIPT}</body>`);
  } else {
    // 본문 fragment (molit inline) → 문서로 감싸며 가드 포함
    html = `<!DOCTYPE html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>이번 주 정책 브리핑 - 래빗해빛</title>${GUARD_STYLE}</head><body style="margin:0;background:#F7FAF7;">${html}${GUARD_SCRIPT}</body></html>`;
  }
  return new Response(html, {
    headers: {
      "content-type": "text/html; charset=utf-8",
      "cache-control": "private, no-store",
      "x-robots-tag": "noindex, nofollow, noarchive",
      "referrer-policy": "no-referrer",
      "x-frame-options": "DENY",
    },
  });
}

// ── 상위 퍼널 클릭 리다이렉터 ────────────────────────────────────────
// 브리핑 안의 상품 링크를 여기로 보내고, 기록한 뒤 실제 주소로 302 한다.
//
//   /r?p=<상품>&s=<지면>&v=<변형>&a=<각도>  →  302 실제 주소
//
// 목적지는 URL 파라미터로 받지 않고 아래 표에서만 고른다. 파라미터로 받으면
// 누구나 우리 도메인을 경유해 아무 데로나 보낼 수 있는 오픈 리다이렉트가 된다.
//
// 기록 실패는 절대 리다이렉트를 막지 않는다. 링크가 죽는 것보다 통계 1건을
// 잃는 쪽이 훨씬 싸다.

// 코드 하나에 목적지 하나. 나중에 목적지가 바뀌면 코드도 새로 만든다.
// 같은 코드로 목적지만 갈아끼우면 무료 유입과 유료 유입이 한 계열에 섞이고,
// 계열이 끊긴 것조차 알아챌 수 없다.
const R_DEST = {
  // 머니프리랩 오픈 알림용 카카오 채널. 모집 전 단계의 유일한 행선지.
  mf_kakao: "https://pf.kakao.com/_RBZrX/friend",

  // 책 「잘잘잘돈」 (예스24 제휴 단축 링크, 수수료 3%).
  //
  // CJ온스타일 유튜브 쇼핑 링크(수수료 10%)를 쓰다가 바꿨다. 수수료율은 높지만
  // 배송비가 붙어 독자가 19,000원대를 내는 반면 예스24는 17,100원이다.
  // 우리가 더 버는 1,387원보다 독자가 더 내는 1,900원이 커서, 차액은 우리도
  // 독자도 아닌 배송비로 사라진다.
  //
  // ★ 단축 코드 자체가 수수료 귀속을 나르므로 주소를 손대지 말 것.
  //   펼쳐서 최종 주소로 바꾸면 수수료가 0이 되고, 그건 정산서를 보기 전까지
  //   드러나지 않는다. 바꿔야 하면 예스24에서 새 링크를 받아 통째로 교체한다.
  book: "https://link.yes24.com/a/LdOL9rzKu3",

  taling: "https://www.taling.me/mkt/rabbit_2",

  // TODO(대표님): 모집 개시 후 결제 페이지 주소를 받으면 교체한다.
  mf_sale: "https://PLACEHOLDER/moneyfreelab",
};

// 허용값. 여기 없는 값이 오면 목적지는 정상 처리하되 ok=0으로 남겨
// "파이프라인이 이상한 값을 보내고 있다"가 조회에서 드러나게 한다.
// 조용히 정규화해버리면 링크 생성 버그를 영영 모른다.
// 지면 코드. 2026-09-27 배치 확정으로 갈아치웠다.
//   ladder    : 기초 다지기와 한줄 인사이트 사이의 상시 3종 블록
//   article_n : 그날 기사에 이어 붙는 트리거 블록 (주 1~2회)
// 옛 코드(basic·insight·footer·article·news)는 지금 배치에 대응하는 자리가
// 없어 폐기했다. 데이터가 0일 때만 할 수 있는 정리라 지금 했다.
const R_SLOTS = new Set(["ladder", "article_n"]);
// 각도는 상시 블록이 셋을 한 번에 보여주는 구조라 의미가 줄었지만,
// 트리거 블록이 어떤 맥락으로 붙었는지는 계속 구분한다.
const R_ANGLES = new Set(["role", "link", "ask", "quote"]);
const R_VARIANT_RE = /^(base|e\d{2}[ab])$/;

const R_BOT_RE = /bot|crawler|spider|crawling|preview|fetch|monitor|slurp|curl|wget|headless|python-requests|okhttp|facebookexternalhit|whatsapp|telegram|kakaotalk-scrap/i;

function rIsBot(request) {
  const ua = request.headers.get("user-agent") || "";
  if (!ua || R_BOT_RE.test(ua)) return 1;
  // 브라우저·메신저의 사전 로딩. 사람이 누른 게 아니다.
  const purpose = (request.headers.get("sec-purpose") || request.headers.get("purpose") || "").toLowerCase();
  if (purpose.includes("prefetch") || purpose.includes("preview")) return 1;
  return 0;
}

// KST 기준 날짜. 워커는 UTC로 도니 직접 더한다.
function rKstDay(ms) {
  return new Date(ms + 9 * 3600 * 1000).toISOString().slice(0, 10);
}

// IP 원문은 저장하지 않는다. 같은 사람의 연타를 조회 시점에 걸러내려면
// 식별자가 필요한데, 그 용도에는 소금 친 해시 앞부분이면 충분하다.
async function rIpHash(env, request) {
  const ip = request.headers.get("cf-connecting-ip") || "";
  if (!ip) return null;
  const salt = env.LINK_SIGN_KEY || env.PROXY_TOKEN || "rh";
  return (await hmacHex(salt, "ip:" + ip)).slice(0, 16);
}

async function serveRedirect(request, reqUrl, env, ctx) {
  const q = reqUrl.searchParams;
  const p = q.get("p") || "";
  const dest = R_DEST[p];

  // 상품 코드가 없으면 보낼 곳이 없다. 이것만은 막는다.
  if (!dest) {
    return new Response(
      BRIEF_ERROR_PAGE("링크가 올바르지 않아요", "브리핑에 있는 링크를 그대로 눌러주세요."),
      { status: 404, headers: { "content-type": "text/html; charset=utf-8" } }
    );
  }

  const s = q.get("s") || "";
  const v = q.get("v") || "";
  const a = q.get("a") || "";
  const ok = R_SLOTS.has(s) && R_ANGLES.has(a) && R_VARIANT_RE.test(v) ? 1 : 0;

  // 기록은 응답을 붙잡지 않는다. 사람은 이미 목적지로 가고 있다.
  if (env.DB) {
    const now = Date.now();
    const row = {
      ts: Math.floor(now / 1000),
      day: rKstDay(now),
      p,
      s: s.slice(0, 32),
      v: v.slice(0, 32),
      a: a.slice(0, 32),
      ok,
      bot: rIsBot(request),
      ua: (request.headers.get("user-agent") || "").slice(0, 200),
    };
    const write = (async () => {
      try {
        const ipx = await rIpHash(env, request);
        await env.DB.prepare(
          "INSERT INTO clicks (ts, day, p, s, v, a, ok, bot, ipx, ua) VALUES (?,?,?,?,?,?,?,?,?,?)"
        ).bind(row.ts, row.day, row.p, row.s, row.v, row.a, row.ok, row.bot, ipx, row.ua).run();
      } catch (err) {
        // 기록 실패는 삼킨다. 단 로그에는 남겨서 조용히 사라지지 않게 한다.
        console.log("click log 실패:", err && err.message);
      }
    })();
    if (ctx && typeof ctx.waitUntil === "function") ctx.waitUntil(write);
  }

  return new Response(null, {
    status: 302,
    headers: {
      location: dest,
      "cache-control": "no-store",       // 캐시되면 클릭이 안 잡힌다
      "referrer-policy": "no-referrer",  // 서명 링크 주소가 목적지로 새지 않게
    },
  });
}

// ── 상품 이미지 서빙 ────────────────────────────────────────────────
// 브리핑 HTML은 매일 새로 만들어지고 publ 아티클에도 그대로 들어가므로,
// 이미지 주소가 변하지 않아야 한다. 저장소의 docs/img/를 우리 도메인으로
// 중계한다. raw.githubusercontent를 직접 걸지 않는 이유가 둘이다.
//   - 거기서는 content-type이 text/plain으로 나가는 경우가 있어 안 뜬다
//   - 주소가 저장소 구조에 묶여서 나중에 옮기면 과거 발행분이 전부 깨진다
// 파일명만 허용하고 경로 문자는 막는다 (디렉터리 탈출 방지).
const IMG_SRC = (name) =>
  `https://raw.githubusercontent.com/rabbit-habbit/kyungje-daily/main/docs/img/${name}`;
const IMG_TYPES = { jpg: "image/jpeg", jpeg: "image/jpeg", png: "image/png", webp: "image/webp" };

async function serveImage(reqUrl, env, ctx) {
  const name = reqUrl.pathname.slice("/img/".length);
  if (!/^[A-Za-z0-9._-]{1,64}$/.test(name) || name.includes("..")) {
    return new Response("not found", { status: 404 });
  }
  const type = IMG_TYPES[(name.split(".").pop() || "").toLowerCase()];
  if (!type) return new Response("not found", { status: 404 });

  const cache = caches.default;
  const hit = await cache.match(reqUrl.toString());
  if (hit) return hit;

  const src = await fetch(IMG_SRC(name), { headers: { "User-Agent": "rh-img" } });
  if (!src.ok) return new Response("not found", { status: 404 });

  const resp = new Response(src.body, {
    headers: {
      "content-type": type,
      // 같은 파일명은 내용이 바뀌지 않는다는 전제. 바꿀 때는 파일명을 바꾼다.
      "cache-control": "public, max-age=604800",
      "x-content-type-options": "nosniff",
    },
  });
  if (ctx && typeof ctx.waitUntil === "function") {
    ctx.waitUntil(cache.put(reqUrl.toString(), resp.clone()));
  }
  return resp;
}

// 집계 조회. 대시보드와 주간 리뷰가 쓴다. PROXY_TOKEN으로 잠근다.
async function serveRedirectStats(reqUrl, env) {
  if (reqUrl.searchParams.get("key") !== env.PROXY_TOKEN) {
    return new Response("forbidden", { status: 403 });
  }
  if (!env.DB) return new Response(JSON.stringify({ error: "DB 바인딩 없음" }), {
    status: 503, headers: { "content-type": "application/json; charset=utf-8" },
  });
  const from = reqUrl.searchParams.get("from") || "0000-00-00";
  const to = reqUrl.searchParams.get("to") || "9999-99-99";
  // bot·ok를 합치지 않고 그대로 내보낸다. 합쳐서 내보내면 봇 트래픽이나
  // 규격 위반이 정상 수치에 섞인 채 대시보드에 올라간다.
  try {
    const rows = await env.DB.prepare(
      `SELECT day, p, s, v, a, ok, bot, COUNT(*) AS n
         FROM clicks WHERE day >= ? AND day <= ?
        GROUP BY day, p, s, v, a, ok, bot
        ORDER BY day DESC`
    ).bind(from, to).all();
    return new Response(JSON.stringify({ from, to, rows: rows.results || [] }, null, 2), {
      headers: { "content-type": "application/json; charset=utf-8" },
    });
  } catch (err) {
    // 조회 실패를 빈 결과로 돌려주면 "클릭이 0건"으로 읽힌다. 에러로 알린다.
    return new Response(JSON.stringify({ error: String(err && err.message || err) }), {
      status: 500, headers: { "content-type": "application/json; charset=utf-8" },
    });
  }
}

export default {
  // Cloudflare Cron Trigger (wrangler.toml [triggers]) — 토 08:37 KST 정각
  async scheduled(event, env, ctx) {
    const r = await dispatchWorkflow(env);
    if (r.status !== 204) {
      console.log("workflow dispatch 실패:", r.status, await r.text());
    }
  },

  async fetch(request, env, ctx) {
    const reqUrl = new URL(request.url);

    // 상위 퍼널 클릭 리다이렉터. 구독자가 브리핑에서 직접 누르므로 공개.
    if (reqUrl.pathname === "/r" && request.method === "GET") {
      return serveRedirect(request, reqUrl, env, ctx);
    }
    if (reqUrl.pathname === "/r/stats" && request.method === "GET") {
      return serveRedirectStats(reqUrl, env);
    }
    if (reqUrl.pathname.startsWith("/img/") && request.method === "GET") {
      return serveImage(reqUrl, env, ctx);
    }

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

    // 유료 브리핑 서명 링크 (멤버십 채팅방용 · 공개 경로)
    if (reqUrl.pathname.startsWith("/brief/") && request.method === "GET") {
      return serveBrief(reqUrl, env);
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
