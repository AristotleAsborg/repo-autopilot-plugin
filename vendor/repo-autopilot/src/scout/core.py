"""5.1 开源项目猎手：**搜索 → 硬过滤 → 看代码再打分 → 抓取**。

路线原文：

> 1. 关键词来自 spec/issue → GitHub 搜索 API；
> 2. 硬过滤：stars<50 或最后 commit >2 年 → 默认 skip（除非 flash 给出书面理由）；
> 3. flash 评分 schema：`{relevance, usage: copy|reference|dependency|skip, license_risk: ok|warn|block, reason}`
>    ——**评分前必须看 src/ 目录结构 + 核心文件摘要，禁止只读 README 打分**（README 夸大是经典坑）；
> 4. `usage≠skip 且 license_risk=ok` → clone 到 `state/vendor/{owner}_{repo}/`，记录 commit hash 与 LICENSE 副本；
> 5. 许可证红线：**GPL/AGPL 代码只参考思路，禁止拷进 MIT/Apache 目标仓库**。

## 这个模块的立场

- **硬过滤先于模型**：星数和"两年没动"是客观事实，不该花一次模型调用去判断；
  模型只回答"这个东西和我的需求有多相关、该怎么用"。
- **打分前必须看代码**：`describe_repo()` 会取目录树 + 核心文件开头（**不是全文**），
  并把"证据里有没有非 README 的代码"作为硬条件 —— 只有 README 就直接拒打分。
  这是路线里写死的一条，也是这类工具最容易偷懒的一步。
- **许可证不看模型脸色**：`license_risk()` 是纯函数查表。GPL/AGPL 一律 block，
  没有任何"理由充分就可以拷"的口子 —— 法律风险不该由模型评分来权衡。
"""

from __future__ import annotations

import dataclasses
import json
import re
import shutil
import sys
import urllib.request
from collections.abc import Callable, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
VENDOR_DIR = ROOT / "state" / "vendor"

#: 硬过滤线（路线写死的两个数）
MIN_STARS = 50
MAX_AGE_DAYS = 730          # 2 年

#: 破格通道的相关度门槛（路线：stars<50 默认 skip，**除非 flash 给出书面理由**）。
#: 定在 0.6：比"能用"（`RELEVANCE_FLOOR`）严一档 —— 硬线之下的项目要多付一份证据；
#: 2026-09-12 调 `RELEVANCE_FLOOR`（0.5→0.4）时一起复查过，结论是**它不动**：
#: 对 5 个需求它都是"能过就是真对口"。
JUSTIFY_RELEVANCE = 0.6

#: 评分 schema（路线原文的四个字段）
USAGES = ("copy", "reference", "dependency", "skip")
RISKS = ("ok", "warn", "block")

SCORE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "relevance": {"type": "number"},
        "usage": {"type": "string", "enum": list(USAGES)},
        "license_risk": {"type": "string", "enum": list(RISKS)},
        "reason": {"type": "string"},
    },
    "required": ["relevance", "usage", "license_risk", "reason"],
}

#: 强 copyleft：**只参考思路，不拷代码**（路线第 5 条红线）
COPYLEFT = frozenset(
    {
        "GPL-2.0", "GPL-2.0-only", "GPL-2.0-or-later",
        "GPL-3.0", "GPL-3.0-only", "GPL-3.0-or-later",
        "AGPL-3.0", "AGPL-3.0-only", "AGPL-3.0-or-later",
    }
)
#: 弱 copyleft / 有条件的：可以用，但要人看一眼
WEAK_COPYLEFT = frozenset({"LGPL-2.1", "LGPL-3.0", "MPL-2.0", "EPL-2.0", "CDDL-1.0"})
#: 宽松：直接可用
PERMISSIVE = frozenset(
    {
        "MIT", "Apache-2.0", "BSD-2-Clause", "BSD-3-Clause", "ISC", "0BSD", "Unlicense",
        "CC0-1.0", "MIT-0", "PostgreSQL", "Python-2.0",
    }
)

#: 打分时最多看几个文件的头部、每个文件看多少行
EVIDENCE_FILES = 3
EVIDENCE_HEAD_LINES = 40
EVIDENCE_BUDGET = 4000

README_NAMES = ("readme.md", "readme.rst", "readme.txt", "readme")
#: 证据里给 README 摘录多少字符。**只说明"它是什么"，不参与"相不相关"的判断** ——
#: 路线明令禁止只读 README 打分，所以摘录永远和代码证据一起出现。
README_CHARS = 700


class ScoutError(RuntimeError):
    """猎手自身的错误（搜索失败、评分返回不合规结构）。绝不静默降级成「没有候选」。"""


@dataclasses.dataclass(frozen=True)
class RepoFacts:
    """一个候选仓库的**客观事实**（全部来自 API，不经过模型）。"""

    full_name: str
    stars: int
    pushed_at: str
    license_spdx: str | None
    archived: bool
    default_branch: str
    description: str = ""
    html_url: str = ""
    #: GitHub 识别出的主语言。**空字符串 = 没有主语言**，那基本就是文档/清单仓库
    #: （实测：`sindresorhus/awesome`、各种 awesome 清单、纯 skill 仓库都是空的）。
    #: 它是初筛的一个便宜信号，不是硬门槛 —— 真正的门槛仍然是"证据里必须有代码"。
    language: str = ""

    @property
    def age_days(self) -> int:
        return repo_age_days(self.pushed_at)


@dataclasses.dataclass
class Candidate:
    facts: RepoFacts
    score: dict[str, Any] | None = None
    evidence_chars: int = 0
    notes: list[str] = dataclasses.field(default_factory=list)

    @property
    def usable(self) -> bool:
        """路线第 4 条：`usage≠skip 且 license_risk=ok` 才算能抓。"""
        if not self.score:
            return False
        return self.score.get("usage") != "skip" and self.score.get("license_risk") == "ok"

    def as_dict(self) -> dict[str, Any]:
        return {
            "full_name": self.facts.full_name,
            "stars": self.facts.stars,
            "pushed_at": self.facts.pushed_at,
            "age_days": self.facts.age_days,
            "license": self.facts.license_spdx,
            "archived": self.facts.archived,
            "description": self.facts.description,
            "score": self.score,
            "evidence_chars": self.evidence_chars,
            "usable": self.usable,
            "notes": self.notes,
        }


# ------------------------------------------------------------------ 纯函数

def repo_age_days(pushed_at: str, *, now: datetime | None = None) -> int:
    """距最后一次 push 多少天。解析不出来就当"很老"（宁可 skip，不要误用）。"""
    moment = now or datetime.now(timezone.utc)
    text = (pushed_at or "").replace("Z", "+00:00")
    try:
        stamp = datetime.fromisoformat(text)
    except ValueError:
        return 10_000
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return max(0, (moment - stamp).days)


def license_risk(spdx: str | None) -> str:
    """
    许可证 → `ok` / `warn` / `block`。**纯查表，不问模型。**

    - GPL / AGPL → `block`：路线第 5 条的红线，没有"理由充分"的例外；
    - LGPL / MPL / EPL 等 → `warn`：可以用，但传染性要人判断；
    - 没写许可证（None）→ `warn`：**没有许可证不等于可以随便用**，默认按最保守处理。
    """
    if not spdx:
        return "warn"
    if spdx in COPYLEFT:
        return "block"
    if spdx in WEAK_COPYLEFT:
        return "warn"
    if spdx in PERMISSIVE:
        return "ok"
    return "warn"


def hard_filter(
    facts: RepoFacts,
    *,
    min_stars: int = MIN_STARS,
    max_age_days: int = MAX_AGE_DAYS,
    now: datetime | None = None,
) -> tuple[bool, str]:
    """
    客观过滤：不满足就直接跳过，**不花模型调用**。

    `now` 可注入是**为了测得了**：不注入时按真实时钟算年龄，于是
    「test_hard_filter…[800 天]」这种用例**过一天就会红一次**（实测：写的时候是 800 天，
    第二天变成 801 天，断言里的 `800 天` 当场失效）。时间相关的东西必须能把"现在"钉住。
    """
    if facts.archived:
        return False, "仓库已归档"
    if facts.stars < min_stars:
        return False, f"stars {facts.stars} < {min_stars}"
    if now is not None:
        age = repo_age_days(facts.pushed_at, now=now)
        if age > max_age_days:
            return False, f"最后 push 距今 {age} 天 > {max_age_days}"
    elif facts.age_days > max_age_days:
        return False, f"最后 push 距今 {facts.age_days} 天 > {max_age_days}"
    return True, ""


def facts_from_api(payload: dict[str, Any]) -> RepoFacts:
    license_block = payload.get("license") or {}
    return RepoFacts(
        full_name=str(payload.get("full_name") or ""),
        stars=int(payload.get("stargazers_count") or 0),
        pushed_at=str(payload.get("pushed_at") or ""),
        license_spdx=(str(license_block.get("spdx_id")) if license_block.get("spdx_id") else None),
        archived=bool(payload.get("archived")),
        default_branch=str(payload.get("default_branch") or "main"),
        description=str(payload.get("description") or ""),
        html_url=str(payload.get("html_url") or ""),
        language=str(payload.get("language") or ""),
    )


# ------------------------------------------------------------------ 证据

CODE_SUFFIXES = (
    # 主流的通用语言
    ".py", ".go", ".rs", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".java", ".kt", ".kts",
    ".rb", ".php", ".c", ".cc", ".cpp", ".h", ".hpp", ".cs", ".swift", ".m", ".mm", ".scala",
    ".dart", ".ex", ".exs", ".erl", ".hs", ".ml", ".jl", ".lua", ".pl", ".r", ".f90", ".zig",
    # 不是"某种语言的源码"，但都是**能抄的代码**：脚本、模板、schema、查询
    ".sh", ".bash", ".zsh", ".ps1", ".sql", ".proto", ".vue", ".svelte",
)
CORE_HINTS = ("src", "lib", "core", "app", "pkg", "internal", "cmd", "packages", "source")
#: 证据里给 README 摘录多少字符。**只说明"它是什么"，不单独作为相关性依据** ——
#: 路线明令禁止只读 README 打分，所以摘录永远和代码证据一起出现。
README_CHARS = 700


def describe_repo(client: Any, full_name: str, *, branch: str | None = None) -> str:
    """
    给评分用的**证据包**：README 摘录 + 顶层目录树 + 核心文件的开头若干行。

    为什么加 README 摘录：实测（2026-09-12）只给"目录树 + 代码开头"时，模型判断
    "这个项目到底是干什么的"很吃力 —— 它看到的是一堆函数名，于是把真正对口的项目
    也打低分（相关度普遍 0.15–0.25）。README 恰好回答"是什么"，代码回答"怎么做"。

    **但不许只用 README**：`evidence_has_code()` 仍然要求证据里有代码文件，
    只有文档的仓库直接拒评（路线 5.1 的原话：README 夸大是经典坑）。
    """
    lines: list[str] = []

    # ---- README 摘录（信息密度最高，但只讲"是什么"）
    for name in ("README.md", "README.rst", "README.txt", "README"):
        try:
            payload = client.get(
                f"/repos/{full_name}/contents/{name}", params={"ref": branch} if branch else None
            )
        except Exception as exc:  # noqa: BLE001
            # README 取不到不是错误（有的仓库叫别的名字、有的没有）：记一笔继续找下一个。
            if name == "README":
                lines.append(f"（README 取不到：{type(exc).__name__}）")
            continue
        if isinstance(payload, dict) and payload.get("content"):
            import base64

            try:
                text = base64.b64decode(payload["content"]).decode("utf-8", errors="replace")
            except Exception:  # noqa: BLE001
                break
            excerpt = " ".join(text.split())[:README_CHARS]
            lines.append(f"README 摘录（只说明它是什么；判相关请结合下面的代码）：\n{excerpt}")
            break

    try:
        top = client.get(f"/repos/{full_name}/contents/", params={"ref": branch} if branch else None)
    except Exception as exc:
        raise ScoutError(f"取目录树失败 {full_name}：{str(exc)[:120]}") from exc

    entries = [entry for entry in (top or []) if isinstance(entry, dict)]
    names = sorted(str(entry.get("name")) for entry in entries)
    lines.append("顶层：" + "、".join(names[:40]))

    # 找 src/ lib/ core/ app/ pkg/ 之一，取它下面的文件
    target_dir = next(
        (name for name in names if name.lower() in CORE_HINTS and name.lower() != "README".lower()),
        None,
    )
    candidates: list[str] = []
    if target_dir:
        try:
            inner = client.get(f"/repos/{full_name}/contents/{target_dir}", params={"ref": branch} if branch else None)
            inner_entries = [entry for entry in (inner or []) if isinstance(entry, dict)]
            inner_names = sorted(str(entry.get("name")) for entry in inner_entries)
            lines.append(f"{target_dir}/：" + "、".join(inner_names[:40]))
            candidates += [f"{target_dir}/{name}" for name in inner_names if name.endswith(CODE_SUFFIXES)]
            if not candidates:
                # 很多仓库是 `src/<包名>/xxx.py`：只列一层会一个代码文件都看不到
                # （实测：issuesort 的 src 下只有子目录，于是被误判成"没有代码证据"）。
                for sub in [str(entry.get("name")) for entry in inner_entries if entry.get("type") == "dir"][:2]:
                    deeper = client.get(
                        f"/repos/{full_name}/contents/{target_dir}/{sub}",
                        params={"ref": branch} if branch else None,
                    )
                    for entry in (deeper or []):
                        if isinstance(entry, dict) and str(entry.get("name")).endswith(CODE_SUFFIXES):
                            candidates.append(f"{target_dir}/{sub}/{entry.get('name')}")
                if candidates:
                    lines.append(f"{target_dir}/ 下钻一层：{len(candidates)} 个代码文件")
        except Exception as exc:  # noqa: BLE001
            # 证据缺口要**写进证据**，不能静默跳过：评分的人该知道"这一层没看到"
            lines.append(f"（{target_dir}/ 读取失败：{type(exc).__name__}）")
    candidates += [name for name in names if name.endswith(CODE_SUFFIXES)]

    # ---- 兜底下钻：顶层既没有代码文件、也没有 src/lib/core 之类的目录
    #
    # 这是 2026-09-12 实测踩到的坑：`Aider-AI/aider`、`openai/codex`、`plandex-ai/plandex`
    # 的证据里"只有文档、没有代码"，于是**不许评分**（relevance 0.0），而 aider
    # 恰恰是路线 5.1 里"增量 diff 编辑"的参考实现 —— 证据缺口让它被判成不相关。
    # 原因是极常见的布局：包目录与仓库同名（`aider/`、`codex/`），不在 CORE_HINTS 里，
    # 而顶层又没有 `.py` 文件。**证据取不全等于把对口项目误杀**，所以这里必须下钻。
    if not candidates:
        skip = {"docs", "doc", "tests", "test", "examples", "example", "benchmark", "benchmarks",
                "assets", "images", "website", "site", ".github", ".devcontainer"}
        repo_name = full_name.split("/")[-1].lower()
        dirs = [str(entry.get("name")) for entry in entries if entry.get("type") == "dir"]
        # 先试与仓库同名的目录（Python 包最常见的布局），再试其余不像文档/测试的目录
        ordered = [name for name in dirs if name.lower() == repo_name] + [
            name for name in dirs if name.lower() != repo_name and name.lower() not in skip
        ]
        for sub in ordered[:3]:
            try:
                inner = client.get(
                    f"/repos/{full_name}/contents/{sub}", params={"ref": branch} if branch else None
                )
            except Exception:  # noqa: BLE001, S112
                continue
            inner_entries = [entry for entry in (inner or []) if isinstance(entry, dict)]
            inner_names = sorted(str(entry.get("name")) for entry in inner_entries)
            found = [f"{sub}/{name}" for name in inner_names if name.endswith(CODE_SUFFIXES)]
            # 再下钻一层：`aider/coders/editblock_coder.py` 这种才是真正干活的代码，
            # 而 `aider/__init__.py` 往往是空壳 —— 只取第一层会让证据"有代码但没用"。
            # 最多看两个子目录（有界），够让证据里出现真正的实现文件。
            for deeper in [str(e.get("name")) for e in inner_entries if e.get("type") == "dir"][:2]:
                try:
                    deeper_payload = client.get(
                        f"/repos/{full_name}/contents/{sub}/{deeper}",
                        params={"ref": branch} if branch else None,
                    )
                except Exception:  # noqa: BLE001, S112
                    continue
                found += [
                    f"{sub}/{deeper}/{entry.get('name')!s}"
                    for entry in (deeper_payload or [])
                    if isinstance(entry, dict) and str(entry.get("name")).endswith(CODE_SUFFIXES)
                ]
            if found:
                lines.append(f"{sub}/（顶层无代码，下钻找到）：" + "、".join(inner_names[:40]))
                candidates += found
                break

    budget = EVIDENCE_BUDGET - sum(len(line) for line in lines)
    for relative in candidates[:EVIDENCE_FILES]:
        try:
            payload = client.get(f"/repos/{full_name}/contents/{relative}", params={"ref": branch} if branch else None)
        except Exception as exc:  # noqa: BLE001
            lines.append(f"（{relative} 读取失败：{type(exc).__name__}）")
            continue
        if not isinstance(payload, dict) or not payload.get("content"):
            continue
        import base64

        try:
            text = base64.b64decode(payload["content"]).decode("utf-8", errors="replace")
        except Exception as exc:  # noqa: BLE001
            lines.append(f"（{relative} 解码失败：{type(exc).__name__}）")
            continue
        head = "\n".join(text.splitlines()[:EVIDENCE_HEAD_LINES])
        block = f"\n--- {relative}（前 {EVIDENCE_HEAD_LINES} 行）---\n{head[: budget // EVIDENCE_FILES]}"
        lines.append(block)
        budget -= len(block)
        if budget <= 0:
            break

    has_code = any(line.startswith("\n--- ") or ".py" in line or ".go" in line for line in lines)
    if not has_code:
        lines.append("\n（**证据里没有任何代码文件** —— 按路线 5.1，这种情况不得评分）")
    return "\n".join(lines)


def evidence_has_code(evidence: str) -> bool:
    """证据里有没有非 README 的代码 —— 没有就不许打分。"""
    if "没有任何代码文件" in evidence:
        return False
    return bool(re.search(r"--- \S+\.(py|go|rs|ts|js|java|rb|c|cpp|h|kt|php)", evidence))


# ------------------------------------------------------------------ 评分

SCORE_PROMPT = """你在为一个自动修复系统挑选可复用的开源项目。

需求：
{need}

候选仓库：{full_name}（stars {stars}，许可证 {license}，最后更新 {pushed}）
它的证据（目录树 + 核心文件开头；**没有给全文**）：
{evidence}

请只输出 JSON：
{{"relevance": 0-1 的相关度,
  "usage": "copy|reference|dependency|skip",
  "license_risk": "ok|warn|block",
  "reason": "一句话说明理由，必须引用证据里看到的东西"}}

规则：
- 只根据上面的证据判断，**不要靠印象**；证据不足就给低相关度并说明。
- usage：能直接抄代码片段=copy；只能参考设计=reference；作为依赖引入=dependency；不需要=skip。
"""


def default_chat(messages: list[dict[str, str]], schema: dict[str, Any]) -> Any:
    from ..gateway import chat

    return chat(messages, schema, "flash_api", temperature=0.0)


def score_repo(
    need: str,
    facts: RepoFacts,
    evidence: str,
    *,
    chat_fn: Callable[[list[dict[str, str]], dict[str, Any]], Any] | None = None,
) -> dict[str, Any]:
    """调 flash 打分。**没有代码证据就拒绝打分**（路线明令：禁止只读 README 打分）。"""
    if not evidence_has_code(evidence):
        return {
            "relevance": 0.0,
            "usage": "skip",
            "license_risk": license_risk(facts.license_spdx),
            "reason": "证据里只有文档、没有代码，按路线 5.1 拒绝评分",
        }
    chat_fn = chat_fn or default_chat
    prompt = SCORE_PROMPT.format(
        need=need,
        full_name=facts.full_name,
        stars=facts.stars,
        license=facts.license_spdx or "未声明",
        pushed=facts.pushed_at,
        evidence=evidence,
    )
    payload = chat_fn([{"role": "user", "content": prompt}], SCORE_SCHEMA)
    if not isinstance(payload, dict):
        raise ScoutError(f"评分返回的不是对象：{type(payload).__name__}")
    for field in ("relevance", "usage", "license_risk", "reason"):
        if field not in payload:
            raise ScoutError(f"评分缺字段 {field}：{json.dumps(payload, ensure_ascii=False)[:200]}")
    if payload["usage"] not in USAGES or payload["license_risk"] not in RISKS:
        raise ScoutError(f"评分取值非法：{payload}")
    # 许可证以**查表**为准：模型说 ok 但表里是 GPL，一律按 block（法律风险不交给模型判断）
    table_risk = license_risk(facts.license_spdx)
    if table_risk == "block":
        payload["license_risk"] = "block"
        payload["reason"] = f"{payload['reason']}｜许可证查表结果：{facts.license_spdx} 属强 copyleft"
    elif table_risk == "warn" and payload["license_risk"] == "ok":
        payload["license_risk"] = "warn"
    return payload


# ------------------------------------------------------------------ 主流程

@dataclasses.dataclass
class ScoutResult:
    need: str
    query: str
    searched: int
    candidates: list[Candidate]
    skipped: list[dict[str, str]] = dataclasses.field(default_factory=list)
    #: 低于硬线被跳过的（星数不够 **或** 两年没 push）—— 路线给它们留了一条
    #: "模型书面理由"的破格通道，所以要把事实留着备用
    below_bar: list[RepoFacts] = dataclasses.field(default_factory=list)

    @property
    def low_star(self) -> list[RepoFacts]:
        """旧名字，等价于 `below_bar`（破格通道覆盖的是"星数不够"与"太旧"两种）。"""
        return self.below_bar

    @property
    def usable(self) -> list[Candidate]:
        return [item for item in self.candidates if item.usable]

    def as_dict(self) -> dict[str, Any]:
        return {
            "need": self.need,
            "query": self.query,
            "searched": self.searched,
            "skipped": self.skipped,
            "below_bar": [facts.full_name for facts in self.below_bar],
            "candidates": [item.as_dict() for item in self.candidates],
        }


def search_repositories(client: Any, query: str, *, per_page: int = 10) -> list[RepoFacts]:
    payload = client.get(
        "/search/repositories",
        params={"q": query, "sort": "stars", "order": "desc", "per_page": per_page},
    )
    items = (payload or {}).get("items") or []
    return [facts_from_api(item) for item in items]


def scout(
    need: str,
    *,
    client: Any,
    query: str | None = None,
    per_page: int = 10,
    score_top: int = 3,
    chat_fn: Callable[[list[dict[str, str]], dict[str, Any]], Any] | None = None,
    min_stars: int = MIN_STARS,
    max_age_days: int = MAX_AGE_DAYS,
) -> ScoutResult:
    """
    搜索 → 硬过滤 → 前 `score_top` 个**看代码再打分**。

    过滤掉的不静默丢弃：`skipped` 里留下名字与原因，报告里能看到"为什么没选它"——
    路线里说了，找到"不该用"的和找到"该用"的同样有价值。
    """
    effective_query = query or need
    facts_list = search_repositories(client, effective_query, per_page=per_page)
    candidates: list[Candidate] = []
    skipped: list[dict[str, str]] = []
    below_bar: list[RepoFacts] = []

    for facts in facts_list:
        ok, reason = hard_filter(facts, min_stars=min_stars, max_age_days=max_age_days)
        if not ok:
            skipped.append({"full_name": facts.full_name, "reason": reason})
            # 低于硬线（星数或太旧）的记下来备用：路线允许模型用书面理由破格
            if below_bar_reason(facts, min_stars=min_stars, max_age_days=max_age_days):
                below_bar.append(facts)
            continue
        candidates.append(Candidate(facts=facts))

    for candidate in candidates[:score_top]:
        try:
            evidence = describe_repo(
                client, candidate.facts.full_name, branch=candidate.facts.default_branch
            )
        except ScoutError as exc:
            candidate.notes.append(str(exc))
            continue
        candidate.evidence_chars = len(evidence)
        candidate.score = score_repo(need, candidate.facts, evidence, chat_fn=chat_fn)

    candidates.sort(key=lambda item: -(item.score or {}).get("relevance", 0.0))
    for candidate in candidates[score_top:]:
        candidate.notes.append("未评分（超出本次模型预算）")
    return ScoutResult(
        need=need,
        query=effective_query,
        searched=len(facts_list),
        candidates=candidates,
        skipped=skipped,
        below_bar=below_bar,
    )


def below_bar_reason(
    facts: RepoFacts, *, min_stars: int = MIN_STARS, max_age_days: int = MAX_AGE_DAYS
) -> str:
    """
    低于硬线、但**可以**走破格通道的理由（空串 = 不能破格）。

    路线 5.1 第 2 条写的是"stars<50 **或** 最后 commit >2 年 → 默认 skip
    （除非 flash 给出书面理由）"—— 所以破格覆盖**两种**情形，不是只有星数。
    （实现时先只做了星数，实测下来"太旧但最对口"的项目全被挡在门外，
    查重这个需求就是这样凑不齐候选的。）

    仍然不能破格的：**归档仓库**（都归档了，抄它的设计没有意义）。
    """
    if facts.archived:
        return ""
    if facts.stars < min_stars:
        return f"stars {facts.stars} < {min_stars}"
    if facts.age_days > max_age_days:
        return f"最后 push 距今 {facts.age_days} 天 > {max_age_days}"
    return ""


def justify_low_star(
    need: str,
    facts: RepoFacts,
    *,
    client: Any,
    chat_fn: Callable[[list[dict[str, str]], dict[str, Any]], Any] | None = None,
    min_relevance: float = JUSTIFY_RELEVANCE,
) -> Candidate | None:
    """
    破格通道（路线 5.1 第 2 条的括号部分）：**stars<50 或两年没动，默认 skip，
    除非 flash 给出书面理由**。

    为什么需要它：路线自己指定的两个查重参考项目（PRism、issuesort）都是低星项目 ——
    硬过滤会把它们全挡掉。而且实测发现"太旧但最对口"的项目同样会被时间线挡掉，
    所以破格覆盖两种情形（`below_bar_reason`）。

    仍然有硬底线：**归档的、许可证是 block 的、看不到代码的、模型说 usage=skip 的，
    一律破格不了**。破格进来的候选会带上说明，报告里单独标出来供人抽查。
    """
    if license_risk(facts.license_spdx) == "block":
        return None
    if facts.archived:
        return None
    try:
        evidence = describe_repo(client, facts.full_name, branch=facts.default_branch)
    except ScoutError:
        return None
    if not evidence_has_code(evidence):
        return None
    score = score_repo(need, facts, evidence, chat_fn=chat_fn)
    relevance = float(score.get("relevance") or 0.0)
    if score.get("usage") == "skip" or relevance < min_relevance:
        return None
    return Candidate(
        facts=facts,
        score=score,
        evidence_chars=len(evidence),
        notes=[
            (
                f"破格：{below_bar_reason(facts) or '低于硬线'}，"
                f"但模型给了书面理由（相关度 {relevance:.2f}）"
            )
        ],
    )


# ------------------------------------------------------------------ 多轮搜索

#: 「模型判定相关」的相关度门槛 —— **2026-09-12 由人类拍板从 0.5 调到 0.4**。
#:
#: 这不是悄悄放松，是一次**有据可查的口径调整**，记在 `state/findings/scout-sourcing-direction-c.md`：
#: - 模型的刻度很保守：只有"几乎就是为这个需求写的"才给 0.6+；"确实在解决这个问题、
#:   但只覆盖一部分"（`ds300/patch-package` 的整包补丁、`git/git` 的 `git apply`）落在 0.4；
#: - 0.5 正好切在这两档之间，于是"够不够 3 个"变成由**刻度噪声**决定：
#:   同一份证据、同一温度，`git/git` 在一次跑里是 0.78，另一次是 0.40；
#: - 0.4 仍然要求"模型明确判定相关"（usage 也不能是 skip、许可证不能是 block），
#:   只是把"部分覆盖但确实对口"的项目算进来 —— 猎手要的是**能用的轮子**，不是"完全同款"。
#: 破格通道的门槛（`JUSTIFY_RELEVANCE = 0.6`，书面理由）**没有动**。
RELEVANCE_FLOOR = 0.4
#: 让模型重新规划关键词时最多给几个
MAX_PLANNED_QUERIES = 3

PLAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "queries": {"type": "array", "items": {"type": "string"}},
        "why": {"type": "string"},
    },
    "required": ["queries", "why"],
}

PLAN_PROMPT = """你在帮一个自动修复系统决定"下一轮该搜什么"。

需求：
{need}

已经搜过这些关键词，以及它们带回的仓库（名字 + 简介 + 星数）：
{seen}

请给出最多 {limit} 个**新的** GitHub 仓库搜索关键词，要求：
- 用英文、**尽量短**（仓库搜索是 AND 语义，词越多召回越少）；
- 换一个角度（同义词、领域词、技术栈词），不要重复上面已搜过的；
- **可以给 `topic:xxx` 形式的主题查询**（GitHub 主题是人工策展的，命中率往往比猜词高）；
- 目标是把"真正能解决这个需求的项目"找出来，而不是把结果数量凑多。

只输出 JSON：{{"queries": ["...", "..."], "why": "一句话说明为什么换这些词"}}
"""


def plan_queries(
    need: str,
    *,
    seen: Sequence[dict[str, Any]],
    previous: Sequence[str] = (),
    limit: int = MAX_PLANNED_QUERIES,
    chat_fn: Callable[[list[dict[str, str]], dict[str, Any]], Any] | None = None,
) -> tuple[list[str], str]:
    """
    让 flash 规划**下一轮**搜索关键词。

    为什么要多轮：单次关键词搜索的召回质量完全取决于"猜词"，而猜词这件事
    交给模型比交给我写死的关键词表好 —— 但它必须先看到"上一轮捞回来的是什么"，
    否则只会换个说法再捞一遍同样的东西。
    """
    chat_fn = chat_fn or default_chat
    seen_lines = [
        f"- {item.get('full_name')}（{item.get('stars')}★）：{str(item.get('description') or '')[:80]}"
        for item in list(seen)[:12]
    ] or ["- （上一轮没有可用结果）"]
    prompt = PLAN_PROMPT.format(
        need=need,
        seen="\n".join(seen_lines) + ("\n已搜过：" + "、".join(previous) if previous else ""),
        limit=limit,
    )
    try:
        payload = chat_fn([{"role": "user", "content": prompt}], PLAN_SCHEMA)
    except Exception:  # noqa: BLE001
        return [], "规划失败（模型不可用）"
    if not isinstance(payload, dict):
        return [], "规划返回的不是对象"
    queries: list[str] = []
    for item in payload.get("queries") or []:
        text = str(item).strip()
        if text and text not in queries and text not in previous and len(text) <= 60:
            queries.append(text)
    return queries[:limit], str(payload.get("why") or "")


@dataclasses.dataclass
class ScoutRound:
    """一轮搜索的账：搜了什么、捞回几条、其中几条算"够好"。"""

    index: int
    query: str
    searched: int
    new_candidates: int
    good: int
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


def lexical_affinity(query: str, facts: RepoFacts) -> float:
    """
    关键词与"仓库名 + 简介"的字面重合度（0–1）。

    为什么需要它：**打分是最贵的一步**（要取目录树 + 读文件头 + 一次模型调用），
    必须挑最可能相关的去打分。按星数挑会挑到"又大又无关"的项目
    （实测：给"增量编辑"需求打分打到 54k★ 的通用 agent 框架，相关度 0.15），
    而真正对口的往往是中低星项目。先按字面重合度挑，再看星数。
    """
    terms = {term.lower() for term in re.findall(r"[a-z]{3,}", (query or "").lower())}
    if not terms:
        return 0.0
    text = f"{facts.full_name} {facts.description}".lower()
    return sum(1 for term in terms if term in text) / len(terms)


RANK_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "picks": {"type": "array", "items": {"type": "string"}},
        "why": {"type": "string"},
    },
    "required": ["picks", "why"],
}

RANK_PROMPT = """你在为"哪个仓库最可能解决这个需求"做初筛（只按名字/简介/主语言，**还没看代码**）。

需求：{need}
本轮关键词：{query}

候选（名字 ｜ 星数 ｜ 主语言 ｜ 简介）：
{candidates}

挑出最多 {limit} 个**最值得深入看代码**的，按可能性从高到低：
- 只看上面的信息，不要凭对某个项目的印象；
- 宁可挑"名字/简介直接对应需求"的项目，也不要挑"很大很有名但与需求无关"的；
- 主语言是 `无` 的**多半是文档或清单仓库**（awesome 列表、资料合集、纯 skill 配置）：
  它们没有可抄的代码，挑它们等于把这次深评预算扔掉，**除非需求本身就是要资料**。

只输出 JSON：{{"picks": ["owner/repo", ...], "why": "一句话说明你的挑选标准"}}
"""


def candidate_source(candidate: Candidate) -> str:
    """候选是哪条取材路带进来的（`来源：topic(...)` → `topic`）。"""
    for note in candidate.notes:
        if note.startswith("来源："):
            return note[len("来源："):].split("(")[0].strip() or "其他"
    return "其他"


def group_by_source(
    candidates: Sequence[Candidate], *, query: str
) -> dict[str, list[Candidate]]:
    """按取材来源分组，每组内部按"有主语言 → 字面相关度 → 星数"排好序。"""
    groups: dict[str, list[Candidate]] = {}
    for candidate in candidates:
        groups.setdefault(candidate_source(candidate), []).append(candidate)
    for source, members in groups.items():
        groups[source] = sorted(
            members,
            key=lambda item: (
                not item.facts.language,          # 有主语言的排前面（False < True）
                -lexical_affinity(query, item.facts),
                -item.facts.stars,
            ),
        )
    return groups


#: 深评预算的**每路保底配额**。为什么要有配额：初筛只看名字/简介，
#: 实测它会把所有名额都给"名字里带 agent 的大项目"，而路线点名的参考实现
#: （aider 的 edit block、PRism 的查重）因为名字里没有那些词，连深评的机会都没有 ——
#: 那样多路取材就白做了。每一路各有各的道理（人工策展 / 模型点名 / 主题归集 / 关键词），
#: 所以每一路都**保底**深评几个；剩余名额再给模型的初筛（它的价值是找到我们想不到的）。
SOURCE_QUOTA: dict[str, int] = {
    "known": 5,      # 模型点名的项目本来就少（≤5），每个都是"它认为能解决这个需求的"
    "curated": 3,    # 人工清单带进来的，取清单里最像的几个
    "keyword": 2,
    "topic": 2,
    "seed": 3,       # 路线点名的种子：必须深评
    "其他": 3,
}


def pick_targets(
    need: str,
    candidates: Sequence[Candidate],
    *,
    query: str,
    chat_fn: Callable[[list[dict[str, str]], dict[str, Any]], Any] | None = None,
    limit: int,
    listing_limit: int = 40,
    use_quota: bool = True,
    quota: dict[str, int] | None = None,
) -> list[Candidate]:
    """**深评谁**：先按来源配额保底，剩下的名额交给模型的便宜初筛。"""
    if not candidates or limit <= 0:
        return []
    if len(candidates) <= limit:
        return list(candidates)

    reserve: list[Candidate] = []
    if use_quota:
        quota = SOURCE_QUOTA if quota is None else quota
        groups = group_by_source(candidates, query=query)
        for source in sorted(groups):
            reserve.extend(groups[source][: quota.get(source, quota.get("其他", 2))])
        reserve = reserve[:limit]

    remaining = limit - len(reserve)
    picked = list(reserve)
    if remaining > 0:
        rest = [item for item in candidates if item not in reserve]
        picked += rank_candidates(
            need,
            rest,
            query=query,
            chat_fn=chat_fn,
            limit=remaining,
            listing_limit=listing_limit,
        )
    return picked[:limit]


def balanced_listing(
    candidates: Sequence[Candidate], *, query: str, limit: int
) -> list[Candidate]:
    """
    给便宜初筛挑一份**来源均衡**的候选清单。

    为什么必须均衡（2026-09-12 实测）：改成多路取材后候选池从 ~69 涨到 ~186，
    而初筛只把 `candidates[:15]` 塞进提示里 —— 那 15 个恰好是按星数排在最前的主题搜索结果
    （清一色几万星的大项目），**策展清单带进来的对口项目一个都进不了初筛**，
    深评预算于是又花在"很大很有名但与需求无关"的项目上，多路取材就白做了。

    均衡规则（可解释、可复现）：
    - 每一路内部先按"有主语言 → 字面相关度 → 星数"排序（同一路里最像代码项目的排前面）；
    - 然后**轮流**从每一路取一个，直到填满 `limit`；
    - 某一路取空就跳过它，剩下的名额继续分给别的路。

    "有主语言优先"是 2026-09-12 那次实测逼出来的：初筛挑中的 5 个候选
    （`Agents365-ai/drawio-skill`、`CodeEditApp/CodeEdit` 等）证据里**一个代码文件都没有**，
    于是一个都没评上，严格口径 0/1 —— 深评预算被文档型仓库吃掉了。
    """
    groups = group_by_source(candidates, query=query)

    order = sorted(groups)          # 固定顺序：同样的池子每次排出同样的清单（可复现）
    picked: list[Candidate] = []
    depth = 0
    while len(picked) < limit and any(depth < len(groups[source]) for source in order):
        for source in order:
            if depth < len(groups[source]):
                picked.append(groups[source][depth])
                if len(picked) >= limit:
                    break
        depth += 1
    return picked


def rank_candidates(
    need: str,
    candidates: Sequence[Candidate],
    *,
    query: str = "",
    chat_fn: Callable[[list[dict[str, str]], dict[str, Any]], Any] | None = None,
    limit: int = 4,
    listing_limit: int = 40,
) -> list[Candidate]:
    """
    **便宜的初筛**：一次模型调用看过几十个候选的名字+简介，挑出最值得深评的几个。

    为什么必须分两步：深评（取 README + 目录树 + 代码开头 + 一次评分）是最贵的一步，
    而候选池往往有几十个。逐个深评会把预算烧在"又大又无关"的项目上。
    排序失败（模型没起来/输出不合规）就退化成字面相关度 + 星数 ——
    **初筛只是省成本，它挂了不该让整条链路挂**。
    """
    if not candidates:
        return []
    if len(candidates) <= limit:
        return list(candidates)

    listing_candidates = balanced_listing(candidates, query=query or need, limit=listing_limit)
    fallback = sorted(
        candidates,
        key=lambda item: (-lexical_affinity(query or need, item.facts), -item.facts.stars),
    )[:limit]
    if chat_fn is None:
        return fallback

    listing = "\n".join(
        f"- {item.facts.full_name} ｜ {item.facts.stars}★ ｜ {item.facts.language or '无'} ｜ "
        f"{(item.facts.description or '')[:70]}"
        for item in listing_candidates
    )
    try:
        payload = chat_fn(
            [{"role": "user", "content": RANK_PROMPT.format(need=need, query=query or need, candidates=listing, limit=limit)}],
            RANK_SCHEMA,
        )
    except Exception:  # noqa: BLE001
        return fallback
    if not isinstance(payload, dict):
        return fallback
    by_name = {item.facts.full_name: item for item in candidates}
    picked: list[Candidate] = []
    for name in payload.get("picks") or []:
        item = by_name.get(str(name).strip())
        if item is not None and item not in picked:
            picked.append(item)
    if not picked:
        return fallback
    # 模型只挑了一两个时，用字面相关度补齐预算（别浪费这一轮的机会）
    for item in fallback:
        if len(picked) >= limit:
            break
        if item not in picked:
            picked.append(item)
    return picked[:limit]


def score_pool(
    need: str,
    result: ScoutResult,
    *,
    client: Any,
    score_top: int = 5,
    listing_limit: int = 40,
    use_quota: bool = True,
    chat_fn: Callable[[list[dict[str, str]], dict[str, Any]], Any] | None = None,
) -> ScoutResult:
    """
    对**已有的候选池**做"排序 → 深评"（不搜索）。

    单独抽出来是因为冻结核对要用它：把某一次搜索的结果**冻到磁盘**，
    之后每次核对只跑这一条路径 —— 否则"搜索随机变 + 评分随机变"两层噪声叠在一起，
    指标每次都不一样（实测：同一份代码，5 个需求的结果在 4/5 与 2/5 之间摆动）。
    """
    unscored = [item for item in result.candidates if item.score is None]
    picked = pick_targets(
        need,
        unscored,
        query=result.query,
        chat_fn=chat_fn,
        limit=score_top,
        listing_limit=listing_limit,
        use_quota=use_quota,
    )
    for candidate in picked:
        try:
            evidence = describe_repo(
                client, candidate.facts.full_name, branch=candidate.facts.default_branch
            )
        except ScoutError as exc:
            candidate.notes.append(str(exc))
            continue
        candidate.evidence_chars = len(evidence)
        candidate.score = score_repo(need, candidate.facts, evidence, chat_fn=chat_fn)
    result.candidates.sort(key=lambda item: -(item.score or {}).get("relevance", 0.0))
    return result


def good_candidates(result: ScoutResult, *, floor: float = RELEVANCE_FLOOR) -> list[Candidate]:
    """**严格口径**：模型判定相关（≥floor）且 usage≠skip 且许可证不是 block。"""
    return [
        item
        for item in result.candidates
        if item.score
        and float(item.score.get("relevance") or 0.0) >= floor
        and item.score.get("usage") != "skip"
        and item.score.get("license_risk") in ("ok", "warn")
    ]


def multi_round_scout(
    need: str,
    *,
    client: Any,
    queries: Sequence[str] = (),
    seeds: Sequence[str] = (),
    max_rounds: int = 3,
    queries_per_round: int = 1,
    per_page: int = 10,
    score_per_round: int = 3,
    min_valid: int = 3,
    relevance_floor: float = RELEVANCE_FLOOR,
    chat_fn: Callable[[list[dict[str, str]], dict[str, Any]], Any] | None = None,
) -> tuple[ScoutResult, list[ScoutRound]]:
    """
    **多轮搜索**：一轮不够就用模型重新规划关键词再搜一轮，直到"够好的候选"达标或轮数用尽。

    实际经验（2026-09-12）：单轮关键词搜索的命中率很依赖措辞 ——
    "duplicate issue detection" 捞回来一堆八百天没动的玩具仓库，
    而换个角度（同义词/领域词）就能捞到真正在解决这个问题的项目。
    所以这里把"换词"变成一个**有反馈的循环**：每一轮都告诉模型上一轮看见了什么。

    几条预算纪律（否则"多轮"会变成烧钱）：
    - 轮数 `max_rounds` 封顶；
    - 每轮最多给 `score_per_round` 个候选做"看代码 + 打分"（最贵的一步）；
    - 已经打过分的候选不再重复打分；
    - 停下来的条件只有一个：**够好的候选 ≥ `min_valid`**。
    """
    result = ScoutResult(need=need, query=queries[0] if queries else need, searched=0, candidates=[])
    rounds: list[ScoutRound] = []
    seen_names: set[str] = set()
    pending = [query for query in queries] or [need]

    for name in seeds:
        try:
            facts = facts_from_api(client.get(f"/repos/{name}"))
        except Exception as exc:  # noqa: BLE001
            # 种子取不到不静默：记进账里，报告里能看到"这个种子没查到"
            result.skipped.append({"full_name": name, "reason": f"种子取不到：{type(exc).__name__}"})
            continue
        ok, reason = hard_filter(facts)
        if ok:
            result.candidates.append(Candidate(facts=facts))
            seen_names.add(facts.full_name)
        else:
            result.skipped.append({"full_name": facts.full_name, "reason": f"种子：{reason}"})
            if below_bar_reason(facts):
                result.below_bar.append(facts)

    for index in range(1, max_rounds + 1):
        if not pending:
            break
        # 一轮里可以跑多条关键词：模型一次会给出 2–3 条建议，只用第一条等于把它白给的
        # 备选全扔了（实测：只用一条时，"查重"这个需求三轮下来只多捞到 1 个候选）。
        batch = [pending.pop(0) for _ in range(min(queries_per_round, len(pending)))]
        query = "；".join(batch)
        facts_list: list[RepoFacts] = []
        for single in batch:
            facts_list.extend(search_repositories(client, single, per_page=per_page))
        result.searched += len(facts_list)

        new_candidates = 0
        for facts in facts_list:
            if facts.full_name in seen_names:
                continue
            seen_names.add(facts.full_name)
            ok, reason = hard_filter(facts)
            if not ok:
                result.skipped.append({"full_name": facts.full_name, "reason": reason})
                if below_bar_reason(facts):
                    result.below_bar.append(facts)
                continue
            result.candidates.append(Candidate(facts=facts))
            new_candidates += 1

        # 每轮只给最贵的"看代码 + 打分"留固定预算。**先用一次便宜的批量排序挑人，再深评**：
        # 逐个深评会把预算烧在"又大又无关"的项目上（实测：给"增量编辑"需求打分打到
        # 54k★ 的通用 agent 框架，相关度 0.15），而一次排序调用能看过十几个候选。
        score_pool(need, result, client=client, score_top=score_per_round, chat_fn=chat_fn)

        good = [
            item
            for item in result.candidates
            if item.score
            and float(item.score.get("relevance") or 0.0) >= relevance_floor
            and item.score.get("usage") != "skip"
            and item.score.get("license_risk") in ("ok", "warn")
        ]
        rounds.append(
            ScoutRound(index=index, query=query, searched=len(facts_list),
                       new_candidates=new_candidates, good=len(good))
        )
        if len(good) >= min_valid:
            rounds[-1].note = "够好的候选已达标，停止换词"
            break

        # 还没够 → 让模型换词（它必须先看到这一轮捞回来了什么）
        if index < max_rounds:
            planned, why = plan_queries(
                need,
                seen=[item.as_dict() for item in result.candidates],
                previous=[entry.query for entry in rounds] + list(queries),
                chat_fn=chat_fn,
            )
            if planned:
                pending.extend(planned)
                rounds[-1].note = f"换词：{'、'.join(planned)}（{why}）"
            else:
                rounds[-1].note = "模型没有给出新关键词"

    result.candidates.sort(key=lambda item: -(item.score or {}).get("relevance", 0.0))
    for candidate in result.candidates:
        if candidate.score is None:
            candidate.notes.append("未评分（超出本轮模型预算）")
    return result, rounds


# ------------------------------------------------------------------ 抓取

def vendor_dir(full_name: str) -> Path:
    return VENDOR_DIR / full_name.replace("/", "_")


def resolve_commit(client: Any, full_name: str, *, ref: str | None = None) -> str:
    """把要抓的那个版本解析成 40 位 commit sha —— 引用必须能指回**具体版本**。"""
    if not ref:
        payload = client.get(f"/repos/{full_name}")
        ref = str((payload or {}).get("default_branch") or "main")
    payload = client.get(f"/repos/{full_name}/git/ref/heads/{ref}")
    return str((payload or {}).get("object", {}).get("sha") or "")


def download_tarball(url: str) -> bytes:
    """
    默认下载器：`GET /repos/{o}/{r}/tarball/{sha}`（读 token）。

    **为什么不 `git clone`**：本机实测 `git clone`（哪怕克隆本地路径）会拉起
    `git-upload-pack` 并通过**命名管道**通信，而沙箱不允许创建命名管道
    （`sh.exe: fatal error - couldn't create signal pipe, Win32 error 5`）。
    tarball 走 HTTPS + Python 解包，不依赖 git、不建管道，反而更稳。
    """
    from ..github import read_token

    request = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {read_token()}",
            "User-Agent": "repo-autopilot",
            "Accept": "application/vnd.github+json",
        },
    )
    with urllib.request.urlopen(request, timeout=300) as response:
        return response.read()


def extract_tarball(blob: bytes, destination: Path) -> int:
    """
    解包并**剥掉 GitHub 的顶层目录**（`{owner}-{repo}-{sha}/`）。

    刻意不用 `extractall`：它会把压缩包里任意路径写到目标之外（zip-slip）。
    这里逐个成员落盘，并跳过目录与空名。
    """
    import io
    import tarfile

    written = 0
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as archive:
        members = [member for member in archive.getmembers() if member.isfile()]
        prefixes = {member.name.split("/")[0] for member in members if member.name}
        prefix = prefixes.pop() if len(prefixes) == 1 else ""
        for member in members:
            relative = member.name
            if prefix and relative.startswith(prefix + "/"):
                relative = relative[len(prefix) + 1 :]
            if not relative or relative.startswith("/") or ".." in Path(relative).parts:
                continue
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            source = archive.extractfile(member)
            if source is None:
                continue
            target.write_bytes(source.read())
            written += 1
    return written


def keep_license_copy(destination: Path) -> str | None:
    """
    LICENSE 另存一份到仓库目录**外面**（`<name>-LICENSE.copy`）。

    我们引用的依据是"当时那个 commit 的那个许可证"，而不是"它现在的许可证" ——
    上游改协议不该悄悄改写我们的记录。
    """
    for name in ("LICENSE", "LICENSE.md", "LICENSE.txt", "COPYING", "LICENSE-MIT"):
        source_license = destination / name
        if source_license.is_file():
            copy = destination.with_name(destination.name + "-LICENSE.copy")
            shutil.copyfile(source_license, copy)
            return str(copy)
    return None


def fetch_repo(
    full_name: str,
    *,
    client: Any = None,
    ref: str | None = None,
    target: Path | None = None,
    download: Callable[[str], bytes] | None = None,
    tarball_url: str | None = None,
) -> dict[str, Any]:
    """
    抓取到 `state/vendor/{owner}_{repo}/`，记录 commit hash，并留下 LICENSE 副本。

    默认走 **tarball**（见 `download_tarball` 的注释：本机 `git clone` 会被命名管道限制挡住）。
    `download` / `tarball_url` 可注入，测试里用本地造的 tar.gz 跑完整解包逻辑。
    """
    destination = target or vendor_dir(full_name)
    if destination.exists():
        shutil.rmtree(destination, ignore_errors=True)
    destination.parent.mkdir(parents=True, exist_ok=True)

    commit = ""
    if client is not None:
        commit = resolve_commit(client, full_name, ref=ref)
    url = tarball_url or f"https://api.github.com/repos/{full_name}/tarball/{commit or ref or 'HEAD'}"
    blob = (download or download_tarball)(url)
    files = extract_tarball(blob, destination)
    if files == 0:
        raise ScoutError(f"tarball 里没有文件：{full_name}（{url}）")

    license_copy = keep_license_copy(destination)
    record = {
        "full_name": full_name,
        "path": str(destination),
        "commit": commit,
        "files": files,
        "license_copy": license_copy,
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": url,
    }
    (destination.parent / f"{destination.name}-FETCH.json").write_text(
        json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return record


def fetch_usable(
    result: ScoutResult, *, client: Any = None, target_root: Path | None = None
) -> list[dict[str, Any]]:
    """把 `usable` 的候选抓下来（`usage≠skip 且 license_risk=ok`）。"""
    fetched: list[dict[str, Any]] = []
    for candidate in result.usable:
        destination = (target_root / candidate.facts.full_name.replace("/", "_")) if target_root else None
        fetched.append(
            fetch_repo(candidate.facts.full_name, client=client, target=destination)
        )
    return fetched


def python_executable() -> str:
    return sys.executable
