"""验收脚本：`state/progress.md` 的步骤状态**只能**由本脚本写入。

## 为什么这个脚本必须存在

路线 0.2 写明：「`state/progress.md` 的步骤状态只能由验收脚本自动写入（脚本置 PASS），
你手工编辑该文件不改变状态——提示词管行为，代码管状态，双保险。」
没有它，之后每一步的 PASS 都只是 AI 的口头声明，而这正是路线第 14 条
（「AI 用『基本完成/核心已通过』糊弄验收」）要防的事。

## 契约（对齐路线 0.2 / 0.3）

* 状态只有四种：`PASS` / `BLOCKED` / `DOING` / `TODO`。
* `PASS` 只能来自**可执行的检查**（测试命令、文件断言），且原始输出必须留痕到
  `state/reports/acceptance/`。
* 失败重试 <= 2 次，仍失败置 `BLOCKED`，并**立即停止**，不再评估后续步骤。
* 本脚本只重写表格块；表格之前、之后的内容原样保留。
* 一个步骤若没有登记检查，只能取 `TODO`（或保留此前的 `PASS`），绝不凭空给 PASS。

## 用法

    python tools/acceptance.py --all
    python tools/acceptance.py --step 1.2
    python tools/acceptance.py --list

注意：本脚本会拉起 pytest 子进程，按本项目实测的教训，**必须**在 DSH 工具管道之外
运行（见 `state/run-acceptance.cmd`），否则可能把 DSH 进程拖死。
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import json
import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / "state"
PROGRESS = STATE / "progress.md"
EVIDENCE_DIR = STATE / "reports" / "acceptance"

CHECK_TIMEOUT_SECONDS = 600
#: 3.1 的冻结 holdout 回放要跑 160+ 条本地模型推理，实测 ~400s，600s 余量太小
TRIAGE_EVAL_TIMEOUT_SECONDS = 1800
#: 3.2 的查重回放要 231+ 条 embedding + 一次全量矩阵，实测 ~150s
DEDUPE_EVAL_TIMEOUT_SECONDS = 900
#: 4.2 的修复成功率要真调 flash + 真跑沙箱，实测 ~60s（5 条样本，每条 1 轮）
REPAIR_EVAL_TIMEOUT_SECONDS = 1500
#: holdout 样本量下限。50 条下 1 条 issue = 2 个百分点，分不清 74.5% 与 76.5%
MIN_HOLDOUT = 150
RETRIES = 2          # 失败后重试次数上限（总尝试 = 1 + RETRIES）
LEGAL_STATUSES = ("PASS", "BLOCKED", "DOING", "TODO")


@dataclasses.dataclass
class Result:
    ok: bool
    detail: str


CheckFn = Callable[[Path], Result]


# ----------------------------------------------------------- 读 token 的来源

def load_read_token() -> tuple[str, str]:
    """
    读 token 的来源，返回 (token, 来源说明)。

    **解析逻辑只有一处**，在 `src.github.tokens`。验收脚本和网关各写一遍的话，
    很快就会在"到底认哪些落点"上分叉，而那种分叉的表现是
    "验收过了、生产读不到 token" —— 最难查的一类问题。
    这里只负责把仓库根加进 sys.path，再把异常转成"取不到"。
    """
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from src.github.tokens import read_token, read_token_source

    try:
        return read_token(), read_token_source()
    except LookupError:
        return "", read_token_source()


def _child_env() -> dict[str, str]:
    """子进程环境：把读 token 统一成 GH_READ_TOKEN，交给被测脚本使用。"""
    env = dict(os.environ)
    token, _ = load_read_token()
    if token:
        env["GH_READ_TOKEN"] = token
    return env


# --------------------------------------------------------------- 通用检查件

def pytest_check(relpath: str) -> CheckFn:
    """跑一个 pytest 文件/目录。输出重定向到**文件**，不用管道（本机实测管道不可靠）。"""

    def run(record: Path) -> Result:
        target = ROOT / relpath
        if not target.exists():
            return Result(False, f"缺少测试目标 {relpath}")
        argv = [sys.executable, "-m", "pytest", relpath, "-q", "--tb=short"]
        return _run_to_file(argv, record)

    return run


def script_check(relpath: str) -> CheckFn:
    """直接跑一个脚本（不是 pytest 模块，没有 test_ 函数的那种）。"""

    def run(record: Path) -> Result:
        target = ROOT / relpath
        if not target.exists():
            return Result(False, f"缺少脚本 {relpath}")
        return _run_to_file([sys.executable, relpath], record)

    return run


def _run_to_file(argv: list[str], record: Path, timeout: int = CHECK_TIMEOUT_SECONDS) -> Result:
    record.parent.mkdir(parents=True, exist_ok=True)
    with open(record, "w", encoding="utf-8") as handle:
        handle.write("$ " + " ".join(argv) + "\n\n")
        handle.flush()
        try:
            proc = subprocess.run(
                argv,
                cwd=str(ROOT),
                stdout=handle,
                stderr=subprocess.STDOUT,   # 合并进同一个**文件**句柄，不建管道
                timeout=timeout,
                env=_child_env(),
                check=False,                # 退出码由下面显式判断（check=True 会抛异常，丢掉日志尾）
            )
        except subprocess.TimeoutExpired:
            return Result(False, f"超时（>{timeout}s）：{' '.join(argv)}")
    text = record.read_text(encoding="utf-8", errors="replace").strip()
    tail = "\n".join(text.splitlines()[-20:])
    if proc.returncode == 0:
        return Result(True, f"exit=0\n{tail}")
    extra = ""
    if proc.returncode == 5:
        extra = "（pytest 退出码 5 = 没收集到用例：该文件不是 pytest 模块）"
    return Result(False, f"exit={proc.returncode}{extra}\n{tail}")


# ------------------------------------------------------------- 各步骤的检查

def check_capabilities(record: Path) -> Result:
    path = STATE / "capabilities.yaml"
    if not path.exists():
        return Result(False, "缺少 state/capabilities.yaml")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    required = [
        "github_read",
        "github_write",
        "local_model_reachable",
        "local_model_pulled",
        "security_constraints",
    ]
    missing = [key for key in required if key not in data]
    if missing:
        return Result(False, f"capabilities.yaml 缺少键: {missing}")
    # 探测报告：仓库内优先（证据必须自带在仓库里），
    # 其次才是工作区根——早期探索留下的原件在那里，不能假设它一定存在。
    candidates = [
        STATE / "reports" / "roadmap-probe-report.md",
        ROOT / "_roadmap-probe-report.md",
        ROOT.parent / "_roadmap-probe-report.md",
    ]
    report = next((path for path in candidates if path.exists()), None)
    if report is None:
        # **现场探一次**（2026-09-12 安装副本逼出来的修法）：
        # 原来这里只找文件，于是在源仓库里一直绿，而任何**新装的副本**上必然红 ——
        # 候选位置全都是"上一台机器/上一次会话留下的"。按 TEST_GATE，检查必须是**可执行的**；
        # "这台机器的能力是什么"本来就必须现在测，而不是去找一份历史报告。
        probe = _run_to_file([sys.executable, "tools/probe_capabilities.py"], record)
        if not probe.ok:
            return Result(False, f"缺少探测报告，现场补探也没成功：\n{probe.detail}")
        report = next((path for path in candidates if path.exists()), None)
        if report is None:
            return Result(False, f"现场探测跑完了，但报告没落盘：{probe.detail}")
    return Result(
        True,
        f"键齐全 {required}；探测报告={report}；github_read={data['github_read']}；"
        f"local_model_pulled={data['local_model_pulled']}",
    )


def check_model_baseline(record: Path) -> Result:
    evals = sorted(STATE.glob("reports/eval_classify_*.json"))
    if not evals:
        return Result(False, "没有 state/reports/eval_classify_*.json")
    newest = evals[-1]
    data = json.loads(newest.read_text(encoding="utf-8"))
    accuracy = data.get("accuracy_pct")
    if accuracy is None:
        return Result(False, f"{newest.name} 里没有 accuracy_pct")
    schema_failures = data.get("schema_failures")
    ok = accuracy >= 70 and schema_failures == 0
    return Result(
        bool(ok),
        f"{newest.name}: accuracy_pct={accuracy}（要求 >=70）、"
        f"schema_failures={schema_failures}（要求 0）、"
        f"valid_samples={data.get('valid_samples')}、model={data.get('model')}",
    )


def check_embedding_evidence(record: Path) -> Result:
    log = STATE / "reports" / "embed-check.log"
    if not log.exists():
        return Result(False, "缺少 state/reports/embed-check.log")
    text = log.read_text(encoding="utf-8", errors="replace")
    has_dimension = "1024" in text
    return Result(
        has_dimension,
        f"embed-check.log 存在；含 1024 维证据={has_dimension}"
        "（绝对阈值的偏差记在 state/reports/model_bench_2026-09-12.md，不在此处重判）",
    )


def check_stop_judger(record: Path) -> Result:
    """
    步骤 2.2 的验收有两半：

      1. 三路信号与表决的确定性测试（含"某一路失灵时绝不误停"这条安全性质）；
      2. 20 组样本的准确率 ≥80%（路线写明"才上线"）。

    第 2 半必须真的跑，不能用"测试全绿"代替 —— 门槛是准确率，不是有没有报错。
    """
    unit = _run_to_file(
        [sys.executable, "-m", "pytest", "tests/integration/test_stopper.py", "-q", "--tb=short"],
        record,
    )
    if not unit.ok:
        return Result(False, f"确定性测试没通过：\n{unit.detail}")

    second = record.with_name(record.name.replace(".log", "-eval.log"))
    evaluation = _run_to_file([sys.executable, "tools/eval_stopper.py"], second)
    with open(record, "a", encoding="utf-8") as handle:
        handle.write(f"\n\n---- 20 组样本评测 ----\n{evaluation.detail}\n")
    if not evaluation.ok:
        return Result(False, f"20 组样本准确率未达 80%：\n{evaluation.detail}")
    return Result(True, f"确定性测试通过；20 组样本达标\n{evaluation.detail}")


def check_triage(record: Path) -> Result:
    """
    步骤 3.1 的验收三条线：

      1. 确定性测试（防注入、动作分级、长文压缩降级）；
      2. **冻结的 holdout 回放**：准确率 ≥75%，**spam/dupe 误杀率 ≤5%**（路线原话：比准确率更硬的线），
         注入样本全部只被当数据处理，0 条失败；
      3. 样本量下限：holdout ≥150 条。

    误杀率单独报，是因为两条线的失败含义完全不同：准确率差一点只是标签不合适，
    误杀的后果是**把人赶走**，而他往往不会再回来解释。

    考卷为什么变了（分数线一条没动）：见 `state/findings/triage-metric-underpowered.md`。
    50 条样本下 1 条 issue = 2 个百分点，同一份代码在两批样本上量出 74.5% 与 81.7%
    —— 那是考卷在变（GitHub search 每天漂移），不是模型在变。现在考卷**冻结在磁盘**上，
    且**只用从未参与调参的 holdout 报分**（dev 只用来改提示词）。
    """
    unit = _run_to_file(
        [sys.executable, "-m", "pytest", "tests/integration/test_triage.py", "-q", "--tb=short"],
        record,
    )
    if not unit.ok:
        return Result(False, f"确定性测试没通过：\n{unit.detail}")

    replay = ROOT / "state" / "corpus" / "triage-replay.jsonl"
    holdout = 0
    if replay.exists():
        with open(replay, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                row = json.loads(line)
                if (row.get("cohort") or "dev") == "holdout":
                    holdout += 1
    if holdout < MIN_HOLDOUT:
        return Result(
            False,
            f"冻结回放集的 holdout 只有 {holdout} 条（要求 ≥{MIN_HOLDOUT}）。"
            "先跑 tools/freeze_triage_set.py --search --per-query 55 "
            '--query-extra "created:<2024-01-01" --cohort holdout',
        )

    second = record.with_name(record.name.replace(".log", "-eval.log"))
    evaluation = _run_to_file(
        [sys.executable, "tools/eval_triage.py", "--from-replay", "--split", "holdout",
         "--tag", "holdout1"],
        second,
        timeout=TRIAGE_EVAL_TIMEOUT_SECONDS,
    )
    with open(record, "a", encoding="utf-8") as handle:
        handle.write(f"\n\n---- 冻结 holdout {holdout} 条回放 ----\n{evaluation.detail}\n")
    if not evaluation.ok:
        return Result(False, f"holdout 回放未达标（{holdout} 条）：\n{evaluation.detail}")
    return Result(True, f"确定性测试通过；冻结 holdout {holdout} 条回放达标\n{evaluation.detail}")


def check_dedupe(record: Path) -> Result:
    """
    步骤 3.2 的验收两条线：

      1. 确定性测试（向量库行序契约、阈值边界、路由表、入站顺序、5000 条性能）；
      2. 真模型回放：同义改写 10 对召回 ≥8、相似但不同 10 对误报 ≤2、5000 条查询 <2s。

    阈值**不是路线里的 0.92/0.98**：那是起点不是真理，路线自己要求用回放数据画曲线。
    实测（bge-m3）同义改写的余弦只有 0.80～0.93，照抄 0.92 的召回是 0/10 ——
    阈值定高了功能等于不存在。选阈值的规则先写死在 `tools/eval_dedupe.py` 里
    （满足两条线的档位里最大化 `召回 − 2×误报`），再看结果，避免拿测试集调参。
    """
    unit = _run_to_file(
        [sys.executable, "-m", "pytest", "tests/integration/test_dedupe.py", "-q", "--tb=short"],
        record,
    )
    if not unit.ok:
        return Result(False, f"确定性测试没通过：\n{unit.detail}")

    second = record.with_name(record.name.replace(".log", "-eval.log"))
    evaluation = _run_to_file(
        [sys.executable, "tools/eval_dedupe.py"],
        second,
        timeout=DEDUPE_EVAL_TIMEOUT_SECONDS,
    )
    with open(record, "a", encoding="utf-8") as handle:
        handle.write(f"\n\n---- 查重回放（真模型） ----\n{evaluation.detail}\n")
    if not evaluation.ok:
        return Result(False, f"查重回放未达标：\n{evaluation.detail}")
    return Result(True, f"确定性测试通过；查重回放达标\n{evaluation.detail}")


def check_bounce(record: Path) -> Result:
    """
    步骤 3.3 的验收三条线：

      1. 单元 + 集成：打回原因/来源是闭集、记账幂等、≥2 次不同事实转 `needs_human`、
         改标签走闸门且只在批准后执行、离线落 outbox、裁决过的样本从 holdout 移进 dev；
      2. 回放：在 3.1 的冻结 holdout 上量打回率（≤20%）与**误打回率**（≤5%，
         即"把标着 bug 的 issue 打回"——真有缺陷却拒绝修）；
      3. 已裁决样本不得再次被打回（没有裁决样本时这条是空断言，会在日志里如实标注）。
    """
    unit = _run_to_file(
        [
            sys.executable,
            "-m",
            "pytest",
            "tests/unit/test_bounce.py",
            "tests/integration/test_bounce_flow.py",
            "-q",
            "--tb=short",
        ],
        record,
    )
    if not unit.ok:
        return Result(False, f"确定性与集成测试没通过：\n{unit.detail}")

    second = record.with_name(record.name.replace(".log", "-eval.log"))
    evaluation = _run_to_file(
        [sys.executable, "tools/eval_bounce.py"], second, timeout=DEDUPE_EVAL_TIMEOUT_SECONDS
    )
    with open(record, "a", encoding="utf-8") as handle:
        handle.write(f"\n\n---- 打回通路回放 ----\n{evaluation.detail}\n")
    if not evaluation.ok:
        return Result(False, f"打回回放未达标：\n{evaluation.detail}")
    return Result(True, f"确定性测试通过；打回回放达标\n{evaluation.detail}")


def check_localize(record: Path) -> Result:
    """
    步骤 4.1 的验收两条线：

      1. 确定性测试：文件树过滤、四类线索的权重、候选 **≤5**、以及两条硬约束
         （全程不调 flash、提示词里不出现任何文件正文 —— 用"只存在于正文里的哨兵字符串"证明）；
      2. 真值样本回放：10 条"真文件 + 合成 issue"里 **≥8 条把正确文件放进前 5**。

    路线 4.1 只写了产物形态（"≤5 个候选文件清单"）没写数量线，这条 ≥80% 是按邻步体例补的，
    同时写在 `ROADMAP.md` 的 4.1 验收里 —— 一个步骤没有验收就没有终点。
    """
    unit = _run_to_file(
        [sys.executable, "-m", "pytest", "tests/integration/test_localize.py", "-q", "--tb=short"],
        record,
    )
    if not unit.ok:
        return Result(False, f"确定性测试没通过：\n{unit.detail}")

    second = record.with_name(record.name.replace(".log", "-eval.log"))
    evaluation = _run_to_file(
        [sys.executable, "tools/eval_localize.py"], second, timeout=DEDUPE_EVAL_TIMEOUT_SECONDS
    )
    with open(record, "a", encoding="utf-8") as handle:
        handle.write(f"\n\n---- 文件定位真值样本回放 ----\n{evaluation.detail}\n")
    if not evaluation.ok:
        return Result(False, f"定位命中率未达标：\n{evaluation.detail}")
    return Result(True, f"确定性测试通过；定位命中率达标\n{evaluation.detail}")


def check_repair(record: Path) -> Result:
    """
    步骤 4.2 的验收两条线：

      1. 确定性测试：增量补丁的解析/应用/拒绝（整文件重写、路径越界、search 漂移）、
         可续跑中间态、`make_patch` 产物能被真实 `git apply` 打上；
      2. 真跑修复：在 5 条**注入已知缺陷**的本地样本上，成功率 ≥40%（路线 4.2 v1 的线）。

    第 2 条要真调 flash（走 `DEEPSEEK_API_KEY` 或 `$DSH_HOME/.deepseek_key`）与真沙箱。
    本地替身样本不是 SWE-smith（本机装不了），所以这个成功率是**上界**，见
    `tools/eval_repair.py` 的头注释。
    """
    unit = _run_to_file(
        [
            sys.executable,
            "-m",
            "pytest",
            "tests/integration/test_repair.py",
            "tests/integration/test_sandbox_patch.py",
            "-q",
            "--tb=short",
        ],
        record,
    )
    if not unit.ok:
        return Result(False, f"确定性测试没通过：\n{unit.detail}")

    second = record.with_name(record.name.replace(".log", "-eval.log"))
    evaluation = _run_to_file(
        [sys.executable, "tools/eval_repair.py"],
        second,
        timeout=REPAIR_EVAL_TIMEOUT_SECONDS,
    )
    with open(record, "a", encoding="utf-8") as handle:
        handle.write(f"\n\n---- 注入缺陷修复回放 ----\n{evaluation.detail}\n")
    if not evaluation.ok:
        return Result(False, f"修复成功率未达标：\n{evaluation.detail}")
    return Result(True, f"确定性测试通过；修复成功率达标\n{evaluation.detail}")


def check_gate(record: Path) -> Result:
    """
    步骤 4.3 的验收两条线：

      1. 确定性测试：四道关各自能拦（黑名单路径表、目标测试、全量回归差集、lint 增量）、
         以及"不该拦的不拦"（基线本来就红的用例不算新增；lint 没涨就放过）；
      2. 真跑三个场景：**场景 A** 过目标测试但破坏另一测试 → 拦；**场景 B** 改测试文件 →
         黑名单拦且**不执行任何命令**；**场景 C** 无害补丁 → 必须放行（只会拦不会放，
         就是另一种坏掉）。

    基线是**实测**的（`capture_baseline` 在未打补丁的副本上跑一遍），不是写死的 0 ——
    写死 0 会把"无害补丁"判成新增告警，这门禁就成了随机拦人。
    """
    unit = _run_to_file(
        [sys.executable, "-m", "pytest", "tests/integration/test_gatekeep.py", "-q", "--tb=short"],
        record,
    )
    if not unit.ok:
        return Result(False, f"确定性测试没通过：\n{unit.detail}")

    second = record.with_name(record.name.replace(".log", "-eval.log"))
    evaluation = _run_to_file(
        [sys.executable, "tools/eval_gate.py"], second, timeout=DEDUPE_EVAL_TIMEOUT_SECONDS
    )
    with open(record, "a", encoding="utf-8") as handle:
        handle.write(f"\n\n---- 门禁三场景真跑 ----\n{evaluation.detail}\n")
    if not evaluation.ok:
        return Result(False, f"门禁场景不符合预期：\n{evaluation.detail}")
    return Result(True, f"确定性测试通过；三个场景符合预期\n{evaluation.detail}")


def check_publish(record: Path) -> Result:
    """
    步骤 4.4 的验收两条线：

      1. 确定性测试：报告五段齐全（缺一段要在正文里明说这是缺陷）、**两次闸门**
         （没点头一个 REST 调用都不发生）、离线进 outbox、拒绝回流、分支名与黑名单；
      2. 真演练：`tools/e2e_publish.py --check` —— 造补丁 → 跑 4.3 门禁 → 写报告 →
         建审批单 → **停在闸门**（`--check` 把"停在闸门"当成通过）。

    为什么真实 PR 不在这里自动建：那正是 4.4 要证明会拦住的东西 ——
    路线写的是"人类只说两次'是'"，所以那两次点头必须由人来给，
    验收只证明"链路通、且没有点头就走不动"。
    """
    unit = _run_to_file(
        [sys.executable, "-m", "pytest", "tests/integration/test_publish.py", "-q", "--tb=short"],
        record,
    )
    if not unit.ok:
        return Result(False, f"确定性测试没通过：\n{unit.detail}")

    second = record.with_name(record.name.replace(".log", "-eval.log"))
    evaluation = _run_to_file(
        [sys.executable, "tools/e2e_publish.py", "--check"],
        second,
        timeout=DEDUPE_EVAL_TIMEOUT_SECONDS,
    )
    with open(record, "a", encoding="utf-8") as handle:
        handle.write(f"\n\n---- 发布链路演练（停在闸门） ----\n{evaluation.detail}\n")
    if not evaluation.ok:
        return Result(False, f"发布链路演练没通过：\n{evaluation.detail}")
    return Result(True, f"确定性测试通过；演练链路通且停在闸门\n{evaluation.detail}")


def check_scout(record: Path) -> Result:
    """
    步骤 5.1 的验收**三条线**：

      1. 确定性测试：许可证查表（GPL/AGPL 一律 block、模型说 ok 也改回来）、硬过滤、
         "没有代码就拒评"、tarball 解包（含 zip-slip 防护）与 LICENSE 副本；
      2. **活体取材冒烟**（`--smoke`）：真的搜一次，四条来源（关键词 / 主题 / 策展清单 /
         已知项目）**每条都要带回候选**，且深评真的取到了证据；
      3. **冻结核对的严格口径**（`--from-frozen`）：5 个真实需求各 ≥3 个"模型判定相关
         （relevance ≥ `RELEVANCE_FLOOR`）且 usage≠skip 且许可证不是 block"的候选，外加 GPL 必须被拦
         （真实 API 数据：moodle/moodle = GPL-3.0），以及"识别不出许可证（git/git 报
         NOASSERTION）也拦在 ok 之外"。
         相关度下限 2026-09-12 由人类拍板从 0.5 调到 0.4（理由与噪声证据见
         `state/findings/scout-sourcing-direction-c.md`）；破格通道的门槛 0.6 未动。

    第 2 条与第 3 条**分工明确、缺一不可**：冻结核对保证指标可复现（模型不是逐位可复现的，
    不冻的话同一份代码能在 4/5 与 2/5 之间摆动），但冻文件不会自己变旧报错 ——
    如果取材三条来源哪天悄悄断了，只跑冻结仍然会绿；所以必须有一条真的去搜的断言。
    """
    unit = _run_to_file(
        [sys.executable, "-m", "pytest", "tests/integration/test_scout.py",
         "tests/integration/test_scout_sources.py", "-q", "--tb=short"],
        record,
    )
    if not unit.ok:
        return Result(False, f"确定性测试没通过：\n{unit.detail}")

    smoke_log = record.with_name(record.name.replace(".log", "-smoke.log"))
    smoke = _run_to_file(
        [sys.executable, "tools/eval_scout.py", "--smoke"], smoke_log, timeout=DEDUPE_EVAL_TIMEOUT_SECONDS
    )
    with open(record, "a", encoding="utf-8") as handle:
        handle.write(f"\n\n---- 猎手活体取材冒烟（四条来源） ----\n{smoke.detail}\n")
    if not smoke.ok:
        return Result(False, f"取材冒烟没通过（四条来源里有断的）：\n{smoke.detail}")

    second = record.with_name(record.name.replace(".log", "-eval.log"))
    evaluation = _run_to_file(
        [sys.executable, "tools/eval_scout.py", "--from-frozen"],
        second,
        timeout=DEDUPE_EVAL_TIMEOUT_SECONDS,
    )
    with open(record, "a", encoding="utf-8") as handle:
        handle.write(f"\n\n---- 猎手冻结核对（严格口径） ----\n{evaluation.detail}\n")
    if not evaluation.ok:
        return Result(False, f"猎手冻结核对未达标：\n{evaluation.detail}")
    return Result(True, f"确定性测试通过；四条来源冒烟通；5 个需求达标且 GPL 被拦\n{evaluation.detail}")


def check_drill(record: Path) -> Result:
    """
    步骤 8.2（7 天实战演练）的验收：**记录本身要经得起审**，然后才轮到人类评审。

    `tools/daily_drill.py --summary` 的退出码就是这条线的判定，它审五件事：
      1. 7 条记录、天数编号 1..7 连续；
      2. **日期互不相同且逐日连续** —— 这条防的是"一天跑七次冒充连续 7 天"（本系统最容易
         自欺的地方）；同一天两条记录一律判不合格；
      3. 每天都得是 `full` 模式（真调本地模型），`--offline` 只验骨架，不算数；
      4. 每天的**对外写调用必须为 0**（`WriteTripwire` 实测，不是"我们没调"的口头保证）；
      5. 每天"通过"：误操作一票否决、人类介入 ≤2 次。

    通过之后也**不等于上线**：路线 8.2 ⑥ 的最后一步是人类评审补丁质量。
    """
    summary = _run_to_file([sys.executable, "tools/daily_drill.py", "--summary"], record)
    with open(record, "a", encoding="utf-8") as handle:
        handle.write(f"\n\n---- 7 天演练台账审计 ----\n{summary.detail}\n")
    if not summary.ok:
        return Result(False, f"演练台账还不合格（或还没满 7 天）：\n{summary.detail}")
    return Result(
        True,
        "7 天记录合格（连续、互不同日、每天 full 模式、对外写 0 次、人类介入 ≤2）\n"
        f"{summary.detail}\n"
        "**仍需人类评审补丁质量**（路线 8.2 ⑥ 的最后一步，脚本不代替）。",
    )


def check_scaffold(record: Path) -> Result:
    """
    步骤 6.1 的验收两条线：

      1. 确定性测试：生成物齐件校验（含三条**环境约束**：pytest 得能找到包、
         ruff 规则集要钉死、用例不许碰系统 temp）、名字只认候选/序号/合法 slug、
         本地 CI 干跑聚合、建仓必须 `auto_init`、首推每个文件都提交、CI 轮询有超时；
      2. 真生成：一句 idea → flash 生成骨架 → **本地 CI 干跑（就地 + 沙箱副本）** 全绿 →
         三个候选名查重 → **停在闸门**（`--check` 把"停在闸门等人类"当成通过）。

    真实的建仓 + 首推 + 云端 CI 绿要人类两次决策（选名 + 批准建仓），不在本检查里自动做。
    """
    unit = _run_to_file(
        [sys.executable, "-m", "pytest", "tests/integration/test_scaffold.py", "-q", "--tb=short"],
        record,
    )
    if not unit.ok:
        return Result(False, f"确定性测试没通过：\n{unit.detail}")

    second = record.with_name(record.name.replace(".log", "-eval.log"))
    evaluation = _run_to_file(
        [sys.executable, "tools/eval_scaffold.py", "--check"],
        second,
        timeout=TRIAGE_EVAL_TIMEOUT_SECONDS,
    )
    with open(record, "a", encoding="utf-8") as handle:
        handle.write(f"\n\n---- 一句 idea → 骨架 → 本地 CI 干跑 ----\n{evaluation.detail}\n")
    if not evaluation.ok:
        return Result(False, f"脚手架流程未达标：\n{evaluation.detail}")
    return Result(True, f"确定性测试通过；骨架生成本地全绿并停在闸门\n{evaluation.detail}")


def check_skills(record: Path) -> Result:
    """
    第七部分的验收两条线（路线 7.2 第 5 条）：

      1. 确定性测试：五段式校验（缺段/改 TEST_GATE/空确认点都要报）、
         指令清单 ↔ AGENTS.md ↔ skills/ 一致性、`/应急` 自检能分辨 offline 与故障、
         批量引擎的五条约束（上限 20 / 可续跑 / 单条失败不阻断 / 逐条审批 / 可暂停）、
         CLI 5 个子命令的 dry-run 路径；
      2. 真跑一遍：8 条指令加载并校验 → 一致性 → 自然语言"系统好像坏了"路由到 `/应急` →
         **在 state 副本里删掉一个子目录**，断言自检报出它（真实 state 只读）。
    """
    unit = _run_to_file(
        [sys.executable, "-m", "pytest", "tests/integration/test_skills.py", "-q", "--tb=short"],
        record,
    )
    if not unit.ok:
        return Result(False, f"确定性测试没通过：\n{unit.detail}")

    second = record.with_name(record.name.replace(".log", "-eval.log"))
    evaluation = _run_to_file(
        [sys.executable, "tools/eval_skills.py"], second, timeout=DEDUPE_EVAL_TIMEOUT_SECONDS
    )
    with open(record, "a", encoding="utf-8") as handle:
        handle.write(f"\n\n---- 指令外壳 + 应急兜底 ----\n{evaluation.detail}\n")
    if not evaluation.ok:
        return Result(False, f"指令外壳验收未达标：\n{evaluation.detail}")
    return Result(True, f"确定性测试通过；8 条指令与应急兜底达标\n{evaluation.detail}")


def check_stage8(record: Path) -> Result:
    """
    第八部分的验收（8.4 门禁脚本 + 8.2 脏测试工具）：

      1. 确定性测试：黑名单/完整性/演练/退化检测的单元行为；
      2. `scripts/check_blacklist.py` —— **正反两面都验**：碰测试文件必须 exit 1（正面拦截），
         只碰源码必须 exit 0（不能误杀）；
      3. `scripts/check_test_integrity.py --self-test` 能跑通且数字合理；
      4. `tools/daily_drill.py --day 1 --offline` —— 一天演练：对抗样本只被当数据、零对外写。

    7 天连续演练（8.2 ⑥）不在这里自动做：它要**真的连续跑 7 天**，第 7 天还要人评审补丁质量。
    """
    unit = _run_to_file(
        [sys.executable, "-m", "pytest", "tests/integration/test_stage8.py", "-q", "--tb=short"],
        record,
    )
    if not unit.ok:
        return Result(False, f"确定性测试没通过：\n{unit.detail}")

    second = record.with_name(record.name.replace(".log", "-eval.log"))
    # 反面：碰测试文件必须被拦（退出码 1 → Result.ok=False）
    blocked = _run_to_file(
        [sys.executable, "scripts/check_blacklist.py", "--files", "tests/integration/test_stage8.py"],
        second,
    )
    if blocked.ok:
        return Result(False, "路径黑名单**放行了**对测试文件的改动 —— 门禁失效")
    # 正面：只改源码不该被拦
    allowed = _run_to_file(
        [sys.executable, "scripts/check_blacklist.py", "--files", "src/repair/core.py"],
        second.with_name(second.name.replace(".log", "-allow.log")),
    )
    if not allowed.ok:
        return Result(False, f"路径黑名单误杀了源码改动：\n{allowed.detail}")

    third = second.with_name(second.name.replace(".log", "-integrity.log"))
    integrity = _run_to_file(
        [sys.executable, "scripts/check_test_integrity.py", "--self-test"], third
    )
    if not integrity.ok:
        return Result(False, f"测试完整性自检失败：\n{integrity.detail}")

    fourth = second.with_name(second.name.replace(".log", "-drill.log"))
    drill = _run_to_file(
        [sys.executable, "tools/daily_drill.py", "--day", "1", "--offline"], fourth
    )
    if not drill.ok:
        return Result(False, f"演练第一天未通过：\n{drill.detail}")

    with open(record, "a", encoding="utf-8") as handle:
        handle.write(
            "\n\n---- 8.4 门禁正反面 ----\n"
            f"碰测试文件：{'**被放行（不该）**' if blocked.ok else '被拦'}\n"
            f"只改源码：{'放行' if allowed.ok else '**被误杀**'}\n"
            f"\n---- 测试完整性自检 ----\n{integrity.detail}\n"
            f"\n---- 演练第一天 ----\n{drill.detail}\n"
        )
    return Result(True, "门禁正反面都正确；完整性自检与演练第一天通过")


def check_test_env(record: Path) -> Result:
    """
    步骤 1.6 的验收有两半，**缺一不可**：

      1. e2e 骨架能在 `sandbox-clean` 上跑通（路线：各模块未建，用桩验证链路骨架）；
      2. 三个陪练仓库存在，且最近一次 CI 是绿的。

    只验前半会让"仓库根本没建"这件事看起来像通过 —— 那正是本脚本存在的意义。
    """
    e2e = _run_to_file([sys.executable, "tests/e2e/e2e_full_run.py"], record)
    if not e2e.ok:
        return Result(False, f"e2e 骨架没跑通：\n{e2e.detail}")

    try:
        from src.github import GitHubClient, read_token

        client = GitHubClient(read_token(), timeout=30)
        owner = client.viewer()["login"]
    except Exception as exc:  # noqa: BLE001
        return Result(False, f"e2e 骨架通过，但读 GitHub 失败：{exc}")

    missing: list[str] = []
    unhealthy: list[str] = []
    for name in ("sandbox-clean", "sandbox-messy", "sandbox-hostile"):
        try:
            client.repo(f"{owner}/{name}")
        except Exception:  # noqa: BLE001
            missing.append(name)
            continue
        try:
            runs = client.get(f"/repos/{owner}/{name}/actions/runs", params={"per_page": 1})
            items = (runs or {}).get("workflow_runs") or []
            if not items:
                unhealthy.append(f"{name}: 还没有 CI 运行")
            elif items[0].get("conclusion") != "success":
                unhealthy.append(f"{name}: 最近一次 CI conclusion={items[0].get('conclusion')}")
        except Exception as exc:  # noqa: BLE001
            unhealthy.append(f"{name}: 查 CI 失败 {exc}")

    detail = f"e2e 骨架通过；缺失仓库={missing or '无'}；CI 不合规={unhealthy or '无'}"
    with open(record, "a", encoding="utf-8") as handle:
        handle.write("\n" + detail + "\n")
    return Result(not missing and not unhealthy, detail)


def check_write_token_isolated(record: Path) -> Result:
    candidates = [STATE / ".write_token", ROOT / "github-write-token.txt"]
    present = [path for path in candidates if path.exists()]
    if not present:
        return Result(False, f"写 token 不在任何候选位置: {[str(p) for p in candidates]}")
    # 物理隔离的硬证据：它必须被 .gitignore 覆盖，且确实没被 git 跟踪
    rel = present[0].relative_to(ROOT).as_posix()
    ignored = subprocess.run(
        ["git", "check-ignore", "-q", rel], cwd=str(ROOT), capture_output=True, check=False
    )
    tracked = subprocess.run(
        ["git", "ls-files", "--error-unmatch", rel], cwd=str(ROOT), capture_output=True, check=False
    )
    if tracked.returncode == 0:
        return Result(False, f"{rel} 被 git 跟踪了——物理隔离不成立")
    if ignored.returncode != 0:
        return Result(False, f"{rel} 未被 .gitignore 覆盖")
    return Result(True, f"{rel} 存在、未被 git 跟踪、且被 .gitignore 覆盖")


@dataclasses.dataclass(frozen=True)
class Step:
    id: str
    name: str
    checks: tuple[tuple[str, CheckFn], ...] = ()
    # 该步骤 BLOCKED 时，人类需要做的那**一件事**。会写进自动生成的 BLOCKED 报告。
    blocked_hint: str = ""


STEPS: tuple[Step, ...] = (
    Step("0.1", "harness 能力探测", (("capabilities", check_capabilities),)),
    Step(
        "0.2",
        "本地小模型安装 + 基线测试",
        (("baseline", check_model_baseline), ("embedding", check_embedding_evidence)),
    ),
    Step(
        "0.3",
        "凭证准备（读写 token 分离）",
        (
            ("credentials", script_check("tests/test_credentials.py")),
            ("isolation", check_write_token_isolated),
        ),
        blocked_hint=(
            "提供读 token，二选一：\n"
            "1. 在 `D:\\dsh\\home\\.read_token` 里写入（一行，前后不要引号/空行/BOM）；\n"
            "2. 把 `GH_READ_TOKEN` 注入 DSH 进程的环境变量。\n"
            "**不要把 token 贴进对话**——那会写进会话日志，等同于泄漏。\n"
            "放好后重跑：`state/run-acceptance.cmd --all`"
        ),
    ),
    Step("1.1", "仓库与 state 目录", (("skeleton", pytest_check("tests/unit/test_skeleton.py")),)),
    Step("1.2", "任务队列", (("queue", pytest_check("tests/integration/test_queue.py")),)),
    Step("1.3", "LLM 网关", (("gateway", pytest_check("tests/integration/test_gateway.py")),)),
    Step(
        "1.4",
        "GitHub 封装 + 人类闸门",
        (("gate", pytest_check("tests/integration/test_github_gate.py")),),
    ),
    Step(
        "1.5",
        "沙箱",
        (("sandbox", pytest_check("tests/integration/test_sandbox.py")),),
        blocked_hint="（无 Docker → 走 1.5 的降级方案 sandbox_strength: weak，网络项按路线跳过）",
    ),
    Step(
        "1.6",
        "测试环境三件套",
        (("env", check_test_env),),
        blocked_hint=(
            "三个陪练仓库**已建好**（均 HTTP 200），文件也传上去了（21 / 8 / 7 个）；"
            "4 条对抗 issue 也在。现在只缺**每个仓库的 CI 工作流**，而它被 token 权限挡住。\n"
            "\n"
            "需要人类做（一件事，两个勾）：\n"
            "GitHub → Settings → Developer settings → Fine-grained tokens → 编辑**写 token**，"
            "在 Repository permissions 里把\n"
            "  1. **Workflows** 设为 **Read and write** —— 否则上传 `.github/workflows/ci.yml` "
            "永远 403 `Resource not accessible by personal access token`。\n"
            "     这是 GitHub 有意的安全控制：工作流文件能执行代码，所以单独设了一道门，没有绕过办法。\n"
            "  2. **Actions** 设为 **Read-only** —— 否则验收读不到 CI 运行结果，"
            "「三个仓库 CI 绿」这一半无法自动核对。\n"
            "\n"
            "**token 的值不用换**，只是多勾两项权限。\n"
            "改完之后执行：`python tools/sandbox_repos.py apply --reply 是`，"
            "再跑 `state/run-acceptance.cmd --all`。"
        ),
    ),
    Step(
        "2.1",
        "追问循环",
        (("refiner", pytest_check("tests/integration/test_spec_refiner.py")),),
        blocked_hint=(
            "2.1 的验收要用模型真跑 3 个 idea。代码已可注入生成器；"
            "生产档位是 flash（`gateway.chat(..., tier=\"flash_api\")`），"
            "而 `DEEPSEEK_API_KEY` 不在环境变量里，flash 的模型 id 也仍是占位值。\n"
            "当前验收用**本地小模型**真跑（验的是循环机制：schema、单问约束、轮次上限、落盘）。\n"
            "要切到 flash：把 `DEEPSEEK_API_KEY` 注入 DSH 进程环境变量即可，代码不用改。"
        ),
    ),
    Step(
        "2.2",
        "停止判断",
        (("stopper", check_stop_judger),),
        blocked_hint=(
            "2.2 的验收含 20 组样本准确率 ≥80%。\n"
            "**先看清一件事**：那 20 组标签是机器按 R1/R2/R3 三条规则造的，"
            "不是人类标的（`tools/eval_stopper.py` 的文件头写明）。\n"
            "所以评测只能证明「判断器实现了这三条规则」，**不能**证明这三条规则对不对。\n"
            "需要人类做：打开 `state/reports/stopper-labels-for-human-review.json`，"
            "复核 20 个场景的标签（该停 / 不该停），有异议的改掉并告诉我。"
        ),
    ),
    Step(
        "3.1",
        "分类打标",
        (("triage", check_triage),),
        blocked_hint=(
            "3.1 的回放要**准确率 ≥75%** 且 **spam/dupe 误杀率 ≤5%**。\n"
            "实测卡在准确率一线（约 74.5%，51 条里差 1 条），误杀率 0%。\n"
            "混淆矩阵显示短板集中在 `question` 类：真实为 question 的 issue 有约九成\n"
            "被判成 bug/feature。这有两种解释，只有人类能分辨：\n"
            "  a) 分类器对『提问』的判据还是不够；\n"
            "  b) 仓库把很多其实是缺陷/需求的 issue 也打了 question 标签（ground truth 噪声）。\n"
            "需要人类做：看 `state/reports/triage-false-kills-for-human-review.json` 里\n"
            "那些不一致的条目，判断是分类器错还是标签本身有偏差。"
        ),
    ),
    Step("3.2", "查重路由", (("dedupe", check_dedupe),),
         blocked_hint=(
             "查重验收红了。三个常见原因，按可能性排：\n"
             "1. 本地 embedding 模型（bge-m3）没在跑 —— 试 `ollama list` 看有没有 bge-m3:latest；\n"
             "2. 阈值漂了：embedding 模型换了版本会让余弦整体位移，"
             "看 `state/reports/dedupe-thresholds-local_embed.md` 里的曲线重新标一次；\n"
             "3. 真实语料的最近邻分布变了：报告里有「一进来就被贴重复标记的比例」，"
             "超过 10% 说明阈值太低、会开始骚扰正常用户。"
         )),
    Step("3.3", "标签打回通路", (("bounce", check_bounce),),
         blocked_hint=(
             "打回通路红了。按顺序看：\n"
             "1. 回放用的输入是 3.1 的 holdout 报告（`triage-eval-*-local_small-holdout1.json`）："
             "它不在就先跑 `--step 3.1`（3.3 复用 3.1 的真实输出，不重复烧 7 分钟本地推理）；\n"
             "2. 误打回率超线（把标着 bug 的 issue 打回）：说明 `PRECHECK_CONFIDENCE`（0.8）"
             "对这批仓库偏松，**不要**直接调大常数了事 —— 先看报告里的 `bounced[]` 逐条判断"
             "是判据错还是仓库标签错，把裁决写进 `state/corpus/triage-adjudicated.jsonl`；\n"
             "3. 打回率 <1%：通路没被使用本身就是失败信号，检查 `precheck` 有没有被真正调用。"
         )),
    Step("4.1", "文件定位", (("localize", check_localize),),
         blocked_hint=(
             "文件定位红了。按顺序看：\n"
             "1. 回放要真 embedding（bge-m3）：`ollama list` 里有没有它；\n"
             "2. 命中率掉了：看 `state/reports/localize-eval-*.json` 里每条样本的 `candidates`，"
             "判断是**哪一类线索**掉的（直接写路径 / 符号名 / 只描述现象 / 测试名映射）——"
             "补信号要按类补，不要笼统调权重；\n"
             "3. 如果掉的是『只描述现象』那一类，说明 embedding 那一路没起作用（模型没起来时"
             "余弦会全 0，正好会表现成这一类集体掉分）。"
         )),
    Step("4.2", "修复循环", (("repair", check_repair),),
         blocked_hint=(
             "修复循环红了。按顺序看：\n"
             "1. **先看补丁到底有没有打上**：`state/reports/repair_eval-*.md` 里若出现"
             "「同一个断言连错好几轮」，八成又是补丁没落上（见 "
             "`state/findings/silent-patch-failure.md`）—— 沙箱现在会比对目录指纹，"
             "日志里 `apply_patch:` 那行说明一切；\n"
             "2. flash 档要能用：`python tools/probe_flash.py`；\n"
             "3. 样本作废（前置条件不成立）说明注入点没打中：检查 `tools/eval_repair.py` 的 `SAMPLES`"
             "—— 注入片段必须在目标文件里唯一。"
         )),
    Step("4.3", "测试门禁", (("gate", check_gate),),
         blocked_hint=(
             "门禁红了。按顺序看：\n"
             "1. `state/reports/gate-eval-<date>.json` 里三个场景逐条有结论："
             "A 该被拦（全量回归新增失败）、B 该被拦（改测试文件）、C 该放行；\n"
             "2. 场景 C 红了多是**基线**问题：`capture_baseline` 必须在未打补丁的副本上实测，"
             "写死 `lint_count: 0` 会让 fixture 仓库自带的告警算成新增（踩过一次）；\n"
             "3. 黑名单命中时不该执行任何命令 —— 若 `gates` 里还出现 target/regression，"
             "说明顺序被改坏了（那等于用被改过的测试去判测试）。"
         )),
    Step("4.4", "报告 + 推送", (("publish", check_publish),),
         blocked_hint=(
             "发布链路红了。按顺序看：\n"
             "1. 报告五段（根因/改动/测试结果/风险/回滚）缺一段就会在正文里写「这是缺陷」——"
             "那是刻意的：一份看不出风险的报告比没有报告更危险；\n"
             "2. 演练停在闸门却报红，多半是 `tools/e2e_publish.py` 里的门禁或补丁应用失败："
             "看 `state/reports/<issue>.md` 与 `state/e2e-publish/apply.log`；\n"
             "3. 真实 PR 需要人类两次「是」（推分支一次、建 PR 一次）；本检查只验证"
             "「链路通且没点头就走不动」，不代替那两次点头。"
         )),
    Step("5.1", "开源项目猎手", (("scout", check_scout),),
         blocked_hint=(
             "猎手红了。按顺序看：\n"
             "1. 某个需求凑不够 3 个有效候选：`state/reports/scout-review-<日期>.md` 里逐条列着"
             "搜索词、搜到几条、为什么跳过 —— 关键词太长是常见原因（GitHub 仓库搜索是 AND 语义，"
             "实测五个词的长句只回来 5 条）；\n"
             "2. 低星但适用的项目：走路线留的破格通道（`justify_low_star`，要求模型给出书面理由），"
             "路线自己点名的 PRism(2★)/issuesort(3★) 就是靠它进来的；\n"
             "3. GPL 那条线不成立：检查 `license_risk` 的查表是否被改过 —— 它**不允许**被模型覆盖。"
         )),
    Step("6.1", "半自动建新仓库", (("scaffold", check_scaffold),),
         blocked_hint=(
             "建新仓库这一步红了。按顺序看：\n"
             "1. 生成物本地跑不起来：`state/scaffold/<name>/` 就在那儿，直接进去跑 "
             "`pytest -q` 与 `ruff check .` 看真实报错 —— 本地绿是上传的前提（路线 6.1 第 3 条）；\n"
             "2. 常见三类：包 import 不到（pyproject 缺 pythonpath）、ruff 规则漂移（缺 [tool.ruff]）、"
             "用例用了 **tmp_path/tempfile**（受限沙箱拒绝系统 temp，必 ERROR）；"
             "这三条已经写进 `SYSTEM_PROMPT` 与校验器，模型漏了就带着缺件清单重试；\n"
             "3. 建仓失败：检查写 token 是否有 repo 权限（`create_repo` 是闸门白名单里的动作）；"
             "**真实建仓需要人类两次决策**（选名 + 批准），本检查不代替那两次点头。"
         )),
    Step("7.1", "skills/ + 8 条对话指令", (("skills", check_skills),),
         blocked_hint=(
             "指令外壳红了。按顺序看：\n"
             "1. 先跑 `python tools/build_skills.py --check`：它会逐条报出是哪份 SKILL.md "
             "与注册表不一致（漏段 / TEST_GATE 被改 / 确认点为空 / AGENTS.md 没指向它）；\n"
             "2. 修的方式是**改 `src/skills/registry.py` 再重建**，不要手改 skills/*.md 与 AGENTS.md —— "
             "手改会在下一次校验里被打回（路线 7.4 第 1 条就是防这个的）；\n"
             "3. 应急那一步红了：`python -m src.cli emergency --state-dir <副本>` 看它到底报了什么；"
             "自检项的含义见 `src/skills/doctor.py` 的注释（特别是 offline 与故障的区分）。"
         )),
    Step("8.1", "CI 门禁 + 脏测试工具", (("stage8", check_stage8),),
         blocked_hint=(
             "第八部分的工具红了。按顺序看：\n"
             "1. 黑名单那条要求**正反两面都对**：碰测试文件必须 exit 1、只改源码必须 exit 0；"
             "只验一面的话，一个「永远拒绝一切」的脚本也会通过；\n"
             "2. 测试完整性数出来的用例数必须**只算本系统的测试** —— "
             "state/ 与 fixtures/ 里全是仓库副本，算进来会得到 666 个文件、5319 个用例这种荒谬数字"
             "（实测踩过）；\n"
             "3. 演练第一天红了：多半是对抗样本 fixture 不在（先跑 `python tools/sandbox_repos.py build`）—— "
             "注意脚本会把「缺件」和「没识别出注入」分开报，别去改 guard。"
         )),
    Step("8.2", "7 天实战演练", (("drill", check_drill),),
         blocked_hint=(
             "演练这道关红了。按顺序看：\n"
             "1. 先跑 `python tools/daily_drill.py --summary` —— 它会把**每一条不合格**列出来，"
             "包括「同一天有两条记录」（那是一天跑七次，不算连续 7 天）、「日期断档」、"
             "「某天是 offline 模式」（只验了骨架，不算数）、「某天出现对外写」（一票否决）、"
             "「某天人类介入 >2 次」；\n"
             "2. 对抗样本 fixture 缺件的报错与「没识别出注入」是**两种不同的红**，"
             "前者先跑 `python tools/sandbox_repos.py build`，别去改 guard；\n"
             "3. 7 天齐了也只是「记录合格」：路线 8.2 ⑥ 的最后一步是**人类评审补丁质量**，"
             "脚本不代替这个判断 —— 所以本检查通过后仍需人类给出上线结论。"
         )),
)


# ------------------------------------------------------------------- 表格读写

def read_statuses() -> dict[str, str]:
    if not PROGRESS.exists():
        return {}
    out: dict[str, str] = {}
    for line in PROGRESS.read_text(encoding="utf-8").splitlines():
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) == 5 and cells[0] and cells[2] in LEGAL_STATUSES:
            out[cells[0]] = cells[2]
    return out


def read_evidence() -> dict[str, str]:
    if not PROGRESS.exists():
        return {}
    out: dict[str, str] = {}
    for line in PROGRESS.read_text(encoding="utf-8").splitlines():
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) == 5 and cells[0] and cells[2] in LEGAL_STATUSES:
            out[cells[0]] = cells[4]
    return out


def _local_now() -> dt.datetime:
    """
    本地时间（带时区）。

    人读的时间戳必须是**本地时间**（表里显示 15:57 就是这里的 15:57），
    但裸的 `datetime.now()` 没有时区，跨时区/夏令时会悄悄错位；
    所以先取 UTC 再转本地 —— 显示不变，语义明确。
    """
    return dt.datetime.now(dt.timezone.utc).astimezone()


def read_when() -> dict[str, str]:
    """读回「时间」列。保留某一步的旧状态时也要保留它的时间戳，不能清空。"""
    if not PROGRESS.exists():
        return {}
    out: dict[str, str] = {}
    for line in PROGRESS.read_text(encoding="utf-8").splitlines():
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) == 5 and cells[0] and cells[2] in LEGAL_STATUSES:
            out[cells[0]] = cells[3]
    return out


def rewrite_progress(rows: list[tuple[str, str, str, str, str]]) -> None:
    lines = PROGRESS.read_text(encoding="utf-8").splitlines()
    start = next((i for i, ln in enumerate(lines) if ln.startswith("| 步骤")), None)
    if start is None:
        raise SystemExit("progress.md 里找不到表头（| 步骤 | ...），拒绝改写")
    end = start
    while end < len(lines) and lines[end].lstrip().startswith("|"):
        end += 1
    table = ["| 步骤 | 名称 | 状态 | 时间 | 证据 |", "|---|---|---|---|---|"]
    # 空白单元格在人读的表格里就是"这里没东西"；统一成 — 更清楚
    table += [
        "| " + " | ".join(cell if cell else "—" for cell in cells) + " |" for cells in rows
    ]
    PROGRESS.write_text("\n".join(lines[:start] + table + lines[end:]) + "\n", encoding="utf-8")


# ----------------------------------------------------------------------- 主流程

def evaluate(step: Step, run_dir: Path) -> tuple[bool, str, str]:
    """返回 (是否全部通过, 摘要, 证据相对路径)。"""
    details: list[str] = []
    evidence: list[str] = []
    for label, check in step.checks:
        result = Result(False, "未执行")
        for attempt in range(RETRIES + 1):
            record = run_dir / f"{step.id}-{label}-a{attempt}.log"
            result = check(record)
            record.parent.mkdir(parents=True, exist_ok=True)
            with open(record, "a", encoding="utf-8") as handle:
                handle.write(
                    f"\n\n==== 尝试 {attempt + 1}/{RETRIES + 1}  "
                    f"结论={'PASS' if result.ok else 'FAIL'} ====\n{result.detail}\n"
                )
            evidence.append(record.relative_to(ROOT).as_posix())
            if result.ok:
                break
        details.append(f"[{'PASS' if result.ok else 'FAIL'}] {label}: {result.detail}")
        if not result.ok:
            return False, "\n\n".join(details), evidence[-1]
    return True, "\n\n".join(details), evidence[-1] if evidence else ""


def write_blocked_report(step: Step, summary: str, run_dir: Path) -> Path:
    """
    把 BLOCKED 落盘。

    路线 0.2 规定：验收不通过时**唯一允许的动作**就是输出 BLOCKED 报告，然后停下等人类。
    报告里必须带失败检查的**原始日志关键段**——没有原始输出的 BLOCKED 等于口头声明。
    所以这里从本轮证据目录里把该步骤的日志尾部整段抄进来。
    """
    stamp = run_dir.name
    report = STATE / "reports" / f"blocked-{step.id}-{stamp}.md"
    logs = sorted(run_dir.glob(f"{step.id}-*.log"))
    excerpts = []
    for log in logs:
        text = log.read_text(encoding="utf-8", errors="replace").strip()
        tail = "\n".join(text.splitlines()[-40:])
        excerpts.append(f"### `{log.relative_to(ROOT).as_posix()}`\n\n```\n{tail}\n```\n")
    if not excerpts:
        excerpts.append("（本轮没有留下日志文件——这本身是缺陷，应检查检查件的实现）\n")
    hint = step.blocked_hint or "（本步骤未登记人工提示，请人工判断）"
    report.write_text(
        "\n".join(
            [
                f"# BLOCKED：步骤 {step.id} {step.name}",
                "",
                f"- 时间：{_local_now().strftime('%Y-%m-%d %H:%M:%S')}",
                f"- 判定者：`tools/acceptance.py`（重试 {RETRIES} 次后仍失败）",
                "- 状态：`BLOCKED`（唯一合法动作是停下等人类，见路线 0.2 TEST_GATE）",
                "",
                "## 检查结论",
                "",
                "```",
                summary,
                "```",
                "",
                "## 需要人类做什么",
                "",
                hint,
                "",
                "## 原始日志关键段",
                "",
                "\n".join(excerpts),
            ]
        ),
        encoding="utf-8",
    )
    return report


def main() -> int:
    # Windows 控制台默认 GBK。被测输出里只要有一个编不出来的字符，
    # print 就会抛 UnicodeEncodeError 把整次验收打断——绝不接受这种事。
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

    parser = argparse.ArgumentParser(description="按可执行检查写入 progress.md 的步骤状态")
    parser.add_argument("--all", action="store_true", help="按序评估所有已登记检查的步骤")
    parser.add_argument("--step", help="只评估一个步骤，例如 1.2")
    parser.add_argument(
        "--from",
        dest="from_step",
        help="从这一步开始评估（配合 --all 用）。为什么要它：一次验收已经 15 分钟以上，"
        "而单次调用有 10 分钟上限 —— 分段跑（0.1→2.2 / 3.1 / 3.2→4.3）才能跑完整轮，"
        "而且顺序不变，跨步骤的状态影响照样能被发现",
    )
    parser.add_argument("--until", dest="until_step", help="评估到这一步为止")
    parser.add_argument("--list", action="store_true", help="列出步骤与是否已登记检查")
    args = parser.parse_args()

    step_ids = [step.id for step in STEPS]
    for flag, value in (("--from", args.from_step), ("--until", args.until_step)):
        if value and value not in step_ids:
            parser.error(f"{flag} 的步骤不存在：{value}；可用：{', '.join(step_ids)}")
    start_index = step_ids.index(args.from_step) if args.from_step else 0
    end_index = step_ids.index(args.until_step) if args.until_step else len(step_ids) - 1
    if end_index < start_index:
        parser.error("--until 不能早于 --from")

    if args.list:
        for step in STEPS:
            marks = "、".join(label for label, _ in step.checks) or "（未登记检查）"
            print(f"{step.id:<5} {step.name:<24} {marks}")
        return 0

    if not args.all and not args.step:
        parser.error("需要 --all 或 --step <id>")

    stamp = _local_now().strftime("%Y%m%d-%H%M%S")
    run_dir = EVIDENCE_DIR / stamp
    run_dir.mkdir(parents=True, exist_ok=True)

    _, token_source = load_read_token()
    print(f"读 token 来源: {token_source}")

    previous = read_statuses()
    previous_evidence = read_evidence()
    previous_when = read_when()
    now = _local_now().strftime("%Y-%m-%d %H:%M")
    rows: list[tuple[str, str, str, str, str]] = []
    blocked_at: str | None = None

    for position, step in enumerate(STEPS):
        status = previous.get(step.id, "TODO")
        when = ""
        proof = previous_evidence.get(step.id, "")

        # 未被本轮评估的步骤：状态、时间、证据一律原样带回
        kept_when = previous_when.get(step.id, "")

        if args.step and step.id != args.step:
            rows.append((step.id, step.name, status, kept_when, proof))
            continue

        if position < start_index or position > end_index:
            # 分段跑：不在这一段的步骤原样带回（状态不变）
            rows.append((step.id, step.name, status, kept_when, proof))
            continue

        if blocked_at is not None and not args.step:
            # 已 BLOCKED，后面一律不动（路线的「停下等人类」）
            rows.append((step.id, step.name, status, kept_when, proof))
            continue

        if not step.checks:
            status = "PASS" if previous.get(step.id) == "PASS" else "TODO"
            rows.append((step.id, step.name, status, kept_when, proof))
            continue

        ok, summary, evidence = evaluate(step, run_dir)
        status = "PASS" if ok else "BLOCKED"
        when = now
        proof = evidence
        print(f"[{status}] {step.id} {step.name}")
        print("    " + summary.replace("\n", "\n    "))
        if not ok:
            # 路线 0.2：唯一允许的动作是把 BLOCKED 报告写出来
            report = write_blocked_report(step, summary, run_dir)
            print(f"    BLOCKED 报告已落盘: {report.relative_to(ROOT).as_posix()}")
            if not args.step:
                blocked_at = step.id
        rows.append((step.id, step.name, status, when, proof))

    rewrite_progress(rows)

    print()
    print(f"表格已写入 {PROGRESS.relative_to(ROOT)}（只由本脚本写入）")
    print(f"本轮证据目录 {run_dir.relative_to(ROOT)}")
    if blocked_at:
        print(f"!! 步骤 {blocked_at} 为 BLOCKED：按路线 0.2 停下等人类，不再继续后续步骤")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
