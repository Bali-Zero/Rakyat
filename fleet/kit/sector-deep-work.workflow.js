export const meta = {
  name: 'sector-deep-work',
  description: 'Deep work on one repo sector: unit map, multi-lens hunt until dry, adversarial verification, optional test-first fixes in isolated worktrees, independent review, ledger',
  whenToUse: 'On a fleet machine that owns one sector. args: { sector:{id,name,brief,known_leads,gate,toolchain_hint}, machine, role:"none"|"keeper_B"|"keeper_F", mode:"audit"|"fix", depth:"scout"|"standard"|"deep", kitDir, outDir, kitId, rules:[...], toolchainHint, limits:{...}, efforts:{...}, allow_hot:[paths], owner_decided:["path#symbol"], gateCmd }. Put a "+NM" output-token budget in the prompt to cap the run; the workflow then stops hunting cleanly and still writes its report.',
  phases: [
    { title: 'Preflight', detail: 'tree state, toolchain, in-flight work, unit plan' },
    { title: 'Seeds', detail: 'a verdict for every high or critical lead from reconnaissance' },
    { title: 'Map', detail: 'one reader per unit' },
    { title: 'Hunt', detail: 'one finder per unit and lens, repeated until dry' },
    { title: 'Verify', detail: 'independent skeptics per finding' },
    { title: 'Fix', detail: 'test-first fixes in isolated worktrees (fix mode only)' },
    { title: 'Review', detail: 'independent review of every fix branch (fix mode only)' },
    { title: 'Report', detail: 'ledger, PR drafts and run report' },
  ],
}

// ---------------------------------------------------------------------------------------------
// Arguments
// ---------------------------------------------------------------------------------------------
const A = args || {}
const S = A.sector || {}
// 'fix-pr' runs the same stages as 'fix'; pushing and opening the PR is done by the session after the run.
const MODE = (A.mode === 'fix' || A.mode === 'fix-pr') ? 'fix' : (A.mode === undefined || A.mode === 'audit') ? 'audit' : null
if (!MODE) throw new Error('args.mode must be audit, fix or fix-pr, got ' + A.mode)
const MACHINE = A.machine || 'unnamed'
const ROLE = (A.role === 'keeper_B' || A.role === 'keeper_F') ? A.role : 'none'
const KIT = A.kitDir
// Findings that need an owner decision (authorisation policy, statutory maths, deletions, anything made public) are
// reported, not fixed, unless the owner approved them at triage: args.owner_decided lists their anchors (path#symbol).
const OWNER_DECIDED = Array.isArray(A.owner_decided) ? A.owner_decided : []
// Hot files the owner approved for fixing at triage (exact repo-relative paths); each needs a merge-tree check first.
const ALLOW_HOT = Array.isArray(A.allow_hot) ? A.allow_hot : []
const OUT = A.outDir
if (!S.id || !KIT || !OUT) throw new Error('args.sector.id, args.kitDir and args.outDir are required')

const DEFAULT_LENSES = ['correctness', 'security', 'reliability', 'data-integrity', 'contract', 'tests']
// Skeptic votes follow severity: 3 for high and critical, 2 for medium, 1 for low (never below the preset's minVotes).
const SEV_VOTES = { critical: 3, high: 3, medium: 2, low: 1 }
// Depth presets. The binding constraints of a fleet are seat quota and CI capacity, not machines: a full-depth run on a
// large sector can cost tens of millions of tokens. 'scout' proves the machinery and skims; 'standard' is the default
// audit; 'deep' is for the money, auth and personal-data code once the standard ledger exists. args.limits overrides any key.
const DEPTH = {
  scout: { maxUnits: 4, lenses: ['correctness', 'security', 'tests'], dryRounds: 1, maxRounds: 1, minVotes: 1, maxFindings: 15, maxFixBatches: 2 },
  standard: { maxUnits: 8, lenses: ['correctness', 'security', 'reliability', 'data-integrity'], dryRounds: 1, maxRounds: 2, minVotes: 1, maxFindings: 40, maxFixBatches: 4 },
  deep: { maxUnits: 14, lenses: DEFAULT_LENSES, dryRounds: 2, maxRounds: 4, minVotes: 2, maxFindings: 100, maxFixBatches: 8 },
}
const DEPTH_NAME = DEPTH[A.depth] ? A.depth : 'standard'
const LIM = Object.assign({ fixMinSeverity: 'medium', batchSize: 4, maxReviewLoops: 2 }, DEPTH[DEPTH_NAME], A.limits || {})
if (A.limits && A.limits.votes && !A.limits.minVotes) LIM.minVotes = A.limits.votes
// Gate commands may name the kit as <kitDir>; every gate command runs through the kit's t wrapper (isolated stack only).
const fixKit = c => String(c).split('<kitDir>').join(KIT)
const T = KIT + '/t'
const SEV = { critical: 4, high: 3, medium: 2, low: 1 }
// Output-token budget guard. budget.total comes from a "+NM" directive in the prompt that started this run; without one
// the depth preset is the only limit. Estimates are deliberately generous so that the report always has room.
const COST = { hunter: 25000, skeptic: 12000, fixBatch: 160000, report: 60000 }
function room(n) { return !budget.total || budget.remaining() > n + COST.report }
// Reasoning effort per stage. Preflight and report are mechanical; hunting, verifying, fixing and reviewing get the depth.
// Override with args.efforts, for example { hunt: 'max', verify: 'max' }.
const EFF = Object.assign({ preflight: 'medium', map: 'high', hunt: 'xhigh', verify: 'xhigh', fix: 'xhigh', review: 'xhigh', report: 'low' }, A.efforts || {})

const LENS = {
  correctness: 'logic errors: wrong conditions, off-by-one, wrong defaults, unhandled None or undefined, state machines that can reach an illegal state, error paths that report success, twin implementations that have drifted apart',
  security: 'defensive review: missing or bypassable authorisation, object access by a client-supplied id without an ownership check, unauthenticated mutating routes, webhook authenticity and replay, injection (SQL, shell, path, prompt text reaching privileged tools), server-side request forgery, secrets or personal data in logs or responses. Describe the weakness and the fix; never write exploit code',
  reliability: 'failure behaviour: external calls without timeout or retry, swallowed exceptions, handlers that are not idempotent and can run twice, races between concurrent requests or workers, unbounded queues or loops, resource leaks, start-up order assumptions',
  'data-integrity': 'money, dates, time zones, rounding, currency and units; SQL correctness (joins, NULL handling, transactions, constraint assumptions); drift between migrations and models; cache invalidation; deduplication keys',
  contract: 'mismatches across a boundary: request or response shape versus its caller, generated types versus the server, configuration keys read but never set, enum values out of sync between layers, documented behaviour the code does not have',
  privacy: 'personal data (names, e-mail addresses, phone numbers, passport or ID numbers, document images, payroll) reaching logs, error messages, analytics, caches, URLs, third-party calls or a caller who should not see it; retention and deletion paths that leave copies behind',
  concurrency: 'two requests, workers or retries racing on the same row, file or external side effect: check-then-act without a lock or unique constraint, counters that are not atomic, double submits, retries that duplicate payments, e-mails or invoices, transactions that span an external call',
  'migration-safety': 'schema changes the previous release cannot tolerate (dropped or renamed columns, NOT NULL without a default), long locks on large tables, backfills inside a migration, code that assumes a column or table no migration creates, migrations that are not idempotent or that collide on a number',
  seam: 'flows that cross the frontend-backend boundary end to end (screen, API client, BFF route, router, service, table): fields that one side sends and the other ignores, routes called but not mounted, mirrored state machines or enums that drifted, generated types that no longer match',
  tests: 'tests that cannot fail (no assertion, asserting a mock, tautologies), important branches with no test, tests that pass for the wrong reason, fixtures that hide real behaviour. Name the precise missing test',
}

const VERIFY_LENSES = [
  { key: 'code-truth', brief: 'Re-derive the claim from the code alone. Read the cited lines and their callers. Is the control flow what the hunter says? Is there a guard, a default, a type, a database constraint or a wrapper that prevents the trigger?' },
  { key: 'reproduce', brief: 'Try to make it happen. Write the smallest test or snippet that demonstrates the defect under the run\'s tmp directory (never in the tracked tree), and run it through the kit\'s t wrapper. Set executed=true only if it really ran. If it cannot be exercised without services you do not have, say exactly what you would need and judge from the code.' },
  { key: 'production-reach', brief: 'Decide whether it matters in production. Is the code path reachable from a mounted route, a scheduled job or a worker in the deployed configuration? Is it behind a flag that is off, dead code, or tooling that is absent from the deployed image? What is the realistic blast radius?' },
]

// ---------------------------------------------------------------------------------------------
// Shared prompt text
// ---------------------------------------------------------------------------------------------
const RULES = (A.rules || []).map((r, i) => (i + 1) + '. ' + r).join('\n')
const COMMON = [
  'You work on ONE sector of the git repository in the current working directory, as part of a fleet in which every machine owns a different sector.',
  'Sector: ' + S.id + ' — ' + (S.name || '') + '. Machine: ' + MACHINE + '. Mode: ' + MODE + '.',
  'Ownership: ' + KIT + '/sectors.json defines which paths each sector owns. To check one path run: python3 ' + KIT + '/resolve_sectors.py --repo . --owner <path>. The file list of this sector is at ' + OUT + '/files.txt once preflight has run.',
  'Rules of engagement: ' + KIT + '/RULES.md. Read that file before your first action and follow it literally.',
  A.toolchainHint ? 'Toolchain on this machine: ' + A.toolchainHint : '',
  S.toolchain_hint ? 'Running tests: ' + fixKit(S.toolchain_hint) : '',
  S.brief ? 'SECTOR BRIEF FROM RECONNAISSANCE\n' + S.brief : '',
  'RULES THAT ALWAYS APPLY',
  '- Never push, merge, deploy, publish, or message anyone. Never print or commit secrets or personal data.',
  '- Never run a whole test suite: targeted tests only. Do not add -q to a pytest command line (the repository guard refuses it).',
  '- Every test, migrate or gate command runs through ' + T + ", one command per call: " + T + " '<command>'. It points every database and Redis variable at the isolated stack (Postgres localhost:5433, Redis localhost:6380) and refuses to run otherwise. Never run an ops, deploy or production script, and never call a production service.",
  '- Scratch files (probes, repro tests, notes) go under ' + OUT + '/tmp, never in the tracked tree.',
  MODE === 'audit'
    ? '- This is an AUDIT run: the tracked tree is read-only. Write only under ' + OUT + ' or a temporary directory.'
    : '- Tracked files may be changed only by the Fix stage, only inside its isolated worktree, and only on a branch named agent/' + MACHINE + '/' + S.id + '/<topic>. Everything else writes only under ' + OUT + ' or a temporary directory.',
  RULES ? 'REPOSITORY RULES OF ENGAGEMENT\n' + RULES : '',
].filter(Boolean).join('\n')

// ---------------------------------------------------------------------------------------------
// Schemas
// ---------------------------------------------------------------------------------------------
const PREFLIGHT_SCHEMA = {
  type: 'object',
  properties: {
    head_sha: { type: 'string' },
    main_sha: { type: 'string' },
    tree_clean: { type: 'boolean' },
    head_on_main: { type: 'boolean', description: 'git merge-base --is-ancestor HEAD origin/main succeeds' },
    db_safe: { type: 'boolean', description: 'false when a fresh shell points DATABASE_URL anywhere but localhost:5433' },
    toolchain_ok: { type: 'boolean' },
    toolchain_notes: { type: 'string' },
    hot_files: { type: 'array', items: { type: 'string' }, description: 'files of this sector that open work is already changing' },
    pr_list_source: { type: 'string', enum: ['gh', 'file', 'none'], description: 'where the open pull-request list came from' },
    units: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          id: { type: 'string', description: 'short kebab-case id' },
          name: { type: 'string' },
          paths: { type: 'array', items: { type: 'string' } },
          test_paths: { type: 'array', items: { type: 'string' } },
          source_loc: { type: 'integer' },
          notes: { type: 'string' },
        },
        required: ['id', 'name', 'paths', 'test_paths', 'source_loc', 'notes'],
      },
    },
    gate_commands: { type: 'array', items: { type: 'string' } },
    warnings: { type: 'array', items: { type: 'string' } },
  },
  required: ['head_sha', 'main_sha', 'tree_clean', 'head_on_main', 'db_safe', 'toolchain_ok', 'toolchain_notes', 'hot_files', 'pr_list_source', 'units', 'gate_commands', 'warnings'],
}

const MAP_SCHEMA = {
  type: 'object',
  properties: {
    summary: { type: 'string' },
    flows: { type: 'array', items: { type: 'string' } },
    invariants: { type: 'array', items: { type: 'string' } },
    risk_areas: { type: 'array', items: { type: 'object', properties: { path: { type: 'string' }, why: { type: 'string' } }, required: ['path', 'why'] } },
    untested: { type: 'array', items: { type: 'string' } },
    map_path: { type: 'string' },
  },
  required: ['summary', 'flows', 'invariants', 'risk_areas', 'untested', 'map_path'],
}

const FINDINGS_SCHEMA = {
  type: 'object',
  properties: {
    findings: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          title: { type: 'string' },
          severity: { type: 'string', enum: ['critical', 'high', 'medium', 'low'] },
          path: { type: 'string' },
          line: { type: 'integer' },
          symbol: { type: 'string', description: 'enclosing function, method or class; line numbers drift' },
          snippet: { type: 'string', description: 'the 1 to 3 cited lines, verbatim: the finding is re-anchored by path, symbol and this text' },
          needs: { type: 'array', items: { type: 'string', enum: ['postgres', 'redis', 'qdrant', 'ollama', 'node', 'mac', 'vendor-sandbox'] }, description: 'services a proof of this finding needs; empty when plain code and unit tests suffice' },
          owner_decision: { type: 'boolean', description: 'true when the fix changes authorisation policy, statutory or money maths, deletes data, or makes something more public: the owner decides' },
          evidence: { type: 'string', description: 'what the code at path:line says' },
          trigger: { type: 'string', description: 'the concrete input or sequence that makes it go wrong, and what goes wrong' },
          suggested_fix: { type: 'string' },
          confidence: { type: 'number' },
        },
        required: ['title', 'severity', 'path', 'line', 'symbol', 'snippet', 'needs', 'owner_decision', 'evidence', 'trigger', 'suggested_fix', 'confidence'],
      },
    },
  },
  required: ['findings'],
}

const VERDICT_SCHEMA = {
  type: 'object',
  properties: {
    real: { type: 'boolean' },
    severity: { type: 'string', enum: ['critical', 'high', 'medium', 'low', 'none'] },
    confidence: { type: 'number' },
    note: { type: 'string' },
    repro: { type: 'string', description: 'what you ran and what it printed, or why it could not be run' },
    executed: { type: 'boolean', description: 'true only if you actually ran a test or snippet and its output is in repro' },
  },
  required: ['real', 'severity', 'confidence', 'note', 'repro', 'executed'],
}

const FIX_SCHEMA = {
  type: 'object',
  properties: {
    branch: { type: 'string' },
    commits: { type: 'array', items: { type: 'string' } },
    files_changed: { type: 'array', items: { type: 'string' } },
    tests_added: { type: 'array', items: { type: 'string' } },
    resolved: { type: 'array', items: { type: 'string' }, description: 'finding ids fixed on the branch' },
    unresolved: { type: 'array', items: { type: 'string' }, description: 'finding id, then why it was not fixed and who must act' },
    gate_passed: { type: 'boolean' },
    gate_evidence: { type: 'string', description: 'the commands run and their tally lines' },
    out_of_scope_edits: { type: 'array', items: { type: 'string' } },
    notes: { type: 'string' },
  },
  required: ['branch', 'commits', 'files_changed', 'tests_added', 'resolved', 'unresolved', 'gate_passed', 'gate_evidence', 'out_of_scope_edits', 'notes'],
}

const REVIEW_SCHEMA = {
  type: 'object',
  properties: {
    verdict: { type: 'string', enum: ['approve', 'changes'] },
    issues: {
      type: 'array',
      items: {
        type: 'object',
        properties: { severity: { type: 'string', enum: ['blocker', 'major', 'minor'] }, path: { type: 'string' }, line: { type: 'integer' }, problem: { type: 'string' } },
        required: ['severity', 'path', 'line', 'problem'],
      },
    },
    evidence: { type: 'string', description: 'what you ran (tests on base and on branch, scope check) and the results' },
  },
  required: ['verdict', 'issues', 'evidence'],
}

const SEED_SCHEMA = {
  type: 'object',
  properties: {
    seed: { type: 'string' },
    verdict: { type: 'string', enum: ['E2', 'refuted', 'blocked'] },
    severity: { type: 'string', enum: ['critical', 'high', 'medium', 'low', 'none'] },
    path: { type: 'string' },
    line: { type: 'integer' },
    symbol: { type: 'string' },
    evidence: { type: 'string', description: 'E2: the guilt test (path or command) and its red output; refuted: the code that rules it out; blocked: the missing service and the exact command to run once it exists' },
    blocked_on: { type: 'string', description: 'the missing service, or empty' },
  },
  required: ['seed', 'verdict', 'severity', 'path', 'line', 'symbol', 'evidence', 'blocked_on'],
}

const REPORT_SCHEMA = {
  type: 'object',
  properties: {
    report_path: { type: 'string' },
    report_markdown: { type: 'string', description: 'the full text of REPORT.md' },
    ledger_path: { type: 'string' },
    pr_drafts: { type: 'array', items: { type: 'string' } },
    leftover_worktrees: { type: 'array', items: { type: 'string' } },
    summary: { type: 'string' },
  },
  required: ['report_path', 'report_markdown', 'ledger_path', 'pr_drafts', 'leftover_worktrees', 'summary'],
}

// ---------------------------------------------------------------------------------------------
// Prompts
// ---------------------------------------------------------------------------------------------
function preflightPrompt() {
  return [
    COMMON,
    '',
    'YOU ARE THE PREFLIGHT OFFICER. Do these six steps in order and report facts, not hopes. This is a short mechanical stage: about 25 tool calls and 10 minutes at most. Do not audit the sector, do not explore beyond these steps, do not study this workflow: the later stages do the deep work.',
    '1. Tree: record git rev-parse HEAD and git status --porcelain (tree_clean is true only if it prints nothing). Check git remote get-url origin names the Bali-Zero/Rakyat repository, else add a warning. Run git fetch origin main, record the SHA of origin/main, and set head_on_main from git merge-base --is-ancestor HEAD origin/main. The run analyses the code as checked out: a dirty tree or a HEAD with commits that are not on origin/main means this is not a clean fleet worktree, and the run will stop. If HEAD is behind origin/main, say by how many commits.',
    '2. Files: run python3 ' + KIT + '/resolve_sectors.py --repo . --sector ' + S.id + ' --out ' + OUT + '/files.txt and create the directories ' + OUT + '/map, ' + OUT + '/findings and ' + OUT + '/pr.',
    '3. Database safety first: run bash -lc \'echo DATABASE_URL=$DATABASE_URL\' in a fresh shell. If it prints a database that is not on localhost port 5433 (the isolated test stack), set db_safe=false and say so: a test run could reach a development or production database. Every test, migrate or gate command later runs through ' + T + ' (' + T + ' \'<command>\'), which exports the isolated environment and refuses any other database: prove it once with ' + T + ' true. Toolchain: check that what this sector needs is installed (for Python code a virtualenv built from apps/backend-rag/requirements.lock.txt; for TypeScript code node_modules at the repo root). ' + KIT + '/TOOLCHAIN.md has the exact commands and the location convention. If something is missing, install it with those commands (they write only to git-ignored or out-of-tree locations) and say what you did. Then prove the toolchain with ONE small targeted test from this sector and report its tally line.',
    '4. In-flight work: find open work that touches this sector, so that this run does not collide with it. This step must take a few minutes, not tens of minutes: ONE listing of open pull requests (gh pr list --state open --json number,title,headRefName,files if gh is available, or the GitHub tools of this session), then at most one git diff --name-only origin/main...<branch> per open pull request whose title or branch name suggests this sector. Ignore branches that have no open pull request. List the files of THIS sector that open work is already changing (hot_files), each with the pull request that changes it. Add to hot_files every file of this sector changed on origin/main on 2 or more distinct days in the last 7 days (git log origin/main --since=7.days --date=short --format=%ad --name-only): such files are very likely to change again tomorrow, so they are read-only for this run. If gh is not available, use the list the coordinator wrote at ~/.rakyat-fleet/open-prs.json (number, branch, files) instead. Set pr_list_source to gh, file or none; with none, say so in warnings and continue with the git-log part (an audit can go on; a fix run will stop).',
    '5. Unit plan: split the sector into units that one agent can hold in its head: about 3,000 to 8,000 source lines each, following package or route-group boundaries, with their tests listed separately. Every source file of the sector belongs to exactly one unit. Generated files, data files and editorial content form their own unit(s) marked as data in the notes. Order the units by risk: money, authorisation, data loss and external side effects first.',
    '6. Gate: give the exact commands that run only this sector\'s tests' + (S.gate && S.gate.length ? ' (reconnaissance suggested ' + JSON.stringify(S.gate) + ' — verify before repeating them)' : '') + '.',
    'Write the same facts to ' + OUT + '/preflight.json, and write ' + OUT + '/status.json with {"sector", "machine", "mode", "state": "running", "phase": "preflight done", "head_sha", "kit_id": ' + JSON.stringify(A.kitId || '') + '} so that the coordinator can see this machine is alive (counts and states only: no finding text, no paths). Then return the facts.',
  ].join('\n')
}

function mapPrompt(u) {
  return [
    COMMON,
    '',
    'YOU ARE THE READER OF UNIT ' + u.id + ' — ' + u.name + '.',
    'Source paths: ' + JSON.stringify(u.paths) + '. Test paths: ' + JSON.stringify(u.test_paths) + '. ' + (u.notes || ''),
    'Read the unit properly: every source file, skimming only generated or data files. Produce the map the hunters will rely on: what the unit does, its entry points and the flows through it, the INVARIANTS it is supposed to keep (state what must always be true, for example "an order is never marked paid without a verified callback"), the risky areas with the reason for each, and the source files that no test exercises.',
    'Write the full map to ' + OUT + '/map/' + u.id + '.md and return the summary. Set map_path to that file.',
  ].join('\n')
}

function huntPrompt(u, m, lens, known, round) {
  const seeds = (S.known_leads || []).slice(0, 20)
  return [
    COMMON,
    '',
    'YOU ARE A HUNTER on unit ' + u.id + ' — ' + u.name + '. Lens: ' + lens.toUpperCase() + '. Round ' + round + '.',
    'Lens definition: ' + (LENS[lens] || lens) + '.',
    'Source paths: ' + JSON.stringify(u.paths) + '. Test paths: ' + JSON.stringify(u.test_paths) + '.',
    'Unit map from the reader (full text at ' + m.map_path + '): ' + m.summary,
    'Invariants the unit must keep: ' + JSON.stringify(m.invariants),
    'Risk areas named by the reader: ' + JSON.stringify(m.risk_areas),
    known.length ? 'ALREADY REPORTED for this unit. Do not report these again: look elsewhere, or deeper.\n' + known.map(k => '- ' + k).join('\n') : '',
    seeds.length && round === 1 ? 'Leads from reconnaissance for this sector. If one falls in your unit and lens, check it first, then go beyond it:\n' + seeds.map(k => '- ' + k).join('\n') : '',
    'For each finding also give: snippet (the cited lines verbatim), needs (services a proof requires), and owner_decision (true when the fix changes authorisation policy, money or statutory maths, deletes data, or makes something more public).',
    'Find real defects through this lens. The standard for each finding: (1) you opened the file and the line you cite; (2) you can state the concrete input or sequence of events that triggers it and what goes wrong; (3) you checked the obvious guard — caller validation, middleware, a database constraint, an existing test — and it does not save it. Report nothing you cannot stand behind: an empty list is a valid answer. At most 8 findings, most severe first.',
  ].filter(Boolean).join('\n')
}

function seedPrompt(t) {
  return [
    COMMON,
    '',
    'YOU ARE A SEED PROVER. Reconnaissance flagged this lead, and this run must close it for good: ' + t,
    'Give exactly one verdict:',
    '- E2: the defect is real. Write the smallest guilt test that fails because of it, under ' + OUT + '/tmp (never in the tracked tree), run it through ' + T + ', and report the test and its red output.',
    '- refuted: the code proves the defect cannot happen (a guard, a constraint, an unreachable path). Cite the lines.',
    '- blocked: proving it needs a service this machine does not have (Postgres, Redis, Qdrant, Ollama, a vendor sandbox). Name it, and give the exact command to run once it exists.',
    'Give the severity as you now see it, and the enclosing symbol. Leave the tracked tree exactly as you found it.',
  ].join('\n')
}

function slim(f) {
  return { id: f.id, title: f.title, lens: f.lens, severity: f.severity, path: f.path, line: f.line, evidence: f.evidence, trigger: f.trigger, suggested_fix: f.suggested_fix }
}

function verifyPrompt(f, vl) {
  return [
    COMMON,
    '',
    'YOU ARE A SKEPTIC (' + vl.key + '). A hunter reported the finding below. Your job is to REFUTE it if it can be refuted.',
    vl.brief,
    'Answer real=false unless evidence that you gathered yourself convinces you. Leave the tracked tree exactly as you found it.',
    'FINDING: ' + JSON.stringify(slim(f)),
  ].join('\n')
}

function fixPrompt(b, branch, baseSha, gate, repair) {
  return [
    COMMON,
    '',
    'YOU ARE A FIXER working in an ISOLATED git worktree (your current directory). Batch ' + b.id + ': ' + b.items.length + ' verified finding(s) under ' + b.dir + '.',
    repair
      ? 'This is a REPAIR round. The branch ' + branch + ' already exists: run git switch ' + branch + ' and address every issue the reviewer raised, with new commits on top (no history rewrite). REVIEWER ISSUES: ' + JSON.stringify(repair)
      : 'Create the branch first: git switch -c ' + branch + ' ' + baseSha,
    'Before the first edit: git fetch origin main, and if any file you are about to touch changed on origin/main since ' + baseSha + ' or is in an open pull request, do not fix that finding; report it as unresolved (hot file). Exception: files the owner approved at triage (' + JSON.stringify(ALLOW_HOT) + '); for those, first run git merge-tree against origin/main (and against each open pull request that touches the file) and go ahead only if it reports no conflict.',
    'Procedure, for each finding in order:',
    ' a. Write a test that fails because of the defect, in the sector\'s existing test layout. Run it and see it fail for the right reason.',
    ' b. Make the smallest change that fixes the defect. Do not refactor around it. Run the test and see it pass, then run the neighbouring tests of every file you touched.',
    ' c. Keep fixes contract-neutral: if a backend fix changes a request or response shape, run apps/backend-rag/backend/tests/unit/scripts/test_generate_openapi_schema_pin.py; if it fails, the change moves the API contract, so do not regenerate schema.d.ts or its pin: leave the finding unresolved as a keeper request for the frontend keeper.',
    ' d. If the correct fix needs a file this sector does not own (check with resolve_sectors.py --owner), a database migration, a dependency or lockfile change, or a regenerated artifact, do NOT make that change: leave the finding unresolved and say exactly what the owning sector or the platform keeper must do.',
    'Then run the sector gate, each command through ' + T + ': ' + JSON.stringify(gate.map(fixKit)) + ' — or, if the full gate needs services this machine does not have, the subset that covers what you touched, and say so plainly.',
    'Commit one finding per commit with a conventional subject in the style of git log (for example fix(scope): what changes for the user) and a body that states the defect and names the test. Never push. The repository is PUBLIC: for a security finding (lens security, or any exposure of data, tokens or access), the branch name, commit subject and body must be neutral (what is hardened, never how to exploit it).',
    'Before you finish: git status is clean; git diff --name-only ' + baseSha + '...HEAD lists only files this sector owns (verify with python3 ' + KIT + '/resolve_sectors.py --repo . --diff ' + baseSha + '...HEAD), and python3 ' + KIT + '/check_keepers.py --role ' + ROLE + ' --diff ' + baseSha + '...HEAD passes (no keeper or frozen file); report any exception in out_of_scope_edits; finally run git switch --detach so that this worktree does not keep the branch locked.',
    'FINDINGS: ' + JSON.stringify(b.items.map(slim)),
  ].join('\n')
}

function reviewPrompt(b, fx, baseSha, gate) {
  return [
    COMMON,
    '',
    'YOU ARE THE INDEPENDENT REVIEWER of fix branch ' + fx.branch + '. You did not write it. You are in an isolated worktree: run git checkout --detach ' + fx.branch + ' to inspect and test it. Commit nothing and leave nothing behind.',
    'Check, with evidence you produce yourself:',
    '1. Each fix addresses the root cause described in its finding, and nothing else changed behaviour.',
    '2. Each new test FAILS without the fix and PASSES with it: actually run it both ways (for example, in your detached worktree, git checkout ' + baseSha + ' -- <the source files only>, run the test, then git checkout ' + fx.branch + ' -- <those files> to restore).',
    '3. Scope: python3 ' + KIT + '/resolve_sectors.py --repo . --diff ' + baseSha + '...' + fx.branch + ' shows only this sector, and python3 ' + KIT + '/check_keepers.py --role ' + ROLE + ' --diff ' + baseSha + '...' + fx.branch + ' passes. No migration, lockfile, dependency or generated-artifact edits.',
    '4. The repository rules of engagement above are respected (secrets, banned endpoints, commit style, file-placement rules).',
    '5. The gate passes, each command through ' + T + ': ' + JSON.stringify(gate.map(fixKit)) + ' (or the stated subset).',
    A.gateCmd ? '6. Cross-family gate: write git diff ' + baseSha + '...' + fx.branch + ' to ' + OUT + '/tmp/' + fx.branch.replace(/\//g, '__') + '.diff and run: ' + A.gateCmd + ' <that file>. Put its verdict line in evidence; a BLOCK or REWORK verdict means changes.' : '',
    'Give the verdict approve only if you would merge it yourself. Otherwise list precise issues with path and line.',
    'FINDINGS THE BRANCH CLAIMS TO FIX: ' + JSON.stringify(b.items.map(slim)),
    'FIXER REPORT: ' + JSON.stringify(fx),
  ].filter(Boolean).join('\n')
}

function reportPrompt(payload) {
  return [
    COMMON,
    '',
    'YOU ARE THE SCRIBE. Turn the run data below into three things, adding nothing that is not in the data. Write files with a shell command (python3 or a quoted heredoc), and ALSO return the full REPORT.md text in report_markdown: the session that launched this workflow saves it if your write was refused.',
    '1. ' + OUT + '/ledger.json — the data exactly as given (valid JSON).',
    '2. ' + OUT + '/REPORT.md — a report for the owner: one paragraph of what was covered and what was not (name every cap that dropped work), a SEED table (every seed from reconnaissance with its verdict E2, refuted or blocked, and the evidence; seeds_total minus the rows is the number still open), a table of CONFIRMED findings sorted by severity (id, severity, proof, lens, path:line, title, one-line trigger, votes, needs, owner decision), a short list of rejected findings with the reason, and — in fix mode — a table of branches (branch, findings resolved, review verdict, gate) followed by the exact commands the owner would run to publish each approved branch (git push -u origin <branch>, then open a DRAFT pull request). Do not run those commands.',
    '3. In fix mode, for each APPROVED branch, a pull-request description at ' + OUT + '/pr/<branch with slashes replaced by double underscores>.md: what was wrong, what changed, how it was verified (tests added, gate evidence), and what the reviewer checked. Plain factual prose. The repository is PUBLIC: for a branch that fixes a security finding, describe only what is hardened and how it was tested, never the weakness or how to exploit it, and mark it SECURITY at the top so the owner ships it deliberately.',
    'Also run git worktree list and report worktrees other than the main one (leftover_worktrees); remove none of them. Finally overwrite ' + OUT + '/status.json with {"sector", "machine", "mode", "state": "done", "confirmed": <count>, "branches": <count>, "kit_id"} (counts and states only: no finding text, paths or branch names).',
    'RUN DATA (JSON):',
    JSON.stringify(payload),
  ].join('\n')
}

// ---------------------------------------------------------------------------------------------
// Run
// ---------------------------------------------------------------------------------------------
phase('Preflight')
const pre = await agent(preflightPrompt(), { label: 'preflight:' + S.id, phase: 'Preflight', schema: PREFLIGHT_SCHEMA, effort: EFF.preflight })
if (!pre) throw new Error('preflight produced no result')
if (pre.tree_clean === false || pre.head_on_main === false) throw new Error('Stopped: this is not a clean fleet worktree (dirty tree, or HEAD has commits that are not on origin/main). Create a fresh worktree from origin/main, start the session inside it, and run again.')
if (MODE === 'fix' && pre.pr_list_source === 'none') throw new Error('Stopped: fix mode needs the open pull-request file list (gh, or ~/.rakyat-fleet/open-prs.json written by the coordinator) to keep hot files read-only.')
if (pre.db_safe === false) throw new Error('Stopped: a fresh shell points DATABASE_URL at a database other than the isolated test stack on localhost:5433. Fix the environment (TOOLCHAIN.md) and run again.')
if (!pre.toolchain_ok) log('Toolchain not proven: ' + pre.toolchain_notes)
let units = pre.units || []
const dropped = { units: [], findings: 0, batches: [], budget: [] }
if (units.length > LIM.maxUnits) {
  dropped.units = units.slice(LIM.maxUnits).map(u => u.id)
  log('Unit cap ' + LIM.maxUnits + ': NOT covered in this run: ' + dropped.units.join(', '))
  units = units.slice(0, LIM.maxUnits)
}
log(S.id + ': ' + units.length + ' unit(s), depth ' + DEPTH_NAME + ', lenses ' + LIM.lenses.join('/') + ', mode ' + MODE + (budget.total ? ', budget ' + Math.round(budget.total / 1000) + 'k output tokens' : ''))

const seen = new Set()
const confirmed = []
const rejected = []
let counter = 0
let verifiedCount = 0

function keyOf(f) {
  return f.path + '#' + (f.symbol ? String(f.symbol).trim() : Math.round((f.line || 0) / 20))
}
// Stable anchor: path, symbol and a hash of the cited lines (FNV-1a over whitespace-normalised text).
function anchorOf(f) {
  const t = String(f.snippet || '').replace(/\s+/g, ' ').trim()
  let h = 0x811c9dc5
  for (let i = 0; i < t.length; i++) { h ^= t.charCodeAt(i); h = Math.imul(h, 0x01000193) >>> 0 }
  return keyOf(f) + '#' + ('0000000' + h.toString(16)).slice(-8)
}

async function verify(f) {
  // Votes follow the hunter's severity: 3 for high and critical, 2 for medium, 1 for low.
  const want = Math.max(SEV_VOTES[f.severity] || 1, LIM.minVotes || 1)
  const lenses = VERIFY_LENSES.slice(0, Math.max(1, Math.min(want, VERIFY_LENSES.length)))
  if (!room(lenses.length * COST.skeptic)) {
    dropped.budget.push('verify ' + f.id)
    return Object.assign({}, f, { survives: false, severity_final: f.severity, votes_for: 0, votes_cast: 0, votes: [{ lens: 'budget', real: false, note: 'not verified: budget exhausted' }] })
  }
  const votes = (await parallel(lenses.map(vl => () =>
    agent(verifyPrompt(f, vl), { label: 'verify:' + f.id + ':' + vl.key, phase: 'Verify', schema: VERDICT_SCHEMA, effort: EFF.verify })
      .then(v => (v ? Object.assign({ lens: vl.key }, v) : null))))).filter(Boolean)
  const need = Math.floor(lenses.length / 2) + 1
  const yes = votes.filter(v => v.real)
  const survives = yes.length >= need
  let sev = f.severity
  if (survives) {
    const ranks = yes.map(v => SEV[v.severity] || 0).filter(n => n > 0).sort((a, b) => a - b)
    if (ranks.length) {
      const mid = ranks[Math.floor((ranks.length - 1) / 2)]
      sev = Object.keys(SEV).find(k => SEV[k] === mid) || sev
    }
  }
  // E2 discipline: a high or critical needs an executed reproduction; otherwise it is reported as unproven at medium.
  let proof = 'proven'
  if (survives && (SEV[sev] || 0) >= 3 && !yes.some(v => v.executed)) {
    proof = 'unproven' + ((f.needs || []).length ? ' (needs ' + f.needs.join(', ') + ')' : '')
    sev = 'medium'
  }
  return Object.assign({}, f, { survives, severity_claimed: f.severity, severity_final: sev, proof, anchor: anchorOf(f), votes_for: yes.length, votes_cast: votes.length, votes })
}

async function unitChain(u) {
  const m = (await agent(mapPrompt(u), { label: 'map:' + u.id, phase: 'Map', schema: MAP_SCHEMA, effort: EFF.map }))
    || { summary: '(the reader produced no map)', flows: [], invariants: [], risk_areas: [], untested: [], map_path: '' }
  const known = []
  let dry = 0
  let round = 0
  while (dry < LIM.dryRounds && round < LIM.maxRounds) {
    if (!room(LIM.lenses.length * COST.hunter + 4 * COST.skeptic)) {
      dropped.budget.push('hunt ' + u.id + ' round ' + (round + 1))
      log(u.id + ': budget reached, hunting stops after round ' + round)
      break
    }
    round++
    const found = (await parallel(LIM.lenses.map(lens => () =>
      agent(huntPrompt(u, m, lens, known, round), { label: 'hunt:' + u.id + ':' + lens + ':r' + round, phase: 'Hunt', schema: FINDINGS_SCHEMA, effort: EFF.hunt })
        .then(r => (r ? r.findings.map(f => Object.assign({}, f, { lens, unit: u.id })) : [])))))
      .filter(Boolean)
      .flat()
    const fresh = []
    for (const f of found) {
      const k = keyOf(f)
      if (seen.has(k)) continue
      seen.add(k)
      if (verifiedCount >= LIM.maxFindings) { dropped.findings++; continue }
      verifiedCount++
      counter++
      fresh.push(Object.assign({ id: S.id + '-' + counter }, f))
    }
    if (!fresh.length) { dry++; continue }
    dry = 0
    for (const f of fresh) known.push(f.title + ' (' + f.path + ':' + f.line + ')')
    const judged = (await parallel(fresh.map(f => () => verify(f)))).filter(Boolean)
    for (const j of judged) (j.survives ? confirmed : rejected).push(j)
    log(u.id + ' round ' + round + ': ' + fresh.length + ' new, ' + judged.filter(j => j.survives).length + ' confirmed')
  }
  return { unit: u.id, rounds: round, map: m.map_path }
}

// Seeds: every high or critical lead from reconnaissance gets a verdict, whether or not a hunter meets it.
const SEEDS = (S.known_leads || []).filter(t => /^\[(critical|high)\b/i.test(String(t)))
let seedPromise = Promise.resolve([])
if (SEEDS.length && room(SEEDS.length * 2 * COST.skeptic)) {
  seedPromise = parallel(SEEDS.map((t, i) => () =>
    agent(seedPrompt(t), { label: 'seed:' + (i + 1), phase: 'Seeds', schema: SEED_SCHEMA, effort: EFF.verify })))
} else if (SEEDS.length) {
  dropped.budget.push('seeds (' + SEEDS.length + ')')
}
const unitRuns = (await parallel(units.map(u => () => unitChain(u)))).filter(Boolean)
const seedRuns = (await seedPromise).filter(Boolean)
const seedsOpen = SEEDS.filter(t => !seedRuns.find(r => r.seed && t.indexOf(String(r.seed).slice(0, 40)) >= 0)).length
if (SEEDS.length) log('Seeds: ' + seedRuns.length + '/' + SEEDS.length + ' with a verdict (' + seedRuns.filter(r => r.verdict === 'E2').length + ' E2, ' + seedRuns.filter(r => r.verdict === 'refuted').length + ' refuted, ' + seedRuns.filter(r => r.verdict === 'blocked').length + ' blocked)')
if (dropped.findings) log('Finding cap ' + LIM.maxFindings + ': ' + dropped.findings + ' reported finding(s) were NOT verified')
confirmed.sort((a, b) => (SEV[b.severity_final] || 0) - (SEV[a.severity_final] || 0))

// ----------------------------------------------------------------- Fix and review (fix mode)
const fixes = []
if (MODE === 'fix' && confirmed.length) {
  const hot = new Set(pre.hot_files || [])
  const minSev = SEV[LIM.fixMinSeverity] || 2
  const severe = confirmed.filter(f => (SEV[f.severity_final] || 0) >= minSev)
  const decided = f => !f.owner_decision || OWNER_DECIDED.indexOf(keyOf(f)) >= 0
  const needsOwner = severe.filter(f => !decided(f)).map(f => f.id)
  if (needsOwner.length) log('Reported, not fixed (owner decision needed): ' + needsOwner.join(', '))
  const eligible = severe.filter(f => decided(f) && (!hot.has(f.path) || ALLOW_HOT.indexOf(f.path) >= 0))
  const skippedHot = severe.filter(f => hot.has(f.path) && ALLOW_HOT.indexOf(f.path) < 0).map(f => f.id)
  if (skippedHot.length) log('Not fixed because open work already changes the file: ' + skippedHot.join(', '))
  const belowBar = confirmed.length - severe.length
  if (belowBar) log(belowBar + ' confirmed finding(s) below severity ' + LIM.fixMinSeverity + ' are reported but not fixed')
  const byDir = {}
  for (const f of eligible) {
    const d = f.path.split('/').slice(0, -1).join('/')
    if (!byDir[d]) byDir[d] = []
    byDir[d].push(f)
  }
  let batches = []
  for (const d of Object.keys(byDir).sort()) {
    const items = byDir[d]
    for (let i = 0; i < items.length; i += LIM.batchSize) batches.push({ dir: d, items: items.slice(i, i + LIM.batchSize) })
  }
  batches.sort((a, b) => Math.max(...b.items.map(f => SEV[f.severity_final] || 0)) - Math.max(...a.items.map(f => SEV[f.severity_final] || 0)))
  batches = batches.map((b, i) => Object.assign({ id: 'b' + (i + 1) }, b))
  if (batches.length > LIM.maxFixBatches) {
    dropped.batches = batches.slice(LIM.maxFixBatches).map(b => b.id + ' (' + b.items.map(f => f.id).join(',') + ')')
    log('Fix cap ' + LIM.maxFixBatches + ': NOT fixed in this run: ' + dropped.batches.join('; '))
    batches = batches.slice(0, LIM.maxFixBatches)
  }
  const gate = (pre.gate_commands || []).map(fixKit)
  const base = pre.head_sha

  const done = await pipeline(
    batches,
    b => {
      if (!room(COST.fixBatch)) { dropped.budget.push('fix ' + b.id); return null }
      const leaf = b.dir.split('/').slice(-1)[0].replace(/[^a-zA-Z0-9]+/g, '-').toLowerCase() || 'root'
      const branch = 'agent/' + MACHINE + '/' + S.id + '/' + leaf + '-' + b.id
      return agent(fixPrompt(b, branch, base, gate, null), { label: 'fix:' + b.id, phase: 'Fix', schema: FIX_SCHEMA, isolation: 'worktree', effort: EFF.fix })
        .then(fx => ({ batch: b, branch, fix: fx }))
    },
    async st => {
      if (!st || !st.fix) return st
      let fx = st.fix
      let review = null
      for (let loop = 0; loop <= LIM.maxReviewLoops; loop++) {
        review = await agent(reviewPrompt(st.batch, fx, base, gate), { label: 'review:' + st.batch.id + ':' + (loop + 1), phase: 'Review', schema: REVIEW_SCHEMA, isolation: 'worktree', effort: EFF.review })
        if (!review || review.verdict === 'approve' || loop === LIM.maxReviewLoops) break
        const again = await agent(fixPrompt(st.batch, st.branch, base, gate, review.issues), { label: 'repair:' + st.batch.id + ':' + (loop + 1), phase: 'Fix', schema: FIX_SCHEMA, isolation: 'worktree', effort: EFF.fix })
        if (!again) break
        fx = again
      }
      return { batch: st.batch, branch: st.branch, fix: fx, review }
    },
  )
  for (const d of done.filter(Boolean)) {
    fixes.push({
      batch: d.batch.id,
      branch: d.branch,
      findings: d.batch.items.map(f => f.id),
      resolved: d.fix ? d.fix.resolved : [],
      unresolved: d.fix ? d.fix.unresolved : ['no fixer result'],
      files_changed: d.fix ? d.fix.files_changed : [],
      tests_added: d.fix ? d.fix.tests_added : [],
      gate_passed: d.fix ? d.fix.gate_passed : false,
      gate_evidence: d.fix ? d.fix.gate_evidence : '',
      out_of_scope_edits: d.fix ? d.fix.out_of_scope_edits : [],
      review_verdict: d.review ? d.review.verdict : 'not reviewed',
      review_issues: d.review ? d.review.issues : [],
      review_evidence: d.review ? d.review.evidence : '',
    })
  }
}

// ----------------------------------------------------------------- Report
phase('Report')
const payload = {
  sector: S.id,
  machine: MACHINE,
  mode: MODE,
  kit_id: A.kitId || null,
  base: { head_sha: pre.head_sha, main_sha: pre.main_sha, tree_clean: pre.tree_clean },
  seeds: seedRuns,
  seeds_total: SEEDS.length,
  coverage: { depth: DEPTH_NAME, units: unitRuns, units_not_covered: dropped.units, findings_not_verified: dropped.findings, batches_not_fixed: dropped.batches, stopped_by_budget: dropped.budget, lenses: LIM.lenses, votes: 'by severity 3/2/1, at least ' + (LIM.minVotes || 1) },
  hot_files: pre.hot_files,
  gate_commands: pre.gate_commands,
  warnings: pre.warnings,
  confirmed: confirmed.map(f => ({
    id: f.id, unit: f.unit, lens: f.lens, severity: f.severity_final, severity_claimed: f.severity_claimed, proof: f.proof,
    anchor: f.anchor, needs: f.needs || [], owner_decision: !!f.owner_decision, path: f.path, line: f.line, title: f.title,
    evidence: f.evidence, trigger: f.trigger, suggested_fix: f.suggested_fix, votes: f.votes_for + '/' + f.votes_cast,
    notes: f.votes.map(v => v.lens + ': ' + v.note),
  })),
  rejected: rejected.map(f => ({ id: f.id, lens: f.lens, path: f.path, line: f.line, title: f.title, votes: f.votes_for + '/' + f.votes_cast, why: f.votes.filter(v => !v.real).map(v => v.lens + ': ' + v.note) })),
  fixes,
}
let rep = null
try {
  rep = await agent(reportPrompt(payload), { label: 'report:' + S.id, phase: 'Report', schema: REPORT_SCHEMA, effort: EFF.report })
} catch (e) {
  log('Report agent could not run (' + String(e && e.message || e) + '): the run data is in the return value of this workflow')
}

return {
  sector: S.id,
  machine: MACHINE,
  mode: MODE,
  units_covered: unitRuns.length,
  units_not_covered: dropped.units,
  stopped_by_budget: dropped.budget,
  depth: DEPTH_NAME,
  seeds_total: SEEDS.length,
  seeds_with_verdict: seedRuns.length,
  seeds: seedRuns.map(r => r.verdict + ' [' + r.severity + '] ' + r.path + ':' + r.line + ' ' + String(r.seed).slice(0, 120)),
  findings_confirmed: confirmed.length,
  findings_rejected: rejected.length,
  findings_not_verified: dropped.findings,
  by_severity: confirmed.reduce((acc, f) => { acc[f.severity_final] = (acc[f.severity_final] || 0) + 1; return acc }, {}),
  top: confirmed.slice(0, 10).map(f => '[' + f.severity_final + '] ' + f.title + ' (' + f.path + ':' + f.line + ')'),
  branches: fixes.map(x => x.branch + ' — review: ' + x.review_verdict + ', gate: ' + (x.gate_passed ? 'passed' : 'not proven')),
  report: rep ? rep.report_path : null,
  report_markdown: rep ? rep.report_markdown : null,
  leftover_worktrees: rep ? rep.leftover_worktrees : [],
  ledger: rep ? rep.ledger_path : null,
}
