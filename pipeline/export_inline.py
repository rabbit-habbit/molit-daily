"""브리핑을 '인라인 스타일 강화' 단일 HTML로 내보내기.

블로그·웹에디터·게시판은 <style> 블록과 <script>를 제거하는 경우가 많아,
모든 디자인을 태그별 inline style로 넣은 복사-붙여넣기용 HTML을 생성한다.
(JS 없음 · 외부 CSS 없음 · 예고 카드는 정적 안내 문구로 대체)

사용:
  python pipeline/export_inline.py                       # state/last_report.json
  python pipeline/export_inline.py --report <경로> --out <경로>
"""
from __future__ import annotations

import argparse
import html as html_mod
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

FONT = "'Pretendard Variable',Pretendard,'Apple SD Gothic Neo','Malgun Gothic',sans-serif"

URL_RE = re.compile(r"(https?://[^\s<]+)")


def _esc(text: str) -> str:
    return html_mod.escape(str(text), quote=False)


def _para(text: str) -> str:
    """이스케이프 + 본문 속 URL 링크화."""
    out = _esc(text)
    return URL_RE.sub(
        r'<a href="\1" target="_blank" style="color:#1B7A4B;font-weight:600;">\1</a>', out
    )


def _chip(label: str, bg: str, color: str, bold: bool = True) -> str:
    w = "700" if bold else "500"
    return (
        f'<span style="display:inline-block;padding:2px 10px;border-radius:999px;'
        f'background:{bg};color:{color};font-weight:{w};font-size:12px;'
        f'margin:0 4px 4px 0;">{_esc(label)}</span>'
    )


def render_inline(d: dict) -> str:
    items_html = []
    for it in d.get("items", []):
        s = it.get("summary", {})
        chips = _chip(it.get("field_name") or "국토부", "#E8F5EE", "#1B7A4B")
        chips += _chip(f"조회 {it.get('views', 0):,}회", "#FFF7E6", "#B45309")
        if it.get("department"):
            chips += _chip(it["department"], "#F1F4F1", "#64716B", bold=False)
        chips += _chip(it.get("date", ""), "#F1F4F1", "#64716B", bold=False)

        one_liner = (
            f'<div style="margin-top:8px;font-size:14.5px;font-weight:600;color:#1B7A4B;">{_esc(s["one_liner"])}</div>'
            if s.get("one_liner") else ""
        )
        paras = "".join(
            f'<p style="margin:12px 0 0 0;font-size:14.5px;line-height:1.7;">{_para(p)}</p>'
            for p in s.get("summary", [])
        )
        kps = "".join(
            f'<div style="display:inline-block;vertical-align:top;background:#F6F9F6;'
            f'border:1px solid #E2EAE4;border-radius:10px;padding:10px 12px;'
            f'margin:8px 8px 0 0;min-width:140px;">'
            f'<div style="font-size:11.5px;color:#64716B;">{_esc(kp.get("label",""))}</div>'
            f'<div style="font-size:14px;font-weight:700;margin-top:2px;">{_esc(kp.get("value",""))}</div></div>'
            for kp in s.get("key_points", [])
        )
        take = (
            f'<div style="margin-top:14px;padding:12px 14px;border-radius:10px;background:#FFF3EE;font-size:13.5px;line-height:1.7;">'
            f'<b style="display:block;font-size:12px;margin-bottom:3px;color:#FF6B35;">🐰 래빗해빛 해석</b>{_para(s["rabbit_take"])}</div>'
            if s.get("rabbit_take") else ""
        )
        check = (
            f'<div style="margin-top:12px;font-size:13px;color:#B45309;line-height:1.6;">📌 <b>원문에서 확인!</b> {_para(s["check_in_source"])}</div>'
            if s.get("check_in_source") else ""
        )
        pdf_link = (
            f' <a href="{it["pdf_url"]}" target="_blank" style="font-size:13px;color:#64716B;font-weight:600;'
            f'text-decoration:none;margin-left:12px;">PDF 내려받기 ↓</a>'
            if it.get("pdf_url") else ""
        )
        items_html.append(f"""
  <div style="background:#FFFFFF;border:1px solid #E2EAE4;border-radius:16px;padding:24px;margin-bottom:20px;">
    <div style="margin-bottom:10px;">{chips}</div>
    <h2 style="margin:0;font-size:18px;font-weight:700;line-height:1.45;color:#24302A;">{_esc(it.get('title',''))}</h2>
    {one_liner}{paras}
    <div style="margin-top:8px;">{kps}</div>
    {take}{check}
    <div style="margin-top:16px;">
      <a href="{it.get('url','')}" target="_blank" style="display:inline-block;padding:10px 18px;border-radius:10px;background:#1B7A4B;color:#FFFFFF;font-weight:700;font-size:14px;text-decoration:none;">📄 원문 읽기</a>{pdf_link}
    </div>
  </div>""")

    return f"""\
<div style="max-width:680px;margin:0 auto;font-family:{FONT};color:#24302A;line-height:1.7;background:#F7FAF7;padding-bottom:8px;">

  <div style="background:linear-gradient(135deg,#DCF2E4 0%,#BFE6CE 55%,#A5DBBC 100%);padding:36px 24px 28px 24px;border-bottom:4px solid #FF6B35;">
    <div style="font-size:12.5px;letter-spacing:0.5px;color:rgba(20,60,40,0.7);font-weight:700;">햇님이들을 위한 이번주 정책 브리핑</div>
    <div style="font-size:24px;font-weight:800;margin-top:8px;">🏗️ 이번 주 핫한 국토부 정책</div>
    <div style="color:rgba(20,60,40,0.75);font-size:14px;margin-top:4px;">{_esc(d.get('date_kr',''))}</div>
    <div style="display:inline-block;margin-top:14px;padding:5px 12px;background:rgba(255,255,255,0.65);border-radius:999px;font-size:12.5px;color:#1F5138;font-weight:600;">이번 주 화제 보도 {len(d.get('items', []))}건</div>
  </div>

  <div style="padding:28px 20px 0 20px;">
    <div style="background:#FFFFFF;border:1.5px solid #1B7A4B;border-radius:16px;padding:20px 22px;margin-bottom:24px;font-size:14px;">
      <div style="font-weight:800;font-size:15px;margin-bottom:8px;">🐰 시작 전에, 햇님이들!</div>
      <p style="margin:6px 0 0 0;">제가 항상 강조하는 거 기억하시죠? <span style="color:#1B7A4B;font-weight:700;">"정책은 꼭 원문으로 확인하세요."</span> 원문을 보는 습관을 길러야 내가 주체적으로 해석하는 힘이 생겨요! 기사 한 줄, 요약 한 장으로는 내 상황에 맞는 조건이나 예외가 안 보이거든요.</p>
      <p style="margin:6px 0 0 0;">그래서 이 브리핑은 정답지가 아니라 <b>지도</b>예요. 아래에서 가닥만 잡고, 끌리는 소식은 <span style="color:#1B7A4B;font-weight:700;">[원문 읽기] 버튼으로 바로 국토부 보도자료를 직접</span> 보실 수 있어요 📄</p>
    </div>
{''.join(items_html)}
    <div style="margin-top:28px;padding:22px 24px;border-radius:16px;background:linear-gradient(135deg,#FFF3EE 0%,#FFE8DC 100%);border:1.5px solid #FF6B35;font-size:14px;">
      <div style="font-size:16px;font-weight:800;margin-bottom:8px;">🌅 매일 아침이 더 궁금하다면?</div>
      <p style="margin:0;">이 국토부 브리핑은 매주 토요일 1회예요. 그런데 햇님이들, 경제 뉴스는 매일 아침 쏟아지잖아요. 그래서 준비 중입니다 - <b>출근 전 5분, 그날의 주요 경제뉴스를 래빗해빛 해석과 함께 보내드리는 「데일리 경제 브리핑」.</b> 오픈하면 가장 먼저, 가장 좋은 조건으로 알려드릴게요.</p>
      <div style="text-align:center;margin-top:14px;font-size:13px;color:#B45309;font-weight:600;">오픈 알림 신청은 매주 토요일 발송되는 메일 속 버튼에서 클릭 한 번이면 돼요 ✉️</div>
    </div>
  </div>

  <div style="padding:24px 12px 40px 12px;text-align:center;font-size:13px;color:#64716B;">
    <div style="font-size:16px;font-weight:800;color:#24302A;">부자습관은 래빗해빛 🐰</div>
    <p style="margin:8px 0 0 0;">햇님이들을 위한 이번주 국토부 정책 소식 🌞</p>
    <p style="margin:8px 0 0 0;">
      <a href="https://www.youtube.com/@rabbit._.habbit" target="_blank" style="color:#FF6B35;font-weight:700;text-decoration:none;">유튜브</a> ·
      <a href="https://www.instagram.com/rabbit._.habbit/" target="_blank" style="color:#FF6B35;font-weight:700;text-decoration:none;">인스타그램</a></p>
    <p style="margin:8px 0 0 0;opacity:0.55;">본 보고서는 국토부 보도자료의 요약본이며, 투자 판단의 근거가 될 수 없습니다.</p>
    <div style="margin-top:16px;font-size:10.5px;opacity:0.4;">출처: 국토교통부 보도자료 (공공누리 자유이용)</div>
  </div>
</div>"""


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="인라인 스타일 HTML 내보내기")
    parser.add_argument("--report", default=str(ROOT / "state" / "last_report.json"))
    parser.add_argument("--out", default="")
    args = parser.parse_args()

    d = json.loads(Path(args.report).read_text(encoding="utf-8"))
    out = Path(args.out) if args.out else ROOT / "out" / f"briefing-{d['date']}-inline.html"
    out.parent.mkdir(exist_ok=True)
    out.write_text(render_inline(d), encoding="utf-8")
    print(f"✓ {out}")
