export const meta = {
  name: 'implement-plan',
  description: 'Implement IA Gate plan tasks one by one: implement (TDD) -> spec + quality review in parallel -> fix loop -> final branch review',
  whenToUse: 'args: { plan: "docs/superpowers/plans/<file>.md", tasks: [1, 2, ...] } — tasks run in order because each consumes the previous ones\' interfaces',
  phases: [
    { title: 'Implement', detail: 'gate-implementer, one task at a time' },
    { title: 'Review', detail: 'spec-reviewer + gate-code-reviewer in parallel, up to 3 fix rounds' },
    { title: 'Final review', detail: 'whole-branch review' },
  ],
}

const MAX_FIX_ROUNDS = 3

const IMPL_SCHEMA = {
  type: 'object',
  properties: {
    status: { type: 'string', enum: ['done', 'blocked'] },
    base_sha: { type: 'string', description: 'HEAD before you started this task' },
    head_sha: { type: 'string', description: 'HEAD after your commit' },
    tests_summary: { type: 'string', description: 'final pytest summary line' },
    deviations: { type: 'string', description: 'deviations from the plan and why, or "none"' },
    blocker: { type: 'string', description: 'exact contradiction or problem when blocked' },
  },
  required: ['status', 'base_sha', 'head_sha', 'tests_summary', 'deviations'],
}

const REVIEW_SCHEMA = {
  type: 'object',
  properties: {
    approved: { type: 'boolean' },
    issues: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          severity: { type: 'string', enum: ['critical', 'important', 'minor'] },
          location: { type: 'string' },
          description: { type: 'string' },
          fix: { type: 'string' },
        },
        required: ['severity', 'location', 'description'],
      },
    },
  },
  required: ['approved', 'issues'],
}

const plan = args && args.plan
const tasks = (args && args.tasks) || []
if (!plan || !tasks.length) {
  throw new Error('args must be { plan: "<path>", tasks: [numbers] }')
}

const blocking = (reviews) =>
  reviews.filter(Boolean).flatMap((r) => r.issues.filter((i) => i.severity !== 'minor'))

const review = (n, base) =>
  parallel([
    () => agent(
      `Review Task ${n} of ${plan} for spec/plan compliance. Commits to review: ${base}..HEAD.`,
      { label: `spec-review:T${n}`, phase: 'Review', agentType: 'spec-reviewer', schema: REVIEW_SCHEMA },
    ),
    () => agent(
      `Review the code of Task ${n} of ${plan} for quality and security. Diff: git diff ${base}..HEAD.`,
      { label: `code-review:T${n}`, phase: 'Review', agentType: 'gate-code-reviewer', schema: REVIEW_SCHEMA },
    ),
  ])

const results = []
let firstBase = null

for (const n of tasks) {
  phase('Implement')
  const impl = await agent(
    `Implement Task ${n} of ${plan}. Report base_sha (HEAD before you start) and head_sha (after your commit).`,
    { label: `implement:T${n}`, phase: 'Implement', agentType: 'gate-implementer', schema: IMPL_SCHEMA },
  )
  if (!impl || impl.status === 'blocked') {
    log(`Task ${n} blocked: ${impl ? impl.blocker : 'implementer died'}`)
    results.push({ task: n, status: 'blocked', detail: impl })
    break
  }
  if (firstBase === null) firstBase = impl.base_sha

  phase('Review')
  let reviews = await review(n, impl.base_sha)
  let open = blocking(reviews)
  let round = 0
  while (open.length && round < MAX_FIX_ROUNDS) {
    round++
    log(`Task ${n}: ${open.length} blocking issue(s), fix round ${round}/${MAX_FIX_ROUNDS}`)
    await agent(
      `Fix these review findings on Task ${n} of ${plan}, then re-run the suite and commit:\n` +
        JSON.stringify(open, null, 2),
      { label: `fix:T${n}#${round}`, phase: 'Review', agentType: 'gate-implementer' },
    )
    reviews = await review(n, impl.base_sha)
    open = blocking(reviews)
  }
  if (open.length) {
    log(`Task ${n}: still ${open.length} blocking issue(s) after ${MAX_FIX_ROUNDS} rounds — stopping for a human`)
    results.push({ task: n, status: 'needs-human', open })
    break
  }
  results.push({
    task: n,
    status: 'done',
    tests: impl.tests_summary,
    deviations: impl.deviations,
    minor: reviews.filter(Boolean).flatMap((r) => r.issues),
  })
}

phase('Final review')
let finalReview = null
if (firstBase && results.every((r) => r.status === 'done')) {
  finalReview = await agent(
    `Whole-branch review of the IA Gate work: git diff ${firstBase}..HEAD against ${plan} and its spec. ` +
      `Look for cross-task inconsistencies (names, types, error handling) and anything the per-task reviews could not see.`,
    { label: 'final-review', phase: 'Final review', agentType: 'gate-code-reviewer', schema: REVIEW_SCHEMA },
  )
}

return { results, finalReview }
