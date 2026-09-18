"""定向实验：怎样才能真正拿到纯 JSON（打掉思考链）。

背景：评估器用原生 /api/chat + think=false 跑，模型仍把中文思考过程当正文输出，
20 条里 20 条 schema 失败。路线易错点 6 正是说这件事：
"推理模型做分类 JSON 不稳 —— 分类/打分只用非推理模型，温度 0"。

本实验并列对比 6 种组合，判据是**能不能拿到可解析的 JSON**，其次才是 token 与延迟。
每格跑 3 条真实语料，避免单次偶然。

结论将直接决定 1.3 网关的默认调用参数，以及 0.2 的模型档位选择。
"""

from __future__ import annotations

import json
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / "state" / "corpus" / "issues.jsonl"
BASE = "http://127.0.0.1:11434"
MODEL = "qwen3:4b"

SYSTEM = "你是开源仓库的 issue 分类器。只输出 JSON，不要任何解释、不要 markdown 代码块。"

USER = """把下面这条 issue 分类到三个标签之一。

标签定义：
- bug：报告已有功能出错、崩溃、行为不符合预期，需要修改代码才能解决。
- feature：请求新增或改进功能，当前不存在该能力。
- question：向维护者提问、寻求使用方法或配置帮助，不需要修改代码。

只输出这个 JSON：{{"label": "bug|feature|question", "confidence": 0-1 的小数}}

issue 标题：{title}

issue 正文：
{body}
"""


def post(path: str, payload: dict, timeout: float = 300.0) -> dict:
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        BASE + path, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def extract_json(text: str) -> dict | None:
    """从可能夹带解释的文本里抽取第一个 JSON 对象。仅用于诊断，不用于评估。"""
    m = re.search(r"\{[^{}]*\}", text)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:  # noqa: BLE001
        return None


def run_one(tag: str, item: dict, variant: dict) -> dict:
    messages = []
    if SYSTEM in variant.get("keep_system", []):
        messages.append({"role": "system", "content": SYSTEM})
    user = USER.format(title=item["title"], body=item["body"])
    if variant.get("no_think"):
        user += "\n/no_think"
    if variant.get("format_json"):
        user += '\n\n只允许输出形如 {"label":"bug","confidence":0.9} 的单行 JSON。'
    messages.append({"role": "user", "content": user})

    payload: dict = {
        "model": MODEL,
        "stream": False,
        "messages": messages,
        "options": {"temperature": 0},
    }
    if "think" in variant:
        payload["think"] = variant["think"]
    if variant.get("format_field"):
        payload["format"] = "json"

    t0 = time.perf_counter()
    try:
        body = post("/api/chat", payload)
    except urllib.error.HTTPError as e:
        return {"tag": tag, "ok": False, "err": f"HTTP {e.code}", "sec": 0, "tok": None}
    except Exception as e:  # noqa: BLE001
        return {"tag": tag, "ok": False, "err": type(e).__name__, "sec": 0, "tok": None}

    sec = time.perf_counter() - t0
    msg = body.get("message") or {}
    content = (msg.get("content") or "").strip()
    thinking = (msg.get("thinking") or "").strip()
    tok = body.get("eval_count")

    strict_ok = False
    try:
        parsed = json.loads(content)
        strict_ok = isinstance(parsed.get("label"), str)
    except Exception:  # noqa: BLE001, S110
        # 这是一个**诊断脚本**：它就是在统计"严格 JSON 解析失败率"，所以解析失败是
        # 它要测的数据，不是要报的错（下面用 `extract_json` 再试一次并记进结果）。
        pass

    recovered = extract_json(content) if not strict_ok else None
    return {
        "tag": tag,
        "ok": strict_ok,
        "recovered": recovered is not None,
        "content_head": content[:70],
        "thinking_len": len(thinking),
        "sec": sec,
        "tok": tok,
    }


VARIANTS: list[tuple[str, dict]] = [
    ("原生 think=false",              {"think": False, "keep_system": [SYSTEM]}),
    ("原生 think=false + /no_think",  {"think": False, "no_think": True, "keep_system": [SYSTEM]}),
    ("原生 think=false + format=json", {"think": False, "format_field": True, "keep_system": [SYSTEM]}),
    ("原生 think=false + format + no_think", {"think": False, "format_field": True, "no_think": True, "keep_system": [SYSTEM]}),
    ("原生 不传 think + format=json",  {"format_field": True, "keep_system": [SYSTEM]}),
    ("原生 think=false + 无 system",   {"think": False}),
]


def main() -> int:
    items = [
        json.loads(line)
        for line in CORPUS.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ][:3]

    print(f"模型 {MODEL}   语料 {len(items)} 条   判据：严格 JSON 可解析")
    print("=" * 104)

    summary = []
    for tag, variant in VARIANTS:
        results = [run_one(tag, item, variant) for item in items]
        ok_count = sum(1 for r in results if r.get("ok"))
        rec_count = sum(1 for r in results if r.get("recovered"))
        secs = [r["sec"] for r in results if r.get("sec")]
        toks = [r["tok"] for r in results if r.get("tok")]
        think_lens = [r.get("thinking_len", 0) for r in results]

        print(f"\n{tag}")
        for r in results:
            mark = "ok  " if r.get("ok") else ("可救 " if r.get("recovered") else "FAIL")
            print(f"  {mark} {r['sec']:6.1f}s  tok={r['tok']!s:>5}  思考={r.get('thinking_len',0):>5} 字  {r.get('content_head','')[:60]!r}")
            if r.get("err"):
                print(f"         错误: {r['err']}")

        summary.append(
            {
                "tag": tag,
                "strict_ok": ok_count,
                "recoverable": rec_count,
                "n": len(items),
                "mean_sec": round(sum(secs) / len(secs), 1) if secs else None,
                "mean_tok": round(sum(toks) / len(toks)) if toks else None,
                "mean_thinking": round(sum(think_lens) / len(think_lens)) if think_lens else 0,
            }
        )

    print("\n" + "=" * 104)
    print("汇总（按严格可解析数排序）")
    print(f"  {'组合':38} {'严格JSON':>8} {'可救':>5} {'均延迟':>8} {'均token':>8} {'均思考字数':>10}")
    for s in sorted(summary, key=lambda x: (-x["strict_ok"], x["mean_sec"] or 0)):
        print(
            f"  {s['tag']:38} {s['strict_ok']:>4}/{s['n']:<3} {s['recoverable']:>5} "
            f"{s['mean_sec']!s:>8} {s['mean_tok']!s:>8} {s['mean_thinking']:>10}"
        )

    out = ROOT / "state" / "reports" / "think-diagnosis.json"
    out.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n写入 {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
