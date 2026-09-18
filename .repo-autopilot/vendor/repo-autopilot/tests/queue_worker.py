"""队列的辅助 worker 进程，供验收测试拉起。

为什么要真的起进程：路线 1.2 的验收要求「dequeue 中途 kill -9 进程，重启后
孤儿任务被回收，无重复消费」。这件事无法用线程或 monkeypatch 替代——
必须有一个真实的、能被强杀的进程，且它的 pid 要写进任务里。

设计约定（都是为了让测试能可靠断言）：
  * 只接受 `--state-dir` 与两个输出文件路径，其余靠它们传递信息
    （不用 stdout 回传：受限环境下管道 stdio 可能被拒）
  * `--record` 写 JSON：本进程的 worker 标识与它认领到的任务 id
  * `--hold` 认领后阻塞在 stdin 上，直到被强杀——模拟"干到一半被杀"

用法（测试里调用）：
  python -m tests.queue_worker --state-dir <dir> --record <file> --hold
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.queue import TaskQueue, TaskState


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--record", required=True, help="记录 worker 标识与认领到的任务 id")
    parser.add_argument("--hold", action="store_true", help="认领后阻塞，等待被强杀")
    parser.add_argument("--loop", type=int, default=0, help="连续取 N 次任务，每次取完即完成")
    args = parser.parse_args()

    queue = TaskQueue(args.state_dir)
    record: dict = {"worker": queue.worker, "claimed": [], "completed": []}

    if args.loop:
        # 模拟一个正常 worker：反复取任务并完成
        for _ in range(args.loop):
            task = queue.dequeue()
            if task is None:
                break
            record["claimed"].append(task.id)
            queue.complete(task.id)
            record["completed"].append(task.id)
    else:
        task = queue.dequeue()
        if task is not None:
            record["claimed"].append(task.id)

    # 先落盘再接续，确保被强杀后测试仍能读到认领记录
    Path(args.record).write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")

    if args.hold:
        # 阻塞等 stdin。测试会 kill -9 掉本进程，于是 doing/ 里留下孤儿任务。
        # 故意不用 sleep：阻塞在 read 上不会因超时自己退出，状态确定。
        sys.stdin.read()

    # 非 hold 模式下把已认领但未完成的任务标记完成（单任务模式）
    if not args.hold and not args.loop:
        for tid in list(record["claimed"]):
            if queue.path_for(tid, TaskState.DOING).exists():
                queue.complete(tid)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
