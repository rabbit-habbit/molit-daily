"""ANTHROPIC_API_KEY가 유효한지 가장 짧은 호출 1회로 확인."""
from __future__ import annotations

import os
import sys

from anthropic import Anthropic
from dotenv import load_dotenv

load_dotenv(override=True)


def main() -> int:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        print("❌ ANTHROPIC_API_KEY가 설정되지 않았습니다.", file=sys.stderr)
        print("   프로젝트 루트의 .env 파일을 확인하세요.", file=sys.stderr)
        return 1

    model = os.environ.get("ANTHROPIC_MODEL", "claude-opus-4-7")
    print(f"키 발견: {key[:8]}...{key[-4:]} (len={len(key)})")
    print(f"모델: {model}")
    print("Anthropic API에 핑 호출 중...")

    try:
        client = Anthropic()
        resp = client.messages.create(
            model=model,
            max_tokens=20,
            messages=[{"role": "user", "content": "say 'pong' in one word"}],
        )
        print(f"✅ 응답: {resp.content[0].text}")
        print(f"   usage: input={resp.usage.input_tokens}, output={resp.usage.output_tokens}")
        return 0
    except Exception as exc:
        print(f"❌ API 호출 실패: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
