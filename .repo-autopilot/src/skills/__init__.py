"""外墙（路线第七部分）：8 条对话指令的注册表、校验与路由。

    from src.skills import COMMANDS, check_consistency, route, load_all

- `registry.COMMANDS` —— **唯一真相**：指令清单、`skills/*.md`、`AGENTS.md` 都由它渲染；
- `check_consistency()` —— 三者一致性校验（路线 7.4，`/体检` 会调用它）；
- `route()` —— 自然语言兜底（"系统好像坏了" → `/应急`）；
- `doctor` —— `/应急` 的自检清单。
"""

from .doctor import (
    Check,
    doctor_report,
    environment_summary,
    overall,
    run_checks,
    state_dir_size,
    token_file_mode,
)
from .registry import (
    AGENTS_PATH,
    AUXILIARY_SKILLS,
    CLI_ENTRY,
    COMMANDS,
    EMERGENCY_HINTS,
    REQUIRED_SECTIONS,
    SKILLS_DIR,
    TEST_GATE_TEXT,
    Command,
    SkillError,
    as_json,
    check_consistency,
    commands_by_name,
    commands_by_slug,
    load_all,
    mentioned_commands,
    parse_skill,
    render_agents,
    render_skill,
    route,
    skill_path,
    slugs,
    validate_skill,
    write_all,
)

__all__ = [
    "AGENTS_PATH",
    "AUXILIARY_SKILLS",
    "CLI_ENTRY",
    "COMMANDS",
    "EMERGENCY_HINTS",
    "REQUIRED_SECTIONS",
    "SKILLS_DIR",
    "TEST_GATE_TEXT",
    "Check",
    "Command",
    "SkillError",
    "as_json",
    "check_consistency",
    "commands_by_name",
    "commands_by_slug",
    "doctor_report",
    "environment_summary",
    "load_all",
    "mentioned_commands",
    "overall",
    "parse_skill",
    "render_agents",
    "render_skill",
    "route",
    "run_checks",
    "skill_path",
    "slugs",
    "state_dir_size",
    "token_file_mode",
    "validate_skill",
    "write_all",
]
