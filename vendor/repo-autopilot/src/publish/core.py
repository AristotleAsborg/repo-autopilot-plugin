"""4.4 报告与确认推送：**把补丁变成一次人类批准过的推送 + PR**。

路线原文：

> 生成 `reports/{issue_id}.md`（根因/改动摘要/测试结果/风险点/回滚方法）→ 过闸门 →
> 新分支 `auto/fix-{issue_id}` → push → 建 PR；人类说"否"→ 原因回流修复器重试。

## 这里有两条不能松的线

1. **两次人类点头，一次都不省**：建分支+提交是一次（`push`），建 PR 是另一次
   （`create_pull_request`）。它们是两个不同的后果：分支推上去可以被看到，PR 一发出去
   就是**公开的邀请**。合成一次批准，等于把"公开"这件事藏在了"推送"的批准里。
2. **推送走 REST，不走本地 git push**：实测本机 `git push` 到本地路径被策略挡
   （`push_local_path: false`），而 https 推送需要凭据落在 git 的凭据管理器里 ——
   那是"密钥离开我们控制"的一种形式。REST contents API 的效果一样
   （新分支 + 提交 + PR），但 token 只在一次 HTTP 调用里出现，且**只在本进程的环境变量里**存在。

## 为什么拒绝要"回流"

人类说"否"从来不是终点：他会说"这个改法不对，应该改 store 而不是 report"。
这句话如果不带回修复器，系统下一轮只会**原样再提一次**，把同一个人问烦。
所以 `rejection_feedback()` 把它变成下一轮提示词里的一段——这是"人机协作"真正的含义。
"""

from __future__ import annotations

import base64
import dataclasses
import os
import urllib.parse
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
REPORT_DIR = ROOT / "state" / "reports"

#: 分支名模板（路线写死的形态：`auto/fix-{issue_id}`）
BRANCH_TEMPLATE = "auto/fix-{issue_id}"

#: 一次提交的 message 模板
COMMIT_MESSAGE = "fix({issue_id}): 由 repo-autopilot 生成的修复\n\n{summary}"


class PublishError(RuntimeError):
    """发布链路自身的错误（改动落在黑名单路径、REST 返回异常）。绝不静默继续。"""


@dataclasses.dataclass(frozen=True)
class FileChange:
    """
    一个要提交的改动：路径 + **改后的完整内容**（contents API 的语义）。

    `base_text` 是"这份改动基于的原文"。给了它，推送前会拿远端当前内容比对 ——
    对不上就**拒绝推送**：说明我们改的是一份别人已经改过的文件，
    硬推上去会把没看到的内容覆盖掉（这类覆盖不会报错，只会静静地删掉别人的改动）。
    """

    path: str
    text: str
    base_text: str | None = None


@dataclasses.dataclass(frozen=True)
class ReportInput:
    """修复报告的输入。五段**缺一不可**：路线写死的五个小节。"""

    issue_id: str
    repo: str
    issue_title: str
    root_cause: str
    changes: list[str]
    test_summary: str
    risks: str
    rollback: str
    patch_path: str = ""
    issue_body: str = ""

    def as_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


def render_report(data: ReportInput) -> str:
    """
    生成给人读的修复报告（五段式）。

    写给的是**要说"是"的那个人**：他要能在一分钟内判断"这次改动值不值得放行"。
    所以顺序是"根因 → 改了什么 → 测过什么 → 有什么风险 → 怎么回滚"——
    风险与回滚必须在**批准之前**看到，而不是出事后去翻日志。
    """
    changes = data.changes or ["（未记录改动）"]
    lines = [
        f"# 修复报告：{data.issue_id}",
        "",
        f"- 仓库：`{data.repo}`",
        f"- issue：{data.issue_title}",
    ]
    if data.issue_body:
        lines += ["", "> " + data.issue_body.strip().replace("\n", "\n> ")]
    lines += [
        "",
        "## 一、根因",
        "",
        data.root_cause or "（未记录根因 —— 这是缺陷：没有根因的修复无法复核）",
        "",
        "## 二、改动摘要",
        "",
    ]
    lines += [f"- {item}" for item in changes]
    lines += [
        "",
        "## 三、测试结果",
        "",
        data.test_summary or "（未记录测试结果 —— 这是缺陷：没有测试结果的修复不该被批准）",
        "",
        "## 四、风险点",
        "",
        data.risks or "（未评估风险 —— 这是缺陷）",
        "",
        "## 五、回滚方法",
        "",
        data.rollback or "（未写回滚方法 —— 这是缺陷）",
        "",
        "---",
        "",
        f"补丁文件：`{data.patch_path or '（无）'}`",
        "推送目标：新分支（不碰 main）+ PR；关掉 PR 即可回滚，main 分支没有任何改动。",
        "",
    ]
    return "\n".join(lines)


def write_report(data: ReportInput, directory: Path | None = None) -> Path:
    target_dir = directory or REPORT_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{data.issue_id}.md"
    target.write_text(render_report(data), encoding="utf-8")
    return target


def branch_name(issue_id: str) -> str:
    """`auto/fix-{issue_id}`。非法字符就地替换 —— 分支名坏了 push 会直接失败。"""
    safe = "".join(char if char.isalnum() or char in "-_." else "-" for char in issue_id)
    return BRANCH_TEMPLATE.format(issue_id=safe)


def check_publishable(changes: Sequence[FileChange]) -> None:
    """
    推送前先过 4.3 的黑名单：**改测试/CI/审批单的改动，一步都不许往外推**。
    这一层刻意与门禁共用同一张表（`src.gatekeep.BLACKLIST`），避免两处规则漂移。
    """
    from ..gatekeep import blacklist_hits

    hits = blacklist_hits([change.path for change in changes])
    if hits:
        raise PublishError(
            "改动落在黑名单路径上，拒绝推送：" + "；".join(f"{path}（{why}）" for path, why in hits)
        )


def normalize_newlines(text: str) -> str:
    """比较内容时忽略换行风格（CRLF/LF）—— 那是格式差异，不是内容差异。"""
    return text.replace("\r\n", "\n").replace("\r", "\n")


def fetch_remote_text(client: Any, repo: str, path: str, ref: str) -> str | None:
    """取远端某个 ref 上某个文件的文本；文件不存在返回 None。"""
    try:
        payload = client.get(f"/repos/{repo}/contents/{urllib.parse.quote(path)}", params={"ref": ref})
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(payload, dict) or not payload.get("content"):
        return None
    try:
        return base64.b64decode(payload["content"]).decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        return None


def verify_bases(
    client: Any, repo: str, base_branch: str, changes: Sequence[FileChange]
) -> None:
    """
    推送前校验：**我们改的是不是远端现在的那份内容**。

    实测踩过：README 在远端是 CRLF、本地读出来是 LF，于是"加一节 Install"变成了
    整文件重写（+40/-31），review 的人看到满屏 diff —— 而如果远端那份还有别人的改动，
    这一推就把它们静静覆盖了。所以：
    - 换行风格不同 → 按远端的风格改回来（保留格式）；
    - 内容（忽略换行）不同 → **拒绝**，让人重新取一次原文再改。
    """
    for change in changes:
        if change.base_text is None:
            continue
        remote = fetch_remote_text(client, repo, change.path, base_branch)
        if remote is None:
            continue          # 新文件：没有基线可比
        if normalize_newlines(remote) != normalize_newlines(change.base_text):
            raise PublishError(
                f"{change.path} 与远端 {base_branch} 上的内容不一致：拒绝推送。"
                "（否则会把没看到的内容覆盖掉 —— 请重新取远端原文再生成改动）"
            )


def adapt_newlines(text: str, reference: str) -> str:
    """把 `text` 的换行风格对齐到 `reference`（远端是 CRLF 就还它 CRLF）。"""
    if "\r\n" in reference and "\r\n" not in text:
        return text.replace("\n", "\r\n")
    if "\r\n" not in reference and "\r\n" in text:
        return text.replace("\r\n", "\n")
    return text


# ------------------------------------------------------------------ REST

def _write_client(write_token: Path | None = None) -> Any:
    """
    建一个**带写 token** 的客户端。

    取 token 的顺序：环境变量（闸门的 `write_token_scope` 会在执行期间放进去）→ 文件。
    这样既服从"token 只在执行期间短暂存在"的纪律，又不会因为在别的进程里调用而取不到。
    """
    from ..github import GitHubClient
    from ..github.tokens import WRITE_TOKEN_ENV, load_write_token

    token = os.environ.get(WRITE_TOKEN_ENV) or load_write_token(write_token)
    return GitHubClient(token, timeout=60)


def default_base_sha(client: Any, repo: str, base_branch: str) -> str:
    payload = client.get(f"/repos/{repo}/git/ref/heads/{base_branch}")
    return str(payload["object"]["sha"])


def push_changes(
    client: Any,
    *,
    repo: str,
    branch: str,
    base_branch: str,
    changes: Sequence[FileChange],
    message: str,
    base_sha: str | None = None,
) -> dict[str, Any]:
    """
    建分支 + 逐文件提交（GitHub contents API）。

    幂等：分支已存在就复用（重复执行不会因为"分支已存在"整条链路失败）；
    文件已存在时带上它的 `sha`，否则 API 会以 422 拒绝（这一条踩过）。
    """
    sha = base_sha or default_base_sha(client, repo, base_branch)
    created = client.request(
        "POST", f"/repos/{repo}/git/refs", body={"ref": f"refs/heads/{branch}", "sha": sha}
    )
    branch_created = created.status < 400
    if not branch_created and "already exists" not in str(created.body):
        raise PublishError(f"建分支失败：HTTP {created.status} {str(created.body)[:200]}")

    committed: list[dict[str, Any]] = []
    for change in changes:
        existing_sha: str | None = None
        try:
            current = client.get(
                f"/repos/{repo}/contents/{urllib.parse.quote(change.path)}",
                params={"ref": branch},
            )
            if isinstance(current, dict):
                existing_sha = str(current.get("sha")) if current.get("sha") else None
                encoded = current.get("content")
                if encoded:
                    decoded = base64.b64decode(encoded).decode("utf-8", errors="replace")
                    if decoded == change.text:
                        # 内容一模一样就别再提交一次：重复执行（例如第二次点头）不该
                        # 在仓库里留下一个空提交，那只会让 diff 变脏。
                        committed.append({"path": change.path, "skipped": "内容与分支上一致"})
                        continue
        except Exception:  # noqa: BLE001
            existing_sha = None      # 新文件：不带 sha 就是"创建"
        body: dict[str, Any] = {
            "message": message,
            "content": base64.b64encode(change.text.encode("utf-8")).decode("ascii"),
            "branch": branch,
        }
        if existing_sha:
            body["sha"] = existing_sha
        response = client.request(
            "PUT",
            f"/repos/{repo}/contents/{urllib.parse.quote(change.path)}",
            body=body,
        )
        if response.status >= 400:
            raise PublishError(
                f"提交 {change.path} 失败：HTTP {response.status} {str(response.body)[:200]}"
            )
        committed.append({"path": change.path, "sha": (response.body or {}).get("commit", {}).get("sha")})
    return {"branch": branch, "base_sha": sha, "branch_created": branch_created, "commits": committed}


def find_existing_pull_request(
    client: Any, *, repo: str, branch: str
) -> dict[str, Any] | None:
    """
    按**头分支**找已存在的开放 PR。

    为什么需要：发布链路必须**可重跑**。实测（2026-09-12 整理轮）：第一次 4.4 演练已经
    为 `auto/fix-e2e-publish` 建过 PR，第二次演练直接吃 `HTTP 422 A pull request already
    exists`，于是整条验收 BLOCKED —— 而"同一件事被再跑一次"在真实使用里太常见了
    （人类重发一次 `/处理反馈`、网络超时后重试、演练重跑）。**建 PR 不是幂等操作，
    但我们的链路必须是。**
    """
    owner = repo.split("/")[0]
    response = client.request(
        "GET",
        f"/repos/{repo}/pulls",
        params={"head": f"{owner}:{branch}", "state": "open", "per_page": 20},
    )
    if response.status >= 400:
        return None
    items = response.body if isinstance(response.body, list) else []
    for item in items:
        if not isinstance(item, dict):
            continue
        head = ((item.get("head") or {}) or {}).get("ref")
        if head == branch:
            return {"url": item.get("html_url"), "number": item.get("number"), "reused": True}
    return None


def create_pull_request(
    client: Any,
    *,
    repo: str,
    branch: str,
    base_branch: str,
    title: str,
    body: str,
) -> dict[str, Any]:
    response = client.request(
        "POST",
        f"/repos/{repo}/pulls",
        body={"title": title, "head": branch, "base": base_branch, "body": body},
    )
    if response.status >= 400:
        detail = str(response.body)
        # 只对"这个分支已经有 PR 了"网开一面：**其他 422 一律照旧报错**
        # （校验失败、权限不足、分支不存在都得让人看到，不能被"复用"掩盖掉）。
        if response.status == 422 and "already exists" in detail:
            existing = find_existing_pull_request(client, repo=repo, branch=branch)
            if existing is not None:
                existing["note"] = f"该分支已有开放 PR，直接复用（原错误：HTTP 422 {detail[:120]}）"
                return existing
            raise PublishError(
                f"建 PR 失败：HTTP 422 说该分支已有 PR，但按 head={branch} 查不到开放的 PR"
                f"（可能已被关闭或已合并，需要人工确认）：{detail[:200]}"
            )
        raise PublishError(f"建 PR 失败：HTTP {response.status} {detail[:300]}")
    payload = response.body if isinstance(response.body, dict) else {}
    return {"url": payload.get("html_url"), "number": payload.get("number")}


# ------------------------------------------------------------------ 两次闸门

@dataclasses.dataclass
class PublishResult:
    published: bool
    stage: str
    branch: str | None = None
    pr_url: str | None = None
    pr_number: int | None = None
    reason: str = ""
    tickets: list[str] = dataclasses.field(default_factory=list)
    queued: list[str] = dataclasses.field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


def publish(
    *,
    repo: str,
    issue_id: str,
    report: ReportInput,
    changes: Sequence[FileChange],
    base_branch: str = "main",
    conversation_reply: str | None = None,
    reply_scope: str | None = None,
    approvals_dir: Path | None = None,
    write_token: Path | None = None,
    outbox_dir: Path | None = None,
    mode: str | None = None,
    client: Any = None,
    report_path: Path | None = None,
) -> PublishResult:
    """
    报告 → 闸门(push) → 分支+提交 → 闸门(PR) → 建 PR。

    任何一步没拿到批准就**停下**并如实返回当前 stage —— "停下"本身是正确结果，不是失败。

    `reply_scope`：人类这一次的「是」**只批哪一个闸门**（`"push"` / `"pull_request"` / None=两个都批）。
    路线 4.4 要求"人类只说两次是"，而两次是说的是两个不同的后果（推分支 / 建 PR）；
    如果不区分范围，一次回复就会把两件事一起办了 —— 那等于把"公开 PR"藏进了"推送"的批准里。
    """
    from ..github import execute_if_approved, require_human_approval

    check_publishable(changes)
    # 报告**一定要落盘**：它是人类审批时看的东西。指定了目标路径就写到那里，
    # 否则写进 state/reports/。（踩过：只把路径记进变量、文件根本没写，
    # 结果"报告：xxx.md"这句话是假的。）
    path = write_report(report, report_path.parent) if report_path else write_report(report)
    branch = branch_name(issue_id)
    tickets: list[str] = []
    queued: list[str] = []

    push_reply = conversation_reply if reply_scope in (None, "push") else None
    pr_reply = conversation_reply if reply_scope in (None, "pull_request") else None

    push_ticket = require_human_approval(
        "push",
        summary=f"把修复推到 {repo} 的新分支 {branch}（不碰 {base_branch}）",
        impact=f"改动 {len(changes)} 个文件；报告见 {path.name}；main 分支不受影响",
        rollback=f"删除分支 {branch} 即可",
        conversation_reply=push_reply,
        request_key=f"push:{repo}:{issue_id}",
        approvals_dir=approvals_dir,
    )
    tickets.append(push_ticket.path.name)

    def do_push() -> dict[str, Any]:
        active = client if client is not None else _write_client(write_token)
        # 推送前先校验基线 + 对齐换行风格：把"整文件被当改了一遍"和"悄悄覆盖别人的改动"
        # 这两件事挡在写操作之前（它们都不会报错，只会让 diff 变得不可信）
        verify_bases(active, repo, base_branch, changes)
        aligned = [
            FileChange(
                path=change.path,
                text=adapt_newlines(
                    change.text,
                    fetch_remote_text(active, repo, change.path, base_branch) or change.text,
                ),
                base_text=change.base_text,
            )
            for change in changes
        ]
        return push_changes(
            active,
            repo=repo,
            branch=branch,
            base_branch=base_branch,
            changes=aligned,
            message=COMMIT_MESSAGE.format(issue_id=issue_id, summary=report.root_cause[:200]),
        )

    try:
        push_outcome = execute_if_approved(
            push_ticket,
            do_push,
            payload={"repo": repo, "branch": branch, "files": [c.path for c in changes]},
            write_token=write_token,
            outbox_dir=outbox_dir,
            mode=mode,
        )
    except PublishError as exc:
        # 安全拒绝（基线对不上等）不是异常崩溃，而是一个**明确的结果**：
        # 链路停在这里、单子留着、人能看到原因。
        return PublishResult(
            published=False,
            stage="push_refused",
            branch=branch,
            reason=f"拒绝推送：{exc}",
            tickets=tickets,
            queued=queued,
        )
    if push_outcome.queued_path is not None:
        queued.append(str(push_outcome.queued_path))
    if not push_outcome.executed:
        return PublishResult(
            published=False,
            stage="push",
            branch=branch,
            reason=push_outcome.reason,
            tickets=tickets,
            queued=queued,
        )

    pr_ticket = require_human_approval(
        "create_pull_request",
        summary=f"在 {repo} 上建 PR：{branch} → {base_branch}",
        impact="对外可见的 PR；不合并、不改默认分支；报告正文随 PR 一起公开",
        rollback="关闭 PR 并删除分支即可",
        conversation_reply=pr_reply,
        request_key=f"pr:{repo}:{issue_id}",
        approvals_dir=approvals_dir,
    )
    tickets.append(pr_ticket.path.name)

    def do_pr() -> dict[str, Any]:
        active = client if client is not None else _write_client(write_token)
        return create_pull_request(
            active,
            repo=repo,
            branch=branch,
            base_branch=base_branch,
            title=f"fix: {report.issue_title}",
            body=render_report(report),
        )

    pr_outcome = execute_if_approved(
        pr_ticket,
        do_pr,
        payload={"repo": repo, "head": branch, "base": base_branch},
        write_token=write_token,
        outbox_dir=outbox_dir,
        mode=mode,
    )
    if pr_outcome.queued_path is not None:
        queued.append(str(pr_outcome.queued_path))
    if not pr_outcome.executed:
        return PublishResult(
            published=False,
            stage="pull_request",
            branch=branch,
            reason=pr_outcome.reason,
            tickets=tickets,
            queued=queued,
        )

    payload = pr_outcome.result if isinstance(pr_outcome.result, dict) else {}
    return PublishResult(
        published=True,
        stage="done",
        branch=branch,
        pr_url=payload.get("url"),
        pr_number=payload.get("number"),
        # 复用已有 PR 时要说清楚：否则人类会以为"又建了一个"，而其实一个都没多
        reason="该分支已有开放 PR，已复用（链路幂等）" if payload.get("reused") else "已批准并推送",
        tickets=tickets,
        queued=queued,
    )


# ------------------------------------------------------------------ 拒绝回流

def rejection_feedback(ticket: Any, reason: str = "") -> str:
    """
    把"人类说否"变成**下一轮修复的输入**。

    没有这一段，人类否决等于白说：系统下一轮会原样再提一次。
    """
    status = getattr(ticket, "status", "unknown")
    summary = getattr(ticket, "summary", "")
    detail = (reason or "").strip() or "（人类没有写具体原因）"
    return (
        "上一轮改动被人类否决了，原因如下，请**按这个原因**重做，不要重复提交同一份改动：\n"
        f"- 被否决的动作：{summary}\n"
        f"- 闸门状态：{status}\n"
        f"- 人类给的原因：{detail}\n"
        "如果原因指向的是「方向错了」（例如该改另一个模块），请重新定位后再生成补丁。"
    )


def now_stamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
