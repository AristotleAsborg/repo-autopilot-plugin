"""批量处理（`/一键处理` 的引擎，路线 7.1 第 2 条的五条专项约束）。

    from src.batch import BatchState, plan, run_batch, render_sheet, apply_decisions

五条约束逐条落地：

1. **单次最多 20 个 issue**（`cap`），超出的留到下一轮并如实告知剩余数量；
2. 进度实时落盘 `state/batch_<date>.json`，重发同一指令从**断点续跑**，不重复处理；
3. 单个 issue 失败**不阻断批次**：标 `needs_human` 后继续下一个，失败清单进审批单附录；
4. 批次末尾生成 `state/approvals/batch_<date>.md`，**逐条**等人回是/否 ——
   批量呈现 ≠ 批量放行（这是 7.1 特别强调的一句）；
5. 批次进行中收到 `/应急` 立即暂停（`pause_requested` → 每处理完一条就停）。

## 为什么把"批量"做成显式的状态机

批量是最容易失控的场景：20 个 issue、每个都可能调模型/跑沙箱/推分支。
一旦进程被杀，没有落盘状态就**全部重来**（并可能重复推送）。
所以状态文件的写入发生在**每一条开始与结束**，而不是批次结束 —— 代价是几次文件写入，
换来的是"断了能接着跑"。
"""

from __future__ import annotations

import dataclasses
import json
import re
from collections.abc import Callable, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
STATE_DIR = ROOT / "state"
APPROVALS_DIR = STATE_DIR / "approvals"

#: 路线 7.1 第 1 条：单次最多 20 个
DEFAULT_CAP = 20

ITEM_PENDING = "pending"
ITEM_DONE = "done"
ITEM_NEEDS_HUMAN = "needs_human"
ITEM_SKIPPED = "skipped"


class BatchError(RuntimeError):
    """批次自身的错误（状态文件损坏）。绝不静默重跑。"""


@dataclasses.dataclass
class BatchItem:
    """批次里的一条。`ticket` 是它的审批单（有的话）。"""

    number: int
    title: str = ""
    state: str = ITEM_PENDING
    detail: str = ""
    risk: str = ""
    ticket: str = ""

    def as_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclasses.dataclass
class BatchState:
    date: str
    repo: str
    items: list[BatchItem] = dataclasses.field(default_factory=list)
    remaining: int = 0
    paused: bool = False
    started_at: str = ""
    updated_at: str = ""

    # ---------------------------------------------------------------- 落盘
    @property
    def path(self) -> Path:
        return STATE_DIR / f"batch_{self.date}.json"

    def save(self, directory: Path | None = None) -> Path:
        target = (directory / f"batch_{self.date}.json") if directory else self.path
        target.parent.mkdir(parents=True, exist_ok=True)
        self.updated_at = _now()
        target.write_text(json.dumps(self.as_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return target

    @classmethod
    def load(cls, date: str, directory: Path | None = None) -> BatchState:
        target = (directory / f"batch_{date}.json") if directory else STATE_DIR / f"batch_{date}.json"
        if not target.is_file():
            raise BatchError(f"没有这一天的批次状态：{target}")
        payload = json.loads(target.read_text(encoding="utf-8"))
        return cls(
            date=str(payload.get("date") or date),
            repo=str(payload.get("repo") or ""),
            items=[BatchItem(**item) for item in payload.get("items") or []],
            remaining=int(payload.get("remaining") or 0),
            paused=bool(payload.get("paused")),
            started_at=str(payload.get("started_at") or ""),
            updated_at=str(payload.get("updated_at") or ""),
        )

    # ---------------------------------------------------------------- 视图
    def as_dict(self) -> dict[str, Any]:
        return {
            "date": self.date,
            "repo": self.repo,
            "remaining": self.remaining,
            "paused": self.paused,
            "started_at": self.started_at,
            "updated_at": self.updated_at,
            "items": [item.as_dict() for item in self.items],
        }

    @property
    def finished(self) -> list[BatchItem]:
        return [item for item in self.items if item.state != ITEM_PENDING]

    @property
    def failures(self) -> list[BatchItem]:
        return [item for item in self.items if item.state == ITEM_NEEDS_HUMAN]

    @property
    def tickets(self) -> list[BatchItem]:
        return [item for item in self.items if item.state == ITEM_DONE and item.ticket]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ------------------------------------------------------------------ 计划与执行

def plan(issues: Sequence[tuple[int, str]], *, repo: str, date: str, cap: int = DEFAULT_CAP) -> BatchState:
    """
    把这一轮的 issue 列表切成"本次处理"与"留到下一轮"。

    **上限是硬约束**（路线 7.1 第 1 条）：超出的记进 `remaining`，
    报告里要如实告诉人类还剩多少 —— 不声不响地少处理会被当成"处理完了"。
    """
    chosen = list(issues)[:cap]
    state = BatchState(
        date=date,
        repo=repo,
        items=[BatchItem(number=number, title=title) for number, title in chosen],
        remaining=max(0, len(issues) - cap),
        started_at=_now(),
        updated_at=_now(),
    )
    return state


def run_batch(
    state: BatchState,
    handler: Callable[[BatchItem], dict[str, Any]],
    *,
    directory: Path | None = None,
    save_every_item: bool = True,
) -> BatchState:
    """
    逐条执行；**每条开始与结束都落盘**（断了能续跑，且不会重复处理已完成的）。

    `handler` 返回 `{"state": ..., "detail": ..., "risk": ..., "ticket": ...}`。
    抛异常 → 该条标 `needs_human`，**继续下一条**（路线 7.1 第 3 条）。
    """
    for item in state.items:
        if state.paused:
            break
        if item.state != ITEM_PENDING:
            continue                      # 断点续跑：已完成的不重做
        item.state = "doing"
        state.save(directory) if save_every_item else None
        try:
            result = handler(item) or {}
            item.state = str(result.get("state") or ITEM_DONE)
            item.detail = str(result.get("detail") or "")
            item.risk = str(result.get("risk") or "")
            item.ticket = str(result.get("ticket") or "")
        except Exception as exc:  # noqa: BLE001
            item.state = ITEM_NEEDS_HUMAN
            item.detail = f"{type(exc).__name__}: {str(exc)[:200]}"
        state.save(directory) if save_every_item else None
    state.save(directory)
    return state


def pause(state: BatchState, *, directory: Path | None = None) -> BatchState:
    """收到 `/应急` 时调用（路线 7.1 第 5 条）：当前条跑完就停，状态留在盘上。"""
    state.paused = True
    state.save(directory)
    return state


def resume(state: BatchState, *, directory: Path | None = None) -> BatchState:
    state.paused = False
    state.save(directory)
    return state


# ------------------------------------------------------------------ 审批

def render_sheet(state: BatchState) -> str:
    """
    批量审批单（路线 7.1 第 4 条）：**列出**这一批待批 PR，但**逐条**等人回是/否。

    批量呈现不等于批量放行 —— 所以单子里每条都有自己的编号与审批单文件名，
    人类回 "1 是 / 2 否" 这种逐条决定。
    """
    lines = [
        f"# 批量审批单：{state.repo}（{state.date}）",
        "",
        (
            f"- 本批 {len(state.items)} 个 issue：完成 {len(state.finished)}，"
            f"待人类 {len(state.failures)}"
        ),
        f"- 留到下一轮：{state.remaining} 个",
        "",
        "> **逐条回是/否**（例如 `1 是 / 2 否 / 3 是`）。批量呈现不等于批量放行：",
        "> 每一条 PR 都会单独走闸门，这里只是把它们的摘要放在一起给你看。",
        "",
        "| # | issue | 标题 | 结论 | 风险 |",
        "|---|---|---|---|---|",
    ]
    for index, item in enumerate(state.items, start=1):
        lines.append(
            f"| {index} | #{item.number} | {item.title[:40]} | {item.state}"
            f"{'：' + item.detail[:60] if item.detail else ''} | {item.risk[:40]} |"
        )
    if state.failures:
        lines += ["", "## 附录：需要人类处理的失败项", ""]
        for item in state.failures:
            lines.append(f"- #{item.number} {item.title[:40]}：{item.detail}")
    lines += [
        "",
        "## 逐条决定",
        "",
        "回复格式：`序号 是` 或 `序号 否`（用 / 分隔多条）。",
        "决定会被写进对应的审批单首行（留痕），而不是只记在对话里。",
        "",
    ]
    return "\n".join(lines)


def write_sheet(state: BatchState, directory: Path | None = None) -> Path:
    target_dir = directory or APPROVALS_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"batch_{state.date}.md"
    target.write_text(render_sheet(state), encoding="utf-8")
    return target


DECISION_RE = re.compile(r"(\d+)\s*[.、:：]?\s*(是|否)")


def parse_decisions(reply: str) -> dict[int, str]:
    """解析 `1 是 / 2 否` 这种逐条回复；没写的一律当作**没决定**（不默认批准）。"""
    decisions: dict[int, str] = {}
    for match in DECISION_RE.finditer(reply or ""):
        decisions[int(match.group(1))] = match.group(2)
    return decisions


def apply_decisions(
    state: BatchState, reply: str, *, approvals_dir: Path | None = None
) -> dict[str, Any]:
    """
    把逐条决定写进各自的审批单**首行**（闸门只读首行、只认一个字）。

    "没决定"与"否"是两回事：没写就是不批，绝不默认通过。
    """
    directory = approvals_dir or APPROVALS_DIR
    decisions = parse_decisions(reply)
    applied: list[dict[str, Any]] = []
    unknown: list[int] = []
    for index, item in enumerate(state.items, start=1):
        if index not in decisions:
            continue
        if not item.ticket:
            unknown.append(index)
            continue
        path = directory / item.ticket
        if not path.is_file():
            unknown.append(index)
            continue
        lines = path.read_text(encoding="utf-8").splitlines() or [""]
        lines[0] = decisions[index]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        applied.append({"index": index, "issue": item.number, "decision": decisions[index], "ticket": item.ticket})
    return {
        "applied": applied,
        "undecided": [index for index in range(1, len(state.items) + 1) if index not in decisions],
        "unknown": unknown,
    }
