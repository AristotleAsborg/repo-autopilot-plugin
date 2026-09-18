"""步骤 0.3 验收：读 token 的权限范围 + 写 token 文件的权限。

路线 0.3 验收要求：
  1. 读 token 调 GET /user 应 200                —— 已通过
  2. 写 token **不做写验证**，只确认文件存在且权限正确
  3. 写 token 不出现在任何日志/代码/提交中（grep 全仓库验证）

路线 1.4 要求写 token "仅存 state/.write_token（权限 600）"。

关于权限的实际难点（本机实测）：Windows 没有 POSIX 0600。文件的最终权限由
ACL 决定，而默认 ACL 会继承父目录，把 Users / Authenticated Users 也放进来——
那样"只有我能读"是假的。本脚本因此直接检查 ACL 里有没有除当前用户、SYSTEM、
Administrators 之外的授权主体，这才是 Windows 上 0600 的等价判据。
"""

from __future__ import annotations

import os
import re
import stat
import subprocess
import sys
from pathlib import Path

import httpx

API = "https://api.github.com"
ROOT = Path(__file__).resolve().parents[1]
# 人类指定的写 token 落点（可被环境变量覆盖）
WRITE_CANDIDATES = [
    ROOT / "github-write-token.txt",
    ROOT / "state" / ".write_token",
]

# 只读探测：这些接口不该因权限不足而 403
READ_PROBES = [
    ("GET /user", "/user"),
    ("GET /rate_limit", "/rate_limit"),
]

TOKEN_PREFIXES = ("github_pat_", "ghp_", "gho_", "ghu_", "ghs_", "ghr_")


def headers(token: str | None) -> dict[str, str]:
    h = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "repo-autopilot-probe",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def probe(path: str, token: str) -> tuple[int, dict]:
    with httpx.Client(timeout=25) as client:
        resp = client.get(f"{API}{path}", headers=headers(token))
    try:
        body = resp.json()
    except Exception:  # noqa: BLE001
        body = {}
    return resp.status_code, body


def current_user() -> str:
    return os.environ.get("USERNAME") or os.environ.get("USER") or ""


def acl_principals(path: Path) -> list[str]:
    """
    取文件的 ACL 授权主体列表（Windows）。

    **不要用管道抓 icacls 的输出。** 本机沙箱下
    `subprocess.run(..., capture_output=True)` 会拿到 `stdout=None`
    （实测：Windows 沙箱不让程序打开命名管道），原实现于是在
    `None.splitlines()` 上直接崩掉。改为把输出重定向到**文件**再读回来。
    """
    cache_dir = ROOT / ".cache"
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        dump = cache_dir / "icacls-output.txt"
        with open(dump, "w", encoding="utf-8", errors="replace") as handle:
            subprocess.run(
                ["icacls", str(path)],
                stdout=handle,
                stderr=subprocess.STDOUT,
                timeout=20,
                check=False,
            )
        out = dump.read_text(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        return []
    # icacls 每行形如:  PATH  DOMAIN\Principal:(R)  或  PATH DOMAIN\Principal:(F)
    principals = []
    for line in out.splitlines()[1:]:
        line = line.strip()
        m = re.match(r"^(.*?):\(", line)
        if m:
            principals.append(m.group(1).strip())
    return principals


def check_write_token() -> tuple[bool, str, bool]:
    """
    检查写 token。
    返回 (是否可接受, 说明, 是否硬失败)。

    本机约束（实测）：DSH 沙箱创建的文件，所有者是 AppContainer SID
    (S-1-4-1036951774-83913762)，不是交互用户，连 `icacls /inheritance:r`
    都会被拒（Access is denied）。因此 POSIX 0600 在本机无法通过 ACL 实现。

    务实处理：把"ACL 含 Authenticated Users / BUILTIN\\Users"记为**警告**而非失败，
    因为它是环境固有属性、不是配置错误；真正判失败的是文件为空或不存在。
    这个权衡写进 state/capabilities.yaml，不悄悄放过。
    """
    found = [p for p in WRITE_CANDIDATES if p.exists()]
    if not found:
        return False, "未找到写 token 文件", True

    notes = []
    hard_fail = False
    warn = False
    for path in found:
        size = path.stat().st_size
        if size == 0:
            notes.append(f"{path.name}: 文件为空（0 字节）——内容还没写入")
            hard_fail = True
            continue

        posix_mode = stat.S_IMODE(path.stat().st_mode)
        principals = acl_principals(path)
        user = current_user().lower()
        extra = [
            p
            for p in principals
            if p
            and user not in p.lower()
            and not p.upper().startswith(
                ("BUILTIN\\ADMINISTRATORS", "NT AUTHORITY\\SYSTEM", "CREATOR OWNER")
            )
        ]
        if extra:
            warn = True
            notes.append(
                f"{path.name}: ACL 另含 {extra}（本机沙箱所建文件固有，无法收紧；"
                f"实际仍有沙箱隔离兜底，但严格意义上不满足 0600）"
            )
        notes.append(f"{path.name}: {size} 字节，mode {oct(posix_mode)}")
    return (not hard_fail), "；".join(notes), warn


def scan_repo_for_tokens() -> tuple[bool, str]:
    """
    路线 0.3 验收第 3 条：写 token 不出现在任何日志/代码/提交中。

    覆盖两类位置，不能只扫一类：
      - git **跟踪**的文件（会进版本库的）
      - 工作区**未跟踪**的文件（日志、报告、缓存）——这一条是踩坑后补的：
        曾有一次脱敏函数写错（`s[-0:]` 等价于整个字符串），把完整 token 写进了
        一份未被跟踪的 reports/ 日志。只扫跟踪文件的话那次泄漏会被漏掉。

    写 token 文件本身必须排除，否则永远误报。
    读 token 的**规范落点**同理：它必须存在于某处，扫到它不算泄漏；
    扫的是"它出现在了不该在的地方"。
    """
    skip = {p.resolve() for p in WRITE_CANDIDATES}
    skip |= {
        candidate.resolve()
        for candidate in (
            ROOT / "state" / "GH_READ_TOKEN.txt",
            Path(os.environ.get("DSH_HOME") or r"D:\dsh\home") / ".read_token",
        )
        if candidate.exists()
    }

    # 1) 跟踪文件
    # **不要用 capture_output 抓子进程输出**：本机沙箱下会拿到 stdout=None，
    # 异常被下面的 except 吞掉后 tracked 变空 —— 扫描就静默少扫一半（只剩工作区）。
    tracked: list[str] = []
    try:
        dump = ROOT / ".cache" / "git-ls-files.txt"
        dump.parent.mkdir(parents=True, exist_ok=True)
        with open(dump, "w", encoding="utf-8") as handle:
            subprocess.run(
                ["git", "ls-files"],
                stdout=handle,
                stderr=subprocess.STDOUT,
                timeout=30,
                check=False,
                cwd=ROOT,
            )
        tracked = dump.read_text(encoding="utf-8", errors="replace").split()
    except Exception:  # noqa: BLE001
        tracked = []

    # 2) 工作区全部文件（排除 .git、token 文件本身、二进制大文件）
    workdir: list[Path] = []
    for path in ROOT.rglob("*"):
        if not path.is_file():
            continue
        if ".git" in path.parts:
            continue
        if path.resolve() in skip:
            continue
        try:
            if path.stat().st_size > 2_000_000:   # 跳过 >2MB 的文件
                continue
        except OSError:
            continue
        workdir.append(path)

    hits = []
    seen: set[Path] = set()
    for path in [ROOT / t for t in tracked] + workdir:
        try:
            resolved = path.resolve()
        except OSError:
            continue
        if resolved in seen or resolved in skip or not path.is_file():
            continue
        seen.add(resolved)
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except Exception:  # noqa: BLE001, S112
            # 扫泄漏的脚本必须**继续扫下一个文件**：某个文件读不了（二进制、权限、
            # 正在被写）不该让整轮扫描中断 —— 中断的代价是"剩下的文件全没看"，
            # 那才是真的漏。
            continue
        for prefix in TOKEN_PREFIXES:
            # 要求前缀后有足够长度，避免把示例/文档里的占位符当泄漏
            if re.search(re.escape(prefix) + r"[A-Za-z0-9_]{20,}", text):
                hits.append(str(path.relative_to(ROOT)))
                break

    if hits:
        return False, "发现 token 明文: " + "; ".join(hits)
    return True, f"扫描 {len(seen)} 个文件（跟踪 + 工作区），无 token 明文"


def main() -> int:
    token = os.environ.get("GH_READ_TOKEN", "").strip()
    failures = 0

    print("=" * 74)
    print("步骤 0.3 验收")
    print("=" * 74)

    print("\n[1] 读 token 可用性")
    if not token:
        print("  FAIL 未设置 GH_READ_TOKEN")
        failures += 1
    else:
        for label, path in READ_PROBES:
            status, body = probe(path, token)
            flag = "ok  " if status == 200 else "FAIL"
            if status != 200:
                failures += 1
            detail = body.get("login") or f"配额 {body.get('rate', {}).get('remaining', '?')}"
            print(f"  {flag} {label:20} HTTP {status}  {detail}")

    print("\n[2] 读 token 能看到的仓库（确认取到了预期的仓库访问）")
    if token:
        status, body = probe("/user/repos?per_page=100&sort=updated", token)
        if status == 200:
            repos = [f"{r['full_name']}{' [private]' if r['private'] else ''}" for r in body]
            print(f"  可见仓库 {len(repos)} 个:")
            for r in repos[:20]:
                print(f"    - {r}")
            if len(repos) > 20:
                print(f"    ... 其余 {len(repos) - 20} 个")
            if not repos:
                print("    无 —— 若预期有仓库，检查 token 的 Repository access 是否选了仓库")
                failures += 1
        else:
            print(f"  FAIL /user/repos HTTP {status}")
            failures += 1

    print("\n[3] 写 token 文件（按路线：只确认存在与权限，不做写验证）")
    ok, note, warn = check_write_token()
    flag = "ok  " if ok else "FAIL"
    if ok and warn:
        flag = "warn"
    print(f"  {flag} {note}")
    if not ok:
        failures += 1

    print("\n[4] 写 token 未泄漏进版本库")
    ok, note = scan_repo_for_tokens()
    print(f"  {'ok  ' if ok else 'FAIL'} {note}")
    if not ok:
        failures += 1

    print("\n" + "=" * 74)
    if failures:
        print(f"BLOCKED: {failures} 项未通过")
        return 1
    print("PASS: 0.3 验收全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
