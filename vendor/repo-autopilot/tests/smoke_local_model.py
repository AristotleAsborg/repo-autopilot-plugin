"""步骤 0.2 冒烟：本地模型的指令遵从与延迟基线。

为什么要用 Python 而不是 PowerShell 调：PowerShell 5.1 用 Invoke-RestMethod 发
中文 JSON 时不会自动按 UTF-8 编码，实测把"只回复JSON"整句变成了乱码
（模型收到的是 `???JSON,????????:`），于是它既没听懂指令、又对乱码困惑，
产生 1377 token 的解释性输出。这个坑必须先排除掉，才能判断模型本身的遵从度。

路线 0.2 第 5 步要求：要求只回 JSON，验证指令遵从。
路线易错点 6：分类/打标不能用推理模型——这里同时验证能否关掉思考链。
"""

from __future__ import annotations

import json
import time

import httpx

BASE = "http://127.0.0.1:11434/v1"
MODEL = "qwen3:4b"


def call(prompt: str, *, think: bool | None = None, extra: dict | None = None) -> dict:
    payload: dict = {
        "model": MODEL,
        "temperature": 0,
        "stream": False,
        "messages": [{"role": "user", "content": prompt}],
    }
    # Ollama 的 OpenAI 兼容端点支持 think 开关；不用思考链时延与 token 都大幅下降
    if think is not None:
        payload["think"] = think
    if extra:
        payload.update(extra)

    started = time.perf_counter()
    with httpx.Client(timeout=180) as client:
        resp = client.post(f"{BASE}/chat/completions", json=payload)
        resp.raise_for_status()
        body = resp.json()
    elapsed = time.perf_counter() - started

    message = body["choices"][0]["message"]
    return {
        "content": (message.get("content") or "").strip(),
        "reasoning": (message.get("reasoning") or "").strip(),
        "prompt_tokens": body.get("usage", {}).get("prompt_tokens"),
        "completion_tokens": body.get("usage", {}).get("completion_tokens"),
        "seconds": round(elapsed, 2),
    }


def main() -> None:
    prompt = '只回复JSON，不要任何其他文字：{"ok":true}'

    print("=" * 72)
    print("A. 默认（思考链开着）")
    print("=" * 72)
    a = call(prompt)
    print(f"  耗时      : {a['seconds']}s")
    print(f"  tokens    : prompt={a['prompt_tokens']} completion={a['completion_tokens']}")
    print(f"  思考链长度: {len(a['reasoning'])} 字符")
    print(f"  回答      : {a['content'][:200]!r}")

    print()
    print("=" * 72)
    print("B. think=false（关掉思考链）")
    print("=" * 72)
    b = call(prompt, think=False)
    print(f"  耗时      : {b['seconds']}s")
    print(f"  tokens    : prompt={b['prompt_tokens']} completion={b['completion_tokens']}")
    print(f"  思考链长度: {len(b['reasoning'])} 字符")
    print(f"  回答      : {b['content'][:200]!r}")

    print()
    print("=" * 72)
    print("C. 真实任务形态：分类到一个封闭标签集，要求 JSON")
    print("=" * 72)
    classify = (
        "把下面这条 issue 分类。只输出 JSON，不要解释。\n"
        '标签集: ["bug", "feature", "question"]\n'
        '输出格式: {"label": "<标签>", "confidence": <0到1>}\n'
        "\n"
        "issue 标题: 保存设置后重启就丢了\n"
        "issue 正文: 每次改完主题色，关掉再打开又变回默认。版本 1.2.3，Windows 11。"
    )
    c = call(classify, think=False)
    print(f"  耗时      : {c['seconds']}s")
    print(f"  tokens    : prompt={c['prompt_tokens']} completion={c['completion_tokens']}")
    print(f"  回答      : {c['content']!r}")
    try:
        parsed = json.loads(c["content"])
        print(f"  JSON 解析 : ok -> {parsed}")
    except Exception as exc:  # noqa: BLE001
        print(f"  JSON 解析 : 失败（{exc.__class__.__name__}）—— 需要 schema 重试机制兜底")


if __name__ == "__main__":
    main()
