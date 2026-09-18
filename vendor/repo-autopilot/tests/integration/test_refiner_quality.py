"""M1 追问循环的**多轮质量**回归（2026-09-12，来自那份 M1 实测报告）。

那份报告的核心批评不是"功能坏了"，而是**验收口径没有覆盖多轮质量**：
既有 19 个用例全部用自造生成器、只断言"单问约束"，于是下面这些缺陷能整整齐齐地穿过去 ——

| 缺陷 | 现象 | 本文件的用例 |
|---|---|---|
| **Q1（人类亲自指出的严重问题）** | 提问是开放的（"哪一类指标必须优先监控？"），而路线 0.5.B① 要求**每轮用是/否回答** | `TestYesNoQuestion*` |
| D1 | `spec_digest` 在本轮 `draft_updates` 合并**之前**快照 → 2.2 的信息增益被系统性低估 | `test_digest_snapshot_includes_this_round_updates` |
| D2 | 本地档抽取什么都不沉淀 → 回答没进 spec（空转），且**不报错** | `TestOfflineFallback*`、`test_empty_updates_are_reported_as_a_warning` |
| D2b | 连续两轮问**同一句**问题（提示词写了不许，但没有代码兜底） | `TestDuplicateQuestion*` |
| D3 | `merge()` 只按精确串去重，换措辞的重述能穿过去 | `test_semantic_dedupe_drops_a_reworded_repeat` |
| D4 | 被后续轮次推翻的条目只能"并存"，终稿自带矛盾 | `TestRetraction*` |
| D5 | `completeness` 的输入含"当前未答轮次" → `missing` 口径不稳定 | `test_completeness_view_only_lists_answered_rounds` |
| D6 | 文档说落点是 `<id>.md`，实现只写 `<id>.draft.json` | `test_markdown_view_is_written_on_convergence` |
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.spec import (
    DUPLICATE_RATIO,
    IdeaRefiner,
    OpenQuestion,
    RepeatedQuestion,
    Round,
    SpecDraft,
    apply_yes_no_answer,
    both_targets,
    find_compound_marker,
    find_duplicate_question,
    find_open_question_marker,
    find_restrictive_marker,
    is_both_answer,
    is_yes_no_question,
    new_draft,
    options_conflict,
    parse_yes_no,
    render_markdown,
    spec_text,
)
from src.spec.stopper import build_completeness_messages


class ScriptedGenerator:
    def __init__(self, script: list[dict]) -> None:
        self.script = list(script)
        self.calls: list[list[dict[str, str]]] = []

    def __call__(self, messages: list[dict[str, str]], schema: object) -> dict:
        self.calls.append(messages)
        if not self.script:
            return {"question": "是否还有别的要补充？", "why": "脚本用完", "draft_updates": {}}
        return self.script.pop(0)


@pytest.fixture
def refiner(state_dir: Path) -> IdeaRefiner:
    return IdeaRefiner(store_dir=state_dir / "specs")


def fake_embed(texts: list[str]) -> np.ndarray:
    """
    确定性的假 embedding：**只按关键字区分**，用来验"语义去重"的判定路径。

    为什么不用真模型：这条用例要验的是"去重逻辑接上了没有"，不是"bge-m3 准不准"。
    真模型的语义能力由 3.2 的评测负责。
    """
    axes = ["越权", "心跳", "CLI", "报告"]
    vectors = []
    for text in texts:
        vector = [1.0 if axis in text else 0.0 for axis in axes]
        vectors.append(vector or [0.01, 0.01, 0.01, 0.01])
    return np.asarray(vectors, dtype=float)


# ==================================================== Q1：必须是是/否问题

class TestYesNoQuestion:
    @pytest.mark.parametrize(
        "question",
        [
            "第一版优先监控哪些指标？",
            "断线的判据是什么？",
            "为什么要做这个系统？",
            "需要监控几个指标？",
            "请列出必须支持的场景。",
            "说明一下越权的定义。",
        ],
    )
    def test_open_questions_are_detected(self, question: str) -> None:
        assert find_open_question_marker(question) is not None, question
        assert is_yes_no_question(question) is False

    @pytest.mark.parametrize(
        "question",
        [
            "第一版是否只监控『任务是否卡住』这一类指标？",
            "断线的判据是否就是『心跳超过 30 秒没有上报』？",
            "这个系统第一版需要多人协作吗？",
            "是否可以把当前这份 spec 当作可动工版本？",
        ],
    )
    def test_yes_no_questions_pass(self, question: str) -> None:
        assert find_open_question_marker(question) is None, question
        assert is_yes_no_question(question) is True

    def test_open_question_is_regenerated_then_accepted(self, refiner: IdeaRefiner) -> None:
        """**人类指出的那个严重问题**：开放问题必须被打回重问，而不是发出去。"""
        generator = ScriptedGenerator(
            [
                {"question": "第一版优先监控哪些指标？", "why": "开放问题", "draft_updates": {}},
                {"question": "第一版是否只监控任务是否卡住？", "why": "改成了是非判断", "draft_updates": {}},
            ]
        )
        refiner.generate = generator
        proposal = refiner.propose(new_draft("一个想法"))

        assert proposal.question == "第一版是否只监控任务是否卡住？"
        reminder = generator.calls[1][-1]["content"]
        assert "哪些" in reminder and "是" in reminder and "否" in reminder, (
            "重生成时必须把'为什么不行 + 该怎么改'讲清楚，否则模型只会原样重来"
        )

    def test_persistently_open_question_refuses_to_ask(self, refiner: IdeaRefiner) -> None:
        refiner.generate = ScriptedGenerator(
            [{"question": f"要监控哪些指标 {i}？", "why": "", "draft_updates": {}} for i in range(5)]
        )
        refiner.regenerate_limit = 3
        with pytest.raises(OpenQuestion) as excinfo:
            refiner.propose(new_draft("一个想法"))
        assert "不发出开放问题" in str(excinfo.value)


# ==================================================== D2b：不许原样重问

class TestDuplicateQuestion:
    def test_near_identical_question_is_flagged(self) -> None:
        previous = "第一版是否只监控任务是否卡住？"
        assert find_duplicate_question(previous, [previous]) is not None

    def test_a_different_angle_is_not_flagged(self) -> None:
        """**不能误杀**"换角度"的追问 —— 那是提示词明确鼓励的行为。"""
        history = ["第一版是否只监控任务是否卡住？"]
        assert find_duplicate_question("这个系统是否需要把监控结果推送到企业微信？", history) is None

    def test_repeated_question_is_regenerated(self, refiner: IdeaRefiner) -> None:
        first = "第一版是否只监控任务是否卡住？"
        generator = ScriptedGenerator([{"question": first, "why": "", "draft_updates": {}}])
        refiner.generate = generator
        draft = new_draft("一个想法")
        refiner.ask(draft)

        generator.script = [
            {"question": first, "why": "原样重问", "draft_updates": {}},
            {"question": "是否需要把监控结果推送到企业微信？", "why": "换了个角度", "draft_updates": {}},
        ]
        proposal = refiner.propose(draft)
        assert proposal.question == "是否需要把监控结果推送到企业微信？"
        assert "几乎一样" in generator.calls[-1][-1]["content"]

    def test_persistently_repeated_question_refuses_to_ask(self, refiner: IdeaRefiner) -> None:
        first = "第一版是否只监控任务是否卡住？"
        generator = ScriptedGenerator([{"question": first, "why": "", "draft_updates": {}}])
        refiner.generate = generator
        draft = new_draft("一个想法")
        refiner.ask(draft)

        generator.script = [{"question": first, "why": "", "draft_updates": {}} for _ in range(5)]
        refiner.regenerate_limit = 2
        with pytest.raises(RepeatedQuestion) as excinfo:
            refiner.propose(draft)
        assert "不发出重复问题" in str(excinfo.value)

    def test_threshold_is_not_so_loose_that_it_catches_rewrites(self) -> None:
        """阈值 0.85 是实测取的：换措辞的复述约 0.6~0.8，原样重问接近 1.0。"""
        assert DUPLICATE_RATIO == 0.85


# ==================================================== D1：快照必须在合并之后

def test_digest_snapshot_includes_this_round_updates(refiner: IdeaRefiner) -> None:
    """
    **D1 的回归**：回答里新确认的内容必须出现在这一轮的 `spec_digest` 里。

    取早了会让 2.2 的信息增益永远在看"上一轮的状态"，本轮变化被低估 ——
    它不报错，只是悄悄把人往"过早收敛"上推。
    """
    generator = ScriptedGenerator([{"question": "第一版是否只做 CLI？", "why": "", "draft_updates": {}}])
    refiner.generate = generator
    draft = new_draft("一个想法")
    refiner.ask(draft)
    refiner.answer(draft, "是", updates={"features": ["只做 CLI"]})

    assert "只做 CLI" in draft.rounds[-1].spec_digest
    assert "只做 CLI" in spec_text(draft)


def test_answer_merges_before_taking_the_digest_even_without_updates(refiner: IdeaRefiner) -> None:
    """`extract` 钩子给出的字段也必须先合并再快照（同一个顺序问题）。"""
    refiner.generate = ScriptedGenerator([{"question": "第一版是否只做 CLI？", "why": "", "draft_updates": {}}])
    refiner.extract = lambda draft, question, answer, options=(), no_means=(): {"features": ["只做 CLI"]}
    draft = new_draft("一个想法")
    refiner.ask(draft)
    refiner.answer(draft, "是")
    assert "只做 CLI" in draft.rounds[-1].spec_digest


# ==================================================== D2：空转可见 + 离线兜底

class TestOfflineFallback:
    def test_parse_yes_no_refuses_to_guess(self) -> None:
        assert parse_yes_no("是") is True
        assert parse_yes_no("不，不需要") is False
        assert parse_yes_no("先按最简单的做法来") is None, "判不出来就必须返回 None，绝不猜"
        assert parse_yes_no("") is None

    def test_yes_answer_lands_in_features_and_no_in_non_goals(self) -> None:
        draft = new_draft("想法")
        assert apply_yes_no_answer(draft, "第一版是否只做 CLI？", "是") == ["features"]
        assert apply_yes_no_answer(draft, "是否需要多人协作？", "否") == ["non_goals"]
        assert draft.features == ["第一版只做 CLI"], draft.features
        # **「否」必须落成显式否定**（第二轮实测 F2）：原来存的是肯定式命题
        # 「需要多人协作」，意思与人类回答**正好相反**。
        assert draft.non_goals == ["不做：需要多人协作"], draft.non_goals

    def test_no_answer_can_never_be_read_as_a_yes(self) -> None:
        """F2 的核心断言：`non_goals` 里的任何一条都不能读成"要做这件事"。"""
        draft = new_draft("想法")
        apply_yes_no_answer(draft, "第一版是否只做 CLI？", "否")
        apply_yes_no_answer(draft, "是否需要多人协作？", "不，不需要")
        assert all(item.startswith("不做：") for item in draft.non_goals), draft.non_goals

    def test_negated_question_inverts_the_answer(self) -> None:
        """
        反向提问的陷阱：「是否不需要 X？」回答「否」= **需要** X。

        中文是非问句里这是固有坑，代码必须处理 —— 不能指望模型每轮都问肯定式问题。
        """
        draft = new_draft("想法")
        assert apply_yes_no_answer(draft, "第一版是否不需要多人协作？", "否") == ["features"]
        assert draft.features == ["第一版不需要多人协作"], draft.features
        assert draft.non_goals == []

    def test_template_answers_are_used_only_when_extraction_yields_nothing(self, refiner: IdeaRefiner) -> None:
        refiner.generate = ScriptedGenerator([{"question": "第一版是否只做 CLI？", "why": "", "draft_updates": {}}])
        refiner.template_answers = True
        draft = new_draft("想法")
        refiner.ask(draft)
        refiner.answer(draft, "是")
        assert draft.features == ["第一版只做 CLI"]
        assert "template" in draft.rounds[-1].adopted

    def test_empty_updates_are_reported_as_a_warning(self, refiner: IdeaRefiner) -> None:
        """**D2 的可见性**：本地档抽不出东西时必须留下痕迹，不能静默空转。"""
        refiner.generate = ScriptedGenerator([{"question": "第一版是否只做 CLI？", "why": "", "draft_updates": {}}])
        draft = new_draft("想法")
        refiner.ask(draft)
        refiner.answer(draft, "随便说点什么")      # 模型没抽（没配 extract），模板也判不出来
        assert draft.rounds[-1].adopted == []
        assert refiner.warnings and "没有沉淀任何字段" in refiner.warnings[0]


# ==================================================== D3：语义去重

def test_semantic_dedupe_drops_a_reworded_repeat() -> None:
    """**D3 的回归**：换措辞的重述不该被当成新需求。"""
    draft = new_draft("想法")
    draft.merge({"features": ["越权请求的判据是预先指定的对话框清单"]})
    adopted = draft.merge(
        {"features": ["以预先指定的对话框清单作为越权证据"]},
        embed_fn=fake_embed,
        dedupe_threshold=0.92,
    )
    assert adopted == [], f"重述被当成了新条目：{draft.features}"
    assert len(draft.features) == 1


def test_semantic_dedupe_keeps_genuinely_new_items() -> None:
    draft = new_draft("想法")
    draft.merge({"features": ["越权请求的判据是预先指定的对话框清单"]})
    adopted = draft.merge({"features": ["靠心跳判断任务是否卡住"]}, embed_fn=fake_embed, dedupe_threshold=0.92)
    assert adopted == ["features"]
    assert len(draft.features) == 2


def test_semantic_dedupe_is_off_by_default_because_merge_must_not_depend_on_the_model() -> None:
    """**默认不做语义去重**：`merge` 是纯数据操作，不该悄悄依赖本地模型。"""
    draft = new_draft("想法")
    draft.merge({"features": ["A"]})
    assert draft.merge({"features": ["A"]}) == []


# ==================================================== D4：撤回要留痕

class TestRetraction:
    def test_retract_moves_the_item_to_withdrawn_and_keeps_the_record(self) -> None:
        draft = new_draft("想法")
        draft.merge({"features": ["监控是否有越权请求", "监控任务是否卡住"]})
        adopted = draft.merge({"retracts": ["监控是否有越权请求"]})

        assert adopted == ["withdrawn"]
        assert draft.features == ["监控任务是否卡住"], "被推翻的条目不该继续算作承诺"
        assert draft.withdrawn == ["监控是否有越权请求"], "但记录必须留着（人类曾经要过它）"
        assert "已撤回" in spec_text(draft), "撤回也必须反映在给 2.2 看的文本里"

    def test_retract_of_an_unknown_item_changes_nothing(self) -> None:
        draft = new_draft("想法")
        draft.merge({"features": ["A"]})
        assert draft.merge({"retracts": ["不存在的条目"]}) == []
        assert draft.features == ["A"] and draft.withdrawn == []

    def test_withdrawn_items_are_shown_to_the_model(self, refiner: IdeaRefiner) -> None:
        from src.spec import build_messages

        draft = new_draft("想法")
        draft.merge({"features": ["F"]})
        draft.merge({"retracts": ["F"]})
        prompt = json.dumps(build_messages(draft), ensure_ascii=False)
        assert "已撤回" in prompt and "F" in prompt


# ==================================================== D5：诊断口径固定

def test_completeness_view_only_lists_answered_rounds() -> None:
    """
    **D5 的回归**：`missing` 说的是"需求还差什么"，不是"你欠我一个答案"。

    所以打分的输入里不能出现当前那一轮还没回答的问题 —— 否则同一个卡片在两个时点
    会给出不一致的 `missing`，这个诊断就没人信了。
    """
    from src.spec import Round

    draft = new_draft("想法")
    draft.rounds.append(Round(index=1, question="第一版是否只做 CLI？", answer="是"))
    draft.rounds.append(Round(index=2, question="是否需要多人协作？"))     # 当前轮，未答

    text = build_completeness_messages(draft)[1]["content"]
    assert "第一版是否只做 CLI？" in text
    assert "是否需要多人协作？" not in text, "未答的当前轮不该进完整度打分的输入"
    assert "（未答）" not in text


# ==================================================== D6：文档与实现对齐

def test_markdown_view_is_written_on_convergence(refiner: IdeaRefiner) -> None:
    """文档说落点有 `.md`，实现原来只写 `.draft.json` → 现在两个都有，源卡为准。"""
    refiner.generate = ScriptedGenerator(
        [{"question": "第一版是否只做 CLI？", "why": "", "draft_updates": {"features": ["只做 CLI"]}}]
    )
    draft = refiner.run("一个想法", ["是"])
    markdown = refiner.markdown_path_for(draft)
    assert markdown.is_file(), "收敛后要写出给人看的 .md"
    text = markdown.read_text(encoding="utf-8")
    assert "只做 CLI" in text and "源卡（唯一真相）" in text
    assert refiner.path_for(draft).is_file(), "源卡仍然是 .draft.json"

    reloaded = SpecDraft.model_validate_json(refiner.path_for(draft).read_text(encoding="utf-8"))
    # 这份 spec 没有验收条件 → 状态必须是 `converged_with_gaps`（F1），
    # 不能与"真的可以动工"共用 `converged`。
    assert reloaded.status == "converged_with_gaps", reloaded.status
    assert "功能" in render_markdown(reloaded)


# ==================================================== F4：没有验收条件要吼

def test_markdown_says_loudly_when_acceptance_is_empty() -> None:
    draft = new_draft("想法")
    draft.features = ["做一个 CLI"]
    text = render_markdown(draft)
    assert "没有任何验收条件" in text and "不能算可动工" in text, text
    assert "## 验收条件" in text


def test_markdown_is_quiet_when_acceptance_exists() -> None:
    draft = new_draft("想法")
    draft.acceptance = ["`pytest -q` 全绿"]
    text = render_markdown(draft)
    assert "没有任何验收条件" not in text
    assert "`pytest -q` 全绿" in text


def test_the_acceptance_requirement_is_in_the_base_prompt_not_only_the_closing_note() -> None:
    """
    第四轮实测：卡 D 第 10 轮就自然收敛，而从第 22 轮才出现的收尾提示**根本没机会生效** ——
    于是唯一那条验收条件照样不可机器验证。所以这条要求必须在**第一轮**的提示里就有。
    """
    from src.spec import build_messages

    base = build_messages(new_draft("想法"), convergence_note=False)[0]["content"]
    assert "acceptance" in base
    assert "能跑出来" in base, "要说清验收条件得是命令/阈值/可数结果"
    assert "converged_with_gaps" in base, "要让模型知道写空话的后果"


# ==================================================== F5：问答历史要好扫读

def test_markdown_puts_the_question_before_the_reason() -> None:
    """理由是二三百字的长句，放在问题前面会把问题埋掉（F5）。"""
    draft = new_draft("想法")
    draft.rounds.append(
        Round(
            index=1,
            question="第一版是否只做 CLI？",
            why="「轻量化」可以走两条完全不同的路，选哪条会决定后面全部的实现方式。",
            answer="是",
        )
    )
    text = render_markdown(draft)
    q_pos = text.index("**问**：第一版是否只做 CLI？")
    why_pos = text.index("理由：")
    assert q_pos < why_pos, "问题必须在理由之前"
    # 理由整体缩进，扫读时不会与问题混在一起
    assert "\n   <sub>理由：" in text


# ==================================================== F3：双重否定要看得见

def test_double_negation_in_non_goals_is_reported(refiner: IdeaRefiner) -> None:
    """
    flash 有时把「是否只靠 X」的"否"写成「不只靠 X」（双重否定，读起来像"还是靠一点"）。
    这是措辞层的不稳定，只**告警**不改写 —— 但必须看得见，否则它会顺着问答历史污染后续提问。

    （问句是限制性的，所以夹具按 G5 的新约定声明 `no_means`：不声明的话，
    第一条告警会是"没声明否分支"，这条用例就测不到双重否定了。）
    """
    refiner.generate = ScriptedGenerator(
        [
            {
                "question": "第一版是否只靠心跳发现异常？",
                "why": "",
                "draft_updates": {},
                "no_means": ["客户端主动上报"],
            }
        ]
    )
    draft = new_draft("想法")
    refiner.ask(draft)
    refiner.answer(draft, "否", updates={"non_goals": ["第一版不只靠 agent 主动上报心跳来发现异常"]})

    assert refiner.warnings, "双重否定必须留下告警"
    assert "双重否定" in refiner.warnings[0] and "不只" in refiner.warnings[0]


def test_no_warning_when_non_goals_use_the_explicit_form(refiner: IdeaRefiner) -> None:
    refiner.generate = ScriptedGenerator(
        [
            {
                "question": "第一版是否只靠心跳发现异常？",
                "why": "",
                "draft_updates": {},
                "no_means": ["客户端主动上报"],
            }
        ]
    )
    draft = new_draft("想法")
    refiner.ask(draft)
    refiner.answer(draft, "否", updates={"non_goals": ["不做：只靠 agent 主动上报心跳来发现异常"]})
    assert refiner.warnings == [], refiner.warnings


def test_extract_prompt_forbids_double_negation() -> None:
    from src.spec import build_extract_messages

    system = build_extract_messages(new_draft("想法"), "第一版是否只靠心跳？", "否")[0]["content"]
    assert "不做" in system and "双重否定" in system
    assert "不只" in system, "要给反例，光说'不要双重否定'模型不知道指什么"


# ==================================================== G2：诊断要反馈进提问

def test_open_gaps_are_injected_into_the_next_question() -> None:
    """
    **G2 的回归**：`completeness.missing` 连续多轮列出同样的缺口（"还缺输入输出格式"），
    却从来没有变成一个问题 —— 诊断信息与提问策略脱节，那份 spec 因此一直空着。
    """
    from src.spec import build_messages

    draft = new_draft("想法")
    prompt = json.dumps(
        build_messages(draft, open_gaps=["还缺输入输出格式", "还缺判定规则"]), ensure_ascii=False
    )
    assert "还没有被任何一轮问到过" in prompt
    assert "还缺输入输出格式" in prompt
    assert "可机器检查" in prompt, "缺口里含验收口径时，要提醒问成可判定的形式"


def test_refiner_feeds_recorded_gaps_into_ask(state_dir: Path) -> None:
    from src.spec import IdeaRefiner, build_messages

    captured: list[list[dict[str, str]]] = []

    def generator(messages: list[dict[str, str]], schema: object) -> dict:
        captured.append(messages)
        return {"question": "第一版是否只做 CLI？", "why": "", "draft_updates": {}}

    refiner = IdeaRefiner(store_dir=state_dir / "specs", generate=generator)
    refiner.note_gaps(["还缺验收口径"])
    refiner.ask(new_draft("想法"))

    assert captured, "生成器必须被调用"
    assert "还缺验收口径" in json.dumps(captured[0], ensure_ascii=False)
    # 顺带确认 `build_messages` 的默认行为没变（不传缺口时不出现那一段）
    assert "还没有被任何一轮问到过" not in json.dumps(build_messages(new_draft("想法")), ensure_ascii=False)


# ==================================================== G4：候选项要带进抽取

def test_question_options_reach_the_extractor(state_dir: Path) -> None:
    """**G4 的回归**：是/否问句常是"是 A 而不是 B"，答"两者都要"时 B 不能丢。"""
    from src.spec import IdeaRefiner

    seen: list[tuple] = []

    def generator(messages: list[dict[str, str]], schema: object) -> dict:
        return {
            "question": "这个脚本的第一用途是否是骰点判定，而不是角色数值录入与派生计算？",
            "why": "",
            "draft_updates": {},
            "options": ["骰点判定", "角色数值录入与派生计算"],
        }

    def extractor(draft, question, answer, options=(), no_means=()):
        seen.append(tuple(options))
        return {"features": ["骰点判定", "角色数值录入与派生计算"]}

    # store_dir 必须落在 scratch 里：早先写 `Path(".")`，把草稿丢进了**仓库根目录**（实测踩过）
    refiner = IdeaRefiner(store_dir=state_dir / "specs", generate=generator, extract=extractor)
    draft = new_draft("想法")
    refiner.ask(draft)
    refiner.answer(draft, "两者都要")

    assert draft.rounds[0].options == ["骰点判定", "角色数值录入与派生计算"]
    assert seen == [("骰点判定", "角色数值录入与派生计算")], "候选项必须传到抽取器"
    assert len(draft.features) == 2, "两个选项要分别落条，不能只剩一句概括"


def test_extract_prompt_tells_the_model_to_split_non_binary_answers() -> None:
    from src.spec import build_extract_messages

    messages = build_extract_messages(
        new_draft("想法"), "是 A 而不是 B 吗？", "两者都要", options=["A", "B"]
    )
    text = json.dumps(messages, ensure_ascii=False)
    assert "候选项：A / B" in text
    assert "分别" in text


# ==================================================== G3：非目标不要膨胀成镜像

def test_extract_prompt_forbids_mirror_negations() -> None:
    """G3：把"另一个选项"机械否定一遍塞进 `non_goals`，跑十轮就有一半是同义反复。"""
    from src.spec import build_extract_messages

    system = build_extract_messages(new_draft("想法"), "q", "a")[0]["content"]
    assert "主动排除" in system
    assert "同义反复" in system


# ==================================================== G5：限制性问句的「否」不能白说

class TestRestrictiveMarker:
    def test_marker_catches_the_three_common_wordings(self) -> None:
        assert find_restrictive_marker("第一版是否只做正算？") == "只"
        assert find_restrictive_marker("是否仅支持命令行？") == "仅"
        assert find_restrictive_marker("是否单纯做数值计算？") == "单纯"

    def test_ordinary_yes_no_question_has_no_marker(self) -> None:
        assert find_restrictive_marker("这个系统的第一目标是否是替代人工巡检？") is None

    def test_negated_forms_are_not_read_as_restrictive(self) -> None:
        """
        「不仅/不只/不单」里的「只/仅」是**强调扩展**，不是收窄 —— 认成限制性问句
        会反过来要求模型给"否"分支候选，而那句话本来就是"否"的意思。
        """
        assert find_restrictive_marker("是否不仅可以正算，还可以反算？") is None
        assert find_restrictive_marker("是否不只做正算？") is None


class TestNoBranchIsCarriedIntoTheNextQuestion:
    def test_no_means_declared_by_the_generator_lands_on_the_round(self, state_dir: Path) -> None:
        refiner = IdeaRefiner(
            store_dir=state_dir / "specs",
            generate=ScriptedGenerator(
                [
                    {
                        "question": "第一版是否只做正算？",
                        "why": "",
                        "draft_updates": {},
                        "no_means": ["反算（给定预算枚举可行组合）"],
                    }
                ]
            ),
        )
        draft = new_draft("TRPG 数值计算脚本")
        refiner.ask(draft)
        assert draft.rounds[0].no_means == ["反算（给定预算枚举可行组合）"]

    def test_no_answer_puts_the_wider_scope_into_the_next_question(self, state_dir: Path) -> None:
        """
        **G5 的回归**：第 7 轮「是否只做正算？」答"否"整轮空转（语义变化 0.0000），
        而那句"否"的正信息是"范围更宽" —— 它必须变成下一问的题目，而不是留在日志里。
        """
        captured: list[list[dict[str, str]]] = []
        script = [
            {
                "question": "第一版是否只做正算？",
                "why": "",
                "draft_updates": {},
                "no_means": ["反算（给定预算枚举可行组合）"],
            },
            {"question": "是否还需要支持反算（给定预算枚举可行组合）？", "why": "", "draft_updates": {}},
        ]

        def generator(messages: list[dict[str, str]], schema: object) -> dict:
            captured.append(messages)
            return script.pop(0)

        refiner = IdeaRefiner(store_dir=state_dir / "specs", generate=generator)
        draft = new_draft("TRPG 数值计算脚本")
        refiner.ask(draft)
        refiner.answer(draft, "否", updates={"non_goals": ["不做：第一版只做正算"]})

        assert refiner.carry_over, "那句「否」必须被接住"
        assert "反算" in refiner.carry_over[0], refiner.carry_over

        refiner.ask(draft)
        assert len(draft.rounds) == 2
        prompt = json.dumps(captured[1], ensure_ascii=False)
        assert "否掉了收窄命题" in prompt, prompt
        assert "反算" in prompt, "候选项要出现在下一问的上下文里"
        assert refiner.carry_over == [], "已经变成具体问题之后要清掉（否则每轮重复塞同一段）"

    def test_no_means_absent_is_visible_and_still_carried(self, state_dir: Path) -> None:
        """没声明候选时退化成通用指令，但**必须留痕** —— 看得见才修得了。"""
        refiner = IdeaRefiner(
            store_dir=state_dir / "specs",
            generate=ScriptedGenerator(
                [{"question": "第一版是否只做正算？", "why": "", "draft_updates": {}}]
            ),
        )
        draft = new_draft("想法")
        refiner.ask(draft)
        refiner.answer(draft, "否")

        assert any("no_means" in warning for warning in refiner.warnings), refiner.warnings
        assert refiner.carry_over and "还要做什么" in refiner.carry_over[0], refiner.carry_over

    def test_carry_over_survives_a_later_note_gaps(self, state_dir: Path) -> None:
        """
        两个缺口通道的生命期不同：`note_gaps` 每次判停后整批替换 `open_gaps`，
        而"人类已经把话说出来了、只是还没说清"不能被它覆盖掉 —— 覆盖就等于丢。
        """
        refiner = IdeaRefiner(
            store_dir=state_dir / "specs",
            generate=ScriptedGenerator(
                [{"question": "第一版是否只做正算？", "why": "", "draft_updates": {}}]
            ),
        )
        draft = new_draft("想法")
        refiner.ask(draft)
        refiner.answer(draft, "否")
        refiner.note_gaps(["还缺输入输出格式"])

        assert refiner.open_gaps == ["还缺输入输出格式"]
        assert refiner.carry_over, "note_gaps 不能把 carry_over 冲掉"

    def test_yes_and_ordinary_no_carry_nothing(self, state_dir: Path) -> None:
        for answer in ("是",):
            refiner = IdeaRefiner(
                store_dir=state_dir / "specs",
                generate=ScriptedGenerator(
                    [{"question": "第一版是否只做正算？", "why": "", "draft_updates": {}}]
                ),
            )
            draft = new_draft("想法")
            refiner.ask(draft)
            refiner.answer(draft, answer)
            assert refiner.carry_over == [], answer

        refiner = IdeaRefiner(
            store_dir=state_dir / "specs",
            generate=ScriptedGenerator(
                [{"question": "是否需要多人协作？", "why": "", "draft_updates": {}}]
            ),
        )
        draft = new_draft("想法")
        refiner.ask(draft)
        refiner.answer(draft, "否")
        assert refiner.carry_over == [], "普通问句的「否」= 排除，不是「还要做别的」"

    def test_generator_prompt_requires_the_no_branch_for_restrictive_questions(self) -> None:
        from src.spec import build_messages

        system = build_messages(new_draft("想法"))[0]["content"]
        assert "no_means" in system
        assert "限制性问句" in system or "收窄" in system


class TestExtractSideOfTheRestrictiveNo:
    def test_extract_prompt_tells_the_model_to_land_the_expansion(self) -> None:
        from src.spec import build_extract_messages

        system = build_extract_messages(new_draft("想法"), "第一版是否只做正算？", "否")[0]["content"]
        assert "范围更宽" in system
        assert "不要编造" in system, "只知道「更宽」而不知道宽在哪里时不能编"

    def test_no_means_reaches_the_extractor(self) -> None:
        from src.spec import build_extract_messages

        messages = build_extract_messages(
            new_draft("想法"), "第一版是否只做正算？", "否", no_means=["反算", "派生计算"]
        )
        text = json.dumps(messages, ensure_ascii=False)
        assert "反算 / 派生计算" in text
        assert "写成 `features`" in text

    def test_extractor_receives_the_question_no_means(self, state_dir: Path) -> None:
        seen: list[tuple] = []

        def generator(messages: list[dict[str, str]], schema: object) -> dict:
            return {
                "question": "第一版是否只做正算？",
                "why": "",
                "draft_updates": {},
                "no_means": ["反算"],
            }

        def extractor(draft, question, answer, options=(), no_means=()):
            seen.append(tuple(no_means))
            return {"features": ["除正算外还需支持反算"]}

        refiner = IdeaRefiner(store_dir=state_dir / "specs", generate=generator, extract=extractor)
        draft = new_draft("想法")
        refiner.ask(draft)
        refiner.answer(draft, "否")

        assert seen == [("反算",)], "否分支候选必须传到抽取器"
        assert draft.features == ["除正算外还需支持反算"]


class TestEmptyRoundIsVisibleInTheArtifact:
    def test_markdown_marks_a_round_that_settled_nothing(self) -> None:
        """G5 建议③：空转不能只活在运行日志里 —— 终稿要看得见。"""
        draft = new_draft("想法")
        draft.rounds.append(Round(index=1, question="第一版是否只做正算？", answer="否", adopted=[]))
        text = render_markdown(draft)
        assert "没有沉淀任何字段" in text

    def test_markdown_is_quiet_when_the_round_settled_something(self) -> None:
        draft = new_draft("想法")
        draft.rounds.append(
            Round(index=1, question="第一版是否只做正算？", answer="是", adopted=["features"])
        )
        assert "没有沉淀任何字段" not in render_markdown(draft)


# ==================================================== G1：两个入口同一条口径

def test_both_entry_points_agree_about_uncheckable_acceptance(state_dir: Path) -> None:
    """
    **G1 的第二处落点**（第四轮实测报告 I3 踩到的那条缝）。

    `IdeaRefiner.run()` 原来自己复制了半条验收规则（只判 `acceptance` 空不空），
    于是同一份草稿 —— 验收条件都在、却一条都不可机器验证 —— 在 `run()` 的产物里是
    `converged`（完整），在官方入口 `StopJudger.finalize()` 里是 `converged_with_gaps`。
    两个入口给出的状态不一致，比单纯"状态失真"更糟：谁都不知道该信哪一份。
    现在两处都调 `acceptance_gap()`（单一口径），这条用例把它们钉在一起。
    """
    from src.spec import StopJudger

    refiner = IdeaRefiner(
        store_dir=state_dir / "specs",
        generate=ScriptedGenerator(
            [
                {
                    "question": "第一版是否只做 CLI？",
                    "why": "",
                    "draft_updates": {"acceptance": ["系统应该好用"]},
                    "no_means": ["图形界面"],
                }
            ]
        ),
    )
    draft = refiner.run("想法", ["是"])

    assert draft.acceptance == ["系统应该好用"]
    assert draft.status == "converged_with_gaps", draft.status
    assert "converged_with_gaps" in refiner.markdown_path_for(draft).read_text(encoding="utf-8")

    official = StopJudger(spec_dir=state_dir / "specs").finalize(draft)
    assert "converged_with_gaps" in official.read_text(encoding="utf-8"), "两个入口必须同口径"


# ==================================================== 第三支答案：两者都要

class TestBothAnswerRecognition:
    def test_both_answer_is_never_read_as_yes(self) -> None:
        """
        **这一支的核心**：`parse_yes_no("两者都要")` 原来返回 True（"要"在 `YES_WORDS` 里），
        于是只落下一个压缩后的命题、"两者"里的第二支静默丢掉。
        """
        assert parse_yes_no("两者都要") is None, "「两者都要」不是是/否，不能被读成「是」"
        assert parse_yes_no("两者都是") is None
        assert is_both_answer("两者都要") is True
        assert is_both_answer("都做") is True
        assert is_both_answer("both") is True

    def test_negations_are_not_both_answers(self) -> None:
        for text in ("两者都不要", "两个都不做", "都不需要", "否", "不", ""):
            assert is_both_answer(text) is False, text
            assert parse_yes_no(text) is False or parse_yes_no(text) is None, text

    def test_a_later_negation_does_not_flip_an_earlier_both_answer(self) -> None:
        """
        「两者都要，**不要**二选一」里那句"不要"否的是二选一，不是"两者"本身。

        宽匹配（"含『不要』就否掉"）会把这句话读成「否」，落成一条
        「不做：…」——**意思正好相反**，比认不出来糟得多。所以只认
        「都/两者 + 不」这种否定句式，且**最先出现的那一段决定口气**。
        """
        assert is_both_answer("两者都要，不要二选一") is True
        assert parse_yes_no("两者都要，不要二选一") is None, "它仍然不是是/否"
        assert is_both_answer("两个都要，不要只做一个") is True
        assert is_both_answer("都做，但不要互相冲突") is True
        assert is_both_answer("两者都不要，二选一吧") is False

    def test_plain_yes_no_is_untouched(self) -> None:
        assert parse_yes_no("是") is True
        assert parse_yes_no("否") is False
        assert is_both_answer("是") is False
        assert is_both_answer("先做正算") is False

    def test_options_conflict_only_claims_what_it_can_prove(self) -> None:
        """
        **不假装能判语义矛盾**："只做正算" vs "只做反算" 到底冲不冲突要模型才判得准，
        而误判的代价是把人类合理的"两个都要"挡在门外 —— 所以代码只认说得清的两种。
        """
        assert options_conflict(["缓存", "不做：缓存"]) is not None, "正反两面必须认出来"
        assert options_conflict(["缓存", "不支持缓存"]) is not None
        assert options_conflict(["缓存", "缓存"]) is not None, "重复条目不是两个分支"
        assert options_conflict(["只做正算", "只做反算"]) is None, "语义矛盾不归代码判"
        assert options_conflict(["正算", "反算"]) is None
        assert options_conflict(["只有一个"]) is None, "一个候选项谈不上矛盾"


class TestBothAnswerLanding:
    def test_each_declared_option_lands_as_a_feature(self) -> None:
        draft = new_draft("想法")
        question = "这个脚本的第一用途是否是骰点判定，而不是角色数值录入与派生计算？"
        adopted = apply_yes_no_answer(
            draft, question, "两者都要", options=["骰点判定", "角色数值录入与派生计算"]
        )
        assert adopted == ["features"]
        assert draft.features == ["骰点判定", "角色数值录入与派生计算"], draft.features
        assert draft.non_goals == [], "「两者都要」不是否定，不该产生非目标"

    def test_three_declared_options_all_land(self) -> None:
        """声明 3 个分支时「都要」= 三个都落（「两者」按"本轮声明的全部分支"理解）。"""
        targets, problem = both_targets("都要", ["A", "B", "C"])
        assert problem is None and targets == ["A", "B", "C"]

    def test_without_options_nothing_lands_and_the_reason_is_explicit(self) -> None:
        """**绝不猜**：问题没声明候选项时，"两者"是哪两者无从得知 —— 不许落盘。"""
        draft = new_draft("想法")
        targets, problem = both_targets("两者都要", [])
        assert targets == [] and problem is not None and "没有声明候选项" in problem
        assert apply_yes_no_answer(draft, "第一版是否只做 CLI？", "两者都要") == []
        assert draft.features == [] and draft.non_goals == []

    def test_contradictory_options_are_refused(self) -> None:
        targets, problem = both_targets("两者都要", ["缓存", "不做：缓存"])
        assert targets == [], "正反两面之间「两个都要」没有意义"
        assert problem is not None and "矛盾" in problem

    def test_not_a_both_answer_returns_no_problem(self) -> None:
        assert both_targets("是", ["A", "B"]) == ([], None), "普通回答不该被判成问题"


class TestBothAnswerInTheLoop:
    def _refiner(self, state_dir: Path, **kwargs) -> IdeaRefiner:
        script = [
            {
                "question": "这个脚本的第一用途是否是骰点判定，而不是角色数值录入与派生计算？",
                "why": "",
                "draft_updates": {},
                "options": ["骰点判定", "角色数值录入与派生计算"],
            }
        ]
        return IdeaRefiner(
            store_dir=state_dir / "specs", generate=ScriptedGenerator(script), **kwargs
        )

    def test_offline_path_lands_both_without_any_model(self, state_dir: Path) -> None:
        """**离线档也不丢**：这一支由代码落，不依赖 flash 抽取。"""
        refiner = self._refiner(state_dir, template_answers=True)
        draft = new_draft("TRPG 数值脚本")
        refiner.ask(draft)
        refiner.answer(draft, "两者都要")

        assert draft.features == ["骰点判定", "角色数值录入与派生计算"], draft.features
        assert draft.rounds[-1].adopted == ["features"], draft.rounds[-1].adopted
        assert refiner.warnings == [], refiner.warnings
        assert refiner.carry_over == []

    def test_extraction_still_runs_for_the_nuance(self, state_dir: Path) -> None:
        """
        「两者都要，**但第一版先做正算**」里后半句是候选项之外的补充信息 ——
        代码只落两个分支，抽取器继续跑，两条路的结果都进 spec。
        """
        seen: list[tuple] = []

        def extractor(draft, question, answer, options=(), no_means=()):
            seen.append(tuple(options))
            return {"features": ["第一版先做骰点判定"]}

        refiner = self._refiner(state_dir, extract=extractor)
        draft = new_draft("TRPG 数值脚本")
        refiner.ask(draft)
        refiner.answer(draft, "两者都要，但第一版先做骰点判定")

        assert draft.features == [
            "骰点判定",
            "角色数值录入与派生计算",
            "第一版先做骰点判定",
        ], draft.features
        assert seen == [("骰点判定", "角色数值录入与派生计算")], "候选项要传给抽取器"
        assert refiner.warnings == [], refiner.warnings

    def test_both_answer_without_options_warns_and_forces_the_next_question(self, state_dir: Path) -> None:
        """没声明候选项时：一句都不许落，但**必须**变成下一问的题目（不许白问）。"""
        captured: list[list[dict[str, str]]] = []
        script = [
            {"question": "第一版是否只做 CLI？", "why": "", "draft_updates": {}},
            {"question": "除了 CLI 是否还要做图形界面？", "why": "", "draft_updates": {}},
        ]

        def generator(messages: list[dict[str, str]], schema: object) -> dict:
            captured.append(messages)
            return script.pop(0)

        refiner = IdeaRefiner(
            store_dir=state_dir / "specs", generate=generator, template_answers=True
        )
        draft = new_draft("想法")
        refiner.ask(draft)
        refiner.answer(draft, "两者都要")

        assert draft.features == [], "没声明候选项就没法落 —— 绝不许猜"
        assert any("两者都要" in warning for warning in refiner.warnings), refiner.warnings
        assert refiner.carry_over and "两者" in refiner.carry_over[0]

        refiner.ask(draft)
        assert "两者" in json.dumps(captured[1], ensure_ascii=False), "必须带进下一问"

    def test_contradictory_options_are_refused_and_carried(self, state_dir: Path) -> None:
        refiner = IdeaRefiner(
            store_dir=state_dir / "specs",
            generate=ScriptedGenerator(
                [
                    {
                        "question": "第一版是否支持缓存而不是不做缓存？",
                        "why": "",
                        "draft_updates": {},
                        "options": ["缓存", "不做：缓存"],
                    }
                ]
            ),
            template_answers=True,
        )
        draft = new_draft("想法")
        refiner.ask(draft)
        refiner.answer(draft, "两者都要")

        assert draft.features == [], "矛盾候选项下「两个都要」不许落盘"
        assert any("矛盾" in warning for warning in refiner.warnings), refiner.warnings
        assert refiner.carry_over, "落不下去就必须变成下一问"


class TestBothAnswerVisibility:
    def test_render_shows_the_candidates_and_the_third_answer(self) -> None:
        """不呈现候选，人类根本没法回答「两者都要」—— 这一条是那一支可用的前提。"""
        draft = new_draft("想法")
        draft.rounds.append(
            Round(
                index=1,
                question="第一版是否只做 CLI？",
                answer="两者都要",
                options=["CLI", "图形界面"],
            )
        )
        text = render_markdown(draft)
        assert "候选：CLI / 图形界面" in text
        assert "两者都要" in text

    def test_render_does_not_advertise_the_third_answer_for_contradictory_options(self) -> None:
        draft = new_draft("想法")
        draft.rounds.append(
            Round(index=1, question="要不要缓存？", answer="是", options=["缓存", "不做：缓存"])
        )
        text = render_markdown(draft)
        assert "自相矛盾" in text
        assert "两项会分别落成条目" not in text, "说了做不到的话等于骗人"

    def test_finalize_prints_the_candidates(self, state_dir: Path) -> None:
        from src.spec import StopJudger

        draft = new_draft("想法")
        draft.acceptance = ["`pytest -q` 退出码 0"]
        draft.rounds.append(
            Round(
                index=1,
                question="第一版是否只做 CLI？",
                answer="两者都要",
                options=["CLI", "图形界面"],
                adopted=["features"],
            )
        )
        draft.features = ["CLI", "图形界面"]
        text = StopJudger(spec_dir=state_dir / "specs").finalize(draft).read_text(encoding="utf-8")
        assert "候选：CLI / 图形界面" in text

    def test_extract_prompt_asks_for_what_the_candidates_do_not_cover(self) -> None:
        from src.spec import build_extract_messages

        messages = build_extract_messages(
            new_draft("想法"), "是 A 而不是 B 吗？", "两者都要", options=["A", "B"]
        )
        text = json.dumps(messages, ensure_ascii=False)
        assert "候选项之外" in text, "抽取器要知道自己补的是候选项没覆盖的部分"

    def test_both_answer_that_adds_nothing_says_the_question_was_redundant(self, state_dir: Path) -> None:
        """两个候选都早已在 spec 里时，"空转"的理由与"抽不出东西"不同，要分开报。"""
        options = ["骰点判定", "角色数值录入与派生计算"]
        refiner = IdeaRefiner(
            store_dir=state_dir / "specs",
            generate=ScriptedGenerator(
                [
                    {
                        "question": "这个脚本的第一用途是否是骰点判定，而不是角色数值录入与派生计算？",
                        "why": "",
                        "draft_updates": {},
                        "options": options,
                    },
                    {
                        "question": "第一版是否把骰点判定与角色数值录入都算作核心功能，而不是只取其一？",
                        "why": "",
                        "draft_updates": {},
                        "options": options,
                    },
                ]
            ),
        )
        draft = new_draft("TRPG 数值脚本")
        refiner.ask(draft)
        refiner.answer(draft, "两者都要")
        refiner.ask(draft)
        refiner.answer(draft, "两者都要")           # 第二次：两个候选都已在 spec 里

        assert draft.features == options, draft.features
        assert any("多余的" in warning for warning in refiner.warnings), refiner.warnings
        assert not any("没有沉淀任何字段" in warning for warning in refiner.warnings), refiner.warnings


# ==================================================== G7：诊断不要拿实现细节当缺口

def test_completeness_prompt_separates_need_level_from_implementation() -> None:
    """
    **G7（2026-09-15 第五轮实测）**：完整度诊断连续两轮把
    「名字冲突怎么办」「名字的长度/字符集限制」这类**实现细节**列成 `missing`，
    还把已经能由清单推出的东西（「多次运行之间保存」⇒ 必然跨进程，不可能只在内存）
    当成缺口。它们占着诊断、把提问引开真正的分叉（第四轮 10 轮里也有同族浪费）。

    代码判不准"这是实现细节吗"，硬过滤会把真缺口一起丢掉 —— 所以这一层修在**提示词**，
    这条用例只钉住"分层规矩确实写进了提示词"（效果要看下一次真跑）。
    """
    from src.spec.stopper import COMPLETENESS_SYSTEM

    assert "需求层" in COMPLETENESS_SYSTEM
    assert "实现细节不是缺口" in COMPLETENESS_SYSTEM
    assert "字符集" in COMPLETENESS_SYSTEM, "要举具体例子，光说「分层」模型不知道指什么"
    assert "逻辑蕴含" in COMPLETENESS_SYSTEM, "已被现有清单推出的结论不许再当缺口"
    assert "多次运行" in COMPLETENESS_SYSTEM, "要把实测里那条自造缺口当反例写进去"


def test_open_gaps_instruction_tells_the_generator_not_to_spend_a_round_on_details() -> None:
    """缺口反馈进提问时，必须同时说清「哪些缺口不值得花一轮」（G2 的通道 + G7 的分层）。"""
    from src.spec import build_messages

    prompt = json.dumps(
        build_messages(new_draft("想法"), open_gaps=["保存数值时如何处理名字冲突"]),
        ensure_ascii=False,
    )
    assert "实现细节" in prompt
    assert "不要为它单独花一轮" in prompt
    assert "跳过它" in prompt


# ==================================================== 第六轮实测 P0③/P1⑤：确认型回答的兜底

class TestConfirmationFallback:
    """
    第六轮受控实验的结论：抽取**不是**坏掉（空草稿 60/60 全落地），
    但草稿被填满之后模型会"合理地"返回空数组 —— 于是人类**亲口点头**的需求静默消失。
    卡 `2b461b3f3bf6` 第 4 轮的"命令行入口"就是这样丢的，而终稿显示 `converged`（完整）。

    `apply_yes_no_answer()` 本来就能救回这一轮，只是**默认没被触发**。现在默认开。
    """

    def test_yes_no_answer_lands_by_default_when_extraction_returns_empty(self, state_dir: Path) -> None:
        refiner = IdeaRefiner(
            store_dir=state_dir / "specs",
            generate=ScriptedGenerator(
                [{"question": "第一版是否会提供命令行入口？", "why": "", "draft_updates": {}}]
            ),
            extract=lambda draft, question, answer, options=(), no_means=(): {},   # 模拟假阴性
        )
        draft = new_draft("TRPG 数值脚本")
        refiner.ask(draft)
        refiner.answer(draft, "是")

        assert draft.features == ["第一版会提供命令行入口"], draft.features
        assert "template" in draft.rounds[-1].adopted
        assert refiner.warnings == [], refiner.warnings

    def test_no_answer_lands_the_explicit_negation_by_default(self, state_dir: Path) -> None:
        refiner = IdeaRefiner(
            store_dir=state_dir / "specs",
            generate=ScriptedGenerator(
                [{"question": "是否需要多人协作？", "why": "", "draft_updates": {}}]
            ),
            extract=lambda draft, question, answer, options=(), no_means=(): {},
        )
        draft = new_draft("想法")
        refiner.ask(draft)
        refiner.answer(draft, "否")
        assert draft.non_goals == ["不做：需要多人协作"], draft.non_goals

    def test_the_fallback_does_not_invent_anything_for_free_text(self, state_dir: Path) -> None:
        """兜底只对**能判定的是/否**生效；判不出来就什么都不做（绝不猜）。"""
        refiner = IdeaRefiner(
            store_dir=state_dir / "specs",
            generate=ScriptedGenerator([{"question": "第一版是否只做 CLI？", "why": "", "draft_updates": {}}]),
            extract=lambda draft, question, answer, options=(), no_means=(): {},
        )
        draft = new_draft("想法")
        refiner.ask(draft)
        refiner.answer(draft, "先按最简单的来")
        assert draft.features == [] and draft.non_goals == []
        assert draft.rounds[-1].adopted == []
        assert any("没有沉淀任何字段" in warning for warning in refiner.warnings)

    def test_the_fallback_still_dedupes_semantically(self, state_dir: Path) -> None:
        """
        兜底默认开启 ⇒ 它有**膨胀 spec 的风险**（问题措辞与已有条目很像时会多加一条）。
        所以它必须走与其它合并路径同一个语义去重（D3），否则就会变成 G3 那个毛病。
        """
        refiner = IdeaRefiner(
            store_dir=state_dir / "specs",
            generate=ScriptedGenerator(
                [{"question": "是否需要靠心跳判断任务是否卡住？", "why": "", "draft_updates": {}}]
            ),
            extract=lambda draft, question, answer, options=(), no_means=(): {},
            embed_fn=fake_embed,
        )
        draft = new_draft("想法")
        draft.features = ["靠心跳判断任务是否卡住"]        # 换措辞的近重复
        refiner.ask(draft)
        refiner.answer(draft, "是")
        assert len(draft.features) == 1, draft.features

    def test_extract_prompt_says_confirmation_answers_are_always_new_information(self) -> None:
        from src.spec import build_extract_messages

        system = build_extract_messages(new_draft("想法"), "第一版是否会提供命令行入口？", "是")[0]["content"]
        assert "确认型回答永远算新信息" in system
        assert "空数组" in system
        assert "逐条核对" in system, "要给出例外条件，否则模型干脆什么都不敢返回"

    def test_the_fallback_flag_can_still_be_turned_off(self, state_dir: Path) -> None:
        """合成草稿的评测想关就关得掉（`template_answers=False` 的行为没变）。"""
        refiner = IdeaRefiner(
            store_dir=state_dir / "specs",
            generate=ScriptedGenerator([{"question": "第一版是否只做 CLI？", "why": "", "draft_updates": {}}]),
            extract=lambda draft, question, answer, options=(), no_means=(): {},
            template_answers=False,
        )
        draft = new_draft("想法")
        refiner.ask(draft)
        refiner.answer(draft, "是")
        assert draft.features == [], "显式关掉之后必须什么都不落（离线降级仍是可选项）"


# ==================================================== B2：生产生成器要有覆盖

def _flash_key_available() -> bool:
    from src.gateway import load_api_key

    key, _source = load_api_key()
    return bool(key)


@pytest.mark.skipif(not _flash_key_available(), reason="flash 档没有 key（CI 上属预期，本机有）")
def test_production_generator_asks_a_yes_no_question(state_dir: Path) -> None:
    """
    **B2**：既有 19 个用例全部注入自造生成器，于是**生产路径（flash）完全没有测试覆盖**。

    这条用默认生成器（`_default_generate` → flash）真问一次，并断言**人类指出的那条硬约束**：
    问题必须能用是/否回答、且不是复合问题。这是"改完真的好了"的最强证据 ——
    自造生成器只能证明代码路径，证明不了模型的真实行为。
    """
    refiner = IdeaRefiner(store_dir=state_dir / "specs")
    draft = new_draft("做一个把零散笔记自动整理成周报的小工具")
    refiner.persist(draft)

    proposal = refiner.propose(draft)

    assert proposal.question.strip(), "flash 必须给出问题"
    assert find_compound_marker(proposal.question) is None, f"复合问题：{proposal.question}"
    assert is_yes_no_question(proposal.question), f"开放问题（人类只能答是/否）：{proposal.question}"
