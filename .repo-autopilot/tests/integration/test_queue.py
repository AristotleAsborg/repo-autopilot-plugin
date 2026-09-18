"""步骤 1.2 验收：任务队列的单元与集成测试。

路线的验收原文：
  「集成测试——并发 20 线程 enqueue/dequeue 100 个任务，断言无丢失无重复；
    dequeue 中途 kill -9 进程，重启后孤儿任务被回收，无重复消费。」

本文件把这条拆成三类：
  A. 单元：schema 校验、排序、状态迁移、重试上限、损坏隔离
  B. 并发：20 线程 100 任务；两个真实进程抢同一件
  C. 崩溃恢复：真实子进程被强杀 → 孤儿回收 → 且不重复消费

测试里刻意没有放宽任何断言。若某条通不过，正确的动作是修实现或如实报 BLOCKED。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from src.queue import (
    MAX_RECOVERIES,
    MAX_RETRIES,
    QueueError,
    Task,
    TaskQueue,
    TaskState,
)

WORKER = ROOT / "tests" / "queue_worker.py"

# 为什么不能直接拿 sys.executable 起 worker：
# 本机 venv 的 python.exe 在 uv 下是**转发器**，它把真解释器另起成子进程。
# 实测：Popen.pid=11068，而子进程里 os.getpid()=10748，两者必然不等。
# 后果有两个，都会让测试说谎：
#   1. `dead_pid == proc.pid` 断言的是转发器的 pid，永远不成立；
#   2. 「kill -9 掉 worker」变得含糊 —— 杀转发器不等于杀到真解释器。
# 所以 worker 一律用**真解释器**启动，并把当前 sys.path 经 PYTHONPATH 传过去
# （venv 的 site-packages 只是个普通目录，PYTHONPATH 能直接解析）。
# 这样 Popen.pid 就是 worker 自己的 pid，原断言重新变得有效。
PY_REAL = getattr(sys, "_base_executable", sys.executable)
WORKER_ENV = {**os.environ, "PYTHONPATH": os.pathsep.join(p for p in sys.path if p)}
PY = PY_REAL

# state_dir 夹具由 tests/conftest.py 提供（不用 pytest 的 tmp_path，
# 原因见 conftest 的模块注释）。


def make_task(task_type: str = "triage", created_at: str | None = None, **kw) -> Task:
    payload = {"n": kw.pop("n", 0)}
    if created_at:
        payload["_created"] = created_at
    return Task(type=task_type, payload=payload, created_at=created_at or datetime.now(timezone.utc).isoformat(), **kw)


# ============================================================ A. 单元

class TestSchema:
    def test_defaults(self) -> None:
        t = Task(type="triage")
        assert t.retry_count == 0
        assert t.recovery_count == 0
        assert t.requires_approval is False
        assert t.claimed_by is None
        assert t.payload == {}
        assert len(t.id) == 36   # uuid4 字符串长度

    def test_rejects_bad_created_at(self) -> None:
        """时间戳坏掉会让 dequeue 排序静默错乱，必须拒收。"""
        # 断言**具体**异常类型，不用 `pytest.raises(Exception)`：后者连"拼错属性名造成的
        # AttributeError"也算通过，等于把校验器测空（实测：pydantic 抛的是 ValidationError）。
        with pytest.raises(ValidationError):
            Task(type="triage", created_at="not-a-date")

    def test_rejects_unknown_field(self) -> None:
        """extra=forbid：拼错字段名要立刻报错，而不是被静默忽略。"""
        with pytest.raises(ValidationError):
            Task(type="triage", typo_field=1)

    def test_sort_key_handles_naive_datetime(self) -> None:
        """naive 与 aware 混比会抛异常；sort_key 必须归一。"""
        naive = Task(type="t", created_at="2026-01-01T00:00:00")
        aware = Task(type="t", created_at="2026-01-01T00:00:01+00:00")
        assert naive.sort_key < aware.sort_key


class TestBasicOps:
    def test_enqueue_then_dequeue(self, state_dir: Path) -> None:
        q = TaskQueue(state_dir)
        t = q.enqueue(make_task())
        assert q.stats()["pending"] == 1

        got = q.dequeue()
        assert got is not None and got.id == t.id
        assert q.stats() == {"pending": 0, "doing": 1, "done": 0, "failed": 0}
        assert got.claimed_by == q.worker      # 认领者必须落盘，孤儿判定要靠它

    def test_dequeue_empty_returns_none(self, state_dir: Path) -> None:
        assert TaskQueue(state_dir).dequeue() is None

    def test_fifo_by_created_at(self, state_dir: Path) -> None:
        """『取最早件』按 created_at，而不是文件名或写入顺序。"""
        q = TaskQueue(state_dir)
        base = datetime.now(timezone.utc)
        old = q.enqueue(make_task(created_at=(base - timedelta(hours=2)).isoformat(), n=1))
        mid = q.enqueue(make_task(created_at=(base - timedelta(hours=1)).isoformat(), n=2))
        new = q.enqueue(make_task(created_at=base.isoformat(), n=3))

        order = [q.dequeue().id for _ in range(3)]
        assert order == [old.id, mid.id, new.id]

    def test_complete_moves_to_done(self, state_dir: Path) -> None:
        q = TaskQueue(state_dir)
        t = q.enqueue(make_task())
        q.dequeue()
        done = q.complete(t.id)
        assert done.id == t.id
        assert q.stats() == {"pending": 0, "doing": 0, "done": 1, "failed": 0}
        assert not q.claim_path(t.id).exists()   # claim 旁证要清掉

    def test_duplicate_enqueue_rejected(self, state_dir: Path) -> None:
        """同 id 重复入队会让『无重复消费』无法验证，必须拒。"""
        q = TaskQueue(state_dir)
        t = make_task()
        q.enqueue(t)
        with pytest.raises(QueueError):
            q.enqueue(t)

    def test_complete_missing_task_raises(self, state_dir: Path) -> None:
        with pytest.raises(QueueError):
            TaskQueue(state_dir).complete("no-such-id")


class TestFailAndRetry:
    def test_retry_until_limit_then_failed(self, state_dir: Path) -> None:
        q = TaskQueue(state_dir)
        t = q.enqueue(make_task())

        # 前 MAX_RETRIES-1 次失败回 pending
        for expected in range(1, MAX_RETRIES):
            q.dequeue()
            task, where = q.fail(t.id, reason=f"第 {expected} 次")
            assert where == TaskState.PENDING
            assert task.retry_count == expected

        # 第 MAX_RETRIES 次失败进 failed
        q.dequeue()
        task, where = q.fail(t.id, reason="最后一次")
        assert where == TaskState.FAILED
        assert task.retry_count == MAX_RETRIES
        assert q.stats()["failed"] == 1

    def test_failure_report_written(self, state_dir: Path) -> None:
        """路线要求进 failed 时写 reports/queue_failure_{id}.md。"""
        q = TaskQueue(state_dir)
        t = q.enqueue(make_task(task_type="fix"))
        for _ in range(MAX_RETRIES):
            q.dequeue()
            q.fail(t.id, reason="测试用原因")

        report = q.reports_dir / f"queue_failure_{t.id}.md"
        assert report.exists()
        text = report.read_text(encoding="utf-8")
        assert t.id in text
        assert "fix" in text
        assert "测试用原因" in text

    def test_fail_clears_claim(self, state_dir: Path) -> None:
        """重试的任务必须清掉认领信息，否则它会被误判为『有主』。"""
        q = TaskQueue(state_dir)
        t = q.enqueue(make_task())
        q.dequeue()
        task, _ = q.fail(t.id)
        assert task.claimed_by is None
        assert task.claimed_at is None


class TestCorruption:
    def test_corrupt_file_quarantined_not_crashed(self, state_dir: Path) -> None:
        """
        损坏的任务文件不能拖垮队列：隔离它、写报告、继续处理其他任务。

        隔离而非删除——损坏原因需要可追溯。
        """
        q = TaskQueue(state_dir)
        good = q.enqueue(make_task(n=9))
        bad = state_dir / "tasks" / "pending" / "broken.json"
        bad.write_text("{ this is not json", encoding="utf-8")

        got = q.dequeue()
        assert got is not None and got.id == good.id, "损坏文件不应阻止好任务被取出"
        assert not bad.exists(), "损坏文件应被移出 pending"
        assert (q.reports_dir / "queue_corrupt_broken.md").exists()

    def test_half_written_tmp_is_ignored(self, state_dir: Path) -> None:
        """enqueue 的半写残骸是 .tmp，不该被当成任务。"""
        q = TaskQueue(state_dir)
        t = q.enqueue(make_task())
        (state_dir / "tasks" / "pending" / ".half.json.abc.tmp").write_text("{", encoding="utf-8")
        got = q.dequeue()
        assert got is not None and got.id == t.id


# ============================================================ B. 并发

class TestConcurrency:
    def test_20_threads_100_tasks_no_loss_no_duplicate(self, state_dir: Path) -> None:
        """
        路线验收：并发 20 线程 enqueue/dequeue 100 个任务，断言无丢失无重复。

        20 个线程同时入队 5 个、同时出队并完成，最后核对：
          * 100 个任务全部到达 done（无丢失）
          * 每个任务恰好被一个线程取到（无重复消费）
        """
        q = TaskQueue(state_dir)
        total = 100
        threads_n = 20
        per_thread = total // threads_n

        enqueue_errors: list[str] = []
        dequeue_errors: list[str] = []
        seen: list[str] = []
        seen_lock = threading.Lock()

        def producer(tid: int) -> None:
            for i in range(per_thread):
                try:
                    q.enqueue(Task(type="triage", payload={"producer": tid, "i": i}))
                except Exception as exc:  # noqa: BLE001
                    enqueue_errors.append(f"{type(exc).__name__}: {exc}")

        def consumer() -> None:
            while True:
                try:
                    task = q.dequeue()
                except Exception as exc:  # noqa: BLE001
                    dequeue_errors.append(f"{type(exc).__name__}: {exc}")
                    return
                if task is None:
                    return
                with seen_lock:
                    seen.append(task.id)
                q.complete(task.id)

        producers = [threading.Thread(target=producer, args=(i,)) for i in range(threads_n)]
        for th in producers:
            th.start()
        for th in producers:
            th.join()

        consumers = [threading.Thread(target=consumer) for _ in range(threads_n)]
        for th in consumers:
            th.start()
        for th in consumers:
            th.join()

        assert not enqueue_errors, f"入队报错: {enqueue_errors[:3]}"
        assert not dequeue_errors, f"出队报错: {dequeue_errors[:3]}"

        stats = q.stats()
        assert stats["done"] == total, f"应有 {total} 个任务完成，实际 {stats}"
        assert stats["pending"] == 0 and stats["doing"] == 0, f"不应残留: {stats}"

        assert len(seen) == total, f"取出次数应恰为 {total}，实际 {len(seen)}"
        assert len(set(seen)) == total, f"出现重复消费：{total - len(set(seen))} 个任务被取了多次"

        done_ids = q.ids_in(TaskState.DONE)
        assert len(done_ids) == total
        assert done_ids == set(seen)

    def test_claim_sharing_violation_is_retried_not_raised(self, state_dir: Path, monkeypatch) -> None:
        r"""
        **2026-09-15 CI 实测的回归**：`os.open(O_CREAT|O_EXCL)` 在 Windows 上还有一种
        失败形态 —— 坑文件正被别人持有（或刚被删、句柄还没落）时拿到的是
        `ERROR_SHARING_VIOLATION` → **PermissionError**，而不是「已存在」。

        原来没接这一支，它一路冒到 `dequeue()` 外面，把一次正常竞争升级成出队失败：
        CI 就红在 `test_20_threads_100_tasks_no_loss_no_duplicate`
        （`PermissionError: ... tasks\doing\<id>.claim`）。

        这条用例**确定性地**复现那个机制：让前 3 次 `os.open` 抛共享冲突，
        真实的第 4 次照常执行 —— 期望 dequeue 仍然拿到任务，而不是抛。
        """
        import src.queue.core as queue_core

        q = TaskQueue(state_dir)
        task = q.enqueue(make_task())

        real_open = os.open
        calls = {"n": 0}

        def flaky_open(path, flags, *args, **kwargs):
            if str(path).endswith(".claim") and calls["n"] < 3:
                calls["n"] += 1
                raise PermissionError(13, "另一个程序正在使用此文件")
            return real_open(path, flags, *args, **kwargs)

        monkeypatch.setattr(queue_core.os, "open", flaky_open)
        got = q.dequeue()
        assert calls["n"] == 3, "必须真的撞上三次共享冲突，否则这条用例没验到东西"
        assert got is not None and got.id == task.id, "瞬时共享冲突必须重试，不能把出队打成失败"

    def test_claim_sharing_violation_that_never_clears_yields_a_task_not_an_error(
        self, state_dir: Path, monkeypatch
    ) -> None:
        """
        共享冲突**持续整段重试预算**时也必须不抛：返回 None（这次没抢到，让路），
        任务**留在 pending**（不丢），下一轮还能取到。

        这就是"有界重试 + 让路"与"无限重试"的区别：占用可能是真的（别人长期持有），
        所以超限后如实返回"没取到"，绝不假装成功、也绝不把竞争报成故障。
        """
        import src.queue.core as queue_core

        q = TaskQueue(state_dir)
        task = q.enqueue(make_task())

        real_open = os.open

        def always_conflict(path, flags, *args, **kwargs):
            if str(path).endswith(".claim"):
                raise PermissionError(13, "另一个程序正在使用此文件")
            return real_open(path, flags, *args, **kwargs)

        monkeypatch.setattr(queue_core.os, "open", always_conflict)
        assert q.dequeue() is None, "抢不到就该让路（None），不许把竞争抛成失败"

        monkeypatch.undo()
        assert q.stats()["pending"] == 1, "任务必须还在 pending —— 让路不等于丢件"
        again = q.dequeue()
        assert again is not None and again.id == task.id, "冲突过去之后必须还能取到它"

    def test_two_processes_cannot_claim_same_task(self, state_dir: Path) -> None:
        """
        两个真实进程同时抢同一件任务：恰好一个成功。

        这是 rename 原子性的直接验证。做法：只放一个任务，拉起两个 worker，
        各自把认领到的 id 写进自己的记录文件；期望合计恰好 1 条认领记录。
        """
        q = TaskQueue(state_dir)
        t = q.enqueue(make_task())

        records = []
        procs = []
        for i in range(2):
            rec = state_dir / f"record-{i}.json"
            records.append(rec)
            procs.append(
                subprocess.Popen(
                    [PY, str(WORKER), "--state-dir", str(state_dir), "--record", str(rec)],
                    env=WORKER_ENV,
                    cwd=str(ROOT),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            )
        for p in procs:
            p.wait(timeout=60)

        claimed: list[str] = []
        for rec in records:
            if rec.exists():
                claimed.extend(json.loads(rec.read_text(encoding="utf-8"))["claimed"])

        assert claimed == [t.id], f"应恰好一个进程认领到它，实际 {claimed}"

    def test_burst_enqueue_is_not_partially_read(self, state_dir: Path) -> None:
        """
        边入队边出队时，读到的任务必须是完整 JSON（半写防护的直接验证）。

        做法：一个线程入队、另一个线程出队并校验解析结果。任何一条读到半写文件
        都会触发 ValidationError。

        刻意给 producer 加节流：不加的话它以远超 consumer 的速率灌入，
        pending 迅速堆积上千，本测试要验的是**并发下的完整性**而不是吞吐极限；
        无限灌入只会把断言变成"谁先跑完"的竞态，并让 consumer 每轮成本线性增长。
        """
        q = TaskQueue(state_dir)
        stop = threading.Event()
        problems: list[str] = []
        produced = 0
        lock = threading.Lock()

        def producer() -> None:
            nonlocal produced
            i = 0
            while not stop.is_set():
                try:
                    q.enqueue(Task(type="triage", payload={"i": i}))
                except Exception as exc:  # noqa: BLE001
                    problems.append(f"enqueue {type(exc).__name__}: {exc}")
                    return
                i += 1
                with lock:
                    produced = i
                time.sleep(0.001)

        def consumer() -> None:
            while not stop.is_set() or q.stats()["pending"] > 0:
                try:
                    task = q.dequeue()
                except Exception as exc:  # noqa: BLE001
                    problems.append(f"dequeue {type(exc).__name__}: {exc}")
                    return
                if task is None:
                    continue
                # 关键断言：解析出来的 payload 必须完整（半写会在这里炸）
                if not isinstance(task.payload.get("i"), int):
                    problems.append(f"payload 不完整: {task.payload}")
                    return
                q.complete(task.id)

        pt = threading.Thread(target=producer)
        ct = threading.Thread(target=consumer)
        pt.start()
        ct.start()
        time.sleep(1.5)
        stop.set()
        pt.join(timeout=30)
        # consumer 负责排空队列。给足时间但有上限，避免测试挂死。
        ct.join(timeout=120)
        assert not ct.is_alive(), "consumer 未在 120s 内排空队列——可能有死循环"

        # **兜底排空**（2026-09-12 在 CI 上实测偶发 `pending=1, done=704`）：
        # 生产者与消费者之间有一个**测试自身**的竞态 —— 生产者已经通过了
        # `while not stop.is_set()` 的判断、正准备 enqueue 最后一个任务时，
        # 消费者可能刚好看到"stop 已置位且队列为空"而先退出，那最后一个任务就留在 pending。
        # 生产者随后 join 成功、消费者也 join 成功，于是断言看到 pending=1。
        # 本机（多核、快）几乎撞不上，CI 的 2 核偶发 —— 这是**测试的假设太强**，不是队列丢了任务：
        # 队列里那个任务完好无损，只是"没人再去取"。所以由测试自己取干净，再断言不丢不重。
        drained = 0
        while True:
            leftover = q.dequeue()
            if leftover is None:
                break
            if not isinstance(leftover.payload.get("i"), int):
                problems.append(f"payload 不完整（兜底排空时发现）: {leftover.payload}")
            q.complete(leftover.id)
            drained += 1

        assert not problems, f"并发读写发现问题: {problems[:3]}"
        stats = q.stats()
        assert stats["pending"] == 0 and stats["doing"] == 0, f"队列未排空: {stats}（兜底排空了 {drained} 个）"
        assert stats["done"] == produced, f"完成 {stats['done']} 个，但入队了 {produced} 个"
        assert q.stats()["pending"] == 0


# ============================================================ C. 崩溃恢复

def _wait_for(predicate, timeout: float = 30.0, interval: float = 0.1) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


class TestOrphanRecovery:
    def test_kill9_then_recover_no_double_consume(self, state_dir: Path) -> None:
        """
        路线验收：dequeue 中途 kill -9，重启后孤儿任务被回收，无重复消费。

        步骤：
          1. 起真实子进程认领任务后阻塞等待被杀
          2. kill -9（SIGKILL 等价物，Windows 上为 TerminateProcess）
          3. 断言任务留在 doing，认领者的 pid 已死
          4. 启动"新进程"执行 recover_orphans
          5. 断言任务回到 pending，且 recovery_count 增加、**retry_count 不变**
             （进程被杀不是任务失败，不该消耗重试预算）
          6. 断言它能被重新取出，且 doing 里没有重复
        """
        q = TaskQueue(state_dir)
        t = q.enqueue(make_task())

        record = state_dir / "crash-record.json"
        proc = subprocess.Popen(
            [PY, str(WORKER), "--state-dir", str(state_dir), "--record", str(record), "--hold"],
            cwd=str(ROOT),
            env=WORKER_ENV,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            assert _wait_for(lambda: record.exists()), "worker 未在超时内认领任务"
            rec = json.loads(record.read_text(encoding="utf-8"))
            assert rec["claimed"] == [t.id], f"worker 应认领 {t.id}，实际 {rec['claimed']}"

            # 确认此刻它确实在 doing
            assert q.path_for(t.id, TaskState.DOING).exists()

            # 强杀：Windows 上 terminate() 即 TerminateProcess，不执行任何清理代码
            proc.kill()
            proc.wait(timeout=30)
            assert proc.returncode != 0, "被强杀的进程不应正常退出"

            # 任务仍在 doing，且认领者已死
            assert q.path_for(t.id, TaskState.DOING).exists(), "被强杀后任务应滞留 doing"
            stuck = Task.model_validate_json(q.path_for(t.id, TaskState.DOING).read_text(encoding="utf-8"))
            assert stuck.claimed_by is not None
            dead_pid = int(stuck.claimed_by.split("@")[0])
            assert dead_pid == proc.pid

            # 新进程启动时的回收
            recovered = q.recover_orphans()
            requeued = [r for r in recovered if r["id"] == t.id]
            assert requeued and requeued[0]["action"] == "requeued", f"应被回收: {recovered}"

            assert not q.path_for(t.id, TaskState.DOING).exists()
            assert q.path_for(t.id, TaskState.PENDING).exists()

            back = Task.model_validate_json(q.path_for(t.id, TaskState.PENDING).read_text(encoding="utf-8"))
            assert back.recovery_count == 1, "回收次数应记 1"
            assert back.retry_count == 0, "进程被杀不是任务失败，retry_count 必须保持 0"
            assert back.claimed_by is None, "回收后必须清掉认领者，否则会再次被误判为有主"

            # 重新取出：恰好一次
            again = q.dequeue()
            assert again is not None and again.id == t.id
            assert q.dequeue() is None, "不应还能再取出同一任务"
        finally:
            if proc.poll() is None:
                proc.kill()

    def test_live_worker_is_not_recovered(self, state_dir: Path) -> None:
        """
        持有者是活进程时绝不能回收。

        这是本模块最重要的安全性质：误回收会导致同一任务被两个 worker 同时执行。
        """
        q = TaskQueue(state_dir)
        t = q.enqueue(make_task())

        record = state_dir / "live-record.json"
        proc = subprocess.Popen(
            [PY, str(WORKER), "--state-dir", str(state_dir), "--record", str(record), "--hold"],
            cwd=str(ROOT),
            env=WORKER_ENV,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            assert _wait_for(lambda: record.exists())
            records = q.recover_orphans()
            kept = [r for r in records if r["id"] == t.id]
            assert kept and kept[0]["action"] == "kept", f"活进程持有的任务不该被回收: {records}"
            assert q.path_for(t.id, TaskState.DOING).exists()
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=30)

    def test_recovery_limit_moves_to_failed_with_distinct_reason(self, state_dir: Path) -> None:
        """
        反复被回收超过上限时进 failed，但报告里要写明原因是『反复被回收』，
        而不是笼统的『任务失败』——两者对人类的含义完全不同。
        """
        q = TaskQueue(state_dir)
        t = q.enqueue(make_task())

        for _ in range(MAX_RECOVERIES):
            q.dequeue()
            # 手工清掉 doing 里的认领者，伪造"进程已死"
            p = q.path_for(t.id, TaskState.DOING)
            task = Task.model_validate_json(p.read_text(encoding="utf-8"))
            task.claimed_by = "999999@nowhere"
            q._write_atomic(p, task.model_dump_json(indent=2))
            q.recover_orphans()

        # 再死一次即超限
        q.dequeue()
        p = q.path_for(t.id, TaskState.DOING)
        task = Task.model_validate_json(p.read_text(encoding="utf-8"))
        task.claimed_by = "999999@nowhere"
        q._write_atomic(p, task.model_dump_json(indent=2))
        records = q.recover_orphans()

        assert any(r["id"] == t.id and r["action"] == "failed" for r in records), f"应进 failed: {records}"
        report = q.reports_dir / f"queue_failure_{t.id}.md"
        assert report.exists()
        assert "反复被孤儿回收" in report.read_text(encoding="utf-8")

    def test_orphan_claim_files_cleaned(self, state_dir: Path) -> None:
        """claim 旁证文件若失去对应任务，应被清掉，否则会越积越多。"""
        q = TaskQueue(state_dir)
        stray = q.claim_path("ghost-task")
        stray.write_text("123@host", encoding="utf-8")
        q.recover_orphans()
        assert not stray.exists()

    def test_manual_file_in_doing_is_recovered(self, state_dir: Path) -> None:
        """
        claimed_by 缺失的任务视为不可判活 → 回收。

        否则手工放进 doing 的文件会永远卡住，且没人知道为什么。
        """
        q = TaskQueue(state_dir)
        t = make_task()
        (state_dir / "tasks" / "doing").joinpath(f"{t.id}.json").write_text(
            t.model_dump_json(indent=2), encoding="utf-8"
        )
        records = q.recover_orphans()
        assert any(r["id"] == t.id and r["action"] == "requeued" for r in records)
        assert q.path_for(t.id, TaskState.PENDING).exists()
