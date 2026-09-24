# Composer R1/R2 rulings and branch-order fixes: implementation plan

- **Date:** 2026-09-24
- **Rulings:** John, 2026-09-24 ("lets lock it in"). They are recorded in §1.2 and carried into the master plan by T1.
- **Master plan:** `docs/plans/2026-09-23-composer-strict-tool-contracts.md`. T1 edits it. T2 to T6 fix two defects
  that the R1/R2 panel found while it measured the strict-contract evidence. Neither defect is a strict-contract
  change.
- **Branch:** `fix/composer-map-order-and-r1r2`, based on local `release/0.8.1` at **`c4c52c110`**. That commit merges
  S0 and S1 and is also on `origin/release/0.8.1` (`git branch -r --contains c4c52c110`).
- **Paths:**
  - `$W` is the worktree root (`git rev-parse --show-toplevel`).
  - `$L` is `$W/.claude/lanes/order`, the gitignored lane directory. It holds the three reader reports
    (`understand-transcript.md`, `understand-hash.md`, `understand-docs.md`), the panel reports under `$L/r1r2/`, and
    every probe and log cited below.
  - `$DBS` is the directory of read-only session-DB copies named in the lane brief. Open them only as
    `sqlite file:...?mode=ro`.
  - Do not write `$L`, `$DBS` or any user-home path into a tracked file.
- **Citations:** every `path:line` was measured in `$W` at `c4c52c110`. Lines drift as tasks land, so re-anchor each
  one with `grep -n` before editing. Evidence is marked as follows:
  - **M:** measured by running code. The log is in `$L`.
  - **R:** read from the source.
  - **I:** inferred, not run.

---

## 1. Header

### 1.1 Goal

Three work items, in this order:

1. **Docs (T1).** Record R1 and R2 in the master plan. Mark S2 and S3 withdrawn, with the reopen trigger. Cite
   `composition_rejection_events` as the unredacted evidence store, and correct §2.3's claim that "shape and value
   cannot be told apart". Fix Appendix A's fork double-count. Add the option-tool measurement split.
2. **Defect A (T2).** When the compose loop replays planner arguments into its transcript, it writes every map in
   alphabetical order. Fix it so the transcript keeps the model's key order.
3. **Defect B (T3 to T6).** The composer authority hashes are blind to the order of coalesce mapping-form branches and
   of the multi-source `sources` map, and so is the advisor sign-off fingerprint. Both maps are order-semantic at
   runtime. Bind their order the way the row_union precedent does. This changes persisted hash preimages that are
   re-verified with a hard failure, so it ends with a session schema epoch bump, as the last commit.

### 1.2 Rulings recorded (John, 2026-09-24)

- **R1: no.**
  - The 10 option-bearing tools and the pipeline-planner terminal stay `strict:false` permanently. S2 and S3 are
    withdrawn. S4 and S5 stay optional.
  - **Reopen trigger, per tool:** R1 reopens for a specific tool only when both of these hold:
    - its shape errors, weighted by planner-turn cost, are more than 50% of its failures, over at least about 50
      failures;
    - enough of that tool's traffic reaches endpoints that enforce strict.
  - **Evidence:**
    - 30 unique option-tool failures: 10 shape, 18 content and 2 ambiguous, from `composition_rejection_events`.
      That is a shape share of 0.33, Wilson 95% [0.19, 0.51].
    - `strict` on an `options_json` string constrains nothing inside the string.
    - Options-as-string has already swung twice (`a5d9e5414`, `e50604428`).
- **R2: yes.** Pair-array ↔ map transcoding is representation, not authoring, under five conditions:
  1. the round trip is lossless;
  2. order is preserved both ways;
  3. duplicate keys are rejected, which is a documented change from today's silent last-wins;
  4. no default is inserted and no choice is made;
  5. `branches` is decoded by element type, and an empty `[]` is rejected.

  R2 has no immediate use, because there is no carrier. It is recorded for future per-tool use.
- **The goal as adopted.** "A strict contract for every tool call" means that every call is admitted only by the
  closed server contract S and is classified against the wire form it was sent in. Grammar-enforced strict is used
  where it measurably pays.

### 1.3 Decisions this plan takes (lead; John may overrule)

| # | Decision | Why |
|---|---|---|
| D1 | **Defect A's fix is to drop `sort_keys=True` at `tool_batch.py:498` and nothing else.** Keep the compact separators. | That keyword is the whole cause. A mutation that removes only it restores the model's order in the unit probe and in the end-to-end compose-loop probe (M, `understand-transcript.md` §1). All 7 callers go through that one `json.dumps` (§2 of the same report). |
| D2 | **Every other site that re-serialises planner-authored arguments is left alone.** §3.2 of this plan gives the table. | The only other site that sorts arguments the model sees is `pipeline_planner.py:3870`. It is inert today, because the planner only runs on an empty pipeline. Every other sorted site is an audit, cache or dedupe key that is sorted by design. Order is bound separately by the authority projection, which T3 to T5 extend. |
| D3 | **`sources` is part of Defect B**, with its own schema tag. It is projected unconditionally (every dict, including a one-entry dict). The singular `source` field is untouched. | The brief says "any other order-semantic map found". `sources` order decides ingest order (`leader_drain.py:192-203`) and the "first source" (`run_lifecycle.py:207`, `processor_factory.py:430`) (R, `understand-transcript.md` §7). The hash reader's objection was that the engine topology hash does not bind it either. That is equally true of coalesce's gap in the composer, so the objection is recorded in §1.4 as a separate engine defect rather than as a reason to leave the composer blind. The alternative, projecting only when there are two or more entries, is rejected: the preimage shape would then depend on the count, and restore would have to accept two forms. |
| D4 | **The coalesce projection does not depend on `merge` or `policy`.** | The engine already carries `branch_order` unconditionally (`builder.py:545-557`). Binding `select` as well is harmless, and it keeps the rule to one line (R, `understand-hash.md` §2). |
| D5 | **Every new projection has its own schema tag. The row_union tag stays byte-identical.** Tags: `composer.coalesce-ordered-branches.v1` and `composer.ordered-sources.v1`. | Row_union preimages must not move (T3 pins them with a golden). A projection of one kind can then never be restored as another. |
| D6 | **`_DRAFT_HASH_SCHEMA` stays `...envelope.v3`, and `_FINGERPRINT_SCHEMA` stays `elspeth.completion_gate_graph.v2`.** | Each changed part of a preimage now carries its own schema tag, so the change separates its own domain. The epoch cut in T6 deletes every stored row, so no v3 or v2 preimage from before the change survives to collide. The cost of choosing otherwise: a v4 draft envelope moves every `draft_hash` literal, including single-`source` pipelines that T3 to T5 leave unchanged. Precedents in the other direction are `49825dd86` (draft v2→v3) and `d7541609c` (fingerprint v1→v2). If the lead prefers to mirror them, the bump goes into T4 for the draft hash and T5 for the fingerprint. |
| D7 | **`completion_gate_fingerprint` is routed through the projection (T5).** | It is the same class of gap: the advisor's sign-off is carried forward onto a graph whose merge order or ingest order changed (`completion_gates.py:166`, `advisor_block_covers_unchanged_graph` at `:383`). The epoch cut in T6 is being paid anyway, so the fix is almost free. Leaving it out would be the kind of debt John has ruled against. |
| D8 | **The epoch bump is 66 → 67. It is the last commit (T6) and touches 10 files (§5).** | Stored hashes are recomputed on read and fail hard with `AuditIntegrityError` (§5.1). John's no-tech-debt rule forbids dual acceptance or a legacy hash path. |
| D9 | **Appendix A joins on `(session_id, call_id)`**, not on `parent_assistant_id`. | The brief names the session-scoped join. It gives the same counts as the parent join on all 13 DB copies (M, `understand-docs.md` §1.2). The parent join is mentioned in the appendix as the tighter alternative. |
| D10 | **The status line in T1 cites the S1 gate record as it stands.** At `d5f8c5aac` the result was FAIL, with 10 failures and `frozen=yes`. | All 10 red ids also fail at `c4c52c110` (M, §2). That commit already contains S1, so this shows the 10 are the lane's known base-red set. It does **not** show that they predate S1. The plan text must say exactly that. |

### 1.4 Out of scope: recorded here, not fixed

**Filigree was unreachable in this session (the MCP connect timed out), so no ticket was filed for any of these.
The lead files them.**

1. **The durable execution envelope alphabetises order-semantic maps before every web run (a live defect, and the
   most serious one found).**
   - `capture_execution_envelope` (`web/execution/envelope.py:343`) writes the envelope with `sort_keys=True`, and
     `get_run_execution_input` (`sessions/service.py:10592`) sorts it again when it reads it back.
   - Every durable web run executes from the restored copy (`execution/service.py:2953-2963`, `:3053`, `:3440`).
   - As a result, coalesce `last_wins`/`first_wins` winners, row_union release order and multi-source ingest order
     follow **alphabetical** order at runtime, not the order that was authored and approved.
   - Measured on `examples/fork_coalesce/settings_union_last_wins.yaml`: the authored order `[path_b, path_a]` is
     restored as `[path_a, path_b]`, and the collision winner flips from `path_a` to `path_b` (M,
     `$L/probes/probe_envelope_order.log`).
   - Nothing detects it, because `validate_run_execution_input` (`envelope.py:559`) and the digests compare without
     regard to order.
   - Fixing it touches the persisted `run_execution_inputs.envelope`, so it raises the same epoch question as
     Defect B. It needs John's decision and its own ticket. **Until it lands, the order that T3 to T5 bind at approval
     is not the order that durable web runs execute.** The handover must say this plainly.
2. **The engine topology hash does not bind `sources` order.** `builder.py:293-321` puts `source_name` in the source
   node config but no ordinal, and `compute_full_topology_hash` (`core/canonical.py:231`) sorts nodes by id. This is an
   engine and Landscape decision, separate from T4.
3. **The `upsert_node` dispatch audit is order-blind.** `begin_dispatch` computes `authority_arguments_*` for
   `set_pipeline` only (`audit.py:639-640`). The `arguments_hash` of an `upsert_node` that carries coalesce or row_union
   `branches` is RFC 8785 and blind to their order. The state it produces is bound by `composition_content_hash`
   after T3. The per-call audit hash is not, and it is never re-derived from stored rows. Record it; do not fix it here.
4. **`_pending_interpretation_validation_candidate_digest`** (`sessions/pending_interpretation.py:1560`) is an
   order-blind raw hash over nodes. Resolving an interpretation does not reorder branches, so the gap is practically
   unreachable. Low priority.
5. **`pipeline_planner.py:3870` sorts the planner's `current_state`.** It is inert today (D2). If the planner ever
   receives a non-empty state, it becomes a second Defect-A site.
6. **The table comment on `composition_rejection_events` is false for ARG_ERROR rows.** The comment is at
   `sessions/models.py:1679` and says the table holds the "EXACT serialized tool response the planner saw". For those
   rows the table holds `{error_class, error_message}`, and its `error_code` column holds the exception class
   (`understand-docs.md` §3, F1).
7. **The S-gate `(loc, code)` pairs are persisted nowhere**, so the option-tool split that T1 defines cannot yet be
   measured. Persisting them is redaction-safe, but it touches S1 ruling 6 ("RejectionRecord untouched"). The lead
   decides.
8. **Six epoch doc, website and receipt pins already fail on base (65 against 66).** The brief forbids editing them.
   After T6 they will expect 67; their count does not change.
9. **Planner-turn cost has no adopted definition.** `understand-docs.md` §3 proposes one, for John to accept.

### 1.5 The ordering rule (hard)

The tasks run T1 → T2 → T3 → T4 → T5 → T6. **T6 (the epoch) is the last commit**, and T3 to T6 land and deploy
together:
- after T3, an epoch-66 store cannot read its own coalesce-map proposals or dispatch envelopes;
- after T4, it cannot read any row whose pipeline carries a `sources` map.

No commit between T3 and T6 may be merged or deployed on its own.

---

## 2. Measured starting point

Environment for every command:

```bash
W=$(git rev-parse --show-toplevel); L=$W/.claude/lanes/order
export PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=$W/src:$W/elspeth-lints/src
PY=$W/.venv/bin/python
cd $W && $PY -c "import elspeth,elspeth_lints;print(elspeth.__file__, elspeth_lints.__file__)"   # both must be under $W
env | grep -c ELSPETH_JUDGE                                                                         # must print 0
# House pytest line (focused runs):
cd $W && $PY -m pytest -n 0 -p no:cacheprovider -o "pythonpath=$W/src $W/elspeth-lints/src" <ids> > $L/<log> 2>&1; echo "exit=$?"
```

| Fact | Value | Reproduce (log in `$L`) |
|---|---|---|
| Base | `release/0.8.1` = `c4c52c110`, which is also on `origin/release/0.8.1`. The worktree was clean on arrival | `git log -1 release/0.8.1`; `git status --short` |
| Defect A, unit | The model sends `zeta` before `alpha` in `sources`, `row_union.branches` and `coalesce.branches`, and in each case the transcript comes back `alpha`, `zeta`. Node field order is sorted too. List-form branches keep their order (control) | `$PY $L/probes/probe_transcript_unit.py` → `probes/probe_transcript_unit.log` (M) |
| Defect A, end to end | On the next provider turn, the transcript shows `['alpha','zeta']` where the model sent `['zeta','alpha']`. Controls: the upsert_node transcript is byte-equal to the raw arguments, and with `sort_keys` neutralised the order is `['zeta','alpha']` | the house pytest line on `$L/probes/test_probe_transcript_e2e.py` → `probes/probe_transcript_e2e.log` (M, 3 passed) |
| Defect A, test impact of the exact fix | The 6 files that read transcript arguments: 560 passed at base and 560 passed mutated. The 86-file compose-loop set: 1 failed, 3721 passed. The failure also fails at base, with the same `for_graph` mismatch | `probes/transcript_tests_{base,mutated}.log`, `probes/loop_tests_mutated.log`, `probes/p5_base.log` (M) |
| Defect B | A reordered coalesce map gives equal hashes for `composer_authority_hash`, `composition_content_hash`, `draft_hash`, the dispatch authority hash, the private-arguments hash and the advisor fingerprint. The row_union control differs everywhere except the advisor fingerprint (a sibling gap). List-form coalesce already differs | `$PY $L/probe_hash_order.py` → `probe_hash_order.log` (M) |
| Persistence keeps key order | `nodes`, `arguments_json` and `arguments_redacted_json` are SQLAlchemy `JSON`, not JSONB, and no serializer is overridden in `src/elspeth/web` | R, `understand-hash.md` §4 |
| Execution envelope alphabetises (out of scope, §1.4 item 1) | admitted `zeta,alpha`; restored `alpha,zeta`; the `last_wins` winner flips | `$PY $L/probes/probe_envelope_order.py` → `probes/probe_envelope_order.log` (M) |
| Epoch | `SESSION_SCHEMA_EPOCH = 66` (`sessions/models.py:362`), `_COORDINATION_HARD_CUT_EPOCH = 66` (`sessions/schema.py:39`), and exact equality is enforced at `schema.py:534`. No other worktree is at 67 | R; worktree loop over `*/src/elspeth/web/sessions/models.py` (M, 2026-09-24) |
| **Base-red set (10 ids)** | 6 epoch doc, website and receipt pins (65 against 66): `test_release_site_contract::test_get_started_has_runnable_cli_and_complete_composer_paths`, `test_release_version_surfaces::{test_operator_schema_version_examples_match_live_constants, test_scenario_b_runbook_record_matches_live_release_derivation}`, `test_readme_release_surface::test_readme_operational_cutover_states_the_live_schema_epochs`, `test_azure_container_apps_runbook_contract::{test_compatibility_record_is_byte_bound_to_the_live_derivation, test_every_epoch_literal_matches_the_live_constants}`. 2 line-pinned: `test_session_db_mutation_authority::{test_live_connection_domain_classification_is_exact, test_session_schema_authority_is_exact_contained_and_bidirectional}`. Plus `test_compose_loop_interpretation_review_dispatch::test_end_advisor_gate_reaches_prompt_template_pipeline_p5_budget_exhaustion` and `test_composer_bedrock::test_bedrock_advisor_uses_default_chain_without_tools_or_gateway_overrides` (`_MalformedLLMResponseError`). **These are exactly the 10 FAILED ids of the S1 gate at `d5f8c5aac`** | `understand-hash-epochpins.log`, `probes/p5_base.log`, `plan-bedrock-base.log` (M); the S1 gate's `pytest.log` |
| Epoch docs that pass on base and must move with the constant | `docs/runbooks/staging-session-db-recreation.md` and `CHANGELOG.md:9-10`, both pinned by `tests/unit/docs/test_staging_session_recreation_policy.py` | R, and M in `understand-hash-epochpins.log` |
| Appendix A fork fix | Corrected total = tool rows in all 13 DB copies. The old join gives 26 against 14 in db10. Per-(tool, outcome) totals equal `census.py`: 496 = 496 | `docs_appendix_a_corrected_dbs.py` → `docs-appendix-a-corrected-dbs.log` (M) |
| T12 fixture on the corrected SQL | 8 groups unchanged, both negative controls unchanged. Fork phase: corrected n=2, old n=4, and removing the session condition restores the over-count | `docs_t12_corrected_test.py` → `docs-t12-corrected.log` (M, 1 passed) |
| Options-as-string signature | `schema_shape`, with a violation whose last loc segment is `options` or `patch` and whose code is `invalid_type`. Structural control: `on_success: 5` gives `('on_success',)` | `docs-loc-probe.log` (M) |
| No test reads the master plan | 8 test files cite it, all in docstrings. The doc-walker excludes `docs/plans/` | R and M, `understand-docs.md` §2 |

---

## 3. Task list

### 3.1 Rules for every task

- **RED first.** Write the tests, run them, and record the failure and its **actual** reason before you change
  production code. If the reason is not the one the task names, record the actual one and stop to reassess.
- **Characterization tests are named as such.** A test that is green on arrival says "characterization" in its
  docstring. It still needs a mutation control.
- **Mutation control.** Every new test gets a named mutation of the thing under test that turns it red. Record the
  log in `$L`, then revert. Flipping an expected value is not a control.
- **Focused runs:** use the house pytest line from §2, and read the exit code, not a tail.
- **Commit.**
  1. Run `git status --short`.
  2. Run `scripts/branch-safety-check.sh --intent commit --base release/0.8.1`.
  3. Run `git commit -- <pathspecs>`. Never `git add` followed by a bare commit.
  4. End the message with `Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>`.
  5. Never stage `.claude/lanes/`.
- **Every-task gate set.** Every task that commits code runs all of these:
  - **G-mock:** `$PY -m pytest $W/tests/unit/test_mock_discipline_baseline.py -n 0 -p no:cacheprovider`
  - **G-walker:** `$PY -m pytest $W/tests/unit/elspeth_lints/test_python_file_walker_authority.py -n 0 -p no:cacheprovider`
    - No literal `rglob("*.py")` or `os.walk(` under `tests/`, in comments and docstrings too.
    - Use `tests/helpers/tree_gate.iter_gate_files`.
  - **G-masq:** `$PY -m elspeth_lints.rules.masquerade.seed_baseline --check` plus
    `tests/unit/elspeth_lints/test_masquerade_gate.py`.
    - The count must equal the T0 baseline.
    - No `getattr` or `hasattr` in new code or new tests.
  - **G-attr:** `tests/unit/web/test_sessions_composer_attribute_contracts.py` and
    `tests/unit/test_no_hasattr_branching.py`.
  - **G-contracts:** `$PY -m scripts.check_contracts`, then `tests/unit/scripts/test_check_contracts.py`.
    - Re-pin with `--write-census` in the same commit only if a `Mapping[str, Any]` or `dict[str, Any]` signature moved.
    - None is expected, because `project_`/`restore_composer_authority_payload` keep their signatures.
  - **G-ruff/mypy:** `$W/.venv/bin/ruff check <files>`, `$W/.venv/bin/ruff format --check <files>`, and
    `$PY -m mypy <touched src files>`.
  - **G-tier:** the trust-tier corpus, as a multiset diff against the T0 base:
    ```bash
    ELSPETH_JUDGE_METADATA_SIGNATURE_VERIFY_MODE=shape-only-when-key-missing \
      $PY -m elspeth_lints.core.cli check --rules all --root src/elspeth > $L/<task>-lints.log 2>&1; echo exit=$?
    norm() { sed -E 's/^([^:]+):[0-9]+:[0-9]+:/\1:/' "$1" | LC_ALL=C sort; }
    grep -v '^WARNING' $L/<task>-lints.log > $L/<task>-lints.findings
    norm $L/<task>-lints.findings > $L/<task>-lints.norm
    diff $L/lints-base.norm $L/<task>-lints.norm > $L/<task>-lints.diff; echo diff-exit=$?
    ```
    - Account for every `>` line and every `<` line.
    - Stage no signatures, and never touch `mcp__elspeth-judge__*`.
    - In `authority_hashing.py` and `completion_gates.py`, use membership reads (`"k" in d`, then `d["k"]`) and
      `type(x) is` checks, as the B49 tier fix did. Never `.get`.
- **Extra gates named per task:**
  - **G-wire:** `tests/unit/scripts/test_composer_wire_census.py`, `tests/unit/scripts/test_composer_wire_scorecard.py`,
    `tests/unit/web/composer/test_tool_model_wire_parity.py` and `tests/unit/web/composer/test_tool_knob_teaching_gate.py`.
  - **G-skill:** `$PY $W/scripts/cicd/generate_skill_inventory.py --check`. It must stay at exit 0, because no tool is
    added or renamed.
- **Never:**
  - `# noqa`, `# type: ignore` or any other suppression;
  - `--no-verify` or `git stash`;
  - `sed` or `awk` for a multi-line edit;
  - compatibility shims or dual acceptance;
  - merge, push or rebase;
  - the full test suite (the lead runs it);
  - editing the epoch doc pins that already fail on base (§2).

### T0 — Preflight (no commit)

1. Run the environment check from §2.
2. Take the baselines:
   - the trust-tier corpus → `$L/lints-base.findings` and `$L/lints-base.norm`;
   - the masquerade `--check` count;
   - `check_contracts` exit code;
   - `generate_skill_inventory.py --check` exit code.
3. Re-run the base-red set from §2 as one house-pytest invocation → `$L/t0-base-red.log`.
   - Record the 10 failing ids and their assertion messages.
   - Later tasks diff failure sets and messages against this log. Counts alone are not enough.
4. **Golden capture for T3 and T4.** Build three fixed payloads with a small script, `$L/t0_golden.py`:
   - a row_union pipeline with map branches;
   - a pipeline using the singular `source` field with a list-form coalesce;
   - a state dict whose `sources` map has a single entry.

   Print `composer_authority_canonical_json` of the first two, and the `composition_content_hash` of a
   `CompositionState` built from the third. Log to `$L/t0-golden.log`. The row_union and singular-`source` canonicals
   become byte pins in T3 and T4: they prove those preimages did not move.
5. **Scope check for T4, by walking the registry (not by grep).** Walk the registered loop tool schemas (the same
   authority `$L/probes/maps.py` used) and list every tool whose **top-level** properties include `sources`.
   - Expected: `set_pipeline` only.
   - Positive control: `set_pipeline` must be found.
   - Negative control: the walk must not list `set_source`.
   - If any other tool has a top-level `sources` property, **stop**. `tool_batch.py:577` hashes every tool's audit
     arguments through the projection, so T4 would silently give another tool's field topology meaning.
6. **Finalization-path fixture for T2.** Find a compose-loop test that drives `finalization.changed` at
   `tool_batch.py:1373` or `:1477`. Candidates are `tests/unit/web/composer/test_required_control_autowire.py` and
   `tests/integration/web/composer/test_freeform_required_controls.py`. Confirm the fixture reaches that branch by
   instrumenting a copy in `$L`, not by reading the test name.

### T1 — Master plan: record the rulings, correct the evidence, fix Appendix A (docs only)

**File:** `docs/plans/2026-09-23-composer-strict-tool-contracts.md`.

`$L/understand-docs.md` §5 gives the exact old and new text of each edit, E1 to E15. Apply them with the Edit tool,
with the changes below. Re-anchor every OLD string first, because the line numbers are at `c4c52c110`.

| Edit | Where | Change from the draft |
|---|---|---|
| E1 | status line (lines 5-8) | Use **variant B**. S1 is merged as `c4c52c110` and is on `origin/release/0.8.1`. Its full-suite gate at `d5f8c5aac` recorded `RESULT=FAIL`: 10 failed, 56,862 passed, `frozen=yes`. All 10 failed ids also fail at `c4c52c110`, where they form this lane's base-red set. Six are the epoch-66 doc, website and receipt drift, two are line-pinned `test_session_db_mutation_authority`, and the last two are the `p5_budget_exhaustion` advisor-gate test and `test_composer_bedrock`. Because `c4c52c110` already contains S1, this matches known reds but does not prove they predate S1. Still owed: attributing them to a pre-S1 commit, the CHANGELOG line, and dev deployment acceptance. Add: "R1 and R2 were ruled on 2026-09-24 (§1, §6.3): S2 and S3 are withdrawn; S4 and S5 stay optional. The branch-order defects the panel found are handled in `docs/plans/2026-09-24-composer-r1-r2-rulings-and-branch-order-fixes.md`." |
| E2 | new opening paragraph of §1 | as drafted |
| E3 | §1.1 "delivered as" | as drafted |
| E4 | §1.2 bullets | as drafted |
| E5 | §2.3 last row, replaced by three rows | as drafted. This is the correction to "shape and value cannot be told apart", and the citation of `composition_rejection_events` as the unredacted store |
| E6, E7 | S2 and S3 headings, openings and closing line | as drafted (withdrawn, with the reopen trigger) |
| E8 | §5.3 table rows 1 and 3 | as drafted |
| E9 | §5.3 sample-size paragraph, plus a new **§5.4 Option-tool split** | As drafted. It defines the options-as-string signature, other-field shape, inside-options content and other rules, and says plainly that the first two classes are not measurable until the `(loc, code)` pairs are persisted (§1.4 item 7). |
| E10 | §6.1 risk 1 | as drafted |
| E11 | §5.2 `wire_decode` row | as drafted |
| E12 | §6.3 items 1-2 | as drafted (R1 ruled no with the trigger; R2 ruled yes with the five conditions) |
| E13 | "S1 as implemented" bullet | **Required, not optional.** Map each pre-merge hash to its hash on `release/0.8.1`, and drop "the merge on John's word" from the owed list |
| E14 | Appendix A SQL and a new "Session-scoped join (corrected 2026-09-24)" bullet | As drafted. Also add one sentence saying that `c.msg_id = t.parent_assistant_id` is the tighter alternative and gives the same counts today. |
| E15 | §6.2 escape-hatch bullet | as drafted |
| E16 | line 656 ("no producer until S2") and lines 380-382 ("confirm each by reading before S2") | Replace with "S2 is withdrawn (§6.3)". Leave the rest of the wording as it is |

**Verification (no pytest RED, because this is docs only):**

Both lane instruments read their "verbatim" SQL from the **tracked** plan path:
- `docs_t12_corrected_test.py:69` sets `PLAN = W / "docs/plans/..."`;
- `docs_appendix_a_corrected_dbs.py:6` has an `open("docs/plans/...")`.

After the edit, that path holds the corrected SQL, so used as-is they would compare the fix with itself. The T12
script would also fail its own `sql != verbatim_sql` assert (`:228`).

0. **Before editing**, snapshot the base plan:
   `git show c4c52c110:docs/plans/2026-09-23-composer-strict-tool-contracts.md > $L/master-plan-base.md`.
   Then make lane copies of both instruments, `$L/t1_t12_check.py` and `$L/t1_dbs_check.py`, whose "verbatim" source
   is `$L/master-plan-base.md` and whose "corrected" source is the edited tracked plan. Record the one-line diff of
   each copy in the T1 log.
1. Run `$L/t1_t12_check.py` with the house pytest line.
   - It must pass: 8 groups, both negative controls, and the fork phase at n=2 (corrected) against n=4 (base).
   - Known negative: point its "corrected" source at `$L/master-plan-base.md` as well. It must fail at the
     `sql != verbatim_sql` assert, which proves the copy really reads two different files.
2. Run `$L/t1_dbs_check.py` over `$DBS`. It must show `corrected == tool_rows` for all 13 DBs, `verbatim=26` against
   `corrected=14` in db10, and parity `496 = 496`.
3. Run `$PY -m pytest $W/tests/unit/docs/test_agent_docs_privacy.py $W/tests/unit/docs/test_deleted_ci_script_references.py $W/tests/unit/web/composer/test_tool_argument_error_category.py -n 0 -p no:cacheprovider`. The last test cites §4 S0 and §5.2, and neither heading may change.
4. Search the edited plan for home-directory paths (`grep -nE '/(home|Users)/'`). Expect no output. For the positive
   control, run the same grep on `$L/understand-docs.md`, which contains such a path.
5. `$L/r1r2/appendix_a_fork_check.py` will now fail its own `assert scoped != sql`. That is expected: it patched the
   old text. Note this in the commit body.

**Commit:** `docs(plans): record R1/R2 rulings, withdraw S2/S3, correct §2.3 evidence and Appendix A fork join`.

### T2 — Defect A: keep the model's key order in the replayed transcript

**Production change:** `src/elspeth/web/composer/tool_batch.py:498`, in `_replace_llm_tool_call_arguments`:

```python
encoded = json.dumps(provider_arguments, separators=(",", ":"))
```

Add one sentence to the docstring: the transcript keeps the model's key order, because map order in `sources`,
`row_union.branches` and `coalesce.branches` is semantic. Nothing else changes (D1, D2).

**New test file:** `tests/unit/web/composer/test_transcript_argument_order.py`. Keep it separate from
`test_compose_loop_wire_decode.py`, so a sibling branch appending to that file cannot conflict with it.

1. **Unit test** on `_replace_llm_tool_call_arguments`, parametrised over every `ToolContractDialect` member and over
   `semantic=True`:
   - The input is a `set_pipeline` with `sources` in the order `zeta_src, alpha_src`, a coalesce and a row_union
     whose branches run `zeta, alpha`, and node keys in the order `id, node_type, input, on_success, branches`.
   - Assert the decoded order of each map.
   - Assert that the exact bytes equal `json.dumps(expected_provider_arguments, separators=(",", ":"))`, so the test
     is an exact-bytes pin that tells sorted output from preserved output. The existing sentinel byte pin cannot do
     this, because its keys are already in sorted order.
   - **RED reason:** the decoded order is `['alpha','zeta']`.
2. **Compose-loop test, success path (caller `:1142`).** Lift `$L/probes/test_probe_transcript_e2e.py`:
   `ComposerServiceImpl._run_one_turn_for_test` with a recording fake LLM that stores
   **`copy.deepcopy(messages)`** for each call. A shallow copy would show the last rewrite, because
   `_replace_llm_tool_call_arguments` mutates `function["arguments"]` in place (`understand-transcript.md` §4 trap).
   - Assert that turn 2's replayed `set_pipeline` keeps `zeta, alpha` for coalesce, row_union and `sources`.
   - **RED reason:** it is sorted.
3. **Compose-loop test, finalization path (callers `:1377`/`:1481`).** Use the T0 step 6 fixture, with a coalesce
   whose branches are reversed.
   - Assert the order on the provider turn after finalization.
   - **This is the one path whose order preservation has not been executed.** `wire_required_controls` and
     `canonicalize_authored_node_review_requirements` were only read (`understand-transcript.md` §2).
   - If the test still shows sorted order after `:498` is fixed, finalization reorders the arguments itself. **Stop
     and report** with the site, rather than widening the fix silently.
4. **Compose-loop test, inline-custody path (caller `:1517`).** Use a `set_pipeline` with an `inline_blob` source.
   The existing custody fixtures are in `tests/unit/web/composer/conftest.py` and `test_promote_set_pipeline.py`.
   Add reversed coalesce branches. Assert the order after the `inline_blob` → `blob_id` rewrite.
5. **Control:** a sentinel replay (the `:936` path) still produces today's exact bytes. That is characterization, and
   the existing `test_sentinel_bytes_equal_todays_output` covers it. Run it unchanged.

**Mutation control:** after GREEN, put `sort_keys=True` back. Tests 1 to 4 must go RED. Log to
`$L/t2-mutation.log`, then revert.

**Regression set:** the 6 transcript-reading files listed in `understand-transcript.md` §4, plus
`test_compose_loop_wire_decode.py` and `test_dispatch_arms_characterization.py`. The 86-file compose-loop set was
already measured under exactly this change (1 failure, which is in the base-red set). Do not re-run it; the lead's
full suite covers it.

**Gates:** the every-task set, plus G-wire. `tool_batch.py` is in the census's AST binding, and nothing is renamed.

**Commit:** `fix(composer): keep planner key order when replaying set_pipeline arguments (Defect A)`.
- The body carries the per-site decision table (§3.2).
- It states that no epoch is needed, because the transcript is never persisted verbatim.

### 3.2 Per-site decisions for Defect A (goes into the T2 commit body)

| Site | What it serialises | Decision |
|---|---|---|
| `tool_batch.py:498` | replayed tool-call arguments (model-facing) | **fix: drop `sort_keys`** |
| `service.py:5934` | the reply-only turn quoting historical messages | leave. It sorts the message envelope only, and `arguments` is an opaque string that inherits the fix |
| `turn_audit.py:237` | persisted P4 assistant rows | already keeps order |
| `audit.py:637-640`, `audit_storage.py:204`, `service.py:2779-2797` | RFC 8785 `arguments_canonical`/hash, redacted canonical | sorted by design. Order is bound by the authority projection (T3 to T5) |
| `pipeline_planner.py:3870` | planner `current_state` | leave. Inert (§1.4 item 5) |
| `pipeline_planner.py:1937` | planner transcript | already keeps order: it replays `raw_arguments` |
| `pipeline_planner.py:5068`, `discovery_cache.py:97` | dedupe and cache keys | sorted by design. Discovery tools carry no order-semantic maps |
| `redaction.py:1400`, `wire_projection.py:305`, `prompts.py:668` | counts, schema text, diagnostics | not planner arguments |
| `prompts.py:489` | compose-loop `current_state` | already keeps order (the warning at `:200`) |
| `execution/envelope.py:343` and `sessions/service.py:10592` | run envelope | **separate live defect** (§1.4 item 1) |

### T3 — Defect B, part 1: bind coalesce mapping-branch order

**Production change:** `src/elspeth/web/composer/authority_hashing.py`.

- Replace the single row_union constant with one tag per node type:
  ```python
  _ORDERED_BRANCH_SCHEMAS: Final[Mapping[str, str]] = MappingProxyType({
      "row_union": "composer.row-union-ordered-branches.v1",  # byte-identical to today
      "coalesce": "composer.coalesce-ordered-branches.v1",
  })
  ```
- **`project_composer_authority_payload`:** for each top-level `nodes` entry whose `node_type` is in the map and whose
  `branches` is a `dict`, replace `branches` with `{"schema": <that node type's tag>, "items": [[alias, connection], ...]}`.
  List branches pass through unchanged. Do not read `merge` or `policy` (D4).
- **`restore_composer_authority_payload`:** for the same node types, a `dict` `branches` must be exactly the
  projection carrying **that node type's** tag. Each item is a 2-list of `str` with no duplicate alias. Anything else
  raises `ValueError`.
  - Keep the row_union error strings byte-identical ("row-union authority projection branches are malformed" and
    "...branch item is malformed"). Use "coalesce authority projection ..." for coalesce.
  - This is R2 conditions 1 to 4 on an audit-internal surface.
- **Docstrings:** update the module docstring, both function docstrings, and `pipeline_proposal.composition_content_hash`.
  Its sentence "Non-row-union content retains the historical preimage" becomes false and must change.
- Record the pre-existing latent ambiguity in the module docstring; do not fix it. A real branch map whose keys are
  literally `schema` and `items` would look like a projection. It is harmless, because restore runs only on stored
  projections, and a real map with those keys is itself projected before it is stored.

**New test file:** `tests/unit/web/composer/test_coalesce_authority_hashing.py`. Mirror
`test_row_union_authority_hashing.py`, which came in with `49825dd86`. Copy its small builders locally instead of
importing a private helper across test modules.

| # | Test | RED expectation |
|---|---|---|
| 1 | Reordering a non-first coalesce map changes `composition_content_hash`, parametrised over `merge=union` with `policy` `last_wins` and `first_wins`, `merge=nested`, and `merge=select` | equal hashes (the gap) |
| 2 | ... changes `pipeline_draft_hash` | equal |
| 3 | ... changes `composer_authority_hash`, `_pipeline_private_arguments_hash`, `_pipeline_audit_payload_hash` and `_composition_state_data_content_hash` | equal |
| 4 | ... changes the `set_pipeline` dispatch binding (`begin_dispatch` authority canonical and hash) without mutating the arguments | equal |
| 5 | Redacted storage keeps the tool's map shape in the generic canonical, and the authority canonical is the projection (`audit_storage.py:250`) | the authority canonical is still a map |
| 6 | A persisted binding whose authority members were reordered or tampered with is rejected (`pipeline_commit._validate_pipeline_authority_binding`, `audit_storage._validated_invocation_arguments`) | accepted |
| 7 | Restore rejects: a plain dict on coalesce, the row_union tag on a coalesce node, the coalesce tag on a row_union node, a duplicate alias, a non-`str` connection, and a 3-item entry | the plain dict passes through |
| 8 | **Characterization:** list-form coalesce gives `arguments_canonical == authority_arguments_canonical`, byte for byte | green on arrival |
| 9 | **Characterization:** the row_union canonical equals the T0 golden byte for byte | green on arrival |
| 10 | **Characterization of R2 conditions 1-2:** `restore(project(p)) == p`, with coalesce key order preserved (compare `list(...)`, not `==`) | green on arrival |
| 11 | The runtime preflight cache key (`RuntimePreflightKey.state_content_hash`) differs for two states that differ only in coalesce order | equal (I, confirm in RED) |

**Mutation controls:**
- Remove `"coalesce"` from the map. Tests 1 to 7 and 11 must go RED.
- Change one character of the row_union tag. Test 9 must go RED.
- In restore, drop the duplicate-alias check. The duplicate case of test 7 must go RED.
- In `project`, iterate over `sorted(branches.items())`. Tests 1 to 4 and 10 must go RED.

**Re-run** (they build coalesce nodes and touch hashes):
- `test_row_union_authority_hashing.py`, `test_sparse_argument_presence.py`, `test_owned_composition_state_authority.py`,
  `test_pipeline_commit_operation_authority.py`, `test_compose_loop_persistence.py`, `test_state.py`,
  `test_state_serialisation_contract.py` and `test_pipeline_planner.py`;
- `tests/unit/web/sessions/test_tool_invocation_redaction.py`, `test_composer_proposals.py` and `test_routes.py`;
- `tests/integration/web/composer/test_pipeline_proposal_lifecycle.py`;
- the `guided/` set in `understand-hash.md` §7.

Any literal hash pin that moves must be a hash of a payload containing a mapping-form coalesce. Regenerate it with
its producer's documented method, and list it in the commit body. A moved pin whose payload has no mapping-form
coalesce is a **stop condition**.

**Gates:** the every-task set.

**Commit:** `fix(composer): bind coalesce mapping-branch order in the authority projection (Defect B)`.

### T4 — Defect B, part 2: bind multi-source `sources` order

**Production change:** `authority_hashing.py`.
- **`project`:** if the top-level `sources` is a `dict`, replace it with
  `{"schema": "composer.ordered-sources.v1", "items": [[name, spec], ...]}`. This applies to every dict, including
  `{}` and single-entry maps (D3). A top-level `source` is untouched.
- **`restore`:** a top-level `dict` `sources` must be exactly that projection. Each item is a 2-list of a `str` name
  and a `dict` spec, with no duplicate name. Anything else raises `ValueError`.
- The T0 step 5 scope check must have passed. **Every** projection caller passes a flat pipeline or state dict:
  `service.py:575/593/646/682`, `pipeline_planner.py:3427/3454/3533`, `pipeline_custody.py:263`, `redaction.py:2515`,
  `audit.py:639`, and `pipeline_proposal.py:610/797`.

**New test file:** `tests/unit/web/composer/test_sources_authority_hashing.py`.

1. Reordering a two-entry `sources` map changes `composition_content_hash`, the draft hash, the private-arguments hash
   and the dispatch binding. RED expectation: equal.
2. Restore rejects: a plain dict, a wrong tag (including either branch tag), a duplicate name, a non-object spec, and a
   3-item entry. RED expectation: the plain dict passes.
3. **Characterization:** a pipeline using singular `source` has a canonical byte-equal to the T0 golden.
4. **Characterization:** `restore(project(p)) == p`, with order preserved.

**Mutation controls:**
- Remove the `sources` arm. Tests 1 and 2 must go RED.
- Project with `sorted(...)`. Test 1 must go RED.

**Expected pin moves.** Every `CompositionState` has a `sources` dict, so **every** `composition_content_hash` moves,
and so does every draft hash whose pipeline uses the `sources` map.

- **Frontend fixture:** `src/elspeth/web/frontend/src/api/__fixtures__/gateProposalProjection.json:9` pins a
  `draft_hash`. The pipeline it is built from uses `sources: {"primary": ...}`
  (`tests/unit/web/composer/guided/test_gate_projection_fixture.py:90-97`), so the fixture **will** regenerate (I).
  Confirm that it goes red, then regenerate it by the method that test documents.
- Then run the frontend test that reads the fixture:
  `cd $W/src/elspeth/web/frontend && npx vitest run src/api/guidedDecoder.gate.test.ts`. No frontend source changes,
  so no frontend rebuild or deploy step is needed.
- Every other moved literal must trace to a payload with a `sources` map or a `CompositionState`. Regenerate it by its
  producer's method and list it in the commit body.
- `test_end_advisor_gate_reaches_prompt_template_pipeline_p5_budget_exhaustion` is already red on its `for_graph`
  hash. Diff its message against `$L/t0-base-red.log`. T5 will move it again.

**Re-run:** the T3 set, plus `test_gate_projection_fixture.py`.

**Gates:** the every-task set.

**Commit:** `fix(composer): bind multi-source ingest order in the authority projection (Defect B)`.

### T5 — Defect B, part 3: the advisor sign-off fingerprint binds the same order

**Production change:** `src/elspeth/web/execution/completion_gates.py:166`, `completion_gate_fingerprint`. Build
`{sources, nodes, edges, outputs}` from `state.to_dict()`, pass it through `project_composer_authority_payload`, and
hash `{"schema": _FINGERPRINT_SCHEMA, "metadata": ..., **projected}`. `_FINGERPRINT_SCHEMA` is unchanged (D6). Update
the docstring.

**Tests:** add them to the existing completion-gate test module. Find it with
`grep -rln completion_gate_fingerprint tests/`, and use a positive control that must hit `test_completion_gate_roundtrip.py`
or its unit sibling.

1. Two states that differ only in coalesce branch order, row_union branch order or `sources` order give different
   fingerprints. RED: equal (M, `probe_hash_order.log`).
2. `advisor_block_covers_unchanged_graph` returns `False` for a reordered graph.
3. `merge_completion_gates` downgrades a carried advisor fact to pending when only the order changed.
4. **Characterization:** reordering `outputs` or `edges` (lists) changes the fingerprint. This proves the list parts
   still bind.

**Mutation control:** revert to the raw `state_d` values. Tests 1 to 3 must go RED.

**Re-run:** `tests/unit/web/sessions/test_completion_gate_roundtrip.py`, `tests/unit/web/execution/test_routes.py`,
`test_compose_loop_interpretation_review_dispatch.py`, and every file that `grep -rln "for_graph" tests/` finds.
The p5 test stays red. Record its new hash pair against the T0 log.

**Gates:** the every-task set.

**Commit:** `fix(composer): bind branch and source order in the advisor sign-off fingerprint`.

### T6 — Session schema epoch 66 → 67 (last commit)

**Before editing**, re-check that the target is still 66:
`git show release/0.8.1:src/elspeth/web/sessions/models.py | grep -n '^SESSION_SCHEMA_EPOCH'`. If `release/0.8.1`
has moved to 67 or higher, **stop and report**: the implementer may not rebase, and the lead takes the next free
number.

**Files (10). Mirror `4f0e16c6c` (65 → 66).**

1. `src/elspeth/web/sessions/models.py:362`: set `SESSION_SCHEMA_EPOCH = 67`, and add a `# 67:` history line: "the
   composer authority projection binds coalesce mapping-branch order and multi-source `sources` order, and the advisor
   fingerprint binds both; stored draft, content, private-argument, dispatch-binding and fingerprint preimages
   changed; pre-1.0 delete and recreate, no legacy path."
2. `src/elspeth/web/sessions/schema.py:39`: set `_COORDINATION_HARD_CUT_EPOCH = 67` and add a comment. **`schema.py:534`
   requires exact equality with the session epoch. Miss this and no session DB opens.**
3. `tests/integration/web/composer/guided/test_schema9_epoch.py:61`: change 66 to 67 and add a history comment.
4. `tests/unit/contracts/test_web_blob_fencing.py:3203`: the same.
5. `tests/unit/web/sessions/test_blob_inline_resolutions_schema.py`: rename `..._epoch_is_66` to `_is_67`, and update
   both `SESSION_SCHEMA_EPOCH == 66` and `PRAGMA user_version == 66`.
6. `tests/unit/web/sessions/test_interpretation_events_table.py`: rename `test_current_session_schema_epoch_is_66` to
   `_is_67`, and update the assert and its comment.
7. `tests/unit/web/sessions/test_proposal_blob_effect_receipts_schema.py:25`: change 66 to 67.
8. `tests/unit/web/sessions/test_schema.py:366`: change 66 to 67.
9. `docs/runbooks/staging-session-db-recreation.md`:
   - heading "(session epoch 66 and Landscape epoch 43)" → 67;
   - line 7 "from 53 to 66" → 67;
   - a new paragraph after the epoch-66 paragraph at `:55-59`, saying that session epoch 67 binds ordered coalesce
     branches and ordered sources in the composer authority hashes, and that epoch-66 databases are recreated. Keep
     the 66 sentences as history;
   - `:202` "repair the epoch-66 release forward" → 67;
   - `:204` "session-epoch-66/Landscape-epoch-43 record" → 67;
   - `:822` "# expect 66 (== SESSION_SCHEMA_EPOCH)" → 67.

   `test_staging_session_recreation_policy.py:31-35` pins each of these.
10. `CHANGELOG.md:9-10`, in the 0.8.1 section:
    - `` `SESSION_SCHEMA_EPOCH` advances from 53\nto 66 `` → `to 67`. **The line break between "53" and "to" is part
      of the asserted string (`test_replica_schema_cutover_belongs_to_0_8_1`).**
    - Add "ordered coalesce branches and source order in composer authority hashes" to the epoch's reason list.
    - Add one "Fixed" line for Defect A (the transcript keeps planner key order) and one for Defect B.
    - The target version is 0.8.1. The base is `release/0.8.1`, and the test pins the clause to the 0.8.1 section.

**Do not touch** the 6 base-red doc, website and receipt pins (§2). After T6 they fail against 67 instead of 66. That
is the same ids, with a new expected value.

**Tests:**
- the 6 pin files from items 3 to 8;
- `tests/unit/docs/test_staging_session_recreation_policy.py`, which must pass;
- the base-red set, where the failure-set diff against `$L/t0-base-red.log` must show the same 10 ids;
- `tests/unit/architecture/test_session_db_mutation_authority.py`. It is line-pinned and already red. The comment in
  `schema.py` shifts lines, so diff its messages and do not re-pin it.

There is no RED/GREEN cycle here: the pins are the tests. As the mutation control, set only `models.py` to 67. Every
session-DB test must then fail at `schema.py:534` ("coordination schema epoch mismatch"), which proves item 2 is
load-bearing. Revert, then apply both.

**Gates:** the every-task set.

**Commit:** `chore(sessions): session schema epoch 67 for ordered coalesce and source authority hashes`. The body
names the deploy obligation from §5.2.

---

## 4. Invariants to prove (in the handover)

1. **The transcript keeps the model's order.** T2 tests 1 to 4, with the mutation log.
2. **Row_union and singular-`source` preimages did not move.** The T3 test 9 and T4 test 3 goldens, captured at base
   in T0.
3. **Each order-semantic map is bound by every stored hash that re-derives it.** This covers content, draft,
   private-arguments, audit-payload, state-data, dispatch binding and fingerprint (T3, T4, T5).
4. **Restore is strict.** The tag for one kind is not accepted for another, duplicates are rejected, and a
   non-projection map is rejected (T3 test 7, T4 test 2).
5. **The trust-tier corpus is unchanged** (G-tier diff), except for lines that are accounted for.
6. **No tool, schema or wire byte changed.** G-wire and G-skill stay green, and no test file of tool declarations is
   edited.

## 5. Epoch and deploy consequences (plain statement)

### 5.1 Why an epoch is required

These stored hashes are recomputed from stored payloads and compared, and a mismatch fails hard
(`AuditIntegrityError`):
- the `PipelineProposal.draft_hash` in `__post_init__`;
- the private-arguments and audit-payload bindings (`service.py:2340/2343`);
- `tool_arguments_hash` (`service.py:2373`, `pipeline_commit.py:413`);
- the dispatch-binding restore and its byte-equality check (`pipeline_commit.py:109-115`, read at `service.py:895` and
  `:13427`);
- `semantic_redacted_pipeline_arguments_hash` (`redaction.py:2513`).

After T3 and T4, every stored row whose payload contains a mapping-form coalesce or a `sources` map fails one of these
(`understand-hash.md` §4-5). The no-tech-debt rule forbids a legacy path, so the store is recreated.

Consumers that are **not** reasons for the epoch:
- the frontend, where the hashes are opaque;
- Landscape, which never receives them. There is no Landscape epoch change;
- the process-local preflight cache;
- custody `creating_arguments_hash`, which is compared row against snapshot and never re-derived.

### 5.2 Deploy obligation (for the lead's handover)

- **Session DB rename on deploy.** The epoch-66 `sessions.db` must be moved aside, and the service starts on a fresh
  epoch-67 store. The served config is `deploy/elspeth-web.env`.
- **Then run `elspeth composer users bootstrap-admin local <user> --note ...`** for every local account. Identities
  live in the session DB, so a rename leaves local accounts pending (401).
- No Landscape epoch change, and no frontend rebuild (T4 changes a test fixture only).
- **Merge-conflict risk:** at the start of this session, the main checkout had an uncommitted `CHANGELOG.md` edit made
  by another session. The lead's merge in the main checkout must reconcile it.
- If another lane lands an epoch bump on `release/0.8.1` first, T6 is redone at the next free number, with its history
  line folded in. It is never merged over.

## 6. Full-suite gate and merge procedure (the lead)

1. Check host capacity: no other suite running, and acceptable load. Then run
   `cd $W && scripts/full-suite-gate.sh --execute --detach --stages ruff,mypy,contracts,lints,pytest,testcontainer`.
   - Read the `stages :` launch line; the default is only ruff and pytest.
   - Wait on the `.done` artefact, never with a `pgrep -f` loop.
   - Read `summary.txt`, and require `frozen=yes`.
   - **Testcontainer is included**, because T6 changes the session schema epoch.
2. **Attribute every red.** Re-run each failing id with `-n 0`.
   - Diff the failure set against the 10-id base-red set (§2) and its messages (`$L/t0-base-red.log`).
   - The p5 test and the 6 doc pins are expected, with new hash or epoch values.
   - Anything else belongs to this branch. So does a known flaky family whose failures differ from a base run.
3. `lints`: diff as a multiset against `lints-base.norm`, and list every addition for the operator's sign-bundle.
   Stage nothing.
4. **Handover, not merge.** Stop at ready-for-merge and report:
   - the commit list;
   - the §4 evidence;
   - the pin moves (T3, T4, T5, T6);
   - the §5.2 deploy obligation;
   - the §1.4 tickets owed. Item 1, the execution envelope, is the one to raise first.

   Merge only on John's word, after running `git merge-tree` against the current `release/0.8.1` tip and
   `scripts/branch-safety-check.sh --intent merge`. Push only when asked.

## 7. Risks and stop conditions

### 7.1 Risks

- **Many hash pins move in T4.** `sources` is always a dict on a state, so every content hash moves. This is honest
  churn under an epoch cut, and John's standing rule is to take the churn rather than reshape behaviour to avoid it.
  The control is attribution: every moved literal must trace to a payload that holds `sources`.
- **The finalization path may reorder on its own** (T2 test 3). If it does, that is a second Defect-A site: stop and
  report.
- **The approval binds an order that the runtime does not yet honour** (§1.4 item 1). T3 to T5 are still correct, but
  they are not end-to-end until the envelope defect is fixed.
- **The epoch number could collide** with sibling lanes (T6 re-check).

### 7.2 Stop conditions (stop and report; do not improvise)

- A RED fails for a reason other than the one named.
- T0 step 5 finds a non-`set_pipeline` tool with a top-level `sources` property.
- T2 test 3 is still sorted after the fix.
- A moved hash pin in T3 or T4 cannot be traced to a payload holding the projected map.
- The G-tier diff has an unaccounted `>` line.
- `release/0.8.1` is no longer at epoch 66 when T6 starts.
- Any change would need a suppression, a compatibility shim, dual acceptance or a legacy hash path.

## 8. Plan review

This section is filled in by the review and fix cycle (Codex), in the same way as the S1 plan's §9.
