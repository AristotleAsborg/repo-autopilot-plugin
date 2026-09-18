"""取证：两个进程抢同一件时，究竟是谁、在什么时刻，把文件移到了哪里。

纸面上的互斥完全依赖「同一文件系统内 rename 原子，且目标存在时 rename 失败」。
`test_two_processes_cannot_claim_same_task` 实测两个进程都认领成功，
所以第一步必须先验证这条前提在本机是否成立，再看真实时间线。

用法：python .cache/double-claim-probe.py
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.queue import Task, TaskQueue, TaskState

PY_REAL = getattr(sys, "_base_executable", sys.executable)
ENV = {**os.environ, "PYTHONPATH": os.pathsep.join(p for p in sys.path if p)}
WORKER = ROOT / "tests" / "queue_worker.py"


def probe_rename(directory: Path) -> str:
    """验证 os.rename 在目标已存在时的真实行为。"""
    a = directory / "a.json"
    b = directory / "b.json"
    a.write_text("A", encoding="utf-8")
    b.write_text("B", encoding="utf-8")
    try:
        os.rename(a, b)
    except FileExistsError as exc:
        return f"FileExistsError —— 符合纸面假设（目标存在则失败）: {exc}"
    except OSError as exc:
        return f"其他 OSError: {type(exc).__name__}: {exc}"
    return f"**竟然成功覆盖**：b 现在={b.read_text(encoding='utf-8')!r}，a 还在={a.exists()}"


def listing(state: Path) -> str:
    out = []
    for state_enum in TaskState:
        names = sorted(p.name for p in (state / "tasks" / state_enum.value).glob("*.json"))
        out.append(f"{state_enum.value}={names}")
    return " ".join(out)


def main() -> int:
    base = ROOT / ".cache" / "double-claim"
    if base.exists():
        shutil.rmtree(base, ignore_errors=True)
    base.mkdir(parents=True, exist_ok=True)

    print("sys.executable  =", sys.executable)
    print("PY_REAL         =", PY_REAL)
    print()
    print("== 1. os.rename 在目标已存在时的行为 ==")
    print("   ", probe_rename(base))
    print()
    print("== 2. 两个进程抢同一件，跑 5 轮 ==")

    for round_no in range(5):
        state = base / f"r{round_no}"
        state.mkdir(parents=True, exist_ok=True)
        queue = TaskQueue(state)
        task = queue.enqueue(Task(type="triage", payload={"round": round_no}))

        records: list[Path] = []
        procs: list[subprocess.Popen] = []
        for index in range(2):
            record = state / f"record-{index}.json"
            records.append(record)
            procs.append(
                subprocess.Popen(
                    [PY_REAL, str(WORKER), "--state-dir", str(state), "--record", str(record)],
                    env=ENV,
                    cwd=str(ROOT),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            )
        for proc in procs:
            proc.wait(timeout=60)

        claimed: list[str] = []
        workers: list[str] = []
        for record in records:
            if record.exists():
                payload = json.loads(record.read_text(encoding="utf-8"))
                claimed.extend(payload["claimed"])
                workers.append(payload["worker"])

        verdict = {0: "无人认领", 1: "恰好一个"}.get(len(claimed), "**重复认领**")
        print(f"  轮{round_no}: 认领数={len(claimed)} {verdict} 认领记录={claimed}")
        print(f"        worker 标识={workers}")
        print(f"        期望 id={task.id}")
        print(f"        {listing(state)}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
