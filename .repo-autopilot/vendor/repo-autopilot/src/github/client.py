"""GitHub REST 封装（路线 1.4 第 1 条）。

## 读直通、写必过闸

* 读操作（issues / PR / 代码 / 搜索）直接放行，统一套 `with_backoff()`：
  超时 30s；5xx 与网络错退避重试 ≤5 次；403 + `Retry-After` 睡到指定秒数；
  **401/402 不重试**，转成凭证告警（与 1.3 同一份 `auth_failure.md`）。
* 写操作**在这里没有便捷入口**。它们只能作为 `perform` 回调传给
  `approval.execute_if_approved()`。少一个入口，就少一条绕过闸门的路 ——
  这是本模块刻意"少写代码"的地方。
"""

from __future__ import annotations

import http.client
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..gateway import write_auth_alert
from .tokens import read_token, redact

DEFAULT_BASE_URL = "https://api.github.com"
DEFAULT_TIMEOUT = 30.0
MAX_ATTEMPTS = 5
BACKOFF_BASE = 1.0
AUTH_STATUSES = (401, 402)
RETRYABLE_STATUSES = frozenset({500, 502, 503, 504})


class GitHubError(RuntimeError):
    """GitHub 调用失败。绝不吞掉、绝不返回半截数据。"""


class TransportError(GitHubError):
    """连不上（超时、DNS、连接被拒）。**可重试**。"""


@dataclass
class Response:
    status: int
    headers: dict[str, str]
    body: Any
    url: str


def _http(
    method: str,
    url: str,
    *,
    headers: dict[str, str],
    body: dict[str, Any] | None,
    timeout: float,
) -> Response:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
            head = {k.lower(): v for k, v in response.headers.items()}
            status = response.status
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        head = {k.lower(): v for k, v in (exc.headers or {}).items()}
        status = exc.code
    except http.client.IncompleteRead as exc:
        # 大响应（比如 base64 的 README）偶尔会被截断。这是**传输问题**，
        # 不是"这个请求本身有问题" —— 当成可重试的传输错误，交给上层退避重试。
        # （实测踩过：猎手取一个仓库的 README 时整个评测崩在 IncompleteRead 上。）
        raise TransportError(f"{method} {url} 响应被截断：{exc}") from exc
    except (urllib.error.URLError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        raise TransportError(f"{method} {url} 连不上：{reason}") from exc

    try:
        parsed = json.loads(raw) if raw else None
    except json.JSONDecodeError:
        parsed = raw
    return Response(status=status, headers=head, body=parsed, url=url)


def retry_after_seconds(headers: dict[str, str]) -> float | None:
    raw = headers.get("retry-after")
    if raw is None:
        return None
    try:
        return max(0.0, float(str(raw).strip()))
    except ValueError:
        return None


def with_backoff(
    send: Callable[[], Response],
    *,
    attempts: int = MAX_ATTEMPTS,
    base_delay: float = BACKOFF_BASE,
    sleep: Callable[[float], None] = time.sleep,
    on_auth_failure: Callable[[Response], None] | None = None,
) -> Response:
    """
    统一的读操作退避包装。

    三类结果分开处理，因为它们要的是**不同的**反应：
      * 401/402 → 凭证问题：**重试没有意义**，立刻告警并抛出；
      * 403 + Retry-After → 限流：睡到对方指定的秒数再试，不要自作聪明地缩短；
      * 5xx / 连不上 → 真·瞬时故障：指数退避重试。

    最后一条 `else: return` 保证 4xx（比如 404）**原样返回**给调用方判断 ——
    "仓库不存在"不是故障，不该被重试逻辑吃掉。
    """
    last_error: Exception | None = None

    for attempt in range(1, attempts + 1):
        try:
            response = send()
        except TransportError as exc:
            last_error = exc
            if attempt >= attempts:
                break
            sleep(base_delay * (2 ** (attempt - 1)))
            continue

        if response.status in AUTH_STATUSES:
            if on_auth_failure is not None:
                on_auth_failure(response)
            raise GitHubError(
                f"凭证被拒（HTTP {response.status}）：{response.url}。"
                "按路线这不是断联，不重试，已写 auth_failure.md 告警。"
            )

        if response.status == 403:
            delay = retry_after_seconds(response.headers)
            if delay is not None and attempt < attempts:
                sleep(delay)
                continue

        if response.status in RETRYABLE_STATUSES:
            last_error = GitHubError(f"HTTP {response.status} from {response.url}")
            if attempt >= attempts:
                break
            sleep(base_delay * (2 ** (attempt - 1)))
            continue

        return response

    raise GitHubError(f"{attempts} 次尝试后仍失败：{last_error}")


class GitHubClient:
    """读操作客户端。写操作请走 `approval.execute_if_approved()`。"""

    def __init__(
        self,
        token: str | None = None,
        *,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = DEFAULT_TIMEOUT,
        attempts: int = MAX_ATTEMPTS,
        sleep: Callable[[float], None] = time.sleep,
        auth_report: Path | None = None,
    ) -> None:
        self._token = token
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.attempts = attempts
        self.sleep = sleep
        self.auth_report = auth_report

    # -------------------------------------------------------------- 基础

    def token(self) -> str:
        """延迟取 token：取不到就抛，绝不带着空凭证去打 API。"""
        return self._token or read_token()

    def headers(self) -> dict[str, str]:
        return {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self.token()}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "repo-autopilot",
        }

    def _alert(self, response: Response) -> None:
        write_auth_alert([("github", f"HTTP {response.status} @ {response.url}")], self.auth_report)

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
        base_url: str | None = None,
    ) -> Response:
        root = (base_url or self.base_url).rstrip("/")
        url = path if path.startswith("http") else f"{root}/{path.lstrip('/')}"
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"
        headers = self.headers()
        return with_backoff(
            lambda: _http(method, url, headers=headers, body=body, timeout=self.timeout),
            attempts=self.attempts,
            sleep=self.sleep,
            on_auth_failure=self._alert,
        )

    def get(self, path: str, *, params: dict[str, Any] | None = None) -> Any:
        response = self.request("GET", path, params=params)
        if response.status >= 400:
            raise GitHubError(f"GET {response.url} -> HTTP {response.status}: {str(response.body)[:300]}")
        return response.body

    def get_json(self, path: str, *, params: dict[str, Any] | None = None) -> Any:
        """同 `get`，但把 3xx 也当成错误（避免跟随重定向拿到一页 HTML）。"""
        return self.get(path, params=params)

    # ---------------------------------------------------------- 常用读接口

    def viewer(self) -> dict[str, Any]:
        return self.get("/user")

    def rate_limit(self) -> dict[str, Any]:
        return self.get("/rate_limit")

    def repo(self, full_name: str) -> dict[str, Any]:
        return self.get(f"/repos/{full_name}")

    def list_issues(self, full_name: str, **params: Any) -> list[dict[str, Any]]:
        defaults = {"state": "open", "per_page": 50}
        defaults.update(params)
        return self.get(f"/repos/{full_name}/issues", params=defaults)

    def issue(self, full_name: str, number: int) -> dict[str, Any]:
        return self.get(f"/repos/{full_name}/issues/{number}")

    def search_issues(self, query: str, **params: Any) -> dict[str, Any]:
        defaults = {"q": query, "per_page": 30}
        defaults.update(params)
        return self.get("/search/issues", params=defaults)

    def search_code(self, query: str, **params: Any) -> dict[str, Any]:
        defaults = {"q": query, "per_page": 30}
        defaults.update(params)
        return self.get("/search/code", params=defaults)


def safe_detail(text: str, *secrets: str) -> str:
    """把要写进日志/报告的文本先脱敏。忘记调用它 = 悄悄泄漏。"""
    return redact(text, *secrets)
