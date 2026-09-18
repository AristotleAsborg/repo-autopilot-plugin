"""插件包的自检（第一轮测试）。

**它测什么**：这个目录**作为一个插件包**是否自洽 —— 不看 DSH 运行时，
所以它能在任何机器上跑（这也是"投稿前要有一个干净机器能跑的验收"的第一步）。

三件事：
1. `plugin.yaml` 的字段齐全、触发器只声明**真能用的**、工具命名空间与 host.js 一致；
2. `host.js` 满足 dynamic Package 的**代码约束**（普通 JS：无 import/require/TS/JSX、
   不用未声明的全局）—— 这条不靠人工看，靠扫描；
3. 清单里承诺的**只读**与**零凭证**能在 host.js 里被核对（模式表 + 不出现 token 相关字样）。
"""

from __future__ import annotations

import importlib.util
import hashlib
import json
import os
import re
import shutil
import sys
from pathlib import Path

import pytest
import yaml

HERE = Path(__file__).resolve().parents[1]
MANIFEST = HERE / "plugin.yaml"
HOST = HERE / "host.js"
SMOKE = HERE / "scripts" / "smoke.py"
INSTALL = HERE / "scripts" / "install.py"
BUNDLED = HERE / "vendor" / "repo-autopilot"


def _load_script(module_name: str, path: Path):
    """按路径加载 `scripts/` 下的脚本（它们不是包，也不该为测试改结构）。

    **必须先登记进 `sys.modules`**：`@dataclass` 在解析字段类型时要顺着
    `cls.__module__` 去 `sys.modules` 里找自己，不登记就 `AttributeError`。
    """
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _load_smoke():
    return _load_script("plugin_smoke", SMOKE)


@pytest.fixture(scope="module")
def manifest() -> dict:
    return yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def manifest_bundle() -> dict:
    """`package.json`（插件包清单）—— 与 `plugin.yaml` 是两回事，别混。"""
    return json.loads((HERE / "package.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def host_source() -> str:
    return HOST.read_text(encoding="utf-8")


# ------------------------------------------------------------------ 清单

def test_manifest_is_a_mapping_with_the_required_fields(manifest: dict) -> None:
    for key in ("name", "version", "summary", "license", "entry", "tools", "triggers", "requires"):
        assert key in manifest, f"清单缺字段：{key}"


def test_name_is_kebab_case_lowercase(manifest: dict) -> None:
    """市场生态的命名惯例是小写 kebab-case（发布工具链在各 host 上都强制它）。"""
    name = manifest["name"]
    assert re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", name), name


def test_version_is_semver(manifest: dict) -> None:
    assert re.fullmatch(r"\d+\.\d+\.\d+", str(manifest["version"])), manifest["version"]


def test_entry_points_exist_on_disk(manifest: dict) -> None:
    for face, filename in (manifest["entry"] or {}).items():
        if filename is None:
            continue                      # client 这一版就是 null（清单里写明了）
        assert (HERE / filename).is_file(), f"{face} 声明的入口不存在：{filename}"


def test_triggers_only_promise_what_works_on_this_machine(manifest: dict) -> None:
    """
    **只声明真能用的触发器**（2026-09-18 的决定）：本机没有公网入口，
    `issues.opened`/`pull_request.opened` 这类事件只能靠轮询 —— 清单里不许承诺做不到的事。
    哪天真的接上了，再把它加进来并把这条用例一起改。
    """
    kinds = {item["kind"] for item in manifest["triggers"]}
    assert kinds == {"manual"}, kinds


def test_deviations_are_recorded_not_hidden(manifest: dict) -> None:
    """与路线 7.3 的偏差必须写明（本项目的老规矩：有偏差要显式记录）。"""
    text = " ".join(manifest.get("deviations") or [])
    assert "events" in text and "cron" in text and "MCP" in text, text


def test_read_only_promise_is_declared(manifest: dict) -> None:
    provides = (manifest["tools"] or {}).get("provides") or []
    assert provides, "清单必须列出提供的工具"
    for item in provides:
        assert item["mode"] == "read-only", item
        assert item["needs_credentials"] is False, item


# ------------------------------------------------------------------ host.js 的代码约束

FORBIDDEN_CALLS = (
    (r"^\s*import\s", "dynamic Package 的代码体不被转译，不许 import"),
    (r"\brequire\s*\(", "没有 CommonJS 运行时，不许 require"),
)
FORBIDDEN_GLOBALS = ("process.", "Buffer", "fetch(", "setTimeout(", "setInterval(", "window.", "document.")


def test_host_source_avoids_forbidden_syntax(host_source: str) -> None:
    """
    判据是**语法**，不是"这几个字母出现过"。

    第一版把 `import ` 当**子串**禁掉，于是插件里那句
    `quote('import ' + PROBE_MODULES)`（那是**要交给解释器的一条命令**）被误报。
    这跟本节另一处"注释被当成代码"是**同一类毛病**：
    **断言的对象要不是同一种东西** —— 禁用语法 ≠ 禁用字符串里的字眼。
    """
    for pattern, why in FORBIDDEN_CALLS:
        assert not re.search(pattern, host_source, re.M), f"host.js 里出现 {pattern}：{why}"
    assert "JSX" not in host_source, "客户端才谈 JSX，这里连 React 都没有"


def test_host_source_uses_no_undeclared_globals(host_source: str) -> None:
    for needle in FORBIDDEN_GLOBALS:
        assert needle not in host_source, f"host.js 里用到未声明的全局：{needle}"


def test_host_source_returns_a_plugin_with_apply(host_source: str) -> None:
    """函数体 = `return { ... }`（**注释不算**：第一版把开头的注释块当成代码，误报了一次）。"""
    code_lines = [
        line for line in host_source.splitlines() if line.strip() and not line.strip().startswith("//")
    ]
    assert code_lines[0].strip() == "return {", f"第一行有效代码应当是 return {{：{code_lines[0]!r}"
    assert "apply(ctx)" in host_source


def test_host_source_owns_its_registration_with_ctx_effect(host_source: str) -> None:
    """注册必须挂在当前 Fiber 上（stop/update 要能自动移除）。"""
    assert "ctx.effect(" in host_source
    assert "harness.registerTool(ctx, tool)" in host_source


def test_host_source_reads_shell_with_an_absence_check(host_source: str) -> None:
    """可选服务要用 ctx.get + undefined 判断；用了 ctx.shell 就必须声明 inject。"""
    uses_optional = "ctx.get('shell')" in host_source
    uses_direct = "ctx.shell" in host_source
    assert uses_optional or uses_direct, "必须从 shell 服务执行命令"
    if uses_direct:
        assert "inject:" in host_source, "直接访问 ctx.shell 就要声明 inject"


# ------------------------------------------------------------------ 清单 ↔ 实现一致

def test_declared_mode_enum_matches_the_implementation(manifest: dict, host_source: str) -> None:
    """清单/描述里承诺的模式集合，必须与 host.js 里的模式表一致。"""
    table = re.search(r"const MODES = \{(.*?)\n    \}", host_source, re.S)
    assert table, "host.js 里找不到 MODES 表"
    implemented = set(re.findall(r"^\s{6}(\w+): \{", table.group(1), re.M))
    assert implemented == {"doctor", "drill", "acceptance", "compare"}, implemented

    enum_line = re.search(r"enum: \[(.*?)\]", host_source, re.S)
    assert enum_line, "参数里要有 mode 的 enum"
    declared = {item.strip().strip("'\"") for item in enum_line.group(1).split(",")}
    assert declared == implemented, (declared, implemented)


def test_exit_code_semantics_are_encoded_not_guessed(host_source: str) -> None:
    """
    这套系统的工具**用退出码表达状态**（`daily_drill --summary` 的 1 是"还没满 7 天"，
    不是失败）。插件必须把语义编码进来，否则模型会把"进行中"当故障。
    """
    assert "ok: [0, 1]" in host_source, "drill 的 1 必须被当成可接受结果"
    assert "verdict" in host_source


def test_no_credential_access_is_possible(host_source: str) -> None:
    """
    默认能力零凭证 —— 判据是"**拿不到**凭证"，不是"没提到这个词"。

    第一版直接禁了 `token` 这个词，于是被**否定句**（"不需要任何 token"）误报：
    这条自检自己的写法太死。真正要挡的是**能读到凭证的路径**。
    """
    lowered = host_source.lower()
    for needle in ("process.env", "environ", "getenv", "authorization", "private_key", "gh_token", ".write_token"):
        assert needle not in lowered, f"host.js 里出现读取凭证的路径：{needle}"
    # 正面承诺也要在：清单/说明里明说零凭证
    assert "needs_credentials: false" in (HERE / "plugin.yaml").read_text(encoding="utf-8")


# ------------------------------------------------- 宿主静态契约（第一轮真机跑出来的三条）
#
# 这一节的用例**不是**想出来的，是 2026-09-18 第一次把这个包喂进 `cordis_define` 时
# 一条条撞出来的：源码扫描全过、`cordis_define` 也收，但 `apply()` 一执行就抛错。
# 三条各自附上**报错原文**，免得以后又犯。
#
# 教训：只做源码自检的测试**测不出**宿主契约 —— 15 个用例全绿的时候，
# 这个包其实一次都跑不起来。真机跑一次，胜过源码扫一百遍。


def _parameters_block(host_source: str) -> str:
    """只取 `parameters:` 到 `output:` 之间那段 —— `output.schema` 里的
    `additionalProperties: false` 是合法的 JsonSchemaNode，不能一起误伤。"""
    start = host_source.index("      parameters: {")
    end = host_source.index("      output: {", start)
    return _strip_comments(host_source[start:end])


def _strip_comments(text: str) -> str:
    """把整行 `//` 注释去掉。

    **这个助手是被 bug 逼出来的**：本文件已经两次因为把注释当成代码而误报
    （一次是开头注释块被当成第一行代码，一次是注释里写着 `required: true` 四个字
    被当成真的用了逐属性 required）。凡是"某写法不许出现"的断言，都要先去掉注释。
    """
    return "\n".join(line for line in text.splitlines() if not line.strip().startswith("//"))


def test_parameters_root_stays_open(host_source: str) -> None:
    """报错原文：`harness.defineTool parameters.additionalProperties must be true or omitted
    because the implicit parameter root is open`（pkg-1 就是死在这条）。"""
    block = _parameters_block(host_source)
    assert "additionalProperties" not in block, (
        "参数根不能写 additionalProperties —— 宿主把它当隐式开放对象"
    )


def test_parameters_use_the_form_both_loaders_accept(host_source: str) -> None:
    """
    判据是**两条装载方式都收的那一种写法**，不是"哪种顺手"。

    这条用例原来断言根级 `required: ['mode']` 数组 —— 那是从 sandbox 的报错
    `harness.defineTool parameters.mode.required belongs to the containing raw
    object schema` 里学来的，而它其实是 **dynamic Package 独有**的写法。
    2026-09-18 包入口（profile 层）第一次真机加载时，`defineTool()` 走
    `parameterSchemaSpecToJsonSchema`（property-map），直接抛

        unsupported JSON schema: parameters.type must be a value schema object

    插件树加载失败 → DSH 起不来；而源码自检全绿、`--dump-config` 也过。
    逐属性 `required: true` 是唯一两边都收的写法：
      * sandbox：`normalizeParameterSchemaSpec` 的非 object 分支（`raw=false`），
        这个位置它明确要求是 `true`；
      * 包入口：`compilePropertyMap`（property-map 形态）。

    `mode` 必填；`repo_root` **不强制** —— 自包含时它由安装时写进 host.local.js 的
    `DEFAULT_REPO_ROOT` 兜底，传参只是覆盖。
    """
    block = _parameters_block(host_source)
    assert "required: true" in block, "必填项要写成逐属性 `required: true`（两条装载方式的公共写法）"
    assert block.count("required: true") == 1, "只有 mode 必填"
    assert "required: ['mode']" not in block, "根级 required 数组只有 sandbox 收，包入口的 defineTool 会拒绝"


def test_shell_command_is_a_pwsh_command_line(host_source: str) -> None:
    """
    `ShellExecRequest.command` 是**命令行**，不是 argv（契约原文：`command: string`）。
    本机实测三次，前两次都是 ParserError：

    1. 裸拼 `"exe" "arg"` → `表达式或语句中存在意外的标记"script.py"`；
    2. 只给每个 token 加单引号、不加调用符 → 同样 ParserError；
    3. `& 'exe' 'arg'` → doctor 七项自检全过、exit 0（**这个才是对的**）。

    第 1 版的 bug 还很阴：doctor 的 ParserError 让插件报出「自检有异常」——
    看起来像仓库有问题，其实是插件把命令行拼错了。**适配器出错会伪装成被适配者出错。**
    """
    assert "const command = '& '" in host_source, "命令行要用 pwsh 调用符起头"
    assert "split(\"'\").join(\"''\")" in host_source, "单引号要按 pwsh 规矩翻倍转义"
    assert "'\"' + item + '\"'" not in host_source, "别再用裸双引号拼命令行"


def test_misdiagnosis_table_is_wired_in(host_source: str) -> None:
    """误诊表必须真的被用上，不能只是写在那儿好看。"""
    assert "const MISDIAGNOSIS" in host_source
    assert "diagnose(result.stderr.text)" in host_source, "误诊表要在拿到 stderr 之后被调用"
    assert "misdiagnosis === null" in host_source


def test_misdiagnosis_patterns_match_the_real_error_texts(host_source: str) -> None:
    """
    **行为测试，不是源码扫描**：把 host.js 里的误诊正则抠出来，
    拿**真机抓下来的报错文本**验一遍。

    第一轮的教训在这里落地 —— 插件把命令行拼错时，doctor 抛的是 ParserError，
    插件却报「自检有异常」，把**适配器的错**说成**仓库的错**。

    ⚠️ **这个夹具曾经是编的，代价很大**：第一版我把"解释器找不到"的文案
    写成 `无法将“…”项识别为 cmdlet`（**我想象中** PowerShell 会这么说），
    用例于是全绿 —— 而真机报的是 `术语 '…' 不会被识别为 cmdlet`。
    **代码和用例基于同一个错误假设，就会手拉手地一起错。**
    下面两条文案是从真机输出里**原文抄下来的**，不是编的。
    """
    start = host_source.index("const MISDIAGNOSIS")
    end = host_source.index("function diagnose", start)
    patterns = re.findall(r"match: /(.+?)/i,", host_source[start:end], re.S)
    assert len(patterns) == 3, f"误诊表应当有三条，实际 {len(patterns)}：{patterns}"

    # 原文抄自 2026-09-18 真机：repo_autopilot_check(python='definitely-not-python')
    interpreter_missing = (
        "&: 术语 'definitely-not-python' 不会被识别为 cmdlet、函数、脚本文件或可执行程序的名称。\n"
        "请检查名称的拼写或验证路径是否正确(如果包含路径)，然后重试。"
    )
    # 原文抄自 2026-09-18 真机：repo_autopilot_check(repo_root='D:\\deepseek harness')
    entry_missing = (
        "D:\\PythonEnv\\uv\\cpython-3.12.13-windows-x86_64-none\\python.exe: can't open file "
        "'D:\\\\deepseek harness\\\\scripts\\\\doctor.py': [Errno 2] No such file or directory"
    )

    # 原文抄自 2026-09-18 真机：裸 `python` 跑 tools/acceptance.py
    module_missing = (
        "Traceback (most recent call last):\n"
        '  File "D:\\deepseek harness\\repo-autopilot\\tools\\acceptance.py", line 41, in <module>\n'
        "    import yaml\n"
        "ModuleNotFoundError: No module named 'yaml'"
    )

    assert any(re.search(p, interpreter_missing, re.I) for p in patterns), "没认出「解释器找不到」"
    assert any(re.search(p, module_missing, re.I) for p in patterns), "没认出「解释器缺依赖」"
    assert any(re.search(p, entry_missing, re.I) for p in patterns), "没认出「入口点找不到」"
    # 反向：**正常的自检输出不许被误诊**
    assert not any(re.search(p, "七项自检全过\nexit 0", re.I) for p in patterns)


def test_platform_assumption_is_recorded_not_hidden(manifest: dict) -> None:
    """`&` 是 PowerShell 的调用符 —— 这个平台假设必须写进偏差记录（不隐藏）。"""
    text = " ".join(manifest.get("deviations") or [])
    assert "PowerShell" in text, text
    assert "bash" in text, text


# --------------------------------------------------------- 干净机器冒烟脚本
#
# 这一节测 `scripts/smoke.py`：它的用途是"拿到一台新机器，先问能不能跑"。
# 一个不会失败的冒烟脚本等于没有 —— 所以**正面和负面都要测**。
#
# 加载方式用 importlib（不起子进程）：沙箱禁止通过管道抓别的程序输出，
# 而且直接调函数能精确断言"哪一条检查失败了"。


def _synthetic_repo(root: Path) -> Path:
    """造一个**只有入口点骨架**的假仓库根 —— 冒烟脚本只该看这些。"""
    for relative, _ in _load_smoke().REQUIRED_ENTRY_POINTS:
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("# stub\n", encoding="utf-8")
    return root


def _rmtree_tolerant(root: Path) -> None:
    """
    删一棵树，遇到只读文件先摘掉只读位。

    `shutil.rmtree(ignore_errors=True)` 在这里**不够**：插件目录里有**只读**的
    `vendor/repo-autopilot/ROADMAP.md`，复制出来的副本也带只读位，于是 rmtree 静默失败、
    上一轮测试的产物**残留**到下一轮（实测：dry-run 那条用例因此误报）。
    `ignore_errors` 会把"没删掉"这件事也一起吞掉 —— 那正是它危险的地方。
    """
    if not root.exists():
        return
    import stat

    for current, _dirs, files in os.walk(root):
        for name in files:
            try:
                (Path(current) / name).chmod(stat.S_IWRITE)
            except OSError:
                pass
    shutil.rmtree(root, ignore_errors=True)


@pytest.fixture
def scratch():
    """
    **不能用 pytest 的 `tmp_path`**：它建在系统临时目录，落在会话工作区**之外**，
    harness 的文件沙箱会直接拒绝（`PermissionError: [WinError 5]`）——
    repo-autopilot 的 `INSTALL.md` 里记过同一个坑（所以那边用 `.cache/test-scratch/`）。

    所以 scratch 建在**插件目录下面**，用完删掉。
    """
    root = HERE / ".cache" / "smoke-tests"
    _rmtree_tolerant(root)
    root.mkdir(parents=True, exist_ok=True)
    try:
        yield root
    finally:
        _rmtree_tolerant(root)


def test_smoke_is_stdlib_only_so_it_can_report_a_missing_dependency() -> None:
    """
    **前提检查**：冒烟脚本自己不能 import 三方库 ——
    否则"缺 yaml"这件事会让脚本本身崩掉，而不是被报出来。
    """
    source = SMOKE.read_text(encoding="utf-8")
    top_level = [
        line.strip()
        for line in source.splitlines()
        if re.match(r"^\s*(import|from)\s+\w", line)
    ]
    assert top_level, "至少要 import 点什么"
    for line in top_level:
        module = re.split(r"[\s.]+", line)[1]
        assert module in {
            "argparse", "shutil", "sys", "dataclasses", "pathlib", "__future__",
        }, f"冒烟脚本引用了非标准库：{line}"


def test_smoke_passes_on_a_complete_repo(scratch, monkeypatch) -> None:
    smoke = _load_smoke()
    monkeypatch.setattr(smoke.shutil, "which", lambda _name: "C:\\fake\\git.exe")
    checks = smoke.run_checks(_synthetic_repo(scratch))
    failed = [item.name for item in checks if not item.ok]
    # 依赖模块按本机实际解释器为准：venv 下应当全过；缺了就是这台机器真缺
    missing_modules = [name for name in failed if name.startswith("模块")]
    assert not missing_modules, f"运行测试的解释器缺依赖：{missing_modules}"
    assert not [name for name in failed if not name.startswith("模块")], failed


def test_smoke_names_the_entry_point_that_is_missing(scratch, monkeypatch) -> None:
    smoke = _load_smoke()
    monkeypatch.setattr(smoke.shutil, "which", lambda _name: "C:\\fake\\git.exe")
    root = _synthetic_repo(scratch)
    (root / "tools" / "package.py").unlink()

    checks = smoke.run_checks(root)
    failed = {item.name: item for item in checks if not item.ok}
    assert "tools/package.py" in failed, failed.keys()
    assert str(root / "tools" / "package.py") in failed["tools/package.py"].fix


def test_smoke_reports_a_missing_repo_root_loudly(scratch) -> None:
    smoke = _load_smoke()
    checks = smoke.run_checks(scratch / "definitely-not-here")
    failed = [item for item in checks if not item.ok]
    assert len(failed) == 1, [item.name for item in failed]
    assert failed[0].name == "仓库根"
    assert "不存在" in failed[0].detail


def test_smoke_module_check_actually_reports_the_import_error() -> None:
    """缺件要**照抄报错原文**，不能只说一句"缺了"。"""
    smoke = _load_smoke()
    check = smoke.check_module("definitely_no_such_module_xyz", "测试用")
    assert check.ok is False
    assert "ModuleNotFoundError" in check.detail
    assert "pip install definitely_no_such_module_xyz" in check.fix


def test_smoke_exit_codes_are_honest(scratch, monkeypatch, capsys) -> None:
    smoke = _load_smoke()
    monkeypatch.setattr(smoke.shutil, "which", lambda _name: "C:\\fake\\git.exe")

    good = _synthetic_repo(scratch / "good")
    good.mkdir(parents=True, exist_ok=True)
    _synthetic_repo(good)
    assert smoke.main(["--repo-root", str(good)]) == 0
    assert "可以跑" in capsys.readouterr().out

    assert smoke.main(["--repo-root", str(scratch / "nope")]) == 1
    assert "不能跑" in capsys.readouterr().out


# --------------------------------------------------------------- 一键安装脚本


def test_install_autodetects_the_repo_root(scratch, monkeypatch) -> None:
    install = _load_script("plugin_install", INSTALL)
    repo = _synthetic_repo(scratch / "somewhere" / "repo-autopilot")
    # 只认这个合成仓库。
    # 不加这行的话，"找不到"这一半根本测不了 —— 因为合成仓库的**祖先目录里就有一个真的
    # `repo-autopilot`**（本插件就放在它旁边），逐级向上找会**正确地**找到它。
    # 那不是 bug，是搜索逻辑按设计工作；但它会让断言变成"看这台机器上有什么"。
    monkeypatch.setattr(install, "is_repo_root", lambda candidate: candidate == repo.resolve())
    assert install.autodetect_repo_root(scratch / "somewhere") == repo.resolve()
    assert install.autodetect_repo_root(scratch / "nowhere") is None


def test_install_says_ok_for_a_complete_setup(scratch, capsys) -> None:
    install = _load_script("plugin_install", INSTALL)
    repo = _synthetic_repo(scratch / "repo-autopilot")
    assert install.main(["--repo-root", str(repo)]) == 0
    assert "可以注册" in capsys.readouterr().out


def test_install_fails_loudly_on_a_missing_repo_root(scratch, capsys) -> None:
    install = _load_script("plugin_install", INSTALL)
    assert install.main(["--repo-root", str(scratch / "nope")]) == 1
    out = capsys.readouterr().out
    assert "不能直接注册" in out
    assert "--repo-root" in out


def test_install_next_steps_survive_a_gbk_console() -> None:
    """
    **这条是真机 bug 逼出来的**：中文 Windows 的控制台是 GBK，
    报错文案里带一个 `⚠️` 就会 `UnicodeEncodeError` 把脚本**当场打崩** ——
    而且偏偏崩在"要报告缺件"的那一刻，用户最需要输出时反而看到 traceback。

    判据很直接：把要打印的文案按 GBK 编一遍，编不过就是会崩。
    """
    install = _load_script("plugin_install", INSTALL)
    for all_good in (True, False):
        text = install.render_next_steps(sys.executable, Path("X:/repo"), all_good=all_good)
        text.encode("gbk")  # 抛 UnicodeEncodeError 就说明这条用例失败


def test_install_dry_run_never_installs_anything(scratch, monkeypatch, capsys) -> None:
    """`--dry-run` 只说不做：一旦真的调了 pip，这条用例就会炸。"""
    install = _load_script("plugin_install", INSTALL)

    def _explode(*_args, **_kwargs):
        raise AssertionError("--dry-run 不应该真的执行 pip")

    monkeypatch.setattr(install.subprocess, "call", _explode)
    repo = _synthetic_repo(scratch / "repo-autopilot")
    install.main(["--repo-root", str(repo), "--install-deps", "--dry-run"])
    capsys.readouterr()


# ------------------------------------------- install.ps1：两条 PowerShell 5.1 的坑
#
# 这两条都是**真机跑出来的**，而且都不便宜：没 BOM 时整个脚本 ParserError 跑不起来；
# 探测代码里带双引号时，装好依赖的解释器会被判成"依赖不齐"。各留一条钉子。


def test_install_ps1_has_a_utf8_bom() -> None:
    """
    **Windows PowerShell 5.1 在没有 BOM 时把 UTF-8 当 ANSI 读** —— 中文注释被解码成乱码，
    解析器接着就在字符串里找不到收尾引号，直接 `ParserError`，**整个脚本跑不起来**。
    （`pwsh` 7 默认按 UTF-8 读，所以这个问题只在"Windows 自带的那个 shell"上出现 ——
    而那恰恰是双击 .ps1 时用的那个。）
    """
    raw = (HERE / "scripts" / "install.ps1").read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf"), "install.ps1 必须以 UTF-8 BOM 开头，否则 PS 5.1 会解析失败"


def test_install_ps1_probes_contain_no_double_quotes() -> None:
    """
    PowerShell 5.1 给原生命令传参时对**双引号**的处理是有损的：探测代码里的 `"yaml"`
    经函数传参后被搞坏，Python 报语法错、退出码 1 —— 于是**装好依赖的解释器也被判成"依赖不齐"**。
    实测：venv 明明依赖齐，却被判不齐，然后去走了"版本够但依赖不齐"那条路。
    所以探测代码一律用 PowerShell 的 `''` 转义在 Python 里造字符串，**全程不见双引号**。
    """
    lines = [
        line
        for line in (HERE / "scripts" / "install.ps1").read_text(encoding="utf-8-sig").splitlines()
        if line.strip().startswith("$probe")
    ]
    assert lines, "install.ps1 里应当能找到 $probe 开头的探测定义"
    for line in lines:
        assert '"' not in line, f"探测代码里不许出现双引号：{line}"


def test_smoke_survives_a_strict_console_encoding(monkeypatch) -> None:
    """
    **这条是 CI 逼出来的**：GitHub 的 `windows-latest` 是英文 Windows，控制台编码 **cp1252**，
    `smoke.py` 打印中文时 `UnicodeEncodeError` 直接退出 1 —— 而那一步在 ubuntu 上是绿的，
    本地（中文 Windows，GBK）也照样绿，所以只在 CI 上暴露。

    这里把 stdout 换成**严格 cp1252** 的 TextIOWrapper 真跑一遍 `main()`：
    只要有兜底就不会抛；没有兜底这条用例会直接 `UnicodeEncodeError`。
    （`install.py` 当初也犯过同一个错，那边已有用例。）
    """
    import io

    smoke = _load_smoke()
    raw = io.BytesIO()
    monkeypatch.setattr(sys, "stdout", io.TextIOWrapper(raw, encoding="cp1252", errors="strict"))
    try:
        rc = smoke.main([])  # 默认查自带副本
    finally:
        sys.stdout.flush()
    written = raw.getvalue().decode("cp1252")
    assert written.strip(), "应当有输出"
    assert rc in (0, 1), rc


def test_bootstrap_advice_matches_what_the_machine_actually_has() -> None:
    """引导档要**按本机实际有什么**分档，不能一律甩一句"去装 Python"。"""
    install = _load_script("plugin_install", INSTALL)
    venv_dir = Path("X:/plugin/.venv")

    with_uv = install.bootstrap_advice({"uv": "C:/uv.exe", "winget": None, "py": None}, venv_dir=venv_dir)
    assert any("uv venv" in line for line in with_uv)
    assert any("uv pip install" in line for line in with_uv)

    with_winget = install.bootstrap_advice(
        {"uv": None, "winget": "C:/winget.exe", "py": None}, venv_dir=venv_dir
    )
    assert any("winget install" in line for line in with_winget)
    assert any("astral-sh.uv" in line for line in with_winget)

    with_neither = install.bootstrap_advice({"uv": None, "winget": None, "py": None}, venv_dir=venv_dir)
    assert any("python.org" in line for line in with_neither)


# --------------------------------------------------- 自包含：自带副本 + 防漂移
#
# 这一节钉住"插件自带一份 repo-autopilot"这件事的两个要害：
#   1. 自带的那份确实在，且与它自己的 MANIFEST 逐文件 sha256 一致（没被改过）；
#   2. 路径是**安装时写进 host.local.js** 的 —— Host 半边拿不到自己的磁盘位置。


def _tiny_bundle(root: Path) -> Path:
    """造一个最小可校验的"包"：一个文件 + 一份含它 sha256 的清单。"""
    root.mkdir(parents=True, exist_ok=True)
    (root / "a.txt").write_text("hello\n", encoding="utf-8")
    digest = hashlib.sha256((root / "a.txt").read_bytes()).hexdigest()
    (root / "MANIFEST.json").write_text(
        json.dumps({"source": {"commit": "deadbee"}, "files": {"a.txt": {"sha256": digest}}}),
        encoding="utf-8",
    )
    return root


def test_bundled_copy_is_shipped_and_matches_its_manifest() -> None:
    """
    **自包含的底线**：随插件打包的那份 repo-autopilot 必须真的在，且逐文件 sha256 与
    它自己的 MANIFEST 一致。这条要是红了，"装完不用另 clone"就是空话。
    """
    install = _load_script("plugin_install", INSTALL)
    assert BUNDLED.is_dir(), f"没有自带副本：{BUNDLED}（应当由 tools/package.py build 打出并复制进来）"
    ok, notes = install.verify_manifest(BUNDLED)
    assert ok, notes
    assert install.bundled_repo_root() == BUNDLED


def test_smoke_defaults_to_the_bundled_copy() -> None:
    """不打 --repo-root 时，冒烟脚本查的就该是自带副本。"""
    smoke = _load_smoke()
    assert smoke.BUNDLED == BUNDLED


def test_verify_manifest_accepts_an_untouched_bundle(scratch) -> None:
    install = _load_script("plugin_install", INSTALL)
    ok, notes = install.verify_manifest(_tiny_bundle(scratch / "bundle"))
    assert ok is True, notes


def test_verify_manifest_catches_a_changed_file(scratch) -> None:
    install = _load_script("plugin_install", INSTALL)
    bundle = _tiny_bundle(scratch / "bundle")
    (bundle / "a.txt").write_text("tampered\n", encoding="utf-8")
    ok, notes = install.verify_manifest(bundle)
    assert ok is False
    assert any("内容不符" in note for note in notes), notes


def test_verify_manifest_catches_a_missing_file(scratch) -> None:
    install = _load_script("plugin_install", INSTALL)
    bundle = _tiny_bundle(scratch / "bundle")
    (bundle / "a.txt").unlink()
    ok, notes = install.verify_manifest(bundle)
    assert ok is False
    assert any("缺失" in note for note in notes), notes


def test_verify_manifest_reports_a_missing_manifest(scratch) -> None:
    install = _load_script("plugin_install", INSTALL)
    ok, notes = install.verify_manifest(scratch / "nothing-here")
    assert ok is False and any("没有清单" in note for note in notes), notes


def test_emit_host_bakes_the_path_and_changes_only_that_line(scratch) -> None:
    install = _load_script("plugin_install", INSTALL)
    target = scratch / "host.local.js"
    install.emit_host(BUNDLED, target)

    original = HOST.read_text(encoding="utf-8").splitlines()
    emitted = target.read_text(encoding="utf-8").splitlines()
    assert len(original) == len(emitted)
    differing = [(n, a, b) for n, (a, b) in enumerate(zip(original, emitted), 1) if a != b]
    assert len(differing) == 1, f"只应当改 DEFAULT_REPO_ROOT 那一行，实际改了 {len(differing)} 行"
    _, before, after = differing[0]
    assert before.strip() == "const DEFAULT_REPO_ROOT = ''"
    assert str(BUNDLED).replace("\\", "\\\\") in after


def test_emit_host_refuses_a_host_without_the_marker(scratch, monkeypatch) -> None:
    """占位符没了就**响亮报错**，不能生成一份"看着像、其实没写路径"的 host。"""
    install = _load_script("plugin_install", INSTALL)
    package = scratch / "pkg"
    (package / "scripts").mkdir(parents=True)
    (package / "host.js").write_text("// 没有占位符\n", encoding="utf-8")
    monkeypatch.setattr(install, "HERE", package / "scripts")
    with pytest.raises(SystemExit):
        install.emit_host(BUNDLED, scratch / "out.js")


def test_host_js_ships_with_an_empty_placeholder(host_source: str) -> None:
    """
    发布出去的 host.js 里，`DEFAULT_REPO_ROOT` 必须是**空串占位** ——
    填好路径的那份是 `host.local.js`（安装时生成、已 gitignore），不进仓库。

    注意**不要**顺手断言"host.js 里不许出现 D:\\"：报错文案里那句
    「例如 D:\\PythonEnv\\venv\\Scripts\\python.exe」是给人看的**示例路径**，不是本机默认值。
    （第一版就是这么写的，误报了。）
    """
    assert "const DEFAULT_REPO_ROOT = ''" in host_source, "必须是空串占位"
    declarations = [
        line.strip() for line in host_source.splitlines() if line.strip().startswith("const DEFAULT_REPO_ROOT")
    ]
    assert declarations == ["const DEFAULT_REPO_ROOT = ''"], f"占位必须是空串：{declarations}"
    # 而且它得**真的被用上**（否则"自带副本"这条路径根本没接进解析逻辑）。
    assert "? args.repo_root : DEFAULT_REPO_ROOT" in host_source, "repo_root 缺省时要落到 DEFAULT_REPO_ROOT"


# ------------------------------------------- 两种装载方式：不允许逻辑分叉
#
# 同一个插件有两种装法：
#   * dynamic Package —— host.js 全文喂给 cordis_define（可热更）；
#   * 插件包 / profile 层 —— lib/index.js 导出 apply，`dsh plugin add` 装（可一键）。
# ROADMAP 7.3 与 AGENTS.md 都要求两条入口共享同一套实现。这里的办法是
# **lib/index.js 由 host.js 生成**，再用一条用例钉住"生成物 == 生成器的输出"。


def test_lib_index_js_is_generated_from_host_js() -> None:
    """手改 `lib/index.js`（或改了 host.js 忘了重新生成）都会在这里红。"""
    builder = _load_script("plugin_build_module", HERE / "scripts" / "build_module.py")
    generated = builder.generate()
    actual = (HERE / "lib" / "index.js").read_text(encoding="utf-8")
    assert actual == generated, "lib/index.js 与 host.js 不一致 —— 跑 python scripts/build_module.py 重新生成"


def test_generated_module_exports_apply_and_reuses_the_same_body() -> None:
    module = (HERE / "lib" / "index.js").read_text(encoding="utf-8")
    host = HOST.read_text(encoding="utf-8")
    assert "export const apply = plugin.apply" in module
    assert host in module, "生成物必须原样包住 host.js 的全文（否则就是抄了一份、会漂移）"


def test_package_json_declares_a_loadable_profile_bundle(manifest_bundle: dict) -> None:
    assert manifest_bundle["name"] == "repo-autopilot-plugin"
    assert manifest_bundle["type"] == "module"
    assert manifest_bundle["main"] == "lib/index.js"

    patch = manifest_bundle["dsh"]["bundle"]["patch"]
    assert (HERE / patch).is_file(), f"dsh.bundle.patch 指向的文件不存在：{patch}"
    assert patch.lstrip("./") in " ".join(manifest_bundle["exports"].values()), (
        "exports 里必须能取到 cordis.patch.yml，否则 profile 层加载时会解析不到"
    )
    for required in ("lib/index.js", "cordis.patch.yml", "vendor/"):
        assert required in manifest_bundle["files"], f"files 里少了 {required}（npm 打包会漏掉它）"


def test_patch_row_points_at_this_package(manifest_bundle: dict) -> None:
    """profile 补丁里的行必须解析到**本包**，否则装完什么都不会装载。"""
    rows = yaml.safe_load((HERE / "cordis.patch.yml").read_text(encoding="utf-8"))
    assert isinstance(rows, list) and rows, "补丁应当是一个非空的列表"
    inserted = [row for entry in rows for row in (entry.get("insert") or [])]
    assert inserted, "补丁里没有 insert 段"
    names = {row["name"] for row in inserted}
    assert manifest_bundle["name"] in names, f"补丁指向的是 {names}，而本包叫 {manifest_bundle['name']}"
    assert all(row.get("id") for row in inserted), "每一行都要有 id（profile 靠 id 定位行）"


def test_one_click_installer_is_present_and_targets_this_package() -> None:
    """
    双击入口必须存在，并且**确实**把"装进 profile"这一步接上了 ——
    否则"点击即一键安装"就是空话（脚本只检查环境、不装东西）。
    装 profile 的逻辑在 install_profile.py 里（A 包管理器 → 失败自动退 B）。
    """
    text = (HERE / "install.cmd").read_text(encoding="utf-8", errors="replace")
    assert text.strip(), "install.cmd 不应为空"
    assert "install.ps1" in text, "要先跑环境检查/完整性校验那一步"
    assert "-EmitHost" in text, "要生成 host.local.js（dynamic Package 那条路也用得上）"
    assert "-InstallProfile" in text, "要真的装进 profile，而不是只检查环境"
    assert (HERE / "scripts" / "install_profile.py").is_file()


def test_cmd_files_are_crlf_in_the_index() -> None:
    """`.cmd` 要 CRLF：cmd.exe 对批处理行尾敏感，跳转标签尤其。"""
    attrs = (HERE / ".gitattributes").read_text(encoding="utf-8")
    assert "*.cmd text eol=crlf" in attrs, ".gitattributes 里要把 .cmd 固定成 CRLF"


# --------------------------------------------- 装进 profile：A 失败要能退回 B
#
# 实测教训：`dsh plugin add` 是 **pnpm 驱动**的，而这台机器没有 pnpm
# （`'pnpm' is not recognized`）。所以装了 A 还必须有一条不需要包管理器、不需要网络的 B：
# 本插件**零依赖**，B 就是 pnpm + reconcilePlugins 会得到的那个终态。


def _fake_profile(root: Path, name: str = "web") -> Path:
    profile = root / "profiles" / name
    profile.mkdir(parents=True, exist_ok=True)
    (profile / "package.json").write_text(
        json.dumps(
            {
                "name": f"dsh-profile-{name}",
                "dependencies": {},
                "dsh": {"profile": {"bundles": ["@deepseek-ai/dsh-base"]}},
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    (profile / "node_modules").mkdir(exist_ok=True)
    return profile


def test_bundle_install_writes_the_final_state_and_verifies(scratch) -> None:
    profile = _fake_profile(scratch)
    module = _load_script("plugin_install_profile", HERE / "scripts" / "install_profile.py")

    ok, detail = module.install_by_bundle(profile, dry_run=False)
    assert ok, detail

    installed = profile / "node_modules" / module.package_name()
    assert (installed / "package.json").is_file(), "插件要真的被复制进 node_modules"
    assert (installed / "cordis.patch.yml").is_file(), "补丁文件必须跟着一起装进去"

    manifest = json.loads((profile / "package.json").read_text(encoding="utf-8"))
    assert module.package_name() in manifest["dsh"]["profile"]["bundles"], "bundles 里要有它"
    assert module.package_name() in manifest["dependencies"]
    # 备份要留下（改别人的 profile 清单，必须可回退）
    assert list(profile.glob("package.json.bak-install-profile-*")), "改清单前要备份"

    ok, notes = module.verify(profile)
    assert ok, notes


def test_bundle_install_dry_run_touches_nothing(scratch) -> None:
    profile = _fake_profile(scratch)
    module = _load_script("plugin_install_profile_dry", HERE / "scripts" / "install_profile.py")
    before = (profile / "package.json").read_text(encoding="utf-8")

    ok, detail = module.install_by_bundle(profile, dry_run=True)
    assert ok and "dry-run" in detail
    assert (profile / "package.json").read_text(encoding="utf-8") == before
    assert not (profile / "node_modules" / module.package_name()).exists()


def test_verify_catches_a_profile_that_would_not_load_it(scratch) -> None:
    """自证要能识别"装了但不会被装载"这种情况 —— 否则等于没验。"""
    profile = _fake_profile(scratch)
    module = _load_script("plugin_install_profile_verify", HERE / "scripts" / "install_profile.py")
    ok, notes = module.verify(profile)
    assert ok is False
    assert any("bundles 里没有" in note for note in notes), notes


def test_pnpm_lookup_prefers_path_then_corepack(monkeypatch) -> None:
    module = _load_script("plugin_install_profile_pnpm", HERE / "scripts" / "install_profile.py")
    monkeypatch.setattr(module.shutil, "which", lambda name: "C:/pnpm.exe" if name == "pnpm" else None)
    assert module.find_pnpm() == ["C:/pnpm.exe"]

    monkeypatch.setattr(module.shutil, "which", lambda name: "C:/corepack.cmd" if name == "corepack" else None)
    assert module.find_pnpm() == ["C:/corepack.cmd", "pnpm"]

    monkeypatch.setattr(module.shutil, "which", lambda name: None)
    assert module.find_pnpm() is None, "两样都没有时要明确返回 None，好让调用方退回 B"


# ------------------------------------- 沙箱拦下工作区外的写：交给人类，而不是反复提权
#
# 这份部署的沙箱是**故意**钉成 workspace-write 的（%DSH_HOME%\cordis.patch.yml 里
# 显式写明 danger-full-access 不启用、并把该预设从表里删掉）。而装 profile 必须写
# %DSH_HOME%\profiles\...，天然在工作区之外 —— 于是 agent 每跑一次就要人批一次。
# 正确的分工：用户双击 install.cmd（不经沙箱、零提示）；agent 只把命令交出去。


def test_bundle_install_hands_the_command_out_instead_of_failing(scratch, monkeypatch) -> None:
    profile = _fake_profile(scratch)
    module = _load_script("plugin_install_profile_outside", HERE / "scripts" / "install_profile.py")

    def _denied(_destination):
        raise PermissionError("[WinError 5] 拒绝访问 —— 沙箱拦下了工作区之外的写")

    monkeypatch.setattr(module, "copy_plugin", _denied)
    with pytest.raises(module.NeedsOutsideSandbox) as excinfo:
        module.install_by_bundle(profile, dry_run=False)

    assert "install_profile.py" in excinfo.value.command, "要把可粘贴的命令交出去"
    assert "--profile web" in excinfo.value.command


def test_main_exits_3_with_a_pasteable_command_when_denied(scratch, monkeypatch, capsys) -> None:
    """退出码 3 = 「请你在沙箱外跑一次」，与 1（真失败）区分开。"""
    profile = _fake_profile(scratch)
    module = _load_script("plugin_install_profile_exit3", HERE / "scripts" / "install_profile.py")

    monkeypatch.setattr(module.shutil, "which", lambda name: None)  # 让 A 直接失败，走 B
    monkeypatch.setattr(
        module,
        "install_by_bundle",
        lambda *_a, **_k: (_ for _ in ()).throw(module.NeedsOutsideSandbox("python x.py", PermissionError("denied"))),
    )
    rc = module.main(["--profile", "web", "--dsh-home", str(scratch), "--method", "bundle"])
    out = capsys.readouterr().out
    assert rc == 3, out
    assert "沙箱" in out and "install.cmd" in out, out
