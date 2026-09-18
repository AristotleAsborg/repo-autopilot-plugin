"""8.2 ①④⑤ 的**每周一次**：真实语料回放 / 并发压测 / 中断恢复。

    python tools/dirty_weekly.py                 # 三项全跑（真调本地小模型）
    python tools/dirty_weekly.py --offline       # 只验骨架（不调模型，会用确定性桩）
    python tools/dirty_weekly.py --limit 30      # 调试用：只回放 30 条
    python tools/dirty_weekly.py --out-dir D:\\tmp\\dirty   # 产物写到别处（测试用）

产物（路线 8.3 的「脏测试（回放/压测/中断），每周」这一行的证据）：

    state/reports/weekly/dirty-<YYYY-MM-DD>.json    结构化结果
    state/reports/weekly/dirty-<YYYY-MM-DD>.md      给人看：三项各自的结论、关键数字、失败原因

**退出码是三档，不是两档**：

    0 = 全过
    1 = 有失败（跑起来了，但某一项没达到判据）
    2 = 缺件（语料/基线/工具不在）—— 这一档必须单独存在

为什么缺件要单独一档：缺件被报成"指标不达标"会把读日志的人带偏 ——
他会去调提示词、调阈值，而真正的问题是**考卷根本没发下来**。
（`tools/daily_drill.py` 的 `load_adversarial` 踩过同一个坑，注释里写着。）

## 三项各自的判据从哪来

① **真实语料回放**：用 `state/corpus/` 的**冻结回放集**（`triage-replay.jsonl`）的
   holdout 一半回放给 `src.triage`，比准确率与**误杀率**（把正常 issue 判成"建议关闭"
   的比例 —— 它是 `gate_close` 动作，即 label 落在 spam/duplicate）。
   与既有基线（`state/reports/triage-eval-*.json`）比，**只允许不下降**。
   为什么必须用 holdout 那一半：基线本身就是在那 164 条上算出来的，
   拿全集去对一条由子集算出来的分数线，比较的就不是同一个东西（苹果比橘子）。

④ **并发压测**：20 个 issue 同时涌入 → 断言批量引擎（`src.batch`）四条：
   单批 ≤20、无重复消费、单条失败不阻断批次、末尾审批单**逐条**列出。
   队列侧另用 20 线程认领 20 件任务，断言"无丢失无重复"（这是 1.2 的硬线，脏测试顺手复验）。

⑤ **中断恢复**：在批次跑到一半时**注入中断**（不是真杀本机进程 ——
   路线 8.2 ⑤ 的"断电式 kill"在本机只能靠可注入的失败点来复现，这一点如实写在报告里），
   然后用同一批重跑，断言：从中断点续跑、**不重复处理**、最终结果与无中断一致。

## 离线模式为什么用关键词桩而不是"跳过回放"

`--offline` 的用途是"在不能调模型的机器上也能验这条链路是否完整"。如果它直接跳过回放，
那就什么都验不到。所以离线用一个**确定性关键词桩**分类器：它的准确率远低于小模型，
一定过不了"不下降"（于是退出码 1）—— 这正是要点：**离线跑出来的分数不许被当成验收通过**，
而链路是否完整（能不能读语料、能不能落产物、缺件会不会报缺件）仍然是真的被测了。
"""

from __future__ import annotations

import argparse
import dataclasses
import glob
import json
import os
import sys
import threading
import traceback
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

#: 产物落点。可以用 `DIRTY_WEEKLY_ROOT` 环境变量整体挪走 ——
#: 验收测试要的就是这个：跑通三个脚本却**不在真实 state/ 里堆测试垃圾**。
OUT_ROOT = Path(os.environ.get("DIRTY_WEEKLY_ROOT") or (ROOT / "state" / "reports" / "weekly"))

CORPUS_DIR = ROOT / "state" / "corpus"
REPLAY_PATH = CORPUS_DIR / "triage-replay.jsonl"
CORPUS_PATH = CORPUS_DIR / "issues.jsonl"

#: 误杀率 = 被判成 spam/duplicate 的真实 issue 比例（与 tools/eval_triage.py 同一口径）。
FALSE_KILL_LABELS = ("spam", "duplicate")

#: 比基线的允许容差。
#:
#: **2026-09-12 实测改了这里**：原来是 1e-6（"只吸收浮点的最后一跳"），结果第一次真跑就
#: 当场翻车 —— 我的 `--step 3.1` 验收同一天重跑了一遍，把基线文件
#: `triage-eval-*-holdout1.json` 从 0.7561 刷成 0.7622；紧接着演练跑出 0.7561，
#: 比分基线**低 0.0061**，于是被判"准确率下降"。可这两个数都来自同一份冻结语料上的
#: **同一个小模型**（温度 0 也不是逐位可复现）：164 条样本里 1 条 = 0.61 个百分点，
#: 0.61pp 的差就是**一次采样噪声**。
#: 所以：
#: - 容差按"采样噪声"定：准确率 1.5pp（≈2.5 条）、误杀率 1pp（≈1.6 条）——
#:   比它小的差**单次跑根本分不出来**，硬判只会让这条周演练随机红；
#: - 真正的趋势判断交给已有的机制：`tools/checkup.py` 的"**连续两周下降**才报警"
#:   （8.3 表里"离线评测"那一行的原话），而不是靠这里的一次比较；
#: - 容差**不吃掉真回归**：超过 1.5pp 仍然判失败，且报告里照旧写明与基线的差。
ACCURACY_TOLERANCE = 0.015
FALSE_KILL_TOLERANCE = 0.01

#: 冻结基线（金样本）。有它就用它，没有才退回"最近一份 triage-eval-*.json"。
#: 为什么要冻：`triage-eval-*.json` 是**活文件** —— 每次 3.1 验收都会重写它，
#: 于是"基线"会随别的动作悄悄变（上面那次翻车的直接原因）。冻结之后，
#: 周演练比的才是同一把尺子。用 `--freeze-baseline` 显式冻结，绝不自动改写。
FROZEN_BASELINE = ROOT / "state" / "corpus" / "dirty-baseline.json"

#: 批量引擎的硬上限（路线 7.1 第 1 条），压测要按它构造 25 个 issue 才能同时验到"超出部分留到下一轮"。
BATCH_CAP = 20
CONCURRENCY = 20

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_MISSING = 2


class MissingArtifact(RuntimeError):
    """缺件。**单独一档**：它既不是通过，也不是"指标不达标"。"""


class Interrupted(RuntimeError):
    """模拟"批次跑到一半断电"。只由压测自己抛，绝不在生产路径上用。"""


# ------------------------------------------------------------------ 结果结构

@dataclasses.dataclass
class Case:
    """三项里的一项。"""

    name: str
    passed: bool
    detail: dict[str, Any]
    failures: list[str] = dataclasses.field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "passed": self.passed,
            "failures": list(self.failures),
            "detail": self.detail,
        }


@dataclasses.dataclass
class WeeklyReport:
    date: str
    mode: str
    corpus: str
    baseline: dict[str, Any]
    cases: list[Case]

    @property
    def passed(self) -> bool:
        return all(case.passed for case in self.cases)

    @property
    def failed(self) -> list[Case]:
        return [case for case in self.cases if not case.passed]

    def as_dict(self) -> dict[str, Any]:
        return {
            "date": self.date,
            "mode": self.mode,
            "corpus": self.corpus,
            "baseline": self.baseline,
            "passed": self.passed,
            "cases": [case.as_dict() for case in self.cases],
        }


# ------------------------------------------------------------------ 语料

def load_corpus(path: Path | None = None, *, limit: int = 0, replay: bool = True) -> tuple[list[dict], str]:
    """
    读语料。优先冻结回放集（`triage-replay.jsonl`），退回 0.2 抓的原始语料。

    **必须能分辨"文件不在"和"文件是空的"**：两者都返回不了语料，但原因完全不同，
    而且都要以缺件的名义吼出来，不能悄悄降级成"0 条样本、准确率 0%"。
    """
    if replay:
        candidates = [path] if path is not None else [REPLAY_PATH, CORPUS_PATH]
        fallback = CORPUS_PATH
    else:
        candidates = [path] if path is not None else [CORPUS_PATH]
        fallback = None

    for candidate in candidates:
        if candidate.is_file():
            selected = candidate
            break
    else:
        raise MissingArtifact(
            f"语料不存在：找过 {[str(item) for item in candidates]}。"
            "先跑 `python tools/freeze_triage_set.py`（冻结回放集）或补上 state/corpus/issues.jsonl。"
        )

    rows: list[dict] = []
    with open(selected, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    if not rows:
        raise MissingArtifact(f"语料是空的：{selected}（先跑 `python tools/freeze_triage_set.py`）")
    origin = "frozen" if selected != fallback else "corpus"
    return rows, f"{origin}({selected.name})"


def baseline_report(pattern: str = "triage-eval-*.json", *, explicit: Path | None = None) -> tuple[Path, dict]:
    """
    找一份基线。**优先用冻结的那一份**（`state/corpus/dirty-baseline.json`）。

    找不到**必须抛缺件**：没有分数线就没法判"不下降"，此时通过等于什么都没验。

    为什么优先冻结件：`triage-eval-*.json` 是**活文件**（每次 3.1 验收都会重写），
    拿"最近修改的那一份"当尺子，等于让基线随别的动作漂移 —— 实测就是这么翻车的
    （见 `ACCURACY_TOLERANCE` 的注释）。冻结件带 `frozen=true` 与来源说明，
    比出来的是同一把尺子。
    """
    if explicit is not None:
        if not explicit.is_file():
            raise MissingArtifact(f"指定的基线不存在：{explicit}")
        return explicit, json.loads(explicit.read_text(encoding="utf-8"))

    if FROZEN_BASELINE.is_file():
        return FROZEN_BASELINE, json.loads(FROZEN_BASELINE.read_text(encoding="utf-8"))

    found = sorted(glob.glob(str(ROOT / "state" / "reports" / pattern)), key=lambda p: os.path.getmtime(p))
    if not found:
        raise MissingArtifact(
            f"找不到基线：既没有冻结基线 {FROZEN_BASELINE.relative_to(ROOT).as_posix()}，"
            f"也没有 state/reports/{pattern}。"
            "先跑 `python tools/eval_triage.py --from-replay --split holdout` 建立基线，"
            "再用 `python tools/dirty_weekly.py --freeze-baseline` 把它冻住。"
        )
    chosen = Path(found[-1])
    return chosen, json.loads(chosen.read_text(encoding="utf-8"))


def _baseline_metrics(path: Path, report: dict) -> dict[str, Any]:
    for key in ("accuracy", "false_kill_rate"):
        if key not in report:
            raise MissingArtifact(f"基线 {path} 里没有 {key} 字段，无法比较（这份报告格式不对）")
    return {
        "path": path.relative_to(ROOT).as_posix() if path.is_relative_to(ROOT) else str(path),
        "date": report.get("date"),
        "accuracy": float(report["accuracy"]),
        "false_kill_rate": float(report["false_kill_rate"]),
        "floor_accuracy": report.get("accuracy_floor"),
        "ceiling_false_kill": report.get("false_kill_ceiling"),
        "samples": report.get("graded") or report.get("samples"),
        # 冻结件带 `frozen: true`：报告里要能一眼看出"这次比的是金样本还是活文件"
        "frozen": bool(report.get("frozen")),
    }


# ------------------------------------------------------------------ ① 真实语料回放

def deterministic_stub(issue: Any) -> tuple[str, float]:
    """
    离线用的**确定性关键词桩**。它是有意做得很弱的。

    为什么要弱：离线模式的产物有可能被人当成一次"回放通过"。让它必然低于基线，
    就没人能把它误读成验收结论 —— 想看真数字就必须让本地小模型真的跑一遍。
    """
    text = f"{issue.title}\n{issue.body}".lower()
    bug_words = ("crash", "error", "fail", "broken", "traceback", "报错", "崩溃", "失败", "异常")
    feature_words = ("please add", "support", "feature request", "希望", "建议增加", "能否支持")
    if "?" in text or "？" in text or "how to" in text or "怎么" in text:
        return "question", 0.6
    if any(word in text for word in bug_words):
        return "bug", 0.6
    if any(word in text for word in feature_words):
        return "feature", 0.6
    return "question", 0.2


def replay_corpus(
    *,
    rows: list[dict],
    offline: bool = False,
    adjudicated: dict[str, str] | None = None,
    label: str = "真实语料回放",
) -> Case:
    """
    把语料逐条喂给分类器，算准确率与误杀率。

    "误杀"在这里的定义与 3.1 完全一致：把一条**真实存在的** issue 判成 spam/duplicate
    （也就是 `gate_close`）。它与准确率分开算，因为判错的代价不对称 ——
    准确率掉一格只是标签不合适，误杀的后果是把写 issue 的人赶走。
    """
    from src.triage import FeedbackClassifier, RawIssue

    verdicts = adjudicated or {}
    classifier = FeedbackClassifier() if not offline else None
    rows_out: list[dict] = []
    correct = kills = errors = 0
    errors_detail: list[dict] = []

    for row in rows:
        item = RawIssue(
            repo=str(row.get("repo") or ""),
            number=int(row.get("number") or 0),
            title=str(row.get("title") or ""),
            body=str(row.get("body") or ""),
            labels=[str(row["label"])] if row.get("label") else [],
            state=str(row.get("state") or "closed"),
        )
        # 人工裁决过的真值优先（路线 3.3 第 4 条），仓库标签只是兜底 ——
        # 与 tools/eval_triage.py 同一口径，否则两边的分数不可比。
        recorded = verdicts.get(f"{item.repo}#{item.number}")
        truth = recorded or (item.labels[0] if item.labels else "?")
        try:
            if classifier is None:
                predicted, confidence = deterministic_stub(item)
                action = "change_label" if predicted not in FALSE_KILL_LABELS else "gate_close"
                reason = "离线关键词桩"
            else:
                result = classifier.classify(item)
                predicted = result.verdict.label
                confidence = result.verdict.confidence
                action = result.action
                reason = result.verdict.reason
        except Exception as exc:  # noqa: BLE001 — 单条失败不该毁掉整次回放
            errors += 1
            errors_detail.append({"repo": item.repo, "number": item.number, "error": f"{type(exc).__name__}: {str(exc)[:200]}"})
            continue

        hit = predicted == truth
        killed = predicted in FALSE_KILL_LABELS
        correct += int(hit)
        kills += int(killed)
        rows_out.append(
            {
                "repo": item.repo,
                "number": item.number,
                "truth": truth,
                "predicted": predicted,
                "confidence": round(float(confidence), 3),
                "action": action,
                "correct": hit,
                "false_kill": killed,
                "reason": reason,
            }
        )

    graded = len(rows_out)
    accuracy = correct / graded if graded else 0.0
    kill_rate = kills / graded if graded else 0.0
    failures: list[str] = []
    if errors:
        # 分类失败计入失败而不是悄悄从分母里消失：连续给不出合规输出的模型
        # 不能用"剩下的都判对了"来证明自己没问题。
        failures.append(f"{errors} 条分类失败（连续拿不到合规结构）")
    if graded == 0:
        failures.append("没有一条有效样本")

    return Case(
        name=label,
        passed=not failures,
        failures=failures,
        detail={
            "mode": "offline_stub" if offline else "local_small",
            "graded": graded,
            "errors": errors,
            "error_samples": errors_detail[:5],
            "accuracy": round(accuracy, 4),
            "false_kill_rate": round(kill_rate, 4),
            "by_truth": _recall(rows_out),
            "misjudged": [row for row in rows_out if not row["correct"]][:20],
        },
    )


def _recall(rows: list[dict]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for truth in sorted({row["truth"] for row in rows}):
        subset = [row for row in rows if row["truth"] == truth]
        out[truth] = {"hit": sum(1 for row in subset if row["correct"]), "total": len(subset)}
    return out


def compare_with_baseline(case: Case, baseline: dict[str, Any]) -> Case:
    """
    与基线比：准确率不得**明显**下降、误杀率不得**明显**上升。

    两条线都判，也各自写清楚失败原因（误杀率方向相反，不做"取最小值"那种聪明事）。

    容差是**按采样噪声定的**（见 `ACCURACY_TOLERANCE` 的注释），不是放松：
    超过容差照旧判失败，报告里也照旧写明与基线的差。趋势判断归 `tools/checkup.py`
    的"连续两周下降才报警"。
    """
    failures = list(case.failures)
    detail = dict(case.detail)
    accuracy = float(detail.get("accuracy") or 0.0)
    kill_rate = float(detail.get("false_kill_rate") or 0.0)
    base_accuracy = float(baseline["accuracy"])
    base_kill = float(baseline["false_kill_rate"])

    detail["baseline"] = {
        "accuracy": base_accuracy,
        "false_kill_rate": base_kill,
        "delta_accuracy": round(accuracy - base_accuracy, 4),
        "delta_false_kill": round(kill_rate - base_kill, 4),
        "frozen": bool(baseline.get("frozen")),
        "tolerance": {"accuracy": ACCURACY_TOLERANCE, "false_kill_rate": FALSE_KILL_TOLERANCE},
    }
    if accuracy + ACCURACY_TOLERANCE < base_accuracy:
        failures.append(
            f"准确率下降超过容差：{accuracy:.4f} < 基线 {base_accuracy:.4f} - {ACCURACY_TOLERANCE}"
            f"（来源 {baseline.get('path')}；容差是采样噪声，见注释）"
        )
    if kill_rate > base_kill + FALSE_KILL_TOLERANCE:
        failures.append(
            f"误杀率上升超过容差：{kill_rate:.4f} > 基线 {base_kill:.4f} + {FALSE_KILL_TOLERANCE}"
            f"（来源 {baseline.get('path')}。路线 8.2 的原话：误杀率是比准确率更硬的线）"
        )
    return Case(name=case.name, passed=not failures, failures=failures, detail=detail)


# ------------------------------------------------------------------ ④ 并发压测

def _stopwatch_handler():
    """批次处理函数：返回结论 + 审批单名，并记下"谁被处理了"。"""
    seen: list[int] = []

    def handler(item) -> dict[str, Any]:
        seen.append(item.number)
        return {"state": "done", "detail": "压测合成项", "risk": "无", "ticket": f"batch-{item.number}.md"}

    handler.seen = seen  # type: ignore[attr-defined]
    return handler


def check_concurrency(*, workdir: Path, session: str) -> Case:
    """
    20 个 issue 同时涌入。**四个断言都必须在同一轮里成立**，因为它们互相牵制：
    只要放宽单批上限就能"不重复消费"，而那是另一条红线的失效。
    """
    from src import batch
    from src.batch import BatchItem, BatchState, run_batch, write_sheet
    from src.queue import Task, TaskQueue, TaskState

    failures: list[str] = []
    detail: dict[str, Any] = {}

    # ---- 队列侧：20 线程抢 20 件任务（无丢失无重复）
    queue = TaskQueue(workdir / "queue")
    tasks = [Task(id=f"stress-{index:02d}", type="noop", payload={"n": index}) for index in range(CONCURRENCY)]
    for task in tasks:
        queue.enqueue(task)

    claimed: list[str] = []
    lock = threading.Lock()

    def consumer() -> None:
        while True:
            task = queue.dequeue()
            if task is None:
                return
            with lock:
                claimed.append(task.id)
            queue.complete(task.id)

    threads = [threading.Thread(target=consumer) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=120)

    duplicates = [item for item, count in Counter(claimed).items() if count > 1]
    detail["queue"] = {
        "claimed": len(claimed),
        "expected": CONCURRENCY,
        "duplicates": duplicates,
        "still_pending": sorted(queue.ids_in(TaskState.PENDING)),
    }
    if duplicates:
        failures.append(f"队列出现重复消费：{duplicates}")
    if len(claimed) != CONCURRENCY:
        failures.append(f"队列有丢失：认领 {len(claimed)} 件，应为 {CONCURRENCY} 件")
    if queue.ids_in(TaskState.PENDING):
        failures.append("队列跑完后仍有待处理任务（消费者提前退出）")

    # 重复入队必须被拒绝：否则"无重复消费"可以靠"同一件排队两次"绕过去
    try:
        queue.enqueue(tasks[0])
    except Exception:  # noqa: BLE001 — 拒绝的**具体异常类型**不重要，拒绝本身才重要
        detail["duplicate_enqueue_rejected"] = True
    else:
        detail["duplicate_enqueue_rejected"] = False
        failures.append("同 id 重复入队被接受了（无重复消费的前提条件不成立）")

    # ---- 批量引擎侧：25 个 issue 涌入 → 本批 ≤20，超出的留到下一轮
    issues = [(number, f"压测 issue #{number}") for number in range(1001, 1001 + CONCURRENCY + 5)]
    state = batch.plan(issues, repo="sandbox-messy", date=f"{session}-batch1")
    detail["plan"] = {"items": len(state.items), "remaining": state.remaining}
    if len(state.items) > BATCH_CAP:
        failures.append(f"单批超过上限：{len(state.items)} > {BATCH_CAP}")
    if state.remaining != 5:
        failures.append(f"超出的部分没有如实告知剩余数：remaining={state.remaining}，应为 5")

    handler = _stopwatch_handler()
    run_batch(state, handler, directory=workdir)
    sheet = write_sheet(state, workdir)
    seen_after_first = list(handler.seen)

    detail["batch"] = {
        "processed_first_round": len(seen_after_first),
        "duplicates_in_first_round": [item for item, count in Counter(seen_after_first).items() if count > 1],
        "sheet": sheet.name,
    }
    if sorted(seen_after_first) != sorted(number for number, _ in issues[:BATCH_CAP]):
        failures.append("本批处理的不是最前面的 20 个（计划与执行不一致）")
    if len(set(seen_after_first)) != len(seen_after_first):
        failures.append("同一批内出现重复处理")

    # 续跑本批：已完成的**绝不能再处理一次**
    run_batch(state, handler, directory=workdir)
    if len(handler.seen) != len(seen_after_first):
        failures.append("重跑已完成的批次时又处理了一遍（不重复消费失守）")

    # 末尾审批单必须**逐条**列出（批量呈现 ≠ 批量放行）
    text = sheet.read_text(encoding="utf-8")
    missing_rows = [number for number, _ in issues[:BATCH_CAP] if f"#{number}" not in text]
    detail["sheet_rows"] = text.count("\n| ")
    detail["sheet_missing_rows"] = missing_rows
    if missing_rows:
        failures.append(f"审批单没有逐条列出：缺 {missing_rows[:5]}")
    if "逐条回是/否" not in text:
        failures.append("审批单没有写清「逐条回是/否」—— 那正是「批量呈现 ≠ 批量放行」的那句话")

    # ---- 单条失败不阻断批次
    def flaky(item) -> dict[str, Any]:
        if item.number == 1003:
            raise RuntimeError("压测注入：这一条必失败")
        return {"state": "done", "detail": "ok", "ticket": f"batch-{item.number}.md"}

    failing_round = BatchState(
        date=f"{session}-batch2",
        repo="sandbox-messy",
        items=[BatchItem(number=number, title=f"混合批次 #{number}") for number in (1001, 1002, 1003, 1004)],
    )
    run_batch(failing_round, flaky, directory=workdir)
    states = {item.number: item.state for item in failing_round.items}
    detail["mixed_batch"] = states
    if states.get(1003) != "needs_human":
        failures.append(f"失败的那一条没有标 needs_human：{states.get(1003)}")
    if any(states.get(number) != "done" for number in (1001, 1002, 1004)):
        failures.append(f"单条失败阻断了批次：{states}")
    appendix = batch.render_sheet(failing_round)
    if "需要人类处理的失败项" not in appendix or "#1003" not in appendix:
        failures.append("失败清单没有进审批单附录（人会以为这一批全好了）")

    return Case(name=f"并发压测（{CONCURRENCY} 个 issue 同时涌入）", passed=not failures, failures=failures, detail=detail)


# ------------------------------------------------------------------ ⑤ 中断恢复

def _plan_numbers(count: int, start: int = 2001) -> list[int]:
    return list(range(start, start + count))


def build_interrupted_state(*, workdir: Path, session: str, numbers: list[int], interrupt_at: int):
    """
    制造一次"跑到一半断电"：盘上留下 `state=="doing"` 的那一条，内存里的进度全丢。

    ## 为什么不做真 kill，也不靠"引擎抛异常"

    - **不真 kill**：本机是 AI 沙箱，杀错一个进程整轮工作就没了；而且 pytest 与被测进程
      同在一个 shell 里，风险与收益不成比例。
    - **不靠把异常从 handler 里抛出去**：`run_batch` 按路线 7.1 第 3 条会把任何异常
      记成"这一条失败、继续下一条" —— 于是中断会被写成 `needs_human`（这条 issue 的失败），
      而不是"批次停了"。实测踩过：断言 `doing` 里留下中断点的那一条时拿到的是空列表，
      因为引擎早就把它标成 needs_human 并继续往前跑了。

    所以这里做的是"进程在 `doing` 已落盘、结果还没落盘的那一刻消失"：
    按 `run_batch` 自己的落盘时序（开始处理落一次、处理完落一次）真跑到中断点，
    到点就停手 —— 于是盘上最后一份状态里，中断点那条是 `doing`、它之前的都是 `done`。
    接着**删掉内存对象、只从磁盘读回来**，续跑测的就真的是落盘状态，不是内存里的对象。
    """
    from src.batch import BatchItem, BatchState

    date = f"{session}-resume"
    state = BatchState(
        date=date,
        repo="sandbox-messy",
        items=[BatchItem(number=number, title=f"中断演练 #{number}") for number in numbers],
    )

    def handler(item) -> dict[str, Any]:
        return {"state": "done", "detail": "ok", "ticket": f"batch-{item.number}.md"}

    # 真跑（复刻 `run_batch` 的落盘时序），但每处理完一条就检查是否到了中断点 ——
    # 到了就停手（进程"消失"）。
    processed: list[int] = []
    for item in state.items:
        item.state = "doing"
        state.save(workdir)          # run_batch：开始处理时先落一次盘
        if item.number == interrupt_at:
            # 结果还没落盘，进程就没了：中断点以 `doing` 留在盘上。
            break
        result = handler(item)
        item.state = str(result["state"])
        item.detail = str(result["detail"])
        item.ticket = str(result["ticket"])
        state.save(workdir)          # run_batch：处理完再落一次盘
        processed.append(item.number)

    # 断电前已经落盘的进度：中断点那条是 `doing`，在它之前的都是 `done` ——
    # 这正是 `run_batch` 自己的落盘语义（每条开始与结束各存一次），不是我们编的状态。
    # 从盘上读回来：续跑测的必须是落盘状态，不是内存里的对象。
    loaded = BatchState.load(date, workdir)
    return state, loaded, processed


def recover_interrupted(state, *, directory: Path):
    """
    中断恢复：把卡在 `doing` 的条目退回 `pending`。

    这是「孤儿回收」在批次层的对应物（队列侧是 `queue.recover_orphans`）。
    没有它，一条被中断的批次会永远卡在 doing —— 既不算完成也不算失败，
    而人看到的是一份"处理中"的审批单，谁也不知道它在等什么。
    """
    recovered: list[int] = []
    for item in state.items:
        if item.state == "doing":
            item.state = "pending"
            item.detail = ""
            recovered.append(item.number)
    state.save(directory)
    return recovered


def check_interrupt_recovery(*, workdir: Path, session: str) -> Case:
    """
    中断 → 续跑，断言"不重复处理、最终结果与无中断一致"。

    对照组的做法值得写清楚：**先跑一遍无中断的批次**，再跑中断版，
    两者用同一份处理账比较。只断言"中断版跑完了"是不够的 ——
    一个把已完成的条目重做一遍的实现也能"跑完"。
    """
    from src.batch import BatchItem, BatchState, run_batch

    numbers = _plan_numbers(12)
    interrupt_at = numbers[5]
    failures: list[str] = []
    detail: dict[str, Any] = {}

    # ---- 对照组：无中断
    clean = BatchState(
        date=f"{session}-clean",
        repo="sandbox-messy",
        items=[BatchItem(number=number, title=f"中断演练 #{number}") for number in numbers],
    )
    clean_seen: list[int] = []

    def clean_handler(item) -> dict[str, Any]:
        clean_seen.append(item.number)
        return {"state": "done", "detail": "ok", "ticket": f"batch-{item.number}.md"}

    run_batch(clean, clean_handler, directory=workdir)

    # ---- 中断版：跑到第 6 条时"断电"
    _interrupted, loaded, first_seen = build_interrupted_state(
        workdir=workdir, session=session, numbers=numbers, interrupt_at=interrupt_at
    )
    stuck = [item.number for item in loaded.items if item.state == "doing"]
    failed = [item.number for item in loaded.items if item.state == "needs_human"]
    detail["interrupt"] = {
        "interrupt_at": interrupt_at,
        "processed_before_interrupt": first_seen,
        "stuck_in_doing": stuck,
        "marked_needs_human": failed,
    }
    if stuck != [interrupt_at]:
        failures.append(
            f"中断点没有留下「处理中」的条目：doing={stuck}（期望 [{interrupt_at}]）。"
            "若它被标成 needs_human，说明我们把自己的中断当成了这条 issue 的失败"
        )

    # ---- 续跑：同一批重跑（先做孤儿回收）
    recovered = recover_interrupted(loaded, directory=workdir)
    second_seen: list[int] = []

    def resume_handler(item) -> dict[str, Any]:
        second_seen.append(item.number)
        return {"state": "done", "detail": "ok", "ticket": f"batch-{item.number}.md"}

    run_batch(loaded, resume_handler, directory=workdir)

    detail["recovery"] = {
        "recovered": recovered,
        "reprocessed": second_seen,
        "repeated": sorted(set(first_seen) & set(second_seen)),
    }
    if recovered != [interrupt_at]:
        failures.append(f"孤儿回收回收了不该回收的条目：{recovered}")
    if set(first_seen) & set(second_seen):
        failures.append(f"续跑重复处理了已完成的条目：{sorted(set(first_seen) & set(second_seen))}")
    # 续跑要处理的**恰好**是"中断点 + 它之后还没跑的" —— 少了说明有任务被漏掉，
    # 多了说明已完成的被重做（上面那条已单独判）。
    outstanding = [number for number in numbers if number not in first_seen]
    if sorted(second_seen) != sorted(outstanding):
        failures.append(f"续跑处理范围不对：拿到 {second_seen}，应为 {outstanding}")

    # ---- 最终结果与无中断一致
    clean_final = {item.number: item.state for item in clean.items}
    resumed_final = {item.number: item.state for item in loaded.items}
    detail["clean_final"] = clean_final
    detail["resumed_final"] = resumed_final
    if clean_final != resumed_final:
        diff = {key: (clean_final.get(key), resumed_final.get(key)) for key in set(clean_final) | set(resumed_final)}
        failures.append(f"恢复后的最终结果与无中断不一致：{diff}")

    return Case(name="中断恢复（批次中途断电 → 续跑）", passed=not failures, failures=failures, detail=detail)


# ------------------------------------------------------------------ 报告

def render_markdown(report: WeeklyReport) -> str:
    lines = [
        f"# 每周脏测试报告 {report.date}",
        "",
        f"- 模式：`{report.mode}`　语料：`{report.corpus}`",
        (
            f"- 基线：`{report.baseline.get('path')}`（准确率 {report.baseline.get('accuracy')}，"
            f"误杀率 {report.baseline.get('false_kill_rate')}）"
        ),
        (
            f"- 结论：**{'全过' if report.passed else '有失败'}**"
            f"（{len(report.cases) - len(report.failed)}/{len(report.cases)} 项通过）"
        ),
        "",
        "> 路线 8.3：「脏测试（回放/压测/中断）—— 每周」；红线同端到端：不过即冻结自动化，只修不开发。",
        "",
    ]
    for case in report.cases:
        lines += [
            f"## {'✅' if case.passed else '❌'} {case.name}",
            "",
        ]
        for key, value in case.detail.items():
            if isinstance(value, dict):
                rendered = "、".join(f"{k}={v}" for k, v in value.items() if not isinstance(v, (dict, list)))
                lines.append(f"- `{key}`：{rendered or '（见 JSON）'}")
            elif isinstance(value, list):
                lines.append(f"- `{key}`：{len(value)} 条（明细见 JSON）")
            else:
                lines.append(f"- `{key}`：{value}")
        if case.failures:
            lines += ["", "**失败原因：**", ""]
            lines += [f"- {item}" for item in case.failures]
        lines.append("")

    lines += [
        "---",
        "",
        "## 说明（为什么这样算）",
        "",
        "- 误杀率 = 被判成 `spam`/`duplicate`（动作 `gate_close`）的真实 issue 比例。",
        "  它与准确率分开看，因为代价不对称：判错一格只是标签不合适，误杀是把人赶走。",
        "- 回放用的是**冻结回放集的 holdout 一半**：基线就在这 164 条上算出来，",
        "  拿全集去对一条由子集算出来的分数线，比较的就不是同一个东西。",
        "- 中断恢复用的是**可注入的失败点**，不是真杀本机进程（本机是 AI 沙箱，",
        "  杀错一个进程整轮工作就没了）。它复现的是同一个控制流：进度只留在盘上。",
        "",
    ]
    return "\n".join(lines)


def write_report(report: WeeklyReport, *, out_dir: Path | None = None) -> tuple[Path, Path]:
    target_dir = out_dir or OUT_ROOT
    target_dir.mkdir(parents=True, exist_ok=True)
    stamp = report.date
    json_path = target_dir / f"dirty-{stamp}.json"
    md_path = target_dir / f"dirty-{stamp}.md"
    json_path.write_text(json.dumps(report.as_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    md_path.write_text(render_markdown(report), encoding="utf-8")
    return json_path, md_path


def run(
    *,
    offline: bool = False,
    limit: int = 0,
    cohort: str = "holdout",
    baseline_path: Path | None = None,
    corpus_path: Path | None = None,
    out_dir: Path | None = None,
) -> tuple[WeeklyReport, Path, Path]:
    """跑三项并落产物。缺件直接抛 `MissingArtifact`，由 `main()` 转成退出码 2。"""
    from src.bounce import latest_verdicts

    rows, origin = load_corpus(corpus_path)
    if cohort:
        selected = [row for row in rows if (row.get("cohort") or "dev") == cohort]
        if not selected:
            raise MissingArtifact(f"语料里没有 cohort={cohort!r} 的样本（{origin}）")
        rows = selected
        origin = f"{origin} cohort:{cohort}"
    if limit:
        # `--limit` 在**切完 cohort 之后**才截断：先截断会让"只跑 30 条"变成
        # "在 30 条里碰运气看有没有 holdout"，于是一条都取不到（实测踩过）。
        rows = rows[:limit]
        origin = f"{origin} limit:{limit}"

    base_path, base_report = baseline_report(explicit=baseline_path)
    baseline = _baseline_metrics(base_path, base_report)

    replay_case = replay_corpus(rows=rows, offline=offline, adjudicated=latest_verdicts(), label="① 真实语料回放")
    replay_case = compare_with_baseline(replay_case, baseline)

    session = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    scratch = OUT_ROOT / "work" / session
    scratch.mkdir(parents=True, exist_ok=True)

    # 压测与中断恢复都在**临时目录**里造状态：它们的产物是"断言通过/失败"，
    # 不是运行期状态，不该污染真实 state/（真实 state/ 只放质检产物）。
    concurrency_case = check_concurrency(workdir=scratch, session=session)
    interrupt_case = check_interrupt_recovery(workdir=scratch, session=session)

    report = WeeklyReport(
        date=datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        mode="offline_stub" if offline else "local_small",
        corpus=origin,
        baseline=baseline,
        cases=[replay_case, concurrency_case, interrupt_case],
    )
    json_path, md_path = write_report(report, out_dir=out_dir)
    return report, json_path, md_path


def freeze_baseline() -> int:
    """
    把"最近一份 `triage-eval-*.json`"冻成金样本 `state/corpus/dirty-baseline.json`。

    为什么必须显式：基线是**判据的来源**，自动改写等于让考卷自己改分数。
    冻结时把来源文件、日期、准确率/误杀率一起写进 `provenance`，
    将来有人问"这把尺子哪来的"能直接查到（而不是只看到一个光秃秃的数字）。
    """
    found = sorted(glob.glob(str(ROOT / "state" / "reports" / "triage-eval-*.json")), key=os.path.getmtime)
    if not found:
        print(
            "缺件：没有 state/reports/triage-eval-*.json 可冻。"
            "先跑 `python tools/eval_triage.py --from-replay --split holdout`。"
        )
        return EXIT_MISSING
    source = Path(found[-1])
    live = json.loads(source.read_text(encoding="utf-8"))
    accuracy = live.get("accuracy")
    kill_rate = live.get("false_kill_rate")
    if accuracy is None or kill_rate is None:
        print(f"缺件：{source.name} 里没有 accuracy / false_kill_rate 字段，冻不了")
        return EXIT_MISSING

    FROZEN_BASELINE.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "frozen": True,
        "date": live.get("date"),
        "accuracy": float(accuracy),
        "false_kill_rate": float(kill_rate),
        "provenance": {
            "source": source.relative_to(ROOT).as_posix(),
            "cohort": "holdout",
            "frozen_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "why": (
                "triage-eval-*.json 是活文件（每次 3.1 验收都会重写），拿它当尺子会让基线漂移；"
                "冻成金样本之后，每周脏测试比的才是同一把尺子。容差见 dirty_weekly.ACCURACY_TOLERANCE。"
            ),
        },
    }
    FROZEN_BASELINE.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"已冻结基线：{FROZEN_BASELINE.relative_to(ROOT).as_posix()}")
    print(f"  来源 {source.name}：准确率 {payload['accuracy']:.4f}、误杀率 {payload['false_kill_rate']:.4f}")
    # 冻结之后立刻用**冻结件**跟它自己比一次：确认读得回来、字段对得上（否则周演练会报缺件）
    path, report = baseline_report()
    baseline = _baseline_metrics(path, report)
    print(f"  自检：读回 {path.name} → 准确率 {baseline['accuracy']:.4f}（frozen={baseline.get('frozen')}）")
    return EXIT_OK


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

    parser = argparse.ArgumentParser(description="8.2 ①④⑤ 每周脏测试")
    parser.add_argument("--offline", action="store_true", help="不调本地模型，用确定性关键词桩（分数必然低，别当验收）")
    parser.add_argument("--limit", type=int, default=0, help="只回放前 N 条（调试用）")
    parser.add_argument("--cohort", default="holdout", help="只用回放集里的哪一半；空字符串=全部")
    parser.add_argument("--baseline", default="", help="显式指定基线 JSON（默认优先冻结基线，其次最近一份 triage-eval-*.json）")
    parser.add_argument("--corpus", default="", help="显式指定语料文件")
    parser.add_argument("--out-dir", default="", help=f"产物目录（默认 {OUT_ROOT}）")
    parser.add_argument(
        "--freeze-baseline",
        action="store_true",
        help=f"把当前最近一份 triage-eval-*.json 冻成 {FROZEN_BASELINE.relative_to(ROOT).as_posix()}（显式动作，绝不自动改写）",
    )
    args = parser.parse_args()

    if args.freeze_baseline:
        return freeze_baseline()

    try:
        report, json_path, md_path = run(
            offline=args.offline,
            limit=args.limit,
            cohort=args.cohort,
            baseline_path=Path(args.baseline) if args.baseline else None,
            corpus_path=Path(args.corpus) if args.corpus else None,
            out_dir=Path(args.out_dir) if args.out_dir else None,
        )
    except MissingArtifact as exc:
        # 缺件**单独一档**：它既不是通过，也不是"指标不达标"。
        print(f"缺件：{exc}")
        return EXIT_MISSING
    except Exception:  # noqa: BLE001 — 崩了要留下痕迹，但退出码仍是"失败"而不是"缺件"
        print("脏测试自身崩了（这不是缺件，是工具坏了）：")
        traceback.print_exc()
        return EXIT_FAILED

    for case in report.cases:
        print(f"{'PASS' if case.passed else 'FAIL'}  {case.name}")
        for key, value in case.detail.items():
            if isinstance(value, (int, float, str)):
                print(f"        {key} = {value}")
        for item in case.failures:
            print(f"        失败：{item}")
    print()
    print(f"JSON：{json_path}")
    print(f"报告：{md_path}")
    return EXIT_OK if report.passed else EXIT_FAILED


if __name__ == "__main__":
    raise SystemExit(main())
