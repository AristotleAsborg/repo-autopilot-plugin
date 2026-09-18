"""取证 5：在**同一种并发压力**下并排比较三种「认领」原语。

已确认的事实（`.cache/rename_race2.py`，40 轮，零项目代码）：
    两个进程同时 os.rename(同一个 src, 同一个 dst)
    → 21/40 轮**两个都返回成功**，19/40 轮恰好一个成功，0 轮两个都失败。
也就是说「目标存在时 rename 失败」这条互斥前提，在真正的并发下**不成立**。

本脚本把候选替代品放在同样的赛道上比较：
    rename : 现状（源 pending.json → 目标 doing.json）
    excl   : os.open(target, O_CREAT|O_EXCL|O_WRONLY)  —— CreateFile/CREATE_NEW，内核原子
    mkdir  : os.mkdir(target)                          —— CreateDirectory，内核原子

判据：一种原语只有做到「每轮恰好一个成功」，才可以用来做互斥。

用法：python .cache/claim_race.py [轮数]
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / ".cache" / "claim-race"
PY_REAL = getattr(sys, "_base_executable", sys.executable)
ENV = {**os.environ, "PYTHONPATH": os.pathsep.join(p for p in sys.path if p)}


def worker(argv: list[str]) -> int:
    mode, src_s, dst_s, gate_s, out_s = argv
    src, dst, gate, out = Path(src_s), Path(dst_s), Path(gate_s), Path(out_s)
    while not gate.exists():
        pass
    try:
        if mode == "rename":
            os.rename(src, dst)
        elif mode == "excl":
            handle = os.open(str(dst), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(handle)
        elif mode == "mkdir":
            os.mkdir(str(dst))
        else:
            raise ValueError(mode)
    except BaseException as exc:  # noqa: BLE001
        result = f"{type(exc).__name__}"
    else:
        result = "OK"
    out.write_text(result, encoding="utf-8")
    return 0


def run_mode(mode: str, rounds: int) -> dict[str, int]:
    tally = {"one": 0, "both": 0, "neither": 0}
    samples: list[str] = []
    me = str(Path(__file__).resolve())
    for round_no in range(rounds):
        src = WORK / "pending.json"
        dst = WORK / "doing.json"
        gate = WORK / "gate"
        for path in (src, dst, gate):
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path, ignore_errors=True)
            elif path.exists():
                path.unlink()
        src.write_text("payload", encoding="utf-8")

        outs = [WORK / f"out-{mode}-{i}.txt" for i in range(2)]
        for out in outs:
            if out.exists():
                out.unlink()
        procs = [
            subprocess.Popen(
                [PY_REAL, me, "worker", mode, str(src), str(dst), str(gate), str(out)],
                env=ENV,
                cwd=str(ROOT),
            )
            for out in outs
        ]
        gate.write_text("go", encoding="utf-8")
        for proc in procs:
            proc.wait(timeout=30)

        results = [out.read_text(encoding="utf-8") if out.exists() else "MISSING" for out in outs]
        ok_count = sum(1 for item in results if item == "OK")
        if ok_count == 1:
            tally["one"] += 1
        elif ok_count > 1:
            tally["both"] += 1
            if len(samples) < 3:
                samples.append(f"轮{round_no}: {results}")
        else:
            tally["neither"] += 1
            if len(samples) < 3:
                samples.append(f"轮{round_no}(都失败): {results}")
    tally["samples"] = samples  # type: ignore[assignment]
    return tally


def main() -> int:
    rounds = int(sys.argv[1]) if len(sys.argv) > 1 else 30
    if WORK.exists():
        shutil.rmtree(WORK, ignore_errors=True)
    WORK.mkdir(parents=True, exist_ok=True)

    print(f"每种原语 {rounds} 轮，两个进程同时抢同一个目标\n")
    print(f"{'原语':<8} {'恰好一个':>8} {'两个都成功':>10} {'两个都失败':>10}   判定")
    for mode in ("rename", "excl", "mkdir"):
        tally = run_mode(mode, rounds)
        verdict = "可用" if tally["one"] == rounds else "**不可用**"
        print(
            f"{mode:<8} {tally['one']:>8} {tally['both']:>10} {tally['neither']:>10}   {verdict}"
        )
        for sample in tally["samples"]:  # type: ignore[index]
            print(f"         样本: {sample}")
    return 0


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "worker":
        raise SystemExit(worker(sys.argv[2:]))
    raise SystemExit(main())
