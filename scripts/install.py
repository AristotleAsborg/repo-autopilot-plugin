"""一键安装/自检：把「装插件」从几条命令压成一条。

用法：

    python scripts/install.py --repo-root D:\\path\\to\\repo-autopilot
    python scripts/install.py --repo-root ... --install-deps     # 缺依赖就顺手装上
    python scripts/install.py --repo-root ... --dry-run          # 只看要做什么，不动手

它做四件事：

  1. 检查**正在跑本脚本的解释器**（版本 >= 3.12、依赖 yaml/requests/numpy）；
  2. 检查 repo-autopilot 仓库根是否齐（四个只读入口点）；`--repo-root` 不给就自动找；
  3. 打印**注册成 Cordis Package 的具体步骤**；
  4. 给出一条 `REPO_AUTOPILOT_PYTHON` 设置命令，省掉以后每次指定解释器。

退出码：**0 = 可以装/已经能跑**；**1 = 有缺件**。

设计取舍（有意为之）：

* **只用标准库**。安装脚本依赖三方库是个死循环 —— 缺 `yaml` 的时候它得能跑起来报缺件。
* **不自动下载任何东西**。`--install-deps` 是**显式**开关；默认只告诉你缺什么、怎么补。
  自动装 Python / 自动下载 uv 会引入网络与信任面，收益不值这个价。
* **不跑 `subprocess` 抓输出**。本 harness 的沙箱禁止用管道抓别的程序的输出
  （会 EPERM）。所以只检查**当前解释器** —— 这也更诚实：报告的永远是你实际在用的那个。
* 检查逻辑**复用** `scripts/smoke.py`，不重写一份。
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import smoke  # noqa: E402  —— 同目录，复用它的检查，避免两套判据漂移

REGISTER_STEPS = """\
注册成 Cordis Package：
  1) 读取 host.js 的**全部内容**；
  2) cordis_define({{
       plugin: {{ kind: 'new', idPrefix: 'rauto' }},
       name: '<包名>',
       purpose: '<一句话用途>',
       code: {{ host: <host.js 的内容> }},
     }});
  3) 用返回的 pluginId / packageId 调 cordis_run（首次用 mode: 'run'）。
"""


def is_repo_root(candidate: Path) -> bool:
    """仓库根的判据 = 四个只读入口点齐全（与 smoke.py 同一套）。"""
    return all((candidate / relative).is_file() for relative, _ in smoke.REQUIRED_ENTRY_POINTS)


def autodetect_repo_root(start: Path) -> Path | None:
    """从 start 逐级向上找 `repo-autopilot`；再退一步，看 start 自己是不是仓库根。"""
    for base in (start, *start.parents):
        for name in ("repo-autopilot", "."):
            candidate = (base / name).resolve()
            if is_repo_root(candidate):
                return candidate
    return None


def missing_modules(checks: list[smoke.Check]) -> list[str]:
    return [
        item.name.removeprefix("模块 ")
        for item in checks
        if not item.ok and item.name.startswith("模块 ")
    ]


def render_next_steps(python: str, repo_root: Path, *, all_good: bool) -> str:
    lines: list[str] = ["", "下一步："]
    lines.append("  · 让插件固定用这个解释器（省掉每次指定）：")
    lines.append(f"      $env:REPO_AUTOPILOT_PYTHON = '{python}'")
    lines.append(REGISTER_STEPS.format().rstrip())
    if all_good:
        lines.append("  · 自检已全过，可以直接注册。")
    else:
        # 这里**不许用 emoji**：中文 Windows 的控制台是 GBK，`⚠️` 会 UnicodeEncodeError
        # 把脚本**当场打崩** —— 而且偏偏是在"要报告缺件"的时候崩，用户最需要输出时看不到输出。
        # 2026-09-18 实测踩过。另有 `errors="replace"` 兜底（见 main）。
        lines.append("  · [!] 上面还有缺件 —— 先把缺件补齐再注册，否则插件会报环境问题。")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    # 兜底：控制台编码装不下的字符**替换**掉，而不是抛异常。
    # （GBK 控制台 + 任何非 GBK 符号都会炸；宁可少一个字符，也不能让安装脚本崩。）
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")

    parser = argparse.ArgumentParser(description="repo-autopilot 插件的一键安装/自检")
    parser.add_argument("--repo-root", help="repo-autopilot 仓库的绝对路径；不给就自动找")
    parser.add_argument("--install-deps", action="store_true", help="缺依赖就顺手装上（默认不装）")
    parser.add_argument("--dry-run", action="store_true", help="只打印要做什么，不执行")
    args = parser.parse_args(argv)

    print("=== 1/3 解释器与依赖 ===")
    checks = [smoke.check_interpreter(), *[smoke.check_module(n, w) for n, w in smoke.REQUIRED_MODULES]]
    print(smoke.render(checks).rsplit("\n\n", 1)[0])

    missing = missing_modules(checks)
    if missing and args.install_deps:
        command = [sys.executable, "-m", "pip", "install", *missing]
        print(f"\n安装缺件：{' '.join(command)}")
        if args.dry_run:
            print("（--dry-run：没有真的执行）")
        else:
            # 继承 stdio，不用管道 —— 沙箱里管道会 EPERM。
            code = subprocess.call(command)
            print(f"pip 退出码：{code}")
            if code == 0:
                checks = [
                    smoke.check_interpreter(),
                    *[smoke.check_module(n, w) for n, w in smoke.REQUIRED_MODULES],
                ]
                missing = missing_modules(checks)

    print("\n=== 2/3 repo-autopilot 仓库 ===")
    repo_root = Path(args.repo_root).expanduser() if args.repo_root else autodetect_repo_root(Path.cwd())
    if repo_root is None:
        print("  [缺] 没有给 --repo-root，自动查找也没找到。")
        print("        → 补法：--repo-root <repo-autopilot 仓库的绝对路径>")
        repo_checks: list[smoke.Check] = []
    else:
        print(f"  （仓库根：{repo_root}）")
        repo_checks = smoke.check_repo_root(repo_root)
        print(smoke.render(repo_checks).rsplit("\n\n", 1)[0])

    print("\n=== 3/3 结论 ===")
    everything = [*checks, *(repo_checks or [])]
    failed = [item for item in everything if not item.ok]
    if missing:
        print(f"  依赖缺 {len(missing)} 个：{'、'.join(missing)}")
        print(f"  （可以加 --install-deps 让本脚本装，或手工：{sys.executable} -m pip install {' '.join(missing)}）")
    if failed:
        print(f"  **不能直接注册** —— {len(failed)} 项缺件。")
        rc = 1
    else:
        print("  **可以注册** —— 解释器、依赖、仓库三项全过。")
        rc = 0

    print(render_next_steps(sys.executable, repo_root or Path("."), all_good=rc == 0))
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
