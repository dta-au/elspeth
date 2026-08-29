---
name: bug-sweep
description: >
  Use to run a read-only, multi-agent bug sweep over a directory or subsystem —
  fan a fleet of review agents across the files (each owning a bounded slice),
  have them lodge verified findings in Filigree under one sweep tag, then
  reconcile the tracker into a clean, deduplicated, severity-ranked report.
  Invoke for "deep dive / audit <dir> for bugs", "sweep <subsystem> and file
  what you find", or any large read-only review that must scale past one agent.
user-invocable: true
---

# Bug sweep — fan-out review → tracker → reconciled report

A bug sweep is three phases. The middle one is a workflow; the discipline around
it is what makes the result trustworthy.

1. **Scope** — decide the target dir, the sweep tag, and the knobs.
2. **Fan out** — run the `dir-bug-sweep` workflow (scout → bin-pack → waves of
   read-only review agents that lodge findings tagged with your sweep tag).
3. **Reconcile** — query the tag authoritatively, dedup, close stray duplicates,
   present by severity. **This phase is yours, not the workflow's.** Skipping it
   is how phantom IDs and duplicates reach the user.

Requires the Filigree MCP server (the tag is a Filigree label) and, ideally,
Loomweave (agents use it to verify findings against real callers).

## Phase 1 — Scope

Pick, and state to the user:

- **Target** — the dir or file (e.g. `src/elspeth/contracts`).
- **Tag** — a unique, memorable label for *this* sweep, e.g. `2806bugsweep`
  (date + word). Everything is reconciled through this tag, so it must not
  collide with a previous sweep.
- **Knobs** (defaults are good): `lineCap` (max lines one agent owns, default
  1000), `maxParallel` (concurrent agents per wave, default 6 — this is the cap
  the workflow honours by chunking into waves), `glob` (default `*.py`),
  `extraGuidance` (project-specific defect doctrine to inject into every agent —
  e.g. "treat dict.get-with-default fabrication as a defect; the audit DB is a
  legal record").

Arithmetic to set expectations: `bins ≈ total_lines / lineCap` (plus one solo
bin per oversized file); `waves ≈ bins / maxParallel`. A 22k-line dir → ~24
agents → 4 waves. That is the honest cost of "review every file"; don't shrink
it by overfilling bins.

## Phase 2 — Fan out

**Compute the inventory yourself first — do not rely on the in-workflow scout.**
The scout is an LLM agent asked to run `find` + `wc`; it has been observed to
ignore its scoped path, enumerate the whole repo (`.venv`, caches, 30k+ files),
exhaust its context, and emit nothing — killing the run before a single review
agent launches. You (the orchestrator) have a shell; the workflow's JS sandbox
does not (that's *why* it delegates). One deterministic command yields the exact
`[{path, lines}]` the workflow wants:

```bash
find <path> -type f -name '<glob>' -not -path '*/__pycache__/*' -print0 \
  | xargs -r -0 wc -l \
  | python3 -c "import sys,json; rows=[parts for line in sys.stdin if len(parts := line.split(None,1)) == 2]; print(json.dumps([{'path':p.rstrip('\n'),'lines':int(n)} for n,p in rows if p.rstrip('\n') != 'total']))"
```

Pass that array as `args.files`; the workflow skips the scout and goes straight
to bin-pack + waves. **Invoke via `scriptPath` to the live workflow file, NOT
`name`** — the `name: 'dir-bug-sweep'` registration is *cached* and silently lags
edits you just made to the file (this bit hard once: a stale cache re-ran a
pre-fix version and mis-scoped the whole run). `scriptPath` re-reads from disk:

```
Workflow({ scriptPath: '/abs/path/to/.claude/workflows/dir-bug-sweep.js', args: {
  path: 'src/elspeth/web/composer',   // still pass it — used for labels + logging
  tag:  '2806web-composer',
  files: [{ path: '.../service.py', lines: 5320 }, ...],   // the inventory above
  extraGuidance: '...project defect doctrine...'   // optional
}})
```

(Omitting `files` falls back to the agent scout — acceptable only for a small,
clearly-scoped dir where it cannot wander. For anything subsystem-sized, precompute.)

**`args` arrives at the script as a JSON *string*, not an object** (runtime
serialises it at the tool boundary). The workflow already `JSON.parse`s it and
logs a `Resolved scope:` line first thing + fails closed if it resolves to a
whole-repo `.` sweep. After launching, **verify scope before trusting the run**:
the first review agents should own files under your target dir, and for a
precomputed run **no scout agent should spawn at all**. If you see `path='.'`,
a scout, or repo-root files, kill it (`TaskStop`) — args didn't land.

The workflow returns self-reported results **and** an `unreviewedFiles` list.
**Do not trust agent-reported issue IDs** — they can be phantom or confabulated;
they're a cross-check, not the source of truth. The tracker is.

### If agents fail (transient rate limits, deaths)

`binsReturned < binsDispatched`, and `unreviewedFiles` is non-empty. **Re-run
only the missing files — do NOT `resumeFromRunId`.** Resume's cache prefix breaks
at the *first* failure, so every downstream agent re-runs and re-lodges
duplicates (this is exactly how a clean sweep grows a pile of dupes). Instead:

```
Workflow({ scriptPath: '/abs/path/to/.claude/workflows/dir-bug-sweep.js', args: {
  tag: '2806bugsweep',                              // SAME tag
  files: [{ path: '.../data.py', lines: 392 }, ...] // the unreviewedFiles, scout skipped
}})
```

To stay under server-side rate limits on big sweeps, keep `maxParallel` at 6 and
avoid bunching several oversized (effort:high) files in one wave — the workflow
already spreads them, but a very large dir can still trip throttling; a targeted
re-run of the failed slice is the recovery, not a panic.

## Phase 3 — Reconcile (the integrity step — always do this)

1. **Authoritative query** — the single source of truth:

   ```
   mcp__filigree__issue_list({ label: '<tag>', no_limit: true, sort_by: 'created_at', direction: 'asc' })
   ```

   If it's too large to return inline it lands in a tool-results file; parse with
   `jq`. A compact view + total:

   ```bash
   jq -r '.items | length' "$F"                                   # count
   jq -r '.items | sort_by(.created_at)
     | .[] | "\(.created_at[11:19])  \(.issue_id)  P\(.priority) \(.type)  \(.title)"' "$F"
   ```

2. **Find duplicates.** Sort by `created_at`; a re-run shows up as a later
   timestamp cluster. Two issues on the **same file** with the **same defect**
   (compare descriptions, not just titles — re-run wording differs) are dupes.
   Re-runs are non-deterministic, so the second pass also surfaces *distinct*
   findings on already-covered files — keep those. Verify each candidate pair's
   descriptions before acting (a wrong close loses a real finding):

   ```bash
   jq -r --arg a "$CANON" --arg d "$DUP" '.items[]
     | select(.issue_id==$a or .issue_id==$d)
     | "\(.issue_id)\n  \(.description[0:340])\n"' "$F"
   ```

3. **Close the duplicate copies** (keep the earliest of each pair). Bugs sit at
   `triage` with no direct close transition, so use `force: true`, and name the
   canonical in the reason so the audit trail explains itself:

   ```
   mcp__filigree__issue_close({ issue_id: '<dup>', force: true, actor: 'bug-sweep',
     reason: 'Duplicate of <canonical> (same file, same defect) — re-lodged by a
              workflow-resume/re-run. Closing redundant copy; canonical remains open.' })
   ```

4. **Coverage check.** "No issue lodged" ≠ "reviewed clean." For any file that
   came back with zero findings — *especially* files from a failed-then-re-run
   bin — read that agent's `notes` and confirm a substantive clean-or-dismissed
   rationale (named candidates examined and cleared), not a bare mention. If a
   file is only listed in `files_reviewed` with no reasoning, re-run that bin.

5. **Present** by severity. Lead with P0/P1/P2 (the real blast radius), compress
   the P3 tail thematically. Be transparent about the mechanism if rows were
   deduped ("N raw → M canonical, and here's why"). Surface cross-file themes the
   agents flagged as out-of-scope. End by noting it was read-only and offering
   the highest-value fix cluster as the next step — but **do not fix anything in
   this skill**; lodging + dedup cleanup are the only authorized writes.

## Discipline (non-negotiable)

- **Read-only.** Agents may roam the whole codebase to verify, but the only
  writes anywhere in a sweep are Filigree create/label (agents) and the dedup
  closes (you). No source edits, no auto-fixing.
- **Verify before lodge.** Each finding needs file:line + why-it-breaks +
  expected + the trace that confirms it. Baked into the agent prompt; hold the
  line in reconciliation by spot-checking descriptions.
- **One tag per sweep**, reconciled once at the end. The tag isolates this
  sweep's findings from the rest of the tracker and from prior sweeps.
- **The tracker is truth, agent IDs are a hint.** Build the final list from the
  authoritative query, never from the workflow's `selfReportedIssues`.

## Files

- Workflow: `.claude/workflows/dir-bug-sweep.js` (scout → FFD bin-pack → waves).
  Edit it to change the review prompt, schema, or packing; re-invoke via its
  absolute `scriptPath` so the runtime reads the live file.
