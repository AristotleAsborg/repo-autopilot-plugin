"""2.1 M1 Idea 细化器验收测试。

路线验收原文：**3 个真实 idea 各跑一遍；每轮输出过 schema；人工检查无复合问题。**

拆成两部分：

* 机制部分（确定性、用假生成器）：单问硬约束、重生成、12 轮上限、
  第 10 轮起的收敛提示、draft_updates 合并规则、每轮落盘与重载。
  这些是"必须每次都对"的东西，不该依赖模型今天心情如何。
* 真实部分（用本地小模型真跑 3 个 idea）：证明链路能端到端走通，
  并把问题原文落盘供人工检查。模型不可达时跳过，而不是假装通过。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.gateway.local_client import LocalModelError, available_models
from src.spec import (
    CONVERGE_FROM_ROUND,
    MAX_ROUNDS,
    CompoundQuestion,
    IdeaRefiner,
    Proposal,
    SpecDraft,
    SpecError,
    StopJudger,
    build_messages,
    checkability_report,
    find_compound_marker,
    is_machine_checkable,
    is_yes_no_question,
    machine_checkable_basis,
    new_draft,
)

REAL_IDEAS = [
    "我想做一个把零散笔记自动整理成周报的小工具",
    "给团队做一个只读的部署看板，能看出哪个环境落后了",
    "做一个命令行小工具，把一堆截图按内容相似度分组",
]


class ScriptedGenerator:
    """按脚本依次返回 proposal 的假生成器，并记录每次收到的 messages。"""

    def __init__(self, script: list[dict]) -> None:
        self.script = list(script)
        self.calls: list[list[dict[str, str]]] = []

    def __call__(self, messages: list[dict[str, str]], schema: object) -> dict:
        self.calls.append(messages)
        if not self.script:
            return {"question": "还有别的要补充吗？", "why": "脚本用完了", "draft_updates": {}}
        return self.script.pop(0)


@pytest.fixture
def refiner(state_dir: Path) -> IdeaRefiner:
    return IdeaRefiner(store_dir=state_dir / "specs")


# ==================================================== 单问硬约束

class TestSingleQuestion:
    @pytest.mark.parametrize("marker", ["且", "和", "以及", "或", "；", ";", "&"])
    def test_every_listed_marker_is_caught(self, marker: str) -> None:
        assert find_compound_marker(f"你希望 A {marker} B 都支持吗？") == marker

    def test_clean_question_passes(self) -> None:
        assert find_compound_marker("这个工具主要给谁用？") is None

    def test_longest_marker_wins_so_the_report_is_not_misleading(self) -> None:
        """「以及」里含「及」不含「和」，但「A以及B」必须报「以及」而不是被别的抢先。"""
        assert find_compound_marker("要支持导入以及导出吗？") == "以及"

    def test_compound_question_is_regenerated_then_accepted(self, refiner: IdeaRefiner) -> None:
        generator = ScriptedGenerator(
            [
                {"question": "你要 A 和 B 都支持吗？", "why": "第一次问了两个", "draft_updates": {}},
                # 注意：第二个问题必须**真的**含标记字。
                # 「还是」不是标记 —— 标记里的是「或」。测试数据写错会让这条用例
                # 假装在验重生成，其实第二轮就放行了（这一版就踩过一次）。
                {"question": "你要命令行或界面？", "why": "还是复合的", "draft_updates": {}},
                # 通过的那一问必须**同时**满足两条硬约束：单问 + 能用是/否回答。
                # 原来这里写的是「这个工具主要给谁用？」—— 那是开放问题（含「谁」），
                # 2026-09-12 加了代码兜底之后它会被打回，测试数据因此必须换成是/否问句。
                {"question": "这个工具第一版是否只给团队内部用？", "why": "终于单问且可判定", "draft_updates": {}},
            ]
        )
        refiner.generate = generator
        draft = new_draft("一个想法")
        proposal = refiner.propose(draft)

        assert proposal.question == "这个工具第一版是否只给团队内部用？"
        assert len(generator.calls) == 3, "应当重生成两次"
        # 重生成时必须把"上一问答错了什么"明确告诉模型，否则它只会原样重来
        reminder = generator.calls[1][-1]["content"]
        assert "和" in reminder and "复合问题" in reminder

    def test_persistently_compound_gives_up_instead_of_asking(self, refiner: IdeaRefiner) -> None:
        refiner.generate = ScriptedGenerator(
            [{"question": f"要 A 和 B 吗 {i}", "why": "", "draft_updates": {}} for i in range(5)]
        )
        refiner.regenerate_limit = 3
        with pytest.raises(CompoundQuestion) as excinfo:
            refiner.propose(new_draft("一个想法"))
        assert "不发出复合问题" in str(excinfo.value)


# ==================================================== schema 与轮次上限

class TestSchemaAndRounds:
    def test_proposal_requires_the_three_fields(self) -> None:
        ok = Proposal.model_validate({"question": "q", "why": "w", "draft_updates": {}})
        assert ok.question == "q"

        # pydantic 校验失败就是 ValidationError；写 `except Exception` 会把
        # 打错字段名这种**测试自身的错**也算成通过（实测 pydantic 抛 ValidationError）。
        with pytest.raises(ValidationError):
            Proposal.model_validate({"why": "缺 question"})

    def test_bad_generator_output_raises_spec_error(self, refiner: IdeaRefiner) -> None:
        refiner.generate = lambda messages, schema: {"nope": 1}
        with pytest.raises(SpecError):
            refiner.propose(new_draft("一个想法"))

    def test_convergence_note_appears_near_the_end(self) -> None:
        from src.spec import Round

        draft = new_draft("一个想法")
        # 先答满第 10 轮之前的问题
        for index in range(1, CONVERGE_FROM_ROUND):
            draft.rounds.append(Round(index=index, question=f"q{index}", answer="a"))

        # 下一个问题就是第 10 轮，必须带收敛提示
        system = build_messages(draft)[0]["content"]
        assert "剩余不超过" in system
        assert f"第 {CONVERGE_FROM_ROUND} 轮" in system

    def test_no_convergence_note_before_round_ten(self) -> None:
        draft = new_draft("一个想法")
        from src.spec import Round

        for index in range(1, CONVERGE_FROM_ROUND - 1):
            draft.rounds.append(Round(index=index, question=f"q{index}", answer="a"))
        assert "剩余不超过" not in build_messages(draft)[0]["content"]

    def test_round_cap_is_enforced(self, refiner: IdeaRefiner) -> None:
        from src.spec import Round

        draft = new_draft("一个想法")
        for index in range(1, MAX_ROUNDS + 1):
            draft.rounds.append(Round(index=index, question=f"q{index}", answer="a"))
        assert draft.is_full

        refiner.generate = ScriptedGenerator([{"question": "还能再问吗？", "why": "", "draft_updates": {}}])
        with pytest.raises(SpecError) as excinfo:
            refiner.propose(draft)
        assert "硬上限" in str(excinfo.value)

    def test_run_stops_at_the_hard_cap(self, refiner: IdeaRefiner) -> None:
        """给远超上限的回答数，也只能问 MAX_ROUNDS 轮 —— 上限不由模型或调用方决定。"""
        refiner.generate = ScriptedGenerator(
            [{"question": f"第{i}个问题？", "why": "", "draft_updates": {}} for i in range(40)]
        )
        draft = refiner.run("一个想法", [f"回答{i}" for i in range(MAX_ROUNDS + 5)])
        assert len(draft.rounds) == MAX_ROUNDS
        assert draft.answered_rounds == MAX_ROUNDS


# ==================================================== 合并与落盘

class TestMergeAndPersist:
    def test_merge_appends_and_dedupes(self) -> None:
        draft = new_draft("想法")
        draft.merge({"features": ["A", "B"]})
        draft.merge({"features": ["B", "C"], "non_goals": ["不做 D"]})
        assert draft.features == ["A", "B", "C"]
        assert draft.non_goals == ["不做 D"]

    def test_merge_never_removes_confirmed_content(self) -> None:
        """后一轮不能"撤回"已经和人类确认过的内容 —— 那是最危险的一种覆盖。"""
        draft = new_draft("想法")
        draft.merge({"features": ["A"]})
        draft.merge({"features": []})
        assert draft.features == ["A"]

    def test_merge_reports_what_it_adopted(self) -> None:
        draft = new_draft("想法")
        assert draft.merge({"features": ["A"]}) == ["features"]
        assert draft.merge({"features": ["A"]}) == []

    def test_ask_persists_and_reload_round_trips(self, refiner: IdeaRefiner) -> None:
        refiner.generate = ScriptedGenerator(
            [
                {
                    # 是/否问句（原来的「给谁用？」含「谁」，现在会被开放问题兜底打回）
                    "question": "第一版是否只做命令行界面？",
                    "why": "决定交互形态",
                    "draft_updates": {"features": ["CLI"]},
                }
            ]
        )
        draft = new_draft("一个想法")
        refiner.persist(draft)
        refiner.ask(draft)

        path = refiner.path_for(draft)
        assert path.exists(), "每问一轮都要落盘"
        reloaded = refiner.load(path)
        assert reloaded.rounds[0].question == "第一版是否只做命令行界面？"
        assert reloaded.features == ["CLI"], "提问时已能确认的内容不该丢"

        refiner.answer(draft, "给我自己用")
        again = refiner.load(path)
        assert again.rounds[0].answer == "给我自己用"

    def test_answer_twice_is_rejected(self, refiner: IdeaRefiner) -> None:
        refiner.generate = ScriptedGenerator([{"question": "q", "why": "", "draft_updates": {}}])
        draft = new_draft("想法")
        refiner.ask(draft)
        refiner.answer(draft, "第一次")
        with pytest.raises(SpecError):
            refiner.answer(draft, "第二次")

    def test_answer_without_question_is_rejected(self, refiner: IdeaRefiner) -> None:
        with pytest.raises(SpecError):
            refiner.answer(new_draft("想法"), "没有问过就回答")

    def test_partial_file_never_replaces_a_good_draft(self, refiner: IdeaRefiner) -> None:
        """半写的 spec 比没有 spec 更糟：下游会把它当完整草稿读进来。"""
        draft = new_draft("想法")
        refiner.generate = ScriptedGenerator([{"question": "q", "why": "", "draft_updates": {}}])
        refiner.ask(draft)
        path = refiner.path_for(draft)
        before = path.read_text(encoding="utf-8")

        refiner.ask(draft)
        # 目录里不该留下任何 .tmp 残骸
        leftovers = list(path.parent.glob(".*.tmp"))
        assert leftovers == []
        assert json.loads(path.read_text(encoding="utf-8"))["rounds"][0]["question"] == "q"
        assert before != path.read_text(encoding="utf-8")


# ==================================================== 3 个真实 idea

def _local_model_ready() -> tuple[bool, str]:
    try:
        names = available_models()
    except LocalModelError as exc:
        return False, str(exc)
    return ("qwen3:4b" in names), f"已装模型：{names}"


@pytest.mark.skipif(not _local_model_ready()[0], reason=f"本地模型不可用：{_local_model_ready()[1]}")
def test_three_real_ideas_end_to_end(state_dir: Path) -> None:
    """
    真实跑 3 个 idea。

    用本地小模型而不是 flash：`DEEPSEEK_API_KEY` 不在环境里，而 flash 的模型 id 也仍是
    待核对的占位值（详见 src/spec/refiner.py 的模块说明）。这里验的是**循环机制**，
    不是 flash 的文案质量；换 flash 只需要换生成器。
    """
    from src.gateway import chat

    refiner = IdeaRefiner(store_dir=state_dir / "specs", regenerate_limit=4)

    def generate(messages: list[dict[str, str]], schema: object) -> dict:
        return chat(
            messages,
            schema,
            "local_small",
            usage_path=state_dir / "token_usage.log",
        )

    refiner.generate = generate

    audit: list[dict] = []
    guard_events: list[dict] = []
    for idea in REAL_IDEAS:
        draft = new_draft(idea)
        refiner.persist(draft)
        # 手工驱动而不是 `run()`：质量兜底（复合/开放/重复）会抛异常停下，
        # 而"停下"本身就是要观察的行为之一 —— `run()` 会把异常直接抛穿。
        for text in ("是", "否", "是"):
            try:
                refiner.ask(draft)
            except SpecError as exc:
                guard_events.append({"idea": idea, "guard": type(exc).__name__, "reason": str(exc)[:200]})
                break
            refiner.answer(draft, text, updates={})

        assert draft.rounds, f"{idea!r} 一轮都没问出来"
        for item in draft.rounds:
            assert item.answered or item is draft.rounds[-1]
            assert find_compound_marker(item.question) is None, f"发出了复合问题：{item.question}"
            assert is_yes_no_question(item.question), f"发出了开放问题：{item.question}"
            audit.append({"idea": idea, "round": item.index, "question": item.question, "why": item.why})

        path = refiner.path_for(draft)
        reloaded = SpecDraft.model_validate_json(path.read_text(encoding="utf-8"))
        assert len(reloaded.rounds) == len(draft.rounds)

    # 人工检查的入口：问题原文落盘到**仓库的** state/reports（不是测试的临时目录），
    # 否则每次跑完就被冲掉，路线的"人工检查无复合问题"就无从检查。
    # 同时记下**质量兜底拦下了什么** —— 本地小模型档重复提问是已知短板（M1 实测报告 D2），
    # 拦下来是设计行为，不该让这条端到端用例变红。
    audit_path = ROOT / "state" / "reports" / "spec-questions-for-human-review.json"
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    audit_path.write_text(
        json.dumps({"questions": audit, "guard_events": guard_events}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    assert len(audit) >= len(REAL_IDEAS), f"3 个 idea 各至少要有 1 个问题，实际 {len(audit)}"
    for event in guard_events:
        assert "不发出" in event["reason"], "兜底拦下时必须说明'不发出什么'"


# ============================================ 运行报告 2026-09-18 的三项修复
#
# 三条都不是猜的 —— 是 agent 驱动（每条命令一个新进程）跑真卡跑出来的。
# 报告：`/细化idea` 卡片 86a0662d5e99。


def test_open_gaps_survive_a_process_boundary(state_dir: Path) -> None:
    """
    §1.1：缺口原来只活在 `IdeaRefiner.open_gaps`（进程内字段），而 `SpecDraft`
    没有承载字段 —— 于是"每条命令一个新进程"的驱动形态下**缺口下一轮就没了**。
    当时的办法是往 `state/specs/<id>.gaps.json` 写旁路日志：**绕过，不是修复**。

    判据：把缺口记进草稿，**丢掉整个 refiner**（等价于进程结束），
    再从源卡重建一个 —— 缺口必须还在，并且真的进了下一问的提示词。
    """
    draft = SpecDraft(id="boundary", idea="任务结束/卡住时通知手机")
    draft.acceptance = ["跑 `bash -c 'sleep 3; exit 1'` 后通知里含非零退出码"]

    first = IdeaRefiner(store_dir=state_dir / "specs", generate=ScriptedGenerator([]))
    first.note_gaps(["「卡住」的判定阈值没有定量"], draft)
    saved = first.persist(draft)
    assert "open_gaps" in json.loads(saved.read_text(encoding="utf-8")), "缺口必须落进源卡"

    # ---- 进程边界：refiner 全新，只有源卡是共享的 ----
    reloaded = SpecDraft.model_validate_json(saved.read_text(encoding="utf-8"))
    assert reloaded.open_gaps == ["「卡住」的判定阈值没有定量"]

    seen: list[str] = []

    def spy(messages: list[dict[str, str]], _schema: object) -> object:
        seen.append(json.dumps(messages, ensure_ascii=False))
        raise AssertionError("到这里就够：只看提示词里有没有那个缺口")

    second = IdeaRefiner(store_dir=state_dir / "specs", generate=spy)
    with pytest.raises(AssertionError):
        second.propose(reloaded)
    assert seen and any("卡住" in item for item in seen), "恢复出来的缺口必须进了下一问的提示词"


def test_render_view_does_not_clobber_the_final_handover(state_dir: Path) -> None:
    """
    §1.2：`finalize()` 与 `write_markdown()` 原来写**同一个** `state/specs/<id>.md`，
    后跑的覆盖先跑的 —— 实测把定稿覆盖成了渲染稿。

    判据：两个产物路径不同，且定稿在渲染稿写完之后**内容不变**。
    """
    draft = SpecDraft(id="collide", idea="两条路都写 md 会互相覆盖")
    draft.acceptance = ["`pytest -q` 返回 0", "系统应该好用"]
    refiner = IdeaRefiner(store_dir=state_dir / "specs", generate=ScriptedGenerator([]))

    official = StopJudger(spec_dir=state_dir / "specs").finalize(draft)
    before = official.read_text(encoding="utf-8")

    rendered = refiner.write_markdown(draft)
    assert rendered != official, "渲染稿与定稿必须是两个不同的文件"
    assert official.read_text(encoding="utf-8") == before, "定稿不许被渲染稿覆盖"


def test_checkability_reports_its_basis() -> None:
    """
    §1.3：这条启发式**很宽松**，只给 bool 时 `not_machine_checkable_count: 0`
    读不出"5 条都验得了"（它其实是"5 条都含反引号"的副产品）。
    判据：依据要能单独取出来、且与 `is_machine_checkable` 同源。
    """
    assert machine_checkable_basis("`pytest -q` 返回 0") == "含数字"
    assert machine_checkable_basis("命令返回 0") is not None
    assert machine_checkable_basis("系统应该好用") is None
    assert machine_checkable_basis("   ") is None

    draft = SpecDraft(id="basis", idea="判据要可审")
    draft.acceptance = ["`pytest -q` 返回 0", "系统应该好用"]
    report = checkability_report(draft)
    assert [item[1] is not None for item in report] == [True, False]
    for condition, basis in report:          # 同源：两处判据必须一致
        assert is_machine_checkable(condition) is (basis is not None)
