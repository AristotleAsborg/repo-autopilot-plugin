"""M2 反馈分类器（路线阶段三）。

* `classifier.FeedbackClassifier` —— **真正的分类器**：长文压缩 → 分类 + 优先级 +
  置信度（温度 0）→ 动作分级；正文全程按不可信数据处理。
* `guard` —— 不可信输入的隔离与注入检测。
* `keywords.classify` —— 关键词**桩**，只留给 e2e 骨架用（它必须快且确定）。
  `classify` 这个短名字保留为它的别名；新代码请直接用 `FeedbackClassifier`。
"""

from .classifier import (
    CONFIDENCE_FOR_LABEL,
    FIRST_REPLY_TEMPLATES,
    SUMMARY_TARGET,
    SUMMARY_THRESHOLD,
    FeedbackClassifier,
    TriageError,
    TriageResult,
    decide_action,
    first_reply,
)
from .guard import (
    INJECTION_PATTERNS,
    UNTRUSTED_BEGIN,
    UNTRUSTED_END,
    GuardReport,
    build_untrusted_block,
    detect_injection,
    neutralise_delimiters,
    wrap_untrusted,
)
from .keywords import LABELS, Classification
from .keywords import classify as classify_keywords
from .models import (
    ACTION_CHANGE_LABEL,
    ACTION_GATE_CLOSE,
    ACTION_REPLY_ONLY,
    ACTION_SUGGEST_LABEL,
    GATED_LABELS,
    LABEL_CHOICES,
    PRIORITY_CHOICES,
    IssueSummary,
    RawIssue,
    TriageVerdict,
)

#: 兼容旧名字：e2e 骨架 import 的就是 `classify`。它就是关键词桩，不是分类器本体。
classify = classify_keywords

__all__ = [
    "ACTION_CHANGE_LABEL",
    "ACTION_GATE_CLOSE",
    "ACTION_REPLY_ONLY",
    "ACTION_SUGGEST_LABEL",
    "CONFIDENCE_FOR_LABEL",
    "FIRST_REPLY_TEMPLATES",
    "GATED_LABELS",
    "INJECTION_PATTERNS",
    "LABELS",
    "LABEL_CHOICES",
    "PRIORITY_CHOICES",
    "SUMMARY_TARGET",
    "SUMMARY_THRESHOLD",
    "UNTRUSTED_BEGIN",
    "UNTRUSTED_END",
    "Classification",
    "FeedbackClassifier",
    "GuardReport",
    "IssueSummary",
    "RawIssue",
    "TriageError",
    "TriageResult",
    "TriageVerdict",
    "build_untrusted_block",
    "classify",
    "classify_keywords",
    "decide_action",
    "detect_injection",
    "first_reply",
    "neutralise_delimiters",
    "wrap_untrusted",
]
