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

## Judging transport policy (updated 2026-07-27)

All judging — including the final signature verdict in the operator step — runs
on the **Codex CLI harness with read-only tool access**:
`--judge-transport codex-cli --judge-tools readonly`. The Codex child
authenticates from the installed CLI account, but receives a minimal environment
that excludes the operator HMAC key, override tokens, provider API keys, and
cloud credentials. Shell, web, apps, hooks, goals, memories, remote plugins, and
subagents are disabled. Read/Grep/Glob context comes only from the sealed
path-confined MCP reader.

The judge inspects the source tree before ruling because the excerpt-blinded
judge misjudged boundary code it could not see and forced bulk operator
overrides. [O1] is unchanged (tool access is read-only context, not key access),
and readonly-mode rationales are secret-scrubbed before persistence.
`--judge-tools readonly` accepts `codex-cli` and the legacy `agent` (Claude
Agent SDK) transport; OpenRouter has no tool loop. Use `codex-cli` for the
normal signing workflow. Do not recommend blinded OpenRouter runs except as a
deliberate fallback.

## Agent side — stage a bundle (key-free MCP: `mcp__elspeth-judge__*`)

Bundles land in `.elspeth/staged-reviews/<bundle_id>.json`.

1. **`verify_signatures`** — read-only, *always shape-only* diagnosis. Use to see
   what drifted without the key. (Authoritative HMAC recompute is the operator
   `diagnose`, not this.)
2. **`stage_scan`** — survey source tree + allowlist into a worklist bundle across
   four lanes: `drift_repair` (re-judge needed), `rotation` (mechanical, non-judge-
   gated keys only), `stale_delete` (orphan), `new_judgment` (uncovered finding).
   Optional `bundle_id`, `staged_by`.
3. **`stage_preview`** (needs installed + authenticated Codex CLI and the
   `[mcp]` extra) — run the read-only Codex judge over each `new_judgment`
   action and record a **non-authoritative** preview (`authoritative=False`);
   surfaces BLOCKED reasons so you can fix the code or rationale *before* the
   operator spends a real judge call. Arg: `bundle_id`.
4. **`stage_status`** — summarise the bundle (per-lane counts, preview outcomes)
   and emit the **paste-ready operator `sign-bundle` command**. Arg: `bundle_id`.
5. **`stage_rekey`** — for a key roll: enumerate currently-valid judge-gated
   entries and flag broken ones into a rekey bundle, recording env-var **names**
   only, never key bytes. Args: `old_key_env`, `new_key_env`.

Then hand the operator the `bundle_id` (or the command `stage_status` printed).
`stage_rekey.keys`/`broken_keys` are an advisory *preview of scope*, not the set
that will be acted on — the CLI re-derives the real set from the tree at fire time.

## Operator side — fire with the key (`elspeth-lints` CLI, key-bearing shell)

Run only where `ELSPETH_JUDGE_METADATA_HMAC_KEY` is held — never in CI. The
Codex transport uses the installed CLI account and does not require a provider
API key in the signing shell. Both commands re-verify the whole bundle against
the tree and abort before the first write on any mismatch.

```
elspeth-lints sign-bundle <bundle.json> --owner <operator-id> \
  --judge-transport codex-cli --judge-tools readonly --dry-run
elspeth-lints rekey --in <bundle.json> --old-key-env <OLD_VAR> --new-key-env <NEW_VAR>
```

- `sign-bundle` is the **only** place a bundle's signature is minted. `drift_repair`
  + `new_judgment` re-judge (a contradicting BLOCK is surfaced and *not* signed;
  the popped entry is restored intact); `rotation` re-binds non-judge-gated keys
  with no judge; `stale_delete` removes an orphan. Always preview with `--dry-run`
  first. A dup-key target aborts (`return 2`) with both copies intact.
  Deterministic deletes and rotations run before paid judge calls.
- Non-dry-run `sign-bundle` works in a private same-filesystem transaction.
  Accepted decisions are HMAC-authenticated and journalled there; the active
  allowlist remains byte-identical on BLOCK, failure, or interruption. Only a
  fully successful, re-verified candidate is published, using one coherent
  Linux `renameat2(RENAME_EXCHANGE)` directory swap.
- On BLOCK, failure, or interruption, use the paste-ready command printed with
  `--resume <transaction-dir>`. Resume authenticates the journal, exact bundle,
  source/bindings, directory identities, candidate/checkpoint/audit evidence,
  prior signatures, and the original non-secret signing policy. It skips
  completed authoritative decisions and retries only unfinished actions.
- `rekey` is a scheme-preserving, **signature-only** swap (only the
  `judge_metadata_signature` line changes). Idempotent/re-runnable; an entry
  verifying under neither old nor new key aborts the whole run (no laundering). Key
  bytes never touch the CLI — only the env-var *names*.

## Recovery versus re-staging

Use the printed `--resume` command when the bundle and source are still current
and the authenticated active/candidate directory identities are either in their
original orientation (not published) or exact swapped orientation (published).
The swapped state remains recoverable if a later coordinated writer advanced
the active contents while the private candidate still holds the authenticated
base; resume finalizes the pending audit without reverting those later bytes.

A BLOCK event remains in the private transaction while the active allowlist is
unchanged. A successful resume publishes the accumulated event history. If a
transaction is abandoned, retain or remove its directory deliberately according
to operator audit policy; it is not silently pruned.

Re-run `stage_scan` instead when source/bindings changed, pre-publication active
contents drifted, the authenticated directory identities/content cannot be
reconciled, or initial preflight rejected the bundle. Expected cases include:

- **AST-position cascade staleness** — the bundle was staged before an edit that
  shifted AST positions (e.g. a new `import`) in a covered `src/elspeth` source.
- **Dup-key in the target file** — resolve the duplicate in the YAML first.

In those cases re-stage; never force, hand-edit signatures, or edit a transaction
journal.

## What this replaced

This seam replaces the per-release signing runbooks and the one-shot
`scripts/cicd/sign_accept_backlog.py` driver. Do not resurrect hand-edited signing
ceremony — stage a bundle and have the operator fire it. Full detail, all flags,
and the lane semantics: `docs/judge-signature-handoff.md`.
