"""6.1 验收：**一句 idea → 骨架 → 本地 CI 干跑 → 停下等人类选名并批准建仓**。

    python tools/eval_scaffold.py                     # 生成 + 本地干跑 + 建审批单（退出码 2）
    python tools/eval_scaffold.py --check             # 验收模式：把"停在闸门"当成通过
    python tools/eval_scaffold.py --name demo-cli --reply 是 --approve create
                                                      # 人类选完名并批准后：建仓 + 首推 + 等 CI

## 人类只出现两次，且都是决策

1. **选名**（三选一，回复序号）——产品决策；
2. **批准建仓**（回复「是」）——写操作过闸门。

其余全自动：生成、落盘、**沙箱里跑全量测试**、CI 配置本地 dry-run、建仓、首推、轮询 CI。
路线 0.5 说得很清楚：运行期还要人类动手配置，说明构建期没收尾 —— 所以这里没有配置步骤。

## 为什么本地必须先跑通过

一个新仓库第一次 CI 就红，会让后面所有"CI 绿"的说法一起失去意义。
所以本脚本在**上传之前**就把 CI 要跑的每条命令在沙箱副本里跑一遍，红了就地停下。
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.scaffold import (
    ScaffoldError,
    check_names,
    ci_commands,
    create_repository,
    dry_run,
    first_push,
    generate_until_green,
    init_local_repository,
    name_candidate_notes,
    name_candidates,
    owner_exists,
    summary_block,
    wait_for_ci,
    write_report,
)

DEFAULT_IDEA = "一个把 Markdown 表格转成 CSV 的小工具，带命令行入口和单元测试"
KEYWORDS = ("markdown", "table", "csv")


def sandbox_checks(directory: Path, *, python: str) -> tuple[bool, str]:
    """沙箱里的全量测试 + CI 干跑（复制副本执行，源目录只读）。"""
    from src.sandbox import run as sandbox_run

    lines: list[str] = []
    passed = True
    for index, command in enumerate(ci_commands(python)):
        outcome = sandbox_run(
            directory,
            test_cmd=command,
            task_id=f"eval-scaffold-{index}",
        )
        ok = bool(getattr(outcome, "passed", False))
        passed = passed and ok
        lines.append(f"  [沙箱] {' '.join(command)} → exit {getattr(outcome, 'exit_code', '?')} {'OK' if ok else 'FAIL'}")
    return passed, "\n".join(lines)


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

    parser = argparse.ArgumentParser(description="6.1 半自动建新仓库")
    parser.add_argument("--idea", default=DEFAULT_IDEA)
    parser.add_argument("--local", action="store_true", help="只把项目保存在本地：不建远端仓库、不首推、不轮询 CI（GitHub 可选）")
    parser.add_argument("--name", default="", help="人类选定的仓库名")
    parser.add_argument("--reply", default=None, help="人类对建仓的回复：是 / 否")
    parser.add_argument("--approve", choices=["create"], default=None)
    parser.add_argument("--check", action="store_true", help="验收模式：停在闸门算通过")
    parser.add_argument("--skip-generation", action="store_true", help="复用本地已生成的目录（不调模型）")
    args = parser.parse_args()

    from src.github import (
        GitHubClient,
        execute_if_approved,
        read_token,
        require_human_approval,
    )

    client = GitHubClient(read_token(), timeout=60)
    owner = client.viewer()["login"]
    # **先确认这块地存在**（2026-09-17 总报告 R6）：`is_name_available()` 对不存在的 owner
    # 也返回 True（GitHub 对"仓库不存在"与"owner 不存在"都给 404），
    # 那会让所有候选名显示"可用"，直到建库那一刻才 404 失败。
    if owner and not owner_exists(client, owner):
        print(f"owner 不存在：{owner} —— 检查 --owner 或 token 对应的账号")
        return 2
    approvals = ROOT / "state" / "approvals"
    write_token = ROOT / "state" / ".write_token"

    if args.skip_generation:
        directory = ROOT / "state" / "scaffold" / (args.name or "demo")
        if not directory.is_dir():
            print(f"没有可复用的生成目录：{directory}")
            return 1
        scaffold = None
        print(f"复用已生成目录：{directory}")
    else:
        print(f"idea：{args.idea}")
        # 生成 → 本地 CI 干跑 → 不过就把失败输出喂回去重来（最多 3 轮）
        scaffold, dry, directory = generate_until_green(args.idea, name=args.name)
        print(f"已生成 {len(scaffold.files)} 个文件 → {directory.relative_to(ROOT).as_posix()}")

    # ---- 本地：沙箱全量测试 + CI 干跑（两条路都跑，互为佐证）
    dry = dry_run(directory)
    sandbox_ok, sandbox_text = sandbox_checks(directory, python=sys.executable)
    print("本地 CI 干跑（就地）：", "通过" if dry["passed"] else "**未通过**")
    print("本地 CI 干跑（沙箱副本）：", "通过" if sandbox_ok else "**未通过**")
    print(sandbox_text)
    if not (dry["passed"] and sandbox_ok):
        print("本地就没过 —— 按路线 6.1 第 3 条，不许上传。")
        for item in dry["results"]:
            print(f"  {item['command']} → exit {item['exit']}\n{item['tail'][-800:]}")
        return 1

    # ---- **本地模式（GitHub 可选）**：只把项目保存在本地，不建远端仓库、不首推、不轮询 CI。
    # 2026-09-18 人类要求："把连接 github 作为可选项，允许把项目保存在本地"。
    # 这条链路里真正有价值的部分（追问 → spec → 骨架 → 本地 CI 干跑）本来就不需要联网；
    # 强制要 GitHub 写权限，等于把"想用这套东西"变成"先交出权限"。
    if args.local:
        saved = init_local_repository(directory)
        print()
        print("**本地模式**（没有创建任何远端仓库、没有用任何 token）")
        print(f"  · 目录：{saved['path']}")
        print(f"  · 分支：{saved['branch']}　提交：{saved['commit'] or '（无）'}　{saved['note']}")
        print("  · 想要远端时再显式开（去掉 --local，或用 `/建新项目` 的远端分支）")
        return 0

    # ---- 名字：三个候选 + 查重
    candidates = name_candidates(args.idea, keywords=KEYWORDS)
    for note in name_candidate_notes(args.idea, candidates, keywords=KEYWORDS):
        print(f"  ⚠️ {note}")       # P1-14：候选不足/裸词撞名风险，不许沉默
    names = check_names(client, owner, candidates)
    summary = summary_block(scaffold, dry, names) if scaffold else "(复用模式，无摘要)"
    print()
    print(summary)

    if scaffold:
        report = write_report(scaffold, dry)
        print(f"骨架报告：{report.relative_to(ROOT).as_posix()}")

    # ---- 闸门：建仓（人类第二次点头）
    chosen = args.name or ""
    if not chosen:
        print()
        print("等你两件事：")
        print("  1. 选一个仓库名（回序号 1/2/3，或直接回名字）")
        print("  2. 批准建仓（回「是」）")
        print("  然后重跑：python tools/eval_scaffold.py --name <名字> --reply 是 --approve create")
        return 0 if args.check else 2

    ticket = require_human_approval(
        "create_repo",
        summary=f"在 {owner} 下新建公共仓库 {chosen} 并推送骨架（{len(scaffold.files) if scaffold else '?'} 个文件）",
        impact="新建一个公共仓库；不触碰任何已有仓库；CI 工作流会立刻跑一次",
        rollback=f"删除仓库 {owner}/{chosen} 即可（刚建的仓库没有下游依赖）",
        conversation_reply=args.reply,
        request_key=f"create_repo:{owner}:{chosen}",
        approvals_dir=approvals,
    )

    def perform() -> dict:
        # **建仓和首推必须用写 token**：上面那个 client 是读 token 的（查名字可用性用的），
        # 拿它去建仓会得到 403 "Resource not accessible by personal access token" ——
        # 实测踩过：报错看起来像"权限不够"，其实是这里传错了客户端。
        from src.github import GitHubClient
        from src.github.tokens import WRITE_TOKEN_ENV, load_write_token

        token = os.environ.get(WRITE_TOKEN_ENV) or load_write_token(write_token)
        writer = GitHubClient(token, timeout=60)
        info = create_repository(writer, chosen, description=(scaffold.summary if scaffold else "generated"))
        pushed = first_push(
            writer,
            scaffold,
            full_name=info["full_name"],
            default_branch=info["default_branch"],
        )
        ci = wait_for_ci(client, info["full_name"], timeout_seconds=600)
        return {"repo": info, "push": pushed, "ci": ci}

    try:
        outcome = execute_if_approved(
            ticket,
            perform,
            payload={"name": chosen, "owner": owner},
            write_token=write_token,
            outbox_dir=ROOT / "state" / "outbox",
        )
    except ScaffoldError as exc:
        # 失败要**说清楚**而不是抛栈：审批单留着、原因写明，人一眼知道该改什么
        print()
        print(f"建仓失败：{exc}")
        print(f"审批单（已批准，无需再问）：state/approvals/{ticket.path.name}")
        return 1
    print()
    print(f"闸门：{'已批准并执行' if outcome.executed else outcome.reason}")
    if not outcome.executed:
        print(f"审批单：state/approvals/{ticket.path.name}")
        return 0 if args.check else 2

    result = outcome.result or {}
    repo = result.get("repo") or {}
    ci = result.get("ci") or {}
    print(f"仓库：{repo.get('html_url')}")
    print(f"首推：{len((result.get('push') or {}).get('commits', []))} 个文件")
    print(f"云端 CI：status={ci.get('status')} conclusion={ci.get('conclusion')} {ci.get('url')}")
    ok = ci.get("conclusion") == "success"
    print(f"结论：{'CI 绿，从一句 idea 到可 clone 的仓库' if ok else '**CI 尚未绿**（如实报告，不当作成功）'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
