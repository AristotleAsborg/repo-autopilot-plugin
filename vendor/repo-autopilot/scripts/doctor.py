"""`/应急` 的自检入口（路线 7.2 第 4 条）。

    python scripts/doctor.py            # 人读的诊断报告（含 A/B/C 修复选项）
    python scripts/doctor.py --json     # 机器读
    python scripts/doctor.py --state-dir <路径>   # 对指定的 state 目录做检查（演练用）

退出码：0 = 七项全过；1 = 有异常（报告里给出选项，**不自行修**）。
修复动作一律过闸门 —— 这个脚本只诊断，不动手。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.skills import doctor_report, overall, run_checks


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

    parser = argparse.ArgumentParser(description="/应急 自检")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--state-dir", default="")
    args = parser.parse_args()

    checks = run_checks(state_dir=Path(args.state_dir) if args.state_dir else None)
    if args.json:
        print(json.dumps([item.as_dict() for item in checks], ensure_ascii=False, indent=2))
    else:
        print(doctor_report(checks))
    return 0 if overall(checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
