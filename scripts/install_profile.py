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


def copy_plugin(destination: Path, source: Path | None = None) -> list[str]:
    """
    把 `source`（默认=插件目录）复制到 destination，**跳过读不了的东西并如实报告**。

    为什么不用 `shutil.copytree`：它对任何一个读不了的文件都会**整体失败** ——
    安装器不该因为一堆垃圾就装不上，但也**不能装作没发生**：跳过了什么必须列出来。
    """
    origin = source or PLUGIN_DIR
    skipped: list[str] = []
    for root, dirs, files in os.walk(origin, onerror=lambda _error: None):
        here = Path(root)
        relative = here.relative_to(origin)
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
    # 联接/符号链接：**只删链接本身**。`shutil.rmtree` 会直接抛
    # `Cannot call rmtree on a symbolic link`（实测），而递归删更危险 —— 会顺着链接
    # 把**目标**（工作区里那份）一起删掉。
    if path.is_symlink():
        os.rmdir(path)
        return
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                os.chmod(Path(root) / name, stat.S_IWRITE)
            except OSError:
                pass
    shutil.rmtree(path)


class NeedsOutsideSandbox(RuntimeError):
    """
    目标在工作区之外，被文件沙箱拦下了 —— **不要在这里反复申请提权**，
    把"请在沙箱外跑一次"的那条命令交给人类。

    为什么：这份部署的沙箱是**故意**钉成 `workspace-write` 的
    （`%DSH_HOME%\\cordis.patch.yml` 里显式写明 `danger-full-access` 不启用，
    并把该预设从选择表里删掉以防误点）。而装 profile 必须写 `%DSH_HOME%\\profiles\\...`，
    天然在工作区之外 —— 于是 agent 每跑一次就要人批一次。

    正确的分工：**用户双击 `install.cmd`（不经沙箱、零提示）；
    agent 只把命令交出去**，而不是一次次触发审批。
    """

    def __init__(self, command: str, error: OSError) -> None:
        super().__init__(str(error))
        self.command = command
        self.error = error


def install_by_bundle(
    profile_dir: Path, *, dry_run: bool, bake_root: Path | None = None
) -> tuple[bool, str]:
    """B：不经过包管理器，直接把终态写出来。"""
    name = package_name()
    manifest_path = profile_dir / "package.json"
    if not manifest_path.is_file():
        return False, f"profile 里没有 package.json：{manifest_path}"

    target = profile_dir / "node_modules" / name
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    if dry_run:
        return True, f"（--dry-run）会把 {PLUGIN_DIR} 复制到 {target}，并更新 {manifest_path}"

    # 从这里开始的写都落在 %DSH_HOME% 里 —— 也就是**会话工作区之外**。
    # 沙箱会拒（这份部署是故意钉成 workspace-write 的），这时**不要**一遍遍申请提权，
    # 而是把"请在沙箱外跑一次"的命令交给人类（见 NeedsOutsideSandbox 的说明）。
    # 注意**删除也算写**：升级时先 force_remove 旧副本，那一步同样会被拦，所以它在 try 里。
    outside_hint = f'python "{PLUGIN_DIR / "scripts" / "install_profile.py"}" --profile {profile_dir.name}'
    try:
        if target.exists():
            force_remove(target)
        target.mkdir(parents=True, exist_ok=True)
        skipped = copy_plugin(target)

        backup = manifest_path.with_name(
            f"package.json.bak-install-profile-{time.strftime('%Y%m%d-%H%M%S')}"
        )
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
    except OSError as error:
        raise NeedsOutsideSandbox(outside_hint, error) from error

    note = f"已直接安装到 {target}（备份：{backup.name}）"
    if bake_root is not None:
        # **必须在复制之后**：先烘再装会被 copy_plugin 覆盖回空串（实测）。
        ensure_workspace_repo(bake_root)
        baked = bake_default_repo_root(target / "lib" / "index.js", bake_root)
        note += f"；已烘入 {baked}"
        note += f"；运行期 state 落在 {bake_root / 'state'}"
    if skipped:
        note += f"；跳过 {len(skipped)} 个读不了的文件：{'、'.join(skipped[:3])}"
    return True, note


def install_by_junction(profile_dir: Path, link_root: Path, *, dry_run: bool) -> tuple[bool, str]:
    """
    B′：**让代码本身落在工作区内**，于是 repo-autopilot 的 `ROOT/state` 也在工作区里。

    为什么不是 A′（改 state 解析）：查过源码 —— `state/` 里同时住着"随包发的金样本"
    与"运行期要写的东西"，而且有文件两者都是（`state/progress.md` 被 `package.py:54`
    的 STATE_FILES 打进包、又由 `acceptance.py` 写；`state/corpus/triage-adjudicated.jsonl`
    是金样本又只增不改）。一个权威 STATE_DIR 指向哪边都会坏另一边 —— 不可调和。
    把**代码**挪进工作区则两边都满足，且一个源码都不用改。

    做法：插件复制到 `link_root`（工作区内），再从 profile 的 `node_modules` 建一个
    **目录联接**指过去。写 `%DSH_HOME%` 只在建联接与改一次清单时发生（一次性），
    之后卡片/报告/台账等**运行期写全在工作区内 → 零提权**。
    """
    name = package_name()
    manifest_path = profile_dir / "package.json"
    link = profile_dir / "node_modules" / name
    if not manifest_path.is_file():
        return False, f"profile 里没有 package.json：{manifest_path}"

    state_root = link_root / "vendor" / "repo-autopilot" / "state"

    if dry_run:
        return True, (
            f"（--dry-run）会把插件复制到 {link_root}，建联接 {link} -> {link_root}；"
            f"运行期 state 落在 {state_root}"
        )

    outside_hint = (
        f'python "{PLUGIN_DIR / "scripts" / "install_profile.py"}" '
        f'--profile {profile_dir.name} --method junction'
    )
    try:
        if link_root.exists():
            force_remove(link_root)
        link_root.mkdir(parents=True, exist_ok=True)
        skipped = copy_plugin(link_root)

        # 拆旧联接：**只删联接本身，绝不递归删**（否则会顺着联接把工作区那份删掉）。
        if link.exists() or link.is_symlink():
            try:
                os.rmdir(link)
            except OSError:
                force_remove(link)
        link.parent.mkdir(parents=True, exist_ok=True)
        code = subprocess.call(["cmd", "/c", "mklink", "/J", str(link), str(link_root)])
        if code != 0:
            return False, f"mklink /J 失败（退出码 {code}）—— 联接建不起来，退回 copy 装法"

        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        backup = manifest_path.with_name(
            f"package.json.bak-install-profile-{time.strftime('%Y%m%d-%H%M%S')}"
        )
        backup.write_text(manifest_path.read_text(encoding="utf-8"), encoding="utf-8")
        dependencies = manifest.setdefault("dependencies", {})
        dependencies[name] = f"file:{link_root.as_posix()}"
        bundles = (
            manifest.setdefault("dsh", {}).setdefault("profile", {}).setdefault("bundles", [])
        )
        if name not in bundles:
            bundles.append(name)
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    except OSError as error:
        raise NeedsOutsideSandbox(outside_hint, error) from error

    note = f"已装成工作区内的联接：{link} -> {link_root}"
    note += f"；运行期 state 落在 {state_root}"
    if skipped:
        note += f"；跳过 {len(skipped)} 个读不了的文件"
    return True, note


BAKE_MARKER = "const DEFAULT_REPO_ROOT = "


def bake_default_repo_root(module_path: Path, repo_root: Path) -> str:
    """
    把 `DEFAULT_REPO_ROOT` 填进**已安装**的 `lib/index.js`。

    profile 层装的是 `lib/index.js`（由 `build_module.py` 从 `host.js` 生成），
    里面的 `DEFAULT_REPO_ROOT` 是**空串占位** —— 不填的话工具一被调用就说
    「没有 repo_root，且这份 host.js 里也没有内置路径」：**装载成功但用不了**。
    （dynamic Package 那条路由 `install.py --emit-host` 负责同一件事。）

    ⚠️ **必须在复制之后做**：实测先烘再装会被 `copy_plugin` 覆盖回空串。
    """
    lines = module_path.read_text(encoding="utf-8").splitlines(keepends=True)
    hits = [i for i, line in enumerate(lines) if line.strip().startswith(BAKE_MARKER)]
    if len(hits) != 1:
        raise OSError(f"期望恰好一处 `{BAKE_MARKER}` 占位，实际 {len(hits)} 处 —— 拒绝改")
    lines[hits[0]] = f"{BAKE_MARKER}{json.dumps(str(repo_root))}\n"
    module_path.write_text("".join(lines), encoding="utf-8")
    return lines[hits[0]].strip()


def ensure_workspace_repo(link_root: Path) -> tuple[bool, str]:
    """
    在工作区内备一份 repo-autopilot（**运行期 state 就落在它旁边**）。

    为什么：repo-autopilot 把 `state/` 绑在代码旁边（`STATE_DIR = ROOT / "state"`），
    而 `state/` 里混着"随包发的金样本"与"运行期写的东西"（有文件两者都是），
    一个权威 STATE_DIR 指向哪边都会坏另一边 —— 所以只能让**代码**落在工作区内。
    返回（是否新建, 说明）。
    """
    if (link_root / "scripts" / "doctor.py").is_file():
        return False, f"已存在，复用：{link_root}"
    link_root.mkdir(parents=True, exist_ok=True)
    # **复制的是 repo-autopilot 子树**，不是插件目录 —— 目标是让 link_root 本身
    # 成为一个 repo-autopilot 根（这样 ROOT/state 就在工作区里）。
    skipped = copy_plugin(link_root, source=PLUGIN_DIR / "vendor" / "repo-autopilot")
    note = f"已从自带副本复制到 {link_root}"
    if skipped:
        note += f"（跳过 {len(skipped)} 个读不了的文件）"
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
    parser.add_argument(
        "--method",
        choices=("auto", "manager", "junction", "bundle"),
        default="auto",
        help="auto = 依次试 manager → junction → bundle；junction 让代码落在工作区内（推荐）",
    )
    parser.add_argument(
        "--link-root",
        help="junction 装法里「工作区内那份副本」的位置；默认 <当前目录>/.repo-autopilot",
    )
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
    # 默认放在**插件目录的上一级**（本项目里那一级就是会话工作区），
    # **不要**依赖 cwd：实测从插件目录里跑会把副本建进插件自己的 git 仓，
    # 于是运行期写的卡片/报告会弄脏插件仓。要改就显式 --link-root。
    link_root = Path(args.link_root) if args.link_root else (PLUGIN_DIR.parent / ".repo-autopilot")
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
            print("            → 先试 B′（工作区内副本 + 联接）")

    if not used and args.method in ("auto", "junction"):
        print(f"\n[B′ 工作区内副本 + 目录联接] 副本位置：{link_root}")
        try:
            ok, detail = install_by_junction(profile_dir, link_root, dry_run=args.dry_run)
        except NeedsOutsideSandbox as blocked:
            print("            [!] 写不进去：目标在工作区之外，被文件沙箱拦下了")
            print(f"                {blocked.error}")
            print()
            print("            → 请在**沙箱之外**（资源管理器双击 install.cmd，或普通终端）执行：")
            print(f"                {blocked.command}")
            print("            → 这一步只需做一次；之后运行期写全在工作区内。")
            return 3
        print(f"            {'成功' if ok else '失败'}：{detail}")
        if ok:
            used = "B′"
        elif args.method == "junction":
            return 1
        else:
            print("            → 联接建不起来，退回 B（复制进 profile）")

    if not used:
        print("\n[B 免包管理器] 直接写出终态")
        try:
            ok, detail = install_by_bundle(profile_dir, dry_run=args.dry_run, bake_root=link_root)
        except NeedsOutsideSandbox as blocked:
            # **不要**在这里申请提权：这份部署的沙箱是故意钉死的。
            # 正确的做法是把命令交出去 —— 用户双击或在普通终端里跑，零提示、一次装完。
            print("            [!] 写不进去：目标在工作区之外，被文件沙箱拦下了")
            print(f"                {blocked.error}")
            print()
            print("            → 请在**沙箱之外**（资源管理器双击 install.cmd，或普通终端）执行：")
            print(f"                {blocked.command}")
            print("            → 装完重开会话即可；本步不需要 agent 提权。")
            return 3
        print(f"            {'成功' if ok else '失败'}：{detail}")
        if not ok:
            return 1
        used = "B"

    print("\n=== 自证 ===")
    if args.dry_run:
        print("  （--dry-run：**什么都没装**；下面是这个 profile **当前**的状态）")
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
