# Adversarial review pipeline (red-team)

Recorded tooling decision (ADR-043 discipline): this directory adds an
agent-facing tool with standing instructions — the `red-team` agent
definition in `.claude/agents/red-team.md` — commissioned 2026-09-02.

## Pieces

- **`.claude/agents/red-team.md`** — the adversarial agent charter: given a
  diff, try to *disprove* the fix. Six attack categories: wrong-reason
  tests, reverted-fix-with-surviving-tests, path/symlink/normalization
  escapes, exit-code/state conflation, fail-open gates, mutation survivors.
- **`trigger.py`** — deterministic trigger. `classify` maps a commit's
  changed paths onto security-seam categories (auth, secrets, security,
  policy gates, state machines, CI/CD gates); `run` spawns 2–3 red-team
  agents in parallel (`claude --agent red-team -p ...`), each with a
  different attack angle, then parses and routes their findings.
- **`meta_check.py`** — the reverted-guard detector: for each recent
  non-merge commit, checks whether its added production lines still exist
  at HEAD while its added test lines survive. Orphaned tests are the tell
  for silently reverted fixes.
- **`scripts/git-hooks/post-commit-red-team.sh`** — fail-soft post-commit
  trigger (opt-in via `install-post-commit-red-team.sh`; disable with
  `ELSPETH_RED_TEAM_DISABLE=1`). Classification is synchronous and cheap;
  agents launch detached.

## Local findings for triage

All findings, at every severity and confidence, and parsing errors append to
`.claude/red-team/review-log.md` (gitignored). Raw agent output remains under
`.claude/red-team/runs/`. The operator triages these reports before deciding
which findings to publish to GitHub Issues; automatic review never publishes.

## Usage

```bash
# Is this commit seam-relevant? (exit 0 = yes, 3 = no)
.venv/bin/python -m scripts.red_team.trigger classify --commit HEAD

# Full run against a commit (spawns agents, logs findings locally)
.venv/bin/python -m scripts.red_team.trigger run --commit HEAD
.venv/bin/python -m scripts.red_team.trigger run --commit HEAD --dry-run

# Reverted-guard sweep over the last 50 non-merge commits
# (exit 0 = clean, 4 = findings)
.venv/bin/python -m scripts.red_team.meta_check --last 50
```

Tests: `tests/unit/scripts/test_red_team_trigger.py`,
`tests/unit/scripts/test_red_team_meta_check.py`.

## Known limits (deliberate, precision-first)

- `meta_check` skips production files deleted or renamed at HEAD: by line
  matching alone a rename is indistinguishable from a revert.
- Merge commits are not scanned directly by `meta_check` (`--no-merges`);
  a fix reverted *by* a merge is still caught, because the original
  non-merge fix commit is what gets compared against HEAD.
- Seam patterns are a curated allowlist in `trigger.SEAM_PATTERNS`; a new
  security-relevant module must be added there to get automatic coverage.
