"""3.3 标签打回通路的内核：**打回不是删除，是把错误记下来**。

路线步骤 3.3 把打回定义成三条来源（4.2 开工前预检 / 作者自纠 / 人工复核），
本文件实现其中最容易被做错的那一半：**记账与状态机**。

## 三条不能违背的规矩

1. **打回不丢数据**：任务文件里追加 `payload["bounces"][]`，一条都不删。
   打回是"退回重判"，不是"失败作废" —— 两者的区别决定这条 issue 以后还会不会被处理。
2. **同一条被打回 ≥2 次就停下**（`MAX_BOUNCES`）：反复打回说明**判据本身有病**，
   继续自动流转只会消耗轮次、把问题推给下一个人。停下来的动作是 `needs_human`
   —— 这正是路线 0.5 的"需要人类介入时停下来"。
3. **记账 ≠ 改真值**：作者说"这不是 bug"可以记账（`source="author"`），
   但**真值只有人裁决才能改**（见 `adjudicated.py`）。否则一个会喊的用户就能改掉事实。

## 为什么动作分级复用 1.4 的闸门，而不是新造一个

改 issue 的 label 是**写操作**，与 push 同性质：一旦判错，外面就多了一个被贴错标签的
issue（甚至被关）。闸门是这套系统里唯一被验证过的"人类点头才动"的机制，
新造第二个只会有两种结果：要么它更松（等于绕过闸门），要么它更严（等于多一道没人维护的门）。
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
from collections.abc import Callable, Sequence
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
STATE_DIR = ROOT / "state"
TASK_STATES = ("pending", "doing", "done", "failed")

#: 同一条 issue 被打回这么多次就停下来找人：判据有病，继续流转是浪费
MAX_BOUNCES = 2

#: 改 label 的置信度门槛（路线 3.1 第 3 条：≥0.7 才改 label，<0.7 只发建议评论）
RELABEL_CONFIDENCE = 0.70

#: 开工前预检的门槛。比改标签高：预检的动作是"拒绝修"，比"发条评论"重得多
PRECHECK_CONFIDENCE = 0.80

PHASE_TRIAGED = "triaged"
PHASE_FIXING = "fixing"
PHASE_NEEDS_HUMAN = "needs_human"


class BounceError(RuntimeError):
    """打回通路自身的错误（非法原因/来源、任务文件坏了）。绝不吞掉。"""


class BounceReason(str, Enum):
    """打回原因。**枚举而不是自由文本**：自由文本没法统计，没法统计就没法改进判据。"""

    NOT_REPRODUCIBLE = "not_reproducible"      # 复现不了
    NOT_A_DEFECT = "not_a_defect"              # 是功能请求/提问，不是缺陷
    UNCLEAR_REQUEST = "unclear_request"        # 没说清要什么
    WRONG_LABEL = "wrong_label"                # 标签与正文不符（仓库标错了）
    DUPLICATE_OF = "duplicate_of"              # 其实是别的 issue 的重复
    AUTHOR_DENIED = "author_denied"            # 作者本人否认（记账用，不等于改真值）


#: 打回的三条来源（路线 3.3 第 1 条），多一个都要报错
BOUNCE_SOURCES = ("precheck", "author", "human_review")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclasses.dataclass(frozen=True)
class Bounce:
    """一次打回。`fingerprint` 让"同一条事实被报两遍"不会记成两次。"""

    reason: BounceReason
    by: str
    source: str
    original_label: str | None = None
    suggested_label: str | None = None
    note: str = ""
    at: str = ""

    def __post_init__(self) -> None:
        reason = self.reason
        if not isinstance(reason, BounceReason):
            try:
                reason = BounceReason(str(reason))
            except ValueError as exc:
                raise BounceError(
                    f"{reason!r} 不是合法的打回原因。合法值：{[r.value for r in BounceReason]}"
                ) from exc
            object.__setattr__(self, "reason", reason)
        if self.source not in BOUNCE_SOURCES:
            raise BounceError(f"{self.source!r} 不是合法来源。合法值：{list(BOUNCE_SOURCES)}")
        if not (self.by or "").strip():
            raise BounceError("打回必须记录『谁打的』——没有责任人的记录等于没有记录")
        if not self.at:
            object.__setattr__(self, "at", _now())

    def fingerprint(self) -> str:
        """按事实去重：同一来源、同一原因、同一对标签，只算一次。"""
        raw = "|".join(
            [
                self.reason.value,
                self.by,
                self.source,
                self.original_label or "",
                self.suggested_label or "",
            ]
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]

    def as_dict(self) -> dict[str, Any]:
        return {
            "reason": self.reason.value,
            "by": self.by,
            "source": self.source,
            "original_label": self.original_label,
            "suggested_label": self.suggested_label,
            "note": self.note,
            "at": self.at,
            "fingerprint": self.fingerprint(),
        }


@dataclasses.dataclass(frozen=True)
class BounceOutcome:
    """记账结果。`recorded=False` 表示这条事实已经在账上了（幂等）。"""

    recorded: bool
    count: int
    phase: str
    needs_human: bool
    reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


# ------------------------------------------------------------------ 任务文件

def find_task(task_id: str, state_dir: Path | None = None) -> Path | None:
    """在四个状态子目录里找任务文件。找不到就返回 None（调用方决定怎么办）。"""
    base = (state_dir or STATE_DIR) / "tasks"
    for name in TASK_STATES:
        candidate = base / name / f"{task_id}.json"
        if candidate.exists():
            return candidate
    return None


def load_task(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BounceError(f"任务文件读不出来或不是 JSON：{path}（{exc}）") from exc


def save_task(path: Path, data: dict[str, Any]) -> None:
    """先写临时文件再整体替换：半截 JSON 会让下一个人工读到一个坏任务。"""
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def bounces_of(task: dict[str, Any]) -> list[dict[str, Any]]:
    payload = task.get("payload") or {}
    entries = payload.get("bounces")
    return list(entries) if isinstance(entries, list) else []


def phase_of(task: dict[str, Any]) -> str:
    return str((task.get("payload") or {}).get("phase") or PHASE_TRIAGED)


def apply_bounce(task: dict[str, Any], bounce: Bounce) -> BounceOutcome:
    """
    把一次打回记进任务（**纯函数，不碰磁盘** —— 这样规则可以被测试钉死）。

    幂等：同一个指纹已经在账上就不再追加，但 **count 仍然按事实数算**，
    所以"同一事实报两遍"既不会重复计数，也不会把 `needs_human` 顶上去。
    """
    existing = bounces_of(task)
    fingerprint = bounce.fingerprint()
    if any(entry.get("fingerprint") == fingerprint for entry in existing):
        return BounceOutcome(
            recorded=False,
            count=len(existing),
            phase=phase_of(task),
            needs_human=phase_of(task) == PHASE_NEEDS_HUMAN,
            reason="同一事实已在账上（幂等）",
        )

    existing.append(bounce.as_dict())
    count = len(existing)
    needs_human = count >= MAX_BOUNCES
    phase = PHASE_NEEDS_HUMAN if needs_human else PHASE_TRIAGED

    payload = dict(task.get("payload") or {})
    payload["bounces"] = existing
    payload["phase"] = phase
    payload["bounce_count"] = count
    task["payload"] = payload
    return BounceOutcome(
        recorded=True,
        count=count,
        phase=phase,
        needs_human=needs_human,
        reason=(
            f"已记账（第 {count} 次）"
            + ("；达到上限，转 needs_human 等人类定夺" if needs_human else "")
        ),
    )


def record_bounce(
    task_path: Path, bounce: Bounce, *, task: dict[str, Any] | None = None
) -> BounceOutcome:
    """落盘版：读 → 记账 → 写回。"""
    data = task if task is not None else load_task(task_path)
    outcome = apply_bounce(data, bounce)
    if outcome.recorded:
        save_task(task_path, data)
    return outcome


# ------------------------------------------------------------------ 开工前预检

def precheck(
    repo_label: str | None,
    model_label: str | None,
    confidence: float,
    *,
    by: str = "precheck",
) -> Bounce | None:
    """
    4.2 的第一步：**这条 issue 真的该修吗？**（路线 3.3 第 1 条来源 (a)）

    判据刻意只用两件已知事实（仓库标签、模型判断 + 置信度），因为它要在"进修复循环之前"
    运行 —— 那时还没有任何代码级信息（能不能复现、是不是真崩溃），
    而等到有了那些信息才知道不该修，已经浪费了一轮模型调用和一次人类审批。

    返回 `None` 表示"照常修"。
    """
    if not repo_label or not model_label:
        return None
    if confidence < PRECHECK_CONFIDENCE:
        return None
    repo_label = repo_label.lower()
    model_label = model_label.lower()
    if repo_label == model_label:
        return None
    if repo_label == "bug" and model_label in ("feature", "question"):
        return Bounce(
            reason=BounceReason.NOT_A_DEFECT,
            by=by,
            source="precheck",
            original_label=repo_label,
            suggested_label=model_label,
            note=f"标签写着 bug，但模型以 {confidence:.2f} 的置信度判为 {model_label}",
        )
    if repo_label in ("feature", "enhancement", "question") and model_label == "bug":
        # 仓库标签与正文不符：不是"不修"，而是"别照着错的标签修"
        return Bounce(
            reason=BounceReason.WRONG_LABEL,
            by=by,
            source="precheck",
            original_label=repo_label,
            suggested_label=model_label,
            note=f"标签写着 {repo_label}，但模型以 {confidence:.2f} 的置信度判为 bug",
        )
    return None


# ------------------------------------------------------------------ 动作分级

def relabel_decision(confidence: float) -> str:
    """
    置信度 → 动作（路线 3.1 第 3 条）。

    - `propose`：≥0.7，可以向人提议改标签（仍要过闸门）；
    - `comment_only`：<0.7，只发"建议标签"评论，**不动标签**。

    门槛不写死在调用方：判据只有一处，改的时候不会漏。
    """
    return "propose" if confidence >= RELABEL_CONFIDENCE else "comment_only"


def request_relabel(
    *,
    repo: str,
    number: int,
    original_label: str,
    suggested_label: str,
    confidence: float,
    reason: str,
    conversation_reply: str | None = None,
    approvals_dir: Path | None = None,
) -> Any:
    """
    走 1.4 的闸门请求改标签。**这是提议，不是执行** —— 执行由 `execute_relabel` 负责，
    而它只在 ticket 已批准时才调用 `perform`。
    """
    from ..github import require_human_approval

    return require_human_approval(
        "relabel",
        summary=f"把 {repo}#{number} 的标签从 {original_label} 改成 {suggested_label}",
        impact=(
            f"置信度 {confidence:.2f}；依据：{reason}。"
            "改错标签会让后续的路由（修复队列 / 追问 / 仅回复）走错分支"
        ),
        rollback=f"把 {repo}#{number} 的标签改回 {original_label} 即可，正文与评论都不动",
        conversation_reply=conversation_reply,
        request_key=f"relabel:{repo}#{number}",
        approvals_dir=approvals_dir,
    )


def execute_relabel(
    ticket: Any,
    perform: Callable[[], Any],
    *,
    payload: dict[str, Any] | None = None,
    write_token: Path | None = None,
    outbox_dir: Path | None = None,
    mode: str | None = None,
) -> Any:
    """经闸门执行改标签。离线时进 outbox，绝不丢。"""
    from ..github import execute_if_approved

    return execute_if_approved(
        ticket,
        perform,
        payload=payload,
        write_token=write_token,
        outbox_dir=outbox_dir,
        mode=mode,
    )


# ------------------------------------------------------------------ 指标

def bounce_rates(rows: Sequence[tuple[bool, str]]) -> dict[str, float]:
    """
    两个必须分开看的比例。每行是 `(是否被打回, 仓库标签)`：

    - `bounce_rate` = 打回数 / 总数：打回通路有没有在动（长期 0 = 形同虚设）；
    - `false_bounce_rate` = **把标着 bug 的 issue 打回**的比例：这才是代价 ——
      真有缺陷却拒绝修，比多修一个不必要的东西严重得多（路线：误杀率是更硬的那条线）。
    """
    total = len(rows) or 1
    bounced = [(flag, label) for flag, label in rows if flag]
    return {
        "bounce_rate": round(len(bounced) / total, 4),
        "false_bounce_rate": round(sum(1 for _, label in bounced if label == "bug") / total, 4),
    }
