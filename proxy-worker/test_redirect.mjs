import worker from "./worker.js";

// 가짜 D1. 기록된 행을 모아둔다.
const rows = [];
const DB = {
  prepare(sql) {
    return {
      bind(...args) {
        return {
          async run() { rows.push({ sql, args }); },
          async all() { return { results: [{ day: "2026-10-01", p: "book", s: "basic", v: "base", a: "link", ok: 1, bot: 0, n: 3 }] }; },
        };
      },
    };
  },
};
const env = { DB, LINK_SIGN_KEY: "testkey", PROXY_TOKEN: "tok" };
const ctx = { waitUntil: (p) => p };

function req(url, headers = {}) {
  return new Request(url, { headers });
}
const B = "https://economy-proxy.example.dev";

let pass = 0, fail = 0;
function check(name, cond, extra = "") {
  if (cond) { pass++; console.log("  OK  " + name); }
  else { fail++; console.log("  X   " + name + (extra ? "  " + extra : "")); }
}

console.log("\n[정상 클릭]");
let r = await worker.fetch(req(`${B}/r?p=book&s=ladder&v=base&a=link`, { "user-agent": "Mozilla/5.0 (iPhone) Safari" }), env, ctx);
check("302로 보낸다", r.status === 302, `status=${r.status}`);
check("목적지가 코드 표에서 나온다", (r.headers.get("location") || "").includes("yes24"));
check("캐시 금지", r.headers.get("cache-control") === "no-store");
check("리퍼러 안 넘김", r.headers.get("referrer-policy") === "no-referrer");
await new Promise((x) => setTimeout(x, 10));
check("1건 기록됨", rows.length === 1, `rows=${rows.length}`);
const a = rows[0].args;
check("p·s·v·a가 각각 별도 컬럼", a[2] === "book" && a[3] === "ladder" && a[4] === "base" && a[5] === "link", JSON.stringify(a.slice(2, 6)));
check("ok=1", a[6] === 1);
check("bot=0", a[7] === 0);
check("IP 원문 저장 안 함", !JSON.stringify(a).includes("1.2.3.4"));

console.log("\n[폐기된 옛 지면 코드가 조용히 통과하면 안 된다]");
rows.length = 0;
r = await worker.fetch(req(`${B}/r?p=book&s=basic&v=base&a=link`, { "user-agent": "Mozilla/5.0 Safari" }), env, ctx);
await new Promise((x) => setTimeout(x, 10));
check("리다이렉트는 정상", r.status === 302);
check("ok=0으로 남아 조회에서 드러난다", rows[0].args[6] === 0);
check("폐기된 옛 코드가 원문 그대로 남는다", rows[0].args[3] === "basic");

console.log("\n[봇·프리페치]");
rows.length = 0;
await worker.fetch(req(`${B}/r?p=book&s=ladder&v=base&a=link`, { "user-agent": "Googlebot/2.1" }), env, ctx);
await worker.fetch(req(`${B}/r?p=book&s=ladder&v=base&a=link`, { "user-agent": "Mozilla/5.0 Safari", "sec-purpose": "prefetch" }), env, ctx);
await worker.fetch(req(`${B}/r?p=book&s=ladder&v=base&a=link`, {}), env, ctx);
await new Promise((x) => setTimeout(x, 10));
check("봇 UA → bot=1", rows[0].args[7] === 1);
check("프리페치 → bot=1", rows[1].args[7] === 1);
check("UA 없음 → bot=1", rows[2].args[7] === 1);

console.log("\n[오픈 리다이렉트 방지]");
r = await worker.fetch(req(`${B}/r?p=book&u=https://evil.example&s=ladder&v=base&a=link`, { "user-agent": "Safari" }), env, ctx);
check("u 파라미터를 무시한다", !(r.headers.get("location") || "").includes("evil"));
r = await worker.fetch(req(`${B}/r?p=https://evil.example`, { "user-agent": "Safari" }), env, ctx);
check("모르는 상품코드는 404", r.status === 404, `status=${r.status}`);

console.log("\n[기록이 실패해도 링크는 살아야 한다]");
const brokenEnv = { ...env, DB: { prepare() { throw new Error("D1 다운"); } } };
r = await worker.fetch(req(`${B}/r?p=taling&s=insight&v=base&a=role`, { "user-agent": "Safari" }), brokenEnv, ctx);
check("D1이 죽어도 302", r.status === 302, `status=${r.status}`);
const noDbEnv = { LINK_SIGN_KEY: "k", PROXY_TOKEN: "tok" };
r = await worker.fetch(req(`${B}/r?p=taling&s=insight&v=base&a=role`, { "user-agent": "Safari" }), noDbEnv, ctx);
check("DB 바인딩이 없어도 302", r.status === 302, `status=${r.status}`);

console.log("\n[카카오 채널 - 실제 주소]");
r = await worker.fetch(req(`${B}/r?p=mf_kakao&s=insight&v=base&a=ask`, { "user-agent": "Safari" }), env, ctx);
check("mf_kakao는 카카오 채널로", (r.headers.get("location") || "") === "https://pf.kakao.com/_RBZrX/friend", r.headers.get("location"));
check("https로 나간다", (r.headers.get("location") || "").startsWith("https://"));

console.log("\n[제휴 링크가 원문 그대로 나가는가 - 수수료가 걸려 있다]");
{
  const EXPECT = {
    book: "https://link.yes24.com/a/LdOL9rzKu3",
    taling: "https://www.taling.me/mkt/rabbit_2",
    mf_kakao: "https://pf.kakao.com/_RBZrX/friend",
  };
  for (const [p, want] of Object.entries(EXPECT)) {
    const rr = await worker.fetch(req(`${B}/r?p=${p}&s=ladder&v=base&a=link`, { "user-agent": "Safari" }), env, ctx);
    const loc = rr.headers.get("location") || "";
    check(`${p}: 주소 한 글자도 안 바뀜`, loc === want, loc);
  }
}

console.log("\n[이미지 경로]");
{
  globalThis.caches = { default: { match: async()=>undefined, put: async()=>{} } };
  const realFetch = globalThis.fetch;
  let asked = null;
  globalThis.fetch = async (u) => { asked = String(u); return new Response("binary", { status: 200 }); };

  let rr = await worker.fetch(req(`${B}/img/book.jpg`), env, ctx);
  check("jpg는 image/jpeg로", rr.headers.get("content-type") === "image/jpeg", rr.headers.get("content-type"));
  check("저장소 경로로 가져온다", (asked||"").endsWith("/docs/img/book.jpg"), asked);
  check("길게 캐시한다", (rr.headers.get("cache-control")||"").includes("604800"));

  rr = await worker.fetch(req(`${B}/img/moneyfreelab.jpg`), env, ctx);
  check("png/jpg 외 확장자만 허용", rr.status === 200);

  // 이미지가 나가지 않는 것이 요건이다. 상태 코드는 경로에 따라 다르다.
  // (../ 는 URL 정규화로 /img/ 밖으로 나가 토큰 검사에서 403으로 막힌다)
  for (const bad of ["../../etc/passwd", "a/b.jpg", "x.svg", "x.html", "%2e%2e%2fsecret.jpg", "x", "x.JPG.svg"]) {
    const br = await worker.fetch(req(`${B}/img/${bad}`), env, ctx);
    const ct = br.headers.get("content-type") || "";
    check(`이미지 안 나감: ${bad}`, br.status !== 200 && !ct.startsWith("image/"), `status=${br.status} ct=${ct}`);
  }
  globalThis.fetch = realFetch;
}

console.log("\n[stats 잠금]");
r = await worker.fetch(req(`${B}/r/stats`), env, ctx);
check("키 없으면 403", r.status === 403, `status=${r.status}`);
r = await worker.fetch(req(`${B}/r/stats?key=tok&from=2026-10-01&to=2026-10-31`), env, ctx);
check("키 맞으면 200", r.status === 200, `status=${r.status}`);

console.log("\n[기존 경로가 안 깨졌나]");
r = await worker.fetch(req(`${B}/brief/k/2026-09-26?e=1&s=x`), env, ctx);
check("잘못된 서명은 여전히 403", r.status === 403, `status=${r.status}`);
r = await worker.fetch(req(`${B}/brief/k/nope`), env, ctx);
check("잘못된 주소는 여전히 404", r.status === 404, `status=${r.status}`);

console.log(`\n통과 ${pass} / 실패 ${fail}`);
process.exit(fail ? 1 : 0);
