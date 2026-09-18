"""步骤 0.2 验收②：embedding 维度与区分度。

路线 0.2 原文要求：
  "embedding 返回 1024 维向量，两条同义句余弦 >0.8，两条无关句 <0.5"

同义/无关对由我构造，但**判据是路线给的固定阈值**，不是我自己放宽的标准。
同时用真实语料测一次查重阈值（路线 2.2.1 的 0.92/0.98 是拿 BGE-M3 类模型调的），
看它在本机模型上是否依然成立——若否，后续 M2 的查重必须重新标定阈值。
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.gateway.local_client import embed

MODEL = "bge-m3:latest"

# 同义对：语义相同，措辞不同（含一组中英跨语言）
SYNONYM_PAIRS = [
    ("保存设置后重启就丢失", "重启之后配置又变回默认了"),
    ("程序启动时崩溃", "应用一打开就闪退"),
    ("上传大文件时超时", "传输大文件会连接中断"),
    ("how do I change the theme color", "changing the theme colour"),
    ("如何修改主题颜色", "怎样更换主题配色"),
]

# 无关对：主题完全不同
UNRELATED_PAIRS = [
    ("保存设置后重启就丢失", "数据库连接池耗尽导致服务不可用"),
    ("程序启动时崩溃", "希望增加一个深色模式开关"),
    ("上传大文件时超时", "文档里的安装步骤写得不够清楚"),
    ("how do I change the theme color", "the CI pipeline fails on windows"),
    ("如何修改主题颜色", "内存占用随时间持续增长"),
]


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def main() -> int:
    print("=" * 76)
    print("步骤 0.2 验收②  embedding 维度与区分度")
    print(f"  模型: {MODEL}")
    print("=" * 76)

    failures = 0

    # --- 维度 ---
    vecs = embed(["测试文本"], model=MODEL)
    dim = len(vecs[0])
    ok_dim = dim == 1024
    print("\n[1] 维度")
    print(f"  {'ok  ' if ok_dim else 'FAIL'} 返回 {dim} 维（要求 1024）")
    if not ok_dim:
        failures += 1

    norms = [math.sqrt(sum(x * x for x in v)) for v in vecs]
    print(f"  L2 范数: {norms[0]:.4f}（未归一化时不应为 1；本系统在调用侧归一化）")

    # --- 同义对 ---
    print("\n[2] 同义句余弦（要求 > 0.8）")
    syn_scores = []
    for a, b in SYNONYM_PAIRS:
        v = embed([a, b], model=MODEL)
        s = cosine(v[0], v[1])
        syn_scores.append(s)
        flag = "ok  " if s > 0.8 else "FAIL"
        if s <= 0.8:
            failures += 1
        print(f"  {flag} {s:.4f}  {a[:30]!r} vs {b[:30]!r}")

    # --- 无关对 ---
    print("\n[3] 无关句余弦（要求 < 0.5）")
    unrel_scores = []
    for a, b in UNRELATED_PAIRS:
        v = embed([a, b], model=MODEL)
        s = cosine(v[0], v[1])
        unrel_scores.append(s)
        flag = "ok  " if s < 0.5 else "FAIL"
        if s >= 0.5:
            failures += 1
        print(f"  {flag} {s:.4f}  {a[:30]!r} vs {b[:30]!r}")

    # --- 间隔 ---
    mean_syn = sum(syn_scores) / len(syn_scores)
    mean_unrel = sum(unrel_scores) / len(unrel_scores)
    gap = mean_syn - mean_unrel
    print("\n[4] 分离度")
    print(f"  同义均值 {mean_syn:.4f}   无关均值 {mean_unrel:.4f}   间隔 {gap:.4f}")
    print(f"  两类的最大/最小值: 同义最低 {min(syn_scores):.4f}，无关最高 {max(unrel_scores):.4f}")

    # --- 对真实语料的查重阈值检验 ---
    print("\n[5] 路线 2.2.1 的查重阈值（0.92 / 0.98）在本机模型上是否成立")
    corpus_path = ROOT / "state" / "corpus" / "issues.jsonl"
    if corpus_path.exists():
        items = [
            json.loads(line)
            for line in corpus_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ][:60]
        texts = [f"{i['title']}\n{i['body'][:600]}" for i in items]
        v = embed(texts, model=MODEL)
        above_92 = 0
        above_98 = 0
        pairs_above = []
        for i in range(len(v)):
            for j in range(i + 1, len(v)):
                s = cosine(v[i], v[j])
                if s > 0.98:
                    above_98 += 1
                    pairs_above.append((s, i, j))
                elif s > 0.92:
                    above_92 += 1
                    pairs_above.append((s, i, j))
        total_pairs = len(v) * (len(v) - 1) // 2
        print(f"  {len(v)} 条真实 issue，两两配对 {total_pairs} 对")
        print(f"  余弦 >0.98: {above_98} 对      >0.92: {above_92} 对")
        if pairs_above:
            pairs_above.sort(reverse=True)
            print("  最高的 3 对（看它们是否真的是同一问题）:")
            for s, i, j in pairs_above[:3]:
                print(f"    {s:.4f}")
                print(f"      A: {items[i]['title'][:64]}")
                print(f"      B: {items[j]['title'][:64]}")
        else:
            print("  没有任何一对超过 0.92 —— 说明这两个阈值在本语料上偏保守（几乎不会误合并），")
            print("  但也意味着真正重复的 issue 未必能被它们抓住。M2 上线前需要用正样本重标定。")
    else:
        print("  语料不存在，跳过")

    print("\n" + "=" * 76)
    if failures:
        print(f"FAIL: {failures} 项未达路线阈值")
        return 1
    print("PASS: embedding 维度与区分度全部达路线阈值")
    return 0


if __name__ == "__main__":
    sys.exit(main())
