"""8.4 门禁：测试完整性检查（防"删测试/加 skip 过测试"）。

    python scripts/check_test_integrity.py                 # CI 里：与 base 比较
    python scripts/check_test_integrity.py --base origin/main
    python scripts/check_test_integrity.py --self-test     # 不比较，只报当前统计

路线 0.2 第 3 条点名禁止的作弊方式：修改/删除/注释测试用例、提高 skip 或 xfail。
这个脚本在 PR 门禁里把它们挡住：

- **用例数减少** → 失败；
- **skip / xfail 数上升** → 失败。

实现刻意用 git 读 base 版本的文本（`git show <base>:<path>`），而不是去 checkout 一份 ——
CI 里 checkout 两次既慢又容易出错，而我们只需要数一数。

只用标准库：它要被复制到被管仓库里跑。
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

TEST_DEF_RE = re.compile(r"^\s*def (test_[A-Za-z0-9_]+)", re.MULTILINE)
SKIP_RE = re.compile(r"pytest\.mark\.(skip|xfail)|@unittest\.skip|pytest\.skip\(", re.MULTILINE)


#: 不该被统计的目录：`state/` 与 `.cache/` 里放着**仓库副本**（沙箱、门禁、演练的副本），
#: `fixtures/` 里是被管项目的测试。把它们算进来会得到荒谬的数字
#: （实测：666 个测试文件 / 5319 个用例 —— 全是副本里的）。
EXCLUDED_PARTS = frozenset({"state", ".cache", "fixtures", "__pycache__", ".git", "sandbox-repos"})


def head_files(root: Path) -> list[str]:
    found: list[str] = []
    for path in root.rglob("test_*.py"):
        parts = set(path.relative_to(root).parts[:-1])
        if parts & EXCLUDED_PARTS:
            continue
        found.append(str(path.relative_to(root)).replace("\\", "/"))
    return sorted(found)


def count_source(text: str) -> tuple[int, int]:
    return len(TEST_DEF_RE.findall(text)), len(SKIP_RE.findall(text))


def base_text(base: str, relative: str) -> str:
    outcome = subprocess.run(
        ["git", "show", f"{base}:{relative}"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    return outcome.stdout if outcome.returncode == 0 else ""


def stats(base: str | None, root: Path) -> dict[str, object]:
    files = head_files(root)
    total_tests = total_skips = 0
    base_tests = base_skips = 0
    removed: list[str] = []
    for relative in files:
        text = (root / relative).read_text(encoding="utf-8", errors="replace")
        tests, skips = count_source(text)
        total_tests += tests
        total_skips += skips
        if base:
            before = base_text(base, relative)
            if before:
                base_t, base_s = count_source(before)
                base_tests += base_t
                base_skips += base_s
            else:
                removed.append(relative)      # base 里没有 = 新增文件，不算删测试
    return {
        "files": files,
        "tests": total_tests,
        "skips": total_skips,
        "base_tests": base_tests,
        "base_skips": base_skips,
        "added_files": removed,
    }


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

    parser = argparse.ArgumentParser(description="测试完整性检查（8.4）")
    parser.add_argument("--base", default="origin/main")
    parser.add_argument("--root", default=".")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()

    root = Path(args.root).resolve()
    import os

    base = args.base
    if os.environ.get("GITHUB_BASE_REF"):
        base = f"origin/{os.environ['GITHUB_BASE_REF']}"
    if args.self_test:
        base = None

    import subprocess as _sub

    probe = _sub.run(
        ["git", "rev-parse", "--verify", base], capture_output=True, text=True, check=False
    ) if base else None
    if base and probe is not None and probe.returncode != 0:
        print(f"取不到基准 {base} —— 当作自检（只报当前统计，不做比较）")
        base = None

    result = stats(base, root)
    print(f"测试文件 {len(result['files'])} 个；用例 {result['tests']} 个；skip/xfail {result['skips']} 个")
    if base:
        print(f"基准 {base}：用例 {result['base_tests']} 个；skip/xfail {result['base_skips']} 个")
        problems: list[str] = []
        if result["tests"] < result["base_tests"]:
            problems.append(f"用例数减少：{result['base_tests']} → {result['tests']}")
        if result["skips"] > result["base_skips"]:
            problems.append(f"skip/xfail 增加：{result['base_skips']} → {result['skips']}")
        if problems:
            print("测试完整性：**不通过**")
            for item in problems:
                print(f"  - {item}")
            print("路线 0.2 第 3 条：删测试/加 skip 过测试属严重违规。")
            return 1
        print("测试完整性：通过（用例没少、skip 没多）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
