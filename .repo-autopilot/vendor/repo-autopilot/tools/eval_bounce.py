"""3.3 验收的第三层：在**真实回放数据**上量打回率与误打回率。

    python tools/eval_bounce.py                 # 用 3.1 那次回放的真实输出（默认，秒级）
    python tools/eval_bounce.py --live          # 现场重跑分类器（慢，~7 分钟）

## 为什么默认不现场重跑

打回通路的输入是"标签 + 模型判断 + 置信度"，而这三样 3.1 的验收已经**真实跑过并落盘**了
（`state/reports/triage-eval-*-local_small-holdout1.json`）。现场重跑一遍只会让验收慢 7 分钟、
结果却完全一样（分类器温度 0）。所以默认读那份报告，读不到才报 BLOCKED 并提示怎么生成。

## 两条线，第二条更硬

    打回率      ≤20%   —— 通路有没有在动；长期 0% 说明它形同虚设（报告会提示，不算失败）
    误打回率    ≤5%    —— **把标着 bug 的 issue 打回**的比例：真有缺陷却拒绝修

第二层还有一条：**已被人裁决过的样本不能再被打回** —— 裁决意味着真值已经修正，
判据应该跟着修正后的真值走；它还被原判据打回，说明回流根本没生效。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.bounce import bounce_rates, latest_verdicts, precheck
from src.bounce.adjudicated import REPLAY_PATH

REPORT_DIR = ROOT / "state" / "reports"
CORPUS_DIR = ROOT / "state" / "corpus"
BOUNCE_CEILING = 0.20
FALSE_BOUNCE_CEILING = 0.05

#: 3.1 冻结回放集里 holdout 队列的报告名（0.5 的人类复核结论：只用没调过参的样本报分）
TRIAGE_REPORT_HINT = "triage-eval-*-local_small-holdout1.json"
#: **金样本**：3.1 在 holdout 上跑出来的逐条预测，冻结成只读夹具。
#: 为什么必须冻结：本地小模型即使温度 0 也不是逐位可复现，而本步的门禁
#: （误打回率 ≤5%）离红线只有 0.1 个百分点 —— 读"最近一次报告"会让门禁随
#: 3.1 是否刚跑过而抖动，抖出一条就是假 BLOCKED。见
#: `state/findings/bounce-gate-margin.md`。
GOLDEN_PATH = CORPUS_DIR / "triage-holdout-predictions.jsonl"


def load_golden() -> tuple[list[dict], str] | None:
    if not GOLDEN_PATH.exists():
        return None
    rows: list[dict] = []
    meta: dict = {}
    with open(GOLDEN_PATH, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            payload = json.loads(line)
            if payload.get("_meta"):
                meta = payload          # 元信息行：顶层就带 generated_at / model / source_report
                continue
            rows.append(payload)
    label = f"金样本 {GOLDEN_PATH.name}（生成于 {meta.get('generated_at', '?')}，"
    label += f"模型 {meta.get('model', '?')}）"
    return rows, label


def freeze_golden() -> int:
    """把当前 3.1 holdout 报告的逐条预测写成金样本（只在分类器变更后重做一次）。"""
    rows, path = load_rows(fallback_only=True)
    CORPUS_DIR.mkdir(parents=True, exist_ok=True)
    meta = {
        "_meta": True,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source_report": path.name,
        "model": "local_small",
        "note": "3.1 在冻结 holdout 上的逐条预测，供 3.3 门禁做可复现输入；分类器变更后重新冻结",
    }
    with open(GOLDEN_PATH, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(meta, ensure_ascii=False) + "\n")
        handle.writelines(json.dumps(
                    {
                        "repo": row["repo"],
                        "number": row["number"],
                        "title": row.get("title", ""),
                        "truth": row.get("truth"),
                        "predicted": row.get("predicted"),
                        "confidence": row.get("confidence"),
                    },
                    ensure_ascii=False,
                )
                + "\n" for row in rows)
    print(f"金样本已冻结：{GOLDEN_PATH}（{len(rows)} 条，来源 {path.name}）")
    return 0


def load_rows(fallback_only: bool = False) -> tuple[list[dict], Path | str]:
    """
    优先读**金样本**；没有再退回"最近一次报告"（并明确警告不可复现）。

    `fallback_only=True` 时只读报告（冻金样本时用，避免拿金样本冻金样本）。
    """
    if not fallback_only:
        golden = load_golden()
        if golden is not None:
            return golden
    candidates = sorted(REPORT_DIR.glob(TRIAGE_REPORT_HINT))
    if not candidates:
        raise FileNotFoundError(
            f"既没有金样本 {GOLDEN_PATH.name}，也找不到 {TRIAGE_REPORT_HINT}。先跑："
            "python tools/eval_triage.py --from-replay --split holdout --tag holdout1"
        )
    report = json.loads(candidates[-1].read_text(encoding="utf-8"))
    return [row for row in report["rows"] if "predicted" in row], candidates[-1]


def load_model_index() -> dict[str, dict]:
    """
    把各次**本地档**回放报告里的（模型判断、置信度）汇成索引。

    裁决层要用它：错题本里的样本已经被移出 holdout（看过答案的题不能再当考卷），
    所以"已裁决样本不得再被打回"这条断言必须回到**当初判过它们的那次回放**里去复查，
    否则它永远是一条空断言（实测踩过：写完裁决记录后这一层仍显示"0 条"）。
    """
    index: dict[str, dict] = {}
    for path in sorted(REPORT_DIR.glob("triage-eval-*-local_small*.json")):
        try:
            report = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for row in report.get("rows", []):
            if "predicted" not in row or "repo" not in row:
                continue
            index[f"{row['repo']}#{row['number']}"] = {
                "predicted": row.get("predicted"),
                "confidence": row.get("confidence"),
                "repo_label": row.get("repo_label") or row.get("truth"),
                "title": row.get("title", ""),
                "source": path.name,
            }
    return index


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

    parser = argparse.ArgumentParser(description="3.3 打回通路回放验收")
    parser.add_argument("--live", action="store_true", help="现场重跑分类器（慢）")
    parser.add_argument(
        "--freeze",
        action="store_true",
        help="把当前 3.1 holdout 报告冻结成金样本（分类器变更后才需要重做）",
    )
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()

    if args.freeze:
        return freeze_golden()

    if args.live:
        print("现场重跑模式：用 3.1 的分类器逐条判断（要 Ollama，约 7 分钟）")
        from src.triage import FeedbackClassifier, RawIssue

        classifier = FeedbackClassifier(tier="local_small")
        replay = [
            json.loads(line)
            for line in REPLAY_PATH.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        rows = []
        for row in replay:
            if (row.get("cohort") or "dev") != "holdout":
                continue
            issue = RawIssue(
                repo=row.get("repo", ""),
                number=int(row.get("number") or 0),
                title=row.get("title") or "",
                body=row.get("body") or "",
                labels=[row["label"]] if row.get("label") else [],
                state="closed",
            )
            try:
                result = classifier.classify(issue)
            except Exception as exc:  # noqa: BLE001
                print(f"  {issue.repo}#{issue.number} 分类失败：{str(exc)[:60]}")
                continue
            rows.append(
                {
                    "repo": issue.repo,
                    "number": issue.number,
                    "truth": row.get("label"),
                    "predicted": result.verdict.label,
                    "confidence": result.verdict.confidence,
                }
            )
        source = "live(local_small)"
    else:
        rows, source_obj = load_rows()
        if isinstance(source_obj, str):
            source = source_obj          # 金样本：自带说明（含生成时间与模型）
        else:
            source = f"3.1 回放报告 {source_obj.name}（**未冻结**：结果可能逐次抖动）"

    if args.limit:
        rows = rows[: args.limit]

    verdicts = latest_verdicts()          # 人工裁决（错题本）
    decisions: list[tuple[bool, str]] = []
    bounced_rows: list[dict] = []
    adjudicated_but_bounced: list[str] = []
    for row in rows:
        key = f"{row['repo']}#{row['number']}"
        repo_label = row.get("truth") or ""
        # 裁决过的样本用**裁决后的真值**：判据该跟着修正后的真值走
        effective = (verdicts.get(key) or {}).get("truth") or repo_label
        bounce = precheck(effective, row.get("predicted"), float(row.get("confidence") or 0.0))
        decisions.append((bool(bounce), repo_label))
        if bounce is not None:
            bounced_rows.append(
                {
                    "key": key,
                    "reason": bounce.reason.value,
                    "repo_label": repo_label,
                    "effective_label": effective,
                    "model_label": row.get("predicted"),
                    "confidence": round(float(row.get("confidence") or 0.0), 3),
                    "title": (row.get("title") or "")[:60],
                    "adjudicated": key in verdicts,
                }
            )
            if key in verdicts:
                adjudicated_but_bounced.append(key)

    rates = bounce_rates(decisions)
    total = len(decisions)
    adjudicated_total = sum(1 for row in rows if f"{row['repo']}#{row['number']}" in verdicts)

    # 裁决层单独复查：错题本里的样本已经不算考卷（不在 holdout 里了），
    # 所以要用**当初判过它们的那次回放**来判断"现在还会不会被打回"。
    model_index = load_model_index()
    reviewed: list[dict] = []
    for key, record in verdicts.items():
        model = model_index.get(key)
        if not model:
            continue
        decision = precheck(
            str(record.get("truth") or ""),
            model["predicted"],
            float(model.get("confidence") or 0.0),
        )
        reviewed.append(
            {
                "key": key,
                "adjudicated_truth": record.get("truth"),
                "model_label": model["predicted"],
                "confidence": round(float(model.get("confidence") or 0.0), 3),
                "source": model["source"],
                "bounced": bool(decision),
            }
        )
    adjudicated_but_bounced = sorted(
        set(adjudicated_but_bounced) | {row["key"] for row in reviewed if row["bounced"]}
    )

    bounce_ok = rates["bounce_rate"] <= BOUNCE_CEILING
    false_ok = rates["false_bounce_rate"] <= FALSE_BOUNCE_CEILING
    # 裁决过的样本必须不再被打回（没有裁决样本时这条是空断言，如实标注）
    adjudicated_ok = not adjudicated_but_bounced
    passed = bounce_ok and false_ok and adjudicated_ok and total > 0
    # 让打回率异常低时也有人知道：通路没被使用本身就是失败信号
    dormant_warning = rates["bounce_rate"] < 0.01

    print(f"来源：{source}")
    print(f"样本 {total} 条（其中已裁决 {adjudicated_total} 条）")
    print(f"打回 {len(bounced_rows)} 条：打回率 {rates['bounce_rate']:.1%}（要求 ≤{BOUNCE_CEILING:.0%}）")
    print(
        f"误打回（把标着 bug 的打回）{sum(1 for r in bounced_rows if r['repo_label'] == 'bug')} 条："
        f"{rates['false_bounce_rate']:.1%}（要求 ≤{FALSE_BOUNCE_CEILING:.0%}）"
    )
    if reviewed:
        print(
            f"已裁决样本复查 {len(reviewed)} 条（错题本共 {len(verdicts)} 条）："
            f"再次被打回 {len(adjudicated_but_bounced)} 条（要求 0）"
        )
    else:
        print(
            f"已裁决样本复查：0 条（错题本里有 {len(verdicts)} 条，但都找不到当初的模型判断）"
            "—— 第三层断言此刻是空的"
        )
    if dormant_warning:
        print("⚠️ 打回率 <1%：通路没被使用本身就是失败信号，检查预检是不是根本没触发")
    print(f"结论：{'达标' if passed else '**未达标**'}")

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    report = {
        "date": stamp,
        "source": source,
        "samples": total,
        "adjudicated_samples": adjudicated_total,
        "adjudicated_reviewed": reviewed,
        "rates": rates,
        "ceilings": {"bounce_rate": BOUNCE_CEILING, "false_bounce_rate": FALSE_BOUNCE_CEILING},
        "bounced": bounced_rows,
        "adjudicated_but_bounced": adjudicated_but_bounced,
        "dormant_warning": dormant_warning,
        "passed": passed,
    }
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    target = REPORT_DIR / f"bounce-eval-{stamp}.json"
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"报告：{target}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
