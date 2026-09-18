"""M1 Idea 细化器（路线 2.1）。

## 它解决什么问题

用户给一句话，直接变成 spec 是灾难：需求里的空白会被"聪明地"补全，而补全的方向
往往不是用户想要的，且没人会发现。追问循环的作用是**在写代码之前**把空白问出来。

## 四条硬约束（都是从"人和模型会怎么偷懒"倒推出来的）

1. **一轮只问一个问题。** 复合问题会让人只答一半，而系统会把整段回答都当成答案，
   剩下那一半就凭空消失了。所以问题句里出现「且/和/以及/或/；/;/&」就重生成。
2. **问题必须能用「是」或「否」回答。** 这是路线 0.5.B① 的原话（"每轮追问回答 是/否"）。
   2026-09-12 的实测报告指出这条**没有被代码守住**：16 轮里出现了
   「哪一类指标是必须优先监控的？」这种开放问题 —— 它逼着人写一段话，而"是/否"
   带来的两个好处同时丢掉：**答案可机器判定**（离线也能沉淀）、**问题被迫变小**
   （开放问题往往一次塞进三件事）。所以现在有 `find_open_question_marker` 代码兜底。
3. **轮数硬上限。** "再问一个"永远有理由。上限必须写死，不由模型的判断决定。
   （路线写 12，人类要求改成 24 —— 见 `MAX_ROUNDS` 处的说明。）
4. **每轮落盘。** 对话会被压缩或截断，spec 的进度必须在盘上，不在上下文里。

## 与路线的一处偏差（记录了，没有藏）

路线写"每轮 flash 输出 schema"。本机环境里 `DEEPSEEK_API_KEY` 曾不在环境变量里
（DSH 把密钥存在自己的凭证库里，不导出给子进程），所以生成器做成**可注入**的，
生产用 `gateway.chat(..., tier="flash_api")`；验收用本地小模型真跑。
2026-09-12 实测：flash 档实际可用（`deepseek-flash`），"占位 id 会 404"的担心已不成立。

## 2026-09-12 修掉的缺陷（都来自那份 M1 实测报告）

- **D1 快照口径**：`spec_digest` 原来在本轮 `draft_updates` 合并**之前**取，
  于是 2.2 的 `info_gain` 系统性低估本轮变化，把人往"过早收敛"上推。
  现在 `answer()` 先合并再取快照（`updates=` 传入或由 `extract` 抽）。
- **D2 本地档空转**：小模型抽取常常返回空 → 回答没沉淀。现在
  ①一轮没沉淀任何字段会记进 `warnings`（可见）；
  ②提供 `template_answers`：**因为问题是是/否，答案本身就能直接落成字段**（离线降级）。
- **D2b 重复提问**：提示词里写了"不要重复"，但没有代码兜底 → 实测连着两轮问同一句。
  现在 `find_duplicate_question` 命中就要求换角度重问，重试用尽则报错停下。
- **D3 语义近重**：`merge()` 原来只按精确串去重，换措辞的重述能穿过去。
  现在可以注入 `embed_fn` 做语义去重（阈值与 `config/models.yaml` 的 `dedup.cluster` 对齐）。
- **D4 只增不删**：被后续轮次推翻的条目原来只能"并存"。现在 `draft_updates.retracts`
  可以把它们标成 `withdrawn` —— **保留记录，但不再算作承诺**。

## 2026-09-13 修掉的缺陷（G2/G3/G4/G5，都来自第 3~4 轮实测报告）

- **G2 诊断不反馈提问**：`completeness.missing` 连续多轮列出同样的缺口却从没变成问题。
  现在 `IdeaRefiner.open_gaps` + `note_gaps()` 把它接进 `build_messages`。
- **G3 非目标膨胀**：`EXTRACT_PROMPT` 明确禁止把"另一个选项"机械否定进 `non_goals`。
- **G4 丢弃未选中选项**：`Proposal.options` / `Round.options` +
  `build_extract_messages(options=...)` 让"两者都要"能分别落条。
- **G5 限制性问句答「否」时信息零落地**：`Proposal.no_means` / `Round.no_means` 要求
  提问时就声明"否"分支候选；`IdeaRefiner._carry_restrictive_no()` **确定性地**把
  "还要做别的"接进 `carry_over`，下一问必被追问；抽取提示词要求把扩展写成 `features`；
  终稿与渲染稿都会标出"这一轮没有沉淀任何字段"的轮次。
- **第三支答案「两者都要」**（2026-09-13 人类要求）：`parse_yes_no` 不再把它读成「是」
  （那会丢掉"两者"里的第二支），改由 `both_targets()` 落到本轮声明的候选项上；
  **只有候选项 ≥2 且互不矛盾时才落**（`options_conflict()`），否则什么都不落、
  告警并写进 `carry_over` 逼下一问问清。提问侧要求候选项是"可以并存的两个具体做法"，
  渲染稿与终稿都把候选项呈现给人 —— 不呈现，人类根本没法回答「两者都要」。
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, ValidationError

ROOT = Path(__file__).resolve().parents[2]
STATE_DIR = ROOT / "state"
SPECS_DIR = STATE_DIR / "specs"

# 追问轮数的硬上限。
# 路线原文写 12；**人类在 2026-09-12 明确要求改成 24**（"12 轮确实少了点"）。
# 这是人对路线的覆盖，不是我自己松的口子，所以记在这里而不是悄悄改。
MAX_ROUNDS = 24
# 第 MAX_ROUNDS-2 轮起提示"即将收敛，剩余 ≤2 问"
CONVERGE_FROM_ROUND = MAX_ROUNDS - 2

# 复合问题的标记。路线原文列的就是这几个。
COMPOUND_MARKERS = ("且", "以及", "或", "；", ";", "&", "和")
# 顺序有讲究：先长后短，否则「以及」会被「和」抢先匹配到，报出来的标记就误导人。
_ORDERED_MARKERS = tuple(sorted(COMPOUND_MARKERS, key=len, reverse=True))

#: 「这问题没法用是/否回答」的标记词。
#:
#: 两类：**疑问词**（哪些/什么/如何/为什么/几个…）与**索取式动词**（列出/说明/描述/举例…）。
#: 命中任何一个都说明它要的是一段话，不是一个是非判断。
OPEN_MARKERS = (
    "哪些", "哪一个", "哪一类", "哪几", "哪种", "什么", "怎么", "如何", "为什么", "为何",
    "何时", "什么时候", "多久", "几个", "多少", "谁", "哪里", "在哪", "多大",
    "列出", "罗列", "列举", "举例", "说明", "描述", "解释", "介绍", "写一段", "给出",
)
_ORDERED_OPEN = tuple(sorted(OPEN_MARKERS, key=len, reverse=True))

#: 两个问题句的字面相似度超过它就当成"同一个问题"。0.85 是实测取的：
#: 换措辞的复述（"哪一类指标必须优先监控" vs "优先监控哪些指标"）能到 0.6~0.8，
#: 而同一个问题的原样重问接近 1.0；0.85 只抓"几乎一样"这种最没价值的重复，
#: 不误伤"换角度"的追问（那是提示词明确鼓励的）。
DUPLICATE_RATIO = 0.85

#: 语义去重的默认阈值。与 `config/models.yaml` 的 `dedup.cluster: 0.92` 对齐 ——
#: 同一个系统里"两条内容算不算同一条"应该只有一个口径，不要各处各定一个。
DEDUPE_THRESHOLD = 0.92

SPEC_STATUSES = ("asking", "converged", "converged_with_gaps", "aborted")


class SpecError(RuntimeError):
    """spec 循环自身的错误。绝不吞掉。"""


class CompoundQuestion(SpecError):
    """重生成多次之后问题仍然是复合的。宁可不问，也不问一个会丢答案的问题。"""


class OpenQuestion(SpecError):
    """重生成多次之后问题仍然不是是/否问题。宁可不问，也不问一个逼人写作文的问题。"""


class RepeatedQuestion(SpecError):
    """重生成多次之后问题仍然和之前问过的几乎一样。原样重问只会得到同样的答案。"""


# ------------------------------------------------------------------ 数据模型

class Round(BaseModel):
    index: int
    question: str
    why: str = ""
    answer: str | None = None
    # 这一轮回答之后，累积 spec 文本的快照。
    # 2.2 的信息增益信号要比较"相邻两轮的 spec 变了多少"，
    # 而累积文本只保留了最终状态 —— 不留快照就只能事后重放，代价更大。
    #
    # **快照必须在"本轮的 draft_updates 合并之后"取**（D1）。取早了会让信息增益
    # 系统性低估本轮变化 —— 那不会报错，只会悄悄把人往"过早收敛"上推。
    spec_digest: str = ""
    #: 本轮实际沉淀下来的字段（`features`/`non_goals`/`acceptance`/`idea`）。
    #: 空列表 = **这一轮什么也没沉淀**（本地小模型档常见）—— 让"空转"可见。
    adopted: list[str] = field(default_factory=list)
    #: 这一轮问句里给出的**候选项**（"是 A 还是 B"）。见 `Proposal.options`。
    options: list[str] = field(default_factory=list)
    #: 若这一轮是**限制性问句**（"是否只做 X"），回答"否"意味着"还要做别的"。
    #: 这里放的就是"别的"的候选项（`Proposal.no_means`）。空 = 提问时没有声明
    #: 否分支，那么"否"只能落成一条非目标，扩展部分要靠下一轮追问补（G5）。
    no_means: list[str] = field(default_factory=list)

    @property
    def answered(self) -> bool:
        return bool(self.answer and self.answer.strip())


class Proposal(BaseModel):
    """
    每轮模型必须返回的结构（路线 2.1 子步骤 2）。

    `draft_updates` 刻意是宽松的 dict：它只包含本轮**新增**的信息，
    字段缺省表示"这一轮没有新东西"，而不是"清空"。
    它还可以带 `retracts`：本轮**明确推翻**的既有条目（D4）。
    """

    question: str
    why: str = ""
    draft_updates: dict[str, Any] = Field(default_factory=dict)
    #: **候选项**（2026-09-13 第三轮实测报告 G4）。
    #:
    #: 是/否问句常常是"是 A 而不是 B"。人类答"两者都要"时，命题压缩（`proposition_of`）
    #: 只能承载 A，**B 就静默丢了** —— 而用户明确说了两个都要。把候选项显式列出来，
    #: 抽取阶段才有位置把 A、B **分别**落成条目。
    options: list[str] = Field(default_factory=list)
    #: **"否"分支的候选项**（2026-09-13 第四轮实测报告 G5）。
    #:
    #: 限制性问句「第一版是否**只**做正算？」被回答"否"时，人类说的是"范围更宽"
    #: —— 而"更宽"具体宽在哪里，命题压缩里没有承载位置，那句"否"就白说了。
    #: 要求生成时就声明候选项，下一轮才能**直接追问**，而不是让模型自由发挥。
    no_means: list[str] = Field(default_factory=list)


class Extraction(BaseModel):
    """一次人类回答能沉淀出的 spec 字段（`extract` 钩子的返回结构）。"""

    features: list[str] = Field(default_factory=list)
    non_goals: list[str] = Field(default_factory=list)
    acceptance: list[str] = Field(default_factory=list)
    idea: str | None = None


class SpecDraft(BaseModel):
    id: str
    idea: str
    rounds: list[Round] = Field(default_factory=list)
    features: list[str] = Field(default_factory=list)
    non_goals: list[str] = Field(default_factory=list)
    acceptance: list[str] = Field(default_factory=list)
    #: 被后续轮次推翻/摘出范围的条目（D4）。**保留记录，但不再算作承诺** ——
    #: 直接从列表里删掉等于抹掉"人类曾经要过这个"的事实，那比留着更危险。
    withdrawn: list[str] = Field(default_factory=list)
    status: str = "asking"
    created_at: str = ""
    updated_at: str = ""

    # ---------------------------------------------------------------- 合并

    def merge(
        self,
        updates: dict[str, Any],
        *,
        embed_fn: Callable[[list[str]], Any] | None = None,
        dedupe_threshold: float = DEDUPE_THRESHOLD,
    ) -> list[str]:
        """
        把本轮的 `draft_updates` 合并进来。返回被采纳的字段名，便于写进日志。

        合并规则：
        1. **列表只增不删**（保序去重）—— spec 的进度是累积的；让后一轮"覆盖"前一轮的结论，
           等于允许模型悄悄撤回已经和人类确认过的内容。
        2. **`retracts` 是唯一的例外，而且它不是删除**：命中的条目从"承诺列表"移到
           `withdrawn`，记录还在（D4：人类自己在后面几轮缩小范围时，旧条目必须留痕）。
        3. 传了 `embed_fn` 就做**语义去重**（D3）：换措辞的重述不该被当成新需求。
           **默认不做**：那要调本地 embedding，而 `merge` 是纯函数这一层不该悄悄依赖模型；
           由调用方显式开启（`IdeaRefiner(embed_fn=...)`）。
        """
        adopted: list[str] = []

        for key in ("features", "non_goals", "acceptance"):
            incoming = updates.get(key)
            if not isinstance(incoming, list):
                continue
            current = list(getattr(self, key))
            incoming_text = [str(item).strip() for item in incoming if str(item).strip()]
            if not incoming_text:
                continue
            fresh: list[str] = []
            for text in incoming_text:
                if text in current or text in fresh:
                    continue
                fresh.append(text)
            if embed_fn is not None and fresh:
                fresh = self._drop_semantic_duplicates(
                    fresh, current, embed_fn=embed_fn, threshold=dedupe_threshold
                )
            if fresh:
                setattr(self, key, current + fresh)
                adopted.append(key)

        retracts = updates.get("retracts")
        if isinstance(retracts, list):
            dropped = 0
            for item in retracts:
                text = str(item).strip()
                if not text:
                    continue
                for key in ("features", "non_goals", "acceptance"):
                    current = list(getattr(self, key))
                    if text in current:
                        current.remove(text)
                        setattr(self, key, current)
                        if text not in self.withdrawn:
                            self.withdrawn.append(text)
                        dropped += 1
            if dropped:
                adopted.append("withdrawn")

        refined = updates.get("idea")
        if isinstance(refined, str) and refined.strip():
            self.idea = refined.strip()
            adopted.append("idea")

        return adopted

    @staticmethod
    def _drop_semantic_duplicates(
        fresh: list[str],
        existing: list[str],
        *,
        embed_fn: Callable[[list[str]], Any],
        threshold: float,
    ) -> list[str]:
        """
        丢掉"和已有条目语义上几乎是同一条"的新条目。

        用**余弦**（与 2.2 的查重同一口径）。`embed_fn` 抛错时**不吞**：
        宁可让人看见"去重没做成"，也不要静默地把重述放进 spec —— 后者会永久留在卡里。
        """
        import numpy as np

        pool = [item for item in [*existing, *fresh] if item.strip()]
        if not pool:
            return fresh
        vectors = np.asarray(embed_fn(pool), dtype=float)
        if vectors.ndim != 2 or vectors.shape[0] != len(pool):
            raise SpecError(f"embedding 形状异常：{vectors.shape}，期望 ({len(pool)}, dim)")
        norms = np.linalg.norm(vectors, axis=1, keepdims=True)
        if np.any(norms == 0):
            raise SpecError("embedding 出现零向量，无法比较（这会让语义去重失效）")
        unit = vectors / norms

        kept: list[str] = []
        # 已有条目之间不比较（它们早就被人类确认过）；只拿每条候选跟"已有 + 已保留"比。
        known = list(unit[: len(existing)])
        for offset, candidate in enumerate(unit[len(existing):]):
            similarities = [float(np.dot(candidate, other)) for other in known]
            if similarities and max(similarities) >= threshold:
                continue
            kept.append(fresh[offset])
            known.append(candidate)
        return kept

    # ---------------------------------------------------------------- 观测

    @property
    def answered_rounds(self) -> int:
        return sum(1 for item in self.rounds if item.answered)

    @property
    def is_full(self) -> bool:
        return self.answered_rounds >= MAX_ROUNDS

    @property
    def live_items(self) -> list[str]:
        """还没被撤回的全部条目（features + non_goals + acceptance）。"""
        return [*self.features, *self.non_goals, *self.acceptance]


# ------------------------------------------------------------------ 问题质量约束

def find_compound_marker(question: str) -> str | None:
    """
    问题句里是否出现复合标记；命中就返回那个标记，否则 None。

    为什么用这种"笨"办法而不是让模型自己判断：模型判断"这算一个问题还是两个"
    非常不稳定，而这条约束的价值恰恰在于**每次都一样**。宁可误报（多问一轮），
    不可漏报（人类答了一半，另一半静默丢失）。
    """
    for marker in _ORDERED_MARKERS:
        if marker in question:
            return marker
    return None


def find_open_question_marker(question: str) -> str | None:
    """
    问题句里是否出现"开放提问"的标记；命中就返回那个词，否则 None。

    路线 0.5.B① 规定每轮追问人类用**是/否**回答。做不到这一点的问句有两类：
    **疑问词**（哪些/什么/如何/为什么/几个…）与**索取式动词**（列出/说明/举例…），
    它们要的是一段话。命中了就重生成 —— 和复合问题同样的道理：
    发出去就换不回一个可判定的答案了。
    """
    for marker in _ORDERED_OPEN:
        if marker in question:
            return marker
    return None


def is_yes_no_question(question: str) -> bool:
    """这句话能不能用「是／否」回答。**只看有没有开放标记**（宁可放过，不可误杀）。"""
    return find_open_question_marker(question) is None


def question_similarity(left: str, right: str) -> float:
    """
    两句话的字面相似度（0~1）：按**字符二元组**的 Jaccard 系数。

    为什么不用 `difflib`：它对中文长句的分数偏高（共同标点/虚词就够 0.6+），
    而这里要的是"几乎一模一样"这种判断。二元组 Jaccard 对措辞变化更敏感，
    也不需要任何外部依赖。
    """
    def bigrams(text: str) -> set[str]:
        cleaned = "".join(ch for ch in text if ch.strip())
        return {cleaned[index:index + 2] for index in range(max(0, len(cleaned) - 1))}

    left_set, right_set = bigrams(left), bigrams(right)
    if not left_set or not right_set:
        return 0.0
    return len(left_set & right_set) / len(left_set | right_set)


def find_duplicate_question(
    question: str,
    history: list[str],
    *,
    threshold: float = DUPLICATE_RATIO,
) -> tuple[str, float] | None:
    """和历史问题里最像的那一条比；超过阈值就返回 `(那条历史问题, 相似度)`。"""
    best: tuple[str, float] | None = None
    for previous in history:
        score = question_similarity(question, previous)
        if score >= threshold and (best is None or score > best[1]):
            best = (previous, score)
    return best


# ------------------------------------------------------------------ 限制性问句

#: **限制性问句**的标记（2026-09-13 第四轮实测报告 G5）。
#:
#: 「第一版是否**只**做 X？」回答「否」的语义是"不只做 X ⇒ **还要做别的**"，
#: 而"别的"在命题压缩（`proposition_of`）里没有承载位置：`as_non_goal()` 只会留下
#: 「不做：只做 X」。人类那句"否"承载的**扩展**因此静默丢掉 —— 实测里白耗一轮
#: （语义变化 0.0000、完整度 0.85→0.50），且关键需求险些永久丢失。
RESTRICTIVE_MARKERS = ("只", "仅", "单纯", "限于", "局限于")
#: 下面这些词里的「只/仅」是**否定**用法（"不仅…"），不是收窄，先摘掉再找标记。
#: 顺序由长到短，否则「不仅仅」会先被「不仅」吃掉、剩下一个「仅」造成误报。
_NON_RESTRICTIVE_WORDS = tuple(
    sorted(("不仅仅", "不仅", "不单单", "不单", "不只"), key=len, reverse=True)
)


def find_restrictive_marker(question: str) -> str | None:
    """
    问句是不是**限制性**问句（"是否只做 X"）；命中就返回那个标记词，否则 None。

    为什么要专门认这一类：同一个「否」在两种问句里读法不同 ——
    限制性问句的「否」= "范围更宽，还得做别的"，普通问句的「否」= "排除 X"。
    两种读法的下一步完全不同（追问 vs 记一条非目标），认错就丢需求，
    所以用代码认，不交给模型临场判断。
    """
    text = question
    for word in _NON_RESTRICTIVE_WORDS:
        text = text.replace(word, "")
    for marker in RESTRICTIVE_MARKERS:
        if marker in text:
            return marker
    return None


# ------------------------------------------------------------------ 是/否答案的直接落地

YES_WORDS = ("是", "对", "是的", "需要", "要", "有", "可以", "同意", "确定", "应该", "ok", "yes", "y")
NO_WORDS = ("否", "不", "不是", "不需要", "不要", "没有", "不行", "不同意", "不用", "no", "n")

_QUESTION_PREFIXES = ("是否", "需不需要", "要不要", "能不能", "可不可以", "有没有", "会不会", "是不是")


def as_non_goal(proposition: str) -> str:
    """
    把一个肯定式命题写成**显式否定**的非目标条目。

    为什么不靠"给中文句子加否定词"（F2 的修法②）：中文取反没有机械规则 ——
    「需要」→「不需要」还算简单，可「只做 CLI」取反是「不只做 CLI」还是「不做 CLI」？
    「第一版只做 CLI」取反后到底是"第一版还做别的"还是"第一版不做 CLI"？**两种读法都通**。
    既然靠语言取反必然有歧义，就换一种**没有歧义**的写法：统一前缀「不做：」。
    这样 `non_goals` 里每条都能一眼读出"这是被明确排除的东西"，也不需要模型去猜。
    """
    text = proposition.strip()
    if text.startswith(("不做", "不采用", "不支持", "不使用", "不依赖")):
        return text                       # 已经是"不做"的口径，别叠成「不做：不采用 X」
    return f"不做：{text}" if text else text


#: 双重否定的标记。命中了要**告警**（F3）：模型把「是否只靠 X」的"否"写成
#: 「不只靠 X」，读起来像"还是靠一点"，而人类的意思是"不采用这条路"。
DOUBLE_NEGATION_MARKERS = ("不只", "不单单", "并非不", "不是不", "不无", "不排除")


def find_double_negation(text: str) -> str | None:
    """文本里有没有双重否定标记（命中就返回那个词）。"""
    for marker in DOUBLE_NEGATION_MARKERS:
        if marker in text:
            return marker
    return None


def parse_yes_no(answer: str) -> bool | None:
    """
    把一句人类回答判成 是 / 否 / 说不清。

    先看开头的词（"是的，但……"算 是），再看整句有没有否定词。
    判不出来就返回 None —— **绝不猜**：猜错会把没确认的东西写进 spec。

    **「两者都要」不是是/否**，这里返回 None：它必须由 `both_targets()` 落到
    本轮声明的候选项上。原来它会被判成「是」（"要"在 `YES_WORDS` 里），
    于是"两者"里的第二支被静默丢掉 —— 正是 2026-09-13 补这一支要修的东西。
    """
    text = (answer or "").strip().lower()
    if not text:
        return None
    if is_both_answer(answer):
        return None
    head = text[:4]
    if any(head.startswith(word) for word in NO_WORDS):
        return False
    if any(head.startswith(word) for word in YES_WORDS):
        return True
    if any(word in text for word in NO_WORDS):
        return False
    if any(word in text for word in YES_WORDS):
        return True
    return None


# ------------------------------------------------------------------ 第三支答案：两者都要

#: 「两者都要」的回答形态（人类 2026-09-13 要求加入的第三支）。
#:
#: 为什么必须有它：`options` 非空的问题（"是 A 而不是 B"）里，人类的"两者都要"
#: 是**信息量最大**的那一句；而在这支加入之前，它被读成「是」，只落下一个压缩后的命题，
#: 另一支静默丢掉（第三轮实测报告 G4 记的就是这个损失，当时只能靠 flash 抽取补，
#: 离线档补不了）。
#:
#: 认法**保守**：只有整句就是这么说的才算（"两者都要""都做""both"…），
#: 不做"句子里带『都』就算"这种宽匹配 —— 误判成"两者都要"会把不存在的第二条需求写进 spec，
#: 而 spec 里的假条目比缺条目更难发现。
BOTH_ANSWER_MARKERS = (
    "两者都是", "两者都要", "两个都是", "两个都要", "全都做", "都要做", "都做", "都要", "both",
)
#: 「两者都不要」「两个都不做」这类**整体否定**的句式：`都` 紧跟着 `不/没/勿`。
#:
#: 为什么用句式而不是"含『不要』就否掉"：人类很可能说「两者都要，**不要**二选一」——
#: 那句"不要"否的是**二选一**，不是"两者"本身。宽匹配会把"两个都要"读成"两个都不要"，
#: **意思正好相反**，比认不出来糟得多（认不出来只是回到"说不清"，交给抽取/人工）。
BOTH_NEGATED_RE = re.compile(r"(两者|两个|全都|都)\s*(不|没|勿)")


def is_both_answer(answer: str) -> bool:
    """
    人类这一轮是不是回答「两者都要」。

    **必须在 `parse_yes_no` 之前判**（那一支里也做了这个转发）：被它读成「是」的话，
    "两者"里的第二支会被静默丢掉。

    口气由**最先出现的那一段**决定（与 `parse_yes_no` 先看句首同一思路）：
    「两者都要，不要二选一」→ 肯定在前，就是"两者都要"；
    「两者都不要」→ 只有否定句式，不是。
    """
    text = (answer or "").strip().lower()
    if not text:
        return False
    hits = [text.find(marker) for marker in BOTH_ANSWER_MARKERS]
    hits = [index for index in hits if index >= 0]
    if not hits:
        return False
    negated = BOTH_NEGATED_RE.search(text)
    if negated is None:
        return True
    return min(hits) < negated.start()


#: `non_goals` 用的显式否定前缀（与 `as_non_goal` 同一套口径）。
_NEGATIVE_PREFIXES = ("不做：", "不做", "不采用", "不支持", "不使用", "不依赖")


def _strip_negative_prefix(option: str) -> str:
    """把「不做：X」剥成「X」，好和另一条「X」比出"正反两面"。"""
    text = option.strip()
    for prefix in _NEGATIVE_PREFIXES:
        if text.startswith(prefix):
            return text[len(prefix):].lstrip("：: ").strip()
    return text


def options_conflict(options: Sequence[str]) -> str | None:
    """
    本轮声明的候选项里有没有**说得清的**自相矛盾；有就返回理由，否则 None。

    只说"说得清的"两种：① 同一条出现两次（那是一个分支，不是两个）；
    ② 一条正好是另一条的显式否定（`X` vs `不做：X`）。
    更细的语义矛盾（"只做正算" vs "只做反算"）要模型才判得准，
    **代码不假装能判** —— 假装判的后果是把人类合理的"两个都要"挡在门外，
    而挡住一句真话比放过一句含糊更糟（含糊至少会被下一轮问到）。
    """
    cleaned = [str(item).strip() for item in options if str(item).strip()]
    if len(cleaned) < 2:
        return None
    if len(cleaned) != len(set(cleaned)):
        return "候选项里有重复的条目（那是一个分支，不是两个）"
    seen: dict[str, str] = {}
    for item in cleaned:
        core = _strip_negative_prefix(item)
        if core in seen and seen[core] != item:
            return f"候选项互相矛盾：「{seen[core]}」与「{item}」是同一件事的正反两面"
        seen[core] = item
    return None


def both_targets(answer: str, options: Sequence[str] = ()) -> tuple[list[str], str | None]:
    """
    回答「两者都要」时，本轮**该落成什么**、以及落不下去时的理由。

    返回 `(要落成 features 的条目, 拒绝的理由)`；两者互斥 —— 理由非空时列表必为空。

    这一支要成立有三个前提（缺一个就**不许猜**，这是 `parse_yes_no` 的"绝不猜"同一条规矩）：

    1. 回答确实是「两者都要」；
    2. 本轮问题**声明了 ≥2 个候选项** —— 没声明就没有"两者"可言，
       猜是哪两者等于替人类编需求；
    3. 候选项之间**不互相矛盾**（`options_conflict`）—— 一条是另一条的正反两面时，
       "两个都要"没有任何意义，落下去只会得到一份自相矛盾的 spec。

    声明了 3 个候选项时，"都要"= 三个都落（「两者」按"本轮声明的全部分支"理解）。
    任何一条不成立都返回空列表 + 理由，由调用方**告警并追问**，绝不静默落盘。
    """
    if not is_both_answer(answer):
        return [], None
    cleaned = [str(item).strip() for item in options if str(item).strip()]
    if len(cleaned) < 2:
        return [], (
            "人类回答「两者都要」，但这一轮的问题没有声明候选项（`options`）—— "
            "「两者」指哪两者无从得知，不猜"
        )
    conflict = options_conflict(cleaned)
    if conflict is not None:
        return [], f"人类回答「两者都要」，但{conflict}"
    return cleaned, None


_PROPOSITION_REWRITES = (
    ("需不需要", "需要"),
    ("要不要", "要"),
    ("能不能", "能"),
    ("可不可以", "可以"),
    ("会不会", "会"),
    ("是不是", "是"),
    ("有没有", "有"),
    ("是否", ""),
)


def proposition_of(question: str) -> str:
    """
    把一个是/否问句压成一句陈述（用于落进 features / non_goals）。

    要处理的三种写法（中文里就这三种最常见）：
      * **前缀式**：「是否需要多人协作」→「需要多人协作」（把疑问短语换回陈述）；
      * **中缀式**：「第一版是否只做 CLI」→「第一版只做 CLI」（`是否` 出现在句中）；
      * **句尾式**：「需要多人协作吗」→「需要多人协作」（去掉「吗/呢/吧」）。
    换不干净的后果很实在：spec 里会留下「…是否…」这种半截疑问句，
    施工者读起来像还没定。
    """
    text = question.strip().rstrip("？?。.!！ ").strip()
    if text.endswith(("吗", "呢", "吧")):
        text = text[:-1].strip()
    for prefix in _QUESTION_PREFIXES:
        if text.startswith(prefix):
            text = text[len(prefix):].strip()
            break
    for source, target in _PROPOSITION_REWRITES:
        if source in text:
            text = text.replace(source, target, 1)
            break
    return text.strip() or question.strip()


#: 「反向提问」的句式：`是否不需要 X`／`要不要不…` —— 答案是**反的**。
#: 中文是非问句里这是固有坑：问「是否不需要 X」而答「否」，意思是"需要 X"。
#: 不能指望模型每轮都问肯定式问题，所以代码里认这一种句式并翻转判定。
NEGATED_QUESTION_RE = re.compile(
    r"(是否|是不是|要不要|需不需要|能不能|可不可以|会不会|有没有)\s*(不|没|无需|不用|不会|不能)"
)


def is_negated_question(question: str) -> bool:
    """问句是不是"反向提问"（问的是"是不是**不**做 X"）。"""
    return NEGATED_QUESTION_RE.search(question) is not None


def apply_yes_no_answer(
    draft: SpecDraft,
    question: str,
    answer: str,
    *,
    options: Sequence[str] = (),
    embed_fn: Callable[[list[str]], Any] | None = None,
    dedupe_threshold: float = DEDUPE_THRESHOLD,
) -> list[str]:
    """
    离线降级：**因为没有模型抽取就丢掉人类的回答**是不可接受的，而问题已经是是/否了，
    所以答案本身就能直接落成字段（D2 的修法③）：

    - 「是」→ 这条命题进 `features`；
    - 「否」→ 进 `non_goals`，**而且必须写成显式否定**（`不做：X`）——
      2026-09-12 第二轮实测的 F2：原来直接把**肯定式命题**塞进 `non_goals`，
      于是"是否需要多人协作？→ 否"被记成"需要多人协作"，**意思正好相反**。
      `features` 与 `non_goals` 是两份相反的承诺，中间那一步取反不能省。
    - **「两者都要」→ 候选项分别进 `features`**（2026-09-13 人类要求加的第三支）：
      落盘的前提由 `both_targets()` 把关 —— 本轮必须声明了 ≥2 个**互不矛盾**的候选项，
      否则什么都不做（留给调用方告警 + 追问），绝不猜"两者"是哪两者。
    - 说不清 → 什么都不做（交给模型抽取或人工）。

    **反向提问**（`是否不需要 X`）要先翻转一次，见 `is_negated_question`。

    `embed_fn` 传进来的话，兜底落的条目也走**语义去重**（D3）—— 这一支现在是**默认开启**的，
    所以"问题措辞与草稿里已有条目很像"时，靠它挡住换措辞的近重复（否则 spec 会膨胀）。

    返回被采纳的字段名（与 `merge` 同一口径），便于记进 `Round.adopted`。
    """
    merge_kwargs = {"embed_fn": embed_fn, "dedupe_threshold": dedupe_threshold}
    targets, _problem = both_targets(answer, options)
    if targets:
        return draft.merge({"features": targets}, **merge_kwargs)
    verdict = parse_yes_no(answer)
    if verdict is None:
        return []
    proposition = proposition_of(question)
    if not proposition:
        return []
    if is_negated_question(question):
        verdict = not verdict              # 「是否不需要 X？」回答「否」= 需要 X
    if verdict:
        return draft.merge({"features": [proposition]}, **merge_kwargs)
    return draft.merge({"non_goals": [as_non_goal(proposition)]}, **merge_kwargs)


# ------------------------------------------------------------------ 提示词

SYSTEM_PROMPT = """你是一个需求澄清助手。你在帮人类把一句粗糙的想法，变成可以动工的 spec。

规则（每一条都有代价，请当真）：

1. **一轮只问一个问题。** 一句话里塞两个问号，人类通常只会答一个，
   剩下那个就静默丢失了 —— 比你少问一次糟糕得多。
   问题句里不要出现「且」「和」「以及」「或」「；」「;」「&」。
2. **问题必须能用「是」或「否」回答。** 人类对每一轮的回答就是「是」或「否」，
   这是硬规矩。所以不要问「哪些」「什么」「如何」「为什么」「几个」这类开放问题，
   也不要说「列出」「说明」「举例」；把你想知道的转成一个是非判断，例如：
   - 不要问「第一版优先监控哪些指标？」→ 改问「第一版是否只监控『任务是否卡住』这一类指标？」
   - 不要问「断线的判据是什么？」→ 改问「断线的判据是否就是『心跳超过 30 秒没有上报』？」
   - 不要问「为什么需要这个系统？」→ 改问「这个系统的第一目标是否是替代人工巡检？」
   如果一次判断装不下你想知道的，就问那个**最关键**的是非判断，剩下的下轮再问。
3. **问题要能砍掉一半可能性。** 如果你的问题无论对方怎么答，你都得再问一遍，
   那它就不值得问。先问最能把方案空间劈成两半的那个。
4. **不要问能自己查的。** 能从已有信息推断的，不要占用人类的时间。
5. **不要重复已经问过的问题。** 上面已经列出了问过和答过的内容；
   如果一个问题问过了但答案还不够，请换一个**角度**再问，并在 `why` 里说明
   为什么必须换个角度 —— 原样再问一遍只会得到同样的答案，白费一轮。
   （这一条有代码兜底：问题和你问过的太像会被打回重问。）
6. **只输出一个 JSON 对象**，结构是：
   {"question": "<问题原文>", "why": "<为什么问这个>", "draft_updates": {...}}
   `draft_updates` 里只放**这一轮新确认的**内容，可以是
   {"features": [...], "non_goals": [...], "acceptance": [...], "idea": "...", "retracts": [...]} 的子集；
   这一轮没有新东西就留空对象。
   **不要换措辞重写已有内容**：重复条目会被语义去重挡掉，浪费的是你自己的轮次。
   * `retracts` 只用于**人类在本轮明确推翻/摘出范围**的旧条目（原文照抄），
   把那条从"承诺"里移到"已撤回"，记录仍然保留。
7. **`options` 字段**：如果这个问题是"是 A 而不是 B"这种二选一/多选一，
   把候选项列在这里（例如 `["骰点判定", "角色数值录入与派生计算"]`）。
   人类除了「是」「否」，还可以回答**「两者都要」** —— 系统会把候选项**分别**落成条目
   （这一步由代码兜底，不靠你记得写）。所以候选项必须是**可以并存的两个具体做法**，
   **不要**写成 `X` / `不做 X` 这种正反两面：那种"两个都要"没有任何意义，
   系统会拒绝并要求下一轮重问。
   人类回答"两者都要"时，本轮问题**必须**声明了候选项，否则那句回答无处可落。
8. **`no_means` 字段（限制性问句必填）**：如果问题里有「只」「仅」「单纯」这类
   **收窄**的措辞（例如「第一版是否**只**做正算？」），人类回答「否」的意思是
   **"范围更宽，还得做别的"** —— 而"别的"是什么，必须有人写下来，否则那一轮白问。
   所以这类问题**必须**在 `no_means` 里列出"否"之后还可能要做的事
   （例如 `["反算（给定预算枚举可行组合）", "派生数值计算"]`），至少一条。
   下一轮会拿它直接追问。**注意**：`no_means` 是"准备去问的候选"，不是"已经确认的需求"，
   所以写在这里不会进 spec；只写进 `features` 的才算确认。
   非限制性问句留空即可。
9. **`acceptance` 不是可选项，而且必须"能跑出来"。** 只要人类确认了一条结论，
   就把它转成**命令、阈值或可数的结果**放进 `draft_updates.acceptance`
   （例如「`pytest -q` 退出码为 0」「给定预算枚举出的组合总花费都不超过预算」）。
   **不要**写「系统应该好用」「结果要正确」这类话 —— 判定器认得出它们没法机器验证，
   那份 spec 就只能停在 `converged_with_gaps`（= 不能动工）。
   （第四轮实测：四张卡里三张的验收条件要么 0 条、要么一条都不可机器验证；
   原来这条要求只写在"快到最后两轮了"的收尾提示里，而那些卡第 4~10 轮就收敛了，
   提示根本没机会出现 —— 所以现在**从第一轮就写在这里**。）
"""

CONVERGENCE_NOTE = """

注意：已经问到第 {round} 轮，剩余不超过 {remaining} 个问题。
接下来的问题必须直指"再不确定就没法动工"的那一点；
如果你已经能写出可动工的 spec，就直接在 draft_updates 里补齐，
并问一个确认性的收尾问题（仍然必须是是/否问题，例如"是否可以把当前这份 spec 当作可动工版本？"）。

**收尾前必须做的一件事**：把已经确认的结论转成 **≥1 条"能跑出来"的验收条件**
（命令、阈值、可数的结果）放进 `acceptance`。2026-09-12 两轮实测都没产出过验收条件 ——
而路线 0.2 的 TEST_GATE 要求验收标准是 PASS/BLOCKED 的判据，
一份没有验收条件的 spec **不能算可动工**（收敛状态会被标成 `converged_with_gaps`）。
"""

EXTRACT_PROMPT = """下面是一轮需求澄清问答。请只把**人类这一轮回答里新确认的信息**抽成 spec 字段。

规则：
- 只抽**人类回答里明确支持**的内容；不要把你自己的推测写进去，也不要复述问题。
- 已经有、或只是换措辞重述的内容**不要**再放进来 —— 空数组是完全合法的答案。
- **回答是「否」时，`non_goals` 必须写成"不做/不采用"的肯定式否定，并且带「不做：」前缀。**
  禁止双重否定：不要写「不只靠 X」「并非不采用 X」这类句式 ——
  它们读起来像"还是靠一点"，而人类的意思是"这条路不走"。
  *反例*：问题「第一版是否只靠 agent 主动上报心跳来发现异常？」+ 回答「否」
    ✗ 错：`不只靠 agent 主动上报心跳来发现异常`（双重否定，语义偏了）
    ✓ 对：`不做：只靠 agent 主动上报心跳来发现异常`（或写成明确采用的做法，放进 features）
- **否掉一个"收窄"表述（只/仅/单纯）时，不要返回空数组。** 这类问句里的「否」
  意思是**"范围更宽"**，那句"否"本身带正信息，必须落下来：把**扩展出去的部分**
  写成一条 `features`。只知道"更宽"而不知道宽在哪里时，照实写
  `除 <原来的 X> 之外还需包含其他方向（具体范围待确认）`，
  **不要编造**具体方向 —— 编造比留空更糟。
  （`no_means` 里给了候选项就用候选项的具体说法，例如 `除正算外还需支持反算`。）
  *例子*：问题「这个脚本的第一版是否只做正算（属性值 → 总花费）？」+ 回答「否」
    ✓ 对：`features: ["除正算外还需支持反算（给定预算枚举可行组合）"]`
    ✓ 对：`features: ["除正算外还需包含其他方向（具体范围待确认）"]`（只知道"更宽"时）
    ✗ 错：返回空数组（那一轮等于白问，"还要做别的"被丢掉）
- 人类在本轮**推翻/缩小范围**时，把被推翻的旧条目原文放进 `retracts`（其余字段留空）。
- **确认型回答永远算新信息**（2026-09-15 第六轮实测 P1⑤）：人类答「是」或「否」时，
  他是在**确认一条需求**，哪怕问题的措辞与草稿里已有的话看起来很像。
  **不要**因为"草稿里好像已经覆盖了"就返回空数组 —— 空数组会让这一轮白问，
  而且系统会把"没记下来"读成"问完了"（实测：卡 `2b461b3f3bf6` 第 4 轮人类点头的
  "命令行入口"就是这样丢的，而终稿显示"完整"）。
  只有当你**逐条核对**后发现该命题确确实实已经在清单里（同样的话、或明确等价的说法），
  才可以返回空数组。
- **`non_goals` 只放人类"主动排除"的路线**（他们在回答里明确说不要/不做的东西）。
  **不要**把"本轮问题里另一个选项"机械否定一遍塞进来 ——
  那是同一件事的两面，不是范围边界（实测：跑十轮会有一半条目是同义反复）。
- `idea` 只在人类明确修正了原始想法时填。
- 确认下来的结论如果是"能跑出来"的判据（命令/阈值/可数结果），请放进 `acceptance` ——
  一份没有验收条件的 spec 不能算可动工。

只输出一个 JSON 对象：{{"features": [], "non_goals": [], "acceptance": [], "idea": null, "retracts": []}}
"""


def build_messages(
    draft: SpecDraft,
    *,
    convergence_note: bool = True,
    open_gaps: Sequence[str] = (),
) -> list[dict[str, str]]:
    """
    拼出这一轮要发给模型的上下文。

    `open_gaps`：**把诊断反馈进提问链路**（第三轮实测报告 G2 的根因）。
    原来 `completeness.missing` 连续多轮列出同样的缺口（"还缺输入输出格式""还缺判定规则"），
    却**从来没有变成一个问题** —— 诊断信息与提问策略是脱节的，那份 spec 因此一直空着。
    """
    lines = [f"原始想法：{draft.idea}", ""]
    if draft.rounds:
        lines.append("已经问过和答过的：")
        for item in draft.rounds:
            lines.append(f"  第{item.index}轮：{item.question}")
            lines.append(f"    回答：{item.answer if item.answered else '（未回答）'}")
        lines.append("")
    lines.append("当前 spec 草稿：")
    lines.append(f"  功能：{draft.features or '（空）'}")
    lines.append(f"  非目标：{draft.non_goals or '（空）'}")
    lines.append(f"  验收条件：{draft.acceptance or '（空）'}")
    if draft.withdrawn:
        lines.append(f"  已撤回（不要再问，也不要当承诺）：{draft.withdrawn}")
    lines.append("")
    if open_gaps:
        lines.append("**完整度诊断反复指出、但还没有被任何一轮问到过的缺口：**")
        for item in open_gaps:
            lines.append(f"  - {item}")
        lines.append(
            "下一步**优先问其中一个**（必须是能用是/否回答的问题）。"
            "如果缺口写的是（或包含）验收口径，那这一问必须问成"
            "「给定 X 与输入 Y，输出是否必须等于 Z」这种**能直接落成可机器检查的验收条件**的形式。"
        )
        lines.append(
            # **G7（2026-09-15 第五轮实测）**：完整度诊断会把实现细节（名字冲突策略、
            # 字符集限制）和已经能由现有清单推出的东西（"多次运行之间保存"⇒ 必然跨进程）
            # 也列成缺口。它们**不是**需求分叉，为它们花一轮等于把注意力从真正的分叉上引开
            # —— 第四轮 10 轮、第五轮 6 轮里都有这种浪费。
            # 这里不硬过滤（代码判不准"这是实现细节吗"，硬过滤会把真缺口也丢掉），
            # 而是明确告诉生成器：**别为它单独花一轮**。
            "**但如果某个缺口只是实现细节**（命名规则、字符集、内部数据结构、文件放哪、日志格式…）"
            "**或已经被当前清单逻辑蕴含**，就不要为它单独花一轮：那由施工者决定，"
            "不是需求分叉。请跳过它，去问真正的分叉点。"
        )
        lines.append("")

    system = SYSTEM_PROMPT
    next_round = draft.answered_rounds + 1
    if convergence_note and next_round >= CONVERGE_FROM_ROUND:
        remaining = max(0, MAX_ROUNDS - draft.answered_rounds)
        system += CONVERGENCE_NOTE.format(round=next_round, remaining=remaining)
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": "\n".join(lines)},
    ]


def build_extract_messages(
    draft: SpecDraft,
    question: str,
    answer: str,
    *,
    options: Sequence[str] = (),
    no_means: Sequence[str] = (),
) -> list[dict[str, str]]:
    """
    拼出"把这轮回答抽成字段"的上下文。

    `options` 是**本轮问句的候选项**（G4）：人类答"两者都要"时，只有把候选项摆出来，
    模型才有位置把 A、B 分别落成条目 —— 否则只留下一句概括，另一支就静默丢了。

    `no_means` 是**限制性问句"否"分支的候选项**（G5）：人类答"否"时，
    那句"否"带的正信息是"范围更宽"，把候选摆出来抽取器才写得出具体说法，
    而不是只能返回空数组。
    """
    extra = ""
    if options:
        listed = " / ".join(options)
        extra += (
            f"\n本轮问题给出的候选项：{listed}\n"
            "如果人类的回答是「都要」「两者都是」这类**非二选一**的答案，"
            "请把候选项**分别**落成条目（不要只写一句概括，那会丢掉未被选中的那一支）。\n"
            "（候选项本身已由代码落了一次，你只需要补**候选项之外**的信息，"
            "例如「两者都要，但第一版先做 A」里的那句先后顺序。）\n"
        )
    if no_means:
        listed = " / ".join(no_means)
        extra += (
            f"\n本轮是**限制性**问句，若回答「否」则范围更宽，可能的扩展方向：{listed}\n"
            "回答是「否」时，请把**扩展出去的部分**写成 `features`（用上面的具体说法），"
            "不要返回空数组。\n"
        )
    return [
        {"role": "system", "content": EXTRACT_PROMPT.format()},
        {
            "role": "user",
            "content": (
                f"原始想法：{draft.idea}\n\n"
                f"本轮问题：{question}\n{extra}\n人类回答：{answer}\n\n"
                f"当前 spec 草稿（用于判断哪些是新的）：\n"
                f"  功能：{draft.features or '（空）'}\n"
                f"  非目标：{draft.non_goals or '（空）'}\n"
                f"  验收条件：{draft.acceptance or '（空）'}"
            ),
        },
    ]


# ------------------------------------------------------------------ 循环

Generate = Callable[[list[dict[str, str]], Any], dict[str, Any]]
Extract = Callable[..., dict[str, Any]]
EmbedFn = Callable[[list[str]], Any]


def _default_generate(messages: list[dict[str, str]], schema: Any) -> dict[str, Any]:
    """生产用的生成器：走网关的 flash 档。"""
    from ..gateway import chat

    return chat(messages, schema, "flash_api")


def default_extract(
    draft: SpecDraft,
    question: str,
    answer: str,
    options: Sequence[str] = (),
    no_means: Sequence[str] = (),
) -> dict[str, Any]:
    """
    生产用的抽取器：走 flash 把人类回答抽成 spec 字段。

    为什么抽取要单独一次调用：提问与抽取是两件事，混在一次调用里模型会
    "为了问下一个问题而编造已确认内容"。分开之后，抽取只看"这一轮回答说了什么"。
    """
    from ..gateway import chat

    return chat(
        build_extract_messages(draft, question, answer, options=options, no_means=no_means),
        Extraction,
        "flash_api",
    )


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%dT%H:%M:%S%z")


def spec_text(draft: SpecDraft) -> str:
    """
    把草稿压成一段可比较的文本。

    2.2 的信息增益拿它做 embedding：比较的是**整份 spec**的变化，
    而不是单条回答 —— 单条回答可能又长又偏，但对方案毫无贡献；
    整份 spec 变了多少，才真正反映"这一轮有没有让事情更清楚"。
    """
    parts = [f"想法：{draft.idea}"]
    if draft.features:
        parts.append("功能：" + "；".join(draft.features))
    if draft.non_goals:
        parts.append("非目标：" + "；".join(draft.non_goals))
    if draft.acceptance:
        parts.append("验收：" + "；".join(draft.acceptance))
    if draft.withdrawn:
        # 撤回也是 spec 状态的一部分：不写进来，2.2 就看不到"这一轮把范围砍小了"。
        parts.append("已撤回：" + "；".join(draft.withdrawn))
    return "\n".join(parts)


def is_machine_checkable(condition: str) -> bool:
    """
    验收条件是否**看起来**能机器执行。

    判据刻意宽松（有数字、有命令样式的反引号、有断言词），目标是抓出
    "系统应该好用"这种一眼就是愿望的条目，而不是做精确的语义判断 ——
    精确判断做不到，假装做到了更糟。

    **放在 `refiner` 而不是 `stopper`**（2026-09-13 第四轮实测报告 I3）：
    `IdeaRefiner.run()` 与 `StopJudger.finalize()` 是两个入口，它们必须用同一条口径
    判"这份草稿有没有验收缺口"。原来这条规则只写在 `stopper` 里，`run()` 只能自己
    复制一遍（而且只复制了"空不空"这一半），于是同一份草稿在两个入口下的状态**不一致**。
    规则只有一份，才谈得上一致；`stopper` 从这里重导出。
    """
    text = condition.strip()
    if not text:
        return False
    if any(char.isdigit() for char in text):
        return True
    markers = ("`", "返回", "等于", ">=", "<=", "≥", "≤", "通过", "失败", "退出码", "命令")
    return any(marker in text for marker in markers)


def acceptance_gap(draft: SpecDraft) -> str | None:
    """
    验收口径上的缺口（**单一口径**，`unresolved_gaps`/`judge`/`run` 都用它，免得判据漂移）。

    2026-09-13 第三轮实测报告 G1：原来只看 `acceptance` **空不空**，于是
    "两条验收条件全都被判为不可机器验证"的卡片照样拿到 `converged`（完整）——
    **用一句没法验的话占住 `acceptance` 的位置，就能让 `converged` 失去意义。**
    所以这里第三种情形（非空但一条都不可机器检查）同样算缺口。
    """
    if not draft.acceptance:
        return "没有任何验收条件"
    if not any(is_machine_checkable(item) for item in draft.acceptance):
        return f"验收条件都不可机器验证（{len(draft.acceptance)} 条）"
    return None


def render_markdown(draft: SpecDraft) -> str:
    """
    把草稿渲染成人看的 spec 终稿（D6：文档说落点是 `.md`，实现却只写 `.draft.json`）。

    **源卡永远是 `.draft.json`**（机器读、逐轮可追溯）；`.md` 只是它的一个视图。
    两份并存时以源卡为准 —— 谁也不会拿渲染稿去改需求。
    """
    lines = [
        f"# spec：{draft.idea}",
        "",
        f"- id：`{draft.id}`　状态：{draft.status}　已答轮数：{draft.answered_rounds}/{MAX_ROUNDS}",
        f"- 源卡（唯一真相）：`state/specs/{draft.id}.draft.json`",
        "",
        "## 功能",
        "",
    ]
    if draft.features:
        lines += [f"- {item}" for item in draft.features]
    else:
        lines.append("-（空）")
    lines += ["", "## 非目标", ""]
    if draft.non_goals:
        lines += [f"- {item}" for item in draft.non_goals]
    else:
        lines.append("-（空）")
    lines += ["", "## 验收条件", ""]
    if draft.acceptance:
        lines += [f"- {item}" for item in draft.acceptance]
    else:
        # 不要安静地写"（空）"：那看起来像"还没填"，而其实是"这份 spec 不能动工"。
        lines.append("**-（空）—— 没有任何验收条件，按 TEST_GATE 不能算可动工。**")
    if draft.withdrawn:
        lines += ["", "## 已撤回（曾经要过，后来被推翻/摘出范围）", ""]
        lines += [f"- ~~{item}~~" for item in draft.withdrawn]
    lines += ["", "## 问答历史", ""]
    for item in draft.rounds:
        # **问题在前、理由在后**（F5）：理由常常二三百字，放在问题前面会把问题埋掉。
        lines.append(f"{item.index}. **问**：{item.question}")
        if item.options:
            # 候选项必须**给人看见**：人类不知道本轮声明了哪两个分支，就没法回答「两者都要」。
            # 声明了互相矛盾的候选项时不宣传第三支 —— 那一支会被代码拒绝，说了等于骗人。
            hint = (
                "可以回答「是」「否」或**「两者都要」**（两项会分别落成条目）"
                if options_conflict(item.options) is None
                else "本轮候选项自相矛盾，「两者都要」无效，只能答「是」或「否」"
            )
            lines.append(f"   <sub>候选：{' / '.join(item.options)} —— {hint}</sub>")
        if item.why:
            lines.append(f"   <sub>理由：{item.why}</sub>")
        answer_line = f"   **答**：{item.answer if item.answered else '（未答）'}"
        if item.answered and not item.adopted:
            # **空转要在终稿里看得见**（第四轮实测报告 G5 的建议③）：
            # 原来"这一轮什么也没沉淀"只出现在一次运行日志里，删掉日志就没人知道
            # 人类答过这一问却没进 spec。
            answer_line += "　⚠️ **本轮没有沉淀任何字段**（这句回答没有被写进上面的清单）"
        lines.append(answer_line)
    lines.append("")
    return "\n".join(lines)


def new_draft(idea: str) -> SpecDraft:
    stamp = _now()
    return SpecDraft(
        id=uuid.uuid4().hex[:12],
        idea=idea.strip(),
        created_at=stamp,
        updated_at=stamp,
    )


@dataclass
class IdeaRefiner:
    """
    追问循环。**它自己不决定何时停** —— 停止判断属于 2.2（三路表决）。
    这里只提供 `is_full`（`MAX_ROUNDS` 硬上限）这一个与模型无关的兜底信号。
    """

    store_dir: Path = field(default_factory=lambda: SPECS_DIR)
    generate: Generate = _default_generate
    regenerate_limit: int = 3
    tier_note: str = ""
    #: 是否强制"提问质量"兜底（**是/否** + 不重复）。默认开。
    #:
    #: 关掉它的唯一正当用途：**合成草稿**去做别的评测 —— 例如 2.2 的停止判断评测要造出
    #: 24 轮的 spec，那些问题是夹具编的、与"提问质量"无关，逼它们各不相同没有意义。
    #: 关掉之后 2.1 的两条硬约束就没人守了，所以**生产路径与 2.1 的验收都必须保持开启**。
    strict_questions: bool = True
    #: 把人类回答抽成 spec 字段的钩子。默认 None = 不抽（调用方自己 `answer(updates=...)`）。
    #: 生产用 `default_extract`（flash）；本地档由 `template_answers` 兜底（见下）。
    extract: Extract | None = None
    #: **确认型回答的兜底**：问题是是/否，所以「是」/「否」本身就能直接落成
    #: features/non_goals —— 只在**抽取什么都没得到**时生效（有模型抽取结果时以模型为准）。
    #:
    #: **2026-09-15 第六轮实测后默认改成 True**（原来默认关）。报告用受控实验证明：
    #: 抽取能力没问题（空草稿上下文 60/60 全落地），但**草稿被填满之后**模型会按提示词
    #: "不要复述已有内容"**合理地**返回空数组 → 于是人类**亲口点头确认**的那条需求没进 spec
    #: （卡 `2b461b3f3bf6` 第 4 轮的"命令行入口"就是这样丢的，而终稿显示 `converged`（完整））。
    #: 兜底逻辑早就在 `apply_yes_no_answer()` 里、本来就救得回这一轮，**只是默认没被触发**。
    #: 它对非是/否的回答（`parse_yes_no` 判不出来）不做任何事，所以开关成本极低、收益很硬。
    template_answers: bool = True
    #: 语义去重的 embedding 钩子（D3）。给了才开，避免 `merge` 悄悄依赖本地模型。
    embed_fn: EmbedFn | None = None
    dedupe_threshold: float = DEDUPE_THRESHOLD
    #: **上一轮诊断里还悬着的缺口**（G2）：由调用方在每轮判停之后写进来
    #: （`note_gaps(decision.blocked_by_gaps)`），下一问会带着它们去问。
    #: 不这么接的话，`completeness.missing` 会连续多轮重复同样的缺口而**永远不被问到**。
    open_gaps: list[str] = field(default_factory=list)
    #: **必须带进下一问的追问题目**（第四轮实测报告 G5）。
    #:
    #: 与 `open_gaps` 分开是因为**生命期不同**：`open_gaps` 由调用方在每次判停后整批替换
    #: （`note_gaps`），而这里是"上一轮人类已经把话说出来了，只是还没说清"——
    #: 它不能被下一次判停覆盖掉，否则那句"否"就真的丢了。
    #: 实测形态：限制性问句（"第一版是否**只**做正算？"）答"否"，
    #: 人类的意思是"还要做别的"，而"别的"没有承载位置。
    carry_over: list[str] = field(default_factory=list)
    #: 本轮运行中累积的告警（例如"某轮什么都没沉淀"）。调用方负责展示，脚本不自己打印。
    warnings: list[str] = field(default_factory=list)

    def note_gaps(self, gaps: Sequence[str]) -> None:
        """记下"还没问到过的缺口"，下一次 `ask()` 会带着它们生成问题。"""
        self.open_gaps = [str(item).strip() for item in gaps if str(item).strip()]

    # ------------------------------------------------------------ 落盘

    def path_for(self, draft: SpecDraft) -> Path:
        return Path(self.store_dir) / f"{draft.id}.draft.json"

    def markdown_path_for(self, draft: SpecDraft) -> Path:
        return Path(self.store_dir) / f"{draft.id}.md"

    def persist(self, draft: SpecDraft) -> Path:
        draft.updated_at = _now()
        target = self.path_for(draft)
        target.parent.mkdir(parents=True, exist_ok=True)
        # 先写临时文件再替换：半写的 spec 比没有 spec 更糟 ——
        # 下游会把它当成一份完整的草稿读进来。
        temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
        temporary.write_text(
            json.dumps(draft.model_dump(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        temporary.replace(target)
        return target

    def write_markdown(self, draft: SpecDraft) -> Path:
        """把渲染稿写到 `state/specs/<id>.md`（D6：文档与实现对齐）。"""
        target = self.markdown_path_for(draft)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(render_markdown(draft), encoding="utf-8")
        return target

    @staticmethod
    def load(path: str | Path) -> SpecDraft:
        try:
            return SpecDraft.model_validate_json(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValidationError) as exc:
            raise SpecError(f"读不了 spec 草稿 {path}: {exc}") from exc

    # ------------------------------------------------------------ 一轮问答

    def propose(self, draft: SpecDraft) -> Proposal:
        """
        让模型给下一个问题；对**复合问题**、**开放问题**（不是是/否）、
        **重复问题**三种情况都重生成。

        重生成上限是有限的：连着几次都不合格，说明这次它帮不上忙，那就**报错停下** ——
        把不合格的问题发出去，代价是丢答案（复合/开放）或者白耗一轮（重复）。
        """
        if draft.is_full:
            raise SpecError(f"已经问满 {MAX_ROUNDS} 轮，硬上限不允许再问")

        history = [item.question for item in draft.rounds]
        # 两类缺口都带进这一问：`carry_over` 是"人类已经说了、只是没说清"（G5，必须先问），
        # `open_gaps` 是判停诊断列出的空白（G2）。
        messages = build_messages(draft, open_gaps=[*self.carry_over, *self.open_gaps])
        last_reason = ""
        last_error: type[SpecError] = OpenQuestion
        for _attempt in range(1, self.regenerate_limit + 1):
            raw = self.generate(messages, Proposal)
            try:
                proposal = Proposal.model_validate(raw)
            except ValidationError as exc:
                # 网关已经保证过 schema，走到这里说明调用方换了生成器
                raise SpecError(f"生成器返回的结构不符合 Proposal：{exc}") from exc

            question = proposal.question.strip()
            marker = find_compound_marker(question)
            if marker is not None:
                last_error = CompoundQuestion
                last_reason = (
                    f"上一个问题里出现了「{marker}」，那是复合问题，"
                    "人类通常只会回答其中一半，另一半会静默丢失。"
                    "请只围绕其中一个点重问，一句话一个问题。"
                )
            elif not self.strict_questions:
                # 合成草稿的评测（例如 2.2 造 24 轮 spec）不关心提问质量：
                # 那里的问题是夹具编的，逼它们各不相同没有意义（见 strict_questions 的注释）。
                return proposal
            else:
                open_marker = find_open_question_marker(question)
                if open_marker is not None:
                    last_error = OpenQuestion
                    last_reason = (
                        f"上一个问题里的「{open_marker}」让它成了开放问题，"
                        "而人类每轮只能用「是」或「否」回答（路线 0.5.B①）。"
                        "请把它改写成一个是非判断：句子里带上「是否」或「吗」，"
                        "让人一眼就能答是或否。"
                    )
                else:
                    duplicate = find_duplicate_question(question, history)
                    if duplicate is not None:
                        previous, score = duplicate
                        last_error = RepeatedQuestion
                        last_reason = (
                            f"上一个问题和第 {history.index(previous) + 1} 轮问过的几乎一样"
                            f"（相似度 {score:.2f}）：「{previous}」。"
                            "原样重问只会得到同样的答案。请换一个**角度**再问"
                            "（仍然必须是是/否问题），并在 why 里说明为什么必须换角度。"
                        )
                    else:
                        return proposal

            messages = messages + [
                {"role": "assistant", "content": proposal.model_dump_json()},
                {"role": "user", "content": last_reason},
            ]
        tail = {
            CompoundQuestion: "不发出复合问题（发出去会丢答案）",
            OpenQuestion: "不发出开放问题（人类只能用是/否回答，开放问题换不回可判定的答案）",
            RepeatedQuestion: "不发出重复问题（原样重问只会得到同样的答案）",
        }[last_error]
        raise last_error(f"重生成 {self.regenerate_limit} 次后问题仍不合格（{last_reason}）—— {tail}")

    def ask(self, draft: SpecDraft) -> Round:
        """生成下一个问题并落盘。返回这一轮（尚未回答）。"""
        proposal = self.propose(draft)
        item = Round(
            index=len(draft.rounds) + 1,
            question=proposal.question.strip(),
            why=proposal.why.strip(),
            options=[str(value).strip() for value in proposal.options if str(value).strip()],
            no_means=[str(value).strip() for value in proposal.no_means if str(value).strip()],
        )
        draft.rounds.append(item)
        # 注意：问题一旦问出，就应该把这一轮的 draft_updates 也先合进来 ——
        # 模型在提问时往往已经能确认一部分内容（例如从原始想法里提取的功能点），
        # 丢掉它等于让人类重复回答已经不用问的东西。
        item.adopted = draft.merge(
            proposal.draft_updates, embed_fn=self.embed_fn, dedupe_threshold=self.dedupe_threshold
        )
        self.persist(draft)
        # `carry_over` 已经变成这一轮的**具体问题**了，清掉；`open_gaps` 由调用方在判停后刷新。
        # 放在 persist 之后清：中途抛错时那句"人类说要、但还没说清"不能丢。
        self.carry_over = []
        return item

    def answer(self, draft: SpecDraft, text: str, *, updates: dict[str, Any] | None = None) -> SpecDraft:
        """
        记录人类对最后一轮的回答并落盘。

        **合并与快照的顺序是有讲究的（D1）**：先把本轮的 `updates` 合并进来，
        再取 `spec_digest`。原来先取快照再合并，于是 2.2 的信息增益看到的永远是
        "上一轮的状态"，本轮变化被系统性低估 —— 不报错，只是把人往过早收敛上推。

        `updates` 的三条来源，优先级从高到低：
        1. 调用方显式传入（`updates=`）；
        2. `self.extract` 钩子（生产用 flash 抽取）；
        3. `template_answers` 的离线兜底（问题是是/否，所以「是」/「否」能直接落地）。
        """
        if not draft.rounds:
            raise SpecError("还没有问过任何问题，无从回答")
        if draft.rounds[-1].answered:
            raise SpecError(f"第 {draft.rounds[-1].index} 轮已经记过回答了")
        current = draft.rounds[-1]
        current.answer = text.strip()

        adopted: list[str] = []
        # **「两者都要」先由代码落一次**（人类 2026-09-13 要求）。它是**机械映射**
        # （候选项 → features），不依赖模型，所以不能只写进提示词等模型照做：
        # 离线档没有模型抽取，那条答案就会把"两者"里的第二支丢掉。
        # 抽取仍然照跑 —— 人类那句里可能还有候选项之外的信息
        # （例如"两者都要，但第一版先做正算"），重复的条目由 `merge` 去重。
        targets, problem = both_targets(current.answer, current.options)
        landed: list[str] = []
        if targets:
            landed = draft.merge(
                {"features": targets}, embed_fn=self.embed_fn, dedupe_threshold=self.dedupe_threshold
            )
            adopted += landed
        if problem is not None:
            self.warnings.append(f"第 {current.index} 轮：{problem}")
            # 「两者」没落下的那一支必须被追问，否则这一轮等于白问 ——
            # 与 G5 的限制性问句共用 `carry_over` 这个承载位置。
            self.carry_over.append(f"{problem}；下一问必须问出「两者」分别是什么")
        if updates is not None:
            adopted += draft.merge(
                updates, embed_fn=self.embed_fn, dedupe_threshold=self.dedupe_threshold
            )
        elif self.extract is not None:
            raw = self.extract(
                draft, current.question, current.answer, current.options, current.no_means
            )
            adopted += draft.merge(
                raw, embed_fn=self.embed_fn, dedupe_threshold=self.dedupe_threshold
            )
        if not adopted and self.template_answers:
            # **确认型兜底**（第六轮实测 P0③，现在是默认开启）：抽取返回空、而回答本身是
            # 「是/否」时，命题按回答落成 features/non_goals —— 不这么做，
            # 人类亲口点头确认过的需求会**静默消失**（实测卡 F 第 4 轮的"命令行入口"）。
            adopted = apply_yes_no_answer(
                draft,
                current.question,
                current.answer,
                options=current.options,
                embed_fn=self.embed_fn,
                dedupe_threshold=self.dedupe_threshold,
            )
            if adopted:
                adopted = [*adopted, "template"]
        # 两条路都跑过，字段名可能重复（都命中 `features`）—— 按首现顺序去重，
        # 否则 `Round.adopted` 会写成 `["features", "features"]`，读日志的人会以为落了两次。
        adopted = list(dict.fromkeys(adopted))
        current.adopted = adopted
        self._carry_restrictive_no(current)
        if not adopted:
            # 「两者都要」但两个候选**都早就在 spec 里**时，空转的理由与"抽不出东西"不同：
            # 说明这一轮的问题本身是多余的（该问的已经问到了）。分开报，人才看得懂。
            if targets and not landed:
                self.warnings.append(
                    f"第 {current.index} 轮回答「两者都要」，但{len(targets)} 个候选条目都已经在 spec 里 —— "
                    "这一轮的问题是多余的（没有新增内容）"
                )
            else:
                self.warnings.append(
                    f"第 {current.index} 轮没有沉淀任何字段（回答：{current.answer[:30]}）—— "
                    "这一轮等于空转；若是本地档，请开 template_answers 或换 flash 抽取"
                )
        # **双重否定自检**（F3）：模型把「是否只靠 X」的"否"写成「不只靠 X」时，
        # 语义会反过来（读起来像"还是靠一点"）。这是措辞层的不稳定，不是确定性 bug，
        # 所以只告警不改写 —— 改写模型的话风险更大，看得见才是重点。
        for item in draft.non_goals:
            marker = find_double_negation(item)
            if marker is not None:
                self.warnings.append(
                    f"第 {current.index} 轮的非目标里出现双重否定「{marker}」：{item} —— "
                    "应以「不做：X」的形式重写（人类的意思是「这条路不走」）"
                )
                break

        # **合并之后**才取快照（D1）
        current.spec_digest = spec_text(draft)
        self.persist(draft)
        return draft

    def _carry_restrictive_no(self, current: Round) -> None:
        """
        **限制性问句答「否」时，把"还要做别的"接住**（2026-09-13 第四轮实测报告 G5）。

        `proposition_of` 把「第一版是否**只**做 X？」压成「第一版只做 X」，
        `as_non_goal` 再写成「不做：第一版只做 X」—— **只记下了"收窄被否"**。
        人类那句"否"带的正信息是"范围更宽、还要做别的"，而"别的"没有承载位置：
        实测里第 7 轮整轮空转（语义变化 0.0000、完整度 0.85→0.50），
        本卡的核心需求（反算）直到第 8 轮换角度重问才补上 ——
        如果人类在第 7 轮之后终止，它就被**永久**丢掉了。

        接住这件事的方法是**确定性**的，不依赖任何模型：写进 `carry_over`，
        下一次 `ask()` 会强制把它带进提问上下文（`build_messages` 里的缺口段）。
        提问侧声明了 `no_means` 就用候选的具体说法；没声明就退化成一条通用指令，
        并在 `warnings` 里留痕 —— **两种情形都必须让"别的"有一个承载位置**。
        """
        if not current.answered:
            return
        verdict = parse_yes_no(current.answer or "")
        if verdict is None:
            return
        if is_negated_question(current.question):
            verdict = not verdict          # 与 `apply_yes_no_answer` 同一口径
        if verdict is not False:
            return
        marker = find_restrictive_marker(current.question)
        if marker is None:
            return
        proposition = proposition_of(current.question)
        note = (
            f"人类在第 {current.index} 轮否掉了收窄命题「{proposition}」（问句里带「{marker}」）——"
            "意思是**范围更宽，还要做别的**。下一问必须先问出「还要做什么」。"
        )
        if current.no_means:
            note += "候选项：" + " / ".join(current.no_means)
        else:
            note += (
                "（上一问没有声明否分支候选，这里只能给通用指令：针对被收窄掉的维度，"
                "问一个「除了 X 之外是否还需要 <某个具体方向>」的是非问题。）"
            )
            self.warnings.append(
                f"第 {current.index} 轮的限制性问句（含「{marker}」）没有声明 `no_means` —— "
                "回答「否」时只有一条「不做：…」落进非目标，扩展部分已由 carry_over 强制追问"
            )
        if note not in self.carry_over:
            self.carry_over.append(note)

    # ------------------------------------------------------------ 驱动整轮

    def run(self, idea: str, answers: list[str]) -> SpecDraft:
        """
        用给定的回答列表把循环跑到底（验收与测试用）。

        回答用完之后就停 —— 不编造答案，也不空转。
        """
        draft = new_draft(idea)
        self.persist(draft)
        for text in answers:
            if draft.is_full:
                break
            self.ask(draft)
            self.answer(draft, text)
        draft.status = "converged" if draft.answered_rounds else "asking"
        # 有缺口就不算"真的可以动工"（F1/G1）：**用与 `finalize()` 完全相同的那一条口径**。
        # 这里原来自己复制了半条规则（只判 `acceptance` 空不空），于是"验收条件都在、
        # 却一条都不可机器验证"的草稿在 run() 下是 `converged`、在官方入口下是
        # `converged_with_gaps` —— 两个入口状态不一致（第四轮实测报告 I3 踩的就是这条缝）。
        if draft.status == "converged" and acceptance_gap(draft) is not None:
            draft.status = "converged_with_gaps"
        self.persist(draft)
        if draft.status in ("converged", "converged_with_gaps"):
            self.write_markdown(draft)
        return draft


__all__ = [
    "BOTH_ANSWER_MARKERS",
    "COMPOUND_MARKERS",
    "CONVERGE_FROM_ROUND",
    "DEDUPE_THRESHOLD",
    "DOUBLE_NEGATION_MARKERS",
    "DUPLICATE_RATIO",
    "MAX_ROUNDS",
    "OPEN_MARKERS",
    "RESTRICTIVE_MARKERS",
    "SPEC_STATUSES",
    "CompoundQuestion",
    "Extract",
    "Extraction",
    "IdeaRefiner",
    "OpenQuestion",
    "Proposal",
    "RepeatedQuestion",
    "Round",
    "SpecDraft",
    "SpecError",
    "acceptance_gap",
    "apply_yes_no_answer",
    "as_non_goal",
    "both_targets",
    "build_extract_messages",
    "build_messages",
    "default_extract",
    "find_compound_marker",
    "find_double_negation",
    "find_duplicate_question",
    "find_open_question_marker",
    "find_restrictive_marker",
    "is_both_answer",
    "is_machine_checkable",
    "is_yes_no_question",
    "new_draft",
    "options_conflict",
    "parse_yes_no",
    "proposition_of",
    "question_similarity",
    "render_markdown",
    "spec_text",
]
