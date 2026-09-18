"""5.1 猎手的确定性测试（不联网、不调模型）。

钉死四件事：

1. **许可证是查表，不是模型判断**：GPL/AGPL 一律 `block`；模型说 ok 也会被改回来
   （法律风险不该由模型权衡）；没写许可证 → `warn`（"没声明"不等于"随便用"）；
2. **硬过滤先于模型**：stars/归档/两年没动 → 直接 skip 并记录原因，**不产生模型调用**；
3. **没代码不许打分**：只有在 README 的仓库直接拒评（路线明令禁止只读 README 打分）；
4. **抓取留痕**：clone 后记 commit hash，并把 LICENSE 另存一份（记录的是"当时那个 commit 的协议"）。

`fetch_repo` 用**本地 git 仓库**验证（file:// 克隆），不需要网络。
"""

from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.scout import (
    MAX_AGE_DAYS,
    Candidate,
    RepoFacts,
    ScoutError,
    describe_repo,
    evidence_has_code,
    facts_from_api,
    fetch_repo,
    hard_filter,
    license_risk,
    repo_age_days,
    score_repo,
    scout,
)

NOW = datetime(2026, 9, 12, tzinfo=timezone.utc)


# ------------------------------------------------------------------ 许可证查表

@pytest.mark.parametrize(
    ("spdx", "expected"),
    [
        ("GPL-3.0", "block"),
        ("GPL-2.0-only", "block"),
        ("AGPL-3.0", "block"),
        ("LGPL-3.0", "warn"),
        ("MPL-2.0", "warn"),
        ("MIT", "ok"),
        ("Apache-2.0", "ok"),
        ("BSD-3-Clause", "ok"),
        (None, "warn"),
        ("WTFPL", "warn"),
    ],
)
def test_license_risk_is_a_lookup_table(spdx: str | None, expected: str) -> None:
    assert license_risk(spdx) == expected


def test_gpl_is_blocked_even_when_the_model_says_ok() -> None:
    """法律风险不交给模型判断：查表是 block，就必须是 block。"""
    facts = RepoFacts("someone/gpl-tool", 5000, "2026-09-01T00:00:00Z", "GPL-3.0", False, "main")
    evidence = "顶层：src\n--- src/core.py\nprint('hi')\n"
    score = score_repo("需要一段 diff 生成逻辑", facts, evidence, chat_fn=lambda *a: {
        "relevance": 0.9, "usage": "copy", "license_risk": "ok", "reason": "看起来能用"
    })
    assert score["license_risk"] == "block"
    assert "copyleft" in score["reason"]


# ------------------------------------------------------------------ 时间与硬过滤

def test_repo_age_days() -> None:
    recent = (NOW - timedelta(days=10)).isoformat()
    old = (NOW - timedelta(days=1000)).isoformat()
    assert repo_age_days(recent, now=NOW) == 10
    assert repo_age_days(old, now=NOW) == 1000
    assert repo_age_days("看不懂的时间", now=NOW) > MAX_AGE_DAYS


def make_facts(**overrides: object) -> RepoFacts:
    base = {
        "full_name": "owner/repo",
        "stars": 500,
        "pushed_at": (NOW - timedelta(days=30)).isoformat(),
        "license_spdx": "MIT",
        "archived": False,
        "default_branch": "main",
    }
    base.update(overrides)
    return RepoFacts(**base)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("overrides", "expected_reason"),
    [
        ({"stars": 49}, "stars 49 < 50"),
        ({"archived": True}, "已归档"),
        ({"pushed_at": (NOW - timedelta(days=800)).isoformat()}, "800 天"),
    ],
)
def test_hard_filter_skips_with_a_reason(overrides: dict, expected_reason: str) -> None:
    # `now=NOW`：把"现在"钉在文件顶部那个常量上。不钉的话年龄会随真实日期每天变，
    # 「800 天」这条过一天就变成 801（实测：写的时候对，第二天就红）。
    ok, reason = hard_filter(make_facts(**overrides), now=NOW)
    assert ok is False
    assert expected_reason in reason


def test_hard_filter_passes_a_healthy_repo() -> None:
    assert hard_filter(make_facts())[0] is True


def test_facts_from_api_maps_the_fields_we_care_about() -> None:
    facts = facts_from_api(
        {
            "full_name": "a/b",
            "stargazers_count": 120,
            "pushed_at": "2026-01-02T03:04:05Z",
            "license": {"spdx_id": "Apache-2.0"},
            "archived": False,
            "default_branch": "trunk",
            "description": "d",
            "html_url": "u",
        }
    )
    assert (facts.full_name, facts.stars, facts.license_spdx, facts.default_branch) == (
        "a/b", 120, "Apache-2.0", "trunk",
    )


# ------------------------------------------------------------------ 证据

class FakeContentsClient:
    """最小可用的 contents API 假件：只有 README 的仓库 与 有代码的仓库。"""

    def __init__(self, *, with_code: bool = True) -> None:
        self.with_code = with_code
        self.calls: list[str] = []

    def get(self, path: str, *, params: dict | None = None) -> object:
        self.calls.append(path)
        if path.endswith("/contents/"):
            entries = [{"name": "README.md", "type": "file"}]
            if self.with_code:
                entries.append({"name": "src", "type": "dir"})
            return entries
        if path.endswith("/contents/src"):
            return [{"name": "core.py", "type": "file"}]
        if "/contents/src/core.py" in path:
            body = "def generate_diff(old, new):\n    return ''\n"
            return {"content": base64.b64encode(body.encode()).decode("ascii")}
        raise RuntimeError(f"未预置的路径：{path}")


def test_describe_repo_collects_tree_and_a_code_head() -> None:
    client = FakeContentsClient()
    evidence = describe_repo(client, "owner/repo", branch="main")
    assert "顶层：" in evidence
    assert "src/core.py" in evidence
    assert "generate_diff" in evidence, "证据里应该有代码开头"
    assert evidence_has_code(evidence) is True


def test_readme_only_repo_has_no_code_evidence() -> None:
    evidence = describe_repo(FakeContentsClient(with_code=False), "owner/docs-only", branch="main")
    assert evidence_has_code(evidence) is False


def test_score_repo_refuses_to_score_a_readme_only_repo() -> None:
    called: list[int] = []

    def chat_fn(*args, **kwargs):
        called.append(1)
        return {"relevance": 1.0, "usage": "copy", "license_risk": "ok", "reason": "README 写得很好"}

    score = score_repo("需要 diff 生成", make_facts(), "顶层：README.md\n", chat_fn=chat_fn)
    assert score["usage"] == "skip"
    assert "拒绝评分" in score["reason"]
    assert called == [], "只有 README 时连模型都不该调"


def test_score_repo_rejects_illegal_schema() -> None:
    evidence = "--- src/a.py\nx = 1\n"
    with pytest.raises(ScoutError, match="缺字段"):
        score_repo("需要", make_facts(), evidence, chat_fn=lambda *a: {"relevance": 1.0})
    with pytest.raises(ScoutError, match="取值非法"):
        score_repo(
            "需要",
            make_facts(),
            evidence,
            chat_fn=lambda *a: {"relevance": 1.0, "usage": "借用一下", "license_risk": "ok", "reason": "r"},
        )


# ------------------------------------------------------------------ 主流程

class FakeSearchClient(FakeContentsClient):
    def __init__(self, payloads: list[dict]) -> None:
        super().__init__(with_code=True)
        self.payloads = payloads

    def get(self, path: str, *, params: dict | None = None) -> object:
        if path == "/search/repositories":
            return {"items": self.payloads}
        return super().get(path, params=params)


def api_payload(name: str, stars: int, days: int, spdx: str, archived: bool = False) -> dict:
    return {
        "full_name": name,
        "stargazers_count": stars,
        "pushed_at": (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(),
        "license": {"spdx_id": spdx},
        "archived": archived,
        "default_branch": "main",
        "description": name,
    }


def test_scout_filters_then_scores_and_keeps_skip_reasons() -> None:
    payloads = [
        api_payload("good/tool", 900, 20, "Apache-2.0"),
        api_payload("tiny/tool", 5, 20, "MIT"),
        api_payload("stale/tool", 900, 900, "MIT"),
        api_payload("archived/tool", 900, 20, "MIT", archived=True),
    ]
    client = FakeSearchClient(payloads)

    def chat_fn(messages, schema):
        return {"relevance": 0.8, "usage": "reference", "license_risk": "ok", "reason": "结构像"}

    result = scout("需要 diff 生成能力", client=client, chat_fn=chat_fn)

    assert result.searched == 4
    assert [c.facts.full_name for c in result.candidates] == ["good/tool"]
    assert len(result.skipped) == 3
    assert all(item["reason"] for item in result.skipped), "跳过必须留下原因"
    assert result.usable and result.usable[0].facts.full_name == "good/tool"


def test_scout_marks_candidates_beyond_the_model_budget() -> None:
    payloads = [api_payload(f"good/tool{i}", 900, 20, "MIT") for i in range(5)]
    client = FakeSearchClient(payloads)

    def chat_fn(messages, schema):
        return {"relevance": 0.5, "usage": "reference", "license_risk": "ok", "reason": "r"}

    result = scout("需要", client=client, chat_fn=chat_fn, score_top=2)
    scored = [c for c in result.candidates if c.score]
    assert len(scored) == 2
    assert any("超出本次模型预算" in note for c in result.candidates for note in c.notes)


def test_usable_requires_ok_license_and_non_skip_usage() -> None:
    facts = make_facts()
    blocked = Candidate(facts=facts, score={"relevance": 1, "usage": "copy", "license_risk": "block", "reason": "r"})
    skipped = Candidate(facts=facts, score={"relevance": 1, "usage": "skip", "license_risk": "ok", "reason": "r"})
    good = Candidate(facts=facts, score={"relevance": 1, "usage": "copy", "license_risk": "ok", "reason": "r"})
    assert blocked.usable is False and skipped.usable is False and good.usable is True


# ------------------------------------------------------------------ 多轮搜索

class QueryAwareClient:
    """按关键词给不同结果的假件 —— 用来验"换词之后确实捞到了别的东西"。"""

    def __init__(self, table: dict[str, list[dict]]) -> None:
        self.table = table
        self.searches: list[str] = []

    def get(self, path: str, *, params: dict | None = None) -> object:
        if path == "/search/repositories":
            query = str((params or {}).get("q") or "")
            self.searches.append(query)
            return {"items": self.table.get(query, [])}
        if path.endswith("/contents/"):
            return [{"name": "README.md", "type": "file"}, {"name": "src", "type": "dir"}]
        if path.endswith("/contents/src"):
            return [{"name": "core.py", "type": "file"}]
        if "/contents/src/core.py" in path:
            return {"content": base64.b64encode(b"def solve():\n    return 42\n").decode("ascii")}
        return {}


def planner_and_scorer(plans: list[list[str]]):
    """假 chat：规划调用给下一批关键词；prompt 里带 "good/repo" 的打高分，其余打低分。"""
    planned = list(plans)

    def chat(messages, schema):
        prompt = messages[-1]["content"]
        if "下一轮该搜什么" in prompt:
            return {"queries": planned.pop(0) if planned else [], "why": "换个角度"}
        good = "good/repo" in prompt
        return {
            "relevance": 0.9 if good else 0.1,
            "usage": "reference" if good else "skip",
            "license_risk": "ok",
            "reason": "证据里有 solve 函数" if good else "和需求对不上",
        }

    return chat


def test_plan_queries_filters_duplicates_and_repeats() -> None:
    from src.scout import plan_queries

    def chat(messages, schema):
        return {"queries": ["dup", "dup", "", "x" * 80, "fresh"], "why": "w"}

    queries, why = plan_queries("need", seen=[], previous=["dup"], chat_fn=chat)
    assert queries == ["fresh"]
    assert why == "w"


def test_plan_queries_survives_a_broken_model() -> None:
    from src.scout import plan_queries

    def boom(*args, **kwargs):
        raise RuntimeError("模型没起来")

    queries, why = plan_queries("need", seen=[], chat_fn=boom)
    assert queries == [] and "失败" in why


def test_multi_round_scout_switches_keywords_until_enough() -> None:
    from src.scout import multi_round_scout

    client = QueryAwareClient(
        {
            "first-query": [api_payload("junk/repo", 900, 10, "MIT")],
            "better-query": [
                api_payload("good/repo1", 800, 10, "MIT"),
                api_payload("good/repo2", 700, 10, "Apache-2.0"),
                api_payload("good/repo3", 600, 10, "MIT"),
            ],
        }
    )
    chat = planner_and_scorer([["better-query"]])

    result, rounds = multi_round_scout(
        "需要 diff 生成能力", client=client, queries=["first-query"], chat_fn=chat, score_per_round=2
    )

    assert [entry.query for entry in rounds] == ["first-query", "better-query"]
    assert "换词" in rounds[0].note, "第一轮不够时要记下换了什么词"
    assert result.searched == 4
    assert sum(1 for item in result.candidates if item.score) <= 4, "打分预算要封顶"


def test_multi_round_scout_stops_as_soon_as_there_are_enough_good_ones() -> None:
    from src.scout import multi_round_scout

    client = QueryAwareClient(
        {
            "q1": [api_payload(f"good/repo{i}", 900 - i, 10, "MIT") for i in range(4)],
            "q2": [api_payload("never/searched", 500, 10, "MIT")],
        }
    )
    chat = planner_and_scorer([["q2"]])

    _result, rounds = multi_round_scout(
        "需要", client=client, queries=["q1"], chat_fn=chat, score_per_round=3, min_valid=3
    )
    assert len(rounds) == 1, "第一轮就够了就不该再搜"
    assert "达标" in rounds[0].note
    assert client.searches == ["q1"]


def test_multi_round_scout_gives_up_after_the_round_budget() -> None:
    from src.scout import multi_round_scout

    client = QueryAwareClient({"q1": [api_payload("junk/repo", 900, 10, "MIT")]})
    chat = planner_and_scorer([["q2"], ["q3"], ["q4"]])

    result, rounds = multi_round_scout(
        "需要", client=client, queries=["q1"], chat_fn=chat, max_rounds=3, min_valid=3
    )
    assert len(rounds) == 3, "轮数封顶"
    assert len(client.searches) == 3
    assert all(entry.good < 3 for entry in rounds)
    assert result.candidates, "搜到的东西仍然要如实返回（只是不够好）"


# ------------------------------------------------------------------ 抓取（本地造的 tar.gz，不联网）

def make_tarball(files: dict[str, str]) -> bytes:
    """造一个 GitHub 形态的 tar.gz：顶层目录 `owner-repo-<sha>/`。"""
    import io
    import tarfile

    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, text in files.items():
            payload = text.encode("utf-8")
            info = tarfile.TarInfo(f"owner-repo-abc123/{name}")
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
    return buffer.getvalue()


class FakeRepoApi:
    def __init__(self, sha: str = "a" * 40, branch: str = "main") -> None:
        self.sha = sha
        self.branch = branch
        self.calls: list[str] = []

    def get(self, path: str, *, params: dict | None = None) -> object:
        self.calls.append(path)
        if path.endswith("/git/ref/heads/main"):
            return {"object": {"sha": self.sha}}
        if path.count("/") == 2:
            return {"default_branch": self.branch}
        return {}


def test_fetch_repo_extracts_records_commit_and_keeps_a_license_copy(scratch: Path) -> None:
    blob = make_tarball(
        {"core.py": "print('hi')\n", "LICENSE": "MIT License\n", "docs/readme.md": "# hi\n"}
    )
    target = scratch / "vendor" / "owner_repo"
    record = fetch_repo(
        "owner/repo",
        client=FakeRepoApi(),
        target=target,
        download=lambda url: blob,
        tarball_url="https://api.github.com/repos/owner/repo/tarball/x",
    )

    assert (target / "core.py").is_file(), "顶层目录必须被剥掉"
    assert (target / "docs" / "readme.md").is_file(), "子目录也要解出来"
    assert record["commit"] == "a" * 40, "必须记录 commit hash（引用要能指回具体版本）"
    assert record["files"] == 3
    assert Path(record["license_copy"]).is_file(), "LICENSE 要另存一份"
    assert "MIT" in Path(record["license_copy"]).read_text(encoding="utf-8")
    assert (target.parent / "owner_repo-FETCH.json").is_file(), "抓取记录要落盘"


def test_fetch_repo_refuses_an_empty_tarball(scratch: Path) -> None:
    import io
    import tarfile

    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz"):
        pass
    with pytest.raises(ScoutError, match="没有文件"):
        fetch_repo(
            "owner/repo",
            client=FakeRepoApi(),
            target=scratch / "vendor" / "x",
            download=lambda url: buffer.getvalue(),
            tarball_url="https://example.invalid/tarball",
        )


def test_extract_tarball_refuses_path_traversal(scratch: Path) -> None:
    """zip-slip：压缩包里带 `../` 的条目不许写到目标之外。"""
    import io
    import tarfile

    from src.scout import extract_tarball

    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        payload = b"evil"
        info = tarfile.TarInfo("owner-repo-abc/../escaped.txt")
        info.size = len(payload)
        archive.addfile(info, io.BytesIO(payload))
    destination = scratch / "vendor" / "safe"
    destination.mkdir(parents=True)
    written = extract_tarball(buffer.getvalue(), destination)
    assert written == 0
    assert not (scratch / "vendor" / "escaped.txt").exists()


def test_resolve_commit_asks_for_the_default_branch_first() -> None:
    from src.scout import resolve_commit

    client = FakeRepoApi()
    assert resolve_commit(client, "owner/repo") == "a" * 40
    assert client.calls[0].endswith("/repos/owner/repo")
