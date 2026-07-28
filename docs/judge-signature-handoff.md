# Judge-signature handoff: agent stages, operator signs

This document describes the two-actor workflow for acquiring and rotating signed
judge metadata on the `trust_tier.tier_model` allowlist, and the HMAC key custody
rule that makes it safe. It is the operator/agent handoff for the
`stage_scan -> sign-bundle` seam and the `elspeth-judge` MCP server.

## The custody rule ([O1])

The judge-metadata signature is an **HMAC** — a symmetric MAC. Any holder of
`ELSPETH_JUDGE_METADATA_HMAC_KEY` can forge a signature: hand-write
`judge_verdict: ACCEPTED` with a fabricated rationale over a publicly-computable
fingerprint, sign it, and pass every gate. The whole design follows from that
single fact (invariant elspeth-b3a3335c9f, *[O1] operator-only HMAC custody*;
the CI-exposure corollary is elspeth-2b351cd004):

- **An agent never holds the key.** Agents may *propose* work — survey the tree,
  stage a bundle, run a non-authoritative preview judge — but the authoritative
  verdict for a finding is only ever minted inside the operator-keyed step.
- **Signing never runs in CI.** The key must never be reachable from
  PR-controlled code. CI keeps *verifying* (`check-override-rate`,
  `check-judge-quality`); it never signs. This is enforced as a standing
  regression guard by `tests/unit/elspeth_lints/test_meta_ci_never_signs.py`,
  which fails if any signing verb is added to a `run:` step of
  `.github/workflows/enforce-allowlist-judge-gates.yaml`.

**Staging asserts; firing verifies.** A staged bundle carries *zero* authority.
Everything it claims (which entries drifted, which findings are orphaned, which
keys re-key) is an assertion the operator step re-derives from the live source
tree before it writes anything. The bundle is a worklist and an audit record,
not a grant.

## Judging runs on the Codex CLI harness with tool access (updated 2026-07-27)

All judging — including the **final signature verdict** — runs via the agentic
harness (`--judge-transport codex-cli`) with read-only tool access
(`--judge-tools readonly`). The judge may Read/Grep/Glob within the source tree
and allowlist dir through a sealed, fail-closed MCP reader before ruling.

The Codex subprocess authenticates from the installed CLI account state, not
from a provider key passed by the signing shell. Its environment is reduced to
executable/home/locale/TLS essentials: the HMAC key, override tokens, provider
API keys, cloud credentials, and arbitrary application environment do not cross
the process boundary. User config and repo rules are ignored; shell, web, apps,
hooks, goals, memories, remote plugins, and subagents are disabled. The only
tool-mode capability is the three-tool read-only MCP server.

Why: the excerpt-blinded judge systematically misjudged boundary code it could
not see — verdicts flipped on whether a function's `def` line happened to fall
inside the excerpt window, and whole rationale families were blocked for
"missing" context that existed one screen away. The blind-judge campaigns ended
in bulk operator overrides, which is a worse audit outcome than an informed
verdict: the extra context is critical to getting good outcomes that don't
require manually excluding everything.

What this does NOT change:

- **[O1] custody is untouched.** Tool access is read-only context for the
  judge; the HMAC key stays operator-only and signing still never runs in CI.
- **No raw bytes in signed YAML.** The original blinding existed so tool-read
  (unscrubbed) source bytes could not enter signed rationales. That invariant
  moved from input blinding to output scrubbing: in readonly mode the judge's
  rationale passes through `scrub_secrets` (the same curated pattern set the
  excerpt goes through) before it is printed or persisted.
- `--judge-tools readonly` accepts `--judge-transport codex-cli` and the legacy
  Claude Agent SDK spelling `agent`; the OpenRouter path has no tool loop and
  is rejected. `codex-cli` is the normal signing transport.

Blinded mode (`--judge-tools none`) remains available and byte-identical to the
historical behavior; entries signed before 2026-07-09 were produced under it.

## Actors and the seam

```
  AGENT (no key)                          OPERATOR (key-bearing shell)
  ------------------------------          ----------------------------------
  elspeth-judge MCP server                elspeth-lints CLI
    stage_scan   -> worklist bundle  -->  sign-bundle <bundle.json>
    stage_preview (advisory verdict)        (re-verifies the bundle against the
    stage_status (paste-ready cmd)           tree, THEN fires the real judge /
    stage_rekey  -> rekey bundle     -->     re-keys in a private transaction;
    verify_signatures (shape-only)           active bytes publish coherently)
                                          rekey --in <bundle.json>
```

Bundles are written under `.elspeth/staged-reviews/<bundle_id>.json`. The agent
side is structurally key-free: the MCP server refuses to start a tool handler if
`ELSPETH_JUDGE_METADATA_HMAC_KEY` is present in its environment (fail-closed,
checked *before* any optional import), so the agent surface can never co-locate
with the key.

## The `elspeth-judge` MCP server (agent side)

Registered in `.mcp.json` as `elspeth-judge`, launched as
`python -m elspeth_lints.mcp --root src/elspeth --allowlist-dir
config/cicd/enforce_tier_model --staged-dir .elspeth/staged-reviews` (with
`PYTHONPATH=.../elspeth-lints/src`). It needs the `[mcp]` extra and an installed
and authenticated Codex CLI. All five tools fail closed when the HMAC key is
present in the environment.

| Tool | Key-free? | LLM? | What it does |
| --- | --- | --- | --- |
| `verify_signatures` | yes | no | Read-only, **always shape-only** signature diagnosis of the tier_model allowlist. The authoritative HMAC recompute is the operator CLI `diagnose`, not this tool. |
| `stage_scan` | yes | no | Survey source tree + allowlist into an authority-free worklist bundle across four lanes — `drift_repair` / `rotation` / `stale_delete` / `new_judgment`. Args: optional `bundle_id`, `staged_by`. |
| `stage_status` | yes | no | Summarise a staged bundle (per-lane/kind counts, preview outcomes) and emit the paste-ready operator `sign-bundle` command. Arg: `bundle_id` (required). |
| `stage_preview` | yes | yes (read-only Codex CLI judge) | Run the sealed read-only Codex judge over each `new_judgment` action and record a **non-authoritative** preview verdict (`authoritative=False`); surfaces BLOCKED reasons. Never signs. Arg: `bundle_id` (required). Needs installed/authenticated Codex CLI plus `[mcp]`. |
| `stage_rekey` | yes | no | Enumerate currently-valid judge-gated entries and flag broken ones into a rekey bundle, recording env-var **names** only — never key bytes. Args: `old_key_env`, `new_key_env` (required), optional `bundle_id`, `staged_by`. |

`stage_scan` feeds the rotation planner only non-judge-gated entries
(`exclude_judge_gated=True`), so the rotation lane serves the 17 non-judge-gated
pre-judge entries and never the 388 judge-gated ones; an fp-shifted judge-gated
entry routes to `drift_repair` only.

## Operator commands (key-bearing shell)

Run these only in an operator-controlled shell that holds
`ELSPETH_JUDGE_METADATA_HMAC_KEY`. The Codex transport authenticates from the
installed CLI account and does not need a provider API key in that shell. Both
commands re-derive every binding from the live tree and abort on any staleness
before touching the configured active allowlist.

### `sign-bundle` — fire a staged review bundle

```
elspeth-lints sign-bundle <bundle.json> --owner <operator-id> \
  --judge-transport codex-cli --judge-tools readonly --dry-run
```

This is the **only** place a judge signature is minted from a bundle. The verify
phase (re-check the whole bundle against the tree) is the all-or-nothing gate; on
any mismatch it aborts before creating a transaction. Execution happens in a
private same-filesystem copy under a sibling `.sign-bundle-transactions/`
directory. `stale_delete` and safe `rotation` actions run before paid judge
calls. Each accepted authoritative decision is journalled, but the configured
active allowlist remains byte-identical until every action succeeds, the bundle
and live tree are re-verified, and every recovered signature verifies
authoritatively. The transaction manifest is HMAC-authenticated with a
purpose-separated key derived from the operator signing key; candidate,
checkpoint, and staged-audit bytes are fsynced before their hashes are recorded,
so scratch bytes and journal claims cannot be changed together by a keyless
process. Publication shares a stable sibling mutation lock with the ordinary
allowlist writers and rechecks both trees while holding it. Any staged rotation
audit records are conflict-checked before the coherent candidate publishes with
one Linux `renameat2(RENAME_EXCHANGE)` directory swap, then the exact staged
audit delta is appended idempotently with the durable publish-start timestamp.
The private transaction is the pending record for the narrow cross-resource
window: if publication commits before the audit append, resume detects the
published bytes and finishes that append without repeating judge work.

A real-judge BLOCK, ordinary action failure, unexpected exception, or
interruption exits non-zero (interrupts return 130), preserves the private
transaction, and prints a paste-ready command containing
`--resume <transaction-dir>`. Resume does not trust the scratch copy: it
authenticates the journal, then re-checks the exact bundle bytes,
source/bindings, active/candidate state, action evidence, and previously
produced HMAC signatures before skipping completed judge work. Recovery accepts
the recorded pre-transaction state, the exact authenticated published
candidate, or a proven post-publish state where the private candidate contains
the exact base and the active tree has since advanced under the shared writer
lock. An ambiguous byte-for-byte return to the base fails closed. Any other tree
change refuses recovery; re-stage against the new tree instead. On
successful completion the
coherent active tree is diagnosed and the canonical override-rate counter
snapshot is refreshed. Resume also binds the original non-secret signing policy
(owner, override mode, judge transport/tools, token setting, roots, environment
file, and output format); changing any of it requires a new transaction.

Per lane:

- `drift_repair` and `new_judgment` run the **real judge** — re-judging, never
  carrying a stale verdict forward over changed content (that would be the [O1]
  forgery). A contradicting BLOCK is surfaced and **not** signed; on BLOCK the
  popped stale entry is restored intact.
- `rotation` re-binds non-judge-gated keys with no judge.
- `stale_delete` removes an orphaned entry, surgically.

Flags:

| Flag | Default | Meaning |
| --- | --- | --- |
| `<bundle>` (positional) | — | Path to the staged review bundle JSON. |
| `--owner` | required | Audit identity recorded on freshly signed entries. |
| `--root` | `src/elspeth` | Source tree to re-scan for the entries' findings. |
| `--repo-root` | none | Repo root for trust-boundary scanners (omit for tier-model-only default). |
| `--allowlist-dir` | `config/cicd/enforce_tier_model` | Per-module allowlist YAML to repair in place. |
| `--operator-override` | off | Forward `--operator-override` to each justify call (requires the override-token env, exactly like `justify`). |
| `--max-tokens` | none | Override judge response `max_tokens` per call. |
| `--dry-run` | off | Print verify + per-lane plan; no judge call and no transaction or other writes. |
| `--yes` | off | Skip the interactive confirm before creating/resuming the private transaction. |
| `--resume` | none | Resume the printed transaction directory after re-verifying bundle bytes, source/bindings, active bytes, journaled effects, and prior signatures. |
| `--rotation-log` | `.elspeth/rotations.log` | Rotation audit JSONL finalized only with coherent publish. |
| `--format` | `text` | Per-entry justify output (`text`/`json`). |
| `--judge-transport` | `openrouter` | Provider that produces the verdict (`codex-cli` is the required normal signing choice; `agent` is the legacy Claude SDK path). |
| `--judge-tools` | `none` | Judge tool configuration; use `readonly` with `codex-cli` for signing. |

Override-token environment (only when `--operator-override` is passed):
`ELSPETH_JUDGE_OVERRIDE_TOKEN` + `ELSPETH_JUDGE_OVERRIDE_TOKEN_SHA256`.

A **dup-key** bundle (the same key occurs more than once in a file) aborts with
`return 2` and both copies intact — `sign-bundle` refuses rather than silently
deleting one.

### `rekey` — rotate the HMAC key

```
elspeth-lints rekey --in <bundle.json> --old-key-env <OLD_VAR> --new-key-env <NEW_VAR>
```

A scheme-preserving, **signature-only** swap: it verifies every judge-gated entry
under the OLD key, recomputes its signature under the NEW key, and atomically
rewrites *only* the `judge_metadata_signature` line (binding + audit lines stay
byte-identical). It re-derives the full judge-gated set from the live tree at fire
time and is idempotent/re-runnable — Pass-1 accepts an entry verifying under OLD
*or* NEW, Pass-2 skips already-NEW entries — so a partial/interrupted run
self-heals on re-run instead of bricking. An entry verifying under **neither**
key aborts the whole run (no laundering of a broken entry). An env-name
flag/bundle mismatch aborts before any write. Requires **both** key env vars
(named by `--old-key-env` / `--new-key-env`); fails closed without them. The key
bytes never appear on the CLI — only the *names* of the env vars holding them.

| Flag | Default | Meaning |
| --- | --- | --- |
| `--in` | required | Path to the staged rekey bundle JSON (its `RekeyPlan` env-var names cross-check the flags). |
| `--old-key-env` | required | NAME of the env var holding the OLD (current) key bytes. |
| `--new-key-env` | required | NAME of the env var holding the NEW (target) key bytes. |
| `--root` | `src/elspeth` | Source tree (for the canonical-pair baseline-regen gate). |
| `--allowlist-dir` | `config/cicd/enforce_tier_model` | Per-module allowlist YAML to re-key in place. |
| `--dry-run` | off | Print verify + planned re-key count; write nothing. |
| `--yes` | off | Skip the interactive confirm before the destructive write phase. |

### `RekeyPlan.keys` / `broken_keys` are advisory preview — never a guarantee

The rekey bundle stores two lists for the operator's convenience: `keys`
(entries the agent's **shape-only** survey believed currently valid) and
`broken_keys` (entries it believed already broken). These are **display and
provenance only**, not a contract:

- The shape-only survey cannot determine HMAC validity (it has no key), so an
  entry that is shape-valid but HMAC-invalid under the old key can be mislabeled
  into `keys`.
- At fire time the `rekey` CLI **re-derives the full judge-gated set from the
  live tree** and Pass-1 (`verify_entry_signature_with_key`) is the authoritative
  HMAC gate. A tree entry absent from `keys` is still re-keyed; a `keys` member
  that fails Pass-1 still aborts the run.

Read the lists as a preview of scope, never as the set that will actually be
acted on.

## Recovery versus re-staging

Use the printed `--resume` command when the bundle and live tree are still
current and the active allowlist still matches the transaction base (or the
authenticated published candidate awaiting its audit append). This reuses
accepted authoritative decisions and retries only unfinished work. Do not
re-run the judge merely because a later action BLOCKed or the process was
interrupted.

A BLOCK decision event is retained in the private transaction even though the
active allowlist is unchanged. A later successful resume publishes the
accumulated event history with the coherent candidate. If the transaction is
abandoned, its BLOCK evidence remains visible only in that printed transaction
directory; retain or remove that directory deliberately according to the
operator's audit policy. `sign-bundle` does not silently prune it.

Re-run `stage_scan` when the live source/bindings or active allowlist changed
after the transaction began, or when the original preflight itself rejected the
bundle. A bundle is a point-in-time assertion about the tree. The verify gate
aborts in two expected situations:

- **AST-position cascade staleness (by design).** A bundle staged *before* an
  edit that shifts AST positions in a covered `src/elspeth` source — for example
  adding an `import` — no longer matches the tree. `verify_bundle_against_tree`
  aborts; re-run `stage_scan` against the current tree and fire the fresh bundle.
- **Dup-key in the target file.** If the same allow-hits key occurs more than
  once in a file, `sign-bundle` refuses (`return 2`, both copies preserved).
  Resolve the duplicate in the YAML, re-run `stage_scan`, and fire again.

In those cases re-stage; never force or edit the transaction journal.

## What replaced the one-off signing scripts

This seam replaces the per-release signing runbooks and the one-shot
`scripts/cicd/sign_accept_backlog.py` driver. Instead of hand-edited ceremony
scripts, an agent stages a bundle via `elspeth-judge` and the operator fires
`sign-bundle` / `rekey` in a key-bearing shell. `scripts/codex_tier_model_rejudge.py`
is retained (it belongs to a separate active workflow); the `notes/060-*` signing
runbooks were gitignored scratch and have been removed.
