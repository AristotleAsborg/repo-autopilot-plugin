"""3.2 查重与路由的确定性测试（不联网、不调模型）。

评测（召回/误报/性能）在 `tools/eval_dedupe.py` 里跑真模型；这里只钉死**代码契约**：

- 阈值边界（0.9199 / 0.92 / 0.9799 / 0.98）——边界错一格，后果从"多一条评论"变成"关掉用户的帖子"；
- 向量库的行序契约与去重语义（错位不会报错，只会给错答案，所以必须由测试守住）；
- 路由表：bug→修复队列、feature→M1、question→仅回复、spam/duplicate→**必须过闸门**；
- 入站顺序：**强重复跳过分类**（省一次调用，也避免给信息不全的重复帖贴假标签）；
- 5000 条库单次查询 <2s（路线 3.2 的性能线；超过这条线才需要上 pgvector）。
"""

from __future__ import annotations

import json
import math
import time

import numpy as np
import pytest

from src.dedupe import (
    LEVEL_DUPLICATE,
    LEVEL_MAYBE,
    LEVEL_UNIQUE,
    SIMILARITY_CLOSE,
    SIMILARITY_FLAG,
    Deduper,
    VectorStore,
    VectorStoreError,
    issue_text,
    judge,
    route_label,
    to_task,
)
from src.queue import Task

DIM = 8


def _unit(*values: float) -> np.ndarray:
    vector = np.zeros(DIM, dtype=float)
    for position, value in enumerate(values):
        vector[position] = value
    return vector / np.linalg.norm(vector)


class DictEmbed:
    """按文本查表返回预置向量 —— 让"相似度"在测试里是可控输入，而不是模型行为。"""

    def __init__(self, mapping: dict[str, np.ndarray]) -> None:
        self.mapping = mapping
        self.calls: list[list[str]] = []

    def __call__(self, texts: list[str]) -> np.ndarray:
        self.calls.append(list(texts))
        return np.array([self.mapping[text] for text in texts], dtype=float)


# --------------------------------------------------------------- 阈值边界

@pytest.mark.parametrize(
    ("score", "expected"),
    [
        (0.0, LEVEL_UNIQUE),
        (SIMILARITY_FLAG - 0.0001, LEVEL_UNIQUE),
        (SIMILARITY_FLAG, LEVEL_MAYBE),
        (SIMILARITY_CLOSE - 0.0001, LEVEL_MAYBE),
        (SIMILARITY_CLOSE, LEVEL_DUPLICATE),
        (0.999, LEVEL_DUPLICATE),
    ],
)
def test_judge_thresholds(score: float, expected: str) -> None:
    """
    边界用例**跟着标定出来的常数走**，不写死数字：阈值会被重新标定（路线要求按仓库标），
    写死 0.92/0.98 的测试在标定后只会变成"测试挂了、代码是对的"这种噪音。
    """
    assert judge(score) == expected


# --------------------------------------------------------------- 向量库

def test_store_add_is_idempotent_by_key() -> None:
    store = VectorStore()
    assert store.add("a", _unit(1.0)) is True
    assert store.add("a", _unit(0.0, 1.0)) is False  # 同 key 不覆盖
    assert len(store) == 1
    # 没被覆盖的硬证据：查 e0 仍然是满分
    assert store.search(_unit(1.0))[0].score == pytest.approx(1.0)


def test_store_rejects_dimension_change() -> None:
    store = VectorStore()
    store.add("a", _unit(1.0))
    with pytest.raises(VectorStoreError, match="维度不一致"):
        store.add("b", np.ones(DIM + 1, dtype=float))


def test_store_rejects_zero_vector() -> None:
    store = VectorStore()
    with pytest.raises(VectorStoreError, match="零向量"):
        store.add("a", np.zeros(DIM, dtype=float))


def test_store_save_load_round_trip_keeps_row_order(scratch) -> None:
    store = VectorStore(scratch)
    keys = ["x#1", "x#2", "x#3"]
    vectors = np.array([_unit(1.0), _unit(0.0, 1.0), _unit(0.0, 0.0, 1.0)])
    store.add_many(keys, vectors, [{"repo": "x", "number": 1}, {"repo": "x", "number": 2}, {}])
    store.save()

    loaded = VectorStore.load(scratch)
    assert loaded.keys == keys
    # 行序契约：第 2 条必须还是第 2 条，否则查重会指向错误的 issue
    assert loaded.search(_unit(0.0, 1.0))[0].key == "x#2"
    assert loaded.search(_unit(0.0, 1.0))[0].meta["number"] == 2


def test_store_load_rejects_row_count_mismatch(scratch) -> None:
    np.save(scratch / "issues.npy", np.zeros((3, DIM), dtype=np.float32))
    (scratch / "issues.jsonl").write_text(
        json.dumps({"key": "only-one", "meta": {}}) + "\n", encoding="utf-8"
    )
    with pytest.raises(VectorStoreError, match="一一对应"):
        VectorStore.load(scratch)


def test_empty_store_search_is_not_an_error() -> None:
    assert VectorStore().search(_unit(1.0)) == []


# --------------------------------------------------------------- 判定

def test_maybe_duplicate_has_comment_but_no_gate() -> None:
    store = VectorStore()
    store.add("r#7", _unit(1.0), repo="owner/repo", number=7)
    # 落在 flag 与 close 之间（不写死数字：阈值会被重新标定）
    middle = (SIMILARITY_FLAG + SIMILARITY_CLOSE) / 2
    deduper = Deduper(store, embed_fn=DictEmbed({"查": _unit(middle, math.sqrt(1 - middle**2))}))

    verdict = deduper.check("查")
    assert verdict.level == LEVEL_MAYBE
    assert verdict.requires_gate is False
    assert "owner/repo#7" in verdict.comment


def test_strong_duplicate_requires_gate() -> None:
    store = VectorStore()
    store.add("r#7", _unit(1.0), repo="owner/repo", number=7)
    deduper = Deduper(store, embed_fn=DictEmbed({"查": _unit(1.0)}))

    verdict = deduper.check("查")
    assert verdict.level == LEVEL_DUPLICATE
    assert verdict.requires_gate is True  # 建议关闭 ⇒ 必须过人类闸门
    assert verdict.is_duplicate


def test_unrelated_text_is_unique() -> None:
    store = VectorStore()
    store.add("r#7", _unit(1.0), repo="owner/repo", number=7)
    deduper = Deduper(store, embed_fn=DictEmbed({"另一个话题": _unit(0.0, 1.0)}))

    verdict = deduper.check("另一个话题")
    assert verdict.level == LEVEL_UNIQUE
    assert verdict.key == "r#7"  # 最近的邻居仍然报出来，供人判断


# --------------------------------------------------------------- 入站顺序

def test_ingest_skips_classification_for_strong_duplicate() -> None:
    store = VectorStore()
    text = issue_text("崩溃", "启动就崩")
    store.add("r#1", _unit(1.0), repo="owner/repo", number=1)
    deduper = Deduper(store, embed_fn=DictEmbed({text: _unit(1.0)}))

    calls: list[int] = []

    def classify() -> object:
        calls.append(1)
        raise AssertionError("强重复不该触发分类")

    result = deduper.ingest("崩溃", "启动就崩", classify=classify)
    assert calls == []                     # 硬证据：分类一次都没调
    assert result.classified is False
    assert result.label is None
    assert result.route.target == "gate"    # 建议关闭 → 闸门
    assert result.route.requires_approval is True


def test_ingest_classifies_and_routes_bug_to_repair_queue() -> None:
    store = VectorStore()
    text = issue_text("崩溃", "启动就崩")
    store.add("r#1", _unit(1.0), repo="owner/repo", number=1)
    deduper = Deduper(store, embed_fn=DictEmbed({text: _unit(0.0, 1.0)}))

    class Verdict:
        label = "bug"

    class Result:
        verdict = Verdict()

    result = deduper.ingest("崩溃", "启动就崩", classify=lambda: Result())
    assert result.classified is True
    assert result.label == "bug"
    assert result.route.target == "repair"
    assert result.route.task_type == "fix"
    assert result.route.requires_approval is False


def test_ingest_marks_suspect_but_still_routes_by_label() -> None:
    store = VectorStore()
    text = issue_text("崩溃", "启动就崩")
    store.add("r#1", _unit(1.0), repo="owner/repo", number=1)
    middle = (SIMILARITY_FLAG + SIMILARITY_CLOSE) / 2
    deduper = Deduper(store, embed_fn=DictEmbed({text: _unit(middle, math.sqrt(1 - middle**2))}))

    class Verdict:
        label = "feature"

    class Result:
        verdict = Verdict()

    result = deduper.ingest("崩溃", "启动就崩", classify=lambda: Result())
    assert result.dedupe.level == LEVEL_MAYBE
    assert result.label == "feature"
    assert result.route.target == "refine"
    assert "疑似重复" in result.note


# --------------------------------------------------------------- 路由表

@pytest.mark.parametrize(
    ("label", "target", "approval"),
    [
        ("bug", "repair", False),
        ("feature", "refine", False),
        ("question", "reply", False),
        ("spam", "gate", True),
        ("duplicate", "gate", True),
        ("BUG", "repair", False),      # 大小写不该影响路由
        ("flarb", "gate", True),       # 认不出的标签 → 保守，不自动流转
        (None, "gate", True),
    ],
)
def test_route_label(label: str | None, target: str, approval: bool) -> None:
    route = route_label(label)
    assert route.target == target
    assert route.requires_approval is approval


def test_to_task_carries_payload_and_no_approval_for_auto_routes() -> None:
    task = to_task(route_label("bug"), repo="owner/repo", number=42, title="崩溃")
    assert isinstance(task, Task)
    assert task.type == "fix"
    assert task.requires_approval is False
    assert task.payload["number"] == 42

    refine = to_task(route_label("feature"), repo="owner/repo", number=43)
    assert refine.type == "refine"


@pytest.mark.parametrize("label", ["question", "spam", "duplicate", "flarb", None])
def test_to_task_refuses_routes_without_an_automatic_action(label: str | None) -> None:
    """
    闸门档（spam/duplicate）和仅回复档（question）都**不产生队列任务** —— 它们要么只是
    回一句话，要么是"建议关闭"这种必须人类点头的请求（走 1.4 的闸门，不是队列）。
    这也是本系统"不自动做危险动作"的编码体现：危险动作没有任务可派。
    """
    route = route_label(label)
    assert route.task_type is None
    with pytest.raises(ValueError):
        to_task(route, repo="owner/repo", number=1)


# --------------------------------------------------------------- 性能线

def test_five_thousand_vectors_query_under_two_seconds() -> None:
    store = VectorStore()
    rng = np.random.default_rng(7)
    store.add_many([f"perf#{i}" for i in range(5000)], rng.normal(size=(5000, 256)))
    started = time.perf_counter()
    hits = store.search(rng.normal(size=256), top_k=5)
    elapsed = time.perf_counter() - started
    assert len(hits) == 5
    assert elapsed < 2.0, f"5000 条查询用了 {elapsed:.3f}s，超过 2s 门槛"
