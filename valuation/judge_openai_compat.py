# -*- coding: utf-8 -*-
"""通用 API 判断层：stdin 进 prompt，stdout 出模型回答。

替代本机 `claude -p`（消费者订阅无法部署、且商用违反 ToS）——走 API 计费，
单次报告成本可计量，是部署/收费的前置。用法：

    export VALUATION_JUDGMENT_CMD="python valuation/judge_openai_compat.py"

    # Anthropic（原生 Messages API，推荐）
    export JUDGE_PROVIDER=anthropic
    export JUDGE_API_KEY=sk-ant-...
    export JUDGE_MODEL=claude-opus-4-8        # 默认

    # 或任何 OpenAI 兼容端点（DeepSeek / Qwen / Kimi / GLM ...）
    export JUDGE_PROVIDER=openai-compat
    export JUDGE_BASE_URL=https://api.deepseek.com/v1
    export JUDGE_API_KEY=sk-...
    export JUDGE_MODEL=deepseek-chat

schema 校验与重试在 valuation_service 里对所有 provider 通用，这里只负责一问一答。
"""
import json
import os
import sys
import time

import httpx

PROVIDER = os.environ.get("JUDGE_PROVIDER", "anthropic")
API_KEY = os.environ.get("JUDGE_API_KEY", "")
MODEL = os.environ.get("JUDGE_MODEL", "claude-opus-4-8")
TIMEOUT = float(os.environ.get("JUDGE_TIMEOUT", "420"))
MAX_TOKENS = int(os.environ.get("JUDGE_MAX_TOKENS", "16000"))

if not API_KEY:
    raise SystemExit("缺少 JUDGE_API_KEY 环境变量")


def call_anthropic(prompt: str) -> str:
    base = os.environ.get("JUDGE_BASE_URL", "https://api.anthropic.com").rstrip("/")
    resp = httpx.post(
        f"{base}/v1/messages",
        headers={"x-api-key": API_KEY, "anthropic-version": "2023-06-01"},
        json={"model": MODEL, "max_tokens": MAX_TOKENS,
              "messages": [{"role": "user", "content": prompt}]},
        timeout=TIMEOUT,
    )
    resp.raise_for_status()
    d = resp.json()
    if d.get("stop_reason") == "refusal":
        raise SystemExit("判断层请求被模型安全策略拒绝（stop_reason=refusal）")
    return "".join(b.get("text", "") for b in d["content"] if b.get("type") == "text")


def call_openai_compat(prompt: str) -> str:
    base = os.environ.get("JUDGE_BASE_URL", "").rstrip("/")
    if not base:
        raise SystemExit("openai-compat 模式需要 JUDGE_BASE_URL（如 https://api.deepseek.com/v1）")
    resp = httpx.post(
        f"{base}/chat/completions",
        headers={"Authorization": f"Bearer {API_KEY}"},
        json={"model": MODEL, "max_tokens": MAX_TOKENS,
              "messages": [{"role": "user", "content": prompt}]},
        timeout=TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]


def main() -> None:
    prompt = sys.stdin.read()
    call = call_anthropic if PROVIDER == "anthropic" else call_openai_compat
    last_err = None
    for attempt in range(3):
        try:
            print(call(prompt))
            return
        except httpx.HTTPStatusError as e:
            # 429/5xx 退避重试，4xx 直接失败
            if e.response.status_code in (429,) or e.response.status_code >= 500:
                last_err = e
                time.sleep(2 ** attempt * 5)
                continue
            raise SystemExit(f"判断层 API 错误 {e.response.status_code}: {e.response.text[:300]}")
        except httpx.TransportError as e:
            last_err = e
            time.sleep(2 ** attempt * 5)
    raise SystemExit(f"判断层 API 重试 3 次仍失败: {last_err}")


if __name__ == "__main__":
    main()
