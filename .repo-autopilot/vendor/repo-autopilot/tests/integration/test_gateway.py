"""1.3 LLM 网关验收测试（对应路线 1.3 的「验收」一节）。

路线原文要求验证三件事：
  1. mock 后端下，schema 不合规会**自动重试**，且**最终抛错而不是返回脏数据**；
  2. 本地小模型进程不在时，调用方收到**明确异常而不是脏数据**；
  3. 断网模拟下 `mode.json` 正确切换。

另外补三条本网关引入、必须钉住的语义：
  4. 401/402 **不算断联**：写 `auth_failure.md` 告警，且**不重试**、不改 mode；
  5. `embed` 返回的向量必须已 L2 归一化（本机相似度阈值全靠它）；
  6. 退避策略是 1s/2s/4s、重试 3 次（用注入的假 sleep 断言，真的等 7 秒没有意义）。
"""

from __future__ import annotations

import http.server
import json
import socket
import sys
import threading
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from typing_extensions import Self

from src.gateway import (
    RETRY_DELAYS,
    GatewayError,
    ProbeResult,
    SchemaError,
    chat,
    connectivity_check,
    embed,
    validate_against,
    write_mode,
)

SCHEMA = {
    "type": "object",
    "properties": {"label": {"type": "string", "enum": ["bug", "feature"]}},
    "required": ["label"],
    "additionalProperties": False,
}


class MockEndpoint:
    """
    最小可用的假 Ollama：按脚本依次吐出 /api/chat 的响应。

    为什么用真 HTTP 服务而不是 monkeypatch 掉 transport：
    被测的是"HTTP + JSON + schema 重试"这条完整链路，把 transport 换掉就等于
    把要验的东西换掉了。这里只把**对端**换成假的。
    """

    def __init__(
        self,
        chat_responses: list[object] | None = None,
        *,
        embed_vectors: list[list[float]] | None = None,
        embeddings: list[dict] | None = None,
        tags: list[str] | None = None,
        tags_status: int = 200,
    ) -> None:
        self.chat_responses = list(chat_responses or [])
        self.embed_vectors = embed_vectors
        #: OpenAI 兼容 `/embeddings` 的 `data` 数组（每项 {"index": i, "embedding": [...]}）
        self.embeddings = embeddings
        self.tags = tags if tags is not None else ["qwen3:4b"]
        self.tags_status = tags_status
        self.seen: list[tuple[str, str]] = []
        self.base_url = ""
        self._server: http.server.ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------ 生命周期

    def __enter__(self) -> Self:
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args: object) -> None:   # 静音
                pass

            def _send(self, status: int, payload: object) -> None:
                body = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:
                outer.seen.append(("GET", self.path))
                if self.path == "/api/tags":
                    if outer.tags_status != 200:
                        self._send(outer.tags_status, {"error": "mock"})
                        return
                    self._send(200, {"models": [{"name": n} for n in outer.tags]})
                    return
                self._send(404, {"error": "not found"})

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                if length:
                    self.rfile.read(length)
                outer.seen.append(("POST", self.path))
                if self.path == "/api/chat":
                    if not outer.chat_responses:
                        self._send(200, {"message": {"content": "{}"}})
                        return
                    item = outer.chat_responses.pop(0)
                    if isinstance(item, tuple):
                        self._send(item[0], item[1])
                        return
                    self._send(
                        200,
                        {
                            "message": {"content": item},
                            "prompt_eval_count": 7,
                            "eval_count": 3,
                        },
                    )
                    return
                if self.path == "/api/embed":
                    self._send(200, {"embeddings": outer.embed_vectors or []})
                    return
                if self.path == "/embeddings":
                    # OpenAI 兼容面。故意**按给定顺序原样返回**（含 index），
                    # 好让"按 index 归位"那条用例自己去构造乱序。
                    self._send(200, {"data": outer.embeddings or []})
                    return
                self._send(404, {"error": "not found"})

        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        port = self._server.server_address[1]
        self.base_url = f"http://127.0.0.1:{port}"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)

    # ---------------------------------------------------------------- 统计

    def calls(self, path: str) -> int:
        return sum(1 for _, seen_path in self.seen if seen_path == path)


def config_for(base_url: str) -> dict:
    return {
        "tiers": {
            "local_small": {
                "base_url": base_url,
                "model": "qwen3:4b",
                "temperature": 0,
                "api_style": "ollama",
            },
        },
        "embed": {
            "local": {"base_url": base_url, "model": "bge-m3:latest", "api_style": "ollama"}
        },
    }


def free_port() -> int:
    """拿一个当前没人监听的端口，用来模拟'模型进程不在'。"""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


# ============================================================ 1. schema 重试

class TestSchemaRetry:
    def test_default_backoff_is_the_roadmap_policy(self) -> None:
        assert RETRY_DELAYS == (1.0, 2.0, 4.0)

    def test_retries_three_times_then_raises_instead_of_returning_dirty_data(self, state_dir: Path) -> None:
        responses = [
            "这不是 JSON",
            '{"label": "other"}',        # 枚举不合法
            '{"label": "bug", "extra": 1}',  # additionalProperties: false
            "[]",                        # 不是对象
        ]
        with MockEndpoint(responses) as mock:
            slept: list[float] = []
            with pytest.raises(SchemaError) as excinfo:
                chat(
                    [{"role": "user", "content": "分类"}],
                    SCHEMA,
                    "local_small",
                    config=config_for(mock.base_url),
                    usage_path=state_dir / "token_usage.log",
                    retry_delays=(0.0, 0.0, 0.0),
                    sleep=slept.append,
                )
            assert mock.calls("/api/chat") == 4, "应为 1 次首发 + 3 次重试"
            assert slept == [0.0, 0.0, 0.0], "三次重试的间隔都必须真的等过"
        assert "schema" in str(excinfo.value)

    def test_recovers_when_second_attempt_is_valid(self, state_dir: Path) -> None:
        with MockEndpoint(["坏输出", '{"label": "bug"}']) as mock:
            result = chat(
                [{"role": "user", "content": "分类"}],
                SCHEMA,
                "local_small",
                config=config_for(mock.base_url),
                usage_path=state_dir / "token_usage.log",
                retry_delays=(0.0, 0.0, 0.0),
                sleep=lambda _: None,
            )
            assert result == {"label": "bug"}
            assert mock.calls("/api/chat") == 2

    def test_every_attempt_is_logged_including_failures(self, state_dir: Path) -> None:
        usage = state_dir / "token_usage.log"
        with MockEndpoint(["坏", '{"label": "bug"}']) as mock:
            chat(
                [{"role": "user", "content": "分类"}],
                SCHEMA,
                "local_small",
                config=config_for(mock.base_url),
                usage_path=usage,
                retry_delays=(0.0, 0.0, 0.0),
                sleep=lambda _: None,
            )
        lines = [json.loads(line) for line in usage.read_text(encoding="utf-8").splitlines()]
        assert len(lines) == 2, "失败的尝试也要记账，否则成本账会漏掉重试"
        assert [entry["ok"] for entry in lines] == [False, True]
        assert all(entry["tier"] == "local_small" for entry in lines)


# ==================================================== 2. 模型不在时的异常

class TestModelUnavailable:
    def test_connection_refused_raises_clear_error(self, state_dir: Path) -> None:
        cfg = config_for(f"http://127.0.0.1:{free_port()}")
        with pytest.raises(GatewayError) as excinfo:
            chat(
                [{"role": "user", "content": "分类"}],
                SCHEMA,
                "local_small",
                config=cfg,
                usage_path=state_dir / "token_usage.log",
                sleep=lambda _: None,
            )
        message = str(excinfo.value)
        assert "qwen3:4b" in message or "无法连接" in message or "调用失败" in message

    def test_transport_failure_is_not_retried(self, state_dir: Path) -> None:
        """运输层失败直接抛：重试它只会把故障时间拉长，也掩盖真正的原因。"""
        cfg = config_for(f"http://127.0.0.1:{free_port()}")
        slept: list[float] = []
        with pytest.raises(GatewayError):
            chat(
                [{"role": "user", "content": "分类"}],
                SCHEMA,
                "local_small",
                config=cfg,
                usage_path=state_dir / "token_usage.log",
                sleep=slept.append,
            )
        assert slept == []


# ========================================================= 3. mode 状态机

class TestConnectivity:
    def _failing(self) -> ProbeResult:
        return ProbeResult("model", False, "连不上: mock")

    def _ok(self) -> ProbeResult:
        return ProbeResult("model", True, "HTTP 200")

    def test_three_consecutive_failures_switch_to_offline(self, state_dir: Path) -> None:
        mode = state_dir / "mode.json"
        write_mode({"mode": "online"}, mode)

        first = connectivity_check(probes=[self._failing], mode_path=mode)
        second = connectivity_check(probes=[self._failing], mode_path=mode)
        assert first["mode"] == "online" and second["mode"] == "online"

        third = connectivity_check(probes=[self._failing], mode_path=mode)
        assert third["changed"] is True
        assert third["mode"] == "offline"

        stored = json.loads(mode.read_text(encoding="utf-8"))
        assert stored["mode"] == "offline"
        assert stored.get("since"), "offline 必须记下 since，路线明确要求"

    def test_two_consecutive_successes_switch_back_online(self, state_dir: Path) -> None:
        mode = state_dir / "mode.json"
        write_mode({"mode": "offline", "since": "2026-01-01T00:00:00"}, mode)

        first = connectivity_check(probes=[self._ok], mode_path=mode)
        assert first["mode"] == "offline", "只成功一次还不够，路线要求连续 2 次"

        second = connectivity_check(probes=[self._ok], mode_path=mode)
        assert second["changed"] is True and second["mode"] == "online"

        stored = json.loads(mode.read_text(encoding="utf-8"))
        assert stored["mode"] == "online"
        assert "since" not in stored, "恢复后要清掉 since，否则后续读取会误判"
        assert stored.get("recovered_at")

    def test_auth_failure_is_not_connectivity_loss(self, state_dir: Path) -> None:
        mode = state_dir / "mode.json"
        auth_report = state_dir / "auth_failure.md"
        write_mode({"mode": "online"}, mode)

        calls: list[int] = []

        def probe() -> ProbeResult:
            calls.append(1)
            return ProbeResult("github", False, "HTTP 401", auth_failure=True)

        result = connectivity_check(probes=[probe], mode_path=mode, auth_report=auth_report)

        assert result["reason"] == "auth_failure"
        assert result["mode"] == "online", "401/402 不算断联，绝不能切 offline"
        assert len(calls) == 1, "凭证错误重试没有意义，路线要求不重试"
        assert auth_report.exists(), "必须写 auth_failure.md 告警人类"
        assert "401" in auth_report.read_text(encoding="utf-8")
        assert json.loads(mode.read_text(encoding="utf-8"))["mode"] == "online"

    def test_alternating_results_do_not_flip_mode(self, state_dir: Path) -> None:
        """失败一次成功一次这种抖动不该切 offline —— 连续计数就是为此存在的。"""
        mode = state_dir / "mode.json"
        write_mode({"mode": "online"}, mode)
        for _ in range(4):
            connectivity_check(probes=[self._failing], mode_path=mode)
            connectivity_check(probes=[self._ok], mode_path=mode)
        assert json.loads(mode.read_text(encoding="utf-8"))["mode"] == "online"


# ============================================================ 4. embedding

class TestEmbed:
    def test_vectors_are_l2_normalized(self) -> None:
        with MockEndpoint([], embed_vectors=[[3.0, 4.0], [0.0, 2.0]]) as mock:
            vectors = embed(["甲", "乙"], config=config_for(mock.base_url))
            assert mock.calls("/api/embed") == 1
        assert isinstance(vectors, np.ndarray)
        assert vectors.shape == (2, 2)
        norms = np.linalg.norm(vectors, axis=1)
        assert np.allclose(norms, 1.0), f"必须 L2 归一化，实际范数 {norms}"

    def test_zero_vector_is_rejected(self) -> None:
        with MockEndpoint([], embed_vectors=[[0.0, 0.0]]) as mock, pytest.raises(GatewayError) as excinfo:
            embed(["甲"], config=config_for(mock.base_url))
        assert "零向量" in str(excinfo.value)

    def test_shape_mismatch_is_rejected(self) -> None:
        with MockEndpoint([], embed_vectors=[[1.0, 0.0]]) as mock, pytest.raises(GatewayError):
            embed(["甲", "乙"], config=config_for(mock.base_url))

    def test_empty_input_returns_empty(self) -> None:
        result = embed([])
        assert result.shape == (0, 0)


# ============================================================== 5. 校验器

class TestValidator:
    def test_json_schema_dict(self) -> None:
        validate_against(SCHEMA, {"label": "bug"})
        with pytest.raises(GatewayError):
            validate_against(SCHEMA, {"label": "other"})

    def test_pydantic_model(self) -> None:
        from pydantic import BaseModel

        class Verdict(BaseModel):
            label: str

        validate_against(Verdict, {"label": "bug"})
        with pytest.raises(GatewayError):
            validate_against(Verdict, {"label": 1})

    def test_none_schema_accepts_any_object(self) -> None:
        validate_against(None, {"anything": 1})


# ============================================== 6. embedding 的 API 替代接法
#
# 本机小模型（Ollama + bge-m3）的替代路径：`embed.active` 指到 `api` 档，
# 走 OpenAI 兼容的 `/embeddings`。
#
# 这一节钉住的是**接错时的行为**：以前 `embed()` 无论配置写什么都在打本机 Ollama，
# 配置里写 `api_style: openai` 的人会一直打 127.0.0.1:11434 拿到一个莫名其妙的失败。


def embed_config_for(
    base_url: str,
    *,
    active: str = "api",
    dim: int | None = 3,
    local_dim: int | None = None,
) -> dict:
    """两个 embed 档位都指向同一个假服务，用 `active` 选当前用哪个。"""
    local: dict = {"base_url": base_url, "model": "bge-m3:latest", "api_style": "ollama"}
    if local_dim is not None:
        local["dim"] = local_dim
    api: dict = {"base_url": base_url, "model": "text-embedding-3-small", "api_style": "openai"}
    if dim is not None:
        api["dim"] = dim
    return {"embed": {"active": active, "local": local, "api": api}}


def inject_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """假 key。只注入"取到了"这件事，绝不把真 key 带进测试。"""
    monkeypatch.setattr("src.gateway.gateway.load_api_key", lambda *a, **k: ("sk-test", "测试注入"))


class TestEmbedApiAlternative:
    def test_api_style_openai_goes_to_the_embeddings_endpoint(self, monkeypatch: pytest.MonkeyPatch) -> None:
        inject_api_key(monkeypatch)
        embeddings = [
            {"index": 0, "embedding": [3.0, 4.0, 0.0]},
            {"index": 1, "embedding": [0.0, 0.0, 2.0]},
        ]
        with MockEndpoint([], embeddings=embeddings) as mock:
            vectors = embed(["甲", "乙"], config=embed_config_for(mock.base_url))
            assert mock.calls("/embeddings") == 1
            assert mock.calls("/api/embed") == 0, "走 API 档时不该再去打本机 Ollama"
        assert vectors.shape == (2, 3)
        assert np.allclose(np.linalg.norm(vectors, axis=1), 1.0)

    def test_active_local_still_uses_ollama(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """**向后兼容**：`active: local`（或不写 active）时行为与从前一致。"""
        inject_api_key(monkeypatch)
        with MockEndpoint([], embed_vectors=[[1.0, 0.0, 0.0]]) as mock:
            embed(["甲"], config=embed_config_for(mock.base_url, active="local", local_dim=3))
            assert mock.calls("/api/embed") == 1
            assert mock.calls("/embeddings") == 0

    def test_missing_key_is_reported_before_any_request(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """缺 key 要**立刻**报清楚：不能发一个裸请求去换 401 ——
        401 会被上层当成「凭证被拒」，把真正的病因（根本没配 key）盖住。"""
        monkeypatch.setattr("src.gateway.gateway.load_api_key", lambda *a, **k: ("", "未找到"))
        with MockEndpoint([], embeddings=[{"index": 0, "embedding": [1.0, 0.0, 0.0]}]) as mock:
            with pytest.raises(GatewayError) as excinfo:
                embed(["甲"], config=embed_config_for(mock.base_url))
            assert mock.calls("/embeddings") == 0, "没有 key 就不该发出请求"
        message = str(excinfo.value)
        assert "DEEPSEEK_API_KEY" in message, "要告诉人 key 填在哪个环境变量"
        assert "deepseek_key" in message, "也要给出文件那条路"

    def test_vectors_are_reordered_by_index(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """OpenAI 兼容端点**不保证顺序**。错配不会报错、只会让查重悄悄变错。"""
        inject_api_key(monkeypatch)
        embeddings = [
            {"index": 1, "embedding": [0.0, 1.0, 0.0]},
            {"index": 0, "embedding": [1.0, 0.0, 0.0]},
        ]
        with MockEndpoint([], embeddings=embeddings) as mock:
            vectors = embed(["甲", "乙"], config=embed_config_for(mock.base_url))
        assert np.allclose(vectors[0], [1.0, 0.0, 0.0]), "index=0 的那条必须排回第一位"
        assert np.allclose(vectors[1], [0.0, 1.0, 0.0])

    def test_dimension_mismatch_is_loud(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """换 embedding 模型必改 dim：对不上就报错，而不是让 dedup 阈值静默失效。"""
        inject_api_key(monkeypatch)
        with (
            MockEndpoint([], embeddings=[{"index": 0, "embedding": [1.0, 0.0, 0.0]}]) as mock,
            pytest.raises(GatewayError) as excinfo,
        ):
            embed(["甲"], config=embed_config_for(mock.base_url, dim=1024))
        assert "重标定" in str(excinfo.value)

    def test_unknown_profile_is_loud_and_lists_options(self) -> None:
        """档位名写错不许静默退回 local（那会让人以为在用 API，其实一直在打本机）。"""
        with pytest.raises(GatewayError) as excinfo:
            embed(["甲"], config=embed_config_for("http://127.0.0.1:1"), profile="nope")
        message = str(excinfo.value)
        assert "nope" in message
        assert "api" in message and "local" in message, "要列出可选档位"

    def test_unknown_api_style_is_loud(self) -> None:
        config = {"embed": {"active": "x", "x": {"base_url": "http://127.0.0.1:1", "api_style": "weird"}}}
        with pytest.raises(GatewayError) as excinfo:
            embed(["甲"], config=config)
        assert "weird" in str(excinfo.value)
