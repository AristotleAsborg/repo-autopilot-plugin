"""4.4 发布链路的确定性测试（不联网、不推任何东西）。

钉死的三件事：

1. **两次点头一次都不能省**：没有批准 → 一个 REST 调用都不许发生；
   只批了 push、没批 PR → 停在建 PR 之前（stage 如实回报）；
2. **离线不偷跑**：`mode=offline` 时写操作进 `state/outbox/`，外面什么都没发生；
3. **拒绝要回流**：人类的"否 + 原因"必须变成下一轮修复的输入，否则下一轮会原样再提一次。

另外报告五段（根因/改动/测试/风险/回滚）缺一段就要在正文里**明说这是缺陷** ——
一份"看不出风险"的报告比没有报告更危险，因为它看上去像已经评估过了。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.publish import (
    FileChange,
    PublishError,
    ReportInput,
    branch_name,
    check_publishable,
    create_pull_request,
    publish,
    push_changes,
    rejection_feedback,
    render_report,
    write_report,
)

FAKE_TOKEN = "fake-write-token-for-publish-tests"


class FakeResponse:
    def __init__(self, status: int, body: object = None) -> None:
        self.status = status
        self.body = body if body is not None else {}
        self.url = "https://api.github.com/fake"


class FakeClient:
    """记录每一次调用；可以预置各种失败形态。"""

    def __init__(self, *, ref_status: int = 201, contents_existing: bool = True, pr_status: int = 201) -> None:
        self.ref_status = ref_status
        self.contents_existing = contents_existing
        self.pr_status = pr_status
        self.calls: list[tuple[str, str]] = []

    def get(self, path: str, *, params: dict | None = None) -> object:
        self.calls.append(("GET", path))
        if "/git/ref/heads/" in path:
            return {"object": {"sha": "base-sha"}}
        if "/contents/" in path:
            if not self.contents_existing:
                raise RuntimeError("404 Not Found")
            return {"sha": "file-sha"}
        return {}

    def request(self, method: str, path: str, *, params: dict | None = None, body: dict | None = None, base_url=None):
        self.calls.append((method, path))
        if method == "POST" and path.endswith("/git/refs"):
            if self.ref_status == 422:
                # 分支已存在是**幂等**情形，不该让整条链路失败
                return FakeResponse(422, {"message": "Reference already exists"})
            return FakeResponse(
                self.ref_status, {} if self.ref_status < 400 else {"message": "Forbidden"}
            )
        if method == "PUT" and "/contents/" in path:
            return FakeResponse(201, {"commit": {"sha": "commit-sha"}})
        if method == "POST" and path.endswith("/pulls"):
            return FakeResponse(self.pr_status, {"html_url": "https://github.com/o/r/pull/7", "number": 7})
        return FakeResponse(200, {})


def report(issue_id: str = "fix-1") -> ReportInput:
    return ReportInput(
        issue_id=issue_id,
        repo="owner/repo",
        issue_title="导出 CSV 时中文乱码",
        root_cause="导出时没有指定 UTF-8 编码",
        changes=["ledger/report.py：to_csv 指定 encoding"],
        test_summary="目标测试 4/4 通过；全量回归无新增失败；lint 无新增告警",
        risks="只影响导出路径；老文件仍按原编码读取",
        rollback="关闭 PR 即可，main 无改动",
        patch_path="state/patches/fix-1.diff",
    )


def changes() -> list[FileChange]:
    return [FileChange("ledger/report.py", "def to_csv(ledger):\n    return ''\n")]


@pytest.fixture
def write_token(scratch: Path) -> Path:
    path = scratch / ".write_token"
    path.write_text(FAKE_TOKEN, encoding="utf-8")
    return path


# ------------------------------------------------------------------ 报告

def test_report_has_all_five_sections() -> None:
    text = render_report(report())
    for heading in ("一、根因", "二、改动摘要", "三、测试结果", "四、风险点", "五、回滚方法"):
        assert heading in text
    assert "UTF-8" in text
    assert "关闭 PR 即可" in text


@pytest.mark.parametrize(
    ("field", "marker"),
    [
        ("root_cause", "没有根因"),
        ("test_summary", "没有测试结果"),
        ("risks", "未评估风险"),
        ("rollback", "未写回滚方法"),
    ],
)
def test_report_says_loudly_when_a_section_is_missing(field: str, marker: str) -> None:
    data = ReportInput(**{**report().as_dict(), field: ""})
    assert marker in render_report(data)


def test_write_report_lands_in_the_reports_directory(scratch: Path) -> None:
    path = write_report(report("fix-9"), scratch)
    assert path.name == "fix-9.md"
    assert "修复报告：fix-9" in path.read_text(encoding="utf-8")


# ------------------------------------------------------------------ 分支名与黑名单

def test_branch_name_follows_the_route_format() -> None:
    # issue_id 传的就是 issue 号：`auto/fix-42`
    assert branch_name("42") == "auto/fix-42"
    assert branch_name("weird id/with:chars") == "auto/fix-weird-id-with-chars"


def test_publishable_rejects_dangerous_paths() -> None:
    for path in ("tests/test_money.py", ".github/workflows/ci.yml", "state/approvals/x.md", "LICENSE"):
        with pytest.raises(PublishError, match="黑名单"):
            check_publishable([FileChange(path, "x")])


def test_publishable_allows_source_paths() -> None:
    check_publishable([FileChange("ledger/report.py", "x")])


# ------------------------------------------------------------------ 闸门

def test_publish_without_approval_touches_nothing(scratch: Path, write_token: Path) -> None:
    client = FakeClient()
    result = publish(
        repo="owner/repo",
        issue_id="fix-1",
        report=report(),
        changes=changes(),
        approvals_dir=scratch / "approvals",
        report_path=scratch / "report.md",
        write_token=write_token,
        client=client,
    )
    assert result.published is False and result.stage == "push"
    assert client.calls == [], "没有人类点头，一个 REST 调用都不许发生"
    assert (scratch / "approvals").exists()
    assert result.tickets, "应该留下一张审批单给人看"


def test_publish_always_writes_the_report_where_told(scratch: Path, write_token: Path) -> None:
    """报告是人类审批的依据：说写到哪儿就必须真的写到哪儿（踩过：只记路径不落盘）。"""
    target = scratch / "reports" / "fix-5.md"
    publish(
        repo="owner/repo",
        issue_id="fix-5",
        report=report("fix-5"),
        changes=changes(),
        approvals_dir=scratch / "approvals",
        report_path=target,
        write_token=write_token,
        client=FakeClient(),
    )
    assert target.is_file(), "报告必须真的落盘"
    assert "五、回滚方法" in target.read_text(encoding="utf-8")


def test_reply_scope_approves_only_one_gate(scratch: Path, write_token: Path) -> None:
    """
    一次「是」只能批一个闸门（路线 4.4 的"两次是"）：
    只批 push 时，PR 单子必须仍然是 pending 状态。
    """
    client = FakeClient()
    result = publish(
        repo="owner/repo",
        issue_id="fix-6",
        report=report("fix-6"),
        changes=changes(),
        conversation_reply="是",
        reply_scope="push",
        approvals_dir=scratch / "approvals",
        report_path=scratch / "report.md",
        write_token=write_token,
        client=client,
    )
    assert result.published is False, "只批了 push，PR 不该被建"
    assert result.stage == "pull_request"
    methods = [f"{method} {path}" for method, path in client.calls]
    assert any("git/refs" in item for item in methods), "分支应该推了"
    assert not any("/pulls" in item for item in methods), "PR 不该被建"
    pr_ticket = next(name for name in result.tickets if "create_pull_request" in name)
    content = (scratch / "approvals" / pr_ticket).read_text(encoding="utf-8")
    assert not content.splitlines()[0].strip().startswith("是"), "PR 单子必须还是 pending"


def test_reply_scope_pull_request_completes_the_publish(scratch: Path, write_token: Path) -> None:
    client = FakeClient()
    result = publish(
        repo="owner/repo",
        issue_id="fix-7",
        report=report("fix-7"),
        changes=changes(),
        conversation_reply="是",
        reply_scope="pull_request",
        approvals_dir=scratch / "approvals",
        report_path=scratch / "report.md",
        write_token=write_token,
        client=client,
    )
    # push 单子这次没有对话回复 → 仍然 pending → 链路停在 push，不会偷偷建 PR
    assert result.published is False and result.stage == "push"
    assert not any("pulls" in path for _, path in client.calls)


def test_publish_refuses_when_the_base_does_not_match_the_remote(scratch: Path, write_token: Path) -> None:
    """
    我们改的必须是远端**现在**那份内容：对不上就拒绝推送 ——
    否则会把别人已经改过、而我们没看到的代码覆盖掉（这种覆盖不会报错）。
    """
    import base64 as _b64

    class RemoteDiffersClient(FakeClient):
        def get(self, path: str, *, params: dict | None = None) -> object:
            if "/contents/" in path:
                self.calls.append(("GET", path))
                return {"sha": "file-sha", "content": _b64.b64encode(b"someone else's newer text\n").decode()}
            return super().get(path, params=params)

    result = publish(
        repo="owner/repo",
        issue_id="fix-8",
        report=report("fix-8"),
        changes=[FileChange("ledger/report.py", "our new text\n", base_text="what we saw\n")],
        conversation_reply="是",
        reply_scope="push",
        approvals_dir=scratch / "approvals",
        report_path=scratch / "report.md",
        write_token=write_token,
        client=RemoteDiffersClient(),
    )
    assert result.published is False
    assert "不一致" in result.reason or "拒绝" in result.reason


def test_newline_style_is_preserved_from_the_remote(scratch: Path) -> None:
    """远端是 CRLF 就还它 CRLF：否则"加一节"会变成整文件重写（实测 +40/-31）。"""
    from src.publish import adapt_newlines, normalize_newlines

    lf = "# title\n## Usage\n"
    crlf = "# title\r\n## Usage\r\n"
    assert "\r\n" in adapt_newlines(lf, crlf)
    assert adapt_newlines(crlf, lf) == "# title\n## Usage\n"
    assert normalize_newlines(crlf) == normalize_newlines(lf)


def test_rejection_stops_at_push(scratch: Path, write_token: Path) -> None:
    client = FakeClient()
    result = publish(
        repo="owner/repo",
        issue_id="fix-2",
        report=report("fix-2"),
        changes=changes(),
        conversation_reply="否",
        approvals_dir=scratch / "approvals",
        report_path=scratch / "report.md",
        write_token=write_token,
        client=client,
    )
    assert result.published is False
    assert client.calls == []


def test_approved_publishes_branch_then_pull_request(scratch: Path, write_token: Path) -> None:
    client = FakeClient()
    result = publish(
        repo="owner/repo",
        issue_id="fix-3",
        report=report("fix-3"),
        changes=changes(),
        conversation_reply="是",
        approvals_dir=scratch / "approvals",
        report_path=scratch / "report.md",
        write_token=write_token,
        client=client,
    )
    assert result.published is True and result.stage == "done"
    assert result.pr_url == "https://github.com/o/r/pull/7"
    methods = [f"{method} {path}" for method, path in client.calls]
    assert any("git/refs" in item for item in methods), "应该建了分支"
    assert any("contents/" in item for item in methods), "应该提交了改动"
    assert any("/pulls" in item for item in methods), "应该建了 PR"
    assert methods.index(next(i for i in methods if "git/refs" in i)) < methods.index(
        next(i for i in methods if "/pulls" in i)
    ), "顺序必须是先推分支、后建 PR"
    assert len(result.tickets) == 2, "两次点头 = 两张审批单"


def test_offline_publish_queues_instead_of_pushing(scratch: Path, write_token: Path) -> None:
    client = FakeClient()
    result = publish(
        repo="owner/repo",
        issue_id="fix-4",
        report=report("fix-4"),
        changes=changes(),
        conversation_reply="是",
        approvals_dir=scratch / "approvals",
        report_path=scratch / "report.md",
        write_token=write_token,
        outbox_dir=scratch / "outbox",
        mode="offline",
        client=client,
    )
    assert result.published is False and result.stage == "push"
    assert client.calls == [], "离线时必须排队，绝不偷跑"
    queued = result.queued
    assert queued, "应该进 outbox"
    payload = json.loads(Path(queued[0]).read_text(encoding="utf-8"))
    assert payload["action"] == "push"
    assert payload["payload"]["branch"] == "auto/fix-fix-4"


# ------------------------------------------------------------------ REST 细节

def test_push_changes_is_idempotent_about_existing_branch_and_file() -> None:
    client = FakeClient(ref_status=422, contents_existing=True)
    outcome = push_changes(
        client,
        repo="owner/repo",
        branch="auto/fix-1",
        base_branch="main",
        changes=changes(),
        message="fix",
    )
    assert outcome["branch_created"] is False
    assert outcome["commits"][0]["path"] == "ledger/report.py"


def test_push_changes_refuses_an_unexpected_ref_failure() -> None:
    client = FakeClient(ref_status=403)
    with pytest.raises(PublishError, match="建分支失败"):
        push_changes(
            client,
            repo="owner/repo",
            branch="auto/fix-1",
            base_branch="main",
            changes=changes(),
            message="fix",
        )


def test_push_changes_skips_a_file_that_is_already_identical() -> None:
    """第二次点头（或重跑演练）不该在仓库里留下一个空提交。"""
    import base64 as _b64

    class SameContentClient(FakeClient):
        def get(self, path: str, *, params: dict | None = None) -> object:
            if "/contents/" in path:
                self.calls.append(("GET", path))
                return {"sha": "file-sha", "content": _b64.b64encode(b"same\n").decode("ascii")}
            return super().get(path, params=params)

    client = SameContentClient()
    outcome = push_changes(
        client,
        repo="owner/repo",
        branch="auto/fix-1",
        base_branch="main",
        changes=[FileChange("ledger/report.py", "same\n")],
        message="fix",
    )
    assert outcome["commits"][0].get("skipped"), "内容一致应当跳过提交"
    assert not any(method == "PUT" for method, _ in client.calls), "不该发出 PUT"


def test_create_pull_request_reports_failures() -> None:
    client = FakeClient(pr_status=422)
    with pytest.raises(PublishError, match="建 PR 失败"):
        create_pull_request(
            client, repo="owner/repo", branch="auto/fix-1", base_branch="main", title="t", body="b"
        )


class ExistingPrClient(FakeClient):
    """建 PR 时回 422「已经有了」；GET /pulls 按 `open_prs` 给出现有 PR 列表。"""

    def __init__(self, open_prs: list[dict]) -> None:
        super().__init__()
        self.open_prs = open_prs

    def request(self, method: str, path: str, *, params: dict | None = None, body: dict | None = None, base_url=None):
        if method == "POST" and path.endswith("/pulls"):
            self.calls.append((method, path))
            return FakeResponse(422, {
                "message": "Validation Failed",
                "errors": [{"resource": "PullRequest", "code": "custom",
                            "message": "A pull request already exists for owner:auto/fix-1."}],
            })
        if method == "GET" and path.endswith("/pulls"):
            self.calls.append((method, path))
            self.head_query = (params or {}).get("head")
            return FakeResponse(200, self.open_prs)
        return super().request(method, path, params=params, body=body, base_url=base_url)


def test_create_pull_request_reuses_an_existing_pr() -> None:
    """**关键回归（2026-09-12 整理轮实测）**：重跑发布链路不该崩在"PR 已存在"上。

    第一次 4.4 演练建过 PR 之后，第二次演练直接吃 `HTTP 422 A pull request already
    exists`，把整条验收打成 BLOCKED。真实使用里重跑太常见（人类重发指令、超时重试、
    演练重跑），所以**建 PR 不幂等，但链路必须幂等**：查出那条开放 PR 直接复用。
    """
    client = ExistingPrClient([
        {"number": 7, "html_url": "https://github.com/owner/repo/pull/7", "head": {"ref": "auto/fix-1"}},
    ])
    result = create_pull_request(
        client, repo="owner/repo", branch="auto/fix-1", base_branch="main", title="t", body="b"
    )
    assert result["number"] == 7 and result["reused"] is True
    assert client.head_query == "owner:auto/fix-1", "要按 head=<owner>:<branch> 精确查，别把别人的 PR 认成自己的"
    assert "已存在" in result["note"] or "复用" in result["note"]


def test_create_pull_request_ignores_a_pr_from_another_branch() -> None:
    """查回来的 PR 头分支必须**真的匹配**：GitHub 的 head 过滤偶尔会带回别的分支的 PR。"""
    client = ExistingPrClient([
        {"number": 9, "html_url": "https://github.com/owner/repo/pull/9", "head": {"ref": "auto/other"}},
    ])
    with pytest.raises(PublishError, match="查不到开放的 PR"):
        create_pull_request(
            client, repo="owner/repo", branch="auto/fix-1", base_branch="main", title="t", body="b"
        )


def test_create_pull_request_does_not_mask_other_422s() -> None:
    """只有"分支已有 PR"能网开一面；别的 422（校验失败等）必须原样报出来。"""
    class Other422Client(FakeClient):
        def request(self, method, path, *, params=None, body=None, base_url=None):
            if method == "POST" and path.endswith("/pulls"):
                return FakeResponse(422, {"message": "Validation Failed", "errors": [{"code": "missing_field"}]})
            return super().request(method, path, params=params, body=body, base_url=base_url)

    with pytest.raises(PublishError, match="建 PR 失败：HTTP 422"):
        create_pull_request(
            Other422Client(), repo="owner/repo", branch="auto/fix-1", base_branch="main", title="t", body="b"
        )


# ------------------------------------------------------------------ 拒绝回流

def test_rejection_feedback_carries_the_reason_into_the_next_round() -> None:
    class Ticket:
        status = "rejected"
        summary = "把修复推到 owner/repo 的新分支 auto/fix-1"

    text = rejection_feedback(Ticket(), "改错模块了，应该改 ledger/store.py")
    assert "被人类否决" in text
    assert "改错模块了" in text
    assert "不要重复提交同一份改动" in text


def test_rejection_feedback_survives_a_silent_human() -> None:
    class Ticket:
        status = "rejected"
        summary = "建 PR"

    assert "没有写具体原因" in rejection_feedback(Ticket(), "")
