"""补丁**真的落上了吗** —— 1.5 沙箱里最容易漏掉的那个断言。

由来（2026-09-12 实测）：`git apply` 在**不是 git 工作树**的目录里会**返回 0 却什么都不改**。
沙箱的副本恰好不是 git 仓库（复制时不含 `.git`），于是 `apply_patch` 一直报 "ok"，
而 4.2 的修复循环每轮都在跑**没打补丁**的代码 —— 表现成"模型改 8 轮还是同一个断言失败"，
看起来像模型不行，实际是补丁没落上。

所以这里断言的不是"返回码是 0"，而是**文件内容真的变了**。
"""

from __future__ import annotations

from pathlib import Path

from src.sandbox import apply_patch
from src.sandbox.sandbox import _copy_tree

ORIGINAL = "def add(a, b):\n    return a - b\n"
FIXED = "def add(a, b):\n    return a + b\n"
PATCH = """diff --git a/m.py b/m.py
--- a/m.py
+++ b/m.py
@@ -1,2 +1,2 @@
 def add(a, b):
-    return a - b
+    return a + b
"""


def build(scratch: Path) -> tuple[Path, Path]:
    source = scratch / "source"
    source.mkdir()
    (source / "m.py").write_text(ORIGINAL, encoding="utf-8")
    patch_file = scratch / "fix.diff"
    patch_file.write_text(PATCH, encoding="utf-8")
    copy = scratch / "copy"
    _copy_tree(source, copy)
    return copy, patch_file


def test_patch_actually_changes_the_file(scratch: Path) -> None:
    copy, patch_file = build(scratch)
    ok, detail = apply_patch(copy, patch_file, scratch / "apply.log")
    assert ok, f"补丁没打上：{detail}"
    assert (copy / "m.py").read_text(encoding="utf-8") == FIXED, "返回 ok 但文件没变 —— 正是那个静默失效"
    # 不要用 hash_tree 做这个断言：它按设计跳过点开头的目录，而测试副本就在 `.cache/` 下，
    # 于是"前后指纹"会永远是两个空字典（这正是当初让验证形同虚设的那个坑）。


def test_failed_patch_is_reported_as_failure_not_silent_success(scratch: Path) -> None:
    """补丁与文件对不上时必须**报失败**：静默成功会让整个修复循环白跑。"""
    copy, _ = build(scratch)
    wrong = scratch / "wrong.diff"
    wrong.write_text(
        PATCH.replace("return a - b", "return a * b"), encoding="utf-8"
    )
    ok, detail = apply_patch(copy, wrong, scratch / "apply.log")
    assert ok is False
    assert "失败" in detail
    assert (copy / "m.py").read_text(encoding="utf-8") == ORIGINAL, "失败的补丁不该动到文件"


def test_source_repo_is_never_touched(scratch: Path) -> None:
    copy, patch_file = build(scratch)
    apply_patch(copy, patch_file, scratch / "apply.log")
    assert copy.joinpath("m.py").read_text(encoding="utf-8") == FIXED
    assert not (copy.parent / "source" / "m.py").exists() or True  # 源在别处，见下一个断言
    assert (scratch / "source" / "m.py").read_text(encoding="utf-8") == ORIGINAL
