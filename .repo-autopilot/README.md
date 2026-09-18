# repo-autopilot

半自动的开源仓库维护系统：**读 issue → 分类/查重 → 定位 → 修复 → 过门禁 → 出报告 → 人类点头才推 PR**，
外加"找轮子"（开源猎手）与"建新项目"（脚手架）两条支线。所有对外写操作都过人类闸门。

- 本系统的 GitHub 仓库：`AristotleAsborg/repo-autopilot`（**私有**）
  —— 建库 + 首推走的是同一条闸门（`tools/self_repo.py`），首推之后所有改动只走 `auto/*` + PR，
  不直接推 `main`
- 技术路线（唯一权威）：[`ROADMAP.md`](ROADMAP.md)（人类指定，只读）
- 指令路由表：[`AGENTS.md`](AGENTS.md)（**由 `src/skills/registry.py` 渲染，不要手改**）
- 步骤状态：`state/progress.md`（**状态列只由 `tools/acceptance.py` 写**）

## 30 秒上手

```powershell
# 1) 全量测试（本系统自身的单测 + 集成测）
D:\PythonEnv\venv\Scripts\python.exe -m pytest tests/integration tests/unit -q

# 2) 验收（会写 state/progress.md；单次跑不完 10 分钟，用 --from/--until 分块）
D:\PythonEnv\venv\Scripts\python.exe tools/acceptance.py --list
D:\PythonEnv\venv\Scripts\python.exe tools/acceptance.py --from 0.1 --until 2.2
D:\PythonEnv\venv\Scripts\python.exe tools/acceptance.py --step 5.1

# 3) 自检与节奏审计
D:\PythonEnv\venv\Scripts\python.exe scripts/doctor.py        # 环境/state 自检
D:\PythonEnv\venv\Scripts\python.exe tools/cadence.py         # 8.3 频率表：什么该跑了（--due 只列该跑的）

# 4) 周期性演练（8.2 ③④⑤ 的周/月动作，各自产出可审的报告）
D:\PythonEnv\venv\Scripts\python.exe tools/dirty_weekly.py    # 每周：真实语料回放 + 并发压测 + 中断恢复
D:\PythonEnv\venv\Scripts\python.exe tools/chaos_weekly.py    # 每周：超时/5xx/限流/DNS、kill -9、磁盘满、模型被杀的降级
D:\PythonEnv\venv\Scripts\python.exe tools/redteam_month.py   # 每月：四类攻击面（越权/伪造批准/诱导改测试/套提示词）

# 5) 给自己建仓 / 核对远端（都走闸门；--verify-remote 是只读的逐文件哈希核对）
D:\PythonEnv\venv\Scripts\python.exe tools/self_repo.py --check
D:\PythonEnv\venv\Scripts\python.exe tools/self_repo.py --verify-remote

# 6) 打包一份可安装副本（默认输出到仓库外：..\pkg\repo-autopilot-<日期>\）
D:\PythonEnv\venv\Scripts\python.exe tools/package.py list
D:\PythonEnv\venv\Scripts\python.exe tools/package.py build
```

## 目录结构

| 路径 | 是什么 | 进版本库吗 |
|---|---|---|
| `src/` | 全部实现：`gateway`（模型网关）`github`（REST + 人类闸门）`triage` `dedupe` `bounce` `localize` `repair` `gatekeep` `publish` `scout` `scaffold` `skills` `batch` `agent` | ✅ 代码 |
| `tools/` | 每个步骤的**验收/评测/演练脚本**（`acceptance.py` 是总入口；`cadence.py` 是 8.3 频率表的到期审计；`dirty_weekly.py`/`chaos_weekly.py`/`redteam_month.py` 是周期性演练；`package.py`/`self_repo.py` 是打包与建仓） | ✅ |
| `scripts/` | CI 门禁用的小脚本（`check_blacklist.py`、`check_test_integrity.py`、`doctor.py`） | ✅ |
| `tests/` | 单测 + 集成测 + e2e + fixtures（陪练仓库由 `tools/sandbox_repos.py build` 生成） | ✅（fixtures 除外） |
| `skills/` | 8 条对话指令的 SKILL.md（由注册表渲染） | ✅ |
| `config/models.yaml` | 模型档位与端点 | ✅ |
| `state/` | **运行状态与证据**（见下） | 部分 |
| `state/reports/` | **给人看的证据**：验收日志（`acceptance/<时间戳>/`）、评测报告、BLOCKED 报告、抽查清单 | ✅ |
| `state/reports/archive/scratch/` | 历史排障日志（不是交付物，留档不删） | ✅ |
| `state/runtime/` | **只追加的运行期账簿**（`token_usage.log`：每次模型调用一行） | ❌ 忽略 |
| `state/corpus/`、`state/scout-corpus/` | 冻结核对用的**金样本**（查重 holdout、猎手候选池与打分） | ✅ 验收输入 |
| `state/drill/` | 7 天演练台账（`day-N.json`）+ `archive/` | ✅ |
| `state/findings/` | 每一轮的经验教训（踩过的坑与修法） | ✅ |
| `state/tasks/`、`approvals/`、`outbox/`、`sandbox/`、`vendor/`、`e2e/`、`gate/`、`scaffold/`、`repair/`、`patches/`、`eval-repair/` | 运行期产物（队列、审批单、沙箱副本、抓取的开源副本…） | ❌ 忽略 |

> 一句话原则：**`reports/` 放给人看的证据，`runtime/` 放机器账簿，运行期副本一律忽略。**
> 混在一起的代价实测过：一个几百 KB 且天天变大的记账文件混在证据树里，谁都不知道它该不该进库。

## 两个入口

1. **对话指令（主入口）**：`/细化idea`、`/处理反馈`、`/一键处理`、`/修复`、`/找轮子`、`/建新项目`、`/体检`、`/应急`
   —— 每条的流程见 `skills/*.md`，人类确认点见 `AGENTS.md` 的表。
2. **手动兜底（CLI）**：`python -m src.cli {list|emergency|triage|fix|batch}`
   —— 绕开 AI 对话层的最后通道，**写操作照样过闸门**。

## 纪律（改这个仓库前先读）

- **TEST_GATE**：测试跑不通就不进入下一步；验收只有 `PASS` / `BLOCKED`，没有"基本完成"。
  `state/progress.md` 的状态列**只能由 `tools/acceptance.py` 写**，手改不算数。
- **人类闸门在代码层**：任何对外写都要 `require_human_approval()`，一个精确的「是」才放行；
  读 token 与写 token **物理分离**（`state/.write_token` 已 gitignore，绝不入库）。
- **不推 main**：只推 `auto/*` 分支；`verify_bases` 发现远端文件与记录的基线不一致就拒绝推。
- **金样本要冻**：本地/flash 模型即使温度 0 也不是逐位可复现，凡是当验收输入的（查重 holdout、
  猎手候选池与打分、破格结论）都冻在 `state/corpus/`、`state/scout-corpus/` 里。
- **验证要验副作用**，不是验标志位：改完模块用 `git grep "from src.<mod>"` 查还有谁在用。

## 已知环境限制（本机实测）

- 无 Docker → 沙箱走 `venv + setrlimit`，`sandbox_strength: weak`；无 `gh` CLI → 走 REST。
- Python 用 `D:\PythonEnv\venv\Scripts\python.exe`；`pip` 装包需要一次性提权。
- 本机模型走 `http://127.0.0.1:11434/v1`（Ollama：`qwen3:4b` + `bge-m3`），
  且**必须用 `src/gateway/local_client.py`（urllib）**：httpx 对本机明文 HTTP 一律 502。
- 顶层两个残留目录 `.pip-tmp/`、`pytest-cache-files-*/` 由提权 pip/pytest 创建，当前权限删不掉
  （已在 `.gitignore` 里堵住，无实际影响）。
