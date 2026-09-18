"""沙箱（路线 1.5）。

先读 `capabilities()` —— 它说的是这套沙箱**实际**拦得住什么。
别把"跑过沙箱"当成"安全"：本机是 `sandbox_strength: weak`，网络没有隔离。
"""

from .jobobject import Job, JobLimits
from .jobobject import available as jobobject_available
from .sandbox import (
    ENV_ALLOWLIST,
    IGNORED_DIRS,
    SANDBOX_ROOT,
    Limits,
    SandboxResult,
    apply_patch,
    capabilities,
    hash_tree,
    run,
)

__all__ = [
    "ENV_ALLOWLIST",
    "IGNORED_DIRS",
    "SANDBOX_ROOT",
    "Job",
    "JobLimits",
    "Limits",
    "SandboxResult",
    "apply_patch",
    "capabilities",
    "hash_tree",
    "jobobject_available",
    "run",
]
