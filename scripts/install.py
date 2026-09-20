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
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import smoke  # noqa: E402  —— 同目录，复用它的检查，避免两套判据漂移

#: 随插件一起打包的那份 repo-autopilot（自包含）。由仓库自带的 tools/package.py 打出，
#: 带 MANIFEST.json（逐文件 sha256），所以"有没有被改过"是可以**验**的，不是靠信。
BUNDLED = HERE.parent / "vendor" / "repo-autopilot"


def bundled_repo_root() -> Path | None:
    """自带副本的路径；不完整就返回 None（不假装自包含可用）。"""
    return BUNDLED if is_repo_root(BUNDLED) else None


def verify_manifest(root: Path) -> tuple[bool, list[str]]:
    """
    拿 MANIFEST.json 里的 sha256 逐文件校验自带副本。

    **只用标准库**：校验要在"依赖还没装"的干净机器上也能跑 —— 那正是最需要它的时刻。
    权威校验仍是 repo-autopilot 自带的 `python tools/package.py verify <dir>`
    （它还会报"多出来的文件"，这里只查缺失与内容不符）。
    """
    manifest_path = root / "MANIFEST.json"
    if not manifest_path.is_file():
        return False, [f"没有清单：{manifest_path}"]
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        return False, [f"清单读不了：{type(error).__name__}: {error}"]

    files = manifest.get("files") or {}
    if not isinstance(files, dict) or not files:
        return False, ["清单里没有 files 段"]

    missing: list[str] = []
    changed: list[str] = []
    for relative, meta in files.items():
        target = root / relative
        if not target.is_file():
            missing.append(relative)
            continue
        if hashlib.sha256(target.read_bytes()).hexdigest() != (meta or {}).get("sha256"):
            changed.append(relative)

    notes: list[str] = []
    source = manifest.get("source") or {}
    notes.append(f"清单 {len(files)} 个文件，源 commit {source.get('commit', '未知')}")
    if missing:
        notes.append(f"缺失 {len(missing)} 个：{'、'.join(missing[:5])}{' …' if len(missing) > 5 else ''}")
    if changed:
        notes.append(f"内容不符 {len(changed)} 个：{'、'.join(changed[:5])}{' …' if len(changed) > 5 else ''}")
    return (not missing and not changed), notes


def _verified_python() -> str:
    """
    当前解释器**验证过**能 import 依赖就返回它的绝对路径，否则空串。

    为什么验证而不是直接写 `sys.executable`：`--install-deps` 那条路会把依赖装进
    一个**新 venv**，而当前解释器仍然是缺依赖的那个 —— 那时把它烘进去等于烘了个坏的。
    验证一次只要几十毫秒，换的是"烘进去的必定能用"。
    """
    try:
        completed = subprocess.run(
            [sys.executable, "-c", "import yaml, requests"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return sys.executable if completed.returncode == 0 else ""


def emit_host(root: Path, target: Path, python: str = "") -> Path:
    """
    生成 `host.local.js`：把安装期已知的两条绝对路径写进去。

    为什么要这样做：Host 半边拿不到自己的磁盘位置（没有 fs、没有 __dirname、没有 process），
    所以"自带的这份在哪"只能在**安装时**写进去。发布出去的 `host.js` 里那两行都是空串。

    * `DEFAULT_REPO_ROOT` —— 自带副本的绝对路径；
    * `DEFAULT_PYTHON` —— 哪个解释器**验证过**能 import 依赖（2026-09-20 加）：
      会话内改不了 `REPO_AUTOPILOT_PYTHON`（shell 继承 DSH 进程环境），
      不烘进去就只能每次手传 `python`。空串 = 不烘，退回盲探（与之前行为一致）。
    """
    source = (HERE.parent / "host.js").read_text(encoding="utf-8")
    marker = "const DEFAULT_REPO_ROOT = ''"
    if marker not in source:
        raise SystemExit("[!] host.js 里找不到 DEFAULT_REPO_ROOT 占位，无法生成（是不是被改过？）")
    emitted = source.replace(marker, f"const DEFAULT_REPO_ROOT = {json.dumps(str(root))}")
    if python:
        py_marker = "const DEFAULT_PYTHON = ''"
        if py_marker not in emitted:
            raise SystemExit("[!] host.js 里找不到 DEFAULT_PYTHON 占位，无法生成（是不是被改过？）")
        emitted = emitted.replace(py_marker, f"const DEFAULT_PYTHON = {json.dumps(str(python))}")
    target.write_text(emitted, encoding="utf-8")
    return target

REGISTER_STEPS = """\
注册成 Cordis Package：
  1) 先跑 `python scripts/install.py --emit-host` 生成 host.local.js
     （它把**随插件打包的那份 repo-autopilot** 的绝对路径写进 DEFAULT_REPO_ROOT）；
  2) 读取 **host.local.js** 的**全部内容**；
  3) cordis_define({{
       plugin: {{ kind: 'new', idPrefix: 'rauto' }},
       name: '<包名>',
       purpose: '<一句话用途>',
       code: {{ host: <host.local.js 的内容> }},
     }});
  4) 用返回的 pluginId / packageId 调 cordis_run（首次用 mode: 'run'）。
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


# --------------------------------------------------------------- 环境引导（uv / winget）
#
# 这一档**只做两件事**：告诉用户装什么、以及在用户明确要求时用 `uv` 建环境。
# 刻意**不自动下载**任何东西 —— 自动下载安装器会引入网络与信任面，收益不值这个价。

def detect_bootstrap_tools() -> dict[str, str | None]:
    """探测本机有没有可用的引导工具。没有就返回 None，绝不猜。"""
    return {name: shutil.which(name) for name in ("uv", "winget", "py")}


def bootstrap_advice(tools: dict[str, str | None], *, venv_dir: Path) -> list[str]:
    """给出**可复制粘贴**的补环境方案。按"本机实际有什么"分档。"""
    lines: list[str] = []
    if tools.get("uv"):
        lines.append(f"  · 检测到 uv（{tools['uv']}）—— 用它建一个干净环境：")
        lines.append(f"      uv venv \"{venv_dir}\" --python 3.12")
        lines.append(
            f"      uv pip install --python \"{venv_dir}\" pyyaml requests numpy"
        )
        lines.append(f"    装完把解释器指过去：$env:REPO_AUTOPILOT_PYTHON = \"{venv_dir / 'Scripts' / 'python.exe'}\"")
    elif tools.get("winget"):
        lines.append("  · 没有 uv，但有 winget —— 装一个（任选其一）：")
        lines.append("      winget install --id astral-sh.uv          # 推荐：uv 能顺带管 Python")
        lines.append("      winget install --id Python.Python.3.12    # 或者直接装 Python")
        lines.append("    装完**重开一个终端**再跑一次本脚本。")
    else:
        lines.append("  · 既没有 uv 也没有 winget —— 手工装 Python 3.12+：")
        lines.append("      https://www.python.org/downloads/")
    lines.append("  · 只想用现成的解释器？直接指过来：--python <解释器绝对路径>")
    return lines


def uv_bootstrap(tools: dict[str, str | None], venv_dir: Path, *, dry_run: bool) -> bool:
    """用 uv 建 venv 并装依赖。返回是否成功（uv 不存在直接返回 False）。

    `uv` 是 MIT/Apache-2.0 的开源工具，能同时管 Python 版本与依赖 ——
    这正是"本机没有 3.12"这个最大障碍的**低成本**解法。
    """
    uv = tools.get("uv")
    if not uv:
        return False
    commands = [
        [uv, "venv", str(venv_dir), "--python", "3.12"],
        [uv, "pip", "install", "--python", str(venv_dir), "pyyaml", "requests", "numpy"],
    ]
    for command in commands:
        print(f"  $ {' '.join(command)}")
        if dry_run:
            print("    （--dry-run：没有真的执行）")
            continue
        # 继承 stdio，不用管道 —— 沙箱里管道会 EPERM。
        code = subprocess.call(command)
        if code != 0:
            print(f"    [!] 退出码 {code}，停下。")
            return False
    return True


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
    parser.add_argument("--repo-root", help="repo-autopilot 仓库的绝对路径；不给就用随插件打包的自带副本")
    parser.add_argument("--python", help="要用哪个解释器（仅用于展示/建议；本脚本只检查自己所在的解释器）")
    parser.add_argument("--install-deps", action="store_true", help="缺依赖就用当前解释器的 pip 装上（默认不装）")
    parser.add_argument("--use-uv", action="store_true", help="缺依赖时用 uv 建一个 .venv 并装依赖（需要本机有 uv）")
    parser.add_argument("--emit-host", action="store_true", help="生成 host.local.js（把自带副本路径写进去）")
    parser.add_argument("--dry-run", action="store_true", help="只打印要做什么，不执行")
    args = parser.parse_args(argv)

    print("=== 1/4 解释器与依赖 ===")
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

    print("\n=== 2/4 环境引导（uv / winget）===")
    tools = detect_bootstrap_tools()
    print(f"  uv: {'有' if tools.get('uv') else '无'}｜winget: {'有' if tools.get('winget') else '无'}")
    if not missing:
        print("  依赖齐了，这一档用不上。")
    elif args.use_uv:
        venv_dir = Path.cwd() / ".venv"
        print(f"  用 uv 建环境（{venv_dir}）：")
        ok = uv_bootstrap(tools, venv_dir, dry_run=args.dry_run)
        if ok:
            print(f"  [OK] 建好了。把解释器指过去：$env:REPO_AUTOPILOT_PYTHON = \"{venv_dir / 'Scripts' / 'python.exe'}\"")
        else:
            print("  [!] uv 不在，或执行失败 —— 退回手工方案：")
            print("\n".join(bootstrap_advice(tools, venv_dir=venv_dir)))
    else:
        print("  缺依赖。补法（本脚本默认**不自动下载**，请挑一条自己执行）：")
        print("\n".join(bootstrap_advice(tools, venv_dir=Path.cwd() / ".venv")))

    print("\n=== 3/4 repo-autopilot 仓库 ===")
    repo_root: Path | None
    if args.repo_root:
        repo_root = Path(args.repo_root).expanduser()
        print(f"  （用参数指定的仓库根：{repo_root}）")
    else:
        repo_root = bundled_repo_root()
        if repo_root is not None:
            print(f"  （用**随插件打包的自带副本**：{repo_root}）")
        else:
            repo_root = autodetect_repo_root(Path.cwd())
            if repo_root is not None:
                print(f"  （没找到自带副本，改为自动查找到：{repo_root}）")

    if repo_root is None:
        print("  [缺] 既没有自带副本，也没给 --repo-root，自动查找也没找到。")
        print("        → 补法：--repo-root <repo-autopilot 仓库的绝对路径>")
        repo_checks: list[smoke.Check] = []
    else:
        repo_checks = smoke.check_repo_root(repo_root)
        print(smoke.render(repo_checks).rsplit("\n\n", 1)[0])

        # 自包含的关键：自带的那份**是不是原样**。用项目自己的 MANIFEST 逐文件验 sha256。
        if str(repo_root).startswith(str(BUNDLED)):
            ok, notes = verify_manifest(repo_root)
            for note in notes:
                print(f"  · {note}")
            if ok:
                print("  [OK] 自带副本与清单逐文件 sha256 一致（没有被改过）")
            else:
                print("  [缺] **自带副本与清单对不上** —— 别用它，换 --repo-root 指向一份干净的检出")
                repo_checks.append(
                    smoke.Check(name="自带副本完整性", ok=False, detail="；".join(notes),
                                fix="重新安装插件，或用 --repo-root 指向干净检出")
                )

    if args.emit_host:
        if repo_root is None:
            print("\n[!] 没有可写进 host.local.js 的路径，跳过 --emit-host")
        else:
            target = Path.cwd() / "host.local.js"
            if args.dry_run:
                print(f"\n（--dry-run）会生成 {target}，DEFAULT_REPO_ROOT = {repo_root}")
            else:
                python = _verified_python()
                written = emit_host(repo_root, target, python=python)
                print(f"\n[OK] 已生成 {written}")
                print(f"     DEFAULT_REPO_ROOT = {repo_root}")
                print(
                    "     DEFAULT_PYTHON     = "
                    + (python or "（没烘：当前解释器 import 不了 yaml/requests，调用时仍需传 python）")
                )
                print("     注册 Cordis Package 时**用这一份**（两条路径都写进去了）。")

    print("\n=== 4/4 结论 ===")
    everything = [*checks, *(repo_checks or [])]
    failed = [item for item in everything if not item.ok]
    if missing:
        print(f"  依赖缺 {len(missing)} 个：{'、'.join(missing)}")
        print(f"  （可以加 --install-deps 用当前解释器装，或 --use-uv 用 uv 建环境；"
              f"手工：{sys.executable} -m pip install {' '.join(missing)}）")
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
