"""路线 2.2：停止判断器的 20 组样本评测。

     python tools/eval_stopper.py            # 跑评测，写报告
    python tools/eval_stopper.py --no-model # 不调模型（只跑规则与算法两路）

## 关于"标注"这件事，先把话说清楚

路线要求"20 组标注样本（该停/不该停）准确率 ≥80% 才上线"。

**这 20 组的标签是我按三条写明的规则造出来的，不是人类标的。** 三条规则是：

    R1  已答满 MAX_ROUNDS 轮     → 该停（规则③，与模型无关）
    R2  内容已足够且最后两轮 spec 没变化 → 该停（对应信号①②）
    R3  轮次未满且 spec 还在变   → 不该停

所以这个评测测的是"判断器有没有实现这三条规则"，**不是**"这三条规则对不对"。
后者只有人类能判断。评测会把全部 20 个场景连同我给的标签、以及实测到的
三路信号一起落盘，供人工复核与修改。

## 为什么场景要真的跑一遍 refiner

如果手工拼一个 SpecDraft 再把 `spec_digest` 写成我想要的样子，信息增益那一路就成了
自证 —— 我写的 digest 当然会给出我要的余弦。所以场景都经过真实的 refiner 流水：
脚本只负责"每轮模型会问什么、会更新什么"，digest 由 refiner 自己按累积文本算出来。
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.spec import MAX_ROUNDS, IdeaRefiner, StopJudger, new_draft

REPORT_DIR = ROOT / "state" / "reports"
# 轮数上限**只从 src.spec 取一处**。这里曾经自己写死 12，
# 而人类把上限改成 24 之后，评测里的 "cap" 场景就再也撞不到上限了 ——
# 那种不一致不会报错，只会让评测悄悄测不到它本该测的东西。


@dataclass
class Scenario:
    key: str
    rule: str                       # R1 / R2 / R3
    should_stop: bool
    idea: str
    answers: list[str]
    updates: list[dict]             # 每轮 draft_updates，长度与 answers 相同
    note: str = ""

    def proposals(self) -> list[dict]:
        """
        造这一场景的逐轮提案。

        **问题必须逐轮不同、且是是/否问句**（2026-09-12）：
        原来每轮都拼成「<idea 前 12 字>——第 N 个问题？」，除了数字以外完全相同 ——
        于是 `IdeaRefiner.propose` 的重复提问兜底（D2b）会把它们打回，
        重试用尽就抛 `RepeatedQuestion`，**整个 2.2 评测直接崩**（实测踩过）。
        评测夹具自己造了不合格的输入，却让被测代码背锅，这是最容易误判的一类失败。

        现在每轮给一个真正不同的是/否问题（仍然只用于评测，不代表文案质量）。
        """
        return [
            {
                "question": f"{self.idea[:12]}——第 {index} 轮：是否按这个方向继续收敛？",
                "why": "脚本生成，仅用于评测",
                "draft_updates": update,
            }
            for index, update in enumerate(self.updates, start=1)
        ]


def _rich(prefix: str, count: int) -> list[str]:
    return [f"{prefix} 功能 {index}" for index in range(1, count + 1)]


def build_scenarios() -> list[Scenario]:
    scenarios: list[Scenario] = []

    # ---- R1：满 MAX_ROUNDS 轮，硬上限必须停（4 组，每轮都还在加新内容，
    #      所以这是最极端的情况：内容"看起来还没完"，但规则说必须停）
    for index in range(1, 5):
        scenarios.append(
            Scenario(
                key=f"cap-{index}",
                rule="R1",
                should_stop=True,
                idea=f"第 {index} 个长期拖着的大需求",
                answers=[f"补充第 {round_no} 点" for round_no in range(1, MAX_ROUNDS + 1)],
                updates=[{"features": [f"功能 {round_no}"]} for round_no in range(1, MAX_ROUNDS + 1)],
                note=f"每轮都在加东西，但 {MAX_ROUNDS} 轮是硬上限",
            )
        )

    # ---- R2：内容够了、最后两轮 spec 不再变化（6 组）
    for index in range(1, 7):
        rounds = 5 + index % 3
        updates = [{"features": [f"功能 {n}"], "acceptance": [f"`cmd {n}` 退出码为 0"]}
                   for n in range(1, rounds - 1)]
        updates += [{}, {}]          # 最后两轮什么都没新增
        scenarios.append(
            Scenario(
                key=f"quiet-{index}",
                rule="R2",
                should_stop=True,
                idea=f"已经聊透了的第 {index} 个需求",
                answers=[f"就这样吧（第 {n} 轮）" for n in range(1, rounds + 1)],
                updates=updates,
                note="最后两轮 spec 没有新增内容",
            )
        )

    # ---- R3：轮次未满且内容还在变（10 组）
    for index in range(1, 11):
        rounds = 2 + index % 5
        updates = [{"features": [f"功能 {n}"]} for n in range(1, rounds + 1)]
        scenarios.append(
            Scenario(
                key=f"moving-{index}",
                rule="R3",
                should_stop=False,
                idea=f"还在展开的第 {index} 个想法",
                answers=[f"还要支持第 {n} 种情况" for n in range(1, rounds + 1)],
                updates=updates,
                note=f"每轮都在加新内容，且远未到 {MAX_ROUNDS} 轮",
            )
        )

    return scenarios


def materialise(scenario: Scenario, scratch: Path):
    """把场景真的跑一遍 refiner，让 digest 由累积文本自然产生。"""
    proposals = scenario.proposals()
    index = {"value": 0}

    def generate(messages: list[dict], schema: object) -> dict:
        current = proposals[index["value"]]
        index["value"] += 1
        return current

    # `strict_questions=False`：这是**合成草稿**去测 2.2，问题是夹具编的。
    # 2.1 的提问质量兜底（是/否 + 不重复）在这里没有意义 —— 逼一个夹具把 24 轮都编成
    # 各不相同的问句，只会让"评测 2.2"变成"评测夹具"，而它的失败还会被误读成判断器坏了。
    refiner = IdeaRefiner(
        store_dir=scratch / scenario.key, generate=generate, strict_questions=False
    )
    draft = new_draft(scenario.idea)
    refiner.persist(draft)
    for answer in scenario.answers:
        refiner.ask(draft)
        refiner.answer(draft, answer)
    return draft


def render_markdown(report: dict) -> str:
    """
    把评测结果渲染成**给人读的** Markdown。

    为什么不能只留 JSON：复核标签的人需要一眼看清"我凭什么这么标、实测到了什么"。
    JSON 里这些信息都在，但读起来要先脑内解析一遍结构 —— 而这是一个**请人花时间**
    的请求，不该让对方先做一遍数据整理。

    每条样本都给出：我给的标签 → 为什么 → 三路信号各自说了什么（含具体数值）。
    """
    lines: list[str] = [
        "# 停止判断器：20 组样本，请复核标签",
        "",
        f"- 生成日期：{report['date']}",
        f"- 模式：{report['mode']}",
        (
            f"- 结果：准确率 {report['accuracy']:.1%}（门槛 {report['threshold']:.0%}）"
            f"，{'达标' if report['passed'] else '**未达标**'}"
        ),
        f"- 标签来源：{report['labels_are']}",
        "",
        "## 你要做什么",
        "",
        "每条样本下面都有一个「**我标的是**」。你要判断的只有一件事：",
        "**这份 spec 到那个时点，算不算已经可以开工了？**",
        "",
        "不同意就在旁边标注，然后把文件给我；不用改格式。",
        "",
        "## 速览",
        "",
        "| # | 场景 | 规则 | 我标的 | 实测表决 | 三路信号（停/不停） |",
        "|---|---|---|---|---|---|",
    ]
    for index, row in enumerate(report["rows"], start=1):
        label = "**该停**" if row["expected_stop"] else "不该停"
        signals = " / ".join(
            f"{s['name']}={'停' if s['suggests_stop'] else '不停'}" for s in row["signals"]
        )
        lines.append(
            f"| {index} | `{row['key']}` | {row['rule']} | {label} | {row['votes']}/3 | {signals} |"
        )

    lines += ["", "## 逐条", ""]
    for index, row in enumerate(report["rows"], start=1):
        lines += [
            (
                f"### {index}. `{row['key']}` —— 我标的是："
                f"{'**该停**' if row['expected_stop'] else '不该停'}"
            ),
            "",
            f"- **想法**：{row['idea']}",
            f"- **我为什么这么标**：{row['note']}",
            (
                f"- **实测**：已答 {row['rounds']} 轮，功能 {row['features']} 项，"
                f"表决 {row['votes']}/3；判断器给出「{'停' if row['predicted_stop'] else '不停'}」"
                f"{'（与我的标签一致）' if row['correct'] else '（与我的标签**不一致**）'}"
            ),
            "",
            "- **三路信号各自说了什么**：",
        ]
        for signal in row["signals"]:
            verdict = "建议停" if signal["suggests_stop"] else "不建议停"
            lines.append(f"  - `{signal['name']}` → {verdict}：{signal['detail']}")
        lines.append("")
        lines.append("- **我想请你特别看**：" + _attention(row))
        lines.append("")

    lines += [
        "---",
        "",
        "## 附：三条规则（我的标签就是从它们推出来的）",
        "",
        "| 规则 | 内容 | 该停？ |",
        "|---|---|---|",
        f"| R1 | 已答满 {MAX_ROUNDS} 轮 | 该停（硬上限，与模型判断无关） |",
        "| R2 | 内容已足够、且最后两轮 spec 没有变化 | 该停（对应信号①完整度 + ②信息增益） |",
        "| R3 | 轮次未满、且 spec 还在变 | 不该停 |",
        "",
        "**评测证明的是「判断器实现了这三条规则」，不是「这三条规则是对的」。**",
        "后者只有你能判断 —— 这也正是这个文件存在的理由。",
        "",
    ]
    return "\n".join(lines)


def _attention(row: dict) -> str:
    """给每条样本一句"最值得你注意的地方"，而不是让复核者自己去发现。"""
    by_name = {s["name"]: s for s in row["signals"]}
    votes = row["votes"]
    if row["rule"] == "R1":
        return (
            f"这条**不是**因为内容够了才停，而是因为撞上了 {MAX_ROUNDS} 轮硬上限 —— "
            "内容看起来还没完。你认同这个上限吗？"
        )
    if row["rule"] == "R2":
        completeness = by_name.get("completeness", {})
        if not completeness.get("suggests_stop"):
            return "完整度那一路其实**没**建议停，是信息增益 + 另一路把它推过线的。这种组合你认可吗？"
        return "两路都说停；重点是「最后两轮没新增内容」这个判据够不够 —— 会不会漏掉还在酝酿的需求？"
    if votes:
        return f"我标的是不该停，但**有 {votes} 路建议停**。这是最接近误停的一类，请重点看。"
    return "三路都没建议停，属于最清楚的一类。"


def main() -> int:
    parser = argparse.ArgumentParser(description="2.2 停止判断器评测")
    parser.add_argument("--no-model", action="store_true", help="不调模型，只跑规则与算法两路")
    parser.add_argument("--limit", type=int, default=0, help="只跑前 N 组（调试用）")
    parser.add_argument(
        "--render-only",
        action="store_true",
        help="不重新评测，只把最近一次的 JSON 结果渲染成可读的 Markdown",
    )
    args = parser.parse_args()

    if args.render_only:
        candidates = sorted(REPORT_DIR.glob("stopper-eval-*.json"))
        if not candidates:
            print("找不到 stopper-eval-*.json，先跑一次评测")
            return 1
        report = json.loads(candidates[-1].read_text(encoding="utf-8"))
        (REPORT_DIR / "stopper-labels-for-human-review.md").write_text(
            render_markdown(report), encoding="utf-8"
        )
        print(f"已渲染：{candidates[-1].name} -> stopper-labels-for-human-review.md")
        return 0

    scratch = ROOT / ".cache" / "stopper-eval"
    scratch.mkdir(parents=True, exist_ok=True)

    if args.no_model:
        judger = StopJudger(
            spec_dir=scratch / "specs",
            score=lambda messages, schema: {"completeness": 0.0, "missing": ["未启用模型"]},
        )
    else:
        judger = StopJudger(spec_dir=scratch / "specs")

    scenarios = build_scenarios()
    if args.limit:
        scenarios = scenarios[: args.limit]

    rows: list[dict] = []
    hits = 0
    for scenario in scenarios:
        draft = materialise(scenario, scratch)
        decision = judger.judge(draft)
        correct = decision.stop == scenario.should_stop
        hits += int(correct)
        rows.append(
            {
                "key": scenario.key,
                "rule": scenario.rule,
                "expected_stop": scenario.should_stop,
                "predicted_stop": decision.stop,
                "correct": correct,
                "votes": decision.votes,
                "rounds": draft.answered_rounds,
                "features": len(draft.features),
                "signals": [
                    {"name": s.name, "suggests_stop": s.suggests_stop, "detail": s.detail}
                    for s in decision.signals
                ],
                "idea": scenario.idea,
                "note": scenario.note,
            }
        )
        flag = "命中" if correct else "**错**"
        print(f"  {scenario.key:<12} {scenario.rule}  期望停={scenario.should_stop!s:<5} "
              f"实际停={decision.stop!s:<5} 票={decision.votes}  {flag}")

    total = len(rows)
    accuracy = hits / total if total else 0.0
    per_signal = {}
    for name in ("completeness", "info_gain", "round_cap"):
        right = 0
        for row in rows:
            signal = next(s for s in row["signals"] if s["name"] == name)
            right += int(signal["suggests_stop"] == row["expected_stop"])
        per_signal[name] = round(right / total, 4) if total else 0.0

    # 时间戳一律 UTC：报告里带本地时间的话，跨时区复核同一份报告会得出不同日期
    # （gate.yml 里 TZ=UTC 也是同一个道理 —— 时间不一致是"测试假失败"的常见来源）。
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    report = {
        "date": stamp,
        "samples": total,
        "accuracy": round(accuracy, 4),
        "threshold": 0.8,
        "passed": accuracy >= 0.8,
        "per_signal_accuracy": per_signal,
        "labels_are": "machine-derived from rules R1/R2/R3 (NOT human-labelled)",
        "mode": "no-model" if args.no_model else "local-small-model",
        "rows": rows,
    }
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    (REPORT_DIR / f"stopper-eval-{stamp}.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (REPORT_DIR / "stopper-labels-for-human-review.json").write_text(
        json.dumps(
            [
                {
                    "key": row["key"],
                    "rule": row["rule"],
                    "idea": row["idea"],
                    "my_label": "该停" if row["expected_stop"] else "不该停",
                    "why": row["note"],
                    "measured": {
                        "rounds": row["rounds"],
                        "features": row["features"],
                        "votes": row["votes"],
                        "signals": row["signals"],
                    },
                }
                for row in rows
            ],
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    # 同一份数据的可读版本。复核是**请人花时间**的事，不该让对方先解析一遍 JSON。
    (REPORT_DIR / "stopper-labels-for-human-review.md").write_text(
        render_markdown(report), encoding="utf-8"
    )

    print()
    print(f"样本 {total} 组，命中 {hits}，准确率 {accuracy:.1%}（门槛 80%）")
    print("各信号单独看：" + "，".join(f"{k}={v:.1%}" for k, v in per_signal.items()))
    print("标签来源：machine-derived（R1/R2/R3）——**需要人工复核**，见 "
          "state/reports/stopper-labels-for-human-review.json")
    return 0 if accuracy >= 0.8 else 1


if __name__ == "__main__":
    raise SystemExit(main())
