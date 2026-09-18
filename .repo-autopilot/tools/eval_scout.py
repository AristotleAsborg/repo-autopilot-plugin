"""5.1 验收：**5 个真实功能需求各返回 ≥3 个有效候选**，且 GPL 项目被拦。

    python tools/eval_scout.py               # 真搜索 + 真 flash 打分（约 1–2 分钟）
    python tools/eval_scout.py --no-score    # 只验搜索与硬过滤（不花模型调用）

## 验收线怎么定的

路线 5.1 的验收原文：「5 个功能需求各返回 ≥3 有效候选，人工抽查评分合理性；故意混入 GPL 项目，断言被拦」。

- **5 个需求是真的**（都来自本项目自己要解决的问题）：增量 diff 编辑、issue 去重、
  沙箱执行、issue 分类打标、人类审批闸门；
- **"有效候选" = 过了硬过滤（stars ≥50、未归档、两年内有 push）且许可证不是 block**；
- **评分合理性由人抽查**：报告里逐条给出 stars / 许可证 / 相关度 / usage / 模型理由，
  人只需要看一眼"这些候选像不像"；
- **GPL 用真实数据验**：拿 `git/git`（GPL-2.0）的真实 API 事实跑一遍，
  断言查表结果是 block、且**即使模型说 ok 也仍然是 block**（法律风险不交给模型）。
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.scout import (
    RELEVANCE_FLOOR,
    Candidate,
    RepoFacts,
    ScoutResult,
    facts_from_api,
    justify_low_star,
    license_risk,
    multi_source_scout,
    score_pool,
    score_repo,
)

REPORT_DIR = ROOT / "state" / "reports"
#: 冻结的候选池：同一批仓库、同一份顺序，核对时只变"排序+评分"这一层
CORPUS_DIR = ROOT / "state" / "scout-corpus"
MIN_VALID = 3
#: 每个需求最多深评几个候选 = **每路保底配额（`SOURCE_QUOTA`，共 12）+ 模型初筛的 4 个**。
#: 多路取材把候选池从 ~69 抬到 ~180+，而深评（取证据 + 一次模型调用）是最贵的一步，
#: 所以必须给"深评谁"定规矩：
#: - 只交给模型初筛不行 —— 实测它把 12 个名额全给了名字里带 `agent` 的大项目
#:   （`aaif-goose/goose`、`affaan-m/ECC`…），而**路线点名的参考实现 aider 明明在清单里
#:   却一个名额都没拿到**（名字里没有 "code/editing/agent" 任何一个词）；
#: - 只按配额也不行 —— 配额取的是"每条路各自最像的"，找不到我们想不到的东西。
#: 所以：**先保底，再让模型填空**。配额与初筛各管一半，谁也不能把对方挤掉。
SCORE_TOP = 16
#: 破格通道最多试几个"硬线之下"的候选。原来是 8，多路取材把候选池从 ~69 抬到 ~125–187
#: 之后，池子深处的**小众但对口**项目（正是破格通道存在的理由：路线自己点名的
#: PRism 2★ / issuesort 3★ 都在硬线下面）根本轮不到 —— 只看头 8 个等于把更深的池子扔掉一半。
#: 注意：**放宽的只是"看几个"，不是"什么算过关"** —— 书面理由的门槛（relevance ≥0.6）没动。
BROKEN_LINE_ATTEMPTS = 20


def scores_path(need: str) -> Path:
    """**金样本**：把模型对每个候选的打分也冻下来。

    为什么要冻：flash 即使温度 0 也不是逐位可复现，实测同样的候选池连跑两次，
    严格口径会在 3/5 与 2/5 之间摆动 —— 那样这条验收线测的是"模型心情"，
    不是"链路好不好"。冻掉打分之后，严格口径变成**确定性**的，
    也就才能拿它去回答"这条线到底该定成什么"。
    """
    return frozen_path(need).with_name(frozen_path(need).stem + "-scores.json")


def save_scores(need: str, result, justified: list | None = None) -> Path:
    target = scores_path(need)
    target.write_text(
        json.dumps(
            {
                "need": need,
                "scores": {
                    item.facts.full_name: {
                        "score": item.score,
                        "evidence_chars": item.evidence_chars,
                    }
                    for item in result.candidates
                    if item.score
                },
                # 破格结论（stars 不足但模型给了书面理由）也冻起来：它同样是模型判断
                "justified": [
                    {"facts": item.facts.__dict__, "score": item.score, "notes": item.notes}
                    for item in (justified or [])
                ],
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return target


def load_justified(need: str) -> list[Candidate] | None:
    """
    读回冻结的破格结论。**None 表示"没有冻过"**，空表表示"冻过，当时一个都没有"。

    这个区分很重要：`[]`（冻过且为空）必须当成**权威结论**，否则每次核对又会去问一遍模型，
    冻结核对就白冻了（模型不是逐位可复现的）。
    """
    target = scores_path(need)
    if not target.is_file():
        return None
    payload = json.loads(target.read_text(encoding="utf-8"))
    if "justified" not in payload:
        return None
    known = {field.name for field in dataclasses.fields(RepoFacts)}
    candidates: list[Candidate] = []
    for entry in payload.get("justified") or []:
        facts = RepoFacts(**{key: value for key, value in (entry.get("facts") or {}).items() if key in known})
        if entry.get("score"):
            candidates.append(
                Candidate(facts=facts, score=entry["score"], notes=list(entry.get("notes") or ["破格（冻结结论）"]))
            )
    return candidates


def apply_frozen_scores(need: str, result) -> int:
    """把冻下的打分贴回候选。返回贴上了几条（0 表示没有金样本）。"""
    target = scores_path(need)
    if not target.is_file():
        return 0
    payload = json.loads(target.read_text(encoding="utf-8"))
    scores = payload.get("scores") or {}
    applied = 0
    for item in result.candidates:
        blob = scores.get(item.facts.full_name)
        if blob and blob.get("score"):
            item.score = blob["score"]
            item.evidence_chars = int(blob.get("evidence_chars") or 0)
            applied += 1
    return applied


def frozen_path(need: str) -> Path:
    import hashlib

    digest = hashlib.sha1(need.encode("utf-8")).hexdigest()[:8]
    ascii_words = "-".join(re.findall(r"[A-Za-z]{3,}", need))[:40] or "need"
    return CORPUS_DIR / f"{ascii_words}-{digest}.json"


def save_frozen(need: str, result, sourcing: dict | None = None) -> Path:
    CORPUS_DIR.mkdir(parents=True, exist_ok=True)
    target = frozen_path(need)
    target.write_text(
        json.dumps(
            {
                "need": need,
                "query": result.query,
                "candidates": [
                    {**item.facts.__dict__, "notes": item.notes} for item in result.candidates
                ],
                "below_bar": [facts.__dict__ for facts in result.below_bar],
                "skipped": result.skipped,
                "searched": result.searched,
                # **取材来源也要冻**：否则事后审报告时看不出"这个对口项目是关键词带来的
                # 还是策展清单带来的"，也就无法回答"改取材方式到底有没有用"。
                "sourcing": sourcing,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return target


def load_frozen(need: str):
    """读回冻结的候选池（没有就返回 None，让调用方如实报"缺池"）。"""
    target = frozen_path(need)
    if not target.is_file():
        return None
    payload = json.loads(target.read_text(encoding="utf-8"))
    result = ScoutResult(need=need, query=payload.get("query", ""), searched=int(payload.get("searched") or 0), candidates=[])
    # 只取 dataclass 认得的字段：冻结池可能是**加字段之前**冻的，
    # 那时还没有 `language`；用 `RepoFacts(**item)` 会直接 TypeError（缺字段有默认值，多余的会炸）。
    known = {field.name for field in dataclasses.fields(RepoFacts)}
    result.candidates = [
        Candidate(
            facts=RepoFacts(**{key: value for key, value in item.items() if key in known}),
            # **来源也要读回来**：冻掉的池子必须和当时那一池子一样，否则冻结核对测的
            # 是另一条链路 —— 实测丢了来源之后，初筛的"每路轮流取一个"退化成单组
            # 按星数排，策展那一路整个被挤出初筛清单（2026-09-12 踩过）。
            notes=list(item.get("notes") or []),
        )
        for item in payload.get("candidates") or []
    ]
    result.below_bar = [
        RepoFacts(**{key: value for key, value in item.items() if key in known})
        for item in payload.get("below_bar") or []
    ]
    result.skipped = list(payload.get("skipped") or [])
    # 取材来源随池子一起冻着读回来：报告里要能原样复现"谁带来的候选"
    result.sourcing = payload.get("sourcing")
    return result

#: (需求描述, 搜索关键词, 路线点名的种子项目)。关键词用**英文、短**：
#: GitHub 的仓库搜索是 AND 语义，词一多召回就掉到个位数（实测：五个词的长句只回来 5 条）。
#:
#: 种子项目的用处：路线 3.2 明文点名 PRism 与 issuesort 作为查重的参考实现，
#: 而它们"星数均低"—— 纯关键词搜索 + stars≥50 会把它们全挡掉。
#: 把路线点名的项目当种子送进同一条流水线（同样过许可证红线与"必须看代码"），
#: 是忠实于路线文档的做法，也让"低星但适用"的项目有机会靠**书面理由**进来。
NEEDS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    # 需求 1 在 2026-09-12 由人类拍板**改写**：原来的写法是「LLM 增量 diff 编辑（像 aider
    # 的 edit block：只改几行、不许整文件重写）」—— 那是一条**能力级**需求，187 个候选里
    # 模型只判 2 个相关（continue 0.75、aider 0.6），凑不出验收要的 3 个，而且不是取材或
    # 预算的问题（深评预算从 16 提到 26 仍是 2 个）。改写成**项目级**需求之后，
    # "谁在解决这个问题"变成可枚举的：patch-package / comby / ast-grep / difftastic 等
    # 都是"把 diff 安全地应用到文件"的项目。**口径没放松**（仍然是模型判定相关，
    # 只是相关度下限 2026-09-12 由人类拍板从 0.5 调到 0.4 —— 理由见 findings）。
    ("把 LLM 输出的 diff 安全地应用到文件：校验、冲突处理、失败可回滚（不许整文件重写）",
     "apply patch", ("Aider-AI/aider",)),
    ("issue 去重 / 语义相似度检索（embedding + 余弦）",
     "duplicate issue detection", ("danhdox/PRism", "Sev7nOfNine/issuesort")),
    ("在沙箱里执行不可信代码并跑测试（进程/文件系统隔离）",
     "untrusted code execution", ()),
    ("GitHub issue 自动分类打标（bug/feature/question）",
     "issue triage bot", ()),
    ("危险操作的人类审批闸门（写操作前必须有人点头）",
     "human in the loop approval", ()),
)

#: 故意混入的 GPL 项目。GitHub 的许可证识别并不总准（`git/git` 就报 NOASSERTION），
#: 所以按顺序试，取第一个**识别成强 copyleft** 的；再单独验"识别不出来"的情形。
GPL_PROBES: tuple[str, ...] = (
    "torvalds/linux",
    "moodle/moodle",
    "WordPress/WordPress",
    "discourse/discourse",
    "git/git",
)


class CachingClient:
    """
    给真客户端套一层**缓存**，把同一轮里重复的往返省掉。

    为什么要套：多路取材会让同一个仓库被反复查到 —— 路线点名的种子、模型点名的知名项目、
    几份清单里的共同条目，在 5 个需求之间高度重叠（aider、cline、continue 每个需求都可能出现），
    取证时同一份 `contents/` 也会被反复取。一次运行只有几分钟，**同一个路径在同一轮里
    内容不会变**，重复取一遍只是白花往返时间（而 10 分钟上限是硬约束）。

    只缓存两类：`/repos/owner/name`（仓库事实）与 `/repos/owner/name/contents/...`（文件内容）。
    **搜索接口一律不缓存** —— 搜索结果是"那一刻的搜索结果"，缓存它会悄悄改变验收所测的东西。
    """

    FACT_PATH = re.compile(r"^/repos/[^/]+/[^/]+$")
    CONTENT_PATH = re.compile(r"^/repos/[^/]+/[^/]+/(contents|readme)(/.*)?$")

    def __init__(self, inner) -> None:
        self.inner = inner
        self.cache: dict[tuple[str, str], Any] = {}
        self.hits = 0

    def get(self, path: str, params: dict | None = None):
        cacheable = (params is None and self.FACT_PATH.match(path)) or self.CONTENT_PATH.match(path)
        if not cacheable:
            return self.inner.get(path, params=params) if params is not None else self.inner.get(path)
        key = (path, json.dumps(params or {}, sort_keys=True))
        if key not in self.cache:
            self.cache[key] = self.inner.get(path, params=params) if params is not None else self.inner.get(path)
        else:
            self.hits += 1
        return self.cache[key]


def fake_optimistic_chat(messages, schema):
    """一个"什么都好"的模型：用来证明许可证不是它说了算。"""
    return {"relevance": 1.0, "usage": "copy", "license_risk": "ok", "reason": "看起来很好用"}


def describe_sourcing(sourcing: dict) -> str:
    """
    一行说清"候选是谁带来的"。

    这一行是回答"改取材方式到底有没有用"的唯一读数：如果达标候选全来自关键词那一路，
    那多取材就是白干；如果对口项目是策展清单/已知项目带进来的，那就说明方向 C 起了作用。
    """
    def names(key: str, limit: int = 3) -> str:
        values = sourcing.get(key) or []
        return "、".join(values[:limit]) if values else "—"

    return (
        f"取材：关键词 {sourcing.get('keyword', 0)} 个"
        f"｜主题 {sourcing.get('topic', 0)} 个（{names('topics_used')}）"
        f"｜策展清单 {sourcing.get('curated', 0)} 个（{names('lists_used')}）"
        f"｜已知项目 {sourcing.get('known', 0)} 个（{names('known_used')}）"
        f"｜种子 {sourcing.get('seed', 0)} 个"
        f" → 关键词之外共 {sourcing.get('extra', 0)} 个"
    )


def broaden_queries(query: str) -> list[str]:
    """
    先用原查询；有效候选不够就把关键词缩短一次。

    GitHub 的仓库搜索是 **AND** 语义：词越多召回越少（实测"duplicate issue detection"
    回来的全是八百天没动的玩具仓库）。只放宽**一次**，两次都记进报告 ——
    放宽是为了找到候选，不是为了把数字凑够；放宽后仍不够就是真不够。
    """
    words = query.split()
    queries = [query]
    if len(words) > 2:
        queries.append(" ".join(words[:2]))
    return queries


def run_smoke(client, need: str) -> bool:
    """
    **活体取材冒烟**（一次真实搜索，几十秒）：证明"改取材方式"这条路真的通着。

    冻结核对（`--from-frozen`）是确定性的，但它只会证明"当时那一池子候选评出来几分"；
    如果某天主题/清单/已知项目三条来源**悄悄断了**（GitHub 换了路径、模型不再给出主题、
    清单 README 解析失效），冻结核对仍然会绿。所以验收里必须有一条**真的去搜一次**的断言：

    - 四条来源**每条都要带回来候选**（关键词、主题、策展清单、已知项目）；
    - 深评真的取到了证据（`evidence_chars > 0`），而不是"取不到就静默跳过"。

    这条不判"相关度够不够"（那是冻结核对的事），只判**取材四路都还活着**。
    """
    result, report = multi_source_scout(
        need,
        client=client,
        queries=broaden_queries("code editing agent"),
        seeds=[],
        max_rounds=1,
        queries_per_round=1,
        per_page=20,
        score_per_round=2,
        min_valid=2,
    )
    print("取材冒烟：")
    print("  " + describe_sourcing(report.as_dict()))
    scored = [item for item in result.candidates if item.score]
    evidenced = [item for item in scored if item.evidence_chars > 0]
    print(f"  搜到 {result.searched} 条 → 候选 {len(result.candidates)} 个、硬线下 {len(result.below_bar)} 个；"
          f"深评 {len(scored)} 个，其中取到证据 {len(evidenced)} 个")
    checks = {
        "关键词带回候选": report.keyword > 0,
        "主题带回候选": report.topic > 0,
        "策展清单带回候选": report.curated > 0,
        "已知项目带回候选": report.known > 0,
        "深评取到了证据": len(evidenced) > 0,
    }
    for label, ok in checks.items():
        print(f"  [{'OK' if ok else 'FAIL'}] {label}")
    return all(checks.values())


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

    parser = argparse.ArgumentParser(description="5.1 猎手验收")
    parser.add_argument("--no-score", action="store_true", help="不调 flash，只验搜索与硬过滤")
    parser.add_argument("--freeze", action="store_true", help="只搜索并把候选池冻到 state/scout-corpus/")
    parser.add_argument("--from-frozen", action="store_true", help="用冻结的候选池评分（不搜索，可重复）")
    parser.add_argument("--freeze-scores", action="store_true", help="给这一轮的候选池打分并**冻下打分**")
    parser.add_argument("--rescore", action="store_true", help="忽略金样本，重新调模型打分")
    parser.add_argument(
        "--only",
        default="",
        help="只跑某个需求（序号或需求里的片段）——**分块跑**用，避开 pwsh 10 分钟上限；部分运行不构成验收",
    )
    parser.add_argument("--list", action="store_true", help="列出 5 个需求与序号")
    parser.add_argument("--smoke", action="store_true", help="只做一次活体取材冒烟（四条来源都要带回候选）")
    parser.add_argument(
        "--score-top",
        type=int,
        default=SCORE_TOP,
        help=f"每个需求最多深评几个候选（默认 {SCORE_TOP}：每路保底配额 + 模型初筛填空）",
    )
    args = parser.parse_args()

    if args.list:
        for index, (need, query, _seeds) in enumerate(NEEDS, start=1):
            print(f"{index}. {need}（关键词：{query}）")
        return 0

    selected = NEEDS
    partial = False
    if args.only:
        partial = True
        if args.only.isdigit():
            picked = [NEEDS[int(args.only) - 1]] if 1 <= int(args.only) <= len(NEEDS) else []
        else:
            picked = [entry for entry in NEEDS if args.only in entry[0]]
        if not picked:
            print(f"**没有匹配 `--only {args.only}` 的需求**（用 --list 看序号）")
            return 1
        selected = picked
        print(f"**部分运行：只跑 {len(selected)}/{len(NEEDS)} 个需求 —— 这不构成 5.1 验收**\n")

    from src.github import GitHubClient, read_token

    client = CachingClient(GitHubClient(read_token(), timeout=60))

    if args.smoke:
        ok = run_smoke(client, NEEDS[0][0])
        print(f"\n取材冒烟：{'通过' if ok else '**未通过**'}")
        return 0 if ok else 1
    results: list[dict] = []
    failures: list[str] = []
    passed_needs = 0

    for need, query, seeds in selected:
        sourcing: dict | None = None
        if args.from_frozen:
            # **冻结核对**：用同一次搜索的候选池评分。这样每次跑只变"排序+评分"这一层，
            # 指标才可比 —— 否则"搜索随机 + 评分随机"两层噪声叠起来，同一份代码能在
            # 4/5 与 2/5 之间摆动（实测踩过，和 3.1/3.3 是同一类问题）。
            result = load_frozen(need)
            if result is None:
                print(f"FAIL {need[:34]:<36} 没有冻结候选池 —— 先跑 `--freeze`")
                failures.append(f"{need[:20]} 缺少冻结池")
                continue
            reused = 0 if args.rescore else apply_frozen_scores(need, result)
            sourcing = getattr(result, "sourcing", None)
            if reused:
                print(f"     （复用金样本打分 {reused} 条，不调模型）")
            else:
                score_pool(need, result, client=client, score_top=0 if args.no_score else args.score_top)
        else:
            # **多路取材**（人类 2026-09-12 定的方向 C）：关键词搜索只算一路 ——
            # 另外三路是 topic 搜索、人工策展清单（awesome-*）、已知项目反查。
            # 三条新来源都只产出候选**名字**，下游关口（硬过滤 → 看代码打分 → 许可证查表）
            # 一个都没放宽：换的是"谁进候选池"，不是"谁能通过"。
            result, report = multi_source_scout(
                need,
                client=client,
                queries=broaden_queries(query),
                seeds=list(seeds),
                max_rounds=2,
                queries_per_round=2,
                per_page=30,          # 每个关键词取更深的池子，再靠便宜的排序挑人深评
                score_per_round=0 if (args.no_score or args.freeze) else args.score_top,
                min_valid=MIN_VALID,
            )
            sourcing = report.as_dict()
        if args.freeze:
            path = save_frozen(need, result, sourcing)
            print(f"OK   {need[:34]:<36} 候选池已冻结：{path.name}（{len(result.candidates)} 个候选，"
                  f"{len(result.below_bar)} 个在硬线下）")
            if sourcing:
                print("     " + describe_sourcing(sourcing))
            continue
        if args.no_score:
            valid = [
                candidate
                for candidate in result.candidates
                if license_risk(candidate.facts.license_spdx) != "block"
            ]
        else:
            # **更严的口径（2026-09-12 按人类要求改）**：「有效候选」= 模型**判定相关**
            # （relevance ≥ RELEVANCE_FLOOR）且 usage≠skip 且许可证不是 block。
            # 之前用的是"过硬过滤且许可证不是 block"（只要大且不是 GPL 就算数）——
            # 那个口径下 20 多个候选里可能一个都不对口，通过线等于没在管质量。
            valid = [
                candidate
                for candidate in result.candidates
                if candidate.score
                and float(candidate.score.get("relevance") or 0.0) >= RELEVANCE_FLOOR
                and candidate.score.get("usage") != "skip"
                and candidate.score.get("license_risk") in ("ok", "warn")
            ]
        good = list(valid)
        ok = len(valid) >= MIN_VALID
        justified: list = []
        frozen_justified: list | None = None
        if not ok and not args.no_score:
            # 路线 5.1 的破格通道：stars<50 **或**两年没 push —— 默认 skip，除非 flash 给出书面理由。
            # 需要它是因为：真正对口的小众项目（包括路线自己点名的 PRism 2★ / issuesort 3★）
            # 正好都在硬线下面。候选池深了之后（per_page 30）这儿也要给够机会 ——
            # 只试 4 个的话，等于把"更深候选池"这个改进又扔掉一半。
            #
            # 破格结论也**一起冻**：它是模型判断，同样不是逐位可复现的。
            # 不冻的话，冻结核对会在"3 个够"与"2 个不够"之间摆动 —— 那就还是没解决问题。
            if args.from_frozen and not args.rescore:
                frozen_justified = load_justified(need)
                if frozen_justified is not None:
                    justified = frozen_justified
                    print(f"        （复用冻结的破格结论 {len(justified)} 个，不调模型）")
            if not justified and (not args.from_frozen or args.rescore or frozen_justified is None):
                for facts in result.below_bar[:BROKEN_LINE_ATTEMPTS]:
                    candidate = justify_low_star(need, facts, client=client)
                    if candidate is not None:
                        justified.append(candidate)
            valid = valid + justified
            ok = len(valid) >= MIN_VALID
            if justified:
                print(f"        破格纳入 {len(justified)} 个（stars 不足但模型给了书面理由）")
        if args.freeze_scores and not args.no_score:
            path = save_scores(need, result, justified)
            print(f"        （打分与破格结论已冻结：{path.name}）")
        passed_needs += int(ok)
        print(
            f"{'OK  ' if ok else 'FAIL'} {need[:34]:<36} 搜索 {result.searched} 条 → "
            f"有效 {len(valid)} 个；模型判定相关（≥{RELEVANCE_FLOOR}）{len(good)} 个"
        )
        if sourcing:
            print("        " + describe_sourcing(sourcing))
        for candidate in valid[:4]:
            score = candidate.score or {}
            mark = "破格" if candidate.notes and "破格" in candidate.notes[0] else "    "
            print(
                f"        [{mark}] {candidate.facts.full_name:<36} {candidate.facts.stars:>6}★ "
                f"{(candidate.facts.license_spdx or '未声明'):<14} "
                f"相关度 {score.get('relevance', '—')} {score.get('usage', '')}"
            )
        if result.skipped:
            print(f"        跳过 {len(result.skipped)} 个（例：{result.skipped[0]['full_name']}：{result.skipped[0]['reason']}）")
        results.append(
            {
                "need": need,
                "query": query,
                "sourcing": sourcing,
                "ok": ok,
                "valid": len(valid),
                "good": len(good),
                "justified": len(justified),
                "result": result.as_dict(),
            }
        )

    # ---------------------------------------------------------- GPL 红线
    print("\nGPL 红线：")
    probe_facts = None
    probe_saw: list[str] = []
    for name in GPL_PROBES:
        facts = facts_from_api(client.get(f"/repos/{name}"))
        probe_saw.append(f"{name}={facts.license_spdx or '未声明'}")
        if license_risk(facts.license_spdx) == "block":
            probe_facts = facts
            break
    print("  试过的仓库：" + "、".join(probe_saw))
    if probe_facts is None:
        print("  **没有找到被识别成强 copyleft 的仓库** —— 拿不到真实数据就无法宣称 GPL 被拦")
        gpl_blocked = False
        merged = {"license_risk": "n/a"}
        not_usable = False
        gpl_probe_name = "（无）"
    else:
        gpl_probe_name = probe_facts.full_name
        table_risk = license_risk(probe_facts.license_spdx)
        gpl_blocked = table_risk == "block"
        print(f"  {probe_facts.full_name} 的许可证 = {probe_facts.license_spdx} → 查表 {table_risk}（要求 block）")
        evidence = "--- src/core.c\nint main(void) { return 0; }\n"
        merged = score_repo("需要一段 diff 生成逻辑", probe_facts, evidence, chat_fn=fake_optimistic_chat)
        not_usable = Candidate(facts=probe_facts, score=merged).usable is False
        print(
            f"  模型说 ok 时，最终风险 = {merged['license_risk']}（要求仍是 block）；"
            f"usable 为 False：{not_usable}"
        )

    # 许可证识别不出来时也必须拦在 ok 之外（git/git 就报 NOASSERTION）
    unknown_facts = facts_from_api(client.get("/repos/git/git"))
    unknown_risk = license_risk(unknown_facts.license_spdx)
    unknown_blocked = unknown_risk != "ok" and Candidate(facts=unknown_facts, score=None).usable is False
    print(f"  识别不出许可证的情形：git/git → {unknown_facts.license_spdx or '未声明'} → {unknown_risk}（要求不是 ok）；拦在 ok 之外：{unknown_blocked}")

    gpl_ok = gpl_blocked and not_usable and unknown_blocked
    passed = (not partial) and passed_needs == len(selected) and gpl_ok and not failures
    print(f"\n需求达标 {passed_needs}/{len(selected)}"
          f"{'（部分运行）' if partial else ''}；GPL 拦截 {'成立' if gpl_ok else '不成立'}")
    print(f"结论：{'达标' if passed else ('**部分运行，不构成验收**' if partial else '**未达标**')}")
    print(f"（仓库事实缓存命中 {client.hits} 次 —— 命中的都是白省下来的往返）")

    if args.freeze:
        # 冻结只是**备料**，不是评测：这一步不写验收报告（免得把半截结果盖到验收报告上）
        return 0

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    report = {
        "date": stamp,
        "partial": partial,
        "min_valid_per_need": MIN_VALID,
        "needs_passed": passed_needs,
        "needs_total": len(NEEDS),
        "needs_selected": len(selected),
        "gpl_probe": {
            "repo": gpl_probe_name,
            "tried": probe_saw,
            "model_says_ok_but_final_risk": merged.get("license_risk"),
            "usable": not not_usable,
            "unknown_license_risk": unknown_risk,
            "unknown_license_blocked": unknown_blocked,
        },
        "needs": results,
        "passed": passed,
    }
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    # 部分运行**另存一份**：绝不能把一次只跑了 1 个需求的结果盖到验收报告上
    suffix = "-partial" if partial else ""
    target = REPORT_DIR / f"scout-eval-{stamp}{suffix}.json"
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    review = REPORT_DIR / f"scout-review-{stamp}{suffix}.md"
    review.write_text(render_markdown(report), encoding="utf-8")
    print(f"报告：{target}")
    print(f"给人抽查的清单：{review}")
    if partial:
        return 2
    return 0 if passed else 1


def render_markdown(report: dict) -> str:
    """给人抽查"评分合理性"的清单：每个需求下逐条列出候选与模型理由。"""
    lines = [
        "# 开源猎手抽查清单（5.1）",
        "",
        f"- 日期：{report['date']}",
        (f"- **部分运行：只跑了 {report.get('needs_selected', report['needs_total'])}/"
         f"{report['needs_total']} 个需求 —— 不构成 5.1 验收**" if report.get("partial") else ""),
        (
            f"- 需求达标：**{report['needs_passed']}/{report['needs_total']}**"
            f"（每个需求要求 ≥{report['min_valid_per_need']} 个有效候选）"
        ),
        (
            f"- GPL 红线：试过 {'、'.join(report['gpl_probe']['tried'])}；模型说 ok 时最终风险仍是 "
            f"**{report['gpl_probe']['model_says_ok_but_final_risk']}**"
            f"（usable={report['gpl_probe']['usable']}）；识别不出许可证时（git/git）风险 = "
            f"{report['gpl_probe']['unknown_license_risk']}，"
            f"拦在 ok 之外：{report['gpl_probe']['unknown_license_blocked']}"
        ),
        "",
        "> 请抽查的是**评分合理性**：这些候选和需求搭不搭、`usage` 判得对不对。",
        "> 硬过滤与许可证是代码判的，不需要你看。",
        "",
    ]
    for item in report["needs"]:
        lines += [f"## {item['need']}", "", f"- 搜索词：`{item['query']}`　搜到 {item['result']['searched']} 条，有效 {item['valid']} 个", ""]
        if item.get("sourcing"):
            lines += [f"- {describe_sourcing(item['sourcing'])}", ""]
        rows = [c for c in item["result"]["candidates"] if (c.get("score") or {}).get("usage") != "skip"]
        if rows:
            lines += ["| 仓库 | ★ | 许可证 | 相关度 | usage | 模型理由 |", "|---|---|---|---|---|---|"]
            for candidate in rows:
                score = candidate.get("score") or {}
                lines.append(
                    f"| `{candidate['full_name']}` | {candidate['stars']} | {candidate.get('license') or '未声明'} | "
                    f"{score.get('relevance', '—')} | {score.get('usage', '—')} | {str(score.get('reason', ''))[:80]} |"
                )
        else:
            lines.append("（本次没有评分的候选）")
        if item["result"]["skipped"]:
            lines += ["", "跳过（硬过滤）：" + "；".join(
                f"`{s['full_name']}`（{s['reason']}）" for s in item["result"]["skipped"][:5]
            )]
        lines.append("")
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
