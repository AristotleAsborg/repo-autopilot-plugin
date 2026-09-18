"""4.2 最小修复循环（`src/repair/`）。

    from src.repair import RepairLoop, make_patch, parse_model_output

- **增量补丁**：模型必须给出 `search`/`replace` 对；`search` 找不到就是行号漂移（绝不模糊匹配）；
  单文件改动超过一半 → 判为整文件重写并拒绝（路线明确禁止）;
- **每轮都在沙箱副本里验证**：`run_tests` 默认走 1.5 的沙箱（复制 → 打补丁 → 跑测试）；
- **不收敛 ≠ 失败**：15 轮没绿会把补丁与逐轮记录落进 `state/repair/{issue_id}.json`，
  下次可以从中间态接着跑。

另外重导出 `stub` 里的 `propose_patch`/`PatchProposal`：那是 **1.6 骨架 e2e 用的桩**，
不是本步的实现（真实现是 `RepairLoop`）。它必须继续可用，否则 1.6 的离线 e2e 会断。
"""

from .core import (
    CONTEXT_BUDGET,
    DEFAULT_MAX_ROUNDS,
    EDIT_SCHEMA,
    PATCH_DIR,
    REPAIR_DIR,
    REREAD_AFTER_MISSES,
    SYSTEM_PROMPT,
    WHOLE_FILE_RATIO,
    EditBlock,
    RepairError,
    RepairLoop,
    RepairResult,
    RoundRecord,
    apply_block,
    build_messages,
    default_chat,
    default_run_tests,
    is_whole_file_rewrite,
    make_patch,
    parse_edit_blocks,
    parse_model_output,
    render_report,
)

# 1.6 骨架的桩：e2e 依赖它，必须留着（见模块头注释）
from .stub import PatchProposal, propose_patch

__all__ = [
    "CONTEXT_BUDGET",
    "DEFAULT_MAX_ROUNDS",
    "EDIT_SCHEMA",
    "PATCH_DIR",
    "REPAIR_DIR",
    "REREAD_AFTER_MISSES",
    "SYSTEM_PROMPT",
    "WHOLE_FILE_RATIO",
    "EditBlock",
    "PatchProposal",
    "RepairError",
    "RepairLoop",
    "RepairResult",
    "RoundRecord",
    "apply_block",
    "build_messages",
    "default_chat",
    "default_run_tests",
    "is_whole_file_rewrite",
    "make_patch",
    "parse_edit_blocks",
    "parse_model_output",
    "propose_patch",
    "render_report",
]
