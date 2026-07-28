---
name: judge-signature-workflow
description: >
  Use when acquiring, repairing, rotating, resuming, or diagnosing signed judge
  metadata on the trust_tier.tier_model allowlist. Covers the key-free
  elspeth-judge staging tools and the operator-keyed sign-bundle/rekey CLI.
---

# Judge-signature workflow — agent stages, operator signs

The `trust_tier.tier_model` allowlist seals judge-gated suppressions with an
operator-held HMAC signature. The full command and flag reference is
`docs/judge-signature-handoff.md`.

## [O1]: the operator HMAC key never crosses to the agent

The signature is a symmetric HMAC: a key holder can forge an `ACCEPTED`
verdict. Therefore:

- The agent never reads, prints, acquires, or synthesizes
  `ELSPETH_JUDGE_METADATA_HMAC_KEY`. Agents stage authority-free work and may
  run non-authoritative previews; only the operator-keyed CLI mints signatures.
- Signing never runs in CI. CI verifies and gates.
- A staged bundle is a worklist, not authority. The operator command re-derives
  its source and binding claims from the live tree.

All judging uses the Codex CLI harness with `--judge-transport codex-cli
--judge-tools readonly`. The child gets a sealed path-confined reader and a
minimal environment that excludes the HMAC key, override tokens, provider keys,
cloud credentials, shell/web/apps/hooks/memories/plugins, and subagents.
Readonly judge rationales are secret-scrubbed before print or persistence.

## Agent side: key-free `elspeth-judge` MCP

Bundles land in `.elspeth/staged-reviews/<bundle_id>.json`.

1. `verify_signatures`: shape-only diagnosis; never an authoritative HMAC
   recompute.
2. `stage_scan`: derive `drift_repair`, `rotation`, `stale_delete`, and
   `new_judgment` actions from the current source and allowlist.
3. `stage_preview`: optional non-authoritative readonly Codex preview for new
   judgments.
4. `stage_status`: show counts/outcomes and emit the paste-ready operator
   `sign-bundle` command.
5. `stage_rekey`: stage the advisory scope for an HMAC key roll, recording only
   environment variable names.

## Operator side: recoverable `sign-bundle`

Always dry-run first:

```bash
elspeth-lints sign-bundle <bundle.json> --owner <operator-id> \
  --judge-transport codex-cli --judge-tools readonly --dry-run
```

Without `--dry-run`, `sign-bundle`:

1. requires the operator HMAC key and re-verifies the complete bundle against
   the active source/allowlist before creating any transaction;
2. copies the allowlist into a private same-filesystem transaction under the
   sibling `.sign-bundle-transactions/` directory;
3. performs deterministic `stale_delete` and safe non-judge `rotation` actions
   before paid judge calls;
4. re-judges `drift_repair` and `new_judgment` actions in the private copy,
   journalling accepted authoritative decisions;
5. re-verifies the original bundle/live tree, unchanged active bytes, journaled
   effects, and every produced HMAC signature;
6. conflict-checks the append-only rotation log;
7. publishes the coherent candidate with one Linux
   `renameat2(RENAME_EXCHANGE)` directory swap; then
8. idempotently appends the exact staged rotation records. The private
   transaction is the durable pending record if an interruption lands between
   the allowlist commit and audit append; resume detects the published bytes and
   finishes the append without repeating judge work.

The active allowlist remains byte-identical on a BLOCK, ordinary action failure,
or interruption before publication. The command preserves the transaction and
prints a paste-ready recovery command:

```bash
elspeth-lints sign-bundle <bundle.json> ... \
  --resume <transaction-dir> --yes
```

Resume skips an accepted judge action only after authoritatively re-verifying its
persisted signature and live source binding. It also checks the exact original
bundle bytes, unchanged active allowlist, original non-secret signing policy,
and action journal. Rotation and stale-delete target paths are re-derived from
fresh verification, never trusted from the bundle. Never hand-edit a transaction
or treat its scratch YAML as authority.

Use resume when only execution failed or was interrupted. Re-run `stage_scan`
when source/bindings or active allowlist bytes changed, when preflight rejected
the bundle, or after resolving a duplicate-key target. Do not retry thousands of
accepted judge calls merely because a later action BLOCKed.

`--dry-run` is strictly no-write: no transaction directory, judge call,
stale-delete, rotation log, or signed entry.

Lane rules remain fail-closed:

- `drift_repair`: pop the stale entry only in the private copy, re-run the real
  judge, restore it intact on BLOCK/failure, and never launder a stale verdict.
- `new_judgment`: the fire-time judge is authoritative; previews never sign.
- `rotation`: only a verified non-judge-gated rotation; duplicate keys preserve
  both copies and fail.
- `stale_delete`: only a verified orphan, routed to its owning YAML.

## Operator side: `rekey`

```bash
elspeth-lints rekey --in <bundle.json> \
  --old-key-env <OLD_VAR> --new-key-env <NEW_VAR>
```

`rekey` remains a scheme-preserving signature-only swap. It derives the live
judge-gated set, requires every entry to verify under OLD or NEW, skips
already-NEW entries, and aborts rather than laundering an entry that verifies
under neither key. Key bytes never appear on the CLI.

## Do not resurrect one-off signing scripts

This seam replaces hand-edited release signing scripts and runbooks. Stage via
`elspeth-judge`, dry-run, then let the operator fire or resume the authoritative
CLI transaction.
