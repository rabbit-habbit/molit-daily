"""publ.biz 관리자 콘솔 그룹 채팅방에 주간 국토부 정책 브리핑 링크 자동 게시.

Playwright 기반 브라우저 자동화 (publ은 공식 API 없음).

환경변수:
  PUBL_EMAIL     — 로그인 이메일 (필수)
  PUBL_PASSWORD  — 로그인 비밀번호 (필수)
  PUBL_ROOM_URL  — 게시할 그룹 채팅방 URL (선택, 기본값 아래 상수)
  PUBL_HEADLESS  — 'false' 하면 브라우저 창 표시 (디버깅용, 기본 true)

CLI:
  python pipeline/notify_publ.py --message "🐰 오늘 브리핑 → https://..."
  python pipeline/notify_publ.py --today   # date 기반 자동 메시지 생성
  python pipeline/notify_publ.py --date 2026-08-22
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(override=True)
logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[1]
SESSION_PATH = ROOT / ".publ_session.json"  # 로그인 세션 저장 (.gitignore)

LOGIN_URL = "https://console.publ.biz/"
DEFAULT_ROOM_URL = (
    "https://console.publ.biz/channels/L2NoYW5uZWxzLzIzMDY0"
    "/p-apps/D00003/chat/group/basic/rooms/bbe171db-83d3-42b1-8cc2-041d5dc0e395"
)
PAGES_BASE = "https://rabbit-habbit.github.io/molit-daily"

KST = timezone(timedelta(hours=9))
WEEKDAYS = ["월", "화", "수", "목", "금", "토", "일"]


def _kst_today_str() -> str:
    return datetime.now(KST).strftime("%Y-%m-%d")


# ── 서명 만료 링크 ──────────────────────────────────────────────────
# 유료 멤버십 채팅방에 올리는 링크는 공개 Pages 주소 대신 Cloudflare Worker의
# /brief/{app}/{date}?e=<만료>&s=<hmac> 서명 링크를 쓴다.
# 날짜·만료를 조작하면 서명이 깨져 열리지 않고, 만료(기본 7일) 후엔 죽는다.
# 지난 호는 publ 아티클(멤버십 인증)에서 보는 것이 정석 경로.
WORKER_BASE = "https://molit-proxy.rabbit-habbit.workers.dev"


def _signed_brief_url(app: str, date_iso: str) -> str:
    import hashlib
    import hmac
    import time

    key = os.environ.get("LINK_SIGN_KEY", "")
    if not key:
        raise RuntimeError("LINK_SIGN_KEY 환경변수가 필요합니다 (서명 링크 생성용).")
    ttl_days = int(os.environ.get("LINK_TTL_DAYS", "7"))
    exp = int(time.time()) + ttl_days * 86400
    sig = hmac.new(key.encode(), f"{app}/{date_iso}/{exp}".encode(), hashlib.sha256).hexdigest()[:32]
    return f"{WORKER_BASE}/brief/{app}/{date_iso}?e={exp}&s={sig}"


def _message_for(date: datetime) -> str:
    """발행일 기반 그룹톡 메시지 (M/D 명시 · 서명 만료 링크)."""
    date_short = f"{date.month}/{date.day}"
    date_iso = date.strftime("%Y-%m-%d")
    url = _signed_brief_url("m", date_iso)
    return (
        f"💚 [{date_short} 정책 브리핑] 이번 한 주의 국토부 주요 내용들 "
        f"(어떤 정책들이 나오고 있을까?)\n"
        f"→ {url}"
    )


def _today_message() -> str:
    return _message_for(datetime.now(KST))


def _perform_login(page, email: str, password: str) -> None:
    """publ 로그인. 이메일·비밀번호 form 자동 감지."""
    logger.info("로그인 페이지 이동: %s", LOGIN_URL)
    page.goto(LOGIN_URL, wait_until="networkidle")

    # 이메일 필드 (readonly·autofill 방지용 fake 필드 제외)
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

    # publ이 2단계 로그인이면 여기서 "다음" 버튼을 눌러야 password 필드가 뜸
    next_btn = page.locator(
        'button:has-text("다음"), button:has-text("계속"), button:has-text("Next")'
    ).first
    if next_btn.count() > 0 and next_btn.is_visible():
        next_btn.click()
        page.wait_for_timeout(1_500)

    # 비밀번호 필드 (readonly·autofill 방지 fake 필드 제외 · name=removePassword 배제)
    pw_input = page.locator(
        'input[type="password"]:not([readonly]):not([name="removePassword"]):not([tabindex="-1"])'
    ).first
    pw_input.wait_for(state="visible", timeout=15_000)
    pw_input.click()
    page.wait_for_timeout(200)
    pw_input.fill(password)
    page.wait_for_timeout(300)

    # 로그인 버튼: type=submit 우선, "로그인" 텍스트 fallback
    submit_btn = page.locator('button[type="submit"]:visible').first
    if submit_btn.count() == 0:
        submit_btn = page.get_by_role("button", name="로그인").first
    submit_btn.click()

    # 로그인 후 URL 전환 대기 (console 대시보드로 이동)
    page.wait_for_load_state("networkidle", timeout=20_000)
    page.wait_for_timeout(1_500)
    if "login" in page.url.lower():
        raise RuntimeError(f"로그인 실패로 보임 · 현재 URL: {page.url}")
    logger.info("로그인 성공 → %s", page.url)


def _send_message(page, room_url: str, message: str) -> None:
    logger.info("채팅방 이동: %s", room_url)
    page.goto(room_url, wait_until="networkidle")
    page.wait_for_timeout(1_500)  # 채팅 UI 로딩

    # 메시지 입력창: publ UI가 한/영 오갈 수 있어 다국어 placeholder 커버
    # (한글: "메시지를 입력해 주세요." · 영문: "Enter chat")
    input_box = page.locator(
        'textarea[placeholder*="메시지"], input[placeholder*="메시지"], '
        'textarea[placeholder*="Enter chat"], textarea[placeholder="Enter chat"], '
        'textarea[placeholder*="Type a message"], textarea[placeholder*="chat"], '
        '[contenteditable="true"]'
    ).first
    input_box.wait_for(state="visible", timeout=15_000)
    input_box.click()
    input_box.fill(message)
    page.wait_for_timeout(300)

    # 전송: Enter 시도 → 실패 시 전송 버튼 클릭
    try:
        input_box.press("Enter")
        page.wait_for_timeout(2_000)
    except Exception:
        pass

    # Enter가 안 먹으면 (일부 에디터는 Enter=줄바꿈) 버튼 클릭
    # 이미 전송됐으면 input이 비어있을 것
    remaining = input_box.input_value() if hasattr(input_box, "input_value") else ""
    if remaining and message in remaining:
        logger.info("Enter로 전송 안 됨 · 전송 버튼 클릭 시도")
        send_btn = page.locator(
            'button[aria-label*="전송"], button[aria-label*="Send"], button[type="submit"]'
        ).first
        if send_btn.count() > 0:
            send_btn.click()
            page.wait_for_timeout(2_000)

    logger.info("메시지 전송 완료")


def post_daily(
    message: str,
    room_url: str | None = None,
    headless: bool = True,
    use_session: bool = True,
) -> None:
    """publ 채팅방에 메시지 게시.

    - 세션 파일 있으면 로그인 skip → 방으로 바로 이동
    - 세션 만료·오류 시 재로그인
    """
    from playwright.sync_api import sync_playwright  # 지연 import (설치 안 된 환경 배려)

    email = os.environ.get("PUBL_EMAIL")
    password = os.environ.get("PUBL_PASSWORD")
    if not email or not password:
        raise RuntimeError("PUBL_EMAIL / PUBL_PASSWORD 환경변수가 필요합니다.")

    room_url = room_url or os.environ.get("PUBL_ROOM_URL", DEFAULT_ROOM_URL)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        session_available = use_session and SESSION_PATH.exists()
        # publ은 브라우저 로케일에 따라 UI 언어를 바꿈 → 한글로 고정
        context_kwargs = {
            "viewport": {"width": 1280, "height": 900},
            "locale": "ko-KR",
            "extra_http_headers": {"Accept-Language": "ko-KR,ko;q=0.9,en;q=0.5"},
        }
        if session_available:
            context_kwargs["storage_state"] = str(SESSION_PATH)
        context = browser.new_context(**context_kwargs)
        page = context.new_page()

        try:
            if session_available:
                logger.info("세션 파일 로드: %s", SESSION_PATH.name)
                page.goto(room_url, wait_until="networkidle")
                page.wait_for_timeout(1_500)
                # 세션 만료 감지: 로그인 페이지로 리다이렉트됐으면 재로그인
                if "login" in page.url.lower() or "console.publ.biz" not in page.url:
                    logger.warning("세션 만료 · 재로그인")
                    _perform_login(page, email, password)
                    page.goto(room_url, wait_until="networkidle")
                    page.wait_for_timeout(1_500)
            else:
                _perform_login(page, email, password)

            _send_message(page, room_url, message)

            # 세션 저장 (다음 실행에서 로그인 skip)
            context.storage_state(path=str(SESSION_PATH))
            logger.info("세션 저장: %s", SESSION_PATH.name)
        finally:
            browser.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--message", help="게시할 텍스트")
    parser.add_argument("--today", action="store_true", help="오늘 날짜 기반 자동 메시지")
    parser.add_argument("--date", help="YYYY-MM-DD (해당 발행일 메시지)")
    parser.add_argument("--room-url", help="publ 방 URL override")
    parser.add_argument("--no-session", action="store_true", help="세션 파일 무시 · 매번 재로그인")
    parser.add_argument("--headed", action="store_true", help="브라우저 창 표시 (디버깅)")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    if args.date:
        message = _message_for(datetime.strptime(args.date, "%Y-%m-%d").replace(tzinfo=KST))
    elif args.today:
        message = _today_message()
    elif args.message:
        message = args.message
    else:
        parser.error("--message · --today · --date 중 하나 필요")

    logger.info("메시지:\n%s\n", message)
    headless = not args.headed
    if os.environ.get("PUBL_HEADLESS", "").lower() == "false":
        headless = False

    post_daily(
        message=message,
        room_url=args.room_url,
        headless=headless,
        use_session=not args.no_session,
    )
    print("✅ publ 게시 완료")
