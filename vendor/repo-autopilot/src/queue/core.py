"""任务队列（路线 1.2）。

四个原子操作 + 孤儿回收，全部基于 `os.rename` 的原子性。

## 并发安全靠什么

**不要用 rename 做互斥。** 路线原文把「rename 是原子操作」当作并发安全的基础，
但 2026-09-12 用零项目代码的最小复现证明它在真并发下不成立：
两个进程同时 `os.rename(同一个 src, 同一个 dst)`，40 轮里 21 轮**两个都成功**
（`tools/probes/rename_race2.py`）。顺序调用时语义正常，只有并发失效。
替代品 `os.open(..., O_CREAT | O_EXCL)` 与 `os.mkdir()` 在同样压力下 30/30 轮
恰好一个成功（`tools/probes/claim_race.py`）。

本实现的做法：
  1. `enqueue` 先写临时文件再 rename 成 `<id>.json` —— 防半写（写到一半被 kill
     时，pending/ 里不会出现半个 JSON）。
  2. `dequeue` **先用 `O_CREAT|O_EXCL` 原子占坑**（`doing/<id>.claim`），
     占到了才把 `pending/x.json` rename 成 `doing/x.json`。
     rename 在这里只负责搬运，不承担互斥职责。
  3. `complete` / `fail` 用 rename 做状态迁移，并删掉坑文件释放任务。

## 孤儿判定

`dequeue` 时把认领者写进任务自身的 `claimed_by`，同时在 `doing/<id>.claim`
留一个旁证文件。回收时读任务里的 `claimed_by`，取其中的 pid，
用 `ProcessLookupError` 判活。**不靠 claim 文件的 mtime 猜超时**——
超时法会把"跑得慢但健康"的任务误回收，而它一旦被回收就可能被第二个 worker
重复执行。宁可少回收，不可误回收。
"""

from __future__ import annotations

import errno
import json
import os
import socket
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from pydantic import ValidationError

from .schema import Task, TaskState

# 真实失败的重试上限（路线：retry_count<3 回 pending，否则进 failed）
MAX_RETRIES = 3

# 孤儿回收次数上限。与 MAX_RETRIES 分开：反复被 kill 不等于任务失败，
# 但也不能无限循环，超限时以「反复被回收」为由进 failed。
MAX_RECOVERIES = 5

# 被回收的任务允许它自己已经失败过的次数（两者独立累加，互不消耗）
_RECOVERABLE = "recoverable"


class QueueError(RuntimeError):
    """队列操作失败。绝不静默吞掉。"""


def worker_id() -> str:
    """当前 worker 标识：pid@host。孤儿判定要靠它拿回 pid。"""
    return f"{os.getpid()}@{socket.gethostname()}"


def _pid_of(claimed_by: str | None) -> int | None:
    """从 'pid@host' 里取 pid。取不到返回 None（视为不可判活）。"""
    if not claimed_by or "@" not in claimed_by:
        return None
    head = claimed_by.split("@", 1)[0]
    try:
        return int(head)
    except ValueError:
        return None


def _pid_alive(pid: int) -> bool:
    """
    判断进程是否还活着。

    ## Windows 上**不能**用 `os.kill(pid, 0)`

    只要还有人持有该进程的句柄（测试里 `Popen` 对象没被回收就会一直持有），
    进程即使早已退出，pid 依然可以被 `OpenProcess` 打开，于是「已被强杀的 worker」
    会被判成活着，孤儿任务永远回收不了 ——
    `test_kill9_then_recover_no_double_consume` 就是这么失败的。

    正确做法是问内核退出码：`GetExitCodeProcess` 返回 `STILL_ACTIVE(259)` 才算活着。
    打不开时分两种：`ERROR_ACCESS_DENIED` 说明进程存在但无权（仍算活着），
    其余（如 `ERROR_INVALID_PARAMETER`）说明进程不存在。
    """
    if os.name == "nt":
        return _pid_alive_windows(pid)

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError as exc:
        # 其他 OSError（如对不存在进程抛的 EINVAL）按不存在处理
        return exc.errno not in (errno.ESRCH, errno.EINVAL)
    return True


_STILL_ACTIVE = 259
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_ERROR_ACCESS_DENIED = 5


def _pid_alive_windows(pid: int) -> bool:
    """Windows 版判活。ctypes 在这里是必要的：os.kill 的语义查不出退出码。"""
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

    handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ctypes.get_last_error() == _ERROR_ACCESS_DENIED
    try:
        code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            # 查不出退出码时保守当作活着：宁可少回收，不可误回收
            return True
        return code.value == _STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------- 瞬时冲突
#
# Windows 上「另一个进程/线程正持有这个文件」是**瞬时**错误，不是逻辑错误。
# 20 线程抢同一件时的真实现场（2026-09-12 实测）：
#   A 线程正在 read_text 这个 pending 文件；B 线程的 os.rename 需要源文件的
#   删除权，于是 B 收到 WinError 32「另一个程序正在使用此文件」。
#   谁都没做错，只是需要重试。
#
# 为什么必须重试而不是报错：报错会把一次正常的并发竞争升级成 dequeue 失败，
# 上层会把它当成真实故障。为什么不能无限重试：占用可能是真的（外部程序长期
# 持有），所以有次数上限；超限后如实抛出或跳过，绝不假装成功。
_RETRY_ATTEMPTS = 20
_RETRY_SLEEP = 0.005

# 认领坑（doing/<id>.claim）的宽限期。
# 坑文件创建与「主人标识写进去」之间有一个极短窗口；在这个窗口里竞争者必须
# 当成「被占用」，绝不能当成死坑。只有坑存在超过这个秒数仍读不出活主人，
# 才认定是「占坑后被杀」留下的死坑。见 _drop_dead_claim。
_CLAIM_GRACE_SECONDS = 30.0


def _read_text_retry(path: Path) -> str:
    """读文件，对瞬时共享冲突重试。文件不存在时如实抛 FileNotFoundError。"""
    last: Exception | None = None
    for _ in range(_RETRY_ATTEMPTS):
        try:
            return path.read_text(encoding="utf-8")
        except PermissionError as exc:
            last = exc
            time.sleep(_RETRY_SLEEP)
    assert last is not None
    raise last


def _rename_retry(source: Path, target: Path) -> None:
    """
    os.rename，对瞬时共享冲突重试。

    刻意**不**重试 FileExistsError / FileNotFoundError：那两个是互斥语义的
    正常结果（别人已抢占 / 文件已被移走），必须原样抛给调用方处理。
    """
    last: Exception | None = None
    for _ in range(_RETRY_ATTEMPTS):
        try:
            os.rename(source, target)
            return
        except PermissionError as exc:
            last = exc
            time.sleep(_RETRY_SLEEP)
    assert last is not None
    raise last


def _replace_retry(source: Path, target: Path) -> None:
    """os.replace，对瞬时共享冲突重试（原子落盘的最后一跳）。"""
    last: Exception | None = None
    for _ in range(_RETRY_ATTEMPTS):
        try:
            os.replace(source, target)
            return
        except PermissionError as exc:
            last = exc
            time.sleep(_RETRY_SLEEP)
    assert last is not None
    raise last


def _unlink_retry(path: Path) -> None:
    """
    删除文件，对瞬时共享冲突重试；文件本来就不在时静默返回。

    为什么必须重试：别的线程/进程可能正**读**着这个文件（例如判断坑主人生死），
    而 Windows 上删除需要独占，于是失败码是 WinError 32 而不是 FileNotFoundError。
    这在 20 线程用例里会直接把 dequeue 打成错误，属于把正常竞争当成故障。
    """
    last: Exception | None = None
    for _ in range(_RETRY_ATTEMPTS):
        try:
            path.unlink()
            return
        except FileNotFoundError:
            return
        except PermissionError as exc:
            last = exc
            time.sleep(_RETRY_SLEEP)
    assert last is not None
    raise last


class TaskQueue:
    """基于文件系统的任务队列。所有路径都从 state_dir 派生。"""

    def __init__(self, state_dir: str | Path, *, worker: str | None = None) -> None:
        self.state_dir = Path(state_dir)
        self.tasks_dir = self.state_dir / "tasks"
        self.reports_dir = self.state_dir / "reports"
        self.worker = worker or worker_id()
        for sub in (TaskState.PENDING, TaskState.DOING, TaskState.DONE, TaskState.FAILED):
            (self.tasks_dir / sub.value).mkdir(parents=True, exist_ok=True)
        self.reports_dir.mkdir(parents=True, exist_ok=True)

    # ---------------------------------------------------------------- paths

    def path_for(self, task_id: str, state: TaskState) -> Path:
        return self.tasks_dir / state.value / f"{task_id}.json"

    def claim_path(self, task_id: str) -> Path:
        return self.tasks_dir / TaskState.DOING.value / f"{task_id}.claim"

    def _write_atomic(self, path: Path, text: str) -> None:
        """
        先写临时文件再 rename。临时文件名带 uuid，避免两个写入者撞名。

        半写防护是这里唯一的目的：进程在写 JSON 的中途被杀，pending/ 里
        只会留下一个 .tmp 残骸，而不会留下一个解析不了的 `<id>.json`。
        """
        tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        tmp.write_text(text, encoding="utf-8")
        # replace 在 Windows 上也允许覆盖目标；对杀软/索引器造成的瞬时占用重试
        _replace_retry(tmp, path)

    # ------------------------------------------------------------- 原子操作

    def enqueue(self, task: Task) -> Task:
        """写入 pending/<id>.json。同 id 重复入队视为错误（防止静默覆盖）。"""
        target = self.path_for(task.id, TaskState.PENDING)
        if target.exists():
            raise QueueError(f"任务 {task.id} 已在 pending 中，拒绝重复入队")

        # 已存在于其他状态也拒绝——同 id 出现两次会让"无重复消费"无法验证
        for state in (TaskState.DOING, TaskState.DONE, TaskState.FAILED):
            if self.path_for(task.id, state).exists():
                raise QueueError(f"任务 {task.id} 已存在于 {state.value}，拒绝重复入队")

        self._write_atomic(target, task.model_dump_json(indent=2))
        return task

    def _read_candidate(self, path: Path) -> Task | None:
        """
        读取候选任务。文件损坏时隔离它并返回 None，绝不猜测内容。

        隔离而非删除：损坏原因需要可追溯，直接删掉等于销毁证据。
        """
        try:
            text = _read_text_retry(path)
        except FileNotFoundError:
            return None   # 别人已把它移走，这不是错误
        except OSError as exc:
            # 读不动 ≠ 内容坏。**绝不能在这里隔离**：隔离会把一个健康任务搬进
            # failed/，还会留下一个假的「损坏」报告。记一笔然后跳过。
            self.reports_dir.joinpath(f"queue_unreadable_{path.stem}.md").write_text(
                f"# 任务文件暂时读不了\n\n文件: {path}\n\n错误:\n```\n{exc}\n```\n\n"
                "（未被隔离：读取失败不等同于内容损坏）\n",
                encoding="utf-8",
            )
            return None

        try:
            return Task.model_validate_json(text)
        except (ValidationError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            quarantine = self.tasks_dir / TaskState.FAILED.value / path.name
            try:
                if quarantine.exists():
                    quarantine = quarantine.with_name(f"{path.stem}.corrupt-{uuid.uuid4().hex[:8]}.json")
                _replace_retry(path, quarantine)
            except OSError:
                pass
            self.reports_dir.joinpath(f"queue_corrupt_{path.stem}.md").write_text(
                f"# 损坏的任务文件\n\n文件: {path}\n\n已隔离到: {quarantine}\n\n"
                f"解析错误:\n```\n{exc}\n```\n",
                encoding="utf-8",
            )
            return None

    # 一次 dequeue 最多完整解析多少个候选。
    # 必要性：早期版本对 pending 下**每个文件**都做完整 JSON 解析。在突发入队场景
    # （producer 高频写入）下每轮成本线性增长，形成越取越慢的死循环——实测把测试
    # 挂住过两次。改为先用 stat 便宜排序、只解析最旧的若干候选。
    # 取值远大于"同时抢占的并发 worker 数"，因此不会漏掉真正的队首。
    _PARSE_BUDGET = 16

    def dequeue(self) -> Task | None:
        """
        取最早的任务并原子移入 doing/。队列为空返回 None。

        排序策略（性能与正确性的取舍，写清楚以免后人"优化"错方向）：
          1. 用 `st_mtime_ns` 做**便宜**粗排。文件写入顺序 ≡ 入队顺序，
             正常情况下 mtime 序与 created_at 序一致。
          2. 只对最旧的 `_PARSE_BUDGET` 个候选做完整解析，取其中 created_at 最小者
             开始认领。

        为什么不用纯 mtime：路线要求"取最早件"是按 created_at 语义。
        为什么不全量解析：见 `_PARSE_BUDGET` 注释。
        两者结合：正常情况零差异；即便有人手工回填旧任务（mtime 新、created_at 旧），
        只要它落在预算内就仍会被正确选中。
        """
        # 每轮重新列目录：任务可能在上一轮之后被别的 worker 投进来
        for _ in range(3):
            pending_dir = self.tasks_dir / TaskState.PENDING.value

            # 第一遍：只 stat，拿廉价候选序
            try:
                statted = sorted(
                    ((p.stat().st_mtime_ns, p) for p in pending_dir.glob("*.json")),
                    key=lambda item: item[0],
                )
            except FileNotFoundError:
                return None

            if not statted:
                return None

            # 第二遍：只解析最旧的若干候选
            parsed: list[tuple[datetime, Path, Task]] = []
            for _, path in statted[: self._PARSE_BUDGET]:
                task = self._read_candidate(path)
                if task is None:
                    continue   # 损坏文件已隔离
                parsed.append((task.sort_key, path, task))

            if not parsed:
                # 候选全是损坏文件且已隔离，再看一轮有没有新的
                continue
            parsed.sort(key=lambda item: item[0])
            claimed = self._claim(parsed)
            if claimed is not None:
                return claimed
            # 候选全被别人抢走，重扫一轮
        return None

    def _claim(self, parsed: list[tuple[datetime, Path, Task]]) -> Task | None:
        """
        对候选按序尝试认领：**先原子占坑，再搬文件**。

        ## 为什么不靠 rename 互斥（2026-09-12 的教训）

        原设计指望「Windows 上目标已存在时 rename 失败」天然互斥。用零项目代码的
        最小复现推翻了它（`tools/probes/rename_race2.py`，40 轮）：
            两个进程同时 `os.rename(同一个 src, 同一个 dst)`
            → **21 轮两个都返回成功**，19 轮恰好一个成功。
        顺序调用时 `FileExistsError` 语义仍然正常，**只有真并发失效**——
        所以单元测试和代码审查都看不见它。后果是同一件任务被两个 worker
        同时认领，重复消费。

        ## 现在怎么做

        占坑用内核保证原子的 `CreateFile(CREATE_NEW)`：
            `os.open(doing/<id>.claim, O_CREAT | O_EXCL | O_WRONLY)`
        实测同样压力下 30/30 轮恰好一个成功（`tools/probes/claim_race.py`）。
        占坑成功才去 `pending → doing` 搬文件；rename 从此只负责搬运，不负责互斥。
        """
        for _, path, task in parsed:
            claim = self.claim_path(task.id)
            if not self._take_claim_slot(claim):
                continue   # 别人拥有（或坑主人还活着），换下一个候选

            target = self.path_for(task.id, TaskState.DOING)
            try:
                _rename_retry(path, target)
            except (FileNotFoundError, FileExistsError, PermissionError):
                # 搬不动就把坑还回去：绝不能留下一个没有对应任务的坑，
                # 那会让这件任务在宽限期内谁都拿不到。
                self._release_claim(claim)
                continue

            task.claimed_by = self.worker
            task.claimed_at = _now()
            self._write_atomic(target, task.model_dump_json(indent=2))
            return task
        return None

    def _take_claim_slot(self, claim: Path) -> bool:
        """
        原子占坑：成功返回 True（坑归我），失败返回 False（**让路，不是报错**）。

        `O_CREAT | O_EXCL` 落到内核是 `CreateFile(CREATE_NEW)`，由文件系统保证
        「已存在则失败」是原子的 —— 这正是 rename 在本机做不到的那件事。

        坑已存在时分两种情况，否则「占坑后、搬文件前被杀」会把任务永久卡死：
          * 主人还活着（或坑太新，来不及判断）→ 真被占用，让路；
          * 主人已不存在且坑已过宽限期     → 死坑，清掉再抢一次。

        **2026-09-15 CI 实测（2 核 runner 上 20 线程用例）**：`CREATE_NEW` 在 Windows 上
        还有**第二种失败形态** —— 坑文件正被别人持有（或刚被删、句柄还没落）时，拿到的是
        `ERROR_SHARING_VIOLATION` → **`PermissionError`**，而不是「已存在」。
        它同样只是"这次没抢到"，不是故障；原来没接这一支，于是它一路冒到 `dequeue()`
        外面，把一次正常竞争升级成出队失败（CI 因此红在
        `test_20_threads_100_tasks_no_loss_no_duplicate`）。
        现在按 `_RETRY_ATTEMPTS` 做**有界重试**（与 `_rename_retry` 同一口径），
        重试用尽仍抢不到就返回 False 让路 —— 任务留在 `pending`，下一轮 `dequeue()` 还会看见它。
        """
        for _ in range(2):
            handle = self._open_claim_slot(claim)
            if handle is not None:
                with os.fdopen(handle, "w", encoding="utf-8") as stream:
                    stream.write(self.worker)
                return True
            if self._drop_dead_claim(claim):
                continue   # 死坑已清掉，再抢一次
            return False
        return False

    def _open_claim_slot(self, claim: Path) -> int | None:
        """
        尝试独占创建坑文件：成功返回 handle，抢不到返回 None（**从不抛**）。

        两种"抢不到"在调用方眼里含义相同（这次没抢到），区别只在要不要顺手清死坑：
          * `FileExistsError` —— 坑真的在；
          * `PermissionError` —— 瞬时共享冲突（Windows 专有），重试到上限仍不行就是
            "现在抢不到"；**不抛**，因为把正常竞争抛出去会让上层把它当真实故障。
        """
        for _ in range(_RETRY_ATTEMPTS):
            try:
                return os.open(str(claim), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                return None
            except PermissionError:
                time.sleep(_RETRY_SLEEP)
        return None

    def _drop_dead_claim(self, claim: Path) -> bool:
        """
        坑的主人已不是活进程时删掉这个坑；返回「是否删掉了」。

        刻意保守：读不出主人且坑还很新时返回 False（当成被占用）。
        理由见 `_CLAIM_GRACE_SECONDS`。
        """
        try:
            stat = claim.stat()
        except FileNotFoundError:
            return True   # 已经没了，外面可以重试
        except OSError:
            return False

        try:
            owner = claim.read_text(encoding="utf-8").strip()
        except OSError:
            owner = ""

        pid = _pid_of(owner) if owner else None
        if pid is not None:
            if _pid_alive(pid):
                return False
            self._release_claim(claim)
            return True

        if time.time() - stat.st_mtime < _CLAIM_GRACE_SECONDS:
            return False   # 太新，可能是别人刚创建还没来得及写标识

        self._release_claim(claim)
        return True

    def _release_claim(self, claim: Path) -> None:
        """
        释放认领坑。**绝不因为释放失败而让调用方报错。**

        坑没删掉最坏的结果是这件任务在宽限期内没人能拿，宽限期一到会被自动清理；
        而抛错会把一次正常的并发竞争升级成调用方可见的故障
        —— 20 线程用例里就是这样把 `dequeue` 打成错误的（别的线程正读着这个坑文件，
        Windows 上删除需要独占，于是 WinError 32）。
        """
        try:
            _unlink_retry(claim)
        except OSError as exc:
            self.reports_dir.joinpath(f"queue_stale_claim_{claim.stem}.md").write_text(
                f"# 认领坑释放失败\n\n坑文件: {claim}\n\n错误:\n```\n{exc}\n```\n\n"
                "（放宽限期后会被自动清理；此文件仅用于留痕）\n",
                encoding="utf-8",
            )

    def complete(self, task_id: str) -> Task:
        """doing → done。"""
        return self._transition(task_id, TaskState.DOING, TaskState.DONE)

    def _transition(self, task_id: str, frm: TaskState, to: TaskState) -> Task:
        source = self.path_for(task_id, frm)
        if not source.exists():
            raise QueueError(f"任务 {task_id} 不在 {frm.value}，无法迁移到 {to.value}")
        # 先读、后 rename：**绝不在 rename 之后再读目标文件**。
        # Windows 上刚 rename 过的文件仍可能被杀软/索引器短暂持有，紧接着
        # read_text 会随机抛 PermissionError —— 20 线程用例里就是这样稳定
        # 炸掉一个 consumer 线程的（PytestUnhandledThreadExceptionWarning）。
        task = Task.model_validate_json(_read_text_retry(source))
        target = self.path_for(task_id, to)
        _rename_retry(source, target)
        self._release_claim(self.claim_path(task_id))
        return task

    def fail(self, task_id: str, reason: str = "") -> tuple[Task, TaskState]:
        """
        失败处理：retry_count < MAX_RETRIES 则回 pending，否则进 failed 并写报告。

        返回 (任务, 最终所在状态)，便于调用方判断是重试还是放弃。
        """
        source = self.path_for(task_id, TaskState.DOING)
        if not source.exists():
            raise QueueError(f"任务 {task_id} 不在 doing，无法标记失败")

        task = Task.model_validate_json(source.read_text(encoding="utf-8"))
        task.retry_count += 1
        task.claimed_by = None
        task.claimed_at = None

        if task.retry_count < MAX_RETRIES:
            target = self.path_for(task_id, TaskState.PENDING)
            self._write_atomic(target, task.model_dump_json(indent=2))
            source.unlink()
            self._release_claim(self.claim_path(task_id))
            return task, TaskState.PENDING

        self._write_failure_report(task, reason, kind="任务失败")
        target = self.path_for(task_id, TaskState.FAILED)
        self._write_atomic(target, task.model_dump_json(indent=2))
        source.unlink()
        self._release_claim(self.claim_path(task_id))
        return task, TaskState.FAILED

    def _write_failure_report(self, task: Task, reason: str, *, kind: str) -> None:
        """路线要求：进 failed 时写 reports/queue_failure_{id}.md。"""
        path = self.reports_dir / f"queue_failure_{task.id}.md"
        path.write_text(
            "\n".join(
                [
                    f"# 队列失败报告（{kind}）",
                    "",
                    f"- 任务 id   : `{task.id}`",
                    f"- 类型      : `{task.type}`",
                    f"- 真实失败数: {task.retry_count} / {MAX_RETRIES}",
                    f"- 被回收次数: {task.recovery_count} / {MAX_RECOVERIES}",
                    f"- 创建时间  : {task.created_at}",
                    f"- 需人类审批: {task.requires_approval}",
                    f"- 最后认领者: {task.claimed_by or '(无)'}",
                    "",
                    "## payload",
                    "",
                    "```json",
                    json.dumps(task.payload, ensure_ascii=False, indent=2),
                    "```",
                    "",
                    "## 原因",
                    "",
                    reason or "(未提供)",
                    "",
                ]
            ),
            encoding="utf-8",
        )

    # ------------------------------------------------------------ 孤儿回收

    def recover_orphans(self, *, dry_run: bool = False) -> list[dict]:
        """
        启动时调用：扫描 doing/，把认领进程已不存在的任务重投 pending。

        判定用 pid 存活，不用超时。理由见模块 docstring。

        返回每条的处置记录，便于测试与诊断。
        """
        records: list[dict] = []
        doing = self.tasks_dir / TaskState.DOING.value

        for path in sorted(doing.glob("*.json")):
            task = self._read_candidate(path)
            if task is None:
                records.append({"id": path.stem, "action": "quarantined"})
                continue

            pid = _pid_of(task.claimed_by)
            # claimed_by 缺失（例如手工放进 doing 的文件）视为不可判活 → 回收，
            # 否则这类任务会永远卡住
            alive = _pid_alive(pid) if pid is not None else False
            if alive:
                records.append({"id": task.id, "action": "kept", "pid": pid})
                continue

            task.recovery_count += 1

            if task.recovery_count > MAX_RECOVERIES:
                self._write_failure_report(
                    task,
                    f"反复被回收（{task.recovery_count - 1} 次）仍未完成——"
                    "疑似任务本身会让进程崩溃，或 worker 反复被强杀。"
                    "注意这与『任务逻辑失败』不同，请人工确认。",
                    kind="反复被孤儿回收",
                )
                os.replace(path, self.path_for(task.id, TaskState.FAILED))
                self._release_claim(self.claim_path(task.id))
                records.append({"id": task.id, "action": "failed", "recoveries": task.recovery_count})
                continue

            task.claimed_by = None
            task.claimed_at = None
            if dry_run:
                records.append({"id": task.id, "action": "would-requeue", "pid": pid})
                continue

            target = self.path_for(task.id, TaskState.PENDING)
            self._write_atomic(target, task.model_dump_json(indent=2))
            path.unlink()
            self._release_claim(self.claim_path(task.id))
            records.append({"id": task.id, "action": "requeued", "pid": pid, "recoveries": task.recovery_count})

        # 清理没有对应任务的坑文件。
        # 两种来源：一是「占坑后、搬文件前被杀」留下的死坑（要清，否则任务卡住）；
        # 二是别的 worker 此刻正处在占坑与搬文件之间（**不能清**，会破坏互斥）。
        # 所以这里只清「已死」或「已过宽限期」的坑，判据与认领路径共用。
        for claim in sorted(doing.glob("*.claim")):
            task_file = claim.with_suffix(".json")
            if task_file.exists():
                continue
            self._drop_dead_claim(claim)

        return records

    # -------------------------------------------------------------- 观测面

    def stats(self) -> dict[str, int]:
        """各状态任务数。用于诊断与测试断言。"""
        out: dict[str, int] = {}
        for state in TaskState:
            out[state.value] = len(list((self.tasks_dir / state.value).glob("*.json")))
        return out

    def ids_in(self, state: TaskState) -> set[str]:
        """某状态下的全部任务 id。验收要用它断言『无丢失无重复』。"""
        return {p.stem for p in (self.tasks_dir / state.value).glob("*.json")}
