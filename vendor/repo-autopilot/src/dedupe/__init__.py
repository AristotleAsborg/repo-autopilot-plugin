"""3.2 查重与路由（`src/dedupe/`）。

    from src.dedupe import Deduper, VectorStore, route_label, to_task

- `store.VectorStore` —— flat numpy 向量库，落盘在 `state/vectors/`；
- `router.Deduper` —— 建库 / 查重 / 入站（先查重再分类）；
- `router.route_label` —— 标签 → 目的地（bug→修复队列、feature→M1、question→仅回复，
  spam/duplicate→人类闸门）。
"""

from .router import (
    LEVEL_DUPLICATE,
    LEVEL_MAYBE,
    LEVEL_UNIQUE,
    ROUTES,
    SIMILARITY_CLOSE,
    SIMILARITY_FLAG,
    UNKNOWN_ROUTE,
    Deduper,
    DedupeVerdict,
    IngestResult,
    Route,
    issue_text,
    judge,
    route_label,
    to_task,
)
from .store import DEFAULT_DIR, Match, VectorStore, VectorStoreError

__all__ = [
    "DEFAULT_DIR",
    "LEVEL_DUPLICATE",
    "LEVEL_MAYBE",
    "LEVEL_UNIQUE",
    "ROUTES",
    "SIMILARITY_CLOSE",
    "SIMILARITY_FLAG",
    "UNKNOWN_ROUTE",
    "DedupeVerdict",
    "Deduper",
    "IngestResult",
    "Match",
    "Route",
    "VectorStore",
    "VectorStoreError",
    "issue_text",
    "judge",
    "route_label",
    "to_task",
]
