"""6.1 半自动建新仓库（`src/scaffold/`）。

    from src.scaffold import generate, dry_run, create_repository, first_push, wait_for_ci

一句 idea → 骨架（flash 生成 + **当场校验缺件**）→ 本地 CI 干跑（与云端同命令）→
三选一的名字 → 闸门批准 → 建仓 → 首推 → 轮询 CI。

两条纪律：

- **本地跑不过不许上传**：新仓库第一次 CI 就红会让后面所有"CI 绿"的说法失去意义；
- **人类只做两次决策**（选名 / 批准建仓），其余全自动（路线 0.5：运行期不该要人做配置）。
"""

from .core import (
    GENERATE_SCHEMA,
    MAX_FILES,
    MAX_TOTAL_BYTES,
    REQUIRED_PATTERNS,
    SYSTEM_PROMPT,
    GeneratedFile,
    Scaffold,
    ScaffoldError,
    build_prompt,
    check_names,
    choose_name,
    ci_commands,
    create_repository,
    default_chat,
    default_runner,
    dry_run,
    first_push,
    generate,
    generate_until_green,
    init_local_repository,
    is_name_available,
    latest_ci,
    name_candidate_notes,
    name_candidates,
    now_stamp,
    owner_exists,
    slugify,
    summary_block,
    validate_files,
    wait_for_ci,
    write_report,
    write_scaffold,
)

__all__ = [
    "GENERATE_SCHEMA",
    "MAX_FILES",
    "MAX_TOTAL_BYTES",
    "REQUIRED_PATTERNS",
    "SYSTEM_PROMPT",
    "GeneratedFile",
    "Scaffold",
    "ScaffoldError",
    "build_prompt",
    "check_names",
    "choose_name",
    "ci_commands",
    "create_repository",
    "default_chat",
    "default_runner",
    "dry_run",
    "first_push",
    "generate",
    "generate_until_green",
    "init_local_repository",
    "is_name_available",
    "latest_ci",
    "name_candidate_notes",
    "name_candidates",
    "now_stamp",
    "owner_exists",
    "slugify",
    "summary_block",
    "validate_files",
    "wait_for_ci",
    "write_report",
    "write_scaffold",
]
