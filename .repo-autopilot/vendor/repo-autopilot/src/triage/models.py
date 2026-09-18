"""M2 的数据模型（路线 3.1）。

`RawIssue` 里的 `labels` 是**仓库自己的标签**，回放评测时当作 ground truth 用。
它带着一个已知的偏差（0.2 记录过）：维护者的标签习惯与"这条 issue 到底是什么"
并不总是一致，所以准确率只应被当作参考线，不是真值。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

LABEL_CHOICES = ("bug", "feature", "question", "duplicate", "spam")
PRIORITY_CHOICES = ("P0", "P1", "P2", "P3")

Label = Literal["bug", "feature", "question", "duplicate", "spam"]
Priority = Literal["P0", "P1", "P2", "P3"]

#: 会被判为"要关闭"的标签 —— 一律走人类闸门，无论置信度多高
GATED_LABELS = ("spam", "duplicate")

#: 动作分级（路线 3.1 子步骤 3）
ACTION_CHANGE_LABEL = "change_label"
ACTION_SUGGEST_LABEL = "suggest_label"
ACTION_GATE_CLOSE = "gate_close"
ACTION_REPLY_ONLY = "reply_only"


class RawIssue(BaseModel):
    repo: str = ""
    number: int = 0
    title: str
    body: str = ""
    comments: list[str] = Field(default_factory=list)
    labels: list[str] = Field(default_factory=list)
    state: str = "open"

    @property
    def text(self) -> str:
        parts = [self.title, self.body, *self.comments]
        return "\n".join(part for part in parts if part)

    @property
    def length(self) -> int:
        return len(self.text)

    def as_fields(self) -> dict[str, str]:
        """给隔离块用的字段顺序。标题在前，因为标题里的注入往往更直接。"""
        fields = {"title": self.title, "body": self.body}
        for index, comment in enumerate(self.comments, start=1):
            fields[f"comment-{index}"] = comment
        return fields


class IssueSummary(BaseModel):
    """长 issue 的压缩结果（路线 3.1 子步骤 1）。"""

    summary: str = Field(description="不超过 200 字的事实性摘要，不要加入评价")


class TriageVerdict(BaseModel):
    """分类器必须返回的结构。`confidence` 是 0~1 的小数，温度 0 下才有可比性。"""

    label: Label
    priority: Priority = "P2"
    confidence: float = Field(ge=0.0, le=1.0)
    reason: str = ""
    duplicate_of: int | None = None

    @field_validator("duplicate_of", mode="before")
    @classmethod
    def _blank_means_none(cls, value: object) -> object:
        """
        把「不适用」的几种写法统一成 None。

        实测：模型很爱给可空字段填 `""`、`"none"`、`"null"`、`0`。
        这不是它在胡闹，是"这个字段我没有值"的自然表达。
        为此丢弃一整条分类结果不划算，所以在这里收口 —— 但要明确：
        **只有这一种宽容**，label / confidence 这些有实义的字段依旧严格。
        """
        if value is None:
            return None
        if isinstance(value, str):
            text = value.strip().lower()
            if text in ("", "none", "null", "n/a", "-"):
                return None
            if text.isdigit():
                return int(text)
            return value
        if isinstance(value, int) and value == 0:
            return None
        return value


__all__ = [
    "ACTION_CHANGE_LABEL",
    "ACTION_GATE_CLOSE",
    "ACTION_REPLY_ONLY",
    "ACTION_SUGGEST_LABEL",
    "GATED_LABELS",
    "LABEL_CHOICES",
    "PRIORITY_CHOICES",
    "IssueSummary",
    "Label",
    "Priority",
    "RawIssue",
    "TriageVerdict",
]
