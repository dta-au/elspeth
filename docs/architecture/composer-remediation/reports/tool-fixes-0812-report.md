# Composer tool fixes (0.8.12 workstream)

Work is confined to branch `fix/composer-0812-tool-fixes`, starting at
`2f38b807b4123c9a2ac08853cd835ef16ca22933`. No push, deployment, service
operation, schema migration, or change to planner authority is included.

## Evidence conventions

All commands run from this worktree's root. Logs are local, ignored files under
`.claude/lanes/tool-fixes/`. The test command used for every run below is:

```bash
export PYTHONPATH="$PWD/src:$PWD/elspeth-lints/src"
export TMPDIR="$PWD/.claude/lanes/tool-fixes/tmp"
./.venv/bin/python -m pytest <selection> -n 0 \
  -o "pythonpath=$PWD/src $PWD/elspeth-lints/src" > <log> 2>&1
result=$?
echo "exit=$result"
```

`bash .claude/lanes/tool-fixes/pytest-run.sh <run-name> <selection>` wraps this
command, records its actual exit code in `<run-name>.exit`, and reads the log
after process termination. Runs are serial. Import checks resolved both
`elspeth` and `elspeth_lints` to this worktree. Initial test-fixture construction
errors were corrected before accepting the red runs below as defect evidence.

For each unit, `./.venv/bin/ruff check <touched Python files>`,
`./.venv/bin/ruff format --check <touched Python files>`, and
`PYTHONPATH="$PWD/src:$PWD/elspeth-lints/src" ./.venv/bin/python -m mypy
<touched production files>` write `uN-ruff.log`, `uN-format.log`, and
`uN-mypy.log`. Local commits use the existing repository trailer verbatim:
`Co-Authored-By: Codex <noreply@openai.com>` (example: `05457bf86`).

## U1 — Chroma client-mode null persistence directory

**Reproduced.** The real `ChromaSinkConfig` accepts client mode with
`persist_directory=None`; `set_output` rejected it with
`sink_path_policy_invalid`, and saved-state validation returned
`Sink 'primary' persist_directory must be a string path; got NoneType`.
`u1-red.log`: exit 1, both regression cases failed for those reasons.

Added `is_absent_sink_path` in `src/elspeth/web/paths.py`. Its exception is
exactly `chroma_sink` / `client` / `persist_directory` / null. Admission callers
now supply sink identity; authored validation and the runtime YAML resolver
share the same predicate. `_validation_materialization.py` already delegates
to that resolver and needs no edit. Every non-null path still goes through
confinement; other plugins, keys, and persistent mode retain null rejection.
Plugin configuration validation remains in force. Final U1 production diff:
30 added lines, two removed lines, including the optional-read cleanup below
(measured with `git diff --numstat -- src`).

Regression module: `tests/unit/web/composer/test_chroma_client_path_admission.py`:
`test_chroma_client_explicit_null_directory_is_admitted[admission]` and
`[saved_preview]` use the real Chroma config and runtime validation; the latter
also calls `preview_pipeline`. `test_chroma_null_exception_cannot_bypass_required_paths`
covers another plugin, persistent mode, a required path alias, and an external
non-null directory.

Commands and evidence:

- `pytest-run.sh u1-red tests/unit/web/composer/test_chroma_client_path_admission.py`:
  exit 1 (two expected failures).
- `pytest-run.sh u1-green` with that regression module plus
  `tests/unit/web/composer/test_tools_mutations.py`,
  `tests/unit/web/execution/test_validation_authoring.py`,
  `tests/unit/web/execution/test_preflight_side_effects.py`, and
  `tests/unit/web/execution/test_local_database_sink_quota_admission.py`:
  exit 0, **419 passed**. This includes the existing null/path-bypass rejections.
- Negative control: temporarily made `is_absent_sink_path` return `False`;
  `pytest-run.sh u1-negative` on the regression module: exit 1, both original
  path refusals returned. Restored the predicate immediately afterward.
- Ruff check, final format check, and mypy: exit 0. The first format check
  requested formatting in `_common.py`; formatting was applied and rechecked.

No contract or schema changes; no deferred functional work.

The initial trust-tier comparison found five new R1 observations from optional
mapping reads. Mode/plugin absence is now read explicitly, consistently with
the surrounding parser, and mode is read once per component. The final
comparison is recorded below. Independent full-file source review found no
correctness/security issue (`u1-review.md`, `u1-common-sessions-review.md`).
`u1-restored.log` exited 0 with 255 passing regression, attribute-contract,
and masquerade tests; the final optional-read cleanup occurred during that
broader run, so it is advisory rather than frozen-final-tree evidence.
`python -m scripts.check_contracts` (`u1-contracts-direct.log`) exited 0.

**Commit blocked by environment.** Explicit-pathspec `git add` failed with
exit 128: Git could not create the shared repository's worktree index lock
because that metadata directory is read-only. The sandbox cannot grant access
in this session. No commit or push was attempted after staging failed. The
branch-safety preflight itself exited 0 (`u1-branch-safety.log`). All changes
remain unstaged in the specified worktree; the same blocker applies to later
units. Intended commit: `fix(composer): admit Chroma client null directories without bypassing confinement`.

Final U1 focused verification after optional-read cleanup: `u1-final.log`,
exit 0, **423 passed**, using the same five modules as `u1-green`.

## U2 — Pending mutation proposals retain inline blob dependencies

**Reproduced.** `proposal_blob_refs.py` omitted all three option-patch tools
from its reference allowlist and therefore from its SQL retention query. The
same defect affected the `options` positions of `upsert_node`, `set_source`,
and `set_output`; contrary to the reported comparison, they were not working
controls on this tree. The existing top-level blob tools remain positive
controls. No broader mutation-tool inventory redesign is included.

Added closed options/patch tool vocabularies to the existing shared allowlist
and recursively collect references only from each tool's owned `options` or
`patch` position. The existing collector, UUID normalization, ownership/status
checks, and pending-proposal query are reused. Unknown names and references
outside owned positions do not gain retention authority; malformed owned
positions still raise. Production diff: 14 added lines, one removed line.

Regression tests:

- `tests/unit/web/sessions/test_proposal_blob_refs.py::test_mutation_options_retain_only_owned_reference_positions`
  (six tools; includes unknown-name, wrong-position and malformed-value controls).
- `tests/unit/web/blobs/test_service.py::TestDeleteBlob::test_delete_blob_rejects_blob_referenced_by_pending_proposal`
  (extended with six mutation tools). This persists a pending proposal through
  the existing fixture, invokes the real blob service, and requires deletion
  refusal plus retained bytes and blob record.

`pytest-run.sh u2-red` with those two tests: exit 1, **12 failed, 4 passed**.
Failures were empty extracted reference tuples and `DID NOT RAISE
BlobPendingProposalError`; existing retention tools passed. Ruff check, format
check, and mypy (`u2-*.log`): exit 0.

PostgreSQL verification is unavailable: `docker info` (`u2-docker-info.log`)
exited 1 with `permission denied while trying to connect to the docker API at
unix:///var/run/docker.sock`. No container or existing service was modified.
The required PostgreSQL suite remains unverified. Local commit remains blocked
by the shared Git metadata permissions described under U1. Intended commit:
`fix(composer): retain blob dependencies of pending option mutations`.

`pytest-run.sh u2-green tests/unit/web/sessions/test_proposal_blob_refs.py
tests/unit/web/blobs/test_service.py`: exit 0, **337 passed, 1 skipped** in
109.60 seconds. The skip is pre-existing and requires
`ELSPETH_TEST_POSTGRES_URL`; no skip was added or modified.
Negative control: removed only `_PATCH_BLOB_TOOLS` from the shared reference
allowlist; `pytest-run.sh u2-negative` on the two regressions with
`-k patch_node_options`: exit 1, **2 failed, 14 deselected**, proving both
extraction and actual deletion protection depend on the fix. Restored the
allowlist immediately; `u2-restored` reruns all 16 focused cases.

`u2-restored.log`: exit 0, **16 passed**; repeated Ruff check, format check,
and mypy all exited 0. Independent full-file review: `u2-u3-review.md`, no
actionable finding.

## U3 — Recovery tools are known in persisted planner receipts

**Reproduced.** The planner already declares and advertises recovery tools,
but its `capture_received_response(..., known_tools=...)` call excluded them.
The persisted `handoff_to_user` receipt was classified `unknown_tool`.
`u3-red.log`: exit 1, **1 failed, 1 passed**; the genuine unknown-name control
already retained `unknown_tool` and its `DISCOVERY_ONLY` refusal.

The production change unions `frozenset(PLANNER_RECOVERY_TOOL_NAMES)` into the
existing known-name set at the receipt site. It does not change dispatch,
recovery argument parsing, singleton-batch enforcement, provider calls, or
candidate construction. One production line changes; no contract/schema
changes. The initial tuple union was rejected by mypy; it was corrected before
the accepted final verification. The concurrently started first neighbour run
had imported that earlier code and is not final evidence.

Tests in `tests/unit/web/sessions/test_graph_recovery_turn.py`:
`test_planner_received_tool_category_is_persisted` exercises the real composer
turn and persistence for `handoff_to_user` and a hallucinated name, asserts one
provider call, exact category and operation/epoch binding, and preserves the
unknown-name refusal. The existing
`test_provider_park_alias_restores_exact_advertised_graph_and_retains_raw_evidence`
now also requires the persisted restore category, across its existing route,
dialect and park-ID spelling cases.

Ruff check, final format check, and mypy: exit 0 (`u3-*.log`). Independent
full-file planner review (`u3-planner-review.md`, `u2-u3-review.md`) found no
authority widening or other actionable issue. Intended commit:
`fix(composer): classify planner recovery calls using the declared vocabulary`.
Local commit remains blocked by the environment described under U1.

`pytest-run.sh u3-focused` on the new persisted-classification test: exit 0,
**2 passed**. `pytest-run.sh u3-neighbours` with
`tests/unit/web/sessions/test_graph_recovery_turn.py`,
`tests/unit/web/composer/test_planner_graph_recovery.py`,
`tests/unit/web/composer/test_private_received_diagnostics.py`, and
`tests/unit/web/composer/test_pipeline_planner.py::test_mutation_or_unknown_tool_is_rejected_without_dispatch_or_retry`
plus `--maxfail=1`: exit 0, **135 passed**, one third-party Pydantic warning.
This is the corrected, unchanged-production-tree run; the earlier
`u3-green.log` (38 failures from the tuple mismatch) is not passing evidence.

The pre-U4 trust-tier ratchet used the starting HEAD's own archived source,
rules and allowlists as its baseline (`pre-u4-tier-ratchet.log`): exit 0,
`head 1994, staged 1994, added 0, removed 0`. Here `staged` is the ratchet
tool's label for the working tree; the Git index is unchanged. The production
diff hash was unchanged across the comparison. Existing stale allowlist
warnings remain; no signatures were edited or operator clearance claimed.
`pre-u4-contracts.log` also exited 0.

U3 negative control: removed only the recovery vocabulary union, then ran the
two persisted-category cases (`u3-negative.log`): exit 1, **1 failed, 1 passed**.
The handoff category reverted to `unknown_tool`; the unknown-name control
still passed. Restored the union; `u3-restored.log`: exit 0, **2 passed**.
Repeated Ruff check, format check and mypy: exit 0.

## U4 — JSON preview diagnostics conform to the existing response contract

**Reproduced.** Three JSON diagnostic branches produced malformed responses:
inspection failure omitted `evidence_locator.source`; universal fixed-schema
extras also emitted unsupported record-count/inspection keys; partial extras
also emitted unsupported severity `warning`. The regression invokes real
JSON source inspection and then `admit_discovery_result`, matching the actual
response boundary. `u4-red.log`: exit 1, **3 failed**, all with
`FrameworkBugError: Malformed generation discovery response`.

Producer-only change in `src/elspeth/web/composer/tools/generation.py`: supply `source="blob"` in all
three locators, omit unsupported locator members, and express the partial
inspection warning as `info`, the existing inspection-warning convention.
Universal all-row loss and inspection failure remain blocking, and the tests
require `preview_is_valid=False` for both. Partial observed loss stays
informational; no claim that every row is lost is introduced. The existing
response contracts, admission, validation and redaction are unchanged.
Production diff after formatting: 9 added lines, 6 removed lines.

Test: `tests/unit/web/composer/test_source_proof_readiness.py::test_json_source_preview_returns_normal_diagnostic_envelope`,
three cases: universal undeclared fields, mixed records with partial undeclared
fields, and invalid saved JSON options. The test requires a normal admitted
preview, exact diagnostic/severity, blob and named-source attribution, and
the correct preview verdict. `u4-focused.log`: exit 0, **3 passed**.
Ruff check, format check and mypy (`u4-*.log`): exit 0. Source review covered
the full producer and response-contract modules (`u4-generation-review.md`,
`u4-u6-review.md`); no actionable finding.

Intended commit: `fix(composer): emit contract-valid JSON preview diagnostics`.
Local commit remains blocked by shared Git metadata permissions.

`pytest-run.sh u4-neighbours tests/unit/web/composer/test_source_proof_readiness.py
tests/unit/web/composer/tools/test_tools_preview.py
tests/unit/web/composer/test_generation_response_contracts.py --maxfail=1`:
exit 0, **252 passed**. Negative control restores only the JSON partial
branch's old `warning` severity, retaining the other repairs, then reruns
the three regression cases (`u4-negative.log`).

That negative control exited 1, **1 failed, 2 passed**, specifically at response
admission for the partial JSON branch. Restored `info`; `u4-restored.log`:
exit 0, **3 passed**. Repeated Ruff check, format check and mypy: exit 0.

## U5 — Removing an output removes its incident edges

**Reproduced.** `CompositionState.without_node` filters both edge endpoints;
`without_output` only removed the output. The `remove_output` handler delegates
directly and does no reconciliation. `u5-red.log`: exit 1, **2 failed**, because
both state mutation and the real tool kept edges referencing the removed sink.

Mirrored the established node-removal convention in `without_output`: filter
both `from_node` and `to_node`, preserving unrelated edge order/identity and
incrementing the version once. Two production lines added, one removed. No
output handler change, new refusal convention, contract, or schema is needed.
Scalar routes remain authored values and normal destination validation still
reports a dangling route; no automatic retargeting or discard is introduced.

Tests:

- `tests/unit/web/composer/test_state.py::TestCompositionState::test_without_output_removes_incident_edges_and_preserves_unrelated_edges`
  requires exact retained edges, both incident directions removed, retained
  output identity, one version increment and the original state unchanged.
- `tests/unit/web/composer/test_tools_mutations.py::TestRemoveOutput::test_remove_output_removes_edges_to_sink`
  exercises the handler and requires removal plus an exact nonempty retained
  edge tuple, preventing an overbroad delete-all repair.

`u5-focused.log`: exit 0, **2 passed**. Ruff check, format check and mypy
(`u5-*.log`): exit 0. Independent source review was split across
`u5-review.md` and `u5-remaining-review.md` to cover the complete state module;
no blocking concern. Sink edge endpoints are bare names and node/sink name
collisions are already rejected by validation.

Intended commit: `fix(composer): remove incident edges with deleted outputs`.
Local commit remains blocked by shared Git metadata permissions.

`pytest-run.sh u5-neighbours tests/unit/web/composer/test_state.py
tests/unit/web/composer/test_tools_mutations.py
tests/unit/web/composer/test_edge_route_reconciliation.py --maxfail=1`:
exit 0, **506 passed**. Negative control omitted only the filtered `edges`
argument from `without_output`'s returned state, then reran the two focused
tests (`u5-negative.log`): exit 1, **2 failed**, with the original dangling
edges retained. Restored `edges=edges`; `u5-restored.log`: exit 0, **2 passed**.
Repeated Ruff check, format check and mypy: exit 0.

## U6 — Oversize content has its own truthful inspection refusal

**Reproduced.** Oversize non-credential ASCII and multibyte UTF-8 blobs both
returned `credential_material_rejected` because the traversal-limit finding
was flattened into a credential refusal. `u6-red.log`: exit 1,
**2 failed, 2 passed**. The exact-boundary and actual-credential controls
already passed.

Verified the live constants and the real wrapper signature before choosing a
limit (`u6-limits.log`, exit 0):

```text
credential wrapper default: 262144
credential traversal policy: 262144
read_attachment limit: 262144
JSON aggregate limit: 1048576
```

The `4096`-byte serialized page bound and `50000`-character page candidate
slice are unrelated to the whole-input credential traversal limit. The fix
reads `DEFAULT_CREDENTIAL_TRAVERSAL_POLICY.max_string_bytes` directly and
refuses verified bytes strictly above it, before decoding/scanning, with
`blob_content_size_limit`, actual size and the inspection limit. No content is
returned. Custody/lifecycle/integrity checks still precede the refusal, and
every in-cap file still undergoes full-content credential inspection before
any page slice. No bounds, response contracts or credential checks changed.

**Correction (batch 2 review).** The commit message of 38045378b says it
"preserves the credential guard across page boundaries". That overstates the
diff: the commit adds only the byte-size gate in `blobs.py`. The full-content
credential guard before slicing already existed and was not changed by U6;
the existing `test_credential_after_page_boundary_refuses_the_first_page`
covers it. History is not rewritten; this note is the correction.

Test: `tests/unit/web/composer/test_tools_blobs.py::TestGetBlobContentGuards::test_content_inspection_size_limit_is_distinct_from_credentials`
has oversize ASCII, oversize multibyte UTF-8 and exactly-at-limit cases.
The existing `test_credential_after_page_boundary_refuses_the_first_page`
remains the real-credential control. `u6-focused.log`: exit 0, **4 passed**.
Ruff check, format check and mypy (`u6-*.log`): exit 0. Full-file source review
and limit tracing are recorded in `u4-u6-review.md`.

`pytest-run.sh u6-neighbours tests/unit/web/composer/test_tools_blobs.py
tests/unit/web/composer/test_blob_response_contracts.py --maxfail=1`:
exit 0, **214 passed**. Negative control disabled only the new size branch;
`u6-negative.log`: exit 1, **2 failed, 2 passed**. Oversize text again received
the credential refusal; exact-boundary and real-credential controls remained
green. The branch was restored immediately afterward.

Intended commit: `fix(composer): distinguish blob inspection size limits from credentials`.
Local commit remains blocked by shared Git metadata permissions.

`u6-restored.log`: exit 0, **4 passed**. Repeated Ruff check, format check and
mypy: exit 0. Final production diff: 10 added lines, zero removed lines.

## Compatibility, scope and remaining delivery limits

The changes preserve existing contracts and persisted schemas. U1 restores
the plugin-supported client configuration while maintaining required-path
refusals. U2 intentionally causes pending options/patch proposals to retain
their blob dependencies, so deletion/replacement can now be refused for those
dependencies. U3 changes private receipt classification, not execution
authority. U4 repairs owned response production without changing admission;
universal loss and inspection failures still block. U5 follows the existing
node-removal edge behavior and retains validation of scalar routes. U6 changes
the public error code for over-limit content to `blob_content_size_limit`;
actual in-cap credential refusals retain `credential_material_rejected`.
No migration, reset, downgrade or live data cleanup is required or performed.

All requested functional units reproduced and were fixed. Commits are the
outstanding delivery action: the shared worktree index is outside this
session's writable sandbox. The intended six independent commit subjects are
listed under their units; no commit was created and nothing was pushed.
`git log --format='%h %s' 2f38b807b4123c9a2ac08853cd835ef16ca22933..HEAD`
and `git diff --cached --name-only` both returned no output (exit 0).
The two reports that were untracked at entry remain untouched.

Default full-suite and PostgreSQL qualification are not claimed. The tests
above are focused/unit-neighbour runs; provider responses in the audit tests
are scripted, and persistence evidence is local SQLite. Docker socket access
was explicitly denied, so the PostgreSQL testcontainer suite could not run.
No merge, service operation, deployment or change under `deploy/` occurred.
The keyless trust-tier comparison does not provide operator signature
clearance. Existing stale signed-allowlist warnings are preserved for the
operator; no key or judge signature was acquired or edited.

Independent final diff review (`final-review.md`) found no actionable issue
across the six production changes and their regression tests. Review and test
evidence are separate claims.

## Final verification

`pytest-run.sh final-regressions-gates <selection> --maxfail=1`: exit 0,
**44 passed** in 98.62 seconds. The selection was:

```text
tests/unit/web/composer/test_chroma_client_path_admission.py
tests/unit/web/blobs/test_service.py::TestDeleteBlob::test_delete_blob_rejects_blob_referenced_by_pending_proposal
tests/unit/web/sessions/test_proposal_blob_refs.py::test_mutation_options_retain_only_owned_reference_positions
tests/unit/web/sessions/test_graph_recovery_turn.py::test_planner_received_tool_category_is_persisted
tests/unit/web/composer/test_source_proof_readiness.py::test_json_source_preview_returns_normal_diagnostic_envelope
tests/unit/web/composer/test_state.py::TestCompositionState::test_without_output_removes_incident_edges_and_preserves_unrelated_edges
tests/unit/web/composer/test_tools_mutations.py::TestRemoveOutput::test_remove_output_removes_edges_to_sink
tests/unit/web/composer/test_tools_blobs.py::TestGetBlobContentGuards::test_content_inspection_size_limit_is_distinct_from_credentials
tests/unit/web/composer/test_tools_blobs.py::TestGetBlobContentGuards::test_credential_after_page_boundary_refuses_the_first_page
tests/unit/web/test_sessions_composer_attribute_contracts.py
tests/unit/test_mock_discipline_baseline.py
tests/unit/elspeth_lints/test_masquerade_gate.py::test_live_tree_has_zero_unbaselined_findings
tests/unit/elspeth_lints/test_masquerade_gate.py::test_self_test_a_fresh_banned_site_is_never_silently_green
tests/unit/elspeth_lints/test_masquerade_gate.py::test_self_test_goes_red_if_a_baseline_entry_covers_it_then_green
```

This includes live AST gates and their positive/negative self-controls. The
tracked source/test diff and new Chroma regression module were hashed before
and after; `diff final-before.sha final-after.sha` exited 0. Production and
test inputs did not change during this run. The initial all-rules scan was
started before U1 edits but overlapped them, so it is discarded as baseline
evidence; the frozen comparisons below supersede it.

Final changed-file quality commands ran with all production paths from
`git diff --name-only -- src`, all changed test paths, and the new Chroma
regression module (listed in the unit sections):

- `./.venv/bin/ruff check <19 touched Python files>`: exit 0,
  `All checks passed!` (`final-ruff.log`).
- `./.venv/bin/ruff format --check <same files>`: exit 0,
  `19 files already formatted` (`final-format.log`).
- `PYTHONPATH="$PWD/src:$PWD/elspeth-lints/src" ./.venv/bin/python -m mypy
  <11 touched production files>`: exit 0,
  `Success: no issues found in 11 source files` (`final-mypy.log`).
- `PYTHONPATH="$PWD/src:$PWD/elspeth-lints/src" ./.venv/bin/python -m scripts.check_contracts`:
  exit 0, including the unchanged soft-mapping census (`final-contracts.log`).
- `TMPDIR="$PWD/.claude/lanes/tool-fixes/tmp"
  PYTHONPATH="$PWD/src:$PWD/elspeth-lints/src" ./.venv/bin/python -m scripts.trust_tier_ratchet`:
  exit 0, `head 1994, staged 1994, added 0, removed 0`
  (`final-tier-ratchet.log`). Uses the original HEAD's own rules/source and
  allowlists as baseline; existing stale allowlist warnings remain.
- `git diff --check`: exit 0.

Measured final production churn (`git diff --numstat -- src`):

```text
1  1  src/elspeth/web/composer/pipeline_planner.py
2  1  src/elspeth/web/composer/state.py
10 1  src/elspeth/web/composer/tools/_common.py
10 0  src/elspeth/web/composer/tools/blobs.py
9  6  src/elspeth/web/composer/tools/generation.py
2  0  src/elspeth/web/composer/tools/outputs.py
1  0  src/elspeth/web/composer/tools/sessions.py
3  1  src/elspeth/web/execution/_validation_authoring.py
5  0  src/elspeth/web/execution/preflight.py
9  0  src/elspeth/web/paths.py
14 1  src/elspeth/web/sessions/proposal_blob_refs.py
```

Each unit remains within the requested production-change bound, and no
contract/schema files changed. HEAD remains the starting commit; local commit
list is empty because staging was refused by the filesystem sandbox.

To preserve the requested unit boundaries despite that blocker, the local
lane contains `u1-commit.patch` through `u6-commit.patch`, each limited to its
unit's explicit production/test pathspecs. U6's patch also contains this final
report. Matching `uN-commit-message.txt` files contain the intended conventional
subject, rationale, and the repository's exact Codex trailer. These are
reviewable code patches, not commits; all live edits remain unstaged.

## Batch 2 and review fixes

This section supersedes the earlier U4 severity/count decision and earlier
final-verification claims for the current package. Work continues on
`fix/composer-0812-tool-fixes`, measured HEAD
`60bbcb4094b7f355a9c398b777a453fac1726445`; the coordinator has committed
U1, U3 and U5. U2, U4, U6, U7, U8 and U9 remain unstaged. No Git metadata,
live service, deployment, operator signature, or persisted schema was changed.

Commands use the worktree interpreter and both local source roots:

```bash
W=$PWD
TMPDIR=$W/.claude/lanes/tool-fixes/tmp \
PYTHONPATH=$W/src:$W/elspeth-lints/src \
  $W/.venv/bin/python -m pytest <selection> -n 4 \
  -o "pythonpath=$W/src $W/elspeth-lints/src" > <log> 2>&1
result=$?
echo exit=$result
```

Logs and actual terminal exit codes are retained together under
`.claude/lanes/tool-fixes/`. The PostgreSQL selection uses its required
`-n 0`. No pytest output was piped through another command. The broad
Composer/execution/sessions/blobs neighbour run covers every unit together;
subagents ran focused selections only. Source/test/config inputs are hashed
before and after the broad run.

### U2 — null option bags do not abort reference projection

The reviewed extractor treated present null `options`/`patch` as corrupt,
although the existing component-options collector treats null as empty.
Added the same explicit `None` return to the six option mutation tools.
Wrong non-null types still raise. The original closed retention allowlist,
owned reference positions, canonical UUID projection, custody checks and
real blob-deletion regression remain in this unit.

Files: `src/elspeth/web/sessions/proposal_blob_refs.py`,
`tests/unit/web/sessions/test_proposal_blob_refs.py`, and the existing U2
change in `tests/unit/web/blobs/test_service.py`.

Red/negative control: `review-fixes-red.log`, exit 1, contains all six new
null-bag failures on the reviewed implementation. The twelve list/string
wrong-type controls pass in that same run. Green: `review-fixes-restored.log`,
exit 0, 525 passed across all review-fix neighbour modules. The earlier U2
retention red and negative-control evidence remains above. No retention or
ownership authority was weakened. PostgreSQL remains unavailable as recorded
in the final verification below.

### U4 — admit legitimate JSON warnings and count evidence

Restored `warning`, `selected_record_count`, `sample_extra_record_count` and
`all_rows_inspected`. The actual defect was the producer-owned discovery
response model: its severity union and evidence fields omitted these existing
outputs. Added `warning` and the three optional, strictly typed evidence
fields to `_generation_responses.py`; counts reject negative values. Retained
the required `source="blob"` attribution fix in all three JSON branches.

This is a contained response-admission change. It does not modify a persisted
schema, frontend-decoded shape, state/configuration model or tool argument.
Absent optional members remain omitted by the existing field-order encoder;
existing wire-byte fixtures remain unchanged. Partial loss remains a warning
without claiming universal loss; complete loss and inspection failure remain
blocking. The admitted response preserves the counts and inspection scope.

Files: `src/elspeth/web/composer/tools/generation.py`,
`src/elspeth/web/composer/tools/_generation_responses.py`,
`tests/unit/web/composer/test_source_proof_readiness.py`, and
`tests/unit/web/composer/test_generation_response_contracts.py`.

Red/negative controls: `review-fixes-red.log`, exit 1, includes the four
existing sparse-JSON warning regressions and the two new count-preservation
failures; `u4-contract-red.log`, exit 1, independently reproduces both rejected
warning/count response cases before the contract fix. Green:
`review-fixes-restored.log`, exit 0, 525 passed, includes the complete configured
source-proof and response-contract modules, strict malformed-evidence controls
and frozen-wire/readmission tests. An intermediate run
(`review-fixes-green.log`, exit 1) caught misplaced count expressions in a CSV
branch; those were corrected before the restored and final frozen runs.
U4 is completed, not deferred.

### U6 — register the truthful size-limit refusal

The earlier size check emitted an unregistered error code, so persisted
redaction erased its useful distinction from a credential finding. Registered
`blob_content_size_limit` in `_EMITTED_VALIDATION_ERROR_CODES`, alongside the
neighbouring emitted validation codes. Added the actual get-blob refusal to
the registered-code redaction cases. The registry is the shared redaction
allowlist and its existing producer census now passes; no separate frontend
closed-code map needs extending. The original whole-input size guard and
credential-across-page tests remain.

Files: `src/elspeth/web/composer/tools/blobs.py`,
`src/elspeth/web/composer/error_codes.py`,
`tests/unit/web/composer/test_tools_blobs.py`, and
`tests/unit/web/composer/test_error_code_redaction.py`.

Red/negative control: `review-fixes-red.log`, exit 1, contains the exact
`TestRegistryCensus::test_every_emitted_literal_code_is_registered` failure
and the new redaction-preservation failure. Green:
`review-fixes-restored.log`, exit 0, 525 passed, includes the whole registry
census/redaction and blob-tool modules. Unknown-code rejection controls remain
green. The earlier size-versus-credential and page-boundary controls remain
documented above. No schema or frontend change.

### U7 — request-start freeform diff baseline

Both freeform dispatch calls omitted `baseline`, causing unconditional diff
refusal. They now pass `TurnEvidence.effects.initial_state`, already owned
by the compose request and retained across provider rounds and checkpoints.
There is no fallback to changing batch state. Direct batch callers without
request evidence retain the missing-baseline refusal. Guidance now describes
missing surface context without claiming that saved revisions do not exist;
the declaration states the Web Composer request-start interval.

Files: `src/elspeth/web/composer/tool_batch.py`, U7-only guidance/description
hunks in `src/elspeth/web/composer/tools/generation.py`, and new
`tests/unit/web/composer/test_freeform_diff_baseline.py`. No service, state,
contract, schema or persistence code changed.

Final negative control: copied the final new test into the untouched HEAD
archive and used that archive's source, tests and import roots.
`u7-head-negative.log`, exit 1: all four cases fail with unavailable-baseline
responses. `u7-green.log`, exit 0: 4 passed, covering same-batch and later-round
edits, both trust modes, zero start diff, and reset on the next request.
Earlier fixture errors in `u7-red.log` are not used as the final negative
control. Independent source review: `u7-independent-review.md`, no production
defect found. Historical version selection, MCP baseline ownership and planner
self-diff are outside this request-relative fix.

### U8 — splice on wholly edge-free planner representations

The splice guard already verified provider-authored logical connections but
then demanded visual edges; execution and replay demanded them too. All three
seams now distinguish a wholly empty edge tuple. Logical splice and identical
replay preserve `edges=()`. Existing nonempty visual graphs retain their
exact-edge, branch and collision checks. Scalar/type/error-route,
multiple-consumer/sink, order, profile/review and version checks remain.
Responses omit visual IDs when no edges were split, as documented by the
tool declaration. No missing visual topology is generated.

Files: `src/elspeth/web/composer/tools/transforms.py`,
`tests/unit/web/composer/test_splice_transform_tool.py`, and
`tests/integration/web/composer/test_profile_splice_flow.py`.

Red/negative control: `u8-red.log`, exit 1, two new predecessor-kind cases
fail against unchanged production with the visual-edge refusal; four logical
refusal controls pass. Final green: `u8-final-green.log`, exit 0, 57 passed.
Coverage includes logical rewiring, one version increment, preserved empty
edges, identical/divergent retry, profile/review lowering, actual freeform
auto-commit and approval staging, and actual proposal acceptance. Intermediate
fixture/assertion repairs are detailed in `u8-findings.md`. Independent source
review: `u8-independent-review.md`, no actionable defect. No contract, schema,
acceptance ownership or persistence implementation changed.

### U9 — truthful pending-proposal next actions

Pending first-turn plans previously omitted the acceptance/rejection action
and could direct users to cards not yet created. Updated only the four
pending-review message constants. Validated review explains that the proposal
is unapplied and needs acceptance or rejection. Findings request inspection
and correction. Unconfirmed validation requests a retry or revised request.
Interpretation handoff directs acceptance first, then card resolution before
running. Wording applies to both trust modes. Green-only auto-commit,
authorization rereads and late trust revocation remain unchanged.

Files: `src/elspeth/web/composer/protocol.py`,
`tests/unit/web/composer/test_planner_stage_runtime_preflight.py`, and new
`tests/unit/web/sessions/test_pending_pipeline_review_guidance.py`.
The specification explicitly requires the message-constant file; no data
contract, schema or frontend decoder changed.

Red/negative control: `u9-red.log`, exit 1, 7 failed / 16 passed before the
copy fix. Green: `u9-green.log`, exit 0, 23 passed across both trust modes
and verdict branches. `u9-acceptance.log`, exit 0, 1 passed, proves the
advertised sequence through real set-pipeline custody and HTTP acceptance:
zero pending cards before acceptance, then one candidate-bound invented-source
card visible through the interpretation route. Independent source review:
`u9-independent-review.md`, no actionable defect. Route coverage uses SQLite;
no live browser/provider or PostgreSQL acceptance claim is made.

### Delivery boundaries and independent review

Refreshed `u2-commit.patch`, `u4-commit.patch`, `u6-commit.patch` and created
`u7-commit.patch`, `u8-commit.patch`, `u9-commit.patch` under the lane directory.
Every unit has `uN-message.txt` and `uN-commit-message.txt` aliases. Earlier
U1/U3/U5 artifacts are preserved. The final report is delivered separately,
so U6 no longer carries the report's whole file in its code patch. Unrelated
pre-existing untracked reports are preserved and excluded from these patches.

The only shared source file is `src/elspeth/web/composer/tools/generation.py`.
Its exact HEAD-relative hunk split is:

- U4: old lines 3886, 4049, 4061, all in
  `_compute_proof_diagnostics_for_source`, adding blob source attribution.
- U7: old lines 458, 4674, 4694: `_VALIDATION_ERROR_PATTERNS`,
  `_execute_diff_pipeline`, `_DIFF_PIPELINE_DECLARATION`.

The partial-file patches omit that file's Git blob-index line. Applying all
six patches to a fresh HEAD archive succeeded, and every reconstructed
production/test path was compared byte-for-byte with the live worktree:
`batch2-patch-verification.log`, `check exit=0`, `apply exit=0`.
`batch2-patch-inventory.json` records every file owner and confirms that
`generation.py` is the only overlap. This is patch reconstruction evidence,
not a commit or merge claim.

Independent reviews found no actionable production defect:
`u2-review-fix-independent-review.md`,
`review-fixes-independent-review.md` (U4/U6), and
`u7-independent-review.md`, `u8-independent-review.md`,
`u9-independent-review.md`. They inspected source/callers/test bodies without
editing measured files or launching competing tests. The U7 review records
an optional stronger explicit-approval staging assertion; the baseline fix
and actual staging owner were source-verified.

### Neighbour-test correction: U7 prompt-size ceiling

The broad selection found two genuine regressions in
`test_pipeline_planner.py::test_initial_request_declares_supplied_information_and_keeps_discovery_available[live]`
and `[small]`. `scaffolding-budget-red.log`, exit 1: the actual initial
request measured 135,811 canonical bytes against the existing 135,730-byte
ceiling. U7's longer diff-tool description accounted for all 81 excess bytes;
U8's splice tool is absent from that actual planner palette. This was measured
from the failing producer request with unchanged-copy and appended-character
controls, not inferred from source text.

Shortened only U7's declaration prefix to:

> Compare with this surface's baseline (Web Composer: request-start state, fixed across provider rounds).

All response-field teaching remains unchanged. The controlled resulting
request measures 135,723 bytes, seven below the original ceiling. The test's
exact-boundary and plus-one-byte controls and ceiling are unchanged.
`scaffolding-budget-green.log`, exit 0: 91 passed, including both formerly red
cases, all four request-baseline cases and the error-code registry/redaction
module. Detailed measurements: `scaffolding-budget-investigation.md`.
U7's patch/message and the final reconstruction were refreshed. This is a
completed correction to U7, not a deferred neighbour regression.

### Test-environment wakeup restriction and diagnostic harness

The first unmodified broad run stopped making progress and was interrupted;
the execution tool confirmed termination with exit 130. It has no completed
pytest summary and is not passing evidence. Bounded diagnostic attempts were
also stopped when their evidence identified the prompt-size correction above.
Their logs remain separate from final verification.

The execution lease and output-streaming tests also stalled on untouched
HEAD. Exact current/HEAD four-worker probes with a 20-second thread watchdog
exited 1 for the same test IDs:

```text
tests/unit/web/execution/test_session_operation_lease.py::test_expired_execute_lease_takeover_stops_queued_real_worker_before_any_stale_effect
tests/unit/web/execution/test_outputs_routes.py::TestRunOutputContentEndpoint::test_streams_file_bytes_when_inside_sink_allowlist
```

Relevant route, helper, execution and test files are byte-identical to HEAD.
Direct runtime controls in `lease-selfpipe-proof.log` demonstrate the actual
environment restriction:

```text
event_loop_selfpipe_send PermissionError 1
without_heartbeat TimeoutError 0.200991
with_heartbeat returned 0.060799
```

The worker deliberately sleeps 50ms in both controls. Asyncio enqueues its
thread completion callback, then its internal wakeup socket write is denied
by this sandbox; the standard-library implementation catches that `OSError`.
A no-op loop timer allows the completed callback to be processed. This is
directly controlled socket/executor evidence, not a conclusion that ELSPETH
should change its scheduling. Detailed reports and raw exits:
`lease-hang-investigation.md`, `stream-hang-investigation.md`.

The final diagnostic broad run explicitly loads the ignored, lane-private
`tool_fixes_loop_heartbeat.py` plugin. It schedules a no-op 10ms timer while
each asyncio loop runs, cancels its handle on return and restores the wrapped
method when pytest exits. It changes no production/test files, assertions,
markers, provider calls, graph authorship or service settings, and skips no
tests. Exact current lease, HEAD lease and current streaming controls pass
with it (one passed per run, exit 0; `lease-heartbeat-current.log`,
`lease-heartbeat-baseline.log`, `streaming-heartbeat-current.log`).

For this environment adaptation only, the final command adds the lane
directory to `PYTHONPATH` solely to import the plugin and adds
`-p tool_fixes_loop_heartbeat`. Both worktree source roots remain explicitly
bound in `PYTHONPATH` and pytest's `pythonpath`; the original interpreter and
four workers are retained. A 90-second thread watchdog and worker replacement
make any remaining hangs explicit failures. The run therefore provides
functional diagnostic evidence under that declared harness. It cannot
qualify unmodified timing or race behavior; an uninstrumented operator run
outside this restriction remains necessary for those claims.

### Completed broad receipt and subsequent test/gate repairs

The completed combined run, `batch2-final-broad.log` / `.exit`, exited 1:

```text
31 failed, 20354 passed, 1 skipped, 20 warnings, 23 errors in 1216.54s (0:20:16)
```

It ran all of `tests/unit/web/composer`, `tests/unit/web/execution`,
`tests/unit/web/sessions`, `tests/unit/web/blobs`, plus the attribute-contract,
masquerade, mock-discipline, walker-authority and Sessions writer gates and
the splice profile integration file. Its source/test/config/plugin hashes
match before and after (`batch2-final-frozen.log`: `frozen=YES`); HEAD remained
the measured commit above. This is a completed non-green run, not a green
suite claim. Its raw failed/error IDs and dispositions are retained in
`batch2-failure-dispositions.json`.

Six failure IDs were independently reproduced on untouched HEAD before the
review fixes (`batch2-head-known.log`, exit 1, 6 failed / 28 passed):

```text
tests/unit/web/composer/test_checkpoint_owner_compatibility.py::test_checkpoint_no_return_retains_projection_until_actual_generation_closure[True]
tests/unit/web/composer/test_failure_validation_guidance.py::TestFreeformRejectionCarriesCataloguedRepairText::test_invalid_source_options_carry_the_catalogued_fix
tests/unit/web/composer/test_failure_validation_guidance.py::TestFreeformRejectionCarriesCataloguedRepairText::test_plugin_repair_preserves_explicit_requirements_and_reports_capability_gaps
tests/unit/web/composer/test_failure_validation_guidance.py::TestValidationGuidanceCustody::test_provider_sees_the_text_but_persistence_sees_a_placeholder
tests/unit/web/composer/test_tool_dispatch_boundary.py::test_source_path_arguments_require_data_dir_for_s2_confinement
tests/unit/web/composer/test_tool_dispatch_boundary.py::test_source_path_arguments_require_data_dir_without_audit_hash
```

The remaining completed-run failures were investigated rather than added to
that accepted six-ID set:

- U7's exact declaration snapshot still expected the old prose. Updated only
  that expected prefix in `test_tool_declarations.py`. The full declaration
  file, fixed prompt-ceiling tests, all request-baseline cases and associated
  focused gates pass: `batch2-post-broad-focused.log`, exit 0, 118 passed.
  U7's patch now includes this fourth file; no further production change.
- Temporary synthetic gate trees inside the ignored lane directory inherited
  the outer repository's ignore rules. This made planted-positive scanner
  controls see empty trees. Corrected the test environment, not the scanners:
  `GIT_CEILING_DIRECTORIES=$W/.claude/lanes/tool-fixes` isolates lane temp and
  exported baseline trees while preserving Git discovery and ignore filtering
  from the real `src`, `tests`, `scripts` and repository roots. All six positive
  and negative root controls pass (`git-ceiling-final-controls.log`). The
  earlier too-broad ceiling at `$W` is not the final gate configuration.
- The 90-second watchdog interrupted several expensive whole-tree scans.
  Re-ran those actual files without that watchdog, under the corrected Git
  discovery environment and declared heartbeat. `batch2-delivery-gates.log`,
  exit 0: **652 passed, 1 xfailed** in 260.22s. All source/test/config/plugin
  inputs stayed unchanged (`batch2-delivery-frozen.log`: `frozen=YES`). This
  includes the complete masquerade, walker-authority and Sessions-authority
  files, full declaration file and both prompt/baseline regression selections.
  The xfail is the existing architecture test
  `test_all_production_sessions_writers_are_reviewed_typed_authorities`;
  its existing `pytest.xfail` branch was not added or changed. It remains a
  limitation, not Sessions authority clearance.
- The skipped blob-service PostgreSQL-URL case is unchanged. No skip, xfail,
  suppression, or gate assertion was added to hide a failure.

### Separate validation-fixture follow-up

The full direct-writer gate found a pre-existing unlisted fixture insertion
in `tests/integration/web/test_shareable_reviews_routes.py`, inside
`test_preview_reports_llm_authored_file_as_not_importable`. Correctly isolated
current and HEAD scans both exited 1 with the same single row: one direct
`chat_messages` / `sqlalchemy_table_insert`. The source writer and allowlist
were byte-identical to HEAD. This was outside the accepted six failures, so
it was repaired rather than silently reclassified.

Replaced that direct insertion with the existing session-service
`add_message` helper under the central `fenced_operation_context` COMPOSE
authority, using the returned message ID as the blob provenance FK. Removed
the unused table import. No production code or allowlist changed. The first
helper attempt omitted the explicit context; the actual integration test
caught it, and the final helper call supplies the required context.

Final evidence: `validation-fixture-singleton-green.log`, exit 0, 1 passed;
`validation-fixture-static-green.log`, exit 0, all 21 direct-writer tests
passed. Their unreviewed/surplus-writer and lock-discipline positive controls
remain in place. Full findings: `static-writer-investigation.md`.

This one-file test repair is packaged separately as
`validation-fixture-commit.patch` and `validation-fixture-message.txt`.
It is disjoint from all six tool-unit patches. The final seven source/test
patches reconstruct all 22 changed Python paths byte-for-byte from HEAD;
the only overlap remains U4/U7's distinct `generation.py` hunks.

### U6 read-location metadata follow-up

The architecture comparison verified that HEAD already xfails its Sessions
inventory test, but U6's added import had shifted three unchanged blob-read
helpers by one line. Because this inventory includes line locations in read
identities, current drift had three extra unexpected/read-connection rows and
three extra stale read pins. Refreshed exactly the three measured locations
in `tests/unit/architecture/test_session_db_mutation_authority.py`:
502 to 503, 538 to 539, 563 to 564. Fingerprints, read classifications,
production code, gate policy and the existing xfail branch are unchanged.
This is folded into the final U6 patch; the lane's separate read-pin patch is
an intermediate receipt and must not be applied again with U6.

Archived HEAD's exact architecture case exits 0 with one xfail in 146.72s;
the restored current case exits 0 with one xfail in 151.67s. All nine reason
bucket counts match: unexpected 106, connection violations 30, unresolved 74,
unnamed writers 2, stale reads 2, all other buckets zero. Displayed identities
also match after normalizing source lines; those displays are capped, so this
is not a claim of exhaustive authority clearance. Evidence:
`session-writer-baseline-comparison.md`, `u6-read-pin-followup.md`,
`architecture-u6-read-pins-restored.log` / `.exit`.

After this metadata-only repair, the narrow live masquerade gate passes:
`batch2-read-pin-masquerade.log`, exit 0, 1 passed in 80.19s; its complete
source/test/config/plugin snapshot is unchanged (`batch2-read-pin-frozen.log`,
`frozen=YES`). Final production bytes still match the completed broad run;
only the declaration snapshot, shareable-file fixture and these three read
pins changed afterward. Their focused checks are recorded above. No whole
broad rerun was used to conceal the isolated test repairs or sandbox blocks.

### Final static checks and remaining verification limits

On the delivery source, with the worktree interpreter and dual source roots:

```text
ruff check (22 changed Python files): exit=0, All checks passed!
ruff format --check (same 22 files): exit=0, 22 files already formatted
mypy (8 changed production files): exit=0, no issues found
python -m scripts.check_contracts: exit=0
trust-tier comparison: exit=0; head 1994, working tree 1994, added 0, removed 0
git diff --check: exit=0
```

Logs: `batch2-ruff-all`, `batch2-format-all`, `batch2-mypy`,
`batch2-contracts`, `batch2-tier-ratchet`, each with recorded `.exit`.
The keyless trust-tier comparison uses shape-only signature verification;
existing stale allowlist warnings remain and no operator signature clearance
is claimed. No signatures were edited.

The local HTTP wire-fidelity fixture cannot create its loopback socket:
each of its 23 setup errors reports `PermissionError: [Errno 1] Operation not
permitted` at `ThreadingHTTPServer` socket construction. Its transport matrix
therefore remains unverified here; it was not mocked, skipped or weakened.

The standalone execution-custody child rebuilds its own environment and
executes the test module as `__main__`, so it does not load the parent pytest
heartbeat. Its exact case fails on both current and HEAD with no checkpoint.
A lane-only exact-child probe explicitly installing the heartbeat proves all
six original checkpoint facts, creates no failure checkpoint, and leaves the
child deliberately live until the original reaper terminates and waits for
it with zero cleanup failures (`child-lease-investigation.md`,
`child-lease-explicit-heartbeat.log`, exit 0). The pytest case still remains
red in the completed broad run; the controlled probe does not erase it.

`pytest tests/ -m testcontainer -n 0 --maxfail=1` was attempted using the
required serial form. `batch2-testcontainer.log` / `.exit`: exit 1 at Docker
client setup with socket `PermissionError`, before the first test body.
PostgreSQL contention and retention remain unverified.

The exact remaining failed/setup-error IDs outside the original six are in
`batch2-remaining-red-ids.txt` (25 IDs: 23 wire setup errors, one standalone
child failure, one PostgreSQL setup error). The architecture xfail above is
reported separately. Nothing here claims a clean uninstrumented broad run,
PostgreSQL qualification, live-provider parity, merge, deployment or operator
signature clearance. All deliverable changes remain unstaged.

Exact remaining failure/setup-error IDs outside the accepted six:

```text
tests/integration/web/test_blobs_ready_hash_postgres.py::test_update_ready_hash_to_malformed_rejected_on_postgres[too-short]
tests/unit/web/composer/test_wire_fidelity_matrix.py::test_azure_chat_flattens_a_root_one_of
tests/unit/web/composer/test_wire_fidelity_matrix.py::test_azure_chat_forwards_the_stamps
tests/unit/web/composer/test_wire_fidelity_matrix.py::test_azure_chat_keeps_a_plain_object_root
tests/unit/web/composer/test_wire_fidelity_matrix.py::test_azure_chat_preferred_control_sends_no_strict_key
tests/unit/web/composer/test_wire_fidelity_matrix.py::test_azure_responses_bridge_forwards_the_stamps
tests/unit/web/composer/test_wire_fidelity_matrix.py::test_azure_responses_bridge_preferred_control_writes_strict_null
tests/unit/web/composer/test_wire_fidelity_matrix.py::test_bedrock_converse_enum_null[control_without_null]
tests/unit/web/composer/test_wire_fidelity_matrix.py::test_bedrock_converse_enum_null[known_negative_null_dropped]
tests/unit/web/composer/test_wire_fidelity_matrix.py::test_gpt_5_4_plus_name_on_a_custom_base_is_bridged_to_responses[gpt-5.5]
tests/unit/web/composer/test_wire_fidelity_matrix.py::test_gpt_5_4_plus_name_on_a_custom_base_is_bridged_to_responses[openai/gpt-5.5]
tests/unit/web/composer/test_wire_fidelity_matrix.py::test_native_anthropic_drops_a_forced_strict_stamp
tests/unit/web/composer/test_wire_fidelity_matrix.py::test_native_anthropic_resolved_list_control
tests/unit/web/composer/test_wire_fidelity_matrix.py::test_openai_compatible_chat_drops_an_ecma_only_pattern[gpt-5.1]
tests/unit/web/composer/test_wire_fidelity_matrix.py::test_openai_compatible_chat_drops_an_ecma_only_pattern[openai/gpt-5.1]
tests/unit/web/composer/test_wire_fidelity_matrix.py::test_openai_compatible_chat_keeps_a_plain_pattern[gpt-5.1]
tests/unit/web/composer/test_wire_fidelity_matrix.py::test_openai_compatible_chat_keeps_a_plain_pattern[openai/gpt-5.1]
tests/unit/web/composer/test_wire_fidelity_matrix.py::test_openai_compatible_custom_base_forwards_the_stamps[gpt-5.1]
tests/unit/web/composer/test_wire_fidelity_matrix.py::test_openai_compatible_custom_base_forwards_the_stamps[openai/gpt-5.1]
tests/unit/web/composer/test_wire_fidelity_matrix.py::test_openai_compatible_custom_base_preferred_control_sends_no_strict_key[gpt-5.1]
tests/unit/web/composer/test_wire_fidelity_matrix.py::test_openai_compatible_custom_base_preferred_control_sends_no_strict_key[openai/gpt-5.1]
tests/unit/web/composer/test_wire_fidelity_matrix.py::test_openrouter_forwards_the_stamped_list_verbatim
tests/unit/web/composer/test_wire_fidelity_matrix.py::test_openrouter_planner_list_forwards_its_stamps
tests/unit/web/composer/test_wire_fidelity_matrix.py::test_openrouter_preferred_control_sends_no_strict_key
tests/unit/web/execution/test_session_operation_lease.py::test_actual_setup_after_arm_retains_canonical_loss_group_and_pending_owner
```

## Batch 2 review fixes

Independent review: `tool-fixes-0812-batch2-review.md`. Logs under
`.claude/lanes/batch2-fixes/`.

- **M1 (U9) refreshed, 0d6b17115.** The `protocol.py:module-bindings` pin in
  `reviewed-writer-recipes.json` was reviewed as text-only (four string
  constants differ, targets and annotations identical) and recomputed; control:
  pin matches, a scratch executable binding makes it mismatch, reverted. The
  whole-tree test cannot be a clean control on this branch because
  `async_workers.py` and `required_executor.py` full-source pins (landing
  commit dc67bdd2f) fail first and return early, masking the writer loop.
- **S1 (U2) 89231e76b.** Malformed model-supplied `blob_ref` at proposal
  admission (Tier 3) is now an audited, repairable refusal
  (`proposal_blob_reference_malformed`); a persisted pending row (Tier 1) still
  raises `AuditIntegrityError`, pinned by a negative-control test. Server-owned
  `set_pipeline` authority still raises `ValueError`.
- **S3 (U7).** In explicit-approval mode `diff_pipeline` now adds a
  `post_call_hints` entry (an existing envelope field) when the session has
  pending proposals, stating held edits are not applied and do not appear.
  Skills were not edited.
- **S2 (U6).** Commit-message over-claim corrected in the U6 section above.
