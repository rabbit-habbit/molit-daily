"""국토부 위클리 브리핑 오케스트레이터 (매주 토요일 아침 실행).

흐름:
  1) 보도자료 목록 스캔 (최근 SCAN_PAGES 페이지, 조회수 포함)
  2) 조회수 임계값(VIEW_THRESHOLD)을 넘었고 아직 보고 안 한 게시물 선별
     - [장관동정] 등 의전성 게시물은 EXCLUDE_TITLE_RE로 제외
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
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pipeline import molit_client, notify_email, notify_kakao, render_report  # noqa: E402
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
DEFAULT_MAX_ITEMS = int(os.environ.get("MAX_ITEMS_PER_RUN", "7"))
# 등록 후 이 일수를 넘긴 글은 조회수가 기준을 넘어도 싣지 않음 ("이번 주" 컨셉 유지)
DEFAULT_MAX_AGE_DAYS = int(os.environ.get("MAX_AGE_DAYS", "14"))
# 의전성 게시물 제외 (조회수가 높아도 정책 실속 없음). 빈 문자열이면 필터 없음.
EXCLUDE_TITLE_RE = os.environ.get(
    "EXCLUDE_TITLE_RE", r"^\[(장관|차관|위원장)?동정\]|^\[인사\]"
)
# 핵심 관심 주제 — max_items 초과로 잘라낼 때 이 주제가 조회수와 무관하게 우선.
# (독자가 브리핑을 구독하는 이유: 대출·부동산·공급)
CORE_TOPIC_RE = re.compile(
    r"주택|부동산|공급|청약|분양|전세|임대|대출|보증|기금|택지|재건축|재개발"
    r"|정비사업|미분양|디딤돌|버팀목|공시가"
)


def _is_core(row) -> bool:
    return bool(CORE_TOPIC_RE.search(row.title) or row.field_name == "주택토지")


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
    max_age_days: int = DEFAULT_MAX_AGE_DAYS,
    weekly_guard: bool = False,
    push: bool = False,
    dry_run_push: bool = False,
    notify: bool = True,
    save_intermediate: bool = True,
) -> dict | None:
    now = _kst_now()
    date_str = now.strftime("%Y-%m-%d")
    min_date = (now - timedelta(days=max_age_days)).strftime("%Y-%m-%d")
    logger.info(
        "=== 국토부 위클리 브리핑 (%s, 기준 %s회, 최근 %d일: %s~) ===",
        date_str, f"{threshold:,}", max_age_days, min_date,
    )
    out_dir = ROOT / "out"
    out_dir.mkdir(exist_ok=True)

    # Actions와 로컬이 번갈아 커밋해도 어긋나지 않도록 실행 전 동기화
    if push:
        pull = _git(["pull", "--rebase", "--autostash"])
        if pull.returncode != 0:
            logger.warning("git pull 실패 (계속 진행): %s", pull.stderr.strip())

    state = load_state()
    reported: dict = state.setdefault("reported", {})

    # 주간 가드: 최근 5일 내 발행했으면 skip (일/월 백업 스케줄이 중복 발행 안 하도록)
    if weekly_guard and state.get("last_published"):
        last_pub = datetime.fromisoformat(state["last_published"])
        if (now - last_pub).days < 5:
            logger.info(
                "이번 주(%s) 이미 발행됨 — 백업 실행 skip", last_pub.strftime("%m/%d")
            )
            return None

    # 1) 목록 스캔 (min_date보다 오래된 페이지에서 조기 종료)
    logger.info("[1/4] 보도자료 목록 스캔 중...")
    session = molit_client.make_session()
    rows = molit_client.scan_pages(session, pages, min_date=min_date)
    if not rows:
        raise RuntimeError("목록을 하나도 읽지 못했습니다 (사이트 구조 변경/차단 의심)")
    max_views = max(r.views for r in rows)
    logger.info("  ✓ %d건 스캔 (최고 조회수 %s회)", len(rows), f"{max_views:,}")

    # 2) 선별: 임계값 초과 + 미보고 + 의전성 게시물 제외
    exclude_re = re.compile(EXCLUDE_TITLE_RE) if EXCLUDE_TITLE_RE else None
    candidates = [
        r
        for r in rows
        if r.views >= threshold
        and r.date >= min_date
        and r.post_id not in reported
        and not (exclude_re and exclude_re.search(r.title))
    ]
    # 대출·부동산·공급 주제 우선, 그 안에서 조회수 내림차순
    candidates.sort(key=lambda r: (not _is_core(r), -r.views))
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

    # 같은 날 이미 발행한 항목이 있으면 합쳐서 렌더 (재시도·추가 실행 시 덮어쓰기 방지)
    prev_path = out_dir / "report_data.json"
    if prev_path.exists():
        try:
            prev = json.loads(prev_path.read_text(encoding="utf-8"))
            if prev.get("date") == date_str:
                new_ids = {it["post_id"] for it in items}
                carried = [
                    it for it in prev.get("items", []) if it["post_id"] not in new_ids
                ]
                if carried:
                    logger.info("  기존 오늘자 %d건과 병합", len(carried))
                    items = carried + items
        except (json.JSONDecodeError, KeyError):
            pass
    items.sort(
        key=lambda it: (
            not (CORE_TOPIC_RE.search(it["title"]) or it["field_name"] == "주택토지"),
            -it["views"],
        )
    )

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
    state["last_published"] = now.isoformat()
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

    # 7) 뉴스레터 이메일 (설정된 경우만, best-effort)
    if notify and os.environ.get("NEWSLETTER_SHEET_CSV_URL"):
        url = f"{PAGES_BASE}/archive/{date_str}.html"
        logger.info("[email] 뉴스레터 발송 중...")
        try:
            result = notify_email.send_newsletter(report_data, url)
            logger.info(
                "  ✓ 이메일: 성공 %d / 실패 %d", result["sent"], result["failed"]
            )
        except Exception as exc:
            logger.warning("  ⚠️  이메일 발송 실패 (보고서 자체는 정상): %s", exc)

    logger.info("=== 완료: %d건 보고 ===", len(items))
    return report_data


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--threshold", type=int, default=DEFAULT_THRESHOLD, help="조회수 기준")
    parser.add_argument("--pages", type=int, default=DEFAULT_PAGES, help="스캔할 목록 페이지 수")
    parser.add_argument("--max-items", type=int, default=DEFAULT_MAX_ITEMS, help="1회 최대 보고 건수")
    parser.add_argument("--max-age-days", type=int, default=DEFAULT_MAX_AGE_DAYS, help="등록 후 N일 지난 글 제외")
    parser.add_argument("--weekly-guard", action="store_true", help="최근 5일 내 발행했으면 skip (백업 스케줄용)")
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
            max_age_days=args.max_age_days,
            weekly_guard=args.weekly_guard,
            push=args.push,
            dry_run_push=args.dry_run_push,
            notify=not args.no_notify,
            save_intermediate=not args.no_save,
        )
    except Exception as exc:
        logger.exception("파이프라인 실패: %s", exc)
        sys.exit(1)
