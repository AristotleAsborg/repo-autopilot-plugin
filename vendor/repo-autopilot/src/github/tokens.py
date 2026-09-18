"""读写 token 的取用与隔离（路线 0.3 第 2 条 + 1.4 第 4 条）。

## 两条硬规矩

1. **读 token**：可以注入所有进程。来源顺序
   环境变量 `GH_READ_TOKEN` → `state/GH_READ_TOKEN.txt` → `state/GH_READ_TOKEN`（见下）
   → `$DSH_HOME/.read_token`。
2. **写 token**：只存在 `state/.write_token` **一个**地方；只在**闸门通过之后**
   临时进入环境，用完立刻删。绝不出现在命令行、日志、报告、审批单里。

第 2 条里的"用完立刻删"不是洁癖：这个进程还要活很久，残留的环境变量会被
**每一个后续子进程**继承，包括我们完全不控制、也不该拿它去写仓库的那些。
"""

from __future__ import annotations

import os
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STATE_DIR = ROOT / "state"

READ_TOKEN_ENV = "GH_READ_TOKEN"
WRITE_TOKEN_ENV = "GH_WRITE_TOKEN"

# GitHub 的 token 前缀。用于脱敏与"有没有泄漏"的扫描。
TOKEN_PREFIXES = ("github_pat_", "ghp_", "gho_", "ghu_", "ghs_", "ghr_")


def dsh_home() -> Path:
    return Path(os.environ.get("DSH_HOME") or r"D:\dsh\home")


def read_token_candidates() -> tuple[Path, ...]:
    """
    读 token 的候选落点。**两种文件名都认**：`GH_READ_TOKEN.txt` 与 `GH_READ_TOKEN`。

    为什么加上不带 `.txt` 的那个（2026-09-12 实测）：人类把文件放进来时，很自然地用
    **环境变量的名字**当文件名（`GH_READ_TOKEN`），于是"token 明明放好了"却报
    `读 token 不可用：LookupError` —— 那时人只会怀疑 token 本身有问题，而其实只是后缀不同。
    **凭证这种东西的失败信息必须指向真原因**，所以宁可在候选里多一个名字。
    （两个名字都被 `.gitignore` 的 `*READ_TOKEN*` 覆盖，不会因为多认一个名字而泄漏。）
    """
    return (
        STATE_DIR / "GH_READ_TOKEN.txt",
        STATE_DIR / "GH_READ_TOKEN",
        dsh_home() / ".read_token",
    )


def read_token(explicit: str | None = None) -> str:
    """
    取读 token。取不到就抛 —— **绝不返回空串让调用方带着空凭证去打 API**
    （那会得到 401，然后被误判成"没有权限"，排查半天）。
    """
    if explicit:
        return explicit.strip()

    env_value = os.environ.get(READ_TOKEN_ENV, "").strip()
    if env_value:
        return env_value

    for candidate in read_token_candidates():
        if candidate.is_file():
            lines = candidate.read_text(encoding="utf-8", errors="replace").splitlines()
            if lines and lines[0].strip():
                return lines[0].strip()

    raise LookupError(
        f"读 token 不可用：环境变量 {READ_TOKEN_ENV} 未设，且 "
        f"{[str(c) for c in read_token_candidates()]} 都不存在或为空文件"
    )


def read_token_source(explicit: str | None = None) -> str:
    """只回报来源，绝不回报 token 本身。"""
    if explicit:
        return "调用方显式传入"
    if os.environ.get(READ_TOKEN_ENV, "").strip():
        return f"环境变量 {READ_TOKEN_ENV}"
    for candidate in read_token_candidates():
        if candidate.is_file():
            lines = candidate.read_text(encoding="utf-8", errors="replace").splitlines()
            if lines and lines[0].strip():
                return f"{candidate}（首行）"
    return "不可用"


def write_token_path(explicit: Path | None = None) -> Path:
    return explicit or (STATE_DIR / ".write_token")


def load_write_token(path: Path | None = None) -> str:
    target = write_token_path(path)
    if not target.is_file():
        raise LookupError(f"写 token 文件不存在：{target}")
    token = target.read_text(encoding="utf-8", errors="replace").strip()
    if not token:
        raise LookupError(f"写 token 文件为空：{target}")
    return token


@contextmanager
def write_token_scope(path: Path | None = None) -> Iterator[str]:
    """
    在 `with` 块内把写 token 放进环境变量，退出时**一定**删掉。

    为什么用 finally 而不是普通赋值：中途抛异常是最容易留下残留的路径，
    而残留恰恰是最危险的（后续所有子进程都继承）。
    """
    token = load_write_token(path)
    previous = os.environ.get(WRITE_TOKEN_ENV)
    os.environ[WRITE_TOKEN_ENV] = token
    try:
        yield token
    finally:
        if previous is None:
            os.environ.pop(WRITE_TOKEN_ENV, None)
        else:
            os.environ[WRITE_TOKEN_ENV] = previous


def redact(text: str, *secrets: str) -> str:
    """
    写日志/报告前先把秘密抹掉。

    实现上刻意**逐个替换已知秘密**，而不是"按前缀正则会替换"：
    本项目已经因为一个脱敏函数写错（`s[-0:]` 等于整个字符串）把完整 token
    写进过一份报告。宁可多调用一次，也不要再赌一次。
    """
    out = text
    for secret in secrets:
        if secret:
            out = out.replace(secret, "***REDACTED***")
    return out


def looks_like_token(text: str) -> bool:
    """粗判：文本里是否出现形如 GitHub token 的字符串。用于自检扫描。"""
    import re

    return any(
        re.search(re.escape(prefix) + r"[A-Za-z0-9_]{20,}", text) for prefix in TOKEN_PREFIXES
    )


def scan_for_leaks(paths: Sequence[Path], *, secrets: Sequence[str]) -> list[str]:
    """
    扫描给定文件里是否出现指定秘密。返回命中的文件相对路径列表。

    这是 1.4 验收第 3 条（"写 token 不出现在进程环境变量之外的任何地方"）
    的可执行版本：没有这个函数，那条红线就只能靠人肉看。
    """
    hits: list[str] = []
    for path in paths:
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for secret in secrets:
            if secret and secret in text:
                hits.append(str(path))
                break
    return hits
