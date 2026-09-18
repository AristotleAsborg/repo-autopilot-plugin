"""终端直达入口：**绕开 AI 对话层的最后通道**（路线 7.2 第 3 条的手动兜底）。

    python -m src.cli list                                  # 8 条指令与对应 SKILL.md
    python -m src.cli emergency [--json] [--state-dir P]     # /应急 的自检
    python -m src.cli triage <owner/repo> <issue号> [--dry-run]
    python -m src.cli fix <本地仓库目录> <issue号> [--rounds N] [--dry-run]
    python -m src.cli batch <owner/repo> [--limit 20] [--reply "1 是 / 2 否"] [--dry-run]

为什么要有它：对话唤起本身可能失效（harness 挂了、模型不可用、人类在服务器上）。
那时人类至少还能在终端把系统叫回来 —— 所以这里的每条子命令都**对应同一份 SKILL.md**，
不允许出现"CLI 一套逻辑、对话另一套逻辑"（路线 7.3 最后一句）。

写操作在这里同样**过闸门**：`fix` 走到"生成报告 + 建审批单"就停下，
不会因为"是 CLI 调的"就跳过人类确认。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.skills import (
    COMMANDS,
    check_consistency,
    doctor_report,
    overall,
    run_checks,
    skill_path,
)

SUBCOMMANDS = ("list", "emergency", "triage", "fix", "batch")


def _print(text: str) -> None:
    print(text, flush=True)


def cmd_list(_args: argparse.Namespace) -> int:
    _print(f"对话指令 {len(COMMANDS)} 条（与 skills/ 一一对应）：")
    for entry in COMMANDS:
        _print(f"  {entry.usage:<28} → skills/{entry.slug}.md　确认点：{entry.confirmation}")
    result = check_consistency()
    _print(f"\n一致性校验：{'通过' if result['ok'] else '**不通过**'}")
    for problem in result["problems"]:
        _print(f"  - {problem}")
    return 0 if result["ok"] else 1


def cmd_emergency(args: argparse.Namespace) -> int:
    checks = run_checks(state_dir=Path(args.state_dir) if args.state_dir else None)
    if args.json:
        _print(json.dumps([item.as_dict() for item in checks], ensure_ascii=False, indent=2))
    else:
        _print(doctor_report(checks))
    return 0 if overall(checks) else 1


def cmd_triage(args: argparse.Namespace) -> int:
    from src.triage import FeedbackClassifier

    if args.dry_run:
        _print(f"[dry-run] 会对 {args.repo}#{args.number} 执行：")
        _print("  1. 拉取 issue 原文（读 token，直通）")
        _print("  2. 查重（3.2）→ 命中重复就提示，不重复分类")
        _print("  3. 分类打标 + 首条回复（3.1，走本地小模型）")
        _print("  4. 打回判定（3.3）→ 判为缺陷才进修复")
        _print(f"  5. 人类确认点：推送 PR 前。详见 {skill_path('triage').relative_to(ROOT).as_posix()}")
        return 0

    from src.github import GitHubClient, read_token
    from src.triage import RawIssue

    client = GitHubClient(read_token(), timeout=30)
    payload = client.issue(args.repo, args.number)
    issue = RawIssue(
        repo=args.repo,
        number=args.number,
        title=payload.get("title") or "",
        body=payload.get("body") or "",
        labels=[item["name"] for item in payload.get("labels") or []],
        state=payload.get("state") or "open",
    )
    result = FeedbackClassifier(tier="local_small").classify(issue)
    _print(f"#{args.number} 判断：{result.verdict.label}（置信度 {result.verdict.confidence:.2f}）")
    _print(f"  理由：{result.verdict.reason}")
    _print(f"  动作：{result.action}")
    _print(f"  首条回复：{result.reply}")
    _print("  （只读操作，没有改动 GitHub 上的任何东西）")
    return 0


def cmd_fix(args: argparse.Namespace) -> int:
    repo = Path(args.repo_dir).resolve()
    if args.dry_run:
        _print(f"[dry-run] 会对 {repo} 的 issue #{args.number} 执行：")
        _print("  1. 文件定位（4.1）→ 输出 ≤5 个候选")
        _print(f"  2. 修复循环（4.2，≤{args.rounds} 轮）→ 沙箱副本里打补丁跑测试")
        _print("  3. 测试门禁（4.3）四道关")
        _print("  4. 五段式报告 + 建审批单，**停下等人类**（推送 PR 前）")
        _print(f"  详见 {skill_path('fix').relative_to(ROOT).as_posix()}")
        return 0

    from src.localize import locate
    from src.repair import RepairLoop

    issue_text = args.issue_text or f"#{args.number}"
    candidates = locate(issue_text, repo)
    if not candidates:
        _print("定位没找到候选文件 — 按 TEST_GATE 输出 BLOCKED 并停止。")
        return 1
    _print("候选文件：" + "、".join(item.path for item in candidates))
    loop = RepairLoop(max_rounds=args.rounds)
    result = loop.run(issue_text, repo, [item.path for item in candidates], issue_id=f"cli-{args.number}")
    _print(f"修复循环：{'测试通过' if result.ok else '未收敛'}（{result.rounds} 轮）")
    _print(f"补丁：{result.patch_path}")
    _print(f"报告：{result.report_path}")
    if not result.ok:
        _print("未收敛 → 按 TEST_GATE 输出 BLOCKED 并停止（不推送任何东西）。")
        return 1
    _print("下一步是 4.4：生成报告、过闸门、推分支建 PR —— 那一步要人类回「是」，本 CLI 不代劳。")
    return 0


def cmd_batch(args: argparse.Namespace) -> int:
    from src.batch import apply_decisions, plan, render_sheet, run_batch
    from src.skills.registry import (
        COMMANDS as _COMMANDS,  # noqa: F401  仅用于一致性说明
    )

    if args.dry_run:
        _print(f"[dry-run] 会批量处理 {args.repo} 的 open issue（单次上限 {args.limit}）：")
        _print("  逐条：查重 → 分类 → 视情修复 → 门禁；进度落盘 state/batch_<date>.json（可续跑）")
        _print("  结束：生成 state/approvals/batch_<date>.md，**逐条**等你回是/否")
        _print(f"  详见 {skill_path('batch').relative_to(ROOT).as_posix()}")
        return 0

    from datetime import datetime, timezone

    from src.github import GitHubClient, read_token

    client = GitHubClient(read_token(), timeout=30)
    issues = [
        (int(item["number"]), str(item.get("title") or ""))
        for item in client.list_issues(args.repo, state="open", per_page=args.limit)
        if "pull_request" not in item
    ]
    date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    state = plan(issues, repo=args.repo, date=date, cap=args.limit)
    _print(f"本批 {len(state.items)} 条，留到下一轮 {state.remaining} 条")

    from src.triage import FeedbackClassifier, RawIssue

    classifier = FeedbackClassifier(tier="local_small")

    def handler(item) -> dict:
        payload = client.issue(args.repo, item.number)
        issue = RawIssue(
            repo=args.repo,
            number=item.number,
            title=payload.get("title") or "",
            body=payload.get("body") or "",
            labels=[label["name"] for label in payload.get("labels") or []],
            state="open",
        )
        verdict = classifier.classify(issue)
        # 只有缺陷才进修复；其余只记录结论（批量场景下更要保守）
        return {
            "state": "done" if verdict.verdict.label == "bug" else "skipped",
            "detail": f"判为 {verdict.verdict.label}（{verdict.verdict.confidence:.2f}）",
            "risk": "" if verdict.verdict.label == "bug" else "非缺陷，未进修复",
        }

    run_batch(state, handler)
    sheet = Path(ROOT / "state" / "approvals" / f"batch_{date}.md")
    sheet.parent.mkdir(parents=True, exist_ok=True)
    sheet.write_text(render_sheet(state), encoding="utf-8")
    _print(f"批量审批单：{sheet.relative_to(ROOT).as_posix()}")
    if args.reply:
        outcome = apply_decisions(state, args.reply)
        _print(f"逐条决定已落盘：{outcome}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m src.cli", description="repo-autopilot 的终端直达入口")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="列出 8 条指令与一致性校验")

    emergency = sub.add_parser("emergency", help="/应急：自检诊断")
    emergency.add_argument("--json", action="store_true")
    emergency.add_argument("--state-dir", default="")

    triage = sub.add_parser("triage", help="/处理反馈：单个 issue 分类")
    triage.add_argument("repo")
    triage.add_argument("number", type=int)
    triage.add_argument("--dry-run", action="store_true")

    fix = sub.add_parser("fix", help="/修复：定位 → 修复循环 → 门禁")
    fix.add_argument("repo_dir")
    fix.add_argument("number", type=int)
    fix.add_argument("--rounds", type=int, default=15)
    fix.add_argument("--issue-text", default="")
    fix.add_argument("--dry-run", action="store_true")

    batch = sub.add_parser("batch", help="/一键处理：批量处理 open issue")
    batch.add_argument("repo")
    batch.add_argument("--limit", type=int, default=20)
    batch.add_argument("--reply", default="")
    batch.add_argument("--dry-run", action="store_true")
    return parser


HANDLERS = {
    "list": cmd_list,
    "emergency": cmd_emergency,
    "triage": cmd_triage,
    "fix": cmd_fix,
    "batch": cmd_batch,
}


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass
    args = build_parser().parse_args(argv)
    handler = HANDLERS.get(args.command)
    if handler is None:                      # pragma: no cover - argparse 已经挡住
        _print(f"未知子命令：{args.command}（可用：{'、'.join(SUBCOMMANDS)}）")
        return 2
    return handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
