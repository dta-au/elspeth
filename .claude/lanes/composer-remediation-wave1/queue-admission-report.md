# SG-07 / tracker 08.01 queue scope admission — implementation handoff

2026-11-09. Implemented and source frozen for independent review and parent integration. Tracker 08.01 remains open. No commit, push, provider SDK/network dispatch, installation, Page edit, signing, metadata repin, broad suite or independent long whole-source proof run was performed.

## Candidate and scope

Exclusive worktree: `/workspace/elspeth/.claude/worktrees/composer-remediation-queue-admission`.
Initial and final detached HEAD: `1bc91710fbbdd205376f71dadf8aa3ca557229d8`. Initial `git status --short` was empty. This is the parent's reviewed D2/cache/foundations integration; no older transform/session bodies were copied over it.

The only production change is three explicit keywords in `_execute_upsert_queue_node`, beside `description`:

```python
scope_name=validated.scope_name,
scope_opener=validated.scope_opener,
scope_policy=validated.scope_policy,
```

The existing intrinsic queue guard, saved-graph refusal helper, early return, schemas, bulk builder, result envelopes and D2 path admission remain their authorities. There was no necessary production scope expansion. Previously silently discarded admitted scope values now reach the existing guard and receive its `queue_config_invalid` refusal. Omission/null and canonical incomplete queues retain their behavior.

Final changed paths, relative to the worktree:

- `src/elspeth/web/composer/tools/transforms.py` — three added constructor fields.
- `tests/unit/web/composer/test_queue_scope_admission.py` — new core admission, full private snapshot, complete real-route parity, canonical and malformed-channel tests.
- `tests/unit/web/composer/test_queue_scope_approval.py` — new scripted real freeform preview tests using real catalog, fenced sessions and a changed-description positive control.
- `tests/unit/composer_mcp/test_queue_scope_admission.py` — new actual SDK handler, private reconstruction/readback, saved-file/checkout and malformed audit tests.
- `tests/unit/web/sessions/routes/composer/test_proposal_accept_terminal_refusal.py` — new real legacy acceptance replay tests.

Full patch including all new files: `/workspace/elspeth/.claude/lanes/composer-remediation-wave1/queue-admission-review.diff` (25,326 bytes; SHA-256 `37f322be46b8b4ee00d73509578aea8e4023362c225747251e8bafd9954c707d`). `git apply --reverse --check` against the frozen candidate exited 0. `git diff --check` exited 0.

## Behavior evidence

The core table covers every nonempty subset, both admitted policy values, individual fields with the other two explicitly null, and empty name/opener strings. Both the registered handler and schema-enforced public dispatcher exercise saved graphs with and without an existing canonical queue. Existing queues are seeded through a real canonical dispatch, not hand-built candidate nodes. Refusals assert the exact sorted non-null field list, leading code/component/subject, identity in-process, complete private `to_dict()` equality and stable hash, composition hash, version, standing findings, empty affected nodes, no data/prior validation/delta/applied echo. The content hash is computed before snapshotting to account for its memo.

Complete route fixtures use real catalog/policy snapshots, actual files under the current session's `data_dir/blobs/<session_id>/` directory, and a real source/queue/consumer/output graph. Each fixture first proves the exact zero-scope bulk and incremental controls succeed with valid completeness telemetry. Bulk and incremental refusals compare intrinsic messages while retaining their distinct envelopes. A separate bulk arbitrary-policy test retains its broader transport admission boundary.

The actual MCP `CallToolRequest` handler is exercised with the real dispatcher. A transparent observer captures its private input, checkout and dispatch result before outbound redaction. Subsequent real `get_pipeline_state` dispatch inputs establish full private equality/hash/version across SDK reconstruction. Every attempted call has exactly one audit invocation; semantic refusal is ordinary text with dispatch SUCCESS status, while malformed values are `isError=True`/ARG_ERROR before dispatch. Saved file bytes, inode/mtime and checkout remain unchanged through refusals. A separate canonical edit followed by explicit save/load establishes surviving persistence authority.

The real freeform explicit-approval loop uses a finite scripted primary completion and the established CLEAN advisor fixture. A changed canonical queue description stages one pending proposal without committing composition; the same edit plus any individual scope field or all three is refused with no proposal, composition row/current-state-id/version/hash change. The graph has no resolved graph-bound reviews, so this does not decide D29. Legacy pending proposals replay through the actual REST accept route: each individual field and the combined case return HTTP 422 `proposal_validation_failed`, terminal rejection, preserved private graph and one composition row; repeat acceptance returns 409 without redispatch. Proposal/audit/chat writes are allowed. A malformed legacy proposal retains the route's existing ordinary schema-validation refusal channel rather than inventing a typed argument-exception channel.

Wrong scalar/container scope types, invalid incremental policy spelling and an unknown top-level property cover direct MODEL_VALIDATION, default dispatcher MODEL_VALIDATION, ordinary schema-enforced dispatcher refusal, raised SCHEMA_SHAPE, and SDK ARG_ERROR channels. Canonical tests cover omitted/default options, explicit nulls, description-only options, top-level description, changed canonical replacement, unset routing/shape/scope fields, and orphan success with incomplete telemetry.

## Completed commands and receipts

All evidence is in `/workspace/elspeth/.claude/lanes/composer-remediation-wave1/queue-admission-evidence/`. Every pytest invocation was serial (`-n 0`) with both worktree roots set explicitly through `PYTHONPATH` and `-o pythonpath`. `provenance.log` proves both imports resolved inside this worktree. Final tests use the project lane's `queue-admission-evidence/pytest` basetemp. Early exploratory fixture runs used the worktree-local `.claude/queue-admission-pytest`; that generated directory was removed and is not in the final patch.

For the commands below:

```bash
cd /workspace/elspeth/.claude/worktrees/composer-remediation-queue-admission
ROOT=$PWD
EVID=/workspace/elspeth/.claude/lanes/composer-remediation-wave1/queue-admission-evidence
export PYTHONPATH="$ROOT/src:$ROOT/elspeth-lints/src"
```

The corrected full red and green cohort used this exact test selection and flags, redirecting all output to the respective named log and recording `$?` in its `.exit` receipt:

```bash
.venv/bin/python -m pytest \
  tests/unit/web/composer/test_queue_scope_admission.py \
  tests/unit/web/composer/test_queue_scope_approval.py \
  tests/unit/composer_mcp/test_queue_scope_admission.py \
  tests/unit/web/sessions/routes/composer/test_proposal_accept_terminal_refusal.py::test_legacy_queue_scope_replay_terminalizes_without_changing_private_composition \
  -n 0 -o pythonpath="$ROOT/src $ROOT/elspeth-lints/src" \
  --basetemp="$EVID/pytest"
```

| Receipt/log stem | Actual completed result | Interpretation |
| --- | --- | --- |
| `core-red` | exit 1; 91 failed, 9 passed | First harness version: 32 genuine silent-admission regressions, 32 incomplete NodeSpec fixture failures, 16 genuine complete-route admission regressions, 11 wrong test enum-name assertions. Kept, not counted as 91 genuine product regressions. |
| `all-red` | exit 1; 91 failed, 31 passed | Remaining fixture failures included missing session-fence setup and the malformed legacy test's incorrect expected HTTP error category. SDK and nonmalformed legacy replay reproduced admission/commit. |
| `routes-red` | exit 1; 9 failed, 1 passed | Legacy four scope variants committed HTTP 200. Approval tests lacked the shared CLEAN advisor stub and stopped at the exact-custody guard before SDK/network dispatch. |
| `approval-red` | exit 1; 4 failed, 1 passed | Corrected real approval fixture: changed canonical description staged successfully; four scope-bearing edits incorrectly staged proposals. |
| `all-green-initial` | exit 1; 32 failed, 90 passed | Patched source passed the independent real routes; existing-queue core fixtures still lacked required NodeSpec constructor fields. Replaced that fixture with real canonical dispatch seeding. |
| `all-red-final` | exit 1; 89 failed, 33 passed in 39.80s | Corrected cohort on production source restored byte-for-byte to HEAD (`git diff --quiet -- src/.../transforms.py` exit 0). All 64 core scope cases and 16 real-route incrementals fail on admission; four approval variants stage, SDK's first forbidden attempt succeeds, four legacy variants commit. Canonical/malformed/bulk controls pass. |
| `all-green` | exit 0; 122 passed in 38.90s | Final three-keyword candidate, corrected fixtures. |
| `mutant-scope_name` | exit 1; 1 failed | Omit only `scope_name`, run `test_scope_refusal_preserves_full_private_saved_state[override0-False-dispatch]`: actual `success=True`. |
| `mutant-scope_opener` | exit 1; 1 failed | Omit only `scope_opener`, run the same test's `[override1-False-dispatch]`: actual `success=True`. |
| `mutant-scope_policy` | exit 1; 1 failed | Omit only `scope_policy`, run `[override2-False-dispatch]`: actual `success=True`. |
| `affected-contracts` | exit 0; 499 passed in 58.77s | Restored single-field regressions plus existing affected tests/contracts, exact selection below. |

Each omission mutant used the public dispatcher, one selected non-null field and no other non-null scope values; the other forwarding keywords remained present. Each mutant was restored immediately. All five candidate-file SHA-256 checks matched after all three restorations. Mutant commands were:

```bash
.venv/bin/python -m pytest 'tests/unit/web/composer/test_queue_scope_admission.py::test_scope_refusal_preserves_full_private_saved_state[override0-False-dispatch]' -n 0 -o pythonpath="$ROOT/src $ROOT/elspeth-lints/src" --basetemp="$EVID/pytest"
.venv/bin/python -m pytest 'tests/unit/web/composer/test_queue_scope_admission.py::test_scope_refusal_preserves_full_private_saved_state[override1-False-dispatch]' -n 0 -o pythonpath="$ROOT/src $ROOT/elspeth-lints/src" --basetemp="$EVID/pytest"
.venv/bin/python -m pytest 'tests/unit/web/composer/test_queue_scope_admission.py::test_scope_refusal_preserves_full_private_saved_state[override2-False-dispatch]' -n 0 -o pythonpath="$ROOT/src $ROOT/elspeth-lints/src" --basetemp="$EVID/pytest"
```

The final restored affected/contract cohort command was:

```bash
.venv/bin/python -m pytest \
  'tests/unit/web/composer/test_queue_scope_admission.py::test_scope_refusal_preserves_full_private_saved_state[override0-False-dispatch]' \
  'tests/unit/web/composer/test_queue_scope_admission.py::test_scope_refusal_preserves_full_private_saved_state[override1-False-dispatch]' \
  'tests/unit/web/composer/test_queue_scope_admission.py::test_scope_refusal_preserves_full_private_saved_state[override2-False-dispatch]' \
  tests/unit/web/composer/test_tools.py::TestUpsertNodeQueue \
  tests/unit/web/composer/test_promote_set_pipeline.py::TestSetPipelineQueue \
  tests/unit/web/composer/test_state_collectors.py \
  tests/unit/web/composer/test_transform_rejection_contracts.py \
  tests/unit/composer_mcp/test_server.py::TestQueueExposure \
  tests/unit/composer_mcp/test_call_tool_audit.py \
  tests/unit/web/composer/test_tool_schema_contract.py \
  tests/unit/web/composer/test_tool_model_schema_bounds.py \
  tests/unit/web/composer/test_tool_declarations.py \
  tests/unit/web/test_sessions_composer_attribute_contracts.py \
  -n 0 -o pythonpath="$ROOT/src $ROOT/elspeth-lints/src" \
  --basetemp="$EVID/pytest"
```

Final non-pytest commands and actual receipts:

```bash
FILES=(
  src/elspeth/web/composer/tools/transforms.py
  tests/unit/web/composer/test_queue_scope_admission.py
  tests/unit/web/composer/test_queue_scope_approval.py
  tests/unit/composer_mcp/test_queue_scope_admission.py
  tests/unit/web/sessions/routes/composer/test_proposal_accept_terminal_refusal.py
)
.venv/bin/python -m ruff check "${FILES[@]}"
.venv/bin/python -m ruff format --check "${FILES[@]}"
.venv/bin/python -m mypy src/elspeth/web/composer/tools/transforms.py
.venv/bin/python -m scripts.check_contracts
ELSPETH_JUDGE_METADATA_SIGNATURE_VERIFY_MODE=shape-only-when-key-missing \
  .venv/bin/python -m elspeth_lints.core.cli check --rules trust_tier.tier_model \
  --root src/elspeth --files src/elspeth/web/composer/tools/transforms.py --format json
```

`ruff`, `ruff-format`, `mypy`, `contracts` receipts all exit 0. Ruff reports all checks passed / five files already formatted. Mypy reports no issues in one source file, plus existing unused configuration-section notes. `scripts.check_contracts` passes all checks including the unchanged soft-mapping census. No pin updates were needed.

The keyless tier command was run once on unchanged production and once on the final production candidate; both exit 1 with the standing corpus. Despite the explicit file selection, its repository rule emits the corpus. Both JSON logs contain 5,224 findings. Comparing complete emitted finding objects except source line/column positions gives zero additions and zero removals (`tier-comparison.json`). The comparison was controlled against an added distinct synthetic finding and equal/line-shift-only negative inputs (`tier-comparator-controls.json`). Stderr compares byte-identically (receipt exit 0). This is a no-new-finding diagnostic, not zero findings, signed allowlist clearance, or operator verification. No signing material or metadata was edited.

## Source freeze and remaining parent gates

`final-files-before.sha256` and `final-files-after.sha256` are identical; `freeze-check.log` checks all five files successfully, receipt exit 0. Final HEAD is recorded in `final-head.txt`; exact final path status is in `status-final.txt`. Production file SHA-256: `ad55d760d9db768111aaf32344d07148c032d9d3e5120f74d9c0217d0b970bb4`. The new files are intentionally untracked for the parent to integrate; no files were staged and no commits created. Source is frozen as of this report.

The focused pytest slot was released to the parent after the 499-test cohort. No further lane pytest process remains. Remaining work belongs to the parent: independent final-diff review; integration on its chosen tip; the assigned frozen-tree full/official partition including the whole-source masquerade/live-tree proofs and any required broader corpus checks; final operator signature verification where applicable. This lane did run the Composer dynamic-attribute contract gate, but did not independently start the deferred whole-source masquerade proof. No schema, SQL, session/Landscape persistence implementation or locks changed, so this lane did not trigger a new PostgreSQL implementation obligation. Parent integration policy still governs its final gate selection.

No unresolved code conflict or additional production behavior change was found. D29 review reconciliation, other kind-field matrix cells, sink mirrors, planner exposure, migrations and no-op/progress policy remain out of scope. These focused results do not close SG-07 or tracker 08.01 before independent review and parent integration verification.
