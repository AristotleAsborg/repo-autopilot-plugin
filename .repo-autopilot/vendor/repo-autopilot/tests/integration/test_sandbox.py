"""1.5 沙箱验收：恶意三件套 + 宿主零影响。

路线验收原文：含 `rm -rf /` 的补丁（隔离生效）、死循环（超时熔断）、
外联请求（断网生效），宿主零影响。

本机没有 Docker、没有管理员权限。路线自己写明这种情况走降级方案
（`sandbox_strength: weak`）并"相应跳过红队演练的逃逸项"。所以第三条
**不假装断网生效**，而是断言：沙箱如实报告网络未隔离，并把真实连通结果写进日志。
把它写成"断网生效"的断言，就会得到一条永远绿的假测试 —— 那比没有测试更糟。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.sandbox import (
    Limits,
    capabilities,
    hash_tree,
    jobobject_available,
    run,
)

PAYLOAD = ROOT / "tests" / "sandbox_payloads.py"
PY = sys.executable


def payload(*args: str) -> list[str]:
    return [PY, str(PAYLOAD), *args]


@pytest.fixture
def fake_repo(state_dir: Path) -> Path:
    """一个会被"恶意补丁"盯上的小仓库，外加一个必须活下来的哨兵文件。"""
    repo = state_dir / "fake-repo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "app.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    (repo / "README.md").write_text("# fake repo\n", encoding="utf-8")
    (repo / "sentinel.txt").write_text("MUST SURVIVE\n", encoding="utf-8")
    return repo


@pytest.fixture
def sandbox_root(state_dir: Path) -> Path:
    return state_dir / "sb"


# ==================================================== 能力表必须诚实

class TestHonestCapabilities:
    def test_strength_and_gaps_are_declared(self) -> None:
        caps = capabilities()
        assert caps["sandbox_strength"] == "weak"
        assert caps["network"].startswith("not-enforced"), "网络确实没隔离，不许含糊"
        assert caps["disk"].startswith("not-enforced")
        assert "copy-based" in caps["filesystem"]
        assert "docker_available=false" in caps["reason"]

    def test_jobobject_probe_reports_something(self) -> None:
        ok, detail = jobobject_available()
        assert isinstance(ok, bool)
        assert detail, "探测必须给出原因，否则失败时无从判断"


# ================================================== 基线：良性命令

class TestBenign:
    def test_benign_command_passes_and_source_untouched(
        self, fake_repo: Path, sandbox_root: Path
    ) -> None:
        result = run(fake_repo, None, payload("ok"), root=sandbox_root)
        assert result.passed, result.log
        assert result.exit_code == 0
        assert result.source_unchanged
        assert "benign payload finished" in result.log
        assert result.workdir.exists() and result.repo_dir.exists()

    def test_workdir_is_a_copy_not_the_original(self, fake_repo: Path, sandbox_root: Path) -> None:
        result = run(fake_repo, None, payload("ok"), root=sandbox_root)
        assert result.repo_dir != fake_repo
        assert (result.repo_dir / "sentinel.txt").exists()
        assert (fake_repo / "sentinel.txt").exists()


# ====================================== 攻击一：破坏性补丁 / 隔离生效

class TestDestructive:
    def test_destructive_payload_cannot_touch_the_source_repo(
        self, fake_repo: Path, sandbox_root: Path
    ) -> None:
        before = hash_tree(fake_repo)
        result = run(fake_repo, None, payload("destroy"), root=sandbox_root)

        assert result.source_unchanged, "源仓库必须一个字节都没变"
        assert hash_tree(fake_repo) == before
        assert (fake_repo / "sentinel.txt").read_text(encoding="utf-8") == "MUST SURVIVE\n"
        assert (fake_repo / "pkg" / "app.py").exists()

        # 同时证明 payload **真的跑了**：副本确实被它清空了。
        # 不验这一条的话，"没被破坏"也可能只是"根本没执行"。
        assert "[destroy]" in result.log
        assert not (result.repo_dir / "sentinel.txt").exists()
        assert not (result.repo_dir / "pkg").exists()

    def test_known_gap_absolute_path_is_not_blocked(
        self, fake_repo: Path, sandbox_root: Path, state_dir: Path
    ) -> None:
        """
        已知缺口，**故意断言攻击成功**。

        作业对象管资源不管权限，没有 Docker 就没有真正的文件系统隔离。
        把它测成"被拦住了"是自欺；测成"确实没拦住"并写在能力表里，
        才能让后面读代码的人不会把 weak 当成 strong。
        """
        outside = state_dir / "outside-sentinel.txt"
        result = run(
            fake_repo, None, [PY, str(PAYLOAD), "absolute", str(outside)], root=sandbox_root
        )
        assert outside.exists(), "本机确实拦不住工作目录之外的写入（已知缺口）"
        assert result.enforced["filesystem"].startswith("copy-based")


# ======================================== 攻击二：死循环 / 超时熔断

class TestTimeout:
    def test_infinite_loop_is_killed(self, fake_repo: Path, sandbox_root: Path) -> None:
        started = time.perf_counter()
        result = run(
            fake_repo,
            None,
            payload("spin"),
            limits=Limits(timeout_seconds=4),
            root=sandbox_root,
        )
        elapsed = time.perf_counter() - started

        assert result.timed_out, "死循环必须被硬超时熔断"
        assert result.passed is False
        assert elapsed < 60, f"熔断太慢：{elapsed:.1f}s"
        assert any("超时熔断" in note for note in result.notes), result.notes
        assert result.source_unchanged

    def test_timed_out_process_is_really_gone(self, fake_repo: Path, sandbox_root: Path) -> None:
        """熔断之后不能留下还在跑的僵尸循环。"""
        result = run(
            fake_repo,
            None,
            payload("spin"),
            limits=Limits(timeout_seconds=3),
            root=sandbox_root,
        )
        assert result.timed_out
        # 再等一会儿，确认它没有"杀完又活过来"（作业对象杀的是整棵树）
        time.sleep(1.0)
        assert result.exit_code is not None, "被熔断的进程应当能拿到退出码"


# ================================================== 攻击三：外联

class TestNetwork:
    def test_network_is_reported_honestly_not_claimed_as_blocked(
        self, fake_repo: Path, sandbox_root: Path
    ) -> None:
        """
        按路线 1.5 的降级条款：红队演练的"断网生效"项**跳过**，
        改为断言沙箱如实声明网络未隔离，并把真实连通结果记进日志。
        """
        result = run(fake_repo, None, payload("net"), root=sandbox_root)

        assert result.enforced["network"].startswith("not-enforced")
        assert "[net]" in result.log, "真实连通结果必须留在日志里，不能只有一句'未隔离'"
        assert ("connected" in result.log) or ("unreachable" in result.log)

    def test_network_gap_is_explicitly_not_a_pass_criterion(self) -> None:
        caps = capabilities()
        assert "跳过" in caps["network"], "要把'跳过'写清楚，而不是含糊成'尽力而为'"


# =========================================== 攻击四/五：环境与内存

class TestEnvAndMemory:
    def test_child_does_not_inherit_host_secrets(
        self, fake_repo: Path, sandbox_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """被测的是不可信代码；宿主环境里有我们的 token，绝不能顺手继承过去。"""
        monkeypatch.setenv("GH_READ_TOKEN", "canary-read-should-not-propagate")
        monkeypatch.setenv("GH_WRITE_TOKEN", "canary-write-should-not-propagate")

        result = run(fake_repo, None, payload("env"), root=sandbox_root)

        assert "(none)" in result.log, result.log
        assert result.passed, "没有泄漏时样本应当返回 0"
        assert "canary" not in result.log

    def test_memory_hog_is_limited_by_job_object(
        self, fake_repo: Path, sandbox_root: Path
    ) -> None:
        ok, detail = jobobject_available()
        if not ok:
            pytest.skip(f"作业对象不可用（{detail}）—— 能力表已如实标注 memory=none")

        result = run(
            fake_repo,
            None,
            payload("memhog"),
            limits=Limits(memory_bytes=256 * 1024 * 1024, timeout_seconds=60),
            root=sandbox_root,
        )
        joined = " ".join(result.notes)
        if "资源上限未生效" in joined:
            pytest.skip(f"拿不到进程接管权限，无法验证内存上限：{joined[:200]}")

        assert result.passed is False, "256MB 上限下不该还能申请到 2GB"
        assert "MemoryError" in result.log


# ================================================== 补丁只在副本里

class TestPatch:
    def test_patch_is_applied_to_the_copy_only(
        self, fake_repo: Path, sandbox_root: Path, state_dir: Path
    ) -> None:
        patch = state_dir / "change.patch"
        patch.write_text(
            "--- a/pkg/app.py\n"
            "+++ b/pkg/app.py\n"
            "@@ -1,2 +1,2 @@\n"
            " def add(a, b):\n"
            "-    return a + b\n"
            "+    return a + b + 1\n",
            encoding="utf-8",
        )

        result = run(fake_repo, patch, payload("ok"), root=sandbox_root)

        assert result.patch_applied, result.notes
        assert "a + b + 1" in (result.repo_dir / "pkg" / "app.py").read_text(encoding="utf-8")
        assert "a + b + 1" not in (fake_repo / "pkg" / "app.py").read_text(encoding="utf-8")
        assert result.source_unchanged

    def test_patch_only_without_test_command(
        self, fake_repo: Path, sandbox_root: Path, state_dir: Path
    ) -> None:
        patch = state_dir / "noop.patch"
        patch.write_text(
            "--- a/pkg/app.py\n"
            "+++ b/pkg/app.py\n"
            "@@ -1,2 +1,2 @@\n"
            " def add(a, b):\n"
            "-    return a + b\n"
            "+    return a + b  # touched\n",
            encoding="utf-8",
        )
        result = run(fake_repo, patch, None, root=sandbox_root)
        assert result.patch_applied
        assert any("没有执行任何命令" in note for note in result.notes)
