---
name: judge-signature-workflow
description: >
  Use when acquiring, repairing, or rotating signed judge metadata on the
  trust_tier.tier_model allowlist — i.e. when the tier-model gate reports signed
  allowlist drift (scope_fingerprint / ast_path / missing / invalid signature),
  when a new tier-model suppression needs a judge verdict, when you must roll the
  ELSPETH_JUDGE_METADATA_HMAC_KEY, or any time you would otherwise hand-edit a
  judge_metadata_signature. Covers the agent-stages / operator-signs seam: the
  key-free elspeth-judge MCP server (stage_scan / stage_status / verify_signatures
  / stage_preview / stage_rekey) and the operator elspeth-lints CLI (sign-bundle /
  rekey). Does NOT apply to ordinary lint runs (`elspeth-lints check`) or to
  non-tier-model allowlists.
---

# Judge-signature workflow — agent stages, operator signs

The `trust_tier.tier_model` allowlist seals every judge-gated suppression with an
**operator-held HMAC signature**. Acquiring, repairing, and rotating those
signatures runs across a two-actor seam. This skill is how to drive it; the full
command/flag reference is `docs/judge-signature-handoff.md`.

## The one rule everything follows from ([O1])

The signature is a **symmetric HMAC** — any holder of
`ELSPETH_JUDGE_METADATA_HMAC_KEY` can forge an `ACCEPTED` verdict. Therefore:

- **You (the agent) NEVER hold the key.** You *propose*: survey the tree, stage a
  bundle, optionally run a non-authoritative preview judge. The authoritative
  verdict is minted only inside the operator-keyed step. The `elspeth-judge` MCP
  tools **fail closed** if the key is in their environment.
- **Signing never runs in CI.** CI only verifies (`check-override-rate`,
  `check-judge-quality`); a standing test (`test_meta_ci_never_signs.py`) fails if
  any signing verb appears in the gate workflow.
- **Staging asserts; firing verifies.** A staged bundle carries *zero* authority.
  Every claim in it (which entries drifted, which findings are new, which keys
  re-key) is re-derived from the live tree by the operator step, which aborts
  before any write on the slightest staleness. The bundle is a worklist + audit
  record, not a grant.

## Judging transport policy (2026-07-09)

All judging — including the final signature verdict in the operator step — runs
on the **agentic harness with read-only tool access**:
`--judge-transport agent --judge-tools readonly`. The judge Read/Grep/Globs the
source tree before ruling; the excerpt-blinded judge misjudged boundary code it
could not see and forced bulk operator overrides. [O1] is unchanged (tool access
is read-only context, not key access), and the no-secrets-in-signed-YAML
invariant moved to the output: readonly-mode judge rationales are secret-scrubbed
before persist. `--judge-tools readonly` requires the agent transport (OpenRouter
has no tool loop). Do not recommend blinded OpenRouter runs for signing except as
a deliberate fallback when the agent harness is unavailable.

## Agent side — stage a bundle (key-free MCP: `mcp__elspeth-judge__*`)

Bundles land in `.elspeth/staged-reviews/<bundle_id>.json`.

1. **`verify_signatures`** — read-only, *always shape-only* diagnosis. Use to see
   what drifted without the key. (Authoritative HMAC recompute is the operator
   `diagnose`, not this.)
2. **`stage_scan`** — survey source tree + allowlist into a worklist bundle across
   four lanes: `drift_repair` (re-judge needed), `rotation` (mechanical, non-judge-
   gated keys only), `stale_delete` (orphan), `new_judgment` (uncovered finding).
   Optional `bundle_id`, `staged_by`.
3. **`stage_preview`** (needs the `[judge-agent]` extra) — run the read-only agent
   judge over each `new_judgment` action and record a **non-authoritative** preview
   (`authoritative=False`); surfaces BLOCKED reasons so you can fix the code or
   rationale *before* the operator spends a real judge call. Arg: `bundle_id`.
4. **`stage_status`** — summarise the bundle (per-lane counts, preview outcomes)
   and emit the **paste-ready operator `sign-bundle` command**. Arg: `bundle_id`.
5. **`stage_rekey`** — for a key roll: enumerate currently-valid judge-gated
   entries and flag broken ones into a rekey bundle, recording env-var **names**
   only, never key bytes. Args: `old_key_env`, `new_key_env`.

Then hand the operator the `bundle_id` (or the command `stage_status` printed).
`stage_rekey.keys`/`broken_keys` are an advisory *preview of scope*, not the set
that will be acted on — the CLI re-derives the real set from the tree at fire time.

## Operator side — fire with the key (`elspeth-lints` CLI, key-bearing shell)

Run only where `ELSPETH_JUDGE_METADATA_HMAC_KEY` (and, for LLM lanes,
`OPENROUTER_API_KEY`) are held — never in CI. Both commands re-verify the whole
bundle against the tree and abort before the first write on any mismatch.

```
elspeth-lints sign-bundle <bundle.json> --owner <operator-id> [--dry-run] [--yes]
elspeth-lints rekey --in <bundle.json> --old-key-env <OLD_VAR> --new-key-env <NEW_VAR>
```

- `sign-bundle` is the **only** place a bundle's signature is minted. `drift_repair`
  + `new_judgment` re-judge (a contradicting BLOCK is surfaced and *not* signed;
  the popped entry is restored intact); `rotation` re-binds non-judge-gated keys
  with no judge; `stale_delete` removes an orphan. Always preview with `--dry-run`
  first. A dup-key target aborts (`return 2`) with both copies intact.
- `rekey` is a scheme-preserving, **signature-only** swap (only the
  `judge_metadata_signature` line changes). Idempotent/re-runnable; an entry
  verifying under neither old nor new key aborts the whole run (no laundering). Key
  bytes never touch the CLI — only the env-var *names*.

## When firing aborts: re-stage, do not force

The verify gate aborting is normal in two cases — the fix is always to re-run
`stage_scan` against the current tree and fire the fresh bundle:

- **AST-position cascade staleness** — the bundle was staged before an edit that
  shifted AST positions (e.g. a new `import`) in a covered `src/elspeth` source.
- **Dup-key in the target file** — resolve the duplicate in the YAML first.

## What this replaced

This seam replaces the per-release signing runbooks and the one-shot
`scripts/cicd/sign_accept_backlog.py` driver. Do not resurrect hand-edited signing
ceremony — stage a bundle and have the operator fire it. Full detail, all flags,
and the lane semantics: `docs/judge-signature-handoff.md`.
