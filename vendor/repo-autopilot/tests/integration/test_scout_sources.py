"""5.1 多路取材（方向 C）的确定性测试：不联网、不调真模型。

要钉死的事情只有一件：**换了取材方式，关口不能跟着松**。

- 三条新来源（topic 搜索 / awesome 清单 / 已知项目反查）只产出**候选名字**；
- 它们带来的候选必须和关键词搜索的候选走**同一条下游**：硬过滤 → 看代码打分 → 许可证查表；
- 所以这里正面验证：清单里混进来的 20★ 仓库、GPL 仓库，**照样进不了达标集**。

另外验证解析层（`repos_in_list` / `_names`）不被脏输入带偏，以及一路失败不影响其它路
（模型没起来时 `suggest_*` 返回空表，取材退化成原来的关键词搜索，而不是整条链路挂掉）。
"""

from __future__ import annotations

import base64

import pytest

from src.scout import (
    MAX_LINKS_PER_LIST,
    Candidate,
    balanced_listing,
    candidate_source,
    describe_repo,
    evidence_has_code,
    facts_from_api,
    good_candidates,
    multi_source_scout,
    pick_targets,
    read_repo_text,
    repos_in_list,
    suggest_curated_lists,
    suggest_known_projects,
    suggest_topics,
    validate_sources,
)
from src.scout.core import RANK_SCHEMA, SCORE_SCHEMA
from src.scout.sources import CURATED_SCHEMA, KNOWN_SCHEMA, TOPIC_SCHEMA

PUSHED = "2026-09-01T00:00:00Z"


def repo_payload(name: str, stars: int, spdx: str | None = "MIT", description: str = "") -> dict:
    return {
        "full_name": name,
        "stargazers_count": stars,
        "pushed_at": PUSHED,
        "license": {"spdx_id": spdx} if spdx else None,
        "archived": False,
        "default_branch": "main",
        "description": description,
        "html_url": f"https://github.com/{name}",
    }


def b64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


class FakeClient:
    """按路径路由的假 GitHub：只实现取材与取证真正用到的几条路径。"""

    def __init__(self, repos: dict[str, dict], searches: dict[str, list[dict]], readmes: dict[str, str]):
        self.repos = repos
        self.searches = searches
        self.readmes = readmes
        self.calls: list[str] = []

    def get(self, path: str, params: dict | None = None):
        self.calls.append(path)
        if path == "/search/repositories":
            return {"items": self.searches.get((params or {}).get("q", ""), [])}
        if path.startswith("/repos/"):
            rest = path[len("/repos/"):]
            for prefix, text in self.readmes.items():
                if rest in (f"{prefix}/readme", f"{prefix}/contents/README.md"):
                    return {"content": b64(text)}
            if rest.endswith("/contents/"):
                return [
                    {"name": "README.md", "type": "file"},
                    {"name": "core.py", "type": "file"},
                    {"name": "src", "type": "dir"},
                ]
            if rest.endswith("/contents/src"):
                return [{"name": "mod.py", "type": "file"}]
            if rest.endswith(("/contents/core.py", "/contents/src/mod.py")):
                return {"content": b64("def handle(issue):\n    return issue.title\n")}
            name = rest.rstrip("/")
            if name in self.repos:
                return self.repos[name]
        raise KeyError(path)


def fake_chat(topics=(), known=(), lists=(), relevance=0.9, usage="copy"):
    """按 schema 分派的假模型：取材三问 + 排序 + 打分。"""

    def _chat(messages, schema):
        if schema is TOPIC_SCHEMA:
            return {"topics": list(topics), "why": "test"}
        if schema is KNOWN_SCHEMA:
            return {"projects": list(known), "why": "test"}
        if schema is CURATED_SCHEMA:
            return {"lists": list(lists), "why": "test"}
        if schema is RANK_SCHEMA:
            return {"picks": []}
        if schema is SCORE_SCHEMA:
            return {"relevance": relevance, "usage": usage, "license_risk": "ok", "reason": "证据里有对口逻辑"}
        raise AssertionError(f"未预料的 schema：{schema}")

    return _chat


# ------------------------------------------------------------------ 解析层

def test_repos_in_list_keeps_only_real_repos() -> None:
    readme = """
# awesome 清单

- [A](https://github.com/acme/one) —— 对口
- [B](https://github.com/acme/two.git)
- 重复：[C](https://github.com/acme/one)
- 不是仓库：[topics](https://github.com/topics/sandbox)
- 自己：[self](https://github.com/listowner/awesome-x)
- 混着写 [D](https://github.com/acme/three/) 和 [E](http://github.com/acme/four)
"""
    client = FakeClient({}, {}, {"listowner/awesome-x": readme})
    assert repos_in_list(client, "listowner/awesome-x") == [
        "acme/one",
        "acme/two",
        "acme/three",
        "acme/four",
    ]


def test_repos_in_list_respects_the_cap() -> None:
    readme = "\n".join(f"- https://github.com/acme/r{index}" for index in range(MAX_LINKS_PER_LIST + 25))
    client = FakeClient({}, {}, {"o/awesome-x": readme})
    assert len(repos_in_list(client, "o/awesome-x")) == MAX_LINKS_PER_LIST


def test_read_repo_text_returns_empty_when_missing() -> None:
    client = FakeClient({}, {}, {})
    assert read_repo_text(client, "nobody/missing") == ""


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (["topic:Sandbox", "sandbox"], ["sandbox"]),           # 前缀去掉 + 去重
        (["human in the loop"], []),                          # 主题名不能含空格
        (["topic:"], []),                                     # 空
        (["#duplicate-detection"], ["duplicate-detection"]),   # 手滑带了 #
    ],
)
def test_suggest_topics_normalises(raw: list[str], expected: list[str]) -> None:
    assert suggest_topics("需求", chat_fn=fake_chat(topics=raw)) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (["a/b"], ["a/b"]),
        (["not a repo", "x/y/z", "/a/b/"], ["a/b"]),   # 只接受恰好一段斜杠
        ([], []),
    ],
)
def test_suggest_known_projects_rejects_junk(raw: list[str], expected: list[str]) -> None:
    assert suggest_known_projects("需求", chat_fn=fake_chat(known=raw)) == expected


def test_each_source_degrades_alone() -> None:
    """一路模型调用挂掉 → 那条路空手而归，不影响别的路（更不该让整条链路挂）。"""
    def boom(*_a, **_k):
        raise RuntimeError("模型没起来")

    assert suggest_topics("需求", chat_fn=boom) == []
    assert suggest_known_projects("需求", chat_fn=boom) == []
    assert suggest_curated_lists("需求", chat_fn=boom) == []


def test_validate_sources_is_clean() -> None:
    assert validate_sources() == []


# ------------------------------------------------------------------ 合并与关口

def build_scout(needs_extra: bool = True):
    repos = {
        # 已知项目反查带来的：达标
        "acme/known-good": repo_payload("acme/known-good", 900, description="正好解决这个需求"),
        # topic 搜索带来的：达标
        "acme/topic-good": repo_payload("acme/topic-good", 700, description="主题命中"),
        # 清单带来的：20★，硬线之下
        "acme/tiny": repo_payload("acme/tiny", 20, description="清单里的玩具"),
        # 清单**独有**带来的：10★，硬线之下（用来验证"策展来源"也会被挡，且挡了要记明来源）
        "acme/curated-tiny": repo_payload("acme/curated-tiny", 10, description="清单里的另一个玩具"),
        # 清单带来的：GPL，许可证关口必须挡住
        "acme/gpl": repo_payload("acme/gpl", 4000, spdx="GPL-3.0", description="很对口但是 GPL"),
        # 关键词搜索带来的：达标
        "acme/keyword-good": repo_payload("acme/keyword-good", 300, description="关键词命中"),
        # 清单本身（模型点名后必须核实它存在，否则视为编造）
        "listowner/awesome-x": repo_payload("listowner/awesome-x", 12_000, description="人工策展清单"),
    }
    searches = {
        "topic:sandbox": [repos["acme/topic-good"], repos["acme/tiny"]],
        "issue triage automation": [repos["acme/keyword-good"]],
    }
    readmes = {
        "listowner/awesome-x": (
            "- https://github.com/acme/tiny\n"
            "- https://github.com/acme/curated-tiny\n"
            "- https://github.com/acme/gpl\n"
        )
    }
    client = FakeClient(repos, searches, readmes)
    chat = fake_chat(
        topics=["sandbox"] if needs_extra else [],
        known=["acme/known-good"] if needs_extra else [],
        lists=["listowner/awesome-x"] if needs_extra else [],
    )
    result, report = multi_source_scout(
        "给 issue 自动分类",
        client=client,
        queries=["issue triage automation"],
        max_rounds=1,
        queries_per_round=1,
        score_per_round=6,
        chat_fn=chat,
    )
    return result, report


def test_new_sources_cannot_bypass_the_gates() -> None:
    """**核心断言**：策展来源带来的候选照样过硬线与许可证关口。"""
    result, _report = build_scout()

    names = {item.facts.full_name for item in result.candidates}
    assert {"acme/known-good", "acme/topic-good", "acme/keyword-good"} <= names
    assert "acme/tiny" not in names, "20★ 不能因为来自 awesome 清单就免检"
    assert "acme/gpl" not in names or all(
        item.score and item.score.get("license_risk") == "block"
        for item in result.candidates
        if item.facts.full_name == "acme/gpl"
    )

    reasons = {entry["full_name"]: entry["reason"] for entry in result.skipped}
    assert "acme/tiny" in reasons and "stars" in reasons["acme/tiny"]
    # 清单**独有**的候选同样被硬线挡住，并且挡下时记清了"是策展路带进来的"
    assert "acme/curated-tiny" in reasons
    assert any(
        entry["full_name"] == "acme/curated-tiny" and entry["source"].startswith("curated(")
        for entry in result.skipped
    )
    # GPL 进得了候选池（硬线只查星数/时效），但**在许可证关口被拿下**，永远进不了达标集
    assert "acme/gpl" not in {item.facts.full_name for item in good_candidates(result)}


def test_report_attributes_every_candidate_to_a_source() -> None:
    _result, report = build_scout()

    assert report.known == 1
    assert report.topic == 1
    assert report.curated == 1, "清单里的 GPL 项目进得了候选池（许可证关口在后面）"
    assert report.keyword == 1
    assert report.extra == 3
    assert report.topics_used == ["sandbox"]
    assert report.lists_used == ["listowner/awesome-x"]
    data = report.as_dict()
    assert data["extra"] == 3 and data["known_used"] == ["acme/known-good"]


def test_sourced_candidates_really_get_scored() -> None:
    """取材只是把人带进来；**能不能用仍然由"看代码打分"决定**。"""
    result, _report = build_scout()
    scored = {item.facts.full_name: item.score for item in result.candidates if item.score}
    assert scored, "关键候选必须真的走到评分这一步，不能只进池子不打分"
    assert any(float(score.get("relevance") or 0) >= 0.5 for score in scored.values())
    assert any("来源：known(" in "｜".join(item.notes) for item in result.candidates)


def test_without_extra_sources_it_falls_back_to_keyword_only() -> None:
    """模型不给主题/清单/项目时，行为退化成原来的关键词搜索（不是崩溃，也不是放宽）。"""
    result, report = build_scout(needs_extra=False)
    assert report.extra == 0
    assert {item.facts.full_name for item in result.candidates} == {"acme/keyword-good"}


# ------------------------------------------------------------------ 初筛配额

def facts(name: str, stars: int, spdx: str | None = "MIT", description: str = ""):
    """测试里造 `RepoFacts` 的快捷方式（走真实的 API 解析路径，不手搓字段）。"""
    return facts_from_api(repo_payload(name, stars, spdx, description))


def test_balanced_listing_gives_every_source_a_turn() -> None:
    """**关键回归**：候选池变大后，便宜的初筛必须每路都看到，否则策展那路等于白找。

    没有这条，多路取材就会被"按星数排在前面的大项目"挤掉：池子里 186 个候选，
    初筛提示只放前 15 个，而那 15 个全是几万星的主题搜索结果。
    """
    mine = Candidate(facts=facts("acme/mine", 5, description="my own"), notes=["来源：keyword"])
    each = [
        Candidate(facts=facts(f"big/huge{index}", 50_000, description="很大很出名"),
                  notes=["来源：topic(sandbox)"])
        for index in range(30)
    ]
    curated = [
        Candidate(facts=facts("acme/on-target", 300, description="正好对口"),
                  notes=["来源：curated(listowner/awesome-x)"])
    ]

    listing = balanced_listing([mine, *each, *curated], query="sandbox", limit=3)
    names = [item.facts.full_name for item in listing]
    assert "acme/mine" in names and "acme/on-target" in names, "每一路都要有机会进入初筛"
    assert len(names) == 3


def test_balanced_listing_is_reproducible() -> None:
    """同样的池子 → 同样的清单（金样本比对的前提）。"""
    pool = [
        Candidate(facts=facts(f"a/r{index}", 100 + index), notes=[f"来源：src{index % 2}"])
        for index in range(20)
    ]
    first = [item.facts.full_name for item in balanced_listing(pool, query="x", limit=8)]
    second = [item.facts.full_name for item in balanced_listing(pool, query="x", limit=8)]
    assert first == second


def test_candidate_source_reads_the_marker_note() -> None:
    assert candidate_source(Candidate(facts=facts("a/b", 100), notes=["来源：topic(sandbox)"])) == "topic"
    assert candidate_source(Candidate(facts=facts("a/b", 100), notes=["未评分（超出本轮模型预算）"])) == "其他"
    assert candidate_source(Candidate(facts=facts("a/b", 100))) == "其他"


# ------------------------------------------------------------------ 深评配额

def test_pick_targets_reserves_quota_for_every_source() -> None:
    """**关键回归**：模型初筛不能把深评名额全给"名字里带 agent 的大项目"。

    实测（2026-09-12）：12 个深评名额全被初筛给了大框架，路线点名的 aider
    因为名字里没有 code/editing/agent 任何一个词，一个名额都没拿到 ——
    于是"多路取材"辛苦找来的对口项目连被打分的机会都没有。
    """
    known = [
        Candidate(facts=facts("Aider-AI/aider", 48_913, description="AI pair programming"), notes=["来源：known"]),
        Candidate(facts=facts("cline/cline", 67_869, description="Autonomous coding agent"), notes=["来源：known"]),
    ]
    huge = [
        Candidate(facts=facts(f"big/framework{index}", 200_000, description="agent agent agent"),
                  notes=["来源：topic(sandbox)"])
        for index in range(20)
    ]
    # 一个"只挑大框架"的模型：它会把所有填空名额都给自己看得顺眼的
    def biased_chat(messages, schema):
        if schema is RANK_SCHEMA:
            return {"picks": [item.facts.full_name for item in huge[:10]], "why": "都很大"}
        raise AssertionError

    picked = pick_targets(
        "LLM 增量 diff 编辑", [*known, *huge], query="code editing agent", chat_fn=biased_chat, limit=6
    )
    names = [item.facts.full_name for item in picked]
    assert "Aider-AI/aider" in names, "配额保底必须让 known 路的每个项目都进深评"
    assert "cline/cline" in names
    assert len(picked) == 6


def test_pick_targets_keeps_all_when_pool_is_small() -> None:
    pool = [Candidate(facts=facts(f"a/r{index}", 100), notes=["来源：keyword"]) for index in range(3)]
    assert len(pick_targets("need", pool, query="q", limit=10)) == 3


# ------------------------------------------------------------------ 证据下钻

class PackageDirClient:
    """模拟"包目录与仓库同名、顶层没有代码文件"的极常见布局（aider 就是这样）。"""

    def __init__(self, repo_dir: str = "acme"):
        self.repo_dir = repo_dir

    def get(self, path: str, params: dict | None = None):
        if path.endswith("/contents/"):
            return [
                {"name": "README.md", "type": "file"},
                {"name": "pyproject.toml", "type": "file"},
                {"name": self.repo_dir, "type": "dir"},
                {"name": "tests", "type": "dir"},
            ]
        if path.endswith(f"/contents/{self.repo_dir}"):
            return [
                {"name": "__init__.py", "type": "file"},
                {"name": "coders", "type": "dir"},
            ]
        if path.endswith(f"/contents/{self.repo_dir}/coders"):
            return [{"name": "editblock_coder.py", "type": "file"}]
        if path.endswith(".py"):
            return {"content": b64("class EditBlockCoder:\n    def apply(self, patch):\n        return patch\n")}
        raise KeyError(path)


def test_evidence_descends_into_the_package_directory() -> None:
    """**关键回归**：顶层只有"与仓库同名的包目录"时，必须下钻取代码。

    不下钻的后果实测过：`Aider-AI/aider`（路线 5.1 里"增量 diff 编辑"的参考实现）证据里
    一个代码文件都没有 → 按路线"没有代码不许评分"被判 relevance 0.0 —— 证据缺口把
    最对口的项目误杀成不相关。
    """
    evidence = describe_repo(PackageDirClient("aider"), "Aider-AI/aider", branch="main")
    assert evidence_has_code(evidence), "下钻之后证据里必须有代码文件"
    assert "editblock_coder.py" in evidence
