"""0.1 的能力探测：**现场探一次这台机器**，把结论落成 `state/reports/roadmap-probe-report.md`。

    python tools/probe_capabilities.py            # 探测并写报告（只读探测，不写任何远端）
    python tools/probe_capabilities.py --print     # 只打印，不落盘

## 为什么需要它（这是安装副本逼出来的）

0.1 的验收原来只做一件事：**找一个早就存在的探测报告文件**。于是在源仓库里它一直绿，
而任何**新装的副本**上它必然红 —— 报告的候选位置有三个，全都是"上一台机器/上一次会话留下的"。
按 TEST_GATE 的要求，验收检查必须是**可执行的**，不能依赖某个必须先存在的历史文件：
"这台机器的能力是什么"本来就必须现在测。

探测什么（对应 `state/capabilities.yaml` 的五个键）：

| 键 | 怎么探 |
|---|---|
| `github_read` | 用读 token 打一次 `GET /rate_limit` |
| `github_write` | 写 token 文件在不在、能不能**只读地**验证它（`GET /user`）——**探测阶段绝不写** |
| `local_model_reachable` | `gateway.model_probe()` 列模型（不消耗推理） |
| `local_model_pulled` | 真调一次 embedding（`bge-m3`），拿到维度才算"装好了" |
| `security_constraints` | `src.sandbox.capabilities()` 说这套沙箱**实际**拦得住什么 |

**yaml 与实测不一致要吼**：`state/capabilities.yaml` 是人写下的结论，如果实测与之相反
（例如 yaml 说 `github_write: true`，而写 token 不见了），那正是最该被看见的事 ——
退出码 1 并在报告里逐条列出。缺件（token 不在、配置文件没了）单独一档退出码 2。
"""

from __future__ import annotations

import argparse
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

STATE = ROOT / "state"
REPORT = STATE / "reports" / "roadmap-probe-report.md"

EXIT_OK = 0
EXIT_MISMATCH = 1
EXIT_MISSING = 2


def probe_github_read() -> tuple[bool | None, str]:
    from src.github import GitHubClient, read_token

    try:
        token = read_token()
    except Exception as exc:  # noqa: BLE001
        return None, f"读 token 取不到（{type(exc).__name__}）：{exc}"
    try:
        payload = GitHubClient(token, timeout=20).get("/rate_limit")
    except Exception as exc:  # noqa: BLE001
        return False, f"读 token 存在但调用失败（{type(exc).__name__}）：{str(exc)[:140]}"
    core = ((payload or {}).get("resources") or {}).get("core") or {}
    return True, f"GET /rate_limit 成功；剩余 {core.get('remaining')}/{core.get('limit')}"


def probe_github_write() -> tuple[bool | None, str]:
    from src.github import GitHubClient
    from src.github.tokens import load_write_token

    target = STATE / ".write_token"
    if not target.is_file():
        return False, f"写 token 文件不存在：{target}（只读流程不受影响）"
    try:
        token = load_write_token(target)
        who = GitHubClient(token, timeout=20).get("/user")
    except Exception as exc:  # noqa: BLE001
        return False, f"写 token 存在但验证失败（{type(exc).__name__}）：{str(exc)[:140]}"
    return True, f"写 token 可用（以 {who.get('login')} 的身份通过只读校验；**探测阶段不写任何东西**）"


def probe_local_model() -> tuple[bool, str, int | None]:
    from src.gateway import embed, model_probe, tier_config

    local = tier_config("local_small")
    reachable = model_probe(local.base_url)
    if not reachable.ok:
        return False, f"探活失败：{reachable.detail}", None
    try:
        vectors = embed(["能力探测：这是一句话。"], raw_embed=None)
    except Exception as exc:  # noqa: BLE001
        return True, f"探活成功但 embedding 失败（{type(exc).__name__}）：{str(exc)[:120]}", None
    return True, f"探活成功（{reachable.detail}）；embedding 维度 {vectors.shape[1]}", vectors.shape[1]


def probe_security() -> str:
    try:
        from src.sandbox import capabilities

        data = capabilities()
    except Exception as exc:  # noqa: BLE001
        return f"（沙箱能力自述取不到：{type(exc).__name__}）"
    if isinstance(data, dict):
        keys = ("sandbox_strength", "network_isolated", "can_limit_memory", "can_limit_processes", "docker")
        return "；".join(f"{key}={data.get(key)}" for key in keys if key in data) or str(data)[:200]
    return str(data)[:200]


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

    parser = argparse.ArgumentParser(description="0.1 能力探测（现场探）")
    parser.add_argument("--print", dest="print_only", action="store_true", help="只打印，不落盘")
    args = parser.parse_args()

    yaml_path = STATE / "capabilities.yaml"
    if not yaml_path.exists():
        print(f"缺件：{yaml_path} 不存在（0.1 的结论本来记在这里）")
        return EXIT_MISSING

    import yaml

    recorded = yaml.safe_load(yaml_path.read_text(encoding="utf-8")) or {}

    read_ok, read_detail = probe_github_read()
    write_ok, write_detail = probe_github_write()
    model_ok, model_detail, dim = probe_local_model()
    security = probe_security()
    docker_available = shutil.which("docker") is not None

    measured = {
        "github_read": bool(read_ok),
        "github_write": bool(write_ok),
        "local_model_reachable": bool(model_ok),
        "local_model_pulled": bool(model_ok and dim),
        "docker_available": docker_available,
    }

    mismatches: list[str] = []
    for key, value in measured.items():
        if key in recorded and bool(recorded[key]) != value:
            mismatches.append(f"{key}：yaml 记的是 {recorded[key]}，实测是 {value}")

    missing = [key for key in ("github_read", "github_write", "local_model_reachable", "local_model_pulled")
               if read_ok is None or (key == "github_write" and write_ok is None)]
    if read_ok is None:
        missing.append("github_read:读token取不到")

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    lines = [
        "# 0.1 能力探测报告（现场实测）",
        "",
        f"- 时间：{stamp}（UTC）",
        f"- 机器：{sys.platform}，Python {sys.version.split()[0]}",
        f"- 结论：**{'与 state/capabilities.yaml 一致' if not mismatches else '与记录不一致（见下）'}**",
        "",
        "| 能力 | 实测 | 证据 |",
        "|---|---|---|",
        f"| github_read | {read_ok} | {read_detail} |",
        f"| github_write | {write_ok} | {write_detail} |",
        f"| local_model_reachable | {model_ok} | {model_detail} |",
        f"| local_model_pulled | {bool(model_ok and dim)} | embedding 维度 {dim} |",
        f"| docker_available | {docker_available} | `shutil.which('docker')` |",
        "",
        f"- 安全约束自述：{security}",
        "",
        "> 这份报告是**现场探测**的产物（`tools/probe_capabilities.py`）。",
        (
            "> 之所以要现场探：0.1 的验收检查必须是可执行的，不能依赖"
            "「上一台机器留下的报告文件」—— 新装的副本上那种文件本来就不会有。"
        ),
    ]
    if mismatches:
        lines += ["", "## 与记录不一致（需要人看）", ""]
        lines += [f"- {item}" for item in mismatches]

    text = "\n".join(lines) + "\n"
    print(text)
    if not args.print_only:
        REPORT.parent.mkdir(parents=True, exist_ok=True)
        REPORT.write_text(text, encoding="utf-8")
        print(f"报告：{REPORT.relative_to(ROOT).as_posix()}")

    if mismatches:
        return EXIT_MISMATCH
    if missing:
        print(f"缺件：{missing}")
        return EXIT_MISSING
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
