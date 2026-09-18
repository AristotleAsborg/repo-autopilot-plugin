"""任务 schema（路线 1.2 子步骤 1）。

路线给的字段：`{id: uuid, type: str, payload: dict, retry_count: int=0,
created_at: iso8601, requires_approval: bool=false}`

在路线基础上增加两个字段，理由写在各自注释里——都不是可有可无的装饰，
而是 1.2 验收（并发无重复、kill -9 后孤儿回收）**必需**的信息：

  * `recovery_count` —— 孤儿回收次数。见下方说明，它与 `retry_count` 必须分开。
  * `claimed_by` / `claimed_at` —— 认领者身份，孤儿判定要靠它。

另有一个由调用方填、但默认空的安全相关字段：
  * `requires_approval` —— 路线已给，透传给 1.4 的闸门。

## 为什么孤儿回收不能复用 retry_count

`fail()` 用 `retry_count` 判是否放弃（<3 回 pending，否则进 failed）。
而孤儿回收的语义完全不同：进程被杀**不代表任务本身失败**——可能是机器重启、
可能是部署，任务代码一行都没跑。

若回收时也递增 `retry_count`，那么一个每次都被 kill 的健康任务会在 3 次重启后
被误判为"失败"永久丢弃。所以分开计数：`retry_count` 只记真实的失败，
`recovery_count` 记被回收的次数。后者仍设上限（防无限循环），
超限时进 failed 并写明原因是"反复被回收"而非"任务失败"。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, field_validator


class TaskState(str, Enum):
    """任务在队列中的四个位置，与 state/tasks/ 下的四个子目录一一对应。"""

    PENDING = "pending"
    DOING = "doing"
    DONE = "done"
    FAILED = "failed"


class Task(BaseModel):
    """一个队列任务。落盘形式为 state/tasks/<state>/<id>.json。"""

    model_config = {"extra": "forbid"}

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    type: str = Field(..., min_length=1, description="任务类型，如 triage / fix / scout")
    payload: dict[str, Any] = Field(default_factory=dict)
    retry_count: int = Field(default=0, ge=0)
    created_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(),
        description="ISO8601 UTC。用于 dequeue 的『取最早件』排序",
    )
    requires_approval: bool = False

    # --- 1.2 验收必需 ---
    recovery_count: int = Field(
        default=0,
        ge=0,
        description="孤儿回收次数。与 retry_count 分开：进程被杀不等于任务失败",
    )
    claimed_by: str | None = Field(default=None, description="认领者的 worker 标识（pid@host）")
    claimed_at: str | None = Field(default=None, description="认领时间 ISO8601，用于诊断")

    @field_validator("created_at")
    @classmethod
    def _validate_created_at(cls, v: str) -> str:
        """
        必须是可解析的 ISO8601，否则 dequeue 的排序会静默错乱。

        这里选择「解析失败即拒收」而不是「容错跳过」：一个时间戳坏掉的任务
        如果被放过，排序会把它排到任意位置，问题会在很久以后以难以复现的方式显现。
        """
        try:
            datetime.fromisoformat(v)
        except ValueError as exc:
            raise ValueError(f"created_at 不是合法 ISO8601: {v!r}") from exc
        return v

    @property
    def sort_key(self) -> datetime:
        """dequeue 用：取最早件。带时区归一，避免 naive/aware 混比抛异常。"""
        dt = datetime.fromisoformat(self.created_at)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
