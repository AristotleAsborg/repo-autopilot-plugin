"""`/应急` 的自检清单（路线 7.2 第 4 条，落地为 `scripts/doctor.py`）。

七项检查（路线原文的顺序）：state 目录完整性、`mode.json` 合法性、队列孤儿任务、
闸门悬挂审批（>72h 标出）、写 token 文件、本地小模型探活、GitHub 连通性。

## 两条设计原则

1. **每一项都要给出"人接下来能做什么"**：诊断不是把红叉丢给人。
   输出用 0.5.B 模板的 **A/B/C 式修复选项**，并且注明"修复动作仍需过闸门"。
2. **探活失败不一定算坏**：offline 模式下连不上 GitHub 是**预期**，不是故障 ——
   检查项要区分"坏了"和"现在本来就该连不上"，否则 `/应急` 会在断网时误报一片红。

所有外部依赖（GitHub 客户端、本地模型探活、当前时间）都可注入，测试里不联网。
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
STATE_DIR = ROOT / "state"

#: `state/` 下必须有这些子目录（少了哪个，对应模块就跑不起来）
REQUIRED_DIRS = (
    "tasks/pending",
    "tasks/doing",
    "tasks/done",
    "tasks/failed",
    "approvals",
    "outbox",
    "reports",
    "corpus",
    "specs",
    "vectors",
    "patches",
    "repair",
)

#: 悬挂审批的判定线（路线 1.4：72 小时未处理标出来）
STALE_APPROVAL_HOURS = 72
#: 认领后多久没动静算孤儿任务
ORPHAN_CLAIM_HOURS = 6


@dataclasses.dataclass
class Check:
    """一项自检。`options` 是给人选的修复动作（A/B/C），空列表表示"无需动作"。"""

    name: str
    ok: bool
    detail: str
    options: list[str] = dataclasses.field(default_factory=list)
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


def _now(now: datetime | None = None) -> datetime:
    return now or datetime.now(timezone.utc)


def _parse(text: str | None) -> datetime | None:
    if not text:
        return None
    try:
        stamp = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError:
        return None
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)


def check_state_dirs(state_dir: Path) -> Check:
    missing = [name for name in REQUIRED_DIRS if not (state_dir / name).is_dir()]
    if missing:
        return Check(
            "state 目录完整性",
            False,
            f"缺少 {len(missing)} 个子目录：{'、'.join(missing)}",
            [
                "A. 让我重建缺失目录（只建目录，不动已有数据）",
                "B. 你自己确认后再建（可能是有意删掉的）",
                "C. 跳过这一项，继续看其它检查",
            ],
        )
    return Check("state 目录完整性", True, f"{len(REQUIRED_DIRS)} 个子目录齐全")


def check_mode(state_dir: Path) -> Check:
    path = state_dir / "mode.json"
    if not path.is_file():
        return Check(
            "mode.json 合法性",
            False,
            "文件不存在（网关与闸门都要读它决定 online/offline）",
            ["A. 让我按 online 重建", "B. 你告诉我应该是 offline", "C. 跳过"],
        )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return Check("mode.json 合法性", False, f"不是合法 JSON：{exc}", ["A. 让我重置为 online", "B. 你手动修", "C. 跳过"])
    mode = str(payload.get("mode") or "")
    if mode not in ("online", "offline"):
        return Check("mode.json 合法性", False, f"mode={mode!r} 不是 online/offline", ["A. 让我改成 online", "B. 你指定", "C. 跳过"])
    return Check("mode.json 合法性", True, f"mode={mode}")


def check_orphan_tasks(state_dir: Path, *, now: datetime | None = None) -> Check:
    doing = state_dir / "tasks" / "doing"
    if not doing.is_dir():
        return Check("队列孤儿任务", True, "没有 doing 目录（无任务在跑）")
    moment = _now(now)
    orphans: list[str] = []
    active = 0
    for path in sorted(doing.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            orphans.append(f"{path.name}（文件损坏）")
            continue
        claimed = _parse(payload.get("claimed_at"))
        if claimed is None or moment - claimed > timedelta(hours=ORPHAN_CLAIM_HOURS):
            orphans.append(f"{path.name}（认领于 {payload.get('claimed_at') or '未知'}）")
        else:
            active += 1
    if orphans:
        return Check(
            "队列孤儿任务",
            False,
            f"{len(orphans)} 个任务被认领后再无动静：{'；'.join(orphans[:3])}",
            [
                "A. 让我把它们退回 pending 重跑",
                "B. 让我标成 failed 并写失败报告",
                "C. 先不动（可能只是跑得慢）",
            ],
        )
    return Check("队列孤儿任务", True, f"{active} 个任务在跑，无孤儿")


def check_stale_approvals(state_dir: Path, *, now: datetime | None = None) -> Check:
    approvals = state_dir / "approvals"
    if not approvals.is_dir():
        return Check("闸门悬挂审批", True, "没有 approvals 目录（没有待批单）")
    moment = _now(now)
    stale: list[str] = []
    pending = 0
    for path in sorted(approvals.glob("*.md")):
        text = path.read_text(encoding="utf-8", errors="replace")
        if text.splitlines() and text.splitlines()[0].strip() in ("是", "否"):
            continue                      # 已决定
        created = None
        for line in text.splitlines():
            if line.startswith("- **创建时间**"):
                created = _parse(line.split("：", 1)[-1].strip())
                break
        if created is None or moment - created > timedelta(hours=STALE_APPROVAL_HOURS):
            pending += 1
            stale.append(path.name)
    if stale:
        return Check(
            "闸门悬挂审批",
            False,
            f"{len(stale)} 张单子超过 {STALE_APPROVAL_HOURS} 小时没处理：{'；'.join(stale[:3])}",
            [
                "A. 你现在逐张决定（是/否写首行）",
                "B. 让我把它们标成 expired（需要过闸门）",
                "C. 保留，我知道它们在那儿",
            ],
        )
    return Check("闸门悬挂审批", True, f"{pending} 张待批单，均未超时")


def check_write_token(state_dir: Path) -> Check:
    path = state_dir / ".write_token"
    if not path.is_file():
        return Check(
            "写 token 文件",
            False,
            f"不存在：{path}（任何写操作都做不了，但读操作不受影响）",
            ["A. 你按 0.3 的清单放一个写 token", "B. 暂时不写（只做只读流程）", "C. 跳过"],
        )
    size = path.stat().st_size
    ignored = True
    try:
        import subprocess

        # 先分清"不是 git 工作树"和"git 说它没被忽略"（2026-09-12 安装副本逼出来的修法）：
        # 装到别的机器上的副本**没有 .git**，此时 `git check-ignore` 返回 128，
        # 原来的写法把它读成"没被忽略"，于是给人一条**假的泄漏警报**
        # （"一旦提交就是不可逆泄漏"）—— 而没有 git 就没有历史，也就无从泄漏。
        inside = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=str(state_dir.parent),
            capture_output=True,
            text=True,
            check=False,
        )
        if inside.returncode != 0 or inside.stdout.strip() != "true":
            return Check(
                "写 token 文件",
                True,
                f"存在（{size} 字节）；**不在 git 工作树里** —— 本项不适用"
                "（没有版本库就没有历史，也就无从泄漏）",
            )
        ignored = (
            subprocess.run(
                ["git", "check-ignore", "-q", "state/.write_token"],
                cwd=str(state_dir.parent),
                capture_output=True,
                check=False,
            ).returncode
            == 0
        )
    except Exception:  # noqa: BLE001
        ignored = True                     # 没有 git 就不强求
    if not ignored:
        return Check(
            "写 token 文件",
            False,
            "存在但**没有被 .gitignore 覆盖** —— 一旦提交就是不可逆泄漏",
            ["A. 让我把它加进 .gitignore（并检查历史）", "B. 你要轮换这个 token", "C. 跳过"],
        )
    return Check("写 token 文件", True, f"存在（{size} 字节）且已被 git 忽略")


def check_local_model(probe: Callable[[], tuple[bool, str]] | None = None) -> Check:
    if probe is None:
        def probe() -> tuple[bool, str]:  # type: ignore[misc]
            from ..gateway import embed

            try:
                vector = embed(["探活"])
                return True, f"embedding 维度 {vector.shape[-1]}"
            except Exception as exc:  # noqa: BLE001
                return False, f"{type(exc).__name__}: {str(exc)[:120]}"

    try:
        ok, detail = probe()
    except Exception as exc:  # noqa: BLE001
        ok, detail = False, f"{type(exc).__name__}: {str(exc)[:120]}"
    if not ok:
        return Check(
            "本地小模型探活",
            False,
            detail,
            [
                "A. 我帮你启动 Ollama 并重试",
                "B. 你手动 `ollama serve` 后我重试",
                "C. 切到 offline 模式（只跑不依赖本地模型的流程）",
            ],
        )
    return Check("本地小模型探活", True, detail)


def check_github(
    client: Any = None, *, mode: str = "online"
) -> Check:
    if client is None:
        try:
            from ..github import GitHubClient, read_token

            client = GitHubClient(read_token(), timeout=15)
        except Exception as exc:  # noqa: BLE001
            return Check("GitHub 连通性", False, f"读 token 不可用：{type(exc).__name__}", ["A. 你补一个读 token", "B. 走 offline", "C. 跳过"])
    try:
        payload = client.get("/rate_limit")
        remaining = (payload or {}).get("resources", {}).get("core", {}).get("remaining")
        return Check("GitHub 连通性", True, f"可达，核心配额剩余 {remaining}")
    except Exception as exc:  # noqa: BLE001
        if mode == "offline":
            # 离线模式连不上是**预期**，不是故障 —— 这条区分很重要，
            # 否则断网时 /应急 会报一片红，把人往错的方向带。
            return Check("GitHub 连通性", True, f"offline 模式：跳过（{type(exc).__name__}）", note="离线模式下的预期状态")
        return Check(
            "GitHub 连通性",
            False,
            f"{type(exc).__name__}: {str(exc)[:120]}",
            ["A. 让我按第六部分进入 offline 模式", "B. 检查网络/代理后重试", "C. 跳过"],
        )


def run_checks(
    *,
    state_dir: Path | None = None,
    client: Any = None,
    local_probe: Callable[[], tuple[bool, str]] | None = None,
    now: datetime | None = None,
) -> list[Check]:
    """跑七项检查（顺序与路线 7.2 第 4 条一致，便于对照）。"""
    directory = state_dir or STATE_DIR
    mode = "online"
    mode_file = directory / "mode.json"
    if mode_file.is_file():
        try:
            mode = str(json.loads(mode_file.read_text(encoding="utf-8")).get("mode") or "online")
        except json.JSONDecodeError:
            mode = "online"
    return [
        check_state_dirs(directory),
        check_mode(directory),
        check_orphan_tasks(directory, now=now),
        check_stale_approvals(directory, now=now),
        check_write_token(directory),
        check_local_model(local_probe),
        check_github(client, mode=mode),
    ]


def doctor_report(checks: list[Check], *, include_gate_note: bool = True) -> str:
    """输出诊断报告：结论 → 每项 → **A/B/C 式修复选项**（0.5.B 模板）。"""
    failed = [item for item in checks if not item.ok]
    lines = [
        "# 应急自检报告",
        "",
        f"- 检查项：{len(checks)}　通过：{len(checks) - len(failed)}　**异常：{len(failed)}**",
        f"- 时间：{_now().strftime('%Y-%m-%d %H:%M:%S')}",
        "",
    ]
    for item in checks:
        mark = "OK  " if item.ok else "FAIL"
        lines.append(f"- `[{mark}]` **{item.name}**：{item.detail}" + (f"（{item.note}）" if item.note else ""))
    lines.append("")
    for item in failed:
        lines += [f"## {item.name}", "", f"- 现象：{item.detail}"]
        if item.options:
            lines += ["", "需要你选一个（回 A / B / C）："] + [f"  {option}" for option in item.options]
        lines.append("")
    if failed and include_gate_note:
        lines += [
            "---",
            "",
            "**注意**：上面任何修复动作只要涉及对外写操作，都必须先过人类闸门（0.3 安全红线）。",
            "",
        ]
    return "\n".join(lines)


def overall(checks: list[Check]) -> bool:
    return all(item.ok for item in checks)


def environment_summary(checks: list[Check]) -> str:
    """一行摘要，放进对话的"结论"里（0.5 规范：人类只想知道要不要动手）。"""
    failed = [item.name for item in checks if not item.ok]
    if not failed:
        return "七项自检全部通过，系统状态正常。"
    return f"{len(failed)} 项异常：{'、'.join(failed)}（详见自检报告）"


def state_dir_size(state_dir: Path | None = None) -> int:
    directory = state_dir or STATE_DIR
    total = 0
    for path in directory.rglob("*"):
        if path.is_file():
            total += path.stat().st_size
    return total


def token_file_mode(path: Path) -> str:
    """给人类看权限（Windows 上 ACL 无法收紧到 600，如实报告）。"""
    try:
        return oct(path.stat().st_mode & 0o777)
    except OSError:
        return "unknown"
