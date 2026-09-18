"""3.2 的判定与路由：阈值 → 动作 → 队列。

路线原文（步骤 3.2）：
1. BGE-M3 向量化全部 open issue，存 `state/vectors/`；
2. 阈值：>0.92 标 `duplicate?` 并评论链接原 issue；>0.98 才建议关闭（走闸门）；
3. 路由：bug → 修复队列；feature → M1 追问（可选）；question → 仅回复；
4. 新 issue 先进来查重再分类（省 token）。

## 两个阈值不是"两个档"，而是两种**后果**

- `>0.92`：**只是加个标记**，帖子照常处理。判错的代价是多一条评论。
- `>0.98`：**建议关闭**。判错的代价是"一个新用户第一次提问就被机器人关掉"——
  他不会再回来。所以这一档**永远走人类闸门**（1.4），而且永远不自动执行。

0.92/0.98 是起点不是真理（路线原话）：不同仓库的 issue 风格差异极大。
真要用好，应该拿回放数据画"阈值-误杀率"曲线、给每个仓库配阈值 —— 那需要先有
一个能稳定测出误杀率的东西，也就是本文件下面这套 `Deduper`。

## 为什么"先查重再分类"

分类要读全文、要调模型，查重只要一次 embedding（本来就比一次生成便宜得多）。
更重要的是**顺序影响结果**：重复 issue 往往写得很短、信息不全，拿来分类很容易
分出一个误导性的标签；先认出"这是重复"，就不必再给一个假标签。
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from .store import Match, VectorStore

#: 加标记的阈值（评论链接原 issue，不停下流程）
#:
#: **不是路线里写的 0.92** —— 0.92 是起点不是真理，路线自己要求"用回放数据画出曲线"。
#: 2026-09-12 用 bge-m3 实测（`tools/eval_dedupe.py`，报告
#: `state/reports/dedupe-thresholds-local_embed.md`）：
#:   - 10 对真实同义改写的余弦只有 0.80～0.93 → 照抄 0.92 的召回是 0/10，功能等于不存在；
#:   - 10 对"相似但不同"的余弦 0.55～0.87；
#:   - 231 条真实 issue 的逐条最近邻：p50=0.563 p90=0.657 p99=0.759 最高 0.893
#:     （最高的那对 element-desktop#8/#9 看着就是真重复）。
#: 选 0.83 的依据是先定好的规则：在满足两条验收线的档位里最大化 `召回 − 2×误报`
#: （误报按两倍计价，因为路线说"误杀率比准确率更硬的线"）。实测 0.83 时
#: 召回 9/10、误报 1/10，真实语料上只有 2/231 条会被贴标记。
SIMILARITY_FLAG = 0.83
#: 建议关闭的阈值（必须过闸门，绝不自动执行）
#:
#: 真实语料里**没有任何一对**达到这个值（最高 0.893），也就是说这一档在当前 embedding 下
#: 几乎不会触发 —— 这是有意的保守：误关一个用户的帖子，他不会回来解释。
#: 代价是它目前接近"死代码"；要不要换成别的判据（标题近乎相同 + 正文高度重叠），
#: 等真有重复样本进错题本时再定。
SIMILARITY_CLOSE = 0.95

LEVEL_UNIQUE = "unique"
LEVEL_MAYBE = "duplicate?"
LEVEL_DUPLICATE = "duplicate"


def judge(score: float) -> str:
    """相似度 → 等级。判等级的地方只有这一处：阈值散落必然漂移。"""
    if score >= SIMILARITY_CLOSE:
        return LEVEL_DUPLICATE
    if score >= SIMILARITY_FLAG:
        return LEVEL_MAYBE
    return LEVEL_UNIQUE


@dataclass(frozen=True)
class DedupeVerdict:
    """查重结论。`comment` 是可以直接贴出去的文案，`requires_gate` 说明要不要过闸门。"""

    level: str
    score: float = 0.0
    key: str | None = None
    comment: str = ""
    requires_gate: bool = False

    @property
    def is_duplicate(self) -> bool:
        return self.level == LEVEL_DUPLICATE

    @property
    def is_suspect(self) -> bool:
        return self.level in (LEVEL_MAYBE, LEVEL_DUPLICATE)

    def as_dict(self) -> dict[str, Any]:
        return {
            "level": self.level,
            "score": round(self.score, 4),
            "key": self.key,
            "comment": self.comment,
            "requires_gate": self.requires_gate,
        }


@dataclass(frozen=True)
class Route:
    """一个标签该往哪儿去。`requires_approval=True` 的任务**只能**由人类放行。"""

    target: str
    task_type: str | None
    requires_approval: bool
    reason: str


ROUTES: dict[str, Route] = {
    "bug": Route("repair", "fix", False, "缺陷 → 修复队列（阶段四）"),
    "feature": Route("refine", "refine", False, "功能请求 → M1 追问（可选）"),
    "question": Route("reply", None, False, "提问 → 仅回复，不排任何自动动作"),
    "spam": Route("gate", None, True, "spam 一律走人类闸门，不自动关闭"),
    "duplicate": Route("gate", None, True, "重复一律走人类闸门，不自动关闭"),
}

#: 认不出的标签 → 保守处理：不自动流转。宁可多问人类一句，也不要自动做错事。
UNKNOWN_ROUTE = Route("gate", None, True, "未知标签 → 不自动流转")


def route_label(label: str | None) -> Route:
    if not label:
        return UNKNOWN_ROUTE
    return ROUTES.get(label.lower(), UNKNOWN_ROUTE)


@dataclass(frozen=True)
class IngestResult:
    """一条新 issue 走完"查重 → （必要时）分类 → 路由"的完整结论。"""

    dedupe: DedupeVerdict
    label: str | None
    route: Route
    classified: bool
    note: str = ""
    extra: dict = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "dedupe": self.dedupe.as_dict(),
            "label": self.label,
            "route": {
                "target": self.route.target,
                "task_type": self.route.task_type,
                "requires_approval": self.route.requires_approval,
                "reason": self.route.reason,
            },
            "classified": self.classified,
            "note": self.note,
        }


def issue_text(title: str, body: str = "") -> str:
    """issue → 用于 embedding 的文本。

    标题在前、正文在后：标题是作者自己浓缩过的一句话，信息密度最高；
    bge-m3 对长文本会做截断，把标题放在前面能保证它一定进得了模型。
    """
    title = (title or "").strip()
    body = (body or "").strip()
    return f"{title}\n{body}".strip()


class Deduper:
    """把向量库、阈值、路由串起来的那一层。

    `embed_fn` 可注入：测试里换成确定性假向量，验收里用真的 bge-m3。
    """

    def __init__(
        self,
        store: VectorStore | None = None,
        embed_fn: Callable[[Sequence[str]], np.ndarray] | None = None,
    ) -> None:
        self.store = store if store is not None else VectorStore()
        self._embed_fn = embed_fn

    # ------------------------------------------------------------- embedding
    def embed(self, texts: Sequence[str]) -> np.ndarray:
        if self._embed_fn is not None:
            return np.asarray(self._embed_fn(list(texts)), dtype=float)
        from src.gateway import embed as gateway_embed

        return gateway_embed(list(texts))

    # ---------------------------------------------------------------- 建库
    def index(self, items: Iterable[tuple[str, str, dict]]) -> int:
        """批量建库：items = (key, text, meta)。已存在的 key 跳过。"""
        pending = [(key, text, meta) for key, text, meta in items if key not in self.store]
        if not pending:
            return 0
        vectors = self.embed([text for _, text, _ in pending])
        return self.store.add_many(
            [key for key, _, _ in pending],
            vectors,
            [meta for _, _, meta in pending],
        )

    # ---------------------------------------------------------------- 查重
    def check(self, text: str) -> DedupeVerdict:
        vectors = self.embed([text])
        hits: list[Match] = self.store.search(vectors[0], top_k=1)
        if not hits:
            return DedupeVerdict(level=LEVEL_UNIQUE)
        return self._verdict(hits[0])

    def check_issue(self, title: str, body: str = "") -> DedupeVerdict:
        return self.check(issue_text(title, body))

    def _verdict(self, hit: Match) -> DedupeVerdict:
        level = judge(hit.score)
        if level == LEVEL_UNIQUE:
            return DedupeVerdict(level=level, score=hit.score, key=hit.key)
        repo = hit.meta.get("repo")
        number = hit.meta.get("number")
        where = f"{repo}#{number}" if repo and number else hit.key
        if level == LEVEL_DUPLICATE:
            comment = (
                f"这条与 {where} 高度相似（相似度 {hit.score:.3f}）。"
                f"**建议关闭为重复** —— 但关闭需要维护者确认，机器人不自动关。"
            )
            return DedupeVerdict(level, hit.score, hit.key, comment, requires_gate=True)
        comment = f"这条与 {where} 看起来很像（相似度 {hit.score:.3f}），可能重复，供参考。"
        return DedupeVerdict(level, hit.score, hit.key, comment, requires_gate=False)

    # ---------------------------------------------------------------- 入站
    def ingest(
        self,
        title: str,
        body: str = "",
        *,
        classify: Callable[[], Any] | None = None,
    ) -> IngestResult:
        """
        新 issue 的入口：**先查重，再分类**（路线 3.2 子步骤 4）。

        `classify` 是一个零参回调（调用方自己闭包住 issue 对象），返回带
        `.verdict.label` 的东西（3.1 的 `TriageResult`）。这样本模块不依赖 3.1 的类型，
        但拿到了"重复的不再分类"这个顺序上的好处。

        强重复（≥0.98）**跳过分类**：重复帖通常信息不全，硬分类只会给出误导性的标签。
        """
        dedupe = self.check_issue(title, body)
        if dedupe.is_duplicate and classify is not None:
            return IngestResult(
                dedupe=dedupe,
                label=None,
                route=route_label("duplicate"),
                classified=False,
                note="强重复：跳过分类（省一次调用，也避免给信息不全的重复帖贴假标签）",
            )

        label: str | None = None
        note = ""
        if classify is not None:
            result = classify()
            verdict = getattr(result, "verdict", None)
            label = getattr(verdict, "label", None)
        elif dedupe.is_suspect:
            note = "疑似重复，但没有分类器可用"

        route = route_label(label)
        if dedupe.level == LEVEL_MAYBE and label:
            # 疑似重复但仍有明确标签：按标签路由，同时把"疑似重复"的评论带出去
            note = note or "疑似重复：按标签正常路由，同时发一条提示评论"
        return IngestResult(
            dedupe=dedupe,
            label=label,
            route=route,
            classified=classify is not None,
            note=note,
        )


def to_task(route: Route, *, repo: str, number: int, title: str = "", reason: str = "") -> Any:
    """把路由结论变成一个队列任务（1.2 的 Task）。不产生副作用，纯构造。"""
    from src.queue import Task

    if route.task_type is None:
        raise ValueError(f"路由 {route.target} 不产生任务（{route.reason}）")
    return Task(
        type=route.task_type,
        payload={"repo": repo, "number": number, "title": title, "reason": reason or route.reason},
        requires_approval=route.requires_approval,
    )
