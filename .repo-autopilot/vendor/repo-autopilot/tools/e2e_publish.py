"""4.4 端到端演练：**真实 bug issue → 门禁 → 人类两次「是」 → PR 创建**。

    python tools/e2e_publish.py                      # 走到闸门就停下，等人类点头
    python tools/e2e_publish.py --reply 是            # 人类点头：推分支（然后停下等第二次）
    python tools/e2e_publish.py --reply 是            # 再点头：建 PR

## 这个演练刻意分两次点头

路线 4.4 的验收是"人类只说两次'是'"。所以本脚本**每次运行只推进一个闸门**：

1. 第一次跑：建 push 审批单 → 停下（退出码 2）；
2. `--reply 是`：推分支 → 建 PR 审批单 → 停下（退出码 2）；
3. 再 `--reply 是`：建 PR → 退出码 0，打印 PR 链接。

重跑是安全的：分支已存在会被复用，内容与分支上一致的文件会被**跳过提交**
（不会留下空提交，见 `src/publish`）。

## 它演练的是链路，不是模型

缺陷用的是 1.6 那个确定性桩（README 缺 Install 段），因为这一步要证明的是
"报告 → 闸门 → 分支 → PR"这条路走得通、且**没有点头就走不动**，而不是"模型修得好不好"
（那是 4.2 的事）。这样演练不依赖 flash，随时可重跑。
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.gatekeep import run_gate
from src.publish import FileChange, ReportInput, publish
from src.repair import propose_patch
from src.sandbox import apply_patch

FIXTURE = ROOT / "tests" / "fixtures" / "sandbox-repos" / "sandbox-clean"
WORK = ROOT / "state" / "e2e-publish"
REPORTS = ROOT / "state" / "reports"

ISSUE_TITLE = "README 里没有安装步骤"
ISSUE_BODY = (
    "新同事 clone 下来之后不知道要装什么、用什么命令跑测试。"
    "README 只讲了 layout 和 usage，没有 Install 一节。建议补上。"
)


def prepare_change(issue_id: str) -> tuple[Path, FileChange, str]:
    """用 1.6 的桩造出真实可用的补丁，并在副本里验证它真的落得上。"""
    proposal = propose_patch(FIXTURE, ISSUE_TITLE, ISSUE_BODY, "feature")
    if not proposal.produced_patch:
        raise SystemExit(f"桩没有产出补丁：{proposal.description}")

    if WORK.exists():
        shutil.rmtree(WORK, ignore_errors=True)
    WORK.mkdir(parents=True, exist_ok=True)
    patch_path = WORK / f"{issue_id}.diff"
    patch_path.write_text(proposal.patch_text, encoding="utf-8")

    workdir = WORK / "repo"
    # 忽略 pytest 在 fixture 里留下的 ACL 锁死目录（实测 copytree 会以 WinError 5 失败）
    shutil.copytree(
        FIXTURE,
        workdir,
        ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache", "pytest-cache-files-*"),
    )
    applied, detail = apply_patch(workdir, patch_path, WORK / "apply.log")
    if not applied:
        raise SystemExit(f"补丁在副本里打不上：{detail}")
    content = (workdir / proposal.target).read_text(encoding="utf-8")
    return patch_path, FileChange(proposal.target, content), proposal.description


def gate_summary(patch_path: Path) -> tuple[bool, str]:
    """跑 4.3 的四道关，把结论写进报告 —— 报告里的"测试结果"必须是**跑出来的**。"""
    verdict = run_gate(
        FIXTURE,
        patch_path,
        target_cmd=[sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"],
        full_cmd=[sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"],
        lint_cmd=[sys.executable, "-m", "ruff", "check", "."],
    )
    gates = verdict.gates
    summary = (
        f"- 目标测试：{'全绿' if gates.get('target', {}).get('ok') else '红'}\n"
        f"- 全量回归：{'无新增失败' if gates.get('regression', {}).get('ok') else '有新增失败'}"
        f"（新增 {len(verdict.new_failures)} 条）\n"
        f"- lint：{'无新增告警' if gates.get('lint', {}).get('ok') else '有新增告警'}"
        f"（基线 {gates.get('lint', {}).get('baseline')} → {gates.get('lint', {}).get('count')}）\n"
        f"- 路径黑名单：{'通过' if gates.get('blacklist', {}).get('ok') else '命中'}\n"
        f"- 补丁应用：{'成功' if gates.get('apply', {}).get('ok') else '失败'}"
    )
    return verdict.passed, summary


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

    parser = argparse.ArgumentParser(description="4.4 发布链路端到端演练")
    parser.add_argument("--repo", default="", help="目标仓库 owner/name；默认 <你的账号>/sandbox-clean")
    parser.add_argument("--issue", default="e2e-publish", help="issue 标识（进分支名与审批单）")
    parser.add_argument("--reply", default=None, help="人类回复：是 / 否")
    parser.add_argument(
        "--approve",
        choices=["push", "pr"],
        default=None,
        help="这一次的「是」批的是哪个闸门（给了 --reply 就必须给）。"
        "路线 4.4 要的是两次点头，而两次说的是两个不同的后果 —— 不区分范围的话，"
        "一次回复会把推分支和建 PR 一起办了",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="验收模式：把「停在闸门等人类」当成通过（退出码 0）。"
        "验收要证明的是**链路通且没有点头就走不动**；真实的 PR 由人类两次点头后另行演练",
    )
    args = parser.parse_args()

    if args.reply and not args.approve:
        print("给了 --reply 就必须给 --approve push|pr：一次点头只批一个闸门。")
        return 1

    if not FIXTURE.is_dir():
        print(f"缺少陪练仓库：{FIXTURE}\n先跑：python tools/sandbox_repos.py build")
        return 1

    repo = args.repo
    if not repo:
        from src.github import GitHubClient, read_token

        owner = GitHubClient(read_token(), timeout=30).viewer()["login"]
        repo = f"{owner}/sandbox-clean"

    patch_path, change, description = prepare_change(args.issue)
    print(f"补丁：{patch_path.relative_to(ROOT).as_posix()}（{len(change.text)} 字节）— {description}")

    passed, summary = gate_summary(patch_path)
    print("门禁四道关：")
    print(summary)
    if not passed:
        print("门禁没过 —— 按路线 4.3，这一步不该继续往外推。")
        return 1

    report = ReportInput(
        issue_id=args.issue,
        repo=repo,
        issue_title=ISSUE_TITLE,
        issue_body=ISSUE_BODY,
        root_cause="README 缺少 Install 一节：新人 clone 后不知道装什么、怎么跑测试。",
        changes=[f"{change.path}：补上 Install 段落（安装命令 + Python 版本要求 + 依赖说明）"],
        test_summary=summary,
        risks="只改文档，不影响运行时代码；对已有 README 结构无破坏（插在 Usage 之前）。",
        rollback="关闭 PR 并删除分支即可；目标仓库的默认分支没有任何改动。",
        patch_path=patch_path.relative_to(ROOT).as_posix(),
    )
    report_path = REPORTS / f"{args.issue}.md"

    # **验收模式绝不消耗人类的真实批准**（2026-09-12 整理轮发现的问题）：
    # `state/approvals/` 里留着以前演练时人类写的「是」，于是每次验收都会拿着那份旧批准
    # 去真的推分支、真的建 PR —— 一条"只读的验收"变成了对外写，而且第二次跑就撞上
    # "PR 已存在"把 4.4 打成 BLOCKED。所以 `--check` 用一份**隔离的审批目录**：
    # 那里永远没有「是」，验收于是每次都走"没有点头就走不动"这条路 —— 那才是它的本意。
    approvals_dir = ROOT / "state" / ("approvals-check" if args.check else "approvals")
    approvals_dir.mkdir(parents=True, exist_ok=True)

    result = publish(
        repo=repo,
        issue_id=args.issue,
        report=report,
        changes=[change],
        conversation_reply=args.reply,
        reply_scope={"push": "push", "pr": "pull_request"}.get(args.approve or ""),
        report_path=report_path,
        approvals_dir=approvals_dir,
        outbox_dir=ROOT / "state" / "outbox",
        write_token=ROOT / "state" / ".write_token",
    )

    # 把人类口头的「是」**留在单子上**：批准必须留痕，否则下一次运行时闸门看不到它，
    # 同一个人得为同一件事再点一次头。只写这一次批的那个闸门。
    if args.reply and args.reply.strip() == "是" and args.approve:
        suffix = {"push": "-push-", "pr": "-create_pull_request-"}[args.approve]
        for name in result.tickets:
            if suffix in name:
                ticket_path = approvals_dir / name
                lines = ticket_path.read_text(encoding="utf-8").splitlines() or [""]
                lines[0] = "是"
                ticket_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
                print(f"已在审批单首行记下这次批准：{name}")
                break

    print()
    print(f"报告：{report_path.relative_to(ROOT).as_posix()}")
    print(f"分支：{result.branch}")
    print(f"阶段：{result.stage}　已发布：{result.published}　原因：{result.reason}")
    for name in result.tickets:
        print(f"审批单：{approvals_dir.relative_to(ROOT).as_posix()}/{name}")

    if result.published:
        print(f"PR：{result.pr_url}（#{result.pr_number}）")
        print("端到端演练完成：issue → 门禁 → 两次人类点头 → 分支 → PR。")
        return 0

    if result.stage == "push":
        print()
        print("等人类点头：把「是」写在上面那张审批单的**第一行**，然后重跑：")
        print(f"  python tools/e2e_publish.py --repo {repo} --reply 是")
        print("（批准后我只会推一个新分支，不碰默认分支；再点一次头才建 PR。）")
        return 0 if args.check else 2

    print()
    print("分支已推好，等第二次点头（建 PR）。重跑：")
    print(f"  python tools/e2e_publish.py --repo {repo} --reply 是")
    return 0 if args.check else 2


if __name__ == "__main__":
    raise SystemExit(main())
