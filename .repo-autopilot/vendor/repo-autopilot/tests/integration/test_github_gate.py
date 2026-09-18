"""1.4 GitHub 封装 + 人类闸门验收测试。

路线把这一节标为**安全红线，永久禁止跳过**，验收三条：
  1. 模拟 push 在未确认时**绝不执行**；
  2. 伪造确认措辞变体（"是的" / "OK" / "批准了" …）**全部不触发执行**；
  3. 写 token 不出现在进程环境变量之外的任何地方。

外加路线 1.4 第 1、5 条的可执行验证：读操作的退避策略（5xx 重试、403+Retry-After、
401 不重试只告警），以及断联时写操作进 outbox 排队而不丢弃。
"""

from __future__ import annotations

import datetime as dt
import http.server
import json
import os
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from typing_extensions import Self

from src.github import (
    WRITE_TOKEN_ENV,
    GateError,
    GitHubClient,
    GitHubError,
    assert_write_action,
    decide,
    execute_if_approved,
    is_explicit_yes,
    request_approval,
    require_human_approval,
    scan_for_leaks,
    write_token_scope,
)

# 假的写 token：**故意**不从真实的 state/.write_token 读，也**故意**不带
# `github_pat_` 这类真前缀。两个原因：
#   1. 验收要证明的是"这个秘密不会漏到别处"，用一个自己造的假秘密就能验，
#      而且不会把真 token 带进任何日志；
#   2. 0.3 的仓库级扫描是按真前缀正则找泄漏的。如果假 token 带真前缀，
#      Python 会在编译期把字符串常量折叠进 `.pyc`，扫描就会在
#      `__pycache__` 里命中它并判 0.3 BLOCKED —— 假阳性，而且很难看穿。
#      （这个坑实测踩过一次，扫描器本身是对的，是测试数据不该长成真 token 的样子。）
FAKE_WRITE_TOKEN = "FAKE-WRITE-TOKEN-0123456789abcdefghijklmnop"


# ====================================================== 测试用 HTTP mock

class MockGitHub:
    """按脚本依次应答的假 GitHub。"""

    def __init__(self, script: list[tuple[int, dict, object]] | None = None) -> None:
        self.script = list(script or [])
        self.seen: list[tuple[str, str]] = []
        self.base_url = ""
        self._server: http.server.ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def __enter__(self) -> Self:
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args: object) -> None:
                pass

            def _respond(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                if length:
                    self.rfile.read(length)
                outer.seen.append((self.command, self.path))
                status, headers, body = (
                    outer.script.pop(0) if outer.script else (200, {}, {"ok": True})
                )
                payload = json.dumps(body).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                for key, value in headers.items():
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            do_GET = _respond
            do_POST = _respond
            do_PUT = _respond
            do_PATCH = _respond
            do_DELETE = _respond

        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base_url = f"http://127.0.0.1:{self._server.server_address[1]}"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)

    @property
    def calls(self) -> int:
        return len(self.seen)


# ============================================================== 公共夹具

@pytest.fixture
def write_token_file(state_dir: Path) -> Path:
    path = state_dir / ".write_token"
    path.write_text(FAKE_WRITE_TOKEN, encoding="utf-8")
    os.environ.pop(WRITE_TOKEN_ENV, None)
    yield path
    os.environ.pop(WRITE_TOKEN_ENV, None)


def new_ticket(state_dir: Path, **overrides: object):
    kwargs = {
        "action": "push",
        "summary": "把修复分支推到 origin",
        "impact": "1 个仓库、1 个分支、预计 +12/-3 行",
        "rollback": "git push origin --delete <branch>；本地分支保留",
        "approvals_dir": state_dir / "approvals",
    }
    kwargs.update(overrides)
    return request_approval(**kwargs)  # type: ignore[arg-type]


def write_first_line(ticket, word: str) -> None:
    """模拟人类在审批单首行写下内容。"""
    lines = ticket.path.read_text(encoding="utf-8").splitlines()
    if not lines:
        lines = [""]
    lines[0] = word
    ticket.path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def read_first_line(ticket) -> str:
    """读审批单首行（闸门与 doctor 都只看这一行）。"""
    return ticket.path.read_text(encoding="utf-8").splitlines()[0].strip()


class Boom(RuntimeError):
    pass


def boom() -> None:
    raise Boom("写操作被执行了——这正是安全红线要阻止的事")


# ========================================================= 1. 未确认不执行

class TestUnconfirmedNeverExecutes:
    def test_pending_ticket_never_calls_perform(self, state_dir: Path, write_token_file: Path) -> None:
        ticket = new_ticket(state_dir)
        calls: list[int] = []
        outcome = execute_if_approved(
            ticket, lambda: calls.append(1), write_token=write_token_file, mode="online"
        )
        assert outcome.executed is False
        assert calls == [], "未确认时 perform 一次都不能被调用"
        assert "未获批准" in outcome.reason

    def test_unconfirmed_push_never_executes(self, state_dir: Path, write_token_file: Path) -> None:
        """路线验收第 1 条的字面版本：模拟 push，未确认时绝不执行。"""
        ticket = new_ticket(state_dir, action="push")
        outcome = execute_if_approved(ticket, boom, write_token=write_token_file, mode="online")
        assert outcome.executed is False

    def test_expired_ticket_is_not_executable(self, state_dir: Path, write_token_file: Path) -> None:
        # naive 本地时间**是刻意的**：闸门模块对"没有偏移量的单子"就是按本地时间处理的
        # （见 `approval._parse_iso`），这条测试要钉的正是那种单子会过期。
        created = dt.datetime(2026, 1, 1, 12, 0, 0)  # noqa: DTZ001
        ticket = new_ticket(state_dir, now=created, ttl_hours=72)
        write_first_line(ticket, "是")   # 过期之后人类才写"是"
        decided = decide(ticket, now=created + dt.timedelta(hours=100))
        assert decided.status == "expired"

        calls: list[int] = []
        outcome = execute_if_approved(
            decided, lambda: calls.append(1), write_token=write_token_file, mode="online"
        )
        assert outcome.executed is False
        assert calls == [], "过期单子之后再补一个「是」也不生效"

    def test_rejection_word_rejects(self, state_dir: Path) -> None:
        ticket = new_ticket(state_dir)
        write_first_line(ticket, "否")
        assert decide(ticket).status == "rejected"

    def test_unknown_action_is_refused(self) -> None:
        with pytest.raises(GateError):
            assert_write_action("rm -rf /")
        # 新增写操作忘了登记时，闸门要立刻报错，而不是悄悄放行
        assert_write_action("push")


# ==================================================== 2. 伪造措辞不触发

FAKE_CONFIRMATIONS = [
    "是的",
    "是的。",
    "是 的",
    "是好",
    "OK",
    "ok",
    "Ok",
    "好的",
    "好的，批准",
    "可以",
    "行",
    "批准了",
    "批准",
    "同意",
    "yes",
    "YES",
    "y",
    "true",
    "确认",
    "是是",
    "是 ",
    " 是 ",          # 这一条前后空白应当被容忍 —— 见下面的正向用例
    "",
    "是，但是先别推 main",
]


class TestFakeConfirmations:
    @pytest.mark.parametrize("word", FAKE_CONFIRMATIONS)
    def test_file_wording_variants_do_not_execute(
        self, state_dir: Path, write_token_file: Path, word: str
    ) -> None:
        if word.strip() == "是":
            pytest.skip("恰好等于「是」的那条由正向用例覆盖")
        ticket = new_ticket(state_dir)
        write_first_line(ticket, word)
        decided = decide(ticket)
        assert decided.status != "approved", f"{word!r} 不该被当成确认"

        calls: list[int] = []
        outcome = execute_if_approved(
            decided, lambda: calls.append(1), write_token=write_token_file, mode="online"
        )
        assert outcome.executed is False and calls == []

    @pytest.mark.parametrize(
        "reply", ["是的", "OK", "好的", "可以", "批准了", "yes", "同意", "是 的", "是。"]
    )
    def test_conversation_wording_variants_do_not_execute(
        self, state_dir: Path, write_token_file: Path, reply: str
    ) -> None:
        ticket = new_ticket(state_dir)
        decided = decide(ticket, conversation_reply=reply)
        assert decided.status != "approved", f"对话里的 {reply!r} 不该被当成确认"
        assert execute_if_approved(
            decided, boom, write_token=write_token_file, mode="online"
        ).executed is False

    @pytest.mark.parametrize("word", ["是", "是\n", "  是  ", "\ufeff是"])
    def test_exact_yes_is_accepted(self, word: str) -> None:
        assert is_explicit_yes(word), f"{word!r} 应当被接受（去掉空白/BOM 后恰好是「是」）"

    @pytest.mark.parametrize("word", ["是的", "OK", "好的", "", None, "是不是"])
    def test_non_exact_is_rejected(self, word: str | None) -> None:
        assert not is_explicit_yes(word)


# ==================================================== 对话里的决定要记回单子（2026-09-17）

class TestDialogueDecisionIsRecorded:
    """
    **为什么要有这一节**：单子有两个决定来源（文件第 1 行 / 对话回复），
    而对话那一路原来**不留痕** —— `scripts/doctor.py` 的"闸门悬挂审批"是**读文件**判定的，
    于是所有已经在对话里批过的单子一直显示"超过 72 小时没处理"，
    `/体检` 因此永远"不通过"。**永远红的检查等于没有检查。**

    另外：「对话里说不」原来**没有分支** → 落到 pending → 同一件事被反复追问，
    这与"人类拒绝之后不许再请求批准"直接冲突。
    """

    def test_dialogue_yes_is_written_into_the_ticket(self, state_dir: Path) -> None:
        ticket = new_ticket(state_dir)
        assert read_first_line(ticket) != "是", "建单时首行是说明文字"
        decided = decide(ticket, conversation_reply="是")
        assert decided.status == "approved"
        assert read_first_line(ticket) == "是", "对话里的「是」必须记回单子（否则检查看不见）"

    def test_dialogue_no_is_a_rejection_and_is_written(self, state_dir: Path) -> None:
        ticket = new_ticket(state_dir)
        decided = decide(ticket, conversation_reply="否")
        assert decided.status == "rejected", "对话里的「否」必须判成 rejected（原来是 pending）"
        assert read_first_line(ticket) == "否"

    def test_a_vague_dialogue_reply_leaves_the_ticket_pending_and_untouched(self, state_dir: Path) -> None:
        ticket = new_ticket(state_dir)
        before = ticket.path.read_text(encoding="utf-8")
        decided = decide(ticket, conversation_reply="先这样吧")
        assert decided.status == "pending"
        assert ticket.path.read_text(encoding="utf-8") == before, "说不清就不许改单子"

    def test_a_decided_ticket_is_not_asked_again(self, state_dir: Path) -> None:
        """拒绝之后不许再请求批准（与"人类拒绝是终局"的操作规矩一致）。"""
        from src.github import require_human_approval

        approvals = state_dir / "approvals"
        first = require_human_approval(
            "push", summary="s", impact="i", rollback="r",
            conversation_reply="否", request_key="k", approvals_dir=approvals,
        )
        assert first.status == "rejected"
        again = require_human_approval(
            "push", summary="s", impact="i", rollback="r",
            conversation_reply=None, request_key="k", approvals_dir=approvals,
        )
        assert again.status == "rejected", "已经拒绝过的单子不该退回 pending 再问一遍"

    def test_the_doctor_hanging_check_sees_a_dialogue_approved_ticket_as_handled(self, state_dir: Path) -> None:
        """
        端到端的那一条：**对话框批过的单子，/体检 的悬挂检查必须当成已处理**。
        （这正是 2026-09-17 实测里 `/体检` 永远红的原因。）
        """
        from src.github import require_human_approval
        from src.skills.doctor import check_stale_approvals

        approvals = state_dir / "approvals"
        require_human_approval(
            "push", summary="s", impact="i", rollback="r",
            conversation_reply="是", request_key="k", approvals_dir=approvals,
        )
        check = check_stale_approvals(state_dir)
        assert check.ok is True, check.detail


# ==================================================== 3. 确认后正常执行

class TestApprovedExecutes:
    def test_file_first_line_yes_executes(self, state_dir: Path, write_token_file: Path) -> None:
        ticket = new_ticket(state_dir)
        write_first_line(ticket, "是")
        decided = decide(ticket)
        assert decided.status == "approved"

        outcome = execute_if_approved(
            decided, lambda: "推完了", write_token=write_token_file, mode="online"
        )
        assert outcome.executed is True
        assert outcome.result == "推完了"

    def test_conversation_yes_executes(self, state_dir: Path, write_token_file: Path) -> None:
        ticket = new_ticket(state_dir)
        decided = decide(ticket, conversation_reply="是")
        assert decided.status == "approved"
        assert execute_if_approved(
            decided, lambda: "ok", write_token=write_token_file, mode="online"
        ).executed is True

    def test_require_human_approval_round_trip(self, state_dir: Path) -> None:
        first = require_human_approval(
            "push",
            summary="s",
            impact="i",
            rollback="r",
            request_key="job-1:push",
            approvals_dir=state_dir / "approvals",
        )
        assert first.status == "pending"

        write_first_line(first, "是")
        second = require_human_approval(
            "push",
            summary="s",
            impact="i",
            rollback="r",
            request_key="job-1:push",
            approvals_dir=state_dir / "approvals",
        )
        assert second.id == first.id, "同一个 request_key 不该重复建单"
        assert second.status == "approved"

    def test_different_request_keys_create_separate_tickets(self, state_dir: Path) -> None:
        a = new_ticket(state_dir, request_key="job-1:push")
        b = new_ticket(state_dir, request_key="job-2:push")
        assert a.path != b.path


# ================================================= 4. 写 token 的隔离

class TestWriteTokenIsolation:
    def test_token_enters_env_only_around_the_call(
        self, state_dir: Path, write_token_file: Path
    ) -> None:
        ticket = new_ticket(state_dir)
        write_first_line(ticket, "是")
        decided = decide(ticket)

        seen: dict[str, str | None] = {}

        def perform() -> str:
            seen["inside"] = os.environ.get(WRITE_TOKEN_ENV)
            return "done"

        outcome = execute_if_approved(decided, perform, write_token=write_token_file, mode="online")
        assert outcome.executed is True
        assert seen["inside"] == FAKE_WRITE_TOKEN, "执行期间写 token 必须在环境里"
        assert WRITE_TOKEN_ENV not in os.environ, "执行完必须立刻删掉"

    def test_scope_removes_token_even_when_perform_raises(
        self, state_dir: Path, write_token_file: Path
    ) -> None:
        """最容易留下残留的路径就是异常路径，必须单独验。"""
        with pytest.raises(Boom), write_token_scope(write_token_file):
            assert os.environ[WRITE_TOKEN_ENV] == FAKE_WRITE_TOKEN
            raise Boom("中途炸了")
        assert WRITE_TOKEN_ENV not in os.environ

    def test_unapproved_ticket_never_touches_the_token(
        self, state_dir: Path, write_token_file: Path
    ) -> None:
        """未批准时连 token 都不该被读出来。"""
        ticket = new_ticket(state_dir)
        outcome = execute_if_approved(ticket, boom, write_token=write_token_file, mode="online")
        assert outcome.executed is False
        assert WRITE_TOKEN_ENV not in os.environ

    def test_write_token_appears_only_in_its_own_file(
        self, state_dir: Path, write_token_file: Path
    ) -> None:
        """路线验收第 3 条：写 token 不出现在进程环境变量之外的任何地方。"""
        ticket = new_ticket(state_dir)
        write_first_line(ticket, "是")
        decided = decide(ticket)
        execute_if_approved(decided, lambda: "done", write_token=write_token_file, mode="online")

        hits = scan_for_leaks(
            [path for path in state_dir.rglob("*")], secrets=[FAKE_WRITE_TOKEN]
        )
        assert hits == [str(write_token_file)], (
            f"写 token 只允许出现在它自己的文件里，实际命中：{hits}"
        )
        assert FAKE_WRITE_TOKEN not in ticket.path.read_text(encoding="utf-8")


# ===================================================== 5. 断联时排队

class TestOfflineQueue:
    def test_offline_queues_instead_of_executing(
        self, state_dir: Path, write_token_file: Path
    ) -> None:
        ticket = new_ticket(state_dir)
        write_first_line(ticket, "是")
        decided = decide(ticket)

        calls: list[int] = []
        outcome = execute_if_approved(
            decided,
            lambda: calls.append(1),
            payload={"branch": "fix/1"},
            write_token=write_token_file,
            outbox_dir=state_dir / "outbox",
            mode="offline",
        )
        assert outcome.executed is False
        assert calls == [], "断联时绝不能执行——要排队，不是丢弃"
        assert outcome.queued_path is not None and outcome.queued_path.exists()
        queued = json.loads(outcome.queued_path.read_text(encoding="utf-8"))
        assert queued["action"] == "push"
        assert queued["payload"] == {"branch": "fix/1"}


# ================================================== 6. 读操作的退避策略

class TestBackoff:
    def test_5xx_retries_then_succeeds(self) -> None:
        with MockGitHub([(500, {}, {}), (502, {}, {}), (200, {}, {"login": "me"})]) as mock:
            slept: list[float] = []
            client = GitHubClient(token="t", base_url=mock.base_url, sleep=slept.append)
            assert client.get("/user") == {"login": "me"}
            assert mock.calls == 3
        assert slept == [1.0, 2.0], "退避必须是 1s / 2s 指数"

    def test_5xx_gives_up_after_five_attempts(self) -> None:
        with MockGitHub([(500, {}, {})] * 5) as mock:
            slept: list[float] = []
            client = GitHubClient(token="t", base_url=mock.base_url, sleep=slept.append)
            with pytest.raises(GitHubError):
                client.get("/user")
            assert mock.calls == 5, "重试上限是 5 次尝试"
        assert slept == [1.0, 2.0, 4.0, 8.0]

    def test_403_with_retry_after_sleeps_exactly_that_long(self) -> None:
        with MockGitHub([(403, {"Retry-After": "7"}, {}), (200, {}, {"ok": True})]) as mock:
            slept: list[float] = []
            client = GitHubClient(token="t", base_url=mock.base_url, sleep=slept.append)
            assert client.get("/user") == {"ok": True}
            assert mock.calls == 2
        assert slept == [7.0], "限流要睡到对方指定的秒数，不要自作聪明缩短"

    def test_401_alerts_and_never_retries(self, state_dir: Path) -> None:
        report = state_dir / "auth_failure.md"
        with MockGitHub([(401, {}, {"message": "Bad credentials"})]) as mock:
            slept: list[float] = []
            client = GitHubClient(
                token="t", base_url=mock.base_url, sleep=slept.append, auth_report=report
            )
            with pytest.raises(GitHubError):
                client.get("/user")
            assert mock.calls == 1, "凭证错误重试没有意义"
        assert slept == []
        assert report.exists() and "401" in report.read_text(encoding="utf-8")

    def test_404_is_returned_to_the_caller_not_retried(self) -> None:
        """仓库不存在是业务结果，不是故障，不该被重试逻辑吃掉。"""
        with MockGitHub([(404, {}, {"message": "Not Found"})]) as mock:
            slept: list[float] = []
            client = GitHubClient(token="t", base_url=mock.base_url, sleep=slept.append)
            with pytest.raises(GitHubError):
                client.get("/repos/no/such")
            assert mock.calls == 1
        assert slept == []

    def test_connection_error_is_retried_then_raises(self) -> None:
        import socket

        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        slept: list[float] = []
        client = GitHubClient(
            token="t", base_url=f"http://127.0.0.1:{port}", sleep=slept.append, attempts=3
        )
        with pytest.raises(GitHubError):
            client.get("/user")
        assert slept == [1.0, 2.0]
