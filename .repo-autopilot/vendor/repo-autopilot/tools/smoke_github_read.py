"""1.4 真实通路冒烟：用真读 token 打一次 GitHub。

为什么单独一个脚本：验收测试用的是 mock 后端，而"只会在 mock 上跑通"是经典失败模式。
这个脚本补上真通路那一步 —— 验证 token 解析、请求头、退避包装在真实 API 上确实能用。

**不打印 token**，只打印账号名、剩余配额与 token 来源。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.github import GitHubClient
from src.github.tokens import read_token_source


def main() -> int:
    client = GitHubClient(timeout=25)
    verify = client.get("/user")
    print("login        =", verify.get("login"))

    rate = client.rate_limit()
    print("quota remain =", rate["rate"]["remaining"])

    repos = client.get("/user/repos", params={"per_page": 10, "sort": "updated"})
    print("visible      =", len(repos), "个仓库")
    for repo in repos[:10]:
        print("   -", repo["full_name"], "[private]" if repo["private"] else "")

    print("token source =", read_token_source())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
