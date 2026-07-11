# molit-daily

국토교통부 보도자료 중 **조회수가 임계값을 넘은 화제 정책 보도**를 주 1회 스크랩·요약하는 "이번 주 핫한 국토부 정책" 위클리 브리핑. (kyungje-daily와 같은 구조, 주중 경제 데일리를 보완하는 토요일 발행)

매주 토요일 아침 목록을 스캔해 조회수 기준을 새로 넘은 보도자료를 골라, 첨부 PDF를 Claude API로 요약하고 HTML 브리핑을 GitHub Pages로 배포 + 카카오톡 알림.

## 동작 방식

1. `lst.jsp` 목록 1~12페이지(최근 약 6주) 스캔 — 제목·분야·등록일·**조회수**
2. `VIEW_THRESHOLD`(기본 3,000회) 이상 & 아직 보고 안 한 게시물 선별
   - 보고 이력은 `state/reported.json`에 기록 → 중복 보고 없음 (멱등)
   - `[장관동정]`·`[인사]` 등 의전성 게시물은 `EXCLUDE_TITLE_RE`로 제외
   - 1회 최대 `MAX_ITEMS_PER_RUN`(기본 7)건 — 첫 실행 백로그 폭주 방지
3. 상세 페이지에서 담당부서·첨부 PDF 추출 → PDF 다운로드
4. Claude API로 요약 (핵심 한줄 / 요약 / 핵심 수치 / 영향 / 체크포인트 / 래빗해빛의 해석)
5. `docs/index.html` + `docs/archive/{날짜}.html` 렌더 → git push → 카카오톡

> **왜 10,000회가 아니라 3,000회?** 2026-07 기준 최근 6주(120건) 중 조회수
> 10,000회를 넘은 보도자료는 0건, 최고가 ~5,200회였음. 3,000회 ≈ 상위 15%
> (주 2~3건). 기준을 바꾸려면 `.env`와 `daily.yml`의 `VIEW_THRESHOLD` 수정.

## 구조

```
molit-daily/
├── pipeline/
│   ├── molit_client.py     # 목록/상세 크롤링 + PDF 다운로드 (WAF 쿠키 대응)
│   ├── summarize.py        # Claude API PDF 요약
│   ├── render_report.py    # Jinja2 HTML 렌더링
│   ├── notify_kakao.py     # 카카오톡 나에게 보내기
│   ├── run.py              # 오케스트레이터
│   └── check_api_key.py    # ANTHROPIC_API_KEY 검증
├── templates/report.html.j2
├── state/reported.json     # 보고 이력 (git 커밋 대상 — Actions 멱등성의 핵심)
├── docs/                   # GitHub Pages 루트 (/docs)
├── out/                    # 중간 산출물 (git ignore)
└── .github/workflows/weekly.yml   # 매주 토요일 아침 (KST) 실행
```

## 셋업 (1회)

```bash
# 1. venv + 패키지
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 2. API 키
cp .env.example .env
# .env 열어서 ANTHROPIC_API_KEY 입력 (kyungje-daily 것 재사용 가능)
python pipeline/check_api_key.py

# 3. (알림 원하면) 카카오 토큰 — kyungje-daily의 .kakao_tokens.json 복사하거나
#    python pipeline/notify_kakao.py authorize-url 부터 새로 발급
```

### 자동화 (로컬 launchd — 현재 운영 방식)

⚠️ **GitHub Actions로는 실행 불가**: molit.go.kr이 해외 IP를 차단함 (2026-07-11
확인, 연결 타임아웃). 그래서 정기 실행은 이 맥의 launchd가 담당하고, GitHub은
보고서 호스팅(Pages)에만 사용.

- 스케줄: `~/Library/LaunchAgents/com.rabbithabbit.molit-weekly.plist`
  — **매주 토요일 09:37 KST** `pipeline/run.py --push` 실행
- 로그: `out/launchd.log`
- 토요일 아침에 맥이 잠자기 상태면 깨어날 때 실행됨 (전원 꺼짐이면 그 주는 skip)

```bash
# 상태 확인 / 수동 실행 / 해제
launchctl list | grep molit
launchctl kickstart gui/$(id -u)/com.rabbithabbit.molit-weekly
launchctl bootout gui/$(id -u)/com.rabbithabbit.molit-weekly
```

### GitHub Pages (호스팅)

- 저장소: https://github.com/rabbit-habbit/molit-daily (Pages: `main` / `/docs`)
- 보고서 URL: https://rabbit-habbit.github.io/molit-daily/
- 계정/저장소 이름이 다르면 `pipeline/run.py`의 `PAGES_BASE` 수정
- Actions의 `MOLIT Weekly Brief` 워크플로는 수동 재시도용으로만 남아 있음
  (스케줄 없음, 해외 IP 차단으로 실패함)

## 사용

```bash
source .venv/bin/activate
python pipeline/run.py                    # 로컬 실행 (push 없이)
python pipeline/run.py --push             # + git push
python pipeline/run.py --threshold 5000   # 기준 임시 변경
python pipeline/run.py --no-notify        # 카카오 알림 끄기

# 단위 테스트
python pipeline/molit_client.py --pages 2         # 목록 크롤링만
python pipeline/molit_client.py --detail 95092208 # 상세 1건
python pipeline/render_report.py --mock           # mock 렌더 미리보기
```

## 비용 (대략)

보도자료 1건 요약 = PDF 1개 입력 (~5-30k tokens) → sonnet 기준 회당 $0.03~0.15.
주 1회 발행 · 회당 2~3건 페이스면 **월 $1~2 수준**.

## 알려진 이슈

- molit.go.kr은 WAF가 첫 요청에 307 + `TMOSHCooKie` 쿠키를 요구.
  `requests.Session`이 자동 처리하지만, WAF 정책이 바뀌면 `molit_client.py` 수정 필요.
- GitHub Actions 러너는 해외 IP라 정부 사이트가 차단할 가능성 있음.
  차단되면 로컬 cron으로 전환:
  `37 9 * * 6 cd /path/to/molit-daily && .venv/bin/python pipeline/run.py --push`
- 본문이 hwpx 첨부에만 있고 PDF가 없는 게시물은 제목 기반 축소 요약으로 대체됨.
- 조회수는 목록 페이지 기준. 스캔 범위(12페이지) 밖으로 밀려난 뒤 임계값을
  넘는 게시물은 놓칠 수 있음 (오래된 글이라 실익 낮음).
