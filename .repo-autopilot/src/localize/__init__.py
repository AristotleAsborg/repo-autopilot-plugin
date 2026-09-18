"""4.1 文件定位（`src/localize/`）。

    from src.localize import locate, to_json, walk_repo

三级漏斗：**词法/grep（免费）→ 本地小模型从路径清单初筛 → bge-m3 档案余弦精排**，
产出路线要求的"≤5 个候选文件清单（JSON）"，每个候选都带 `why`。

两条硬约束（路线原文）：

- **禁止把整仓库喂给 flash**：本模块从不调用 flash 档，也从不把文件正文发给任何模型 ——
  模型只看得到**路径清单**；
- **候选 ≤5 个**：超了直接报错，不悄悄多给。
"""

from .core import (
    DEFAULT_POOL,
    DEFAULT_TOP_K,
    SKIP_DIRS,
    TEXT_SUFFIXES,
    Candidate,
    FileEntry,
    LocalizeError,
    embedding_scores,
    extract_tokens,
    language_of,
    lexical_scores,
    locate,
    profile_text,
    read_text_safely,
    screen_paths,
    symbol_names,
    to_json,
    walk_repo,
)

__all__ = [
    "DEFAULT_POOL",
    "DEFAULT_TOP_K",
    "SKIP_DIRS",
    "TEXT_SUFFIXES",
    "Candidate",
    "FileEntry",
    "LocalizeError",
    "embedding_scores",
    "extract_tokens",
    "language_of",
    "lexical_scores",
    "locate",
    "profile_text",
    "read_text_safely",
    "screen_paths",
    "symbol_names",
    "to_json",
    "walk_repo",
]
