# 步骤状态表

> **本文件的状态列只能由验收脚本写入。**（路线 0.2 的配套硬机制：提示词管行为，代码管状态，双保险。）
> AI 手工编辑本文件不改变状态。状态只有 `PASS` / `BLOCKED` / `DOING` / `TODO` 四种，
> 没有"基本完成"这类中间态。

| 步骤 | 名称 | 状态 | 时间 | 证据 |
|---|---|---|---|---|
| 0.1 | harness 能力探测 | PASS | 2026-09-12 22:05 | state/reports/acceptance/20260912-220527/0.1-capabilities-a0.log |
| 0.2 | 本地小模型安装 + 基线测试 | PASS | 2026-09-12 19:51 | state/reports/acceptance/20260912-195151/0.2-embedding-a0.log |
| 0.3 | 凭证准备（读写 token 分离） | PASS | 2026-09-12 19:51 | state/reports/acceptance/20260912-195151/0.3-isolation-a0.log |
| 1.1 | 仓库与 state 目录 | PASS | 2026-09-12 19:51 | state/reports/acceptance/20260912-195151/1.1-skeleton-a0.log |
| 1.2 | 任务队列 | PASS | 2026-09-12 19:51 | state/reports/acceptance/20260912-195151/1.2-queue-a0.log |
| 1.3 | LLM 网关 | PASS | 2026-09-12 19:51 | state/reports/acceptance/20260912-195151/1.3-gateway-a0.log |
| 1.4 | GitHub 封装 + 人类闸门 | PASS | 2026-09-12 20:31 | state/reports/acceptance/20260912-203135/1.4-gate-a0.log |
| 1.5 | 沙箱 | PASS | 2026-09-12 20:31 | state/reports/acceptance/20260912-203145/1.5-sandbox-a0.log |
| 1.6 | 测试环境三件套 | PASS | 2026-09-12 19:51 | state/reports/acceptance/20260912-195151/1.6-env-a0.log |
| 2.1 | 追问循环 | PASS | 2026-09-17 13:19 | state/reports/acceptance/20260917-131941/2.1-refiner-a0.log |
| 2.2 | 停止判断 | PASS | 2026-09-17 13:19 | state/reports/acceptance/20260917-131954/2.2-stopper-a0.log |
| 3.1 | 分类打标 | PASS | 2026-09-15 12:32 | state/reports/acceptance/20260915-123248/3.1-triage-a0.log |
| 3.2 | 查重路由 | PASS | 2026-09-12 19:54 | state/reports/acceptance/20260912-195423/3.2-dedupe-a0.log |
| 3.3 | 标签打回通路 | PASS | 2026-09-12 19:54 | state/reports/acceptance/20260912-195423/3.3-bounce-a0.log |
| 4.1 | 文件定位 | PASS | 2026-09-12 19:54 | state/reports/acceptance/20260912-195423/4.1-localize-a0.log |
| 4.2 | 修复循环 | PASS | 2026-09-12 19:54 | state/reports/acceptance/20260912-195423/4.2-repair-a0.log |
| 4.3 | 测试门禁 | PASS | 2026-09-12 19:54 | state/reports/acceptance/20260912-195423/4.3-gate-a0.log |
| 4.4 | 报告 + 推送 | PASS | 2026-09-12 20:03 | state/reports/acceptance/20260912-200331/4.4-publish-a0.log |
| 5.1 | 开源项目猎手 | PASS | 2026-09-12 20:03 | state/reports/acceptance/20260912-200339/5.1-scout-a0.log |
| 6.1 | 半自动建新仓库 | PASS | 2026-09-12 20:03 | state/reports/acceptance/20260912-200339/6.1-scaffold-a0.log |
| 7.1 | skills/ + 8 条对话指令 | PASS | 2026-09-12 20:08 | state/reports/acceptance/20260912-200853/7.1-skills-a0.log |
| 8.1 | CI 门禁 + 脏测试工具 | PASS | 2026-09-17 12:25 | state/reports/acceptance/20260917-122512/8.1-stage8-a0.log |
| 8.2 | 7 天实战演练 | TODO | — | — |

## 状态列的写入者

**本表的「状态 / 时间 / 证据」三列只由 `tools/acceptance.py` 写入**（路线 0.2 的硬机制：
提示词管行为，代码管状态）。手工编辑本文件不改变状态。

- 运行方式：`state/run-acceptance.cmd --all`（必须在 DSH 工具管道之外跑）
- 每次运行的原始输出：`state/reports/acceptance/<时间戳>/`
- 判定规则：只认可执行检查（pytest / 脚本 / 文件断言）；失败重试 ≤2 次；
  仍失败即 `BLOCKED` 并**立即停止**，不再评估后续步骤
- 没有登记检查的步骤只能取 `TODO`（或保留此前的 `PASS`），绝不凭空给 PASS

## 环境备忘（避免每轮重新探测）

- 可用 Python venv：`D:\PythonEnv\venv\Scripts\python.exe`（`python` 命令指向的是 uv 托管解释器，PEP 668 保护，装不了包）
- 依赖已齐：pydantic 2.13.5 / httpx 0.28.1 / tenacity 9.1.4 / numpy 2.5.3 / pytest 9.1.1 / ruff 0.16.7 / mypy 2.3.1 / PyYAML 6.0.3
- **pip 装包需一次性提权**（`danger-full-access`）：沙箱会拒读 pip 自己解包的 `.whl`（Errno 13）。已在 0.1 记录。
- 无 Docker → 沙箱走 `venv + setrlimit`，`sandbox_strength: weak`
- 无 `gh` CLI → GitHub 操作走 REST（httpx）
- 本地模型端点：`http://127.0.0.1:11434/v1`（Ollama，形态一）。**调本机模型必须走
  `src/gateway/local_client.py`（urllib）**：httpx 对本机明文 HTTP 一律 502/0 字节。
- 读 token 解析顺序（`tools/acceptance.py::load_read_token`）：环境变量 `GH_READ_TOKEN`
  → `state/GH_READ_TOKEN.txt` → `$DSH_HOME/.read_token`。已写入用户级环境变量，
  但要等 DSH 重启才对进程可见；在那之前一律走文件，不必打断工作。
- 长命令执行方式：一律 `state/run-*.cmd` + `Start-Process`，并让调用方 `WaitForExit`
  活到子进程结束。**禁止** `job_output{wait:true}` 与 `run_in_background` 跑长命令
  —— 这两条会把 DSH 服务端进程整个拖死，见 `dsh-disconnect-investigation.md`。
- `.cmd` 文件只用 ASCII：cmd 以 OEM 代码页读取，非 ASCII 注释放进括号块会被撕碎
  （实测踩过一次）。中文注释请写在 `.md` 或 Python 里。
- 并发互斥**不要用 `os.rename`**：见 `finding-rename-mutex-broken.md`，
  用 `os.open(..., O_CREAT|O_EXCL)` 或 `os.mkdir()`。
- **`Start-Process` 撒手不管的后台子进程会被一并回收**（实测 pid 消失、日志 0 字节）。
  长命令必须在同一次 pwsh 调用内跑完，或走 `state/run-*.cmd`。
- 回放/评测结果文件名一律带**档位与批次**（`-<tier>[-<tag>]`）。踩过一次：
  flash 的结果把本地档的同名文件覆盖了，而那正是对比里最要紧的一半数据。
- 3.1 验收的样本口径：见 `state/findings/triage-metric-underpowered.md`
  —— 50 条样本下去分辨 75% 分数线等于掷硬币，改为**冻结回放集**上评估；
  调参只用 dev，报数只报 holdout。
- flash API key：`D:\dsh\home\.deepseek_key`（一行，35 字符）。2026-09-12 已轮换：
  新 key 实测 `/models` 200 + 一次结构化调用 200（`tools/probe_flash.py`），
  旧 key 实测 HTTP 401（已失效）。**key 不进仓库、不进对话**——贴进对话等于进会话日志。
- `ruff check .` 全仓目前 52 条告警（多数在老文件里，15 条可自动修）。这是 8.4
  「门禁自动化」要还的债，不属于任何单步的验收线，别拿它去卡 3.x。

- 写 token 的能力（2026-09-12 实测）：能建仓（`POST /user/repos` → 201，响应头
  `x-accepted-github-permissions: administration=write`）、能推分支/提交/建 PR、能删仓库。
  **踩过的坑**：6.1 首次建仓报 403 "Resource not accessible"，看着像权限不够，
  实际是代码里把**读 token 的客户端**传给了写操作 —— 见
  `state/findings/write-client-wiring.md`。报错说"权限不够"时，先确认用的是不是有权限的那个凭证。
- 真实成功的端到端产物：陪练仓库 PR `AristotleAsborg/sandbox-clean#1`（4.4）、
  新建仓库 `AristotleAsborg/markdown-csv`（6.1，云端 CI 绿），锚点报告见
  `state/reports/first_e2e_success.md`。
