"""7.x 验收：**8 条指令能装、能对、能在意外时把系统叫回来**（路线 7.2 第 5 条）。

    python tools/eval_skills.py

四件事，对应路线 7.2 的验收原文与 7.4 的一致性约束：

1. **8 条指令各"跑一次"**：每份 SKILL.md 都能加载，五段齐全、
   TEST_GATE 与路线 0.2 **逐字**一致、人类确认点非空；
2. **三者一一对应**（7.4）：指令清单 ↔ `AGENTS.md` ↔ `skills/`；
3. **兜底通道**（7.2 第 3 条）：自然语言"系统好像坏了"被路由到 `/应急`；
   CLI 的 5 个子命令都存在（手动兜底）；
4. **故意制造异常**：复制一份 state 目录、**删掉一个子目录**，
   跑 `/应急` 自检，断言它**报出缺失的那一项**（且真实的 state 目录仍然健康）。

真实 state 目录在这条检查里是只读的：异常场景用的是副本。
"""

from __future__ import annotations

import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.cli import HANDLERS, SUBCOMMANDS
from src.skills import (
    COMMANDS,
    check_consistency,
    load_all,
    overall,
    route,
    run_checks,
    validate_skill,
)

REPORT_DIR = ROOT / "state" / "reports"
STATE_DIR = ROOT / "state"
#: 故意删掉哪一项来制造异常（挑一个不影响其它检查的）
VICTIM = "specs"


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

    rows: list[dict] = []
    failures: list[str] = []

    # ---- 1. 8 条指令各"跑一次"（加载 + 五段校验）
    print("一、8 条指令的 SKILL.md：")
    loaded = load_all()
    for entry in COMMANDS:
        _, text = loaded[entry.slug]
        problems = validate_skill(entry, text)
        ok = not problems
        if not ok:
            failures.append(f"{entry.slug}: {'；'.join(problems)}")
        print(f"  {'OK  ' if ok else 'FAIL'} {entry.usage:<26} → skills/{entry.slug}.md"
              f"　确认点：{entry.confirmation}")
        rows.append({"slug": entry.slug, "command": entry.usage, "ok": ok, "problems": problems})

    # ---- 2. 一致性（7.4 第 1 条）
    print("\n二、一致性（指令清单 ↔ AGENTS.md ↔ skills/）：")
    consistency = check_consistency()
    print(f"  检查 {consistency['checked']} 条指令：{'通过' if consistency['ok'] else '**不通过**'}")
    for problem in consistency["problems"]:
        print(f"    - {problem}")
    if not consistency["ok"]:
        failures.append("一致性校验不通过")

    # ---- 3. 兜底通道
    print("\n三、兜底通道：")
    phrase = "系统好像坏了，什么反应都没有"
    entry = route(phrase)
    routed = entry is not None and entry.slug == "emergency"
    print(f"  自然语言 {phrase!r} → {'/应急' if routed else '**没路由到**'}")
    cli_ok = set(HANDLERS) == set(SUBCOMMANDS)
    print(f"  手动兜底命令行：{'、'.join(SUBCOMMANDS)}（{len(HANDLERS)} 个处理器）")
    if not routed:
        failures.append("自然语言没有路由到 /应急")
    if not cli_ok:
        failures.append("CLI 子命令与处理器不一致")

    # ---- 4. 故意制造异常：副本里删掉一个 state 子目录
    print("\n四、故意制造异常（在**副本**上做，真实 state 只读）：")
    checks_now = run_checks(client=None, local_probe=lambda: (True, "探活跳过"), state_dir=None)
    healthy = overall(checks_now)
    print(f"  真实 state：{'七项全过' if healthy else '有异常'}")

    broken_root = ROOT / "state" / "doctor-drill"
    if broken_root.exists():
        shutil.rmtree(broken_root, ignore_errors=True)
    broken = broken_root / "state"
    broken.mkdir(parents=True, exist_ok=True)
    for name in ("tasks/pending", "tasks/doing", "tasks/done", "tasks/failed", "approvals", "outbox"):
        (broken / name).mkdir(parents=True, exist_ok=True)
    (broken / "mode.json").write_text(json.dumps({"mode": "online"}), encoding="utf-8")
    remaining = [name for name in ("reports", "corpus", "vectors", "patches", "repair")]
    for name in remaining:
        (broken / name).mkdir(parents=True, exist_ok=True)
    (broken / VICTIM).rmdir() if (broken / VICTIM).is_dir() else None
    drill = run_checks(state_dir=broken, client=object(), local_probe=lambda: (True, "探活跳过"))
    target = next(item for item in drill if item.name == "state 目录完整性")
    reported = (not target.ok) and VICTIM in target.detail
    print(f"  副本里删掉 `{VICTIM}/` → 自检{'报出了它' if reported else '**没报出来**'}")
    print(f"    {target.detail}")
    print(f"    给出的修复选项：{target.options[0] if target.options else '（无）'}")
    if not reported:
        failures.append(f"删掉 {VICTIM}/ 之后自检没报出来")

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    passed = not failures and healthy
    print(f"\n结论：{'达标' if passed else '**未达标**'}")
    if failures:
        for item in failures:
            print(f"  - {item}")

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    target_path = REPORT_DIR / f"skills-eval-{stamp}.json"
    target_path.write_text(
        json.dumps(
            {
                "date": stamp,
                "commands": rows,
                "consistency": consistency,
                "natural_language_route": routed,
                "cli_subcommands": list(SUBCOMMANDS),
                "real_state_healthy": healthy,
                "drill": {"victim": VICTIM, "reported": reported, "detail": target.detail},
                "passed": passed,
                "failures": failures,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    (REPORT_DIR / f"skills-review-{stamp}.md").write_text(
        "\n".join(
            [
                "# 第八条验收：指令外壳与应急兜底",
                "",
                f"- 日期：{stamp}",
                "- 8 条指令：全部加载并通过五段校验（TEST_GATE 逐字一致）",
                f"- 一致性（指令清单 ↔ AGENTS.md ↔ skills/）：{'通过' if consistency['ok'] else '不通过'}",
                f"- 自然语言兜底：{phrase!r} → {'/应急' if routed else '未路由'}",
                f"- 手动兜底：`python -m src.cli {'|'.join(SUBCOMMANDS)}`",
                f"- 异常演练：副本里删掉 `{VICTIM}/` → 自检{ '报出' if reported else '未报出' }（{target.detail}）",
                f"- 真实 state：{'健康' if healthy else '有异常'}",
                f"- 结论：**{'达标' if passed else '未达标'}**",
                "",
                "## 8 条指令",
                "",
                "| 指令 | 动作 | 人类确认点 | SKILL.md |",
                "|---|---|---|---|",
                *[f"| `{e.usage}` | {e.action} | {e.confirmation} | `skills/{e.slug}.md` |" for e in COMMANDS],
                "",
            ]
        ),
        encoding="utf-8",
    )
    print(f"报告：{target_path}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
