"""6.1 半自动建新仓库：**一句 idea → 骨架 → 本地 CI 干跑 → 人类两次确认 → 建仓 + 首推 + 验 CI**。

路线原文：

> 1. 读 spec → flash 生成：项目骨架 + 单测 + README + LICENSE（默认 MIT）+ CI 配置
>    （含 8.4 门禁工作流）+ `.gitignore`；
> 2. 仓库名查重：search API 检查，备 3 个候选名给人类选；
> 3. 沙箱全量测试 + CI 配置本地 dry-run 通过；
> 4. 摘要过闸门 → `create_repository` → 首 push → 验证云端 CI 绿。

## 人类只需要出现两次，且两次都是**决策**不是**操作**

1. **选名字**（三选一）——这是产品决策，不是配置操作；
2. **批准建仓**——建仓是写操作，闸门管着。

其余全部自动：生成、写出、本地测试、CI 干跑、建仓、首推、轮询 CI 结果。
"运行期还需要人类动手配置，说明构建期没收尾"（路线 0.5）——所以这里一条配置步骤都不该有。

## 生成物必须**先能跑**再谈上传

`dry_run()` 会在生成目录里跑**和 CI 完全相同的命令**（pytest / ruff / 黑名单检查）。
本地跑不过就不许往上传：一个新仓库第一次 CI 就红，是最贵的失败
（它会让后面所有"CI 绿"的说法都失去意义）。
"""

from __future__ import annotations

import dataclasses
import json
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
SCAFFOLD_DIR = ROOT / "state" / "scaffold"

#: 生成物里**必须**有的东西（少一样就不算骨架）。用正则匹配路径。
REQUIRED_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"^pyproject\.toml$|^setup\.py$", "打包配置（pyproject.toml 或 setup.py）"),
    (r"^README\.md$", "README"),
    (r"^LICENSE(\.md|\.txt)?$", "LICENSE（默认 MIT）"),
    (r"^\.gitignore$", ".gitignore"),
    (r"^\.github/workflows/[^/]+\.ya?ml$", "CI 工作流（路线 8.4 的门禁）"),
    (
        r"^scripts/check_blacklist\.py$",
        "scripts/check_blacklist.py（CI 工作流里会跑它；不给它，那条门禁在云端必红）",
    ),
    (r"^(?!tests/)[\w./-]+\.py$", "至少一个包内模块"),
    (r"^tests/test_[\w-]+\.py$", "至少一个测试文件"),
)

#: 生成物的体量上限：防止模型"顺手写一个完整框架"回来
MAX_FILES = 40
MAX_TOTAL_BYTES = 200_000

GENERATE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "summary": {"type": "string"},
        "rationale": {"type": "string"},
        "files": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                "required": ["path", "content"],
            },
        },
    },
    "required": ["name", "summary", "files", "rationale"],
}

SYSTEM_PROMPT = """你在为一句 idea 生成一个**最小可用**的 Python 项目骨架。

硬性要求：
1. 只输出 JSON：{name, summary, rationale, files: [{path, content}]}，path 用相对路径（POSIX 风格）。
2. **必须**包含：pyproject.toml、README.md、LICENSE（MIT 全文）、.gitignore、
   .github/workflows/ci.yml、至少一个包内模块、至少一个 tests/test_*.py。
3. CI 工作流要跑：`python -m pytest -q`、`python -m ruff check .`，
   以及（若存在）`python scripts/check_blacklist.py`；Python 版本矩阵 3.10 与 3.12。
4. 代码要**真的能跑**：导入路径、测试断言、依赖声明三者必须自洽。
   宁可少写功能，也不要写跑不通的代码。
5. 单测至少覆盖：模块能导入 + 一个真实行为的正例 + 一个边界/反例。
6. 不要输出大段无关文档；README 讲清"这是什么、怎么装、怎么跑测试"即可。
7. LICENSE 用 MIT 标准全文，作者写 "repo-autopilot"。
8. pyproject.toml 里必须写清测试怎么找包，否则 CI 会 import 失败：
   - 包在 `src/<包名>/` → `[tool.pytest.ini_options]` 里 `pythonpath = ["src"]`
   - 包在仓库根目录 → `pythonpath = ["."]`
   同时写一个 `[tool.ruff]` 段把规则集钉死：`line-length = 100`，
   `[tool.ruff.lint]` 里 `select = ["E4", "E7", "E9", "F"]`（ruff 默认集）。
9. **不要使用 pytest 的 `tmp_path` / `tmpdir`、`tempfile`、系统临时目录**：
   运行环境（受限沙箱）拒绝访问系统 temp，用了必然 ERROR。
   需要落盘就在项目内建 `.scratch/` 并在用例结束时删掉；能写成纯函数测试就写纯函数。
10. 如果 idea 涉及"规则/配置由外部提供"，**必须**同时生成 `scripts/check_blacklist.py`：
   CI 工作流会跑它，缺了它云端 CI 直接红。该脚本只扫**包源码目录**
   （`src/<包>/` 或包目录）里的 `.py`，用于确认"规则内容没有被写回脚本"。
   它是**字面量扫描**（不看语法结构，注释与文档字符串一起扫），所以：
   - 包源码的**文档字符串/注释里不要写完整的规则公式**（如 `(STR - 10) // 2`）。
     示例请用**中性且不成公式**的形式，例如 `derive("(X - 10) // 2")` 这种
     会命中运算符的写法也要避免——直接写"用外部配置里的表达式算派生值"这类描述；
   - 规则的真实公式与属性名一律放外部配置（如 `rules/example_rules.json`）；
   - 判据要真正跑一遍：`python scripts/check_blacklist.py` 在本地必须 exit 0。
"""

#: 名字里不许出现的词（GitHub 保留 / 容易撞车）
RESERVED_NAMES = frozenset(
    {"test", "src", "lib", "app", "main", "python", "repo", "project", "none", "null"}
)

#: 池子不够时用来派生候选名的**角色后缀**（2026-09-17 总报告 P1-14）。
#: 都是通用角色词，不编造领域含义：`trpg` → `trpg` / `trpg-tool` / `trpg-engine`。
#: 顺序有意义：第一个与旧的 `pool[1]-tool` 那条重复，派生时跳过。
NAME_ROLE_SUFFIXES = ("tool", "engine", "kit", "cli")


class ScaffoldError(RuntimeError):
    """脚手架自身的错误（生成物缺件、路径非法、名字不可用）。绝不静默上传半成品。"""


@dataclasses.dataclass(frozen=True)
class GeneratedFile:
    path: str
    content: str


@dataclasses.dataclass
class Scaffold:
    idea: str
    name: str
    summary: str
    rationale: str
    files: list[GeneratedFile]

    def as_dict(self) -> dict[str, Any]:
        return {
            "idea": self.idea,
            "name": self.name,
            "summary": self.summary,
            "rationale": self.rationale,
            "files": [{"path": item.path, "bytes": len(item.content.encode("utf-8"))} for item in self.files],
        }

    @property
    def total_bytes(self) -> int:
        return sum(len(item.content.encode("utf-8")) for item in self.files)


# ------------------------------------------------------------------ 名字

def slugify(text: str) -> str:
    """英文小写 + 连字符。中文等非 ASCII 直接丢掉（GitHub 仓库名不支持）。"""
    ascii_only = re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-").lower()
    return re.sub(r"-{2,}", "-", ascii_only)


def name_candidates(idea: str, *, count: int = 3, keywords: Sequence[str] = ()) -> list[str]:
    """
    从 idea 生成候选仓库名。**不用模型**：名字要能解释得清是怎么来的，
    而且这一步不该花一次调用（真正的决策由人选，不是由模型猜）。

    做法：取 idea 里的英文词 + 调用方给的补充关键词，拼出 `词-词` 形态的短名。

    **2026-09-17 总报告 P1-14**：中文 idea 里抽不出英文词（正则只认 `[A-Za-z]`），
    没有 `keywords` 时池子只剩一个词，于是"三选一"只给出**一个**候选
    （实测那份中文 idea 只出 `trpg`）—— 而承诺是三选一。两个补救：
      ① 池子不够时，用**角色后缀**把同一个词派生成几个**真实可选**的名字
         （`trpg` / `trpg-tool` / `trpg-engine`）——比"只给一个然后装作三选一"诚实；
      ② 仍然不够就在 `name_candidate_notes()` 里**明说**要补 keywords，并提示裸名撞 PyPI 的风险。
    注意 ① 不是编造：后缀是通用角色词，且**候选最终由人选/人改**（`choose_name` 允许自己给名字）。
    """
    words = [word for word in re.findall(r"[A-Za-z][A-Za-z0-9]+", idea or "") if len(word) > 2]
    pool: list[str] = []
    for word in [slugify(item) for item in (list(words) + list(keywords))]:
        if word and word not in RESERVED_NAMES and word not in pool:
            pool.append(word)          # 去重：否则会拼出 markdown-markdown 这种名字
    candidates: list[str] = []
    if len(pool) >= 2:
        candidates.append(f"{pool[0]}-{pool[1]}")
        candidates.append(f"{pool[1]}-{pool[0]}")
    if len(pool) >= 3:
        candidates.append(f"{pool[0]}-{pool[1]}-{pool[2]}")
    if pool:
        candidates.append(pool[0])
        # 池子不够（典型：中文 idea 且没给 keywords）→ 用角色后缀派生，凑够"三选一"
        for suffix in NAME_ROLE_SUFFIXES:
            if len(pool) >= 2 and suffix == NAME_ROLE_SUFFIXES[0]:
                continue               # 上面已经拼过 `pool[1]-tool` 了
            candidates.append(f"{pool[0]}-{suffix}")
    if len(pool) >= 2:
        candidates.append(f"{pool[1]}-tool")
    seen: list[str] = []
    for name in candidates:
        if name not in seen and len(name) >= 3 and name not in RESERVED_NAMES:
            seen.append(name)
    return seen[:count]


def name_candidate_notes(
    idea: str, candidates: Sequence[str], *, count: int = 3, keywords: Sequence[str] = ()
) -> list[str]:
    """
    候选名的**诚实提示**（2026-09-17 总报告 P1-14）：该说的两条不许沉默。

    * 候选**不足承诺的数量**时：说清原因（idea 里没有足够的英文词）并给出补救
      （补 `keywords`，或自己给一个名字）——而不是安静地只给一个；
    * 候选里出现**裸词**（`trpg` 这种单词名）时：提示它与 PyPI/常见包**同名撞车**的风险
      （生成的项目要 `import` 自己的包名，撞名会直接 import 到别人的包）。
    """
    notes: list[str] = []
    if len(candidates) < count:
        words = [word for word in re.findall(r"[A-Za-z][A-Za-z0-9]+", idea or "") if len(word) > 2]
        notes.append(
            f"候选名只有 {len(candidates)} 个（承诺 {count} 个）："
            + ("idea 里没有足够的英文词" if len(words) < 2 else "可用词都被保留名挡住了")
            + "。补救：调用时补 `keywords=[...]`（领域英文词），或直接自己给一个 slug。"
        )
    bare = [name for name in candidates if "-" not in name]
    if bare:
        notes.append(
            "候选里有**裸词**（" + "、".join(bare) + "）："
            "生成的项目要 `import` 自己的包名，裸词与 PyPI/常见包同名会直接 import 到别人的包。"
            "建议选带后缀的名字，或先查一下 PyPI。"
        )
    return notes


def is_name_available(client: Any, owner: str, name: str) -> bool:
    """
    仓库名可用 = `GET /repos/{owner}/{name}` 是 404。其他错误一律当"不可用"（保守）。

    **实测过的语义（2026-09-17，总报告 R6 由此关闭）**：

    | 情形 | 返回 |
    |---|---|
    | 仓库已存在 | `False` ✓ |
    | owner 存在、名字没被占用 | `True` ✓ |
    | **owner 本身不存在** | `True` ⚠️ **假"可用"** |

    第三种是 GitHub 的行为：不存在的 owner 也回 404，于是这里会误报"可用"，
    而真正去建库时会 404 失败。所以**调用方必须先确认 owner 存在**
    （`owner_exists()`），别把这个函数的 `True` 当成"能建"。
    """
    try:
        client.get(f"/repos/{owner}/{name}")
    except Exception as exc:  # noqa: BLE001
        return "404" in str(exc) or "Not Found" in str(exc)
    return False


def owner_exists(client: Any, owner: str) -> bool:
    """
    `owner`（用户或组织）是否存在。

    为什么要单独查一次：`is_name_available()` 对**不存在的 owner** 也会返回 `True`
    （GitHub 对两者都给 404）—— 于是一个打错的 `--owner` 会让所有候选名显示"可用"，
    直到建库那一刻才失败。这一步把"名字可用"与"这块地存在"分开。
    """
    try:
        client.get(f"/users/{owner}")
        return True
    except Exception as exc:  # noqa: BLE001
        return not ("404" in str(exc) or "Not Found" in str(exc))


def check_names(client: Any, owner: str, candidates: Sequence[str]) -> list[dict[str, Any]]:
    return [
        {"name": name, "available": is_name_available(client, owner, name)} for name in candidates
    ]


def choose_name(reply: str | None, candidates: Sequence[str]) -> str:
    """
    人类选名：候选名、序号，或者**自己给一个合法 slug**。

    为什么允许自定义：候选名只是"替他想好三个"，他想自己起是更常见的真实情况。
    但仍然**只接受合法 slug**（小写字母数字与连字符、长度 ≥3）—— 名字要写进 URL，
    猜错比问第二次贵。
    """
    if not candidates:
        raise ScaffoldError("没有候选名可选")
    if not reply:
        raise ScaffoldError("人类还没选名字（请回复候选名、序号 1/2/3，或自己的名字）")
    text = reply.strip()
    if text in candidates:
        return text
    if text.isdigit() and 1 <= int(text) <= len(candidates):
        return candidates[int(text) - 1]
    slug = slugify(text)
    if len(slug) >= 3 and slug not in RESERVED_NAMES:
        return slug
    raise ScaffoldError(f"回复 {reply!r} 既不是候选名/序号，也不是合法仓库名；候选：{list(candidates)}")


# ------------------------------------------------------------------ 校验与落盘

def validate_files(files: Sequence[GeneratedFile]) -> list[str]:
    """返回**缺失/非法项**的清单（空列表 = 通过）。"""
    problems: list[str] = []
    if not files:
        return ["什么都没生成"]
    if len(files) > MAX_FILES:
        problems.append(f"文件数 {len(files)} > {MAX_FILES}")
    total = sum(len(item.content.encode("utf-8")) for item in files)
    if total > MAX_TOTAL_BYTES:
        problems.append(f"总体积 {total} 字节 > {MAX_TOTAL_BYTES}")

    paths = [item.path for item in files]
    for path in paths:
        if path.startswith("/") or "\\" in path or ".." in Path(path).parts:
            problems.append(f"非法路径：{path}")
    for pattern, label in REQUIRED_PATTERNS:
        if not any(re.search(pattern, path, re.MULTILINE) for path in paths):
            problems.append(f"缺少{label}")

    license_file = next((item for item in files if re.search(r"^LICENSE(\.md|\.txt)?$", item.path)), None)
    if license_file and "MIT License" not in license_file.content:
        problems.append("LICENSE 不是 MIT 全文（路线默认 MIT）")
    test_file = next((item for item in files if re.search(r"^tests/test_[\w-]+\.py$", item.path)), None)
    if test_file and "def test_" not in test_file.content:
        problems.append("测试文件里没有 test_ 用例")

    # ---- 下面三条是**环境约束**，不是风格偏好。它们都由实测换来：
    # 生成物本地就跑不起来，上传后第一次 CI 必红（而"第一次 CI 就红"是最贵的失败）。
    pyproject = next((item for item in files if item.path == "pyproject.toml"), None)
    if pyproject:
        text = pyproject.content
        if "[tool.pytest.ini_options]" not in text or "pythonpath" not in text:
            problems.append(
                "pyproject.toml 缺少 [tool.pytest.ini_options] 的 pythonpath —— "
                "CI 会因为 import 不到包而失败"
            )
        if "[tool.ruff" not in text:
            problems.append(
                "pyproject.toml 缺少 [tool.ruff] 配置 —— 规则集不钉死的话，"
                "CI 的 ruff 结果会随版本漂移"
            )
    offenders = [
        item.path
        for item in files
        if item.path.startswith("tests/")
        and re.search(r"\btmp_path\b|\btmpdir\b|\btempfile\b|gettempdir", item.content)
    ]
    if offenders:
        problems.append(
            f"{'、'.join(offenders)} 用了系统临时目录（tmp_path/tempfile）—— "
            "受限沙箱拒绝访问系统 temp，用例会直接 ERROR；请改成项目内 .scratch/ 或纯函数测试"
        )
    return problems


def write_scaffold(scaffold: Scaffold, root: Path | None = None) -> Path:
    """把生成物写到磁盘（默认 `state/scaffold/<name>/`）。先清空目标目录，避免残留混进去。"""
    destination = (root or SCAFFOLD_DIR) / scaffold.name
    if destination.exists():
        shutil.rmtree(destination, ignore_errors=True)
    for item in scaffold.files:
        target = destination / item.path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(item.content, encoding="utf-8")
    return destination


# ------------------------------------------------------------------ 生成

def default_chat(messages: list[dict[str, str]], schema: dict[str, Any]) -> Any:
    from ..gateway import chat

    return chat(messages, schema, "flash_api", temperature=0.0)


def build_prompt(idea: str, *, name: str = "", extra: str = "") -> list[dict[str, str]]:
    hint = f"\n仓库名已定：{name}（包名用它的下划线形式）。" if name else ""
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": f"idea：{idea.strip()}{hint}"},
    ]
    if extra:
        messages.append({"role": "user", "content": extra})
    return messages


def generate(
    idea: str,
    *,
    name: str = "",
    chat_fn: Callable[[list[dict[str, str]], dict[str, Any]], Any] | None = None,
    max_attempts: int = 2,
    extra_context: str = "",
) -> Scaffold:
    """
    让 flash 生成骨架，并**当场校验**；缺件就带着缺件清单再要一次。

    为什么重试一次：模型漏 `.gitignore` 或漏测试是常见形态，而这两样恰恰是
    "CI 干跑"能不能成立的前提。带清单重试一次的代价远小于"生成一个跑不起来的仓库"。
    """
    chat_fn = chat_fn or default_chat
    problems: list[str] = []
    for attempt in range(1, max_attempts + 1):
        # **重试也要带着 `extra_context`**（2026-09-17 总报告 P1-13）：
        # 原来写成 `extra_context if attempt == 1 else ""` —— 第 2 次生成时把它丢掉了，
        # 于是模型"只补缺件"时**看不到调用方给的上下文**（例如 spec 摘要 / 暂存卡路径），
        # 补出来的东西可能与需求脱节。缺件清单是**追加**在后面的，不需要拿掉前面的上下文。
        messages = build_prompt(idea, name=name, extra=extra_context)
        if problems:
            messages.append(
                {
                    "role": "user",
                    "content": "上一次生成物的问题（请只补这些问题，不要重写全部）：\n- "
                    + "\n- ".join(problems),
                }
            )
        payload = chat_fn(messages, GENERATE_SCHEMA)
        if not isinstance(payload, dict):
            raise ScaffoldError(f"生成返回的不是对象：{type(payload).__name__}")
        files = [
            GeneratedFile(str(item.get("path")), str(item.get("content")))
            for item in (payload.get("files") or [])
            if isinstance(item, dict) and item.get("path") and item.get("content") is not None
        ]
        problems = validate_files(files)
        if not problems:
            return Scaffold(
                idea=idea,
                name=str(payload.get("name") or name or "new-project"),
                summary=str(payload.get("summary") or ""),
                rationale=str(payload.get("rationale") or ""),
                files=files,
            )
    raise ScaffoldError("生成物始终缺件：" + "；".join(problems))


def generate_until_green(
    idea: str,
    *,
    name: str = "",
    chat_fn: Callable[[list[dict[str, str]], dict[str, Any]], Any] | None = None,
    runner: Callable[[Sequence[str], Path], tuple[int, str]] | None = None,
    python: str | None = None,
    root: Path | None = None,
    max_rounds: int = 3,
    extra_context: str = "",
) -> tuple[Scaffold, dict[str, Any], Path]:
    """
    生成 → 本地 CI 干跑 → **不过就把失败输出喂回去重来**。

    这是这套流程里最有价值的一步：结构缺件（`validate_files`）能挡掉"少文件"，
    但挡不住"代码 import 不到包""ruff 报 F401""用了系统 temp 目录"这类**只有跑一遍才知道**的问题。
    与其上传后在云端 CI 上发现，不如本地跑绿再走 —— 新仓库第一次 CI 就红是最贵的失败。

    **`extra_context` 会贯穿所有重试轮**（2026-09-17 总报告 P1-13）：调用方通常在这里放
    spec 摘要/需求要点，而第 1 轮之后模型是"只修失败"的 —— 如果这时候把它丢掉，
    模型就**看不到需求**，修出来的东西可能对得上 CI、对不上 spec。
    实现上：基础上下文每轮都带，失败清单从第 2 轮起追加在后面。

    返回 `(scaffold, dry_run 结果, 目录)`；`max_rounds` 轮都不过就抛错（不带着红的骨架往下走）。
    """
    base = (extra_context or "").strip()
    last: dict[str, Any] = {"passed": False, "results": []}
    for round_index in range(1, max_rounds + 1):
        failures = [
            f"`{item['command']}` → exit {item['exit']}\n{item['tail'][-900:]}"
            for item in last["results"]
            if not item["ok"]
        ]
        context = base
        if failures:
            note = (
                f"这是第 {round_index} 次生成，但**本地 CI 跑出来是红的**。"
                "请只修下面这些实际问题（不要重写全部文件），"
                "并保持上面的需求/约束不变：\n\n" + "\n\n".join(failures)
            )
            context = f"{base}\n\n{note}" if base else note
        scaffold = generate(idea, name=name, chat_fn=chat_fn, extra_context=context)
        directory = write_scaffold(scaffold, root)
        last = dry_run(directory, runner=runner, python=python)
        if last["passed"]:
            return scaffold, last, directory
    raise ScaffoldError(
        "本地 CI 始终不过（" + f"{max_rounds} 轮" + "）："
        + "；".join(item["command"] for item in last["results"] if not item["ok"])
    )


# ------------------------------------------------------------------ 本地 CI 干跑

def ci_commands(python: str | None = None, root: Path | None = None) -> list[list[str]]:
    """
    **与生成的 CI 工作流一致**的命令（本地跑的就是云端要跑的）。

    2026-09-17 实测（第六轮交接观察台账 A7）：本函数以前只返回 `pytest` + `ruff` 两条，
    而生成的 `.github/workflows/ci.yml` **还跑 `scripts/check_blacklist.py`**。
    后果是"本地门禁绿、首推后云端红"——首个真实生成的仓库
    （`AristotleAsborg/trpg-values-engine`）就是这样红在第一推上的，
    而 `SYSTEM_PROMPT` 里那句"与 CI 完全相同的命令"没被守住。

    现在把黑名单那一步也纳入本地干跑。它是**条件命令**（脚本不存在就跳过），
    这与 CI 工作流里的 `if [ -f scripts/check_blacklist.py ]` 是同一语义；
    同时兼容不生成该脚本的骨架（既有测试用的手写骨架就没有它）。
    """
    executable = python or sys.executable
    commands = [[executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"]]
    if root is not None and (Path(root) / "scripts" / "check_blacklist.py").is_file():
        commands.append([executable, "scripts/check_blacklist.py"])
    commands.append([executable, "-m", "ruff", "check", "."])
    return commands


def default_runner(command: Sequence[str], cwd: Path) -> tuple[int, str]:
    outcome = subprocess.run(
        list(command),
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=900,
        check=False,
    )
    return outcome.returncode, (outcome.stdout or "") + (outcome.stderr or "")


def dry_run(
    root: Path,
    *,
    runner: Callable[[Sequence[str], Path], tuple[int, str]] | None = None,
    python: str | None = None,
) -> dict[str, Any]:
    """
    在生成目录里跑一遍 CI 命令。任一红 → `passed=False`，并带上输出尾部。

    跑之前**先检查文件齐不齐**：缺件的项目会让 pytest "0 items collected"，
    那种绿是假绿（什么都没测）。
    """
    runner = runner or default_runner
    results: list[dict[str, Any]] = []
    passed = True
    for command in ci_commands(python, root):
        code, output = runner(command, root)
        ok = code == 0
        passed = passed and ok
        results.append(
            {"command": " ".join(command), "exit": code, "ok": ok, "tail": output.strip()[-1500:]}
        )
    return {"passed": passed, "results": results, "root": str(root)}


# ------------------------------------------------------------------ 本地保存（GitHub 可选）

def init_local_repository(
    directory: Path,
    *,
    message: str = "chore: 由 repo-autopilot 生成的初始骨架",
    branch: str = "main",
) -> dict[str, Any]:
    """
    **把生成的项目保存在本地**：`git init` + `git add` + `git commit`，不碰网络、不用任何 token。

    **为什么要有这条**（2026-09-18）：路线 6.1 的默认路径是"生成骨架 → 建 GitHub 仓库 → 首推 → 轮询云端 CI"，
    于是"想用这套东西"就等于"必须交出 GitHub 写权限"。而这条链路里真正有价值的部分
    （追问 → spec → 骨架 → **本地 CI 干跑**）**根本不需要联网**。
    所以 GitHub 应当是可选项：默认可以只存在本地，想要远端时再显式开。

    幂等（本系统对"事件驱动"的要求就是幂等 + 可从 `state/` 冷启动）：
      * 已经是 git 仓库 → 不再 `init`；
      * 没有新改动 → **不报错**，如实返回 `committed=False`（"没什么可提交的"不是失败）。

    返回 `{"path", "branch", "commit", "committed", "note"}`。
    `git` 不可用时**响亮报错**、不静默降级 —— 用户以为存下来了、其实没有，比直接失败糟得多。
    """
    if not Path(directory).is_dir():
        raise ScaffoldError(f"本地保存失败：目录不存在 {directory}")
    root = Path(directory)

    def git(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *args], cwd=str(root), capture_output=True, text=True,
            encoding="utf-8", errors="replace", check=False,
        )

    if git("rev-parse", "--is-inside-work-tree").returncode != 0:
        if not shutil.which("git"):
            raise ScaffoldError("本地保存失败：这台机器上没有可用的 `git` —— 装好 git 再试，或改用远端模式")
        created = git("init", "-b", branch)
        if created.returncode != 0:
            raise ScaffoldError(f"git init 失败：{(created.stderr or created.stdout).strip()[:200]}")
    else:
        # **"在某个仓库里"不等于"这个目录就是仓库根"**（实测踩过）：目标目录常常落在
        # 另一个仓库的工作树内（例如生成到 `.cache/` 下），这时 `--is-inside-work-tree` 也是真，
        # 于是我们会去动**外层仓库**的索引与 HEAD —— 那是别人的仓库，后果很脏。
        # 判据改成"这个目录是不是它自己的根"：不是就 `git init` 建一个**嵌套**仓库（这正是我们要的语义）。
        toplevel = git("rev-parse", "--show-toplevel")
        is_own_root = (
            toplevel.returncode == 0
            and (toplevel.stdout or "").strip()
            and Path((toplevel.stdout or "").strip()).resolve() == root.resolve()
        )
        if not is_own_root:
            created = git("init", "-b", branch)
            if created.returncode != 0:
                raise ScaffoldError(f"git init 失败：{(created.stderr or created.stdout).strip()[:200]}")

    git("add", "-A")
    if not (git("status", "--porcelain").stdout or "").strip():
        head = git("rev-parse", "--short", "HEAD")
        return {
            "path": str(root), "branch": branch,
            "commit": (head.stdout or "").strip() or None,
            "committed": False, "note": "没有新改动，仓库保持不变（幂等）",
        }
    commit = git(
        "-c", "user.name=repo-autopilot", "-c", "user.email=repo-autopilot@localhost",
        "commit", "-m", message,
    )
    if commit.returncode != 0:
        raise ScaffoldError(f"git commit 失败：{(commit.stderr or commit.stdout).strip()[:300]}")
    head = git("rev-parse", "--short", "HEAD")
    return {
        "path": str(root), "branch": branch,
        "commit": (head.stdout or "").strip() or None,
        "committed": True, "note": "已保存在本地（没有创建任何远端仓库）",
    }


# ------------------------------------------------------------------ 建仓与首推（远端，可选）

def create_repository(
    client: Any, name: str, *, description: str = "", private: bool = False, auto_init: bool = True
) -> dict[str, Any]:
    """
    建空仓库。`auto_init=True` 会带一个初始提交 —— **这是 contents API 能落文件的前提**
    （没有初始提交的分支，PUT contents 会 409/422）。
    """
    response = client.request(
        "POST",
        "/user/repos",
        body={"name": name, "description": description[:350], "private": private, "auto_init": auto_init},
    )
    if response.status >= 400:
        raise ScaffoldError(f"建仓失败：HTTP {response.status} {str(response.body)[:300]}")
    payload = response.body if isinstance(response.body, dict) else {}
    return {
        "full_name": payload.get("full_name"),
        "html_url": payload.get("html_url"),
        "default_branch": payload.get("default_branch") or "main",
    }


def first_push(
    client: Any,
    scaffold: Scaffold,
    *,
    full_name: str,
    default_branch: str = "main",
    message: str = "chore: 由 repo-autopilot 生成的初始骨架",
) -> dict[str, Any]:
    """
    首推：把生成物逐个文件提交到**默认分支**（复用 4.4 的 `push_changes`，
    它的"分支已存在就复用"正好适合这里）。
    """
    from ..publish import FileChange, push_changes

    changes = [FileChange(path=item.path, text=item.content) for item in scaffold.files]
    return push_changes(
        client,
        repo=full_name,
        branch=default_branch,
        base_branch=default_branch,
        changes=changes,
        message=message,
    )


def latest_ci(client: Any, full_name: str) -> dict[str, Any]:
    payload = client.get(f"/repos/{full_name}/actions/runs", params={"per_page": 1})
    runs = (payload or {}).get("workflow_runs") or []
    if not runs:
        return {"status": "missing", "conclusion": None, "url": None}
    run = runs[0]
    return {
        "status": run.get("status"),
        "conclusion": run.get("conclusion"),
        "url": run.get("html_url"),
        "created_at": run.get("created_at"),
    }


def wait_for_ci(
    client: Any,
    full_name: str,
    *,
    timeout_seconds: int = 600,
    interval_seconds: int = 20,
    sleep: Callable[[float], None] | None = None,
) -> dict[str, Any]:
    """
    轮询云端 CI 到出结论。**有超时**：没完就如实说"还在跑"，绝不把"没结论"说成"绿"。
    """
    sleep = sleep or time.sleep
    deadline = time.monotonic() + timeout_seconds
    last: dict[str, Any] = {"status": "unknown", "conclusion": None, "url": None}
    while time.monotonic() < deadline:
        last = latest_ci(client, full_name)
        if last["conclusion"]:
            return last
        sleep(interval_seconds)
    return {**last, "timed_out": True}


def summary_block(scaffold: Scaffold, dry: dict[str, Any], names: Sequence[dict[str, Any]]) -> str:
    """给人类看的两段式摘要：本地干跑结果 + 候选名（他只需要回一个序号）。"""
    lines = [
        f"- 生成 {len(scaffold.files)} 个文件，共 {scaffold.total_bytes} 字节",
        f"- 一句话：{scaffold.summary or '（模型没给摘要）'}",
        f"- 本地 CI 干跑：{'全部通过' if dry['passed'] else '**有红**'}",
    ]
    for item in dry["results"]:
        lines.append(f"  - `{item['command']}` → exit {item['exit']}")
    if not names:
        # P1-14：候选名一个都给不出来时要吼，不能安静地让人以为"没得选"
        lines.append("- **候选仓库名：一个都没生成出来** —— 请补 keywords 或直接给一个 slug")
    else:
        lines.append(f"- 候选仓库名（{len(names)} 个，请回序号）：")
        for index, item in enumerate(names, start=1):
            mark = "可用" if item["available"] else "**已被占用**"
            lines.append(f"  - {index}. `{item['name']}` —— {mark}")
    # **诚实标注覆盖缺口**（2026-09-17 总报告 P2-22）：本地干跑只跑当前解释器，
    # 而生成的 CI 是 3.10 + 3.12 矩阵。不说清楚的话，"本地绿"会被误读成"云端一定绿"。
    lines.append(
        f"- **已知覆盖缺口**：本地干跑用的是当前解释器 `{sys.version.split()[0]}`；"
        "生成的 CI 还跑 3.10 矩阵，本地覆盖不到（措辞上不要声称'与 CI 完全一致'）。"
    )
    return "\n".join(lines)


def now_stamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def write_report(scaffold: Scaffold, dry: dict[str, Any], root: Path | None = None) -> Path:
    destination = (root or ROOT / "state" / "reports") / f"scaffold-{scaffold.name}.md"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        "\n".join(
            [
                f"# 新仓库骨架：{scaffold.name}",
                "",
                f"- 来源 idea：{scaffold.idea}",
                f"- 摘要：{scaffold.summary}",
                f"- 为什么这么设计：{scaffold.rationale}",
                f"- 文件（{len(scaffold.files)} 个，{scaffold.total_bytes} 字节）：",
                *[f"  - `{item.path}`（{len(item.content.encode('utf-8'))} 字节）" for item in scaffold.files],
                "",
                f"- 本地 CI 干跑：{'通过' if dry['passed'] else '**未通过**'}",
                "",
                "```",
                json.dumps(dry, ensure_ascii=False, indent=2)[:4000],
                "```",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return destination
