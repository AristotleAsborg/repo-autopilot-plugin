"""把插件装进一个 **dsh profile**：先走包管理器（A），失败自动退回免包管理器（B）。

    python scripts/install_profile.py --profile web
    python scripts/install_profile.py --profile web --method bundle   # 强制走 B
    python scripts/install_profile.py --profile web --dry-run

## 两种方法，同一个终态

`dsh plugin --profile <p> add <spec>` 的本质是：

  1. pnpm 把包装进 `profiles/<p>/node_modules/`；
  2. `reconcilePlugins` 把**声明了 `dsh.bundle` 的依赖**追加进 `profiles/<p>/package.json`
     的 `dsh.profile.bundles`。

所以：
* **A（包管理器）**：先确保 pnpm 可用（PATH → corepack），再调 `dsh plugin add`。**需要网络。**
* **B（免包管理器）**：直接把本目录复制进 `node_modules/`，并补 `dependencies` 与
  `dsh.profile.bundles` —— **就是 A 会得到的终态**。本插件零依赖，所以这一步不需要解析依赖图。

为什么要 B：实测这台机器**没有 pnpm**（`dsh plugin add` 报 `'pnpm' is not recognized`），
而离线/内网环境拿不到它。A 失败时自动退 B，两边都不会卡死。

**安全**：改 `package.json` 前先备份成 `package.json.bak-install-profile-<时间戳>`；
写入用「读 → 改 → 写」并保持缩进；`--dry-run` 只说不做。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
PLUGIN_DIR = HERE.parent
#: 复制进 node_modules 时要排除的东西（都不是插件运行需要的）
COPY_EXCLUDE = {".git", ".cache", "__pycache__", ".pytest_cache", ".ruff_cache", "node_modules"}
#: pytest 在沙箱里留下的临时目录：**ACL 属受限主体，删不掉也读不了**。
#: 不排除的话复制会直接 `WinError 5` 失败（实测踩到）。
COPY_EXCLUDE_PREFIXES = ("pytest-cache-files-",)


def _excluded(name: str) -> bool:
    return name in COPY_EXCLUDE or name.startswith(COPY_EXCLUDE_PREFIXES)


def copy_plugin(destination: Path) -> list[str]:
    """
    把插件目录复制到 destination，**跳过读不了的东西并如实报告**。

    为什么不用 `shutil.copytree`：它对任何一个读不了的文件都会**整体失败** ——
    安装器不该因为一堆垃圾就装不上，但也**不能装作没发生**：跳过了什么必须列出来。
    """
    skipped: list[str] = []
    for root, dirs, files in os.walk(PLUGIN_DIR, onerror=lambda _error: None):
        here = Path(root)
        relative = here.relative_to(PLUGIN_DIR)
        dirs[:] = [name for name in dirs if not _excluded(name)]
        (destination / relative).mkdir(parents=True, exist_ok=True)
        for name in files:
            try:
                shutil.copy2(here / name, destination / relative / name)
            except OSError:
                skipped.append((relative / name).as_posix())
    return skipped


def default_dsh_home() -> Path:
    return Path(os.environ.get("DSH_HOME") or (Path.home() / ".dsh"))


def package_name() -> str:
    return json.loads((PLUGIN_DIR / "package.json").read_text(encoding="utf-8"))["name"]


def find_pnpm() -> list[str] | None:
    """pnpm 的调用前缀：PATH 上有就直用；否则试 corepack 代理（Node 自带）。"""
    found = shutil.which("pnpm")
    if found:
        return [found]
    corepack = shutil.which("corepack")
    if corepack:
        return [corepack, "pnpm"]
    return None


def try_package_manager(profile: str, dsh_bin: str, *, dry_run: bool) -> tuple[bool, str]:
    """A：走 dsh 自己的 `plugin add`。返回（成功与否, 说明）。"""
    pnpm = find_pnpm()
    if pnpm is None:
        return False, "本机既没有 pnpm，也没有 corepack —— 走不了包管理器这条路"
    if dry_run:
        return True, f"（--dry-run）会执行：{' '.join(pnpm)} … 以及 {dsh_bin} plugin --profile {profile} add"

    # corepack 首次使用要先把 pnpm 落地（需要网络；失败就退 B）
    if pnpm[0].lower().endswith("corepack") or pnpm[0].lower().endswith("corepack.cmd"):
        prepared = subprocess.call([*pnpm[:1], "prepare", "pnpm@latest", "--activate"])
        if prepared != 0:
            return False, "corepack 备 pnpm 失败（多半是没有网络）"

    code = subprocess.call([dsh_bin, "plugin", "--profile", profile, "add", str(PLUGIN_DIR)])
    if code != 0:
        return False, f"dsh plugin add 退出码 {code}"
    return True, "已通过 dsh plugin add 安装"


def force_remove(path: Path) -> None:
    """
    删掉一棵树，遇到**只读文件**先把只读位摘掉再删。

    为什么需要：`vendor/repo-autopilot/ROADMAP.md` 是**只读**的（那份路线图本来就是只读的），
    `shutil.copytree` 会把只读位一起带进安装副本，于是**第二次安装**（重装/升级）会在
    `rmtree` 上撞 `PermissionError: [WinError 5]`。装一次没问题、装第二次才炸 ——
    这种"只在重跑时出现"的缺陷必须在这里挡住。
    """
    if not path.exists():
        return
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                os.chmod(Path(root) / name, stat.S_IWRITE)
            except OSError:
                pass
    shutil.rmtree(path)


def install_by_bundle(profile_dir: Path, *, dry_run: bool) -> tuple[bool, str]:
    """B：不经过包管理器，直接把终态写出来。"""
    name = package_name()
    manifest_path = profile_dir / "package.json"
    if not manifest_path.is_file():
        return False, f"profile 里没有 package.json：{manifest_path}"

    target = profile_dir / "node_modules" / name
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    if dry_run:
        return True, f"（--dry-run）会把 {PLUGIN_DIR} 复制到 {target}，并更新 {manifest_path}"

    if target.exists():
        force_remove(target)
    target.mkdir(parents=True, exist_ok=True)
    skipped = copy_plugin(target)

    backup = manifest_path.with_name(f"package.json.bak-install-profile-{time.strftime('%Y%m%d-%H%M%S')}")
    backup.write_text(manifest_path.read_text(encoding="utf-8"), encoding="utf-8")

    dependencies = manifest.setdefault("dependencies", {})
    dependencies[name] = f"file:{PLUGIN_DIR.as_posix()}"
    dsh = manifest.setdefault("dsh", {})
    profile_section = dsh.setdefault("profile", {})
    bundles = profile_section.setdefault("bundles", [])
    if name not in bundles:
        bundles.append(name)
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    note = f"已直接安装到 {target}（备份：{backup.name}）"
    if skipped:
        note += f"；跳过 {len(skipped)} 个读不了的文件：{'、'.join(skipped[:3])}"
    return True, note


def verify(profile_dir: Path) -> tuple[bool, list[str]]:
    """装完自证：bundles 里有它、node_modules 里也有它、它的补丁文件能被解析到。"""
    name = package_name()
    notes: list[str] = []
    manifest_path = profile_dir / "package.json"
    if not manifest_path.is_file():
        return False, [f"没有 {manifest_path}"]
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    bundles = ((manifest.get("dsh") or {}).get("profile") or {}).get("bundles") or []
    ok = True
    if name in bundles:
        notes.append(f"profile 的 dsh.profile.bundles 里有 {name}")
    else:
        notes.append(f"**bundles 里没有 {name}** —— 不会装载")
        ok = False

    installed = profile_dir / "node_modules" / name
    if (installed / "package.json").is_file():
        notes.append(f"node_modules/{name} 在位")
    else:
        notes.append(f"**node_modules/{name} 不在** —— 解析不到")
        ok = False

    patch = installed / "cordis.patch.yml"
    notes.append(f"补丁文件：{'在' if patch.is_file() else '**不在**'}")
    ok = ok and patch.is_file()
    return ok, notes


def find_node(home: Path) -> str | None:
    """跑 boot 检查用的 node：`DSH_NODE` → PATH → DSH_HOME 旁边那套 runtime。"""
    for candidate in (
        os.environ.get("DSH_NODE"),
        shutil.which("node"),
        str(home.parent / "runtime" / "node" / "node.exe"),
    ):
        if candidate and Path(candidate).is_file():
            return str(candidate)
    return None


def find_dsh_bin(home: Path, explicit: str | None) -> str | None:
    """dsh 的入口 **.js**（boot 检查要用 node 直接执行它，所以 .cmd 不算）。"""
    for candidate in (
        explicit,
        os.environ.get("DSH_BIN"),
        str(
            home.parent
            / "runtime"
            / "dsh"
            / "node_modules"
            / "@deepseek-ai"
            / "dsh"
            / "lib"
            / "bin.js"
        ),
    ):
        if candidate and Path(candidate).is_file() and candidate.lower().endswith(".js"):
            return str(candidate)
    found = shutil.which("dsh")
    if found and found.lower().endswith(".js"):
        return found
    return None


def boot_check(
    home: Path,
    profile: str,
    *,
    node: str,
    dsh_bin: str | None,
    timeout_s: int = 120,
) -> tuple[bool, str]:
    """
    真机 boot 一次 —— **唯一会真的执行 `apply()` 的检查**。

    为什么非有这一步不可（2026-09-18 两次事故的共同形状）：
      * `dsh --dump-config` 只组合 patch 层就退出，**不加载插件代码**；
      * 安装器的 `verify()` 只看文件在不在；
      * 插件树加载失败**不会**写进 session 日志（会话还没建）。
    于是"装好了"和"DSH 起不来了"可以同时成立。判据只有一个：
    stdout 上出现 `dsh web: http://...` 那一行。

    用 `--port 0` 让系统挑空闲端口，所以正在跑的 harness 不会干扰这次检查，
    也**不需要**为了检查先把用户的 harness 停掉。
    """
    if dsh_bin is None:
        return False, "找不到 dsh 的 bin.js（用 --dsh-bin 指定；这一步不能当作已通过）"

    workspace = home.parent / "workspace"
    env = dict(os.environ)
    env["DSH_HOME"] = str(home)
    env.setdefault("DSH_TELEMETRY_MODE", "DISABLED")

    args = [node, dsh_bin, "--profile", profile, "--no-open", "--port", "0"]
    lines: list[str] = []
    try:
        proc = subprocess.Popen(
            args,
            cwd=str(workspace if workspace.is_dir() else home),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
    except OSError as error:
        return False, f"起不来 boot 检查子进程：{error}"

    def pump() -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            lines.append(line.rstrip())

    threading.Thread(target=pump, daemon=True).start()

    served = False
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if any("dsh web: http" in line for line in lines):
            served = True
            break
        if proc.poll() is not None:
            break
        time.sleep(0.25)

    # 无论结果如何都把子进程（及其子进程）收干净，别让探测自己变成僵尸 harness。
    try:
        subprocess.run(
            ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
            capture_output=True,
            check=False,
        )
    except OSError:
        pass
    try:
        proc.kill()
    except OSError:
        pass

    tail = "\n".join(f"        {line}" for line in lines[-12:]) or "        (没有任何输出)"
    if served:
        return True, "profile 带着这一行真的启动了（stdout 出现 `dsh web: http`）"
    return False, "profile 没有走到 serving 那一行，最后 12 行输出：\n" + tail


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")

    parser = argparse.ArgumentParser(description="把插件装进一个 dsh profile")
    parser.add_argument("--profile", required=True, help="profile 名字，例如 web")
    parser.add_argument("--dsh-home", help="DSH_HOME；默认取环境变量，再退到用户目录下的 .dsh")
    parser.add_argument("--dsh-bin", help="dsh 可执行文件；默认在常见位置里找")
    parser.add_argument("--method", choices=("auto", "manager", "bundle"), default="auto")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--boot-check",
        dest="boot_check",
        action="store_true",
        default=True,
        help="装完真的 boot 一次这个 profile，要求它 serve（默认开，--port 0 所以不撞正在跑的 harness）",
    )
    parser.add_argument(
        "--no-boot-check",
        dest="boot_check",
        action="store_false",
        help="跳过 boot 检查；**不要**拿 --dump-config 当代替品（它不加载插件代码）",
    )
    args = parser.parse_args(argv)

    home = Path(args.dsh_home) if args.dsh_home else default_dsh_home()
    profile_dir = home / "profiles" / args.profile
    print(f"DSH_HOME : {home}")
    print(f"profile  : {args.profile} -> {profile_dir}")
    print(f"插件目录 : {PLUGIN_DIR}")

    if not profile_dir.is_dir():
        print(f"[缺] 这个 profile 目录不存在：{profile_dir}")
        print("     → 先确认 profile 名字（启动命令里的 --profile <名字>）")
        return 1

    dsh_bin = args.dsh_bin or shutil.which("dsh") or ""
    detail = ""
    used = ""
    if args.method in ("auto", "manager"):
        if dsh_bin:
            ok, detail = try_package_manager(args.profile, dsh_bin, dry_run=args.dry_run)
        else:
            ok, detail = False, "找不到 dsh 可执行文件（用 --dsh-bin 指定）"
        print(f"\n[A 包管理器] {'成功' if ok else '失败'}：{detail}")
        if ok:
            used = "A"
        elif args.method == "manager":
            return 1
        else:
            print("            → 自动退回 B（免包管理器）")

    if not used:
        print("\n[B 免包管理器] 直接写出终态")
        ok, detail = install_by_bundle(profile_dir, dry_run=args.dry_run)
        print(f"            {'成功' if ok else '失败'}：{detail}")
        if not ok:
            return 1
        used = "B"

    print("\n=== 自证 ===")
    ok, notes = verify(profile_dir)
    for note in notes:
        print(f"  · {note}")
    print(f"结论：{'**装好了**' if ok else '**没装好**'}（方法 {used}）")
    if ok and not args.dry_run:
        print("\n下一步：**重开一个会话**（或重启 harness），然后试 repo_autopilot_check(mode='doctor')")
        print(f"卸载（B 装法）：删 {profile_dir / 'node_modules' / package_name()}，并从 bundles 里去掉 {package_name()}")
    if ok and not args.dry_run and args.boot_check:
        node = find_node(home)
        dsh_bin = find_dsh_bin(home, args.dsh_bin)
        print("\n[boot check] -------------------------------")
        if node is None:
            ok = False
            print("  ** 找不到 node（设 DSH_NODE，或用 --no-boot-check 明确跳过）")
        else:
            served, detail = boot_check(home, args.profile, node=node, dsh_bin=dsh_bin)
            print(f"  {'ok  ' if served else 'FAIL'} {detail}")
            ok = ok and served
    elif ok and not args.dry_run:
        print("\n[.. boot ..] 已被 --no-boot-check 跳过 —— 这一步没跑，别把它理解成通过")

    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
