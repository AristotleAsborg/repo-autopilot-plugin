"""包入口（lib/index.js）的**真机加载**用例。

## 为什么有这个文件

2026-09-18 DSH 直接起不来：

    Error: dsh: plugin tree failed to load: failed to apply loader entry
    repo-autopilot-plugin (repo-autopilot-plugin): harness is not defined
        at Object.apply [as callback] (.../repo-autopilot-plugin/lib/index.js:242:18)

`harness` 是 dynamic Package 沙箱的**内置对象**（`HOST_BUILTIN_INSPECTION` 里的
`harness.handle/defineTool/registerTool`），而 profile 层的 ESM 入口没有沙箱。

原来的用例**一条都没碰过 `lib/index.js`**：要么扫 `host.js` 的源码文本，
要么核对 sandbox 契约。于是"包入口到底能不能被 import、`apply()` 会不会抛"
这件事，直到真机把 DSH 打死才被发现。

这个文件的判据只有一条：**把包入口真的 import 进来，真的 `apply()` 一次。**

## 为什么要搬进 profile 再跑

`lib/index.js` 会 `import '@deepseek-ai/dsh-tools'`，这个裸名字只在 dsh 的
node_modules 树里能解析到。用例把生成物搬到 `<DSH_HOME>/profiles/web/.load-smoke/`，
那个位置沿目录向上就是 profile 的 `node_modules` —— 与真机装载的解析路径一致。
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
PLUGIN = HERE.parent
STAGE_NAME = ".load-smoke"


def _node() -> str | None:
    """跑用例的 node：优先环境变量，其次 PATH，最后本机 dsh 自带的那个。"""
    for candidate in (
        os.environ.get("DSH_NODE"),
        shutil.which("node"),
        r"D:\dsh\runtime\node\node.exe",
    ):
        if candidate and Path(candidate).is_file():
            return candidate
    return None


def _profile() -> Path:
    """找一个真的 dsh profile：环境变量优先，其次本机那两个已知的 home。

    候选顺序是有意的：`DSH_HOME` 是唯一的权威来源，`D:\\dsh\\home` 是本机的
    加固安装，`~/.dsh` 是 dsh 的出厂默认（本机那份是历史遗留，排在最后）。
    """
    homes: list[Path] = []
    if os.environ.get("DSH_HOME"):
        homes.append(Path(os.environ["DSH_HOME"]))
    homes += [Path(r"D:\dsh\home"), Path.home() / ".dsh"]
    for home in homes:
        profile = home / "profiles" / "web"
        if profile.is_dir():
            return profile
    raise AssertionError(f"找不到任何 profile（试过 {', '.join(str(h) for h in homes)}）")


def test_package_entry_loads_and_registers_the_tool() -> None:
    """
    判据只有一条：**把包入口真的 import 进来、真的 apply() 一次**。

    跑不了的时候**跳过并说明原因**，而不是判红 —— 这条用例需要一个**真的 dsh 安装**
    （裸 `import '@deepseek-ai/dsh-tools'` 只有在那棵树里才解析得到）以及**能写 DSH_HOME**
    的权限。CI 容器与默认沙箱都不满足，若判红就会长期挂着一条没人能修的红灯，
    久而久之大家就学会忽略它了 —— 那比没有这条用例更糟。

    反过来，只要环境满足，它就必须真跑、并且在包入口抛异常时**响亮判红**。
    """
    node = _node()
    if node is None:
        pytest.skip("找不到 node（设 DSH_NODE，或把它放上 PATH）—— 这条用例在这台机器上跑不了")

    try:
        profile = _profile()
    except AssertionError as error:
        pytest.skip(f"{error} —— 这条用例需要一个真的 dsh 安装")

    tools = profile.parent / "node_modules" / "@deepseek-ai" / "dsh-tools"
    if not tools.is_dir():
        pytest.skip(f"{profile} 的上一级没有 node_modules/@deepseek-ai/dsh-tools，裸 import 解析不到")

    stage = profile / STAGE_NAME
    try:
        if stage.exists():
            shutil.rmtree(stage, ignore_errors=True)
        (stage / "lib").mkdir(parents=True)
    except OSError as error:
        pytest.skip(
            f"暂存目录写不进去（{error}）—— profile 在会话工作区之外，默认沙箱会拦；"
            "请在能写 DSH_HOME 的地方跑这条用例"
        )

    try:
        # 生成物 + 包清单 + 冒烟脚本，三样都进暂存目录。
        shutil.copy2(PLUGIN / "lib" / "index.js", stage / "lib" / "index.js")
        shutil.copy2(PLUGIN / "package.json", stage / "package.json")
        shutil.copy2(HERE / "package_load_smoke.mjs", stage / "package_load_smoke.mjs")

        done = subprocess.run(
            [node, "package_load_smoke.mjs", "./lib/index.js"],
            cwd=stage,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
        )
    finally:
        if stage.exists():
            shutil.rmtree(stage, ignore_errors=True)

    report = (done.stdout or "") + (done.stderr or "")
    assert done.returncode == 0, "包入口加载失败（这就是那次 DSH 起不来的形状）：\n" + report
    assert "PACKAGE LOAD: PASS" in report, report
