# repo-autopilot-plugin

把 [repo-autopilot](https://github.com/AristotleAsborg/repo-autopilot) 的**只读检查**注册成
DeepSeek Harness 的模型工具。

* 提供 **1 个工具**、**4 种模式**：`doctor` / `drill` / `acceptance` / `compare`
* **只读**：不建仓库、不评论、不打标签、不开 PR、不推送
* **零凭证**：不读任何 GitHub token
* **适配器**：不含 repo-autopilot 的业务逻辑，只调用它已有的只读入口点

---

## 目录

- [一、环境要求](#一环境要求)
- [二、安装](#二安装)
- [三、使用方法](#三使用方法)
- [四、技术路线](#四技术路线)
- [五、排错](#五排错)
- [六、许可证](#六许可证)

---

## 一、环境要求

| 项目 | 要求 | 为什么 | 缺了会怎样 |
|---|---|---|---|
| **Python** | 3.12 或更高 | 要跑 repo-autopilot 的 `tools/` | 全部模式都跑不了 |
| **Python 模块** | `yaml`、`requests`、`numpy` | 读配置 / 走网络 / 本地模型向量 | 对应模式报「解释器缺依赖」 |
| **repo-autopilot 仓库** | 一份本地检出即可 | 插件是适配器，业务逻辑全在那里 | 全部模式都跑不了 |
| **shell 服务** | PowerShell 系（Windows 上即 `pwsh`） | 命令行用 `&` 调用符拼接 | 插件报「没有 shell 服务」或 PowerShell 解析错误 |
| **git** | 可选 | 只有本地保存类功能需要 | 只读模式不受影响 |

> **注意**：repo-autopilot 仓库本身**没有** `requirements.txt`，依赖装在哪个解释器里，
> 就得用哪个解释器去跑。见 [2.1](#21-路线-a一键脚本推荐)。

---

## 二、安装

三条路线，选一条即可。**路线 A 最省事**。

### 2.1 路线 A：一键脚本（推荐）

```bash
python scripts/install.py --repo-root <repo-autopilot 的绝对路径>
```

脚本会依次做四件事：

1. 检查**正在跑它的那个解释器**（版本 ≥ 3.12、三个依赖模块是否齐）；
2. 检查 repo-autopilot 仓库根（四个只读入口点是否齐）—— 不给 `--repo-root` 就自动向上查找；
3. 打印把 host.js 注册成 Cordis Package 的具体步骤；
4. 给出一条 `REPO_AUTOPILOT_PYTHON` 设置命令，省掉以后每次指定解释器。

参数：

| 参数 | 说明 |
|---|---|
| `--repo-root PATH` | repo-autopilot 仓库绝对路径；省略则从当前目录逐级向上自动查找 |
| `--install-deps` | 缺依赖时**顺手装上**（默认只提示、不动手） |
| `--dry-run` | 只打印要做什么，不执行（与 `--install-deps` 同用时不会真的调 pip） |

退出码：**0 = 可以注册**，**1 = 有缺件**。

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
python scripts/smoke.py --repo-root <repo-autopilot 的绝对路径>
```

退出码：**0 = 全过**，**1 = 有缺件**。

### 2.5 注册成 Cordis Package

`host.js` 的**全部内容**就是喂给 `cordis_define` 的 `code.host` —— 普通 JavaScript，
没有 TypeScript、没有 `import` / `require`、不依赖未声明的全局。

```js
cordis_define({
  plugin: { kind: 'new', idPrefix: 'rauto' },
  name: '<包名>',
  purpose: '<一句话用途>',
  code: { host: <host.js 的全部内容> },
})
// 然后用返回的 pluginId / packageId 调 cordis_run（首次 mode: 'run'）
```

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
| `repo_root` | 是 | string | repo-autopilot 仓库的**绝对路径** |
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

## 四、技术路线

### 4.1 形态与数据流

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

### 4.2 生命周期

* `shell` 通过 `ctx.get('shell')` 读取并做**缺席判断**；没有 shell 时插件**显式报告自己不可用**，
  而不是静默地注册不出工具。
* 工具注册挂在当前 Fiber 上：`ctx.effect(() => harness.registerTool(ctx, tool), ...)`，
  `stop` / `update` / `undefine` 时自动移除，不留残留。
* `execute` 转发 `exec.signal`，取消能传导到子进程。

### 4.3 与宿主的静态契约

`harness.defineTool` 的参数 schema 有三条硬性要求（都是实测撞出来的，各有一条回归用例）：

| # | 要求 | 违反时的报错 |
|---|---|---|
| 1 | 参数**根**不能写 `additionalProperties` | `must be true or omitted because the implicit parameter root is open` |
| 2 | 必填项写成**根级数组** `required: [...]` | `parameters.x.required belongs to the containing raw object schema` |
| 3 | 命令行用 `& 'exe' 'arg'` | 裸拼 `"exe" "arg"` → PowerShell `ParserError` |

### 4.4 shell 与命令行拼接

`ShellExecRequest.command` 是一条**命令行**（不是 argv），在 Windows 上由 `pwsh -Command` 执行。
因此：

* 用调用符 `&` 起头；引号开头的 token 会被 PowerShell 当表达式；
* 每个参数用**单引号**包裹（PowerShell 里是字面量），内部单引号按 pwsh 规矩翻倍转义；
* `$env:...` 形式的候选**不加引号**，原样交给 pwsh 展开。

**已知限制**：`&` 是 PowerShell 的调用符；bash 系 shell 的 `&` 是后台运算符，语义不同，此时插件不可用。

### 4.5 误诊表

拿到 stderr 后先做一次「谁的错」分类，命中就不把责任推给仓库。
三条模式的文案都是**从真机输出里原文抄下来的**（而不是想象外部系统会怎么报错）：

| 匹配 | 结论 |
|---|---|
| `不会被识别为` / `CommandNotFoundException` | 解释器找不到（环境问题） |
| `ModuleNotFoundError` / `No module named` | 解释器缺依赖（环境问题） |
| `can't open file` / `No such file or directory` | 入口点找不到（路径问题） |

### 4.6 安全边界

* 四种模式**全是只读**，不改任何文件；
* **不读取任何凭证**：源码里没有 `process.env` / `environ` / `authorization` / `.write_token`
  这类读取路径（有用例钉住）。判据是「**拿不到**凭证」，不是「没提到这个词」；
* 不自动下载、不自动安装（`--install-deps` 为显式开关，且只作用于 Python 依赖）；
* GitHub 写操作（评论、开 PR、合并、建仓）**不在本插件范围内**，属于 repo-autopilot 本身的闸门流程。

### 4.7 目录结构

```text
plugin-repo-autopilot/
├── plugin.yaml                     # 清单：入口、工具命名空间、触发器、偏差与验证记录
├── host.js                         # Host 半边源码（其全文即 cordis_define 的 code.host）
├── scripts/
│   ├── install.py                  # 一键安装/自检（标准库）
│   └── smoke.py                    # 干净机器冒烟自检（标准库）
├── tests/test_plugin_package.py    # 包自检 32 例（不需要 DSH 运行时）
├── pytest.ini                      # 测试配置（含沙箱下的临时目录注意事项）
├── README.md                       # 本文档
├── NOTES.md                        # 工程记录：踩过的坑与实测证据
└── LICENSE
```

### 4.8 测试策略

```bash
python -m pytest -q          # 32 passed
```

覆盖五类：

1. **清单语义**：字段齐全、命名规范（kebab-case）、版本 semver、触发器只声明真能用的、
   偏差必须显式记录（不隐藏）、只读与零凭证承诺；
2. **`host.js` 代码约束**：无 `import` / `require` / JSX / 未声明全局
   （判据是**语法**而非"这些字母出现过"）；
3. **清单 ↔ 实现一致**：模式集合、退出码语义；
4. **宿主静态契约**：4.3 的三条，各附报错原文；
5. **行为测试**：把误诊表的正则抠出来喂**真机抓下来的**报错原文；
   冒烟/安装脚本的正面与负面路径、GBK 控制台安全性、`--dry-run` 不落盘。

> 测试的一个原则：**夹具必须来自真机输出**。曾经因为把外部系统的报错文案"想象"出来，
> 代码和用例基于同一个错误假设、手拉手地一起错，用例全绿而功能是坏的。
> 详见 `NOTES.md`。

### 4.9 已知限制（不隐藏，逐条记录在 `plugin.yaml` 的 `deviations`）

| 限制 | 说明 |
|---|---|
| shell 必须是 PowerShell 系 | 命令行用 `&`；bash 语义不同，此时不可用 |
| Python 运行时不打包 | 假设本机已有 Python 3.12+ 与依赖；不做自包含分发。纯 Node 降级未做 |
| `REPO_AUTOPILOT_PYTHON` 候选 | 机制已验证（变量未设时报 `InvalidOperation` 且退出码为空 → 正确跳过；设上后 exit 0），但**没能端到端验证** —— 插件的 shell 继承 DSH 进程环境，开发会话里改不了它 |
| 触发器只有 `manual` | 无公网入口，事件驱动（issues/PR）与 cron 只能靠轮询，故不在清单里承诺 |
| MCP 命名空间 | 未单独起 MCP server，走 harness 的 dynamic Tool |
| `drill` 的人类评审环节 | 脚本只负责出材料；补丁质量由人判断，脚本不代替 |

---

## 五、排错

| 现象 | 原因 | 处理 |
|---|---|---|
| 「解释器找不到」 | `python` 不在 PATH 或路径写错 | 用 `python` 参数给绝对路径，或设 `REPO_AUTOPILOT_PYTHON` |
| 「解释器缺依赖」 | 选的解释器没装 `yaml`/`requests`/`numpy` | 换解释器，或 `<解释器> -m pip install pyyaml requests numpy` |
| 「repo_root 下找不到这个入口点」 | `repo_root` 给错（少一层/多一层） | 指向**含 `scripts/` 与 `tools/` 的那一层** |
| 「找不到能用的解释器」 | 三个候选都不满足依赖要求 | 设 `REPO_AUTOPILOT_PYTHON` |
| 「不可用：这个环境没有 shell 服务」 | 宿主没有 `shell` service | 该环境不支持本插件 |
| PowerShell `ParserError` | shell 不是 PowerShell 系 | 见 [4.4](#44-shell-与命令行拼接) 的已知限制 |
| `doctor` 报某项失败但模型/Ollama 正常 | 多半是解释器不对（详见 `NOTES.md`） | 先确认「解释器：」那一行指对了没有 |
| `compare` 报副本缺失 | 副本确实少了文件，或反向比对（工作副本当作源） | 以**正仓为源**比对；确认差异是谁改的 |

---

## 六、许可证

MIT，见 [`LICENSE`](LICENSE)。
