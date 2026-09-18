# 步骤 1.1 验收测试
#
# 路线要求：`pytest tests/unit/test_skeleton.py` —— 断言目录树完整、progress.md 格式合法。
#
# 本测试用真实断言，不 mock 目录结构本身：它检查的是磁盘上的实际状态。
# 按 TEST_GATE：本文件的断言只允许收紧，不允许为了让某次运行变绿而放宽。

from __future__ import annotations

import re
from pathlib import Path

import pytest

# repo-autopilot 根目录（本文件位于 tests/unit/ 下）
ROOT = Path(__file__).resolve().parents[2]

# 路线 1.1 子步骤 2 规定的目录树，逐条列出以便失败时能指名道姓
REQUIRED_DIRS = [
    "state/tasks/pending",
    "state/tasks/doing",
    "state/tasks/done",
    "state/tasks/failed",
    "state/specs",
    "state/reports",
    "state/patches",
    "state/outbox",
    "state/approvals",
    "state/vendor",
    "state/vectors",
    "src/queue",
    "src/gateway",
    "src/github",
    "src/sandbox",
    "src/triage",
    "src/repair",
    "src/scout",
    "src/scaffold",
    "tests/unit",
    "tests/integration",
    "tests/e2e",
    "tests/corpus",
    "config",
]

# 路线 1.1 子步骤 3 规定的三个状态文件
REQUIRED_FILES = [
    "state/progress.md",
    "state/mode.json",
    "state/capabilities.yaml",
    "config/models.yaml",
    ".gitignore",
]

VALID_STATUSES = {"PASS", "BLOCKED", "DOING", "TODO"}


@pytest.mark.parametrize("rel", REQUIRED_DIRS)
def test_required_directory_exists(rel: str) -> None:
    path = ROOT / rel
    assert path.is_dir(), f"缺少目录: {rel}"


@pytest.mark.parametrize("rel", REQUIRED_FILES)
def test_required_file_exists(rel: str) -> None:
    path = ROOT / rel
    assert path.is_file(), f"缺少文件: {rel}"


def test_progress_md_has_table_header() -> None:
    """progress.md 必须是一张含表头的表格——它是要被脚本写入的，不是散文。"""
    text = (ROOT / "state/progress.md").read_text(encoding="utf-8")
    assert "| 步骤 |" in text, "progress.md 缺少表头行"
    assert "| 状态 |" in text, "progress.md 表头缺少状态列"
    assert "| 证据 |" in text, "progress.md 表头缺少证据列"


def test_progress_md_statuses_are_legal() -> None:
    """
    每一行的状态列只能是四种合法值之一。

    存在的意义：路线 0.2 禁止"基本完成"这类中间态。若有人手工往表里写
    "基本完成"，这条断言会当场拒绝——这就是把约定变成代码。
    """
    text = (ROOT / "state/progress.md").read_text(encoding="utf-8")
    rows = [ln for ln in text.splitlines() if ln.startswith("|") and not re.match(r"^\|\s*-+", ln)]
    # 去掉表头行
    rows = [ln for ln in rows if "| 步骤 |" not in ln]
    assert rows, "progress.md 里没有步骤行"

    for row in rows:
        cells = [c.strip() for c in row.strip("|").split("|")]
        assert len(cells) >= 3, f"列数不足: {row}"
        status = cells[2]
        assert status in VALID_STATUSES, (
            f"非法状态 {status!r}（只允许 {'/'.join(sorted(VALID_STATUSES))}）: {row}"
        )


def test_gitignore_covers_write_token() -> None:
    """
    路线 1.1 验收明确要求：.gitignore 生效，git status 不出现 .write_token。

    这里直接检查规则文本（不依赖 git 是否可用），git 层面的验证在集成测试里。
    """
    text = (ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "state/.write_token" in text, ".gitignore 未忽略写 token"


def test_capabilities_yaml_has_five_required_keys() -> None:
    """
    路线 0.1 验收：五项探测全部有明确 true/false 结论并落盘。

    用 PyYAML 真解析，而不是子串匹配——子串匹配在键改名后会假通过。
    """
    yaml = pytest.importorskip("yaml")
    data = yaml.safe_load((ROOT / "state/capabilities.yaml").read_text(encoding="utf-8"))
    assert isinstance(data, dict), "capabilities.yaml 顶层不是映射"

    for key in (
        "github_write",
        "local_model_reachable",
        "cron_available",
        "persistent_fs",
        "docker_available",
    ):
        assert key in data, f"capabilities.yaml 缺少必需键: {key}"
        assert isinstance(data[key], bool), f"{key} 必须是布尔值，实际是 {type(data[key]).__name__}"


def test_mode_json_is_online_initially() -> None:
    """路线 1.1 子步骤 3：初始化 state/mode.json: {"mode":"online"}"""
    import json

    data = json.loads((ROOT / "state/mode.json").read_text(encoding="utf-8"))
    assert data.get("mode") in {"online", "offline"}, f"mode 非法: {data.get('mode')}"
    assert data["mode"] == "online", "初始模式应为 online"
