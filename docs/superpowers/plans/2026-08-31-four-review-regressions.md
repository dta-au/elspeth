# Four Review Regressions Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` to execute this plan task by task, and `superpowers:test-driven-development` for every behavior change.

**Goal:** Repair four confirmed review regressions: make the validation-guidance tests reach finalization with valid tool arguments, preserve required-nullable values in runtime schemas, reject concrete reference-join declarations refuted by known runtime values, and distinguish same-turn persisted prose from identical prose produced on a later Composer turn.

**Architecture:** Keep the guidance change test-only. Repair nullability at the shared schema materialization seam while keeping presence (`required`) independent from value nullability. Retain private reference-join derivation evidence so public `any` does not erase whether values are genuinely unknown or known heterogeneous/unrepresentable. Add one owned ComposerResult same-turn discriminator, minted only where the current dispatch and persisted row are jointly known, and require the turn-end writer to use it before stripping prose.

**Tech Stack:** Python 3.12, dataclasses, Pydantic v2, pytest, Ruff, ELSPETH plugin-contract and trust-tier gates.

---

## Execution rules

- Work only in the dedicated `four-review-regressions` worktree on branch `fix/four-review-regressions`.
- Run commands from that worktree. Prefix Python/test commands with `PYTHONPATH="$PWD/src:$PWD/elspeth-lints/src"` and use the worktree's `.venv/bin/python` symlink.
- Run focused tests serially with `-n 0`; the coordinator alone runs the final full suite.
- Apply test-first RED/GREEN discipline. Do not change production code before observing each new regression test fail for the intended reason.
- Use `apply_patch` for source edits. Preserve unrelated work. Commit only the named paths for each task.

### Task 1: Exercise validation guidance with schema-valid arguments

**Files:**

- Modify: `tests/unit/web/composer/test_failure_validation_guidance.py`

**Step 1: Confirm the existing RED state**

Run:

```bash
env PYTHONPATH="$PWD/src:$PWD/elspeth-lints/src" \
  .venv/bin/python -m pytest -n 0 -q \
  tests/unit/web/composer/test_failure_validation_guidance.py
```

Expected: exactly the three `_sourceless_set_pipeline` callers fail before finalization with `ToolArgumentError`; the other eight tests pass.

**Step 2: Replace only the invalid fixture**

- Rename `_sourceless_set_pipeline` to `_unknown_source_plugin_set_pipeline`.
- Submit a singular source object with `plugin: definitely_not_a_plugin` and `on_success: rows`, plus empty nodes, edges, and outputs.
- Update the three callers to assert the semantic rejection code `plugin_not_installed`, its catalogued guidance, absence of an explain-tool advertisement, and redaction custody.
- Do not import the larger `_valid_pipeline_args` fixture and do not change production code.

**Step 3: Verify GREEN**

Run the identical focused command. Expected: all 11 tests pass.

**Step 4: Review and commit**

Inspect the diff for a test-only change, then commit only this test file and this plan:

```bash
git add docs/superpowers/plans/2026-08-31-four-review-regressions.md \
  tests/unit/web/composer/test_failure_validation_guidance.py
git commit -m "test: use valid rejection for validation guidance"
```

### Task 2: Preserve nullable runtime values and reference-join type evidence

**Files:**

- Modify: `src/elspeth/plugins/infrastructure/schema_factory.py`
- Modify: `src/elspeth/plugins/transforms/reference_join.py`
- Modify: `tests/unit/plugins/test_schema_factory.py`
- Modify: `tests/unit/plugins/transforms/test_reference_join.py`
- Modify: `tests/unit/plugins/test_validation_path_agreement.py`

**Step 1: Add the shared schema-factory regression**

Add `test_required_nullable_field_requires_presence_and_accepts_none` using a strict explicit schema for `FieldDefinition(name="value", field_type="str", required=True, nullable=True)`.

Assert:

- `model_fields["value"].is_required()` is true;
- a present `None` validates strictly;
- an omitted `value` raises `ValidationError`.

Run only that test. Expected RED: present `None` is rejected as a string.

**Step 2: Make presence and nullability independent**

In `_get_python_type`, return `base_type | None` whenever `field_def.nullable or not field_def.required`; otherwise return `base_type`. Leave `_create_explicit_schema` defaults unchanged so required fields still use `...`.

Run the schema-factory test and then the entire `test_schema_factory.py`. Expected GREEN.

**Step 3: Add reference-join runtime-schema regressions**

In `test_reference_join.py`:

- Add a concrete-plus-null table test that processes the null row and validates the emitted row through `output_schema.model_validate(..., strict=True)`; separately assert omission remains invalid.
- Change the on-miss-null expectation so known CSV hit values derive `str`, remain `required=True`, gain `nullable=True`, and both a hit string and miss `None` validate strictly.
- Strengthen the un-authored heterogeneous case so runtime `any` accepts both emitted concrete types.

Run only these tests. Expected RED on required-nullable validation and the current on-miss-null `any` derivation.

**Step 4: Add authored-declaration evidence regressions**

Replace the currently permissive heterogeneous declaration test with a load-time refusal for authored `code: str` against known `{1, "X"}` values. Add:

- refusal of a concrete scalar declaration against emitted dict/list values;
- survival of a concrete declaration against a closed but vacuous emitted set;
- retention of authored `any` as the explicit abstention control.

Add the heterogeneous refusal to `_TRANSFORM_REJECTION_CASES` in `test_validation_path_agreement.py` so pre-validation and construction agree.

Run only the new cases. Expected RED: heterogeneous and structured declarations are accepted today.

**Step 5: Retain derivation evidence privately**

Add a frozen, slotted private `_JoinedFieldDerivation` with:

```python
definition: FieldDefinition
known_non_null_runtime_types: frozenset[type] | None
```

Use `None` only for genuinely unknown evidence, an empty frozenset for closed/vacuous evidence, one supported exact type for homogeneous values, multiple exact types for heterogeneous values, and unmapped exact types for known unrepresentable values.

Derive the public field definition as before except:

- `on_miss: null` retains known hit-type evidence and forces nullable instead of discarding all evidence;
- public `field_type` is concrete only for one homogeneous supported exact type, otherwise `any`.

Validation must accept authored `any`, genuinely unknown evidence, vacuous evidence, and a matching homogeneous concrete declaration. It must reject a concrete declaration against a mismatching homogeneous type, heterogeneous types, or known unrepresentable values. Keep required and nullable validation independent.

**Step 6: Format, rotate the plugin source hash, and verify focused behavior**

Run Ruff format/check on the five touched Python files. Recompute `ReferenceJoin.source_file_hash` only after formatting and update the class declaration through the repository’s plugin-hash workflow; do not hand-edit judge signatures or regenerate unrelated corpora.

Run serially:

```bash
env PYTHONPATH="$PWD/src:$PWD/elspeth-lints/src" \
  .venv/bin/python -m pytest -n 0 -q \
  tests/unit/plugins/test_schema_factory.py \
  tests/unit/plugins/transforms/test_reference_join.py \
  tests/unit/plugins/test_validation_path_agreement.py
```

```bash
env PYTHONPATH="$PWD/src:$PWD/elspeth-lints/src" \
  .venv/bin/python -m pytest -n 0 -q \
  tests/integration/core/dag tests/unit/architecture
```

```bash
env PYTHONPATH="$PWD/src:$PWD/elspeth-lints/src" \
  .venv/bin/python -m elspeth_lints.core.cli check \
  --rules plugin_contract.plugin_hashes --root src/elspeth
```

Expected: all focused tests pass and the plugin hash gate reports no stale hash.

**Step 7: Review and commit**

Commit only the five named files (plus any mechanically required plugin-hash manifest path proven by the gate):

```bash
git add src/elspeth/plugins/infrastructure/schema_factory.py \
  src/elspeth/plugins/transforms/reference_join.py \
  tests/unit/plugins/test_schema_factory.py \
  tests/unit/plugins/transforms/test_reference_join.py \
  tests/unit/plugins/test_validation_path_agreement.py
git commit -m "fix: preserve reference join output contracts"
```

### Task 3: Carry explicit Composer turn identity

**Files:**

- Modify: `src/elspeth/web/composer/protocol.py`
- Modify: `src/elspeth/web/composer/service.py`
- Modify: `src/elspeth/web/sessions/routes/_helpers.py`
- Modify: `tests/unit/web/sessions/routes/test_turn_end_assistant_row.py`
- Modify: `tests/unit/web/composer/test_compose_loop_interpretation_review_dispatch.py`
- Modify: `tests/unit/web/sessions/test_routes.py`

**Step 1: Add the helper-level regression matrix**

Extend the local result factory with `persisted_assistant_matches_terminal_model_turn: bool = False`. Set it true in existing same-turn staged-handoff tests. Add a later-turn case where persisted content and raw content are byte-identical repeated prose and `message` is that prose plus a suffix, but the flag is false; assert the full synthesized message and original raw content are preserved.

Add constructor tests for a true flag without a persisted pair, without `persisted_tool_call_turn`, with raw/persisted mismatch, and with a prefix mismatch. Keep the exact-duplicate helper failure with the flag true.

Run `test_turn_end_assistant_row.py`. Expected RED because `ComposerResult` has no discriminator.

**Step 2: Add a real compose-loop regression**

In `test_compose_loop_interpretation_review_dispatch.py`:

- Extend the existing staged-handoff loop test to assert the new flag is true.
- Add a `max_composition_turns=1` B-4D-3 test: a schema-valid `set_pipeline` tool-call response carries prose P; the bonus text-only response repeats P; finalization appends a suffix. Assert persisted and raw content both equal P, the message is augmented, and the new flag remains false.

Expose the boolean on the existing internal `ComposeLoopTestResult` test carrier for the assertion. Run only these two loop tests. Expected RED before implementation.

**Step 3: Mint and enforce explicit same-turn identity**

Add `persisted_assistant_matches_terminal_model_turn: bool = False` to `ComposerResult`.

When true, `__post_init__` must require:

- `persisted_tool_call_turn is True`;
- persisted id/content are both present;
- raw assistant content equals persisted content;
- message starts with persisted content.

Do not enforce the inverse: equal bytes can legitimately arise on distinct turns.

Set the field true only in the staged-handoff return in `_classify_and_budget_turn`, where the current `_DispatchOutcome` and its immediately corresponding `_PersistOutcome` are jointly owned. Leave all no-tool and B-4D-3 return paths false.

In `composer_turn_end_assistant_row`, return the full message unless the flag is true. With a true flag, treat content/prefix inconsistency as an audit invariant violation, then retain the non-empty-suffix failure and suffix-only backend-chrome result.

Update the same-turn route stub in `test_routes.py` to set the flag true.

**Step 4: Verify the slice and cross-cutting inventories**

Run serially:

```bash
env PYTHONPATH="$PWD/src:$PWD/elspeth-lints/src" \
  .venv/bin/python -m pytest -n 0 -q \
  tests/unit/web/sessions/routes/test_turn_end_assistant_row.py
```

```bash
env PYTHONPATH="$PWD/src:$PWD/elspeth-lints/src" \
  .venv/bin/python -m pytest -n 0 -q \
  tests/unit/web/composer/test_compose_loop_interpretation_review_dispatch.py \
  -k "staged_handoff_threads_the_persisted_row_content_to_the_route or repeated_synthesized"
```

```bash
env PYTHONPATH="$PWD/src:$PWD/elspeth-lints/src" \
  .venv/bin/python -m pytest -n 0 -q \
  tests/unit/web/sessions/test_routes.py \
  -k "send_message_does_not_re_emit_the_already_persisted_turn_prose or recompose_auto_commit_revoked_persists_the_post_rebind_message"
```

```bash
env PYTHONPATH="$PWD/src:$PWD/elspeth-lints/src" \
  .venv/bin/python -m pytest -n 0 -q \
  tests/unit/web/test_sessions_composer_attribute_contracts.py \
  tests/unit/elspeth_lints/test_masquerade_gate.py
```

Expected: all pass without inventory updates.

**Step 5: Review and commit**

Commit only the six named paths:

```bash
git add src/elspeth/web/composer/protocol.py \
  src/elspeth/web/composer/service.py \
  src/elspeth/web/sessions/routes/_helpers.py \
  tests/unit/web/sessions/routes/test_turn_end_assistant_row.py \
  tests/unit/web/composer/test_compose_loop_interpretation_review_dispatch.py \
  tests/unit/web/sessions/test_routes.py
git commit -m "fix: distinguish persisted Composer turn identity"
```

### Task 4: Integrated review, gates, merge, and custody closeout

**Step 1: Independent reviews**

After each implementation task, obtain a fresh specification-compliance review and then a fresh code-quality review. Correct every material finding and have the relevant reviewer recheck. After all slices, obtain one final whole-diff review against all four issue acceptance criteria.

**Step 2: Formatting and focused integration**

Run Ruff format/check over every touched Python file, confirm `git diff --check`, and rerun all focused commands above serially with explicit worktree imports.

**Step 3: Trust-tier corpus comparison**

Run the all-rules lint gate with `ELSPETH_JUDGE_METADATA_SIGNATURE_VERIFY_MODE=shape-only-when-key-missing` before and after the implementation, compare exact finding sets, and confirm the change introduced no new trust-tier findings or signature drift. The known global exit 1 is expected; do not sign or hand-edit judge metadata.

**Step 4: Full CI-equivalent suite**

Once other full-suite processes have cleared, run:

```bash
env PYTHONPATH="$PWD/src:$PWD/elspeth-lints/src" \
  .venv/bin/python -m pytest -n 0 tests/
```

Expected: exit 0 with the terminal pytest summary captured.

**Step 5: Merge and verify the integrated feature branch**

- Recheck both worktrees are clean and the feature branch has not gained unintegrated commits.
- If the feature branch advanced, merge it into `fix/four-review-regressions`, resolve only owned conflicts, and rerun affected checks.
- Merge `fix/four-review-regressions` into `feature/unified-lineage` with `--no-ff`.
- Verify the merged ancestry and rerun the focused integration checks plus the full suite on the merged feature branch.
- Do not push.

**Step 6: Close tracker custody and clean the worktree**

Add evidence comments and close:

- `elspeth-c884f74f96`
- `elspeth-7979c73f1b`
- `elspeth-b2aa395b7d`
- `elspeth-970692e913`

Notify the three predecessor tickets of the landed follow-up commit without changing their ownership or status. After verifying the branch is merged and the worktree is clean, remove the task worktree and delete the merged task branch.
