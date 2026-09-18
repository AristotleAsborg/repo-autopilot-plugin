"""抽查语料质量：标签与内容是否真的一致。

语料的可信度全押在"维护者标签能代表 issue 类型"这个假设上。如果一条 issue
挂着 bug 却在问怎么用，那它作为 bug 的 gold 就是错的，整个准确率数字也跟着失真。

本脚本做三件事：
  1. 打印随机样本（含标签、正文摘要），供人工对照
  2. 统计容易被误标的信号：正文里出现"how do I / 怎么 / ?"等提问句式的，
     却挂在 bug 或 feature 下的比例——这些不是必然错，但需要人工确认
  3. 输出每类的样本量、正文长度分布，供后续判断是否需要再补语料
"""

from __future__ import annotations

import json
import random
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / "state" / "corpus" / "issues.jsonl"

# 提问式信号：出现在 bug/feature 类里值得人工复核
QUESTION_SIGNALS = [
    r"\bhow do i\b", r"\bhow to\b", r"\bis it possible\b", r"\bcan i\b",
    r"\bwhy does\b", r"\bwhat is\b", r"\bany idea\b", r"\bhelp\b",
    r"怎么", r"如何", r"为什么", r"是否", r"请问", r"\?$",
]


def load() -> list[dict]:
    return [json.loads(line) for line in CORPUS.read_text(encoding="utf-8").splitlines() if line.strip()]


def main() -> int:
    items = load()
    print(f"语料总数: {len(items)}\n")

    by_gold = Counter(i["gold"] for i in items)
    print("分类分布:", dict(by_gold))

    lens = sorted(i["body_len"] for i in items)
    print(f"正文长度: 最短 {lens[0]}  中位 {lens[len(lens)//2]}  最长 {lens[-1]}")

    langs = Counter(i["language_hint"] for i in items)
    print("语言分布:", dict(langs))

    repos = Counter(i["repo"] for i in items)
    print(f"来源仓库数: {len(repos)}   最多来源: {repos.most_common(3)}")

    # 提问式信号统计
    print("\n提问式信号（出现在 bug/feature 类中，需人工复核）:")
    for gold in ("bug", "feature", "question"):
        subset = [i for i in items if i["gold"] == gold]
        hits = []
        for i in subset:
            text = f"{i['title']}\n{i['body']}".lower()
            if any(re.search(p, text) for p in QUESTION_SIGNALS):
                hits.append(i)
        pct = len(hits) / len(subset) * 100 if subset else 0
        print(f"  {gold:9} {len(hits):>3}/{len(subset):>3}  ({pct:.0f}%)")

    # 随机抽查
    random.seed(20260911)
    print("\n" + "=" * 76)
    print("随机抽查 9 条（人工对照标签与内容）")
    print("=" * 76)
    for item in random.sample(items, 9):
        print(f"\n[{item['gold']}] {item['repo']}#{item['number']}")
        print(f"  答案来源: {item['gold_source']}")
        print(f"  全部标签: {item['labels']}")
        print(f"  标题    : {item['title'][:86]}")
        body = re.sub(r"\s+", " ", item["body"])[:220]
        print(f"  正文    : {body}…")
        print(f"  链接    : {item['html_url']}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
