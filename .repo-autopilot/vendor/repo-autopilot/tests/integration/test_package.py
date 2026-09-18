"""打包工具的确定性测试：**包该带什么、不该带什么**，以及校验真的会亮红灯。

打包规则最容易悄悄漂移（多加一个目录、少带一份金样本），而漂移的后果要到"装到别的机器上
才发现验收跑不动"。所以这里把三条规则钉死：

1. **必带**：代码、文档、状态骨架、**金样本**（3.1/3.2/5.1 的验收输入）、对抗样本库；
2. **必不带**：证据（`state/reports/**`、`state/findings/**`）、运行期产物、`.git`、缓存；
3. **校验必须会红**：改一个字节、少一个文件、多一个文件都要报出来 ——
   否则 `verify` 只是个安慰剂。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def load_module(name: str, relative: str):
    """从路径导入脚本（`tools/` 不是包）。必须登记进 `sys.modules`：dataclass 解析字段时要回查。"""
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def planned() -> set[str]:
    module = load_module("package_plan", "tools/package.py")
    files, _notes = module.plan()
    return {path.relative_to(ROOT).as_posix() for path in files}


def test_package_carries_code_docs_and_state_skeleton() -> None:
    paths = planned()
    for required in ("README.md", "AGENTS.md", "ROADMAP.md", "pytest.ini", "src/gateway/gateway.py",
                     "tools/acceptance.py", "skills/fix.md", "config/models.yaml",
                     "state/capabilities.yaml", "state/mode.json", "state/progress.md"):
        assert required in paths, f"{required} 必须进包"


def test_package_only_carries_git_tracked_content() -> None:
    """**核心规则**：包的内容 = 某个 commit 的树的子集。

    第一版用"遍历目录 + 排除清单"，把**本地生成的**陪练仓库/对抗样本（10MB）当成源码装了进去 ——
    排除清单一定会漏。改成"只打 git 跟踪的文件"之后，垃圾根本无从进入。

    注意：这条规则**只在 git 工作树里成立**。安装到别的机器的副本没有 `.git`
    （INSTALL.md 明说"不需要 git 历史"），那里 `plan()` 走的是**降级**的遍历法，
    所以这里没有 git 就跳过 —— 否则"装好的副本跑自己的测试"会看起来像装坏了（实测踩过）。
    """
    import subprocess

    module = load_module("package_plan_git", "tools/package.py")
    if module.git_tracked() is None:
        pytest.skip("不在 git 工作树里（部署副本）：'只打跟踪文件'这条规则不适用，走的是降级遍历法")
    paths = planned()
    tracked = set(
        subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=False)
        .stdout.split()
    )
    assert paths <= tracked, f"包里有没被 git 跟踪的文件：{sorted(paths - tracked)[:5]}"


def test_package_carries_the_frozen_golden_samples() -> None:
    """金样本是**验收输入**：不带它们，装到新机器上 3.1/3.2/5.1 直接跑不动。"""
    paths = planned()
    assert any(path.startswith("state/corpus/") for path in paths)
    assert any(path.startswith("state/scout-corpus/") for path in paths)


def test_package_carries_tracked_fixtures_but_not_generated_ones() -> None:
    """
    边界要划清楚（2026-09-12 修正过一次）：

    - **生成物**（`tests/fixtures/sandbox-repos/`，含 10MB 对抗样本）不进包 —— 它是产物，
      现场跑 `tools/sandbox_repos.py build` 重建；
    - **入库的样本**（`tests/fixtures/redteam/`）**必须进包** —— 它是演练的输入，
      不带上的话红队演练在目标机器上直接缺件。

    第一版测试把整个 `tests/fixtures/` 一刀切成"不进包"，入库样本一加进来就红 ——
    教训：规则要按"是不是生成物"划，不是按目录划。
    """
    paths = planned()
    assert not any(path.startswith("tests/fixtures/sandbox-repos/") for path in paths)
    assert any(path.startswith("tests/fixtures/redteam/") for path in paths), "入库的对抗样本必须进包"
    # 安装说明在两种布局里位置不同：仓库里是 `packaging/INSTALL.md`，
    # 打包时被提到包根当 `INSTALL.md`（`dest_relative` 干的）。两种都要认 ——
    # 只认一种的话，"在安装副本里跑它自己的测试"会 FileNotFoundError（实测踩过）。
    install = ROOT / "packaging" / "INSTALL.md"
    if not install.is_file():
        install = ROOT / "INSTALL.md"
    assert install.is_file(), "找不到安装说明"
    assert "sandbox_repos.py build" in install.read_text(encoding="utf-8"), (
        "INSTALL.md 必须写明这个前提，否则演练第一天就缺件"
    )


def test_package_leaves_evidence_and_runtime_behind() -> None:
    paths = planned()
    forbidden_prefixes = (
        "state/reports/", "state/findings/", "state/drill/", "state/runtime/",
        "state/sandbox/", "state/e2e/", "state/vendor/", "state/gate/", "state/scaffold/",
        ".git/", "tests/fixtures/sandbox-repos/",
    )
    for path in paths:
        if path.endswith(".gitkeep"):
            continue        # 骨架占位符：空文件、不含证据，见下面那条例外
        assert not path.startswith(forbidden_prefixes), f"{path} 不该进包"
    assert not any("__pycache__" in path for path in paths), "缓存不该进包"
    # **例外**：骨架目录里的 `.gitkeep` 是占位文件（空文件、不含任何证据），
    # 它正是"干净的 checkout 也能跑"的原因，必须带上；其余 state/reports/** 一律不带。
    skeletons = [path for path in paths if path.startswith("state/reports/")]
    assert skeletons == ["state/reports/.gitkeep"], skeletons


def test_package_verify_catches_tampering(scratch: Path) -> None:
    module = load_module("package_verify", "tools/package.py")
    target = scratch / "pkg"
    target.mkdir()
    (target / "keep.txt").write_text("hello\n", encoding="utf-8")
    manifest = {
        "files": {"keep.txt": {"sha256": module.digest(target / "keep.txt"), "bytes": 6}},
    }
    (target / "MANIFEST.json").write_text(json.dumps(manifest), encoding="utf-8")
    assert module.verify(target) == 0, "没被动过的包必须过"

    # **装完之后本来就该有的东西**：凭证与运行期产物。它们不是"包被改过"的证据 ——
    # INSTALL.md 让人类装完跑 `verify .`，而刚放好 token 的目录以前会报"校验不通过"。
    (target / "state").mkdir()
    (target / "state" / "GH_READ_TOKEN.txt").write_text("github_pat_x\n", encoding="utf-8")
    (target / "state" / ".write_token").write_text("t\n", encoding="utf-8")
    (target / "state" / "reports").mkdir()
    (target / "state" / "reports" / "run.json").write_text("{}\n", encoding="utf-8")
    assert module.verify(target) == 0, "本机自有文件不该让校验失败"

    # **跑过测试之后**必然出现的编译/缓存产物同理（实测：安装副本里先跑全量测试，
    # 再 `verify .` 就报 `src/dedupe/__pycache__/__init__.cpython-312.pyc` 是"多出来的文件"）。
    (target / "src").mkdir()
    (target / "src" / "__pycache__").mkdir()
    (target / "src" / "__pycache__" / "m.cpython-312.pyc").write_bytes(b"\x00\x01")
    (target / ".ruff_cache").mkdir()
    (target / ".ruff_cache" / "CACHEDIR.TAG").write_text("x\n", encoding="utf-8")
    assert module.verify(target) == 0, "缓存产物不该让校验失败"
    # **装完之后跑出来的产物**：`/细化idea` 的 spec 卡、M2 的向量库。
    # 实测（2026-09-15）：给工作区外那份安装副本做升级时，它 `state/specs/` 下有三张卡，
    # `verify .` 当场报"多出来的文件 = 包被改过"——和凭证、缓存是同一类假警报。
    (target / "state" / "specs").mkdir()
    (target / "state" / "specs" / "429df9784dbe.md").write_text("卡\n", encoding="utf-8")
    (target / "state" / "vectors").mkdir()
    (target / "state" / "vectors" / "issues.npy").write_bytes(b"\x00")
    assert module.verify(target) == 0, "跑出来的 spec 卡与向量库不该让校验失败"
    for relative in (
        "src/__pycache__/m.cpython-312.pyc",
        ".ruff_cache/CACHEDIR.TAG",
        "a/b.pyc",
        "state/specs/429df9784dbe.md",
        "state/vectors/issues.npy",
        "state/batch_2026-09-12.json",
    ):
        assert module.is_local_only(relative), relative
    for relative in ("src/spec/refiner.py", "tools/package.py", "state/progress.md", "state/corpus/issues.jsonl"):
        assert not module.is_local_only(relative), relative

    # 但**清单外的源码文件**必须仍然报出来（否则"混进什么东西"就看不出来了）
    (target / "smuggled.py").write_text("x = 1\n", encoding="utf-8")
    assert module.verify(target) == 1
    (target / "smuggled.py").unlink()

    (target / "keep.txt").write_text("hello!\n", encoding="utf-8")     # 改一个字节
    assert module.verify(target) == 1

    (target / "keep.txt").write_text("hello\n", encoding="utf-8")
    (target / "extra.txt").write_text("多出来的\n", encoding="utf-8")
    assert module.verify(target) == 1, "**多一个文件也要报** —— 否则包里混进什么都看不出来"

    (target / "extra.txt").unlink()
    (target / "keep.txt").unlink()
    assert module.verify(target) == 1, "少一个文件必须报"


# ==================================================== compare：源仓库 ↔ 副本（/体检 的常规检查）

class TestCompare:
    """
    **2026-09-17 人类要求写进 `/体检` 常规检查的那条规矩**：
    覆盖/合并任何副本之前，先逐文件哈希比对。

    起因是一次真实翻车：报告 §三 写着"源仓库改动：`src/scaffold/core.py`"，
    实际那两个文件**只改在工作区外那份副本里** —— 源仓库/存档包/GitHub 三处都没有。
    照叙述往下走，那次修复会随下一次覆盖消失。
    """

    def _fake_tree(self, module, scratch: Path) -> Path:
        """造一份"与源仓库一致"的副本：只放 3 个包内文件，其余用 plan 的清单对齐不现实。"""
        target = scratch / "copy"
        target.mkdir()
        return target

    def _mirror(self, package, target: Path) -> None:
        """按**副本里的路径**摆一份与源仓库一致的树（`packaging/INSTALL.md` → `INSTALL.md`）。"""
        files, _notes = package.plan()
        for path in files:
            rel = package.dest_relative(path)
            destination = target / rel
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(path.read_bytes())

    def test_identical_copy_reports_consistent(self, scratch: Path) -> None:
        package = load_module("package_compare_ok", "tools/package.py")
        target = scratch / "copy"
        self._mirror(package, target)
        # 本机自有文件不算差异
        (target / "state" / ".write_token").write_text("t\n", encoding="utf-8")
        (target / "src" / "__pycache__").mkdir(parents=True, exist_ok=True)
        (target / "src" / "__pycache__" / "x.cpython-312.pyc").write_bytes(b"\x00")
        # **副本自己放的探针脚本也不算差异**（本机那份有 17 个）：
        # 合并时它们不会被覆盖、也不污染产品；把它们当"不一致"只会让检查永远红着。
        (target / "tools" / "m1_drive.py").write_text("x = 1\n", encoding="utf-8")
        assert package.compare(target) == 0

    def test_a_single_changed_byte_is_reported(self, scratch: Path) -> None:
        package = load_module("package_compare_diff", "tools/package.py")
        target = scratch / "copy"
        self._mirror(package, target)
        victim = target / "src" / "scaffold" / "core.py"
        victim.write_text(victim.read_text(encoding="utf-8") + "\n# 副本里偷偷改的一行\n", encoding="utf-8")
        assert package.compare(target) == 1, "内容不同必须报出来（这就是那次翻车的形态）"

    def test_missing_packaged_file_is_a_merge_risk(self, scratch: Path) -> None:
        package = load_module("package_compare_more", "tools/package.py")
        target = scratch / "copy"
        self._mirror(package, target)
        (target / "src" / "scaffold" / "core.py").unlink()          # 副本缺失 → 合并会"少东西"
        assert package.compare(target) == 1

    def test_stale_file_the_source_already_removed_is_a_merge_risk(self, scratch: Path) -> None:
        """副本自己的清单声明过、而源仓库已经删掉的文件：留着就是旧代码（靠副本 MANIFEST 认出来）。"""
        package = load_module("package_compare_stale", "tools/package.py")
        target = scratch / "copy"
        self._mirror(package, target)
        (target / "tools" / "legacy_gone.py").write_text("old = 1\n", encoding="utf-8")
        manifest = json.loads((target / "MANIFEST.json").read_text(encoding="utf-8")) if (target / "MANIFEST.json").is_file() else {"files": {}}
        manifest.setdefault("files", {})["tools/legacy_gone.py"] = {"sha256": "x", "bytes": 8}
        (target / "MANIFEST.json").write_text(json.dumps(manifest), encoding="utf-8")
        assert package.compare(target) == 1

    def test_compare_uses_the_copy_layout_for_packaging_install_md(self, scratch: Path) -> None:
        """
        **这条是"真跑一次"逼出来的**：`packaging/INSTALL.md` 在包里叫 `INSTALL.md`，
        第一版 `compare` 拿源仓库路径去比，于是**每一份副本**都被误报成
        "缺 `packaging/INSTALL.md` + 多出 `INSTALL.md`"。合成用例当时按源路径造副本，
        把这个 bug 一起复制了 —— 所以这条用例专门钉住"副本里的路径"。
        """
        package = load_module("package_compare_layout", "tools/package.py")
        target = scratch / "copy"
        self._mirror(package, target)
        assert (target / "INSTALL.md").is_file(), "包里的落点是 INSTALL.md"
        assert not (target / "packaging" / "INSTALL.md").exists(), "包里没有 packaging/ 那一层"
        assert package.compare(target) == 0

    def test_missing_directory_is_exit_2(self, scratch: Path) -> None:
        package = load_module("package_compare_missing", "tools/package.py")
        assert package.compare(scratch / "not-here") == 2


def test_checkup_discovers_copies_without_hardcoding_machine_paths(scratch: Path, monkeypatch) -> None:
    """
    `/体检` 的副本检查要能发现副本，但**不许把机器路径写死在仓库里**（换机器就跑不了）。
    两个来源：工作区内的标准安装位置（自动），以及 `state/copies.json` 里登记的（机器特有）。
    """
    checkup = load_module("checkup_copies", "tools/checkup.py")
    fake_root = scratch / "repo"
    (fake_root / "state").mkdir(parents=True)
    installed = scratch / "installed" / "repo-autopilot"
    installed.mkdir(parents=True)
    registered = scratch / "elsewhere" / "copy"
    registered.mkdir(parents=True)
    (fake_root / "state" / "copies.json").write_text(
        json.dumps({"copies": [str(registered), "  ", 123]}), encoding="utf-8"
    )
    monkeypatch.setattr(checkup, "ROOT", fake_root)

    found = checkup.known_copies()
    assert installed in found, found
    assert registered in found, found
    assert len(found) == 2, f"空串与非字符串条目要被忽略：{found}"


def test_checkup_copy_registry_is_read_from_state_and_survives_garbage(scratch: Path, monkeypatch) -> None:
    """`state/copies.json` 坏掉时**不能把体检整个搞崩** —— 只是发现不了登记过的副本。"""
    checkup = load_module("checkup_copies_bad", "tools/checkup.py")
    fake_root = scratch / "repo"
    (fake_root / "state").mkdir(parents=True)
    (fake_root / "state" / "copies.json").write_text("{ 这不是 JSON", encoding="utf-8")
    monkeypatch.setattr(checkup, "ROOT", fake_root)
    assert checkup.known_copies() == []


def test_copy_consistency_metric_is_named_ok_so_it_counts(scratch: Path, monkeypatch) -> None:
    """
    **指标名必须以 `_ok` 结尾**，否则它不进 `main()` 的判定 ——
    第一版叫 `copies_consistent`，于是它 **False 的时候体检照样报"通过"**：
    一个能和自己打架的指标比没有这个指标更糟（跑完一次体检才看出来）。
    """
    checkup = load_module("checkup_copy_metric", "tools/checkup.py")
    other = scratch / "copy"
    other.mkdir()
    monkeypatch.setattr(checkup, "known_copies", lambda: [other])
    monkeypatch.setattr("tools.package.compare", lambda target: 1)

    ok, details = checkup.copy_consistency()
    assert ok is False
    assert any("有差异" in line for line in details), details

    monkeypatch.setattr("tools.package.compare", lambda target: 0)
    ok, _details = checkup.copy_consistency()
    assert ok is True


def test_copy_consistency_without_copies_is_not_a_failure(scratch: Path, monkeypatch) -> None:
    """没有可比的副本不是缺陷（干净 checkout / 别人的机器上都会是这样）。"""
    checkup = load_module("checkup_copy_metric_none", "tools/checkup.py")
    monkeypatch.setattr(checkup, "known_copies", list)
    ok, details = checkup.copy_consistency()
    assert ok is True and any("没发现可比的副本" in line for line in details)


# ------------------------------------------------ dirty 标记：只看会进包的路径
#
# 2026-09-18 修：`git_dirty()` 原来是"整棵树有任何改动就算脏"。
# 而 `tests/integration/test_spec_refiner.py` **故意**把人工检查材料写到 `state/reports/`
# （那是要留给人看的，不能跑完就被冲掉），`state/reports/` 又根本不进包 ——
# 于是每跑一次全量测试，包就被记成 dirty，哪怕包内一个字节都没动。
# dirty 一旦总是真就等于没有；而"这是不是一个干净 commit 的产物"正是它唯一要说清楚的事。


def _fake_status(monkeypatch, module, porcelain: str) -> None:
    class FakeCompleted:
        stdout = porcelain
        returncode = 0

    monkeypatch.setattr(module.subprocess, "run", lambda *a, **k: FakeCompleted())


def test_git_dirty_ignores_paths_that_cannot_enter_the_package(monkeypatch) -> None:
    module = load_module("package_dirty_local", "tools/package.py")

    _fake_status(monkeypatch, module, " M state/reports/spec-questions-for-human-review.json\n")
    assert module.git_dirty() is False, "不进包的路径变了，包不算脏"

    _fake_status(monkeypatch, module, "?? state/reports/weekly/dirty-2026-09-18.md\n")
    assert module.git_dirty() is False


def test_git_dirty_still_catches_real_packaged_changes(monkeypatch) -> None:
    module = load_module("package_dirty_real", "tools/package.py")

    _fake_status(monkeypatch, module, " M src/gateway/gateway.py\n")
    assert module.git_dirty() is True, "包内文件变了必须报脏"

    _fake_status(monkeypatch, module, " M config/models.yaml\n M state/reports/x.json\n")
    assert module.git_dirty() is True, "包内 + 不进包的混合，仍要报脏"

    _fake_status(monkeypatch, module, "")
    assert module.git_dirty() is False


def test_git_dirty_checks_both_sides_of_a_rename(monkeypatch) -> None:
    """重命名是 `XY old -> new`：两边都要看，否则'把包内文件挪进 state/'会被漏掉。"""
    module = load_module("package_dirty_rename", "tools/package.py")

    _fake_status(monkeypatch, module, "R  src/old.py -> state/reports/old.py\n")
    assert module.git_dirty() is True, "从包内移出到不进包的目录，必须报脏"

    _fake_status(monkeypatch, module, "R  state/reports/a.json -> state/reports/b.json\n")
    assert module.git_dirty() is False, "两边都不进包，不算脏"
