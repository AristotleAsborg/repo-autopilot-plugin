"""M1 追问循环的**单轮驱动**（把 `IdeaRefiner` 用起来的外壳）。

为什么需要它：`IdeaRefiner` 是纯代码实现，它**自己不决定何时停**（停止判断属 2.2），
也不持有对话。对话在 DSH 会话里，代码在仓库里，两边靠 `state/specs/<id>.draft.json`
（源卡，唯一真相）交接：每条命令只做一轮，读写同一份 draft，因此**进程可以随时死**，
进度不丢（路线 2.1 硬约束 4：每轮落盘）。

命令：
  init     一句话 idea → 新 draft + 第 1 问
  ask      出下一问
  answer   记录人类回答（flash 抽取成 spec 字段）
  judge    2.2 三路表决（完整度 / 信息增益 / 已答轮数）→ 判停 + 缺口反馈下一问
  finalize 产出 `state/specs/<id>.md`（终稿）
  status   只读打印当前 draft

所有输出都是 JSON（写到 `--out`，缺省 stdout），并强制 UTF-8：
本机是 Windows，控制台编码会把中文变成乱码，读文件比读终端可靠。
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.spec.refiner import (  # noqa: E402
    Extraction,
    SpecDraft,
    build_extract_messages,
    new_draft,
)
from src.spec.refiner import IdeaRefiner as _IdeaRefiner  # noqa: E402
from src.spec.stopper import StopJudger, is_machine_checkable, unresolved_gaps  # noqa: E402


def _make_refiner(store_dir: Path, generate_tier: str, extract_tier: str) -> _IdeaRefiner:
    """按档位装配生成器/抽取器。`flash_api` = 强模型（生产），`local_small` = 本机兜底。"""

    def generate(messages, schema):
        from src.gateway import chat

        return chat(messages, schema, generate_tier)

    def extract(draft, question, answer, options=(), no_means=()):
        from src.gateway import chat

        return chat(
            build_extract_messages(draft, question, answer, options=options, no_means=no_means),
            Extraction,
            extract_tier,
        )

    return _IdeaRefiner(
        store_dir=store_dir,
        generate=generate,
        extract=extract if extract_tier else None,
    )


def _gaps_path(store_dir: Path, spec_id: str) -> Path:
    return store_dir / f"{spec_id}.gaps.json"


def _load_gaps(store_dir: Path, spec_id: str) -> list[str]:
    path = _gaps_path(store_dir, spec_id)
    if not path.exists():
        return []
    try:
        return [str(item) for item in json.loads(path.read_text(encoding="utf-8")).get("open_gaps", [])]
    except (OSError, ValueError):
        return []


def _save_gaps(store_dir: Path, spec_id: str, gaps: list[str]) -> None:
    path = _gaps_path(store_dir, spec_id)
    path.write_text(
        json.dumps({"open_gaps": [str(g).strip() for g in gaps if str(g).strip()]}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _load(store_dir: Path, spec_id: str) -> SpecDraft:
    return _IdeaRefiner.load(store_dir / f"{spec_id}.draft.json")


def _dump(payload: dict, out: str | None) -> None:
    # `StopDecision` 是普通 dataclass，里面套着 `Signal` dataclass 的列表 ——
    # `json.dumps` 不认它们（实测报 "Object of type Signal is not JSON serializable"）。
    # 这里统一递归拍平，而不是在每个命令里各写一遍。
    text = json.dumps(payload, ensure_ascii=False, indent=2, default=_json_fallback)
    if out:
        Path(out).write_text(text + "\n", encoding="utf-8")
    else:
        sys.stdout.write(text + "\n")


def _json_fallback(value):
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return dataclasses.asdict(value)
    if isinstance(value, Path):
        return str(value)
    return repr(value)


def _round_view(item) -> dict:
    return {
        "index": item.index,
        "question": item.question,
        "why": item.why,
        "options": list(item.options),
        "no_means": list(item.no_means),
    }


def cmd_init(args) -> int:
    store = Path(args.specs)
    refiner = _make_refiner(store, args.generate_tier, args.extract_tier)
    draft = new_draft(args.idea)
    refiner.persist(draft)
    item = refiner.ask(draft)
    _dump(
        {
            "ok": True,
            "id": draft.id,
            "draft_path": str(refiner.path_for(draft)),
            "round": _round_view(item),
            "features": draft.features,
            "non_goals": draft.non_goals,
            "acceptance": draft.acceptance,
            "warnings": refiner.warnings,
        },
        args.out,
    )
    return 0


def cmd_ask(args) -> int:
    store = Path(args.specs)
    refiner = _make_refiner(store, args.generate_tier, args.extract_tier)
    # **缺口必须从盘上读回来**：`note_gaps` 只活在内存里，而每条命令都是一个新进程，
    # 且 `SpecDraft` 没有 `open_gaps` 字段（`model_dump()` 里确认过没有）——
    # 所以不落一份旁路日志，判停诊断出的缺口会**每轮静默丢失**，
    # `completeness.missing` 就会连续多轮重复同样的缺口却永远变不成问题。
    gaps = _load_gaps(store, args.id)
    if args.note_gap:
        gaps.extend(args.note_gap)
    refiner.note_gaps(gaps)
    draft = _load(store, args.id)
    item = refiner.ask(draft)
    _save_gaps(store, args.id, refiner.open_gaps)
    _dump(
        {
            "ok": True,
            "id": draft.id,
            "round": _round_view(item),
            "adopted": item.adopted,
            "gaps_asked_with": gaps,
            "warnings": refiner.warnings,
            "rounds_so_far": len(draft.rounds),
        },
        args.out,
    )
    return 0


def cmd_answer(args) -> int:
    store = Path(args.specs)
    refiner = _make_refiner(store, args.generate_tier, args.extract_tier)
    draft = _load(store, args.id)
    if not draft.rounds:
        _dump({"ok": False, "error": "还没有问过任何一轮，先跑 ask"}, args.out)
        return 2
    last = draft.rounds[-1]
    if last.answered:
        _dump(
            {"ok": False, "error": f"第 {last.index} 轮已经答过了（{last.answer!r}）"},
            args.out,
        )
        return 2
    draft = refiner.answer(draft, args.text)
    item = draft.rounds[-1]
    _dump(
        {
            "ok": True,
            "id": draft.id,
            "answered_round": item.index,
            "answer": item.answer,
            "adopted": item.adopted,
            "adopted_empty_warning": not item.adopted,
            "features": draft.features,
            "non_goals": draft.non_goals,
            "acceptance": draft.acceptance,
            "warnings": refiner.warnings,
        },
        args.out,
    )
    return 0


def cmd_judge(args) -> int:
    store = Path(args.specs)
    draft = _load(store, args.id)
    judger = StopJudger(spec_dir=store)
    decision = judger.judge(draft)
    # 缺口反馈回提问（路线 2.1 第 6 条）：不接这一步，缺口会永远不变成问题。
    # 落盘而不是只留在内存里 —— 见 `cmd_ask` 里的说明。
    _save_gaps(store, args.id, list(decision.blocked_by_gaps))
    # 用库自己的 `as_dict()`：手写 `__dict__` 会把嵌套的 `Signal` 原样漏出去。
    payload = decision.as_dict() if hasattr(decision, "as_dict") else dataclasses.asdict(decision)
    payload.update(
        {
            "ok": True,
            "id": draft.id,
            "status": draft.status,
            "answered_rounds": draft.answered_rounds if hasattr(draft, "answered_rounds") else None,
            "unresolved_gaps": unresolved_gaps(draft),
            "gaps_note": f"已写回 open_gaps，下一问会带着这 {len(decision.blocked_by_gaps)} 条缺口",
        }
    )
    _dump(payload, args.out)
    return 0


def cmd_finalize(args) -> int:
    store = Path(args.specs)
    draft = _load(store, args.id)
    judger = StopJudger(spec_dir=store)
    # **只用官方定稿入口 `finalize()`，不要再调 `write_markdown()`。**
    # 两者写的是**同一个路径**（`state/specs/<id>.md`），后者是中间渲染稿，
    # 调它等于把官方产物覆盖掉 —— 而 `finalize()` 比 `render_markdown()` 多三件事
    # （状态 converged/converged_with_gaps 分离、标注不可机器验证的验收条件、写定稿），
    # 这正是 `tools/m1_finalize_probe.py` 记下来的口径。
    target = judger.finalize(draft)
    _save_gaps(store, args.id, list(unresolved_gaps(draft)))
    unresolved = unresolved_gaps(draft)
    vague = [item for item in draft.acceptance if not is_machine_checkable(item)]
    _dump(
        {
            "ok": True,
            "id": draft.id,
            "status": draft.status,
            "final_md": str(target),
            "features": draft.features,
            "non_goals": draft.non_goals,
            "withdrawn": draft.withdrawn,
            "acceptance": draft.acceptance,
            "not_machine_checkable": vague,
            "not_machine_checkable_count": len(vague),
            "unresolved_gaps": unresolved,
        },
        args.out,
    )
    return 0


def cmd_note_gap(args) -> int:
    """人工/外部检查发现、而三路信号没看出来的缺口，写进旁路日志逼下一问问清。"""
    store = Path(args.specs)
    gaps = _load_gaps(store, args.id)
    gaps.append(args.gap)
    _save_gaps(store, args.id, gaps)
    _dump({"ok": True, "id": args.id, "open_gaps": gaps}, args.out)
    return 0


def cmd_status(args) -> int:
    store = Path(args.specs)
    draft = _load(store, args.id)
    _dump(
        {
            "ok": True,
            "id": draft.id,
            "idea": draft.idea,
            "status": draft.status,
            "rounds": [_round_view(r) | {"answer": r.answer, "adopted": r.adopted} for r in draft.rounds],
            "features": draft.features,
            "non_goals": draft.non_goals,
            "acceptance": draft.acceptance,
        },
        args.out,
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="M1 追问循环单轮驱动")
    parser.add_argument("--specs", default=str(REPO_ROOT / "state" / "specs"))
    parser.add_argument("--out", default=None, help="JSON 落点；缺省 stdout")
    parser.add_argument("--generate-tier", default="flash_api")
    parser.add_argument("--extract-tier", default="flash_api")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("init")
    p.add_argument("--idea", required=True)
    p.set_defaults(func=cmd_init)

    for name, func in (("ask", cmd_ask), ("judge", cmd_judge), ("finalize", cmd_finalize), ("status", cmd_status)):
        p = sub.add_parser(name)
        p.add_argument("--id", required=True)
        if name == "ask":
            p.add_argument(
                "--note-gap",
                action="append",
                default=[],
                help="额外指定一条缺口，逼下一问问它（可重复）",
            )
        p.set_defaults(func=func)

    p = sub.add_parser("note-gap")
    p.add_argument("--id", required=True)
    p.add_argument("--gap", required=True)
    p.set_defaults(func=cmd_note_gap)

    p = sub.add_parser("answer")
    p.add_argument("--id", required=True)
    p.add_argument("--text", required=True)
    p.set_defaults(func=cmd_answer)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except Exception as exc:  # noqa: BLE001
        _dump({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, args.out)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
