// repo-autopilot 插件 · Host 半边（Cordis dynamic Package 源码）
//
// 这个文件的**全部内容**就是喂给 `cordis_define({ code: { host: <本文件> } })` 的函数体：
// 它是一个返回 Cordis Plugin 的普通函数体，只使用 Inspect 确认过的 Builtin
// （`ctx` / `harness` / `console`），不 import、不 require、不用任何未声明的全局。
//
// 设计原则（与 ROADMAP 7.3 那条一致）：**插件是适配器，不是分叉** ——
// 它只调用仓库里已经存在的只读入口点，不复制任何业务逻辑。
// 默认能力零凭证：这里四个模式全是只读的，都不需要 GitHub token。
return {
  apply(ctx) {
    // `shell` 是这个插件的**硬依赖**：它的全部工作就是"在给定仓库根上跑一条只读命令"。
    // 但这里仍用 ctx.get + 缺席时给出可读错误（而不是 inject 进等待态）——
    // 理由：插件在少 shell 的环境里应当**显式报告自己不可用**，而不是静默地注册不出工具。
    const shell = ctx.get('shell')

    // 模式表：一条模式 = 一条只读命令 + 它的退出码语义。
    // 退出码语义写在这里而不是交给模型猜 —— 这套系统的工具**大量用退出码表达状态**
    // （例如 daily_drill --summary 的 1 是"还没满 7 天"，不是失败）。
    const MODES = {
      doctor: {
        args: ['scripts/doctor.py', '--json'],
        ok: [0],
        verdict: (code) => (code === 0 ? '七项自检全过' : '自检有异常，见 summary'),
        timeoutMs: 120000,
      },
      drill: {
        args: ['tools/daily_drill.py', '--summary'],
        ok: [0, 1],
        verdict: (code) =>
          code === 0 ? '7 天演练台账合格（仍需人类评审补丁质量）' : '演练进行中或某天不合格',
        timeoutMs: 120000,
      },
      acceptance: {
        args: ['tools/acceptance.py', '--list'],
        ok: [0],
        verdict: (code) => (code === 0 ? '验收步骤表可加载' : '验收表加载失败'),
        timeoutMs: 120000,
      },
      compare: {
        args: ['tools/package.py', 'compare', '{target}'],
        ok: [0, 1],
        verdict: (code) => (code === 0 ? '副本与源仓库一致，没有合并风险' : '副本有差异，合并前先问清是谁改的'),
        timeoutMs: 180000,
        needsTarget: true,
      },
    }

    const text = (value, fallback) => (typeof value === 'string' && value.length > 0 ? value : fallback)

    // 自包含：安装脚本会把这里填成**随插件一起打包的那份 repo-autopilot** 的绝对路径。
    //
    // 为什么必须"写进来"而不是运行时自己找：Host 半边拿不到自己的磁盘位置 ——
    // 没有 fs、没有 __dirname、没有 process。发布出去的 host.js 里它是空串，
    // `python scripts/install.py --emit-host` 会生成一份填好路径的 `host.local.js`，
    // 注册那一份即可。传了 repo_root 参数时**以参数为准**（可指向任何外部检出）。
    const DEFAULT_REPO_ROOT = ''

    // 命令行要用 pwsh 的调用符 + 单引号（理由见 runMode 里的注释）。
    // 提到这个作用域，因为"探测解释器"和"跑真正的命令"都要用它。
    const quote = (item) => "'" + String(item).split("'").join("''") + "'"

    // ---- 解释器解析：**只探一次，之后记住** ----
    //
    // 为什么要有这一段：本机默认的 `python` 指向一个**没装依赖**的解释器，
    // 于是 doctor 报出「本地小模型探活 失败」并建议「A. 我帮你启动 Ollama 并重试」——
    // **建议是错的**：Ollama 好好的，是解释器不对。人照着 A 走会白折腾一场。
    // 这已经是同一类误诊的第三次（前两次：命令行拼错、误诊正则的文案猜错）。
    // 所以不再"出错之后猜原因"，而是**开工前先把解释器探明白**。
    const PROBE_MODULES = 'yaml, requests'
    const PY_CANDIDATES = [
      { expr: '$env:REPO_AUTOPILOT_PYTHON', raw: true, label: '环境变量 REPO_AUTOPILOT_PYTHON' },
      { expr: 'python3', raw: false, label: 'python3' },
      { expr: 'python', raw: false, label: 'python' },
    ]

    let probedPython = null

    async function resolvePython(explicit, root, signal) {
      if (explicit !== '') {
        return { path: explicit, raw: false, origin: '由 python 参数指定（未探测）' }
      }
      if (probedPython !== null) {
        return probedPython
      }
      const tried = []
      for (let index = 0; index < PY_CANDIDATES.length; index += 1) {
        const candidate = PY_CANDIDATES[index]
        // `$env:...` 要**原样**交给 pwsh 展开，不能加引号（加了就变成字面量）。
        const target = candidate.raw ? candidate.expr : quote(candidate.expr)
        const probe = await shell.run(
          shell.resolve({
            command: '& ' + target + ' -c ' + quote('import ' + PROBE_MODULES),
            workdir: root,
            timeoutMs: 30000,
            stdoutMaxBytes: 4000,
            signal: signal,
          }),
        )
        if (probe.exitCode === 0) {
          probedPython = {
            path: candidate.expr,
            raw: candidate.raw,
            origin: candidate.label + '（能 import ' + PROBE_MODULES + '）',
          }
          return probedPython
        }
        tried.push(candidate.label)
      }
      probedPython = { path: null, raw: false, origin: '自动探测失败', tried: tried }
      return probedPython
    }

    // 误诊表：**"谁坏了"要先分清**。
    // 第一轮吃过这个亏 —— 插件把命令行拼错，doctor 抛 ParserError，
    // 插件据此报出「自检有异常」，看起来像**仓库**坏了。适配器的故障会伪装成被适配者的故障。
    // 这几条一旦命中，就**不许**再把责任推给仓库。
    const MISDIAGNOSIS = [
      {
        // 真实文案是**跑出来的**，不是想出来的：
        //   `&: 术语 'definitely-not-python' 不会被识别为 cmdlet、函数、脚本文件或可执行程序的名称。`
        // 第一版正则写的是 `无法将.*识别为` —— 那是**猜的**，真机根本不这么报，
        // 于是坏解释器仍被误报成「自检有异常」。**猜出来的夹具会替代码圆谎。**
        match: /CommandNotFoundException|无法将.*识别为|不会被识别为|is not recognized/i,
        verdict:
          '解释器找不到（是环境的问题，不是仓库的问题）—— 用 python 参数给出绝对路径，' +
          '或设环境变量 REPO_AUTOPILOT_PYTHON；例如 D:\\PythonEnv\\venv\\Scripts\\python.exe',
      },
      {
        // 夹具原文抄自 2026-09-18 真机（用裸 `python` 跑 tools/acceptance.py）：
        //   Traceback (most recent call last):
        //     File "...\\tools\\acceptance.py", line 41, in <module>
        //       import yaml
        //   ModuleNotFoundError: No module named 'yaml'
        match: /ModuleNotFoundError|ImportError|No module named/i,
        verdict:
          '解释器缺依赖（是环境的问题，不是仓库的问题）—— 用 python 参数或环境变量 ' +
          'REPO_AUTOPILOT_PYTHON 指向装了依赖的解释器',
      },
      {
        match: /can't open file|No such file or directory|找不到路径/i,
        verdict: 'repo_root 下找不到这个入口点（多半是路径给错了，不是仓库坏了）',
      },
    ]

    function diagnose(stderr) {
      for (let index = 0; index < MISDIAGNOSIS.length; index += 1) {
        if (MISDIAGNOSIS[index].match.test(stderr)) {
          return MISDIAGNOSIS[index].verdict
        }
      }
      return null
    }

    async function runMode(args, signal) {
      if (shell === undefined) {
        return { exit: -1, command: '', verdict: '不可用：这个环境没有 shell 服务', tail: '' }
      }
      const mode = MODES[args.mode]
      if (mode === undefined) {
        return { exit: -2, command: '', verdict: '未知模式：' + String(args.mode), tail: '' }
      }
      // repo_root 可选：优先用参数，其次用安装时写进来的自带副本路径。
      const root = text(args.repo_root, '') !== '' ? args.repo_root : DEFAULT_REPO_ROOT
      if (root === '') {
        return {
          exit: -2,
          command: '',
          verdict:
            '没有 repo_root，且这份 host.js 里也没有内置路径。两种修法：' +
            '① 调用时传 repo_root 参数；' +
            '② 在插件目录下跑 python scripts/install.py --emit-host，' +
            '它会生成把自带副本路径写好的 host.local.js，注册那一份即可',
          tail: '',
        }
      }
      if (mode.needsTarget === true && text(args.target, '') === '') {
        return { exit: -2, command: '', verdict: 'compare 模式需要 target（要比对的副本目录）', tail: '' }
      }
      const explicit = text(args.python, '')
      // 先探解释器，再跑命令 —— 顺序反了就会产生"解释器不对、却怪仓库"的误诊。
      const resolved = await resolvePython(explicit, root, signal)
      if (resolved.path === null) {
        return {
          exit: -3,
          command: '',
          verdict:
            '找不到能用的解释器（试过：' +
            resolved.tried.join('、') +
            '，都没法 import ' +
            PROBE_MODULES +
            '）—— 这是环境问题，不是仓库问题。' +
            '请用 python 参数指向装了依赖的解释器，或设环境变量 REPO_AUTOPILOT_PYTHON',
          tail: '',
        }
      }
      const argv = mode.args.map((item) => (item === '{target}' ? text(args.target, '') : item))
      // `ShellExecRequest.command` 是一条**命令行**（宿主在 Windows 上是 pwsh -Command），不是 argv。
      // 三个实测结论都写在这儿，免得下次又踩：
      //   1. 裸拼 `"exe" "arg"` → ParserError（引号开头的 token 被当成表达式）；
      //   2. 只把每个 token 用单引号包起来、不加调用符 → 同样 ParserError；
      //   3. `& 'exe' 'arg'` 才是对的（本机 doctor 七个自检全过、exit 0）。
      // 单引号在 pwsh 里是字面量，内部单引号按 pwsh 规矩翻倍转义。
      // 已知限制（未隐藏）：`&` 是 PowerShell 的调用符，bash 系 shell 语义不同，此时本插件不可用。
      const launcher = resolved.raw ? resolved.path : quote(resolved.path)
      const command = '& ' + [launcher, ...argv.map(quote)].join(' ')
      const spec = shell.resolve({
        command: command,
        workdir: root,
        timeoutMs: mode.timeoutMs,
        stdoutMaxBytes: 40000,
        signal: signal,
      })
      const result = await shell.run(spec)
      const combined = result.stdout.text + (result.stderr.text ? '\n' + result.stderr.text : '')
      const tail = combined.trim().split('\n').slice(-25).join('\n')
      const flag = mode.ok.includes(result.exitCode) ? true : false
      // 先看是不是"我们这边"的毛病，再谈仓库。
      const misdiagnosis = diagnose(result.stderr.text)
      const verdict =
        misdiagnosis === null
          ? mode.verdict(result.exitCode) + (flag === true ? '' : '（退出码不在预期集合内）')
          : misdiagnosis + '（退出码 ' + String(result.exitCode) + '）'
      return {
        exit: result.exitCode === null ? -1 : result.exitCode,
        command: command,
        interpreter: resolved.origin,
        verdict: verdict,
        tail: tail,
      }
    }

    const tool = harness.defineTool({
      name: 'repo_autopilot_check',
      description:
        '在给定的 repo-autopilot 仓库根上跑一条**只读**检查，并把退出码语义一并告诉你。' +
        '模式：doctor（七项自检）/ drill（7 天演练台账）/ acceptance（验收步骤表）/ compare（副本哈希比对）。' +
        '不写任何文件、不需要任何 token。',
      // 参数根由宿主隐式当成**开放对象**，所以这里**不能**写 `additionalProperties: false`
      // —— 写了会在 apply() 阶段直接抛错（本机实测：pkg-1 就是这么挂的）。
      parameters: {
        type: 'object',
        // 必填项写成**根级数组**：逐属性写 `required: true` 会被宿主拒绝
        // （本机实测报错："parameters.mode.required belongs to the containing raw object schema"）。
        required: ['mode'],
        properties: {
          mode: {
            type: 'string',
            enum: ['doctor', 'drill', 'acceptance', 'compare'],
            description: '要跑的只读检查。',
          },
          repo_root: {
            type: 'string',
            description:
              'repo-autopilot 仓库的绝对路径。不填则用随插件打包的那份自带副本' +
              '（需要先跑 install.py --emit-host 生成 host.local.js）。',
          },
          target: {
            type: 'string',
            description: 'compare 模式要比对的副本目录（其它模式忽略）。',
          },
          python: {
            type: 'string',
            description: '解释器绝对路径；默认 `python`（本机常用 D:\\PythonEnv\\venv\\Scripts\\python.exe）。',
          },
        },
      },
      output: {
        schema: {
          type: 'object',
          additionalProperties: false,
          properties: {
            exit: { type: 'integer' },
            command: { type: 'string' },
            interpreter: { type: 'string' },
            verdict: { type: 'string' },
            tail: { type: 'string' },
          },
        },
        render: (args, value) => [
          {
            type: 'text',
            text:
              '[' +
              String(args.mode) +
              '] ' +
              String(value.verdict) +
              // 解释器是谁、怎么来的，一并说清楚 —— 环境问题的排查成本主要在这上面。
              (value.interpreter ? '\n解释器：' + String(value.interpreter) : '') +
              '\n$ ' +
              String(value.command) +
              '\n' +
              String(value.tail),
          },
        ],
      },
      async execute(args, exec) {
        // 契约要求：异步工作必须**观察或转发** `exec.signal`（取消要能传导到子进程）。
        const signal = exec && exec.signal ? exec.signal : undefined
        return await runMode(args, signal)
      },
    })

    // 工具注册属于当前 Fiber：stop / update 时自动移除。
    ctx.effect(() => harness.registerTool(ctx, tool), 'repo-autopilot tool')
  },
}
