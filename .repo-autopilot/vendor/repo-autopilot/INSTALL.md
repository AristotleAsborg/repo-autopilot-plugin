# 安装这份副本（repo-autopilot）

> 这是一份**已打好、还没装**的副本。装之前先按下面第 2 步验证它没被改坏。
> 包里的 `MANIFEST.json` 记着源仓库 commit、生成时间、每个文件的 sha256。

## 0. 前提（本机实测过的环境）

| 需要什么 | 本机现状 | 没有会怎样 |
|---|---|---|
| Python 3.12 + venv | `D:\PythonEnv\venv\Scripts\python.exe`（依赖已齐） | 什么都跑不了 |
| 本地模型（Ollama，`qwen3:4b` + `bge-m3`） | `http://127.0.0.1:11434` | 分类/查重/停止判断全走不了；可先 `state/mode.json` 设 offline |
| 读 token（必填，只读用） | 环境变量 `GH_READ_TOKEN`，或 `state/GH_READ_TOKEN.txt`，或 `state/GH_READ_TOKEN`（**两个文件名都认**） | 拉不到 issue |
| 写 token（**只在要推 PR 时**才需要） | `state/.write_token`（已被 gitignore 挡死） | 只读流程不受影响 |
| 远端模型 key（可选） | 环境变量 `DEEPSEEK_API_KEY` 或 `$DSH_HOME/.deepseek_key` | 走本地模型档 |

## 1. 放到哪

包是**自包含的目录**，不依赖 git 历史，整个拷过去即可：

```powershell
# 例：会话工作区是 D:\deepseek harness —— **装在它里面**
Copy-Item -Recurse <包目录> 'D:\deepseek harness\installed\repo-autopilot'
cd 'D:\deepseek harness\installed\repo-autopilot'
```

> **⚠️ 装在 harness 的工作区里，别装在别处**（2026-09-13 实测教训）
>
> harness 有文件沙箱：**只有会话工作区（workspace）内的写入免批准**，工作区之外每次写都要
> 人工批准。把副本装在 `D:\apps\...` 这类工作区外的路径上，症状是：
> - `pytest` 的 scratch fixture 建 `.cache/test-scratch/` 被拒 → **32 个 setup ERROR**
>   （看起来像"回归跑不干净"）；
> - `/细化idea` 每写一次 `state/specs/` 就要批准一次。
>
> 这**不是 ACL 坏了**（工作区 ACL 正常：`Authenticated Users:(M)` 继承，工作区里读写、
> 跑全量测试都不需要任何批准 —— 已实测）；原因就是**路径在工作区之外**。
> 两条可行做法：装在**会话工作区里面**，或者把那个目录**本身**作为会话工作区打开。

## 2. 先验证这份包是好的（**别跳过**）

```powershell
# 2.1 逐文件校验 sha256（少一个、多一个、改一个字节都会报）
D:\PythonEnv\venv\Scripts\python.exe tools/package.py verify .

# 2.2 全量测试（单测 + 集成测）
D:\PythonEnv\venv\Scripts\python.exe -m pytest tests/integration tests/unit -q

# 2.3 自检（环境 / state / 闸门 / 模型 / GitHub 连通性，7 项）
D:\PythonEnv\venv\Scripts\python.exe scripts/doctor.py
```

`2.3` 若报缺目录/缺 token，按它给的 A/B/C 选项处理（它只会读，不会自己动手）。

### 2.4 演练用的陪练仓库要**现场生成**（包里不带）

包里**只有 git 跟踪的文件**，所以 `tests/fixtures/sandbox-repos/`（三个陪练仓库）没带 ——
它们是可重建的产物，而且里面有 10MB 对抗样本：

```powershell
# 需要网络 + 写 token（它会在你的账号下建/更新 sandbox-clean|messy|hostile 三个仓库）
D:\PythonEnv\venv\Scripts\python.exe tools/sandbox_repos.py build
```

不跑这一步也不影响只读流程；只有 `/修复` 的沙箱演练、混沌/红队演练与 `state/drill` 需要它。
缺件时演练脚本会**明确报"缺件"**（不会伪装成"对抗样本没被识别出来"）。

## 3. 在副本里怎么验证（**完整验收请回源仓库跑**）

这一步要说清楚，因为一开始写错了：**本副本跑不了完整的 `acceptance.py`**，而且那是**设计如此**。

验收的每一步核对的是"**开发过程中留下的逐步证据**"（0.2 的分类基线评测报告
`eval_classify_*.json`、0.3 的 `embed-check.log`……）。那些是**证据**，不是代码，
所以按"只打 git 跟踪的、且只带骨架与金样本"的打包规则**不进包**。
在副本里跑 `--all` 会在 0.2 停成 BLOCKED —— 那不代表装坏了，只代表"这里没有那台开发机的历史证据"。

副本里**该跑、也能跑通**的是这四样：

```powershell
# 3.1 自检（7 项：state/mode/队列/闸门/token/本地模型/GitHub 连通性）
D:\PythonEnv\venv\Scripts\python.exe scripts/doctor.py

# 3.2 全量测试（单测 + 集成测；缺陪练仓库的用例会按设计跳过并说明原因）
D:\PythonEnv\venv\Scripts\python.exe -m pytest tests/unit tests/integration -q

# 3.3 就地能跑的验收步骤：0.1 是**现场探测**（不依赖任何历史文件）
D:\PythonEnv\venv\Scripts\python.exe tools/acceptance.py --step 0.1
D:\PythonEnv\venv\Scripts\python.exe tools/probe_capabilities.py

# 3.4 工具冒烟：猎手（真搜一次，四条来源都要带回候选）+ 频率表到期审计
D:\PythonEnv\venv\Scripts\python.exe tools/eval_scout.py --smoke
D:\PythonEnv\venv\Scripts\python.exe tools/cadence.py
```

想跑**完整验收**（0.1→8.1）：请在**源仓库**里跑，那里有全部证据；分块用 `--from/--until`，
单次不要超过 10 分钟。副本里跑过的 `--step 0.1` 会往 `state/progress.md` 写状态，
想把表恢复成"源仓库那一份"，用包里 `state/progress.md` 覆盖即可。

另外两条不变：**`--step 8.2` 现在不要跑**（7 天演练才第 1 天，跑了只会记成 BLOCKED），
用 `python tools/daily_drill.py --summary` 看进度；4.4 的验收用隔离审批目录、不做任何对外写。

## 4. 装进 harness（**作为项目根**）

本系统按**项目根**安装：把本目录当作 harness 的工作区（project root）打开即可，
不需要插件、不需要注册表、不需要常驻进程 —— 这条正好与路线第十一部分的结论一致
（"事件驱动"在本机的实际形态就是"cron 轮询 + 对话唤起"）。

| 你要做的 | 为什么 |
|---|---|
| 把本目录（或它的一个副本）作为 DSH 会话的**工作区/项目根**打开 | harness 会自动读取项目根下的 `AGENTS.md`（本会话已实测：`repo-autopilot\AGENTS.md` 被当作项目指令加载），8 条指令的路由表就在那里 |
| 保持 `AGENTS.md` 与 `skills/` 在项目根下 | 它们由 `src/skills/registry.py` 渲染；`tools/build_skills.py --check` 会验证三者一致 |
| 想手动兜底时用 `python -m src.cli {list|emergency|triage|fix|batch}` | CLI 绕开对话层，但**写操作照样过闸门** |

**没有**做也不需要做的事：不改 `settings.yaml`、不往 `.agent-presets/` 写预设、不装 Cordis 插件
（插件形态是路线 7.3 的**可选增强**，本次安装按人类选择走"项目根"这条线）。

## 5. 回滚

包是纯目录，没有注册表、没有服务、没有系统级改动：

```powershell
Remove-Item -Recurse -Force D:\apps\repo-autopilot
```

唯一的外部痕迹是你在 GitHub 上点过头的那次真实演练（`auto/*` 分支与 PR），
以及本系统的私有仓库 `AristotleAsborg/repo-autopilot`
（关掉 PR、删掉分支；仓库本身删掉即可 —— 报告里每次都带"回滚"一节，写明了这一条）。
