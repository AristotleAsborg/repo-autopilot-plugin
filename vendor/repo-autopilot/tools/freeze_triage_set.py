"""把回放集的**正文**冻结到磁盘，让 3.1 的验收可重复、样本不会漂、开发集与验证集分得开。

    # 一、直接从 GitHub 搜索抓一批新的（推荐：正文来自搜索结果，一次请求一批）
    python tools/freeze_triage_set.py --search --per-query 50 \
        --query-extra "created:<2024-01-01" --cohort holdout

    # 二、把已有回放报告里的条目补进冻结集（正文按 repo#number 单独抓）
    python tools/freeze_triage_set.py --cohort dev state/reports/triage-eval-A.json [...]

输出：`state/corpus/triage-replay.jsonl`，每行
    {"repo", "number", "title", "body", "label", "state", "cohort", "frozen_at"}

## 为什么要冻

3.1 的验收是"回放已关闭 issue"。GitHub search 的结果**每次都在变**（有人关了新 issue、
仓库被归档、排序权重调整），所以同一个分类器今天 74.5%、明天可能 81.7% —— 那不是模型
在变，是考卷在变。用漂移的考卷对固定的分数线，等于每次都在掷硬币。

## 为什么分 cohort

`cohort=dev` 是调提示词时用过的；`cohort=holdout` 是**从未参与调参**的。
3.1 是否 PASS **只报 holdout 的数**，dev 只用来改。不分开，就会出现
"在整卷上调参、再用整卷报分"的自欺。

冻结集只增不减（路线 3.1 的原话："回放集会越攒越厚，它就是分类器的错题本"）：
按 `repo#number` 去重，已存在的不重新抓（省额度，也防止正文被作者事后编辑掉）。
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
# 查询口径只有一处定义（eval_triage.LABEL_QUERIES）：两处各写一份必然漂移
sys.path.insert(0, str(ROOT / "tools"))

from eval_triage import LABEL_QUERIES

REPLAY = ROOT / "state" / "corpus" / "triage-replay.jsonl"


def load_existing(path: Path) -> dict[str, dict]:
    if not path.exists():
        return {}
    rows: dict[str, dict] = {}
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            rows[f"{row['repo']}#{row['number']}"] = row
    return rows


def pop_flag(argv: list[str], name: str, default: str = "") -> str:
    if name in argv:
        index = argv.index(name)
        value = argv[index + 1] if index + 1 < len(argv) else default
        del argv[index : index + 2]
        return value
    return default


def from_search(per_query: int, query_extra: str, sort: str) -> list[dict]:
    """一次搜索请求就能拿到正文（搜索结果的 item 自带 body），不用 N+1 次抓取。"""
    from src.github import GitHubClient, read_token

    client = GitHubClient(read_token(), timeout=30)
    found: list[dict] = []
    for label, query in LABEL_QUERIES:
        full = f"{query} {query_extra}".strip()
        if sort:
            full += f" sort:{sort}"
        payload = client.search_issues(full, per_page=per_query)
        items = payload.get("items", [])
        print(f"  [{label}] {query_extra!r} -> {len(items)} 条")
        for item in items:
            found.append(
                {
                    "repo": item["repository_url"].split("/repos/")[-1],
                    "number": item["number"],
                    "title": item.get("title") or "",
                    "body": item.get("body") or "",
                    "label": label,
                    "state": "closed",
                }
            )
    return found


def from_reports(names: list[str]) -> list[dict]:
    from src.github import GitHubClient, read_token

    client = GitHubClient(read_token(), timeout=30)
    found: list[dict] = []
    for name in names:
        report = json.loads(Path(name).read_text(encoding="utf-8"))
        for row in report["rows"]:
            if "predicted" not in row:
                continue
            try:
                payload = client.issue(row["repo"], row["number"])
            except Exception as exc:  # noqa: BLE001
                # 抓不到就跳过 —— 冻结集里宁缺勿滥，不塞假正文
                print(f"  {row['repo']}#{row['number']} 抓取失败：{str(exc)[:60]}")
                continue
            found.append(
                {
                    "repo": row["repo"],
                    "number": row["number"],
                    "title": payload.get("title") or row.get("title") or "",
                    "body": payload.get("body") or "",
                    "label": row.get("truth") or "",
                    "state": "closed",
                }
            )
    return found


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

    argv = sys.argv[1:]
    cohort = pop_flag(argv, "--cohort", "dev") or "dev"
    per_query = int(pop_flag(argv, "--per-query", "50") or 50)
    query_extra = pop_flag(argv, "--query-extra")
    sort = pop_flag(argv, "--sort")
    use_search = "--search" in argv
    if use_search:
        argv.remove("--search")

    if not use_search and not argv:
        print(__doc__)
        return 2

    known = load_existing(REPLAY)
    candidates = from_search(per_query, query_extra, sort) if use_search else from_reports(argv)

    todo: list[dict] = []
    seen: set[str] = set()
    for row in candidates:
        key = f"{row['repo']}#{row['number']}"
        if key in known or key in seen:
            continue
        seen.add(key)
        todo.append(row)

    print(f"冻结集现有 {len(known)} 条；本批待写入 {len(todo)} 条（cohort={cohort}）")
    if not todo:
        print(f"无需变化：{REPLAY}")
        return 0

    # 记账时间戳用 UTC：这份 jsonl 是**追加式**的历史，混进本地时间就没法比较先后。
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    REPLAY.parent.mkdir(parents=True, exist_ok=True)
    with open(REPLAY, "a", encoding="utf-8") as handle:
        for row in todo:
            row["cohort"] = cohort
            row["frozen_at"] = stamp
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")

    counts: dict[str, int] = {}
    for row in load_existing(REPLAY).values():
        counts[row.get("cohort") or "dev"] = counts.get(row.get("cohort") or "dev", 0) + 1
    print(f"\n冻结集：{len(known)} → {len(known) + len(todo)} 条｜按 cohort 分布：{counts}")
    print(f"文件：{REPLAY}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
