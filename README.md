# repo-autopilot-plugin

把 [repo-autopilot](https://github.com/AristotleAsborg/repo-autopilot) 的**只读检查**注册成
DeepSeek Harness 的模型工具。**只读、零凭证、零写入。**

提供 1 个工具 `repo_autopilot_check`，4 种模式：`doctor` / `drill` / `acceptance` / `compare`。

---

## 一、安装

### 1.1 环境要求

| 需要什么 | 说明 | 没有会怎样 |
|---|---|---|
| Python **3.12+** | 要跑 repo-autopilot 的 `tools/` | 什么都跑不了 |
| Python 模块 `yaml` / `requests` / `numpy` | 同上 | 对应模式报「解释器缺依赖」 |
| **repo-autopilot 仓库** | 插件是**适配器**，本身不含业务代码 | 所有模式都跑不了 |
| `git` | 仅「本地保存」类功能需要 | 只读模式不受影响 |

repo-autopilot 仓库本身没有 `requirements.txt`，依赖装在用哪个解释器就跑哪个：

```bash
<解释器> -m pip install pyyaml requests numpy
```

### 1.2 冒烟自检（**装完先跑这个**）

```bash
python scripts/smoke.py --repo-root <repo-autopilot 的绝对路径>
```

逐条检查「解释器版本 / 三个依赖模块 / 仓库四个入口点 / git」，缺什么就写清补法。

```
  [OK] 解释器版本：3.12.13（D:\PythonEnv\venv\Scripts\python.exe）
  [OK] 模块 yaml：读 config/ 与 state/ 里的 YAML
  ...
  [缺] 模块 numpy：缺失（本地小模型档的向量运算）—— ModuleNotFoundError: No module named 'numpy'
        → 补法：D:\PythonEnv\venv\Scripts\python.exe -m pip install numpy

结论：**不能跑** —— 3 项缺件（上面逐条写了补法）。
```

退出码：**0 = 全过**，**1 = 有缺件**。脚本**只用标准库**，所以它能在「缺依赖」的机器上
把缺件报出来，而不是自己先崩掉。

### 1.3 放哪

目录结构：

```text
plugin-repo-autopilot/
├── plugin.yaml                     # 清单：入口、工具命名空间、触发器、偏差记录
├── host.js                         # Host 半边源码（其全文即 cordis_define 的 code.host）
├── scripts/smoke.py                # 干净机器冒烟自检（只用标准库）
├── tests/test_plugin_package.py    # 包自检 27 例（不需要 DSH 运行时）
├── README.md
└── LICENSE
```

### 1.4 注册成 dynamic Package

`host.js` 的**全部内容**就是喂给 `cordis_define({ code: { host: <本文件> } })` 的函数体 ——
普通 JavaScript，没有 TypeScript、没有 `import` / `require`、不依赖未声明的全局。
载入后用 `cordis_run` 激活即可。

包自检（不需要 DSH 运行时，任何装了 Python 的机器都能跑）：

```bash
python -m pytest tests -q -p no:cacheprovider
```

---

## 二、使用方法

### 2.1 参数

| 参数 | 必填 | 说明 |
|---|---|---|
| `mode` | 是 | `doctor` / `drill` / `acceptance` / `compare` |
| `repo_root` | 是 | repo-autopilot 仓库的**绝对路径** |
| `target` | `compare` 必填 | 要比对的副本目录 |
| `python` | 否 | 解释器路径；不填则自动探测（见 2.4） |

### 2.2 四种模式

| 模式 | 实际执行的命令 | 需要凭证 | 退出码语义 |
|---|---|---|---|
| `doctor` | `scripts/doctor.py --json` | 否 | `0` = 七项自检全过 |
| `drill` | `tools/daily_drill.py --summary` | 否 | `0` = 7 天台账合格；**`1` = 还没满 7 天，不是失败** |
| `acceptance` | `tools/acceptance.py --list` | 否 | `0` = 验收步骤表可加载 |
| `compare` | `tools/package.py compare <target>` | 否 | `0` = 副本一致；`1` = 有差异（合并前先问清是谁改的） |

**退出码语义被编码进插件**：repo-autopilot 的工具大量用退出码表达状态，
插件把「哪些退出码算正常」写在模式表里，并把结论渲染成人话，不让模型去猜。

### 2.3 输出

```
[doctor] 七项自检全过
解释器：由 python 参数指定（未探测）
$ & 'D:\PythonEnv\venv\Scripts\python.exe' 'scripts/doctor.py' '--json'
<命令输出的末尾 25 行>
```

四段：**结论**（人话）→ **用的哪个解释器、怎么来的** → **实际命令行**（可复制重跑）→ **输出尾部**。

### 2.4 解释器解析顺序

不填 `python` 时，按顺序探测，第一个能 `import yaml, requests` 的胜出：

1. 环境变量 `REPO_AUTOPILOT_PYTHON`
2. `python3`
3. `python`

探测结果**缓存**，同一次运行里只探一次。三个都不行时明确报「这是环境问题，不是仓库问题」，
并列出试过哪些 —— 不会把环境故障说成「仓库自检失败」。

指定解释器（推荐，最省事）：

```powershell
$env:REPO_AUTOPILOT_PYTHON = 'D:\PythonEnv\venv\Scripts\python.exe'
```

### 2.5 故障分类

插件会先把「**谁的错**」分清楚，再报告：

| 症状 | 插件的话 |
|---|---|
| 解释器不存在 | 解释器找不到（是环境的问题，不是仓库的问题） |
| 解释器缺依赖 | 解释器缺依赖（是环境的问题，不是仓库的问题） |
| `repo_root` 给错 | repo_root 下找不到这个入口点（多半是路径给错了，不是仓库坏了） |

---

## 三、技术路线

### 3.1 形态

* **Host 半边**（Node 侧）：注册模型工具，通过 `shell` 服务起子进程调 Python。
* **无客户端**：没有浏览器 UI。第一版只做 Host。
* **适配器，不是分叉**：不复制 repo-autopilot 的任何业务逻辑，只调用它**已有**的只读入口点。
  对话指令与插件两条入口共享同一套实现。

### 3.2 与宿主的三条静态契约

`harness.defineTool` 的参数 schema 有几条硬性要求（都是实测撞出来的，已写成回归用例）：

1. 参数**根**不能写 `additionalProperties: false` —— 宿主把它当隐式开放对象；
2. 必填项要写成**根级数组** `required: [...]`，逐属性 `required: true` 会被拒；
3. `shell` 的 `command` 是**命令行**不是 argv —— Windows 上用 `& 'exe' 'arg'`，
   引号开头的 token 会被 PowerShell 当表达式（`ParserError`）。

### 3.3 生命周期

工具注册挂在当前 Fiber 上（`ctx.effect` + `harness.registerTool`），
`stop` / `update` / `undefine` 时自动移除，不留残留。
`execute` 转发 `exec.signal`，取消能传导到子进程。

`shell` 用 `ctx.get` 读取并做缺席判断：没有 shell 的环境里插件会**显式报告自己不可用**，
而不是静默地注册不出工具。

### 3.4 安全边界

* 四种模式**全是只读**：不建仓库、不评论、不打标签、不开 PR、不推送；
* **不读取任何凭证**：源码里没有 `process.env` / `environ` / `authorization` / `.write_token`
  这类读取路径（有用例钉住）；
* 判据是「**拿不到**凭证」，不是「没提到这个词」。

### 3.5 测试

```bash
python -m pytest tests -q -p no:cacheprovider     # 27 passed
```

覆盖：清单字段与语义、`host.js` 的代码约束（无 `import` / `require` / 未声明全局）、
清单↔实现一致（模式集合、退出码语义）、三条宿主契约、误诊表**行为**测试
（把正则抠出来喂真机抓下来的报错原文）、以及冒烟脚本的正面与负面路径。

### 3.6 已知限制（不隐藏）

| 限制 | 说明 |
|---|---|
| shell 必须是 **PowerShell 系** | 命令行用 `&` 调用符；bash 的 `&` 是后台运算符，语义不同 |
| 需要本机有 Python + 依赖 | 尚未做自包含打包或纯 Node 降级 |
| `drill` 的人类评审环节 | 脚本只出材料，补丁质量由人判断 |
| 触发器只有 `manual` | 无公网入口，事件驱动只能靠轮询 |

---

## 四、许可证

MIT，见 [`LICENSE`](LICENSE)。
