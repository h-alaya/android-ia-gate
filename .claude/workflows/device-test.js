export const meta = {
  name: 'device-test',
  description: 'Install and test an APK on several Android devices in parallel through the ia-gate MCP server, then consolidate the verdicts',
  whenToUse: 'args: { devices: ["VAL-042", ...], apk: "<path on gate PC>", package: "com.x", testPackage?: "com.x.test", testClass?: "...", scenario?: "free-text UI scenario" }',
  phases: [
    { title: 'Test', detail: 'one device-tester per device' },
  ],
}

const RESULT_SCHEMA = {
  type: 'object',
  properties: {
    device_id: { type: 'string' },
    model: { type: 'string' },
    android_version: { type: 'string' },
    verdict: { type: 'string', enum: ['pass', 'fail', 'blocked'] },
    reason: { type: 'string' },
    installed: { type: 'boolean' },
    launched: { type: 'boolean' },
    crash_evidence: { type: 'string', description: 'relevant log lines, or "none observed"' },
    tests_passed: { type: 'integer' },
    tests_failed: { type: 'integer' },
    failures: { type: 'array', items: { type: 'string' } },
  },
  required: ['device_id', 'verdict', 'reason', 'installed', 'launched', 'crash_evidence'],
}

const a = args || {}
if (!Array.isArray(a.devices) || !a.devices.length || !a.apk || !a.package) {
  throw new Error('args must include devices (array of gate IDs), apk and package')
}

const brief = (id) =>
  [
    `Target device: ${id}.`,
    `APK (path on the gate PC): ${a.apk}.`,
    `Package: ${a.package}.`,
    a.testPackage ? `Instrumented test package: ${a.testPackage}${a.testClass ? `, class filter ${a.testClass}` : ''}.` : 'No instrumented tests.',
    a.scenario ? `UI scenario to drive: ${a.scenario}` : 'No UI scenario: install, launch, check for crashes.',
  ].join('\n')

phase('Test')
const results = (
  await parallel(
    a.devices.map((id) => () =>
      agent(brief(id), { label: `test:${id}`, phase: 'Test', agentType: 'device-tester', schema: RESULT_SCHEMA }),
    ),
  )
).map((r, i) => r || { device_id: a.devices[i], verdict: 'blocked', reason: 'agent failed', installed: false, launched: false, crash_evidence: 'n/a' })

const count = (v) => results.filter((r) => r.verdict === v).length
log(`pass ${count('pass')} / fail ${count('fail')} / blocked ${count('blocked')}`)

return {
  summary: { pass: count('pass'), fail: count('fail'), blocked: count('blocked') },
  results,
}
