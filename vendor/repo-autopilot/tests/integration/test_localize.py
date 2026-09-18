"""4.1 文件定位的确定性测试（不联网、不调模型）。

钉死三类容易做错的事：

1. **文件树**：`.git` / `node_modules` / 缓存目录不进候选，二进制后缀不进候选；
2. **信号**：作者直接写出路径 > 文件名 > 测试名映射 > 正文命中符号；无关文件必须是 0 分；
3. **边界**（路线原文的两条硬约束）：
   - 候选 **≤5**，超了直接报错，不悄悄多给；
   - **禁止把整仓库喂给 flash**：全程只调 `local_small`，而且发给模型的提示词里
     **不能出现任何文件正文** —— 用"只存在于文件正文里的哨兵字符串"来证明这一点。
"""

from __future__ import annotations

import json

import numpy as np
import pytest

from src.localize import (
    LocalizeError,
    extract_tokens,
    lexical_scores,
    locate,
    profile_text,
    screen_paths,
    to_json,
    walk_repo,
)

SENTINEL = "SENTINEL_BODY_ONLY_9137"     # 只写在文件正文里，任何提示词里都不该出现


def zeros_embed(texts: list[str]) -> np.ndarray:
    return np.zeros((len(texts), 4), dtype=float)


def build_repo(scratch, files: dict[str, str]):
    for name, body in files.items():
        path = scratch / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    return scratch


class SpyChat:
    """记录每一次模型调用 —— 用来证明"没调 flash、没发正文"。"""

    def __init__(self, reply: dict | None = None) -> None:
        self.calls: list[dict] = []
        self.reply = reply if reply is not None else {"files": []}

    def __call__(self, messages, schema, tier, **kwargs):
        self.calls.append({"messages": messages, "schema": schema, "tier": tier})
        return self.reply

    @property
    def prompt(self) -> str:
        return "\n".join(str(message.get("content", "")) for call in self.calls for message in call["messages"])


# ------------------------------------------------------------------ 文件树

def test_walk_repo_skips_junk_and_binaries(scratch) -> None:
    build_repo(
        scratch,
        {
            "app/main.py": "def main(): pass\n",
            "app/util.js": "export const x = 1\n",
            "node_modules/dep/index.js": "module.exports = {}\n",
            ".git/config": "[core]\n",
            "__pycache__/main.cpython-312.pyc": "junk",
            "assets/logo.png": "PNG",
            "docs/readme.md": "# hi\n",
        },
    )
    paths = [entry.path for entry in walk_repo(scratch)]
    assert paths == ["app/main.py", "app/util.js", "docs/readme.md"]


def test_walk_repo_rejects_missing_directory(scratch) -> None:
    with pytest.raises(LocalizeError, match="不存在"):
        walk_repo(scratch / "nope")


# ------------------------------------------------------------------ 信号

def test_extract_tokens_picks_paths_tests_and_symbols() -> None:
    tokens = extract_tokens(
        "Money.parse 在处理 '1,234.5' 时抛异常，见 ledger/money.py，"
        "另外 tests/test_cli.py 里的用例也挂了。the issue is not a bug"
    )
    assert "ledger/money.py" in tokens["paths"]
    assert any(name.endswith("money.py") for name in tokens["paths"])
    assert tokens["test_names"] == ["test_cli"]
    assert "Money" in tokens["identifiers"]
    assert "the" not in tokens["identifiers"], "停用词不该进信号"


def test_lexical_scores_prefer_explicit_paths(scratch) -> None:
    build_repo(
        scratch,
        {
            "ledger/money.py": "class Money:\n    def parse(cls, text): pass\n",
            "ledger/report.py": "def to_csv(ledger): pass\n",
            "ledger/store.py": "class Ledger:\n    pass\n",
        },
    )
    entries = walk_repo(scratch)
    tokens = extract_tokens("账本里的金额解析出错，见 ledger/money.py")
    scores = lexical_scores(scratch, entries, tokens)
    assert scores["ledger/money.py"][0] >= 100
    assert "ledger/report.py" not in scores, "没被提到的文件不该有分"


def test_lexical_scores_map_test_names_to_source(scratch) -> None:
    build_repo(
        scratch,
        {
            "ledger/money.py": "def parse(text): pass\n",
            "tests/test_money.py": "def test_parse_whole_number(): pass\n",
        },
    )
    entries = walk_repo(scratch)
    tokens = extract_tokens("tests/test_money.py::test_parse_whole_number 现在挂了")
    scores = lexical_scores(scratch, entries, tokens)
    assert "ledger/money.py" in scores
    assert any("测试名" in reason or "测试" in reason for reason in scores["ledger/money.py"][1])


# ------------------------------------------------------------------ 主流程边界

def test_locate_returns_at_most_five_sorted_candidates(scratch) -> None:
    build_repo(scratch, {f"pkg/mod{i}.py": f"def fn{i}(): pass\n" for i in range(8)})
    candidates = locate("pkg/mod3.py 里的 fn3 出错", scratch, embed_fn=zeros_embed, use_model=False)
    assert len(candidates) <= 5
    assert [item.score for item in candidates] == sorted((item.score for item in candidates), reverse=True)
    assert all(item.why for item in candidates), "每个候选都要能解释『为什么是它』"


def test_locate_refuses_more_than_five(scratch) -> None:
    build_repo(scratch, {"a.py": "pass\n"})
    with pytest.raises(LocalizeError, match="最多 5"):
        locate("随便", scratch, top_k=6, embed_fn=zeros_embed, use_model=False)


def test_locate_never_calls_flash_and_never_sends_file_bodies(scratch) -> None:
    """
    两条路线硬约束的**同一份证据**：
    模型只被调用在 `local_small` 档，且提示词里只有路径、没有正文。
    """
    build_repo(
        scratch,
        {f"pkg/mod{i}.py": f"# {SENTINEL}\ndef fn{i}(): pass\n" for i in range(25)},
    )
    spy = SpyChat({"files": ["pkg/mod3.py", "pkg/mod7.py"]})
    candidates = locate("pkg/mod3.py 里的 fn3 出错", scratch, chat_fn=spy, embed_fn=zeros_embed)

    assert spy.calls, "文件数超过候选池时应该走一次本地模型初筛"
    assert {call["tier"] for call in spy.calls} == {"local_small"}, "绝不能调 flash 档"
    assert SENTINEL not in spy.prompt, "提示词里不允许出现文件正文"
    assert "pkg/mod3.py" in spy.prompt, "提示词里应该只有路径清单"
    assert candidates and candidates[0].path in {"pkg/mod3.py", "pkg/mod7.py"}


def test_screen_paths_drops_paths_the_model_invented(scratch) -> None:
    listing = ["a.py", "b.py"]
    spy = SpyChat({"files": ["a.py", "ghost/made-up.py"]})
    picked = screen_paths(listing, "issue", chat_fn=spy)
    assert picked == ["a.py"], "模型编出来的路径必须被丢掉"


def test_screen_paths_survives_a_broken_model(scratch) -> None:
    """初筛只是省成本：它挂了不该让整条定位链路挂。"""

    def boom(*args, **kwargs):
        raise RuntimeError("模型没起来")

    assert screen_paths(["a.py"], "issue", chat_fn=boom) == []


def test_profile_text_is_bounded_and_has_symbols(scratch) -> None:
    body = "class Thing:\n    def method(self): pass\n" + "\n".join(
        f"# filler {i}" for i in range(200)
    )
    build_repo(scratch, {"pkg/thing.py": body})
    entry = walk_repo(scratch)[0]
    profile = profile_text(scratch, entry)
    assert "Thing" in profile and "method" in profile, "符号名必须进档案文本"
    assert "filler 199" not in profile, "档案文本不该把整个文件塞进去"


def test_to_json_is_machine_readable(scratch) -> None:
    build_repo(scratch, {"a.py": "def fn(): pass\n"})
    candidates = locate("a.py 里的 fn", scratch, embed_fn=zeros_embed, use_model=False)
    payload = json.loads(to_json(candidates))
    assert isinstance(payload, list) and payload
    assert {"path", "score", "lexical", "embedding", "why"} <= set(payload[0])
