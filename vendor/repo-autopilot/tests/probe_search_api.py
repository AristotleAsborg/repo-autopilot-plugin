"""探测 GitHub 搜索 API：能否用它抓取"已解决 + 已打标签"的 issue 作为语料。

设计要点（为什么这样做）：
  评估本地小模型的分类准确率，需要**不依赖我判断**的 ground truth。
  issue 上的 label 由维护者人工打，是独立于我的第三方判断——正好可用。
  因此只收「恰好命中一个目标标签」的 issue；多标签、无标签一律丢弃，
  避免把模糊样本混进评估集。

本步只做探测：确认搜索端点可用、限流多少、返回结构如何。
"""

from __future__ import annotations

import os

import httpx

API = "https://api.github.com"


def main() -> None:
    token = os.environ.get("GH_READ_TOKEN", "").strip()
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "repo-autopilot-probe",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    with httpx.Client(timeout=30) as client:
        print("=" * 74)
        print("搜索 API 能力探测")
        print("=" * 74)

        # 1) 限流：search 有独立的、低得多的配额
        for name, path in (("/rate_limit", "/rate_limit"),):
            r = client.get(f"{API}{path}", headers=headers).json()
            for key in ("core", "search", "code_search"):
                info = r.get("resources", {}).get(key)
                if info:
                    print(f"  {key:12} limit={info['limit']:>6}  remaining={info['remaining']:>6}")

        # 2) 一次真实搜索：闭合的、带 bug 标签的 issue
        query = 'label:bug state:closed type:issue'
        print(f"\n查询: {query!r}")
        resp = client.get(
            f"{API}/search/issues",
            headers=headers,
            params={"q": query, "per_page": 3, "sort": "updated", "order": "desc"},
        )
        print(f"  HTTP {resp.status_code}")
        if resp.status_code != 200:
            print(f"  错误: {resp.text[:300]}")
            return

        body = resp.json()
        print(f"  total_count = {body['total_count']}")
        print(f"  incomplete_results = {body['incomplete_results']}")
        print(f"\n  样本条目（前 {len(body['items'])} 条）:")
        for it in body["items"]:
            labels = [l["name"] for l in it["labels"]]
            print(f"\n    #{it['number']}  {it['title'][:66]}")
            print(f"      repo      : {it['repository_url'].rsplit('/', 2)[-2]}/{it['repository_url'].rsplit('/', 1)[-1]}")
            print(f"      labels    : {labels}")
            print(f"      state     : {it['state']}  reason={it.get('state_reason')}")
            print(f"      comments  : {it['comments']}")
            print(f"      body 长度 : {len(it.get('body') or '')}")
            print(f"      html_url  : {it['html_url']}")


if __name__ == "__main__":
    main()
