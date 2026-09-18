"""8.4 门禁：路径黑名单检查（装到本系统与每个被管仓库）。

    python scripts/check_blacklist.py                 # CI 里：与 base 分支比较
    python scripts/check_blacklist.py --files a.py b.py
    python scripts/check_blacklist.py --base origin/main

为什么单独一个脚本：路线 8.4 把它放在 PR 门禁的第一条。**补丁碰了测试/CI/审批单/LICENSE，
后面所有检查的结果都不可信** —— 测试可能是他改的、CI 可能是他放宽的。

这个脚本只用标准库：它要被**复制到被管仓库**里跑，不能依赖本系统的任何模块。
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys

#: (正则, 为什么不能碰)。与本系统 `src/gatekeep` 的名单保持一致。
BLACKLIST: tuple[tuple[str, str], ...] = (
    (r"^state/approvals/", "审批单是闸门的证据，改它等于篡改批准记录"),
    (r"^state/progress\.md$", "状态表只能由验收脚本写"),
    (r"^\.github/", "CI 配置：改了它就能让门禁自己失效"),
    (r"(^|/)LICENSE(\.|$)", "许可证"),
    (r"(^|/)tests?/", "测试代码本身（防改测试过测试）"),
    (r"(^|/)test_[^/]*\.py$", "测试文件"),
    (r"_test\.py$", "测试文件"),
    (r"(^|/)conftest\.py$", "测试配置"),
    (r"^scripts/check_(blacklist|test_integrity)\.py$", "门禁脚本自身"),
)


def changed_files(base: str) -> list[str]:
    """与 base 比较得到的改动文件（三点比较，CI 里用 GITHUB_BASE_REF）。"""
    for args in (["git", "diff", "--name-only", f"{base}...HEAD"], ["git", "diff", "--name-only", base, "HEAD"]):
        outcome = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False)
        if outcome.returncode == 0:
            return [line.strip() for line in outcome.stdout.splitlines() if line.strip()]
    return []


def hits(paths: list[str]) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    for path in paths:
        normalized = path.replace("\\", "/")
        normalized = normalized.removeprefix("./")
        for pattern, why in BLACKLIST:
            if re.search(pattern, normalized):
                found.append((path, why))
                break
    return found


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

    parser = argparse.ArgumentParser(description="路径黑名单检查（8.4）")
    parser.add_argument("--base", default="origin/main")
    parser.add_argument("--files", nargs="*", default=None)
    args = parser.parse_args()

    if args.files is None:
        base = args.base
        import os

        if os.environ.get("GITHUB_BASE_REF"):
            base = f"origin/{os.environ['GITHUB_BASE_REF']}"
        paths = changed_files(base)
        print(f"基准：{base}；改动文件 {len(paths)} 个")
    else:
        paths = list(args.files)
        print(f"直接给定的文件 {len(paths)} 个")

    found = hits(paths)
    if not found:
        print("路径黑名单：通过")
        return 0
    print(f"路径黑名单：**命中 {len(found)} 条** —— 补丁碰了不该碰的路径")
    for path, why in found:
        print(f"  - {path}（{why}）")
    print("这些路径下任何改动都必须由人来做，不能随补丁进来。")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
