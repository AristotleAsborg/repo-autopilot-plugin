"""从 `host.js` 生成 `lib/index.js`（插件包入口）。

## 为什么要有这个生成器

同一份逻辑有两个装载方式：

1. **dynamic Package**：把 `host.js` 的**全文**当函数体喂给 `cordis_define`；
2. **插件包（profile 层）**：`lib/index.js` 用 ES module 导出 `apply`，由 `dsh plugin add` 装进 profile。

`host.js` 的全文是一个以 `return { apply(ctx) {...} }` 结尾的函数体，所以包一层 IIFE
就能拿到那个插件对象，再把它导出 —— 这是**机械变换**，不是抄一遍。
（ROADMAP 7.3 与 AGENTS.md 都要求两条入口共享同一套实现、不允许逻辑分叉；
`tests/` 里有一条用例钉住"生成物与生成器一致"，手改 `lib/index.js` 会直接红。）

用法：

    python scripts/build_module.py            # 生成
    python scripts/build_module.py --check    # 只检查是否已是最新（CI 用）
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PACKAGE = HERE.parent
HOST = PACKAGE / "host.js"
MODULE = PACKAGE / "lib" / "index.js"

HEADER = """// 本文件由 `scripts/build_module.py` 从 `host.js` **生成** —— 不要手改。
// 手改会在 `python scripts/build_module.py --check`（以及包自检）里直接红。
//
// 为什么是生成而不是另写一份：同一个插件有两种装载方式，
//   * dynamic Package —— 把 host.js 全文当函数体喂给 cordis_define；
//   * 插件包（profile 层）—— 本文件导出 apply，由 `dsh plugin add` 装载。
// 两条入口必须共享同一套实现（ROADMAP 7.3 / AGENTS.md：不允许逻辑分叉）。

const plugin = (() => {
"""

FOOTER = """})()
"""

TAIL = """
export const name = 'repo-autopilot'
export const apply = plugin.apply
"""


def generate() -> str:
    body = HOST.read_text(encoding="utf-8")
    return f"{HEADER}{body}{FOOTER}{TAIL}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="从 host.js 生成 lib/index.js")
    parser.add_argument("--check", action="store_true", help="只检查是否已是最新")
    args = parser.parse_args(argv)

    expected = generate()
    current = MODULE.read_text(encoding="utf-8") if MODULE.is_file() else None

    if args.check:
        if current == expected:
            print(f"[OK] {MODULE.relative_to(PACKAGE).as_posix()} 与 host.js 一致")
            return 0
        print(f"[缺] {MODULE.relative_to(PACKAGE).as_posix()} 不是由当前 host.js 生成的")
        print("     → 跑 python scripts/build_module.py 重新生成")
        return 1

    MODULE.parent.mkdir(parents=True, exist_ok=True)
    MODULE.write_text(expected, encoding="utf-8")
    print(f"[OK] 已生成 {MODULE.relative_to(PACKAGE).as_posix()}（{len(expected.splitlines())} 行）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
