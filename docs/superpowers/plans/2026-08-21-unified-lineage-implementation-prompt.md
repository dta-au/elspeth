# Unified Lineage Campaign — Implementation Prompt (subagent-driven)

Paste the block below into a fresh Claude Code session at /home/john/elspeth to begin
(or resume) execution. It is self-contained; the session needs no other context.

---

Execute the unified-lineage campaign (spec rev 3.2) using
**superpowers:subagent-driven-development** — invoke that skill first and follow it:
one fresh subagent per task, two-stage review between tasks, you personally review
every subagent's diff before accepting it.

**Read before dispatching anything:**
1. `docs/superpowers/plans/2026-08-21-unified-lineage-master.md` — the sequencer,
   decision canon, and gates. It is authoritative for ordering.
2. `docs/superpowers/specs/2026-08-21-barrier-scopes-full-nesting-spec.md` — the spec
   (rulings 1–28 final; zero open decisions).
3. `docs/agents/recent-code-hints.md` and `AGENTS.md` — whole-tree AST gates and
   conventions; every implementing subagent's brief must tell it to read these too.

**Execution order** (from the master plan — do not reorder):
WS0 → protocols Tasks 1–2 → WS1a (Task 8a before protocols Task 3) → protocols
Task 3 (freeze) → WS1b (Phase A, then the atomic flip, then Phase C) →
**WS1 CHECKPOINT** → WS2 → WS3 ∥ WS4 (WS4 Tasks 11–12 gate on WS3) → WS3+WS4
integration → WS5+6. Resume wherever the previous session stopped — determine
position from git log and the plans' checkbox state, never by assumption.

**Per task:** dispatch a fresh subagent with the task's full text (Files, Interfaces,
every checkbox step verbatim), plus: the mechanical citation pre-flight where the plan
mandates one; the instruction to read every file it will modify before editing; TDD as
written (failing test first, run it, implement, run green). Review the diff yourself
before the commit step. Commit per the task's commit block — **stage by explicit
pathspec only, never `git add -A`**.

**Standing rules (non-negotiable):**
- Shared checkout on `release/0.7.2`. Subagents cannot use worktrees. Never bypass
  hooks except the documented `--no-verify`-with-reconciliation grant.
- Slice boundaries (marked in each plan): full `pytest tests/` (`-n 12`), trust-tier
  corpus **count** diff vs baseline (COUNT findings, never tail; add nothing), the
  wardline gate command from AGENTS.md. Push after each green slice.
- Mutation tasks run `-n 0`. Cap per-subagent pytest parallelism when running
  subagents concurrently.
- **WS1 CHECKPOINT is a STOP gate:** frozen-oracle diff clean + deltas only in the
  §4.1a-enumerated surfaces, or STOP and surface to the maintainer. Do not press into
  WS2/WS3 on a red foundation. Same for any NEW casualty found by an RC-5 grep.
- Never stage or hand-edit judge signatures; no judge-bundle staging during the
  campaign. Never hold `ELSPETH_JUDGE_METADATA_HMAC_KEY`.
- The trust-tier gate's existing failure corpus is the known fail-closed baseline
  (`elspeth-13f0cc04fb`) — compare counts against it; it is not yours to clear.
- WS3∥WS4 may run as parallel subagent lanes after WS2; everything else is serial.
- If a subagent dies on a session limit, resume it rather than redispatching blind.

**Reporting:** after each task, a one-paragraph outcome (what landed, review verdict,
commit hash). At each slice boundary, the gate evidence (suite result, trust-tier
count delta, wardline exit). No calendar commitments; if a plan step conflicts with
the live tree, stop that lane and reconcile against the plan's Interfaces blocks
before writing code — do not improvise around it.

---
