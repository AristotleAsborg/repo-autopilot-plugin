"""把**本系统自己**建成一个 GitHub 仓库（路线 6.1 的"给自己建一次"）。

    python tools/self_repo.py --check              # 干跑：要建什么、要推多少文件（不写、不过闸门）
    python tools/self_repo.py --reply 是           # 走闸门：建私有库 + 首推（人类点头才动）
    python tools/self_repo.py --public --reply 是  # 同上，但建成公开库

## 为什么单独写一个工具，而不是手敲几条 API

1. **必须过闸门**：建库与首推都是对外写（`create_repo` / `push` 在 `WRITE_ACTIONS` 白名单里），
   所以走的是与其它写操作**同一条** `require_human_approval` → `execute_if_approved` 路径。
   手敲 curl/gh 会绕开这道闸门 —— 那正是路线 0.3 反复强调不许做的事。
2. **必须幂等**：仓库已存在不能报错；文件内容没变的不能重复提交（`push_changes` 已经做到了
   "同一内容跳过"，这里只是复用）。重跑一次的结果应该是"什么都没变"，而不是又一份提交。
3. **必须可审**：推了什么、跳过了什么、闸门单号，全部落盘到
   `state/reports/self-repo-push-<日期>.json`，并且与 `tools/package.py` 用**同一份文件清单**
   （"只打 git 跟踪的文件"），这样"装到别的机器的包"与"推到 GitHub 的仓库"内容一致，不会各说各话。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DEFAULT_NAME = "repo-autopilot"
DEFAULT_DESCRIPTION = (
    "半自动开源仓库维护系统：issue 分类/查重 → 定位 → 修复 → 四道门禁 → 报告 → 人类点头才推 PR；"
    "外加开源猎手与脚手架，全部写操作过人类闸门。"
)


def git_blob_sha(data: bytes) -> str:
    """
    算出 git 的 blob sha（`sha1(b"blob <len>\\0" + data)`）。

    为什么自己算而不是 `git hash-object`：GitHub 的 contents API 返回的 `sha` 就是 blob sha，
    拿它和本地算的比一次就知道"这个文件在远端是不是已经一模一样" —— 幂等判断因此不需要
    额外的 GET，也不需要起子进程。
    """
    import hashlib

    header = f"blob {len(data)}\0".encode()
    return hashlib.sha1(header + data).hexdigest()


def collect() -> tuple[list[tuple[str, str]], list[str]]:
    """
    收集要推的文件：**复用打包工具的清单**（"只打 git 跟踪的文件"）。

    返回（[(仓库内路径, 文本内容)], 跳过的二进制文件路径）。二进制文件**响亮地跳过**：
    contents API 要 base64 而且这类文件在本系统里本该走 tarball/附件通道，不该混进源码提交。
    """
    from tools.package import plan  # type: ignore[import-not-found]

    files, _notes = plan()
    text_files: list[tuple[str, str]] = []
    binary: list[str] = []
    for path in files:
        raw = path.read_bytes()
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            binary.append(path.relative_to(ROOT).as_posix())
            continue
        text_files.append((path.relative_to(ROOT).as_posix(), text))
    return text_files, binary


def repo_exists(client, full_name: str) -> bool:
    try:
        client.repo(full_name)
        return True
    except Exception:  # noqa: BLE001
        return False


def do_push(client, *, full_name: str, files: list[tuple[str, str]], branch: str) -> dict:
    from src.publish import FileChange, push_changes

    changes = [FileChange(path=path, text=text) for path, text in files]
    return push_changes(
        client,
        repo=full_name,
        branch=branch,
        base_branch=branch,
        changes=changes,
        message=f"chore: repo-autopilot 入库（{len(files)} 个文件）",
    )


def verify_remote(client, *, full_name: str, branch: str = "main") -> int:
    """
    **核对远端就是本地这一份**：逐个文件比 git blob sha。

    "我推上去了"不算证据，"远端每一个文件的内容哈希都等于本地"才是。之所以能这样比：
    GitHub 的 tree API 返回的就是 blob sha，而 `git_blob_sha()` 能在本地算出来 ——
    于是 160 个文件的**内容**（不是文件名、不是大小）可以在一次调用里逐条核对完。
    """
    tree = client.get(f"/repos/{full_name}/git/trees/{branch}", params={"recursive": "1"})
    remote = {
        item["path"]: item["sha"]
        for item in (tree or {}).get("tree") or []
        if item.get("type") == "blob"
    }
    local_files, binary = collect()
    local = {path: git_blob_sha(text.encode("utf-8")) for path, text in local_files}

    missing = sorted(set(local) - set(remote))
    extra = sorted(set(remote) - set(local))
    different = sorted(path for path in set(local) & set(remote) if local[path] != remote[path])

    print(f"核对 {full_name}@{branch}")
    print(f"  本地 {len(local)} 个文件，远端 {len(remote)} 个 blob")
    for label, items in (("本地有、远端没有", missing), ("远端多出来的", extra), ("内容不一致", different)):
        if items:
            print(f"  **{label}** {len(items)} 个：")
            for item in items[:10]:
                print(f"    - {item}")
    if missing or extra or different:
        print("结论：**不一致**（推的东西和本地不是一份）")
        return 1
    print(f"结论：{len(local)} 个文件的内容哈希逐一相同 —— 远端就是本地这一份")
    if binary:
        print(f"（另外 {len(binary)} 个二进制文件按设计没推：{'、'.join(binary[:5])}）")
    return 0


def verify_ci(client, *, full_name: str, branch: str = "main", limit: int = 10) -> int:
    """
    **读 CI 结论**（只读）：要证明的不是"有绿"，而是"**远端当前这一份**是绿的"。

    2026-09-14 之前这一格一直是空的：读 token 缺这个仓库的 Actions 权限，
    `GET /actions/runs` 返回 403，所以"CI 绿"只能靠人类截图（见
    `state/findings/ci-evidence-2026-09-14.md`）。人类把 **Actions: Read** 补上之后，
    这件事变成一条命令 —— 报告里那句"CI 无法自证"可以删掉了。

    两件事必须分开做，缺一不可：

    1. 拿远端 `branch` 当前 head 的 sha；
    2. 在运行列表里找 **`head_sha` == 那个 sha** 的运行（可能不止一个工作流），逐个看状态。

    只报"最近一次运行是绿的"**不作数** —— 那可能是**上一个提交**的运行，
    而当前这一份从没被 CI 跑过。整仓同步每次新建一个提交，运行列表在它跑完之前
    最新一条仍指向上一个 sha，差一步就会被读成"绿"。

    返回码：`0` 当前 head 的运行全部成功；`1` 有跑完但不成功的；`2` 没有针对当前 head
    的运行（刚推上去还在排队）、还有没跑完的、或 Actions 读不到。
    **"没跑"绝不当成"通过"** —— 那正是这一格曾经空着的原因。
    """
    print(f"核对 {full_name}@{branch} 的 CI")
    try:
        head = (client.get(f"/repos/{full_name}/commits/{branch}") or {}).get("sha", "")
        payload = client.get(f"/repos/{full_name}/actions/runs", params={"per_page": limit}) or {}
    except Exception as exc:  # noqa: BLE001
        print(f"  读不到：{exc}")
        print("  （多半是读 token 缺这个仓库的 **Actions: Read** —— 那只是只读权限，补上即可）")
        return 2
    if not head:
        print(f"  **读不到 {branch} 的 head**")
        return 2
    print(f"  远端 head：{head[:8]}")

    runs = payload.get("workflow_runs") or []
    if not runs:
        print("  **这个仓库还没有任何 CI 运行**")
        return 2
    for run in runs[:5]:
        mark = "→" if run.get("head_sha") == head else " "
        print(
            f"  {mark} #{run.get('run_number')} {str(run.get('head_sha'))[:8]} "
            f"{run.get('status')}/{run.get('conclusion')} {run.get('name')}"
        )
    current = [run for run in runs if run.get("head_sha") == head]
    if not current:
        print(f"结论：**当前 head {head[:8]} 还没有 CI 运行**（可能刚推上去还在排队）—— 不等，返回 2")
        return 2
    pending = [run for run in current if run.get("status") != "completed"]
    if pending:
        states = "、".join(sorted({str(run.get("status")) for run in pending}))
        print(f"结论：**当前 head 还有 {len(pending)} 个运行没跑完**（{states}）—— 返回 2，不当作通过")
        return 2
    failed = [run for run in current if run.get("conclusion") != "success"]
    if failed:
        detail = "；".join(f"#{run.get('run_number')} {run.get('conclusion')}" for run in failed)
        print(f"结论：**CI 不绿**：{detail}")
        return 1
    print(f"结论：当前 head 的 {len(current)} 个运行**全部成功** —— 远端这一份的 CI 是绿的")
    return 0


def sync_single_commit(
    client: Any, *, full_name: str, files: list[tuple[str, str]], branch: str, message: str
) -> dict[str, Any]:
    """
    用 Git Data API 把本地这份**当成一个提交**同步上去。

    为什么不复用 `push_changes`（它按文件逐个 PUT，等于一个文件一个提交）：
    那套逻辑是为**增量补丁**写的（改 3 个文件就是 3 个提交，正好），
    但整仓同步动辄几十个文件 —— 几十个提交就是几十次 CI 运行，
    而且中间那些提交**必然是红的**（测试先到、被测的模块后到，反之亦然）。
    人看到的就是"这个仓库一直是 failed"。所以整仓同步走"一次提交"：
      blob（只建变化的）→ tree（`base_tree` 挂在新树上）→ commit → 更新 ref。
    内容没变的文件**连 blob 都不建**（先比 sha），所以反复同步是零成本的。
    """
    import base64

    ref = client.get(f"/repos/{full_name}/git/ref/heads/{branch}")
    parent = ref["object"]["sha"]
    commit = client.get(f"/repos/{full_name}/git/commits/{parent}")
    base_tree = commit["tree"]["sha"]
    tree_payload = client.get(f"/repos/{full_name}/git/trees/{base_tree}", params={"recursive": "1"})
    remote = {
        entry["path"]: entry["sha"]
        for entry in (tree_payload or {}).get("tree") or []
        if entry.get("type") == "blob"
    }

    entries: list[dict[str, Any]] = []
    updated: list[str] = []
    for path, text in files:
        sha = git_blob_sha(text.encode("utf-8"))
        if remote.get(path) == sha:
            continue
        response = client.request(
            "POST",
            f"/repos/{full_name}/git/blobs",
            body={"content": base64.b64encode(text.encode("utf-8")).decode("ascii"), "encoding": "base64"},
        )
        if response.status >= 400:
            raise RuntimeError(f"建 blob 失败 {path}：HTTP {response.status} {str(response.body)[:200]}")
        entries.append({"path": path, "mode": "100644", "type": "blob", "sha": response.body["sha"]})
        updated.append(path)

    if not entries:
        return {"updated": 0, "skipped": len(files), "commit": parent, "paths": []}

    tree = client.request(
        "POST", f"/repos/{full_name}/git/trees", body={"base_tree": base_tree, "tree": entries}
    )
    if tree.status >= 400:
        raise RuntimeError(f"建 tree 失败：HTTP {tree.status} {str(tree.body)[:200]}")
    new_commit = client.request(
        "POST",
        f"/repos/{full_name}/git/commits",
        body={"message": message, "tree": tree.body["sha"], "parents": [parent]},
    )
    if new_commit.status >= 400:
        raise RuntimeError(f"建 commit 失败：HTTP {new_commit.status} {str(new_commit.body)[:200]}")
    moved = client.request(
        "PATCH", f"/repos/{full_name}/git/refs/heads/{branch}", body={"sha": new_commit.body["sha"]}
    )
    if moved.status >= 400:
        raise RuntimeError(f"更新 ref 失败：HTTP {moved.status} {str(moved.body)[:200]}")
    return {
        "updated": len(entries),
        "skipped": len(files) - len(entries),
        "commit": new_commit.body["sha"],
        "paths": updated,
    }


def run_sync(
    reader,
    *,
    full_name: str,
    files: list[tuple[str, str]],
    binary: list[str],
    branch: str,
    message: str,
    reply: str | None,
    check: bool,
) -> int:
    """整仓同步走闸门（动作名 `push`）：先算出**变了哪些文件**，再请人类点头。"""
    from src.github import execute_if_approved, require_human_approval

    if not repo_exists(reader, full_name):
        print(f"仓库不存在：{full_name} —— 先不带 --sync 跑一次（建库 + 首推）")
        return 1

    ref = reader.get(f"/repos/{full_name}/git/ref/heads/{branch}")
    parent = ref["object"]["sha"]
    tree_payload = reader.get(f"/repos/{full_name}/git/trees/{parent}", params={"recursive": "1"})
    remote = {
        entry["path"]: entry["sha"]
        for entry in (tree_payload or {}).get("tree") or []
        if entry.get("type") == "blob"
    }
    changed = [
        path for path, text in files if remote.get(path) != git_blob_sha(text.encode("utf-8"))
    ]
    missing = sorted(path for path, _ in files if path not in remote)

    print(f"整仓同步 → {full_name}@{branch}（**一个提交**）")
    print(f"  本地 {len(files)} 个文件；远端已有 {len(remote)} 个 blob")
    print(f"  需要更新 {len(changed)} 个（其中远端没有的 {len(missing)} 个）")
    for path in changed[:12]:
        print(f"    · {'新增' if path in missing else '修改'} {path}")
    if len(changed) > 12:
        print(f"    · …（其余 {len(changed) - 12} 个）")
    if binary:
        print(f"  **跳过 {len(binary)} 个二进制文件**：{'、'.join(binary[:5])}")
    if check:
        print("\n干跑结束：没有建单、没有写。真正执行要 `--reply 是`（人类点头）。")
        return 0
    if not changed:
        print("远端已经和本地一致 —— 什么都不用做（同步是幂等的）")
        return 0

    ticket = require_human_approval(
        "push",
        summary=f"把本地整仓同步到 {full_name}@{branch}（一个提交，{len(changed)} 个文件变化）",
        impact=(
            f"会向 {full_name} 的 **{branch}** 分支追加一个提交（{len(changed)} 个文件："
            f"{'、'.join(changed[:3])}{'…' if len(changed) > 3 else ''}）。"
            "不改分支保护、不改仓库设置、不碰其它分支。"
        ),
        rollback=f"`git revert` 那个提交，或把 {branch} 指回上一个提交（{parent[:8]}）即可回滚。",
        conversation_reply=reply,
        request_key=f"self-repo-sync:{full_name}:{branch}",
        approvals_dir=ROOT / "state" / "approvals",
    )
    print(f"\n审批单：state/approvals/{ticket.path.name}（状态 {ticket.status}）")

    def perform() -> dict:
        from src.publish.core import _write_client

        writer = _write_client(ROOT / "state" / ".write_token")
        return sync_single_commit(
            writer,
            full_name=full_name,
            files=files,
            branch=branch,
            message=message or f"sync: 本地整仓同步（{len(changed)} 个文件变化）",
        )

    outcome = execute_if_approved(
        ticket,
        perform,
        payload={"repo": full_name, "branch": branch, "files": len(changed)},
        write_token=ROOT / "state" / ".write_token",
        outbox_dir=ROOT / "state" / "outbox",
    )
    if not outcome.executed:
        print(f"没有执行：{outcome.reason}")
        return 2

    result = outcome.result if isinstance(outcome.result, dict) else {}
    ledger = {
        "at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "repo": full_name,
        "branch": branch,
        "mode": "single-commit-sync",
        "updated": result.get("updated"),
        "skipped": result.get("skipped"),
        "commit": result.get("commit"),
        "paths": result.get("paths"),
        "ticket": ticket.path.name,
    }
    reports = ROOT / "state" / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    target = reports / f"self-repo-sync-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}.json"
    target.write_text(json.dumps(ledger, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"\n完成：提交 {str(result.get('commit'))[:8]}（更新 {result.get('updated')} 个文件，"
          f"跳过 {result.get('skipped')} 个未变的）")
    print(f"  账簿：{target.relative_to(ROOT).as_posix()}")
    print("  核对：python tools/self_repo.py --verify-remote")
    return 0


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

    parser = argparse.ArgumentParser(description="给本系统自己建 GitHub 仓库")
    parser.add_argument("--name", default=DEFAULT_NAME)
    parser.add_argument("--owner", default="", help="默认取读 token 对应的账号")
    parser.add_argument("--public", action="store_true", help="建成公开库（默认私有）")
    parser.add_argument("--reply", default=None, help="人类回复：是 / 否（给了才会真的写）")
    parser.add_argument("--check", action="store_true", help="只干跑，不建单不写")
    parser.add_argument("--verify-remote", action="store_true", help="只读：核对远端内容与本地一致")
    parser.add_argument(
        "--verify-ci",
        action="store_true",
        help="只读：核对**远端当前 head** 的 CI 结论（0 全绿 / 1 不绿 / 2 没跑或读不到）",
    )
    parser.add_argument(
        "--sync",
        action="store_true",
        help="整仓同步成**一个提交**（Git Data API）。默认是建库+首推路径，按文件逐个提交",
    )
    parser.add_argument("--branch", default="main")
    parser.add_argument("--message", default="", help="整仓同步时的提交信息")
    args = parser.parse_args()

    from src.github import (
        GitHubClient,
        execute_if_approved,
        read_token,
        require_human_approval,
    )

    reader = GitHubClient(read_token(), timeout=60)
    owner = args.owner or reader.viewer()["login"]
    full_name = f"{owner}/{args.name}"
    private = not args.public

    if args.verify_remote:
        return verify_remote(reader, full_name=full_name, branch=args.branch)

    if args.verify_ci:
        return verify_ci(reader, full_name=full_name, branch=args.branch)

    files, binary = collect()

    if args.sync:
        return run_sync(
            reader,
            full_name=full_name,
            files=files,
            binary=binary,
            branch=args.branch,
            message=args.message,
            reply=args.reply,
            check=args.check,
        )

    total_bytes = sum(len(text.encode("utf-8")) for _path, text in files)
    # 全新仓库 `auto_init` 出来的默认分支就是 main；之后所有改动只走 auto/* + PR，
    # 所以这里只会有这一次"往默认分支写"的机会。
    branch = "main"
    exists = repo_exists(reader, full_name)

    print(f"目标仓库：{full_name}（{'私有' if private else '公开'}）")
    print(f"现状：{'已存在' if exists else '不存在（将创建）'}")
    print(f"要推：{len(files)} 个文本文件，合计 {total_bytes / 1024 / 1024:.2f} MB → 分支 {branch}")
    for path, _text in files[:5]:
        print(f"  · {path}")
    if len(files) > 5:
        print(f"  · …（其余 {len(files) - 5} 个）")
    if binary:
        print(f"**跳过 {len(binary)} 个二进制文件**（contents API 只收文本）：{'、'.join(binary[:5])}")

    if args.check:
        print("\n干跑结束：没有建单、没有写。真正执行要 `--reply 是`（人类点头）。")
        return 0

    ticket = require_human_approval(
        "create_repo",
        summary=f"在 {owner} 下建{'私有' if private else '公开'}仓库 {args.name}，并把本系统 {len(files)} 个文件首推到 {branch}",
        impact=(
            "会在你的 GitHub 账号下新增一个仓库；**会向默认分支写第一次提交**"
            "（全新仓库没有别的分支可用，这是唯一的例外；之后的改动一律只走 auto/* + PR）。"
            "写操作用的是写 token，读操作用读 token，两者物理分离。"
        ),
        rollback="删除该仓库即可完全回滚（DELETE /repos/{owner}/{name}），本地代码与 state 都不受影响。",
        conversation_reply=args.reply,
        request_key=f"self-repo:{full_name}",
        approvals_dir=ROOT / "state" / "approvals",
    )
    print(f"\n审批单：state/approvals/{ticket.path.name}（状态 {ticket.status}）")

    def perform() -> dict:
        from src.publish.core import _write_client
        from src.scaffold.core import create_repository

        # **写操作必须用写 token 的客户端**：外面那个 `reader` 是用读 token 建的，
        # 拿它去 `POST /user/repos` 会得到 `403 Resource not accessible by personal access token`
        # —— 这个坑 6.1 已经踩过一次（记在 state/findings/write-client-wiring.md），
        # 症状看起来像"token 权限不够"，其实是**把读客户端传进了写路径**。
        writer = _write_client(ROOT / "state" / ".write_token")
        created = False
        default_branch = branch
        if not repo_exists(reader, full_name):
            info = create_repository(
                writer,
                args.name,
                description=DEFAULT_DESCRIPTION,
                private=private,
                auto_init=True,
            )
            created = True
            default_branch = info.get("default_branch") or branch
        result = do_push(writer, full_name=full_name, files=files, branch=default_branch)
        return {"created": created, "default_branch": default_branch, "push": result}

    outcome = execute_if_approved(
        ticket,
        perform,
        payload={"repo": full_name, "files": len(files), "private": private},
        write_token=ROOT / "state" / ".write_token",
        outbox_dir=ROOT / "state" / "outbox",
    )
    if not outcome.executed:
        print(f"没有执行：{outcome.reason}")
        if outcome.queued_path is not None:
            print(f"（已进 outbox 排队：{outcome.queued_path}）")
        return 2

    payload = outcome.result if isinstance(outcome.result, dict) else {}
    push = payload.get("push") or {}
    committed = push.get("commits") or []
    ledger = {
        "at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "repo": full_name,
        "visibility": "private" if private else "public",
        "created": payload.get("created"),
        "default_branch": payload.get("default_branch"),
        "files_planned": len(files),
        "files_committed": len(committed),
        "skipped_binary": binary,
        "ticket": ticket.path.name,
        "commits": committed,
    }
    reports = ROOT / "state" / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    target = reports / f"self-repo-push-{datetime.now(timezone.utc).strftime('%Y%m%d')}.json"
    target.write_text(json.dumps(ledger, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"\n完成：{'新建' if payload.get('created') else '已存在（复用）'} {full_name}")
    print(f"  提交文件：{len(committed)} 个（内容没变的会被跳过 —— 幂等）")
    print(f"  账簿：{target.relative_to(ROOT).as_posix()}")
    print(f"  仓库地址：https://github.com/{full_name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

