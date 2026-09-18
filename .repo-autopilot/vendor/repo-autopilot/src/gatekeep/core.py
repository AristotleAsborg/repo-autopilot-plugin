"""4.3 测试门禁：合入前的四道关（**补丁再漂亮也得先过这四关**）。

路线原文：

> ① 目标测试全绿；② 全量回归不新增失败；③ lint/类型检查无新增告警；
> ④ 路径黑名单（`state/approvals/`、CI 配置、LICENSE、**测试与门禁代码本身**——防 Agent 改测试过测试）。
> 任一失败 → 回修复循环；同一 issue 最多 3 轮完整尝试 → `needs_human` + 失败报告。

## 为什么门禁必须**在修复循环之外**独立存在

修复循环自己跑测试就够了 —— 那是"目标测试"。问题在于**另外三件事它看不见**：
补丁可能顺手改了一个测试文件（于是"目标测试全绿"变成自证）、
可能把别处弄坏了（目标测试不管别的模块）、可能引入一堆 lint 告警（回头 CI 红）。
这三件事的共同点是：**它们都只在"别人那里"才会发作**。

## 顺序不是随便排的

黑名单**排第一**：一个改了测试文件的补丁，后面三关的结果**全部不可信**
（测试是他改的），先把它拦下来，省下的是两分钟一次的全量回归。

## "不新增失败"要按**差集**算

全量回归的绝对失败数没有意义（仓库本来就可能有红的）。基线在**未打补丁的副本**上先跑一遍，
只把"基线没有、打完之后才有"的失败算作新增 —— 否则任何一次脏仓库都会让门禁永远红。
"""

from __future__ import annotations

import dataclasses
import re
import shutil
import subprocess
import sys
import uuid
from collections.abc import Callable, Sequence
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
GATE_ROOT = ROOT / "state" / "gate"

#: 同一 issue 最多允许的**完整尝试**次数（含门禁）。超了就交给人，不再自动刷轮次。
MAX_GATE_ATTEMPTS = 3

#: 路径黑名单：(正则, 为什么不能碰)。顺序即匹配顺序，命中即拦。
BLACKLIST: tuple[tuple[str, str], ...] = (
    (r"^state/approvals/", "审批单是闸门的证据，改它等于篡改批准记录"),
    (r"^state/progress\.md$", "状态表只能由验收脚本写（路线 0.2 的硬机制）"),
    (r"^\.github/", "CI 配置：改了它就能让门禁自己失效"),
    (r"(^|/)LICENSE(\.|$)", "许可证"),
    (r"(^|/)tests?/", "测试代码本身（防 Agent 改测试过测试）"),
    (r"(^|/)test_[^/]*\.py$", "测试文件"),
    (r"_test\.py$", "测试文件"),
    (r"(^|/)conftest\.py$", "测试配置"),
    (r"^src/gatekeep/", "门禁自己的代码"),
    (r"^tools/acceptance\.py$", "验收脚本（它就是那个写状态表的东西）"),
    (r"^scripts/check_[^/]*\.py$", "CI 里跑的检查脚本"),
)

IGNORE = shutil.ignore_patterns(
    ".git", "__pycache__", ".pytest_cache", "pytest-cache-files-*", ".ruff_cache", ".mypy_cache"
)

_FAILED_RE = re.compile(r"^(?:FAILED|ERROR)\s+(\S+)", re.MULTILINE)
_LINT_COUNT_RE = re.compile(r"Found (\d+) error")

RunCmd = Callable[[Sequence[str], Path], tuple[int, str]]


class GateError(RuntimeError):
    """门禁自身的错误（补丁打不上、仓库不存在）。绝不静默放过。"""


@dataclasses.dataclass(frozen=True)
class GateVerdict:
    """四道关的结果。`reasons` 是给修复循环/人看的一句话结论。"""

    passed: bool
    gates: dict[str, dict]
    reasons: list[str]
    new_failures: list[str] = dataclasses.field(default_factory=list)
    lint_delta: int = 0
    workdir: str | None = None

    def as_dict(self) -> dict:
        return dataclasses.asdict(self)

    def summary(self) -> str:
        return "；".join(self.reasons) if self.reasons else "四道关全绿"


# ------------------------------------------------------------------ 补丁解析

def patch_paths(patch_text: str) -> list[str]:
    """
    从 unified diff 里取被改动的路径（去重、保序）。

    三种来源都看：`diff --git a/x b/x`、`--- a/x`、`+++ b/x`。
    只看 `+++` 会漏掉**纯删除**的文件（它只有 `---` 没有 `+++`）——
    而"删掉测试文件"正是最该被拦的一种。
    """
    paths: list[str] = []
    for line in (patch_text or "").splitlines():
        candidate: str | None = None
        if line.startswith("diff --git "):
            parts = line.split()
            if len(parts) >= 4:
                candidate = parts[3]
        elif line.startswith(("+++ ", "--- ")):
            candidate = line[4:].strip()
        if not candidate or candidate == "/dev/null":
            continue
        name = candidate[2:] if candidate[:2] in ("a/", "b/") else candidate
        name = name.split("\t")[0].strip()
        if name and name not in paths:
            paths.append(name)
    return paths


def blacklist_hits(paths: Sequence[str]) -> list[tuple[str, str]]:
    hits: list[tuple[str, str]] = []
    for path in paths:
        normalized = path.replace("\\", "/")
        # 只吃掉一个开头的 "./" —— 用 lstrip("./") 会把 `.github/...` 的**点**也吃掉，
        # 于是"CI 配置"这条黑名单形同虚设（实测踩过）。
        normalized = normalized.removeprefix("./")
        for pattern, why in BLACKLIST:
            if re.search(pattern, normalized):
                hits.append((path, why))
                break
    return hits


# ------------------------------------------------------------------ 输出解析

def parse_pytest_failures(output: str) -> set[str]:
    """从 pytest 输出里取失败用例 id（`FAILED tests/x.py::test_y`）。"""
    return {match.group(1) for match in _FAILED_RE.finditer(output or "")}


def parse_lint_count(output: str) -> int:
    """从 ruff 输出里取告警条数；解析不出来按 0（宁可漏，不可误报）。"""
    match = _LINT_COUNT_RE.search(output or "")
    return int(match.group(1)) if match else 0


# ------------------------------------------------------------------ 执行

def default_runner(command: Sequence[str], cwd: Path) -> tuple[int, str]:
    outcome = subprocess.run(
        list(command),
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=1800,
        check=False,
    )
    return outcome.returncode, (outcome.stdout or "") + (outcome.stderr or "")


def default_commands(full_scope: str = ".") -> dict[str, list[str]]:
    """三道命令：目标测试由调用方给；这里是全量回归与 lint 的默认值。"""
    return {
        "full": [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"],
        "lint": [sys.executable, "-m", "ruff", "check", full_scope],
    }


# ------------------------------------------------------------------ 主流程

def capture_baseline(
    repo: Path | str,
    *,
    runner: RunCmd | None = None,
    full_cmd: Sequence[str] | None = None,
    lint_cmd: Sequence[str] | None = None,
) -> dict:
    """
    在**未打补丁**的副本上先跑一遍全量回归与 lint，作为差集基线。

    为什么要副本而不是直接在原仓库跑：门禁会执行仓库里的代码，
    而"执行别人的代码"这件事只在副本里做（与 1.5 沙箱同一条纪律）。
    """
    runner = runner or default_runner
    commands = default_commands()
    workdir = GATE_ROOT / f"baseline-{uuid.uuid4().hex[:8]}"
    if workdir.exists():
        shutil.rmtree(workdir, ignore_errors=True)
    shutil.copytree(Path(repo), workdir, ignore=IGNORE)

    _, full_output = runner(list(full_cmd or commands["full"]), workdir)
    _, lint_output = runner(list(lint_cmd or commands["lint"]), workdir)
    return {
        "workdir": str(workdir),
        "failures": sorted(parse_pytest_failures(full_output)),
        "lint_count": parse_lint_count(lint_output),
    }


def run_gate(
    repo: Path | str,
    patch_file: Path | str,
    *,
    target_cmd: Sequence[str] | None = None,
    full_cmd: Sequence[str] | None = None,
    lint_cmd: Sequence[str] | None = None,
    baseline: dict | None = None,
    runner: RunCmd | None = None,
) -> GateVerdict:
    """
    跑四道关。**任一道红即整单否决**，并把原因带回去给修复循环。

    `baseline` 由 `capture_baseline()` 提供；不给就现场算一遍（慢但正确）。
    """
    runner = runner or default_runner
    repo_path = Path(repo)
    patch_path = Path(patch_file)
    commands = default_commands()
    target_cmd = list(target_cmd or [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"])
    full_cmd = list(full_cmd or commands["full"])
    lint_cmd = list(lint_cmd or commands["lint"])

    gates: dict[str, dict] = {}
    reasons: list[str] = []

    # ---- 第 ④ 关先跑：补丁碰了不该碰的路径，后面三关的结果都不可信
    paths = patch_paths(patch_path.read_text(encoding="utf-8", errors="replace"))
    hits = blacklist_hits(paths)
    gates["blacklist"] = {"ok": not hits, "paths": paths, "hits": hits}
    if hits:
        reasons.append("路径黑名单：" + "；".join(f"{path}（{why}）" for path, why in hits))
        return GateVerdict(False, gates, reasons)

    # ---- 打补丁到副本
    workdir = GATE_ROOT / f"gate-{uuid.uuid4().hex[:8]}"
    if workdir.exists():
        shutil.rmtree(workdir, ignore_errors=True)
    shutil.copytree(repo_path, workdir, ignore=IGNORE)
    from src.sandbox import apply_patch  # 局部导入：避免与沙箱模块形成循环依赖

    # 日志**必须写在副本之外**：apply_patch 靠"目录指纹变了没有"验证补丁是否真的落上，
    # 而写进副本里的日志本身就会改变指纹 —— 于是"git apply 什么都没做"也会被判成功
    # （实测踩过：门禁报 apply ok，副本里的文件根本没改，接着三关全在测旧代码）。
    applied, detail = apply_patch(workdir, patch_path, workdir.parent / f"{workdir.name}-apply.log")
    gates["apply"] = {"ok": applied, "detail": detail}
    if not applied:
        reasons.append(f"补丁打不上：{detail}")
        return GateVerdict(False, gates, reasons, workdir=str(workdir))

    # ---- 第 ① 关：目标测试
    target_code, _target_output = runner(target_cmd, workdir)
    gates["target"] = {"ok": target_code == 0, "exit": target_code}
    if target_code != 0:
        reasons.append("目标测试没全绿")
        return GateVerdict(False, gates, reasons, workdir=str(workdir))

    # ---- 第 ② 关：全量回归不新增失败（按差集）
    if baseline is None:
        baseline = capture_baseline(repo_path, runner=runner, full_cmd=full_cmd, lint_cmd=lint_cmd)
    baseline_failures = set(baseline.get("failures") or [])
    _, full_output = runner(full_cmd, workdir)
    failures = parse_pytest_failures(full_output)
    new_failures = sorted(failures - baseline_failures)
    gates["regression"] = {
        "ok": not new_failures,
        "baseline_failures": sorted(baseline_failures),
        "failures": sorted(failures),
        "new": new_failures,
    }
    if new_failures:
        reasons.append("全量回归新增失败：" + "、".join(new_failures[:5]))

    # ---- 第 ③ 关：lint 不新增告警
    _, lint_output = runner(lint_cmd, workdir)
    lint_count = parse_lint_count(lint_output)
    baseline_lint = int(baseline.get("lint_count") or 0)
    lint_delta = lint_count - baseline_lint
    gates["lint"] = {"ok": lint_delta <= 0, "count": lint_count, "baseline": baseline_lint}
    if lint_delta > 0:
        reasons.append(f"lint 新增 {lint_delta} 条告警（基线 {baseline_lint} → {lint_count}）")

    return GateVerdict(
        passed=not reasons,
        gates=gates,
        reasons=reasons,
        new_failures=new_failures,
        lint_delta=lint_delta,
        workdir=str(workdir),
    )


def needs_human(attempts: int) -> bool:
    """完整尝试达到上限 → 交给人（路线：同一 issue 最多 3 轮，超了就 `needs_human`）。"""
    return attempts >= MAX_GATE_ATTEMPTS


def failure_report(issue_id: str, verdicts: Sequence[GateVerdict]) -> str:
    """把若干轮门禁结果写成一份给人看的失败报告（配合 3.3 的 `needs_human` 一起用）。"""
    lines = [
        f"# 门禁失败报告：{issue_id}",
        "",
        f"- 完整尝试 {len(verdicts)} 轮（上限 {MAX_GATE_ATTEMPTS}）",
        f"- 结论：**{MAX_GATE_ATTEMPTS} 轮仍未过门禁 → needs_human**（不再自动重试）",
        "",
        "> 停下来说明的不是「我们不行」，而是「再自动试下去只会重复同一种失败」。",
        "> 下一步该由人决定：放宽哪一条、还是换思路。",
        "",
    ]
    for index, verdict in enumerate(verdicts, start=1):
        lines.append(f"## 第 {index} 轮")
        for reason in verdict.reasons:
            lines.append(f"- {reason}")
        for name, gate in verdict.gates.items():
            lines.append(f"- `{name}`：{gate}")
        lines.append("")
    return "\n".join(lines)
