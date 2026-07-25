"""뉴스레터 이메일 발송 (네이버 SMTP).

구독자 명단: 구글폼 응답이 쌓이는 구글시트를 '웹에 게시(CSV)'한 URL에서 읽는다.
- '이메일' 또는 'email'이 들어간 헤더 컬럼을 찾고, 없으면 @ 포함 셀을 사용
- 행에 '수신거부'라는 셀이 있으면 그 행은 제외
- 주소는 중복 제거, 1명씩 개별 발송 (수신자 간 주소 노출 없음)

환경변수 (.env / GitHub secrets):
  NAVER_SMTP_USER      네이버 아이디 (@naver.com 앞부분 또는 전체 주소)
  NAVER_SMTP_PASSWORD  네이버 비밀번호 (2단계 인증 사용 시 애플리케이션 비밀번호)
  NEWSLETTER_SHEET_CSV_URL  구글시트 '웹에 게시' CSV 링크
  NEWSLETTER_FROM_NAME 발신자 표시 이름 (기본: 래빗해빛)
"""
from __future__ import annotations

import base64
import csv
import hashlib
import hmac
import io
import logging
import os
import re
import smtplib
import time
from email.header import Header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr

import json
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv(override=True)
logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[1]
# 발송 이력: {"YYYY-MM-DD(호)": [보낸 이메일...]} - 같은 호 재실행 시
# 이미 받은 사람은 제외되고 추가 신청자에게만 나간다 (git 커밋 대상)
LOG_PATH = ROOT / "state" / "newsletter_log.json"

SMTP_HOST = "smtp.naver.com"
SMTP_PORT = 587
SEND_DELAY = 1.5  # 발송 간격 (초) — 스팸 분류 방지

EMAIL_RE = re.compile(r"^[\w.+-]+@[\w-]+\.[\w.-]+$")


def _smtp_user() -> str:
    user = os.environ.get("NAVER_SMTP_USER", "").strip()
    if not user:
        raise RuntimeError("NAVER_SMTP_USER가 설정되지 않았습니다")
    return user


def _from_address() -> str:
    user = _smtp_user()
    return user if "@" in user else f"{user}@naver.com"


def fetch_subscribers() -> list[str]:
    """구글시트 CSV에서 구독자 이메일 목록을 읽는다."""
    url = os.environ.get("NEWSLETTER_SHEET_CSV_URL", "").strip()
    if not url:
        raise RuntimeError("NEWSLETTER_SHEET_CSV_URL이 설정되지 않았습니다")
    r = requests.get(url, timeout=30)
    r.raise_for_status()
    r.encoding = "utf-8"
    rows = list(csv.reader(io.StringIO(r.text)))
    if not rows:
        return []

    header = [h.strip().lower() for h in rows[0]]
    email_col = next(
        (i for i, h in enumerate(header) if "이메일" in h or "email" in h), None
    )

    emails: list[str] = []
    seen = set()
    for row in rows[1:]:
        if any("수신거부" in cell for cell in row):
            continue
        candidates = (
            [row[email_col]] if email_col is not None and email_col < len(row) else row
        )
        for cell in candidates:
            addr = cell.strip().lower()
            if EMAIL_RE.match(addr) and addr not in seen:
                seen.add(addr)
                emails.append(addr)
                break
    return emails


# ── 이메일 본문 ───────────────────────────────────────────────────────

BTN_STYLE = (
    "display:inline-block;padding:12px 28px;border-radius:10px;"
    "background:#1B7A4B;color:#ffffff;font-weight:bold;font-size:15px;"
    "text-decoration:none;"
)


def _token_pair(addr: str) -> tuple[str, str]:
    """수신자 서명 토큰: t = base64(이메일), s = HMAC-SHA256(이메일) 앞 32자."""
    secret = os.environ.get("MOLIT_PROXY_TOKEN", "")
    if not secret:
        return "", ""
    t = base64.urlsafe_b64encode(addr.encode()).decode().rstrip("=")
    s = hmac.new(secret.encode(), addr.encode(), hashlib.sha256).hexdigest()[:32]
    return t, s


def _waitlist_url(addr: str) -> str:
    """수신자 전용 원클릭 신청 링크 (Cloudflare Worker /waitlist).

    클릭만 하면 재입력 없이 대기명단에 기록된다.
    """
    base = os.environ.get("WAITLIST_BASE_URL", "").rstrip("/")
    t, s = _token_pair(addr)
    if not (base and t):
        return ""
    return f"{base}?t={t}&s={s}"


def _tokenized_report_url(report_url: str, addr: str) -> str:
    """브리핑 링크에 수신자 토큰을 붙여, 웹 페이지 하단 예고 카드도
    본인 원클릭 버튼으로 작동하게 한다."""
    t, s = _token_pair(addr)
    if not t:
        return report_url
    sep = "&" if "?" in report_url else "?"
    return f"{report_url}{sep}t={t}&s={s}"


def _promo_block(report_data: dict, promo_url: str = "") -> str:
    """데일리 브리핑 예고 카드 (링크가 있을 때만)."""
    promo_url = (
        promo_url
        or report_data.get("promo_url")
        or os.environ.get("PROMO_URL", "")
    )
    if not promo_url:
        return ""
    return f"""
    <div style="margin-top:22px;padding:18px 20px;border-radius:14px;background:#FFF3EE;border:1.5px solid #FF6B35;">
      <div style="font-size:15px;font-weight:bold;">🌅 매일 아침이 더 궁금하다면?</div>
      <p style="margin:8px 0 0 0;font-size:13.5px;">이 국토부 브리핑은 매주 토요일 1회예요.
      그런데 햇님이들, 경제 뉴스는 매일 아침 쏟아지잖아요. 그래서 준비 중입니다 -
      <b>출근 전 5분, 그날의 주요 경제뉴스를 래빗해빛 해석과 함께 보내드리는 「데일리 경제 브리핑」.</b>
      오픈하면 가장 먼저, 가장 좋은 조건으로 알려드릴게요.</p>
      <div style="text-align:center;margin-top:14px;">
        <a href="{promo_url}" style="display:inline-block;padding:10px 24px;border-radius:10px;background:#FF6B35;color:#ffffff;font-weight:bold;font-size:14px;text-decoration:none;">🔔 오픈 알림 신청하기</a>
      </div>
    </div>"""


def build_html(report_data: dict, report_url: str, promo_url: str = "") -> str:
    """이메일 클라이언트 호환(인라인 스타일) HTML 본문."""
    items = report_data.get("items", [])
    date_kr = report_data.get("date_kr", "")

    item_blocks = []
    for it in items:
        one_liner = it.get("summary", {}).get("one_liner", "")
        item_blocks.append(
            f"""
      <div style="margin:0 0 14px 0;padding:14px 16px;background:#F6F9F6;border-radius:10px;">
        <div style="font-size:12px;color:#64716B;margin-bottom:4px;">
          {it.get('field_name','')} · 조회 {it.get('views',0):,}회</div>
        <div style="font-size:15px;font-weight:bold;color:#24302A;line-height:1.5;">
          {it.get('title','')}</div>
        {f'<div style="font-size:13.5px;color:#1B7A4B;margin-top:4px;">{one_liner}</div>' if one_liner else ''}
      </div>"""
        )

    return f"""\
<div style="margin:0 auto;max-width:600px;font-family:'Apple SD Gothic Neo','Malgun Gothic',sans-serif;color:#24302A;line-height:1.7;">
  <div style="padding:28px 24px;background:#DCF2E4;border-radius:14px 14px 0 0;">
    <div style="font-size:12.5px;color:#1F5138;font-weight:bold;">햇님이들을 위한 이번주 정책 브리핑</div>
    <div style="font-size:22px;font-weight:bold;margin-top:6px;">🏗️ 이번 주 핫한 국토부 정책</div>
    <div style="font-size:13px;color:#1F5138;margin-top:4px;">{date_kr}</div>
  </div>
  <div style="padding:24px;background:#ffffff;border:1px solid #E2EAE4;border-top:none;border-radius:0 0 14px 14px;">
    <p style="margin:0 0 16px 0;font-size:14px;">
      안녕하세요, 래빗해빛이에요 🐰 제가 항상 강조하는 거 기억하시죠?
      <b style="color:#1B7A4B;">"정책은 꼭 원문으로 확인하세요."</b>
      이번 주 화제가 된 국토부 소식 {len(items)}건, 가닥만 잡아드릴게요 -
      자세한 건 브리핑에서 원문으로 바로 이동할 수 있어요!</p>
    {''.join(item_blocks)}
    <div style="text-align:center;margin:24px 0 8px 0;">
      <a href="{report_url}" style="{BTN_STYLE}">전체 브리핑 보기 📄</a>
    </div>
    {_promo_block(report_data, promo_url)}
  </div>
  <div style="padding:20px 12px;text-align:center;font-size:12px;color:#64716B;">
    <div style="font-size:14px;font-weight:bold;color:#24302A;">부자습관은 래빗해빛 🐰</div>
    <p style="margin:6px 0 0 0;">
      <a href="https://www.youtube.com/@rabbit._.habbit" style="color:#FF6B35;font-weight:bold;text-decoration:none;">유튜브</a> ·
      <a href="https://www.instagram.com/rabbit._.habbit/" style="color:#FF6B35;font-weight:bold;text-decoration:none;">인스타그램</a></p>
    <p style="margin:8px 0 0 0;opacity:0.7;">본 메일은 무료 맛보기를 신청해주신 분들께 발송됩니다.<br>
      수신을 원치 않으시면 이 메일에 <b>"수신거부"</b>라고 회신해주세요.</p>
    <p style="margin:6px 0 0 0;opacity:0.5;">본 보고서는 국토부 보도자료의 요약본이며, 투자 판단의 근거가 될 수 없습니다.</p>
  </div>
</div>"""


def send_newsletter(
    report_data: dict, report_url: str, recipients: list[str] | None = None
) -> dict:
    """구독자 전원에게 개별 발송. 반환: {sent, failed, total}.

    recipients를 주면 명단 조회를 건너뛰고 그 주소로만 발송 (테스트용, 이력 미기록).
    """
    emails = recipients if recipients is not None else fetch_subscribers()

    # 발송 이력 기반 증분 발송 (테스트 발송은 이력에 안 남김)
    issue = report_data.get("date", "")
    log = None
    if recipients is None and issue:
        log = json.loads(LOG_PATH.read_text(encoding="utf-8")) if LOG_PATH.exists() else {}
        already = set(log.get(issue, []))
        if already:
            before = len(emails)
            emails = [e for e in emails if e not in already]
            logger.info(
                "이번 호(%s) 기발송 %d명 제외 → 신규 %d명", issue, before - len(emails), len(emails)
            )

    if not emails:
        logger.info("발송 대상 없음 (전원 기발송 또는 구독자 없음)")
        return {"sent": 0, "failed": 0, "total": 0}

    user = _smtp_user()
    password = os.environ.get("NAVER_SMTP_PASSWORD", "")
    if not password:
        raise RuntimeError("NAVER_SMTP_PASSWORD가 설정되지 않았습니다")
    from_addr = _from_address()
    from_name = os.environ.get("NEWSLETTER_FROM_NAME", "래빗해빛")

    items = report_data.get("items", [])
    date_kr = report_data.get("date_kr", "")
    m = re.search(r"(\d+)월 (\d+)일", date_kr)
    date_short = f"{m.group(1)}/{m.group(2)}" if m else ""
    subject = f"🏗️ 이번 주 핫한 국토부 정책 {len(items)}건 - {date_short} 래빗해빛 브리핑"
    # 원클릭 대기명단 모드면 수신자마다 전용 링크가 든 본문을 개별 생성
    use_waitlist = bool(os.environ.get("WAITLIST_BASE_URL"))
    shared_html = None if use_waitlist else build_html(report_data, report_url)

    sent = failed = 0
    sent_addrs: list[str] = []
    BATCH = 20  # 연결 하나로 너무 오래 보내면 서버가 끊을 수 있어 배치마다 재접속
    for i in range(0, len(emails), BATCH):
        batch = emails[i : i + BATCH]
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as smtp:
            smtp.starttls()
            smtp.login(user, password)
            for addr in batch:
                url_r = (
                    _tokenized_report_url(report_url, addr)
                    if use_waitlist
                    else report_url
                )
                html = (
                    build_html(report_data, url_r, promo_url=_waitlist_url(addr))
                    if use_waitlist
                    else shared_html
                )
                msg = MIMEMultipart("alternative")
                msg["Subject"] = str(Header(subject, "utf-8"))
                msg["From"] = formataddr((str(Header(from_name, "utf-8")), from_addr))
                msg["To"] = addr
                msg.attach(MIMEText(f"이번 주 브리핑: {url_r}", "plain", "utf-8"))
                msg.attach(MIMEText(html, "html", "utf-8"))
                try:
                    smtp.sendmail(from_addr, [addr], msg.as_string())
                    sent += 1
                    sent_addrs.append(addr)
                except smtplib.SMTPException as exc:
                    failed += 1
                    logger.warning("발송 실패 %s: %s", addr, exc)
                time.sleep(SEND_DELAY)
        if i + BATCH < len(emails):
            logger.info("  ... %d/%d 발송, 잠시 후 계속", sent, len(emails))
            time.sleep(5)

    if log is not None and sent_addrs:
        log.setdefault(issue, []).extend(sent_addrs)
        LOG_PATH.write_text(
            json.dumps(log, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    logger.info("이메일 발송: 성공 %d / 실패 %d / 총 %d", sent, failed, len(emails))
    return {"sent": sent, "failed": failed, "total": len(emails)}


if __name__ == "__main__":
    import argparse
    import json
    from pathlib import Path

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(description="뉴스레터 발송 단독 실행")
    parser.add_argument("--list", action="store_true", help="구독자 명단만 확인")
    parser.add_argument("--test-to", help="이 주소 1명에게만 테스트 발송")
    parser.add_argument(
        "--report", default="out/report_data.json", help="report_data.json 경로"
    )
    parser.add_argument(
        "--url",
        default="",
        help="브리핑 URL (생략 시 report의 date로 archive URL 자동 생성)",
    )
    args = parser.parse_args()

    if args.list:
        subs = fetch_subscribers()
        print(f"구독자 {len(subs)}명:")
        for e in subs:
            print(" -", e)
    else:
        data = json.loads(Path(args.report).read_text(encoding="utf-8"))
        url = args.url or (
            f"https://rabbit-habbit.github.io/molit-daily/archive/{data['date']}.html"
        )
        recipients = [args.test_to] if args.test_to else None
        result = send_newsletter(data, url, recipients=recipients)
        print(result)
