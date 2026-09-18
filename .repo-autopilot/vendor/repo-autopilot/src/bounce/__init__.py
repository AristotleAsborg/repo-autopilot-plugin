"""3.3 标签打回通路（`src/bounce/`）。

    from src.bounce import Bounce, BounceReason, precheck, record_bounce, relabel_decision

- `core` —— 打回原因/来源的枚举与校验、任务文件上的记账与状态机
  （`triaged ↔ fixing`、≥2 次 → `needs_human`）、开工前预检、动作分级（改标签走 1.4 闸门）；
- `adjudicated` —— 错题本 `state/corpus/triage-adjudicated.jsonl`（只增不改）
  与"裁决过的样本从 holdout 移入 dev"的 cohort 迁移。

一句话记住这个模块的立场：**打回不是删除，记账不等于改真值。**
"""

from .adjudicated import (
    ADJUDICATED_PATH,
    REPLAY_PATH,
    VERDICTS,
    Adjudication,
    AdjudicationError,
    append,
    cohort_counts,
    iter_truths,
    latest_verdicts,
    load,
    migrate_cohorts,
    truth_map,
)
from .core import (
    BOUNCE_SOURCES,
    MAX_BOUNCES,
    PHASE_FIXING,
    PHASE_NEEDS_HUMAN,
    PHASE_TRIAGED,
    PRECHECK_CONFIDENCE,
    RELABEL_CONFIDENCE,
    Bounce,
    BounceError,
    BounceOutcome,
    BounceReason,
    apply_bounce,
    bounce_rates,
    bounces_of,
    execute_relabel,
    find_task,
    load_task,
    phase_of,
    precheck,
    record_bounce,
    relabel_decision,
    request_relabel,
    save_task,
)

__all__ = [
    "ADJUDICATED_PATH",
    "BOUNCE_SOURCES",
    "MAX_BOUNCES",
    "PHASE_FIXING",
    "PHASE_NEEDS_HUMAN",
    "PHASE_TRIAGED",
    "PRECHECK_CONFIDENCE",
    "RELABEL_CONFIDENCE",
    "REPLAY_PATH",
    "VERDICTS",
    "Adjudication",
    "AdjudicationError",
    "Bounce",
    "BounceError",
    "BounceOutcome",
    "BounceReason",
    "append",
    "apply_bounce",
    "bounce_rates",
    "bounces_of",
    "cohort_counts",
    "execute_relabel",
    "find_task",
    "iter_truths",
    "latest_verdicts",
    "load",
    "load_task",
    "migrate_cohorts",
    "phase_of",
    "precheck",
    "record_bounce",
    "relabel_decision",
    "request_relabel",
    "save_task",
    "truth_map",
]
