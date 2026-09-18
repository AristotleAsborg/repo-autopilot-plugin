# 工程记录（不属于展示材料）

展示用说明在 [`README.md`](README.md)；这里放**开发过程与实测证据**，
包括撞过的坑、真机输出原文和工具预算。**不写"创作目的与思路"以外的东西进 README**，
所以那部分内容留在本文件。

---

## 一、宿主静态契约：三条，都是 `apply()` 抛错换来的

`cordis_define` **收下了**三个版本，`apply()` 一跑全挂。三条各附报错原文，
并各有一条回归用例钉在 `tests/test_plugin_package.py` 末尾那节。

| # | 错法 | 宿主原话 |
|---|---|---|
| 1 | 参数根 `additionalProperties: false` | `harness.defineTool parameters.additionalProperties must be true or omitted because the implicit parameter root is open` |
| 2 | 逐属性 `required: true` | `harness.defineTool parameters.mode.required belongs to the containing raw object schema` |
| 3 | 裸拼 `"exe" "arg"` 当命令行 | PowerShell `ParserError`；只加单引号也不行；**`& 'exe' 'arg'` 才对** |

**这三条源码自检测不出** —— 15 个用例全绿的时候，这个包**一次都跑不起来**。
源码扫描测的是"代码长什么样"，真机测的是"宿主认不认"，**两件事**。

### 1.1 第 3 条最阴：适配器的故障会伪装成被适配者的故障

命令行拼错后 `doctor` 抛出的是 ParserError，插件据此报出「**自检有异常**」——
看起来像 **repo-autopilot 仓库**坏了，其实是**插件**把命令行拼错了。

后续的「误诊表」就是为这一类问题加的。

---

## 二、**猜出来的夹具会替代码圆谎**（本文件最值得记的一条）

第二轮加「误诊表」拦上面那类误诊。第一版**没修好，而且用例是绿的**：

* 我把 PowerShell 的文案**想象**成 `无法将“…”项识别为 cmdlet`；
* 真机报的是 `&: 术语 'xxx' 不会被识别为 cmdlet、函数、脚本文件或可执行程序的名称。`
* 而我的**用例喂给代码的正是我自己编的那句** —— 代码和用例基于**同一个错误假设**，
  **手拉手地一起错，用例全绿**。

> 用例的价值不在于"它通过了"，而在于"**它的输入是不是真的**"。
> 夹具一旦是编的，测试就从"证据"退化成"复读机"。

现在的两条夹具是从真机输出里**原文抄下来的**，注释里标了出处：

```
&: 术语 'definitely-not-python' 不会被识别为 cmdlet、函数、脚本文件或可执行程序的名称。
请检查名称的拼写或验证路径是否正确(如果包含路径)，然后重试。
```

```
Traceback (most recent call last):
  File "D:\deepseek harness\repo-autopilot\tools\acceptance.py", line 41, in <module>
    import yaml
ModuleNotFoundError: No module named 'yaml'
```

**推论**：凡是对"外部系统怎么报错"做的假设，都必须先跑一次把原文抄下来。

---

## 三、同一个"注释/字符串被当成代码"的毛病，犯了**三次**

`tests/test_plugin_package.py` 里三次误报，都是"断言的对象要不是同一种东西"：

| 次 | 症状 | 修法 |
|---|---|---|
| 1 | 开头**注释块**被当成第一行代码 | 过滤 `//` 行 |
| 2 | 注释里写了 `required: true` 四个字，被当成真用了逐属性 required | `_strip_comments` |
| 3 | `quote('import ' + PROBE_MODULES)` 是**要交给解释器的命令**，被"不许 import"的子串检查误报 | 判据改成**语法**（`^\s*import\s`） |

**用中文写注释、又用源码扫描做断言，就会反复踩这个坑。**

---

## 四、第三轮：解释器可移植性

### 4.1 发现的真问题

本机默认的 `python`（`D:\PythonEnv\uv\cpython-3.12.13-...\python.exe`）**没装依赖**。
用它跑 `doctor`，插件报出：

```
[doctor] 自检有异常，见 summary（退出码不在预期集合内）
  "name": "本地小模型探活", "ok": false, "detail": "ModuleNotFoundError: No module named 'numpy'",
  "options": ["A. 我帮你启动 Ollama 并重试", ...]
```

**建议是错的** —— Ollama 好好的，是解释器不对。人照着 A 走会白折腾一场。
这已经是同一类误诊的**第三次**。

### 4.2 改法：不再"出错之后猜原因"，而是**开工前先探**

`resolvePython`：`REPO_AUTOPILOT_PYTHON`（环境变量）→ `python3` → `python`，
第一个能 `import yaml, requests` 的胜出；结果缓存，一次运行只探一次。
三个都不行就明确报「这是环境问题，不是仓库问题」。

`$env:...` 必须**原样**交给 pwsh 展开，不能加引号（加了就成字面量）——
所以候选带了 `raw` 标志。

**真机验证**：

| 输入 | 输出 |
|---|---|
| 不给 `python` | 「找不到能用的解释器（试过：环境变量 REPO_AUTOPILOT_PYTHON、python3、python，都没法 import yaml, requests）—— 这是环境问题，不是仓库问题」 |
| `python=<venv>` | 「七项自检全过 / 解释器：由 python 参数指定（未探测）」 |
| 变量未设时 `& $env:...` | `InvalidOperation`，`$LASTEXITCODE` 为**空**（所以探测正确地跳过它） |
| 变量设上后同一形式 | `exit 0` |

> ⚠️ **未在真机验证的一处**：`REPO_AUTOPILOT_PYTHON` 这条候选**没能端到端跑通** ——
> 插件的 shell 继承 DSH 进程的环境，而本会话里改不了它。
> 已验证的是**机制**（上面两行）与**候选被跳过时的行为**。**如实记录，不假装验过。**

---

## 五、干净机器冒烟脚本

`scripts/smoke.py`：**只用标准库**。

* 为什么不用 `subprocess`：沙箱禁止程序通过管道抓别的程序的输出（会 EPERM）。
  所以只检查"正在跑本脚本的那个解释器" —— 反而更诚实：报告的永远是你实际在用的那个。
* 为什么不能 import 三方库：否则"缺 yaml"会让脚本自己崩掉，而不是被报出来（有用例钉住）。

真机结果：

| 场景 | 结果 |
|---|---|
| venv 跑，仓库正确 | 10 条全 OK，**exit 0** |
| 裸 `python` 跑（缺依赖） | 3 条缺件，逐条给 `pip install` 补法，**exit 1** |
| `--repo-root` 给错 | 4 个入口点缺失，逐条标出绝对路径，**exit 1** |

测试里**不能用 pytest 的 `tmp_path`**：它建在系统临时目录，落在会话工作区**之外**，
沙箱直接拒绝（`PermissionError: [WinError 5]`）—— 已改成插件目录下自管的 `scratch` 夹具
（同一进程里建、同一进程里删，不跨运行残留）。

### 5.1 顺带撞出来的**环境 bug**：沙箱建的目录，用户删不掉

`tmp_path` 被拒之后，pytest 会在**当前目录**留下 `pytest-cache-files-*` 残渣。
那些目录的 ACL 属于**沙箱的受限主体**，普通用户：

* 列目录 → `UnauthorizedAccessException`
* `Remove-Item` / `cmd rmdir /s /q` → `Access denied`
* `attrib -r -s -h` → `Access denied`

**删都删不掉**。试过 `--basetemp=.cache/pytest-tmp`：**更糟** —— `--basetemp` 的设计前提就是
跨次运行复用，第二次运行要去列那个目录，同样被拒、直接把会话打挂，还**留下第三个删不掉的目录**。

结论：**本仓库不设 basetemp**（`pytest.ini` 里写明了原因），测试一律用 `scratch` 夹具。

> 这三处残渣（2 个 `pytest-cache-files-*` + 1 个 `.cache/pytest-tmp`）**留在插件目录里**。
> 它们**不会进公开仓库**（git 根本读不到，所以不会 stage），但也没法从普通权限清掉 ——
> 要清得用管理员权限 `takeown` + `icacls`。**如实记录，不假装工作区是干净的。**

这条更像 **harness 沙箱的问题**而不是本插件的问题：受限主体创建的目录应当可被工作区所有者回收。

---

## 六、真机验证汇总

```text
rauto-1/pkg-7 (run-7)  running
  doctor      → 七项自检全过（exit 0）
  drill       → 7 天演练台账合格，7/7 全 full、人类介入 0、对外写 0（exit 0）
  acceptance  → 验收步骤表可加载，0.1~8.2 全在（exit 0）
  compare     → 189 个包内文件哈希逐一相同，无合并风险（exit 0）
  不给 python → 明确报"环境问题"，不再伪装成仓库故障
  cordis_inspect_self 回读 Package 源码 ↔ host.js 逐行一致（无漂移）
tests: 27 passed
```

`rauto-1` 下共 7 个 Package（pkg-1~pkg-7），**前 3 个都是被宿主静态契约打掉的**。

---

## 七、工具预算（如实记）

| 阶段 | 工具调用 | 预算 |
|---|---|---|
| 第一轮（打包 + 撞契约） | 约 40 | 15 |
| 第二、三轮（误诊表 + 可移植性） | 约 45 | 15 |

**超了 3~5 倍。** 主要花在真机迭代上（7 个 Package）。
不修预算、不伪装；但那几次迭代是**必要的** —— 其中 4 次各自换回一条真实缺陷。
