"""干净机器上的冒烟自检：**只用标准库**，拿到一台新机器就能跑。

用法：

    python scripts/smoke.py --repo-root D:\\path\\to\\repo-autopilot

它回答一个问题：**这台机器现在能不能跑这个插件？** 逐条给出"缺什么、怎么补"。

检查四件事：
  1. 解释器版本（repo-autopilot 要求 Python >= 3.12）
  2. 依赖模块（`yaml` / `requests` / `numpy`）
  3. repo-autopilot 仓库根是否齐（四个只读入口点）
  4. `git` 是否可用（本地保存功能要用）

退出码：**0 = 全过**；**1 = 有缺件**。缺件逐条列出来，不静默、不降级。

为什么不用 `subprocess`：本 harness 的沙箱禁止程序通过管道抓别的程序的输出
（Node.js 的 `stdio: 'pipe'` 会 EPERM，Python 的 `subprocess` 同理有风险）。
所以这里**只检查"正在跑本脚本的那个解释器"** —— 换个解释器跑一遍，就检查那一个。
这反而更诚实：报告的永远是你**实际在用的**那个解释器。
"""

from __future__ import annotations

import argparse
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

MIN_PYTHON = (3, 12)

# 依赖紧跟 repo-autopilot 的实际用法：yaml 读配置、requests 走网络、numpy 做向量。
REQUIRED_MODULES: tuple[tuple[str, str], ...] = (
    ("yaml", "读 config/ 与 state/ 里的 YAML"),
    ("requests", "GitHub / 本地模型探活"),
    ("numpy", "本地小模型档的向量运算"),
)

# 插件真正会调的四个只读入口点。少一个，对应的模式就会失败。
REQUIRED_ENTRY_POINTS: tuple[tuple[str, str], ...] = (
    ("scripts/doctor.py", "doctor 模式（七项自检）"),
    ("tools/daily_drill.py", "drill 模式（7 天演练台账）"),
    ("tools/acceptance.py", "acceptance 模式（验收步骤表）"),
    ("tools/package.py", "compare 模式（副本哈希比对）"),
)


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str
    fix: str = ""


def check_interpreter() -> Check:
    current = ".".join(str(part) for part in sys.version_info[:3])
    ok = sys.version_info[:2] >= MIN_PYTHON
    return Check(
        name="解释器版本",
        ok=ok,
        detail=f"{current}（{sys.executable}）",
        fix="" if ok else f"需要 Python >= {MIN_PYTHON[0]}.{MIN_PYTHON[1]}",
    )


def check_module(module_name: str, why: str) -> Check:
    try:
        __import__(module_name)
    except Exception as error:  # noqa: BLE001 —— 缺件就是缺件，理由照抄给人类看
        return Check(
            name=f"模块 {module_name}",
            ok=False,
            detail=f"缺失（{why}）—— {type(error).__name__}: {error}",
            fix=f"{sys.executable} -m pip install {module_name}",
        )
    return Check(name=f"模块 {module_name}", ok=True, detail=why)


def check_repo_root(root: Path) -> list[Check]:
    checks: list[Check] = []
    if not root.is_dir():
        return [
            Check(
                name="仓库根",
                ok=False,
                detail=f"目录不存在：{root}",
                fix="给 --repo-root 一个 repo-autopilot 仓库的绝对路径",
            )
        ]
    checks.append(Check(name="仓库根", ok=True, detail=str(root)))
    for relative, why in REQUIRED_ENTRY_POINTS:
        target = root / relative
        checks.append(
            Check(
                name=relative,
                ok=target.is_file(),
                detail=why,
                fix="" if target.is_file() else f"仓库根不对？{target} 不存在",
            )
        )
    return checks


def check_git() -> Check:
    found = shutil.which("git")
    return Check(
        name="git",
        ok=found is not None,
        detail=found or "未找到",
        fix="" if found else "装 git（本地保存功能要用；只读模式不用）",
    )


def run_checks(repo_root: Path) -> list[Check]:
    return [
        check_interpreter(),
        *[check_module(name, why) for name, why in REQUIRED_MODULES],
        *check_repo_root(repo_root),
        check_git(),
    ]


def render(checks: list[Check]) -> str:
    lines = []
    for item in checks:
        lines.append(f"  [{'OK' if item.ok else '缺'}] {item.name}：{item.detail}")
        if not item.ok and item.fix:
            lines.append(f"        → 补法：{item.fix}")
    failed = [item for item in checks if not item.ok]
    lines.append("")
    if failed:
        lines.append(f"结论：**不能跑** —— {len(failed)} 项缺件（上面逐条写了补法）。")
    else:
        lines.append("结论：**可以跑** —— 四项检查全过。")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="repo-autopilot 插件的干净机器冒烟自检")
    parser.add_argument("--repo-root", required=True, help="repo-autopilot 仓库的绝对路径")
    args = parser.parse_args(argv)

    checks = run_checks(Path(args.repo_root))
    print(render(checks))
    return 1 if any(not item.ok for item in checks) else 0


if __name__ == "__main__":
    raise SystemExit(main())
