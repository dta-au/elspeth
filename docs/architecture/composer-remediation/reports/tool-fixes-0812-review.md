# Independent review: composer tool fixes U1-U6 (uncommitted, HEAD 2f38b807b)

Reviewer read `git diff`, callers and tests; ran tests in the worktree (logs: `.claude/lanes/tool-fixes-review/`).
Patch integrity: the six `uN-commit.patch` files together cover exactly the 18 modified files plus the new Chroma test
module (file lists per patch are disjoint, no file appears twice; u6 patch also carries the report .md). Verified by listing
`diff --git` headers per patch against `git diff --name-only`; I did not byte-compare hunks.

Broad run: `tests/unit/web/composer`, `tests/unit/web/execution`, the touched sessions/blobs tests, -n 4:
`11 failed, 16147 passed, 1 skipped` (broad.log). Attribution below.

## MUST-FIX

### M1 (U6) `blob_content_size_limit` is not a registered error code
`src/elspeth/web/composer/tools/blobs.py:2183`. `error_codes.py` is a closed registry; redaction lets only registered codes
through and summarises the rest away. `tests/unit/web/composer/test_error_code_redaction.py::TestRegistryCensus::test_every_emitted_literal_code_is_registered`
FAILS: `('composer/tools/blobs.py','_execute_get_blob_content','blob_content_size_limit')`. Scenario: a persisted rejection loses
its code, so the "truthful" distinction the unit exists for is erased on exactly the persisted path. Verified by running
(redaction.log, exit 1; also in broad.log). Author's neighbour runs never included this file. Fix: add the code to
`_EMITTED_VALIDATION_ERROR_CODES`, and consider a guidance entry.

### M2 (U4) breaks an existing test that pins the old severity
`generation.py` partial-loss branch changed `warning` -> `info`. `tests/unit/web/composer/test_configured_source_shape_proof.py:100-107`
(`test_json_sparse_extras_warn_without_claiming_universal_loss`, 4 params) asserts `severity == "warning"` and now FAILS
(`assert any(...)` False). Verified by running (broad.log). Passes on author's logs only because that file was not run.
Also the report calls `info` the "existing inspection-warning convention", but this test (and its name) pins the opposite: a
reviewer-visible warning is downgraded to informational. Either (a) keep severity `warning` and make the response contract
admit it (contract fix, the real defect), or (b) deliberately downgrade and update that test plus state it as a behaviour change.
Needs a decision; as written it ships a red test.

## SHOULD-FIX

### S1 (U4) locator detail dropped
`generation.py` ~4056-4068: `selected_record_count`, `sample_extra_record_count`, `all_rows_inspected` removed from the locators.
Operators/LLM lose the counts that said how much data is affected. Read only. If the contract forbids the keys, say so in the
commit message; otherwise carry the counts in the message text.

### S2 (U2) `proposal_blob_refs.py` ~120-129: type error is Tier-1 abort for the whole session
New branch raises `ValueError` if `options`/`patch` is present but not a dict (e.g. `null`). `pending_proposal_reference_id`
(line ~255) converts that to `AuditIntegrityError` for every blob-delete/replace in the session. Tool schemas make `options`
an object so a persisted null should be unreachable (read only: `transforms.py:283`, `sources.py:252`, `outputs.py:90`),
and `_collect_component_options_references` already treats a null component `options` as empty. Cheap to align: treat `None` as
no references. Low likelihood, severe blast radius.

### S3 (U2/U5/U1) PostgreSQL not exercised
Author could not run testcontainer suite (Docker denied). U2 changes a retention query path; run
`pytest tests/ -m testcontainer -n 0` in an operator context before merge.

## NOTES

- U1 verdict basis: `is_absent_sink_path` is scoped exactly to chroma_sink/client/persist_directory/None; all three
  `_validate_sink_path` callers (outputs.py:182,312; sessions.py:1839) now pass plugin; the other consumers
  (`service.py:2379`, `path_canonicalization.py:24`) already tolerate None. `mode` is a required field of ChromaSinkConfig, so
  mode-less None is correctly still refused. Verified by reading + author's red/negative exit codes (both 1).
- U3: one-line union of the declared `PLANNER_RECOVERY_TOOL_NAMES` (capability_skill.py:40) into the receipt `known_tools`;
  dispatch (pipeline_planner.py:1897, 4379) untouched; no provider-path or authority change; no composer-invariant issue. Line is
  ~150 chars but `ruff check`/`ruff format --check` on all changed files exit 0 (I ran them).
- U5: mirrors `without_node`; only caller is `_execute_remove_output` (outputs.py:245). Scalar `on_success`/route references to the
  removed sink are intentionally left for validation. Tests (state + tool) exist; author red/negative exit 1.
- U6 behaviour change: oversize blobs previously returned `credential_material_rejected`; now a new code. Skill text
  (pipeline_composer.md ~388-412) does not mention it; no frontend reference found in `src`. The limit is read from
  `DEFAULT_CREDENTIAL_TRAVERSAL_POLICY.max_string_bytes`, so it tracks the real scanner bound.
- No `# noqa`, `type: ignore`, broad `except`, or defensive `getattr` added in the diff (grep of diff). No server-authored
  pipeline structure, no tutorial path.
- Pre-existing failures: six tests fail identically on an untouched `git archive HEAD` extract (deleted afterwards):
  `test_failure_validation_guidance.py` x3, `test_tool_dispatch_boundary.py` x2 (source-path message), and
  `test_checkpoint_owner_compatibility.py::...[True]`. Not attributable to this diff (base.log run, same 6).
- Tests-fail-without-fix: not re-run by me; I confirmed each author red/negative log exit file is `exit=1` for U1-U6 and read
  the test bodies, which assert the specific fixed behaviour.

## Verdicts

- U1: land as-is
- U2: land with fix (S2 optional but recommended; S3 gate)
- U3: land as-is
- U4: do not land until M2 resolved (decision + test update)
- U5: land as-is
- U6: land with fix (M1)
