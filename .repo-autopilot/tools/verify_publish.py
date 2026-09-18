"""确认发布**真的发生了**（不是我们自己报告说发生了）。

    python tools/verify_publish.py --repo AristotleAsborg/sandbox-clean --pr 1
    python tools/verify_publish.py --repo ... --branch auto/fix-e2e-publish

为什么单独做这个：4.3 的教训是"标志位是自证的"。发布也一样 ——
脚本返回 `published: True` 只证明它自己相信成功；**只有回查 GitHub 才算证据**。
所以这个工具读的是远端：PR 的标题/状态/head/base/改动文件，以及分支是否存在。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

    parser = argparse.ArgumentParser(description="回查发布结果")
    parser.add_argument("--repo", required=True, help="owner/name")
    parser.add_argument("--pr", type=int, default=0)
    parser.add_argument("--branch", default="")
    parser.add_argument("--expect-head", default="", help="期望的 head 分支（不匹配即失败）")
    args = parser.parse_args()

    from src.github import GitHubClient, read_token

    client = GitHubClient(read_token(), timeout=60)
    ok = True

    if args.pr:
        payload = client.get(f"/repos/{args.repo}/pulls/{args.pr}")
        head = (payload.get("head") or {}).get("ref")
        base = (payload.get("base") or {}).get("ref")
        files = client.get(f"/repos/{args.repo}/pulls/{args.pr}/files")
        print(f"PR #{args.pr}：{payload.get('title')}")
        print(f"  状态：{payload.get('state')}　html_url：{payload.get('html_url')}")
        print(f"  head → base：{head} → {base}")
        print(f"  改动文件：{[(item.get('filename'), item.get('additions'), item.get('deletions')) for item in (files or [])]}")
        if payload.get("state") != "open":
            print("  **PR 状态不是 open**")
            ok = False
        if args.expect_head and head != args.expect_head:
            print(f"  **head 不是期望的 {args.expect_head}**")
            ok = False

    if args.branch:
        try:
            ref = client.get(f"/repos/{args.repo}/git/ref/heads/{args.branch}")
            print(f"分支 {args.branch} 存在，sha={ref.get('object', {}).get('sha', '')[:12]}")
        except Exception as exc:  # noqa: BLE001
            print(f"**分支 {args.branch} 查不到：{type(exc).__name__} {str(exc)[:120]}**")
            ok = False

    print(f"结论：{'远端已确认' if ok else '**未确认**'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
