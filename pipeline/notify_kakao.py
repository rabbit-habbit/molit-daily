"""카카오톡 '나에게 보내기' 알림.

OAuth 흐름:
  최초 1회) authorize-url 출력 → 브라우저 동의 → redirect_uri의 ?code= 추출
  최초 1회) python pipeline/notify_kakao.py exchange-code CODE
            → access_token + refresh_token 발급 → .kakao_tokens.json 저장
  이후) send 호출 시 refresh_token으로 access_token 자동 갱신

토큰 로드 우선순위:
  1. 환경변수 KAKAO_REFRESH_TOKEN (GitHub Actions 용)
  2. 로컬 .kakao_tokens.json (개발자 머신 용)

환경변수:
  KAKAO_REST_API_KEY        (필수)
  KAKAO_REDIRECT_URI        (선택, 기본 https://localhost:3000/callback)
  KAKAO_REFRESH_TOKEN       (선택, Actions에서 우선 사용)
  REPORT_URL                (선택, send 기본 링크)
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import urllib.parse
from pathlib import Path
from typing import Optional

import requests
from dotenv import load_dotenv

load_dotenv(override=True)
logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[1]
TOKENS_PATH = ROOT / ".kakao_tokens.json"

OAUTH_AUTHORIZE = "https://kauth.kakao.com/oauth/authorize"
OAUTH_TOKEN = "https://kauth.kakao.com/oauth/token"
SEND_TO_ME = "https://kapi.kakao.com/v2/api/talk/memo/default/send"
DEFAULT_REDIRECT = "https://localhost:3000/callback"


def _client_id() -> str:
    key = os.environ.get("KAKAO_REST_API_KEY")
    if not key:
        raise RuntimeError(
            "KAKAO_REST_API_KEY가 설정되지 않았습니다 (.env 또는 환경변수 확인)"
        )
    return key


def _redirect_uri() -> str:
    return os.environ.get("KAKAO_REDIRECT_URI", DEFAULT_REDIRECT)


def _client_secret() -> Optional[str]:
    """카카오 콘솔에서 Client Secret을 '사용함'으로 설정한 경우에만 필요."""
    return os.environ.get("KAKAO_CLIENT_SECRET") or None


# ── OAuth ─────────────────────────────────────────────────────────────


def authorize_url() -> str:
    params = {
        "client_id": _client_id(),
        "redirect_uri": _redirect_uri(),
        "response_type": "code",
        "scope": "talk_message",
    }
    return f"{OAUTH_AUTHORIZE}?{urllib.parse.urlencode(params)}"


def exchange_code(code: str) -> dict:
    """authorization_code → access_token + refresh_token."""
    data = {
        "grant_type": "authorization_code",
        "client_id": _client_id(),
        "redirect_uri": _redirect_uri(),
        "code": code,
    }
    secret = _client_secret()
    if secret:
        data["client_secret"] = secret
    r = requests.post(OAUTH_TOKEN, data=data, timeout=15)
    if r.status_code != 200:
        raise RuntimeError(f"토큰 교환 실패: {r.status_code} {r.text}")
    tokens = r.json()
    if "access_token" not in tokens or "refresh_token" not in tokens:
        raise RuntimeError(f"응답에 토큰 누락: {tokens}")
    _save_tokens(tokens)
    return tokens


def _load_tokens() -> Optional[dict]:
    if not TOKENS_PATH.exists():
        return None
    try:
        return json.loads(TOKENS_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def _save_tokens(tokens: dict) -> None:
    TOKENS_PATH.write_text(
        json.dumps(tokens, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _resolve_refresh_token() -> str:
    # 1) 환경변수 우선 (Actions)
    env_token = os.environ.get("KAKAO_REFRESH_TOKEN")
    if env_token:
        return env_token
    # 2) 로컬 파일
    stored = _load_tokens()
    if stored and stored.get("refresh_token"):
        return stored["refresh_token"]
    raise RuntimeError(
        "refresh_token이 없습니다. 먼저 exchange-code로 발급하세요."
    )


def refresh_access_token() -> dict:
    """refresh_token → 새 access_token (+ 만료 임박 시 새 refresh_token)."""
    refresh = _resolve_refresh_token()
    data = {
        "grant_type": "refresh_token",
        "client_id": _client_id(),
        "refresh_token": refresh,
    }
    secret = _client_secret()
    if secret:
        data["client_secret"] = secret
    r = requests.post(OAUTH_TOKEN, data=data, timeout=15)
    if r.status_code != 200:
        raise RuntimeError(f"refresh 실패: {r.status_code} {r.text}")
    new = r.json()

    stored = _load_tokens() or {}
    stored["access_token"] = new["access_token"]
    stored["expires_in"] = new.get("expires_in")
    if "refresh_token" in new:
        # 카카오는 refresh_token이 1개월 미만 남으면 자동 갱신해줌
        logger.warning(
            "⚠️  새 refresh_token 발급됨. GitHub Secret 갱신 권장:\n   gh secret set KAKAO_REFRESH_TOKEN --body '%s'",
            new["refresh_token"],
        )
        stored["refresh_token"] = new["refresh_token"]
        stored["refresh_token_expires_in"] = new.get("refresh_token_expires_in")
    _save_tokens(stored)
    return new


def get_access_token() -> str:
    """매번 refresh로 access_token 발급. (캐시·만료체크 생략 — 단순함 우선)"""
    return refresh_access_token()["access_token"]


# ── 메시지 전송 ───────────────────────────────────────────────────────


def send_to_me(
    text: str,
    link_url: Optional[str] = None,
    button_label: str = "보고서 보기",
) -> dict:
    """카카오톡 '나에게 보내기' — text 템플릿 단일 형식.

    카카오톡 클라이언트 동작 정리:
    - text 템플릿의 buttons 배열: 클라이언트가 무시함 (단일 button_title만 지원)
    - feed 템플릿: image_url 없이 보내면 PC·일부 모바일에서 카드 자체가 렌더 안 됨
    - 가장 robust한 방법: text 본문에 URL을 직접 명시 → 카카오톡이 자동으로 링크화
    """
    if not link_url:
        raise ValueError("link_url 필수")
    access_token = get_access_token()
    template = {
        "object_type": "text",
        "text": text,
        "link": {"web_url": link_url, "mobile_web_url": link_url},
        "button_title": button_label,
    }
    r = requests.post(
        SEND_TO_ME,
        headers={"Authorization": f"Bearer {access_token}"},
        data={"template_object": json.dumps(template, ensure_ascii=False)},
        timeout=15,
    )
    if r.status_code != 200:
        raise RuntimeError(f"send 실패: {r.status_code} {r.text}")
    return r.json()


TEXT_LIMIT = 200


def _short_date(date_kr: str) -> str:
    """'2026년 5월 20일 수요일' → '5/20 수' 형태로 압축."""
    import re as _re
    m = _re.match(r"\d+년 (\d+)월 (\d+)일\s*(.)?", date_kr)
    if m:
        mo, d, wk = m.group(1), m.group(2), m.group(3) or ""
        return f"{mo}/{d} {wk}".strip()
    return date_kr


def _compose_message(
    report_data: dict,
    full_url: str,
    limit: int = TEXT_LIMIT,
) -> str:
    """헤더 + 새 보도자료 제목 목록 + URL. 카카오톡이 URL을 자동 링크화."""
    date_kr = report_data.get("date_kr", "오늘")
    date_short = _short_date(date_kr)
    items = report_data.get("items", [])

    header = f"🏗️ 국토부 정책 브리핑\n{date_short} · 화제 보도 {len(items)}건"
    title_lines = []
    for it in items[:3]:
        t = it.get("title", "")
        if len(t) > 28:
            t = t[:27] + "…"
        title_lines.append(f"• {t}")
    if len(items) > 3:
        title_lines.append(f"  외 {len(items) - 3}건")
    body = ("\n" + "\n".join(title_lines)) if title_lines else ""

    msg = f"{header}{body}\n\n🔗 {full_url}"
    if len(msg) > limit:
        # 제목 목록을 줄여서라도 URL은 남긴다
        msg = f"{header}\n\n🔗 {full_url}"
        if len(msg) > limit:
            msg = msg[: limit - 1] + "…"
    return msg


def notify_from_report(report_data: dict, full_url: str) -> dict:
    """run.py에서 호출. 본문에 URL 직접 명시 (카카오톡이 자동 링크화)."""
    msg = _compose_message(report_data, full_url)
    return send_to_me(msg, link_url=full_url, button_label="브리핑 보기")


# ── CLI ───────────────────────────────────────────────────────────────


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="카카오톡 나에게 보내기")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("authorize-url", help="OAuth 동의 URL 출력")
    p_ex = sub.add_parser("exchange-code", help="authorization_code → 토큰 발급")
    p_ex.add_argument("code", help="redirect URL의 ?code= 값")
    sub.add_parser("refresh", help="access_token만 강제 갱신")
    p_send = sub.add_parser("send", help="테스트 메시지 전송")
    p_send.add_argument("--text", default="🏗️ 국토부 정책 브리핑 테스트 알림 🐰")
    p_send.add_argument(
        "--url",
        default=os.environ.get(
            "REPORT_URL", "https://rabbit-habbit.github.io/molit-daily/latest.html"
        ),
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    try:
        if args.cmd == "authorize-url":
            print(authorize_url())
        elif args.cmd == "exchange-code":
            t = exchange_code(args.code)
            print("✓ 발급 완료")
            print(
                f"access_token:  {t['access_token'][:24]}... (expires_in={t.get('expires_in')}s)"
            )
            print(
                f"refresh_token: {t['refresh_token'][:24]}... (expires_in={t.get('refresh_token_expires_in')}s)"
            )
            print(f"saved to: {TOKENS_PATH.name}")
        elif args.cmd == "refresh":
            t = refresh_access_token()
            print(f"✓ access_token: {t['access_token'][:24]}...")
        elif args.cmd == "send":
            r = send_to_me(args.text, args.url)
            print(f"✓ send response: {r}")
    except Exception as exc:
        print(f"❌ {exc}", file=sys.stderr)
        sys.exit(1)
