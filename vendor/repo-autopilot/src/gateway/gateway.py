"""LLM 网关（路线 1.3）。

## 职责

全系统只有这一个地方直接跟模型说话：

1. `chat(messages, schema, tier)` —— 结构化输出。过 JSON Schema 校验；不合规就重试
   （1s/2s/4s 指数退避，最多 3 次重试），仍不合规抛 `SchemaError`。
   **绝不返回脏数据，也不解析自由文本凑数**（路线 0.1 第 4 条）。
2. `embed(texts)` —— L2 归一化的向量，返回 `np.ndarray`。
3. 记账 —— 每次调用（含失败的尝试）追加一行到 `state/reports/token_usage.log`。
4. `connectivity_check()` —— 探 GitHub 与模型端点，连续 3 次失败切 `offline`，
   连续 2 次成功切回 `online`；**401/402 不算断联**：写 `reports/auth_failure.md`
   告警人类，且**不重试**。

## 与路线写的差异（实测所得，非擅自改动）

路线写「OpenAI 兼容调用」。本机有一条硬约束（详见 `local_client` 模块头）：
httpx 对本机明文 HTTP 一律 502/0 字节，只有 urllib 能通。
因此网关**统一用 urllib** 发 HTTP；消息体仍是 OpenAI 风格，只是底层不是 httpx。
本机档位另外走 Ollama **原生** `/api/chat`，因为结构化输出必须带 `format: "json"`
（实测 0.4s/20 token，对比 16s/1121 token）。

## 为什么档位带 api_style

`config/models.yaml` 每个档位声明 `api_style: ollama | openai`。
把它写成显式配置而不是"猜到端口就是 Ollama"，是为了让换模型时只改配置、不改代码。
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import numpy as np
import yaml
from jsonschema import Draft202012Validator
from jsonschema import exceptions as jsonschema_exceptions

from .local_client import LocalModelError
from .local_client import chat as local_chat
from .local_client import embed as local_embed

ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = ROOT / "config" / "models.yaml"
STATE_DIR = ROOT / "state"
REPORTS_DIR = STATE_DIR / "reports"
#: **运行期账簿与证据分开放**（2026-09-12 整理）：`reports/` 是给人看的证据（验收日志、
#: 评测报告、BLOCKED 报告），而记账是**只追加的运行期数据**（每次调用一行，跑得越久越大）。
#: 把两者混在一个目录里，结果是"证据树"里混进一个几百 KB 且天天变大的机器账本，
#: 谁都不知道它该不该进版本库。所以账簿搬到 `state/runtime/`（已 gitignore）。
RUNTIME_DIR = STATE_DIR / "runtime"
MODE_PATH = STATE_DIR / "mode.json"
USAGE_LOG = RUNTIME_DIR / "token_usage.log"
AUTH_FAILURE_REPORT = REPORTS_DIR / "auth_failure.md"

# 路线 1.3 子步骤 2：失败重试 <=3 次，指数退避 1s/2s/4s。
# 3 个退避值 => 最多 4 次调用（1 次首发 + 3 次重试）。
RETRY_DELAYS: tuple[float, ...] = (1.0, 2.0, 4.0)

# 路线 1.3 子步骤 5 的状态机阈值
OFFLINE_AFTER_FAILURES = 3
ONLINE_AFTER_SUCCESSES = 2

GITHUB_PROBE_URL = "https://api.github.com/rate_limit"
AUTH_STATUSES = (401, 402)

#: flash 档的 API key 从哪来。顺序与读 token 一致：环境变量 → DSH_HOME 下的文件。
#: **刻意不放在仓库里** —— 它一旦进了 git，历史里删不掉，只能轮换。
API_KEY_ENV = "DEEPSEEK_API_KEY"
API_KEY_FILE = "deepseek_key"


def load_api_key(name: str = API_KEY_ENV, filename: str = API_KEY_FILE) -> tuple[str, str]:
    """
    取 API key，返回 (key, 来源说明)。取不到返回空串。

    只回报来源，绝不回报 key 本身。
    """
    env_value = os.environ.get(name, "").strip()
    if env_value:
        return env_value, f"环境变量 {name}"

    candidate = Path(os.environ.get("DSH_HOME") or r"D:\dsh\home") / f".{filename}"
    if candidate.is_file():
        lines = candidate.read_text(encoding="utf-8", errors="replace").splitlines()
        if lines and lines[0].strip():
            return lines[0].strip(), str(candidate)

    return "", f"未找到（环境变量 {name} 为空，且 {candidate} 不存在）"


class GatewayError(RuntimeError):
    """网关层失败。绝不吞掉、绝不返回脏数据。"""


class SchemaError(GatewayError):
    """模型输出连续多次不符合 schema。路线要求此时抛错，而不是凑数。"""


class AuthError(GatewayError):
    """401/402。这类错误**不是断联**，要立即告警人类且不重试。"""


class _SchemaViolation(GatewayError):
    """内部信号：本次输出不合规，应当重试。不对外暴露。"""


# ----------------------------------------------------------------- 配置

@dataclass(frozen=True)
class TierConfig:
    name: str
    base_url: str
    model: str
    temperature: float
    api_style: str
    #: 期望的向量维度（只有 embedding 档位有）。配了就在 `embed()` 里**硬校验** ——
    #: 换 embedding 模型而维度变了的话，`config/models.yaml` 的 dedup 阈值会**静默失效**
    #: （不报错、只是相似度全错），那正是本项目最不能接受的一类 bug。
    dim: int | None = None


def load_config(path: Path | None = None) -> dict[str, Any]:
    source = path or CONFIG_PATH
    return yaml.safe_load(source.read_text(encoding="utf-8")) or {}


def tier_config(tier: str, config: dict[str, Any] | None = None) -> TierConfig:
    data = config if config is not None else load_config()
    tiers = data.get("tiers") or {}
    if tier not in tiers:
        raise GatewayError(f"未知档位 {tier!r}；config/models.yaml 里只有 {sorted(tiers)}")
    raw = tiers[tier] or {}
    base_url = str(raw.get("base_url") or "").rstrip("/")
    if not base_url:
        raise GatewayError(f"档位 {tier!r} 没配 base_url")
    return TierConfig(
        name=tier,
        base_url=base_url,
        model=str(raw.get("model") or ""),
        temperature=float(raw.get("temperature") or 0.0),
        api_style=str(raw.get("api_style") or "openai"),
    )


#: embedding 默认用哪个档位。写死默认值是为了**向后兼容**：
#: 老配置里没有 `embed.active` 时行为与从前完全一致。
EMBED_DEFAULT_PROFILE = "local"


def embed_config(config: dict[str, Any] | None = None, *, profile: str | None = None) -> TierConfig:
    """
    取 embedding 档位配置。

    `config/models.yaml` 里 `embed` 下可以有多个档位（`local` / `api` …），
    用 `embed.active` 选当前用哪个；`profile` 参数可以就地覆盖（测试与临时切换用）。
    没有 `embed.active` 时退回 `local`，所以**老配置不受影响**。
    """
    data = config if config is not None else load_config()
    section = data.get("embed") or {}
    if not isinstance(section, dict):
        raise GatewayError("config/models.yaml 的 embed 段必须是映射")

    chosen = profile or str(section.get("active") or EMBED_DEFAULT_PROFILE)
    raw = section.get(chosen)
    if raw is None:
        # 档位名拼错时**响亮报错并列出可选项**，绝不静默退回 local ——
        # 静默退回会让人以为在用 API，其实一直在打本机 11434。
        available = sorted(key for key, value in section.items() if isinstance(value, dict))
        raise GatewayError(
            f"config/models.yaml 里没有 embed.{chosen} 档位（可选：{'、'.join(available) or '无'}）"
        )
    if not isinstance(raw, dict):
        raise GatewayError(f"config/models.yaml 的 embed.{chosen} 必须是映射")

    base_url = str(raw.get("base_url") or "").rstrip("/")
    if not base_url:
        raise GatewayError(f"config/models.yaml 缺少 embed.{chosen}.base_url")

    dim = raw.get("dim")
    return TierConfig(
        name=f"embed.{chosen}",
        base_url=base_url,
        model=str(raw.get("model") or "bge-m3:latest"),
        temperature=0.0,
        api_style=str(raw.get("api_style") or "ollama"),
        dim=int(dim) if dim is not None else None,
    )


# --------------------------------------------------------------- 记账

def log_usage(entry: dict[str, Any], path: Path | None = None) -> None:
    """追加一行调用记录。失败的尝试也要记 —— 否则成本账会漏掉重试。"""
    target = path or USAGE_LOG
    target.parent.mkdir(parents=True, exist_ok=True)
    record = {"at": time.strftime("%Y-%m-%dT%H:%M:%S"), **entry}
    with open(target, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


# --------------------------------------------------------------- 校验

def validate_against(schema: Any, payload: Any) -> None:
    """
    用 schema 校验 payload；不合规抛内部的 `_SchemaViolation`。

    schema 支持两种写法，都指向同一件事「输出必须机器可验」：
      * JSON Schema `dict`（用 jsonschema 校验），
      * pydantic 模型类（用 model_validate 校验）。
    schema 为 None 时只要求是 JSON 对象。
    """
    if schema is None:
        return
    if isinstance(schema, dict):
        try:
            Draft202012Validator(schema).validate(payload)
        except jsonschema_exceptions.ValidationError as exc:
            location = "/".join(str(part) for part in exc.absolute_path) or "(根)"
            raise _SchemaViolation(f"不符合 schema @ {location}: {exc.message}") from exc
        return
    model_validate = getattr(schema, "model_validate", None)
    if model_validate is None:
        raise GatewayError(f"不认识的 schema 类型: {type(schema)!r}")
    try:
        model_validate(payload)
    except Exception as exc:
        raise _SchemaViolation(f"pydantic 校验失败: {exc}") from exc


# --------------------------------------------------------------- HTTP

def request_json(
    url: str,
    payload: dict[str, Any] | None = None,
    *,
    timeout: float = 300.0,
    extra_headers: dict[str, str] | None = None,
) -> tuple[int, Any]:
    """
    发一次请求，返回 (状态码, 解析后的 body 或原始文本)。

    刻意**不**在 4xx/5xx 上抛异常：401/402 必须能被上层区分出来（那不是断联）。
    连不上（URLError）才抛 —— 那是运输层失败。
    """
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if extra_headers:
        headers.update(extra_headers)
    request = urllib.request.Request(
        url,
        data=data,
        headers=headers,
        method="POST" if data is not None else "GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8", errors="replace")
            status = response.status
    except urllib.error.HTTPError as exc:
        raw = exc.read()[:400].decode("utf-8", errors="replace")
        try:
            return exc.code, json.loads(raw)
        except json.JSONDecodeError:
            return exc.code, raw
    except urllib.error.URLError as exc:
        raise GatewayError(f"无法连接 {url}: {exc.reason}") from exc
    except OSError as exc:
        raise GatewayError(f"请求 {url} 失败: {exc}") from exc

    try:
        return status, json.loads(body)
    except json.JSONDecodeError:
        return status, body


@dataclass
class Completion:
    content: str
    prompt_tokens: int | None = None
    completion_tokens: int | None = None


Transport = Callable[[list[dict[str, str]], TierConfig, bool], Completion]


def ollama_transport(messages: list[dict[str, str]], cfg: TierConfig, want_json: bool) -> Completion:
    """本机档位：走 Ollama 原生 /api/chat（结构化输出必须带 format=json）。"""
    try:
        result = local_chat(
            messages,
            model=cfg.model,
            base_url=cfg.base_url,
            temperature=cfg.temperature,
            think=False if want_json else None,
            force_json=want_json,
        )
    except LocalModelError as exc:
        raise GatewayError(f"本机模型调用失败（{cfg.model} @ {cfg.base_url}）：{exc}") from exc
    return Completion(result.content, result.prompt_tokens, result.completion_tokens)


def openai_transport(messages: list[dict[str, str]], cfg: TierConfig, want_json: bool) -> Completion:
    """
    远程档位：OpenAI 兼容 /chat/completions（HTTP 层是 urllib，理由见模块头）。

    没有 key 时**立刻报清楚**，而不是发一个不带 Authorization 的请求去换一个 401 ——
    401 会被上层当成"凭证被拒"写进 auth_failure.md，于是真正的病因
    （根本没配 key）被一条错误告警盖住了。
    """
    key, source = load_api_key()
    if not key:
        raise GatewayError(
            f"档位 {cfg.name} 需要 API key：{source}。"
            f"把 key 放进环境变量 {API_KEY_ENV}，或写到 $DSH_HOME/.{API_KEY_FILE}（首行）。"
        )

    payload: dict[str, Any] = {
        "model": cfg.model,
        "messages": messages,
        "temperature": cfg.temperature,
    }
    if want_json:
        payload["response_format"] = {"type": "json_object"}
    status, body = request_json(
        f"{cfg.base_url}/chat/completions",
        payload,
        extra_headers={"Authorization": f"Bearer {key}"},
    )
    if status in AUTH_STATUSES:
        raise AuthError(f"{cfg.base_url} 返回 {status}：凭证无效或余额不足")
    if status != 200:
        raise GatewayError(f"{cfg.base_url} 返回 HTTP {status}: {str(body)[:300]}")
    if not isinstance(body, dict):
        raise GatewayError(f"{cfg.base_url} 返回的不是 JSON 对象: {str(body)[:200]}")
    choices = body.get("choices") or []
    if not choices:
        raise GatewayError(f"{cfg.base_url} 响应里没有 choices: {str(body)[:200]}")
    content = ((choices[0].get("message") or {}).get("content")) or ""
    usage = body.get("usage") or {}
    return Completion(str(content).strip(), usage.get("prompt_tokens"), usage.get("completion_tokens"))


def default_transport(cfg: TierConfig) -> Transport:
    return ollama_transport if cfg.api_style == "ollama" else openai_transport


# --------------------------------------------------------------- chat

def _parse_json_object(text: str) -> dict[str, Any] | None:
    """
    严格解析。**不做**"从散文里抠 JSON"这种事 —— 那就是路线禁止的"解析自由文本凑数"。
    解析不出来就当成违规，交给重试。
    """
    stripped = text.strip()
    if not stripped:
        return None
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def chat(
    messages: list[dict[str, str]],
    schema: Any = None,
    tier: str = "local_small",
    *,
    config: dict[str, Any] | None = None,
    transport: Transport | None = None,
    sleep: Callable[[float], None] = time.sleep,
    retry_delays: Sequence[float] = RETRY_DELAYS,
    usage_path: Path | None = None,
    temperature: float | None = None,
) -> dict[str, Any]:
    """
    结构化输出调用。

    **返回值是已经过 schema 校验的 `dict`，不是 pydantic 模型实例**（D7：实测有调用方
    按名字以为是模型实例，于是 `result.question` 直接 AttributeError）。要模型对象就自己
    `Model.model_validate(结果)`；`src.spec.refiner` 就是这么做的。

    重试只针对**输出不合规**（不是 JSON 对象，或不符合 schema）。
    运输层失败（连不上、HTTP 5xx）直接抛，重试它只会拖长故障时间。

    `retry_delays` / `sleep` / `usage_path` 是给验收测试注入用的：
    测试要断言「重试了 3 次、退避是 1/2/4」，但不必真的等 7 秒。
    默认值就是路线规定的那三个数。
    """
    cfg = tier_config(tier, config)
    if temperature is not None and temperature != cfg.temperature:
        # 覆盖档位自带的温度。用途很具体：路线 0.4 规定"分类与打分一律温度 0"，
        # 而 flash 档在配置里是 0.2（给生成任务用的）。同一个档位服务两类任务时，
        # 温度必须由**调用方**按任务性质指定，而不是由档位一锤定音。
        cfg = replace(cfg, temperature=temperature)
    call = transport or default_transport(cfg)
    delays = tuple(retry_delays)
    attempts = len(delays) + 1
    last_violation: str | None = None

    for attempt in range(attempts):
        started = time.perf_counter()
        try:
            completion = call(messages, cfg, schema is not None)
        except Exception as exc:
            # 运输层失败（超时 / DNS 解析不了 / 连接被重置）也要记一笔再抛。
            #
            # 为什么：路线 0.1 第 4 条与 1.3 的账本要求"记下每一次尝试" ——
            # 只有合规失败（模型给了 JSON 但不合 schema）才记账的话，
            # "模型根本没答上话"这一类**恰恰是最贵、最该被看见**的尝试会一条都不留，
            # 于是周检看到的成本曲线是平的，而事实上端点正在整片超时。
            # 混沌演练（tools/chaos_weekly.py）就是这么抓到这条漏记的。
            log_usage(
                {
                    "tier": cfg.name,
                    "model": cfg.model,
                    "api_style": cfg.api_style,
                    "attempt": attempt + 1,
                    "ok": False,
                    "violation": f"运输层失败：{type(exc).__name__}: {str(exc)[:160]}",
                    "prompt_tokens": None,
                    "completion_tokens": None,
                    "latency_s": round(time.perf_counter() - started, 3),
                },
                usage_path,
            )
            raise
        elapsed = time.perf_counter() - started

        parsed = _parse_json_object(completion.content)
        violation: str | None = None
        if parsed is None:
            violation = f"输出不是 JSON 对象: {completion.content[:200]!r}"
        else:
            try:
                validate_against(schema, parsed)
            except _SchemaViolation as exc:
                violation = str(exc)

        log_usage(
            {
                "tier": cfg.name,
                "model": cfg.model,
                "api_style": cfg.api_style,
                "attempt": attempt + 1,
                "ok": violation is None,
                "violation": violation,
                "prompt_tokens": completion.prompt_tokens,
                "completion_tokens": completion.completion_tokens,
                "latency_s": round(elapsed, 3),
            },
            usage_path,
        )

        if violation is None:
            # violation 为 None 等价于"parsed 是 dict 且过了校验"，但类型检查器
            # 看不出来：设置 violation 的两条分支都在 parsed 判断之后。
            assert parsed is not None
            return parsed

        last_violation = violation
        if attempt < len(delays):
            sleep(delays[attempt])

    raise SchemaError(
        f"{attempts} 次尝试均未产出符合 schema 的输出（档位 {cfg.name}，模型 {cfg.model}）。"
        f"最后一次：{last_violation}"
    )


# --------------------------------------------------------------- embed

def _order_by_index(data: list[Any]) -> list[Any]:
    """
    按 `index` 把 embedding 归位。

    OpenAI 兼容端点**不保证**返回顺序与输入一致（规范里是"可以乱序、靠 index 对齐"）。
    不归位的话，向量和文本会错配 —— 而错配**不会报错**，只会让查重结果悄悄变错。
    全都有 index 才排序；缺 index 就按原顺序（并保持形状校验兜底）。
    """
    if data and all(isinstance(item, dict) and "index" in item for item in data):
        return sorted(data, key=lambda item: item["index"])
    return data


def openai_embed(texts: list[str], cfg: TierConfig) -> list[list[float]]:
    """
    远程档位：OpenAI 兼容 `/embeddings`。

    这是**本机小模型（Ollama + bge-m3）的替代接法**：没有显卡、不想常驻 Ollama、
    或者要用更强的 embedding 时，把 `config/models.yaml` 的 `embed.active` 指到 `api` 即可。

    没有 key 时**立刻报清楚**（与 chat 侧同一个理由）：绝不发一个不带 Authorization
    的请求去换一个 401 —— 401 会被上层当成"凭证被拒"，把真正的病因（根本没配 key）盖住。
    """
    key, source = load_api_key()
    if not key:
        raise GatewayError(
            f"embedding 档位 {cfg.name} 需要 API key：{source}。"
            f"把 key 放进环境变量 {API_KEY_ENV}，或写到 $DSH_HOME/.{API_KEY_FILE}（首行）。"
        )

    status, body = request_json(
        f"{cfg.base_url}/embeddings",
        {"model": cfg.model, "input": list(texts)},
        extra_headers={"Authorization": f"Bearer {key}"},
    )
    if status in AUTH_STATUSES:
        raise AuthError(f"{cfg.base_url} 返回 {status}：凭证无效或余额不足")
    if status != 200:
        raise GatewayError(f"{cfg.base_url} 返回 HTTP {status}: {str(body)[:300]}")
    if not isinstance(body, dict):
        raise GatewayError(f"{cfg.base_url} 返回的不是 JSON 对象: {str(body)[:200]}")

    data = body.get("data") or []
    if not isinstance(data, list) or len(data) != len(texts):
        raise GatewayError(
            f"embedding 条数对不上：要 {len(texts)} 条，{cfg.base_url} 返回 {len(data) if isinstance(data, list) else '非列表'}"
        )
    return [list((item or {}).get("embedding") or []) for item in _order_by_index(data)]


def embed(
    texts: Sequence[str],
    *,
    config: dict[str, Any] | None = None,
    profile: str | None = None,
    raw_embed: Callable[[list[str]], list[list[float]]] | None = None,
) -> np.ndarray:
    """
    取 embedding 并做 L2 归一化（路线 1.3 子步骤 3）。

    归一化放在网关而不是调用方：相似度阈值是按归一化向量标定的，
    散落在各处做归一化迟早会有一处漏掉，而那种错误不会报错、只会让阈值失效。

    **按 `api_style` 分派**（`ollama` → 本机 Ollama；`openai` → OpenAI 兼容 API）。
    这条分派以前是缺的：`embed()` 无论配置写什么都在调本机 Ollama，
    于是配置里写 `api_style: openai` 的人会一直打 `127.0.0.1:11434` 拿到一个莫名其妙的失败。
    """
    cfg = embed_config(config, profile=profile)
    texts = list(texts)
    if not texts:
        return np.zeros((0, 0), dtype=float)

    if raw_embed is not None:
        vectors = raw_embed(texts)
    elif cfg.api_style == "ollama":
        try:
            vectors = local_embed(texts, model=cfg.model, base_url=cfg.base_url)
        except LocalModelError as exc:
            raise GatewayError(f"embedding 调用失败（{cfg.model} @ {cfg.base_url}）：{exc}") from exc
    elif cfg.api_style == "openai":
        vectors = openai_embed(texts, cfg)
    else:
        raise GatewayError(
            f"embed 档位 {cfg.name} 的 api_style 不认识：{cfg.api_style!r}（只支持 ollama / openai）"
        )

    array = np.asarray(vectors, dtype=float)
    if array.ndim != 2 or array.shape[0] != len(texts):
        raise GatewayError(f"embedding 返回形状异常: {array.shape}，期望 ({len(texts)}, dim)")
    if cfg.dim is not None and array.shape[1] != cfg.dim:
        raise GatewayError(
            f"embedding 维度是 {array.shape[1]}，而 config/models.yaml 的 embed 档位写的是 {cfg.dim}。"
            f"换 embedding 模型**必须重标定** dedup 阈值（不得沿用默认值）—— 否则阈值会静默失效。"
        )
    norms = np.linalg.norm(array, axis=1, keepdims=True)
    if np.any(norms == 0):
        raise GatewayError("embedding 里出现零向量，无法归一化（这会让相似度全部变成 0）")
    return array / norms


# --------------------------------------------------------- 连通性状态机

@dataclass
class ProbeResult:
    name: str
    ok: bool
    detail: str
    auth_failure: bool = False


def model_probe(base_url: str, *, timeout: float = 10.0) -> ProbeResult:
    """轻量探活：列模型。不依赖模型是否已加载，也不消耗推理。"""
    try:
        status, body = request_json(f"{base_url.rstrip('/')}/api/tags", None, timeout=timeout)
    except GatewayError as exc:
        return ProbeResult("model", False, str(exc))
    if status in AUTH_STATUSES:
        return ProbeResult("model", False, f"HTTP {status}", auth_failure=True)
    if status != 200:
        return ProbeResult("model", False, f"HTTP {status}: {str(body)[:120]}")
    models = [m.get("name") for m in (body.get("models") or [])] if isinstance(body, dict) else []
    return ProbeResult("model", True, f"HTTP 200，模型 {len(models)} 个")


def github_probe(url: str = GITHUB_PROBE_URL, *, token: str | None = None, timeout: float = 10.0) -> ProbeResult:
    """探 GitHub。401/402 标记为 auth_failure —— 那是凭证问题，不是断网。"""
    request = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json"})
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return ProbeResult("github", True, f"HTTP {response.status}")
    except urllib.error.HTTPError as exc:
        if exc.code in AUTH_STATUSES:
            return ProbeResult("github", False, f"HTTP {exc.code}", auth_failure=True)
        return ProbeResult("github", False, f"HTTP {exc.code}")
    except (urllib.error.URLError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        return ProbeResult("github", False, f"连不上: {reason}")


def read_mode(path: Path | None = None) -> dict[str, Any]:
    target = path or MODE_PATH
    if not target.exists():
        return {"mode": "online"}
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"mode": "online"}
    return data if isinstance(data, dict) else {"mode": "online"}


def write_mode(state: dict[str, Any], path: Path | None = None) -> None:
    target = path or MODE_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_auth_alert(entries: Sequence[tuple[str, str]], report: Path | None = None) -> Path:
    """
    写凭证告警（401/402）。

    **这不是断联**：不重试、不切 offline，只告警人类去换凭证。

    做成公开函数是因为 GitHub 客户端（步骤 1.4）也要写同一种告警 ——
    两处必须格式一致，否则人类会在两个地方读到两种说法。
    """
    target = report or AUTH_FAILURE_REPORT
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        "\n".join(
            [
                "# 凭证告警（401/402）",
                "",
                f"- 时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
                (
                    "- 判定：**这不是断联**。401/402 不重试、不切 offline，"
                    "立即告警人类去换凭证。"
                ),
                "",
                "## 现场",
                "",
                *[f"- `{name}`: {detail}" for name, detail in entries],
                "",
                "## 要做的事",
                "",
                "1. 检查对应 token 是否过期/被撤销；",
                "2. 若为 402，检查账户余额；",
                "3. 换好凭证后重跑 `state/run-acceptance.cmd --all`。",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return target


def _write_auth_failure(results: list[ProbeResult], report: Path | None = None) -> Path:
    bad = [r for r in results if r.auth_failure]
    return write_auth_alert([(r.name, r.detail) for r in bad], report)


def connectivity_check(
    *,
    probes: Sequence[Callable[[], ProbeResult]] | None = None,
    mode_path: Path | None = None,
    auth_report: Path | None = None,
    config: dict[str, Any] | None = None,
    model_base_url: str | None = None,
) -> dict[str, Any]:
    """
    探活并推进 online/offline 状态机。

    「连续」必须跨调用保留，所以计数写进 mode.json：
        {"mode": "offline", "since": "...", "fail_streak": 3, "ok_streak": 0}

    返回本次的处置，便于调用方与验收断言。
    """
    if probes is None:
        base = model_base_url or tier_config("local_small", config).base_url
        probes = [github_probe, lambda: model_probe(base)]

    results = [probe() for probe in probes]

    auth_failures = [r for r in results if r.auth_failure]
    if auth_failures:
        report = _write_auth_failure(results, auth_report)
        state = read_mode(mode_path)
        return {
            "mode": state.get("mode", "online"),
            "changed": False,
            "reason": "auth_failure",
            "auth_report": str(report),
            "probes": [r.__dict__ for r in results],
        }

    state = read_mode(mode_path)
    previous = state.get("mode", "online")
    all_ok = all(r.ok for r in results)

    if all_ok:
        state["ok_streak"] = int(state.get("ok_streak") or 0) + 1
        state["fail_streak"] = 0
        if previous == "offline" and state["ok_streak"] >= ONLINE_AFTER_SUCCESSES:
            state["mode"] = "online"
            state.pop("since", None)
            state["recovered_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
            state["fail_streak"] = 0
    else:
        state["fail_streak"] = int(state.get("fail_streak") or 0) + 1
        state["ok_streak"] = 0
        if previous != "offline" and state["fail_streak"] >= OFFLINE_AFTER_FAILURES:
            state["mode"] = "offline"
            state["since"] = time.strftime("%Y-%m-%dT%H:%M:%S")

    write_mode(state, mode_path)
    return {
        "mode": state.get("mode", "online"),
        "changed": state.get("mode", "online") != previous,
        "reason": "probes_ok" if all_ok else "probes_failed",
        "fail_streak": state.get("fail_streak", 0),
        "ok_streak": state.get("ok_streak", 0),
        "probes": [r.__dict__ for r in results],
    }
