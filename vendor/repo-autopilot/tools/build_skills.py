"""从 `src/skills/registry.py` 渲染 `skills/*.md` 与 `AGENTS.md`（幂等）。

    python tools/build_skills.py          # 重建
    python tools/build_skills.py --check  # 只校验（/体检 用这个）

为什么要"渲染"而不是手写这 9 份文档：路线 7.4 第 1 条要求指令清单、`AGENTS.md`、
`skills/` **三者一一对应**。手维护三处必然漂移，所以真相只有一处（`COMMANDS` 表），
文档由它渲染，`--check` 再反过来验证磁盘上的文件与表一致。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.skills import COMMANDS, check_consistency, write_all


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

    parser = argparse.ArgumentParser(description="渲染 skills/ 与 AGENTS.md")
    parser.add_argument("--check", action="store_true", help="只校验一致性，不写文件")
    args = parser.parse_args()

    if args.check:
        result = check_consistency()
        print(f"一致性校验：{'通过' if result['ok'] else '**不通过**'}（检查了 {result['checked']} 条指令）")
        for problem in result["problems"]:
            print(f"  - {problem}")
        return 0 if result["ok"] else 1

    written = write_all()
    print(f"已渲染 {len(written)} 个文件：")
    for path in written:
        print(f"  {path.relative_to(ROOT).as_posix()}")
    result = check_consistency()
    print(f"一致性校验：{'通过' if result['ok'] else '**不通过**'}")
    for problem in result["problems"]:
        print(f"  - {problem}")
    print(f"指令清单（{len(COMMANDS)} 条）：" + "、".join(entry.usage for entry in COMMANDS))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
