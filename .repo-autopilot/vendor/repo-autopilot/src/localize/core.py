"""4.1 文件定位：issue 文本 + 仓库文件树 → **≤5 个候选文件**（JSON）。

路线原文：

> issue 文本 + 仓库文件树 → 本地小模型初筛 → grep/embedding 精排 → 输出 ≤5 个候选文件清单（JSON）。
> **禁止把整仓库喂给 flash** —— 长上下文漂移是主要翻车源。

## 三级漏斗，越往后越贵

1. **文件树 + 词法信号**（零模型成本）：issue 里直接写出来的路径、文件名、符号名，
   以及这些词在文件正文里的命中次数。写 `Money.parse` 的 issue，答案在 `money.py` 里 ——
   这个信号强到不需要模型。
2. **本地小模型初筛**（本地、免费、有界）：只把**路径清单**给模型，让它圈一个子集。
   **路径清单不是文件内容** —— 这条边界就是"禁止把整仓库喂给 flash"的落地点：
   本模块从头到尾**不调用 flash 档**，也从不把文件正文发给任何模型。
3. **embedding 精排**（本地 bge-m3）：对候选文件的"档案文本"（路径 + 符号名 + 开头注释）
   算余弦，与词法分加权合并。

## 为什么把"为什么是它"一起输出

定位错了以后最容易浪费时间的动作是"再读一遍仓库"。每个候选都带上 `why`
（命中了哪个路径/符号、余弦多少），下一个人（或下一轮）能直接判断**是信号错了还是收窄不够**，
而不是重新猜。3.2 的阈值标定、3.3 的打回账本，用的都是同一个原则：让判断过程可复核。

## 为什么不做成"让 flash 直接读仓库"

那是这类系统最常见的翻车方式：上下文一长，模型开始编造路径和行号，
而且**它编得很像真的**。与其事后校验幻觉，不如让它一开始就没机会看到全文。
"""

from __future__ import annotations

import dataclasses
import json
import re
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path
from typing import Any

import numpy as np

ROOT = Path(__file__).resolve().parents[2]

#: 目录名黑名单：这些目录里的东西不是"待修的源码"
SKIP_DIRS = frozenset(
    {
        ".git", ".hg", ".svn", "node_modules", "__pycache__", ".mypy_cache", ".pytest_cache",
        ".ruff_cache", ".venv", "venv", "env", "dist", "build", "target", ".idea", ".vscode",
        "vendor", "third_party", "site-packages", ".cache",
    }
)

#: 只看这些后缀（其余当二进制/无关内容跳过）
TEXT_SUFFIXES = frozenset(
    {
        ".py", ".pyi", ".js", ".jsx", ".ts", ".tsx", ".go", ".rs", ".java", ".kt", ".rb", ".php",
        ".c", ".h", ".cc", ".cpp", ".hpp", ".cs", ".swift", ".m", ".mm", ".scala", ".sh", ".ps1",
        ".sql", ".md", ".txt", ".toml", ".yaml", ".yml", ".json", ".ini", ".cfg", ".html", ".css",
    }
)

#: 默认候选上限：路线要求最终 ≤5 —— 这个数是"给人读的"，多一个就多一份读的成本
DEFAULT_TOP_K = 5
#: 精排阶段的候选池大小
DEFAULT_POOL = 20
#: 单个文件最多读多少字节做 grep/档案文本（防大文件拖死）
MAX_FILE_BYTES = 120_000

_PATH_RE = re.compile(r"[\w./\\-]+\.(?:py|pyi|js|jsx|ts|tsx|go|rs|java|kt|rb|php|c|h|cc|cpp|hpp|cs|sh|ps1|sql|toml|yaml|yml|json|md)", re.IGNORECASE)
_IDENT_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]{2,})\b")
_TEST_RE = re.compile(r"\b(test_[A-Za-z0-9_]+)\b")
#: 这些词出现在几乎每条 issue 里，拿它们做信号只会把噪声排到前面
STOPWORDS = frozenset(
    {
        "the", "and", "for", "with", "when", "this", "that", "not", "but", "are", "was", "were",
        "have", "has", "had", "does", "did", "can", "could", "should", "would", "from", "into",
        "out", "use", "used", "using", "get", "got", "set", "error", "issue", "bug", "fix",
        "please", "help", "thanks", "line", "file", "code", "test", "tests", "python", "import",
        "expected", "actual", "result", "return", "returns", "https", "http", "www",
        "com", "org", "github", "print", "value", "values", "true", "false", "none", "self",
    }
)


class LocalizeError(RuntimeError):
    """定位器自身的错误（仓库不存在、候选超限）。绝不静默返回空结果。"""


@dataclasses.dataclass(frozen=True)
class FileEntry:
    path: str          # 相对仓库根的 POSIX 路径
    size: int
    language: str

    @property
    def basename(self) -> str:
        return self.path.rsplit("/", 1)[-1]

    @property
    def stem(self) -> str:
        return self.basename.rsplit(".", 1)[0]

    def as_dict(self) -> dict[str, Any]:
        return {"path": self.path, "size": self.size, "language": self.language}


@dataclasses.dataclass(frozen=True)
class Candidate:
    """一个候选文件。`why` 是给**人**看的，不是给日志看的。"""

    path: str
    score: float
    lexical: float
    embedding: float
    why: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "score": round(self.score, 4),
            "lexical": round(self.lexical, 4),
            "embedding": round(self.embedding, 4),
            "why": self.why,
        }


# ------------------------------------------------------------------ 文件树

def language_of(path: str) -> str:
    name = path.rsplit("/", 1)[-1]
    return name.rsplit(".", 1)[-1].lower() if "." in name else ""


def walk_repo(root: Path | str, *, max_files: int = 4000) -> list[FileEntry]:
    """
    走一遍仓库，返回**待修源码**的清单。

    只做两件事：排除无关目录（`.git`/`node_modules`/缓存），只留文本类后缀。
    刻意不读内容 —— 这一步要能在几万文件的仓库上瞬间完成，读内容留到精排阶段。
    """
    base = Path(root)
    if not base.is_dir():
        raise LocalizeError(f"仓库目录不存在：{base}")
    entries: list[FileEntry] = []
    for path in sorted(base.rglob("*")):
        if len(entries) >= max_files:
            break
        if not path.is_file():
            continue
        parts = set(path.relative_to(base).parts[:-1])
        if parts & SKIP_DIRS:
            continue
        if path.suffix.lower() not in TEXT_SUFFIXES:
            continue
        relative = path.relative_to(base).as_posix()
        entries.append(FileEntry(relative, path.stat().st_size, language_of(relative)))
    return entries


def read_text_safely(path: Path, limit: int = MAX_FILE_BYTES) -> str:
    """读文件正文，坏编码不抛异常（`errors="replace"`）—— 定位不该死在乱码上。"""
    try:
        with open(path, "rb") as handle:
            raw = handle.read(limit)
    except OSError:
        return ""
    return raw.decode("utf-8", errors="replace")


def symbol_names(text: str, limit: int = 40) -> list[str]:
    """从源码里抠出 `def`/`class`/`function` 名 —— 档案文本的主料。"""
    names = re.findall(r"^\s*(?:def|class|function|func|fn)\s+([A-Za-z_][A-Za-z0-9_]*)", text, re.MULTILINE)
    return names[:limit]


# ------------------------------------------------------------------ 词法信号

def extract_tokens(issue_text: str) -> dict[str, list[str]]:
    """
    从 issue 里抽三类信号：

    - `paths`：正文里直接写出来的文件路径（最强信号，作者自己指了位置）；
    - `test_names`：`test_xxx` —— 测试名与源文件名的映射能反推源文件；
    - `identifiers`：驼峰/下划线标识符（去掉停用词与纯数字）。
    """
    text = issue_text or ""
    paths = [match.group(0).replace("\\", "/").lstrip("./") for match in _PATH_RE.finditer(text)]
    tests = sorted(set(_TEST_RE.findall(text)))
    identifiers: list[str] = []
    for match in _IDENT_RE.finditer(text):
        token = match.group(1)
        lowered = token.lower()
        if lowered in STOPWORDS or token.isdigit():
            continue
        if len(token) < 3:
            continue
        identifiers.append(token)
    seen: set[str] = set()
    unique: list[str] = []
    for token in identifiers:
        if token not in seen:
            seen.add(token)
            unique.append(token)
    return {"paths": paths, "test_names": tests, "identifiers": unique[:60]}


def lexical_scores(
    root: Path | str,
    entries: Sequence[FileEntry],
    tokens: dict[str, list[str]],
    *,
    max_bytes: int = MAX_FILE_BYTES,
    total_bytes_budget: int = 6_000_000,
) -> dict[str, tuple[float, list[str]]]:
    """
    词法/ grep 打分。返回 `{path: (分数, 命中理由)}`。

    权重是有讲究的：**作者直接把路径写出来**（100 分）远强于**正文里恰好出现了某个词**（每个 12 分），
    因为前者是意图，后者可能只是巧合。测试名映射给 40 分 —— 它是很强的间接证据
    （`tests/test_money.py` 出问题，八成在 `ledger/money.py`）。

    ## 为什么要做词频过滤（df）

    实测踩过：issue 里写了 `ledger/money.py`，而 `ledger` 这个词**出现在半个仓库里**
    （`def summary(ledger: Ledger)` 这种参数名到处都是），于是每个文件都拿到 12 分的"命中"。
    在**超过 30% 的文件**里都出现的词没有任何定位能力，直接不算命中 ——
    这一条同时解决了"通用词"和"目录名"两类噪声。

    正文读取有总预算（默认 6MB）：大仓库里读不完就只保留路径/测试名信号，
    绝不让"打分"这件事把内存吃光。
    """
    base = Path(root)
    text_paths = {p.rsplit("/", 1)[-1]: p for p in tokens.get("paths", [])}
    identifiers = tokens.get("identifiers", [])
    test_names = tokens.get("test_names", [])

    # 先读正文（带总预算），并统计每个标识符出现在多少个文件里
    bodies: dict[str, str] = {}
    spent = 0
    for entry in entries:
        if spent >= total_bytes_budget:
            break
        body = read_text_safely(base / entry.path, max_bytes)
        bodies[entry.path] = body
        spent += len(body)

    document_frequency: dict[str, int] = {}
    # 大小写不敏感：issue 里写 `ledger`，代码里是 `Ledger` —— 这是同一件事。
    # （实测踩过：大小写敏感时 `ledger` 的 df 从 2 掉到 1，词频过滤就失效了）
    lowered = {path: body.lower() for path, body in bodies.items()}
    for token in identifiers:
        needle = token.lower()
        document_frequency[token] = sum(1 for body in lowered.values() if needle in body)
    if len(bodies) <= 4:
        # 小仓库里 30% 没有统计意义：出现在**一个以上**文件里的词就是噪声
        # （3 个文件的仓库里，"ledger" 出现在 2 个文件就足以说明它不区分任何东西）
        threshold = 1
    else:
        threshold = max(2, int(0.3 * len(bodies)))
    useful = [token for token in identifiers if document_frequency.get(token, 0) <= threshold]

    scores: dict[str, tuple[float, list[str]]] = {}

    for entry in entries:
        score = 0.0
        reasons: list[str] = []

        for raw in tokens.get("paths", []):
            if entry.path.endswith(raw) or raw.endswith(entry.path):
                score += 100
                reasons.append(f"正文直接提到路径 {raw}")
                break

        for name in text_paths:
            if name == entry.basename:
                score += 60
                reasons.append(f"正文提到文件名 {name}")
                break

        for test_name in test_names:
            stem = test_name[len("test_") :]
            if entry.stem == stem:
                score += 40
                reasons.append(f"正文提到测试 {test_name} → 对应源文件 {entry.path}")
                break

        body = bodies.get(entry.path) or ""
        if body and useful:
            hits = [token for token in useful if token in body]
            if hits:
                # 命中越多越可信，但别让"到处都是的通用词"压过路径信号
                score += min(12 * len(hits), 96)
                reasons.append("正文命中符号：" + "、".join(hits[:6]))

        if score:
            scores[entry.path] = (score, reasons)
    return scores


# ------------------------------------------------------------------ 档案文本 + embedding

def profile_text(root: Path | str, entry: FileEntry, *, header_lines: int = 12) -> str:
    """
    一个文件的"档案文本"：路径 + 符号名 + 开头注释。

    **不是全文**：全文会淹没信号（大文件里什么都提了一嘴），也让 embedding 偏向长文件。
    """
    base = Path(root)
    body = read_text_safely(base / entry.path, 20_000)
    header = "\n".join(
        line for line in body.splitlines()[:header_lines] if line.strip().startswith(("#", "//", '"""', "*"))
    )
    names = " ".join(symbol_names(body, limit=30))
    return f"{entry.path}\n{entry.language}\n{names}\n{header}".strip()


def embedding_scores(
    issue_text: str,
    root: Path | str,
    entries: Sequence[FileEntry],
    *,
    embed_fn: Callable[[Sequence[str]], np.ndarray] | None = None,
) -> dict[str, float]:
    """候选文件档案与 issue 的余弦。空候选直接返回空（不调模型）。"""
    if not entries:
        return {}
    texts = [issue_text] + [profile_text(root, entry) for entry in entries]
    if embed_fn is None:
        from src.gateway import embed as gateway_embed

        embed_fn = gateway_embed
    vectors = np.asarray(embed_fn(texts), dtype=float)
    issue = vectors[0]
    return {
        entry.path: float(issue @ vectors[index + 1])
        for index, entry in enumerate(entries)
    }


# ------------------------------------------------------------------ 本地模型初筛

#: 初筛提示词：**只给路径**。这一条是"禁止把整仓库喂给模型"的落地点。
SCREEN_PROMPT = """你在帮一个修复机器人缩小范围。

下面是仓库里的文件路径清单（**没有文件内容**）：
{listing}

请只根据路径名判断：哪 20 个文件最可能包含下面这条 issue 所说的代码？
只输出 JSON：{{"files": ["路径1", "路径2", ...]}}，路径必须来自上面的清单。

issue：
{issue}
"""


def screen_paths(
    listing: Sequence[str],
    issue_text: str,
    *,
    chat_fn: Callable[..., Any] | None = None,
    limit: int = DEFAULT_POOL,
) -> list[str]:
    """
    本地小模型初筛：从路径清单里圈一个子集。

    `chat_fn` 默认走 `local_small` 档 —— 本模块**从不调用 flash**。
    模型返回的路径必须在清单里，否则丢弃（防止它编一个不存在的路径）。
    """
    if chat_fn is None:
        from src.gateway import chat as chat_fn  # type: ignore[assignment]

    known = set(listing)
    prompt = SCREEN_PROMPT.format(listing="\n".join(listing), issue=(issue_text or "")[:4000])
    schema = {
        "type": "object",
        "properties": {"files": {"type": "array", "items": {"type": "string"}}},
        "required": ["files"],
    }
    try:
        result = chat_fn([{"role": "user", "content": prompt}], schema, "local_small")
    except Exception:  # noqa: BLE001
        # 初筛只是"省一点精排成本"，失败不该让整条定位链路失败
        return []
    picked = [str(item) for item in (result or {}).get("files", [])]
    return [item for item in picked if item in known][:limit]


# ------------------------------------------------------------------ 主流程

def locate(
    issue_text: str,
    root: Path | str,
    *,
    top_k: int = DEFAULT_TOP_K,
    pool: int = DEFAULT_POOL,
    chat_fn: Callable[..., Any] | None = None,
    embed_fn: Callable[[Sequence[str]], np.ndarray] | None = None,
    use_model: bool = True,
) -> list[Candidate]:
    """
    三级漏斗。返回 ≤`top_k` 个候选（按分数降序），每个都带 `why`。

    `top_k` 硬上限 5：路线写死了"输出 ≤5 个候选文件清单"，
    超了就让调用方显式改代码 —— 而不是悄悄多给几个把成本转嫁给读的人。
    """
    if top_k > DEFAULT_TOP_K:
        raise LocalizeError(f"top_k 最多 {DEFAULT_TOP_K}（路线要求 ≤5 个候选），收到 {top_k}")

    entries = walk_repo(root)
    if not entries:
        return []

    tokens = extract_tokens(issue_text)
    lexical = lexical_scores(root, entries, tokens)

    # 第一级：本地模型从**路径清单**里圈子集（只在清单较大时才值得做）
    shortlist: list[FileEntry] = entries
    if use_model and len(entries) > pool:
        picked = screen_paths([entry.path for entry in entries], issue_text, chat_fn=chat_fn, limit=pool)
        if picked:
            wanted = set(picked)
            shortlist = [entry for entry in entries if entry.path in wanted]
    # 词法分高的文件必须留在池子里：模型漏掉的，词法兜住
    lexical_top = sorted(lexical.items(), key=lambda item: -item[1][0])[:pool]
    for path, _ in lexical_top:
        if all(entry.path != path for entry in shortlist):
            shortlist.append(next(entry for entry in entries if entry.path == path))

    if len(shortlist) > pool * 2:
        shortlist = shortlist[: pool * 2]

    embeddings = embedding_scores(issue_text, root, shortlist, embed_fn=embed_fn)

    max_lexical = max((value[0] for value in lexical.values()), default=1.0) or 1.0
    candidates: list[Candidate] = []
    for entry in shortlist:
        raw_lexical, reasons = lexical.get(entry.path, (0.0, []))
        normalized = raw_lexical / max_lexical
        cosine = embeddings.get(entry.path, 0.0)
        score = 0.6 * normalized + 0.4 * max(cosine, 0.0)
        if not reasons:
            reasons = [f"档案文本与 issue 余弦 {cosine:.3f}"]
        candidates.append(
            Candidate(
                path=entry.path,
                score=score,
                lexical=raw_lexical,
                embedding=cosine,
                why="；".join(reasons),
            )
        )

    candidates.sort(key=lambda item: (-item.score, item.path))
    return candidates[:top_k]


def to_json(candidates: Iterable[Candidate]) -> str:
    """路线要求的产物：候选文件清单（JSON）。"""
    return json.dumps([item.as_dict() for item in candidates], ensure_ascii=False, indent=2)
