"""第七部分的确定性测试：技能文档、一致性、自检、批量引擎、CLI 兜底。

路线第七部分要的是三件事，这里逐条钉死：

1. **五段式**（7.2 第 2 条）：缺一段、TEST_GATE 改一个字、确认点为空，都必须被判出来；
2. **三者一一对应**（7.4 第 1 条）：指令清单、`AGENTS.md`、`skills/` 任何一处漂移都要报错；
3. **兜底通道**（7.2 第 3 条）：自然语言"系统好像坏了"必须路由到 `/应急`，
   且 CLI 的每个子命令都能在**不联网**的前提下被调用（`--dry-run`）。

外加 `/一键处理` 的五条专项约束（7.1 第 2 条）：上限 20、可续跑、单条失败不阻断、
末尾逐条审批（没写就是不批）。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.batch import (
    ITEM_DONE,
    ITEM_NEEDS_HUMAN,
    ITEM_PENDING,
    BatchItem,
    BatchState,
    apply_decisions,
    parse_decisions,
    pause,
    plan,
    render_sheet,
    run_batch,
)
from src.cli import HANDLERS, SUBCOMMANDS, build_parser
from src.cli import main as cli_main
from src.skills import (
    COMMANDS,
    TEST_GATE_TEXT,
    check_consistency,
    load_all,
    parse_skill,
    render_skill,
    route,
    run_checks,
    validate_skill,
)

# ------------------------------------------------------------------ 五段式

def test_all_eight_commands_are_registered() -> None:
    assert len(COMMANDS) == 8
    assert {entry.name for entry in COMMANDS} == {
        "/细化idea", "/处理反馈", "/一键处理", "/修复", "/找轮子", "/建新项目", "/体检", "/应急",
    }
    assert len({entry.slug for entry in COMMANDS}) == 8


def test_every_command_has_a_non_empty_confirmation_point() -> None:
    """路线 7.4 第 2 条：确认点列不能为空（纯只读写"无"）。"""
    for entry in COMMANDS:
        assert entry.confirmation.strip(), f"{entry.name} 没有确认点"
        assert entry.action.strip() and entry.stage.strip()


def test_rendered_skill_has_the_five_sections() -> None:
    for entry in COMMANDS:
        text = render_skill(entry)
        sections = parse_skill(text)
        assert len(sections) == 5, f"{entry.name} 段数不对：{list(sections)}"
        assert validate_skill(entry, text) == []


def test_validator_catches_a_tampered_skill() -> None:
    entry = COMMANDS[0]
    text = render_skill(entry)
    broken_gate = text.replace("四舍五入无效", "四舍五入可以接受")
    problems = validate_skill(entry, broken_gate)
    assert any("TEST_GATE" in problem for problem in problems)

    missing_section = text.replace("## 四、人类确认点", "## 四、别的")
    problems = validate_skill(entry, missing_section)
    assert any("人类确认点" in problem for problem in problems)


def test_skills_on_disk_are_consistent() -> None:
    result = check_consistency()
    assert result["ok"], result["problems"]
    assert result["checked"] == 8


def test_load_all_reads_eight_skills() -> None:
    loaded = load_all()
    assert len(loaded) == 8
    assert all(TEST_GATE_TEXT in text for _, text in loaded.values())


# ------------------------------------------------------------------ 一致性

def test_consistency_catches_a_missing_skill_file(scratch: Path) -> None:
    directory = scratch / "skills"
    directory.mkdir()
    for entry in COMMANDS:
        (directory / f"{entry.slug}.md").write_text(render_skill(entry), encoding="utf-8")
    (directory / "fix.md").unlink()
    agents = scratch / "AGENTS.md"
    agents.write_text("随便", encoding="utf-8")

    result = check_consistency(skills_dir=directory, agents_path=agents)
    assert result["ok"] is False
    assert any("fix.md" in problem for problem in result["problems"])
    assert any("AGENTS.md" in problem for problem in result["problems"])


def test_consistency_catches_an_extra_skill_file(scratch: Path) -> None:
    directory = scratch / "skills"
    directory.mkdir()
    for entry in COMMANDS:
        (directory / f"{entry.slug}.md").write_text(render_skill(entry), encoding="utf-8")
    (directory / "ghost.md").write_text("# 我是谁", encoding="utf-8")
    result = check_consistency(skills_dir=directory, agents_path=Path(__file__))
    assert any("ghost.md" in problem for problem in result["problems"])


# ------------------------------------------------------------------ 兜底路由

@pytest.mark.parametrize("text", ["系统好像坏了", "它卡住了", "一直报错", "not working at all"])
def test_natural_language_falls_back_to_emergency(text: str) -> None:
    entry = route(text)
    assert entry is not None and entry.slug == "emergency"


def test_route_returns_none_for_unrelated_text() -> None:
    assert route("帮我看看这个仓库的 README") is None
    assert route("") is None


def test_cli_covers_every_subcommand() -> None:
    assert set(HANDLERS) == set(SUBCOMMANDS)
    for name in SUBCOMMANDS:
        extra = ["x/y", "1"] if name in ("triage", "fix") else (["x/y"] if name == "batch" else [])
        assert build_parser().parse_args([name] + extra)


def test_cli_list_runs_offline(capsys) -> None:
    assert cli_main(["list"]) == 0
    out = capsys.readouterr().out
    assert "一致性校验：通过" in out
    for entry in COMMANDS:
        assert entry.usage in out


def test_cli_fix_dry_run_does_not_touch_anything(capsys, scratch: Path) -> None:
    assert cli_main(["fix", str(scratch), "42", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "文件定位" in out and "停下等人类" in out


def test_cli_batch_dry_run_prints_the_constraints(capsys) -> None:
    assert cli_main(["batch", "owner/repo", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "单次上限" in out and "逐条" in out


# ------------------------------------------------------------------ /应急 自检

def test_doctor_reports_a_missing_state_directory(scratch: Path) -> None:
    state = scratch / "state"
    (state / "approvals").mkdir(parents=True)
    (state / "mode.json").write_text(json.dumps({"mode": "online"}), encoding="utf-8")
    checks = run_checks(state_dir=state, client=object(), local_probe=lambda: (True, "ok"))
    broken = next(item for item in checks if item.name == "state 目录完整性")
    assert broken.ok is False
    assert "tasks/pending" in broken.detail
    assert broken.options, "诊断必须给出人接下来能做什么"


def test_doctor_distinguishes_offline_from_broken(scratch: Path) -> None:
    """offline 时连不上 GitHub 是**预期**，不该报红（否则断网时一片红，把人带偏）。"""
    state = scratch / "state"
    for name in ("tasks/pending", "tasks/doing", "tasks/done", "tasks/failed", "approvals",
                 "outbox", "reports", "corpus", "specs", "vectors", "patches", "repair"):
        (state / name).mkdir(parents=True, exist_ok=True)
    (state / "mode.json").write_text(json.dumps({"mode": "offline"}), encoding="utf-8")
    (state / ".write_token").write_text("x", encoding="utf-8")

    class Boom:
        def get(self, path: str, **kwargs: object) -> object:
            raise RuntimeError("network down")

    checks = run_checks(state_dir=state, client=Boom(), local_probe=lambda: (True, "ok"))
    github = next(item for item in checks if item.name == "GitHub 连通性")
    assert github.ok is True and "离线" in github.detail or github.note


def test_doctor_flags_stale_approvals(scratch: Path) -> None:
    state = scratch / "state"
    approvals = state / "approvals"
    approvals.mkdir(parents=True)
    (state / "mode.json").write_text(json.dumps({"mode": "online"}), encoding="utf-8")
    old = datetime.now(timezone.utc) - timedelta(hours=100)
    (approvals / "old.md").write_text(
        f"- **创建时间**：{old.strftime('%Y-%m-%dT%H:%M:%S%z')}\n", encoding="utf-8"
    )
    checks = run_checks(state_dir=state, client=object(), local_probe=lambda: (True, "ok"))
    stale = next(item for item in checks if item.name == "闸门悬挂审批")
    assert stale.ok is False and "old.md" in stale.detail


# ------------------------------------------------------------------ 批量引擎

def test_plan_caps_the_batch_and_reports_the_rest() -> None:
    issues = [(number, f"issue {number}") for number in range(1, 26)]
    state = plan(issues, repo="owner/repo", date="2026-09-12", cap=20)
    assert len(state.items) == 20
    assert state.remaining == 5, "超出的必须如实告知，不能悄悄丢掉"


def test_run_batch_continues_after_a_failure(scratch: Path) -> None:
    state = plan([(1, "a"), (2, "b"), (3, "c")], repo="r", date="2026-09-12")

    def handler(item: BatchItem) -> dict:
        if item.number == 2:
            raise RuntimeError("这一条炸了")
        return {"state": ITEM_DONE, "detail": "ok"}

    # `directory=scratch` 不能省：不传就落到仓库自己的 `state/batch_<date>.json`，
    # 于是**每跑一次测试，仓库工作树就脏一个文件**（实测：它出现在每一次提交里，
    # 还让安装副本的 `package.py verify .` 报"多出来的文件"）。
    # 测试绝不写进仓库的 state/ —— 与"G4 用例把草稿写进仓库根"是同一类毛病。
    run_batch(state, handler, save_every_item=False, directory=scratch)
    assert [item.state for item in state.items] == [ITEM_DONE, ITEM_NEEDS_HUMAN, ITEM_DONE]
    assert "炸了" in state.items[1].detail


def test_run_batch_is_resumable(scratch: Path) -> None:
    """断点续跑：已完成的不重做（否则会重复推送）。"""
    state = plan([(1, "a"), (2, "b")], repo="r", date="2026-09-12")
    calls: list[int] = []

    def handler(item: BatchItem) -> dict:
        calls.append(item.number)
        if item.number == 2:
            raise RuntimeError("先失败一次")
        return {"state": ITEM_DONE}

    run_batch(state, handler, directory=scratch)
    assert calls == [1, 2]

    reloaded = BatchState.load("2026-09-12", directory=scratch)
    reloaded.items[1].state = ITEM_PENDING     # 人修好之后从断点重跑
    processed: list[int] = []

    def second_pass(item: BatchItem) -> dict:
        processed.append(item.number)
        return {"state": ITEM_DONE}

    run_batch(reloaded, second_pass, directory=scratch)
    assert processed == [2], "第 1 条已经完成，不该被重做"


def test_pause_stops_before_the_next_item(scratch: Path) -> None:
    state = plan([(1, "a"), (2, "b")], repo="r", date="2026-09-12")
    state.paused = True
    # 同样必须落 scratch：`pause()`/`run_batch()` 不传目录就写仓库自己的 `state/`。
    pause(state, directory=scratch)
    run_batch(state, lambda item: {"state": ITEM_DONE}, save_every_item=False, directory=scratch)
    assert all(item.state == ITEM_PENDING for item in state.items), "暂停后一条都不该跑"


def test_sheet_lists_every_item_and_the_failures() -> None:
    state = plan([(1, "a"), (2, "b")], repo="owner/repo", date="2026-09-12")
    state.items[0].state = ITEM_DONE
    state.items[0].detail = "已生成 PR"
    state.items[1].state = ITEM_NEEDS_HUMAN
    state.items[1].detail = "补丁不收敛"
    sheet = render_sheet(state)
    assert "逐条回是/否" in sheet
    assert "批量呈现不等于批量放行" in sheet
    assert "补丁不收敛" in sheet


def test_decisions_are_per_item_and_undecided_stays_undecided(scratch: Path) -> None:
    state = plan([(1, "a"), (2, "b"), (3, "c")], repo="r", date="2026-09-12")
    for index, item in enumerate(state.items, start=1):
        item.ticket = f"t{index}.md"
        (scratch / item.ticket).write_text("（等人类）\n", encoding="utf-8")

    outcome = apply_decisions(state, "1 是 / 3 否", approvals_dir=scratch)
    assert [entry["decision"] for entry in outcome["applied"]] == ["是", "否"]
    assert outcome["undecided"] == [2], "没写就是不批，绝不默认通过"
    assert (scratch / "t1.md").read_text(encoding="utf-8").splitlines()[0] == "是"
    assert (scratch / "t3.md").read_text(encoding="utf-8").splitlines()[0] == "否"
    assert (scratch / "t2.md").read_text(encoding="utf-8").splitlines()[0] == "（等人类）"


def test_parse_decisions_ignores_chatter() -> None:
    assert parse_decisions("我觉得 1 是 可以，2 否，先这样") == {1: "是", 2: "否"}
    assert parse_decisions("都同意") == {}
