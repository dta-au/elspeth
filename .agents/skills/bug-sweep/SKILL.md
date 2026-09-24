---
name: bug-sweep
description: >
  Use to run a read-only, multi-agent bug sweep over a directory or subsystem.
  Assign bounded file slices, verify findings against callers and tests, and
  produce a local deduplicated report for operator triage. GitHub issue uploads
  are a separate effort.
user-invocable: true
---

# Bug sweep — parallel review and local report

Run the `dir-bug-sweep` workflow to review source without modifying it. The
workflow returns findings locally; it never creates, labels, comments on, or
closes remote issues. The operator triages findings and uploads selected issues
to GitHub separately through the shared development configuration.

## Scope

State the target directory or explicit file list, a unique sweep tag, and the
review parameters. Defaults are `lineCap: 1000`, `maxParallel: 6`, and
`glob: '*.py'`. Use `extraGuidance` for relevant project defect doctrine.
Oversized files get one agent each with high effort. Do not reduce coverage
by overfilling bins.

Compute the inventory before dispatch, using a repository-relative target:

```bash
find <path> -type f -name '<glob>' -not -path '*/__pycache__/*' -print0 \
  | xargs -r -0 wc -l \
  | python3 -c "import sys,json; rows=[parts for line in sys.stdin if len(parts := line.split(None,1)) == 2]; print(json.dumps([{'path':p.rstrip('\n'),'lines':int(n)} for n,p in rows if p.rstrip('\n') != 'total']))"
```

Check the inventory against known files before dispatch. The workflow's optional
LLM scout has previously broadened scope into caches and exhausted its context;
use it only for a small, clearly scoped directory.

## Run

Invoke the live workflow by absolute `scriptPath`, not by its cached registered
name. Pass the explicit inventory as `files`:

```
Workflow({ scriptPath: '/abs/path/to/.claude/workflows/dir-bug-sweep.js', args: {
  path: 'src/elspeth/web/composer',
  tag: '20260925-composer-review',
  files: [{ path: 'src/elspeth/web/composer/service.py', lines: 5320 }],
  maxParallel: 6,
  extraGuidance: 'Verify every suspected failure against the real callers.'
}})
```

The runtime may deliver `args` as a JSON string; the workflow parses it and logs
its resolved scope. Check that first line. With explicit files, no scout should
run. Stop a mis-scoped run and correct the arguments.

Each agent reads every assigned file, follows callers and tests using `rg` and
file reads, and returns evidence for each verified finding: exact file:line,
broken invariant, expected behavior, and how the failure was verified. Clean
files are valid outcomes. Findings include title, severity P0–P4, kind, file,
and an evidence-bearing summary; no remote issue ID is expected.

If agents fail, use `unreviewedFiles` to rerun only the missing files with the
same tag and explicit inventory. Do not use `resumeFromRunId`: its cache can
rerun successful agents after the first failure and duplicate findings.

## Reconcile and report

1. Compare returned `files_reviewed` with the dispatched inventory. A missing
   finding does not prove a file was reviewed clean. Check each agent's notes
   for substantive clean-or-dismissed reasoning; rerun inadequate reviews.
2. Deduplicate findings about the same file and defect, comparing the full
   evidence rather than titles. Keep distinct failures on the same file.
3. Spot-check each retained finding against source and tests. Separate confirmed
   defects from uncertain candidates and record unresolved coverage.
4. Write the durable report under `docs/audit/` after confirming the chosen
   path is not ignored. Use `.claude/lanes/<run>/` for coordination artifacts.
5. Present findings by severity, including coverage, verification limits, and
   the report path. The report is ready for operator triage and later GitHub
   upload; this workflow does not publish it or fix source.

## Files

- `.claude/workflows/dir-bug-sweep.js`: scout, first-fit-decreasing bin packing,
  parallel review waves, and structured local findings.
