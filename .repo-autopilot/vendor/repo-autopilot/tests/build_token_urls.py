"""验证 GitHub 预填 token 创建页的 URL 参数格式。

来源：github/docs 官方文档 content/authentication/keeping-your-account-and-data-secure/
managing-your-personal-access-tokens.md 的 "Pre-filling token details using URL parameters" 一节。

官方原文（已核对）：
  - 路径: https://github.com/settings/personal-access-tokens/new
  - 参数: name / description / target_name / expires_in / <permission>=<level>
  - 权限取值: read / write / admin（write 含 read，admin 含 write）
  - 官方给出的示例（Update code and open a pull request）用的正是
    contents=write&pull_requests=write&workflows=write

本脚本做两件事：
  1. 打印本系统需要的两个 token 的完整 URL
  2. 用只读请求验证 URL 路径可达（不登录，因此预期是 302/200 而非 404）

不验证权限组合是否被 GitHub 接受——那需要登录态，只能由人类在浏览器里确认。
"""

from __future__ import annotations

import urllib.parse

import httpx

BASE = "https://github.com/settings/personal-access-tokens/new"


def build(name: str, description: str, perms: dict[str, str], expires_in: str = "none") -> str:
    query = {
        "name": name,
        "description": description,
        "expires_in": expires_in,
        **perms,
    }
    # safe=":" 让 contents:read 这类值保持可读；逗号与空格按 RFC 编码
    return f"{BASE}?{urllib.parse.urlencode(query, safe=':')}"


READ_PERMS = {
    "contents": "read",
    "issues": "read",
    "pull_requests": "read",
    "metadata": "read",
}

WRITE_PERMS = {
    "contents": "write",
    "issues": "write",
    "pull_requests": "write",
    "administration": "write",
    "metadata": "read",
}


def main() -> None:
    read_url = build("autopilot-read", "只读: 拉取 issue/PR/代码，用于分类与修复", READ_PERMS)
    write_url = build("autopilot-write", "只读+写: 推分支/开 PR，仅在人类闸门通过后使用", WRITE_PERMS)

    print("=" * 78)
    print("只读 token（这个发给我）")
    print("=" * 78)
    print(read_url)
    print()
    print("=" * 78)
    print("可写 token（这个不要发给我，自己保存）")
    print("=" * 78)
    print(write_url)
    print()

    # 只验证可达性：GitHub 对未登录用户会重定向到登录页，不该是 404
    print("URL 可达性检查（未登录，只看是否 404）:")
    with httpx.Client(timeout=20, follow_redirects=False) as client:
        resp = client.get(BASE, headers={"User-Agent": "repo-autopilot-probe"})
    status = resp.status_code
    verdict = "路径存在" if status in (200, 302, 301) else "路径可疑"
    print(f"  {BASE} -> HTTP {status}  {verdict}")


if __name__ == "__main__":
    main()
