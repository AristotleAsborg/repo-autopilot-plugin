"""2.2 停止判断器验收测试（确定性部分）。

三路信号各自单独验、再验表决；最后一类专门验"某一路失灵时会不会误停" ——
那是这类系统最危险的失败模式：**该继续的时候停了**，而且不会有任何报错。
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.spec import (
    COMPLETENESS_THRESHOLD,
    INFO_GAIN_THRESHOLD,
    MAX_ROUNDS,
    Round,
    SpecDraft,
    StopJudger,
    StopperError,
    cosine,
    is_machine_checkable,
    new_draft,
    semantic_delta,
    spec_text,
)

DIM = 4


def unit(*values: float) -> list[float]:
    vector = np.asarray(values, dtype=float)
    return (vector / np.linalg.norm(vector)).tolist()


def make_draft(rounds: list[tuple[str, str]], **fields: object) -> SpecDraft:
    draft = new_draft("一个想法")
    for index, (question, answer) in enumerate(rounds, start=1):
        # `adopted=["features"]`：这些夹具轮次代表"问到了、也记下来了"的正常轮次。
        # 2026-09-15 第六轮实测 P0① 之后，`adopted=[]`（问过答了却什么都没沉淀）
        # 本身就算一条缺口 → 终稿状态会落 `converged_with_gaps`。
        # 想验"没有缺口就 converged"的用例，必须用**真的沉淀过东西**的轮次（下面这条）。
        draft.rounds.append(
            Round(index=index, question=question, answer=answer, adopted=["features"])
        )
    # 默认给一条可机器检查的验收条件：**没有它就不许收敛**（F1 的硬下限），
    # 于是"想验别的信号"的用例必须显式说明自己要造一份没有验收条件的草稿。
    draft.acceptance = ["`pytest -q` 全绿"]
    for key, value in fields.items():
        setattr(draft, key, value)
    return draft


# ==================================================== F1：完备度下限（硬拦）

class TestConvergenceGate:
    """
    2026-09-12 第二轮实测 F1：**默认参数下 4 轮就判收敛**，而那份 spec
    只有 2 条功能、**0 条验收条件**。两个投"停"的信号都对"不完整"不敏感 ——
    `info_gain` 量的是变化速率，`completeness` 由本地小模型给且很宽松。

    所以这里钉死一条**与模型无关**的下限：没有验收条件就不许判停（硬上限除外）。
    """

    def test_acceptance_gate_blocks_a_majority_vote(self) -> None:
        # 3 轮 → 2 个相邻快照差 → `info_gain` 才可能投停（它要"连续 2 轮"低于阈值）
        draft = make_draft([("q1", "a1"), ("q2", "a2"), ("q3", "a3")], acceptance=[])
        for item in draft.rounds:
            item.spec_digest = "A"
        judger = StopJudger(
            score=lambda m, s: {"completeness": 0.95, "missing": []},
            embed=lambda texts: [unit(1, 0, 0, 0) for _ in texts],
        )
        decision = judger.judge(draft)

        assert decision.votes >= 2, "两路信号都投了停（正是 F1 的场景）"
        assert decision.stop is False, "没有验收条件时不许收敛"
        assert any("验收条件" in item for item in decision.blocked_by_gaps), decision.blocked_by_gaps
        assert "完备度下限拦下" in decision.reason

    def test_round_cap_still_wins_over_the_gate(self) -> None:
        """硬上限必须还有出口：到了 24 轮就停（状态会被标成 `converged_with_gaps`）。"""
        draft = make_draft(
            [(f"q{i}", f"a{i}") for i in range(1, MAX_ROUNDS + 1)], acceptance=[]
        )
        judger = StopJudger(score=lambda m, s: {"completeness": 0.1, "missing": []})
        decision = judger.judge(draft)
        assert decision.stop is True and decision.forced_by_cap is True
        assert decision.blocked_by_gaps == [], "硬上限生效时不该再报'被拦住'"

    def test_gate_is_satisfied_once_acceptance_exists(self) -> None:
        draft = make_draft([("q1", "a1"), ("q2", "a2"), ("q3", "a3")])
        for item in draft.rounds:
            item.spec_digest = "A"
        judger = StopJudger(
            score=lambda m, s: {"completeness": 0.95, "missing": []},
            embed=lambda texts: [unit(1, 0, 0, 0) for _ in texts],
        )
        assert judger.judge(draft).stop is True

    def test_high_score_with_gaps_is_downgraded(self) -> None:
        """
        模型自相矛盾（既说 0.95 又列出两条缺口）时，按"有缺口"处理。

        两条缺口是照着 spec 说的，高分会把"没完备"糊过去 —— 判定必须站在缺口那边。
        """
        draft = make_draft([("q1", "a1"), ("q2", "a2")])
        judger = StopJudger(
            score=lambda m, s: {"completeness": 0.95, "missing": ["还缺异常阈值", "还缺进程层级"]}
        )
        decision = judger.judge(draft)
        signal = next(s for s in decision.signals if s.name == "completeness")
        assert signal.suggests_stop is False, "自相矛盾时不许投停"
        assert "模型自相矛盾" in signal.detail
        assert "还缺异常阈值" in signal.detail

    def test_coherent_high_score_still_votes_stop(self) -> None:
        """分数高**且没有缺口**时照旧投停 —— 别把这条规则变成"永远不停"。"""
        draft = make_draft([("q1", "a1"), ("q2", "a2")])
        judger = StopJudger(score=lambda m, s: {"completeness": 0.95, "missing": []})
        signal = next(s for s in judger.judge(draft).signals if s.name == "completeness")
        assert signal.suggests_stop is True

    def test_finalize_marks_converged_with_gaps(self, scratch: Path) -> None:
        from src.spec import Completeness

        draft = make_draft([("q1", "a1")], acceptance=[])
        judger = StopJudger(spec_dir=scratch / "specs")
        target = judger.finalize(
            draft, completeness=Completeness(completeness=0.4, missing=["还缺异常阈值"])
        )

        assert draft.status == "converged_with_gaps", draft.status
        text = target.read_text(encoding="utf-8")
        assert "converged_with_gaps" in text
        assert "还缺异常阈值" in text, "缺口要写进定稿，人一眼能看到"
        assert "没有任何验收条件" in text

    def test_finalize_marks_converged_when_there_are_no_gaps(self, scratch: Path) -> None:
        from src.spec import Completeness

        draft = make_draft([("q1", "a1")])
        judger = StopJudger(spec_dir=scratch / "specs")
        judger.finalize(draft, completeness=Completeness(completeness=0.95, missing=[]))

        assert draft.status == "converged", draft.status

    # ---- G1（第三轮实测报告）：验收条件"非空但不能验"同样算缺口

    def test_vague_acceptance_alone_blocks_a_complete_verdict(self, scratch: Path) -> None:
        """
        **G1 的回归**：用一句没法验的话（"系统应该好用"）占住 `acceptance` 的位置，
        原来就能拿到 `converged`（完整）—— 那让这个状态彻底失去意义。
        """
        from src.spec import Completeness, acceptance_gap, unresolved_gaps

        draft = make_draft([("q1", "a1")], acceptance=["系统应该好用"])
        assert acceptance_gap(draft) is not None, "非空但不可机器检查 → 必须算缺口"
        assert unresolved_gaps(draft), unresolved_gaps(draft)
        assert "不可机器验证" in (acceptance_gap(draft) or "")

        judger = StopJudger(spec_dir=scratch / "specs")
        judger.finalize(draft, completeness=Completeness(completeness=0.95, missing=[]))
        assert draft.status == "converged_with_gaps", draft.status

    def test_one_checkable_criterion_is_enough(self) -> None:
        """**不许矫枉过正**：只要有一条能机器检查的验收条件，就不算验收缺口。"""
        from src.spec import acceptance_gap

        draft = make_draft([("q1", "a1")], acceptance=["系统应该好用", "`pytest -q` 全绿"])
        assert acceptance_gap(draft) is None

    def test_vague_acceptance_blocks_the_stop(self) -> None:
        draft = make_draft([("q1", "a1"), ("q2", "a2"), ("q3", "a3")], acceptance=["要足够快"])
        for item in draft.rounds:
            item.spec_digest = "A"
        judger = StopJudger(
            score=lambda m, s: {"completeness": 0.95, "missing": []},
            embed=lambda texts: [unit(1, 0, 0, 0) for _ in texts],
        )
        decision = judger.judge(draft)
        assert decision.stop is False
        assert any("不可机器验证" in item for item in decision.blocked_by_gaps)



class FakeEmbedder:
    """按文本查表返回向量；没有登记过的文本返回一个默认向量。"""

    def __init__(self, table: dict[str, list[float]], default: list[float] | None = None) -> None:
        self.table = table
        self.default = default or unit(1, 0, 0, 0)
        self.calls: list[list[str]] = []

    def __call__(self, texts: list[str]) -> np.ndarray:
        self.calls.append(list(texts))
        return np.asarray([self.table.get(text, self.default) for text in texts], dtype=float)


# ==================================================== 相似度

class TestSimilarity:
    def test_cosine_of_identical_vectors_is_one(self) -> None:
        assert cosine([1, 2, 3], [2, 4, 6]) == pytest.approx(1.0)

    def test_cosine_of_orthogonal_vectors_is_zero(self) -> None:
        assert cosine([1, 0], [0, 1]) == pytest.approx(0.0)

    def test_cosine_does_not_assume_normalised_input(self) -> None:
        """传进来的向量可能没归一化；静默算错比算慢糟糕得多。"""
        assert cosine([3.0, 0.0], [10.0, 0.0]) == pytest.approx(1.0)

    def test_zero_vector_is_an_error_not_a_zero(self) -> None:
        with pytest.raises(StopperError):
            cosine([0, 0], [1, 1])

    def test_semantic_delta_is_one_minus_cosine(self) -> None:
        assert semantic_delta([1, 0], [1, 0]) == pytest.approx(0.0)
        assert semantic_delta([1, 0], [0, 1]) == pytest.approx(1.0)


# ==================================================== 信号① 完整度

class TestCompletenessSignal:
    def test_high_score_suggests_stop(self, state_dir: Path) -> None:
        judger = StopJudger(score=lambda messages, schema: {"completeness": 0.9, "missing": []})
        draft = make_draft([("q", "a")])
        decision = judger.judge(draft, gains=[1.0], )
        signal = next(s for s in decision.signals if s.name == "completeness")
        assert signal.suggests_stop

    def test_low_score_does_not(self, state_dir: Path) -> None:
        judger = StopJudger(
            score=lambda messages, schema: {"completeness": 0.4, "missing": ["还没定给谁用"]}
        )
        decision = judger.judge(make_draft([("q", "a")]), gains=[1.0])
        signal = next(s for s in decision.signals if s.name == "completeness")
        assert not signal.suggests_stop
        assert "还没定给谁用" in signal.detail

    def test_threshold_is_the_roadmap_number(self) -> None:
        assert COMPLETENESS_THRESHOLD == 0.85

    def test_bad_schema_raises(self) -> None:
        judger = StopJudger(score=lambda messages, schema: {"completeness": 2.0})
        with pytest.raises(StopperError):
            judger.completeness(make_draft([("q", "a")]))


# ==================================================== 信号② 信息增益

class TestInfoGainSignal:
    def test_two_quiet_rounds_suggest_stop(self) -> None:
        judger = StopJudger(score=lambda m, s: {"completeness": 0.1, "missing": []})
        decision = judger.judge(make_draft([("q", "a")]), gains=[0.5, 0.01, 0.005])
        signal = next(s for s in decision.signals if s.name == "info_gain")
        assert signal.suggests_stop

    def test_one_quiet_round_is_not_enough(self) -> None:
        judger = StopJudger(score=lambda m, s: {"completeness": 0.1, "missing": []})
        decision = judger.judge(make_draft([("q", "a")]), gains=[0.5, 0.01])
        signal = next(s for s in decision.signals if s.name == "info_gain")
        assert not signal.suggests_stop

    def test_noisy_rounds_do_not_suggest_stop(self) -> None:
        judger = StopJudger(score=lambda m, s: {"completeness": 0.1, "missing": []})
        decision = judger.judge(make_draft([("q", "a")]), gains=[0.5, 0.3, 0.2])
        signal = next(s for s in decision.signals if s.name == "info_gain")
        assert not signal.suggests_stop

    def test_threshold_is_the_roadmap_number(self) -> None:
        assert INFO_GAIN_THRESHOLD == 0.02

    def test_gains_are_computed_from_accumulated_spec_text(self) -> None:
        """比较的是**整份 spec** 的变化，不是单条回答的长度。"""
        draft = make_draft([("q1", "a1"), ("q2", "a2")])
        draft.rounds[0].spec_digest = "功能：A"
        draft.rounds[1].spec_digest = "功能：A"          # 完全没变
        embedder = FakeEmbedder({"功能：A": unit(1, 0, 0, 0)})
        judger = StopJudger(embed=embedder)
        assert judger.information_gains(draft) == [pytest.approx(0.0, abs=1e-9)]

    def test_fewer_than_two_answered_rounds_gives_no_gains(self) -> None:
        judger = StopJudger(embed=FakeEmbedder({}))
        assert judger.information_gains(make_draft([("q", "a")])) == []


# ==================================================== 信号③ 规则兜底

class TestRoundCapSignal:
    def test_cap_rounds_force_stop(self) -> None:
        judger = StopJudger(score=lambda m, s: {"completeness": 0.0, "missing": []})
        draft = make_draft([(f"q{i}", f"a{i}") for i in range(MAX_ROUNDS)])
        decision = judger.judge(draft, gains=[0.9])
        assert decision.stop, "12 轮是硬上限，必须停"
        cap = next(s for s in decision.signals if s.name == "round_cap")
        assert cap.suggests_stop

    def test_eleven_rounds_do_not(self) -> None:
        judger = StopJudger(score=lambda m, s: {"completeness": 0.0, "missing": []})
        decision = judger.judge(make_draft([(f"q{i}", f"a{i}") for i in range(MAX_ROUNDS - 1)]), gains=[0.9])
        assert not decision.stop


# ==================================================== 表决与失灵

class TestVote:
    def _judger(self, completeness: float) -> StopJudger:
        return StopJudger(score=lambda m, s: {"completeness": completeness, "missing": []})

    def test_two_votes_stop(self) -> None:
        # ① 高分 + ③ 满轮 → 停
        draft = make_draft([(f"q{i}", f"a{i}") for i in range(MAX_ROUNDS)])
        decision = self._judger(0.9).judge(draft, gains=[1.0])
        assert decision.votes >= 2 and decision.stop

    def test_one_vote_does_not_stop(self) -> None:
        # 只有 ① 高分，② 说还有信息，③ 未满轮 → 继续
        decision = self._judger(0.95).judge(make_draft([("q", "a")]), gains=[0.8])
        assert decision.votes == 1 and not decision.stop

    def test_model_failure_must_not_count_as_stop(self) -> None:
        """
        最危险的失败模式：打分这条路挂了，却因为"挂了"而被当成"可以停了"。
        停是不可逆的（收敛即定稿），所以失灵一律弃权。
        """

        def broken(messages: object, schema: object) -> dict:
            raise RuntimeError("本地模型不可达")

        judger = StopJudger(score=broken)
        decision = judger.judge(make_draft([("q", "a")]), gains=[0.8])
        completeness = next(s for s in decision.signals if s.name == "completeness")
        assert not completeness.suggests_stop
        assert "弃权" in completeness.detail
        assert not decision.stop

    def test_embedding_failure_also_abstains(self) -> None:
        def broken_embed(texts: list[str]) -> np.ndarray:
            raise StopperError("embedding 服务挂了")

        draft = make_draft([("q1", "a1"), ("q2", "a2")])
        draft.rounds[0].spec_digest = "A"
        draft.rounds[1].spec_digest = "A"
        judger = StopJudger(score=lambda m, s: {"completeness": 0.1, "missing": []}, embed=broken_embed)
        decision = judger.judge(draft)
        gain = next(s for s in decision.signals if s.name == "info_gain")
        assert not gain.suggests_stop and "弃权" in gain.detail

    def test_decision_is_serialisable_for_reports(self) -> None:
        judger = StopJudger(score=lambda m, s: {"completeness": 0.9, "missing": []})
        payload = judger.judge(make_draft([("q", "a")]), gains=[0.8]).as_dict()
        assert set(payload) == {
            "stop",
            "votes",
            "forced_by_cap",
            "blocked_by_gaps",
            "reason",
            "signals",
        }
        assert len(payload["signals"]) == 3

    def test_cap_is_a_veto_not_just_a_vote(self) -> None:
        """
        满 12 轮必须**越过表决**直接停。

        路线把这个信号写成"强制停（独立于模型判断，防小模型'永远觉得不够'）"。
        如果它只是一票，那么"模型永远说不够 + 信息增益一直有噪声"就能把循环拖过
        12 轮 —— 而那正是这条规则存在的理由。这里用最不配合的另外两路来验。
        """
        judger = StopJudger(score=lambda m, s: {"completeness": 0.0, "missing": ["还缺"]})
        draft = make_draft([(f"q{i}", f"a{i}") for i in range(MAX_ROUNDS)])
        decision = judger.judge(draft, gains=[0.9])   # 信息增益说"还在变"
        assert decision.votes == 1, "另外两路确实都没建议停"
        assert decision.forced_by_cap is True
        assert decision.stop, "硬上限必须能单独拍板"
        assert "硬上限强制停" in decision.reason


# ==================================================== 定稿

class TestFinalize:
    def _draft(self) -> SpecDraft:
        draft = make_draft([("给谁用？", "给我自己")])
        draft.features = ["整理笔记", "输出周报"]
        draft.non_goals = ["多人协作"]
        draft.acceptance = ["`python -m weekly` 退出码为 0", "系统应该好用"]
        return draft

    def test_finalize_writes_markdown_with_all_sections(self, state_dir: Path) -> None:
        judger = StopJudger(spec_dir=state_dir / "specs")
        path = judger.finalize(self._draft())
        text = path.read_text(encoding="utf-8")

        assert path.name.endswith(".md")
        for heading in ("## 概述", "## 功能清单", "## 非目标", "## 验收条件", "## 追问记录"):
            assert heading in text
        assert "整理笔记" in text and "多人协作" in text

    def test_finalize_flags_conditions_that_are_not_machine_checkable(
        self, state_dir: Path
    ) -> None:
        """借鉴 facts 的规矩：验收条件必须能机器执行，含糊的标出来而不是放过去。"""
        judger = StopJudger(spec_dir=state_dir / "specs")
        text = judger.finalize(self._draft()).read_text(encoding="utf-8")
        assert "⚠️ 不可机器验证" in text
        assert "系统应该好用" in text
        # 明确的命令不该被标
        assert text.count("⚠️ 不可机器验证") == 1

    def test_default_summary_does_not_invent_prose(self, state_dir: Path) -> None:
        """没有汇总器时用模板拼，而不是编一段像模像样的散文 ——
        编造的概述会被下游当成'这就是需求'。"""
        judger = StopJudger(spec_dir=state_dir / "specs")
        text = judger.finalize(self._draft()).read_text(encoding="utf-8")
        assert "本 spec 共" in text

    def test_injected_summarizer_is_used(self, state_dir: Path) -> None:
        judger = StopJudger(spec_dir=state_dir / "specs", summarize=lambda draft: "这是汇总")
        assert "这是汇总" in judger.finalize(self._draft()).read_text(encoding="utf-8")

    def test_finalize_marks_the_draft_converged(self, state_dir: Path) -> None:
        draft = self._draft()
        StopJudger(spec_dir=state_dir / "specs").finalize(draft)
        assert draft.status == "converged"

    def test_finalize_renders_withdrawn_items(self, state_dir: Path) -> None:
        """
        **G6（2026-09-15 第五轮实测）**：撤回过的条目原来**只**出现在
        `render_markdown()` 与给模型的上下文里，官方定稿入口 `finalize()` 一个字都不写。

        代价很实在：施工者看到终稿"没提命令行"，可能又把它加回来 —— 而它正是被人类
        明确否掉过的东西。用例钉住"终稿里必须能看见它、且标明不算承诺"。
        """
        draft = self._draft()
        draft.withdrawn = ["不做：以命令行终端输入表达式作为运行方式"]
        text = StopJudger(spec_dir=state_dir / "specs").finalize(draft).read_text(encoding="utf-8")

        assert "## 已撤回（曾经排除、后来被推翻）" in text
        assert "~~不做：以命令行终端输入表达式作为运行方式~~" in text, "与渲染稿同口径：删除线"
        assert "不再算承诺" in text, "要说清它为什么留着（不是承诺，而是解释最终形态）"
        # 撤回段排在"非目标"之后：它讲的是"曾经要过、后来不要了"，与当前承诺分开
        assert text.index("## 非目标") < text.index("## 已撤回") < text.index("## 验收条件")

    def test_finalize_has_no_withdrawn_section_when_nothing_was_retracted(self, state_dir: Path) -> None:
        """没有撤回时不许出现空标题 —— 空段落会让人以为"这里本来该有东西"。"""
        draft = self._draft()
        assert draft.withdrawn == []
        text = StopJudger(spec_dir=state_dir / "specs").finalize(draft).read_text(encoding="utf-8")
        assert "已撤回" not in text

    def test_finalize_shows_which_rounds_settled_nothing(self, state_dir: Path) -> None:
        """
        第四轮实测报告 G5 建议③：空转必须成为**终稿可见的事实**。

        实测卡 D 第 7 轮「第一版是否只做正算？」答"否" → `adopted=[]`，
        而这件事原来只出现在一次运行日志里；日志一删，就没人知道人类答过那一问。
        """
        draft = self._draft()
        draft.rounds.append(Round(index=2, question="第一版是否只做正算？", answer="否", adopted=[]))
        draft.rounds.append(
            Round(index=3, question="是否还需要支持反算？", answer="是", adopted=["features"])
        )
        text = StopJudger(spec_dir=state_dir / "specs").finalize(draft).read_text(encoding="utf-8")

        assert "| 沉淀 |" in text, "追问记录要有「沉淀」列"
        assert "**（空转）**" in text, "没沉淀的轮次要标出来"
        assert "| features |" in text, "沉淀了的轮次要写清沉淀了什么"

    @pytest.mark.parametrize(
        ("condition", "expected"),
        [
            ("`pytest` 全绿", True),
            ("延迟 < 200ms", True),
            ("返回码等于 0", True),
            ("系统应该好用", False),
            ("代码要优雅", False),
            ("", False),
        ],
    )
    def test_machine_checkable_heuristic(self, condition: str, expected: bool) -> None:
        assert is_machine_checkable(condition) is expected


# ==================================================== 第六轮实测 P0①：空转轮次必须成为缺口

class TestEmptyAdoptionGaps:
    def test_a_round_that_settled_nothing_becomes_a_gap(self) -> None:
        """
        **P0① 的回归**（第六轮受控实验）：`adopted=[]` 原来**不产生任何结构性信号** ——
        只在运行日志里打一行 `[!]`。实测因此丢过一条人类亲口确认的需求
        （卡 `2b461b3f3bf6` 第 4 轮的"命令行入口"），而终稿是 `converged`（完整）、缺口 0 条。

        现在它是一条**缺口**：进 `blocked_by_gaps`、进终稿的"还没定的事"、并让状态落到
        `converged_with_gaps`。措辞是"需要确认是否已被后续轮次覆盖"（代码判不准"覆盖"）。
        """
        from src.spec import empty_adoption_gaps

        draft = make_draft([("q1", "a1")])
        draft.rounds.append(Round(index=2, question="第一版是否会提供命令行入口？", answer="是", adopted=[]))
        gaps = empty_adoption_gaps(draft)
        assert len(gaps) == 1 and "第 2 轮" in gaps[0], gaps
        assert "命令行入口" not in gaps[0] or "答：" in gaps[0], "缺口里要带上那句回答的摘要"
        assert "需要确认" in gaps[0], "措辞必须是「待确认」，不是断言内容丢了"

    def test_answered_round_that_landed_something_is_not_a_gap(self) -> None:
        from src.spec import empty_adoption_gaps

        draft = make_draft([("q1", "a1"), ("q2", "a2")])
        assert empty_adoption_gaps(draft) == []

    def test_unanswered_round_is_not_counted(self) -> None:
        """当前那一轮还没回答时不算空转 —— 那是流程状态，不是需求缺口（D5 同一口径）。"""
        from src.spec import empty_adoption_gaps

        draft = make_draft([("q1", "a1")])
        draft.rounds.append(Round(index=2, question="还没答的问题"))
        assert empty_adoption_gaps(draft) == []

    def test_empty_adoption_flips_the_finalize_status(self, state_dir: Path) -> None:
        """最重要的那一条：**少了人类点头确认的东西，就不许显示"完整"**。"""
        draft = self._draft_with_empty_round()
        text = StopJudger(spec_dir=state_dir / "specs").finalize(draft).read_text(encoding="utf-8")
        assert draft.status == "converged_with_gaps", draft.status
        assert "还没定的事" in text
        assert "第 2 轮的回答没有沉淀任何字段" in text

    def test_a_spec_with_no_empty_round_can_still_be_converged(self, state_dir: Path) -> None:
        """反面：不许把"完整"变成永远拿不到的状态（那样这个状态就没人看了）。"""
        draft = self._draft_with_empty_round()
        draft.rounds[-1].adopted = ["features"]
        StopJudger(spec_dir=state_dir / "specs").finalize(draft)
        assert draft.status == "converged", draft.status

    @staticmethod
    def _draft_with_empty_round() -> SpecDraft:
        draft = make_draft([("q1", "a1")])
        draft.acceptance = ["`pytest -q` 退出码为 0"]
        draft.rounds.append(Round(index=2, question="第一版是否会提供命令行入口？", answer="是", adopted=[]))
        return draft


# ==================================================== spec_text

class TestSpecText:
    def test_includes_all_three_lists(self) -> None:
        draft = new_draft("想法")
        draft.features = ["F"]
        draft.non_goals = ["N"]
        draft.acceptance = ["A"]
        text = spec_text(draft)
        assert "想法" in text and "F" in text and "N" in text and "A" in text

    def test_empty_lists_do_not_add_empty_labels(self) -> None:
        assert spec_text(new_draft("只有想法")) == "想法：只有想法"
