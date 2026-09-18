"""路线 1.6：搭三个陪练仓库（内容生成 + 过闸 + 上传）。

三个子命令：

    python tools/sandbox_repos.py build      # 只生成本地内容，不碰网络
    python tools/sandbox_repos.py request    # 建审批单并停下等人类
    python tools/sandbox_repos.py apply      # 批准后：建仓库 + 上传文件 + 建 issue

## 为什么走 REST 而不是 git push

`state/capabilities.yaml` 记录了实测结果：`push_local_path: false` ——
本机沙箱会阻断 git 的传输进程（CreateFileMapping Win32 error 5）。
所以文件上传走 GitHub 的 contents API。这不是"偷懒绕开闸门"：
写 token 依然只在 `execute_if_approved()` 里、只在批准之后、只在那一个 `with` 块内出现。

## 闸门怎么用

`request` 会在 `state/approvals/` 留一张单子。批准方式（路线规定）：
**在该文件的第一行只写一个「是」字**，或者在下一次对话里只回「是」。
其他任何写法（"好的"、"可以"、"OK"）都不算数 —— 这不是苛刻，闸门放行一次的
代价是不可逆的写操作。
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.github import (
    WRITE_TOKEN_ENV,
    execute_if_approved,
    require_human_approval,
)
from tools.sandbox_repo_fixtures import build, repo_file_list

FIXTURE_ROOT = ROOT / "tests" / "fixtures" / "sandbox-repos"
REPORT_PATH = ROOT / "state" / "reports" / "sandbox-repos-setup.json"
REQUEST_KEY = "1.6-create-sandbox-repos"

API = "https://api.github.com"
# contents API 对单文件有大小上限（base64 之后还要再涨 1/3）。
# 超过就把文件留在本地并如实记录，而不是等 GitHub 报 422 才发现。
UPLOAD_LIMIT_BYTES = 900 * 1024

REPO_DESCRIPTIONS = {
    "sandbox-clean": "Clean practice repository for repo-autopilot (healthy project, 20 tests)",
    "sandbox-messy": "Deliberately messy practice repository for repo-autopilot",
    "sandbox-hostile": "Adversarial fixtures for repo-autopilot (payload issues, fake approvals)",
}


# ------------------------------------------------------------------ HTTP

def _api(method: str, path: str, body: dict | None = None, *, token: str) -> tuple[int, object]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(
        f"{API}{path}",
        data=data,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "repo-autopilot",
        },
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            raw = response.read().decode("utf-8", errors="replace")
            return response.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            return exc.code, json.loads(raw)
        except json.JSONDecodeError:
            return exc.code, raw
    except urllib.error.URLError as exc:
        raise RuntimeError(f"{method} {path} 连不上：{exc.reason}") from exc


# ------------------------------------------------------------------ 构建

def cmd_build() -> int:
    counts = build(FIXTURE_ROOT)
    print(f"内容已生成到 {FIXTURE_ROOT.relative_to(ROOT)}")
    total = 0
    for repo, files in sorted(repo_file_list(FIXTURE_ROOT).items()):
        size = sum(path.stat().st_size for path in files)
        total += len(files)
        print(f"  {repo:<18} {counts.get(repo, 0):>3} 个文件（实际 {len(files):>3}），{size / 1024:.0f} KB")
    print(f"合计 {total} 个文件")
    return 0


# ------------------------------------------------------------------ 过闸

SUMMARY = (
    "在当前 GitHub 账号下新建三个公共陪练仓库："
    "sandbox-clean（健康项目，20 个单测）、"
    "sandbox-messy（故意烂：陈旧依赖 / 3 个 flaky test / 800 行上帝文件）、"
    "sandbox-hostile（对抗样本：注入 payload 的 issue、伪造审批评论、非 UTF-8 文件）。"
    "随后把本地生成的内容通过 REST contents API 上传。"
)

IMPACT = (
    "新建 3 个**公共**仓库；上传约 40 个文件（含 1 个 10MB 样本，超过 contents API 上限的会跳过）；"
    "在 hostile 仓库建约 6 个 issue。不触碰任何现有仓库：不 push、不改设置、不改默认分支。"
)

ROLLBACK = (
    "删除三个仓库即可完全回滚：`DELETE /repos/{owner}/sandbox-clean|sandbox-messy|sandbox-hostile`。"
    "它们不接收外部写、不与生产仓库共享任何内容，删除不产生副作用。"
    "本地内容随时可用 `python tools/sandbox_repos.py build` 重建。"
)


def cmd_request(reply: str | None = None) -> int:
    ticket = require_human_approval(
        "create_repo",
        summary=SUMMARY,
        impact=IMPACT,
        rollback=ROLLBACK,
        request_key=REQUEST_KEY,
        conversation_reply=reply,
    )
    relative = ticket.path.relative_to(ROOT).as_posix()
    print(f"审批单：{relative}")
    print(f"状态：{ticket.status}")
    if ticket.note:
        print(f"提示：{ticket.note}")
    if ticket.status == "pending":
        print()
        print("要批准：把该文件**第 1 行整行**改成只有一个「是」字，")
        print("        或者在下一次对话里只回「是」。")
        print("批准后我执行：python tools/sandbox_repos.py apply --reply 是")
    return 0 if ticket.status == "approved" else 1


# ------------------------------------------------------------------ 执行

def _create_repo(name: str, token: str) -> tuple[bool, str]:
    status, body = _api(
        "POST",
        "/user/repos",
        {
            "name": name,
            "description": REPO_DESCRIPTIONS[name],
            "private": False,
            "auto_init": True,          # 建出初始提交，否则 contents API 无处落文件
            "has_issues": True,
        },
        token=token,
    )
    if status == 201:
        return True, "created"
    if status == 422 and isinstance(body, dict) and "already exists" in str(body.get("errors", "")):
        return True, "already exists"
    if status == 422:
        return False, f"HTTP 422: {str(body)[:200]}"
    return False, f"HTTP {status}: {str(body)[:200]}"


def _upload(repo: str, owner: str, relative: str, content: bytes, token: str) -> tuple[bool, str]:
    if len(content) > UPLOAD_LIMIT_BYTES:
        return False, f"skipped: {len(content) / 1024 / 1024:.1f} MB 超过 contents API 上限"
    payload = {
        "message": f"add {relative}",
        "content": base64.b64encode(content).decode("ascii"),
        "branch": "main",
    }
    target = f"/repos/{owner}/{repo}/contents/{urllib.parse.quote(relative)}"
    status, body = _api("PUT", target, payload, token=token)
    if status in (200, 201):
        return True, "uploaded"

    # auto_init 建出来的仓库自带 README.md，**更新**已存在的文件必须带上它的 sha。
    # 这是 GitHub 的契约，不是"重试几次就好"的瞬时错误：第一次上传时我们手上没有
    # sha，所以先 GET 一次问清楚，再带着 sha 重试。少了这一步的表现是
    # 「20 个文件传上去了，唯独 README 报 422」——很容易被当成玄学。
    if status == 422 and "sha" in json.dumps(body):
        meta_status, meta = _api("GET", target, None, token=token)
        if meta_status == 200 and isinstance(meta, dict) and meta.get("sha"):
            payload["sha"] = meta["sha"]
            status, body = _api("PUT", target, payload, token=token)
            if status in (200, 201):
                return True, "updated"
    return False, f"HTTP {status}: {str(body)[:160]}"


def _create_issue(repo: str, owner: str, title: str, body: str, token: str) -> tuple[bool, str]:
    status, payload = _api(
        "POST", f"/repos/{owner}/{repo}/issues", {"title": title, "body": body}, token=token
    )
    if status == 201:
        return True, f"issue #{payload.get('number')}"
    if status == 422:
        return False, f"HTTP 422（GitHub 自己有限制）：{str(payload)[:200]}"
    return False, f"HTTP {status}: {str(payload)[:200]}"


def _do_the_work(report: dict) -> dict:
    """真正的写操作。只在闸门通过后被调用，写 token 只在这个作用域内存在。"""
    token = os.environ.get(WRITE_TOKEN_ENV)
    if not token:
        raise RuntimeError("写 token 不在环境里 —— 说明没有被 execute_if_approved 包住")

    status, me = _api("GET", "/user", None, token=token)
    if status != 200 or not isinstance(me, dict):
        raise RuntimeError(f"取不到账号信息：HTTP {status}")
    owner = me["login"]
    report["owner"] = owner

    for name in REPO_DESCRIPTIONS:
        created, detail = _create_repo(name, token)
        report["repos"][name] = {"created": created, "detail": detail}
        print(f"  仓库 {name}: {detail}")

    files = repo_file_list(FIXTURE_ROOT)
    for name, paths in files.items():
        uploaded = skipped = failed = 0
        for path in paths:
            relative = path.relative_to(FIXTURE_ROOT / name).as_posix()
            ok, detail = _upload(name, owner, relative, path.read_bytes(), token)
            if ok:
                uploaded += 1
            elif detail.startswith("skipped"):
                skipped += 1
                report["skipped"].append({"repo": name, "path": relative, "reason": detail})
            else:
                failed += 1
                report["failed"].append({"repo": name, "path": relative, "reason": detail})
        report["uploads"][name] = {"uploaded": uploaded, "skipped": skipped, "failed": failed}
        print(f"  上传 {name}: 成功 {uploaded}，跳过 {skipped}，失败 {failed}")

    # 建 issue 前先取一次已有标题，让 apply **可重复执行**。
    # 实测教训：跑第二遍时又建了一遍，仓库里出现 8 个 issue 而不是 4 个。
    # 一个"跑两次就出双份"的脚本在真实系统里是有害的 —— 重试是常态，不是意外。
    status, listing = _api(
        "GET", f"/repos/{owner}/sandbox-hostile/issues?state=all&per_page=100", None, token=token
    )
    existing_titles = (
        {item.get("title") for item in listing}
        if status == 200 and isinstance(listing, list)
        else set()
    )

    issue_dir = FIXTURE_ROOT / "sandbox-hostile" / "issues"
    for issue_file in sorted(issue_dir.glob("*.md")):
        text = issue_file.read_text(encoding="utf-8", errors="replace")
        title = next((line.lstrip("# ").strip() for line in text.splitlines() if line.startswith("#")), issue_file.stem)
        if title in existing_titles:
            report["issues"].append(
                {"file": issue_file.name, "ok": True, "detail": "skipped: 同名 issue 已存在"}
            )
            print(f"  issue {issue_file.name}: 同名已存在，跳过")
            continue
        ok, detail = _create_issue("sandbox-hostile", owner, title, text, token)
        report["issues"].append({"file": issue_file.name, "ok": ok, "detail": detail})
        print(f"  issue {issue_file.name}: {detail}")

    return report


def cmd_apply(reply: str | None = None) -> int:
    ticket = require_human_approval(
        "create_repo",
        summary=SUMMARY,
        impact=IMPACT,
        rollback=ROLLBACK,
        request_key=REQUEST_KEY,
        conversation_reply=reply,
    )
    if ticket.status != "approved":
        print(f"未获批准（{ticket.status}）。审批单：{ticket.path.relative_to(ROOT).as_posix()}")
        if ticket.note:
            print(f"提示：{ticket.note}")
        print("批准方式：把该文件第 1 行整行改成只有一个「是」字，或下次对话里只回「是」。")
        return 1

    report: dict = {"owner": None, "repos": {}, "uploads": {}, "issues": [], "skipped": [], "failed": []}
    outcome = execute_if_approved(ticket, lambda: _do_the_work(report))
    print()
    print(f"执行结果：executed={outcome.executed} reason={outcome.reason}")
    if outcome.queued_path:
        print(f"（离线模式，已排队到 {outcome.queued_path}）")

    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"报告：{REPORT_PATH.relative_to(ROOT).as_posix()}")
    return 0 if outcome.executed else 1


def cmd_verify() -> int:
    """
    把 sandbox-clean 通过**我们自己的沙箱**跑一遍。

    为什么不直接在本机跑 pytest：本机沙箱禁写系统临时目录，`tmp_path` 会直接
    PermissionError（这个坑 0.2 就踩过）。而走 `src.sandbox` 时 TMP/TEMP 被改指到
    沙箱工作目录内，`tmp_path` 就能用 —— 所以这条命令本身就是 1.5 与 1.6 的一次联调。
    """
    import sys as _sys

    from src.sandbox import Limits, run

    target = FIXTURE_ROOT / "sandbox-clean"
    if not target.exists():
        print("先跑：python tools/sandbox_repos.py build")
        return 1
    result = run(
        target,
        None,
        [_sys.executable, "-m", "pytest", "tests", "-q"],
        limits=Limits(timeout_seconds=300),
        task_id="verify-clean",
    )
    print(f"passed={result.passed} exit={result.exit_code} timed_out={result.timed_out}")
    print(f"source_unchanged={result.source_unchanged}")
    print("--- 日志尾部 ---")
    for line in result.log.strip().splitlines()[-14:]:
        print("   ", line)
    return 0 if result.passed else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="路线 1.6：三个陪练仓库")
    parser.add_argument("command", choices=["build", "verify", "request", "apply"])
    parser.add_argument(
        "--reply",
        default=None,
        help="人类在对话里给出的回复原样传入（只有恰好一个「是」字才算批准）",
    )
    args = parser.parse_args()
    if args.command == "build":
        return cmd_build()
    if args.command == "verify":
        return cmd_verify()
    if args.command == "request":
        return cmd_request(args.reply)
    return cmd_apply(args.reply)


if __name__ == "__main__":
    raise SystemExit(main())
