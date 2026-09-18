"""路线 1.6 子步骤 2：一条命令跑完「建 issue → 分类 → 修复 → 门禁 → PR」。

    python tests/e2e/e2e_full_run.py                 # 跑 1~4 步（离线，不碰网络）
    python tests/e2e/e2e_full_run.py --publish       # 额外建 PR 审批单，停下等人类
    python tests/e2e/e2e_full_run.py --no-negative-control

## 这一步验的是什么

各模块还没建（阶段二~四才建），所以分类和修复用的是**桩**。骨架要证明的不是
"分类准不准"，而是**链路通不通、门禁灵不灵**：

  * 建 issue → 分类 → 修复 → 打补丁 → 跑测试 → 出结论，这条链能一路走完；
  * 中间每一步都经**任务队列**（1.2）流转，而不是直接函数调用——
    这样 1.2 也被真实地走了一遍；
  * 补丁由 `src.sandbox`（1.5）打进**副本**再跑测试，源仓库零变化；
  * 发布动作由**人类闸门**（1.4）拦住。

## 为什么默认还跑一条"负面控制"

只有一次绿的门禁不算门禁 —— 它可能是永远绿的。所以第 4b 步故意打一个会挂的补丁，
要求门禁**必须变红**。如果它也绿了，这次 e2e 直接判定失败：
说明我们测的不是代码，而是自己的乐观。

## 退出码

  0  1~4 步全部符合预期（含负面控制变红）
  1  链路断了（某步结果不符合预期）
  2  `--publish` 下已建审批单，正在等人类批准（链路本身是通的）
"""

from __future__ import annotations

import argparse
import difflib
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.queue import Task, TaskQueue
from src.repair import propose_patch
from src.sandbox import Limits
from src.sandbox import run as sandbox_run
from src.triage import classify

FIXTURE_ROOT = ROOT / "tests" / "fixtures" / "sandbox-repos"
DEFAULT_REPO = FIXTURE_ROOT / "sandbox-clean"
E2E_ROOT = ROOT / "state" / "e2e"
PY = sys.executable

DEFAULT_ISSUE = {
    "title": "README 里没有安装步骤",
    "body": (
        "新同事 clone 下来之后不知道要装什么、用什么命令跑测试。"
        "README 只讲了 layout 和 usage，没有 Install 一节。建议补上。"
    ),
    "source": "e2e-fixture（离线；真实链路里这里会是从 GitHub 拉来的 issue）",
}


def log(step: str, message: str) -> None:
    print(f"[{step}] {message}", flush=True)


def through_queue(queue: TaskQueue, kind: str, payload: dict, work) -> tuple[object, dict]:
    """
    把一步工作放进任务队列走一遍，而不是直接调用。

    骨架阶段这么做是有意的：如果 e2e 直接调函数，1.2 的真实行为（认领、
    状态迁移、重复消费防护）在链路里就完全是空白，等到阶段二接上队列时才会炸。
    现在就走一遍，代价是六行代码。
    """
    task = queue.enqueue(Task(type=kind, payload=payload))
    claimed = queue.dequeue()
    if claimed is None or claimed.id != task.id:
        raise RuntimeError(f"队列没能取回刚入队的任务 {task.id}（拿到 {claimed}）")
    result = work(claimed.payload)
    queue.complete(claimed.id)
    return result, {"task_id": task.id, "state": "done"}


def broken_patch(repo: Path) -> tuple[str, str]:
    """故意造一个会让单测变红的补丁（把整数加法改成多加一分）。"""
    target = repo / "ledger" / "money.py"
    before = target.read_text(encoding="utf-8")
    needle = "return Money(self.cents + other.cents)"
    if needle not in before:
        raise RuntimeError("找不到要破坏的那一行，负面控制无法执行")
    after = before.replace(needle, "return Money(self.cents + other.cents + 1)", 1)
    diff = "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile="a/ledger/money.py",
            tofile="b/ledger/money.py",
        )
    )
    return diff, "把钱数加错一分，单测必须抓到"


def main() -> int:
    parser = argparse.ArgumentParser(description="1.6 全链路骨架")
    parser.add_argument("--repo", default=str(DEFAULT_REPO), help="要跑的目标仓库")
    parser.add_argument("--issue-file", default=None, help="从 JSON 文件读 issue")
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--publish", action="store_true", help="额外建 PR 审批单")
    parser.add_argument("--no-negative-control", action="store_true")
    args = parser.parse_args()

    repo = Path(args.repo).resolve()
    if not repo.is_dir():
        log("准备", f"目标仓库不存在：{repo}（先跑 python tools/sandbox_repos.py build）")
        return 1

    run_id = args.run_id or time.strftime("%Y%m%d-%H%M%S")
    run_dir = E2E_ROOT / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    queue = TaskQueue(run_dir / "queue")
    summary: dict = {"run_id": run_id, "repo": str(repo), "steps": [], "ok": False}
    started = time.perf_counter()

    def record(name: str, ok: bool, detail: str) -> None:
        summary["steps"].append({"step": name, "ok": ok, "detail": detail})
        log(name, ("OK  " if ok else "FAIL") + " " + detail)

    # ---------------------------------------------------------- 1. 建 issue
    if args.issue_file:
        issue = json.loads(Path(args.issue_file).read_text(encoding="utf-8"))
    else:
        issue = dict(DEFAULT_ISSUE)
    (run_dir / "issue.json").write_text(
        json.dumps(issue, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    record("1-issue", bool(issue.get("title")), f"{issue['title']!r}（{len(issue.get('body') or '')} 字）")

    # ---------------------------------------------------------- 2. 分类
    classification, queue_info = through_queue(
        queue,
        "triage",
        {"title": issue["title"], "body": issue.get("body") or ""},
        lambda payload: classify(payload["title"], payload["body"]),
    )
    (run_dir / "classification.json").write_text(
        json.dumps(
            {"label": classification.label, "why": classification.why, "stub": classification.stub},
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    record("2-triage", True, f"label={classification.label} why={classification.why} {queue_info}")

    # ---------------------------------------------------------- 3. 修复
    proposal, queue_info = through_queue(
        queue,
        "repair",
        {"title": issue["title"], "body": issue.get("body") or "", "label": classification.label},
        lambda payload: propose_patch(repo, payload["title"], payload["body"], payload["label"]),
    )
    patch_path = run_dir / "patch.diff"
    if proposal.produced_patch:
        patch_path.write_text(proposal.patch_text, encoding="utf-8")
    record(
        "3-repair",
        proposal.produced_patch,
        f"{proposal.description}；补丁 {len(proposal.patch_text)} 字节 -> {patch_path.name}",
    )
    if not proposal.produced_patch:
        summary["ok"] = False
        (run_dir / "summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        return 1

    # ---------------------------------------------------------- 4. 门禁
    limits = Limits(timeout_seconds=300)
    green = sandbox_run(
        repo,
        patch_path,
        [PY, "-m", "pytest", "tests", "-q"],
        limits=limits,
        task_id=f"e2e-{run_id}-green",
    )
    record(
        "4-gate(green)",
        green.passed,
        f"exit={green.exit_code} timed_out={green.timed_out} 源仓库未变={green.source_unchanged}",
    )
    if not green.passed or not green.source_unchanged:
        summary["ok"] = False
        (run_dir / "summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        return 1

    # ------------------------------------------------- 4b. 负面控制
    if not args.no_negative_control:
        diff, why = broken_patch(repo)
        bad_patch = run_dir / "patch-broken.diff"
        bad_patch.write_text(diff, encoding="utf-8")
        red = sandbox_run(
            repo,
            bad_patch,
            [PY, "-m", "pytest", "tests", "-q"],
            limits=limits,
            task_id=f"e2e-{run_id}-red",
        )
        # 这里要的正是"红"：门禁必须能分辨好坏补丁
        record(
            "4b-negative-control",
            not red.passed,
            f"{why}；门禁结果 exit={red.exit_code}（期望非 0）源仓库未变={red.source_unchanged}",
        )
        if red.passed:
            log("4b-negative-control", "门禁对坏补丁也放行 —— 它测的不是代码")
            summary["ok"] = False
            (run_dir / "summary.json").write_text(
                json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            return 1

    # ---------------------------------------------------------- 5. 发布
    publish_state = "skipped"
    if args.publish:
        from src.github import require_human_approval

        ticket = require_human_approval(
            "create_pull_request",
            summary=f"为 issue「{issue['title']}」创建一个 PR，包含 {patch_path.name} 的改动",
            impact="在目标仓库新建 1 个分支 + 1 个 PR；不合并、不改默认分支、不推送 main",
            rollback="关掉 PR、删掉分支即可；目标仓库的主分支不受影响",
            request_key=f"e2e-{run_id}-pr",
        )
        publish_state = ticket.status
        record("5-publish", ticket.status == "approved", f"闸门状态={ticket.status} 单子={ticket.path.name}")

    summary["ok"] = True
    summary["publish_state"] = publish_state
    summary["duration_s"] = round(time.perf_counter() - started, 2)
    (run_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    print()
    print(f"链路跑通（{summary['duration_s']}s）。产物：{run_dir.relative_to(ROOT).as_posix()}/")
    if publish_state == "pending":
        print("发布动作在等人类批准 —— 批准后这一步才会真的建 PR。")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
