#!/usr/bin/env bash
# 路线 1.6 子步骤 2 要求的入口：一条命令跑完全链路。
#
# 真正的链路实现在同目录的 e2e_full_run.py，这里只是一层薄壳。为什么不是纯 bash：
# 本项目的验收必须在**本机**（Windows）跑通，而 Windows 上没有 bash。
# 所以把链路写成 Python，CI（Linux）用这个 .sh，本机用 state/run-e2e.cmd 的同一入口。
#
# 用法：
#   tests/e2e/e2e_full_run.sh
#   tests/e2e/e2e_full_run.sh --publish
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if command -v python3 >/dev/null 2>&1; then
  exec python3 "${here}/e2e_full_run.py" "$@"
fi
exec python "${here}/e2e_full_run.py" "$@"
