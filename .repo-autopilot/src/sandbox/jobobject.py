"""Windows 作业对象（Job Object）：内存上限、进程数上限、随作业关闭杀整棵树。

## 为什么不是 resource.setrlimit

路线 1.5 的降级方案写的是 `venv + resource.setrlimit`。但 `resource` 是
**Unix-only**，Windows 上 `import resource` 直接失败 —— 那条路在本机根本不存在。
Windows 的等价物是作业对象，而且在"限制内存"和"杀掉整棵进程树"这两件事上更强：
子进程再往外生孙子，也仍然在同一个作业里。

## 边界（必须说清楚，不能假装有）

作业对象管的是**资源**，不是**权限**。它能拦住"吃光内存"和"进程炸弹"，
拦不住"读走宿主上任何当前用户可读的文件"。文件系统隔离靠的是
`sandbox.py` 的副本策略，不是这里。
"""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes
from dataclasses import dataclass

from typing_extensions import Self

IS_WINDOWS = sys.platform == "win32"

# SetInformationJobObject 的信息类
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9

# 限制标志
JOB_OBJECT_LIMIT_PROCESS_MEMORY = 0x00000100
JOB_OBJECT_LIMIT_JOB_MEMORY = 0x00000200
JOB_OBJECT_LIMIT_ACTIVE_PROCESS = 0x00000008
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000

# OpenProcess 权限
PROCESS_SET_QUOTA = 0x0100
PROCESS_TERMINATE = 0x0001
PROCESS_SET_INFORMATION = 0x0200

ERROR_ACCESS_DENIED = 5

LARGE_INTEGER = ctypes.c_longlong
SIZE_T = ctypes.c_size_t
ULONG_PTR = ctypes.c_size_t


class IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", LARGE_INTEGER),
        ("PerJobUserTimeLimit", LARGE_INTEGER),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", SIZE_T),
        ("MaximumWorkingSetSize", SIZE_T),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ULONG_PTR),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", IO_COUNTERS),
        ("ProcessMemoryLimit", SIZE_T),
        ("JobMemoryLimit", SIZE_T),
        ("PeakProcessMemoryUsed", SIZE_T),
        ("PeakJobMemoryUsed", SIZE_T),
    ]


@dataclass
class JobLimits:
    memory_bytes: int | None = None
    active_processes: int | None = None
    kill_on_close: bool = True


def can_assign() -> tuple[bool, str]:
    """
    端到端探测：真的起一个短命子进程，试着把它放进作业。

    **不能只用 `available()` 判断。** 实测（本机）：`CreateJobObjectW` 成功、
    `SetInformationJobObject` 成功，但 `OpenProcess` 直接 err=5 ——
    因为 DSH 自己就用作业对象管着进程树，我们拿不到接管权限。
    只看"能不能创建作业"会得到"资源上限已生效"的错误结论，
    而那个结论会一路传到能力表里，变成一句不成立的保证。

    结果缓存：探测要起进程，不能每次调用都做。
    """
    global _CAN_ASSIGN
    if _CAN_ASSIGN is not None:
        return _CAN_ASSIGN

    ok, detail = available()
    if not ok:
        _CAN_ASSIGN = (False, detail)
        return _CAN_ASSIGN

    import subprocess

    process = None
    job = None
    try:
        job = Job(JobLimits(memory_bytes=512 * 1024 * 1024, active_processes=16))
        process = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(0.3)"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        assigned, why = job.assign_pid(process.pid)
        _CAN_ASSIGN = (assigned, why)
        return _CAN_ASSIGN
    except OSError as exc:
        _CAN_ASSIGN = (False, f"自检失败：{exc}")
        return _CAN_ASSIGN
    finally:
        if process is not None:
            try:
                process.kill()
                process.wait(timeout=10)
            except Exception:  # noqa: BLE001, S110
                # 这里是 `finally` 里的**收尾**：目标进程可能已经自己退出、已经被回收，
                # 或者等待超时。收尾失败不该盖掉上面真正的结论（"能不能限制住子进程"），
                # 所以刻意吞掉 —— 吞掉的只是"清理没干净"，不是"限制没生效"。
                pass
        if job is not None:
            job.close()


_CAN_ASSIGN: tuple[bool, str] | None = None


def _kernel32() -> ctypes.WinDLL:
    return ctypes.WinDLL("kernel32", use_last_error=True)


def available() -> tuple[bool, str]:
    """
    探测作业对象能不能用。

    不能想当然：本机跑在 DSH 的沙箱里，Windows API 有可能被挡。
    探测不出来就如实降级，而不是"假定能用、失败时才发现"。
    """
    if not IS_WINDOWS:
        return False, "非 Windows 平台，作业对象不适用"
    try:
        kernel32 = _kernel32()
        handle = kernel32.CreateJobObjectW(None, None)
        if not handle:
            return False, f"CreateJobObjectW 失败，err={ctypes.get_last_error()}"
        kernel32.CloseHandle(handle)
        return True, "ok"
    except OSError as exc:                     # pragma: no cover - 平台相关
        return False, f"调用失败：{exc}"


class Job:
    """作业对象句柄的薄封装。`close()` 之后句柄失效。"""

    def __init__(self, limits: JobLimits) -> None:
        if not IS_WINDOWS:
            raise OSError("作业对象只在 Windows 上可用")
        self._kernel32 = _kernel32()
        self._handle = self._kernel32.CreateJobObjectW(None, None)
        if not self._handle:
            raise OSError(f"CreateJobObjectW 失败，err={ctypes.get_last_error()}")
        self._apply(limits)

    def _apply(self, limits: JobLimits) -> None:
        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        flags = 0
        if limits.kill_on_close:
            flags |= JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if limits.memory_bytes:
            # 同时设单进程与整作业上限：只设一个的话，进程炸弹仍能绕过去
            flags |= JOB_OBJECT_LIMIT_PROCESS_MEMORY | JOB_OBJECT_LIMIT_JOB_MEMORY
            info.ProcessMemoryLimit = limits.memory_bytes
            info.JobMemoryLimit = limits.memory_bytes
        if limits.active_processes:
            flags |= JOB_OBJECT_LIMIT_ACTIVE_PROCESS
            info.BasicLimitInformation.ActiveProcessLimit = limits.active_processes
        info.BasicLimitInformation.LimitFlags = flags

        ok = self._kernel32.SetInformationJobObject(
            self._handle,
            JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(info),
            ctypes.sizeof(info),
        )
        if not ok:
            error = ctypes.get_last_error()
            self.close()
            raise OSError(f"SetInformationJobObject 失败，err={error}")

    # ------------------------------------------------------------- 句柄操作

    def assign_pid(self, pid: int) -> tuple[bool, str]:
        """
        把进程放进作业。失败时返回原因而**不抛异常** ——
        调用方需要知道"限制没生效"，并且要继续把命令跑完（宁可弱一点也别不跑）。
        """
        handle = self._kernel32.OpenProcess(
            PROCESS_SET_QUOTA | PROCESS_TERMINATE | PROCESS_SET_INFORMATION, False, pid
        )
        if not handle:
            return False, f"OpenProcess 失败，err={ctypes.get_last_error()}"
        try:
            ok = self._kernel32.AssignProcessToJobObject(self._handle, handle)
            if not ok:
                error = ctypes.get_last_error()
                if error == ERROR_ACCESS_DENIED:
                    return False, (
                        "AssignProcessToJobObject 被拒（err=5）：进程可能已经属于另一个作业"
                        "（DSH 自己就在用作业对象管进程树）"
                    )
                return False, f"AssignProcessToJobObject 失败，err={error}"
            return True, "ok"
        finally:
            self._kernel32.CloseHandle(handle)

    def terminate(self) -> bool:
        return bool(self._kernel32.TerminateJobObject(self._handle, 1))

    def close(self) -> None:
        if getattr(self, "_handle", None):
            self._kernel32.CloseHandle(self._handle)
            self._handle = None

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def set_affinity(pid: int, cores: int) -> tuple[bool, str]:
    """
    把进程限制在前 N 个核上跑（路线要求 CPU 2 核）。

    用进程亲和性近似"2 核"：Windows 没有免管理员的 CPU 配额，
    亲和性是同一件事的可用近似，且**可验证**（能读回掩码）。
    """
    if not IS_WINDOWS:
        return False, "非 Windows"
    try:
        kernel32 = _kernel32()
        handle = kernel32.OpenProcess(PROCESS_SET_INFORMATION | PROCESS_SET_QUOTA, False, pid)
        if not handle:
            return False, f"OpenProcess 失败，err={ctypes.get_last_error()}"
        try:
            mask = (1 << cores) - 1
            ok = kernel32.SetProcessAffinityMask(handle, ULONG_PTR(mask))
            if not ok:
                return False, f"SetProcessAffinityMask 失败，err={ctypes.get_last_error()}"
            return True, f"mask=0x{mask:x}"
        finally:
            kernel32.CloseHandle(handle)
    except OSError as exc:                      # pragma: no cover - 平台相关
        return False, str(exc)
