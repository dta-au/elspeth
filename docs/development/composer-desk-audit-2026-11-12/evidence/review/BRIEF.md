# Adversarial review of the composer desk audit — shared brief

You are one of several independent reviewers. Your job is to try to DISPROVE the audit's
claims in your assigned slice, not to agree with them. A review that confirms everything
without having tried to break it is worthless.

## What you are reviewing

The audit (commit `45b65e50` on branch `ccr-d14093ba-glu6bw`, code baseline `main` @ `1a7ff0e6`):

- `docs/development/composer-desk-audit-2026-11-12/README.md` — findings F1–F18, method, perf, limits
- `docs/development/composer-desk-audit-2026-11-12/remediation-plan.md` — fixes R1–R16, P1–P6, E1–E6, work packages, demo runbook
- `docs/development/composer-desk-audit-2026-11-12/tool-call-register.md` and `evidence/` (turn table, surface cross-check, two verification reports, register JSON, screenshots)
- `docs/reference/composer-tools.md` (lightly refreshed)
- `evals/composer-live-audit/` (the harness that produced the live evidence)

## Raw evidence you can use (read-only)

- Live battery captures, one dir per scenario/turn:
  `.claude/lanes/composer-desk-audit-20261112/live/sessions/<scenario>/turnNN/`
  (`llm_calls.json` = every provider request+response for that turn incl. DeepSeek
  `reasoning_content`; `terminal.json` = what the API returned; `state.json`,
  `validation.json`, `summary.json` one level up, `transcript.md`).
  Later runs: `evals/composer-live-audit/runs/sessions/` (same layout).
- Raw per-instance captures: `.claude/lanes/composer-desk-audit-20261112/live/capture/llm_calls.jsonl`,
  `.claude/lanes/composer-desk-audit-20261112/live/instances/*/capture/llm_calls.jsonl`,
  `evals/composer-live-audit/runs/instances/*/capture/llm_calls.jsonl`.
- Rendered model views: `.claude/lanes/composer-desk-audit-20261112/llm-view/` and
  `.claude/lanes/composer-desk-audit-20261112/opus/surfaces/`.
- Earlier helper outputs: `.claude/lanes/composer-desk-audit-20261112/opus/` (digest.md, register.json,
  agent reports, tutorial walk logs `tut1*.log`, screenshots).
- Source: `src/elspeth/...`. Python: `.venv/bin/python` (editable install).

## Rules

- READ-ONLY. Do not edit anything under `src/`, `tests/`, `docs/`, `evals/`, `scripts/`.
  No git writes, no `git stash`. Do not start web servers or make provider/network calls.
  Do not run directory-wide pytest; a single focused test with `-n 0` is fine.
- Always `cd <repo root> &&` first.
- Evidence standard: every verdict cites exact `file:line` (current tree) or an exact evidence
  file path, and states how you checked: EXECUTED (you ran code/queries and show raw output),
  READ (traced code through its callers), or INFERRED. Re-derive counts yourself from the raw
  captures or live objects; never trust the audit's number. Control any grep/regex instrument
  against a known positive and a known negative before relying on a zero.
- For each claim in your slice, return one of:
  - CONFIRMED — you independently reproduced it;
  - CONFIRMED-WITH-CORRECTION — substance holds, but a number, citation, line, wording or
    severity is wrong (say exactly what to change);
  - OVERSTATED / UNDERSTATED — severity or scope is wrong (say why);
  - REFUTED — the claim is false (show the evidence);
  - UNVERIFIABLE — evidence insufficient (say what would settle it).
- Also review the matching remediation items: would the proposed fix actually fix it? Does it
  violate a project invariant (AGENTS.md "Composer invariants": the LLM authors all pipeline
  structure — no server-synthesized structure; no tutorial-special paths in the composer)? Is
  there a smaller or safer fix? Does it break something else?
- Report anything important the audit MISSED in your slice (with evidence), but keep scope tight.

## Output

Write your review to `.claude/lanes/composer-desk-audit-20261112/review/<your-id>.md` with:
a verdict table (claim → verdict → one-line reason), then one section per non-CONFIRMED verdict
with evidence and the exact correction, then remediation comments, then missed issues.
Your final message must be SHORT: the file path, verdict counts, and one line per
REFUTED/OVERSTATED/CORRECTION item.
