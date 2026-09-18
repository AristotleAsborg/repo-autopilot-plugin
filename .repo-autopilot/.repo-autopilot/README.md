# repo-autopilot-plugin

把 [repo-autopilot](https://github.com/AristotleAsborg/repo-autopilot) **本体**打包成
DeepSeek Harness 插件。仓库里自带一份打好的 repo-autopilot（`vendor/repo-autopilot/`），
装完**不需要另外 clone**。

---

## repo-autopilot 是什么

**[repo-autopilot](https://github.com/AristotleAsborg/repo-autopilot)** 是一个把**仓库维护工作
自动化**的系统：它把「拿到反馈 → 分类 → 定位 → 改代码 → 过测试门禁 → 开 PR」这条链路
做成可重复的流程，并且**在每一个对外写动作之前都插入人类闸门**。

它同时是一条**从一句话到可运行项目**的链路：`/细化idea "我想要个 X"` 会把它逐轮追问成
**可施工的 spec**，随后**自动执行** —— 生成项目骨架、写实现、跑内部测试与测试门禁，
最后（可选）建仓首推。**你只需要回答「是 / 否」，中间不用写代码。**

```text
一句话 idea
   │  /细化idea        逐轮追问（你只答 是/否）→ 可施工的 spec
   ▼
spec
   │  /建新项目        自动执行：生成骨架 → 写实现 → 内部测试 → 测试门禁
   ▼
可运行的项目（可选：建仓 + 首推）
```

### 五个模块

| 模块 | 做什么 |
|---|---|
| **M1** 细化 idea | 把一句话 idea 用「是 / 否」逐轮追问成**可施工的 spec** |
| **M2** 处理反馈 | issue 分类打标 → 查重聚类 → 路由（该修 / 该问 / 该关） |
| **M3** 修复 | 定位文件 → 修复循环 → **测试门禁** → 出报告 → 推送 PR |
| **M4** 找轮子 | 搜索现成实现 → 硬过滤 → 看代码打分 → 抓取可用件 |
| **M5** 建新项目 | **自动执行 spec**：生成骨架 + 写实现 + 内部测试 +（可选）建仓首推 |

### 八条对话指令

`/细化idea`　`/处理反馈`　`/一键处理`　`/修复`　`/找轮子`　`/建新项目`　`/体检`　`/应急`

指令清单、`AGENTS.md` 路由表、`skills/` 三者**必须一一对应**，由 `tools/build_skills.py` 渲染并校验。

### 工程上的硬约束

* **人类闸门**：所有对外写（评论 / 打标 / 开 PR / 合并 / 建仓）都要过闸门，且闸门是**代码级**的；
* **读写凭证物理分离**：读 token 与写 token 分开存放，写 token 只在闸门通过之后才被使用；
* **模型分档**：判断题走**本地小模型**（断网可用、不花钱），生成 / 计划 / diff 走强模型；
  两种档位都在 `config/models.yaml` 里声明，支持 **API 替代接法**（见 [四](#四本地小模型与-api-替代接法)）；
* **不许静默劣化**：缺件、失败一律响亮报错，**绝不返回脏数据**；
* **7 天实战演练 + 全量验收表**：`0.1`~`8.2` 逐步留证据，`/体检` 做全量回归与指标评测。

---

## 这个插件做什么

把上面这套系统装进 harness，并把它已有的入口点接成模型可调用的工具。

提供 **1 个工具**：`repo_autopilot_check`，四种模式：

| 模式 | 检查什么 |
|---|---|
| `doctor` | 七项环境自检（state 完整性 / 闸门悬挂审批 / 本地小模型 / GitHub 连通性 …） |
| `drill` | 7 天实战演练台账是否合格 |
| `acceptance` | 验收步骤表能否加载 |
| `compare` | 源仓库 ↔ 副本的**逐文件内容哈希比对**（合并两份副本之前跑） |

对外只做三件事：**执行一条只读命令 → 把退出码翻译成人话 → 把原始输出带回来**。

**自包含**：仓库里带着一份**已打好的 repo-autopilot**（`vendor/repo-autopilot/`，190 个文件，
含 `MANIFEST.json` 逐文件 sha256）。需要时也可以用 `repo_root` 参数指向你自己的检出。

**边界**：这四种模式**全是只读**的 —— 不建仓库、不评论、不打标签、不开 PR、不推送，
也不读取任何 GitHub 凭证。repo-autopilot 的**写**能力属于它自己的闸门流程，不在本插件范围内。

### 基本原理

```text
模型调用 repo_autopilot_check(mode, repo_root?)
        │
        ▼
Host 半边（host.js · Cordis dynamic Package · Node 侧）
  ① 定位仓库根    ② 解析解释器    ③ 拼一条命令行    ④ 经 shell 服务起子进程
        │
        ▼
repo-autopilot 的既有入口点（scripts/doctor.py 等 · Python 侧）
        │
        ▼
stdout / stderr + 退出码
  ⑤ 误诊分类 → 退出码语义 → 渲染成「结论 + 解释器 + 命令行 + 输出尾部」
```

五条基本设计：

1. **自包含 + 可校验** —— 随插件带一份打好包的 repo-autopilot，并用它自己的 `MANIFEST.json` 逐文件
   sha256 校验；装完即用，且「自带的那份有没有被改过」是**可验的**，不是靠信。
2. **适配器，不是分叉** —— 插件里没有一行 repo-autopilot 的业务逻辑，只负责「调哪个入口点 + 怎么解释退出码」。两条入口（对话指令 / 插件）共享同一套实现。
3. **退出码语义内置** —— 这套系统的工具用退出码表达**状态**而非**成败**（`drill` 的 `1` 是「还没满 7 天，不是失败」）。语义写在模式表里，不让模型去猜。
4. **先分清「谁的错」** —— 拿到 stderr 先做一次误诊分类：解释器找不到 / 缺依赖 / 路径错，各归各的账，绝不把**适配器的故障**说成**仓库的故障**。
5. **零凭证、只读** —— 四种模式全是只读命令；源码里不存在任何读取凭证的路径。

> **仓库根怎么定位**：`repo_root` 参数 → 否则用安装时写进 `host.local.js` 的自带副本路径。
> Host 半边拿不到自己的磁盘位置（没有 `fs` / `__dirname` / `process`），所以这个路径必须在
> **安装时**写进去 —— 见 [2.5](#25-注册成-cordis-package)。

---

## 目录

- [repo-autopilot 是什么](#repo-autopilot-是什么)
- [这个插件做什么](#这个插件做什么)
- [一、环境要求](#一环境要求)
- [二、安装](#二安装)
- [三、使用方法](#三使用方法)
- [四、本地小模型与 API 替代接法](#四本地小模型与-api-替代接法)
- [五、技术路线](#五技术路线)
- [六、排错](#六排错)
- [七、许可证](#七许可证)

---

## 一、环境要求

| 项目 | 要求 | 为什么 | 缺了会怎样 |
|---|---|---|---|
| **Python** | 3.12 或更高 | 要跑 repo-autopilot 的 `tools/` | 全部模式都跑不了 |
| **Python 模块** | `yaml`、`requests`、`numpy` | 读配置 / 走网络 / 本地模型向量 | 对应模式报「解释器缺依赖」 |
| **repo-autopilot 仓库** | **随插件自带**（`vendor/repo-autopilot/`），无需另外准备 | 插件是适配器，业务逻辑全在那里 | 全部模式都跑不了（可用 `repo_root` 指向你自己的检出） |
| **shell 服务** | PowerShell 系（Windows 上即 `pwsh`） | 命令行用 `&` 调用符拼接 | 插件报「没有 shell 服务」或 PowerShell 解析错误 |
| **git** | 可选 | 只有本地保存类功能需要 | 只读模式不受影响 |

> **注意**：repo-autopilot 仓库本身**没有** `requirements.txt`，依赖装在哪个解释器里，
> 就得用哪个解释器去跑。见 [2.1](#21-路线-a一键脚本推荐)。

---

## 二、安装

两种装法，**同一份实现**（`lib/index.js` 由 `host.js` 生成，有用例钉住不许漂移）：

| 装法 | 怎么做 | 适合 |
|---|---|---|
| **一键（profile 层）** | **双击 `install.cmd`** | 想要"装完就能用、重启即生效" |
| 会话内（dynamic Package） | `python scripts/install.py --emit-host`，把 `host.local.js` 喂给 `cordis_define` | 想要热更、不想动 profile |

### 2.0 一键安装（双击 `install.cmd`）

双击它会依次做三件事：

1. 找 Python（按 `-Python` → `REPO_AUTOPILOT_PYTHON` → `python3` → `python` → `py -3.12`），
   检查版本与依赖，并**逐文件 sha256 校验**随包自带的那份 repo-autopilot；
2. 生成 `host.local.js`（把自带副本路径写进 `DEFAULT_REPO_ROOT`）；
3. `dsh plugin --profile <名字> add <本目录>` —— 装成一个 **profile 层**，重开会话即生效。

可用环境变量：`DSH_PROFILE`（默认 `standard`）、`DSH_BIN`（dsh 可执行文件路径）。

> **两条限制，先说清**：① 装 profile 要写 `%DSH_HOME%\profiles\...`，**在 DSH 会话里跑会被沙箱拦**
> （实测报 `EPERM: mkdir 'D:\dsh\home\profiles\...'`）—— 所以要在资源管理器里双击、或普通终端里跑；
> ② 这一步用 pnpm，需要能访问 registry。
>
> 卸载：`dsh plugin --profile <名字> remove repo-autopilot-plugin`

### 2.1 会话内装法（dynamic Package）

```bash
python scripts/install.py --emit-host
```

然后把 **`host.local.js`** 的全部内容喂给 `cordis_define`（见 [2.5](#25-注册成-cordis-package)）。


三条路线，选一条即可。**路线 A 最省事**。

### 2.1 路线 A：一键脚本（推荐）

```bash
python scripts/install.py --repo-root <repo-autopilot 的绝对路径>
```

**Windows 上还可以走 PowerShell 包装**（它会先替你找到一个能用的解释器，再交给上面那个脚本）：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\install.ps1 -RepoRoot "D:\work\repo-autopilot"
```

`install.ps1` 找解释器分**两趟**：先找「版本够 **且依赖齐**」的，找不到再退而求其次找「版本够」的。
所有检查与报告都由 `install.py` 完成，`install.ps1` 只负责找解释器并转参数。

脚本会依次做四件事：

1. 检查**正在跑它的那个解释器**（版本 ≥ 3.12、三个依赖模块是否齐）；
2. 检查 repo-autopilot 仓库根（四个只读入口点是否齐）—— 不给 `--repo-root` 就自动向上查找；
3. 打印把 host.js 注册成 Cordis Package 的具体步骤；
4. 给出一条 `REPO_AUTOPILOT_PYTHON` 设置命令，省掉以后每次指定解释器。

参数：

| 参数 | 说明 |
|---|---|
| `--repo-root PATH` | repo-autopilot 仓库绝对路径；省略则从当前目录逐级向上自动查找 |
| `--python PATH` | 指定解释器（仅用于展示/建议；脚本只检查**自己所在的**解释器） |
| `--install-deps` | 缺依赖时用当前解释器的 pip **顺手装上**（默认只提示、不动手） |
| `--use-uv` | 缺依赖时用 `uv` 建一个 `.venv` 并装依赖（需要本机有 uv） |
| `--dry-run` | 只打印要做什么，不执行（与上面两个开关同用时不会真的执行） |

退出码：**0 = 可以注册**，**1 = 有缺件**。

#### 环境引导档（uv / winget）

缺依赖时，脚本探测本机**实际有什么**，再给分档方案（`uv: 有/无｜winget: 有/无`）：

| 本机情况 | 给出的方案 |
|---|---|
| 有 `uv` | `uv venv .venv --python 3.12` + `uv pip install --python .venv pyyaml requests numpy` |
| 只有 `winget` | `winget install --id astral-sh.uv`（推荐）或 `winget install --id Python.Python.3.12` |
| 都没有 | 指向 python.org 的手工下载 |

`uv` 是 MIT/Apache-2.0 的开源工具，能同时管 Python 版本与依赖 —— 这是「本机没有 3.12」
这个最大障碍的**低成本**解法。加 `--use-uv` 时脚本才会真的去建环境；**默认只打印命令**，
不下载、不安装任何东西。

`install.py` / `smoke.py` **只用标准库**（装依赖的脚本自己不能依赖三方库），
并且只检查**正在跑它的那个解释器**。设计取舍与理由见 [`NOTES.md`](NOTES.md) 第八节。

成功时：

```
=== 1/3 解释器与依赖 ===
  [OK] 解释器版本：3.12.13（D:\PythonEnv\venv\Scripts\python.exe）
  [OK] 模块 yaml：读 config/ 与 state/ 里的 YAML
  [OK] 模块 requests：GitHub / 本地模型探活
  [OK] 模块 numpy：本地小模型档的向量运算

=== 2/3 repo-autopilot 仓库 ===
  （仓库根：D:\deepseek harness\repo-autopilot）
  [OK] scripts/doctor.py：doctor 模式（七项自检）
  ...

=== 3/3 结论 ===
  **可以注册** —— 解释器、依赖、仓库三项全过。

下一步：
  · 让插件固定用这个解释器（省掉每次指定）：
      $env:REPO_AUTOPILOT_PYTHON = 'D:\PythonEnv\venv\Scripts\python.exe'
  · 注册成 Cordis Package：...
```

缺件时**逐条给出补法**：

```
  [缺] 模块 numpy：缺失（本地小模型档的向量运算）—— ModuleNotFoundError: No module named 'numpy'
        → 补法：D:\PythonEnv\venv\Scripts\python.exe -m pip install numpy
...
  **不能直接注册** —— 3 项缺件。
```

**脚本的设计取舍（有意为之）**

* **只用标准库**：安装脚本依赖三方库是个死循环 —— 缺 `yaml` 的时候它得能跑起来报缺件。
* **不自动下载任何东西**：`--install-deps` 是显式开关。自动装 Python、自动下载工具
  会引入网络与信任面，收益不值这个价。
* **不抓子进程输出**：本 harness 的沙箱禁止用管道抓别的程序输出（会 EPERM）。
  所以只检查**当前解释器** —— 这也更诚实：报告的永远是你实际在用的那个。

### 2.2 路线 B：对已有的解释器

如果你已经有一个装好依赖的解释器（例如某个 venv），直接用它跑自检即可：

```bash
<你的解释器> scripts/install.py --repo-root <路径>
```

之后把这个路径告诉插件，就不用每次指定了：

```powershell
$env:REPO_AUTOPILOT_PYTHON = 'D:\PythonEnv\venv\Scripts\python.exe'
```

### 2.3 路线 C：手工

```bash
<解释器> -m pip install pyyaml requests numpy
python scripts/smoke.py --repo-root <repo-autopilot 的绝对路径>
```

### 2.4 冒烟自检

`install.py` 面向"装之前"，`smoke.py` 面向"装之后随时查"。两者检查项一致，都用标准库：

```bash
python scripts/smoke.py                 # 默认查随插件自带的那份
python scripts/smoke.py --repo-root <你自己的 repo-autopilot 检出>
```

退出码：**0 = 全过**，**1 = 有缺件**。

### 2.5 注册成 Cordis Package

**先生成 `host.local.js`**（把自带副本的绝对路径写进去）：

```bash
python scripts/install.py --emit-host
```

它只改 `host.js` 里的一行 —— `const DEFAULT_REPO_ROOT = ''` 会被填成自带副本的绝对路径。
生成的那份**已 gitignore**（里面是本机路径，不该进仓库）。

然后把 **`host.local.js`** 的**全部内容**喂给 `cordis_define`：它是普通 JavaScript，
没有 TypeScript、没有 `import` / `require`、不依赖未声明的全局。

```js
cordis_define({
  plugin: { kind: 'new', idPrefix: 'rauto' },
  name: '<包名>',
  purpose: '<一句话用途>',
  code: { host: <host.local.js 的全部内容> },
})
// 然后用返回的 pluginId / packageId 调 cordis_run（首次 mode: 'run'）
```

> 为什么必须"写进去"而不是运行时自己找：Host 半边没有 `fs`、没有 `__dirname`、
> 没有 `process`，**拿不到自己的磁盘位置**。所以自带副本在哪，只能在安装时确定。
> 不改 `host.js` 也能用 —— 每次调用传 `repo_root` 参数即可。

### 2.6 包自检

不需要 DSH 运行时，任何装了 Python 的机器都能跑：

```bash
python -m pytest -q
```

---

## 三、使用方法

### 3.1 工具与参数

工具名：**`repo_autopilot_check`**

| 参数 | 必填 | 类型 | 说明 |
|---|---|---|---|
| `mode` | 是 | string | `doctor` / `drill` / `acceptance` / `compare` |
| `repo_root` | 否 | string | repo-autopilot 仓库的**绝对路径**；不填则用随插件自带的那份 |
| `target` | 仅 `compare` | string | 要比对的副本目录 |
| `python` | 否 | string | 解释器绝对路径；不填则自动探测（见 [3.4](#34-解释器解析顺序)） |

### 3.2 四种模式

#### `doctor` —— 七项环境自检

```bash
& '<python>' 'scripts/doctor.py' '--json'
```

| 退出码 | 含义 |
|---|---|
| `0` | 七项自检全过 |

七项分别是：**state 目录完整性**、**mode.json 合法性**、**队列孤儿任务**、
**闸门悬挂审批**、**写 token 文件**、**本地小模型探活**、**GitHub 连通性**。

真实输出（成功时，插件渲染为）：

```
[doctor] 七项自检全过
解释器：由 python 参数指定（未探测）
$ & 'D:\PythonEnv\venv\Scripts\python.exe' 'scripts/doctor.py' '--json'
    "name": "本地小模型探活",
    "ok": true,
    "detail": "embedding 维度 1024",
...
```

#### `drill` —— 7 天实战演练台账

```bash
& '<python>' 'tools/daily_drill.py' '--summary'
```

| 退出码 | 含义 |
|---|---|
| `0` | 7 天台账合格 |
| `1` | **还没满 7 天，不是失败** |

> 这条退出码语义是这套系统的典型情况：工具用退出码表达**状态**而不是**成败**。
> 插件把它编码进模式表，模型不需要猜。

```
[drill] 7 天演练台账合格（仍需人类评审补丁质量）
$ & '...python.exe' 'tools/daily_drill.py' '--summary'
演练累计记录 7 条：
  day  1　2026-09-12　模式 full　人类介入 0 次　对外写 0 次　误操作 0　通过
  ...
  · 7 天记录齐了 —— 剩下的判断（补丁质量）按路线属于人类评审
```

#### `acceptance` —— 验收步骤表

```bash
& '<python>' 'tools/acceptance.py' '--list'
```

| 退出码 | 含义 |
|---|---|
| `0` | 验收步骤表可加载 |

列出 0.1 ~ 8.2 全部验收步骤及其检查项（capabilities / baseline / queue / gate / drill …）。

#### `compare` —— 副本哈希比对

```bash
& '<python>' 'tools/package.py' 'compare' '<target>'
```

| 退出码 | 含义 |
|---|---|
| `0` | 副本与源仓库一致，没有合并风险 |
| `1` | 有差异 —— **合并前先问清是谁改的** |

**合并两份副本之前先跑它。** 真实输出：

```
[compare] 副本与源仓库一致，没有合并风险
$ & '...python.exe' 'tools/package.py' 'compare' 'D:\apps\repo-autopilot'
比对 源仓库 ↔ D:\apps\repo-autopilot
  源仓库清单 189 个文件
  （副本多出 17 个非包内文件，**不算合并风险**，列出来给你看：...）
  （另有 173 个本机自有文件，按设计不算差异）
结论：189 个包内文件内容哈希逐一相同，没有合并风险
```

> 判据是**内容哈希**，不是时间戳、不是文件名、不是"推送成功的提示"。
> 副本多出的文件是**告知**（不算风险）；缺文件或内容不符才是**合并风险**。

### 3.3 输出格式

```
[doctor] 七项自检全过                    ← 结论（人话）
解释器：由 python 参数指定（未探测）      ← 用的哪个解释器、怎么来的
$ & 'D:\...\python.exe' 'scripts/doctor.py' '--json'   ← 实际命令行（可复制重跑）
<命令输出的末尾 25 行>                   ← 原始输出
```

* 命令行的**实际文本**永远回显，便于手工复现与排错；
* 输出tail 限 25 行，避免撑爆上下文；
* 结论永远是**人话**，不是退出码。

### 3.4 解释器解析顺序

不填 `python` 参数时，按顺序探测，**第一个能 `import yaml, requests` 的胜出**：

1. 环境变量 `REPO_AUTOPILOT_PYTHON`
2. `python3`
3. `python`

探测结果会**缓存**，同一次运行只探一次。三个都不行时明确报：

```
找不到能用的解释器（试过：环境变量 REPO_AUTOPILOT_PYTHON、python3、python，
都没法 import yaml, requests）—— 这是环境问题，不是仓库问题。
请用 python 参数指向装了依赖的解释器，或设环境变量 REPO_AUTOPILOT_PYTHON
```

### 3.5 故障分类

插件会先把「**谁的错**」分清楚再报告，避免把环境问题说成仓库问题：

| 症状 | 插件的结论 |
|---|---|
| 解释器不存在 | 解释器找不到（是环境的问题，不是仓库的问题） |
| 解释器缺依赖 | 解释器缺依赖（是环境的问题，不是仓库的问题） |
| `repo_root` 给错 | repo_root 下找不到这个入口点（多半是路径给错了，不是仓库坏了） |
| 退出码不在预期集合 | 照常给出模式结论，并注明「退出码不在预期集合内」 |

### 3.6 典型用法

**① 合并两份副本之前确认没风险**

```
repo_autopilot_check(mode='compare', repo_root='D:\work\repo-autopilot', target='D:\apps\repo-autopilot')
```

**② 一条命令看这台机器能不能跑**

```
repo_autopilot_check(mode='doctor', repo_root='D:\work\repo-autopilot')
```

**③ 交付前确认演练台账合格**

```
repo_autopilot_check(mode='drill', repo_root='D:\work\repo-autopilot')
```

---

## 四、本地小模型与 API 替代接法

repo-autopilot 的**分类 / 查重 / 停止判断 / 文件定位**默认走**本机小模型**
（Ollama + `qwen3:4b` + `bge-m3`）。本插件不直接调用模型，但 `doctor` 模式会检查它 ——
这一节说明**没有本机模型时怎么换成 API**。

配置写在 repo-autopilot 的 `config/models.yaml`：**随插件打包的那份**在
`vendor/repo-autopilot/config/models.yaml`；如果你用 `repo_root` 指向自己的检出，就改你自己那份。

### 4.1 两种接法

| 用途 | 本机档位（默认） | API 替代档位 |
|---|---|---|
| 对话（分类 / 打标 / 停止判断） | `tiers.local_small` · Ollama `/api/chat` | `tiers.*` 里写 `api_style: "openai"` |
| **向量**（查重 / 文件定位精排） | `embed.local` · Ollama `/api/embeddings` | `embed.api` · OpenAI 兼容 `/embeddings` |

切换方式：改 `config/models.yaml` 里的 **`embed.active`**（`local` / `api`），**不用改代码**。
档位名写错会**响亮报错并列出可选项**，不会静默退回本机。

### 4.2 API 填在哪

**Key 只放两处**（二选一，取到就用）：

| 方式 | 位置 |
|---|---|
| 环境变量（推荐） | `DEEPSEEK_API_KEY` |
| 文件 | `$DSH_HOME/.deepseek_key` 的**首行** |

**不要**把 key 写进 `config/models.yaml` —— `config/` 是进 git 的。

**端点与模型名**填在 `config/models.yaml`：

```yaml
embed:
  active: "api"                              # ← 改这里切档
  api:
    base_url: "https://api.deepseek.com/v1"  # ← 换成你的 OpenAI 兼容端点
    model: "your-embedding-model"            # ← 换成服务商实际的 embedding 模型 id
    api_style: "openai"
    dim: 1024                                # ← 必须与模型真实维度一致
```

对话档位同理，在 `tiers` 下写 `base_url` / `model` / `api_style: "openai"`。

### 4.3 换档后必须做的两件事

1. **重标定 dedup 阈值。** 文件末尾的 `dedup.suggest_close` / `dedup.cluster`
   （默认 `0.98` / `0.92`）是**拿 bge-m3 标出来的**。换 embedding 模型后余弦分布会变，
   沿用旧值等于阈值失效 —— 而失效**不会报错**。
2. **核对 `dim`。** 配了 `dim` 就会**硬校验**：实际维度对不上直接抛错，而不是让阈值悄悄错下去。

> 向量条数对不上、维度对不上、响应里没有 `data`、缺 key —— 这四种都**立刻抛错**，不返回部分结果。
> OpenAI 兼容端点**不保证**返回顺序与输入一致，插件按响应里的 `index` 归位后才使用。

### 4.4 安全声明

* **数据会离开本机。** 切到 API 档后，查重与文件定位会把 **issue 正文、文件路径与代码片段**
  发往 `base_url` 指向的第三方。**涉密仓库请留在 `local` 档**（本机档位不出网）。
* **默认不外发。** 出厂配置是 `embed.active: "local"`；不主动改配置就不会有任何内容发出去。
* **凭证边界。** API key 只从上面那两处读，**不写进仓库、不进日志、不回显** ——
  报错时只回报"来源"（环境变量名或文件路径），不回报 key 本身。
* **与本插件的只读承诺不冲突。** 本插件自身不调用任何模型、不读任何凭证；
  这一节讲的是它**检查的那个系统**的模型配置。
* **成本。** API 档按量计费；本机档位不花钱。重试与失败的调用也会记入 `state/` 下的用量日志。

---

## 五、技术路线

### 5.1 形态与数据流

```
模型
 │  调用工具 repo_autopilot_check(mode, repo_root, target?, python?)
 ▼
Host 半边（host.js，Node 侧，Cordis dynamic Package）
 │  ① 解析解释器（探测/缓存）
 │  ② 拼一条 PowerShell 命令行
 │  ③ 经 shell 服务起子进程
 ▼
repo-autopilot 的既有只读入口点（scripts/doctor.py 等，Python 侧）
 │
 ▼
stdout / stderr + 退出码
 │  ④ 误诊分类 → 退出码语义 → 渲染成人话
 ▼
模型看到：结论 + 解释器 + 命令行 + 输出尾部
```

* **只有 Host 半边**，没有客户端 UI。
* **不复制业务逻辑**：插件里没有一行 repo-autopilot 的算法，只有「调用哪个入口点 + 怎么解释退出码」。
* 对话指令与插件两条入口共享同一套实现，不允许出现逻辑分叉。

### 5.2 生命周期

* `shell` 通过 `ctx.get('shell')` 读取并做**缺席判断**；没有 shell 时插件**显式报告自己不可用**，
  而不是静默地注册不出工具。
* 工具注册挂在当前 Fiber 上：`ctx.effect(() => harness.registerTool(ctx, tool), ...)`，
  `stop` / `update` / `undefine` 时自动移除，不留残留。
* `execute` 转发 `exec.signal`，取消能传导到子进程。

### 5.3 与宿主的静态契约

`harness.defineTool` 的参数 schema 有三条硬性要求（各有一条回归用例钉住）：

| # | 要求 | 违反时的报错 |
|---|---|---|
| 1 | 参数**根**不能写 `additionalProperties` | `must be true or omitted because the implicit parameter root is open` |
| 2 | 必填项写成**根级数组** `required: [...]` | `parameters.x.required belongs to the containing raw object schema` |
| 3 | 命令行用 `& 'exe' 'arg'` | 裸拼 `"exe" "arg"` → PowerShell `ParserError` |

### 5.4 shell 与命令行拼接

`ShellExecRequest.command` 是一条**命令行**（不是 argv），在 Windows 上由 `pwsh -Command` 执行。
因此：

* 用调用符 `&` 起头；引号开头的 token 会被 PowerShell 当表达式；
* 每个参数用**单引号**包裹（PowerShell 里是字面量），内部单引号按 pwsh 规矩翻倍转义；
* `$env:...` 形式的候选**不加引号**，原样交给 pwsh 展开。

**已知限制**：`&` 是 PowerShell 的调用符；bash 系 shell 的 `&` 是后台运算符，语义不同，此时插件不可用。

### 5.5 误诊表

拿到 stderr 后先做一次「谁的错」分类，命中就不把责任推给仓库。
三条模式的文案都是**从真机输出里原文抄下来的**（而不是想象外部系统会怎么报错）：

| 匹配 | 结论 |
|---|---|
| `不会被识别为` / `CommandNotFoundException` | 解释器找不到（环境问题） |
| `ModuleNotFoundError` / `No module named` | 解释器缺依赖（环境问题） |
| `can't open file` / `No such file or directory` | 入口点找不到（路径问题） |

### 5.6 安全边界

* 四种模式**全是只读**，不改任何文件；
* **不读取任何凭证**：源码里没有 `process.env` / `environ` / `authorization` / `.write_token`
  这类读取路径（有用例钉住）。判据是「**拿不到**凭证」，不是「没提到这个词」；
* 不自动下载、不自动安装（`--install-deps` 为显式开关，且只作用于 Python 依赖）；
* GitHub 写操作（评论、开 PR、合并、建仓）**不在本插件范围内**，属于 repo-autopilot 本身的闸门流程。

### 5.7 目录结构

```text
plugin-repo-autopilot/
├── plugin.yaml                     # 清单：入口、工具命名空间、触发器、偏差与验证记录
├── host.js                         # Host 半边源码（其全文即 cordis_define 的 code.host）
├── scripts/
│   ├── install.py                  # 一键安装/自检/完整性校验/--emit-host（标准库）
│   ├── install.ps1                 # Windows 薄包装：找到解释器后交给 install.py
│   └── smoke.py                    # 干净机器冒烟自检（标准库，默认查自带副本）
├── vendor/repo-autopilot/          # **随插件打包的 repo-autopilot**（190 文件 + MANIFEST.json）
├── tests/test_plugin_package.py    # 包自检 44 例（不需要 DSH 运行时）
├── pytest.ini                      # 测试配置（含沙箱下的临时目录注意事项）
├── README.md                       # 本文档
├── NOTES.md                        # 工程记录：踩过的坑与实测证据
└── LICENSE

host.local.js                       # 安装时生成（已 gitignore）：DEFAULT_REPO_ROOT 已填好
```

`install.ps1` 是 **UTF-8 with BOM**：Windows PowerShell 5.1 在没有 BOM 时会把 UTF-8 当 ANSI 读，
中文注释被解码成乱码、解析器随即在字符串里找不到收尾引号并报 `ParserError`。改动它时保留 BOM。

### 5.8 测试策略

```bash
python -m pytest -q          # 44 passed
```

覆盖六类：

1. **清单语义**：字段齐全、命名规范（kebab-case）、版本 semver、触发器只声明真能用的、
   偏差必须显式记录（不隐藏）、只读与零凭证承诺；
2. **`host.js` 代码约束**：无 `import` / `require` / JSX / 未声明全局
   （判据是**语法**而非"这些字母出现过"）；
3. **清单 ↔ 实现一致**：模式集合、退出码语义；
4. **宿主静态契约**：5.3 的三条，各附报错原文；
5. **自包含与防漂移**：自带副本在不在、与它的 `MANIFEST.json` 逐文件 sha256 是否一致、
   篡改与缺失能否被抓到、`--emit-host` 是否只改那一行、占位符丢了会不会响亮报错；
6. **行为测试**：把误诊表的正则抠出来喂**真机抓下来的**报错原文；
   冒烟/安装脚本的正面与负面路径、GBK 控制台安全性、`--dry-run` 不落盘。

### 5.9 已知限制（不隐藏，逐条记录在 `plugin.yaml` 的 `deviations`）

| 限制 | 说明 |
|---|---|
| shell 必须是 PowerShell 系 | 命令行用 `&`；bash 语义不同，此时不可用 |
| Python 运行时不打包 | 自带的是 repo-autopilot 的**代码包**，不是 Python 解释器：仍需本机有 Python 3.12+ 与依赖。纯 Node 降级未做 |
| `REPO_AUTOPILOT_PYTHON` 候选 | 机制已验证，但**未端到端验证**（插件的 shell 继承 DSH 进程环境，开发会话里改不了它） |
| 触发器只有 `manual` | 无公网入口，事件驱动（issues/PR）与 cron 只能靠轮询，故不在清单里承诺 |
| MCP 命名空间 | 未单独起 MCP server，走 harness 的 dynamic Tool |
| `drill` 的人类评审环节 | 脚本只负责出材料；补丁质量由人判断，脚本不代替 |

---

## 六、排错

| 现象 | 原因 | 处理 |
|---|---|---|
| 「解释器找不到」 | `python` 不在 PATH 或路径写错 | 用 `python` 参数给绝对路径，或设 `REPO_AUTOPILOT_PYTHON` |
| 「解释器缺依赖」 | 选的解释器没装 `yaml`/`requests`/`numpy` | 换解释器，或 `<解释器> -m pip install pyyaml requests numpy` |
| 「repo_root 下找不到这个入口点」 | `repo_root` 给错（少一层/多一层） | 指向**含 `scripts/` 与 `tools/` 的那一层** |
| 「找不到能用的解释器」 | 三个候选都不满足依赖要求 | 设 `REPO_AUTOPILOT_PYTHON` |
| 「不可用：这个环境没有 shell 服务」 | 宿主没有 `shell` service | 该环境不支持本插件 |
| PowerShell `ParserError` | shell 不是 PowerShell 系 | 见 [5.4](#54-shell-与命令行拼接) 的已知限制 |
| `doctor` 报某项失败但模型/Ollama 正常 | 多半是解释器不对（详见 `NOTES.md`） | 先确认「解释器：」那一行指对了没有 |
| `compare` 报副本缺失 | 副本确实少了文件，或反向比对（工作副本当作源） | 以**正仓为源**比对；确认差异是谁改的 |

---

## 七、许可证

MIT，见 [`LICENSE`](LICENSE)。
