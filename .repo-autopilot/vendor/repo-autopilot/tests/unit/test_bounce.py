"""3.3 打回通路的确定性单元测试（不联网、不调模型）。

钉死的四件事（都对应路线 3.3 里的具体句子）：

1. **枚举与校验**：打回原因/来源是闭集，非法值直接报错（自由文本没法统计，没法统计就没法改进）；
2. **记账幂等 + 次数上限**：同一事实报两遍不重复计数；**两条不同事实**才触发 `needs_human`；
3. **动作分级**：改标签要 ≥0.7 且必须落在闸门的写操作白名单里（`relabel`）；
4. **真值只有人能改**：作者自纠只记账，`triage-adjudicated.jsonl` 里不会凭空多出一条；
   裁决过的样本必须从 holdout 移进 dev（看过答案的题不能再当考卷）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.bounce import (
    MAX_BOUNCES,
    PHASE_FIXING,
    PHASE_NEEDS_HUMAN,
    PHASE_TRIAGED,
    RELABEL_CONFIDENCE,
    Adjudication,
    AdjudicationError,
    Bounce,
    BounceError,
    BounceReason,
    apply_bounce,
    bounce_rates,
    cohort_counts,
    find_task,
    load_task,
    migrate_cohorts,
    phase_of,
    precheck,
    record_bounce,
    relabel_decision,
    truth_map,
)
from src.bounce.adjudicated import append as append_adjudication
from src.github import WRITE_ACTIONS


def make_task(task_id: str = "t1", phase: str = PHASE_FIXING) -> dict:
    return {"id": task_id, "type": "fix", "payload": {"repo": "o/r", "number": 1, "phase": phase}}


# ------------------------------------------------------------------ 枚举与校验

def test_valid_bounce_is_accepted() -> None:
    bounce = Bounce(reason=BounceReason.NOT_A_DEFECT, by="precheck", source="precheck")
    assert bounce.reason is BounceReason.NOT_A_DEFECT
    assert bounce.at  # 自动补时间戳


@pytest.mark.parametrize("reason", ["随便写的理由", "", "NOT_A_DEFECT2"])
def test_illegal_reason_is_refused(reason: str) -> None:
    with pytest.raises(BounceError, match="不是合法的打回原因"):
        Bounce(reason=reason, by="precheck", source="precheck")  # type: ignore[arg-type]


def test_illegal_source_is_refused() -> None:
    with pytest.raises(BounceError, match="不是合法来源"):
        Bounce(reason=BounceReason.UNCLEAR_REQUEST, by="x", source="某个新来源")


def test_bounce_without_a_person_is_refused() -> None:
    """没有责任人的记录等于没有记录。"""
    with pytest.raises(BounceError, match="谁打的"):
        Bounce(reason=BounceReason.UNCLEAR_REQUEST, by="  ", source="precheck")


def test_fingerprint_ignores_note_and_time() -> None:
    first = Bounce(reason=BounceReason.NOT_A_DEFECT, by="precheck", source="precheck", note="a")
    second = Bounce(reason=BounceReason.NOT_A_DEFECT, by="precheck", source="precheck", note="b")
    assert first.fingerprint() == second.fingerprint()


def test_fingerprint_differs_on_labels() -> None:
    first = Bounce(reason=BounceReason.WRONG_LABEL, by="p", source="precheck", original_label="question")
    second = Bounce(reason=BounceReason.WRONG_LABEL, by="p", source="precheck", original_label="feature")
    assert first.fingerprint() != second.fingerprint()


# ------------------------------------------------------------------ 记账

def test_apply_bounce_records_and_returns_to_triage() -> None:
    task = make_task()
    outcome = apply_bounce(task, Bounce(BounceReason.NOT_REPRODUCIBLE, "precheck", "precheck"))
    assert outcome.recorded is True
    assert outcome.count == 1
    assert outcome.phase == PHASE_TRIAGED
    assert outcome.needs_human is False
    assert phase_of(task) == PHASE_TRIAGED


def test_same_fact_twice_is_idempotent() -> None:
    task = make_task()
    bounce = Bounce(BounceReason.NOT_A_DEFECT, "precheck", "precheck")
    apply_bounce(task, bounce)
    again = apply_bounce(task, bounce)
    assert again.recorded is False
    assert again.count == 1, "同一事实报两遍不该把它算成两次"
    assert again.needs_human is False


def test_two_distinct_bounces_stop_the_line() -> None:
    task = make_task()
    apply_bounce(task, Bounce(BounceReason.NOT_REPRODUCIBLE, "precheck", "precheck"))
    second = apply_bounce(task, Bounce(BounceReason.UNCLEAR_REQUEST, "author", "author"))
    assert second.count == MAX_BOUNCES
    assert second.needs_human is True
    assert second.phase == PHASE_NEEDS_HUMAN
    assert phase_of(task) == PHASE_NEEDS_HUMAN


def test_bounce_history_is_never_deleted() -> None:
    task = make_task()
    for reason in (BounceReason.NOT_REPRODUCIBLE, BounceReason.UNCLEAR_REQUEST):
        apply_bounce(task, Bounce(reason, "precheck", "precheck"))
    entries = task["payload"]["bounces"]
    assert [entry["reason"] for entry in entries] == ["not_reproducible", "unclear_request"]
    assert all(entry["at"] for entry in entries)


def test_record_bounce_writes_a_readable_task_file(scratch: Path) -> None:
    path = scratch / "t1.json"
    path.write_text(json.dumps(make_task(), ensure_ascii=False), encoding="utf-8")
    outcome = record_bounce(path, Bounce(BounceReason.WRONG_LABEL, "human", "human_review"))
    assert outcome.recorded is True
    reloaded = load_task(path)                     # 必须是合法 JSON（半截文件会在这里炸）
    assert reloaded["payload"]["bounces"][0]["reason"] == "wrong_label"
    assert not list(scratch.glob("*.tmp")), "临时文件必须已经被替换掉，不能留在盘上"


def test_find_task_searches_every_state_directory(scratch: Path) -> None:
    base = scratch / "tasks" / "doing"
    base.mkdir(parents=True)
    (base / "abc.json").write_text(json.dumps(make_task("abc")), encoding="utf-8")
    assert find_task("abc", scratch) == base / "abc.json"
    assert find_task("nope", scratch) is None


# ------------------------------------------------------------------ 预检

def test_precheck_bounces_a_question_labelled_bug() -> None:
    bounce = precheck("bug", "question", 0.9)
    assert bounce is not None
    assert bounce.reason is BounceReason.NOT_A_DEFECT
    assert bounce.suggested_label == "question"


def test_precheck_bounces_a_bug_labelled_question() -> None:
    bounce = precheck("question", "bug", 0.9)
    assert bounce is not None
    assert bounce.reason is BounceReason.WRONG_LABEL


def test_precheck_stays_quiet_below_confidence() -> None:
    assert precheck("bug", "question", 0.79) is None


def test_precheck_stays_quiet_when_labels_agree() -> None:
    assert precheck("bug", "bug", 0.99) is None


def test_precheck_stays_quiet_without_a_label() -> None:
    assert precheck(None, "bug", 0.99) is None
    assert precheck("bug", None, 0.99) is None


# ------------------------------------------------------------------ 动作分级

@pytest.mark.parametrize(
    ("confidence", "expected"),
    [(0.0, "comment_only"), (0.699, "comment_only"), (RELABEL_CONFIDENCE, "propose"), (0.99, "propose")],
)
def test_relabel_decision_boundary(confidence: float, expected: str) -> None:
    assert relabel_decision(confidence) == expected


def test_relabel_is_a_gated_write_action() -> None:
    """
    改标签必须走闸门 —— 这条测试的意义是：**将来有人新增写操作时不会忘记给它加闸**
    （白名单是 fail-closed 的，不在名单里的动作会被 `assert_write_action` 拒掉）。
    """
    assert "relabel" in WRITE_ACTIONS
    assert "close_issue" not in WRITE_ACTIONS  # 关闭只能复用 close_other_issue，不存在新动作


def test_bounce_rates_separate_the_two_lines() -> None:
    rates = bounce_rates([(True, "bug"), (True, "question"), (False, "bug"), (False, "feature")])
    assert rates["bounce_rate"] == 0.5
    assert rates["false_bounce_rate"] == 0.25


# ------------------------------------------------------------------ 真值与错题本

def _adjudication(key: str, verdict: str = "model", **overrides: object) -> Adjudication:
    kwargs: dict = {
        "key": key,
        "repo_label": "question",
        "model_label": "feature",
        "verdict": verdict,
        "by": "human",
        "basis": "正文在要功能，不是提问",
    }
    kwargs.update(overrides)
    return Adjudication(**kwargs)  # type: ignore[arg-type]


def test_adjudication_requires_a_person_and_a_basis() -> None:
    with pytest.raises(AdjudicationError, match="裁决人"):
        _adjudication("o/r#1", by=" ")
    with pytest.raises(AdjudicationError, match="依据"):
        _adjudication("o/r#1", basis="")


def test_adjudication_verdict_must_be_known() -> None:
    with pytest.raises(AdjudicationError, match="不是合法裁决"):
        _adjudication("o/r#1", verdict="我说了算")


def test_adjudication_other_requires_a_final_label() -> None:
    with pytest.raises(AdjudicationError, match="final_label"):
        _adjudication("o/r#1", verdict="other")


@pytest.mark.parametrize(
    ("verdict", "expected"),
    [("repo", "question"), ("model", "feature")],
)
def test_adjudication_truth_follows_the_verdict(verdict: str, expected: str) -> None:
    assert _adjudication("o/r#1", verdict=verdict).truth == expected


def test_author_bounce_does_not_touch_the_truth_book(scratch: Path) -> None:
    """
    作者说"这不是 bug"只记账（`source="author"`），**真值一个字不改** ——
    否则一个会喊的人就能改掉事实。
    """
    path = scratch / "triage-adjudicated.jsonl"
    assert truth_map(path) == {}
    task = make_task()
    apply_bounce(task, Bounce(BounceReason.AUTHOR_DENIED, "author", "author", note="作者说不是 bug"))
    assert task["payload"]["bounces"][0]["source"] == "author"
    assert truth_map(path) == {}, "记账不等于改真值"


def test_append_only_truth_book_keeps_last_verdict(scratch: Path) -> None:
    path = scratch / "triage-adjudicated.jsonl"
    append_adjudication(_adjudication("o/r#1", verdict="repo"), path)
    append_adjudication(_adjudication("o/r#1", verdict="model"), path)
    with open(path, encoding="utf-8") as handle:
        lines = [line for line in handle if line.strip()]
    assert len(lines) == 2, "推翻裁决要再追加一条，不能改掉旧的"
    assert truth_map(path)["o/r#1"] == "feature"  # 后写的生效


def test_migrate_cohorts_moves_adjudicated_samples_to_dev(scratch: Path) -> None:
    replay = scratch / "triage-replay.jsonl"
    rows = [
        {"repo": "o/r", "number": 1, "title": "a", "body": "", "label": "question", "cohort": "holdout"},
        {"repo": "o/r", "number": 2, "title": "b", "body": "", "label": "bug", "cohort": "holdout"},
        {"repo": "o/r", "number": 3, "title": "c", "body": "", "label": "bug"},  # 无 cohort = dev
    ]
    replay.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n", encoding="utf-8"
    )
    book = scratch / "triage-adjudicated.jsonl"
    append_adjudication(_adjudication("o/r#1"), book)

    result = migrate_cohorts(replay, book)
    assert result["moved"] == 1
    assert cohort_counts(replay) == {"dev": 2, "holdout": 1}
    moved = json.loads(replay.read_text(encoding="utf-8").splitlines()[0])
    assert moved["cohort"] == "dev"
    assert "背答案" in moved["cohort_reason"]
    # 正文一个字都不能动：它同时还是错题本
    assert moved["title"] == "a"
    assert moved["label"] == "question"
