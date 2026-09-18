"""`/体检`（路线 8.3）：跑一遍**固定样本集**的离线评测，并与上一轮对比。

    python tools/checkup.py                 # 快速档：全量测试 + 查重/停止判断评测 + 一致性 + 自检
    python tools/checkup.py --full          # 加上分类 holdout 回放（约 7 分钟）与修复成功率
    python tools/checkup.py --report        # 只看最近一次体检报告

## 为什么单独做一个体检脚本

8.3 的频率表要求"离线评测：每周，固定样本集；**连续 2 周下降 → 回滚模型或 prompt**"。
那条红线的关键在"**连续**"——单次波动不算数。所以体检要把每轮的指标**落盘成时间序列**，
再把本轮与历史比：只有连续两轮下降才报警并要求回滚（否则我们会对着噪声来回横跳）。

样本集必须是**冻结**的（3.1 的 holdout / 3.2 的标定集 / 2.2 的停止判断样本）：
现抓的样本每天都在变，指标就不再可比，也防不住"背题涨分"。
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

REPORT_DIR = ROOT / "state" / "reports"
HISTORY = ROOT / "state" / "checkup-history.jsonl"

#: 指标下跌超过这个幅度才算"下降"（避免对着噪声来回横跳）
REGRESSION_EPSILON = 0.01


def run(command: list[str], *, timeout: int = 1800) -> tuple[int, str]:
    outcome = subprocess.run(
        command,
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )
    return outcome.returncode, (outcome.stdout or "") + (outcome.stderr or "")


def latest(pattern: str) -> dict | None:
    candidates = sorted(REPORT_DIR.glob(pattern))
    if not candidates:
        return None
    try:
        return json.loads(candidates[-1].read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def collect(full: bool) -> dict:
    """跑一遍检查，收集**指标**（不只是"过没过"）。"""
    metrics: dict = {}
    details: list[str] = []

    code, output = run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--ignore=tests/fixtures"])
    metrics["unit_tests_ok"] = code == 0
    details.append(f"全量测试：exit={code}　{output.strip().splitlines()[-1] if output.strip() else ''}")

    # 一致性（路线 7.4）：指令清单 ↔ AGENTS.md ↔ skills/
    from src.skills import check_consistency

    consistency = check_consistency()
    metrics["skills_consistent"] = consistency["ok"]
    details.append(f"指令一致性：{'通过' if consistency['ok'] else '**不通过**'}（{len(consistency['problems'])} 个问题）")

    # 自检七项（/应急 的清单，体检顺手跑一遍）
    from src.skills import overall, run_checks

    checks = run_checks(client=None, local_probe=lambda: (True, "体检跳过探活"))
    metrics["doctor_ok"] = overall(checks)
    details.append(f"自检七项：{'全过' if metrics['doctor_ok'] else '有异常'}")

    # 查重（3.2）：准确率相关的是召回/误报数
    code, output = run([sys.executable, "tools/eval_dedupe.py"], timeout=900)
    dedupe = latest("dedupe-eval-*-local_embed.json")
    metrics["dedupe_ok"] = code == 0
    if dedupe:
        metrics["dedupe_recall"] = dedupe.get("at_chosen_flag", {}).get("recall")
        metrics["dedupe_false_positives"] = dedupe.get("at_chosen_flag", {}).get("false_positives")
    details.append(f"查重评测：exit={code}　{metrics.get('dedupe_recall')}/10 召回、{metrics.get('dedupe_false_positives')}/10 误报")

    # 停止判断（2.2）：那条线是"准确率 ≥80%"
    code, output = run([sys.executable, "tools/eval_stopper.py"], timeout=900)
    stopper = latest("stopper-eval-*.json")
    metrics["stopper_ok"] = code == 0
    if stopper:
        metrics["stopper_accuracy"] = stopper.get("accuracy")
    details.append(f"停止判断评测：exit={code}　准确率 {metrics.get('stopper_accuracy')}")

    if full:
        # 分类 holdout（3.1）：固定样本集，绝不能现抓
        code, output = run([sys.executable, "tools/eval_triage.py", "--from-replay", "--split", "holdout", "--tag", "checkup"], timeout=1800)
        triage = latest("triage-eval-*-local_small-holdout1.json") or latest("triage-eval-*-checkup.json")
        metrics["triage_ok"] = code == 0
        if triage:
            metrics["triage_accuracy"] = triage.get("accuracy")
            metrics["triage_false_kill"] = triage.get("false_kill_rate")
        details.append(f"分类 holdout：exit={code}　准确率 {metrics.get('triage_accuracy')}")

        code, output = run([sys.executable, "tools/eval_repair.py"], timeout=1800)
        metrics["repair_ok"] = code == 0
        details.append(f"修复成功率：exit={code}")

    # **副本一致性**（2026-09-17 人类要求写进常规检查）：逐文件哈希比对源仓库与各份副本。
    # 动机是一次真实的翻车：报告说"源仓库改了 src/scaffold/core.py"，实际只改在副本里 ——
    # 只看叙述就往下走，那次修复会随下一次覆盖消失。所以规矩前置到体检里：
    # **合并/覆盖任何副本之前，先看这份报告里列出的差异清单。**
    #
    # 指标名必须以 `_ok` 结尾：`main()` 的判定是 `all(v for k, v in metrics.items() if k.endswith("_ok"))`。
    # 第一版叫 `copies_consistent`，于是**它 False 的时候体检照样报"通过"** ——
    # 一个能和自己打架的指标，比没有这个指标更糟（这条是跑完一次体检才看出来的）。
    copies_ok, copy_details = copy_consistency()
    metrics["copies_ok"] = copies_ok
    details.extend(copy_details)

    return {"metrics": metrics, "details": details}


def copy_consistency() -> tuple[bool, list[str]]:
    """
    副本一致性检查：**合并/覆盖任何副本之前先跑它**（2026-09-17 人类要求写进常规检查）。

    返回 `(是否没有合并风险, 明细行)`。没有可比的副本时返回 `(True, ["没发现可比的副本…"])`
    —— "没有副本"不是缺陷（干净 checkout、或别人的机器上都会这样）。
    """
    copies = known_copies()
    if not copies:
        return True, ["副本一致性：没发现可比的副本（工作区内 installed/、state/copies.json 里登记的，都算）"]
    from tools.package import compare as compare_copies

    ok = True
    details: list[str] = []
    for path in copies:
        code = compare_copies(path)
        ok = ok and code == 0
        details.append(
            f"副本一致性 {path}：exit={code}（{'一致' if code == 0 else '**有差异，合并前先问清是谁改的**'}）"
        )
    return ok, details


def known_copies() -> list[Path]:
    """
    找出"值得比对"的副本目录。

    两个来源，**都不写死机器路径**（这是给别人的机器也能跑的检查）：
      1. 工作区里的标准安装位置：`<仓库上一级>/installed/repo-autopilot`；
      2. `state/copies.json` 里登记的路径 —— 机器特有的副本（例如工作区之外那份）
         由人类/代理登记进去，检查本身保持可移植。
    目录不存在就跳过（不报失败）：没有副本不是缺陷。
    """
    candidates: list[Path] = [ROOT.parent / "installed" / "repo-autopilot"]
    registry = ROOT / "state" / "copies.json"
    if registry.is_file():
        try:
            payload = json.loads(registry.read_text(encoding="utf-8"))
            entries = payload.get("copies") if isinstance(payload, dict) else payload
            for item in entries or []:
                if isinstance(item, str) and item.strip():
                    candidates.append(Path(item.strip()))
        except (OSError, json.JSONDecodeError):
            pass
    seen: list[Path] = []
    for path in candidates:
        resolved = path if path.is_absolute() else (ROOT.parent / path)
        if resolved.is_dir() and resolved.resolve() != ROOT.resolve() and resolved not in seen:
            seen.append(resolved)
    return seen


def history() -> list[dict]:
    if not HISTORY.is_file():
        return []
    rows = []
    for line in HISTORY.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def compare(metrics: dict, past: list[dict]) -> list[str]:
    """
    与历史比：**连续两轮下降**才算退化（8.3 的红线）。

    单轮波动不报警 —— 否则每周都会有人对着噪声调参，越调越差。
    """
    alarms: list[str] = []
    for key, value in metrics.items():
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            continue
        series = [row.get("metrics", {}).get(key) for row in past[-2:]]
        series = [item for item in series if isinstance(item, (int, float)) and not isinstance(item, bool)]
        if len(series) < 2:
            continue
        # **连续两轮下降**：上一轮比上上轮低、本轮又比上一轮低，两段跌幅都超过噪声阈值。
        # 只跌一轮（比如上轮持平、本轮下跌）不报警 —— 那更像波动。
        if series[0] - series[1] > REGRESSION_EPSILON and series[1] - value > REGRESSION_EPSILON:
            alarms.append(
                f"{key} 连续两轮下降：{series[0]} → {series[1]} → {value}　（按 8.3 应回滚模型或 prompt）"
            )
    return alarms


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

    parser = argparse.ArgumentParser(description="/体检：离线评测 + 退化检测")
    parser.add_argument("--full", action="store_true", help="加上分类 holdout 与修复成功率（慢）")
    parser.add_argument("--report", action="store_true", help="只看最近一次报告")
    args = parser.parse_args()

    if args.report:
        target = REPORT_DIR / "checkup-latest.md"
        if not target.is_file():
            print("还没有体检报告")
            return 1
        print(target.read_text(encoding="utf-8"))
        return 0

    past = history()
    result = collect(args.full)
    metrics = result["metrics"]
    alarms = compare(metrics, past)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    passed = all(value for key, value in metrics.items() if key.endswith("_ok")) and not alarms

    lines = [
        f"# 体检报告 {stamp}",
        "",
        f"- 结论：**{'通过' if passed else '不通过'}**" + ("（有退化告警）" if alarms else ""),
        f"- 历史轮数：{len(past)}",
        "",
        "## 指标",
        "",
    ]
    for key, value in sorted(metrics.items()):
        lines.append(f"- `{key}`：{value}")
    lines += ["", "## 明细", ""]
    lines += [f"- {item}" for item in result["details"]]
    if alarms:
        lines += ["", "## 退化告警（连续两轮下降）", ""] + [f"- {item}" for item in alarms]
    lines += [
        "",
        "## 说明",
        "",
        "- 样本集是**冻结**的（holdout / 标定集），现抓样本会让指标不可比，也防不住背题涨分。",
        "- 单轮波动不报警：8.3 的红线是**连续两周下降 → 回滚**，否则我们会对着噪声来回横跳。",
        "- 这一轮的报告与指标都落盘（`state/checkup-history.jsonl`），下一轮比的就是它们。",
        "",
    ]
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    (REPORT_DIR / f"checkup-{stamp}.md").write_text("\n".join(lines), encoding="utf-8")
    (REPORT_DIR / "checkup-latest.md").write_text("\n".join(lines), encoding="utf-8")
    with open(HISTORY, "a", encoding="utf-8") as handle:
        handle.write(json.dumps({"date": stamp, "metrics": metrics, "passed": passed}, ensure_ascii=False) + "\n")

    print("\n".join(lines[:14]))
    if alarms:
        print("\n退化告警：")
        for item in alarms:
            print(f"  - {item}")
    print(f"\n报告：state/reports/checkup-{stamp}.md")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
