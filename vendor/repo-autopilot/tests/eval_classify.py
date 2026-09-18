"""步骤 0.2 验收①：本地小模型的 issue 三分类准确率。

## 口径声明（重要，别误读结果）

本脚本测的是「预测**维护者标签**」的准确率，而不是「语义正确性」。
两者不等价：有的仓库把用户报的崩溃也打上 `question`（当支持论坛用），
此时"预测成 bug"在语义上或许更对，但在本评估里算错。

这个口径是刻意选的：维护者标签是**独立于我的人工判断**，能防"我出题我判分"。
代价是它含着各仓库自己的标签习惯。因此在报告里同时输出：
  * 总体准确率
  * 混淆矩阵（能看出错在哪个方向）
  * 置信度分布（模型自报的 confidence 是否与实际对错相关）

## 输出契约

所有模型输出必须过 JSON Schema（pydantic），失败重试 ≤3 次（指数退避），
仍失败记为该条预测失败并计入错误——**不用自由文本凑数**（路线 0.1 第 4 条）。
"""

from __future__ import annotations

import json
import os
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import BaseModel, ValidationError
from pydantic import Field as PField

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# 本机模型一律走这个客户端：httpx 对本机明文 HTTP 一律 502（见模块注释）
from src.gateway.local_client import (
    DEFAULT_BASE,
    LocalModelError,
    chat_json,
)

CORPUS = ROOT / "state" / "corpus" / "issues.jsonl"
OUT_DIR = ROOT / "state" / "reports"

MODEL = os.environ.get("AUTOPILOT_LOCAL_MODEL", "qwen3:4b")
BASE_URL = os.environ.get("AUTOPILOT_LOCAL_URL", DEFAULT_BASE)
LABELS = ("bug", "feature", "question")
# 分类任务必须关思考链（路线易错点 6）。原生端点支持这个开关。
THINK = os.environ.get("AUTOPILOT_THINK", "false").lower() == "true"

SYSTEM_PROMPT = (
    "你是开源仓库的 issue 分类器。只输出 JSON，不要任何解释、不要 markdown 代码块。"
)

USER_TEMPLATE = """把下面这条 issue 分类到三个标签之一。

标签定义：
- bug：报告已有功能出错、崩溃、行为不符合预期，需要修改代码才能解决。
- feature：请求新增或改进功能，当前不存在该能力。
- question：向维护者提问、寻求使用方法或配置帮助，不需要修改代码。

只输出这个 JSON：{{"label": "bug|feature|question", "confidence": 0-1 的小数}}

issue 标题：{title}

issue 正文：
{body}
"""


class Prediction(BaseModel):
    """输出 schema。校验失败即视为该条预测失败，绝不猜测补齐。"""

    label: str = PField(..., description="bug / feature / question")
    confidence: float = PField(..., ge=0.0, le=1.0)


@dataclass
class RunResult:
    total: int = 0
    correct: int = 0
    schema_failures: int = 0
    errors: list[str] = field(default_factory=list)
    confusion: Counter = field(default_factory=Counter)
    confidence_buckets: Counter = field(default_factory=Counter)
    per_label: Counter = field(default_factory=Counter)
    per_label_correct: Counter = field(default_factory=Counter)
    latencies: list[float] = field(default_factory=list)


def load_corpus(limit: int | None) -> list[dict]:
    items = [
        json.loads(line)
        for line in CORPUS.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if limit:
        items = items[:limit]
    return items


def classify(title: str, body: str) -> tuple[Prediction | None, float, str]:
    """
    调用本地模型并校验 schema。返回 (预测或 None, 耗时, 备注)。

    传输重试在 local_client.chat_json 里；这里只处理 schema 校验失败的重试，
    两者分开，避免把"服务不可达"算成"模型不会输出 JSON"。
    """
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": USER_TEMPLATE.format(title=title, body=body)},
    ]

    last_error = ""
    for attempt in range(3):
        try:
            result = chat_json(messages, model=MODEL, base_url=BASE_URL, think=THINK, timeout=300)
            content = result.content
            # 模型偶尔包 markdown 代码块，剥掉再校验（不改变内容）
            if content.startswith("```"):
                content = content.strip("`")
                if content.lower().startswith("json"):
                    content = content[4:]
                content = content.split("```")[0].strip()
            return Prediction.model_validate_json(content), result.seconds, ""
        except ValidationError as exc:
            last_error = f"schema: {exc.errors()[:1]}"
        except LocalModelError as exc:
            last_error = f"transport: {exc}"
        time.sleep(2 ** attempt)   # 1s / 2s / 4s 指数退避

    return None, 0.0, last_error


def main() -> int:
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else None
    items = load_corpus(limit)
    print("=" * 76)
    print("步骤 0.2 验收①  issue 三分类准确率")
    print(f"  模型      : {MODEL}")
    print(f"  端点      : {BASE_URL}")
    print(f"  语料      : {len(items)} 条")
    print("  口径      : 预测维护者标签（非语义正确性）")
    print("=" * 76)

    r = RunResult()
    details: list[dict] = []
    started_all = time.perf_counter()

    for idx, item in enumerate(items, 1):
        pred, elapsed, note = classify(item["title"], item["body"])
        r.total += 1
        r.per_label[item["gold"]] += 1

        if pred is None:
            r.schema_failures += 1
            r.errors.append(f"{item['repo']}#{item['number']}: {note}")
            details.append({**item, "predicted": None, "confidence": None, "correct": False, "note": note})
        elif pred.label not in LABELS:
            # schema 形状对但取值非法：同样算失败，不猜
            r.schema_failures += 1
            r.errors.append(f"{item['repo']}#{item['number']}: 非法取值 {pred.label!r}")
            details.append({**item, "predicted": pred.label, "confidence": pred.confidence,
                            "correct": False, "note": "非法取值"})
        else:
            correct = pred.label == item["gold"]
            if correct:
                r.correct += 1
                r.per_label_correct[item["gold"]] += 1
            r.confusion[(item["gold"], pred.label)] += 1
            r.confidence_buckets[(round(pred.confidence, 1), correct)] += 1
            r.latencies.append(elapsed)
            details.append({**item, "predicted": pred.label, "confidence": pred.confidence,
                            "correct": correct, "note": ""})

        if idx % 10 == 0 or idx == r.total:
            acc = r.correct / idx * 100
            mean = sum(r.latencies) / len(r.latencies) if r.latencies else 0.0
            print(f"  [{idx:>3}/{r.total}] 累计准确率 {acc:5.1f}%   平均延迟 {mean:5.1f}s")

    total_time = time.perf_counter() - started_all
    valid = r.total - r.schema_failures
    accuracy = r.correct / r.total * 100 if r.total else 0

    print("\n" + "=" * 76)
    print("结果")
    print("=" * 76)
    print(f"  样本数          : {r.total}")
    print(f"  正确            : {r.correct}")
    print(f"  准确率          : {accuracy:.1f}%")
    print(f"  schema 失败     : {r.schema_failures}（计为错误，未猜测补齐）")
    print("  有效样本延迟    : ", end="")
    if r.latencies:
        print(f"平均 {sum(r.latencies)/len(r.latencies):.1f}s，"
              f"最短 {min(r.latencies):.1f}s，最长 {max(r.latencies):.1f}s")
    else:
        print("无有效样本（全部 schema 失败）")
    print(f"  总耗时          : {total_time/60:.1f} 分钟")

    print("\n  分类别:")
    for label in LABELS:
        n = r.per_label[label]
        c = r.per_label_correct[label]
        print(f"    {label:9} {c:>3}/{n:>3}  {c/n*100 if n else 0:5.1f}%")

    print("\n  混淆矩阵（行=真实标签，列=预测）:")
    print(f"    {'':10}" + "".join(f"{l:>10}" for l in LABELS))
    for gold in LABELS:
        row = "".join(f"{r.confusion.get((gold, p), 0):>10}" for p in LABELS)
        print(f"    {gold:10}{row}")

    print("\n  置信度与实际对错（看模型自报的 confidence 是否可信）:")
    for conf in sorted({k[0] for k in r.confidence_buckets}, reverse=True):
        right = r.confidence_buckets.get((conf, True), 0)
        wrong = r.confidence_buckets.get((conf, False), 0)
        n = right + wrong
        if n:
            print(f"    confidence≈{conf:.1f}: {right}/{n} 正确 ({right/n*100:.0f}%)")

    if r.errors:
        print(f"\n  schema/传输失败共 {len(r.errors)} 条，前 8 条原因（完整原因必须可见——")
        print("  前几轮教训：报错里缺字段，代价是一整轮排查）:")
        for e in r.errors[:8]:
            print(f"    {e[:300]}")

    # 落盘
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y-%m-%d")
    (OUT_DIR / f"eval_classify_{stamp}.json").write_text(
        json.dumps(
            {
                "model": MODEL,
                "endpoint": BASE_URL,
                "corpus_size": r.total,
                "ground_truth_caveat": "答案为维护者标签；个别仓库用 question 当支持论坛，语义上可能是 bug",
                "accuracy_pct": round(accuracy, 2),
                "correct": r.correct,
                "schema_failures": r.schema_failures,
                "valid_samples": valid,
                "per_label": {l: {"n": r.per_label[l], "correct": r.per_label_correct[l]} for l in LABELS},
                "confusion": {f"{g}->{p}": c for (g, p), c in r.confusion.items()},
                "latency_s": {
                    "mean": round(sum(r.latencies) / len(r.latencies), 2) if r.latencies else None,
                    "min": round(min(r.latencies), 2) if r.latencies else None,
                    "max": round(max(r.latencies), 2) if r.latencies else None,
                },
                "total_minutes": round(total_time / 60, 2),
                "errors": r.errors[:20],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    (OUT_DIR / f"eval_classify_{stamp}_details.jsonl").write_text(
        "\n".join(json.dumps(d, ensure_ascii=False) for d in details), encoding="utf-8"
    )
    print(f"\n  明细已写入 {OUT_DIR}/eval_classify_{stamp}_details.jsonl")

    print("\n" + "=" * 76)
    if accuracy >= 70:
        print(f"PASS: 准确率 {accuracy:.1f}% ≥ 70%（路线 0.2 验收①）")
        return 0
    print(f"FAIL: 准确率 {accuracy:.1f}% < 70%，需升档或改选型（路线 0.2 验收①）")
    return 1


if __name__ == "__main__":
    sys.exit(main())
