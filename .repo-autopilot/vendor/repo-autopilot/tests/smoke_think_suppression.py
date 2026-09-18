"""步骤 0.2 补充：思考链能否真正关闭。

起因：第一次冒烟发现 qwen3:4b 即使传 think=false，仍产出 422~1373 completion
token。路线易错点 6 明确说"推理模型做分类 JSON 不稳且拖慢"，而分类是本系统
最高频的动作（每个 issue 都要跑）。19.5 秒/条不可接受。

本脚本对比四种抑制手段，用同一道分类题，取 completion token 与延迟为判据。
不猜、不靠文档，全部实测。
"""

from __future__ import annotations

import json
import time

import httpx

BASE = "http://127.0.0.1:11434/v1"
NATIVE = "http://127.0.0.1:11434/api/chat"
MODEL = "qwen3:4b"

TASK = (
    "把下面这条 issue 分类。只输出 JSON，不要解释。\n"
    '标签集: ["bug", "feature", "question"]\n'
    '输出格式: {"label": "<标签>", "confidence": <0到1>}\n'
    "\n"
    "issue 标题: 保存设置后重启就丢了\n"
    "issue 正文: 每次改完主题色，关掉再打开又变回默认。版本 1.2.3，Windows 11。"
)


def openai_call(*, content: str, think: bool | None) -> dict:
    payload: dict = {
        "model": MODEL,
        "temperature": 0,
        "stream": False,
        "messages": [{"role": "user", "content": content}],
    }
    if think is not None:
        payload["think"] = think

    started = time.perf_counter()
    with httpx.Client(timeout=300) as client:
        resp = client.post(f"{BASE}/chat/completions", json=payload)
        resp.raise_for_status()
        body = resp.json()
    elapsed = time.perf_counter() - started

    msg = body["choices"][0]["message"]
    return {
        "content": (msg.get("content") or "").strip(),
        "reasoning": (msg.get("reasoning") or "").strip(),
        "completion": body.get("usage", {}).get("completion_tokens"),
        "seconds": round(elapsed, 2),
    }


def native_call(*, content: str, think: bool | None) -> dict:
    """Ollama 原生 /api/chat —— think 参数在这里是官方的一等公民。"""
    payload: dict = {
        "model": MODEL,
        "stream": False,
        "options": {"temperature": 0},
        "messages": [{"role": "user", "content": content}],
    }
    if think is not None:
        payload["think"] = think

    started = time.perf_counter()
    with httpx.Client(timeout=300) as client:
        resp = client.post(NATIVE, json=payload)
        resp.raise_for_status()
        body = resp.json()
    elapsed = time.perf_counter() - started

    return {
        "content": (body.get("message", {}).get("content") or "").strip(),
        "reasoning": (body.get("message", {}).get("thinking") or "").strip(),
        "eval_count": body.get("eval_count"),
        "seconds": round(elapsed, 2),
    }


def report(tag: str, r: dict) -> None:
    ptok = r.get("completion") or r.get("eval_count")
    content = r["content"]
    try:
        parsed = json.loads(content)
        ok = f"ok {parsed}"
    except Exception as exc:  # noqa: BLE001
        ok = f"解析失败（{exc.__class__.__name__}）"
    print(f"  {tag:34} {r['seconds']:>6.2f}s  生成 token={ptok!s:>5}  思考链={len(r['reasoning']):>4} 字  {ok}")


def main() -> None:
    print(f"模型: {MODEL}   任务: 三分类，要求纯 JSON")
    print("-" * 110)

    report("OpenAI 端点，think 未传", openai_call(content=TASK, think=None))
    report("OpenAI 端点，think=false", openai_call(content=TASK, think=False))
    report("OpenAI 端点，prompt+/no_think", openai_call(content=TASK + "\n/no_think", think=None))
    report("原生端点，think=false", native_call(content=TASK, think=False))

    print("-" * 110)
    print("判据：生成 token 越少、延迟越低越好；JSON 必须可解析。")


if __name__ == "__main__":
    main()
