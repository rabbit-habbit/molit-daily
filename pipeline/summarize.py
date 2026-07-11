"""Claude API로 국토부 보도자료 PDF를 요약.

보도자료 1건당 1회 호출. PDF가 없거나 다운로드 실패 시
제목·부처 정보만으로 축소 요약을 시도한다.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import re
from typing import Optional

from anthropic import Anthropic
from dotenv import load_dotenv

load_dotenv(override=True)
logger = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-sonnet-4-6"

BRAND_CONTEXT = """\
[브랜드: 래빗해빛]
- 25~45 직장인 대상 재테크·경제 콘텐츠 브랜드 (유튜브/릴스/블로그)
- 이 브리핑은 래빗해빛 구독자(직장인)에게 배포되는 콘텐츠입니다
- 독자 관심사: 부동산·청약, 대출·보험, 교통 인프라와 집값, 월급으로 하는 재테크
- 톤: "공부 잘하는 현실 친구" 같은 친근한 해요체. "~예요/~죠/~거든요" 자연스럽게.
  어려운 행정용어는 풀어서 설명.
"""

SYSTEM_PROMPT = f"""\
당신은 래빗해빛의 시니어 정책 큐레이터입니다. 조회수가 높아 화제가 된 국토부
보도자료를 받아 직장인 구독자에게 배포할 스크랩 요약을 작성합니다.

{BRAND_CONTEXT}

## 규칙
- 보도자료(PDF)에 실제로 적힌 내용만 사용하세요. 수치·날짜·지역명을 지어내지 마세요.
- PDF가 없으면 제목에서 확실히 알 수 있는 것만 쓰고, summary에 "상세 내용은 원문 확인 필요"를 명시하세요.
- 행정용어는 풀어서: "공모 착수" → "신청 접수를 시작해요".

## 출력 — 단일 JSON 객체만 (```json 코드블록 가능, 다른 설명 금지)
{{
  "one_liner": "핵심 한 줄 (40자 이내, 이모지 1개 시작)",
  "summary": ["문단1 (2~3문장)", "문단2 (2~3문장)"],
  "key_points": [
    {{"label": "핵심 항목명", "value": "수치·날짜·규모"}}
  ],
  "who_affected": "누구에게 어떤 영향인지 1~2문장 (예: 청약 대기자, 수도권 출퇴근 직장인)",
  "action_or_watch": "독자가 확인·준비할 것 또는 지켜볼 포인트 1~2문장",
  "rabbit_take": "래빗해빛의 해석 2~3문장 — 이 정책이 직장인의 지갑·내집마련·출퇴근에 실제로 어떤 의미인지, 표면 아래의 맥락(왜 지금 나왔는지, 시장에 어떤 신호인지)을 친근한 해요체로. 보도자료를 그대로 반복하지 말고 한 걸음 들어간 관점을 줄 것."
}}
- key_points는 2~5개. 보도자료의 실제 수치만.
- rabbit_take에서도 사실은 보도자료·상식 범위 안에서만. 과도한 투자 조언("사세요/파세요") 금지.
- JSON 형식 엄격 준수: 큰따옴표, trailing comma 금지.
"""


def _client() -> Anthropic:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY가 없습니다 (.env 확인)")
    return Anthropic(api_key=key)


def _model() -> str:
    return os.environ.get("ANTHROPIC_MODEL", DEFAULT_MODEL)


def _parse_json(text: str) -> dict:
    m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    payload = m.group(1) if m else text
    start = payload.find("{")
    end = payload.rfind("}")
    if start == -1 or end == -1:
        raise ValueError(f"JSON을 찾을 수 없음: {text[:200]}")
    return json.loads(payload[start : end + 1])


def summarize_post(
    *,
    title: str,
    field_name: str,
    department: str,
    date: str,
    views: int,
    pdf_bytes: Optional[bytes] = None,
    body_text: str = "",
) -> dict:
    """보도자료 1건 요약. 반환: 프롬프트의 JSON 스키마 + _meta."""
    meta_line = (
        f"제목: {title}\n분야: {field_name}\n담당부서: {department}\n"
        f"등록일: {date}\n조회수: {views:,}회"
    )
    content: list[dict] = []
    if pdf_bytes:
        content.append(
            {
                "type": "document",
                "source": {
                    "type": "base64",
                    "media_type": "application/pdf",
                    "data": base64.standard_b64encode(pdf_bytes).decode(),
                },
            }
        )
        prompt = f"다음 국토교통부 보도자료를 요약해주세요.\n\n{meta_line}\n\n원문은 첨부한 PDF입니다."
    else:
        body_part = f"\n\n[페이지 본문 발췌]\n{body_text[:3000]}" if body_text else ""
        prompt = (
            f"다음 국토교통부 보도자료를 요약해주세요. PDF 원문이 없으므로 "
            f"아래 정보에서 확실한 것만 쓰세요.\n\n{meta_line}{body_part}"
        )
    content.append({"type": "text", "text": prompt})

    client = _client()
    model = _model()
    resp = client.messages.create(
        model=model,
        max_tokens=2000,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": content}],
    )
    text = "".join(b.text for b in resp.content if b.type == "text")
    data = _parse_json(text)
    data["_meta"] = {
        "model": model,
        "input_tokens": resp.usage.input_tokens,
        "output_tokens": resp.usage.output_tokens,
        "had_pdf": bool(pdf_bytes),
    }
    return data


if __name__ == "__main__":
    import argparse
    from pathlib import Path

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(description="요약 단독 테스트")
    parser.add_argument("--pdf", help="로컬 PDF 경로")
    parser.add_argument("--title", required=True)
    parser.add_argument("--field", default="")
    parser.add_argument("--dept", default="")
    parser.add_argument("--date", default="")
    parser.add_argument("--views", type=int, default=0)
    args = parser.parse_args()

    pdf = Path(args.pdf).read_bytes() if args.pdf else None
    out = summarize_post(
        title=args.title,
        field_name=args.field,
        department=args.dept,
        date=args.date,
        views=args.views,
        pdf_bytes=pdf,
    )
    print(json.dumps(out, ensure_ascii=False, indent=2))
