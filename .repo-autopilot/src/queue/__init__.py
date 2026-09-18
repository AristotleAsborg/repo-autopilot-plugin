"""任务队列包（路线 1.2）。

对外只暴露三样东西：Task 数据结构、TaskQueue 操作面、以及两个上限常量。
内部实现细节（临时文件、claim 旁证、损坏隔离）不对外泄漏。
"""

from .core import MAX_RECOVERIES, MAX_RETRIES, QueueError, TaskQueue, worker_id
from .schema import Task, TaskState

__all__ = [
    "MAX_RECOVERIES",
    "MAX_RETRIES",
    "QueueError",
    "Task",
    "TaskQueue",
    "TaskState",
    "worker_id",
]
