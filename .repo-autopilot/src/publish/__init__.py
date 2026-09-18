"""4.4 报告与确认推送（`src/publish/`）。

    from src.publish import ReportInput, publish, rejection_feedback

- **报告**：五段式（根因 / 改动摘要 / 测试结果 / 风险点 / 回滚方法），写给要说"是"的那个人；
- **两次闸门**：`push`（建分支+提交）与 `create_pull_request` 各要一次人类点头 ——
  合成一次等于把"公开"藏进了"推送"里；
- **推送走 REST contents API**：本机 `git push` 到本地路径被策略挡，https 推送要凭据落地；
  REST 的效果一样（新分支 + 提交 + PR），但 token 只在一次 HTTP 调用里出现；
- **拒绝回流**：`rejection_feedback()` 把人类的"否 + 原因"变成下一轮修复的输入。
"""

from .core import (
    BRANCH_TEMPLATE,
    COMMIT_MESSAGE,
    REPORT_DIR,
    FileChange,
    PublishError,
    PublishResult,
    ReportInput,
    adapt_newlines,
    branch_name,
    check_publishable,
    create_pull_request,
    default_base_sha,
    fetch_remote_text,
    normalize_newlines,
    now_stamp,
    publish,
    push_changes,
    rejection_feedback,
    render_report,
    verify_bases,
    write_report,
)

__all__ = [
    "BRANCH_TEMPLATE",
    "COMMIT_MESSAGE",
    "REPORT_DIR",
    "FileChange",
    "PublishError",
    "PublishResult",
    "ReportInput",
    "adapt_newlines",
    "branch_name",
    "check_publishable",
    "create_pull_request",
    "default_base_sha",
    "fetch_remote_text",
    "normalize_newlines",
    "now_stamp",
    "publish",
    "push_changes",
    "rejection_feedback",
    "render_report",
    "verify_bases",
    "write_report",
]
