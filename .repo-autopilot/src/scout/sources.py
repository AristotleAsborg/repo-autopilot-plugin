"""5.1 的**多路取材**（人类在 2026-09-12 定的方向 C）：

> 不再只靠关键词搜索 —— 改用 **topic 搜索 + 人工策展清单（awesome-*）+ 已知项目反查**。

## 为什么关键词搜索不够

关键词搜索返回的是"名字/简介里恰好含这些词"的仓库，与"能不能解决这个需求"只有弱相关。
实测：给"增量 diff 编辑"需求，关键词搜索把 54k★ 的通用 agent 框架排在最前（相关度 0.15），
而真正对口的项目要么名字不带这些词，要么因为星数/时效被硬线挡掉。

三条补充来源各自回答一个不同的问题：

| 来源 | 回答的问题 | 为什么可信 |
|---|---|---|
| `topic:` 搜索 | "这个领域的人把哪些项目归到了这个主题下" | 主题是**人工策展**的，噪音比关键词小一个量级 |
| awesome 清单 | "同行已经替我筛过一遍的清单是什么" | 清单本身是人工维护的，链接即推荐 |
| 已知项目反查 | "这个领域里大家都知道的那几个项目是什么" | 模型的知识在这里是**资产**而非风险（它给名字，我们去核实） |

**共同的安全边界**：三条来源都只产出**候选名字**；所有候选仍然走同一条下游
（硬过滤 → 看代码打分 → 许可证查表）。**没有任何一条来源能绕过验收条件** ——
来源只影响"谁进入候选池"，不影响"谁能通过"。
"""

from __future__ import annotations

import base64
import dataclasses
import re
from collections.abc import Callable, Sequence
from typing import Any

from .core import (
    Candidate,
    RepoFacts,
    ScoutResult,
    below_bar_reason,
    facts_from_api,
    hard_filter,
    multi_round_scout,
    score_pool,
    search_repositories,
)

#: 从清单 README 里抠 GitHub 链接
REPO_LINK_RE = re.compile(r"https?://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)")
#: 这些路径不是仓库
NOT_REPOS = frozenset({"topics", "sponsors", "search", "collections", "marketplace", "apps", "orgs"})
#: 一份清单最多取多少条链接。清单动辄几百条，全取会把预算和 10 分钟上限一起烧光：
#: 实测一份 awesome 清单取 40 条要 40 次 API 调用，两份就是 80 次（每 5 分钟只允许 5000 次，
#: 但**时间**花在往返上），而多路取材本来就是为了让对口项目**出现**，不是把池子灌满。
MAX_LINKS_PER_LIST = 24

TOPIC_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"topics": {"type": "array", "items": {"type": "string"}}, "why": {"type": "string"}},
    "required": ["topics", "why"],
}
KNOWN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"projects": {"type": "array", "items": {"type": "string"}}, "why": {"type": "string"}},
    "required": ["projects", "why"],
}
CURATED_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"lists": {"type": "array", "items": {"type": "string"}}, "why": {"type": "string"}},
    "required": ["lists", "why"],
}

TOPIC_PROMPT = """给下面这个需求挑 3 个 **GitHub 主题（topic）** 名字。

需求：{need}

要求：
- 只要主题名本身（如 `sandbox`、`human-in-the-loop`、`code-generation`），**不要**写成 `topic:xxx`；
- 主题要能在 GitHub 上真实存在（宁可用常见主题词）；
- 按库里项目跟这个需求的相关程度从高到低给。

只输出 JSON：{{"topics": ["...", "...", "..."], "why": "一句话"}}
"""

KNOWN_PROMPT = """列出 3–5 个**你确实知道**能解决下面需求的知名开源项目。

需求：{need}

要求：
- 每条写成 `owner/repo`（GitHub 全名），不确定的宁可不写；
- 不要编造仓库名 —— 我们会逐个去 GitHub 核实，编造只会浪费一次调用；
- **优先给专门做这件事的工具/库**（CLI、库、小工具），不要把大而全的 agent 框架
  排在前面：框架只能"参考设计"，专门工具才可能"直接抄/当依赖"；
- 如果需求里提到了某个框架，请在它之外再给几个**独立实现**；
- 按相关程度从高到低。

只输出 JSON：{{"projects": ["owner/repo", ...], "why": "一句话"}}
"""

CURATED_PROMPT = """给下面这个需求挑 2 份 **awesome 清单**（人工策展的 "awesome-xxx" 仓库）。

需求：{need}

要求：
- 每条写成 `owner/repo`，且必须是你确实知道存在的 awesome 列表；
- 挑那种**大量收录同类项目**的清单。

只输出 JSON：{{"lists": ["owner/repo", ...], "why": "一句话"}}
"""


def default_chat(messages: list[dict[str, str]], schema: dict[str, Any]) -> Any:
    from ..gateway import chat

    return chat(messages, schema, "flash_api", temperature=0.0)


def _names(payload: Any, key: str, *, limit: int, owner_repo: bool) -> list[str]:
    """从模型输出里取一串名字（`owner/repo` 或主题名），顺手做格式校验。"""
    if not isinstance(payload, dict):
        return []
    names: list[str] = []
    for item in payload.get(key) or []:
        text = str(item).strip().strip("/").lstrip("#")
        if owner_repo:
            if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", text):
                continue
        else:
            text = text.removeprefix("topic:").lower()
            if not text or " " in text or not re.fullmatch(r"[a-z0-9_.-]+", text):
                continue
        if text not in names:
            names.append(text)
    return names[:limit]


# ------------------------------------------------------------------ 三个来源

def suggest_topics(
    need: str,
    *,
    chat_fn: Callable[[list[dict[str, str]], dict[str, Any]], Any] | None = None,
    limit: int = 3,
) -> list[str]:
    """让模型给几个 GitHub 主题名（**只要名字**，`topic:` 前缀由我们拼）。"""
    chat_fn = chat_fn or default_chat
    try:
        payload = chat_fn([{"role": "user", "content": TOPIC_PROMPT.format(need=need)}], TOPIC_SCHEMA)
    except Exception:  # noqa: BLE001
        return []
    return _names(payload, "topics", limit=limit, owner_repo=False)


def suggest_known_projects(
    need: str,
    *,
    chat_fn: Callable[[list[dict[str, str]], dict[str, Any]], Any] | None = None,
    limit: int = 5,
) -> list[str]:
    """让模型点名它知道的知名项目（**我们逐个去核实**，编造的会被 API 挡掉）。"""
    chat_fn = chat_fn or default_chat
    try:
        payload = chat_fn([{"role": "user", "content": KNOWN_PROMPT.format(need=need)}], KNOWN_SCHEMA)
    except Exception:  # noqa: BLE001
        return []
    return _names(payload, "projects", limit=limit, owner_repo=True)


def suggest_curated_lists(
    need: str,
    *,
    chat_fn: Callable[[list[dict[str, str]], dict[str, Any]], Any] | None = None,
    limit: int = 2,
) -> list[str]:
    """让模型点名 2 份 awesome 清单（同样逐个核实存在性）。"""
    chat_fn = chat_fn or default_chat
    try:
        payload = chat_fn([{"role": "user", "content": CURATED_PROMPT.format(need=need)}], CURATED_SCHEMA)
    except Exception:  # noqa: BLE001
        return []
    return _names(payload, "lists", limit=limit, owner_repo=True)


def read_repo_text(client: Any, full_name: str, *, limit: int = 400_000) -> str:
    """取一份 README 的文本（截断到 limit 字节，清单动辄几百 KB）。

    取不到就返回空串 —— 这是**尽力而为**的读取：清单可能叫 README.rst、可能压根没有。
    调用方（`repos_in_list`）拿到空串会当成"这份清单没链接"，不会静默当成"清单为空"。
    """
    for path in ("readme", "contents/README.md"):
        try:
            payload = client.get(f"/repos/{full_name}/{path}")
        except Exception:  # noqa: BLE001, S112
            continue
        if isinstance(payload, dict) and payload.get("content"):
            try:
                return base64.b64decode(payload["content"])[:limit].decode("utf-8", errors="replace")
            except Exception:  # noqa: BLE001, S112
                continue
    return ""


def repos_in_list(client: Any, list_full_name: str, *, limit: int = MAX_LINKS_PER_LIST) -> list[str]:
    """从一份 awesome 清单里抠出它链接的仓库名（去重、去掉清单自己与不是仓库的路径）。"""
    text = read_repo_text(client, list_full_name)
    if not text:
        return []
    found: list[str] = []
    own = list_full_name.lower()
    for owner, repo in REPO_LINK_RE.findall(text):
        if owner.lower() in NOT_REPOS:
            continue
        name = f"{owner}/{repo.removesuffix('.git')}"
        if name.lower() == own or name in found:
            continue
        found.append(name)
        if len(found) >= limit:
            break
    return found


def topic_search(client: Any, topic: str, *, per_page: int = 20) -> list[RepoFacts]:
    return search_repositories(client, f"topic:{topic}", per_page=per_page)


# ------------------------------------------------------------------ 多路取材

@dataclasses.dataclass
class SourcingReport:
    """每路来源各贡献了多少候选 —— 报告里要看得出"是谁把对口项目带进来的"。"""

    keyword: int = 0
    topic: int = 0
    curated: int = 0
    known: int = 0
    seed: int = 0
    topics_used: list[str] = dataclasses.field(default_factory=list)
    lists_used: list[str] = dataclasses.field(default_factory=list)
    known_used: list[str] = dataclasses.field(default_factory=list)

    @property
    def extra(self) -> int:
        """关键词之外的三条来源一共贡献了多少（这是"改取材方式"到底有没有用的直接读数）。"""
        return self.topic + self.curated + self.known + self.seed

    def as_dict(self) -> dict[str, Any]:
        data = dataclasses.asdict(self)
        data["extra"] = self.extra
        return data


def multi_source_scout(
    need: str,
    *,
    client: Any,
    queries: Sequence[str] = (),
    seeds: Sequence[str] = (),
    max_rounds: int = 2,
    queries_per_round: int = 2,
    per_page: int = 20,
    score_per_round: int = 6,
    min_valid: int = 3,
    chat_fn: Callable[[list[dict[str, str]], dict[str, Any]], Any] | None = None,
) -> tuple[ScoutResult, SourcingReport]:
    """
    关键词搜索（多轮换词）+ topic 搜索 + awesome 清单 + 已知项目反查，**四路合并**后评分。

    合并规则与单路完全一致（硬过滤 → 打分），所以"多取材"不会降低任何门槛；
    它只是让候选池里**更可能出现真正对口的项目**。
    `SourcingReport` 记录每一路贡献了多少 —— 免得我们以为"关键词搜索变好了"。
    """
    from .core import RELEVANCE_FLOOR

    result = ScoutResult(need=need, query=queries[0] if queries else need, searched=0, candidates=[])
    report = SourcingReport()
    seen: set[str] = set()

    def absorb(facts_list: Sequence[RepoFacts], source: str) -> int:
        added = 0
        for facts in facts_list:
            result.searched += 1
            if not facts.full_name or facts.full_name in seen:
                continue
            seen.add(facts.full_name)
            ok, reason = hard_filter(facts)
            if not ok:
                result.skipped.append({"full_name": facts.full_name, "reason": reason, "source": source})
                if below_bar_reason(facts):
                    result.below_bar.append(facts)
                continue
            candidate = Candidate(facts=facts)
            candidate.notes.append(f"来源：{source}")
            result.candidates.append(candidate)
            added += 1
        return added

    def facts_of(name: str, why_missing: str) -> RepoFacts | None:
        try:
            return facts_from_api(client.get(f"/repos/{name}"))
        except Exception:  # noqa: BLE001
            result.skipped.append({"full_name": name, "reason": why_missing, "source": "lookup"})
            return None

    # ---- 1) 已知项目反查：模型点名 → 我们核实存在性
    report.known_used = suggest_known_projects(need, chat_fn=chat_fn)
    report.known = absorb(
        [facts for name in report.known_used if (facts := facts_of(name, "模型点名的项目不存在（已核实）"))],
        f"known({','.join(report.known_used)})" if report.known_used else "known",
    )

    # ---- 2) topic 搜索
    report.topics_used = suggest_topics(need, chat_fn=chat_fn)
    topic_facts: list[RepoFacts] = []
    for topic in report.topics_used:
        topic_facts.extend(topic_search(client, topic, per_page=per_page))
    report.topic = absorb(topic_facts, f"topic({','.join(report.topics_used)})" if report.topics_used else "topic")

    # ---- 3) awesome 清单（模型点名清单 → 抠链接 → 逐个核实）
    report.lists_used = suggest_curated_lists(need, chat_fn=chat_fn)
    curated_facts: list[RepoFacts] = []
    for list_name in report.lists_used:
        if facts_of(list_name, "模型点名的清单不存在（已核实）") is None:
            continue
        for repo_name in repos_in_list(client, list_name):
            facts = facts_of(repo_name, "清单里的链接取不到")
            if facts is not None:
                curated_facts.append(facts)
    report.curated = absorb(
        curated_facts, f"curated({','.join(report.lists_used)})" if report.lists_used else "curated"
    )

    # ---- 4) 关键词搜索（沿用原来的多轮换词 + 每轮深评预算）
    keyword_result, _rounds = multi_round_scout(
        need,
        client=client,
        queries=list(queries) or [need],
        seeds=list(seeds),
        max_rounds=max_rounds,
        queries_per_round=queries_per_round,
        per_page=per_page,
        score_per_round=score_per_round,
        min_valid=min_valid,
        chat_fn=chat_fn,
    )
    result.searched += keyword_result.searched
    result.below_bar.extend(keyword_result.below_bar)
    for entry in keyword_result.skipped:
        result.skipped.append({**entry, "source": "keyword"})
    for candidate in keyword_result.candidates:
        name = candidate.facts.full_name
        if name in seen:
            # 两条路都捞到同一个仓库：把关键词那条已经打好的分**搬家**过来，省一次深评
            existing = next((item for item in result.candidates if item.facts.full_name == name), None)
            if existing is not None and existing.score is None and candidate.score is not None:
                existing.score = candidate.score
                existing.evidence_chars = candidate.evidence_chars
                existing.notes.append("关键词路已评分（复用）")
            continue
        seen.add(name)
        candidate.notes.append("来源：keyword")
        result.candidates.append(candidate)
        report.keyword += 1

    # ---- 统一补评：三条新来源的候选还没打分
    score_pool(need, result, client=client, score_top=score_per_round, chat_fn=chat_fn)
    result.candidates.sort(key=lambda item: -(item.score or {}).get("relevance", 0.0))
    for candidate in result.candidates:
        if candidate.score is None:
            candidate.notes.append("未评分（超出本轮模型预算）")
    # 记录来源与达标情况，报告里要看得出"对口项目是关键词带来的还是策展带来的"
    for candidate in result.candidates:
        score = candidate.score or {}
        if float(score.get("relevance") or 0.0) >= RELEVANCE_FLOOR and score.get("usage") != "skip":
            candidate.notes.append("达标：模型判定相关")
    return result, report


def validate_sources() -> list[str]:
    """自检：解析层在明显输入下的行为（`/体检` 会调）。"""
    problems: list[str] = []
    if _names({"topics": ["topic:Sandbox", "a b", "", "Sandbox"]}, "topics", limit=3, owner_repo=False) != ["sandbox"]:
        problems.append("主题名解析：应去掉 topic: 前缀、拒绝含空格、去重")
    if _names({"projects": ["a/b", "not a repo", "x/y/z"]}, "projects", limit=5, owner_repo=True) != ["a/b"]:
        problems.append("owner/repo 解析：应只接受恰好一段斜杠")
    if repos_in_list(type("C", (), {"get": staticmethod(lambda *a, **k: {})})(), "a/b"):
        problems.append("空响应不该解析出仓库")
    return problems
