"""读 token 的**落点**规则（2026-09-12 人类实机安装逼出来的一条）。

人类把文件放进来时用的是**环境变量的名字**（`state/GH_READ_TOKEN`，没有 `.txt`），
于是"token 明明放好了"却报 `读 token 不可用：LookupError` —— 那时人只会怀疑 token 本身。

**凭证类失败的报错必须指向真原因**，所以候选落点里两个名字都认。这个文件把规则钉住：
以后有人"整理"这个函数、把不带后缀的那个名字删掉时，会立刻看到这条测试红。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.github import tokens


def test_read_token_accepts_the_name_without_dot_txt(scratch: Path, monkeypatch) -> None:
    monkeypatch.setattr(tokens, "STATE_DIR", scratch)
    monkeypatch.delenv(tokens.READ_TOKEN_ENV, raising=False)
    (scratch / "GH_READ_TOKEN").write_text("github_pat_例\n", encoding="utf-8")

    assert tokens.read_token() == "github_pat_例"
    assert "GH_READ_TOKEN" in tokens.read_token_source()


def test_read_token_prefers_the_dot_txt_name_when_both_exist(scratch: Path, monkeypatch) -> None:
    """两个都在时以带 `.txt` 的为准（先列先取）：候选顺序变了要在这里看见。"""
    monkeypatch.setattr(tokens, "STATE_DIR", scratch)
    monkeypatch.delenv(tokens.READ_TOKEN_ENV, raising=False)
    (scratch / "GH_READ_TOKEN.txt").write_text("带后缀的\n", encoding="utf-8")
    (scratch / "GH_READ_TOKEN").write_text("不带后缀的\n", encoding="utf-8")

    assert tokens.read_token() == "带后缀的"


def test_read_token_still_fails_loudly_when_nothing_is_placed(scratch: Path, monkeypatch) -> None:
    """一个都没有时必须抛（绝不返回空串带着空凭证去打 API）。"""
    monkeypatch.setattr(tokens, "STATE_DIR", scratch)
    monkeypatch.setenv("DSH_HOME", str(scratch / "no-such-home"))
    monkeypatch.delenv(tokens.READ_TOKEN_ENV, raising=False)
    with pytest.raises(Exception, match="读 token 不可用"):
        tokens.read_token()
