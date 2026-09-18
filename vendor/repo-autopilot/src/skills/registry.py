"""第七部分：外壳的实现 —— **指令是产品说明书，注册表是唯一真相**。

路线 7.1 给了 8 条指令；7.2 要求为每条建一份 `skills/*.md`（五段式）、
再把路由表写进 `AGENTS.md`；7.4 要求"指令清单、AGENTS.md、skills/ 三者一一对应"。

## 为什么把指令表写成代码

三处（指令清单、路由表、SKILL.md）手工维护必然漂移 —— 这是 7.4 自己点出的风险。
所以这里把 8 条指令做成**一张表**：`COMMANDS`。
`skills/*.md` 与 `AGENTS.md` 都由 `tools/build_skills.py` 从这张表渲染出来，
`check_consistency()` 再反过来校验磁盘上的文件与表一致（谁改了文件、没改表，立刻报错）。

## 五段式（路线 7.2 第 2 条，缺一不可）

1. **TEST_GATE 全文**（原样嵌入，由 `TEST_GATE_TEXT` 提供，校验时逐字比对）
2. **前置**：读 `state/capabilities.yaml` 与 `state/mode.json`
3. **主流程**：本指令的步骤（引用第五部分对应阶段，不重复抄写）
4. **人类确认点**：何时停下、用哪个 0.5.B 模板
5. **失败出口**：按 TEST_GATE 输出 BLOCKED 报告并停止
"""

from __future__ import annotations

import dataclasses
import json
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
SKILLS_DIR = ROOT / "skills"
AGENTS_PATH = ROOT / "AGENTS.md"

#: TEST_GATE 全文（路线 0.2，**逐字**嵌入每一份 SKILL.md；校验时会逐字比对）
TEST_GATE_TEXT = """1. 当前步骤的验收测试未全部通过时，你绝对禁止：
   - 声明"完成"、"基本完成"、"核心功能已完成"或任何近义表述；
   - 进入下一步骤或建议进入下一步骤；
   - 输出 spec/补丁/仓库等任何交付物并标注为可用。
2. 你唯一允许的动作：输出 BLOCKED 报告（失败测试的原始日志关键段、
   根因分析、已尝试的修复、下一步建议），然后停止，等待指令。
3. 绝对禁止通过以下方式"让测试通过"：修改/删除/注释测试用例、
   提高 skip 或 xfail、放宽断言、降低阈值、mock 掉被测对象本身、
   缩短超时以规避 flaky 失败。发现即视为严重违规。
4. 声明 PASS 时必须附上测试命令与原始输出的关键段落作为证据；
   无证据的 PASS 声明视为无效，等同 BLOCKED。
5. 验收标准里写有"≥X%"的指标，四舍五入无效，以原始数值判定。"""


@dataclasses.dataclass(frozen=True)
class Command:
    """一条对话指令（路线 7.1 的一行）。"""

    name: str                 # `/修复`
    slug: str                 # skills/fix.md
    args: str                 # `<issue号>`
    action: str               # 一句话说明干什么
    confirmation: str         # 人类确认点（只读写"无"）
    stage: str                # 引用第五部分的步骤
    flow: tuple[str, ...]     # 主流程步骤（不含五段式骨架）
    writes: bool              # 是否含对外写操作（决定闸门）

    @property
    def usage(self) -> str:
        return f"{self.name} {self.args}".strip()


#: 8 条指令（路线 7.1 的最终版）。顺序即文档顺序。
COMMANDS: tuple[Command, ...] = (
    Command(
        name="/细化idea",
        slug="refine",
        args="<一句话>",
        action="M1 追问循环：把一句 idea 追问成可施工的 spec",
        confirmation="每轮追问回答 是/否（0.5.B① 模板）",
        stage="阶段二 2.1 / 2.2",
        flow=(
            "读 `state/capabilities.yaml` 与 `state/mode.json`；offline 走本地模型。",
            (
                "把 idea 落成 `state/specs/<id>.draft.json`（**源卡，唯一真相**），进入追问循环"
                "（`src.spec.IdeaRefiner`）；收敛后另写一份给人看的 `state/specs/<id>.md`。"
            ),
            (
                "每轮一个问题、且必须是**是/否问题**（0.5.B① 模板：开放问题会被代码打回重问），"
                "回答写回 spec；`src.spec.StopJudger` 判定是否收敛。"
            ),
            (
                "**本轮声明了 `options`（是 A 还是 B）时，把候选一并呈现给人类**，并说明可以回答"
                "「是」「否」或**「两者都要」**：后者会把候选项**分别**落成条目"
                "（`both_targets()` 代码兜底，离线档也不丢）。"
                "没声明候选时「两者都要」无处可落 —— 系统会告警并把它变成下一问的题目，"
                "所以这类问题不要漏 `options`。"
            ),
            (
                "收敛后产出 spec 终稿，并把「是否可以把当前这份 spec 当作可动工版本」交给人类"
                "（一次是/否）。"
            ),
            (
                "**每轮判停之后把缺口反馈回提问**：`refiner.note_gaps(decision.blocked_by_gaps)` —— "
                "下一问会带着这些缺口去问。不接这一步的话，`completeness.missing` 会连续多轮"
                "重复同样的缺口而**永远不变成一个问题**（实测：那份 spec 因此一直空着验收条件）。"
            ),
            (
                "**限制性问句的「否」不能白说**（第四轮实测报告 G5）：问句里带「只／仅／单纯」时，"
                "模型必须在 `no_means` 里声明「否」之后还要做的候选；人类答「否」时 "
                "`IdeaRefiner` 会把「范围更宽、还要做什么」记进 `carry_over` 并**强制带进下一问**。"
                "同时把 `refiner.warnings`（尤其「某轮没有沉淀任何字段」）如实转述给人类 —— "
                "那是「这一问白问了」的信号，终稿的追问记录表里也会把它标成「（空转）」。"
            ),
        ),
        writes=False,
    ),
    Command(
        name="/处理反馈",
        slug="triage",
        args="[仓库] [issue号]",
        action="单个 issue：M2 分类 → 视情进 M3",
        confirmation="推送 PR 前（0.5.B② 模板）",
        stage="阶段三 3.1–3.3 / 阶段四",
        flow=(
            "拉取 issue 原文（读操作直通）；先进查重（3.2，命中就提示重复，不重复分类）。",
            "分类打标 + 首条回复模板（3.1）；置信度 <0.7 只发建议评论，不动标签。",
            "打回判定（3.3）：预检不过就退回并记账，不进入修复。",
            "判为缺陷 → 走 `/修复` 的主流程（定位 → 修复循环 → 门禁）。",
            "产出报告并调用 `require_human_approval()`；**未获「是」之前禁止任何写操作**。",
        ),
        writes=True,
    ),
    Command(
        name="/一键处理",
        slug="batch",
        args="[仓库]",
        action="批量处理该仓库全部 open issue，末尾一次性汇总审批",
        confirmation="末尾逐条批是/否（批量呈现≠批量放行，0.5.B② 模板）",
        stage="阶段三 + 阶段四（批量调度见 `src.batch`）",
        flow=(
            "拉取该仓库全部 open issue，**单次最多 20 个**；超出的留到下轮并告知剩余数量。",
            ("每个 issue 独立走一遍「查重 → 分类 → 视情修复 → 门禁」，进度实时落盘 "
            "`state/batch_<date>.json`；重发同一指令从断点续跑，不重复处理。"),
            "单个 issue 失败不阻断批次：标 `needs_human` 后继续下一个，失败清单进审批单附录。",
            ("全部完成后生成 `state/approvals/batch_<date>.md`：列出本批全部待批 PR"
            "（每条含一句话摘要 + 风险），**人类逐条回是/否**。"),
            "批次进行中收到 `/应急` 立即暂停当前批次，先处理应急。",
        ),
        writes=True,
    ),
    Command(
        name="/修复",
        slug="fix",
        args="<issue号>",
        action="跳过分类直接进 M3 修复循环",
        confirmation="推送 PR 前（0.5.B② 模板）",
        stage="阶段四 4.1–4.4",
        flow=(
            "拉取 issue 原文（读操作直通）。",
            "文件定位（4.1）：词法/grep → 本地小模型只看路径清单初筛 → 档案余弦精排，输出 ≤5 个候选。",
            "修复循环（4.2）：增量 diff → 沙箱副本打补丁 → 跑测试 → 带报错重来（≤15 轮，不收敛则落盘待续跑）。",
            "测试门禁（4.3）：目标测试全绿 / 全量回归不新增失败 / lint 不新增告警 / 路径黑名单。",
            "生成五段式报告并调用 `require_human_approval()`；未获「是」之前禁止任何写操作（4.4）。",
        ),
        writes=True,
    ),
    Command(
        name="/找轮子",
        slug="scout",
        args="<功能描述>",
        action="M4 猎手：搜索 → 硬过滤 → 看代码打分 → 抓取",
        confirmation="无（只读）",
        stage="阶段五 5.1",
        flow=(
            "多轮搜索（模型规划换词）→ 硬过滤（stars/时效/归档）→ 只对候选看目录树与文件开头再打分。",
            "许可证**查表**判定：GPL/AGPL 一律 block，模型说 ok 也不改；识别不出按 warn（永不 usable）。",
            "低星或太旧但模型给出书面理由的可破格纳入，报告里单独标出供人抽查。",
            "`usage≠skip 且 license_risk=ok` 才抓取到 `state/vendor/`，记 commit hash 与 LICENSE 副本。",
        ),
        writes=False,
    ),
    Command(
        name="/建新项目",
        slug="scaffold",
        args="<spec路径|一句话>",
        action="M5 脚手架 + 内部测试 + 建仓",
        confirmation="选仓库名（三选一）+ 建库/首推前（两次，0.5.A③/0.5.B②）",
        stage="阶段六 6.1",
        flow=(
            "读 spec（或一句 idea）→ flash 生成骨架 + 单测 + README + LICENSE(MIT) + CI + .gitignore。",
            "生成物当场校验齐件；**本地跑与 CI 完全相同的命令**，红了就把真实失败喂回去重来（≤3 轮）。",
            "仓库名查重后给三个候选（也可自定义合法 slug）。",
            (
                "**候选名不足三个时不许装作三选一**（2026-09-17 总报告 P1-14）：中文 idea 抽不出 "
                "英文词时明说原因、请人类给名字或补 `keywords`；候选里出现**裸词**要提示与 "
                "PyPI/常见包撞名的风险（生成的项目要 `import` 自己的包名）。"
            ),
            "摘要 + 本地结果过闸门（`create_repo`）→ 建仓 → 首推 → 轮询云端 CI 到出结论。",
            (
                "**GitHub 是可选项（2026-09-18 人类要求）**：也可以只把项目**保存在本地** —— "
                "`tools/eval_scaffold.py --local` 走到「生成 + 本地 CI 干跑」为止，"
                "再调 `init_local_repository()` 做 `git init/add/commit`（不建远端仓库、不用任何 token）。"
                "理由：这条链路里真正有价值的部分（追问 → spec → 骨架 → 本地干跑）本来就不需要联网 —— "
                "**插件形态下更不该要求用户先交出 GitHub 写权限。**"
            ),
        ),
        writes=True,
    ),
    Command(
        name="/体检",
        slug="checkup",
        args="",
        action="全量回归 + 指标评测 + 输出报告",
        confirmation="无（只读）",
        stage="阶段八 8.3 离线评测",
        flow=(
            "跑全量测试套件与分段全量验收（`tools/acceptance.py`）。",
            "跑三份离线评测：分类（holdout）、查重、停止判断；比对上一轮指标。",
            (
                "**副本一致性（2026-09-17 人类要求写进常规检查）**：逐文件哈希比对源仓库与各份副本 —— "
                "工作区内的 `installed/repo-autopilot` 自动发现，机器特有的副本登记在 `state/copies.json`；"
                "比对逻辑是 `tools/package.py compare <目录>`（列出内容不同/缺失/多出的文件）。"
                "**规矩：覆盖或合并任何副本之前，先看这份差异清单，不一致就先问清是谁改的。**"
                "（起因是一次真实翻车：报告说『源仓库改了 `src/scaffold/core.py`』，实际只改在副本里，"
                "照叙述往下走那次修复会随下一次覆盖消失。）"
            ),
            "输出 `state/reports/checkup-<date>.md`：通过项、退化项、以及建议动作。",
            "连续两周下降 → 回滚模型或 prompt（8.3 红线）。",
        ),
        writes=False,
    ),
    Command(
        name="/应急",
        slug="emergency",
        args="[问题描述]",
        action="兜底唤起：自检诊断 + 给出修复选项",
        confirmation="执行修复动作前（0.5.B② 模板，A/B/C 选项）",
        stage="阶段七 7.2 第 4 条",
        flow=(
            ("跑 `scripts/doctor.py`：state 目录完整性、mode.json 合法性、队列孤儿任务、"
            "闸门悬挂审批（>72h 标出）、写 token 文件、本地小模型探活、GitHub 连通性。"),
            "输出诊断报告 + **A/B/C 式修复选项**（0.5.B② 模板）。",
            "人类选定后，修复动作仍然**必须过闸门**；未获「是」之前什么都不做。",
            "若队列里有批次在跑，先暂停它（7.1 第 5 条）。",
        ),
        writes=True,
    ),
)

#: 自然语言兜底（路线 7.2 第 3 条）：人类记不清指令名时的唤起通道
EMERGENCY_HINTS = (
    "坏了", "卡住", "报错", "异常", "不动了", "没反应", "挂了", "失败",
    "broken", "stuck", "error", "not working", "failed",
)

#: 手动兜底（路线 7.2 第 3 条）：终端直达，绕开对话层
CLI_ENTRY = "python -m src.cli <triage|fix|batch|emergency>"


class SkillError(RuntimeError):
    """技能文档或注册表不一致（缺段、缺件、漂移）。绝不静默放过。"""


def commands_by_name() -> dict[str, Command]:
    return {command.name: command for command in COMMANDS}


def commands_by_slug() -> dict[str, Command]:
    return {command.slug: command for command in COMMANDS}


def render_skill(entry: Command) -> str:
    """把一条指令渲染成**五段式** SKILL.md（路线 7.2 第 2 条，缺一不可）。"""
    lines = [
        f"# {entry.usage}",
        "",
        f"> {entry.action}　·　引用：{entry.stage}　·　含写操作：{'是' if entry.writes else '否'}",
        "",
        "## 一、TEST_GATE（测试铁律，原样嵌入路线 0.2）",
        "",
        "```",
        TEST_GATE_TEXT,
        "```",
        "",
        "## 二、前置",
        "",
        "1. 读 `state/capabilities.yaml`（能力探测结果）与 `state/mode.json`（online/offline）。",
        "2. offline → 走本地模式（不调 flash；需要生成能力的步骤按第六部分降级）。",
        "3. 写操作前的最后一步永远是：读 `state/.write_token` 是否存在、是否被 git 忽略。",
        "",
        "## 三、主流程",
        "",
    ]
    lines += [f"{index}. {step}" for index, step in enumerate(entry.flow, start=1)]
    lines += [
        "",
        "## 四、人类确认点",
        "",
        f"- **{entry.confirmation}**",
        "- 请示一律用 0.5.B 模板：`【需要你批准的操作】【干了什么】【验证结果】【风险】【如何回滚】`。",
        "- **一次只问一件事**；闸门只认恰好一个「是」字（写在审批单首行，或对话里只回一个字）。",
        "",
        "## 五、失败出口",
        "",
        ("任一步失败 → 按 TEST_GATE 输出 BLOCKED 报告（原始日志关键段 + 根因 + 已尝试的修复 + 下一步建议）"
        "并**停止**，不再自行往下走；需要人类介入的内容写进报告开头三行。"),
        "",
        "---",
        "",
        ("机器可读的指令定义见 `src/skills/registry.py` 的 `COMMANDS`（本文件由 "
        "`tools/build_skills.py` 从它渲染，改指令请改表再重建）。"),
        "",
    ]
    return "\n".join(lines)


def render_agents() -> str:
    """渲染 `AGENTS.md`：路由表 + 两条兜底 + 一致性约束（路线 7.2 第 3 条）。"""
    lines = [
        "# AGENTS.md —— 指令路由表",
        "",
        "> 本文件由 `tools/build_skills.py` 从 `src/skills/registry.py` 渲染，**不要手改**：",
        "> 手改会在 `/体检` 的一致性校验里报错（路线 7.4 第 1 条）。",
        "",
        "## 一、指令 → SKILL.md",
        "",
        "| 指令 | 动作 | 人类确认点 | SKILL.md |",
        "|---|---|---|---|",
    ]
    for entry in COMMANDS:
        lines.append(
            f"| `{entry.usage}` | {entry.action} | {entry.confirmation} | `skills/{entry.slug}.md` |"
        )
    lines += [
        "",
        "## 二、兜底通道（人类记不清指令名时）",
        "",
        (
            "1. **自然语言兜底**：若用户描述仓库自动化系统异常/卡住/报错/坏掉，"
            "一律视为触发 `/应急`，执行 `skills/emergency.md`。"
        ),
        ("2. **手动兜底**：若对话唤起本身失效，在终端直接跑 "
        f"`{CLI_ENTRY}`（CLI 是绕开 AI 对话层的最后通道）。"),
        "",
        "## 三、一致性与纪律",
        "",
        ("- 指令清单（路线 7.1）、本文件、`skills/` 三者必须一一对应；"
        "新增/删除/改名一律先改 `src/skills/registry.py` 再重建。"),
        "- 每条指令的「人类确认点」不允许为空（纯只读写「无」）。",
        "- 任何模块改动导致指令行为变化时，同步改对应 SKILL.md，并在 `/体检` 中验证指令仍可达。",
        ("- 主入口是对话指令；插件（阶段七 7.3）是**可选增强**，两条入口共享同一套 SKILL.md，"
        "不允许出现逻辑分叉。"),
        "",
    ]
    return "\n".join(lines)


# ------------------------------------------------------------------ 校验

def _strip_section_number(title: str) -> str:
    """去掉标题里的序号前缀：`一、TEST_GATE（…）` → `TEST_GATE（…）`。

    （第一版忘了这一步，于是"五段齐全"永远校验失败 —— 校验器本身也要被校验。）
    """
    return re.sub(r"^(?:[一二三四五六七八九十]+[、.]|\d+[、.])\s*", "", title).strip()


def parse_skill(text: str) -> dict[str, str]:
    """把 SKILL.md 拆成五段（按 `## ` 标题切）。缺段在 `validate_skill` 里报。"""
    sections: dict[str, str] = {}
    current = ""
    buffer: list[str] = []
    for line in text.splitlines():
        if line.startswith("## "):
            if current:
                sections[current] = "\n".join(buffer).strip()
            current = _strip_section_number(line[3:].strip())
            buffer = []
        elif current:
            buffer.append(line)
    if current:
        sections[current] = "\n".join(buffer).strip()
    return sections


REQUIRED_SECTIONS = ("TEST_GATE", "前置", "主流程", "人类确认点", "失败出口")

#: 允许存在、但不对应指令的辅助文档（`TEST_GATE.md` 是全文单一来源）
AUXILIARY_SKILLS = frozenset({"TEST_GATE"})


def validate_skill(entry: Command, text: str) -> list[str]:
    """校验一份 SKILL.md：五段齐全、TEST_GATE 逐字一致、确认点非空、主流程有步骤。"""
    problems: list[str] = []
    sections = parse_skill(text)
    if len(sections) != len(REQUIRED_SECTIONS):
        problems.append(f"段数不对：{len(sections)}（应为 {len(REQUIRED_SECTIONS)}）")
    for required in REQUIRED_SECTIONS:
        match = next((key for key in sections if key.startswith(required)), None)
        if match is None:
            problems.append(f"缺少「{required}」段")
        elif required in ("主流程", "人类确认点") and len(sections[match]) < 20:
            problems.append(f"「{required}」段几乎是空的")
    gate = next((value for key, value in sections.items() if key.startswith("TEST_GATE")), "")
    if TEST_GATE_TEXT not in gate:
        problems.append("TEST_GATE 与路线 0.2 不一致（必须原样嵌入）")
    if entry.confirmation.strip() in ("", "待定"):
        problems.append("人类确认点不能为空（纯只读写「无」）")
    if len(entry.flow) < 3:
        problems.append("主流程至少要有 3 步")
    return problems


def check_consistency(
    skills_dir: Path | None = None, agents_path: Path | None = None
) -> dict[str, Any]:
    """
    路线 7.4 第 1 条：指令清单、`AGENTS.md`、`skills/` 三者一一对应。

    返回 `{"ok": bool, "problems": [...], "checked": n}` —— 不抛异常，
    让 `/体检` 能把问题一次性列全。
    """
    directory = skills_dir or SKILLS_DIR
    agents = agents_path or AGENTS_PATH
    problems: list[str] = []

    for entry in COMMANDS:
        path = directory / f"{entry.slug}.md"
        if not path.is_file():
            problems.append(f"缺少 skills/{entry.slug}.md（{entry.name}）")
            continue
        problems += [f"skills/{entry.slug}.md：{item}" for item in validate_skill(entry, path.read_text(encoding="utf-8"))]

    present = {path.stem for path in directory.glob("*.md")} if directory.is_dir() else set()
    known = {entry.slug for entry in COMMANDS}
    for extra in sorted(present - known - AUXILIARY_SKILLS):
        problems.append(f"skills/{extra}.md 没有对应指令（多余的文件会让人以为是入口）")

    if not agents.is_file():
        problems.append("缺少 AGENTS.md（路由表）")
    else:
        text = agents.read_text(encoding="utf-8")
        for entry in COMMANDS:
            if f"`{entry.usage}`" not in text:
                problems.append(f"AGENTS.md 里没有 `{entry.usage}`")
            if f"skills/{entry.slug}.md" not in text:
                problems.append(f"AGENTS.md 没有把 `{entry.name}` 指向 skills/{entry.slug}.md")
        if CLI_ENTRY not in text:
            problems.append("AGENTS.md 缺少手动兜底（CLI 入口）")
        if "/应急" not in text:
            problems.append("AGENTS.md 缺少自然语言兜底（→ /应急）")

    return {"ok": not problems, "problems": problems, "checked": len(COMMANDS)}


def route(text: str | None) -> Command | None:
    """
    自然语言兜底（路线 7.2 第 3 条）：记不清指令名时也能把系统叫回来。

    命中"异常/卡住/报错"这类词 → `/应急`；否则返回 `None`（由调用方按原话理解）。
    """
    if not text:
        return None
    lowered = text.lower()
    if any(hint.lower() in lowered for hint in EMERGENCY_HINTS):
        return commands_by_name()["/应急"]
    return None


def load_all(skills_dir: Path | None = None) -> dict[str, tuple[Command, str]]:
    """把 8 份 SKILL.md 读进来（`{slug: (command, text)}`）。缺件即报错。"""
    directory = skills_dir or SKILLS_DIR
    loaded: dict[str, tuple[Command, str]] = {}
    for entry in COMMANDS:
        path = directory / f"{entry.slug}.md"
        if not path.is_file():
            raise SkillError(f"缺少 {path}")
        loaded[entry.slug] = (entry, path.read_text(encoding="utf-8"))
    return loaded


def write_all(skills_dir: Path | None = None, agents_path: Path | None = None) -> list[Path]:
    """按注册表**重建** `skills/*.md` 与 `AGENTS.md`（幂等）。"""
    directory = skills_dir or SKILLS_DIR
    directory.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    gate_file = directory / "TEST_GATE.md"
    gate_file.write_text(f"# TEST_GATE（路线 0.2 全文，原样）\n\n```\n{TEST_GATE_TEXT}\n```\n", encoding="utf-8")
    written.append(gate_file)
    for entry in COMMANDS:
        path = directory / f"{entry.slug}.md"
        path.write_text(render_skill(entry), encoding="utf-8")
        written.append(path)
    agents = agents_path or AGENTS_PATH
    agents.write_text(render_agents(), encoding="utf-8")
    written.append(agents)
    return written


def as_json() -> str:
    return json.dumps(
        [
            {
                "command": entry.usage,
                "slug": entry.slug,
                "action": entry.action,
                "confirmation": entry.confirmation,
                "stage": entry.stage,
                "writes": entry.writes,
            }
            for entry in COMMANDS
        ],
        ensure_ascii=False,
        indent=2,
    )


def slugs() -> Sequence[str]:
    return [entry.slug for entry in COMMANDS]


def skill_path(slug: str) -> Path:
    if slug not in {entry.slug for entry in COMMANDS}:
        raise SkillError(f"没有这个技能：{slug}；可用：{', '.join(slugs())}")
    return SKILLS_DIR / f"{slug}.md"


MENTION_RE = re.compile(r"`?/([\u4e00-\u9fffA-Za-z]+)")


def mentioned_commands(text: str) -> list[Command]:
    """从一段话里找出被提到的指令（用于路由表自测与人工复核）。"""
    found = []
    for match in MENTION_RE.finditer(text or ""):
        candidate = f"/{match.group(1)}"
        entry = commands_by_name().get(candidate)
        if entry and entry not in found:
            found.append(entry)
    return found
