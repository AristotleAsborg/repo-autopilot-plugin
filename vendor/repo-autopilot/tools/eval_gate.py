"""4.3 验收：两个**真跑**的场景（真 pytest、真 ruff、真补丁）。

    python tools/eval_gate.py

路线 4.3 的验收原文就是两个场景：

- **场景 A**：故意喂"过目标测试但破坏另一测试"的补丁 → 断言被第 ② 关拦下；
- **场景 B**：故意喂试图修改测试文件的补丁 → 断言被第 ④ 关（黑名单）拦下。

再加**场景 C**（反向）：「什么都不破坏的补丁必须放行」——
门禁只会拦不会放，就是另一种坏掉（会把所有修复都堵死）。
三个场景都在 `tests/fixtures/sandbox-repos/sandbox-clean` 的**副本**上跑，
原仓库只读；不调模型、不联网。

产物：`state/reports/gate-eval-<date>.json`
"""

from __future__ import annotations

import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.gatekeep import capture_baseline, run_gate
from src.repair import make_patch

FIXTURE = ROOT / "tests" / "fixtures" / "sandbox-repos" / "sandbox-clean"
REPORT_DIR = ROOT / "state" / "reports"
WORK = ROOT / "state" / "gate-eval"

TARGET_CMD = [sys.executable, "-m", "pytest", "tests/test_money.py", "-q", "-p", "no:cacheprovider"]
FULL_CMD = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"]
LINT_CMD = [sys.executable, "-m", "ruff", "check", "."]


def prepare() -> None:
    if WORK.exists():
        shutil.rmtree(WORK, ignore_errors=True)
    WORK.mkdir(parents=True, exist_ok=True)


def patch_break_other_module() -> Path:
    """
    场景 A 的补丁：**目标测试（money）照样全绿**，但把 `report.summary()` 的金额改了 1 分，
    `tests/test_report.py` 会红。这正是"修复循环自己看不见"的那种破坏。
    """
    original = (FIXTURE / "ledger/report.py").read_text(encoding="utf-8")
    updated = original.replace("amount.cents for name", "amount.cents + 1 for name", 1)
    text = make_patch([("ledger/report.py", original, updated)])
    path = WORK / "break-other.diff"
    path.write_text(text, encoding="utf-8")
    return path


def patch_edit_tests() -> Path:
    """场景 B 的补丁：直接改测试文件（黑名单该在第 ④ 关拦住）。"""
    text = (
        "diff --git a/tests/test_money.py b/tests/test_money.py\n"
        "--- a/tests/test_money.py\n"
        "+++ b/tests/test_money.py\n"
        "@@ -1,1 +1,1 @@\n"
        "-old\n"
        "+new\n"
    )
    path = WORK / "edit-tests.diff"
    path.write_text(text, encoding="utf-8")
    return path


def patch_harmless() -> Path:
    """场景 C 的补丁：只加一行注释，什么都不破坏（门禁必须放行）。"""
    original = (FIXTURE / "ledger/report.py").read_text(encoding="utf-8")
    updated = "# 这行注释是门禁的放行用例加的，不影响任何行为\n" + original
    text = make_patch([("ledger/report.py", original, updated)])
    path = WORK / "harmless.diff"
    path.write_text(text, encoding="utf-8")
    return path


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

    prepare()
    # 基线必须**实测**，不能拍脑袋写 0：这个 fixture 仓库自己就有若干 ruff 告警，
    # 假基线会把"无害补丁"判成"新增告警"（第一次跑就是这么红的）。
    baseline = capture_baseline(FIXTURE, full_cmd=FULL_CMD, lint_cmd=LINT_CMD)
    print(f"基线：全量失败 {len(baseline['failures'])} 条，lint {baseline['lint_count']} 条")
    results: list[dict] = []

    def record(name: str, expectation: str, verdict) -> dict:
        ok = {
            "expect_blocked_regression": (not verdict.passed and bool(verdict.new_failures)),
            "expect_blocked_blacklist": (
                not verdict.passed and verdict.gates.get("blacklist", {}).get("ok") is False
            ),
            "expect_passed": verdict.passed,
        }[expectation]
        print(f"=== {name} ===")
        print(f"  结论：{'符合预期' if ok else '**不符合预期**'}　（{verdict.summary()}）")
        for gate, detail in verdict.gates.items():
            print(f"  · {gate}: {detail}")
        row = {"name": name, "expectation": expectation, "as_expected": ok, "verdict": verdict.as_dict()}
        results.append(row)
        return row

    record(
        "A 过目标测试但破坏另一测试",
        "expect_blocked_regression",
        run_gate(
            FIXTURE,
            patch_break_other_module(),
            target_cmd=TARGET_CMD,
            full_cmd=FULL_CMD,
            lint_cmd=LINT_CMD,
            baseline=baseline,
        ),
    )
    record(
        "B 改测试文件",
        "expect_blocked_blacklist",
        run_gate(
            FIXTURE,
            patch_edit_tests(),
            target_cmd=TARGET_CMD,
            full_cmd=FULL_CMD,
            lint_cmd=LINT_CMD,
            baseline=baseline,
        ),
    )
    record(
        "C 无害补丁必须放行",
        "expect_passed",
        run_gate(
            FIXTURE,
            patch_harmless(),
            target_cmd=TARGET_CMD,
            full_cmd=FULL_CMD,
            lint_cmd=LINT_CMD,
            baseline=baseline,
        ),
    )

    passed = all(row["as_expected"] for row in results)
    print(f"\n三个场景 {sum(1 for r in results if r['as_expected'])}/{len(results)} 符合预期")
    print(f"结论：{'达标' if passed else '**未达标**'}")

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    report = {
        "date": stamp,
        "scenarios": len(results),
        "as_expected": sum(1 for row in results if row["as_expected"]),
        "commands": {"target": TARGET_CMD, "full": FULL_CMD, "lint": LINT_CMD},
        "rows": results,
        "passed": passed,
    }
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    target = REPORT_DIR / f"gate-eval-{stamp}.json"
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"报告：{target}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
