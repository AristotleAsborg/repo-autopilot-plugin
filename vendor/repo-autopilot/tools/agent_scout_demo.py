"""概念验证：**模型自主调用工具**（这里用的例子是开源猎手）。

    python tools/agent_scout_demo.py
    python tools/agent_scout_demo.py --task "我需要一个能把 Markdown 表格转成 CSV 的库"

## 它证明什么

**工具调用不是我们写死的流程，而是模型自己决定"该调哪个、用什么参数"。**

跑法是一个最小的 agent 循环：

1. 把"任务 + 可用工具清单（JSON schema）"给 flash；
2. flash 返回一个工具调用（`{"tool": "scout", "args": {"need": "...", "query": "..."}}`）；
3. 我们执行那个工具（这里就是 5.1 的猎手），把结果回灌；
4. flash 给出最终决定（选哪个候选、为什么）。

## 边界（重要）

- **只有白名单里的工具**能被调用：名字不在名单里的一律拒绝（`run_tool` 里 fail-closed）。
- **每个工具自带预算与副作用等级**：猎手是只读的；将来接入会写 GitHub 的工具时，
  它们必须走 `require_human_approval` —— **"模型自主调用"不等于"模型自主放行"**。
- 每次调用与结果都落盘 `state/agent/<时间戳>.jsonl`，**可审计**：
  谁（哪个模型）、什么时候、拿什么参数、调了什么工具、返回了什么。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

AUDIT_DIR = ROOT / "state" / "agent"

#: 工具白名单：名字 → (用途, 副作用等级)。**不在名单里的调用一律拒绝。**
TOOLS: dict[str, dict] = {
    "scout": {
        "purpose": "搜索开源项目（只读）：多轮搜索 → 硬过滤 → 看代码打分",
        "side_effects": "read_only",
        "args": {"need": "要解决的需求（中文一句话）", "query": "第一轮英文关键词（短）"},
    },
    "list_files": {
        "purpose": "列出本地仓库的文件树（只读）",
        "side_effects": "read_only",
        "args": {"path": "仓库根目录"},
    },
    "propose_patch": {
        "purpose": "生成补丁提议（只生成，不推送）",
        "side_effects": "read_only",
        "args": {"repo_dir": "本地仓库", "issue_text": "issue 原文"},
    },
}

TOOL_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "tool": {"type": "string"},
        "args": {"type": "object"},
        "why": {"type": "string"},
    },
    "required": ["tool", "args", "why"],
}

PLAN_PROMPT = """你在完成一个开发任务。可用工具（**只能从这些里选**）：

{tools}

任务：{task}

请决定**下一步调哪个工具、用什么参数**。只输出 JSON：
{{"tool": "工具名", "args": {{...}}, "why": "一句话说明为什么现在要调它"}}
"""

FINAL_PROMPT = """任务：{task}

你调用了 `{tool}`（参数 {args}），它返回：

{result}

请给出最终结论：**你会采用哪个候选/下一步做什么**，一句话说明理由。只输出 JSON：
{{"decision": "...", "reason": "...", "confidence": 0.0-1.0}}
"""


def flash(messages: list[dict[str, str]], schema: dict) -> dict:
    from src.gateway import chat

    return chat(messages, schema, "flash_api", temperature=0.0)


def run_tool(name: str, args: dict, *, client) -> dict:
    """
    执行一次工具调用。**白名单 fail-closed**：名字不在表里直接拒绝，不猜、不兜底。
    """
    if name not in TOOLS:
        return {"error": f"工具 {name!r} 不在白名单里；可用：{sorted(TOOLS)}"}
    if name == "scout":
        from src.scout import good_candidates, multi_round_scout

        result, rounds = multi_round_scout(
            str(args.get("need") or ""),
            client=client,
            queries=[str(args.get("query") or args.get("need") or "")],
            max_rounds=2,
            queries_per_round=2,
            per_page=20,
            score_per_round=5,
            min_valid=3,
        )
        good = good_candidates(result)
        return {
            "searched": result.searched,
            "rounds": [entry.as_dict() for entry in rounds],
            "candidates": [
                {
                    "repo": item.facts.full_name,
                    "stars": item.facts.stars,
                    "license": item.facts.license_spdx,
                    "relevance": (item.score or {}).get("relevance"),
                    "usage": (item.score or {}).get("usage"),
                    "reason": (item.score or {}).get("reason"),
                }
                for item in (good or result.candidates[:5])
            ],
        }
    if name == "list_files":
        from src.localize import walk_repo

        entries = walk_repo(str(args.get("path") or "."))
        return {"count": len(entries), "files": [entry.path for entry in entries[:40]]}
    if name == "propose_patch":
        from src.repair import propose_patch

        proposal = propose_patch(str(args.get("repo_dir") or "."), str(args.get("issue_text") or ""))
        return {"produced": proposal.produced_patch, "description": proposal.description, "bytes": len(proposal.patch_text)}
    return {"error": "未实现的工具"}


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

    parser = argparse.ArgumentParser(description="模型自主调用工具（概念验证）")
    parser.add_argument("--task", default="我在做一个把 Markdown 表格转成 CSV 的小工具，需要找现成的库参考")
    args = parser.parse_args()

    from src.github import GitHubClient, read_token

    client = GitHubClient(read_token(), timeout=60)
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    audit = AUDIT_DIR / f"scout-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}.jsonl"

    def record(payload: dict) -> None:
        with open(audit, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")

    tools_desc = "\n".join(
        f"- {name}（{spec['side_effects']}）：{spec['purpose']}；参数 {spec['args']}"
        for name, spec in TOOLS.items()
    )
    print(f"任务：{args.task}")
    print(f"可用工具：{'、'.join(TOOLS)}（模型自己挑）\n")

    plan = flash(
        [{"role": "user", "content": PLAN_PROMPT.format(tools=tools_desc, task=args.task)}], TOOL_SCHEMA
    )
    tool = str(plan.get("tool") or "")
    tool_args = plan.get("args") or {}
    print(f"模型决定调用：`{tool}`　参数 {json.dumps(tool_args, ensure_ascii=False)}")
    print(f"  理由：{plan.get('why')}")
    record({"stage": "plan", "plan": plan})

    output = run_tool(tool, tool_args, client=client)
    record({"stage": "tool_result", "tool": tool, "args": tool_args, "output": output})
    if "error" in output:
        print(f"工具拒绝执行：{output['error']}")
        print(f"审计：{audit.relative_to(ROOT).as_posix()}")
        return 1
    print(f"工具返回：搜索 {output.get('searched')} 条，候选 {len(output.get('candidates') or [])} 个")
    for item in (output.get("candidates") or [])[:4]:
        print(f"  - {item['repo']}（{item['stars']}★　{item['license']}）相关度 {item['relevance']} {item['usage']}")

    final = flash(
        [
            {
                "role": "user",
                "content": FINAL_PROMPT.format(
                    task=args.task,
                    tool=tool,
                    args=json.dumps(tool_args, ensure_ascii=False),
                    result=json.dumps(output, ensure_ascii=False)[:3000],
                ),
            }
        ],
        {
            "type": "object",
            "properties": {
                "decision": {"type": "string"},
                "reason": {"type": "string"},
                "confidence": {"type": "number"},
            },
            "required": ["decision", "reason", "confidence"],
        },
    )
    record({"stage": "final", "final": final})
    print(f"\n模型的决定：{final.get('decision')}")
    print(f"  理由：{final.get('reason')}（置信度 {final.get('confidence')}）")
    print(f"\n审计日志：{audit.relative_to(ROOT).as_posix()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
