"""1.6 骨架用的**桩**，不是 4.2 的实现 —— 真实现在 `src/repair/core.py`。

它为什么还在：路线 1.6 的 e2e（`tests/e2e/e2e_full_run.py`）是**离线跑**的，
它要验的是"链路通不通、门禁灵不灵"，不是"模型修得好不好"。所以那一步的修复环节
用一个确定性的桩：只处理"README 缺少安装说明"这一类 issue，产出一个**真实可用**的 diff。

之所以要真的产出 diff 而不是假装产出，是因为 e2e 里 `src.sandbox.run()` 会
**真的把补丁打进去再跑测试** —— 假补丁会让那条链路白验。

> ⚠️ 踩过的坑（2026-09-12 整合轮）：4.2 落地时我把 `src/repair/__init__.py` 整个重写成了
> 新引擎的导出，把这个桩一起删了，1.6 的 e2e 当场 `ImportError`。
> 教训：**覆盖一个模块前，先看谁在 import 它**（`git grep "from src.<mod>"`）。
> 现在桩单独一个文件，和真引擎同时存在，名字上也不会混淆。
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass
from pathlib import Path

from .core import RepairError

INSTALL_SECTION = """## Install

```console
python -m pip install -e .
```

Requires Python 3.10 or newer. The runtime has no third-party dependencies;
`pytest` is needed only to run the test suite.
"""


@dataclass(frozen=True)
class PatchProposal:
    kind: str            # "patch" | "none"
    description: str
    patch_text: str = ""
    target: str = ""

    @property
    def produced_patch(self) -> bool:
        return self.kind == "patch" and bool(self.patch_text)


def _unified_diff(relative: str, before: str, after: str) -> str:
    diff = difflib.unified_diff(
        before.splitlines(keepends=True),
        after.splitlines(keepends=True),
        fromfile=f"a/{relative}",
        tofile=f"b/{relative}",
    )
    return "".join(diff)


def propose_patch(repo_dir: str | Path, title: str, body: str = "", label: str = "") -> PatchProposal:
    """
    返回一个补丁提议。**不写盘、不执行** —— 落盘与执行由调用方（e2e / 修复循环）负责。

    桩不做的事（真实现补上）：不定位文件（这里直接写死 README.md）、不读代码上下文、不重试。
    """
    repo = Path(repo_dir)
    text = f"{title}\n{body}".lower()

    wants_install_docs = ("readme" in text or "文档" in text or "安装" in text) and (
        "缺少" in text or "没有" in text or "install" in text
    )
    if not wants_install_docs:
        return PatchProposal("none", "桩只会处理「README 缺少安装说明」这一类 issue")

    readme = repo / "README.md"
    if not readme.is_file():
        raise RepairError(f"仓库里没有 README.md：{readme}")

    before = readme.read_text(encoding="utf-8")
    if "## Install" in before:
        return PatchProposal("none", "README 里已经有 Install 段落，无需改动")

    # 插在 Usage 之前。找不到定位点就整体追加，绝不"猜一个位置"然后写坏文件。
    marker = "\n## Usage"
    if marker in before:
        after = before.replace(marker, "\n" + INSTALL_SECTION + marker, 1)
    else:
        after = before.rstrip("\n") + "\n\n" + INSTALL_SECTION

    return PatchProposal(
        kind="patch",
        description=f"在 README.md 里补上安装说明（标签 {label or '未分类'}）",
        patch_text=_unified_diff("README.md", before, after),
        target="README.md",
    )


__all__ = ["INSTALL_SECTION", "PatchProposal", "propose_patch"]
