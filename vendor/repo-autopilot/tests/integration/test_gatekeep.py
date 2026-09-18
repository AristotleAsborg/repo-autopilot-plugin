"""4.3 门禁的确定性测试：四道关各自能拦、以及"不该拦的不拦"。

路线 4.3 的验收就是两个场景，这里把它们钉成可重复的测试：

- **场景 A**：补丁让目标测试全绿，却**打坏了另一个测试** → 必须被第 ② 关拦下；
- **场景 B**：补丁试图**修改测试文件** → 必须被第 ④ 关（黑名单）拦下。

另外三条是防止门禁"误伤"：基线本来就红的用例不算新增；lint 条数没涨就放过；
目标测试就没过时，不该再去跑全量（省一次两分钟）。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from src.gatekeep import (
    MAX_GATE_ATTEMPTS,
    GateVerdict,
    blacklist_hits,
    failure_report,
    needs_human,
    parse_lint_count,
    parse_pytest_failures,
    patch_paths,
    run_gate,
)
from src.repair import make_patch  # 补丁统一用真实文件内容生成，不手写 hunk

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "sandbox-repos" / "sandbox-clean"

# 陪练仓库是**生成物**（`tools/sandbox_repos.py build`，需要写 token + 网络），不进版本库；
# 干净的 checkout（CI、刚 clone 下来的机器）里没有它。这时整份文件**跳过并说明原因**，
# 而不是把"缺件"表现成"门禁坏了" —— 后者会让人去改门禁（实测踩过同类坑）。
# 本机跑验收时 fixture 一定在（`state/run-acceptance.cmd` 的环境里已 build）。
pytestmark = pytest.mark.skipif(
    not FIXTURE.is_dir(),
    reason="需要陪练仓库 tests/fixtures/sandbox-repos/sandbox-clean（生成物）："
    "先跑 python tools/sandbox_repos.py build",
)


def fake_runner(plan: dict[str, tuple[int, str]]):
    """按命令的第一个参数分流，返回预置的 (exit, output)，并记录调用顺序。"""
    calls: list[list[str]] = []

    def runner(command, cwd):
        calls.append(list(command))
        for key, result in plan.items():
            if key in " ".join(command):
                return result
        return 0, ""

    runner.calls = calls  # type: ignore[attr-defined]
    return runner


def build_patch(scratch: Path, *, touch: str = "ledger/report.py") -> Path:
    """
    用 `make_patch` 从**真实文件内容**生成补丁，而不是手写 hunk。

    手写 hunk 的行号/上下文对不上时 `git apply` 会拒收（实测踩过：整批测试
    因为补丁打不上而报"目标测试没全绿"，看日志才发现是补丁本身不合法）。
    用真实内容生成，测的就只是门禁，不是我的行号算术。
    """
    relative = touch if touch in ("ledger/report.py",) else "ledger/report.py"
    original = (FIXTURE / relative).read_text(encoding="utf-8")
    updated = original.replace("amount.cents for name", "amount.cents + 1 for name", 1)
    text = make_patch([(touch, original, updated)])
    path = scratch / "change.diff"
    path.write_text(text, encoding="utf-8")
    return path


def build_blacklist_patch(scratch: Path, path: str) -> Path:
    """黑名单在第 ④ 关就返回，补丁内容不需要真能打上 —— 只要路径是敏感路径。"""
    text = (
        f"diff --git a/{path} b/{path}\n"
        f"--- a/{path}\n"
        f"+++ b/{path}\n"
        "@@ -1,1 +1,1 @@\n"
        "-old\n"
        "+new\n"
    )
    target = scratch / "danger.diff"
    target.write_text(text, encoding="utf-8")
    return target


# ------------------------------------------------------------------ 补丁解析

def test_patch_paths_finds_all_three_headers() -> None:
    text = (
        "diff --git a/pkg/a.py b/pkg/a.py\n--- a/pkg/a.py\n+++ b/pkg/a.py\n"
        "diff --git a/gone.py b/gone.py\n--- a/gone.py\n+++ /dev/null\n"
    )
    assert patch_paths(text) == ["pkg/a.py", "gone.py"]


def test_patch_paths_sees_deleted_files() -> None:
    """只删不增的补丁只有 `--- a/x`：漏掉它就等于放过"删掉测试文件"这种操作。"""
    assert patch_paths("--- a/tests/test_money.py\n+++ /dev/null\n") == ["tests/test_money.py"]


# ------------------------------------------------------------------ 第 ④ 关

@pytest.mark.parametrize(
    "path",
    [
        "tests/test_money.py",
        "tests/integration/test_gatekeep.py",
        "state/approvals/20260912-approve.md",
        "state/progress.md",
        ".github/workflows/autopilot-gate.yml",
        "LICENSE",
        "src/gatekeep/core.py",
        "tools/acceptance.py",
        "scripts/check_blacklist.py",
        "conftest.py",
        "pkg/conftest.py",
    ],
)
def test_blacklist_blocks_dangerous_paths(path: str) -> None:
    assert blacklist_hits([path]), f"{path} 应该被黑名单拦下"


@pytest.mark.parametrize("path", ["ledger/report.py", "src/repair/core.py", "docs/readme.md"])
def test_blacklist_allows_normal_source_paths(path: str) -> None:
    assert blacklist_hits([path]) == [], f"{path} 不该被拦"


def test_gate_blocks_a_patch_that_edits_tests(scratch: Path) -> None:
    """**场景 B**：改测试文件 → 黑名单拦下，而且不该再去跑任何测试。"""
    patch = build_blacklist_patch(scratch, "tests/test_money.py")
    runner = fake_runner({})
    verdict = run_gate(FIXTURE, patch, runner=runner)

    assert verdict.passed is False
    assert verdict.gates["blacklist"]["ok"] is False
    assert "测试代码" in verdict.summary()
    assert runner.calls == [], "黑名单命中就该立刻返回，不该浪费一次全量回归"


# ------------------------------------------------------------------ 第 ①②③ 关

def test_gate_passes_when_everything_is_green(scratch: Path) -> None:
    patch = build_patch(scratch)
    runner = fake_runner({"pytest": (0, "5 passed"), "ruff": (0, "All checks passed!")})
    verdict = run_gate(FIXTURE, patch, runner=runner, baseline={"failures": [], "lint_count": 0})

    assert verdict.passed is True, verdict.summary()
    assert verdict.gates["target"]["ok"] and verdict.gates["lint"]["ok"]


def test_gate_blocks_a_patch_that_breaks_another_test(scratch: Path) -> None:
    """**场景 A**：目标测试全绿，但全量回归多了一个失败 → 第 ② 关拦下。"""
    patch = build_patch(scratch)
    runner = fake_runner(
        {
            "tests/test_money.py": (0, "10 passed"),          # 目标测试：绿
            "pytest": (1, "FAILED tests/test_report.py::test_summary_is_in_cents"),
            "ruff": (0, "All checks passed!"),
        }
    )
    verdict = run_gate(
        FIXTURE,
        patch,
        target_cmd=[sys.executable, "-m", "pytest", "tests/test_money.py"],
        runner=runner,
        baseline={"failures": [], "lint_count": 0},
    )

    assert verdict.passed is False
    assert verdict.new_failures == ["tests/test_report.py::test_summary_is_in_cents"]
    assert "新增失败" in verdict.summary()


def test_baseline_failures_are_not_counted_as_new(scratch: Path) -> None:
    """仓库本来就是红的：只要不是这次弄坏的，就不该拦。"""
    patch = build_patch(scratch)
    runner = fake_runner(
        {
            "tests/test_money.py": (0, "10 passed"),
            "pytest": (1, "FAILED tests/test_cli.py::test_summary_reads_a_file"),
            "ruff": (0, "All checks passed!"),
        }
    )
    verdict = run_gate(
        FIXTURE,
        patch,
        target_cmd=[sys.executable, "-m", "pytest", "tests/test_money.py"],
        runner=runner,
        baseline={"failures": ["tests/test_cli.py::test_summary_reads_a_file"], "lint_count": 0},
    )
    assert verdict.passed is True, verdict.summary()


def test_lint_regression_blocks(scratch: Path) -> None:
    patch = build_patch(scratch)
    runner = fake_runner(
        {
            "tests/test_money.py": (0, "10 passed"),
            "pytest": (0, "10 passed"),
            "ruff": (1, "Found 3 errors."),
        }
    )
    verdict = run_gate(
        FIXTURE,
        patch,
        target_cmd=[sys.executable, "-m", "pytest", "tests/test_money.py"],
        runner=runner,
        baseline={"failures": [], "lint_count": 1},
    )
    assert verdict.passed is False
    assert verdict.lint_delta == 2
    assert "lint 新增" in verdict.summary()


def test_target_failure_stops_before_regression(scratch: Path) -> None:
    patch = build_patch(scratch)
    runner = fake_runner({"tests/test_money.py": (1, "FAILED tests/test_money.py::test_parse_whole_number")})
    verdict = run_gate(
        FIXTURE,
        patch,
        target_cmd=[sys.executable, "-m", "pytest", "tests/test_money.py"],
        runner=runner,
        baseline={"failures": [], "lint_count": 0},
    )
    assert verdict.passed is False
    assert "目标测试没全绿" in verdict.summary()
    assert not any("ruff" in " ".join(call) for call in runner.calls), "目标没过就别再跑 lint"


# ------------------------------------------------------------------ 输出解析与上限

def test_parse_pytest_failures_reads_failed_and_error_lines() -> None:
    output = "FAILED tests/a.py::test_x - assert 1 == 2\nERROR tests/b.py::test_y\n1 failed"
    assert parse_pytest_failures(output) == {"tests/a.py::test_x", "tests/b.py::test_y"}


def test_parse_lint_count_defaults_to_zero() -> None:
    assert parse_lint_count("Found 7 errors.") == 7
    assert parse_lint_count("看不懂的输出") == 0


def test_three_attempts_means_needs_human() -> None:
    assert needs_human(MAX_GATE_ATTEMPTS - 1) is False
    assert needs_human(MAX_GATE_ATTEMPTS) is True
    report = failure_report("issue-42", [GateVerdict(False, {}, ["目标测试没全绿"])] * MAX_GATE_ATTEMPTS)
    assert "needs_human" in report
    assert "第 3 轮" in report


# ------------------------------------------------------------------ 真跑一遍（本地 pytest，不联网）

def test_real_patch_and_real_pytest_blocks_a_breaking_change(scratch: Path) -> None:
    """
    用**真的** pytest 跑一遍场景 A：补丁让 `summary()` 多算一分钱 ——
    `tests/test_report.py` 会红，门禁必须拦住。这一步不调模型、不联网。
    """
    patch = build_patch(scratch)
    verdict = run_gate(
        FIXTURE,
        patch,
        target_cmd=[sys.executable, "-m", "pytest", "tests/test_money.py", "-q", "-p", "no:cacheprovider"],
        full_cmd=[sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"],
        lint_cmd=[sys.executable, "-m", "ruff", "check", "--select", "F401", "."],
        baseline={"failures": [], "lint_count": 0},
    )
    assert verdict.gates["apply"]["ok"] is True
    assert verdict.gates["target"]["ok"] is True, "目标测试（money）应该还是绿的"
    assert verdict.passed is False, "改动破坏了 report 的测试，必须被拦"
    assert any("test_report" in name for name in verdict.new_failures)


def test_real_blacklist_scenario_does_not_execute_anything(scratch: Path) -> None:
    patch = build_blacklist_patch(scratch, "tests/test_money.py")
    verdict = run_gate(FIXTURE, patch)
    assert verdict.passed is False
    assert "blacklist" in verdict.gates
    assert "target" not in verdict.gates, "黑名单命中时不该执行仓库里的任何命令"


def test_ruff_is_available_in_this_environment() -> None:
    """门禁第 ③ 关依赖 ruff；环境里没有它就等于这道关永远是绿的（假绿）。"""
    outcome = subprocess.run(
        [sys.executable, "-m", "ruff", "--version"], capture_output=True, text=True, check=False
    )
    assert outcome.returncode == 0, "ruff 不可用：第 ③ 关会变成假绿"
