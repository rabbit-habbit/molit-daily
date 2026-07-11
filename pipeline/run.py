"""국토부 정책 브리핑 오케스트레이터.

흐름:
  1) 보도자료 목록 스캔 (최근 SCAN_PAGES 페이지, 조회수 포함)
  2) 조회수 임계값(VIEW_THRESHOLD)을 넘었고 아직 보고 안 한 게시물 선별
  3) 각 게시물: 상세 조회 → PDF 다운로드 → Claude 요약
  4) HTML 렌더 (index / latest / archive) + state/reported.json 갱신
  5) (--push) git commit + push
  6) 카카오톡 알림 (신규 건이 있을 때만)

멱등성: state/reported.json에 기록된 게시물은 다시 보고하지 않으므로
같은 날 여러 번 실행돼도 (Actions 백업 cron) 중복 보고·중복 비용이 없다.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pipeline import molit_client, notify_kakao, render_report  # noqa: E402
from pipeline import summarize as sm  # noqa: E402

load_dotenv(override=True)
logger = logging.getLogger(__name__)

KST = timezone(timedelta(hours=9))
WEEKDAYS_KR = ["월", "화", "수", "목", "금", "토", "일"]

STATE_PATH = ROOT / "state" / "reported.json"
PAGES_BASE = "https://rabbit-habbit.github.io/molit-daily"

# 기본값 — .env 또는 환경변수로 조정
# (참고: 2026-07 기준 최근 6주 최고 조회수는 ~5,200회. 10,000으로 올리면
#  거의 걸리는 게 없으니 VIEW_THRESHOLD로 조정할 것)
DEFAULT_THRESHOLD = int(os.environ.get("VIEW_THRESHOLD", "3000"))
DEFAULT_PAGES = int(os.environ.get("SCAN_PAGES", "12"))
DEFAULT_MAX_ITEMS = int(os.environ.get("MAX_ITEMS_PER_RUN", "5"))


def _kst_now() -> datetime:
    return datetime.now(KST)


def _date_kr(dt: datetime) -> str:
    return f"{dt.year}년 {dt.month}월 {dt.day}일 {WEEKDAYS_KR[dt.weekday()]}요일"


# ── state ───────────────────────────────────────────────────────────


def load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            logger.warning("state 파일 손상 — 새로 시작")
    return {"reported": {}}


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )


# ── git ─────────────────────────────────────────────────────────────


def _git(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True)


def _git_commit_push(date_str: str, *, dry_run: bool) -> bool:
    targets = ["docs/", "state/"]
    status = _git(["status", "--porcelain", *targets]).stdout.strip()
    if not status:
        logger.info("git: 변경사항 없음 — skip")
        return False
    if dry_run:
        logger.info("git (dry-run) 변경 파일:\n%s", status)
        return False
    _git(["add", *targets])
    r = _git(["commit", "-m", f"chore: MOLIT brief {date_str}"])
    if r.returncode != 0:
        logger.error("git commit 실패: %s", r.stderr)
        return False
    push = _git(["push"])
    if push.returncode != 0:
        logger.error("git push 실패: %s", push.stderr)
        return False
    logger.info("✅ git push 완료")
    return True


# ── 메인 ────────────────────────────────────────────────────────────


def run(
    *,
    threshold: int = DEFAULT_THRESHOLD,
    pages: int = DEFAULT_PAGES,
    max_items: int = DEFAULT_MAX_ITEMS,
    push: bool = False,
    dry_run_push: bool = False,
    notify: bool = True,
    save_intermediate: bool = True,
) -> dict | None:
    now = _kst_now()
    date_str = now.strftime("%Y-%m-%d")
    logger.info(
        "=== 국토부 정책 브리핑 (%s, 기준 %s회, %d페이지 스캔) ===",
        date_str, f"{threshold:,}", pages,
    )
    out_dir = ROOT / "out"
    out_dir.mkdir(exist_ok=True)

    state = load_state()
    reported: dict = state.setdefault("reported", {})

    # 1) 목록 스캔
    logger.info("[1/4] 보도자료 목록 스캔 중...")
    session = molit_client.make_session()
    rows = molit_client.scan_pages(session, pages)
    if not rows:
        raise RuntimeError("목록을 하나도 읽지 못했습니다 (사이트 구조 변경/차단 의심)")
    max_views = max(r.views for r in rows)
    logger.info("  ✓ %d건 스캔 (최고 조회수 %s회)", len(rows), f"{max_views:,}")

    # 2) 선별: 임계값 초과 + 미보고
    candidates = [r for r in rows if r.views >= threshold and r.post_id not in reported]
    candidates.sort(key=lambda r: r.views, reverse=True)
    skipped = len(candidates) - max_items if len(candidates) > max_items else 0
    candidates = candidates[:max_items]
    logger.info(
        "[2/4] 신규 화제 보도자료 %d건 선별%s",
        len(candidates),
        f" (백로그 {skipped}건은 다음 실행에)" if skipped > 0 else "",
    )
    if not candidates:
        logger.info("  신규 없음 — 보고서 생성 skip. 다음 실행에서 다시 확인합니다.")
        state["last_run"] = now.isoformat()
        save_state(state)
        if push or dry_run_push:
            _git_commit_push(date_str, dry_run=dry_run_push)
        return None

    # 3) 상세 + PDF + 요약
    items = []
    for i, row in enumerate(candidates, 1):
        logger.info("[3/4] (%d/%d) %s", i, len(candidates), row.title)
        detail = molit_client.fetch_detail(session, row.post_id)
        pdf_att = detail.pdf_attachment
        pdf_bytes = None
        if pdf_att:
            pdf_bytes = molit_client.download_pdf(session, pdf_att["url"])
            logger.info(
                "  PDF: %s (%s)",
                pdf_att["name"],
                f"{len(pdf_bytes)/1e6:.1f}MB" if pdf_bytes else "다운로드 실패",
            )
        try:
            summary = sm.summarize_post(
                title=row.title,
                field_name=row.field_name,
                department=detail.department,
                date=row.date,
                views=row.views,
                pdf_bytes=pdf_bytes,
                body_text=detail.body_text,
            )
        except Exception as exc:
            logger.error("  요약 실패 — 이 건은 다음 실행에서 재시도: %s", exc)
            continue
        meta = summary.pop("_meta", {})
        logger.info(
            "  ✓ 요약 완료 (in=%s, out=%s tokens)",
            meta.get("input_tokens"), meta.get("output_tokens"),
        )
        items.append(
            {
                "post_id": row.post_id,
                "title": row.title,
                "field_name": row.field_name,
                "department": detail.department,
                "date": row.date,
                "views": row.views,
                "url": row.url,
                "pdf_url": pdf_att["url"] if pdf_att else None,
                "summary": summary,
            }
        )

    if not items:
        logger.warning("요약에 성공한 건이 없음 — 종료 (state 미변경, 다음 실행에서 재시도)")
        return None

    # 4) 렌더 + state 갱신
    logger.info("[4/4] HTML 렌더 + state 갱신...")
    report_data = {
        "date": date_str,
        "date_kr": _date_kr(now),
        "threshold": threshold,
        "scan": {"pages": pages, "posts_scanned": len(rows)},
        "items": items,
        "generated_at": now.strftime("%Y-%m-%d %H:%M KST"),
    }
    if save_intermediate:
        (out_dir / "report_data.json").write_text(
            json.dumps(report_data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    html = render_report.render(report_data)
    paths = render_report.save(html, date_str)
    for label, p in paths.items():
        logger.info("  ✓ %s: %s", label, p.relative_to(ROOT))

    for it in items:
        reported[it["post_id"]] = {
            "title": it["title"],
            "date": it["date"],
            "views_at_report": it["views"],
            "reported_on": date_str,
        }
    state["last_run"] = now.isoformat()
    save_state(state)

    # 5) git
    if push or dry_run_push:
        logger.info("[git] 커밋·푸시...")
        _git_commit_push(date_str, dry_run=dry_run_push)

    # 6) 카카오 알림 (영구 archive URL — 어제 링크를 눌러도 그날 보고서 유지)
    if notify:
        url = f"{PAGES_BASE}/archive/{date_str}.html"
        logger.info("[kakao] 알림 전송: %s", url)
        try:
            notify_kakao.notify_from_report(report_data, url)
            logger.info("  ✓ 카카오톡 알림 완료")
        except Exception as exc:
            logger.warning("  ⚠️  카카오톡 알림 실패 (보고서 자체는 정상): %s", exc)

    logger.info("=== 완료: %d건 보고 ===", len(items))
    return report_data


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--threshold", type=int, default=DEFAULT_THRESHOLD, help="조회수 기준")
    parser.add_argument("--pages", type=int, default=DEFAULT_PAGES, help="스캔할 목록 페이지 수")
    parser.add_argument("--max-items", type=int, default=DEFAULT_MAX_ITEMS, help="1회 최대 보고 건수")
    parser.add_argument("--push", action="store_true", help="git commit + push 실행")
    parser.add_argument("--dry-run-push", action="store_true", help="git 변경사항 확인만")
    parser.add_argument("--no-notify", action="store_true", help="카카오톡 알림 비활성화")
    parser.add_argument("--no-save", action="store_true", help="중간 JSON 저장 안 함")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    try:
        run(
            threshold=args.threshold,
            pages=args.pages,
            max_items=args.max_items,
            push=args.push,
            dry_run_push=args.dry_run_push,
            notify=not args.no_notify,
            save_intermediate=not args.no_save,
        )
    except Exception as exc:
        logger.exception("파이프라인 실패: %s", exc)
        sys.exit(1)
