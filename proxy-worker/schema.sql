-- 상위 퍼널 클릭 기록 (D1)
--
-- 생성:
--   npx wrangler d1 create rh-clicks
--   npx wrangler d1 execute rh-clicks --remote --file=schema.sql
--
-- 4개 파라미터를 각각 별도 컬럼으로 둔다. 하나로 합치면 나중에 지면별·
-- 각도별로 쪼갤 수 없고, 합계만 남은 데이터는 쓸모가 없다.

CREATE TABLE IF NOT EXISTS clicks (
  id   INTEGER PRIMARY KEY AUTOINCREMENT,
  ts   INTEGER NOT NULL,          -- unix seconds (UTC)
  day  TEXT    NOT NULL,          -- KST 기준 YYYY-MM-DD
  p    TEXT    NOT NULL,          -- 상품  book | mf_vote | mf_sale | taling
  s    TEXT    NOT NULL,          -- 지면  basic | insight | footer | article | news
  v    TEXT    NOT NULL,          -- 변형  base | e01a ...
  a    TEXT    NOT NULL,          -- 각도  role | link | ask | quote
  ok   INTEGER NOT NULL DEFAULT 1,-- 0이면 s/v/a 중 규격을 벗어난 값이 왔다는 뜻
  bot  INTEGER NOT NULL DEFAULT 0,-- 1이면 봇·프리페치로 판정
  ipx  TEXT,                      -- 소금 친 IP 해시 앞 16자. 원문 IP는 저장하지 않는다
  ua   TEXT                       -- 판정 근거 확인용
);

CREATE INDEX IF NOT EXISTS idx_clicks_day ON clicks (day);
CREATE INDEX IF NOT EXISTS idx_clicks_dim ON clicks (day, p, s, a, v);

-- ── 자주 쓰는 조회 ──────────────────────────────────────────────────
--
-- 월간 상위 퍼널 클릭(North Star). 봇과 규격 위반은 빼고 센다.
--   SELECT COUNT(*) FROM clicks
--    WHERE day BETWEEN '2026-10-01' AND '2026-10-31' AND bot = 0 AND ok = 1;
--
-- 지면별 (같은 상품 안에서만 비교할 것. 상품이 다르면 자리 차이와
--  상품 차이가 섞여 순위가 의미를 잃는다)
--   SELECT s, COUNT(*) n FROM clicks
--    WHERE p = 'book' AND bot = 0 AND ok = 1 GROUP BY s ORDER BY n DESC;
--
-- 규격 위반 감시. 0이 아니면 링크 생성 쪽에 버그가 있다는 신호다.
--   SELECT day, p, s, v, a, COUNT(*) FROM clicks
--    WHERE ok = 0 GROUP BY day, p, s, v, a;
--
-- 같은 사람 연타 제거가 필요할 때 (같은 분 안의 같은 조합을 1건으로)
--   SELECT COUNT(*) FROM (
--     SELECT DISTINCT ipx, p, s, ts / 60 FROM clicks WHERE bot = 0 AND ok = 1
--   );
