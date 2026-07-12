"""Jinja2로 국토부 정책 브리핑 HTML을 렌더링.

데이터 모델:
    {
        "date": "2026-07-11",
        "date_kr": "2026년 7월 11일 금요일",
        "threshold": 3000,
        "scan": {"pages": 12, "posts_scanned": 120},
        "items": [
            {
                "post_id": "95092208",
                "title": "...",
                "field_name": "국토도시",
                "department": "지리정보과",
                "date": "2026-07-10",
                "views": 5217,
                "url": "https://...dtl.jsp?...",
                "pdf_url": "https://...DownloadMltm2.jsp?...",  # 없으면 None
                "summary": {one_liner, summary[], key_points[],
                            rabbit_take, check_in_source},
            },
        ],
        "generated_at": "2026-07-11 09:40 KST",
    }
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Optional

from jinja2 import Environment, FileSystemLoader, select_autoescape

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES_DIR = ROOT / "templates"
DOCS_DIR = ROOT / "docs"


def render(context: dict) -> str:
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=select_autoescape(["html"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    return env.get_template("report.html.j2").render(**context)


def save(html: str, date_str: str, also_index: bool = True) -> dict[str, Path]:
    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    (DOCS_DIR / "archive").mkdir(parents=True, exist_ok=True)
    latest = DOCS_DIR / "latest.html"
    archive = DOCS_DIR / "archive" / f"{date_str}.html"
    latest.write_text(html, encoding="utf-8")
    archive.write_text(html, encoding="utf-8")
    out = {"latest": latest, "archive": archive}
    if also_index:
        index = DOCS_DIR / "index.html"
        index.write_text(html, encoding="utf-8")
        out["index"] = index
    return out


def mock_data() -> dict:
    return {
        "date": "2026-07-11",
        "date_kr": "2026년 7월 11일 금요일",
        "threshold": 3000,
        "scan": {"pages": 12, "posts_scanned": 120},
        "items": [
            {
                "post_id": "95092208",
                "title": "반세기 전 아날로그 항공사진, 디지털로 되살린다",
                "field_name": "국토도시",
                "department": "지리정보과",
                "date": "2026-07-10",
                "views": 5217,
                "url": "https://www.molit.go.kr/USR/NEWS/m_71/dtl.jsp?lcmspage=1&id=95092208",
                "pdf_url": None,
                "summary": {
                    "one_liner": "🗺️ 50년 전 항공사진을 온라인에서 열람할 수 있게 돼요",
                    "summary": [
                        "국토지리정보원이 경상남도와 협약을 맺고 아날로그 항공사진을 디지털로 전환해요. 온라인 열람 서비스도 함께 제공됩니다.",
                    ],
                    "key_points": [
                        {"label": "협약 기관", "value": "국토지리정보원·경남도"},
                        {"label": "대상", "value": "반세기 전 항공사진"},
                    ],
                    "rabbit_take": "과거 지형 자료가 디지털로 풀리면 토지 이력 확인이 훨씬 쉬워져요.",
                    "check_in_source": "온라인 열람 서비스 오픈 시점과 대상 지역 목록",
                },
            }
        ],
        "generated_at": "2026-07-11 09:40 KST",
    }


def _load_input(input_arg: Optional[str]) -> dict:
    if input_arg is None or input_arg == "-":
        return json.load(sys.stdin)
    return json.loads(Path(input_arg).read_text(encoding="utf-8"))


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="국토부 브리핑 렌더러")
    parser.add_argument("--input", help="JSON 입력 경로 (또는 '-'로 stdin)")
    parser.add_argument("--mock", action="store_true", help="내장 mock 데이터로 렌더")
    args = parser.parse_args()

    data = mock_data() if args.mock else _load_input(args.input)
    html = render(data)
    paths = save(html, data["date"])
    for label, p in paths.items():
        print(f"✓ {label}: {p}")
