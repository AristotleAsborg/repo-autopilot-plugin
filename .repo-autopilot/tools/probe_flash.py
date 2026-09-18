"""确认 flash 档真的可用：列模型 + 发一次结构化请求。

    python tools/probe_flash.py

为什么单独一个脚本：`config/models.yaml` 里 flash 的模型 id 一直是个**没核对过的占位值**
（`deepseek-v4.1-flash`）。没有这一步，第一次真调用会以一个 404/400 的形式
出现在某个业务逻辑深处，而那时你已经在排查别的东西了。

**不打印 key**，只打印它的来源。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.gateway import (
    GatewayError,
    load_api_key,
    load_config,
    request_json,
    tier_config,
)


def main() -> int:
    key, source = load_api_key()
    print(f"key 来源：{source}")
    print(f"key 长度：{len(key)}（不打印内容）")
    if not key:
        return 1

    cfg = tier_config("flash_api", load_config())
    print(f"配置档位：base_url={cfg.base_url} model={cfg.model} temperature={cfg.temperature}")

    status, body = request_json(
        f"{cfg.base_url}/models", None, timeout=30, extra_headers={"Authorization": f"Bearer {key}"}
    )
    print(f"\nGET {cfg.base_url}/models -> HTTP {status}")
    if status == 200 and isinstance(body, dict):
        ids = [item.get("id") for item in body.get("data", [])]
        print("可用模型：" + ", ".join(str(i) for i in ids))
        if cfg.model not in ids:
            print(f"\n!! 配置里的 {cfg.model!r} 不在列表里 —— 需要改 config/models.yaml")

    print("\n发一次真请求（要 JSON 输出）：")
    try:
        candidate = cfg.model if status == 200 and isinstance(body, dict) and cfg.model in [
            item.get("id") for item in body.get("data", [])
        ] else (body.get("data") or [{}])[0].get("id") if status == 200 and isinstance(body, dict) else cfg.model
        print(f"  用模型 id：{candidate}")
        status, reply = request_json(
            f"{cfg.base_url}/chat/completions",
            {
                "model": candidate,
                "messages": [
                    {"role": "system", "content": "只输出 JSON：{\"ok\": true, \"note\": \"一句话\"}"},
                    {"role": "user", "content": "打招呼"},
                ],
                "temperature": 0,
                "response_format": {"type": "json_object"},
            },
            timeout=60,
            extra_headers={"Authorization": f"Bearer {key}"},
        )
        print(f"  HTTP {status}")
        text = (reply.get("choices") or [{}])[0].get("message", {}).get("content") if isinstance(reply, dict) else reply
        print(f"  回复：{str(text)[:200]}")
        usage = reply.get("usage") if isinstance(reply, dict) else None
        if usage:
            print(f"  用量：{json.dumps(usage, ensure_ascii=False)}")
        return 0 if status == 200 else 1
    except GatewayError as exc:
        print(f"  失败：{exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
