"""4.1 验收：**带已知答案的定位样本**上，候选清单命中率。

    python tools/eval_localize.py            # 真 embedding（Ollama bge-m3），约 1 分钟
    python tools/eval_localize.py --offline  # 只用词法信号（不调模型），秒级

## 关于验收标准

路线 4.1 只写了产物形态（"输出 ≤5 个候选文件清单"），**没写数量指标**。
本脚本按邻步的体例补一条可执行的线：**10 条样本里 ≥8 条把正确文件放进前 5**，
并把它写进 `ROADMAP.md` 的 4.1 验收 —— 一个步骤没有验收就等于没有终点。

## 样本是"真文件 + 合成 issue"

`tests/fixtures/sandbox-repos/` 里的仓库是真的（有真实的模块、符号名、测试名），
issue 文本是按这些真实符号**合成**的 —— 因为真 issue 没有"正确答案文件"这种标注。
受控样本测的是下界：真实 issue 往往更含糊，所以这条线才有意义。

样本刻意覆盖四种线索：**直接写出路径**、**写出符号名**、**只描述现象（无符号）**、
**提到测试名（要靠测试名→源文件的映射反推）**。哪一类掉分，下一轮就知道该补哪种信号。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.localize import locate

REPORT_DIR = ROOT / "state" / "reports"
FIXTURES = ROOT / "tests" / "fixtures" / "sandbox-repos"

HIT_FLOOR = 0.8     # 10 条里至少 8 条命中
TOP_K = 5

#: (仓库目录名, issue 文本, 期望文件, 线索类型)
SAMPLES: tuple[tuple[str, str, str, str], ...] = (
    (
        "sandbox-clean",
        "Money.parse('1,234.5') 直接抛异常：金额里带千分位就解析不了，去掉逗号才行",
        "ledger/money.py",
        "符号名",
    ),
    (
        "sandbox-clean",
        "Ledger.post 允许 debit 和 credit 传同一个账户名，结果账本自己跟自己平了",
        "ledger/store.py",
        "符号名",
    ),
    (
        "sandbox-clean",
        "to_csv 生成的报表里，memo 里含逗号时没有加引号，Excel 打开列全错位",
        "ledger/report.py",
        "符号名",
    ),
    (
        "sandbox-clean",
        "导出 CSV 的时候，备注里带逗号会把后面几列冲散，应该给字段加引号",
        "ledger/report.py",
        "只描述现象",
    ),
    (
        "sandbox-clean",
        "tests/test_cli.py::test_bad_amount_returns_two 现在返回的是 1，应该返回 2",
        "ledger/cli.py",
        "测试名映射",
    ),
    (
        "sandbox-clean",
        "读取账本文件时如果内容不是合法 JSON，抛出来的是 JSONDecodeError，没有包装成 LedgerError",
        "ledger/store.py",
        "只描述现象",
    ),
    (
        "sandbox-messy",
        "step_42 的 factor 默认值算出来是 41，比实际步号少一",
        "god_module.py",
        "符号名",
    ),
    (
        "sandbox-messy",
        "保存进度用的 state.json 写到了相对路径上，换个工作目录再启动就读不回来",
        "god_module.py",
        "只描述现象",
    ),
    (
        "sandbox-messy",
        "mixed_indent.compute 在 tab 与空格混用的文件里直接抛 TabError",
        "mixed_indent.py",
        "符号名",
    ),
    (
        "sandbox-messy",
        "legacy_has_key 在 key 不存在时返回 None，调用方按 False 判断就漏了",
        "legacy_py2_notes.py",
        "符号名",
    ),
)

#: 空 embedding：只留词法信号，用来回答"embedding 到底加了多少分"
def zeros_embed(texts: list[str]) -> np.ndarray:
    return np.zeros((len(texts), 4), dtype=float)


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

    parser = argparse.ArgumentParser(description="4.1 文件定位验收")
    parser.add_argument("--offline", action="store_true", help="只用词法信号，不调 embedding")
    parser.add_argument("--top-k", type=int, default=TOP_K)
    args = parser.parse_args()

    if not FIXTURES.is_dir():
        # 陪练仓库被 .gitignore 排除（由 tools/sandbox_repos.py 生成）。
        # 不报这一句的话，新克隆上跑出来会是"命中率 0/10"，看起来像定位器坏了。
        print(f"缺少陪练仓库：{FIXTURES}")
        print("先跑：python tools/sandbox_repos.py build")
        return 1

    embed_fn = zeros_embed if args.offline else None
    mode = "lexical_only" if args.offline else "lexical+embedding"
    print(f"模式：{mode}　候选上限 {args.top_k}")

    rows: list[dict] = []
    hits = 0
    for repo, issue, expected, clue in SAMPLES:
        candidates = locate(issue, FIXTURES / repo, top_k=args.top_k, embed_fn=embed_fn, use_model=False)
        paths = [item.path for item in candidates]
        hit = expected in paths
        hits += int(hit)
        rank = paths.index(expected) + 1 if hit else 0
        rows.append(
            {
                "repo": repo,
                "issue": issue,
                "expected": expected,
                "clue": clue,
                "hit": hit,
                "rank": rank,
                "candidates": [item.as_dict() for item in candidates],
            }
        )
        mark = f"HIT@{rank}" if hit else "MISS "
        print(f"  {mark:<7} [{clue}] 期望 {expected}")
        print(f"          实际 {'、'.join(paths) or '（空）'}")

    rate = hits / len(SAMPLES)
    passed = rate >= HIT_FLOOR

    if not args.offline:
        # 对照：同一批样本只用词法会怎样（把 embedding 的贡献量化出来）
        lexical_hits = 0
        for repo, issue, expected, _ in SAMPLES:
            paths = [item.path for item in locate(issue, FIXTURES / repo, top_k=args.top_k, embed_fn=zeros_embed, use_model=False)]
            lexical_hits += int(expected in paths)
        print(f"\n对照：只用词法信号 {lexical_hits}/{len(SAMPLES)}；加 embedding 后 {hits}/{len(SAMPLES)}")
    else:
        lexical_hits = hits

    print(f"\n命中率 {hits}/{len(SAMPLES)} = {rate:.0%}（要求 ≥{HIT_FLOOR:.0%}）")
    print(f"结论：{'达标' if passed else '**未达标**'}")

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    report = {
        "date": stamp,
        "mode": mode,
        "top_k": args.top_k,
        "hit_floor": HIT_FLOOR,
        "hits": hits,
        "samples": len(SAMPLES),
        "hit_rate": round(rate, 4),
        "lexical_only_hits": lexical_hits,
        "rows": rows,
        "passed": passed,
    }
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    target = REPORT_DIR / f"localize-eval-{stamp}-{mode}.json"
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"报告：{target}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
