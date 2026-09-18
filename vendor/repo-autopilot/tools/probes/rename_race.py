"""取证 3：两个进程看到的是不是**同一个文件系统视图**。

背景：`.cache/trace_claim.py` 抓到的现场里，两个进程先后执行
    os.rename(pending/x.json, doing/x.json)
**两次都返回成功**。而单进程实验已证明「目标存在时 os.rename 必然 FileExistsError」。
同一份源码、同一个源文件，两次都成功 —— 逻辑上只可能是两个进程看到的目录内容不同。

本脚本直接把这件事摊开：
  * A 进程：等到 gate 后 rename，然后把「自己看到的目录清单」落盘；
  * B 进程：等到 gate 后**先等 400ms**（A 早已做完），再落盘它看到的清单，最后才尝试 rename。
若 B 在 400ms 之后**仍然看到 pending/x.json 存在**，那就是两个视图不一致。

用法：python .cache/rename_race.py
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / ".cache" / "rename-race"
PY_REAL = getattr(sys, "_base_executable", sys.executable)
ENV = {**os.environ, "PYTHONPATH": os.pathsep.join(p for p in sys.path if p)}


def snapshot(directory: Path) -> dict:
    return {
        "abs": str(directory),
        "exists": directory.exists(),
        "entries": sorted(p.name for p in directory.iterdir()) if directory.exists() else None,
        "pending_exists": (directory / "pending.json").exists(),
        "pending_realpath": os.path.realpath(str(directory / "pending.json")),
    }


def run_role_a(argv: list[str]) -> int:
    work = Path(argv[0])
    out = Path(argv[1])
    gate = work / "gate"
    while not gate.exists():
        pass
    result = "OK"
    try:
        os.rename(work / "pending.json", work / "doing.json")
    except BaseException as exc:  # noqa: BLE001
        result = f"{type(exc).__name__}/winerr={getattr(exc, 'winerror', None)}"
    out.write_text(
        json.dumps({"role": "A", "rename": result, "after": snapshot(work)}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return 0


def run_role_b(argv: list[str]) -> int:
    work = Path(argv[0])
    out = Path(argv[1])
    gate = work / "gate"
    while not gate.exists():
        pass
    time.sleep(0.4)   # A 必然早已完成
    before = snapshot(work)
    result = "skipped(src 不存在)"
    if (work / "pending.json").exists():
        try:
            os.rename(work / "pending.json", work / "doing.json")
            result = "OK（!! 说明 B 看到了 A 已经移走的源文件）"
        except BaseException as exc:  # noqa: BLE001
            result = f"{type(exc).__name__}/winerr={getattr(exc, 'winerror', None)}"
    out.write_text(
        json.dumps(
            {"role": "B", "waited_ms": 400, "before": before, "rename": result, "after": snapshot(work)},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return 0


def main() -> int:
    if WORK.exists():
        shutil.rmtree(WORK, ignore_errors=True)
    WORK.mkdir(parents=True, exist_ok=True)
    gate = WORK / "gate"

    (WORK / "pending.json").write_text("payload", encoding="utf-8")
    print("初始目录:", sorted(p.name for p in WORK.iterdir()))

    out_a = WORK / "out-A.json"
    out_b = WORK / "out-B.json"
    me = str(Path(__file__).resolve())

    proc_a = subprocess.Popen([PY_REAL, me, "a", str(WORK), str(out_a)], env=ENV, cwd=str(ROOT))
    proc_b = subprocess.Popen([PY_REAL, me, "b", str(WORK), str(out_b)], env=ENV, cwd=str(ROOT))
    gate.write_text("go", encoding="utf-8")
    proc_a.wait(timeout=30)
    proc_b.wait(timeout=30)

    print("\n=== A 进程（先 rename）===")
    print(out_a.read_text(encoding="utf-8") if out_a.exists() else "(没有输出)")
    print("=== B 进程（等 400ms 后才看）===")
    print(out_b.read_text(encoding="utf-8") if out_b.exists() else "(没有输出)")
    print("=== 最终目录 ===", sorted(p.name for p in WORK.iterdir()))
    return 0


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] in {"a", "b"}:
        role = sys.argv[1]
        raise SystemExit(run_role_a(sys.argv[2:]) if role == "a" else run_role_b(sys.argv[2:]))
    raise SystemExit(main())
