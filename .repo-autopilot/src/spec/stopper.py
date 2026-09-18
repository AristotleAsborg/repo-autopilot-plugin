"""路线 2.2：停止判断器（本地小模型 + 算法，**不用 flash**）。

## 为什么不能只听模型的

"你觉得问够了吗"这个问题，模型的答案取决于它当时有多想继续问 ——
而追问的收益归它自己（上下文更全），成本归人类（多回答一轮）。
把停止权完全交给它，等于让被监督者决定监督何时结束。

所以这里用**三路独立信号 + 多数表决**，其中两路根本不问模型：

| 信号 | 来源 | 失灵时会怎样 |
| --- | --- | --- |
| ① 完整度打分 | 本地小模型，带 schema | 模型永远觉得不够 → 还有 ②③ |
| ② 信息增益 | 纯算法（embedding 语义变化量） | 小项目上信号弱 → 还有 ①③ |
| ③ 12 轮硬上限 | 纯规则 | 永不失灵（它不依赖任何模型） |

① 与 ② 各算一票，**≥2 票**才停。③ 不是普通一票，它是**否决权**：
满 12 轮直接停，越过表决。理由见 `judge()` 里的注释 ——
如果硬上限只是一票，"模型永远说不够 + 信息增益有噪声"就能把循环拖过 12 轮，
而那恰恰是这条规则存在的意义。

## 与路线的一处偏差（记录，不隐藏）

路线说定稿由 **flash** 汇总产出。本机 `DEEPSEEK_API_KEY` 不在环境变量里，
flash 档的模型 id 也仍是未核对的占位值。所以 `finalize()` 的汇总器可注入，
默认走本地小模型；切 flash 只需换一个参数，代码不用改。
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
from pydantic import BaseModel, Field, ValidationError

from .refiner import (
    MAX_ROUNDS,
    Round,
    SpecDraft,
    acceptance_gap,
    is_machine_checkable,
    spec_text,
)

ROOT = Path(__file__).resolve().parents[2]
SPECS_DIR = ROOT / "state" / "specs"

# 路线 2.2 子步骤 1~4 里的三个阈值/票数
COMPLETENESS_THRESHOLD = 0.85
INFO_GAIN_THRESHOLD = 0.02
INFO_GAIN_CONSECUTIVE = 2
VOTES_NEEDED = 2


class StopperError(RuntimeError):
    """停止判断器自身的错误。绝不吞掉。"""


class Completeness(BaseModel):
    """信号① 的 schema（路线原文：`{"completeness": float, "missing": [str]}`）。"""

    completeness: float = Field(ge=0.0, le=1.0)
    missing: list[str] = Field(default_factory=list)


@dataclass
class Signal:
    name: str
    suggests_stop: bool
    detail: str


@dataclass
class StopDecision:
    stop: bool
    votes: int
    signals: list[Signal] = field(default_factory=list)
    # 是否由 12 轮硬上限**直接**决定（越过表决）。
    forced_by_cap: bool = False
    #: 因为"还没到能收敛的条件"而被硬拦下的理由（空 = 没拦）。
    #:
    #: 2026-09-12 第二轮实测报告 F1：默认参数下**4 轮就判收敛**，而那份 spec
    #: 只有 2 条功能、0 条验收条件 —— 用户看到 `converged` 会以为可以动工。
    #: 教训：**"变化慢"不等于"够了"**，而三路信号里有两路对"不完整"不敏感，
    #: 所以必须有一条**与模型无关**的下限规则。
    blocked_by_gaps: list[str] = field(default_factory=list)

    @property
    def reason(self) -> str:
        stopped = [s.name for s in self.signals if s.suggests_stop]
        head = "硬上限强制停" if self.forced_by_cap else f"{self.votes}/{len(self.signals)} 路建议停"
        if self.blocked_by_gaps and not self.stop:
            return f"{head}，但被完备度下限拦下：{'；'.join(self.blocked_by_gaps)}"
        return f"{head}（{'、'.join(stopped) or '无'}）"

    def as_dict(self) -> dict[str, Any]:
        return {
            "stop": self.stop,
            "votes": self.votes,
            "forced_by_cap": self.forced_by_cap,
            "blocked_by_gaps": self.blocked_by_gaps,
            "reason": self.reason,
            "signals": [s.__dict__ for s in self.signals],
        }


def empty_adoption_gaps(draft: SpecDraft) -> list[str]:
    """
    **问过、答了、却什么都没沉淀**的轮次（2026-09-15 第六轮实测报告 P0①）。

    报告用受控实验把因果链钉死了：抽取**不是**坏掉（空草稿上下文 60/60 都能落地），
    真正的坑是**上下文驱动的假阴性** —— 草稿被填满之后，模型按提示词"不要复述已有内容"
    **合理地**返回空数组，于是那一轮的内容**没进 spec**。而 `adopted=[]` 当时
    **不产生任何结构性信号**：只在运行日志里打一行 `[!]`，终稿、`missing`、`status` 全不反映。
    最坏的一种后果：卡 `2b461b3f3bf6` 第 4 轮人类点头确认的"命令行入口"丢了，
    而终稿是 `converged`（完整）、缺口 0 条 —— **人类没有任何结构性线索能发现它**。

    所以这里把它升格成**缺口**：进 `blocked_by_gaps`（下一问优先补）、
    进终稿的"还没定的事"、并让状态落到 `converged_with_gaps`。

    措辞刻意是"**需要确认是否已被覆盖**"而不是"内容丢了"：后续轮次换角度重问时
    确实可能已经补回来（G5 的 `carry_over` 就是干这个的），而代码判不准"覆盖"这件事 ——
    所以如实记成一条**待人确认的开放项**，不假装内容一定丢了、也不假装一定补上了。
    """
    gaps: list[str] = []
    for item in draft.rounds:
        if not item.answered or item.adopted:
            continue
        snippet = (item.answer or "").strip().replace("\n", " ")[:24]
        gaps.append(
            f"第 {item.index} 轮的回答没有沉淀任何字段（答：{snippet}）—— "
            "这句回答没有进任何清单，需要确认它是否已被后续轮次覆盖"
        )
    return gaps


def unresolved_gaps(draft: SpecDraft, completeness: Completeness | None = None) -> list[str]:
    """
    **收敛的下限**（F1/G1）：现在还差什么。

    - **验收口径**：这是确定性判据，也是唯一害过人的那一条 ——
      TEST_GATE 要求"验收标准"是 PASS/BLOCKED 的判据，一份没有**可机器检查**的验收条件的 spec
      根本没法判定做完没有。**不许模型投票把它投过去。**（三种情形见 `acceptance_gap`）
    - **空转轮次**（G6/P0①）：问过、答了、什么都没沉淀 —— 见 `empty_adoption_gaps`。
      它**不**参与 `judge` 的硬拦（那一条只认验收口径），但它决定**终稿状态**与
      **给人类看的缺口清单**：一份"少了人类亲口确认过的东西"的 spec 不该显示为"完整"。
    - **完整度打分列出的缺口**：这是模型判断，只作补充（不单独当一票否决），
      但它同样要进 `blocked_by_gaps` 让人看见 —— 尤其当分数还很高的时候（自相矛盾）。
    """
    gaps: list[str] = []
    missing_acceptance = acceptance_gap(draft)
    if missing_acceptance is not None:
        gaps.append(missing_acceptance)
    gaps.extend(empty_adoption_gaps(draft))
    if completeness is not None:
        gaps.extend(str(item) for item in completeness.missing if str(item).strip())
    return gaps


# ------------------------------------------------------------------ 相似度

def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    """
    余弦相似度。**不假设输入已归一化** —— 调用方可能传进来原始向量，
    静默算错比算慢糟糕得多（阈值是绝对值，算错就没人会发现）。
    """
    va = np.asarray(a, dtype=float)
    vb = np.asarray(b, dtype=float)
    na = float(np.linalg.norm(va))
    nb = float(np.linalg.norm(vb))
    if na == 0.0 or nb == 0.0:
        raise StopperError("向量范数为 0，余弦无定义（上游 embedding 出了问题）")
    return float(np.dot(va, vb) / (na * nb))


def semantic_delta(before: Sequence[float], after: Sequence[float]) -> float:
    """语义变化量 = 1 - 余弦。范围 [0, 2]，越小说明文本越没变。"""
    return 1.0 - cosine(before, after)


# ------------------------------------------------------------------ 提示词

COMPLETENESS_SYSTEM = """你在判断一份需求草稿能不能动工了。

只输出一个 JSON 对象：
  {"completeness": <0~1 的小数>, "missing": ["还缺什么，最多三条"]}

怎么打分（请如实，不要为了显得尽责而压低分数）：
  * 1.0 —— 可以直接开工：做什么、不做什么、怎么算做完了都清楚；
  * 0.85 以上 —— 只剩无关紧要的细节没定，可以先做起来；
  * 0.5 上下 —— 主干清楚，但有一两处不定就得返工；
  * 0.3 以下 —— 连要解决的问题都还没说清。

**两条硬规矩（2026-09-12 第二轮实测后加的，必须遵守）：**
1. **`missing` 非空时，`completeness` 必须低于 0.85。** 一边说"很完整"一边列出缺口，
   两句话只能有一句是真的 —— 而缺口是照着 spec 说的，所以低分才是真的。
   （判定器有代码兜底：出现这种自相矛盾时，分数会被按"有缺口"处理。）
2. **`acceptance` 为空时必须把"没有任何验收条件"写进 `missing`，且分数不得高于 0.5。**
   两份真实实测都出现过"跑完 4 轮 / 16 轮，一条验收条件都没有"——
   按路线 0.2 的 TEST_GATE，没有验收标准就没法判 PASS/BLOCKED，那种 spec 不能动工。

`missing` 只写**真正会导致返工**的空白。写不出具体的空白，就说明分数不该低。

**哪些不算"需求空白"（2026-09-15 第五轮实测 G7 加的分层规矩，必须遵守）：**

3. **只报"需求层"的未知，实现细节不是缺口。** 施工者自己会定的事 ——
   名字冲突怎么办、名字的长度与字符集限制、内部用什么数据结构、文件放哪、日志什么格式 ——
   都不要写进 `missing`。写进去的后果很实在：它会被当成"还缺需求"连续几轮占着诊断，
   把注意力从真正的分叉（用户是谁、运行形态是什么）上引开。
4. **已经被现有清单逻辑蕴含的东西不要再列。** 例如 `功能` 里已经写了
   "在**多次运行之间**保存数值"，那就**不要**再问"是否需要持久化到文件，还是只在内存里"——
   "多次运行"已经排除了"只在内存"。这类"自造缺口"同样是白占一轮。

判断口径：**这条空白如果不定，会不会导致做出来的东西不是用户要的？** 会 → 写；
不会（只是施工选择）→ 不写。`missing` 宁少勿滥：漏一条真缺口由人类在收尾轮补，
而列三条假缺口会让诊断本身失去可信度。
"""


def build_completeness_messages(draft: SpecDraft) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": COMPLETENESS_SYSTEM},
        {"role": "user", "content": spec_text(draft) + "\n\n问答历史：\n" + _qa_text(draft)},
    ]


def _qa_text(draft: SpecDraft) -> str:
    """
    问答历史（**只列已答轮次**）。

    口径固定这一点很重要（2026-09-12 的 M1 实测报告 D5）：`missing` 是这个系统对人类的
    主要诊断输出（"还差什么"）。如果把它打分的输入里塞进"当前那一轮还没回答的问题"，
    模型就会把"你欠我一个答案"写进 `missing` —— 而那是**流程状态**，不是"需求还有空白"。
    同一个卡片在不同时点打分就会给出不一致的 `missing`，这个诊断也就没人信了。
    """
    answered = [item for item in draft.rounds if item.answered]
    if not answered:
        return "（还没有已答的问答）"
    return "\n".join(
        f"第{item.index}轮：{item.question}\n  答：{item.answer}" for item in answered
    )


# ------------------------------------------------------------------ 判断器

ScoreFn = Callable[[list[dict[str, str]], Any], dict[str, Any]]
EmbedFn = Callable[[list[str]], np.ndarray]
SummarizeFn = Callable[[SpecDraft], str]


@dataclass
class StopJudger:
    """
    三路信号 + 多数表决。

    `score` / `embed` / `summarize` 都可注入：验收要能构造确定的输入去验每一路，
    而不是每次都去求模型配合。
    """

    score: ScoreFn | None = None
    embed: EmbedFn | None = None
    summarize: SummarizeFn | None = None
    spec_dir: Path = field(default_factory=lambda: SPECS_DIR)

    # ------------------------------------------------------------ 默认实现

    def _score(self, messages: list[dict[str, str]], schema: Any) -> dict[str, Any]:
        if self.score is not None:
            return self.score(messages, schema)
        from ..gateway import chat

        return chat(messages, schema, "local_small")

    def _embed(self, texts: list[str]) -> np.ndarray:
        if self.embed is not None:
            return self.embed(texts)
        from ..gateway import embed

        return embed(texts)

    # ------------------------------------------------------------ 信号①

    def completeness(self, draft: SpecDraft) -> Completeness:
        raw = self._score(build_completeness_messages(draft), Completeness)
        try:
            verdict = Completeness.model_validate(raw)
        except ValidationError as exc:
            raise StopperError(f"完整度打分不符合 schema：{exc}") from exc
        return verdict

    # ------------------------------------------------------------ 信号②

    def information_gains(self, draft: SpecDraft) -> list[float]:
        """
        逐轮的语义变化量。第 i 个 = 第 i 轮回答之后，spec 文本相对上一轮变了多少。

        用**累积文本**而不是单条回答：单条回答可能又长又偏，但对 spec 毫无贡献；
        累积文本的变化才真正反映"这一轮有没有让方案更清楚"。
        """
        digests = [item.spec_digest for item in draft.rounds if item.answered]
        if len(digests) < 2:
            return []
        vectors = self._embed(digests)
        array = np.asarray(vectors, dtype=float)
        if array.ndim != 2 or array.shape[0] != len(digests):
            raise StopperError(f"embedding 形状异常：{array.shape}，期望 ({len(digests)}, dim)")
        return [
            semantic_delta(array[index - 1], array[index]) for index in range(1, len(array))
        ]

    # ------------------------------------------------------------ 三路表决

    def judge(
        self,
        draft: SpecDraft,
        *,
        completeness: Completeness | None = None,
        gains: list[float] | None = None,
    ) -> StopDecision:
        signals: list[Signal] = []
        verdict: Completeness | None = None

        # ① 完整度
        try:
            verdict = completeness if completeness is not None else self.completeness(draft)
            effective = verdict.completeness
            detail = (
                f"完整度 {verdict.completeness:.2f}（阈值 {COMPLETENESS_THRESHOLD}）"
                + (f"，还缺：{verdict.missing}" if verdict.missing else "")
            )
            if verdict.missing and verdict.completeness >= COMPLETENESS_THRESHOLD:
                # **自相矛盾就按有缺口处理**（F1）：一边说 0.95 一边列出两条缺口，
                # 那两条缺口是真的（它是照着 spec 说的），高分会把"没完备"糊过去。
                # 判定改成"低于阈值一点点"，并在 detail 里写明为什么 —— 让人能看出
                # 这个分数不是模型给的，是**一致性规则**改过的。
                effective = COMPLETENESS_THRESHOLD - 0.01
                detail += (
                    f"；**模型自相矛盾**（既给 {verdict.completeness:.2f} 又列出缺口）"
                    f"→ 按 {effective:.2f} 处理"
                )
            signals.append(
                Signal("completeness", effective >= COMPLETENESS_THRESHOLD, detail)
            )
        except (StopperError, Exception) as exc:  # noqa: BLE001
            # 打分失败**不能**当成"停"：停是一个不可逆的决定（收敛之后就定稿了）。
            # 失败时把这一路置为"不停"，让另外两路决定，并把原因记下来。
            signals.append(Signal("completeness", False, f"打分失败，这一路弃权：{exc}"))

        # ② 信息增益
        try:
            deltas = gains if gains is not None else self.information_gains(draft)
            tail = deltas[-INFO_GAIN_CONSECUTIVE:]
            quiet = (
                len(tail) >= INFO_GAIN_CONSECUTIVE
                and all(delta < INFO_GAIN_THRESHOLD for delta in tail)
            )
            shown = "、".join(f"{delta:.4f}" for delta in tail) or "（轮次不足）"
            signals.append(
                Signal(
                    "info_gain",
                    quiet,
                    f"最近 {len(tail)} 轮语义变化 {shown}（阈值 {INFO_GAIN_THRESHOLD}）",
                )
            )
        except Exception as exc:  # noqa: BLE001
            signals.append(Signal("info_gain", False, f"信息增益算不出来，这一路弃权：{exc}"))

        # ③ 规则兜底（不依赖任何模型，永不弃权）
        cap = draft.answered_rounds >= MAX_ROUNDS
        signals.append(
            Signal("round_cap", cap, f"已答 {draft.answered_rounds}/{MAX_ROUNDS} 轮")
        )

        votes = sum(1 for signal in signals if signal.suggests_stop)
        # **完备度下限**（F1/G1）：验收口径不成立时，无论几路投"停"都不许收敛 ——
        # 除非 24 轮硬上限到了（那时必须停，否则循环没有出口；但状态会标成
        # `converged_with_gaps`，见 `finalize`）。
        #
        # 为什么验收口径能用"硬拦"：它是**确定性**判据（列表空不空、有没有一条能机器检查），
        # 不依赖任何模型的判断；而三路信号里恰恰没有一路对"不完整"敏感 ——
        # `info_gain` 量的是"变化速率"，一份还缺关键判据的 spec 只要新增变少就会投停（F1 实测）。
        gaps = unresolved_gaps(draft, verdict)
        hard_block = acceptance_gap(draft) is not None
        cap = draft.answered_rounds >= MAX_ROUNDS
        blocked = hard_block and not cap
        # 满 24 轮**越过表决直接停**。
        # 路线对这个信号的措辞是"强制停（独立于模型判断，防小模型'永远觉得不够'）"——
        # 如果它只是一票，那么"模型永远说不够 + 信息增益一直有噪声"就能把循环拖过上限，
        # 而那正是这条规则存在的理由。所以它是**否决权**，不是普通一票。
        return StopDecision(
            stop=cap or (votes >= VOTES_NEEDED and not blocked),
            votes=votes,
            signals=signals,
            forced_by_cap=cap,
            blocked_by_gaps=gaps if blocked else [],
        )

    # ------------------------------------------------------------ 定稿

    def finalize(self, draft: SpecDraft, *, completeness: Completeness | None = None) -> Path:
        """
        产出定稿 `state/specs/{id}.md`（功能清单 / 非目标 / 验收条件）。

        `facts` 那条借鉴来的规矩：**验收条件必须可机器执行**，否则打回。
        这里不"打回"，而是把含糊的条件**标出来**给人看 ——
        自动拒绝会把人也一起挡在门外，标出来则让人一眼看到哪条是空的。

        **`converged` 与"可用"分离**（F1）：还有缺口时状态写成 `converged_with_gaps`，
        并在定稿里显式列出缺口。用户看到 `converged` 就意味着"可以动工"，
        那个状态不该被一份没有验收条件的 spec 拿去用。
        """
        gaps = unresolved_gaps(draft, completeness)
        draft.status = "converged_with_gaps" if gaps else "converged"
        target = Path(self.spec_dir) / f"{draft.id}.md"
        target.parent.mkdir(parents=True, exist_ok=True)

        vague = [item for item in draft.acceptance if not is_machine_checkable(item)]
        body = self.summarize(draft) if self.summarize is not None else self._default_summary(draft)

        lines = [
            f"# spec {draft.id}",
            "",
            f"- 原始想法：{draft.idea}",
            f"- 定稿时间：{datetime.now(timezone.utc).astimezone().strftime('%Y-%m-%dT%H:%M:%S%z')}",
            f"- 追问轮数：{draft.answered_rounds}",
            f"- **收敛状态：`{draft.status}`**"
            + (
                # 缺口条数直接写进状态行（2026-09-17 总报告阶段一-2）：
                # "一眼可见"这件事不该要求人去数下面那一节。
                f"（还有 {len(gaps)} 条缺口，不能直接动工）"
                if gaps
                else "（完整）"
            ),
            "",
            "## 概述",
            "",
            body.strip() or "（汇总为空）",
            "",
            "## 功能清单",
            "",
        ]
        # 显式分支，不用 `[...] or ["（空）"]`：那种写法靠"空列表为假"的隐式行为，
        # 读起来像"拼接失败才追加"，改动时极易出错（F6）。
        if draft.features:
            lines += [f"- {item}" for item in draft.features]
        else:
            lines.append("-（空）")
        lines += ["", "## 非目标", ""]
        if draft.non_goals:
            lines += [f"- {item}" for item in draft.non_goals]
        else:
            lines.append("-（空）")
        if draft.withdrawn:
            # **G6（2026-09-15 第五轮实测）**：撤回过的条目以前**只**出现在
            # `render_markdown()` 与给模型的上下文里，官方定稿入口 `finalize()` 一个字都不写。
            # 后果很实在：施工者看到终稿"没提命令行"，可能又把它加回来 ——
            # 而它正是被人类明确否掉过的东西。记录还在源卡里，但交付物里看不到。
            # 写法与 `render_markdown()` 一致（删除线），并说明**为什么**留着它。
            lines += [
                "",
                "## 已撤回（曾经排除、后来被推翻）",
                "",
                "> 下面这些**不再算承诺**，但保留记录：它们解释了最终形态为什么长这样。",
                "> 施工时不要因为「终稿里没提」就把它们又加回来。",
                "",
            ]
            lines += [f"- ~~{item}~~" for item in draft.withdrawn]
        lines += ["", "## 验收条件", ""]
        if draft.acceptance:
            lines += [
                f"- {item}" + ("" if is_machine_checkable(item) else "   ⚠️ 不可机器验证")
                for item in draft.acceptance
            ]
            # **把口径写在交付物里**（2026-09-17 总报告阶段一-3）：有人会以为"全部验收条件
            # 都必须可机器验证"，也有人会以为"有一条就算过"。实际门槛是**后者**——
            # 那是有意选的最小门槛（否则大部分真实 spec 会被卡在门外），说清楚免得被误读。
            lines += [
                "",
                (
                    "> **口径说明**：本系统的门槛是「**至少一条**可机器验证」（有意选的最小门槛）。"
                    "其余条目若不可验证会带 ⚠️ —— 它们不是没有价值，而是不能当 PASS/BLOCKED 的判据。"
                    "若你要更严（全部可验证才算过），请在追问阶段把每条都写成能跑出来的判据。"
                ),
            ]
        else:
            # F4：两轮实测都没产出过验收条件 —— 而"（空）"太安静了，看起来像没填而已。
            lines.append("**-（空）—— 这份 spec 没有任何验收条件，按 TEST_GATE 不能算可动工。**")
            lines.append("")
            lines.append("> 请回去补一轮：把已确认的结论转成**能跑出来**的判据（命令/阈值/可数结果）。")
        lines += [""]
        if gaps:
            lines += ["## 还没定的事（收敛状态为 `converged_with_gaps` 的原因）", ""]
            lines += [f"- {item}" for item in gaps]
            lines += [""]
        if vague:
            lines += [
                f"> ⚠️ 上面有 {len(vague)} 条验收条件看不出怎么机器验证。",
                "> 验收条件必须是**能跑出来**的（命令、阈值、可数的结果），",
                "> 否则它只是一句愿望，会在最后一步变成扯皮。",
                "",
            ]
        lines += ["## 追问记录", "", "| # | 问题 | 回答 | 沉淀 |", "|---|---|---|---|"]
        lines += [_round_row(item) for item in draft.rounds]
        lines.append("")
        if any(item.answered and not item.adopted for item in draft.rounds):
            # **空转要在终稿里看得见**（第四轮实测报告 G5 建议③）：原来"这一轮什么也没沉淀"
            # 只出现在一次运行日志里，日志一删就没人知道人类答过这一问却没进 spec。
            lines += [
                "> ⚠️ 上表「沉淀」为空的行 = **人类答了这一问，但那句回答没有写进任何清单**。",
                (
                    "> 最典型的成因是限制性问句（「是否只做 X」）被回答「否」—— 那句「否」的意思是"
                    "「还要做别的」，提问时若没声明候选，就只能靠下一轮追问补回来。"
                ),
                "",
            ]

        temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
        temporary.write_text("\n".join(lines), encoding="utf-8")
        temporary.replace(target)
        return target

    def _default_summary(self, draft: SpecDraft) -> str:
        """
        末尾兜底：没有汇总器时用模板拼，而不是编造一段像模像样的散文。

        编造的概述比没有概述更危险 —— 它会被下游当成"这就是需求"。
        """
        if not draft.features and not draft.acceptance:
            return "（草稿里还没有可汇总的内容）"
        bits = [f"要做 {len(draft.features)} 件事"]
        if draft.non_goals:
            bits.append(f"明确不做 {len(draft.non_goals)} 件")
        if draft.acceptance:
            bits.append(f"{len(draft.acceptance)} 条验收条件")
        return "本 spec 共" + "，".join(bits) + "。详见下面各节。"


def _round_row(item: Round) -> str:
    """
    追问记录表的一行。

    **「沉淀」列是"终稿里可见的事实"**（第四轮实测报告 G5 建议③）：
    空 = 人类答了这一问，但那句回答没有被写进任何清单。原来这种空转只出现在
    一次运行日志里，日志一删就查不到了 —— 而它恰恰是"需求被丢掉"的信号。
    """
    def cell(text: object) -> str:
        # 表格里出现裸 `|` 会把列切断 —— 问题句里有"或"的时候人类写过竖线。
        return str(text).replace("|", "\\|")

    mark = "、".join(item.adopted) if item.adopted else "**（空转）**"
    question = cell(item.question)
    if item.options:
        # 候选项要进终稿（2026-09-13 加「两者都要」这一支之后更必要）：
        # 不写出来，读者看到「两者都要」这句回答根本不知道"两者"是哪两者。
        question += f"　<sub>候选：{cell(' / '.join(item.options))}</sub>"
    return f"| {item.index} | {question} | {cell(item.answer or '（未答）')} | {mark} |"


__all__ = [
    "COMPLETENESS_THRESHOLD",
    "INFO_GAIN_CONSECUTIVE",
    "INFO_GAIN_THRESHOLD",
    "VOTES_NEEDED",
    "Completeness",
    "Signal",
    "StopDecision",
    "StopJudger",
    "StopperError",
    "acceptance_gap",
    "build_completeness_messages",
    "cosine",
    "empty_adoption_gaps",
    "is_machine_checkable",
    "semantic_delta",
    "spec_text",
]
