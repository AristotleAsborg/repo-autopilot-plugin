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

let failures = 0
function check(ok, label, detail) {
  console.log((ok ? '  ok   ' : '  FAIL ') + label + (detail === undefined ? '' : ' :: ' + detail))
  if (!ok) failures += 1
  return ok
}

const module_ = await import(modulePath)

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

console.log(failures === 0 ? 'PACKAGE LOAD: PASS' : `PACKAGE LOAD: FAIL (${failures})`)
process.exit(failures === 0 ? 0 : 1)
