"""沙箱红队样本（路线 1.5 的"恶意三件套"，外加三件自选的）。

这些脚本**故意**是恶意的 —— 它们存在的意义就是证明沙箱挡得住，或者诚实地
暴露出挡不住。所有破坏性动作都限制在自己的工作目录内，**不写死任何宿主路径**
（除了调用方显式传进来的那一个，用于验证"绝对路径这条路我们确实没堵"）。

输出**全部用 ASCII 标记**：子进程的 stdout 走的是系统代码页，
而验收测试要按字符串断言日志内容 —— 用中文标记会让断言随编码飘。
标记本身是给机器看的，中文解释放在本文件的注释里就够了。

用法：python tests/sandbox_payloads.py <mode> [args...]
"""

from __future__ import annotations

import os
import shutil
import sys
import time
from pathlib import Path


def mode_destroy() -> int:
    """攻击一：把能看到的东西全删了（对应路线里的 `rm -rf /`）。"""
    here = Path.cwd()
    print(f"[destroy] cwd = {here}")
    removed = 0
    for child in sorted(here.iterdir()):
        try:
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()
            removed += 1
        except OSError as exc:
            print(f"[destroy] cannot remove {child.name}: {exc}")
    remaining = sorted(p.name for p in here.iterdir())
    print(f"[destroy] removed {removed} top-level entries, {remaining} left")
    return 0


def mode_spin() -> int:
    """攻击二：死循环（对应"死循环 → 超时熔断"）。"""
    print("[spin] entering infinite loop", flush=True)
    while True:
        time.sleep(0.05)


def mode_net() -> int:
    """攻击三：外联（对应"外联请求 → 断网生效"）。"""
    import urllib.error
    import urllib.request

    target = sys.argv[2] if len(sys.argv) > 2 else "http://127.0.0.1:11434/api/tags"
    print(f"[net] probing {target}")
    try:
        with urllib.request.urlopen(target, timeout=8) as response:
            body = response.read(120)
            print(f"[net] connected: HTTP {response.status}, {len(body)} bytes")
            return 0
    except urllib.error.URLError as exc:
        print(f"[net] unreachable: {exc.reason}")
        return 3
    except OSError as exc:
        print(f"[net] unreachable: {exc}")
        return 3


def mode_env() -> int:
    """攻击四（自选）：看看能不能捡到宿主的秘密环境变量。"""
    leaks = sorted(
        name
        for name in ("GH_READ_TOKEN", "GH_WRITE_TOKEN", "DEEPSEEK_API_KEY", "DSH_HOME")
        if os.environ.get(name)
    )
    print(f"[env] sensitive vars visible: {leaks or '(none)'}")
    print(f"[env] total env vars = {len(os.environ)}")
    return 1 if leaks else 0


def mode_memhog() -> int:
    """攻击五（自选）：吃内存，验证内存上限真的生效。"""
    chunks = []
    total = 0
    try:
        for _ in range(20):                     # 每次 100MB，最多 2GB
            chunks.append(bytearray(100 * 1024 * 1024))
            total += 100
            print(f"[memhog] allocated {total} MB", flush=True)
    except MemoryError:
        print(f"[memhog] stopped by memory limit (MemoryError) at {total} MB")
        return 4
    print(f"[memhog] managed to allocate {total} MB -- memory limit did NOT work")
    return 0


def mode_absolute_write() -> int:
    """攻击六（自选）：往工作目录**之外**写文件。

    这一条是**已知缺口**：作业对象管资源不管权限，我们也没有 Docker/管理员权限。
    样本存在，就是为了让这个缺口是"被记录的"而不是"被忘记的"。
    """
    target = Path(sys.argv[2])
    try:
        target.write_text("written from inside the sandbox\n", encoding="utf-8")
        print(f"[absolute] wrote outside the workdir: {target}")
        return 0
    except OSError as exc:
        print(f"[absolute] blocked: {exc}")
        return 5


def mode_ok() -> int:
    print("[ok] benign payload finished")
    return 0


MODES = {
    "destroy": mode_destroy,
    "spin": mode_spin,
    "net": mode_net,
    "env": mode_env,
    "memhog": mode_memhog,
    "absolute": mode_absolute_write,
    "ok": mode_ok,
}


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] not in MODES:
        print(f"usage: {sys.argv[0]} <{'|'.join(MODES)}> [args...]")
        return 2
    return MODES[sys.argv[1]]()


if __name__ == "__main__":
    raise SystemExit(main())
