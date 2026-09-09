// dir-bug-sweep — read-only multi-agent bug sweep of a directory.
//
// Invoke (from the main loop, after deciding scope):
//   Workflow({ scriptPath: '/abs/path/to/.claude/workflows/dir-bug-sweep.js', args: {
//     path: 'src/elspeth/contracts',     // dir (or file) to sweep — required in practice
//     tag: '2806bugsweep',               // Filigree label applied to every lodged issue — required
//     glob: '*.py',                      // optional, default '*.py'
//     lineCap: 1000,                     // optional, max lines an agent may own, default 1000
//     maxParallel: 6,                    // optional, concurrent agents per wave, default 6
//     extraGuidance: '',                 // optional, appended to every review prompt
//     files: null,                       // [{path,lines}] — PREFER passing this; skips the fragile scout (see PRECOMPUTE)
//   }})
//
// The workflow does: scout (inventory + line counts) -> deterministic first-fit-decreasing
// bin-pack into <=lineCap bins (oversized files become solo, effort:'high') -> review agents
// in waves of maxParallel -> return structured results. It does NOT reconcile the tracker;
// the `bug-sweep` skill owns the post-run authoritative-query / dedup / present step.
//
// PRECOMPUTE THE INVENTORY (preferred path): the orchestrator (main loop) should compute the file
//   list itself in a shell and pass it as args.files — do NOT lean on the in-workflow scout. The
//   scout is an LLM agent (the JS sandbox has no filesystem/shell, which is WHY it delegates); at
//   effort:low it has been observed to ignore the scoped path, enumerate the whole repo (.venv /
//   caches / 30k+ files), exhaust its context, and emit nothing — killing the run before a single
//   review agent launches. One deterministic command yields the exact [{path,lines}] the workflow
//   needs:
//     find <path> -type f -name '<glob>' -not -path '*/__pycache__/*' -print0 | xargs -0 wc -l
//   Fall back to the agent scout only for a small, clearly-scoped dir where it cannot wander.
//
// TARGETED RE-RUN ON PARTIAL FAILURE (important):
//   If some agents die (e.g. a transient server-side rate limit), do NOT resume this run with
//   resumeFromRunId — resume's cache prefix breaks at the FIRST failure, so every downstream
//   agent re-runs and re-lodges DUPLICATES. Instead, launch a fresh dir-bug-sweep passing only
//   the unreviewed files via `args.files: [{path, lines}, ...]` (scout is skipped). Same tag.

export const meta = {
  name: 'dir-bug-sweep',
  description: 'Read-only multi-agent bug sweep of a directory; each agent owns <=lineCap lines and lodges findings in Filigree under a sweep tag',
  whenToUse: 'Auditing a directory or subsystem for bugs, architectural defects, and enhancement opportunities at scale, with one tracker tag per sweep',
  phases: [
    { title: 'Scout', detail: 'inventory files + line counts' },
    { title: 'Review', detail: 'waves of read-only review agents' },
  ],
}

// `args` may arrive as a parsed object OR as a JSON-encoded STRING (the runtime serialises it at the
// tool boundary). If we naively read `args.path` on a string it is undefined and every field falls
// through to its default — path='.' (the WHOLE REPO), tag='bugsweep' — silently mis-scoping the run.
// Parse defensively, then FAIL CLOSED on a path default we did not expect.
let A = args
if (typeof A === 'string') { try { A = JSON.parse(A) } catch (e) { A = {} } }
if (A == null || typeof A !== 'object') A = {}

function normalizeScoutPath(value) {
  const raw = String(value).replace(/\\/g, '/')
  const absolute = raw.startsWith('/')
  const parts = []
  for (const part of raw.split('/')) {
    if (part === '' || part === '.') continue
    if (part === '..') {
      if (parts.length && parts[parts.length - 1] !== '..') parts.pop()
      else if (!absolute) parts.push(part)
    } else {
      parts.push(part)
    }
  }
  let normalized = `${absolute ? '/' : ''}${parts.join('/')}`
  if (!normalized) normalized = absolute ? '/' : '.'
  // Shell quoting prevents shell metacharacter expansion, but GNU find still parses a quoted
  // leading-dash starting point as one of its own options/actions. Make relative paths
  // unambiguously positional before interpolating the command.
  if (!absolute && normalized.startsWith('-')) normalized = `./${normalized}`
  return normalized
}

const path = normalizeScoutPath(A.path || '.')
const TAG = A.tag || 'bugsweep'
const GLOB = A.glob || '*.py'
const LINE_CAP = A.lineCap || 1000
const MAX_PARALLEL = Object.prototype.hasOwnProperty.call(A, 'maxParallel') ? A.maxParallel : 6
const EXTRA = A.extraGuidance || ''
const HAS_EXPLICIT_FILES = Object.prototype.hasOwnProperty.call(A, 'files')
const EXPLICIT_FILES = HAS_EXPLICIT_FILES ? A.files : null
const IS_WHOLE_REPO_SCOPE = path === '.' || path === '/'
const ESCAPES_REPO_SCOPE = path === '..' || path.startsWith('../')
const IS_ABSOLUTE_SCOPE = path.startsWith('/')

// Self-diagnostic: echo the resolved scope FIRST so a mis-delivered args is visible on line 1, not agent 97.
log(`Resolved scope: path='${path}' tag='${TAG}' glob='${GLOB}' lineCap=${LINE_CAP} maxParallel=${MAX_PARALLEL} explicitFiles=${Array.isArray(EXPLICIT_FILES) ? EXPLICIT_FILES.length : 0} extraGuidance=${EXTRA ? EXTRA.length + 'chars' : 'none'}`)
if (!Number.isInteger(MAX_PARALLEL) || MAX_PARALLEL <= 0) {
  log(`REFUSING invalid maxParallel=${JSON.stringify(MAX_PARALLEL)} — expected a positive integer.`)
  return { error: 'invalid_max_parallel', maxParallel: MAX_PARALLEL }
}
if (HAS_EXPLICIT_FILES && !Array.isArray(EXPLICIT_FILES)) {
  log(`REFUSING invalid files inventory — expected an array, got typeof=${typeof EXPLICIT_FILES}.`)
  return { error: 'invalid_files_inventory', filesType: typeof EXPLICIT_FILES }
}
if (IS_ABSOLUTE_SCOPE && !HAS_EXPLICIT_FILES) {
  log(`REFUSING absolute scout path '${path}'. Scout scopes must be repository-relative; pass an explicit files inventory for a targeted run. Aborting.`)
  return { error: 'absolute_scout_path_refused', resolvedPath: path }
}
if (ESCAPES_REPO_SCOPE && !HAS_EXPLICIT_FILES) {
  log(`REFUSING scout path '${path}' because it escapes the repository scope. Pass a scoped path or an explicit files inventory. Aborting.`)
  return { error: 'out_of_repo_sweep_refused', resolvedPath: path }
}
if (IS_WHOLE_REPO_SCOPE && !HAS_EXPLICIT_FILES) {
  log(`REFUSING to sweep the whole repo from '.' with no explicit files — args likely did not arrive (got typeof=${typeof args}). Pass {path, tag} (and ideally files). Aborting.`)
  return { error: 'unscoped_sweep_refused', argsType: typeof args, resolvedPath: path }
}

const FILE_SCHEMA = {
  type: 'object', additionalProperties: false,
  properties: {
    files: {
      type: 'array',
      items: {
        type: 'object', additionalProperties: false,
        properties: { path: { type: 'string' }, lines: { type: 'integer' } },
        required: ['path', 'lines'],
      },
    },
  },
  required: ['files'],
}

// ---------- scout ----------
let inventory
if (HAS_EXPLICIT_FILES) {
  inventory = EXPLICIT_FILES
  log(`Using ${inventory.length} explicitly-provided files (scout skipped — targeted re-run)`)
} else {
  phase('Scout')
  const scout = await agent(
    `READ-ONLY scout for a code sweep. Run EXACTLY ONE shell command and emit its result — nothing else.\n` +
    `Run this VERBATIM; do NOT broaden it, do NOT run any other find, do NOT enumerate any directory:\n` +
    `  find ${shellQuote(path)} -type f -name ${shellQuote(GLOB)} -not -path '*/__pycache__/*' -print0 | xargs -0 wc -l\n` +
    `Scope is STRICTLY \`${path}\`. NEVER walk parent dirs, .venv, .uv-cache, node_modules, build caches, or the repo root — ` +
    `if you find yourself looking at thousands of files you have widened the scope and MUST stop and re-run the exact command above. ` +
    `Return EVERY matching file as {path, lines}, path exactly as printed, EXCLUDING the wc 'total' row. ` +
    `Do not read file contents and do not modify anything.`,
    { label: 'scout', phase: 'Scout', schema: FILE_SCHEMA }
  )
  inventory = (scout && scout.files) || []
  log(`Scout found ${inventory.length} files matching ${GLOB} under ${path}`)
}
if (!inventory.length) {
  log(`No files found — nothing to sweep.`)
  return { error: 'no_files_found', path, glob: GLOB, bins: 0, results: [] }
}

// ---------- bin-pack (first-fit-decreasing; oversized files solo) ----------
const sorted = [...inventory].sort((a, b) => b.lines - a.lines)
const heavyBins = []
const normalItems = []
for (const f of sorted) {
  if (f.lines > LINE_CAP) heavyBins.push({ effort: 'high', files: [f.path], lines: f.lines })
  else normalItems.push(f)
}
const normalBins = []
for (const f of normalItems) {
  let placed = false
  for (const bin of normalBins) {
    if (bin.lines + f.lines <= LINE_CAP) { bin.files.push(f.path); bin.lines += f.lines; placed = true; break }
  }
  if (!placed) normalBins.push({ files: [f.path], lines: f.lines })
}
// Spread heavy (slow) bins across waves — a wave's wall-clock is its slowest member.
const ordered = []
let hi = 0, ni = 0, idx = 0
while (hi < heavyBins.length || ni < normalBins.length) {
  if (idx % MAX_PARALLEL === 0 && hi < heavyBins.length) ordered.push(heavyBins[hi++])
  else if (ni < normalBins.length) ordered.push(normalBins[ni++])
  else ordered.push(heavyBins[hi++])
  idx++
}
const waveCount = Math.ceil(ordered.length / MAX_PARALLEL)
log(`Packed ${inventory.length} files into ${ordered.length} bins (${heavyBins.length} oversized solo, effort:high); ${waveCount} wave(s) of <=${MAX_PARALLEL}.`)

// ---------- review schema + prompt ----------
const ISSUE_SCHEMA = {
  type: 'object', additionalProperties: false,
  properties: {
    files_reviewed: { type: 'array', items: { type: 'string' } },
    issues: {
      type: 'array',
      items: {
        type: 'object', additionalProperties: false,
        properties: {
          id: { type: 'string', description: 'REAL Filigree issue id from issue_create' },
          title: { type: 'string' },
          severity: { type: 'string', description: 'P0|P1|P2|P3|P4' },
          kind: { type: 'string', description: 'bug|architecture|enhancement|security' },
          file: { type: 'string' },
          summary: { type: 'string' },
        },
        required: ['id', 'title', 'severity', 'kind', 'file', 'summary'],
      },
    },
    notes: { type: 'string' },
  },
  required: ['files_reviewed', 'issues', 'notes'],
}

function shellQuote(value) {
  return "'" + String(value).replace(/'/g, "'\"'\"'") + "'"
}

function buildPrompt(bin) {
  const fileList = bin.files.map(f => `  - ${f}`).join('\n')
  return `You are a meticulous READ-ONLY code reviewer running a bug sweep.\n\n` +
`YOUR OWNED FILES (review EVERY one, in depth — you own these end to end):\n${fileList}\n\n` +
`WHAT TO HUNT FOR: correctness bugs (None/Optional mishandling, mutable defaults, wrong comparisons, broken (de)serialisation, implicit fabrication via dict.get-with-default where a missing required value should raise); contract-invariant violations (validation theatre — a validator that returns success without validating; fields that permit contradictory states; lifecycle/telemetry invariants only partially enforced); architectural defects (leaky/duplicated boundaries, fail-open / silent-failure, trust-boundary gaps); security (secret/PII egress, unredacted error text, unsafe URL/host handling, weak signing); and concrete enhancement opportunities ONLY where there is a real, named deficiency.\n\n` +
`THIS IS A READ-ONLY RUN. Do NOT modify any file. Do NOT use Edit/Write/NotebookEdit. Do NOT run any mutating tool (no analyze, no fix, no git writes). The ONLY writes permitted are creating + labelling Filigree issues (below).\n\n` +
`METHOD: (1) Read each owned file fully. (2) VERIFY each candidate before lodging — roam into callers, referenced classes/functions, and tests and confirm it actually manifests (use Loomweave MCP tools entity_find/entity_callers_list/entity_at/entity_source_get and Grep/Read). A candidate you cannot substantiate does NOT get lodged. (3) Evidence bar per issue: exact file:line, what is wrong, WHY (the invariant/caller expectation it breaks), expected behaviour, and the verification you did. No style nitpicks. A clean file is a valid outcome — report it clean, do NOT force-lodge.\n\n` +
`LODGING IN FILIGREE (the only permitted writes): (1) FIRST load the deferred tool — ToolSearch query "select:mcp__filigree__issue_create". (2) Create each finding with mcp__filigree__issue_create: type "bug" for defects/architecture/security, "task" for a pure enhancement; title prefixed with the file; priority P0..P3 by real blast radius; description = file:line + evidence + why + expected + your verification; labels: ["${TAG}"] in this same creation call; if it has an actor/assignee field set it to "bug-sweep". The post-run reconciliation queries on this exact label, so it must be committed atomically with the issue. (3) Capture the REAL id returned by issue_create; never invent or narrate one.\n\n` +
`RETURN (structured): files_reviewed = every owned file; issues = one entry per lodged issue with the REAL id; notes = clean files, cross-file patterns, anything deferred. Empty issues array is fine.` +
(EXTRA ? `\n\nADDITIONAL GUIDANCE FOR THIS SWEEP:\n${EXTRA}` : ``)
}

// ---------- waves ----------
const allResults = []
let waveNo = 0
for (let i = 0; i < ordered.length; i += MAX_PARALLEL) {
  waveNo += 1
  const wave = ordered.slice(i, i + MAX_PARALLEL)
  const phaseTitle = `Wave ${waveNo}`
  phase(phaseTitle)
  log(`${phaseTitle}: dispatching ${wave.length} review agents`)
  const res = await parallel(wave.map(bin => () => {
    const opts = { label: bin.files.length === 1 ? bin.files[0].split('/').pop() : `${bin.files.length} files`, phase: phaseTitle, schema: ISSUE_SCHEMA }
    if (bin.effort) opts.effort = bin.effort
    return agent(buildPrompt(bin), opts)
  }))
  const ok = res.map((result, index) => result ? { bin: wave[index], result } : null).filter(Boolean)
  allResults.push(...ok)
  const lodged = ok.reduce((n, item) => n + (item.result.issues ? item.result.issues.length : 0), 0)
  const failed = wave.length - ok.length
  log(`${phaseTitle} done: ${ok.length}/${wave.length} returned${failed ? ` (${failed} FAILED — re-run those files via args.files, NOT resume)` : ''}, ${lodged} issues lodged`)
}

const lodged = allResults.flatMap(item => item.result.issues || [])
const reviewed = allResults.flatMap(item => {
  const claimed = new Set(item.result.files_reviewed || [])
  const unexpected = [...claimed].filter(path => !item.bin.files.includes(path))
  if (unexpected.length) log(`IGNORING files_reviewed outside assigned bin: ${unexpected.join(', ')}`)
  return item.bin.files.filter(path => claimed.has(path))
})
// Which owned files never came back (failed agents) — feed these to a targeted re-run.
const reviewedSet = new Set(reviewed)
const missing = inventory.map(f => f.path).filter(p => !reviewedSet.has(p))
log(`Sweep complete: ${allResults.length}/${ordered.length} bins returned; ${reviewed.length} files reviewed; ${lodged.length} issues self-reported. ${missing.length} files unreviewed.`)
if (missing.length) log(`UNREVIEWED (re-run with args.files): ${missing.join(', ')}`)

return {
  tag: TAG,
  path,
  binsDispatched: ordered.length,
  binsReturned: allResults.length,
  filesReviewed: reviewed.length,
  unreviewedFiles: missing,
  selfReportedIssues: lodged,
  perAgent: allResults.map(item => {
    const claimed = new Set(item.result.files_reviewed || [])
    const files = item.bin.files.filter(path => claimed.has(path))
    return { files, issueCount: (item.result.issues || []).length, notes: item.result.notes, issues: item.result.issues || [] }
  }),
}
