"""把人类裁决写进错题本 `state/corpus/triage-adjudicated.jsonl`（**只增不改**）。

    python tools/record_adjudication.py --from-json state/decisions/adjudications-2026-09-12.json
    python tools/record_adjudication.py --from-json ... --migrate

## 为什么用"决策文件 + 一条命令"而不是手写记录

裁决是**人类说的话**，必须留痕：决策文件（`state/decisions/*.json`）里存着当时的
原文、依据、逐条理由；本工具只负责把它翻译成 `Adjudication` 追加进错题本。
两样东西分开的好处是：错题本是**机器读的真值**，决策文件是**人读的档案**，
以后有人问"这条为什么标成 feature"，两边都能对上。

`--migrate` 会跑 `migrate_cohorts()`：裁决过的样本从 holdout 移进 dev
（看过答案的题不能再当考卷）。幂等：同一 (key, verdict, by, basis) 已经记过就跳过。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.bounce import (
    Adjudication,
    cohort_counts,
    load,
    migrate_cohorts,
    truth_map,
)
from src.bounce.adjudicated import append as append_adjudication


def already_recorded(key: str, verdict: str, by: str, basis: str) -> bool:
    for row in load():
        if (
            str(row.get("key")) == key
            and str(row.get("verdict")) == verdict
            and str(row.get("by")) == by
            and str(row.get("basis")) == basis
        ):
            return True
    return False


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

    parser = argparse.ArgumentParser(description="记录人类裁决")
    parser.add_argument("--from-json", required=True, help="决策文件（见 state/decisions/）")
    parser.add_argument("--migrate", action="store_true", help="顺便把裁决过的样本移进 dev")
    args = parser.parse_args()

    payload = json.loads(Path(args.from_json).read_text(encoding="utf-8"))
    by = str(payload.get("by") or "human")
    basis = str(payload.get("basis") or "")
    default_verdict = str(payload.get("verdict") or "model")

    added = skipped = 0
    for item in payload.get("items") or []:
        key = str(item["key"])
        verdict = str(item.get("verdict") or default_verdict)
        item_basis = str(item.get("basis") or basis)
        if already_recorded(key, verdict, by, item_basis):
            skipped += 1
            print(f"  跳过（已记录）：{key}")
            continue
        record = Adjudication(
            key=key,
            repo_label=item.get("repo_label"),
            model_label=item.get("model_label"),
            verdict=verdict,
            by=by,
            basis=item_basis,
            final_label=item.get("final_label"),
        )
        append_adjudication(record)
        added += 1
        print(f"  已记录：{key}　仓库标签={record.repo_label} → 真值={record.truth}（裁决={verdict}）")

    print(f"\n新增 {added} 条，跳过 {skipped} 条；错题本现有真值 {len(truth_map())} 条")

    if args.migrate:
        result = migrate_cohorts()
        print(f"cohort 迁移：moved={result['moved']} already={result['already']} "
              f"considered={result['considered']}")
        print(f"当前队列分布：{cohort_counts()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
