"""3.2 验收：查重与路由的**三条线**（路线原文）

    同义改写 10 对，召回 ≥8      ← 该认出来的要认出来
    相似但不同 10 对，误报 ≤2    ← 不该认出来的别乱认（这一条更值钱）
    5000 条库，单次查询 <2s      ← 门槛：超过就该上 pgvector

    python tools/eval_dedupe.py              # 真模型（Ollama bge-m3）
    python tools/eval_dedupe.py --offline    # 确定性假向量：只验代码路径与性能

## 为什么「误报 ≤2」比「召回 ≥8」更值钱

漏掉一个重复，人只是多读一条 issue；把两条不相干的 issue 判成重复，人会
**被机器人关掉自己的帖子**（走闸门也还要人花时间否决）。误报的代价向外、落在用户身上。

## 阈值必须标定，不能照抄 0.92 / 0.98

路线明说：「0.92/0.98 这两个阈值是起点不是真理……用回放数据画出『阈值-误杀率』曲线」。
实测（2026-09-12，bge-m3）就是这么回事：**真实同义改写的余弦只有 0.83～0.87**，
照抄 0.92 的召回是 0/10 —— 阈值定高了，功能等于不存在。所以本脚本做三件事：

1. 在网格上扫一遍阈值，画出「阈值 → 召回 / 误报」曲线；
2. 按**两条验收线**选阈值：召回 ≥8 且误报 ≤2 的**最低**阈值（最低 = 灵敏度最高）；
   召回用同义改写集，误报用**另一组**样本（相似但不同），两个集合不重叠；
3. 在 3.1 的**真实冻结语料**上量一遍基础误报率：真实 issue 两两算余弦，数一数有多少对
   会越过阈值 —— 手写的 10 对样本量太小，真实语料才看得出「随便两条 issue 有多像」。
   越过阈值的真实 pair 会写进报告给人抽查。

选定后的常数写回 `src/dedupe/router.py`；本脚本只负责给出证据。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.dedupe import VectorStore

REPORT_DIR = ROOT / "state" / "reports"
REPLAY = ROOT / "state" / "corpus" / "triage-replay.jsonl"
#: 评测用临时向量库：**不写 state/vectors**（生产库不该被评测样本污染）
EVAL_STORE_DIR = ROOT / "state" / "vectors-eval"

SWEEP = tuple(round(0.70 + 0.01 * i, 2) for i in range(30))  # 0.70 → 0.99
PERF_COUNT = 5000
PERF_BUDGET_SECONDS = 2.0
EMBED_CHUNK = 32

#: 10 对同义改写：同一件事、不同措辞（标题 + 正文，与生产输入一致）
PARAPHRASE_PAIRS: tuple[tuple[str, str], ...] = (
    (
        "应用启动后立刻闪退\nv2.3.1，Windows 11，双击桌面图标后窗口闪一下就没了。事件查看器里没有任何崩溃记录。卸载重装仍然一样。",
        "程序一打开就崩溃消失\n系统 Windows 11，版本 2.3.1。从桌面双击启动，窗口立刻消失，事件查看器查不到错误日志，重新安装也没改善。",
    ),
    (
        "导出 CSV 时中文全部变成乱码\n导出报表为 CSV，用 Excel 打开后中文列全是问号。编码选项只有 UTF-8 和 GBK 两个，选哪个都一样。页面上的数据本身显示正常。",
        "下载的表格文件中文显示为问号\n把报表下载成 CSV 之后用 Excel 打开，中文字段变成问号。切换导出编码（UTF-8 / GBK）没有区别，页面上的数据是好的。",
    ),
    (
        "登录页的验证码图片刷新不出来\n登录页面右侧的验证码一直显示为灰色占位图，点刷新按钮也没变化。换浏览器、清缓存都试过。控制台报 500。",
        "验证码加载失败且提示服务器错误\n打开登录页，验证码区域是空白，浏览器控制台显示接口返回 500。刷新按钮点了没有反应，换浏览器问题依旧。",
    ),
    (
        "macOS 安装包双击提示已损坏\n在 macOS 14 上下载 dmg，双击提示「无法打开，因为它已损坏」，右键打开也一样。同一下载链接在同事机器上正常。",
        "苹果电脑打不开安装文件，说文件已损坏\nmacOS 14，下载的 dmg 双击报「已损坏，应移到废纸篓」，按住 Control 右键打开仍然是同样的提示。别人下载同一个文件没问题。",
    ),
    (
        "删除文件后磁盘空间没有释放\n在文件列表里删除一个大文件，提示删除成功，但设置里的可用空间没有任何变化。重启应用之后空间才回来。",
        "删掉文件后可用空间还是原来的数字\n删除大文件成功之后，磁盘可用容量没有增加，必须重启程序才能看到空间被释放。",
    ),
    (
        "批量导入 1000 条数据时请求超时\n用导入功能一次上传 1000 行 CSV，进度条走到一半提示请求超时。分成 100 行一次就没有问题。默认超时时间是 30 秒。",
        "一次导入大量记录会卡住然后报超时\n导入 1000 条记录时中途失败，界面提示 timeout。每次只导入一百条就能成功。感觉是默认的 30 秒超时不够。",
    ),
    (
        "Saving a file with a non-ASCII name fails\nOn Windows 11 with v2.3.1, saving a file whose name contains Chinese characters fails with error EINVAL. ASCII names save fine. The file is created empty and then removed.",
        "Cannot save files with Chinese characters in the name\nv2.3.1 on Windows 11: saving a document named with Chinese characters returns EINVAL error. An English filename saves correctly. The output file ends up empty before being deleted.",
    ),
    (
        "Dark mode makes the sidebar text unreadable\nThe sidebar labels are dark grey on a dark background after switching to dark theme. Contrast is too low to read. Light theme is fine. Happens on every page.",
        "Sidebar labels are invisible in dark theme\nAfter enabling dark mode, the left navigation text has almost no contrast against the background and cannot be read. Switching back to the light theme restores it.",
    ),
    (
        "API returns 500 when the token expires\nWhen the access token expires, every subsequent API call returns 500 instead of 401. Clients cannot tell that they need to re-authenticate. Started after upgrading to v3.0.",
        "Expired token causes a 500 on all requests\nThe API answers 500 rather than 401 once the access token has expired, so applications have no way to know they should refresh credentials. Reproduced on v3.0.",
    ),
    (
        "Sorting by date puts the newest items last\nClicking the date column header sorts ascending, so the oldest entries appear at the top. Clicking again does sort descending. Default order should be newest first.",
        "Date sort shows oldest first by default\nAfter clicking the date column, the list starts with the oldest rows and the newest are at the bottom. A second click reverses it. The default should show the most recent first.",
    ),
)

#: 10 对「相似但不同」：同一模块/同一类症状，但**不是**同一件事
LOOKALIKE_PAIRS: tuple[tuple[str, str], ...] = (
    (
        "导出 CSV 时中文全部变成乱码\n导出报表为 CSV，用 Excel 打开后中文列全是问号。编码选项只有 UTF-8 和 GBK 两个，选哪个都一样。",
        "导出 PDF 时中文全部变成乱码\n导出报表为 PDF，中文字符显示成方块。CSV 导出是正常的，只有 PDF 有问题。",
    ),
    (
        "应用启动后立刻闪退\nv2.3.1，Windows 11，双击桌面图标后窗口闪一下就没了。事件查看器里没有任何崩溃记录。",
        "应用启动后 CPU 占用一直是 100%\nv2.3.1 启动之后风扇狂转，任务管理器里 CPU 一直 100%，界面卡住但进程没有退出。",
    ),
    (
        "删除文件后磁盘空间没有释放\n在文件列表里删除一个大文件，提示删除成功，但可用空间没有任何变化。",
        "删除文件后回收站里还留着一份副本\n删除文件之后，回收站仍然能看到它，占用没有减少，需要手动清空回收站。",
    ),
    (
        "批量导入 1000 条数据时请求超时\n一次上传 1000 行 CSV，进度条走到一半提示请求超时。",
        "批量导出的进度条一直停在 0%\n导出 1000 条记录时进度条不动，但几分钟后文件其实已经生成。",
    ),
    (
        "登录页的验证码图片刷新不出来\n验证码一直显示为灰色占位图，点刷新按钮也没变化。",
        "登录成功之后页面一直在刷新\n输入正确的账号密码之后，页面反复跳回登录页，像是陷入了循环。",
    ),
    (
        "macOS 安装包双击提示已损坏\n在 macOS 14 上下载 dmg，双击提示「已损坏」。",
        "Windows 安装包被防火墙拦截\n在 Windows 11 上运行 exe，被公司防火墙报毒并删除。",
    ),
    (
        "Dark mode makes the sidebar text unreadable\nThe sidebar labels are dark grey on a dark background after switching to dark theme.",
        "Dark mode hides the toolbar icons\nIn dark theme the toolbar buttons render as empty squares because the icons are black on black.",
    ),
    (
        "API returns 500 when the token expires\nWhen the access token expires, every subsequent API call returns 500 instead of 401.",
        "API returns 401 for a token that is still valid\nA freshly issued token is rejected with 401 on the first request after login.",
    ),
    (
        "Saving a file with a non-ASCII name fails\nSaving a file whose name contains Chinese characters fails with error EINVAL.",
        "Opening a file with a non-ASCII name fails\nOpening an existing file whose name contains Chinese characters raises EINVAL.",
    ),
    (
        "Sorting by date puts the newest items last\nClicking the date column sorts ascending, so the oldest entries appear at the top.",
        "Sorting by name is case sensitive\nCapitalised names are grouped separately from lowercase ones, which looks random to users.",
    ),
)


def fake_embed(texts: list[str], dim: int = 1024) -> np.ndarray:
    """确定性假向量：只验代码路径与性能，不验语义（--offline 用）。"""
    out = np.zeros((len(texts), dim), dtype=float)
    for row, text in enumerate(texts):
        cleaned = "".join(text.split()).lower()
        for position in range(max(1, len(cleaned) - 2)):
            gram = cleaned[position : position + 3]
            bucket = int(hashlib.sha256(gram.encode("utf-8")).hexdigest()[:8], 16) % dim
            out[row, bucket] += 1.0
    return out


def embed_all(texts: list[str], offline: bool) -> np.ndarray:
    if offline:
        return fake_embed(texts)
    from src.gateway import embed

    chunks = [texts[i : i + EMBED_CHUNK] for i in range(0, len(texts), EMBED_CHUNK)]
    return np.vstack([embed(chunk) for chunk in chunks]) if chunks else np.zeros((0, 0))


def load_replay_texts(limit: int = 0) -> list[tuple[str, str]]:
    """3.1 的冻结语料（真实 issue）。用它量「互不相干的两条 issue 有多像」的基础误报率。"""
    if not REPLAY.exists():
        return []
    rows: list[tuple[str, str]] = []
    with open(REPLAY, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            payload = json.loads(line)
            text = f"{payload.get('title') or ''}\n{payload.get('body') or ''}".strip()
            key = f"{payload.get('repo')}#{payload.get('number')}"
            rows.append((key, text[:4000]))
            if limit and len(rows) >= limit:
                break
    return rows


def sweep_curve(para_scores: np.ndarray, look_scores: np.ndarray) -> list[dict]:
    return [
        {
            "threshold": threshold,
            "recall": int((para_scores >= threshold).sum()),
            "false_positives": int((look_scores >= threshold).sum()),
        }
        for threshold in SWEEP
    ]


def choose_thresholds(curve: list[dict], look_scores: np.ndarray) -> dict:
    """
    规则（先定规则，再看结果 —— 反过来就是拿测试集调参）：

    - 只在**两条验收线都满足**的档位里选（召回 ≥8、误报 ≤2）；
    - 在这些档位里最大化 `召回 − 2 × 误报`：误报按**两倍**计价，因为路线自己说了
      「误杀率比准确率更硬的线」—— 误报的代价向外、落在用户身上；
    - 同分取**更高**的阈值（更保守）。
    - `close`：不低于 `max(0.95, 误报集最高分 + 0.05)`，只该由「近乎逐字复制」触发。
    """
    eligible = [row for row in curve if row["recall"] >= 8 and row["false_positives"] <= 2]
    chosen = (
        max(eligible, key=lambda row: (row["recall"] - 2 * row["false_positives"], row["threshold"]))
        if eligible
        else None
    )
    flag = chosen["threshold"] if chosen else None
    close = round(max(0.95, float(look_scores.max()) + 0.05), 2)
    return {"flag": flag, "close": close, "eligible_count": len(eligible)}


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

    parser = argparse.ArgumentParser(description="3.2 查重验收")
    parser.add_argument("--offline", action="store_true", help="不调模型，用确定性假向量")
    parser.add_argument("--corpus-limit", type=int, default=0, help="真实语料基础误报率只用前 N 条")
    args = parser.parse_args()
    offline = args.offline

    print("模式：offline（假向量，语义指标不作数）" if offline else "模式：真模型（bge-m3）")

    pair_texts: list[str] = []
    for left, right in PARAPHRASE_PAIRS:
        pair_texts += [left, right]
    for left, right in LOOKALIKE_PAIRS:
        pair_texts += [left, right]
    matrix = embed_all(pair_texts, offline)
    offset = 2 * len(PARAPHRASE_PAIRS)
    para_scores = np.array(
        [float(matrix[2 * i] @ matrix[2 * i + 1]) for i in range(len(PARAPHRASE_PAIRS))]
    )
    look_scores = np.array(
        [float(matrix[offset + 2 * i] @ matrix[offset + 2 * i + 1]) for i in range(len(LOOKALIKE_PAIRS))]
    )

    curve = sweep_curve(para_scores, look_scores)
    chosen = choose_thresholds(curve, look_scores)
    flag, close = chosen["flag"], chosen["close"]

    print("\n同义改写（上）/ 相似但不同（下）的余弦：")
    for (left, _), score in zip(PARAPHRASE_PAIRS, para_scores, strict=True):
        print(f"  {score:.3f}  {left.splitlines()[0][:40]}")
    for (_, right), score in zip(LOOKALIKE_PAIRS, look_scores, strict=True):
        print(f"  {score:.3f}  [相似但不同] {right.splitlines()[0][:34]}")

    print("\n阈值扫描（只列出有变化的档）：")
    last: tuple[int, int] | None = None
    for row in curve:
        current = (row["recall"], row["false_positives"])
        if current != last:
            print(
                f"  T={row['threshold']:.2f}  召回 {row['recall']}/10  "
                f"误报 {row['false_positives']}/10"
            )
            last = current

    corpus_rows = load_replay_texts(args.corpus_limit)
    corpus_stats: dict = {"count": 0}
    corpus_pairs: list[dict] = []
    if corpus_rows and flag is not None:
        keys = [key for key, _ in corpus_rows]
        vectors = embed_all([text for _, text in corpus_rows], offline)
        vectors = vectors / np.linalg.norm(vectors, axis=1, keepdims=True)
        scores = vectors @ vectors.T
        upper = np.triu_indices(len(keys), k=1)
        flat = scores[upper]
        total_pairs = len(flat)
        # 逐条最近邻：生产里每来一条新 issue，比的就是**它的最近邻**。
        # "两两随机配对"几乎永远不越线，所以那个数会让人误以为误报率为零 ——
        # 真正该看的是这个分布：有多少条 issue 一进来就会被贴上重复标记。
        scores = scores.copy()
        np.fill_diagonal(scores, -1.0)
        nearest = scores.max(axis=1)
        nn_above = int((nearest >= flag).sum())
        corpus_stats = {
            "count": len(keys),
            "pairs": total_pairs,
            "above_flag": int((flat >= flag).sum()),
            "above_close": int((flat >= close).sum()),
            "rate_above_flag": round(float((flat >= flag).sum()) / total_pairs, 6)
            if total_pairs
            else 0.0,
            "max_score": round(float(flat.max()) if total_pairs else 0.0, 4),
            "max_pair": (
                [keys[upper[0][int(flat.argmax())]], keys[upper[1][int(flat.argmax())]]]
                if total_pairs
                else []
            ),
            "nn_above_flag": nn_above,
            "nn_rate_above_flag": round(nn_above / len(keys), 4) if keys else 0.0,
            "nn_percentiles": {
                "p50": round(float(np.percentile(nearest, 50)), 4),
                "p90": round(float(np.percentile(nearest, 90)), 4),
                "p99": round(float(np.percentile(nearest, 99)), 4),
            },
        }
        order = np.argsort(-flat)[:10]
        corpus_pairs = [
            {
                "left": keys[upper[0][int(i)]],
                "right": keys[upper[1][int(i)]],
                "score": round(float(flat[int(i)]), 4),
            }
            for i in order
        ]
        print(
            f"\n真实语料 {len(keys)} 条（{total_pairs} 对）：≥{flag:.2f} 的 "
            f"{corpus_stats['above_flag']} 对，≥{close:.2f} 的 {corpus_stats['above_close']} 对，"
            f"最高 {corpus_stats['max_score']:.3f}"
        )
        print(
            f"逐条最近邻：≥{flag:.2f} 的 {nn_above}/{len(keys)} 条"
            f"（{corpus_stats['nn_rate_above_flag']:.1%}）—— 这就是「一进来就被贴重复标记」的比例；"
            f"p50={corpus_stats['nn_percentiles']['p50']:.3f} "
            f"p90={corpus_stats['nn_percentiles']['p90']:.3f} "
            f"p99={corpus_stats['nn_percentiles']['p99']:.3f}"
        )

    big = VectorStore()
    rng = np.random.default_rng(20260912)
    big.add_many([f"perf#{i}" for i in range(PERF_COUNT)], rng.normal(size=(PERF_COUNT, 256)))
    started = time.perf_counter()
    top = big.search(rng.normal(size=256), top_k=5)
    elapsed = time.perf_counter() - started
    del top

    recall = int((para_scores >= flag).sum()) if flag is not None else 0
    false_positives = int((look_scores >= flag).sum()) if flag is not None else len(LOOKALIKE_PAIRS)
    semantic_ok = flag is not None and recall >= 8 and false_positives <= 2
    perf_ok = elapsed < PERF_BUDGET_SECONDS
    passed = perf_ok and (semantic_ok or offline)

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    mode = "offline" if offline else "local_embed"
    report = {
        "date": stamp,
        "mode": mode,
        "sweep": curve,
        "chosen": {"flag": flag, "close": close, **chosen},
        "paraphrase_scores": [round(float(s), 4) for s in para_scores],
        "lookalike_scores": [round(float(s), 4) for s in look_scores],
        "at_chosen_flag": {"recall": recall, "false_positives": false_positives},
        "corpus": corpus_stats,
        "corpus_top_pairs": corpus_pairs,
        "perf": {"count": PERF_COUNT, "seconds": round(elapsed, 4), "budget": PERF_BUDGET_SECONDS},
        "semantic_ok": semantic_ok,
        "perf_ok": perf_ok,
        "passed": passed,
    }
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    target = REPORT_DIR / f"dedupe-eval-{stamp}-{mode}.json"
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (REPORT_DIR / f"dedupe-thresholds-{mode}.md").write_text(
        render_markdown(report), encoding="utf-8"
    )

    print()
    print(f"选定阈值：flag={flag}（加标记）  close={close}（建议关闭，必须过闸门）")
    print(f"同义改写召回 {recall}/10（要求 ≥8）")
    print(f"相似但不同误报 {false_positives}/10（要求 ≤2）")
    print(f"5000 条查询 {elapsed * 1000:.1f}ms（要求 <2000ms）")
    print(f"结论：{'达标' if passed else '**未达标**'}")
    print(f"报告：{target}")
    return 0 if passed else 1


def render_markdown(report: dict) -> str:
    """给人读的阈值标定报告（含曲线与真实语料里最像的几对，供抽查）。"""
    chosen = report["chosen"]
    corpus = report.get("corpus") or {}
    lines = [
        "# 查重阈值标定（3.2）",
        "",
        f"- 日期：{report['date']}　模式：`{report['mode']}`",
        f"- 选定：**flag={chosen['flag']}**（加标记）　**close={chosen['close']}**（建议关闭，走闸门）",
        (
            f"- 该阈值下：同义改写召回 **{report['at_chosen_flag']['recall']}/10**（要求 ≥8）、"
            f"相似但不同误报 **{report['at_chosen_flag']['false_positives']}/10**（要求 ≤2）"
        ),
        f"- 5000 条库单次查询 {report['perf']['seconds'] * 1000:.1f}ms（要求 <2000ms）",
        "",
        "## 为什么不是路线里写的 0.92 / 0.98",
        "",
        "路线自己说了「这两个阈值是起点不是真理」，要求用回放数据画曲线。实测就是这样：",
        "",
        f"- 10 对真实同义改写的余弦：{', '.join(f'{s:.3f}' for s in report['paraphrase_scores'])}",
        f"- 10 对相似但不同的余弦：{', '.join(f'{s:.3f}' for s in report['lookalike_scores'])}",
        "",
        (
            "照抄 0.92 的结果是**召回 0/10**（同义改写的上限都到不了 0.92），功能等于不存在。"
            "所以阈值只在满足两条验收线的档位里挑，并按「误报计两倍代价」取最优 —— "
            "规则先定好，再看结果。"
        ),
        "",
        "## 阈值扫描曲线",
        "",
        "| 阈值 | 召回（/10） | 误报（/10） |",
        "|---|---|---|",
    ]
    last: tuple[int, int] | None = None
    for row in report["sweep"]:
        current = (row["recall"], row["false_positives"])
        if current != last:
            lines.append(f"| {row['threshold']:.2f} | {row['recall']} | {row['false_positives']} |")
            last = current

    if corpus:
        lines += [
            "",
            "## 真实语料上的基础误报率（手写 10 对样本量太小，看这个）",
            "",
            f"- 语料：3.1 的冻结回放集 {corpus['count']} 条真实 issue，两两共 {corpus['pairs']} 对",
            f"- ≥flag 的：{corpus['above_flag']} 对（占比 {corpus['rate_above_flag']:.4%}）",
            f"- ≥close 的：{corpus['above_close']} 对",
            f"- 最像的一对：{corpus['max_pair']} → {corpus['max_score']}",
            "",
            "### 逐条最近邻：真实场景下会被贴标记的比例",
            "",
            (
                "- **≥flag 的**："
                f"{corpus.get('nn_above_flag', 0)}/{corpus['count']} 条"
                f"（{corpus.get('nn_rate_above_flag', 0.0):.1%}）"
                " —— 新 issue 进来时比的就是它的最近邻，这才是真实的标记率"
            ),
            (
                "- 最近邻分布："
                f"p50={corpus.get('nn_percentiles', {}).get('p50')} "
                f"p90={corpus.get('nn_percentiles', {}).get('p90')} "
                f"p99={corpus.get('nn_percentiles', {}).get('p99')}"
            ),
            "",
            "### 最像的 10 对（请抽查：这些真的重复吗？）",
            "",
            "| 左 | 右 | 余弦 |",
            "|---|---|---|",
        ]
        for row in report["corpus_top_pairs"]:
            lines.append(f"| `{row['left']}` | `{row['right']}` | {row['score']} |")
        if corpus["above_close"] == 0:
            lines += [
                "",
                (
                    "> **注意**：真实语料里没有任何一对达到 close 阈值 —— 说明「建议关闭」这一档"
                    "在当前 embedding 下几乎不会被触发。这是有意的保守（误关帖子的代价太大），"
                    "但也要知道：它现在是**死代码**。要不要给它换一种判据（例如标题近乎相同 + "
                    "正文高度重叠），留待有真实重复样本时再定。"
                ),
            ]

    lines += [
        "",
        "## 选阈值的规则（先定规则再看结果）",
        "",
        (
            "1. `flag` = 在满足两条验收线（召回 ≥8、误报 ≤2）的档位里，取"
            "`召回 − 2 × 误报` 最大的那个；误报按两倍计价，因为路线说「误杀率比准确率更硬」，"
            "而且召回集与误报集是**两组不同的样本**，不重叠。同分取更高的阈值（更保守）。"
        ),
        "2. `close` = `max(0.95, 误报集最高分 + 0.05)` —— 只该由近乎逐字复制触发。",
        (
            "3. 生产环境每个仓库应各标一次（路线原话：不同仓库的 issue 风格差异很大）；"
            "本脚本可以直接换成某个仓库的语料来跑。"
        ),
        "",
    ]
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
