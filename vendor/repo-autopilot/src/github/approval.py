"""人类闸门（路线 1.4 第 2、3、4、5 条）。

## 为什么闸门必须在代码里，而不是提示词里

路线 0.3 安全红线：所有对外写操作必须过**代码级**人类闸门，实现在 API 封装层。
理由很直白：提示词是建议，代码是约束。写操作漏过一次，后果不可逆
（推错分支、删错文件、建错仓库），而且"我明明说过要批准"这种话事后毫无用处。

## 闸门语义（照抄路线原文，逐条实现）

* 写 `state/approvals/{id}.md`：动作、影响面、回滚方法、72h 过期时间；
* 人类在**文件首行**写 `是`，或在对话中回复 `是` → 执行；
  **其他任何措辞（"好的""可以""yes 吧"）一律视为未确认**；
* 72h 无响应 → 自动作废，任务回 failed。

## 为什么"挂起"不实现成阻塞

这是回合制 agent。真阻塞在一个 `while` 里等于把会话卡死，人类也没机会回话。
所以"挂起"= **建单并返回 pending，本轮到此结束**；下一轮拿同一个 `request_key`
再问一次，闸门去读文件看有没有批。`request_key` 保证同一个待批动作不会重复建单。
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import os
import re
import uuid
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from .tokens import STATE_DIR, write_token_scope

ROOT = Path(__file__).resolve().parents[2]
APPROVALS_DIR = STATE_DIR / "approvals"
OUTBOX_DIR = STATE_DIR / "outbox"
MODE_PATH = STATE_DIR / "mode.json"

APPROVAL_WORD = "是"
REJECTION_WORD = "否"
DEFAULT_TTL_HOURS = 72

# 路线 1.4 第 2 条列的写操作清单。**闸门只认这些动作名**，
# 不在清单里的动作一律拒绝执行 —— 这样"新加一个写操作却忘了给它加闸"会立刻报错，
# 而不是悄悄绕过闸门。这一条是防"忘记"的，比防"故意"更重要。
WRITE_ACTIONS = frozenset(
    {
        "push",
        "create_repo",
        "create_pull_request",
        "release",
        "change_default_branch",
        "change_repo_settings",
        "delete_file",
        "close_other_issue",
        # 路线 3.3：打回通路提议改 issue 的 label —— 与 push 同性质（判错就在外面留下
        # 一个贴错标签的 issue），所以必须走同一道闸门。**关闭 issue 依然只能走
        # close_other_issue，不存在"顺手关掉"的新动作。**
        "relabel",
    }
)

STATUS_PENDING = "pending"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"
STATUS_EXPIRED = "expired"

_METADATA_RE = re.compile(r"```json\s*(\{.*?\})\s*```", re.DOTALL)


class GateError(RuntimeError):
    """闸门自身的错误（配置错、动作名非法等）。绝不吞掉。"""


@dataclasses.dataclass
class ApprovalTicket:
    id: str
    action: str
    summary: str
    impact: str
    rollback: str
    created_at: str
    expires_at: str
    path: Path
    request_key: str | None = None
    status: str = STATUS_PENDING
    decided_at: str | None = None
    # 给人类看的提示（例如"你的「是」写在第 3 行，闸门只读第 1 行"）。
    # 它**不参与判定**，只用来解释为什么还是 pending。
    note: str | None = None

    @property
    def executable(self) -> bool:
        return self.status == STATUS_APPROVED


@dataclasses.dataclass
class GateOutcome:
    executed: bool
    reason: str
    result: Any = None
    queued_path: Path | None = None


def assert_write_action(action: str) -> None:
    if action not in WRITE_ACTIONS:
        raise GateError(
            f"{action!r} 不在写操作清单里。清单：{sorted(WRITE_ACTIONS)}。"
            "新增写操作时必须同时把它加进 WRITE_ACTIONS，否则它就能绕过闸门。"
        )


# ------------------------------------------------------------------ 时间

def _now(moment: dt.datetime | None = None) -> dt.datetime:
    """
    统一产出**带时区**的时间。

    为什么必须带时区：过期时间是安全属性（72 小时后自动作废）。不带时区的话，
    换台机器、或者单子在不同时区之间传递，过期判断就会悄悄偏几小时 ——
    这种错误不会报错，只会让一张早该作废的单子继续有效。
    """
    if moment is None:
        return dt.datetime.now().astimezone()
    return moment if moment.tzinfo else moment.astimezone()


def _iso(moment: dt.datetime) -> str:
    # 带偏移量写出：人看到的仍是本地时间，同时不留歧义
    return moment.strftime("%Y-%m-%dT%H:%M:%S%z")


def _parse_iso(text: str) -> dt.datetime | None:
    """解析时间戳。优先认带偏移量的；早期没有偏移量的单子按本地时间处理。"""
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S"):
        try:
            # 这套格式串刻意**不带 %z 的兜底分支**：落盘的单子里既有带偏移量的新格式，
            # 也有早期没有偏移量的旧格式，两种都得认（见上面 `astimezone()` 的归一）。
            # 这条 noqa 是"知情不改"：改成强制 %z 会让**已经批准过的旧单子全部解析失败**，
            # 闸门会因此把人类早先点过的头当成没点过 —— 那比 lint 告警严重得多。
            parsed = dt.datetime.strptime(text, fmt)  # noqa: DTZ007
        except ValueError:
            continue
        return parsed if parsed.tzinfo else parsed.astimezone()
    return None


def _near_miss(path: Path) -> str | None:
    """
    第 1 行没动，但「是」出现在了文件别处 —— 这是最容易犯的错。

    实测发生过一次：人类把「是」写在了说明文字那一行的开头，闸门按规矩读第 1 行，
    于是状态一直是 pending，而人类以为已经批准了，双方都在等对方。
    闸门**不因此放宽**（放宽就等于把"只读首行"这条规则作废，而规则正是它的价值），
    但必须把这件事明确说出来，否则人会一直等一个不会发生的批准。
    """
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    for index, line in enumerate(lines[1:], start=2):
        stripped = line.strip()
        if not stripped:
            continue
        if stripped == APPROVAL_WORD:
            return (
                f"注意：第 {index} 行有一个单独的「{APPROVAL_WORD}」，"
                f"但闸门只读**第 1 行**。请把第 1 行整行改成只有一个「{APPROVAL_WORD}」字。"
            )
        if stripped.startswith(APPROVAL_WORD) and len(stripped) > len(APPROVAL_WORD):
            return (
                f"注意：第 {index} 行以「{APPROVAL_WORD}」开头但后面还有别的内容，"
                f"而闸门只读**第 1 行**、且要求那一行只有一个「{APPROVAL_WORD}」字。"
            )
    return None


# -------------------------------------------------------------- 判定措辞

def is_explicit_yes(text: str | None) -> bool:
    """
    只认"去掉首尾空白后恰好是『是』"。

    这不是严格过头，而是**故意的**：路线明确要求"其他任何措辞一律视为未确认"。
    模糊匹配（包含"是"就算）会把"是的吧""是不是不用了"也放行 —— 而闸门放行一次的
    代价是不可逆的写操作。宁可多问一轮。
    """
    if text is None:
        return False
    return text.strip().lstrip("\ufeff").strip() == APPROVAL_WORD


def is_explicit_no(text: str | None) -> bool:
    if text is None:
        return False
    return text.strip().lstrip("\ufeff").strip() == REJECTION_WORD


def _first_line(path: Path) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            return handle.readline()
    except OSError:
        return ""


# ------------------------------------------------------------------ 建单

def _render(ticket: ApprovalTicket) -> str:
    metadata = {
        "id": ticket.id,
        "action": ticket.action,
        "summary": ticket.summary,
        "impact": ticket.impact,
        "rollback": ticket.rollback,
        "created_at": ticket.created_at,
        "expires_at": ticket.expires_at,
        "request_key": ticket.request_key,
    }
    return "\n".join(
        [
            (f"把本行整行改成只有一个「{APPROVAL_WORD}」字即为批准"
            f"（拒绝则整行改成只有一个「{REJECTION_WORD}」字）"),
            "",
            "<!-- 闸门只读**第 1 行**，且要求那一行只有一个字。",
            f"     写在第 2 行、第 3 行、或者「{APPROVAL_WORD}好的」都不算数。",
            "     其他任何说法（好的 / 可以 / OK / yes / 批准了）也都不算数。",
            "     写好后无需通知，下一轮我会自己来读。 -->",
            "",
            f"# 审批单 `{ticket.id}`",
            "",
            f"- **动作**：`{ticket.action}`",
            f"- **做什么**：{ticket.summary}",
            f"- **影响面**：{ticket.impact}",
            f"- **怎么回滚**：{ticket.rollback}",
            f"- **创建时间**：{ticket.created_at}",
            f"- **过期时间**：{ticket.expires_at}（{DEFAULT_TTL_HOURS} 小时无响应自动作废）",
            "",
            "## 下面是机器读区，请勿手改",
            "",
            "```json",
            json.dumps(metadata, ensure_ascii=False, indent=2),
            "```",
            "",
        ]
    )


def _write_new(path: Path, text: str) -> None:
    """只在文件不存在时创建。避免和正在编辑它的人类抢同一个文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


def _record_decision(path: Path, word: str) -> None:
    """
    把**对话里给出的决定**记回单子的第 1 行。

    **为什么必须记**（2026-09-17 实测）：单子有两个决定来源 —— 文件第 1 行、对话回复 ——
    而对话那一路原来**不留痕**。后果很实在：`scripts/doctor.py` 的"闸门悬挂审批"
    是**读文件**判定的，于是**所有已经在对话里批过的单子**一直显示"超过 72 小时没处理"，
    `/体检` 因此永远报"不通过"。**永远红的检查等于没有检查** —— 这正是这个系统反复在防的东西。

    记的是**人类说的那个字**，不是系统伪造的：`conversation_reply` 本来就是人类的回复，
    这一步只是"把话记下来"，判据一个字没放松（读的时候照样要求那一行只有一个字）。
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return
    lines = text.splitlines(keepends=True)
    if not lines:
        return
    lines[0] = f"{word}\n"
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text("".join(lines), encoding="utf-8")
    os.replace(temporary, path)


def _parse_metadata(path: Path) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise GateError(f"读不了审批单 {path}: {exc}") from exc
    matches = _METADATA_RE.findall(text)
    if not matches:
        raise GateError(f"审批单 {path} 里找不到机器读区（可能被手改坏了）")
    return json.loads(matches[-1])


def _load(path: Path) -> ApprovalTicket:
    data = _parse_metadata(path)
    return ApprovalTicket(
        id=str(data.get("id") or path.stem),
        action=str(data.get("action") or ""),
        summary=str(data.get("summary") or ""),
        impact=str(data.get("impact") or ""),
        rollback=str(data.get("rollback") or ""),
        created_at=str(data.get("created_at") or ""),
        expires_at=str(data.get("expires_at") or ""),
        path=path,
        request_key=data.get("request_key"),
    )


def _find_pending(request_key: str, approvals_dir: Path) -> Path | None:
    for candidate in sorted(approvals_dir.glob("*.md"), reverse=True):
        try:
            data = _parse_metadata(candidate)
        except GateError:
            continue
        if data.get("request_key") == request_key:
            return candidate
    return None


def request_approval(
    action: str,
    *,
    summary: str,
    impact: str,
    rollback: str,
    request_key: str | None = None,
    approvals_dir: Path | None = None,
    ttl_hours: int = DEFAULT_TTL_HOURS,
    now: dt.datetime | None = None,
) -> ApprovalTicket:
    """
    建一张审批单（同一个 `request_key` 已有单子时直接返回它）。

    注意：本函数**不执行任何写操作**，它只负责把"我想做什么"落到人类能读的地方。
    """
    assert_write_action(action)
    directory = approvals_dir or APPROVALS_DIR
    directory.mkdir(parents=True, exist_ok=True)

    if request_key:
        existing = _find_pending(request_key, directory)
        if existing is not None:
            return decide(_load(existing), now=now)

    created = _now(now)
    ticket = ApprovalTicket(
        id=uuid.uuid4().hex[:12],
        action=action,
        summary=summary,
        impact=impact,
        rollback=rollback,
        created_at=_iso(created),
        expires_at=_iso(created + dt.timedelta(hours=ttl_hours)),
        path=directory / f"{created.strftime('%Y%m%d-%H%M%S')}-{action}-{uuid.uuid4().hex[:6]}.md",
        request_key=request_key,
    )
    _write_new(ticket.path, _render(ticket))
    return ticket


# ------------------------------------------------------------------ 判定

def decide(
    ticket: ApprovalTicket,
    *,
    conversation_reply: str | None = None,
    now: dt.datetime | None = None,
) -> ApprovalTicket:
    """
    读审批单 + 可选的人类对话回复，给出**当前有效状态**。

    两处来源只要有一处是明确的「是」就算批准；但两处都必须是**恰好一个「是」字**。
    过期优先于批准：单子已过期，之后再写「是」也不生效（否则一张清理不及时的单子
    会变成长期有效的万能通行证）。

    **对话里给出的决定会被记回单子第 1 行**（`_record_decision`，2026-09-17）：
    否则"对话里批过"的单子在任何**读文件**的检查眼里都还是"没处理"——
    `scripts/doctor.py` 的悬挂审批检查就是这么被永远点红的。
    「否」同样有分支（以前没有，会落到 pending → 反复追问，与"拒绝之后不再请求"冲突）。
    """
    moment = _now(now)

    # 文件首行
    line = _first_line(ticket.path)
    file_yes = is_explicit_yes(line)
    file_no = is_explicit_no(line)

    if ticket.expires_at:
        expires = _parse_iso(ticket.expires_at)
        if expires is not None and moment > expires:
            # 过期**优先于一切**，包括已经写在文件里的「是」。
            # 否则一张没人清理的旧单子会变成长期有效的万能通行证：
            # 任何时候补一个「是」就生效，闸门等于不存在。
            ticket.status = STATUS_EXPIRED
            return ticket

    if file_no:
        ticket.status = STATUS_REJECTED
        ticket.decided_at = _iso(moment)
        return ticket
    if is_explicit_no(conversation_reply):
        # 「对话里说不」以前**没有分支** → 落到下面的 pending，于是同一件事会被反复问
        # （这与"人类拒绝之后不许再请求批准"直接冲突）。现在如实记成 rejected，
        # 并把那个「否」记回单子（`require_human_approval` 对 rejected 会直接返回，不再追问）。
        ticket.status = STATUS_REJECTED
        ticket.decided_at = _iso(moment)
        _record_decision(ticket.path, REJECTION_WORD)
        return ticket
    if is_explicit_yes(conversation_reply):
        ticket.status = STATUS_APPROVED
        ticket.decided_at = _iso(moment)
        _record_decision(ticket.path, APPROVAL_WORD)
        return ticket
    if file_yes:
        ticket.status = STATUS_APPROVED
        ticket.decided_at = _iso(moment)
        return ticket

    ticket.status = STATUS_PENDING
    # 仍是 pending 时，检查是不是"写错了位置"这种最常见的误解，并明确说出来
    ticket.note = _near_miss(ticket.path)
    return ticket


def require_human_approval(
    action: str,
    *,
    summary: str,
    impact: str,
    rollback: str,
    conversation_reply: str | None = None,
    request_key: str | None = None,
    approvals_dir: Path | None = None,
    ttl_hours: int = DEFAULT_TTL_HOURS,
    now: dt.datetime | None = None,
) -> ApprovalTicket:
    """路线里的那个入口：建单/续单，并把当前状态判定出来。"""
    ticket = request_approval(
        action,
        summary=summary,
        impact=impact,
        rollback=rollback,
        request_key=request_key,
        approvals_dir=approvals_dir,
        ttl_hours=ttl_hours,
        now=now,
    )
    if ticket.status in (STATUS_APPROVED, STATUS_REJECTED, STATUS_EXPIRED):
        return ticket
    return decide(ticket, conversation_reply=conversation_reply, now=now)


# ------------------------------------------------------------------ 执行

def _mode() -> str:
    """
    读取在线/离线状态。

    刻意复用 gateway 的 `read_mode()`，而不是在这儿再写一遍解析：
    同一个文件被两处用不同方式解析，是"改了一处忘了另一处"的经典来源，
    而这个值决定写操作是**执行**还是**排队** —— 那种不一致的代价很大。
    """
    from ..gateway import read_mode

    return str(read_mode(MODE_PATH).get("mode") or "online")


def queue_for_offline(
    action: str, *, summary: str, payload: dict[str, Any], outbox_dir: Path | None = None
) -> Path:
    """断联时的写操作落 outbox 排队（路线 1.4 第 5 条：绝不丢弃）。"""
    directory = outbox_dir or OUTBOX_DIR
    directory.mkdir(parents=True, exist_ok=True)
    # 这里刻意用**本地时间**：outbox 的文件名与 `queued_at` 要与本模块历史上落盘的单子
    # 可比（旧单子按本地时间写的，见 `_parse_iso`）。改成 UTC 会让同一天的两批文件
    # 排序错乱，而这块的判据是"谁先排队"。
    target = directory / f"{dt.datetime.now().strftime('%Y%m%d-%H%M%S')}-{action}-{uuid.uuid4().hex[:6]}.json"  # noqa: DTZ005
    target.write_text(
        json.dumps(
            {
                "action": action,
                "summary": summary,
                "payload": payload,
                "queued_at": _iso(dt.datetime.now()),  # noqa: DTZ005
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return target


def execute_if_approved(
    ticket: ApprovalTicket,
    perform: Callable[[], Any],
    *,
    payload: dict[str, Any] | None = None,
    write_token: Path | None = None,
    outbox_dir: Path | None = None,
    mode: str | None = None,
) -> GateOutcome:
    """
    只有 `ticket.status == approved` 时才调用 `perform`。

    这是整个安全红线的**唯一执行点**：验收要证明的就是"未确认时 `perform` 一次都没被调用"。
    因此这里的判断刻意写成"正向白名单"——只有 approved 这一个值能走到 `perform`，
    而不是"排除几种情况后就执行"（后者迟早会漏掉新加的状态）。
    """
    assert_write_action(ticket.action)

    if ticket.status != STATUS_APPROVED:
        return GateOutcome(executed=False, reason=f"未获批准（{ticket.status}）")

    current_mode = mode if mode is not None else _mode()
    if current_mode == "offline":
        target = queue_for_offline(
            ticket.action, summary=ticket.summary, payload=payload or {}, outbox_dir=outbox_dir
        )
        return GateOutcome(executed=False, reason="offline：已进 outbox 排队", queued_path=target)

    with write_token_scope(write_token):
        result = perform()
    return GateOutcome(executed=True, reason="已批准并执行", result=result)


def approval_files(directory: Path | None = None) -> Sequence[Path]:
    target = directory or APPROVALS_DIR
    if not target.exists():
        return []
    return sorted(target.glob("*.md"))
