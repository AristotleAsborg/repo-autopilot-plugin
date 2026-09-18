"""错题本：人工裁决过的真值 `state/corpus/triage-adjudicated.jsonl`（**只增不改**）。

## 为什么真值不能由系统自己改

3.1 的实测给了结论：**仓库标签有噪声**（人类复核确认那 5 条是标签打错了）。
但"标签有噪声"和"系统可以自己改真值"是两件完全不同的事：

- 作者说"这不是 bug" → 记账（`bounce.core` 的 `source="author"`），**不改真值**：
  会喊的人不等于对的人；
- **只有人裁决**才能往这里写一条。所以这个文件是**真值来源唯一的升级路径**，
  仓库标签降级成它的兜底。

## 为什么"裁决过的样本要从 holdout 移进 dev"

3.1 的分数只在 holdout 上报，而 holdout 的定义是"**从未参与调参、也从未被人裁决过**"。
一条样本一旦被人看过答案并写进这里，它就不再是考卷了 —— 再拿它报分就是背答案。
所以 `migrate_cohorts()` 把它从 holdout 移进 dev：**看过答案的题，只能当练习册**。

文件只增不改（append-only）：裁决是可以被推翻的（人也会看错），但推翻要**再追加一条**
新的裁决，而不是改掉旧的 —— 历史里必须留着"当时是谁、凭什么这么判"。
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
ADJUDICATED_PATH = ROOT / "state" / "corpus" / "triage-adjudicated.jsonl"
REPLAY_PATH = ROOT / "state" / "corpus" / "triage-replay.jsonl"

#: 人工裁决的合法取值：认仓库标签 / 认模型判断 / 都不认（另给一个标签）
VERDICTS = ("repo", "model", "other")


class AdjudicationError(RuntimeError):
    """裁决记录不合法（缺裁决人、verdict 非法）。绝不吞掉。"""


@dataclasses.dataclass(frozen=True)
class Adjudication:
    """一条人工裁决。`by` 与 `basis` 是必填的 —— 没有依据的裁决无法被后人复核。"""

    key: str
    repo_label: str | None
    model_label: str | None
    verdict: str
    by: str
    basis: str = ""
    final_label: str | None = None
    at: str = ""

    def __post_init__(self) -> None:
        if self.verdict not in VERDICTS:
            raise AdjudicationError(f"{self.verdict!r} 不是合法裁决。合法值：{list(VERDICTS)}")
        if not (self.by or "").strip():
            raise AdjudicationError("裁决必须记录裁决人")
        if not (self.basis or "").strip():
            raise AdjudicationError("裁决必须写依据 —— 否则以后没人能复核它")
        if self.verdict == "other" and not self.final_label:
            raise AdjudicationError("verdict=other 时必须给出 final_label")
        if not self.key or "#" not in self.key:
            raise AdjudicationError(f"key 必须是 repo#number 形式，收到 {self.key!r}")
        if not self.at:
            object.__setattr__(self, "at", datetime.now(timezone.utc).isoformat(timespec="seconds"))

    @property
    def truth(self) -> str | None:
        """这条样本裁决后的真值。"""
        if self.verdict == "repo":
            return self.repo_label
        if self.verdict == "model":
            return self.model_label
        return self.final_label

    def as_dict(self) -> dict[str, Any]:
        data = dataclasses.asdict(self)
        data["truth"] = self.truth
        return data


def append(record: Adjudication, path: Path | None = None) -> Path:
    target = path or ADJUDICATED_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record.as_dict(), ensure_ascii=False) + "\n")
    return target


def load(path: Path | None = None) -> list[dict[str, Any]]:
    target = path or ADJUDICATED_PATH
    if not target.exists():
        return []
    rows: list[dict[str, Any]] = []
    with open(target, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def truth_map(path: Path | None = None) -> dict[str, str]:
    """
    当前生效的真值表：同一条被裁决多次时**以最后一条为准**（追加式文件的时间序就是优先级）。
    """
    truths: dict[str, str] = {}
    for row in load(path):
        if row.get("truth"):
            truths[str(row["key"])] = str(row["truth"])
    return truths


def latest_verdicts(path: Path | None = None) -> dict[str, dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for row in load(path):
        latest[str(row["key"])] = row
    return latest


def migrate_cohorts(
    replay_path: Path | None = None, adjudicated_path: Path | None = None
) -> dict[str, Any]:
    """
    把裁决过的样本从 holdout 移进 dev（**看过答案的题不能再当考卷**）。

    返回 `{"moved": n, "considered": m, "already": k}`。
    只改 `cohort` 字段，正文与标签一个字都不动 —— 那份数据同时是错题本。
    """
    replay = replay_path or REPLAY_PATH
    adjudicated = set(truth_map(adjudicated_path))
    if not replay.exists() or not adjudicated:
        return {"moved": 0, "considered": len(adjudicated), "already": 0}

    rows: list[dict[str, Any]] = []
    moved = already = 0
    with open(replay, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            key = f"{row.get('repo')}#{row.get('number')}"
            if key in adjudicated and (row.get("cohort") or "dev") != "dev":
                row["cohort"] = "dev"
                row["cohort_reason"] = "已被人裁决过：从 holdout 移入 dev，防止背答案报分"
                moved += 1
            elif key in adjudicated:
                already += 1
            rows.append(row)

    if moved:
        temporary = replay.with_suffix(replay.suffix + ".tmp")
        with open(temporary, "w", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        temporary.replace(replay)
    return {"moved": moved, "considered": len(adjudicated), "already": already}


def cohort_counts(replay_path: Path | None = None) -> dict[str, int]:
    replay = replay_path or REPLAY_PATH
    counts: dict[str, int] = {}
    if not replay.exists():
        return counts
    with open(replay, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            cohort = str(row.get("cohort") or "dev")
            counts[cohort] = counts.get(cohort, 0) + 1
    return counts


def iter_truths(rows: Iterable[dict[str, Any]]) -> dict[str, str]:
    """从一个已读入的裁决列表里取真值（给测试与工具用，避免重复读文件）。"""
    truths: dict[str, str] = {}
    for row in rows:
        if row.get("truth"):
            truths[str(row["key"])] = str(row["truth"])
    return truths
