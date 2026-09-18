"""`/一键处理` 的批量引擎（路线 7.1 的五条专项约束）。

    from src.batch import BatchState, plan, run_batch, render_sheet, apply_decisions

单次 ≤20 条、逐条落盘可续跑、单条失败不阻断批次、末尾逐条审批（批量呈现≠批量放行）、
收到 `/应急` 立即暂停。
"""

from .core import (
    APPROVALS_DIR,
    DEFAULT_CAP,
    ITEM_DONE,
    ITEM_NEEDS_HUMAN,
    ITEM_PENDING,
    ITEM_SKIPPED,
    STATE_DIR,
    BatchError,
    BatchItem,
    BatchState,
    apply_decisions,
    parse_decisions,
    pause,
    plan,
    render_sheet,
    resume,
    run_batch,
    write_sheet,
)

__all__ = [
    "APPROVALS_DIR",
    "DEFAULT_CAP",
    "ITEM_DONE",
    "ITEM_NEEDS_HUMAN",
    "ITEM_PENDING",
    "ITEM_SKIPPED",
    "STATE_DIR",
    "BatchError",
    "BatchItem",
    "BatchState",
    "apply_decisions",
    "parse_decisions",
    "pause",
    "plan",
    "render_sheet",
    "resume",
    "run_batch",
    "write_sheet",
]
