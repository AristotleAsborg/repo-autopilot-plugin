// Load the **package entry** (`lib/index.js`) the way the profile loader does,
// and run `apply()` against a fake Cordis context.
//
// Why this file exists
// --------------------
// On 2026-09-18 the harness would not start at all:
//
//     Error: dsh: plugin tree failed to load: failed to apply loader entry
//     repo-autopilot-plugin (repo-autopilot-plugin): harness is not defined
//         at Object.apply [as callback] (.../repo-autopilot-plugin/lib/index.js:242:18)
//
// `harness` is a dynamic-Package *sandbox builtin*; the ESM entry has no
// sandbox, so the generated module must supply it (see scripts/build_module.py).
// The old suite could not catch this: every test either scanned host.js as text
// or exercised the sandbox contract. Nobody had ever *loaded* lib/index.js.
//
// This script is that missing test. It needs `@deepseek-ai/dsh-tools` to be
// resolvable, so it is meant to run from inside a dsh profile tree (run it from
// the installed package, or via tests/test_package_entry.py which stages it).
//
// Usage:  node package_load_smoke.mjs [ ./lib/index.js ]
// Exit:   0 = loaded and registered, 1 = failed (prints why)

const modulePath = process.argv[2] || './lib/index.js'

// 绝对 Windows 路径不能直接交给 `import()` —— 它会被当成 scheme 为 `d:` 的 URL
// （`ERR_UNSUPPORTED_ESM_URL_SCHEME`，实测）。而 `readFileSync` 要的恰恰是路径本身。
// 所以两个用法分开：import 用 file:// URL，读文件用原路径。
const { pathToFileURL } = await import('node:url')
const moduleSpecifier = /^[a-zA-Z]:[\\/]/.test(modulePath)
  ? pathToFileURL(modulePath).href
  : modulePath

let failures = 0
function check(ok, label, detail) {
  console.log((ok ? '  ok   ' : '  FAIL ') + label + (detail === undefined ? '' : ' :: ' + detail))
  if (!ok) failures += 1
  return ok
}

const module_ = await import(moduleSpecifier)

// 1. The export surface the profile loader reads.
check(module_.name === 'repo-autopilot', 'exports name = repo-autopilot', module_.name)
check(typeof module_.apply === 'function', 'exports apply()')
check(
  Array.isArray(module_.inject) && module_.inject.includes('tools'),
  "exports inject including 'tools' (ctx.tools is a real Cordis service)",
  JSON.stringify(module_.inject),
)

// 2. A fake context with just the surface host.js uses.
const registered = []
const disposers = []
const ctx = {
  get: (name) => (name === 'shell' ? undefined : undefined),
  effect: (callback) => {
    const disposer = callback()
    disposers.push(disposer)
    return disposer
  },
  tools: {
    register: (definition) => {
      registered.push(definition)
      return () => {}
    },
  },
}

// 3. apply() must not throw — this is the exact call that killed boot.
let threw = null
try {
  module_.apply(ctx, {})
} catch (error) {
  threw = error
}
check(!threw, 'apply(ctx) did not throw', threw ? `${threw.name}: ${threw.message}` : undefined)
check(registered.length === 1, 'registered exactly one tool', String(registered.length))

const tool = registered[0]
if (tool) {
  check(tool.name === 'repo_autopilot_check', 'tool name', tool.name)
  check(typeof tool.description === 'string' && tool.description.length > 0, 'tool has a description')
  check(typeof tool.execute === 'function', 'tool has execute()')
  check(
    tool.output && typeof tool.output.render === 'function',
    'tool declares output { schema, render }',
  )
  const parameters = tool.parameters || {}
  const required = parameters.required
  check(
    Array.isArray(required) && required.length === 1 && required[0] === 'mode',
    'parameters require exactly [mode] after compilation',
    JSON.stringify(required),
  )
  const properties = parameters.properties || {}
  check(
    ['mode', 'repo_root', 'target', 'python'].every((key) => Object.hasOwn(properties, key)),
    'parameters declare mode/repo_root/target/python',
    Object.keys(properties).join(','),
  )
  check(
    Array.isArray(properties.mode && properties.mode.enum) && properties.mode.enum.length === 4,
    "mode is an enum of the four read-only modes",
  )
}

// 4. 退出码语义 + 结论摘要 —— 用**假 shell 真调一次工具**。
//
// 为什么不能只扫源码：2026-09-20 实测报告① 的毛病就在"输出被 tail 切掉"，
// 而 tail 是**运行时**才拼出来的。源码里看着对，跑起来仍可能把不合格项切走。
// 所以这里造一个假 shell 返回指定的 stdout/exitCode，直接调 execute() 看结论。
const DOCTOR_JSON = JSON.stringify([
  { name: 'state 目录完整性', ok: true, detail: '12 个子目录齐全' },
  { name: 'mode.json 合法性', ok: true, detail: 'mode=online' },
  { name: '队列孤儿任务', ok: true, detail: '0 个任务在跑，无孤儿' },
  { name: '闸门悬挂审批', ok: true, detail: '0 张待批单' },
  { name: '写 token 文件', ok: false, detail: '不存在 state\\.write_token' },
  { name: '本地小模型探活', ok: true, detail: 'embedding 维度 1024' },
  { name: 'GitHub 连通性', ok: false, detail: '读 token 不可用：LookupError' },
])

function contextWithShell(exitCode, stdout, stderr) {
  const reg = []
  const fakeShell = {
    resolve: (spec) => spec,
    run: async (spec) => {
      // 解释器探测那条命令里有 `import`；统一放行，免得它干扰本场景。
      if (String(spec.command).includes('import ')) {
        return { exitCode: 0, stdout: { text: '' }, stderr: { text: '' } }
      }
      return {
        exitCode,
        stdout: { text: stdout },
        stderr: { text: stderr || '' },
      }
    },
  }
  return {
    reg,
    ctx: {
      get: (name) => (name === 'shell' ? fakeShell : undefined),
      effect: (callback) => callback(),
      tools: { register: (definition) => { reg.push(definition); return () => {} } },
    },
  }
}

const abnormal = contextWithShell(1, DOCTOR_JSON)
module_.apply(abnormal.ctx, {})
const doctorResult = await abnormal.reg[0].execute(
  { mode: 'doctor', repo_root: '.' },
  { signal: undefined },
)
check(
  !String(doctorResult.verdict).includes('不在预期集合内'),
  'doctor 退出码 1 不再被当成"未知码"',
  doctorResult.verdict,
)
check(
  String(doctorResult.verdict).includes('不合格 2 项'),
  '结论里带上了不合格项的条数',
  doctorResult.verdict,
)
check(
  String(doctorResult.verdict).includes('写 token 文件') &&
    String(doctorResult.verdict).includes('GitHub 连通性'),
  '结论里点名了是哪两项',
  doctorResult.verdict,
)

// 反例：真·未知退出码仍要提示，别把这一档一起吞掉。
const unknown = contextWithShell(2, '{}')
module_.apply(unknown.ctx, {})
const unknownResult = await unknown.reg[0].execute({ mode: 'doctor', repo_root: '.' }, {})
check(
  String(unknownResult.verdict).includes('不在预期集合内'),
  '退出码 2 仍然提示"不在预期集合内"',
  unknownResult.verdict,
)

// 反例：是**我们这边**拼错命令时，不许再谈"哪几项不合格"（误诊表优先）。
const misdiagnosed = contextWithShell(
  1,
  DOCTOR_JSON,
  "&: 术语 'definitely-not-python' 不会被识别为 cmdlet、函数、脚本文件或可执行程序的名称。",
)
module_.apply(misdiagnosed.ctx, {})
const misdiagnosedResult = await misdiagnosed.reg[0].execute({ mode: 'doctor', repo_root: '.' }, {})
check(
  !String(misdiagnosedResult.verdict).includes('不合格 2 项'),
  '命令拼错时不谈仓库的不合格项（误诊优先）',
  misdiagnosedResult.verdict,
)

// 5. **安装时烘入的解释器**：不传 python 参数时，只有它能 import 依赖 —— 就该选它。
//
// 这条治的是常态化摩擦（2026-09-20 报告②）：会话内改不了 REPO_AUTOPILOT_PYTHON，
// 于是每次调用都得手传 python。烘进去以后应当**不用传**。
// 只有模块被烘过（装进 profile 的那份）才有意义；仓库里那份是空串，跳过。
const { readFileSync } = await import('node:fs')
const moduleSource = readFileSync(modulePath, 'utf8')
const bakedMatch = moduleSource.match(/const DEFAULT_PYTHON = "((?:[^"\\]|\\.)*)"/)
// 源码里是**转义过**的 Windows 路径（`"D:\\PythonEnv\\..."`），必须反转义再拿去比对，
// 否则子串永远匹配不上、探测全失败（第一版就是这么误报的）。
const bakedPython = bakedMatch ? JSON.parse('"' + bakedMatch[1] + '"') : ''
if (!bakedPython) {
  console.log('  skip  未烘入解释器（仓库里的发布版就是这样）—— 跳过"不用传 python"这一项')
} else {
  const baked = contextWithShell(1, DOCTOR_JSON)
  const probes = []
  baked.ctx.get = (name) =>
    name === 'shell'
      ? {
          resolve: (spec) => spec,
          run: async (spec) => {
            const cmd = String(spec.command)
            if (cmd.includes('import ')) {
              probes.push(cmd)
              // 只放行烘入的那个：环境变量/python3/python 一律失败。
              const isBaked = cmd.includes(bakedPython)
              return { exitCode: isBaked ? 0 : 1, stdout: { text: '' }, stderr: { text: '' } }
            }
            return { exitCode: 1, stdout: { text: DOCTOR_JSON }, stderr: { text: '' } }
          },
        }
      : undefined
  module_.apply(baked.ctx, {})
  const bakedResult = await baked.reg[0].execute({ mode: 'doctor', repo_root: '.' }, {})
  check(
    String(bakedResult.interpreter).includes('烘入'),
    '不传 python 时选中了安装时烘入的解释器',
    String(bakedResult.interpreter),
  )
  // `interpreter` 报的是**来源标签**，不是路径 —— 所以"选中了那个路径"这件事
  // 只能从**探测命令**上看：烘入的那条必须真被探过（且只有它探测通过）。
  check(
    probes.some((cmd) => cmd.includes(bakedPython)),
    '探测里确实用了烘入的那个路径',
    bakedPython,
  )
}

console.log(failures === 0 ? 'PACKAGE LOAD: PASS' : `PACKAGE LOAD: FAIL (${failures})`)
process.exit(failures === 0 ? 0 : 1)
