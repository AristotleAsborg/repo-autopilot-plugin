"""取证 4：**纯**并发 rename 竞争，不含本项目任何代码。

目标：回答一个问题 —— 在本机（且在本 DSH 沙箱下），
两个进程同时 `os.rename(同一个 src, 同一个 dst)`，会不会两个都成功？

若会出现「两个都成功」，说明纸面上「目标存在则 rename 失败」这条互斥前提
在这里不成立，整个基于 rename 的认领设计必须换机制。

用法：python .cache/rename_race2.py [轮数]
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / ".cache" / "rename-race2"
PY_REAL = getattr(sys, "_base_executable", sys.executable)
ENV = {**os.environ, "PYTHONPATH": os.pathsep.join(p for p in sys.path if p)}


def race_worker(argv: list[str]) -> int:
    src = Path(argv[0])
    dst = Path(argv[1])
    gate = Path(argv[2])
    out = Path(argv[3])
    # 忙等到 gate 出现，尽量让两个进程在同一瞬间发起 rename
    while not gate.exists():
        pass
    src_existed_before = src.exists()
    try:
        os.rename(src, dst)
        result = "OK"
    except BaseException as exc:  # noqa: BLE001
        result = f"{type(exc).__name__}/winerr={getattr(exc, 'winerror', None)}"
    out.write_text(f"{result}|src_before={src_existed_before}", encoding="utf-8")
    return 0


def main() -> int:
    rounds = int(sys.argv[1]) if len(sys.argv) > 1 else 40
    if WORK.exists():
        shutil.rmtree(WORK, ignore_errors=True)
    WORK.mkdir(parents=True, exist_ok=True)
    me = str(Path(__file__).resolve())

    both_ok = 0
    neither = 0
    anomalies: list[str] = []
    for round_no in range(rounds):
        src = WORK / "pending.json"
        dst = WORK / "doing.json"
        gate = WORK / "gate"
        for path in (src, dst, gate):
            if path.exists():
                path.unlink()
        src.write_text("payload", encoding="utf-8")

        outs = [WORK / f"out-{i}.txt" for i in range(2)]
        for out in outs:
            if out.exists():
                out.unlink()
        procs = [
            subprocess.Popen(
                [PY_REAL, me, "worker", str(src), str(dst), str(gate), str(out)],
                env=ENV,
                cwd=str(ROOT),
            )
            for out in outs
        ]
        gate.write_text("go", encoding="utf-8")
        for proc in procs:
            proc.wait(timeout=30)

        results = [out.read_text(encoding="utf-8") if out.exists() else "MISSING" for out in outs]
        ok_count = sum(1 for item in results if item.startswith("OK"))
        if ok_count > 1:
            both_ok += 1
            if len(anomalies) < 5:
                anomalies.append(f"轮{round_no}: {results}")
        elif ok_count == 0:
            neither += 1
            if len(anomalies) < 5:
                anomalies.append(f"轮{round_no}(都失败): {results}")

    print(f"共 {rounds} 轮并发 rename 竞争")
    print(f"  恰好一个成功 : {rounds - both_ok - neither}")
    print(f"  两个都成功   : {both_ok}   <-- 若不为 0，互斥前提不成立")
    print(f"  两个都失败   : {neither}")
    for line in anomalies:
        print("   异常样本:", line)
    return 0


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "worker":
        raise SystemExit(race_worker(sys.argv[2:]))
    raise SystemExit(main())
