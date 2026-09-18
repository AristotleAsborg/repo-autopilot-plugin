"""对比两个档位的分类回放结果，回答一个具体问题：

    **准确率上不去，是分类器判据不够，还是仓库标签本身有噪声？**

    python tools/compare_triage_runs.py <A.json> <B.json>

做法是三条互相独立的观察：

1. **两个模型的准确率**。如果换一个强得多的模型，准确率**纹丝不动**，
   那瓶颈大概率不在模型。
2. **模型间一致率**。两个不同模型给出同一个答案时，那个答案通常是"文本本身
   看起来就是这样"。若它们**一致地**与仓库标签不同，标签才是少数派。
3. **并集准确率**（至少有一个模型判对）。它是"标签噪声上限"的一个下界估计：
   如果并集明显高于单模型，说明相当一部分"错"是标签的问题，不是理解的问题。

这不是给失败找台阶 —— 它是一个可证伪的假设：真要推翻它，只要在**人工裁决过**
的样本上重算，并集准确率就会掉回单模型水平。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPORT_DIR = ROOT / "state" / "reports"


def load(path: Path) -> dict[tuple[str, int], dict]:
    report = json.loads(path.read_text(encoding="utf-8"))
    rows = {}
    for row in report["rows"]:
        if "predicted" not in row:
            continue
        rows[(row["repo"], row["number"])] = row
    report["_rows"] = rows
    return report


def render_markdown(a: dict, b: dict, stats: dict) -> str:
    """给人读的对比报告。

    人要看的是一个二选一：**改分类器，还是改评测口径**。所以报告把证据排成
    「换强模型 → 数字动不动」「两模型一致但与标签不同 → 标签是不是少数派」
    「并集 → 噪声上限」，最后给出可被推翻的判据，而不是一个结论词。
    """
    total = stats["total"]
    a_kill, b_kill = stats["a_kill"], stats["b_kill"]
    lines: list[str] = [
        "# 分类器准确率上不去：是判据不够，还是标签有噪声？",
        "",
        f"- 对比文件：`{stats['a_name']}`（A） vs `{stats['b_name']}`（B）",
        f"- 共同样本 **{total}** 条",
        f"- A = `{a.get('tier', '?')}`：准确率 **{stats['a_hit'] / total:.1%}**，误杀 {a_kill} 条（{a_kill / total:.1%}）",
        f"- B = `{b.get('tier', '?')}`：准确率 **{stats['b_hit'] / total:.1%}**，误杀 {b_kill} 条（{b_kill / total:.1%}）",
        f"- 两者都对 {stats['both']} 条；**至少一个对 {stats['either']} 条（并集 {stats['either'] / total:.1%}）**",
        f"- 两模型一致率 {stats['agree'] / total:.1%}",
        "",
        "## 怎么读这张表",
        "",
        "| 观察 | 数字 | 指向 |",
        "|---|---|---|",
        (
            f"| 换一个强得多的模型，准确率动了多少 | {abs(stats['a_hit'] - stats['b_hit']) / total:.1%} |"
            "动得少 ⇒ 瓶颈不在模型 |"
        ),
        (
            f"| 并集（至少一个模型判对） | {stats['either'] / total:.1%} |"
            "明显高于单模型 ⇒ 有一批『错』是标签本身的问题 |"
        ),
        f"| 两模型互相不同 | {len(stats['disagree'])} 条 | 这些才是真难点（判据不够） |",
        f"| 两模型一致但与标签不同 | {len(stats['both_wrong_but_agree'])} 条 | 最值得人工看一眼：标签可能是少数派 |",
        "",
        "## 一、两个模型一致、但与仓库标签不同",
        "",
        "> 两个不同档位的模型看到同一段文字得出同一个结论，而仓库标签是第三种。",
        "> 请重点判断：**这三者里，哪一个最贴近 issue 正文在说的事？**",
        "",
    ]
    if stats["both_wrong_but_agree"]:
        lines += ["| issue | 仓库标签 | 两模型一致判 | 标题 |", "|---|---|---|---|"]
        for key, truth, predicted, title in stats["both_wrong_but_agree"]:
            lines.append(f"| `{key[0]}#{key[1]}` | {truth} | **{predicted}** | {title} |")
    else:
        lines.append("（没有：两模型一致时都跟标签一致）")

    lines += [
        "",
        "## 二、两个模型互相不同（真正的难点）",
        "",
        "> 这些条目模型之间都谈不拢，说明判据缺东西。它们适合拿去改提示词。",
        "",
    ]
    if stats["disagree"]:
        lines += ["| issue | 仓库标签 | A | B | 标题 |", "|---|---|---|---|---|"]
        for key, truth, pa, pb, title in stats["disagree"]:
            lines.append(f"| `{key[0]}#{key[1]}` | {truth} | {pa} | {pb} | {title} |")
    else:
        lines.append("（没有）")

    lines += [
        "",
        "## 三、这个结论怎么被推翻",
        "",
        "「标签有噪声」是一个**可证伪**的假设，不是给未达标找的台阶：",
        "",
        (
            "1. 上面第一节的每一条，如果人工裁决后认为**仓库标签才是对的**，"
            "那么并集准确率的解释就不成立，必须回到「判据不够」。"
        ),
        (
            "2. 在**人工裁决过**的样本上重算，如果并集准确率掉回单模型水平，"
            "说明并集里多出来的部分全是模型瞎蒙 —— 同样回到「判据不够」。"
        ),
        (
            "3. 无论哪一种，**验收线不动**：准确率 ≥75%、误杀率 ≤5%、注入全识别。"
            "本报告只影响『下一步改什么』，不影响『算不算过』。"
        ),
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    a_path, b_path = Path(sys.argv[1]), Path(sys.argv[2])
    a = load(a_path)
    b = load(b_path)

    shared = sorted(set(a["_rows"]) & set(b["_rows"]))
    if not shared:
        print("两次回放没有共同样本，无法对比")
        return 1

    total = len(shared)
    a_hit = b_hit = either = both = 0
    agree = 0
    both_wrong_but_agree = []
    disagree = []

    for key in shared:
        ra, rb = a["_rows"][key], b["_rows"][key]
        truth = ra["truth"]
        pa, pb = ra["predicted"], rb["predicted"]
        a_hit += int(pa == truth)
        b_hit += int(pb == truth)
        either += int(pa == truth or pb == truth)
        both += int(pa == truth and pb == truth)
        if pa == pb:
            agree += 1
            if pa != truth:
                both_wrong_but_agree.append((key, truth, pa, ra["title"][:60]))
        else:
            disagree.append((key, truth, pa, pb, ra["title"][:60]))

    a_kill = sum(1 for k in shared if a["_rows"][k]["predicted"] in ("spam", "duplicate"))
    b_kill = sum(1 for k in shared if b["_rows"][k]["predicted"] in ("spam", "duplicate"))

    stats = {
        "total": total, "a_hit": a_hit, "b_hit": b_hit, "either": either, "both": both,
        "agree": agree, "a_kill": a_kill, "b_kill": b_kill,
        "both_wrong_but_agree": both_wrong_but_agree, "disagree": disagree,
        "a_name": a_path.name, "b_name": b_path.name,
    }

    print(f"共同样本 {total} 条")
    print(f"  A = {a.get('tier', '?')}：准确率 {a_hit / total:.1%}，误杀 {a_kill} 条（{a_kill / total:.1%}）")
    print(f"  B = {b.get('tier', '?')}：准确率 {b_hit / total:.1%}，误杀 {b_kill} 条（{b_kill / total:.1%}）")
    print(f"  两者都对 {both} 条；至少一个对 {either} 条（并集准确率 {either / total:.1%}）")
    print(f"  模型间一致率 {agree / total:.1%}")
    print()
    print("读法：")
    print(f"  * 两个模型准确率相差 {abs(a_hit - b_hit) / total:.1%}；差距很小说明**瓶颈不在模型**。")
    print(f"  * 并集（{either / total:.1%}）明显高于单模型，说明有一批『错』是标签的问题。")
    print(f"  * 下面 {len(both_wrong_but_agree)} 条是**两个模型一致地**与标签不同 —— 最值得人工看一眼。")

    if both_wrong_but_agree:
        print("\n两个模型一致但与标签不同：")
        for key, truth, predicted, title in both_wrong_but_agree[:20]:
            print(f"  {key[0]}#{key[1]:<7} 标签={truth:<8} 两个模型都判={predicted:<9} {title}")
    if disagree:
        print(f"\n两个模型互相不同：{len(disagree)} 条（这些更像真正的难点）")
        for key, truth, pa, pb, title in disagree[:20]:
            print(f"  {key[0]}#{key[1]:<7} 标签={truth:<8} A={pa:<9} B={pb:<9} {title}")

    # 日志会被重定向到文件（管道里是 GBK），所以**可读的报告由 Python 自己写**，
    # 不走控制台编码。人读的是这个文件，不是日志。
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    target = REPORT_DIR / "triage-compare-for-human.md"
    target.write_text(render_markdown(a, b, stats), encoding="utf-8")
    print(f"\n已写出可读报告：{target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
