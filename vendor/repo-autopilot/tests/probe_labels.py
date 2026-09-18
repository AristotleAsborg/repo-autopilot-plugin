"""诊断：主流 Python 仓库实际在用什么标签？

上一轮 build_corpus 从 425 条已关闭 issue 里只选出 2 条——说明我的别名表
和现实不符。不继续猜，先把这些仓库的标签分布统计出来。

同时统计"每条 issue 命中几个标签"，因为本语料的核心筛选规则是
「单一命中」；如果现实里 issue 普遍挂 2~3 个标签，那条规则就得重新设计。
"""

from __future__ import annotations

import collections
import os
import time

import httpx

API = "https://api.github.com"
REPOS = [
    "psf/requests",
    "pallets/flask",
    "pallets/click",
    "encode/httpx",
    "tiangolo/typer",
    "psf/black",
    "python-poetry/poetry",
    "pydantic/pydantic",
]


def headers(token: str) -> dict[str, str]:
    return {
        "Accept": "application/vnd.github+json",
        "User-Agent": "repo-autopilot-probe",
        "X-GitHub-Api-Version": "2022-11-28",
        "Authorization": f"Bearer {token}",
    }


def main() -> None:
    token = os.environ.get("GH_READ_TOKEN", "").strip()
    with httpx.Client(timeout=40, follow_redirects=True) as client:
        for repo in REPOS:
            resp = client.get(
                f"{API}/repos/{repo}/issues",
                headers=headers(token),
                params={"state": "closed", "per_page": 100, "sort": "updated"},
            )
            if resp.status_code != 200:
                print(f"{repo}: HTTP {resp.status_code}")
                continue
            batch = [x for x in resp.json() if "pull_request" not in x]

            label_counter: collections.Counter[str] = collections.Counter()
            per_issue_counts: collections.Counter[int] = collections.Counter()
            for it in batch:
                names = [l["name"] for l in it.get("labels", [])]
                per_issue_counts[len(names)] += 1
                for n in names:
                    label_counter[n] += 1

            print(f"\n=== {repo}  （{len(batch)} 条已关闭 issue）===")
            dist = ", ".join(f"{k}个标签:{v}条" for k, v in sorted(per_issue_counts.items()))
            print(f"  每条 issue 挂几个标签: {dist}")
            print("  出现最多的标签:")
            for name, count in label_counter.most_common(15):
                print(f"    {count:>3}  {name}")
            time.sleep(1.0)


if __name__ == "__main__":
    main()
