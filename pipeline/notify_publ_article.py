"""publ.biz 아티클 앱에 주간 국토부 정책 브리핑을 자동 등록.

흐름:
  1. 그 주 exports/briefing-{date}-inline.html 로드 (공개용 · 얼리버드 카드 없음)
  2. Playwright로 HTML 렌더 → 썸네일(.news-card 첫 요소 정사각) + 커버(.brief-header 가로배너) 스크린샷
  3. publ 로그인(세션 재사용) → 아티클 목록 → "새 글" 클릭
  4. 제목 입력 → Source code 모달로 HTML 붙여넣기 → 우측 공개·카테고리 설정
  5. 이미지 탭에서 썸네일·커버 업로드 → "생성" 클릭

환경변수:
  PUBL_EMAIL / PUBL_PASSWORD  — 로그인 (세션 만료 시 fallback)
  PUBL_CATALOG_LABEL          — 유료 카탈로그 행 이름 (기본 "유료 콘텐츠들(경제)")
  PUBL_HEADLESS               — 'false' → 브라우저 창 표시 (디버깅)

CLI:
  python pipeline/notify_publ_article.py --today
  python pipeline/notify_publ_article.py --date 2026-08-22
  python pipeline/notify_publ_article.py --html-path exports/xxx.html --title "💚 ..."
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(override=True)
logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[1]
SESSION_PATH = ROOT / ".publ_session.json"

LOGIN_URL = "https://console.publ.biz/"
LIST_URL = "https://console.publ.biz/channels/L2NoYW5uZWxzLzIzMDY0/p-apps/B00001/posts"
MONETIZE_URL = "https://console.publ.biz/channels/L2NoYW5uZWxzLzIzMDY0/p-apps/B00001/monetization/catalogs"
# 유료 카탈로그 행 이름 (publ 콘솔 표기와 정확히 일치해야 함).
# 정책 브리핑도 경제 데일리와 같은 카탈로그를 쓴다 (2026-08-19 대표 확인).
CATALOG_ROW_LABEL = os.environ.get("PUBL_CATALOG_LABEL", "유료 콘텐츠들(경제)").strip()

KST = timezone(timedelta(hours=9))
WEEKDAYS = ["월", "화", "수", "목", "금", "토", "일"]

CATEGORY_LABEL = "💚 정책 브리핑"


def _kst_today() -> datetime:
    return datetime.now(KST)


def _default_title(date: datetime) -> str:
    # 기존 수동 등록 아티클과 동일한 형식 (예: "💚 8/15 한 주의 정책 브리핑")
    return f"💚 {date.month}/{date.day} 한 주의 정책 브리핑"


def _default_html_path(date: datetime) -> Path:
    """공개용 인라인 버전 (exports/) - 얼리버드 카드가 없는 채널이다."""
    return ROOT / "exports" / f"briefing-{date.strftime('%Y-%m-%d')}-inline.html"


def _render_and_capture(html_path: Path, out_dir: Path) -> tuple[Path, Path]:
    """HTML을 렌더해서 썸네일(정사각) + 커버(가로) PNG 저장. (thumb_path, cover_path) 반환."""
    from playwright.sync_api import sync_playwright

    out_dir.mkdir(parents=True, exist_ok=True)
    thumb_path = out_dir / "thumbnail.png"
    cover_path = out_dir / "cover.png"

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": 720, "height": 1200},
            device_scale_factor=2,  # 고해상도
        )
        page = context.new_page()
        page.goto(f"file://{html_path.resolve()}", wait_until="networkidle")
        page.wait_for_timeout(800)

        # 1) 커버: .brief-header (인라인 HTML은 <header> 태그를 쓰지 않음)
        header = page.locator(".brief-header").first
        header.screenshot(path=str(cover_path))
        logger.info("✓ 커버 저장: %s", cover_path.name)

        # 2) 썸네일: 첫 .news-card, 정사각 crop
        card = page.locator(".news-card").first
        box = card.bounding_box()
        if not box:
            raise RuntimeError(".news-card bounding_box 못 잡음")
        # 정사각 = min(w, h) — 카드 상단부터 정사각으로 잘라내기
        side = min(box["width"], box["height"])
        page.screenshot(
            path=str(thumb_path),
            clip={"x": box["x"], "y": box["y"], "width": side, "height": side},
        )
        logger.info("✓ 썸네일 저장: %s (%.0fx%.0f)", thumb_path.name, side, side)

        browser.close()

    return thumb_path, cover_path


def _perform_login(page, email: str, password: str) -> None:
    """publ 로그인 (readonly·autofill 방지 셀렉터 사용)."""
    logger.info("로그인 시도")
    page.goto(LOGIN_URL, wait_until="networkidle")

    email_input = page.locator(
        'input[type="email"]:not([readonly]), '
        'input[name="email"]:not([readonly]):not([name="removeEmail"]), '
        'input[autocomplete="username"]:not([readonly]), '
        'input[placeholder*="이메일"]:not([readonly]), '
        'input[placeholder*="아이디"]:not([readonly])'
    ).first
    email_input.wait_for(state="visible", timeout=15_000)
    email_input.click()
    page.wait_for_timeout(200)
    email_input.fill(email)
    page.wait_for_timeout(400)

    next_btn = page.locator(
        'button:has-text("다음"), button:has-text("계속"), button:has-text("Next")'
    ).first
    if next_btn.count() > 0 and next_btn.is_visible():
        next_btn.click()
        page.wait_for_timeout(1_500)

    pw_input = page.locator(
        'input[type="password"]:not([readonly]):not([name="removePassword"]):not([tabindex="-1"])'
    ).first
    pw_input.wait_for(state="visible", timeout=15_000)
    pw_input.click()
    page.wait_for_timeout(200)
    pw_input.fill(password)
    page.wait_for_timeout(300)

    submit_btn = page.locator('button[type="submit"]:visible').first
    if submit_btn.count() == 0:
        submit_btn = page.get_by_role("button", name="로그인").first
    submit_btn.click()

    page.wait_for_load_state("networkidle", timeout=20_000)
    page.wait_for_timeout(1_500)
    if "login" in page.url.lower():
        raise RuntimeError(f"로그인 실패 · URL: {page.url}")
    logger.info("로그인 성공 → %s", page.url)


def _create_article(page, title: str, html_body: str, thumb: Path, cover: Path, dry_run: bool = False) -> None:
    """새 아티클 생성 → 저장까지."""
    # 목록 → 새 글
    logger.info("아티클 목록 이동")
    page.goto(LIST_URL, wait_until="networkidle")
    page.wait_for_timeout(2_000)

    logger.info("'새 글 / Create Post' 클릭")
    page.locator('button:has-text("새 글"), button:has-text("Create Post")').first.click()
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(2_500)

    # 제목 (한/영 UI 커버)
    logger.info("제목 입력: %s", title)
    title_input = page.locator(
        'textarea[placeholder*="제목"], textarea[placeholder*="Title"], '
        'textarea[placeholder*="title"], input[placeholder*="제목"], input[placeholder*="Title"]'
    ).first
    title_input.wait_for(state="visible", timeout=10_000)
    title_input.fill(title)
    page.wait_for_timeout(400)

    # Source code 모달 → HTML 붙여넣기 → Save
    logger.info("Source code 모달 열기")
    page.locator('button[aria-label="Source code"]').click()
    page.wait_for_timeout(1_200)

    ta = page.locator('.tox-dialog textarea').first
    ta.wait_for(state="visible", timeout=8_000)
    # 큰 HTML은 fill이 느리므로 JS로 직접 세팅 후 change 이벤트
    ta.evaluate("(el, v) => { el.value = v; el.dispatchEvent(new Event('input', {bubbles:true})); }", html_body)
    page.wait_for_timeout(500)

    logger.info("Source code Save 클릭")
    page.locator('.tox-dialog button:has-text("Save")').click()
    page.wait_for_timeout(1_500)

    # 공개 상태: 비공개/Private → 공개/Public
    logger.info("공개 상태 = 공개")
    dropdown = page.locator(
        'div[class*="container"]:has-text("비공개"), div[class*="container"]:has-text("Private")'
    ).first
    dropdown.click()
    page.wait_for_timeout(600)
    page.locator('li:has-text("공개"), li:has-text("Public")').first.click()
    page.wait_for_timeout(600)

    # 카테고리 = 💙 경제 브리핑 (카테고리 라벨은 자체 텍스트라 언어 무관)
    logger.info("카테고리 = %s", CATEGORY_LABEL)
    cat_dd = page.locator(
        'div[class*="container"]:has-text("선택하지 않음"), div[class*="container"]:has-text("Not selected")'
    ).first
    cat_dd.click()
    page.wait_for_timeout(600)
    page.locator(f'li:has-text("{CATEGORY_LABEL}")').first.click()
    page.wait_for_timeout(600)

    # 이미지 탭 (한/영)
    logger.info("이미지 탭 이동 → 썸네일·커버 업로드")
    img_tab = page.locator(
        ':is(button, [role="tab"], div, a):has-text("이미지"), '
        ':is(button, [role="tab"], div, a):has-text("Image")'
    )
    # 위 셀렉터는 부모까지 잡힐 수 있어 텍스트 정확 매칭 우선 시도
    exact = page.get_by_text("이미지", exact=True).or_(page.get_by_text("Image", exact=True)).first
    (exact if exact.count() > 0 else img_tab.first).click()
    page.wait_for_timeout(1_000)

    file_inputs = page.locator('input[type="file"]')
    n = file_inputs.count()
    if n < 2:
        raise RuntimeError(f"이미지 탭 file input {n}개 · 2개 필요")
    # 순서: 첫 번째 = 썸네일, 두 번째 = 커버 (탐색 시 확인됨)
    file_inputs.nth(0).set_input_files(str(thumb))
    page.wait_for_timeout(3_000)  # 썸네일 서버 업로드 대기
    file_inputs.nth(1).set_input_files(str(cover))
    page.wait_for_timeout(5_000)  # 커버 서버 업로드 대기 (이전 2초는 너무 짧아 실패 유발)
    # 업로드 트래픽 정리 대기 (이미지 크면 몇 초 더 필요)
    try:
        page.wait_for_load_state("networkidle", timeout=15_000)
    except Exception:
        logger.warning("이미지 업로드 networkidle 대기 타임아웃 · 계속 진행")

    # 생성 (dry-run이면 클릭 건너뛰고 스크린샷만)
    if dry_run:
        shot = ROOT / ".publ_images" / "dry_run_preview.png"
        page.screenshot(path=str(shot), full_page=True)
        logger.info("🧪 DRY-RUN · 생성 버튼은 클릭하지 않음. 스크린샷: %s", shot)
        return

    logger.info("생성 버튼 클릭")
    create_btn = page.locator('button:has-text("생성"), button:has-text("Create")').first
    create_btn.wait_for(state="visible", timeout=10_000)
    # 생성 버튼이 실제로 enabled 될 때까지 대기 (이미지 서버 처리 완료 시그널)
    for _ in range(20):
        if create_btn.is_enabled():
            break
        page.wait_for_timeout(500)
    else:
        raise RuntimeError("생성 버튼 10초간 비활성 · 이미지 업로드 미완료 또는 필수 필드 누락")

    # 진단: 클릭 직전 상태 저장 (폼 완성도 확인용)
    dbg = ROOT / ".publ_images"
    dbg.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(dbg / "before_create.png"), full_page=True)

    # 클릭 재시도 루프: publ 생성 버튼은 첫 .click()이 이벤트로 발화 안 되는 케이스 관찰됨
    # (버튼 활성, 스크린샷도 동일, 네트워크 요청 0건). hover → click → force click 순으로 시도.
    def _url_left_create() -> bool:
        return not page.url.rstrip("/").endswith("/posts/create")

    click_success = False
    for attempt, method in enumerate([
        lambda: create_btn.click(),
        lambda: (create_btn.hover(), page.wait_for_timeout(300), create_btn.click()),
        lambda: create_btn.click(force=True),
        lambda: create_btn.dispatch_event("click"),
    ], start=1):
        logger.info("생성 클릭 시도 #%d", attempt)
        try:
            method()
        except Exception as e:
            logger.warning("클릭 방법 #%d 예외: %s", attempt, e)
            continue
        try:
            page.wait_for_url(lambda u: not u.rstrip("/").endswith("/posts/create"), timeout=10_000)
            click_success = True
            break
        except Exception:
            logger.warning("클릭 #%d 후 URL 변화 없음", attempt)
            page.wait_for_timeout(1_500)

    if click_success:
        page.wait_for_load_state("networkidle", timeout=15_000)
        page.wait_for_timeout(1_500)
    logger.info("생성 후 URL: %s", page.url)

    # 발행 성공 검증: URL이 /posts/create에서 벗어나야 함
    if page.url.rstrip("/").endswith("/posts/create"):
        # 에러 dialog / 배너 텍스트 수집 (원인 진단용)
        try:
            body_text = page.evaluate("() => document.body.innerText")[:3000]
        except Exception:
            body_text = "(innerText 추출 실패)"
        (dbg / "after_create_bodytext.txt").write_text(body_text, encoding="utf-8")
        page.screenshot(path=str(dbg / "create_failed.png"), full_page=True)
        # 페이지 HTML도 덤프 (에러 요소 찾기용)
        try:
            (dbg / "create_failed.html").write_text(page.content(), encoding="utf-8")
        except Exception:
            pass
        raise RuntimeError(
            f"발행 실패 · URL이 create 페이지 그대로: {page.url} · "
            f"스크린샷/텍스트/HTML: {dbg}"
        )
    logger.info("✓ 발행 완료")


def _link_to_paid_catalog(page, article_title: str) -> None:
    """유료 콘텐츠 카테고리에 오늘 아티클 연결.

    수익창출 → 카탈로그 행 hover → 콘텐츠 관리(Contents) → +추가(Add) →
    모달에서 오늘 제목 체크 → Add N Post(s) 클릭.
    """
    logger.info("=== 유료 콘텐츠 카테고리 연결 ===")
    if not CATALOG_ROW_LABEL:
        raise RuntimeError(
            "PUBL_CATALOG_LABEL 환경변수가 비어 있습니다 - "
            "publ 콘솔 '수익창출 > 카탈로그'의 정책용 행 이름을 그대로 넣어주세요."
        )
    page.goto(MONETIZE_URL, wait_until="domcontentloaded", timeout=45_000)
    page.wait_for_timeout(2_500)

    # 행 hover → 우측 액션(Contents / 콘텐츠 관리) 노출
    logger.info("카탈로그 행 hover: %s", CATALOG_ROW_LABEL)
    row = page.locator(f'div:has-text("{CATALOG_ROW_LABEL}")').first
    row.hover()
    page.wait_for_timeout(600)

    # "콘텐츠 관리" / "Contents" 링크 클릭
    contents_link = page.get_by_text("콘텐츠 관리", exact=True).or_(
        page.get_by_text("콘텐츠관리", exact=True)
    ).or_(page.get_by_text("Contents", exact=True)).first
    contents_link.wait_for(state="visible", timeout=10_000)
    contents_link.click()
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(2_000)

    # +추가 / Add 버튼 (우측 하단)
    logger.info("추가 버튼 클릭")
    # 페이지 상단에도 '추가하기' 같은 유사 텍스트가 있을 수 있어 last 사용
    add_btn = page.locator(
        'button:has-text("+ 추가"), button:has-text("추가"), '
        'button:has-text("+ Add"), button:has-text("Add")'
    ).last
    add_btn.click()
    page.wait_for_timeout(1_500)

    # 모달: 오늘 아티클 행 클릭 (체크박스 커스텀 UI)
    logger.info("모달에서 오늘 아티클 체크: %s", article_title)
    target_row = page.locator(f'div:has-text("{article_title}")').first
    try:
        target_row.wait_for(state="visible", timeout=8_000)
    except Exception:
        # 모달 리스트 로딩이 늦거나 스크롤이 필요할 수 있음 → scroll_into_view 시도
        logger.info("첫 시도 실패 · scroll_into_view 후 재시도")
        page.wait_for_timeout(2_000)
        try:
            target_row.scroll_into_view_if_needed(timeout=5_000)
        except Exception:
            pass
        target_row.wait_for(state="visible", timeout=8_000)
    target_row.click()
    page.wait_for_timeout(500)

    # 확인: "Add 1 Post(s)" / "포스트 1개 추가하기"
    confirm = page.locator(
        'button:has-text("포스트 1개 추가하기"), button:has-text("포스트"), '
        'button:has-text("Add 1 Post"), button:has-text("Add ")'
    ).last
    confirm.wait_for(state="visible", timeout=5_000)
    if not confirm.is_enabled():
        raise RuntimeError("확인 버튼 비활성 · 체크 안 됨")
    confirm.click()
    page.wait_for_timeout(2_500)
    logger.info("✓ 유료 콘텐츠 연결 완료")


def post_article(
    html_path: Path,
    title: str,
    headless: bool = True,
    use_session: bool = True,
    dry_run: bool = False,
    link_only: bool = False,
) -> None:
    from playwright.sync_api import sync_playwright

    email = os.environ.get("PUBL_EMAIL")
    password = os.environ.get("PUBL_PASSWORD")
    if not email or not password:
        raise RuntimeError("PUBL_EMAIL / PUBL_PASSWORD 필요")

    if not link_only:
        if not html_path.exists():
            raise RuntimeError(f"HTML 없음: {html_path}")
        html_body = html_path.read_text(encoding="utf-8")
        logger.info("HTML 로드: %s (%d bytes)", html_path.name, len(html_body))

        # 이미지 생성
        logger.info("=== 썸네일·커버 캡처 ===")
        tmp_dir = ROOT / ".publ_images"
        thumb, cover = _render_and_capture(html_path, tmp_dir)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        session_available = use_session and SESSION_PATH.exists()
        # publ은 브라우저 로케일에 따라 UI 언어를 바꿈 → 한글로 고정
        ctx_kwargs = {
            "viewport": {"width": 1440, "height": 900},
            "locale": "ko-KR",
            "extra_http_headers": {"Accept-Language": "ko-KR,ko;q=0.9,en;q=0.5"},
        }
        if session_available:
            ctx_kwargs["storage_state"] = str(SESSION_PATH)
        context = browser.new_context(**ctx_kwargs)
        page = context.new_page()

        try:
            initial_url = MONETIZE_URL if link_only else LIST_URL
            if session_available:
                logger.info("세션 로드: %s", SESSION_PATH.name)
                # networkidle은 광고·마케팅 픽셀 때문에 안 잡히는 경우 있음 → domcontentloaded 사용
                page.goto(initial_url, wait_until="domcontentloaded", timeout=45_000)
                page.wait_for_timeout(2_500)
                if "login" in page.url.lower() or "console.publ.biz" not in page.url:
                    logger.warning("세션 만료 · 재로그인")
                    _perform_login(page, email, password)
            else:
                _perform_login(page, email, password)

            if not link_only:
                _create_article(page, title, html_body, thumb, cover, dry_run=dry_run)

            # dry-run이 아니면 유료 콘텐츠 카테고리에 연결
            if not dry_run:
                try:
                    _link_to_paid_catalog(page, title)
                except Exception as exc:
                    logger.error("유료 콘텐츠 연결 실패 (아티클 자체는 등록됨): %s", exc)
                    raise

            context.storage_state(path=str(SESSION_PATH))
            logger.info("세션 저장: %s", SESSION_PATH.name)
        finally:
            browser.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--today", action="store_true", help="오늘(KST) 기준 자동")
    parser.add_argument("--date", help="YYYY-MM-DD (지정한 날 브리핑)")
    parser.add_argument("--html-path", help="인라인 HTML 파일 override")
    parser.add_argument("--title", help="제목 override")
    parser.add_argument("--no-session", action="store_true")
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--dry-run", action="store_true", help="생성 버튼 누르지 않고 스크린샷만")
    parser.add_argument("--link-only", action="store_true", help="아티클 등록 skip · 유료 카테고리 연결만")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    if args.date:
        d = datetime.strptime(args.date, "%Y-%m-%d").replace(tzinfo=KST)
    else:
        d = _kst_today()

    html_path = Path(args.html_path) if args.html_path else _default_html_path(d)
    title = args.title or _default_title(d)

    logger.info("제목: %s", title)
    logger.info("HTML: %s", html_path)

    headless = not args.headed
    if os.environ.get("PUBL_HEADLESS", "").lower() == "false":
        headless = False

    post_article(
        html_path=html_path, title=title, headless=headless,
        use_session=not args.no_session, dry_run=args.dry_run,
        link_only=args.link_only,
    )
    if args.dry_run:
        print("🧪 DRY-RUN 완료")
    elif args.link_only:
        print("✅ 유료 카테고리 연결 완료")
    else:
        print("✅ publ 아티클 등록 + 유료 연결 완료")
