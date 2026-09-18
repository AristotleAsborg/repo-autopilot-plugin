"""6.1 脚手架的确定性测试（不联网、不调模型）。

钉死四件事：

1. **生成物必须齐件**：缺 LICENSE/CI/测试/`.gitignore` 一律算缺件，并且要**带着缺件清单重试一次**；
2. **名字只认候选与序号**：仓库名要写进 URL，猜错比问第二次贵；
3. **人类只做两次决策**：选名（`choose_name`）与批准建仓（闸门）；其余全自动；
4. **本地 CI 干跑是真的**：真 pytest + 真 ruff 跑一遍手工写的合法骨架（证明这套契约可满足）。

另外验 REST 细节：建仓用 `auto_init=True`（否则 contents API 无处落文件）、
首推幂等、CI 轮询**有超时且不把"还在跑"说成"绿"**。
"""

from __future__ import annotations

import base64
import sys
from pathlib import Path

import pytest

from src.scaffold import (
    GeneratedFile,
    Scaffold,
    ScaffoldError,
    choose_name,
    ci_commands,
    create_repository,
    dry_run,
    first_push,
    generate,
    latest_ci,
    name_candidates,
    slugify,
    validate_files,
    wait_for_ci,
    write_scaffold,
)

MIT_TEXT = "MIT License\n\nCopyright (c) 2026 repo-autopilot\n\nPermission is hereby granted, free of charge...\n"

MINIMAL = {
    "pyproject.toml": (
        "[project]\nname = \"demo\"\nversion = \"0.1.0\"\nrequires-python = \">=3.10\"\n\n"
        "[tool.pytest.ini_options]\npythonpath = [\".\"]\n\n"
        "[tool.ruff]\nline-length = 100\n\n[tool.ruff.lint]\nselect = [\"E4\", \"E7\", \"E9\", \"F\"]\n"
    ),
    "demo/__init__.py": "def double(value: int) -> int:\n    return value * 2\n",
    "tests/test_demo.py": "from demo import double\n\n\ndef test_double() -> None:\n    assert double(2) == 4\n",
    "README.md": "# demo\n\n怎么装、怎么跑测试。\n",
    "LICENSE": MIT_TEXT,
    ".gitignore": "__pycache__/\n",
    # CI 工作流会跑它，所以齐件校验要求它存在（2026-09-17 A7：缺了它云端 CI 必红）
    "scripts/check_blacklist.py": (
        "import sys\n"
        "print('[blacklist] OK')\n"
        "sys.exit(0)\n"
    ),
    ".github/workflows/ci.yml": "name: ci\non: [push]\njobs:\n  t:\n    runs-on: ubuntu-latest\n    steps:\n      - run: echo hi\n",
}


def files_from(mapping: dict[str, str]) -> list[GeneratedFile]:
    return [GeneratedFile(path, text) for path, text in mapping.items()]


# ------------------------------------------------------------------ 名字

def test_slugify_drops_non_ascii_and_collapses_dashes() -> None:
    assert slugify("Hello, World!! 你好") == "hello-world"
    assert slugify("a--b") == "a-b"


def test_name_candidates_are_deduped_and_avoid_reserved_words() -> None:
    candidates = name_candidates("build a tiny ledger CLI for books", count=3)
    assert len(candidates) == 3
    assert len(set(candidates)) == 3
    assert all(candidate not in {"test", "app", "main"} for candidate in candidates)


def test_choose_name_accepts_the_name_or_its_index() -> None:
    candidates = ["alpha-tool", "beta-tool", "gamma"]
    assert choose_name("alpha-tool", candidates) == "alpha-tool"
    assert choose_name("2", candidates) == "beta-tool"


@pytest.mark.parametrize("reply", ["", None, "9", "随便"])
def test_choose_name_refuses_anything_else(reply: str | None) -> None:
    with pytest.raises(ScaffoldError):
        choose_name(reply, ["alpha-tool", "beta-tool", "gamma"])


def test_choose_name_accepts_a_custom_slug() -> None:
    """人类想自己起名是常见情况 —— 只要它是合法 slug 就接受。"""
    assert choose_name("My Shiny Tool!", ["alpha-tool", "beta-tool", "gamma"]) == "my-shiny-tool"


# ------------------------------------------------------------------ 校验与落盘

def test_validate_files_accepts_a_complete_scaffold() -> None:
    assert validate_files(files_from(MINIMAL)) == []


@pytest.mark.parametrize(
    ("missing", "keyword"),
    [
        ("LICENSE", "LICENSE"),
        (".github/workflows/ci.yml", "CI 工作流"),
        (".gitignore", ".gitignore"),
        ("tests/test_demo.py", "测试"),
    ],
)
def test_validate_files_reports_missing_pieces(missing: str, keyword: str) -> None:
    mapping = {path: text for path, text in MINIMAL.items() if path != missing}
    problems = validate_files(files_from(mapping))
    assert any(keyword in problem for problem in problems), problems


def test_validate_files_rejects_illegal_paths_and_non_mit() -> None:
    mapping = dict(MINIMAL)
    mapping["../evil.py"] = "x = 1\n"
    mapping["LICENSE"] = "All rights reserved, no license.\n"
    problems = validate_files(files_from(mapping))
    assert any("非法路径" in problem for problem in problems)
    assert any("MIT" in problem for problem in problems)


def test_validate_files_rejects_a_test_file_without_cases() -> None:
    mapping = dict(MINIMAL)
    mapping["tests/test_demo.py"] = "# 没有用例\n"
    assert any("没有 test_" in problem for problem in validate_files(files_from(mapping)))


def test_validate_files_encodes_the_environment_constraints() -> None:
    """
    三条**环境约束**由实测换来（生成物上传后第一次 CI 必红是最贵的失败）：
    pytest 找不到包、ruff 规则漂移、用例用系统 temp 目录（沙箱直接 PermissionError）。
    """
    mapping = dict(MINIMAL)
    mapping["pyproject.toml"] = "[project]\nname = \"demo\"\n"
    mapping["tests/test_demo.py"] = (
        "from pathlib import Path\n\n\n"
        "def test_x(tmp_path: Path) -> None:\n    (tmp_path / 'a').write_text('x')\n"
    )
    problems = validate_files(files_from(mapping))
    assert any("pythonpath" in problem for problem in problems), problems
    assert any("tool.ruff" in problem for problem in problems), problems
    assert any("系统临时目录" in problem for problem in problems), problems


def test_generate_until_green_feeds_failures_back(scratch: Path) -> None:
    """第一轮本地 CI 红 → 把失败输出喂回去 → 第二轮绿。这是"不许带着红骨架往下走"的实现。"""
    from src.scaffold import generate_until_green

    rounds: list[list[dict]] = []

    def chat(messages, schema):
        rounds.append(messages)
        return {"name": "demo", "summary": "s", "rationale": "r",
                "files": [{"path": p, "content": t} for p, t in MINIMAL.items()]}

    calls = {"n": 0}

    def runner(command, cwd):
        calls["n"] += 1
        # 第一轮的两条命令都红，之后全绿
        return (1, "F401 unused import") if calls["n"] <= 2 else (0, "9 passed")

    _scaffold, dry, directory = generate_until_green(
        "一句 idea", chat_fn=chat, runner=runner, root=scratch
    )
    assert dry["passed"] is True
    assert len(rounds) == 2
    assert "F401" in rounds[1][-1]["content"], "重试时必须把本地 CI 的真实失败喂回去"
    assert (directory / "pyproject.toml").is_file()


def test_generate_until_green_keeps_the_spec_context_on_retry_rounds(scratch: Path) -> None:
    """
    **P1-13 的回归**（2026-09-17 总报告）：`extra_context` 原来**没有这个参数**，
    而重试轮的 `context` 又会被失败信息**整个覆盖** —— 于是模型第 2 轮"只修失败"时
    **看不到需求**，可能修得对得上 CI、对不上 spec。现在：基础上下文每轮都在，
    失败清单从第 2 轮起追加。
    """
    from src.scaffold import generate_until_green

    rounds: list[list[dict]] = []

    def chat(messages, schema):
        rounds.append(messages)
        return {"name": "demo", "summary": "s", "rationale": "r",
                "files": [{"path": p, "content": t} for p, t in MINIMAL.items()]}

    calls = {"n": 0}

    def runner(command, cwd):
        calls["n"] += 1
        return (1, "F401 unused import") if calls["n"] <= 2 else (0, "9 passed")

    generate_until_green(
        "一句 idea",
        chat_fn=chat,
        runner=runner,
        root=scratch,
        extra_context="SPEC 要点：命令行入口必须支持 --rule 参数。",
    )
    assert len(rounds) == 2
    for index, messages in enumerate(rounds, start=1):
        blob = "\n".join(item["content"] for item in messages)
        assert "--rule" in blob, f"第 {index} 轮丢了 spec 上下文：{blob[:200]}"
    # 第 1 轮不该有失败清单，第 2 轮必须有（两条不能互相顶掉）
    assert "本地 CI 跑出来是红的" not in "\n".join(i["content"] for i in rounds[0])
    assert "本地 CI 跑出来是红的" in "\n".join(i["content"] for i in rounds[1])


def test_generate_keeps_extra_context_across_its_own_retries(scratch: Path) -> None:
    """`generate()` 内部也有重试（缺件清单）：那里的上下文同样不许被丢掉。"""
    from src.scaffold import generate

    prompts: list[str] = []

    def chat(messages, schema):
        prompts.append("\n".join(item["content"] for item in messages))
        # 第 1 次故意漏 .gitignore（触发它的内部重试），第 2 次给全
        files = MINIMAL if len(prompts) > 1 else {k: v for k, v in MINIMAL.items() if k != ".gitignore"}
        return {"name": "demo", "summary": "s", "rationale": "r",
                "files": [{"path": p, "content": t} for p, t in files.items()]}

    generate("一句 idea", chat_fn=chat, extra_context="SPEC 要点：模型档是 flash。")
    assert len(prompts) == 2, prompts
    assert all("模型档是 flash" in text for text in prompts), "重试那一次也要看得到上下文"


def test_name_candidates_covers_chinese_ideas_instead_of_silently_returning_one() -> None:
    """
    **P1-14 的回归**（2026-09-17 总报告）：中文 idea 抽不出英文词，
    没有 keywords 时只给出**一个**候选（实测只有 `trpg`），而承诺是三选一。
    现在用角色后缀派生出真实可选的三个，并把"裸词撞 PyPI"的风险显式说出来。
    """
    from src.scaffold import name_candidate_notes, name_candidates

    idea = "需求一个用于 trpg 的数值计算脚本"
    candidates = name_candidates(idea)
    assert len(candidates) == 3, candidates
    assert candidates[0] == "trpg"
    assert all(name == "trpg" or name.startswith("trpg-") for name in candidates), candidates

    notes = name_candidate_notes(idea, candidates)
    assert any("PyPI" in note for note in notes), "裸词候选必须提示撞名风险"
    # 英文 idea 不该多嘴
    english = name_candidates("a markdown table to csv converter")
    assert name_candidate_notes("a markdown table to csv converter", english) == []


def test_name_candidate_notes_say_so_when_keywords_are_missing() -> None:
    """给不出三个候选时要**明说原因与补救**，而不是安静地少给。"""
    from src.scaffold import name_candidate_notes

    notes = name_candidate_notes("纯中文的一句话想法", ["duihua"])
    assert any("keywords" in note for note in notes), notes
    assert any("只" in note or "候选名只有" in note for note in notes), notes


def test_owner_exists_distinguishes_a_missing_owner_from_a_free_name() -> None:
    """
    **R6 的回归**（2026-09-17 总报告把它列为"未验证"）：实测确认
    `is_name_available()` 对**不存在的 owner** 也返回 `True`（GitHub 对"仓库不存在"
    与"owner 不存在"都给 404）—— 一个打错的 `--owner` 会让所有候选名显示"可用"，
    直到建库那一刻才 404 失败。`owner_exists()` 把这两件事分开。
    """
    from src.scaffold import is_name_available, owner_exists

    class Fake:
        def __init__(self, missing: set[str]) -> None:
            self.missing = missing

        def get(self, path, params=None):
            if path in self.missing:
                raise RuntimeError(f"GET {path} -> HTTP 404: Not Found")
            return {"ok": True}

    client = Fake({"/users/ghost", "/repos/ghost/whatever"})
    assert owner_exists(client, "ghost") is False
    assert owner_exists(client, "real") is True
    # 关键：owner 不存在时 is_name_available 仍说"可用" —— 所以调用方必须自己查 owner
    assert is_name_available(client, "ghost", "whatever") is True


def test_init_local_repository_saves_the_project_without_any_github(scratch: Path) -> None:
    """
    **GitHub 可选**（2026-09-18 人类要求）：这条链路里真正有价值的部分
    （追问 → spec → 骨架 → 本地 CI 干跑）本来就不需要联网 ——
    强制要 GitHub 写权限，等于把「想用这套东西」变成「先交出权限」。
    所以默认必须能**只把项目保存在本地**。
    """
    import subprocess

    from src.scaffold import init_local_repository

    work = scratch / "demo"
    work.mkdir()
    (work / "pyproject.toml").write_text("[project]\nname = 'demo'\n", encoding="utf-8")
    (work / "README.md").write_text("# demo\n", encoding="utf-8")

    saved = init_local_repository(work, message="init")
    assert saved["committed"] is True and saved["commit"], saved
    assert saved["branch"] == "main"
    assert "没有创建任何远端仓库" in saved["note"], "要明确告诉人：这一步没碰远端"

    log = subprocess.run(["git", "log", "--oneline"], cwd=work, capture_output=True, text=True, check=False)
    assert log.returncode == 0 and "init" in log.stdout, log
    tracked = subprocess.run(["git", "ls-files"], cwd=work, capture_output=True, text=True, check=False)
    assert "pyproject.toml" in tracked.stdout and "README.md" in tracked.stdout

    again = init_local_repository(work)
    assert again["committed"] is False and again["commit"] == saved["commit"], again
    assert "幂等" in again["note"], again


def test_init_local_repository_fails_loudly_on_a_missing_directory(scratch: Path) -> None:
    """目录不在就响亮报错 —— 不许让人以为「存下来了」。"""
    from src.scaffold import ScaffoldError, init_local_repository

    with pytest.raises(ScaffoldError, match="目录不存在"):
        init_local_repository(scratch / "not-here")


def test_local_mode_is_reachable_from_the_driver() -> None:
    """`/建新项目` 的驱动要有「本地模式」这个开关（不建远端仓库、不首推、不轮询 CI）。"""
    source = (Path(__file__).resolve().parents[2] / "tools" / "eval_scaffold.py").read_text(encoding="utf-8")
    assert '"--local"' in source
    assert "init_local_repository" in source
    local_block = source.index("if args.local:")
    assert source.index("create_repository(") > local_block, "本地模式的分支必须排在远端建仓之前"
    assert source.index("first_push(") > local_block


def test_generate_until_green_gives_up_loudly(scratch: Path) -> None:
    from src.scaffold import generate_until_green

    def chat(messages, schema):
        return {"name": "demo", "summary": "s", "rationale": "r",
                "files": [{"path": p, "content": t} for p, t in MINIMAL.items()]}

    with pytest.raises(ScaffoldError, match="本地 CI 始终不过"):
        generate_until_green("一句 idea", chat_fn=chat, runner=lambda c, d: (1, "boom"), root=scratch, max_rounds=2)


def test_write_scaffold_clears_stale_files(scratch: Path) -> None:
    scaffold = Scaffold("idea", "demo", "s", "r", files_from(MINIMAL))
    root = scratch / "out"
    (root / "demo").mkdir(parents=True)
    (root / "demo" / "stale.txt").write_text("old", encoding="utf-8")

    written = write_scaffold(scaffold, root)
    assert (written / "demo" / "__init__.py").is_file()
    assert not (written / "stale.txt").exists(), "旧残留必须被清掉，不能混进新项目"


# ------------------------------------------------------------------ 生成

def test_generate_retries_once_with_the_problem_list() -> None:
    calls: list[list[dict]] = []

    def chat(messages, schema):
        calls.append(messages)
        if len(calls) == 1:
            incomplete = {k: v for k, v in MINIMAL.items() if k != ".gitignore"}
            return {"name": "demo", "summary": "s", "rationale": "r",
                    "files": [{"path": p, "content": t} for p, t in incomplete.items()]}
        return {"name": "demo", "summary": "s", "rationale": "r",
                "files": [{"path": p, "content": t} for p, t in MINIMAL.items()]}

    scaffold = generate("一句 idea", chat_fn=chat)
    assert scaffold.name == "demo"
    assert len(calls) == 2
    assert ".gitignore" in calls[1][-1]["content"], "重试时必须带上缺件清单"


def test_generate_gives_up_with_the_problem_list() -> None:
    def chat(messages, schema):
        return {"name": "demo", "summary": "s", "rationale": "r",
                "files": [{"path": "README.md", "content": "# hi\n"}]}

    with pytest.raises(ScaffoldError, match="缺件"):
        generate("一句 idea", chat_fn=chat, max_attempts=2)


# ------------------------------------------------------------------ CI 干跑

def test_ci_commands_include_the_blacklist_step_when_the_script_exists(scratch: Path) -> None:
    """
    契约：**本地干跑要跑 CI 会跑的东西**。

    2026-09-17 实测（观察台账 A7）：本地只跑 pytest+ruff，而生成的 CI 还跑
    `scripts/check_blacklist.py`，于是"本地全绿、首推后云端红"——
    首个真实生成的仓库就是这样红在第一推上。
    """
    (scratch / "scripts").mkdir(parents=True, exist_ok=True)
    (scratch / "scripts" / "check_blacklist.py").write_text("raise SystemExit(0)\n", encoding="utf-8")

    rendered = [" ".join(command) for command in ci_commands(sys.executable, scratch)]
    assert any("check_blacklist.py" in item for item in rendered), rendered


def test_ci_commands_skip_the_blacklist_step_when_it_is_absent(scratch: Path) -> None:
    """没有那个脚本就不该跑它（条件命令，与 CI 里的 `if [ -f … ]` 同语义）。"""
    rendered = [" ".join(command) for command in ci_commands(sys.executable, scratch)]
    assert not any("check_blacklist.py" in item for item in rendered), rendered


def test_dry_run_aggregates_command_results(scratch: Path) -> None:
    (scratch / "x.py").write_text("x = 1\n", encoding="utf-8")
    (scratch / "scripts").mkdir(parents=True, exist_ok=True)
    (scratch / "scripts" / "check_blacklist.py").write_text("raise SystemExit(0)\n", encoding="utf-8")
    plan = iter([(0, "5 passed"), (0, "[blacklist] OK"), (1, "Found 3 errors.")])

    result = dry_run(scratch, runner=lambda command, cwd: next(plan))
    assert result["passed"] is False
    assert [item["exit"] for item in result["results"]] == [0, 0, 1]


def test_real_dry_run_passes_on_a_handwritten_scaffold(scratch: Path) -> None:
    """真 pytest + 真 ruff：证明"生成物能跑"这条契约是可满足的。"""
    scaffold = Scaffold("idea", "demo", "s", "r", files_from(MINIMAL))
    root = write_scaffold(scaffold, scratch)
    result = dry_run(root, python=sys.executable)
    tails = " | ".join(item["tail"][-200:] for item in result["results"])
    assert result["passed"] is True, tails


# ------------------------------------------------------------------ REST

class FakeScaffoldClient:
    def __init__(self, *, repo_status: int = 201, ref_status: int = 422, runs: list[dict] | None = None) -> None:
        self.repo_status = repo_status
        self.ref_status = ref_status
        self.runs = runs if runs is not None else []
        self.calls: list[tuple[str, str, dict | None]] = []

    def get(self, path: str, *, params: dict | None = None) -> object:
        self.calls.append(("GET", path, params))
        if path.endswith("/actions/runs"):
            return {"workflow_runs": self.runs}
        if "/contents/" in path:
            return {"sha": "file-sha"}
        return {"object": {"sha": "base-sha"}}

    def request(self, method: str, path: str, *, params=None, body=None, base_url=None):
        self.calls.append((method, path, body))

        class Response:
            def __init__(self, status: int, body: object) -> None:
                self.status = status
                self.body = body
                self.url = "https://api.github.com/x"

        if method == "POST" and path == "/user/repos":
            if self.repo_status >= 400:
                return Response(self.repo_status, {"message": "name already exists"})
            return Response(self.repo_status, {"full_name": "owner/demo", "html_url": "u", "default_branch": "main"})
        if method == "POST" and path.endswith("/git/refs"):
            return Response(self.ref_status, {"message": "Reference already exists"})
        if method == "PUT":
            return Response(201, {"commit": {"sha": "c"}})
        return Response(200, {})


def test_create_repository_asks_for_auto_init() -> None:
    client = FakeScaffoldClient()
    info = create_repository(client, "demo", description="d")
    assert info["full_name"] == "owner/demo"
    body = next(body for method, path, body in client.calls if method == "POST" and path == "/user/repos")
    assert body["auto_init"] is True, "没有初始提交，contents API 落不了文件"


def test_create_repository_surfaces_failures() -> None:
    with pytest.raises(ScaffoldError, match="建仓失败"):
        create_repository(FakeScaffoldClient(repo_status=422), "demo")


def test_first_push_commits_every_file_to_the_default_branch() -> None:
    client = FakeScaffoldClient()
    scaffold = Scaffold("idea", "demo", "s", "r", files_from(MINIMAL))
    outcome = first_push(client, scaffold, full_name="owner/demo", default_branch="main")

    assert outcome["branch"] == "main"
    puts = [entry for entry in client.calls if entry[0] == "PUT"]
    assert len(puts) == len(MINIMAL), "每个文件都要提交"
    paths = {entry[1].split("/contents/")[-1] for entry in puts}
    assert "LICENSE" in paths and "pyproject.toml" in paths
    content = base64.b64decode(next(body for method, path, body in client.calls if method == "PUT")["content"])
    assert content, "内容不能是空的"


def test_latest_ci_reads_the_most_recent_run() -> None:
    client = FakeScaffoldClient(runs=[{"status": "completed", "conclusion": "success", "html_url": "u"}])
    assert latest_ci(client, "owner/demo")["conclusion"] == "success"


def test_wait_for_ci_returns_as_soon_as_there_is_a_conclusion() -> None:
    client = FakeScaffoldClient(runs=[{"status": "completed", "conclusion": "success", "html_url": "u"}])
    slept: list[float] = []
    result = wait_for_ci(client, "owner/demo", sleep=slept.append)
    assert result["conclusion"] == "success"
    assert slept == [], "已有结论就不该再睡"


def test_wait_for_ci_reports_a_timeout_instead_of_pretending_green() -> None:
    client = FakeScaffoldClient(runs=[{"status": "in_progress", "conclusion": None, "html_url": "u"}])
    result = wait_for_ci(client, "owner/demo", timeout_seconds=0, sleep=lambda _: None)
    assert result.get("timed_out") is True
    assert result["conclusion"] is None
