"""8.2 ② 的**每周一次**：混沌故障注入。

    python tools/chaos_weekly.py                  # 全部注入式故障，离线可跑
    python tools/chaos_weekly.py --out-dir D:\\tmp\\chaos
    python tools/chaos_weekly.py --json           # 只打印 JSON（前端/脚本消费）

产物（路线 8.3 的「混沌演练，每周」那一行的证据）：

    state/reports/weekly/chaos-<YYYY-MM-DD>.json
    state/reports/weekly/chaos-<YYYY-MM-DD>.md

退出码与 `tools/dirty_weekly.py` 同语义：0 = 全过 / 1 = 有失败 / 2 = 缺件。
**缺件单独一档**：把"压根没跑起来"混进"降级没生效"里，读日志的人会去改重试策略，
而真正的问题是他的机器上根本没有解释器或配置文件。

## 一条贯穿全篇的纪律：故障是**注入**的，不是造出来的

路线 8.2 ② 的原话是"磁盘写满 90% 的优雅降级""git 操作中途 kill -9"。
真把磁盘写满、真杀本机进程，在这台机器上等于**把工作环境砸了再测**：
杀错一个进程，整轮工作就没了，而且下一次也没法复现。所以：

| 路线原文 | 这里的做法 | 复现的是同一件事吗 |
|---|---|---|
| API 超时 / 5xx / 限流 / DNS 失败 | 在 transport 层注入（`src.gateway` 的 transport 是可替换的） | 是：调用方看到的就是这四种异常 |
| git 操作中途 kill -9 | 真起一个**自己的**子进程，在它提交任务文件的半途 kill 掉 | 是：盘上留下 `doing` 里的孤儿任务、没有半截 JSON |
| 磁盘 90% 优雅降级 | 给写入函数注入**配额/剩余空间提供者** | 是：写入方拿到"空间不足"，而盘上一致性由同一条写路径保证 |
| 小模型进程被杀 | 真起一个**自己的**假 Ollama 子进程，写完半截 JSON 就退出 | 是：网关拿到截断/非 JSON 的响应 |

**有一条不做**：不真写满磁盘（WinError/权限/污染缓存，得不偿失），
**有一条不能省**：401/402 必须"单独告警且不重试" —— 那是安全红线，
凭证错的时候重试只会把 token 锁死，而且会掩盖真正的原因（凭证本身）。
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import shutil
import subprocess
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

#: 产物落点。`CHAOS_WEEKLY_ROOT` 可以把整棵树挪走（验收测试要的就是这个）。
OUT_ROOT = Path(os.environ.get("CHAOS_WEEKLY_ROOT") or (ROOT / "state" / "reports" / "weekly"))

#: 工作目录放**运行期**区（已 gitignore），不放 reports/ —— 混沌演练的中间文件
#: 不是给人看的证据，是"注入故障"的现场。
WORK_ROOT = ROOT / "state" / "runtime" / "chaos"

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_MISSING = 2

#: 路线 1.3 的重试策略。测试与报告都引用这一份，别处不要另抄一遍。
GATEWAY_RETRY_DELAYS = (1.0, 2.0, 4.0)

#: 磁盘优雅降级的门槛：配额用到这个比例就算"空间不足"，写入方必须拒绝动手。
DISK_PRESSURE_RATIO = 0.90


class MissingArtifact(RuntimeError):
    """缺件：解释器、配置文件、被调用的模块不在。与"降级没生效"分开报。"""


# ------------------------------------------------------------------ 结果结构

@dataclasses.dataclass
class Case:
    name: str
    passed: bool
    fault: str
    detail: dict[str, Any]
    failures: list[str] = dataclasses.field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "fault": self.fault,
            "passed": self.passed,
            "failures": list(self.failures),
            "detail": self.detail,
        }


@dataclasses.dataclass
class ChaosReport:
    date: str
    cases: list[Case]

    @property
    def passed(self) -> bool:
        return all(case.passed for case in self.cases)

    def as_dict(self) -> dict[str, Any]:
        return {
            "date": self.date,
            "passed": self.passed,
            "cases": [case.as_dict() for case in self.cases],
        }


# ------------------------------------------------------------------ 注入用的 transport

class ScriptedTransport:
    """
    按脚本依次返回结果的 transport。它替换的是 `src.gateway` 的**对端**，不是被测对象。

    为什么要做到这一步而不是 monkeypatch `chat()`：路线 1.3 要验的是"重试次数、
    退避间隔、以及 401/402 不重试"这三件事 —— 它们全在 `chat()` 里面。
    把 `chat()` 换掉，就等于把要验的东西换掉了。
    """

    def __init__(self, responses: list[Any]) -> None:
        self.responses = list(responses)
        self.calls = 0
        self.slept: list[float] = []

    def __call__(self, messages, cfg, want_json: bool):
        from src.gateway import Completion, GatewayError

        self.calls += 1
        index = min(self.calls, len(self.responses)) - 1
        item = self.responses[index]
        if isinstance(item, BaseException):
            raise item
        if isinstance(item, str):
            return Completion(item, prompt_tokens=1, completion_tokens=1)
        raise GatewayError(f"脚本里放不进去的类型：{type(item)!r}")

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)


def model_timeout_error() -> Exception:
    """造一个"调用超时"的异常（`local_client` 抛的是 LocalModelError，被包成 GatewayError）。"""
    from src.gateway import GatewayError

    return GatewayError("本机模型调用失败（qwen3:4b @ http://127.0.0.1:11434）：timed out")


def model_dns_error() -> Exception:
    """造一个"域名解析不了"的异常。"""
    from src.gateway import GatewayError

    reason = OSError(11001, "getaddrinfo failed")
    return GatewayError(f"无法连接 http://127.0.0.1:11434/api/chat: {reason}")


def model_truncated_error() -> Exception:
    """
    造一个"模型响应被截断"的异常。

    真实链路里它是 `IncompleteRead`：HTTP 头声明了 Content-Length，
    连接却在实际收到那么多字节之前就断了（进程被杀就是这个形态）。
    `_http` 把它包成 `GatewayError`。

    **为什么不在用例里写死"截断"这个字符串**：假 Ollama 回半截 JSON 时，
    客户端既可能拿到"截断的传输"（`IncompleteRead`），也可能拿到"完整的坏 JSON"
    （服务端写完 Content-Length 声明的那几个字节再退出）。两者都是"模型没给出可用输出"，
    都是**明确报错**。所以断言认的是"网关层的明确异常 + 记账全为失败"，
    而不是某一种具体的传输错误 —— 认死一种会把 flaky 引进验收。
    """
    from src.gateway import GatewayError

    return GatewayError("混沌演练注入：模型响应被截断（IncompleteRead）")


# ------------------------------------------------------------------ API 类故障

def check_api_fault(
    *,
    name: str,
    transport,
    usage_path: Path,
    retry_delays: tuple[float, ...] = GATEWAY_RETRY_DELAYS,
    expect_retries: int | None = None,
    expect_error: type[BaseException] | None = None,
) -> Case:
    """
    跑一次 `chat()` 并断言降级行为。

    三件必须钉住的事：
      1. **重试 ≤3 次**（`RETRY_DELAYS` 三个值 => 最多 4 次调用），
      2. **指数退避**（1s/2s/4s，用假 sleep 断言，不真的等 7 秒），
      3. **每一次尝试都记账**（失败也要记，否则成本账会漏掉重试）。
    """
    from src.gateway import chat

    failures: list[str] = []
    slept: list[float] = []
    outcome: dict[str, Any] = {}
    error: BaseException | None = None

    def sleep(seconds: float) -> None:
        slept.append(seconds)

    schema = {"type": "object", "properties": {"label": {"type": "string"}}, "required": ["label"]}
    try:
        chat(
            [{"role": "user", "content": "混沌演练"}],
            schema,
            "local_small",
            transport=transport,
            sleep=sleep,
            retry_delays=retry_delays,
            usage_path=usage_path,
        )
        outcome["returned"] = True
    except BaseException as exc:  # noqa: BLE001 — 这里就是要把异常本身当成结果
        error = exc
        outcome["returned"] = False
        outcome["error"] = f"{type(exc).__name__}: {str(exc)[:200]}"

    lines: list[dict] = []
    if usage_path.is_file():
        lines = [json.loads(line) for line in usage_path.read_text(encoding="utf-8").splitlines() if line.strip()]

    outcome["transport_calls"] = transport.calls
    outcome["attempts_logged"] = len(lines)
    outcome["sleeps"] = slept
    outcome["logged_ok_flags"] = [entry.get("ok") for entry in lines]
    outcome["all_failures_logged"] = bool(lines) and all(entry.get("ok") is False for entry in lines)

    if expect_error is not None and not isinstance(error, expect_error):
        failures.append(f"异常类型不对：拿到 {type(error).__name__}，期望 {expect_error.__name__}")
    if expect_error is None and error is not None:
        failures.append(f"不该抛异常却抛了：{type(error).__name__}: {error}")
    if expect_retries is not None and transport.calls > expect_retries + 1:
        failures.append(f"重试超过上限：实际调用 {transport.calls} 次（上限 {expect_retries} 次重试 = {expect_retries + 1} 次调用）")
    if slept and slept != list(retry_delays[: len(slept)]):
        failures.append(f"退避间隔不是指数退避：{slept}，期望前缀 {list(retry_delays[: len(slept)])}")
    if len(lines) != transport.calls:
        failures.append(f"记账行数（{len(lines)}）与调用次数（{transport.calls}）对不上：失败的尝试也必须记账")
    if lines and not outcome["all_failures_logged"]:
        failures.append("有失败尝试被记成 ok=true（账会骗人）")
    return Case(name=name, passed=not failures, fault="api", detail=outcome, failures=failures)


def check_github_backoff(*, workdir: Path) -> Case:
    """
    GitHub 读操作的降级（路线 1.4 第 1 条）：`with_backoff` 的 5xx 退避与 403 限流。

    这一条**不联网**：`with_backoff` 接受一个 `send` 回调，注入它就等于注入了一台 API。

    ## 为什么 5xx 的"重试"要在这里验，而不是在 `src.gateway.chat`

    两层的重试语义**不同**，混在一起会把红线验错：

      * 网关（1.3）：重试只针对**输出不合规**（模型给了 JSON 但不符合 schema）。
        运输层失败（连不上、超时、DNS 解析不了）**直接抛** ——
        重试它只会拉长故障时间，还掩盖真正的原因。
      * GitHub 读接口（1.4）：5xx 是**真·瞬时故障**，必须指数退避重试。

    401/402 在两层都一样：**不重试、单独告警**（见下面两条专门用例）。
    """
    from src.github.client import Response, with_backoff

    workdir.mkdir(parents=True, exist_ok=True)
    failures: list[str] = []
    detail: dict[str, Any] = {}

    # ---- 5xx：指数退避，最多 MAX_ATTEMPTS 次
    responses = [
        Response(500, {}, None, "https://api.github.com/x"),
        Response(502, {}, None, "https://api.github.com/x"),
        Response(503, {}, None, "https://api.github.com/x"),
        Response(200, {}, {"ok": True}, "https://api.github.com/x"),
    ]
    calls = {"n": 0}

    def send() -> Response:
        call = responses[min(calls["n"], len(responses) - 1)]
        calls["n"] += 1
        return call

    slept: list[float] = []
    result = with_backoff(send, attempts=5, base_delay=1.0, sleep=slept.append)
    detail["5xx"] = {"calls": calls["n"], "sleeps": slept, "status": result.status}
    if result.status != 200:
        failures.append(f"5xx 退避后没有恢复到 200：{result.status}")
    if slept != [1.0, 2.0, 4.0]:
        failures.append(f"5xx 退避不是指数退避：{slept}，期望 [1.0, 2.0, 4.0]")
    if calls["n"] != 4:
        failures.append(f"5xx 调用次数不对：{calls['n']}，期望 4（1 次首发 + 3 次重试）")

    # ---- 403 + Retry-After：限流要**按对方说的秒数**睡
    limiter = [
        Response(403, {"retry-after": "7"}, None, "https://api.github.com/x"),
        Response(200, {}, {"ok": True}, "https://api.github.com/x"),
    ]
    limiter_calls = {"n": 0}

    def send_limited() -> Response:
        item = limiter[min(limiter_calls["n"], len(limiter) - 1)]
        limiter_calls["n"] += 1
        return item

    limited_sleeps: list[float] = []
    limited = with_backoff(send_limited, attempts=5, sleep=limited_sleeps.append)
    detail["rate_limit"] = {"calls": limiter_calls["n"], "sleeps": limited_sleeps, "status": limited.status}
    if limited_sleeps != [7.0]:
        failures.append(f"限流没有按 Retry-After 睡：{limited_sleeps}，期望 [7.0]")
    if limited.status != 200:
        failures.append(f"限流退避后没有恢复：{limited.status}")

    # ---- 401/402：**单独告警且不重试**（红线）
    auth_case = check_auth_no_retry(workdir=workdir)
    detail["auth_no_retry"] = auth_case.detail
    if not auth_case.passed:
        failures += [f"401/402 红线：{item}" for item in auth_case.failures]

    return Case(name="API 降级：5xx 指数退避 / 403 限流 / 401-402 不重试", passed=not failures, fault="api", detail=detail, failures=failures)


def check_auth_no_retry(*, workdir: Path, status: int = 401) -> Case:
    """
    **红线**：401/402 不重试、切告警、不改 mode。

    为什么单独一条：凭证错误的正确反应是"停下并喊人"。重试它没有任何意义
    （token 不会自己变好），而且会掩盖真正的原因 —— 人看到的会是"重试 5 次都失败"，
    于是去查网络，而真正要做的只是换一个 token。
    """
    from src.github.client import Response, with_backoff

    workdir.mkdir(parents=True, exist_ok=True)
    failures: list[str] = []
    calls = {"n": 0}

    def send() -> Response:
        calls["n"] += 1
        return Response(status, {}, {"message": "Bad credentials"}, "https://api.github.com/user")

    alerts: list[int] = []
    slept: list[float] = []
    error: BaseException | None = None
    try:
        with_backoff(
            send,
            attempts=5,
            sleep=slept.append,
            on_auth_failure=lambda response: alerts.append(response.status),
        )
    except Exception as exc:  # noqa: BLE001 — 抛不抛、抛什么都在断言范围内
        error = exc

    detail = {
        "status": status,
        "calls": calls["n"],
        "alerts": alerts,
        "sleeps": slept,
        "error": f"{type(error).__name__}: {str(error)[:160]}" if error else None,
    }
    if calls["n"] != 1:
        failures.append(f"401/402 被重试了 {calls['n']} 次（红线要求一次都不重试）")
    if not alerts:
        failures.append("401/402 没有触发凭证告警回调（人就不知道要去换 token）")
    if slept:
        failures.append(f"401/402 还睡了一次退避：{slept}")
    if error is None:
        failures.append("401/402 没有抛错：调用方会以为自己读到了正常响应")
    return Case(name=f"API {status}：单独告警且不重试", passed=not failures, fault="api", detail=detail, failures=failures)


def check_gateway_auth_no_retry(*, workdir: Path, status: int = 401) -> Case:
    """
    网关侧的 401/402：`openai_transport` 抛 `AuthError`，`chat()` 不重试。

    与上一条一起构成"401/402 红线"的两半：GitHub 读接口那半、模型网关那半。
    """
    from src.gateway import AuthError, Completion, chat

    workdir.mkdir(parents=True, exist_ok=True)
    calls = {"n": 0}
    slept: list[float] = []

    def transport(messages, cfg, want_json: bool):
        calls["n"] += 1
        raise AuthError(f"{cfg.base_url} 返回 {status}：凭证无效或余额不足")

    _ = Completion
    error: BaseException | None = None
    try:
        chat(
            [{"role": "user", "content": "混沌演练"}],
            None,
            "local_small",
            transport=transport,
            sleep=slept.append,
            usage_path=workdir / "usage-auth.log",
        )
    except BaseException as exc:  # noqa: BLE001
        error = exc

    failures: list[str] = []
    detail = {
        "status": status,
        "calls": calls["n"],
        "sleeps": slept,
        "error": f"{type(error).__name__}: {str(error)[:160]}" if error else None,
    }
    if calls["n"] != 1:
        failures.append(f"网关对 {status} 重试了 {calls['n']} 次（红线：不重试）")
    if slept:
        failures.append(f"网关对 {status} 还做了退避等待：{slept}")
    if not isinstance(error, AuthError):
        failures.append(f"网关没有抛 AuthError：{type(error).__name__}")
    return Case(name=f"网关 {status}：不重试、抛 AuthError", passed=not failures, fault="api", detail=detail, failures=failures)


def check_missing_credential(*, workdir: Path) -> Case:
    """
    "没配 key"必须**报清楚**，而不是发一个不带 Authorization 的请求去换一个 401。

    这条验的是 1.3 里一句注释：401 会被上层当成"凭证被拒"写进 auth_failure.md，
    于是真正的病因（根本没配 key）被一条错误告警盖住。

    ## 为什么要连"取 key"和"发请求"两跳一起换掉，而不是只清空环境变量

    实测踩过两件事：

    1. 这台机器的 `$DSH_HOME/.deepseek_key` 里**真的有**一把可用的 key，
       于是"清空环境变量"之后请求照样带着 key 发了出去 —— 演练不但没验到缺凭证的报错，
       还真的往 DeepSeek 发了**一次对外请求**（返回 400）。缺凭证用例必须**离线**。
    2. 只换 `src.gateway.request_json` 也**没用**：`src.gateway` 是包，
       `openai_transport.__globals__` 是 `src.gateway.gateway` 这个模块的命名空间。
       补丁要打在**函数真正查找名字的地方**上；否则看起来打上了、请求照样出网。
       （这一条本身就是一次真实的"补丁没落上而你以为落了"的事故，值得留在注释里。）

    所以这里把 `load_api_key` 打成"这台机器没有任何 key"，并把 `request_json` 打成
    "永远连不上"作为第二道保险 —— 有没有 key 都不出网。
    """
    from src import gateway as gw
    from src.gateway import gateway as gw_impl

    failures: list[str] = []
    env_backup = dict(os.environ)
    request_backup = gw_impl.request_json
    key_backup = gw_impl.load_api_key

    def no_key(_name: str = gw.API_KEY_ENV, _filename: str = gw.API_KEY_FILE) -> tuple[str, str]:
        return "", f"演练注入：这台机器没有任何 key（环境变量 {gw.API_KEY_ENV} 与 $DSH_HOME/.* 都当作不存在）"

    def offline_request(*_args: object, **_kwargs: object):
        raise gw.GatewayError("演练注入：本用例不出网（连不上 https://api.deepseek.com/v1）")

    try:
        os.environ.pop(gw.API_KEY_ENV, None)
        gw_impl.load_api_key = no_key  # type: ignore[assignment]
        gw_impl.request_json = offline_request  # type: ignore[assignment]
        config = {
            "tiers": {
                "flash_api": {
                    "base_url": "https://api.deepseek.com/v1",
                    "model": "deepseek-v4.1-flash",
                    "temperature": 0.2,
                    "api_style": "openai",
                }
            }
        }
        error: BaseException | None = None
        try:
            gw.chat(
                [{"role": "user", "content": "x"}],
                None,
                "flash_api",
                config=config,
                usage_path=workdir / "usage-key.log",
            )
        except BaseException as exc:  # noqa: BLE001
            error = exc
    finally:
        gw_impl.request_json = request_backup  # type: ignore[assignment]
        gw_impl.load_api_key = key_backup  # type: ignore[assignment]
        os.environ.clear()
        os.environ.update(env_backup)

    detail = {"error": f"{type(error).__name__}: {str(error)[:200]}" if error else None}
    if not isinstance(error, gw.GatewayError):
        failures.append(f"缺 key 时没有抛出明确错误：{type(error).__name__}")
    elif gw.API_KEY_ENV not in str(error):
        failures.append(f"缺 key 的报错里没说清去配哪个环境变量：{error}")
    return Case(name="API 缺凭证：报清楚、不去换一个 401", passed=not failures, fault="api", detail=detail, failures=failures)


# ------------------------------------------------------------------ git 中途 kill -9

#: 子进程脚本：真的 enqueue + dequeue（占坑、写 claimed_by），然后停在那里等人杀。
#: 它只碰**自己的** state 目录，不碰仓库里的真实 state/。
CHILD_WORKER = r'''
import os, sys, time
from pathlib import Path
root = Path(sys.argv[1]); state = Path(sys.argv[2])
sys.path.insert(0, str(root))
from src.queue import Task, TaskQueue, TaskState
queue = TaskQueue(state / "state")
for index in range(4):
    queue.enqueue(Task(id=f"chaos-{index}", type="noop", payload={"n": index}))
task = queue.dequeue()
Path(state / "claimed.txt").write_text(task.id if task else "", encoding="utf-8")
sys.stdout.write("ready\n"); sys.stdout.flush()
while True:
    time.sleep(0.2)          # 停在这里：任务已经在 doing/ 里，进程随时会被 kill
'''


def _kill_child(process: subprocess.Popen) -> None:
    """
    只杀**我们自己起的**那个子进程，绝不 `/T`（不牵连它的后代）、绝不按名字杀。

    这条纪律是硬的：本机是 AI 沙箱，按名字杀进程会连测试运行器一起带走。
    """
    if process.poll() is not None:
        return
    subprocess.run(
        ["taskkill", "/F", "/PID", str(process.pid)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    try:
        process.wait(timeout=30)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=10)


def check_kill9_state(*, workdir: Path) -> Case:
    """
    git/写操作中途 kill -9 之后，`state/` 必须一致，且重跑能自愈。

    四件断言：
      1. **没有半截 JSON**：pending/doing 里每个 `.json` 都必须能被解析；
      2. **有孤儿任务**（`doing` 里那条的主人已经没了）—— 这是"中途被杀"的物证，
         没有它说明我们其实没杀在正确的位置上，后面的自愈断言也就没意义了；
      3. **孤儿被回收**：重跑 `recover_orphans()` 后任务回到 pending；
      4. **重跑不重复消费**：回收后重新认领，已完成的不再被处理。
    """
    from src.queue import TaskQueue, TaskState

    workdir.mkdir(parents=True, exist_ok=True)
    failures: list[str] = []
    detail: dict[str, Any] = {}

    child_script = workdir / "child_worker.py"
    child_script.write_text(CHILD_WORKER, encoding="utf-8")
    child_state = workdir / "child"
    child_state.mkdir(parents=True, exist_ok=True)
    log_path = workdir / "child.log"

    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT)
    env["PYTHONIOENCODING"] = "utf-8"

    with open(log_path, "w", encoding="utf-8") as log:
        process = subprocess.Popen(
            [sys.executable, str(child_script), str(ROOT), str(child_state)],
            cwd=str(workdir),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        try:
            deadline = time.time() + 120
            claimed_path = child_state / "claimed.txt"
            claimed = ""
            while time.time() < deadline:
                if claimed_path.is_file():
                    claimed = claimed_path.read_text(encoding="utf-8").strip()
                    if claimed:
                        break
                if process.poll() is not None:
                    break
                time.sleep(0.2)
            detail["claimed_before_kill"] = claimed
            if not claimed:
                failures.append(f"子进程没能在被杀前认领任务（见 {log_path.name}）")
            # 真正 kill -9：/F 是强杀，且只针对这一个 pid
            _kill_child(process)
        finally:
            _kill_child(process)

    detail["child_exit_code"] = process.returncode

    # ---- 1. 盘上一致：没有半截 JSON
    queue = TaskQueue(child_state / "state")
    broken: list[str] = []
    per_state: dict[str, int] = {}
    for state in (TaskState.PENDING, TaskState.DOING):
        files = sorted((child_state / "state" / "tasks" / state.value).glob("*.json"))
        per_state[state.value] = len(files)
        for path in files:
            try:
                json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError, OSError) as exc:
                broken.append(f"{path.name}: {type(exc).__name__}")
    detail["files"] = per_state
    detail["half_written_json"] = broken
    if broken:
        failures.append(f"state/ 里出现解析不了的半截 JSON：{broken}")

    # ---- 2. 有孤儿任务（被杀在正确的位置上的物证）
    doing = sorted((child_state / "state" / "tasks" / "doing").glob("*.json"))
    detail["doing"] = [path.stem for path in doing]
    if claimed and claimed not in {path.stem for path in doing}:
        failures.append(f"被杀的子进程认领的任务不在 doing/ 里：{claimed}（说明 kill 没落在占坑与完成之间）")

    stats_before = queue.stats()
    detail["stats_before_recover"] = stats_before
    if stats_before.get("doing", 0) == 0:
        failures.append("doing/ 里没有孤儿任务：这次 kill 没有复现「中途被杀」的现场，自愈断言也就没意义")

    # ---- 3. 重跑自愈：孤儿回收
    records = queue.recover_orphans()
    detail["recover_records"] = records
    stats_after = queue.stats()
    detail["stats_after_recover"] = stats_after
    if stats_after.get("doing", 0) != 0:
        failures.append(f"孤儿回收后 doing/ 里还有任务：{stats_after}")
    if stats_after.get("pending", 0) != stats_before.get("pending", 0) + stats_before.get("doing", 0):
        failures.append(f"孤儿没有全数回到 pending：{stats_before} -> {stats_after}")

    # ---- 4. 回收后重新认领，且不重复处理已完成的
    twice: list[str] = []
    while True:
        task = queue.dequeue()
        if task is None:
            break
        twice.append(task.id)
        queue.complete(task.id)
    detail["processed_after_recover"] = twice
    duplicates = [item for item in set(twice) if twice.count(item) > 1]
    if duplicates:
        failures.append(f"回收后出现重复消费：{duplicates}")
    if claimed and claimed not in twice:
        failures.append(f"被中断的那件任务没有被续跑：{claimed} 不在 {twice}")
    if len(twice) != stats_after.get("pending", 0):
        failures.append(f"回收后应处理 {stats_after.get('pending', 0)} 件，实际 {len(twice)} 件")

    # ---- 5. 台账报告必须能落盘（半截的 markdown 也算脏数据）
    reports = sorted((child_state / "state" / "reports").glob("*.md"))
    detail["reports"] = [path.name for path in reports]
    empty = [path.name for path in reports if not path.read_text(encoding="utf-8").strip()]
    if empty:
        failures.append(f"state/reports/ 里有空文件（半截产物）：{empty}")

    return Case(name="git/写操作中途 kill -9：state 一致且能自愈", passed=not failures, fault="process_kill", detail=detail, failures=failures)


# ------------------------------------------------------------------ 磁盘 90% 优雅降级

class DiskFull(RuntimeError):
    """空间不足。**必须在动手写之前抛** —— 写到一半才发现写不下，盘上就多了半截文件。"""


class Quota:
    """
    可注入的剩余空间提供者。

    为什么做成可注入：真把磁盘写满会污染整台机器（缓存、日志、虚拟内存全在里面），
    而且这台机器上没人有权限清配额。注入配额复现的是同一个判断点：
    **写入方在动手之前先问"还有空间吗"**，而盘上一致性由同一条写路径保证。
    """

    def __init__(self, total_bytes: int, *, reserve_ratio: float = DISK_PRESSURE_RATIO) -> None:
        self.total = total_bytes
        self.reserve_ratio = reserve_ratio
        self.used = 0

    def used_ratio(self) -> float:
        return self.used / self.total if self.total else 1.0

    def available(self) -> int:
        return max(0, self.total - self.used)

    def pressure(self) -> bool:
        """用量达到门槛就算"空间紧张"：调用方必须走优雅降级，而不是硬写。"""
        return self.used_ratio() >= self.reserve_ratio

    def consume(self, size: int) -> None:
        self.used += size


def write_atomic(path: Path, payload: dict, *, quota: Quota | None = None) -> Path:
    """
    原子写一个 JSON 产物：先写临时文件、再 `os.replace`。

    ## 两次拦住"半截产物"

    1. **动手之前查配额**（优雅降级）：不够就抛 `DiskFull`，一个字节都不写；
    2. **先写临时文件再替换**：即使写到一半进程被杀，目标路径上要么是旧内容、
       要么是新内容，**永远不会是半个 JSON**（pytest 里那条 .tmp 残骸就是这个机制的代价，
       它是无害的，而且下一次写入不会复用它）。
    """
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    size = len(text.encode("utf-8"))
    if quota is not None and (quota.pressure() or quota.available() < size):
        raise DiskFull(
            f"空间不足：可用 {quota.available()} 字节、已用 {quota.used_ratio():.1%}"
            f"（门槛 {quota.reserve_ratio:.0%}），本条需要 {size} 字节。"
            "已放弃写入，目标文件保持原样（不会留下半截产物）。"
        )
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)
    if quota is not None:
        quota.consume(size)
    return path


def check_disk_pressure(*, workdir: Path) -> Case:
    """
    磁盘 90% 的优雅降级：**报错清楚、不产生半截产物**。

    三个阶段：空间充足时能写；越过门槛后拒绝写（且目标文件保持"没有"的状态）；
    清理之后能继续写。
    """
    workdir.mkdir(parents=True, exist_ok=True)
    failures: list[str] = []
    quota = Quota(total_bytes=1000, reserve_ratio=0.90)
    detail: dict[str, Any] = {"total_bytes": quota.total, "reserve_ratio": quota.reserve_ratio}

    # ---- 阶段一：空间充足
    first = write_atomic(workdir / "ledger-1.json", {"task": "a", "ok": True}, quota=quota)
    detail["first_write_bytes"] = quota.used
    detail["first_write_ratio"] = round(quota.used_ratio(), 3)
    if not first.is_file():
        failures.append("空间充足时反而没写成")

    # ---- 阶段二：推到门槛以上，再写必须被拒
    quota.consume(int(quota.total * 0.95))
    detail["pressure_ratio"] = round(quota.used_ratio(), 3)
    detail["pressure"] = quota.pressure()
    if not quota.pressure():
        failures.append("配额没有进入「空间不足」状态，这条用例就没测到东西")
    error: BaseException | None = None
    target = workdir / "ledger-2.json"
    try:
        write_atomic(target, {"task": "b", "ok": True}, quota=quota)
    except BaseException as exc:  # noqa: BLE001
        error = exc
    detail["second_error"] = f"{type(error).__name__}: {str(error)[:160]}" if error else None
    if not isinstance(error, DiskFull):
        failures.append(f"空间不足时没有抛出明确错误：{type(error).__name__}")
    elif "空间不足" not in str(error):
        failures.append(f"空间不足的报错不够清楚：{error}")
    if target.exists():
        failures.append("空间不足却还是写出了目标文件（半截产物）")
    leftovers = sorted(path.name for path in workdir.glob(".*.tmp"))
    detail["tmp_leftovers"] = leftovers
    if leftovers:
        failures.append(f"空间不足时留下了临时文件残骸：{leftovers}")

    # ---- 阶段三：清理后能继续写（优雅降级不是永久躺平）
    quota.used = 0
    third = write_atomic(workdir / "ledger-3.json", {"task": "c", "ok": True}, quota=quota)
    if not third.is_file():
        failures.append("空间恢复后仍然写不进去")

    # ---- 一致性总检：目录里**只有完整的 JSON**，没有半截的
    broken = []
    for path in sorted(workdir.glob("*.json")):
        try:
            json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            broken.append(f"{path.name}: {type(exc).__name__}")
    detail["files"] = sorted(path.name for path in workdir.glob("*.json"))
    if broken:
        failures.append(f"产物目录里出现半截 JSON：{broken}")

    return Case(name="磁盘 90%：优雅降级、无半截产物", passed=not failures, fault="disk", detail=detail, failures=failures)


# ------------------------------------------------------------------ 小模型被杀 / 半截 JSON

#: 假 Ollama：收到一个请求就回一段**写了一半的 JSON**，然后退出（模拟进程被杀）。
#: `Content-Length` 按实际发出的字节数算：发多少写多少，客户端才算"读到了完整的坏 JSON"
#: （不这么写会变成"连接被重置"，那是另一种故障形态，见 `model_truncated_error` 的注释）。
FAKE_OLLAMA_DIES = r'''
import socket, sys, time
port = int(sys.argv[1])
server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
server.bind(("127.0.0.1", port))
server.listen(4)
sys.stdout.write("listening\n"); sys.stdout.flush()
conn, _ = server.accept()
buf = b""
while b"\r\n\r\n" not in buf:
    chunk = conn.recv(4096)
    if not chunk:
        break
    buf += chunk
body = b'{"message": {"content": "{\\"label\\": \\"bu'      # 半截 JSON
head = (
    "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
    "Content-Length: %d\r\nConnection: close\r\n\r\n" % len(body)
).encode()
conn.sendall(head + body)
sys.stdout.write("served-once\n"); sys.stdout.flush()
# 停一下再退出：要复现的是"模型给了半截输出"，不是"TCP 被硬重置"。
# 实测踩过：立刻 close 时客户端偶尔收到 ConnectionAbortedError，
# 于是断言"明确错误"的那一条会在两种传输错误之间 flaky（多跑几次就复现）。
time.sleep(1.5)
conn.close()
server.close()
'''


def check_model_killed(*, workdir: Path) -> Case:
    """
    小模型进程被杀 / 只回半截 JSON：网关必须抛明确错误、**不产脏数据**。

    四件断言：
      1. 调用方拿到的是网关层的明确异常（`GatewayError` 家族，含 `SchemaError`），
         不是"某个默认标签"；
      2. **记账行一条都不许是 ok=true** —— 半截输出被记成成功，成本账和质检账都会骗人；
      3. 工作目录里没有半截产物文件（本次调用根本不落业务文件，这是断言基线）；
      4. 真实路径上确实发生了"截断响应"这件事（子进程真的起过、真的断开过）。
    """
    from src.gateway import chat

    workdir.mkdir(parents=True, exist_ok=True)
    failures: list[str] = []
    detail: dict[str, Any] = {}

    port = _free_port()
    script = workdir / "fake_ollama.py"
    script.write_text(FAKE_OLLAMA_DIES, encoding="utf-8")
    log_path = workdir / "fake_ollama.log"
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"

    with open(log_path, "w", encoding="utf-8") as log:
        process = subprocess.Popen(
            [sys.executable, str(script), str(port)],
            stdout=log,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            env=env,
        )
        try:
            ready = False
            deadline = time.time() + 60
            while time.time() < deadline:
                if log_path.is_file() and "listening" in log_path.read_text(encoding="utf-8", errors="replace"):
                    ready = True
                    break
                if process.poll() is not None:
                    break
                time.sleep(0.2)
            detail["fake_model_ready"] = ready
            if not ready:
                failures.append(f"假 Ollama 没起来（见 {log_path.name}）")

            config = {
                "tiers": {
                    "local_small": {
                        "base_url": f"http://127.0.0.1:{port}",
                        "model": "qwen3:4b",
                        "temperature": 0,
                        "api_style": "ollama",
                    }
                }
            }
            usage = workdir / "usage-killed.log"
            error: BaseException | None = None
            if ready:
                try:
                    chat(
                        [{"role": "user", "content": "混沌演练：分类"}],
                        {"type": "object", "properties": {"label": {"type": "string"}}, "required": ["label"]},
                        "local_small",
                        config=config,
                        usage_path=usage,
                        retry_delays=(0.0, 0.0, 0.0),
                        sleep=lambda _: None,
                    )
                except BaseException as exc:  # noqa: BLE001
                    error = exc
            detail["error"] = f"{type(error).__name__}: {str(error)[:200]}" if error else None
            from src.gateway import GatewayError

            if ready and not isinstance(error, GatewayError):
                failures.append(f"半截输出没有变成明确错误：{type(error).__name__}")

            lines: list[dict] = []
            if usage.is_file():
                lines = [json.loads(line) for line in usage.read_text(encoding="utf-8").splitlines() if line.strip()]
            detail["attempts_logged"] = len(lines)
            detail["ok_flags"] = [entry.get("ok") for entry in lines]
            if any(entry.get("ok") for entry in lines):
                failures.append("半截输出被记成了一次成功调用（账会骗人）")
            if ready and not lines:
                failures.append("模型失败时一条账都没记（成本账会漏掉整片故障）")
        finally:
            _kill_child(process)

    detail["child_exit_code"] = process.returncode
    dirty = sorted(path.name for path in workdir.glob("*.json") if path.name.startswith("ledger"))
    detail["ledger_files"] = dirty
    if dirty:
        failures.append(f"模型失败却产出了业务产物（脏数据）：{dirty}")

    return Case(name="小模型被杀/半截 JSON：明确报错、不产脏数据", passed=not failures, fault="model_kill", detail=detail, failures=failures)


def _free_port() -> int:
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


# ------------------------------------------------------------------ 报告

def render_markdown(report: ChaosReport) -> str:
    lines = [
        f"# 每周混沌故障注入报告 {report.date}",
        "",
        (
            f"- 结论：**{'全过' if report.passed else '有失败'}**"
            f"（{len(report.cases) - len([case for case in report.cases if not case.passed])}/{len(report.cases)} 项通过）"
        ),
        "- 全部故障都是**注入式**的：不真写满磁盘、不杀本机进程（只杀自己起的子进程）。",
        "",
        "> 路线 8.3：「混沌演练 —— 每周」；红线：**降级未生效 = 冻结写权限**。",
        "",
        "| 故障 | 结论 | 关键数字 |",
        "|---|---|---|",
    ]
    for case in report.cases:
        numbers = []
        for key in ("calls", "transport_calls", "sleeps", "attempts_logged", "stats_after_recover", "used_ratio"):
            if key in case.detail:
                numbers.append(f"{key}={case.detail[key]}")
        lines.append(f"| {case.name} | {'通过' if case.passed else '**失败**'} | {'；'.join(numbers) or '见 JSON'} |")

    lines += ["", "## 逐条明细", ""]
    for case in report.cases:
        lines += [f"### {'✅' if case.passed else '❌'} {case.name}", "", f"- 故障类型：`{case.fault}`", ""]
        for key, value in case.detail.items():
            rendered = json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else str(value)
            lines.append(f"- `{key}`：{rendered[:400]}")
        if case.failures:
            lines += ["", "**失败原因：**", ""]
            lines += [f"- {item}" for item in case.failures]
        lines.append("")

    lines += [
        "---",
        "",
        "## 说明（为什么这样注入）",
        "",
        "- **API 故障**注入在 transport 层：`src.gateway` 的 transport 是可替换的，",
        "  换掉它就是把对端换成一台会超时/会 5xx/会限流的服务器，被测的 `chat()` 一个字没改。",
        "- **kill -9** 杀的是我们自己起的子进程（`taskkill /F /PID`，不带 `/T`）——",
        "  真杀本机进程会连测试运行器一起带走，而且下次无法复现。",
        "- **磁盘 90%** 用注入配额而不是真写满：复现的是同一个判断点（动手前先问还有没有空间），",
        "  盘上一致性由同一条原子写路径保证。",
        "- **小模型被杀**起的是一个真子进程（回半截 JSON 就退出），走的是真的 HTTP + 真的 `urllib`。",
        "",
    ]
    return "\n".join(lines)


def write_report(report: ChaosReport, *, out_dir: Path | None = None) -> tuple[Path, Path]:
    target_dir = out_dir or OUT_ROOT
    target_dir.mkdir(parents=True, exist_ok=True)
    json_path = target_dir / f"chaos-{report.date}.json"
    md_path = target_dir / f"chaos-{report.date}.md"
    json_path.write_text(json.dumps(report.as_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    md_path.write_text(render_markdown(report), encoding="utf-8")
    return json_path, md_path


def run(*, workdir: Path | None = None, out_dir: Path | None = None, skip_process: bool = False) -> tuple[ChaosReport, Path, Path]:
    """跑全部混沌用例。`skip_process` 只跳过需要起子进程的三条（给极简环境用）。"""
    if sys.version_info < (3, 12):
        raise MissingArtifact(f"需要 Python 3.12，当前 {sys.version.split()[0]}（本仓库的解释器是 D:\\PythonEnv\\venv\\Scripts\\python.exe）")

    session = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    work = workdir or (WORK_ROOT / session)
    work.mkdir(parents=True, exist_ok=True)

    api_dir = work / "api"
    cases: list[Case] = [
        check_api_fault(
            name="API 超时：明确异常、不空转重试",
            transport=ScriptedTransport([model_timeout_error()]),
            usage_path=api_dir / "usage-timeout.log",
            expect_error=Exception,
            expect_retries=0,
        ),
        check_api_fault(
            name="API DNS 失败：明确异常、不空转重试",
            transport=ScriptedTransport([model_dns_error()]),
            usage_path=api_dir / "usage-dns.log",
            expect_error=Exception,
            expect_retries=0,
        ),
        check_api_fault(
            name="API 输出不合规：重试 3 次、指数退避 1/2/4、最后抛 SchemaError",
            transport=ScriptedTransport(["不是 JSON"] * 4),
            usage_path=api_dir / "usage-schema.log",
            expect_error=_schema_error(),
            expect_retries=3,
        ),
        check_github_backoff(workdir=work / "github"),
        check_gateway_auth_no_retry(workdir=work / "gateway-auth", status=401),
        check_gateway_auth_no_retry(workdir=work / "gateway-auth-402", status=402),
        check_missing_credential(workdir=work / "missing-key"),
    ]
    if not skip_process:
        cases += [
            check_kill9_state(workdir=work / "kill9"),
            check_model_killed(workdir=work / "model"),
        ]
    cases.append(check_disk_pressure(workdir=work / "disk"))

    report = ChaosReport(date=datetime.now(timezone.utc).strftime("%Y-%m-%d"), cases=cases)
    json_path, md_path = write_report(report, out_dir=out_dir)
    return report, json_path, md_path


def _schema_error() -> type[BaseException]:
    from src.gateway import SchemaError

    return SchemaError


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

    parser = argparse.ArgumentParser(description="8.2 ② 每周混沌故障注入")
    parser.add_argument("--out-dir", default="", help=f"产物目录（默认 {OUT_ROOT}）")
    parser.add_argument("--work-dir", default="", help="注入故障的工作目录（默认 state/runtime/chaos/<时间戳>）")
    parser.add_argument("--json", action="store_true", help="只打印 JSON")
    parser.add_argument("--skip-process", action="store_true", help="跳过需要起子进程的三条用例")
    parser.add_argument("--keep", action="store_true", help="保留工作目录（默认保留：故障现场就是证据）")
    args = parser.parse_args()

    try:
        report, json_path, md_path = run(
            workdir=Path(args.work_dir) if args.work_dir else None,
            out_dir=Path(args.out_dir) if args.out_dir else None,
            skip_process=args.skip_process,
        )
    except MissingArtifact as exc:
        print(f"缺件：{exc}")
        return EXIT_MISSING
    except Exception:  # noqa: BLE001
        print("混沌演练自身崩了（这不是缺件，是工具坏了）：")
        traceback.print_exc()
        return EXIT_FAILED

    if args.json:
        print(json.dumps(report.as_dict(), ensure_ascii=False, indent=2))
    else:
        for case in report.cases:
            print(f"{'PASS' if case.passed else 'FAIL'}  {case.name}")
            for key, value in case.detail.items():
                if isinstance(value, (int, float, str, bool)) or value is None:
                    print(f"        {key} = {value}")
            for item in case.failures:
                print(f"        失败：{item}")
        print()
        print(f"JSON：{json_path}")
        print(f"报告：{md_path}")

    if not args.keep:
        shutil.rmtree(OUT_ROOT / "work", ignore_errors=True)
    return EXIT_OK if report.passed else EXIT_FAILED


if __name__ == "__main__":
    raise SystemExit(main())
