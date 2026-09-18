"""取证 2：给两个竞争进程装上「文件系统操作追踪」，抓一次重复认领的现场。

用法：
  python .cache/trace_claim.py driver [--hold]
  python .cache/trace_claim.py worker ...

已知现场（第一次抓到）：
    0 pid=12716 rename pending/x.json -> doing/x.json OK
    1 pid=32052 rename pending/x.json -> doing/x.json OK   <- 目标已存在，居然也成功
这与「目标存在时 os.rename 必然 FileExistsError」的纸面假设冲突，
所以本版继续加厚取证：每次调用前后都记录 src/dst 是否存在，并记录调用栈。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

PY_REAL = getattr(sys, "_base_executable", sys.executable)
ENV = {**os.environ, "PYTHONPATH": os.pathsep.join(p for p in sys.path if p)}

_TRACE: Path | None = None
_real_rename = os.rename
_real_replace = os.replace


def _frames() -> list[str]:
    stack = traceback.extract_stack()[:-2]
    return [f"{Path(f.filename).name}:{f.lineno}:{f.name}" for f in stack[-5:]]


def _log(op: str, src, dst, outcome: str, extra: dict) -> None:
    if _TRACE is None:
        return
    payload = {
        "t": time.time_ns(),
        "pid": os.getpid(),
        "op": op,
        "src": str(src),
        "dst": str(dst),
        "outcome": outcome,
        "caller": _frames(),
    }
    payload.update(extra)
    with open(_TRACE, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")


def rename_traced(src, dst, *args, **kwargs):
    extra = {"src_exists": os.path.exists(src), "dst_exists": os.path.exists(dst)}
    try:
        _real_rename(src, dst, *args, **kwargs)
    except BaseException as exc:
        _log("rename", src, dst, f"{type(exc).__name__}/winerr={getattr(exc, 'winerror', None)}", extra)
        raise
    _log("rename", src, dst, "OK", extra)


def replace_traced(src, dst, *args, **kwargs):
    extra = {"src_exists": os.path.exists(src), "dst_exists": os.path.exists(dst)}
    try:
        _real_replace(src, dst, *args, **kwargs)
    except BaseException as exc:
        _log("replace", src, dst, f"{type(exc).__name__}/winerr={getattr(exc, 'winerror', None)}", extra)
        raise
    _log("replace", src, dst, "OK", extra)


os.rename = rename_traced
os.replace = replace_traced

from src.queue import Task, TaskQueue, TaskState


def run_worker(argv: list[str]) -> int:
    global _TRACE
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--record", required=True)
    parser.add_argument("--trace", required=True)
    parser.add_argument("--hold", action="store_true")
    args = parser.parse_args(argv)
    _TRACE = Path(args.trace)

    queue = TaskQueue(args.state_dir)
    record = {"worker": queue.worker, "claimed": []}
    task = queue.dequeue()
    if task is not None:
        record["claimed"].append(task.id)
    Path(args.record).write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
    if args.hold:
        sys.stdin.read()
    if not args.hold:
        for task_id in record["claimed"]:
            if queue.path_for(task_id, TaskState.DOING).exists():
                queue.complete(task_id)
    return 0


def _one_round(base: Path, round_no: int, hold: bool) -> tuple[list[str], list[str]]:
    state = base / f"r{round_no}"
    state.mkdir(parents=True, exist_ok=True)
    trace = state / "trace.jsonl"
    trace.write_text("", encoding="utf-8")

    queue = TaskQueue(state)
    queue.enqueue(Task(type="triage", payload={"round": round_no}))

    procs = []
    records = []
    for index in range(2):
        record = state / f"record-{index}.json"
        records.append(record)
        argv = [
            PY_REAL,
            str(Path(__file__).resolve()),
            "worker",
            "--state-dir",
            str(state),
            "--record",
            str(record),
            "--trace",
            str(trace),
        ]
        if hold:
            argv.append("--hold")
        procs.append(
            subprocess.Popen(
                argv,
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
    return claimed, workers


def _print_scene(state: Path, claimed: list[str], workers: list[str]) -> None:
    trace = state / "trace.jsonl"
    print("\n=== 复现现场 ===")
    print(f"认领记录 = {claimed}")
    print(f"worker 标识 = {workers}")
    print("\n--- 合并时间线（追加顺序 = 真实发生顺序） ---")
    for index, line in enumerate(trace.read_text(encoding="utf-8").splitlines()):
        if not line.strip():
            continue
        event = json.loads(line)
        src = f"{Path(event['src']).parent.name}/{Path(event['src']).name}"
        dst = f"{Path(event['dst']).parent.name}/{Path(event['dst']).name}"
        print(
            f"{index:3d} pid={event['pid']:<7} {event['op']:<8} {src} -> {dst}  {event['outcome']}"
            f"  [调用前 src存在={event.get('src_exists')} dst存在={event.get('dst_exists')}]"
        )
        print(f"      调用栈: {' < '.join(event.get('caller', []))}")
    print("\n--- 目录 ---")
    for state_enum in TaskState:
        names = sorted(p.name for p in (state / "tasks" / state_enum.value).glob("*"))
        print(f"  {state_enum.value}: {names}")


def run_driver() -> int:
    hold = "--hold" in sys.argv
    base = ROOT / ".cache" / "trace-claim2"
    if base.exists():
        shutil.rmtree(base, ignore_errors=True)
    base.mkdir(parents=True, exist_ok=True)

    for round_no in range(16):
        claimed, workers = _one_round(base, round_no, hold)
        flag = "重复认领!" if len(claimed) > 1 else ("恰好一个" if len(claimed) == 1 else "无人认领")
        print(f"轮{round_no:2d}: 认领数={len(claimed)} {flag}")
        if len(claimed) > 1:
            _print_scene(base / f"r{round_no}", claimed, workers)
            return 0
    print("\n16 轮内未复现")
    return 0


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "worker":
        raise SystemExit(run_worker(sys.argv[2:]))
    raise SystemExit(run_driver())
