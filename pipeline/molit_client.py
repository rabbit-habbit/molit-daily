"""국토교통부 보도자료 크롤링 클라이언트.

- 목록: https://www.molit.go.kr/USR/NEWS/m_71/lst.jsp?lcmspage=N
  (번호 / 제목 / 분야 / 등록일 / 조회수)
- 상세: dtl.jsp?lcmspage=1&id=NNN — 담당부서 + 첨부파일(hwpx/pdf)
- 본문은 페이지에 없고 첨부 PDF에만 있음 → PDF를 내려받아 요약에 사용

주의: molit.go.kr 은 WAF가 첫 요청에 307 + TMOSHCooKie 쿠키를 내려주고
같은 URL로 재접속시킨다. requests.Session이 쿠키를 유지하며 리다이렉트를
따라가므로 세션 하나로 자연스럽게 통과된다.
"""
from __future__ import annotations

import html as html_mod
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import parse_qs, urlparse

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

BASE = "https://www.molit.go.kr"
LIST_URL = f"{BASE}/USR/NEWS/m_71/lst.jsp"
DTL_URL = f"{BASE}/USR/NEWS/m_71/dtl.jsp"

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

REQUEST_DELAY = 0.5  # 정부 사이트 예의상 요청 간격 (초)
MAX_PDF_BYTES = 30 * 1024 * 1024  # Claude PDF 입력 한도(32MB)보다 약간 작게


@dataclass
class PostRow:
    """목록 페이지의 한 행."""

    post_id: str
    title: str
    field_name: str  # 분야 (주택토지/도로철도/...)
    date: str  # YYYY-MM-DD
    views: int

    @property
    def url(self) -> str:
        return f"{DTL_URL}?lcmspage=1&id={self.post_id}"


@dataclass
class PostDetail:
    """상세 페이지 정보."""

    post_id: str
    department: str = ""
    registered_at: str = ""
    attachments: list[dict] = field(default_factory=list)  # {name, url}
    body_text: str = ""  # 페이지에 본문이 있는 경우 (대부분 비어있음)

    @property
    def pdf_attachment(self) -> Optional[dict]:
        for att in self.attachments:
            if att["name"].lower().endswith(".pdf"):
                return att
        return None


def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept-Language": "ko-KR,ko;q=0.9"})
    return s


def _get(session: requests.Session, url: str, **kwargs) -> requests.Response:
    time.sleep(REQUEST_DELAY)
    r = session.get(url, timeout=30, **kwargs)
    r.raise_for_status()
    return r


def fetch_list_page(session: requests.Session, page: int) -> list[PostRow]:
    """목록 1페이지(10건) 파싱."""
    r = _get(session, LIST_URL, params={"lcmspage": page})
    soup = BeautifulSoup(r.text, "html.parser")
    rows: list[PostRow] = []
    for tr in soup.select("table tbody tr"):
        a = tr.select_one("td.bd_title a")
        num = tr.select_one("td.bd_num")
        date_td = tr.select_one("td.bd_date")
        inq = tr.select_one("td.bd_inquiry")
        field_td = tr.select_one("td.bd_field")
        if not (a and date_td and inq):
            continue  # 공지 등 구조가 다른 행은 skip
        href = a.get("href", "")
        qs = parse_qs(urlparse(href).query)
        post_id = (qs.get("id") or [""])[0]
        if not post_id:
            continue
        try:
            views = int(inq.get_text(strip=True).replace(",", ""))
        except ValueError:
            continue
        rows.append(
            PostRow(
                post_id=post_id,
                title=" ".join(a.get_text(" ", strip=True).split()),
                field_name=field_td.get_text(strip=True) if field_td else "",
                date=date_td.get_text(strip=True),
                views=views,
            )
        )
    if not rows:
        logger.warning("page %d: 파싱된 행 없음 (레이아웃 변경 가능성) num=%s", page, num)
    return rows


def scan_pages(session: requests.Session, pages: int) -> list[PostRow]:
    """1..pages 페이지를 순서대로 스캔."""
    all_rows: list[PostRow] = []
    for p in range(1, pages + 1):
        rows = fetch_list_page(session, p)
        all_rows.extend(rows)
        if not rows:
            break
    return all_rows


def fetch_detail(session: requests.Session, post_id: str) -> PostDetail:
    """상세 페이지에서 담당부서·첨부파일 추출."""
    r = _get(session, DTL_URL, params={"lcmspage": 1, "id": post_id})
    raw = r.text
    detail = PostDetail(post_id=post_id)

    m = re.search(r"담당부서</strong>\s*<span>([^<]+)</span>", raw)
    if m:
        detail.department = m.group(1).strip()
    m = re.search(r"등록일</strong>\s*<span>([^<]+)</span>", raw)
    if m:
        detail.registered_at = m.group(1).strip()

    # 첨부파일 링크는 두 종류:
    #   hwpx 원본: /portal/common/download/DownloadMltm2.jsp?FilePath=...&FileName=...
    #   PDF 변환본: /LCMS/DWN.jsp?fold=...&fileName=<urlencoded>.pdf
    for dm in re.finditer(
        r'href=[\'"]((?:/portal/common/download/DownloadMltm2|/LCMS/DWN)\.jsp\?[^\'"]+)[\'"]',
        raw,
    ):
        href = html_mod.unescape(dm.group(1))
        qs = parse_qs(urlparse(href).query)
        name = (qs.get("FileName") or qs.get("fileName") or [""])[0]
        if name and not any(a["name"] == name for a in detail.attachments):
            detail.attachments.append({"name": name, "url": BASE + href})

    # 본문이 페이지에 있는 경우 대비 (현재는 대부분 첨부파일만 존재)
    soup = BeautifulSoup(raw, "html.parser")
    cont = soup.select_one(".bd_view_cont")
    if cont:
        text = " ".join(cont.get_text(" ", strip=True).split())
        if len(text) > 80:  # 안내문구 수준이면 무시
            detail.body_text = text
    return detail


def download_pdf(session: requests.Session, url: str) -> Optional[bytes]:
    """첨부 PDF 다운로드. 실패하거나 너무 크면 None."""
    try:
        r = _get(session, url)
    except requests.RequestException as exc:
        logger.warning("PDF 다운로드 실패: %s (%s)", url, exc)
        return None
    data = r.content
    if len(data) > MAX_PDF_BYTES:
        logger.warning("PDF %.1fMB — 한도 초과, 요약에서 제외", len(data) / 1e6)
        return None
    if not data[:5].startswith(b"%PDF"):
        logger.warning("PDF 시그니처 아님 (WAF 응답?): %s", data[:80])
        return None
    return data


if __name__ == "__main__":
    import argparse

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(description="국토부 보도자료 크롤러 단독 테스트")
    parser.add_argument("--pages", type=int, default=2)
    parser.add_argument("--detail", help="post_id 상세 조회")
    args = parser.parse_args()

    s = make_session()
    if args.detail:
        d = fetch_detail(s, args.detail)
        print(f"담당부서: {d.department} / 등록일: {d.registered_at}")
        for a in d.attachments:
            print(f"  첨부: {a['name']}")
        print(f"  PDF: {d.pdf_attachment['name'] if d.pdf_attachment else '없음'}")
    else:
        for row in scan_pages(s, args.pages):
            print(f"[{row.post_id}] {row.date} {row.views:>6,}회 ({row.field_name}) {row.title}")
