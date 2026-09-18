"""把"两个模型一致、但与仓库标签不同"的条目整理成**逐条可裁决**的清单。

    python tools/review_adjudication.py                 # 本地模型 vs flash
    python tools/review_adjudication.py A.json B.json   # 指定两份回放报告

产物：`state/reports/triage-adjudication-review.md`

## 为什么要单独做这一步

"仓库标签有噪声"是一句结论，但它落不到地上：真正要人做的是**逐条判断**
"仓库标签对，还是模型判断对"。这件事必须一次给足材料 ——
标题、仓库标签、两个档位的判断与置信度、模型给的理由、正文摘录 ——
否则人还得自己去翻 JSON 和 GitHub，那就等于没审核。

裁决结果请写进 `state/corpus/triage-adjudicated.jsonl`（用
`src.bounce.Adjudication` 追加），它会同时：
① 成为 3.1 评测的真值来源（仓库标签降为兜底）；
② 把这条样本从 holdout 移进 dev（看过答案的题不能再当考卷）；
③ 让 3.3 的"已裁决样本不得再被打回"这条断言变成真的。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

REPORT_DIR = ROOT / "state" / "reports"
REPLAY = ROOT / "state" / "corpus" / "triage-replay.jsonl"
TARGET = REPORT_DIR / "triage-adjudication-review.md"
BODY_EXCERPT = 600


def latest_report(tier: str) -> Path:
    candidates = sorted(REPORT_DIR.glob(f"triage-eval-*-{tier}*.json"))
    if not candidates:
        raise FileNotFoundError(f"找不到 {tier} 档的回放报告")
    return candidates[-1]


def load_rows(path: Path) -> dict[tuple[str, int], dict]:
    report = json.loads(path.read_text(encoding="utf-8"))
    return {
        (row["repo"], row["number"]): row
        for row in report["rows"]
        if "predicted" in row and "repo" in row
    }


def load_replay() -> dict[tuple[str, int], dict]:
    rows: dict[tuple[str, int], dict] = {}
    if not REPLAY.exists():
        return rows
    for line in REPLAY.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        rows[(row["repo"], row["number"])] = row
    return rows


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

    a_path = Path(sys.argv[1]) if len(sys.argv) > 2 else latest_report("local_small")
    b_path = Path(sys.argv[2]) if len(sys.argv) > 2 else latest_report("flash_api")
    a, b = load_rows(a_path), load_rows(b_path)
    replay = load_replay()

    shared = sorted(set(a) & set(b))
    agree_wrong = [key for key in shared if a[key]["predicted"] == b[key]["predicted"] != a[key]["truth"]]

    lines = [
        "# 逐条裁决：两个模型一致、但与仓库标签不同",
        "",
        f"- 对比：`{a_path.name}`（本地小模型） vs `{b_path.name}`（flash）　共同样本 {len(shared)} 条",
        f"- 本清单 {len(agree_wrong)} 条 —— 两个**不同档位**的模型看到同一段文字得出同一个结论，",
        "  而仓库标签是第三种。",
        "",
        "请对每一条回答一个问题：**仓库标签对，还是模型判断对？**",
        "（都不是的话，请给出你认为对的标签）",
        "",
    ]
    for index, key in enumerate(agree_wrong, start=1):
        row = a[key]
        body = (replay.get(key, {}).get("body") or "").strip()
        lines += [
            f"## {index}. `{key[0]}#{key[1]}`",
            "",
            f"- **标题**：{row.get('title') or '（无标题）'}",
            f"- **仓库标签**：`{row['truth']}`",
            (
                f"- **本地小模型**：`{a[key]['predicted']}`"
                f"（置信度 {float(a[key].get('confidence') or 0):.2f}）"
                f"　**flash**：`{b[key]['predicted']}`"
            ),
            f"- **模型给的理由**：{row.get('reason') or '（模型没给）'}",
            f"- **正文摘录**：{body[:BODY_EXCERPT] or '（无正文）'}",
            "",
            "**请裁决**：仓库标签对 / 模型对 / 都不是（应为 ______）",
            "",
        ]

    lines += [
        "---",
        "",
        "## 裁决之后我会做什么",
        "",
        "把每条按 `src.bounce.Adjudication` 追加进 `state/corpus/triage-adjudicated.jsonl`，然后：",
        "① 3.1 评测的真值优先取裁决结果（仓库标签降为兜底）；",
        "② 这些样本从 holdout 移进 dev（看过答案的题不能再当考卷）；",
        "③ 3.3「已裁决样本不得再被打回」这条断言从空断言变成真断言。",
        "",
        "**任何一条的裁决都不会改动验收线**；它只决定「下一轮该改判据，还是改真值来源」。",
        "",
    ]
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    TARGET.write_text("\n".join(lines), encoding="utf-8")

    print(f"共同样本 {len(shared)} 条；两模型一致但与标签不同 {len(agree_wrong)} 条")
    for index, key in enumerate(agree_wrong, start=1):
        row = a[key]
        print(f"  {index}. {key[0]}#{key[1]}  标签={row['truth']:<9} 两模型都判={a[key]['predicted']:<9} "
              f"{(row.get('title') or '')[:48]}")
    print(f"\n可裁决清单：{TARGET}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
