"""打包一份**可安装的副本**（人类 2026-09-12 要求：先打好，不急着装）。

    python tools/package.py list              # 干跑：列出会打包 / 不会打包的路径
    python tools/package.py build             # 生成 ..\\pkg\\repo-autopilot-<日期>\\
    python tools/package.py verify <目录>     # 按 MANIFEST.json 逐个文件校验 sha256

## 打包什么、不打包什么（这是这个工具存在的全部理由）

一个能装的副本需要**代码 + 状态骨架 + 金样本**，但**不需要证据**：

| 类别 | 内容 | 进包吗 |
|---|---|---|
| 代码与配置 | `src/ tools/ scripts/ tests/ skills/ config/ .github/ pytest.ini .gitignore` | ✅ |
| 文档 | `README.md ROADMAP.md AGENTS.md` + `INSTALL.md`（来自 `packaging/`） | ✅ |
| 状态**骨架** | 12 个运行期子目录（空）+ `capabilities.yaml` + `mode.json` + `progress.md` | ✅ |
| **金样本** | `state/corpus/`、`state/scout-corpus/`（3.1/3.2/5.1 的验收输入，不带上就跑不动） | ✅ |
| 证据 | `state/reports/**`、`state/findings/`、`state/drill/`、验收日志 | ❌ |
| 运行期产物 | `state/runtime/ sandbox/ e2e/ vendor/ gate/ scaffold/ tasks/*/*.json …` | ❌ |
| 其它 | `.git/`、各种 cache、**本地生成的 fixtures**（陪练仓库/对抗样本：`tools/sandbox_repos.py build` 重建） | ❌ |

> **一条硬规则：只打 git 跟踪的文件。** 包的内容 = 某个 commit 的树的子集，
> 所以清单里每个文件都能在源仓库里查到，垃圾无从进入（第一版用"遍历+排除清单"，
> 结果把本地生成的 10MB 陪练仓库当成源码装了进去 —— 排除清单一定会漏）。

**默认输出到仓库外面**（`..\\pkg\\repo-autopilot-<日期>\\`）：包不该生在要打包的那棵树里，
否则下一次打包会把自己装进去（这类"包里有包"的循环很常见，且会越来越大）。

## 为什么带 `MANIFEST.json` 与 `verify`

安装前必须能回答"我装进去的到底是什么"：清单里记着源仓库的 commit、生成时间、
每个文件的 sha256 与字节数。`verify` 会**逐个文件重算**（少一个、多一个、改一个字节都报），
因为"我打包的时候是好的"不是证据，"现在再核一遍还是好的"才是。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

#: 代码/配置/文档（**整目录**打包，但只限 git 跟踪的文件）
INCLUDE_DIRS = ("src", "tools", "scripts", "tests", "skills", "config", ".github")
INCLUDE_FILES = ("pytest.ini", ".gitignore", ".gitattributes", "README.md", "ROADMAP.md", "AGENTS.md")
#: 状态骨架里**必须带上的文件**（验收 0.1/0.2 就靠它们）
STATE_FILES = ("capabilities.yaml", "mode.json", "progress.md")
#: 金样本目录（验收输入，必须带上）
STATE_GOLDEN = ("corpus", "scout-corpus")


def required_dirs() -> tuple[str, ...]:
    """状态骨架用到哪些子目录 —— 直接问自检模块，避免两处清单漂移。"""
    from src.skills.doctor import REQUIRED_DIRS

    return tuple(REQUIRED_DIRS)


def git_commit() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=False
        )
        return out.stdout.strip() or "unknown"
    except OSError:
        return "unknown"


def git_dirty() -> bool:
    """
    工作区是否脏 —— **只看会进包的那些路径**。

    为什么不能直接看 `git status --porcelain` 的整体输出（2026-09-18 改）：
    端到端用例**故意**把人工检查材料落到 `state/reports/`
    （`tests/integration/test_spec_refiner.py`，理由写在那边：要留给人看，不能跑完就被冲掉），
    而 `state/reports/` 根本不进包。于是**每跑一次全量测试，包就被记成 dirty**，
    哪怕包内一个字节都没动。`dirty` 一旦总是真，就等于没有这个标记 ——
    而"这个包到底是不是某个干净 commit 的产物"正是它唯一要说清楚的事。
    """
    out = subprocess.run(["git", "status", "--porcelain"], cwd=ROOT, capture_output=True, text=True, check=False)
    for line in out.stdout.splitlines():
        if len(line) < 4:
            continue
        # porcelain 形如 `XY path`；重命名是 `XY old -> new`，两边都要看。
        changed = line[3:].strip().strip('"')
        candidates = [part.strip().strip('"') for part in changed.split(" -> ")]
        if any(not is_local_only(path) for path in candidates if path):
            return True
    return False


def git_tracked() -> list[str] | None:
    """
    返回 git 跟踪的路径列表；**不是 git 工作树时返回 None**。

    为什么要有这个区分：装到别的机器上的副本**没有 `.git`**（INSTALL.md 明说"不需要 git 历史"），
    而"只打 git 跟踪的文件"那条规则在非 git 树里根本无从谈起 —— 实测就是：
    在安装副本里跑它自己的测试，`git ls-files` 空手而归，于是 `plan()` 返回空，
    `test_package_*` 全红（看起来像"装坏了"，其实是"规则只对 git 树成立"）。
    """
    try:
        out = subprocess.run(
            ["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, text=True, check=False
        )
    except OSError:
        return None
    if out.returncode != 0:
        return None
    items = [item for item in out.stdout.split("\0") if item]
    return items or None


def fallback_walk() -> list[str]:
    """
    非 git 工作树时的退路：按"包含规则"走一遍文件系统。

    **这是降级，不是等价**：它保证不了"包的内容 = 某个 commit 的树"，
    所以调用方必须把这件事说清楚（`plan()` 的 notes 里会写）。
    排除 `sandbox-repos/`（生成物，含 10MB 对抗样本）与各类缓存。
    """
    skip_parts = {"__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache", "sandbox-repos"}
    found: list[str] = []
    for name in INCLUDE_FILES:
        if (ROOT / name).is_file():
            found.append(name)
    for directory in INCLUDE_DIRS:
        base = ROOT / directory
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            if not path.is_file():
                continue
            relative = path.relative_to(ROOT)
            if any(part in skip_parts for part in relative.parts):
                continue
            if path.suffix in {".pyc", ".pyo"}:
                continue
            found.append(relative.as_posix())
    # 状态骨架：占位符 + 骨架文件 + 金样本
    state_root = ROOT / "state"
    if state_root.is_dir():
        for path in sorted(state_root.rglob("*")):
            if not path.is_file():
                continue
            relative = path.relative_to(ROOT).as_posix()
            if relative.endswith("/.gitkeep") or is_state_keep(relative):
                found.append(relative)
    if (ROOT / "packaging" / "INSTALL.md").is_file():
        found.append("packaging/INSTALL.md")
    return found


def plan() -> tuple[list[Path], list[str]]:
    """
    算出（要打包的文件, 说明行）。**优先只打 git 跟踪的文件** —— 这是这个工具最重要的一条规则。

    为什么用 `git ls-files` 而不是"遍历目录再排除"：遍历法必须靠一张排除清单去猜哪些是垃圾，
    而清单一定会漏（实测第一版就把**本地生成的** 10MB 陪练仓库/对抗样本当成源码装了进去，
    还差点把缓存和证据一起带上）。反过来做就干净了：**包的内容 = 某个 commit 的树的子集**，
    清单里每一个文件都能在源仓库里查到，垃圾根本无从进入。

    代价是"包里的内容必须先提交"（工具会提示工作区是否 dirty）；
    而**没有 `.git` 的部署副本**会退回遍历法，并**明说这是降级**（见 `fallback_walk`）。
    """
    files: list[Path] = []
    notes: list[str] = []

    tracked = git_tracked()
    degraded = tracked is None
    candidates = fallback_walk() if degraded else tracked

    def keep(relative: str) -> bool:
        if relative in INCLUDE_FILES or relative == "packaging/INSTALL.md":
            return True
        if relative.split("/")[0] in INCLUDE_DIRS:
            return True
        return is_state_keep(relative)

    for relative in candidates or []:
        if keep(relative):
            files.append(ROOT / relative)
    if degraded:
        notes.append(
            "**这不是 git 工作树**（部署副本没有 .git）：改用遍历法，"
            f"因此不能保证包等于某个 commit 的树；文件数 {len(files)}"
        )
    else:
        notes.append(f"git 跟踪的文件里，符合打包规则的有 {len(files)} 个")

    state_files = sum(1 for path in files if path.relative_to(ROOT).parts[0] == "state")
    notes.append(f"其中状态骨架与金样本 {state_files} 个")
    return files, notes


def is_state_keep(relative: str) -> bool:
    """
    状态目录里只有这三样进包：骨架文件 + 两份金样本。**证据与运行期一律不带。**

    外加一条 2026-09-12 补上的：**骨架目录里的 `.gitkeep` 必须带上**。
    git 不跟踪空目录，所以"刚 clone 下来的仓库里 `state/tasks/pending/` 不存在"——
    而 1.1 的验收与 `scripts/doctor.py` 都会检查这些目录。占位文件就是让
    "干净的 checkout 也能跑"的那件东西（CI 第一次跑就红在这一条上，
    报的是 13 个 `test_required_directory_exists` 失败）。
    """
    if relative in {f"state/{name}" for name in STATE_FILES}:
        return True
    if relative.endswith("/.gitkeep") and relative.startswith("state/"):
        return True
    return any(relative.startswith(f"state/{name}/") for name in STATE_GOLDEN)


def dest_relative(path: Path) -> str:
    """文件在**包里**的相对路径（`packaging/INSTALL.md` 提到包根当 `INSTALL.md`）。"""
    relative = path.relative_to(ROOT).as_posix()
    return "INSTALL.md" if relative == "packaging/INSTALL.md" else relative


def digest(path: Path) -> str:
    sha = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            sha.update(chunk)
    return sha.hexdigest()


def target_name() -> str:
    return f"repo-autopilot-{datetime.now(timezone.utc).strftime('%Y%m%d')}"


def force_rmtree(path: Path) -> None:
    """
    删掉一棵目录树，**先把只读位清掉**。

    实测（2026-09-12）：源仓库的 `ROADMAP.md` 是**只读**的（人类有意设的：路线文档不让人随手改），
    而 `shutil.copy2` 会把只读位一起带进包里 —— 于是下一次打包时 `shutil.rmtree` 直接
    `WinError 5`，包**重建不了**。这不是"权限不够"，是包自己在跟自己的属性较劲。
    """
    import os
    import stat

    for root, dirs, files in os.walk(path):
        for name in [*dirs, *files]:
            try:
                os.chmod(Path(root) / name, stat.S_IWRITE)
            except OSError:
                pass
    shutil.rmtree(path)


def build(out_root: Path) -> int:
    files, notes = plan()
    if not files:
        print("没有可打包的文件 —— 是不是在错误的目录里跑？")
        return 1
    if git_dirty():
        print("**注意**：工作区有未提交的改动 —— 包里会包含它们（清单里记为 dirty）")

    target = out_root / target_name()
    if target.exists():
        force_rmtree(target)
    target.mkdir(parents=True)

    for path in files:
        destination = target / dest_relative(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, destination)

    # 状态骨架：12 个空目录（验收 0.1 的 doctor 会逐个检查）
    for name in required_dirs():
        (target / "state" / name).mkdir(parents=True, exist_ok=True)

    entries = {
        dest_relative(path): {"sha256": digest(path), "bytes": path.stat().st_size} for path in files
    }
    manifest = {
        "name": target.name,
        "built_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": {"path": str(ROOT), "commit": git_commit(), "dirty": git_dirty()},
        "counts": {"files": len(entries), "bytes": sum(item["bytes"] for item in entries.values())},
        "state_skeleton_dirs": list(required_dirs()),
        "install_hint": "把整个目录拷到目标机，按 INSTALL.md 验证；不需要 git 历史。",
        "files": entries,
    }
    manifest_path = target / "MANIFEST.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print("打包完成：")
    for note in notes:
        print(f"  · {note}")
    print(f"  · 输出：{target}")
    print(f"  · 文件 {manifest['counts']['files']} 个，合计 {manifest['counts']['bytes'] / 1024 / 1024:.1f} MB")
    print(f"  · 源 commit：{manifest['source']['commit']}{'（dirty）' if manifest['source']['dirty'] else ''}")
    print(f"  校验：python tools/package.py verify \"{target}\"")
    return 0


#: 安装之后**必然会出现**的本机自有文件：凭证与运行期产物。
#: 它们按设计不进包，所以 `verify` 不该把它们当成"包被改过"（2026-09-12 实测：
#: INSTALL.md 让人类装完跑 `verify .`，而刚放好 token 的目录立刻就报"校验不通过" ——
#: 那句话会让人以为包坏了，其实只是"凭证当然不在清单里"）。
LOCAL_ONLY_PREFIXES = (
    "state/.write_token",
    "state/GH_READ_TOKEN",
    "state/runtime/",
    "state/reports/",
    "state/approvals/",
    "state/approvals-check/",
    "state/outbox/",
    "state/tasks/",
    "state/drill/",
    "state/sandbox/",
    "state/e2e/",
    "state/vendor/",
    "state/gate/",
    "state/scaffold/",
    "state/patches/",
    "state/repair/",
    "state/eval-repair/",
    # 批量处理的进度账簿：**运行期状态**，装了之后跑一次 `/一键处理` 就会出现。
    # 2026-09-13 之前它连 git 都跟踪着（见 .gitignore 里那一段），于是一次真实的批量
    # 演练会把它写进安装副本，而 `verify .` 会把它报成"多出来的文件 = 包被改过"。
    "state/batch_",
    # **M1 的 spec 卡**（`/细化idea` 的产物）与 **M2 的向量库**（`state/vectors/issues.npy`）。
    # 2026-09-15 踩到：给工作区外那份安装副本做升级时才发现 —— 它跑过 `/细化idea`，
    # `state/specs/` 下有三张卡，于是 `verify .` 立刻报"多出来的文件"。
    # 这两个目录进包的只有 `.gitkeep`（源仓库也只跟踪 `.gitkeep`），
    # 所以其它内容一律是**装完之后跑出来的产物**，不是"包被改过"的证据。
    "state/specs/",
    "state/vectors/",
    ".cache/",
)


#: 进了安装副本之后**必然会出现**的编译/缓存产物（跑一次测试或 import 一次就有）。
#: 与上面那些凭证/运行期文件同理，它们不是"包被改过"的证据。
CACHE_DIR_NAMES = ("__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache")
CACHE_SUFFIXES = (".pyc", ".pyo")


def is_local_only(relative: str) -> bool:
    if any(relative.startswith(prefix) for prefix in LOCAL_ONLY_PREFIXES):
        return True
    # 2026-09-13 实测踩到：刷新安装副本后先跑了全量测试，再跑 `verify .` 就报
    # `src/dedupe/__pycache__/__init__.cpython-312.pyc` 是"多出来的文件"。
    if any(part in CACHE_DIR_NAMES for part in relative.split("/")):
        return True
    return relative.endswith(CACHE_SUFFIXES)


def verify(target: Path) -> int:
    manifest_path = target / "MANIFEST.json"
    if not manifest_path.is_file():
        print(f"没有清单：{manifest_path}")
        return 1
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    expected: dict[str, dict] = manifest.get("files") or {}

    missing: list[str] = []
    changed: list[str] = []
    for relative, meta in expected.items():
        path = target / relative
        if not path.is_file():
            missing.append(relative)
            continue
        if digest(path) != meta.get("sha256"):
            changed.append(relative)

    found = {
        path.relative_to(target).as_posix()
        for path in target.rglob("*")
        if path.is_file() and path.name != "MANIFEST.json"
    }
    unexpected = found - set(expected)
    extra = sorted(item for item in unexpected if not is_local_only(item))
    local_only = sorted(item for item in unexpected if is_local_only(item))

    print(f"校验 {target}")
    print(f"  清单声明：{len(expected)} 个文件")
    for label, items in (("缺失", missing), ("内容不符", changed), ("多出来的文件", extra)):
        if items:
            print(f"  **{label}** {len(items)} 个：")
            for item in items[:10]:
                print(f"    - {item}")
    if local_only:
        # 这些是**装完之后本来就该有的**（凭证 + 运行期产物）：它们不是"包被改过"的证据。
        print(f"  （另有 {len(local_only)} 个本机自有文件，按设计不在清单里：{local_only[:5]}…）")
    if missing or changed or extra:
        print("结论：**校验不通过**（包被改过或拷坏了）")
        return 1
    print("结论：逐文件 sha256 全部一致 —— 这个包就是清单里描述的那个包")
    return 0


def list_plan() -> int:
    files, notes = plan()
    print("干跑（不会写任何文件）：")
    for note in notes:
        print(f"  · {note}")
    print(f"  · 合计 {len(files)} 个文件")
    print("  不会打包：state/reports/**、state/findings/**、state/drill/**、state/runtime/**、")
    print("            运行期产物（sandbox/e2e/vendor/gate/scaffold/tasks 里的任务文件）、.git、各种 cache")
    return 0


def compare(target: Path) -> int:
    """
    **逐文件哈希比对两棵树**：源仓库（权威） vs 任意一份副本。

    为什么需要它（2026-09-17 的教训）：跨副本协作时，"报告说改了 X" 与 "仓库里真有 X"
    是两件事。那一次报告 §三 写着"源仓库改动：`src/scaffold/core.py`"，
    实际那两个文件**只改在副本里**，源仓库/存档包/GitHub 三个地方都没有 ——
    如果只看叙述就往下走，那次修复会随下一次覆盖消失。
    所以规矩是：**覆盖/合并任何副本之前，先跑一次这个比对**，
    把"差在哪几个文件"摆出来；不一致就先问清是谁改的，再决定谁让谁。

    退出码：`0` 没有合并风险（本机自有文件、以及副本自己放进去的额外脚本都不算）；
    `1` **有合并风险**（包内文件内容不同 / 副本缺了包内文件 / 副本里留着源仓库已经删掉的旧包内文件）；
    `2` 目标不存在。

    **为什么"副本多出文件"不算失败**：一份工作副本里多出探针脚本是常态（本机那份就有 17 个），
    合并时它们不会被覆盖、也不会污染产品；把它们当成"不一致"只会让这个检查永远红着，
    而永远红的检查等于没有检查。真正的风险只有三种：
    ① 包内文件**被改过**（源仓库没有那个改动 —— 2026-09-17 那次翻车的形态）；
    ② 包内文件**缺失**（合并后会"少东西"）；
    ③ 副本里留着**源仓库已经删掉**的旧包内文件（靠副本自己的 `MANIFEST.json` 认出来）。
    多出的文件仍然**列出来给人看**，只是不参与判定。

    与 `verify()` 的区别：`verify` 拿**包自己的清单**核对"这个包有没有被改坏"；
    `compare` 拿**源仓库的清单**核对"这份副本与源仓库差在哪" —— 方向相反，用途不同。
    """
    if not target.is_dir():
        print(f"副本目录不存在：{target}")
        return 2
    files, _notes = plan()
    # **用副本里的路径比**（`dest_relative`）：`packaging/INSTALL.md` 在包里叫 `INSTALL.md`。
    # 第一版直接拿源仓库路径去比，于是每份副本都被报成"缺 packaging/INSTALL.md + 多出 INSTALL.md" ——
    # 这是**跑一次真副本**才发现的（合成用例当时按源路径造副本，把这个 bug 一起复制了）。
    local = {dest_relative(path): path for path in files}

    different: list[str] = []
    missing: list[str] = []
    for rel, path in sorted(local.items()):
        other = target / rel
        if not other.is_file():
            missing.append(rel)
            continue
        if digest(path) != digest(other):
            different.append(rel)

    # 副本多出来的：本机自有文件不算，其余**列出来但不判失败**
    extra: list[str] = []
    local_only: list[str] = []
    for path in target.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(target).as_posix()
        if rel in local or rel == "MANIFEST.json":
            continue
        (local_only if is_local_only(rel) else extra).append(rel)

    # 副本里留着源仓库**已经删掉**的旧包内文件：靠副本自己的清单认（没有清单就查不了）
    stale: list[str] = []
    manifest = target / "MANIFEST.json"
    if manifest.is_file():
        try:
            declared = set(json.loads(manifest.read_text(encoding="utf-8")).get("files") or {})
        except (OSError, json.JSONDecodeError):
            declared = set()
        stale = sorted(rel for rel in declared - set(local) if (target / rel).is_file())

    print(f"比对 源仓库 ↔ {target}")
    print(f"  源仓库清单 {len(local)} 个文件")
    for label, items in (("内容不同（⚠️ 合并风险）", different), ("副本缺失（⚠️ 合并风险）", missing),
                         ("源仓库已删、副本还留着（⚠️ 合并风险）", stale)):
        if items:
            print(f"  **{label}** {len(items)} 个：")
            for item in items[:12]:
                print(f"    · {item}")
            if len(items) > 12:
                print(f"    · …（其余 {len(items) - 12} 个）")
    if extra:
        print(f"  （副本多出 {len(extra)} 个非包内文件，**不算合并风险**，列出来给你看："
              f"{'、'.join(extra[:5])}{'…' if len(extra) > 5 else ''}）")
    if local_only:
        print(f"  （另有 {len(local_only)} 个本机自有文件，按设计不算差异）")
    if different or missing or stale:
        print("结论：**这份副本与源仓库不一致，存在合并风险** —— 先问清这些差异是谁改的：")
        print("      （2026-09-17 的教训：报告里说'源仓库改了'，实际只改在副本里）")
        return 1
    print(f"结论：{len(local)} 个包内文件内容哈希逐一相同，没有合并风险")
    return 0


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

    parser = argparse.ArgumentParser(description="打包一份可安装的副本")
    parser.add_argument("action", choices=["list", "build", "verify", "compare"])
    parser.add_argument("target", nargs="?", default="", help="verify/compare 用：包目录或副本目录")
    parser.add_argument(
        "--out",
        default="",
        help="输出根目录，默认仓库上一级的 pkg/（包不该生在要打包的那棵树里）",
    )
    args = parser.parse_args()

    if args.action == "list":
        return list_plan()
    if args.action == "build":
        return build(Path(args.out) if args.out else ROOT.parent / "pkg")
    if not args.target:
        print(f"{args.action} 需要一个目录")
        return 2
    if args.action == "compare":
        return compare(Path(args.target))
    return verify(Path(args.target))


if __name__ == "__main__":
    raise SystemExit(main())
