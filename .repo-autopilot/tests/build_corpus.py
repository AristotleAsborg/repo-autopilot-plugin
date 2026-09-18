"""从 GitHub 抓取已关闭 issue 构成分类评估语料（搜索 API 版）。

## 为什么答案可信

评估本地小模型的分类准确率，答案不能由我给出——否则是我出题我判分。
本语料用 **issue 上的维护者标签**作为 gold，这是独立于我的第三方人工判断。

## 标签 → 三分类的映射规则

现实调查（见 state/reports/label-probe.log）显示标签命名差异极大：
  `bug` / `T: bug` / `kind/bug` / `bug V2` / `Feature Request` / `kind/question` ...
且大量 issue 挂 2~5 个标签。

因此规则是「按类别取，跨类别并存则丢弃」：
  1. 把标签归入三类之一（bug / feature / question），映射表见 TYPE_LABELS
  2. 若一条 issue 同时命中**两个及以上不同类别** → 丢弃（标签本身自相矛盾，
     例如同时挂 bug 与 enhancement，无法作为清晰答案）
  3. 同类多标签（如 `kind/bug` + `area/core`）不算冲突——`area/core` 不属于
     任何目标类别，直接忽略
  4. 恰好命中一个类别 → 该类别即为 gold

## 反"背题"的采样措施

搜索结果里混杂大量低质量小仓库，其标签可能随手乱打。因此：
  * 查询串限定 `language:Python` 且多数限定 `stars:>200`，优先取标签纪律好的仓库
  * 要求 `state_reason` 为 completed 或未标注
  * body 长度 80~8000 字符，且至少含 10 个字母/汉字（挡掉纯截图、纯日志、spam）
  * 不按仓库去重，但要记录来源分布，若某仓库占比过高会在 manifest 里标注

## 限流

search 端点 30 次/分钟。每次请求间 sleep 7s，并统计实际消耗。
"""

from __future__ import annotations

import collections
import json
import os
import re
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path

import httpx

API = "https://api.github.com"
ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "state" / "corpus"

# 标签 → 目标类别。键一律小写比对。只列**类型指示性**标签；
# 区域类（area/*、C: *）、状态类（status/*、pending）、优先级类（P1、Priority: *）
# 一律不入表，它们不表达 issue 类型。
TYPE_LABELS: dict[str, str] = {
    # ---- bug ----
    "bug": "bug",
    "t: bug": "bug",
    "kind/bug": "bug",
    "bug v2": "bug",
    "type: bug": "bug",
    "defect": "bug",
    # ---- feature ----
    "enhancement": "feature",
    "feature": "feature",
    "feature request": "feature",
    "kind/feature": "feature",
    "type: feature": "feature",
    "improvement": "feature",
    # ---- question ----
    "question": "question",
    "kind/question": "question",
    "type: question": "question",
    "question/not a bug": "question",
    "help wanted": "question",
}

QUERIES: list[tuple[str, str]] = [
    ("bug", 'label:bug state:closed type:issue language:Python stars:>200'),
    ("bug", 'label:"T: bug" state:closed type:issue language:Python'),
    ("bug", 'label:"kind/bug" state:closed type:issue language:Python'),
    ("bug", 'label:"bug V2" state:closed type:issue language:Python'),
    ("feature", 'label:enhancement state:closed type:issue language:Python stars:>200'),
    ("feature", 'label:"kind/feature" state:closed type:issue language:Python'),
    ("feature", 'label:"Feature Request" state:closed type:issue language:Python'),
    ("question", 'label:question state:closed type:issue language:Python stars:>200'),
    ("question", 'label:"kind/question" state:closed type:issue language:Python'),
    ("question", 'label:"Question/Not a bug" state:closed type:issue language:Python'),
]

MIN_BODY = 80
MAX_BODY = 8000
MIN_TEXT_CHARS = 10
PER_LABEL_TARGET = 40
MAX_PAGES = 2


@dataclass
class CorpusItem:
    repo: str
    number: int
    title: str
    body: str
    labels: list[str]
    gold: str
    gold_source: str
    html_url: str
    comments: int
    body_len: int
    language_hint: str


def headers(token: str) -> dict[str, str]:
    return {
        "Accept": "application/vnd.github+json",
        "User-Agent": "repo-autopilot-corpus",
        "X-GitHub-Api-Version": "2022-11-28",
        "Authorization": f"Bearer {token}",
    }


def language_hint(text: str) -> str:
    cjk = len(re.findall(r"[\u4e00-\u9fff]", text))
    latin = len(re.findall(r"[A-Za-z]", text))
    total = cjk + latin
    if total == 0:
        return "unknown"
    if cjk / total > 0.3:
        return "cjk-heavy"
    if latin / total > 0.9:
        return "latin-only"
    return "mixed"


def classify_labels(names: list[str]) -> tuple[str | None, list[str]]:
    """返回 (gold 或 None, 命中的类别集合)。跨类别并存返回 None。"""
    hits = {TYPE_LABELS[n.strip().lower()] for n in names if n.strip().lower() in TYPE_LABELS}
    if len(hits) == 1:
        return next(iter(hits)), sorted(hits)
    return None, sorted(hits)


def to_item(raw: dict) -> CorpusItem | None:
    labels = [l["name"] for l in raw.get("labels", [])]
    gold, _hits = classify_labels(labels)
    if gold is None:
        return None   # 无命中，或跨类别冲突

    if raw.get("state_reason") not in (None, "completed"):
        return None

    title = (raw.get("title") or "").strip()
    body = (raw.get("body") or "").strip()
    if not (MIN_BODY <= len(body) <= MAX_BODY):
        return None
    if len(re.findall(r"[A-Za-z\u4e00-\u9fff]", title + body)) < MIN_TEXT_CHARS:
        return None

    repo = raw["repository_url"].split("/repos/")[-1]
    # 记录命中的那个标签原文，便于人工追溯答案来源
    hit_label = next((l for l in labels if l.strip().lower() in TYPE_LABELS), "?")
    return CorpusItem(
        repo=repo,
        number=raw["number"],
        title=title,
        body=body,
        labels=labels,
        gold=gold,
        gold_source=f"maintainer label: {hit_label!r}",
        html_url=raw["html_url"],
        comments=raw.get("comments", 0),
        body_len=len(body),
        language_hint=language_hint(f"{title}\n{body}"),
    )


def main() -> int:
    token = os.environ.get("GH_READ_TOKEN", "").strip()
    if not token:
        print("FAIL 未设置 GH_READ_TOKEN")
        return 1

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    items: list[CorpusItem] = []
    seen_keys: set[tuple[str, int]] = set()
    query_stats: list[dict] = []
    requests_made = 0

    with httpx.Client(timeout=40) as client:
        for want, query in QUERIES:
            if sum(1 for i in items if i.gold == want) >= PER_LABEL_TARGET:
                continue
            for page in range(1, MAX_PAGES + 1):
                resp = client.get(
                    f"{API}/search/issues",
                    headers=headers(token),
                    params={"q": query, "per_page": 50, "page": page, "sort": "updated", "order": "desc"},
                )
                requests_made += 1
                if resp.status_code == 403:
                    print(f"  限流，暂停 65s（query={query[:40]}）")
                    time.sleep(65)
                    resp = client.get(
                        f"{API}/search/issues",
                        headers=headers(token),
                        params={"q": query, "per_page": 50, "page": page},
                    )
                    requests_made += 1
                if resp.status_code != 200:
                    print(f"  HTTP {resp.status_code}: {resp.text[:120]}")
                    break

                body = resp.json()
                kept = 0
                for raw in body["items"]:
                    item = to_item(raw)
                    if item is None:
                        continue
                    key = (item.repo, item.number)
                    if key in seen_keys:
                        continue
                    seen_keys.add(key)
                    items.append(item)
                    kept += 1

                query_stats.append(
                    {
                        "want": want,
                        "query": query,
                        "page": page,
                        "total_count": body["total_count"],
                        "page_size": len(body["items"]),
                        "kept": kept,
                    }
                )
                print(f"  [{want:8}] {query[:46]:48} page{page}  拉 {len(body['items']):>2} 收 {kept:>2}  （累计 {len(items)}）")
                time.sleep(7)   # search 30/min

    by_gold = collections.Counter(i.gold for i in items)
    by_repo = collections.Counter(i.repo for i in items)
    by_lang = collections.Counter(i.language_hint for i in items)

    print("\n分类分布:", dict(by_gold))
    print("语言分布:", dict(by_lang))
    print("仓库数:", len(by_repo))
    print("最大来源:", by_repo.most_common(3))

    corpus_path = OUT_DIR / "issues.jsonl"
    with corpus_path.open("w", encoding="utf-8") as fh:
        for it in items:
            fh.write(json.dumps(asdict(it), ensure_ascii=False) + "\n")

    manifest = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "source": "GitHub REST /search/issues",
        "ground_truth": "维护者标签；按类别取，跨类别并存则丢弃",
        "type_label_map": TYPE_LABELS,
        "queries": query_stats,
        "search_requests_used": requests_made,
        "total": len(items),
        "by_gold": dict(by_gold),
        "by_language": dict(by_lang),
        "distinct_repos": len(by_repo),
        "top_repos": by_repo.most_common(10),
        "filters": {
            "min_body": MIN_BODY,
            "max_body": MAX_BODY,
            "min_text_chars": MIN_TEXT_CHARS,
            "state_reason": "completed 或未标注",
            "cross_category_labels": "丢弃",
        },
    }
    (OUT_DIR / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print(f"\n写入 {corpus_path}（{len(items)} 条）")
    print(f"消耗 search 请求 {requests_made} 次")
    return 0 if len(items) >= 30 else 1


if __name__ == "__main__":
    sys.exit(main())
