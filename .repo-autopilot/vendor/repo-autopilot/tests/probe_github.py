"""步骤 0.3 前置探测：GitHub 连通性与读取权限（只读，不做任何写操作）。

路线 0.1 子步骤 1 要求：探测 GitHub 写能力——调用只读接口确认连通，
并翻工具清单确认是否存在写接口。本脚本只做只读部分。

路线 0.3 要求：
  - 读 token 进环境变量 GH_READ_TOKEN
  - 写 token 仅存 state/.write_token（权限 600）
  - 写 token **不做写验证**（避免无谓写操作），只确认文件存在与权限

因此本脚本绝不尝试任何写操作。
"""

from __future__ import annotations

import os
from pathlib import Path

import httpx

API = "https://api.github.com"
ROOT = Path(__file__).resolve().parents[1]
TOKEN_FILE = ROOT / "state" / ".write_token"


def get_token() -> tuple[str | None, str]:
    """按路线 0.3 的顺序取读 token：环境变量优先，其次只读探测 .write_token 是否存在。"""
    for name in ("GH_READ_TOKEN", "GITHUB_TOKEN", "GH_TOKEN"):
        value = os.environ.get(name)
        if value:
            return value.strip(), f"环境变量 {name}"
    return None, "未找到读 token（GH_READ_TOKEN / GITHUB_TOKEN / GH_TOKEN 均未设置）"


def probe(label: str, path: str, token: str | None) -> tuple[bool, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "repo-autopilot-probe",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    try:
        with httpx.Client(timeout=20) as client:
            resp = client.get(f"{API}{path}", headers=headers)
    except Exception as exc:  # noqa: BLE001
        return False, f"{exc.__class__.__name__}: {exc}"

    if resp.status_code == 200:
        return True, f"200 OK（剩余配额 {resp.headers.get('x-ratelimit-remaining', '?')}）"
    if resp.status_code == 401:
        return False, "401 未认证 —— token 无效或未提供"
    if resp.status_code == 403:
        return False, f"403 被拒（{resp.headers.get('x-ratelimit-remaining', '?')} 剩余配额）"
    return False, f"{resp.status_code} {resp.text[:120]}"


def main() -> None:
    print("=" * 72)
    print("GitHub 连通性与读取能力探测（全程只读，不做任何写操作）")
    print("=" * 72)

    token, source = get_token()
    print(f"读 token 来源: {source}")
    print(f"写 token 文件: {'存在' if TOKEN_FILE.exists() else '不存在'} ({TOKEN_FILE})")
    print()

    # 匿名与认证两种状态各测一次，区分“网络不通”与“token 缺失”
    print("--- 匿名（不带 token）---")
    ok, detail = probe("rate_limit", "/rate_limit", None)
    print(f"  GET /rate_limit        : {'ok  ' if ok else 'FAIL'} {detail}")

    print()
    print("--- 带 token（若已提供）---")
    for label, path in (
        ("GET /user", "/user"),
        ("GET /rate_limit", "/rate_limit"),
    ):
        ok, detail = probe(label, path, token)
        print(f"  {label:22} : {'ok  ' if ok else 'FAIL'} {detail}")

    print()
    print("--- 写 token 文件权限（路线 0.3 要求只确认存在与权限，不做写验证）---")
    if TOKEN_FILE.exists():
        st = TOKEN_FILE.stat()
        mode = oct(st.st_mode & 0o777)
        flag = "ok  " if mode == "0o600" else "注意"
        print(f"  {flag} 权限 {mode}（应为 0o600）")
    else:
        print("  未创建 —— 属阶段 0.3，需人类提供")

    print()
    print("=" * 72)
    print("结论汇总（供写入 capabilities.yaml）")
    print("=" * 72)
    anon_ok, _ = probe("anon", "/rate_limit", None)
    auth_ok, _ = probe("auth", "/user", token)
    print(f"  github_reachable : {anon_ok}")
    print(f"  github_read_auth : {auth_ok}")
    print("  github_write     : 未探测（按路线 0.3，写能力不在本步验证）")


if __name__ == "__main__":
    main()
