"""查看读 token 能访问的仓库概览（只读）。

起因：0.3 验收时发现账号下有一个 `dsh-example-token-balance` 私有仓库，
而本地那份示例仓库从未推送过——需要确认它是什么、由谁创建。
同时确认另一个仓库的内容，以便规划「测试仓库新建」这一步。
"""

from __future__ import annotations

import os

import httpx

API = "https://api.github.com"


def main() -> None:
    token = os.environ.get("GH_READ_TOKEN", "").strip()
    if not token:
        print("未设置 GH_READ_TOKEN")
        return

    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "repo-autopilot-probe",
        "X-GitHub-Api-Version": "2022-11-28",
        "Authorization": f"Bearer {token}",
    }

    with httpx.Client(timeout=25) as client:
        me = client.get(f"{API}/user", headers=headers).json()
        print(f"账号: {me['login']}  ({me.get('name') or '无名'})")
        print(f"账号类型: {me['type']}   创建于 {me['created_at']}")
        print()

        repos = client.get(f"{API}/user/repos?per_page=100&sort=updated", headers=headers).json()
        print(f"可见仓库 {len(repos)} 个：")
        for r in repos:
            print()
            print(f"  {r['full_name']}")
            print(f"    私有      : {r['private']}")
            print(f"    fork      : {r['fork']}")
            print(f"    默认分支  : {r['default_branch']}")
            print(f"    大小      : {r['size']} KB")
            print(f"    创建于    : {r['created_at']}")
            print(f"    最后推送  : {r.get('pushed_at')}")
            print(f"    描述      : {r.get('description') or '(无)'}")
            print(f"    许可证    : {(r.get('license') or {}).get('spdx_id') or '(无)'}")

            # 看根目录有什么，判断是不是我方产物
            contents = client.get(f"{API}/repos/{r['full_name']}/contents", headers=headers)
            if contents.status_code == 200:
                names = [c["name"] for c in contents.json()]
                print(f"    根目录    : {', '.join(names[:12])}{' ...' if len(names) > 12 else ''}")
            elif contents.status_code == 409:
                print("    根目录    : (空仓库)")
            else:
                print(f"    根目录    : 取不到（HTTP {contents.status_code}）")

            # 提交数：用 commits 端点取一页看长度，判断是否为活跃仓库
            commits = client.get(
                f"{API}/repos/{r['full_name']}/commits?per_page=5", headers=headers
            )
            if commits.status_code == 200 and isinstance(commits.json(), list):
                entries = commits.json()
                print(f"    最近提交  : {len(entries)} 条（本页）")
                for c in entries[:3]:
                    msg = c["commit"]["message"].splitlines()[0][:60]
                    who = (c.get("author") or {}).get("login") or c["commit"]["author"]["name"]
                    print(f"      {c['sha'][:8]}  {who}  {msg}")


if __name__ == "__main__":
    main()
