"""给自己建仓（`tools/self_repo.py`）的确定性测试：**清单一致 + 幂等判断算得对**。

这个工具会真的往 GitHub 写，所以它的"不写"部分必须被测住：

1. `git_blob_sha` 必须与 git 自己算的一致 —— 幂等判断（"远端这个文件是不是已经一模一样"）
   全靠它；算错了会导致"每次都重复提交"或"该提交的没提交"；
2. 推上去的文件清单必须与 `tools/package.py` **同一份**（"装到别的机器的包"与
   "推到 GitHub 的仓库"内容一致），否则两边会各说各话；
3. 建仓动作必须在闸门白名单里（`create_repo`）—— 不在白名单就说明有人绕过了闸门。
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def load_module(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_blob_sha_matches_git_hash_object() -> None:
    """自己算的 blob sha 必须与 `git hash-object` 逐位一致（幂等判断的地基）。"""
    module = load_module("self_repo_sha", "tools/self_repo.py")
    for relative in ("README.md", "src/gateway/gateway.py", "tools/acceptance.py"):
        path = ROOT / relative
        expected = subprocess.run(
            ["git", "hash-object", relative], cwd=ROOT, capture_output=True, text=True, check=False
        ).stdout.strip()
        assert module.git_blob_sha(path.read_bytes()) == expected, relative


def test_plan_is_shared_with_the_packager() -> None:
    """推给 GitHub 的清单 = 打包用的清单（同一份规则，不各说各话）。"""
    self_repo = load_module("self_repo_plan", "tools/self_repo.py")
    package = load_module("self_repo_package", "tools/package.py")
    files, binary = self_repo.collect()
    package_files, _notes = package.plan()
    assert {path for path, _text in files} | set(binary) == {
        path.relative_to(ROOT).as_posix() for path in package_files
    }


def test_collect_returns_text_and_reports_binary_separately() -> None:
    self_repo = load_module("self_repo_collect", "tools/self_repo.py")
    files, binary = self_repo.collect()
    assert files, "至少要有源码文件"
    # `.gitkeep` 是**空文件**（占位用），不是"内容丢了"：这里只要求它们能被当文本读，
    # 不要求非空 —— 早先那条 `text != ""` 的断言把骨架占位符误判成异常（踩过）。
    for path, text in files:
        assert isinstance(text, str), path
        assert text != "" or path.endswith(".gitkeep"), f"{path} 不该是空文件"
    assert all(not path.endswith(".pyc") for path in binary)


def test_create_repo_is_a_gated_action() -> None:
    """建仓是写操作：动作名必须在闸门白名单里，否则就是绕闸门。"""
    from src.github.approval import WRITE_ACTIONS

    assert "create_repo" in WRITE_ACTIONS


# ==================================================== CI 核对（2026-09-15 人类补上 Actions: Read）

class FakeCI:
    """只实现 `verify_ci` 用到的两个只读端点。"""

    def __init__(self, head: str, runs: list[dict], *, boom: bool = False) -> None:
        self.head = head
        self.runs = runs
        self.boom = boom

    def get(self, path: str, params: dict | None = None) -> dict:
        if self.boom:
            raise RuntimeError("HTTP 403: Resource not accessible by personal access token")
        if path.endswith("/commits/main"):
            return {"sha": self.head}
        if "/actions/runs" in path:
            return {"workflow_runs": self.runs}
        raise AssertionError(f"没预料到的路径：{path}")


def ci_run(sha: str, *, conclusion: str | None = "success", status: str = "completed", number: int = 1) -> dict:
    return {
        "head_sha": sha,
        "status": status,
        "conclusion": conclusion,
        "run_number": number,
        "name": "gate",
    }


def test_verify_ci_passes_when_the_current_head_is_green() -> None:
    module = load_module("self_repo_ci_ok", "tools/self_repo.py")
    client = FakeCI("a" * 40, [ci_run("a" * 40), ci_run("b" * 40)])
    assert module.verify_ci(client, full_name="o/r") == 0


def test_verify_ci_refuses_to_call_an_older_green_run_a_pass() -> None:
    """
    **这条是这一格曾经空着的真正原因**：只报"最近一次运行是绿的"不作数 ——
    整仓同步每次新建提交，在新提交跑完之前，列表里最新的仍是**上一个提交**的绿。
    所以必须核对 `head_sha`，对不上就是"没跑"（返回 2），不是"通过"。
    """
    module = load_module("self_repo_ci_stale", "tools/self_repo.py")
    client = FakeCI("c" * 40, [ci_run("b" * 40)])          # 绿的是上一个提交
    assert module.verify_ci(client, full_name="o/r") == 2


def test_verify_ci_reports_a_failure() -> None:
    module = load_module("self_repo_ci_fail", "tools/self_repo.py")
    client = FakeCI("a" * 40, [ci_run("a" * 40, conclusion="failure")])
    assert module.verify_ci(client, full_name="o/r") == 1


def test_verify_ci_waits_rather_than_guessing_while_a_run_is_in_progress() -> None:
    module = load_module("self_repo_ci_pending", "tools/self_repo.py")
    client = FakeCI("a" * 40, [ci_run("a" * 40, conclusion=None, status="in_progress")])
    assert module.verify_ci(client, full_name="o/r") == 2


def test_verify_ci_requires_every_workflow_on_the_head_to_be_green() -> None:
    module = load_module("self_repo_ci_multi", "tools/self_repo.py")
    client = FakeCI(
        "a" * 40,
        [ci_run("a" * 40, number=1), ci_run("a" * 40, conclusion="failure", number=2)],
    )
    assert module.verify_ci(client, full_name="o/r") == 1, "有一个工作流没绿就不算绿"


def test_verify_ci_says_it_cannot_read_instead_of_pretending(capsys) -> None:
    """读不到（缺 Actions: Read）时返回 2 并说清原因 —— 不是 0，也不是崩。"""
    module = load_module("self_repo_ci_403", "tools/self_repo.py")
    assert module.verify_ci(FakeCI("a" * 40, [], boom=True), full_name="o/r") == 2
    assert "Actions: Read" in capsys.readouterr().out
