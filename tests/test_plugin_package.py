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


def test_required_is_a_root_level_array(host_source: str) -> None:
    """报错原文：`harness.defineTool parameters.mode.required belongs to the containing
    raw object schema`（pkg-2 死在这条）。

    `mode` 必填；`repo_root` **不强求** —— 自包含时它由安装时写进 host.local.js 的
    `DEFAULT_REPO_ROOT` 兜底，传参只是覆盖。
    """
    block = _parameters_block(host_source)
    assert "required: ['mode']" in block, "必填项要写成根级数组，且只要求 mode"
    assert "required: true" not in block, "逐属性 required: true 会被宿主拒绝"


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


@pytest.fixture
def scratch():
    """
    **不能用 pytest 的 `tmp_path`**：它建在系统临时目录，落在会话工作区**之外**，
    harness 的文件沙箱会直接拒绝（`PermissionError: [WinError 5]`）——
    repo-autopilot 的 `INSTALL.md` 里记过同一个坑（所以那边用 `.cache/test-scratch/`）。

    所以 scratch 建在**插件目录下面**，用完删掉。
    """
    root = HERE / ".cache" / "smoke-tests"
    if root.exists():
        shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True, exist_ok=True)
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


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
