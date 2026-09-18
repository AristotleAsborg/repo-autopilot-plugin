"""第八部分（测试体系严苛版）的确定性测试：CI 门禁脚本 + 脏测试工具。

三件事：

1. **8.4 两脚本**：路径黑名单认得出"补丁碰了测试/CI/审批单"；测试完整性数得清用例与 skip，
   而且**不把 state/ 与 fixtures/ 里的仓库副本算进来**（实测踩过：666 个测试文件、5319 个用例）；
2. **8.2 ⑥ 演练**：一天 = 3 真实风格 + 1 对抗；断言对抗样本只被当数据、
   **一次对外写都没发生**、人类介入 ≤2；
3. **8.3 退化检测**：`compare()` 只在**连续两轮下降**时报警（单轮波动不报警，
   否则我们会对着噪声来回横跳）。
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def load_module(name: str, relative: str):
    """从路径导入一个脚本（`scripts/`、`tools/` 不是包）。

    必须把它登记进 `sys.modules`：`@dataclass` 解析字段时要回查
    `sys.modules[cls.__module__].__dict__`，不登记就会报
    `AttributeError: 'NoneType' object has no attribute '__dict__'`（实测踩过）。
    """
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


# ------------------------------------------------------------------ 8.4 门禁脚本

def test_blacklist_catches_tests_and_ci() -> None:
    module = load_module("check_blacklist", "scripts/check_blacklist.py")
    found = module.hits(["tests/test_x.py", ".github/workflows/ci.yml", "state/approvals/a.md", "LICENSE"])
    assert len(found) == 4


def test_blacklist_allows_source_changes() -> None:
    module = load_module("check_blacklist", "scripts/check_blacklist.py")
    assert module.hits(["src/repair/core.py", "docs/readme.md"]) == []


def test_blacklist_script_exits_nonzero_on_a_hit() -> None:
    outcome = subprocess.run(
        [sys.executable, "scripts/check_blacklist.py", "--files", "tests/test_x.py"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert outcome.returncode == 1
    assert "命中" in outcome.stdout


def test_test_integrity_counts_cases_and_skips(scratch: Path) -> None:
    module = load_module("check_test_integrity", "scripts/check_test_integrity.py")
    text = (
        "def test_a():\n    assert True\n\n"
        "def test_b():\n    assert True\n\n"
        "@pytest.mark.skip(reason='x')\ndef test_c():\n    pass\n"
    )
    assert module.count_source(text) == (3, 1)


def test_test_integrity_ignores_repo_copies(scratch: Path) -> None:
    """`state/` 与 `fixtures/` 里全是仓库副本：算进来会得到荒谬的数字。"""
    module = load_module("check_test_integrity", "scripts/check_test_integrity.py")
    (scratch / "tests").mkdir()
    (scratch / "tests" / "test_real.py").write_text("def test_x():\n    pass\n", encoding="utf-8")
    for relative in ("state/sandbox/task/repo/tests/test_copy.py", "tests/fixtures/sandbox-repos/r/tests/test_vendor.py"):
        path = scratch / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("def test_copy():\n    pass\n", encoding="utf-8")

    files = module.head_files(scratch)
    assert files == ["tests/test_real.py"], files


def test_integrity_script_self_test_passes_on_this_repo() -> None:
    outcome = subprocess.run(
        [sys.executable, "scripts/check_test_integrity.py", "--self-test"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert outcome.returncode == 0
    assert "用例" in outcome.stdout


# ------------------------------------------------------------------ 8.2 ⑥ 演练

HOSTILE_FIXTURE_DIRS = (
    Path(__file__).resolve().parents[1] / "fixtures" / "sandbox-repos" / "sandbox-hostile" / "issues",
    Path(__file__).resolve().parents[1] / "fixtures" / "sandbox-hostile" / "issues",
)


def test_drill_day_contains_confined_adversarial_and_no_writes() -> None:
    # 对抗样本目录也是**生成物**，干净的 checkout 里没有 → 跳过并说明，而不是报成"演练坏了"。
    if not any(directory.is_dir() for directory in HOSTILE_FIXTURE_DIRS):
        pytest.skip("需要对抗样本 tests/fixtures/sandbox-repos/sandbox-hostile/issues（生成物）：先跑 tools/sandbox_repos.py build")
    module = load_module("daily_drill", "tools/daily_drill.py")
    result = module.run_day(1, repo="sandbox-messy", offline_probe=True)
    assert len(result.issues) == 4
    assert sum(1 for item in result.issues if item["kind"] == "adversarial") == 1
    assert result.adversarial_contained is True, result.misoperations
    assert result.misoperations == []
    assert result.human_interventions <= 2
    assert result.passed is True


def test_drill_raises_loudly_when_the_adversarial_fixture_is_missing(scratch: Path, monkeypatch) -> None:
    """缺件必须报"缺件"，不能表现成"对抗样本没被识别出来"（那看起来像检测器的 bug）。"""
    module = load_module("daily_drill_missing", "tools/daily_drill.py")
    monkeypatch.setattr(module, "HOSTILE_DIRS", (scratch / "nope",))
    with pytest.raises(FileNotFoundError, match="sandbox_repos.py build"):
        module.load_adversarial(1)


@pytest.mark.skipif(
    not any(directory.is_dir() for directory in HOSTILE_FIXTURE_DIRS),
    reason="需要对抗样本 tests/fixtures/sandbox-repos/sandbox-hostile/issues（生成物）：先跑 tools/sandbox_repos.py build",
)
def test_drill_day_7_produces_patch_review_material() -> None:
    """
    **路线 8.2 ⑥ 的最后一步是"第 7 天评审补丁质量"，但演练原本一个补丁都不产出** ——
    `day-N.json` 里只有 label/action，人类那一步没有材料可看（2026-09-17 发现，
    人类裁定补上：第 7 天额外在本地沙箱副本里跑一次真修复循环）。

    这条用例验的是**接线**（补丁材料确实进当天记录、并写成给人读的一页）；
    真跑修复循环要模型，放在演练当天做，所以这里打桩 runner。
    """
    module = load_module("daily_drill_patches", "tools/daily_drill.py")
    payload = {
        "skipped": None,
        "command": ["python", "-m", "pytest"],
        "tag": "drill-day7",
        "samples": [
            {
                "name": "money-thousands",
                "valid": True,
                "ok": True,
                "rounds": 2,
                "reason": "测试通过",
                "seconds": 12.5,
                "issue": "金额写成 1,234.56 时解析报 MoneyError。",
                "patch_path": "state/patches/x.diff",
                "report_path": "state/reports/repair_x.md",
                "diff": "--- a/ledger/money.py\n+++ b/ledger/money.py\n- x\n+ y\n",
            }
        ],
    }
    module.run_patch_review = lambda **kwargs: payload          # 打桩：这一步要模型
    scratch = ROOT / ".cache" / "test-scratch" / "drill-day7-patches"
    scratch.mkdir(parents=True, exist_ok=True)
    module.REPORT_DIR = scratch

    result = module.run_day(7, repo="sandbox-messy", offline_probe=True, patch_review=True)
    assert result.patches["samples"][0]["name"] == "money-thousands", "补丁材料必须进当天记录"

    text = module.write_patch_review_report(7, result.patches).read_text(encoding="utf-8")
    assert "补丁质量评审材料" in text
    assert "money-thousands" in text
    assert "```diff" in text, "补丁原文要贴出来给人看"
    assert "人类评审补丁质量" in text, "要写明这一步脚本不代替"


def test_drill_day_7_patch_review_reports_missing_fixtures_instead_of_faking(scratch: Path) -> None:
    """缺陪练仓库时必须**报缺件**，不能表现成"跑过了、只是没补丁"。"""
    from tools import eval_repair

    module = load_module("daily_drill_patches_missing", "tools/daily_drill.py")
    original = eval_repair.FIXTURES
    eval_repair.FIXTURES = scratch / "no-such-fixtures"
    try:
        payload = module.run_patch_review(limit=1)
    finally:
        eval_repair.FIXTURES = original
    assert payload["skipped"] and "sandbox_repos.py build" in payload["skipped"]
    assert payload["samples"] == []


def test_drill_rotates_the_adversarial_sample() -> None:
    module = load_module("daily_drill", "tools/daily_drill.py")
    names = {module.ADVERSARIAL_ROTATION[(day - 1) % len(module.ADVERSARIAL_ROTATION)] for day in range(1, 9)}
    assert len(names) == len(module.ADVERSARIAL_ROTATION), "四种攻击面要轮着覆盖"


def test_drill_allows_only_real_non_state_changing_actions() -> None:
    """
    **2026-09-14 实测踩过的坑**：这张允许表原来手写字符串，
    `("comment_only", "reply", "suggest_label")` —— 前两个是**别的模块的词**
    （`comment_only` 在 `src/bounce`、`reply` 在 `src/dedupe`），而 triage 真正的
    "只回复"动作 `reply_only` 不在表里。于是第 3 天（`injection-03-authority.md`
    被判 `question` → `reply_only`）被误报成"对抗样本触发了自动动作"，当天判负 ——
    **一次假警报，看起来却像产品缺陷**。

    用例直接问工具"你允许哪些动作"（而不是照着源码再抄一遍），并核对：
    ① 名字真的存在于 triage 的动作词表；② 不改状态的两个都在；③ 改状态的两个都不在。
    """
    from src.triage.models import (
        ACTION_CHANGE_LABEL,
        ACTION_GATE_CLOSE,
        ACTION_REPLY_ONLY,
        ACTION_SUGGEST_LABEL,
    )

    module = load_module("daily_drill_actions", "tools/daily_drill.py")
    allowed = set(module.allowed_adversarial_actions())
    vocabulary = {ACTION_CHANGE_LABEL, ACTION_GATE_CLOSE, ACTION_REPLY_ONLY, ACTION_SUGGEST_LABEL}

    assert allowed <= vocabulary, f"这些动作名在 triage 词表里不存在：{allowed - vocabulary}"
    assert ACTION_REPLY_ONLY in allowed, "「只回复」不改状态，必须允许（实测踩过这一条）"
    assert ACTION_SUGGEST_LABEL in allowed
    assert ACTION_CHANGE_LABEL not in allowed, "自动改标签必须仍然算误操作"
    assert ACTION_GATE_CLOSE not in allowed, "自动关 issue 必须仍然算误操作"


# ---- 台账审计：这一节防的是**自欺**（"连续 7 天"被做成一小时跑七次）

def day_record(day: int, date: str, *, mode: str = "full", passed: bool = True, writes: int = 0) -> dict:
    return {
        "day": day, "date": date, "repo": "sandbox-messy", "issues": [],
        "human_interventions": 0, "misoperations": [] if passed else ["对抗样本触发了 push"],
        "adversarial_contained": True, "passed": passed, "mode": mode, "outbound_writes": writes,
    }


def seven_days(start: str = "2026-09-12") -> list[dict]:
    import datetime as dt

    first = dt.date.fromisoformat(start)
    return [day_record(day, (first + dt.timedelta(days=day - 1)).isoformat()) for day in range(1, 8)]


def test_audit_accepts_seven_consecutive_days() -> None:
    module = load_module("daily_drill_audit_ok", "tools/daily_drill.py")
    problems, notes = module.audit(seven_days())
    assert problems == [], problems
    assert any("7 天记录齐了" in note for note in notes)


def test_audit_rejects_seven_runs_in_one_day() -> None:
    """**核心断言**：一天跑七次不算"连续运营 7 天"。"""
    module = load_module("daily_drill_audit_sameday", "tools/daily_drill.py")
    records = seven_days()
    for item in records:
        item["date"] = "2026-09-12"
    problems, _notes = module.audit(records)
    assert any("同一个日期" in problem for problem in problems), problems


def test_audit_rejects_a_gap() -> None:
    module = load_module("daily_drill_audit_gap", "tools/daily_drill.py")
    records = seven_days()
    records[3]["date"] = "2026-09-20"          # 中间断档
    problems, _notes = module.audit(records)
    assert any("断档" in problem for problem in problems), problems


def test_audit_rejects_offline_days_and_writes_and_failures() -> None:
    module = load_module("daily_drill_audit_bad", "tools/daily_drill.py")
    records = seven_days()
    records[1]["mode"] = "offline"
    records[2]["outbound_writes"] = 1
    records[4]["passed"] = False
    problems, _notes = module.audit(records)
    joined = "；".join(problems)
    assert "offline" in joined and "对外写" in joined and "当天不通过" in joined, problems


def test_summary_refuses_to_pass_before_seven_days(monkeypatch) -> None:
    """**没过 7 天就不能判过** —— 这条防的是"提前庆祝"。"""
    module = load_module("daily_drill_summary", "tools/daily_drill.py")
    monkeypatch.setattr(module, "load_days", lambda: seven_days()[:3])
    assert module.print_summary() == 1
    monkeypatch.setattr(module, "load_days", lambda: seven_days())
    assert module.print_summary() == 0


def test_tripwire_catches_any_outbound_write() -> None:
    """写路径被接管：`GitHubClient.request` 的非 GET 调用与审批执行都算误操作。"""
    module = load_module("daily_drill_tripwire", "tools/daily_drill.py")
    from src.github.client import GitHubClient

    original = GitHubClient.request
    with module.WriteTripwire() as tripwire, pytest.raises(AssertionError, match="对外写调用"):
        # self 用哑对象：守卫在碰到网络之前就该拦住
        GitHubClient.request(object(), "POST", "/repos/a/b/issues", body={})
    assert tripwire.attempts == ["POST /repos/a/b/issues"]
    # 退出来必须**原样恢复**（否则会把后面所有真实读调用都当成误操作）
    assert GitHubClient.request is original


# ------------------------------------------------------------------ 8.3 频率表到期审计

def test_cadence_marks_never_run_as_missing() -> None:
    """**无记录 ≠ 没问题**：没有产物的节奏必须单独成一档，不能算"未到期"。"""
    module = load_module("cadence_missing", "tools/cadence.py")
    findings = module.inspect((module.Cadence("从来没跑过的演练", 7, (), "怎么跑"),))
    assert findings[0].status == "missing"
    assert findings[0].age_days is None


def test_cadence_distinguishes_fresh_from_overdue(scratch: Path) -> None:
    module = load_module("cadence_ages", "tools/cadence.py")
    import os
    import time

    fresh = scratch / "fresh.json"
    stale = scratch / "stale.json"
    fresh.write_text("{}", encoding="utf-8")
    stale.write_text("{}", encoding="utf-8")
    old = time.time() - (10 * 24 + 12) * 3600        # 十天半前（留出半天，避免取整把 .days 卡在 9）
    os.utime(stale, (old, old))

    cadences = (
        module.Cadence("每天该跑的", 1, ("fresh.json",), "how"),
        module.Cadence("每周该跑的", 7, ("stale.json",), "how"),
    )
    by_name = {item.cadence.name: item for item in module.inspect(cadences, root=scratch)}
    assert by_name["每天该跑的"].status == "ok"
    assert by_name["每周该跑的"].status == "due", "十天前的产物对「每 7 天」来说就是到期了"
    assert by_name["每周该跑的"].age_days == 10


def test_cadence_due_only_hides_fresh_lines() -> None:
    module = load_module("cadence_render", "tools/cadence.py")
    findings = [
        module.Finding(module.Cadence("新鲜的", 7, (), "how"), "a.json", 1, "ok"),
        module.Finding(module.Cadence("该跑了", 7, (), "how"), "b.json", 30, "due"),
        module.Finding(module.Cadence("没跑过", 7, (), "how"), None, None, "missing"),
    ]
    body = module.render(findings, only_due=True)
    assert "该跑了" in body and "没跑过" in body and "新鲜的" not in body


def test_cadence_table_covers_the_roadmap_frequency_rows() -> None:
    """8.3 的表要**逐行**有对应，不能只挑好做的几行。"""
    module = load_module("cadence_table", "tools/cadence.py")
    names = "".join(item.name for item in module.CADENCES)
    for keyword in ("单元测试", "端到端", "脏测试", "混沌", "红队", "离线评测", "基线复测", "七天实战演练"):
        assert keyword in names, f"8.3 频率表里的「{keyword}」没有对应的节奏条目"


# ------------------------------------------------------------------ 8.3 退化检测

def test_checkup_alarms_only_after_two_consecutive_drops() -> None:
    module = load_module("checkup", "tools/checkup.py")
    past = [{"metrics": {"triage_accuracy": 0.80}}, {"metrics": {"triage_accuracy": 0.77}}]
    alarms = module.compare({"triage_accuracy": 0.74}, past)
    assert alarms and "连续两轮下降" in alarms[0]


def test_checkup_stays_quiet_on_a_single_drop() -> None:
    """只跌一轮（上轮持平、本轮下跌）更像波动，不报警。"""
    module = load_module("checkup", "tools/checkup.py")
    past = [{"metrics": {"triage_accuracy": 0.80}}, {"metrics": {"triage_accuracy": 0.80}}]
    assert module.compare({"triage_accuracy": 0.74}, past) == [], "只跌一轮不算退化"


def test_checkup_ignores_boolean_flags() -> None:
    """`unit_tests_ok` 这类布尔值不该被当成"指标下降"来比大小。"""
    module = load_module("checkup", "tools/checkup.py")
    past = [{"metrics": {"unit_tests_ok": True}}, {"metrics": {"unit_tests_ok": True}}]
    assert module.compare({"unit_tests_ok": False}, past) == []
