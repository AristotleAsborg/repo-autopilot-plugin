"""关键词分类桩（路线 3.1 的**占位实现**，不是分类器本体）。

真正的分类器在 `classifier.py`：本地小模型 + schema + 防注入 + 动作分级。
这个文件保留下来只有一个用途：**e2e 骨架**需要一条快且确定的分类路径
（`tests/e2e/e2e_full_run.py` 用它），而真分类器要调模型、不适合放在冒烟链路里。

它的自我约束与 2.x 时一致：确定性、不假装聪明、返回 `stub=True`。
"""

from __future__ import annotations

from dataclasses import dataclass

LABELS = ("bug", "feature", "question")

_BUG_MARKERS = (
    "报错", "失败", "崩溃", "异常", "挂掉", "traceback", "error", "exception",
    "crash", "fail", "500", "无法运行", "跑不起来",
)
_FEATURE_MARKERS = (
    "希望", "建议", "新增", "支持", "缺少", "没有", "文档", "readme", "install",
    "安装", "feature", "add", "request", "能不能",
)


@dataclass(frozen=True)
class Classification:
    label: str
    why: str
    stub: bool = True


def classify(title: str, body: str = "") -> Classification:
    """
    按关键词给出一个标签。

    顺序刻意是"先 bug 后 feature"：一个 issue 同时提到"报错"和"建议"时，
    报错更紧急，也更可能被误判成 feature 而漏掉。宁可误报 bug（多看一眼），
    不可漏报 bug。
    """
    text = f"{title}\n{body}"
    lowered = text.lower()

    for marker in _BUG_MARKERS:
        if marker in text or marker in lowered:
            return Classification("bug", f"命中 bug 关键词 {marker!r}")

    for marker in _FEATURE_MARKERS:
        if marker in text or marker in lowered:
            return Classification("feature", f"命中 feature 关键词 {marker!r}")

    return Classification("question", "没有命中任何关键词，按提问处理")


__all__ = ["LABELS", "Classification", "classify"]
