"""路线 8.3「测试金字塔与频率表」的**到期审计** —— 把那张表从"愿望"变成"每天能问一句"。

    python tools/cadence.py            # 逐条列出：上次什么时候跑的、距今几天、到没到期
    python tools/cadence.py --due      # 只列"该跑了 / 从来没跑过"的
    python tools/cadence.py --json     # 给别的脚本读

## 为什么需要它

路线第十一部分写明本方案最大的现实修正：**"事件驱动"实际形态是"cron 轮询 + 对话唤起"**。
也就是说"每周跑一次混沌演练""每月复测模型基线"这类事，本来指望 cron 提醒 ——
但本系统没有常驻调度，于是它们**只能靠人记得**。人记不住的东西等于不存在：
8.3 那张表如果没人对着它问"今天该跑什么"，它就只是一张好看的表格。

这个工具就是那个"cron"的人工版本：它读**已经落盘的产物**（验收日志、演练台账、
评测报告、基线文件）反推每一条节奏上次是什么时候跑的，然后如实报三种状态：

- `未到期`：产物在周期内；
- `已到期`：产物太旧，**该跑了**（附上怎么跑）；
- `无记录`：**从来没跑过，或者根本没有产物** —— 这一条绝不能显示成"没问题"。
  它和"未到期"是两回事：一个是"跑过且新鲜"，一个是"根本没有这回事"。

`--due` 的退出码：0 = 没有到期的；1 = 有到期的；2 = 有"无记录"的（比到期更严重）。
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

STATE = ROOT / "state"


@dataclasses.dataclass(frozen=True)
class Cadence:
    """一条节奏：路线 8.3 表里的一行。"""

    name: str
    interval_days: int
    #: 产物 glob（相对仓库根）。**空元组 = 这条节奏目前没有产物**，会如实报"无记录"。
    evidence: tuple[str, ...]
    how: str
    note: str = ""
    #: **建议什么时候跑**。与 `note` 分开是因为它们是两件事：
    #: `note` 说"这条线的判据是什么"，`suggested` 说"人类该在哪天想起它"。
    #: 没有常驻调度器（路线第十一部分：本方案没有 cron），所以"哪天跑"只能靠人记得 ——
    #: 那就必须写在一个每天都会被人看到的地方，而不是留在某次对话里。
    suggested: str = ""


#: 路线 8.3 的频率表，逐行搬过来。**"证据从哪来"必须写清楚** ——
#: 写不出证据来源的那一行，就是"其实没人在跑"的那一行（红队/混沌/脏测试正是如此）。
CADENCES: tuple[Cadence, ...] = (
    Cadence(
        name="单元测试 + 集成测试（每补丁 / 每步骤验收）",
        interval_days=1,
        evidence=("state/reports/acceptance/*/",),
        how="python tools/acceptance.py --all",
        suggested="每个补丁落地时顺手跑；作为每日基线则由 `python tools/cadence.py --due` 提醒",
    ),
    Cadence(
        name="七天实战演练当日（每日，演练期内）",
        interval_days=1,
        evidence=("state/drill/day-*.json",),
        how="python tools/daily_drill.py --day N",
        note="只有 full 模式的天数算数，见 tools/daily_drill.py 的台账审计",
        suggested="演练期内每天一次，**固定的钟点**（例如每天开工第一件事）；同一天只跑一次，"
        "一天跑七次不算连续七天（台账审计会判不合格）",
    ),
    Cadence(
        name="离线评测：固定样本集（每周）",
        interval_days=7,
        evidence=(
            "state/checkup-history.jsonl",
            "state/reports/triage-eval-*.json",
            "state/reports/dedupe-eval-*.json",
            "state/reports/bounce-eval-*.json",
            "state/reports/localize-eval-*.json",
            "state/reports/repair-eval-*.json",
            "state/reports/gate-eval-*.json",
            "state/reports/skills-eval-*.json",
            "state/reports/scout-eval-*.json",
        ),
        how="python tools/checkup.py",
        note="连续两周下降才算退化（3.3 的判据）",
        suggested="每周一跑（固定样本集是冻结的，哪天跑都可比；周一跑能让整周都用新指标）",
    ),
    Cadence(
        name="端到端回归 e2e_full_run（每日）",
        interval_days=1,
        # 真实产物是**每次运行一个目录**（`state/e2e/<时间戳>/…/summary.json`）加上运行日志。
        # 第一版这里写成了 `state/reports/e2e-*.json`，于是明明跑过却报"无记录" ——
        # **假警报和漏报一样坏**（会让人以为"从来没跑"，从而重复劳动）。
        evidence=("state/e2e/*/", "state/reports/e2e-run.log", "state/reports/e2e-publish.md"),
        how="python tests/e2e/e2e_full_run.py",
        suggested="每日一次；**本系统自己有改动时先跑它**（8.3 的红线：e2e 失败即冻结自动化，只修不开发）",
    ),
    Cadence(
        name="脏测试：真实语料回放 / 并发压测 / 中断恢复（每周）",
        interval_days=7,
        # 产物由 tools/dirty_weekly.py 写出（8.2 的 ①④⑤ 三项一次跑完）。
        evidence=("state/reports/weekly/dirty-*.json",),
        how="python tools/dirty_weekly.py",
        note="回放用的是冻结回放集的 holdout 一半，与基线（triage-eval-*.json）比，**只允许不下降**；"
        "退出码 2 = 缺件（语料/基线不在），不是「指标不达标」",
        suggested="每周六跑 —— **刻意避开 7 天演练的每日动作**："
        "演练期每天都要跑 daily_drill 与 e2e，周末人少、机器空，适合跑这三项重的（回放要调模型）",
    ),
    Cadence(
        name="混沌故障注入：超时 / 5xx / 限流 / kill -9 / 磁盘满（每周）",
        interval_days=7,
        evidence=("state/reports/weekly/chaos-*.json",),
        how="python tools/chaos_weekly.py",
        note="全部是**注入式**故障：不真写满磁盘、不杀本机进程（只杀自己起的子进程）；"
        "红线是「降级未生效 = 冻结写权限」，其中 401/402 不重试那条是安全红线",
        suggested="每周六跑，紧跟脏测试（同一个周末窗口，一起看两份报告）",
    ),
    Cadence(
        name="红队对抗样本（每月）",
        interval_days=30,
        evidence=("state/reports/monthly/redteam-*.json",),
        how="python tools/redteam_month.py",
        note="任一失守 = 立即吊销写 token（8.3 红线）；样本在 tests/fixtures/redteam/（入库）"
        "与 sandbox-hostile（tools/sandbox_repos.py build 生成）",
        suggested="每月 1 号跑：**先跑红队再跑别的**。若它失守，当天剩下的自动化都不该继续 ——"
        "写权限要先冻掉（8.3：任一失守 = 立即吊销写 token）",
    ),
    Cadence(
        name="模型基线复测（每月）",
        interval_days=30,
        evidence=("state/capabilities.yaml",),
        how="python tools/probe_flash.py + 本地小模型探活（对照 0.2 的基线）",
        note="漂移 >10% → 排查（8.3）",
        suggested="每月 1 号跑（与红队同一天，但**排在红队之后**）",
    ),
)


@dataclasses.dataclass
class Finding:
    cadence: Cadence
    last_run: str | None
    age_days: int | None
    status: str          # ok | due | missing

    def as_dict(self) -> dict:
        return {
            "name": self.cadence.name,
            "interval_days": self.cadence.interval_days,
            "last_run": self.last_run,
            "age_days": self.age_days,
            "status": self.status,
            "how": self.cadence.how,
            "note": self.cadence.note,
            "suggested": self.cadence.suggested,
        }


def newest_evidence(patterns: tuple[str, ...], *, root: Path = ROOT) -> tuple[Path, datetime] | None:
    """
    在一组 glob 里找**最新**的产物（按文件 mtime）。

    用 mtime 而不是"文件名里的日期"：文件名格式在 12 个工具之间并不统一，
    而 mtime 不会骗人（除非有人手工改时间戳，那已经超出这个工具的职责了）。

    `root` 可注入是为了**测得了**：判"到期/未到期"的逻辑必须能用造出来的旧文件验证。
    """
    newest: tuple[Path, datetime] | None = None
    for pattern in patterns:
        for path in root.glob(pattern):
            candidates = [path] if path.is_file() else [item for item in path.rglob("*") if item.is_file()]
            for item in candidates:
                moment = datetime.fromtimestamp(item.stat().st_mtime, tz=timezone.utc)
                if newest is None or moment > newest[1]:
                    newest = (item, moment)
    return newest


def inspect(
    cadences: tuple[Cadence, ...] = CADENCES,
    *,
    now: datetime | None = None,
    root: Path = ROOT,
) -> list[Finding]:
    now = now or datetime.now(timezone.utc)
    findings: list[Finding] = []
    for cadence in cadences:
        hit = newest_evidence(cadence.evidence, root=root) if cadence.evidence else None
        if hit is None:
            findings.append(Finding(cadence, None, None, "missing"))
            continue
        path, moment = hit
        age = (now - moment).days
        status = "ok" if age <= cadence.interval_days else "due"
        findings.append(Finding(cadence, path.relative_to(root).as_posix(), age, status))
    return findings


def render(findings: list[Finding], *, only_due: bool = False) -> str:
    lines: list[str] = []
    marks = {"ok": "未到期", "due": "**已到期**", "missing": "**无记录**"}
    for finding in findings:
        if only_due and finding.status == "ok":
            continue
        cadence = finding.cadence
        if finding.status == "missing":
            head = f"[{marks[finding.status]}] {cadence.name}（要求每 {cadence.interval_days} 天）"
            lines.append(head)
            lines.append(f"    从没跑过，或没有产物可查 —— 怎么跑：{cadence.how}")
        else:
            head = (
                f"[{marks[finding.status]}] {cadence.name}（要求每 {cadence.interval_days} 天）"
                f"　上次 {finding.last_run}（距今 {finding.age_days} 天）"
            )
            lines.append(head)
            if finding.status == "due":
                lines.append(f"    该跑了：{cadence.how}")
        if cadence.suggested:
            # 建议时机与"该跑了 / 没跑过"无关：**不管到期与否都打出来**。
            # 它的读者是"想安排下个月什么时候跑"的人，不是"今天该跑什么"的判定。
            lines.append(f"    建议时机：{cadence.suggested}")
        if cadence.note:
            lines.append(f"    注：{cadence.note}")
    return "\n".join(lines)


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

    parser = argparse.ArgumentParser(description="路线 8.3 频率表的到期审计")
    parser.add_argument("--due", action="store_true", help="只列该跑了 / 从没跑过的")
    parser.add_argument("--json", action="store_true", help="输出 JSON（给别的脚本读）")
    args = parser.parse_args()

    findings = inspect()
    if args.json:
        print(json.dumps([item.as_dict() for item in findings], ensure_ascii=False, indent=2))
    else:
        body = render(findings, only_due=args.due)
        print(body or "没有到期的节奏（全部在周期内）")
        missing = [item for item in findings if item.status == "missing"]
        due = [item for item in findings if item.status == "due"]
        print()
        print(f"小结：未到期 {len(findings) - len(missing) - len(due)} 条、已到期 {len(due)} 条、无记录 {len(missing)} 条")
        if missing:
            print("**无记录 ≠ 没问题**：这几条节奏从来没人跑过（或没有产物可查），要么补上产物，要么按 8.3 承认没在做。")

    if any(item.status == "missing" for item in findings):
        return 2
    if any(item.status == "due" for item in findings):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
