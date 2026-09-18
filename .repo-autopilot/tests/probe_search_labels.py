"""诊断 2：用搜索 API 直接按标签取 issue，质量是否够？

上一轮结论：按仓库遍历 + 本地过滤，入选率极低（425 条中 2 条），
因为主流仓库大量 issue 无标签、且标签命名差异极大。

改用搜索 API 的好处：服务端就按标签过滤，拿回来的条目必然带该标签。
代价：search 限流 30/min，比 core 的 5000/hr 紧得多，必须省着用。

本步验证三件事：
  1. 带标签的搜索能否拿到足量结果
  2. 结果的标签组合是否单一（决定语料筛选规则）
  3. 真实标签命名分布（用于修正别名表）
"""

from __future__ import annotations

import collections
import os
import time

import httpx

API = "https://api.github.com"

# 用语言限定 + 星数下限，尽量挑标签纪律较好的活跃仓库
QUERIES = {
    "bug": 'label:bug state:closed type:issue language:Python stars:>200',
    "T: bug": 'label:"T: bug" state:closed type:issue language:Python',
    "kind/bug": 'label:"kind/bug" state:closed type:issue language:Python',
    "enhancement": 'label:enhancement state:closed type:issue language:Python stars:>200',
    "feature": 'label:feature state:closed type:issue language:Python stars:>200',
    "question": 'label:question state:closed type:issue language:Python stars:>200',
    "kind/question": 'label:"kind/question" state:closed type:issue language:Python',
}


def headers(token: str) -> dict[str, str]:
    return {
        "Accept": "application/vnd.github+json",
        "User-Agent": "repo-autopilot-probe",
        "X-GitHub-Api-Version": "2022-11-28",
        "Authorization": f"Bearer {token}",
    }


def main() -> None:
    token = os.environ.get("GH_READ_TOKEN", "").strip()
    with httpx.Client(timeout=40) as client:
        rate = client.get(f"{API}/rate_limit", headers=headers(token)).json()
        remaining = rate["resources"]["search"]["remaining"]
        print(f"search 剩余配额: {remaining}\n")

        for label, query in QUERIES.items():
            resp = client.get(
                f"{API}/search/issues",
                headers=headers(token),
                params={"q": query, "per_page": 20, "sort": "updated", "order": "desc"},
            )
            if resp.status_code != 200:
                print(f"{label:16} HTTP {resp.status_code}  {resp.text[:150]}")
                time.sleep(7)
                continue

            body = resp.json()
            items = body["items"]
            total = body["total_count"]

            # 统计标签组合复杂度
            combo_counter: collections.Counter[str] = collections.Counter()
            single = 0
            for it in items:
                names = tuple(sorted(l["name"] for l in it["labels"]))
                combo_counter[" + ".join(names) if names else "(无)"] += 1
                if len(names) == 1:
                    single += 1

            print(f"=== {label}  (total_count={total}) ===")
            print(f"  本页 {len(items)} 条，其中单标签 {single} 条")
            print("  最常见的标签组合:")
            for combo, cnt in combo_counter.most_common(5):
                print(f"    {cnt:>3}  {combo}")

            # 展示两条正文样本，判断是否可用
            usable = [it for it in items if 40 <= len(it.get("body") or "") <= 8000]
            print(f"  正文长度在 40~8000 之间的: {len(usable)}/{len(items)}")
            for it in usable[:2]:
                repo = it["repository_url"].rsplit("/", 2)[-2] + "/" + it["repository_url"].rsplit("/", 1)[-1]
                print(f"    [{repo}#{it['number']}] {it['title'][:60]}")
                print(f"      labels: {[l['name'] for l in it['labels']]}  body={len(it['body'])} 字")
            print()
            time.sleep(7)   # 30/min 限流，留足间隔


if __name__ == "__main__":
    main()
