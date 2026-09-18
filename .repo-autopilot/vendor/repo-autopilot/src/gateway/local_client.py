"""本机小模型调用客户端（只用 urllib，不用 httpx）。

## 为什么单独一个模块

实测（见 state/reports/）发现一条会浪费大量时间的运行时约束：

    httpx -> https://api.github.com        HTTP 200
    httpx -> http://127.0.0.1:11434        HTTP 502，0 字节，0.1 秒
    httpx -> http://localhost:11434        HTTP 502
    httpx -> http://[::1]:11434            HTTP 502
    urllib -> http://127.0.0.1:11434       HTTP 200

规律：httpx 对本机**明文 HTTP** 一律 502（立即返回、响应体为空、Ollama 日志里
根本没有这次请求），而 urllib 正常。极可能是运行时对本机 HTTP 的拦截层，
只对某些客户端库生效。

**结论**：调本机模型一律走本模块。不要改回 httpx —— 那会让所有本地推理
静默失败（而且失败得很快，看起来像服务端问题，极具误导性）。

注意：访问 GitHub 用 httpx 是正常的，那里不受影响。两个场景分开。
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

DEFAULT_BASE = "http://127.0.0.1:11434"
DEFAULT_MODEL = "qwen3:4b"


@dataclass
class ChatResult:
    content: str
    reasoning: str
    prompt_tokens: int | None
    completion_tokens: int | None
    seconds: float
    raw: dict[str, Any]


class LocalModelError(RuntimeError):
    """本机模型调用失败。绝不吞掉、绝不返回脏数据（路线 0.1 第 4 条）。"""


def _post_json(url: str, payload: dict, timeout: float) -> dict:
    """urllib 版 POST。失败抛 LocalModelError，带可诊断的上下文。"""
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
            return json.loads(body.decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read()[:400].decode("utf-8", errors="replace")
        raise LocalModelError(f"HTTP {exc.code} from {url}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise LocalModelError(f"无法连接 {url}: {exc.reason}") from exc
    except json.JSONDecodeError as exc:
        raise LocalModelError(f"{url} 返回的不是 JSON: {exc}") from exc


def chat(
    messages: list[dict[str, str]],
    *,
    model: str = DEFAULT_MODEL,
    base_url: str = DEFAULT_BASE,
    temperature: float = 0.0,
    think: bool | None = None,
    force_json: bool = False,
    timeout: float = 300.0,
) -> ChatResult:
    """
    走 Ollama 原生 /api/chat（不是 OpenAI 兼容端点）。

    ## 为什么必须用原生端点

    实测（state/reports/json-output-diag.log，同一道三分类题各跑 3 次）：

      | 组合                        | 严格 JSON | 延迟  | token |
      |-----------------------------|-----------|-------|-------|
      | think=false + format=json   |   3/3     | 0.4s  |  20   |
      | format=json（不传 think）    |   3/3     | 16.0s | 1121  |
      | think=false                 |   0/3     | 14.9s | 1049  |
      | think=false + /no_think     |   0/3     | 13.0s |  930  |

    两个反直觉的结论：

    1. **`think=false` 压不住思考链。** qwen3 会把整段中文推理当作 content 正文
       输出（content 以"首先，用户要求我作为…"开头），于是 JSON 解析必然失败。
       它并没有报错，只是安静地给出不可用的输出。
    2. **真正管用的是 `format: "json"`。** 它让 llama.cpp 以 JSON 文法约束解码，
       模型不再走思考链，直接产出 JSON。副作用是巨大的性能提升：
       0.4s / 20 token，对比 16s / 1121 token。

    所以：**凡是要结构化输出，一律带 force_json=True**。这不是优化，是正确性前提。
    """
    payload: dict[str, Any] = {
        "model": model,
        "stream": False,
        "messages": messages,
        "options": {"temperature": temperature},
    }
    if think is not None:
        payload["think"] = think
    if force_json:
        # Ollama 的 JSON 文法约束。见上面的实测表。
        payload["format"] = "json"

    started = time.perf_counter()
    body = _post_json(f"{base_url.rstrip('/')}/api/chat", payload, timeout)
    elapsed = time.perf_counter() - started

    message = body.get("message") or {}
    return ChatResult(
        content=(message.get("content") or "").strip(),
        reasoning=(message.get("thinking") or "").strip(),
        prompt_tokens=body.get("prompt_eval_count"),
        completion_tokens=body.get("eval_count"),
        seconds=elapsed,
        raw=body,
    )


def chat_json(
    messages: list[dict[str, str]],
    *,
    model: str = DEFAULT_MODEL,
    base_url: str = DEFAULT_BASE,
    think: bool | None = False,
    timeout: float = 300.0,
    retries: int = 3,
) -> ChatResult:
    """
    结构化输出调用：强制 JSON 文法 + 关思考链。

    默认 think=False 是双保险：format 已经能压住思考链，但显式关掉更清楚，
    也便于将来换模型时对比行为。
    """
    last: Exception | None = None
    for attempt in range(retries):
        try:
            return chat(
                messages,
                model=model,
                base_url=base_url,
                think=think,
                force_json=True,
                timeout=timeout,
            )
        except LocalModelError as exc:
            last = exc
            time.sleep(2**attempt)   # 1s / 2s / 4s
    raise LocalModelError(f"{retries} 次尝试均失败，最后一次：{last}")


def embed(texts: list[str], *, model: str = "bge-m3:latest", base_url: str = DEFAULT_BASE) -> list[list[float]]:
    """embedding 调用。同样只走 urllib。"""
    body = _post_json(
        f"{base_url.rstrip('/')}/api/embed",
        {"model": model, "input": texts},
        120.0,
    )
    vectors = body.get("embeddings")
    if not isinstance(vectors, list) or len(vectors) != len(texts):
        raise LocalModelError(f"embed 返回结构异常: {str(body)[:200]}")
    return vectors


def available_models(base_url: str = DEFAULT_BASE) -> list[str]:
    """列模型。用于启动前的探活，不依赖模型是否已加载。"""
    try:
        with urllib.request.urlopen(f"{base_url.rstrip('/')}/api/tags", timeout=15) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        return [m["name"] for m in body.get("models", [])]
    except Exception as exc:
        raise LocalModelError(f"无法列出模型: {exc}") from exc
