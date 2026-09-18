"""4.3 测试门禁（`src/gatekeep/`）。

    from src.gatekeep import run_gate, capture_baseline, patch_paths, needs_human

四道关，**任一道红即整单否决**：

1. `target` —— 目标测试全绿；
2. `regression` —— 全量回归**不新增**失败（与未打补丁的基线做差集）；
3. `lint` —— lint / 类型检查**不新增**告警；
4. `blacklist` —— 路径黑名单：审批单、状态表、CI 配置、LICENSE、**测试与门禁代码本身**。

黑名单排在最前面：一个改了测试文件的补丁，后面三关的结果全部不可信。
连续 `MAX_GATE_ATTEMPTS` 轮仍不过 → `needs_human` + `failure_report()`。
"""

from .core import (
    BLACKLIST,
    GATE_ROOT,
    MAX_GATE_ATTEMPTS,
    GateError,
    GateVerdict,
    blacklist_hits,
    capture_baseline,
    default_commands,
    default_runner,
    failure_report,
    needs_human,
    parse_lint_count,
    parse_pytest_failures,
    patch_paths,
    run_gate,
)

__all__ = [
    "BLACKLIST",
    "GATE_ROOT",
    "MAX_GATE_ATTEMPTS",
    "GateError",
    "GateVerdict",
    "blacklist_hits",
    "capture_baseline",
    "default_commands",
    "default_runner",
    "failure_report",
    "needs_human",
    "parse_lint_count",
    "parse_pytest_failures",
    "patch_paths",
    "run_gate",
]
