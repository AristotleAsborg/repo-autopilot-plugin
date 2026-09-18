"""4.2 验收：在**注入已知缺陷**的仓库上量修复成功率。

    python tools/eval_repair.py                 # 真 flash + 真沙箱
    python tools/eval_repair.py --limit 2       # 先跑两条看看链路通不通

## 为什么不用 SWE-smith

路线 4.2 的验收写的是"SWE-smith 造 20 个含已知 bug 的样本仓库"。本机**没有网络安装
SWE-smith 的环境**（无 Docker、pip 装包要一次性提权），所以这里用一个**等价的本地替身**：
把 `tests/fixtures/sandbox-repos/` 里的真实仓库复制一份、**注入一个已知缺陷**
（通常是改一个运算符或一个常量），确认测试真的变红，然后把 issue 描述交给修复循环。

替身比 SWE-smith 弱的地方要说清楚：样本只有个位数、缺陷类型偏单一（都是"单点小改"）、
issue 描述是**我写的**（比真 issue 干净得多）。所以这里的成功率是**上界**，
不是"系统在真实仓库上的水平"。等有 Docker/网络条件时，把 `SAMPLES` 换成 SWE-smith 生成的
20 个样本，验收线（≥40%）不变 —— 这也是为什么本脚本把样本表单独放在最上面。

## 判定与分母

- **前置条件**：注入后测试必须真的失败，而且失败得"像是跑起来了"（日志里没有
  `No module named pytest` 这类环境问题）。不满足 → 这条样本**作废**，不进分母 ——
  否则"环境坏了"会被记成"修不好"。
- 成功率 = 修复后测试转绿 / 有效样本。路线 4.2 v1 的线是 **≥40%**。
- 轮数上限取 8（循环本身支持 15）：一次验收 5 条样本，15 轮会把 flash 调用量翻好几倍；
  这里先把**低配下的下界**量出来，生产跑的时候仍用 15。
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.repair import RepairLoop
from src.sandbox import run as sandbox_run

FIXTURES = ROOT / "tests" / "fixtures" / "sandbox-repos"
EVAL_ROOT = ROOT / "state" / "eval-repair"
REPORT_DIR = ROOT / "state" / "reports"

SUCCESS_FLOOR = 0.40
EVAL_MAX_ROUNDS = 8


@dataclass(frozen=True)
class Breakage:
    """一条样本：把 `old` 换成 `broken`，测试就该红；issue 描述只讲现象。"""

    name: str
    repo: str
    path: str
    old: str
    broken: str
    issue: str


SAMPLES: tuple[Breakage, ...] = (
    Breakage(
        name="money-thousands",
        repo="sandbox-clean",
        path="ledger/money.py",
        old='cleaned = text.strip().replace(",", "")',
        broken="cleaned = text.strip()",
        issue="金额写成 1,234.56 的时候解析直接报 MoneyError，去掉千分位逗号就能过。",
    ),
    Breakage(
        name="report-cents",
        repo="sandbox-clean",
        path="ledger/report.py",
        old="return {name: amount.cents for name, amount in ledger.balances().items()}",
        broken="return {name: amount.cents // 100 for name, amount in ledger.balances().items()}",
        issue="summary() 返回的余额比实际金额小了一百倍，客户端拿到的数字不对。",
    ),
    Breakage(
        name="report-biggest",
        repo="sandbox-clean",
        path="ledger/report.py",
        old="name = max(balances, key=lambda key: abs(balances[key]))",
        broken="name = max(balances, key=lambda key: balances[key])",
        issue="biggest_account 挑出来的不是金额最大的那个账户，负数余额的账户被排到了最后。",
    ),
    Breakage(
        name="cli-exit-code",
        repo="sandbox-clean",
        path="ledger/cli.py",
        # 注意要带上 `except MoneyError` 那一行：cli.py 里有**两个** `return 2`
        # （加载失败、金额解析失败），只写 `print(...)+return 2` 会命中前面那个，
        # 注入完测试还是绿的 —— 前置条件会直接把这条样本判作废（第一次就踩了）。
        old='        except MoneyError as exc:\n            print(f"error: {exc}", file=sys.stderr)\n            return 2',
        broken='        except MoneyError as exc:\n            print(f"error: {exc}", file=sys.stderr)\n            return 1',
        issue="金额格式错误时命令行的退出码是 1，文档和测试都要求返回 2。",
    ),
    Breakage(
        name="legacy-has-key",
        repo="sandbox-messy",
        path="legacy_py2_notes.py",
        old="return key in mapping",
        broken="return mapping.get(key)",
        issue="legacy_has_key 在 key 不存在时返回 None，调用方按 False 判断会漏掉，应该返回布尔值。",
    ),
)


def test_command() -> list[str]:
    """
    沙箱里的 `python` 指向 uv 托管解释器（没有 pytest），必须用绝对路径；
    而且**传列表**：沙箱把命令拼成 `cmd /c <命令>` 时会把字符串里的引号转义坏掉（实测踩过）。
    `-p no:cacheprovider` 关掉缓存：沙箱 ACL 下它只会刷一屏告警，把失败断言挤出反馈窗口。
    """
    return [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"]


IGNORE = shutil.ignore_patterns(".git", "__pycache__", ".pytest_cache", "pytest-cache-files-*")


def prepare(sample: Breakage) -> Path:
    work = EVAL_ROOT / sample.name
    if work.exists():
        shutil.rmtree(work, ignore_errors=True)
    # 忽略缓存目录：pytest 在这些 fixture 里留下过 ACL 锁死的临时目录，
    # 直接 copytree 会以 WinError 5 失败（实测踩过），而它们本来也不该被复制
    shutil.copytree(FIXTURES / sample.repo, work, ignore=IGNORE)
    target = work / sample.path
    text = target.read_text(encoding="utf-8")
    if sample.old not in text:
        raise RuntimeError(f"{sample.name}: 注入点找不到，fixture 可能改过了")
    target.write_text(text.replace(sample.old, sample.broken, 1), encoding="utf-8")
    return work


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

    parser = argparse.ArgumentParser(description="4.2 修复成功率")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--rounds", type=int, default=EVAL_MAX_ROUNDS)
    parser.add_argument(
        "--only",
        action="append",
        default=[],
        help="只跑指定样本名（可重复）。一次调用有 10 分钟上限，样本多时分开跑，结果并入当天报告",
    )
    args = parser.parse_args()

    if not FIXTURES.is_dir():
        # 陪练仓库被 .gitignore 排除（由 tools/sandbox_repos.py 生成）。
        # 不报这一句的话，新克隆上会表现成"样本全部作废"，看起来像修复循环坏了。
        print(f"缺少陪练仓库：{FIXTURES}")
        print("先跑：python tools/sandbox_repos.py build")
        return 1

    samples = [sample for sample in SAMPLES if not args.only or sample.name in args.only]
    if args.limit:
        samples = samples[: args.limit]
    if not samples:
        print(f"--only 没匹配到样本：{args.only}")
        return 2
    command = test_command()
    print(f"样本 {len(samples)} 条；轮数上限 {args.rounds}；测试命令 {command}\n")

    rows: list[dict] = []
    valid = fixed = 0
    for sample in samples:
        started = time.perf_counter()
        print(f"=== {sample.name} ===")
        work = prepare(sample)
        baseline = sandbox_run(work, test_cmd=command, task_id=f"eval-{sample.name}-base")
        baseline_log = str(getattr(baseline, "log", ""))
        if baseline.passed:
            print("  前置条件不成立：注入后测试仍然是绿的（样本作废）")
            rows.append({"name": sample.name, "valid": False, "reason": "注入后测试仍绿"})
            continue
        if "No module named" in baseline_log or "is not recognized" in baseline_log:
            print("  前置条件不成立：沙箱里根本跑不起 pytest（样本作废）")
            rows.append({"name": sample.name, "valid": False, "reason": "沙箱环境跑不起测试"})
            continue
        valid += 1

        loop = RepairLoop(max_rounds=args.rounds)
        result = loop.run(
            sample.issue, work, [sample.path], issue_id=f"eval-{sample.name}", test_cmd=command
        )
        fixed += int(result.ok)
        elapsed = time.perf_counter() - started
        print(f"  {'修复成功' if result.ok else '未修复'}：{result.reason}（{result.rounds} 轮，{elapsed:.0f}s）")
        rows.append(
            {
                "name": sample.name,
                "valid": True,
                "ok": result.ok,
                "rounds": result.rounds,
                "reason": result.reason,
                "seconds": round(elapsed, 1),
                "patch": result.patch_path,
                "report": result.report_path,
            }
        )

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    target = REPORT_DIR / f"repair-eval-{stamp}.json"

    # 一次调用有 10 分钟上限，样本多了只能分批跑 —— 所以结果是**并入**当天报告的，
    # 而不是覆盖：分母必须是"今天跑过的全部有效样本"，不是"这一次跑的那两条"。
    merged: dict[str, dict] = {}
    if target.exists():
        try:
            for row in json.loads(target.read_text(encoding="utf-8")).get("rows", []):
                if row.get("name"):
                    merged[str(row["name"])] = row
        except (OSError, json.JSONDecodeError):
            pass
    for row in rows:
        merged[str(row["name"])] = row

    all_rows = [merged[sample.name] for sample in SAMPLES if sample.name in merged]
    total_valid = sum(1 for row in all_rows if row.get("valid"))
    total_fixed = sum(1 for row in all_rows if row.get("ok"))
    total_rate = total_fixed / total_valid if total_valid else 0.0
    # 当天报告只有在**样本表跑全**时才敢下结论；没跑全就标 pending，不拿部分样本当结论
    complete = total_valid + sum(1 for row in all_rows if not row.get("valid")) >= len(SAMPLES)
    total_passed = complete and total_valid > 0 and total_rate >= SUCCESS_FLOOR

    print()
    print(
        f"当天累计：有效样本 {total_valid}/{len(SAMPLES)} 条，修复成功 {total_fixed} 条 → "
        f"成功率 {total_rate:.0%}（要求 ≥{SUCCESS_FLOOR:.0%}）"
    )
    if not complete:
        print("（样本表还没跑全 → 报告标 pending，不作数）")
    print(f"结论：{'达标' if total_passed else '**未达标/待续**'}")
    print(f"逐条修复日志：{REPORT_DIR}/repair_eval-*.md")

    report = {
        "date": stamp,
        "samples": len(samples),
        "samples_in_table": len(SAMPLES),
        "complete": complete,
        "valid": total_valid,
        "fixed": total_fixed,
        "success_rate": round(total_rate, 4),
        "floor": SUCCESS_FLOOR,
        "max_rounds": args.rounds,
        "test_command": command,
        "note": (
            "本地替身样本（注入单点缺陷），非 SWE-smith；issue 描述由本系统编写，"
            "所以成功率是上界。前置条件：注入后测试必须真的失败且沙箱能跑 pytest。"
            "本文件是**当天累计**：分批跑的结果会并入同一个文件。"
        ),
        "rows": all_rows,
        "passed": total_passed,
    }
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"报告：{target}")
    return 0 if total_passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
