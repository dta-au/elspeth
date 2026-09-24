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
- **Citations:** every `path:line` was measured in `$W` at `c4c52c110`, and re-checked by two read-only critiques at
  `41125ac74` (§8). Lines drift as tasks land, so re-anchor each one with `grep -n` before editing. Evidence is marked
  as follows:
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
   runtime. Bind their order the way the row_union precedent does. Two plugin-owned option maps were also found to be
   order-semantic; they are recorded for a scope ruling, not fixed (D13, §1.4 item 11). Restore is narrowed to check only the
   projection's own structure, which also fixes a base defect in the row_union arm (D11). This changes persisted hash
   preimages that are re-verified with a hard failure (every stored composition content hash moves), so it ends with
   a session schema epoch bump, as the last commit.

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
| D2 | **Every other site that re-serialises planner-authored arguments is left alone.** §3.2 of this plan gives the table. | The only other site that sorts arguments the model sees is `pipeline_planner.py:3870` (the planner's `current_state`, serialised with `canonical_json`). It is inert on **freeform only**: freeform reaches the planner through `_plan_and_stage_empty_pipeline` (`service.py:5289`, gated on `state_is_empty` at `:4180-4191`). **Guided-full still reaches it with a non-empty state**: `plan_guided_full_pipeline` (`composer/service.py:4381`) passes `project_server_owned_option_metadata(current_state.to_dict())` at `:4435` with no emptiness gate (R). It is left alone because guided mode is being removed (John, 2026-09-22) and no further investment goes into the guided lane; it is recorded as §1.4 item 5. Every other sorted site is an audit, cache or dedupe key that is sorted by design. Order is bound separately by the authority projection, which T3 to T5 extend. |
| D3 | **`sources` is part of Defect B**, with its own schema tag. It is projected unconditionally (every dict, including `{}` and a one-entry dict). The singular `source` field is untouched. | The brief says "any other order-semantic map found". `sources` order decides ingest order (`leader_drain.py:192-203`) and the "first source" (`run_lifecycle.py:207`, `processor_factory.py:430`) (R, `understand-transcript.md` §7). The hash reader's objection was that the engine topology hash does not bind it either. That is equally true of coalesce's gap in the composer, so the objection is recorded in §1.4 as a separate engine defect rather than as a reason to leave the composer blind. Two alternatives are rejected. Projecting only when there are two or more entries makes the preimage shape depend on the count. Excluding only `{}` means restore must accept both a plain `{}` and a `{schema, items}` envelope for the same field, which is dual acceptance. **Consequence, measured:** `CompositionState.to_dict()` always emits `"sources"` (`state.py:7286`), and the constructor folds a singular `source` into `sources` (`state.py:7170`), so after T4 **every** `composition_content_hash` moves, including an empty state's (M, `$L/critique-1/probe_sources_always.log`: "empty-state content hash moves under a T4-shaped projection: True"). Raw `set_pipeline` arguments that use the singular `source` and carry no `sources` key do not move. |
| D4 | **The coalesce projection does not depend on `merge` or `policy`.** | The engine already carries `branch_order` unconditionally (`builder.py:545-557`). Binding `select` as well is harmless, and it keeps the rule to one line (R, `understand-hash.md` §2). Note on the collision rule: `policy` is the **arrival** policy (`require_all`, `quorum`, `best_effort`, `first`; `core/config.py:1015`). Field collisions are governed by `union_collision_policy` (`config.py:1023`, default `last_wins`), which the composer cannot set: `yaml_importer._UNSUPPORTED_COALESCE_FIELDS` lists it (`yaml_importer.py:35-41`) and `NodeSpec.from_dict` drops it (M, `$L/critique0/policy_probe.log`). So every composer coalesce with `merge=union` runs under `last_wins`, and its branch order decides the collision winner. |
| D5 | **Every new projection has its own schema tag. The row_union tag stays byte-identical.** Tags: `composer.coalesce-ordered-branches.v1` and `composer.ordered-sources.v1`. | Row_union preimages must not move (T3 pins them with a golden). A projection of one kind can then never be restored as another. |
| D6 | **`_DRAFT_HASH_SCHEMA` stays `...envelope.v3`, and `_FINGERPRINT_SCHEMA` stays `elspeth.completion_gate_graph.v2`.** | Each changed part of a preimage now carries its own schema tag, so the change separates its own domain. The epoch cut in T6 deletes every stored row, so no v3 or v2 preimage from before the change survives to collide. The cost of choosing otherwise: a v4 draft envelope moves every `draft_hash` literal, including single-`source` pipelines that T3 to T5 leave unchanged. Precedents in the other direction are `49825dd86` (draft v2→v3) and `d7541609c` (fingerprint v1→v2). If the lead prefers to mirror them, the bump goes into T4 for the draft hash and T5 for the fingerprint. |
| D7 | **`completion_gate_fingerprint` is routed through the projection (T5).** | It is the same class of gap: the advisor's sign-off is carried forward onto a graph whose merge order or ingest order changed (`completion_gates.py:166`, `advisor_block_covers_unchanged_graph` at `:383`). The epoch cut in T6 is being paid anyway, so the fix is almost free. Leaving it out would be the kind of debt John has ruled against. |
| D8 | **The epoch bump is 66 → 67. It is the last commit (T6) and touches 10 files (§5).** | Stored hashes are recomputed on read and fail hard with `AuditIntegrityError` (§5.1). John's no-tech-debt rule forbids dual acceptance or a legacy hash path. |
| D9 | **Appendix A joins on `(session_id, call_id)`**, not on `parent_assistant_id`. | The brief names the session-scoped join. It gives the same counts as the parent join on all 13 DB copies (M, `understand-docs.md` §1.2). The parent join is mentioned in the appendix as the tighter alternative. |
| D10 | **The status line in T1 cites the S1 gate record as it stands, with the measured attribution of the two non-drift reds.** At `d5f8c5aac` the result was FAIL, with 10 failures and `frozen=yes`. | All 10 red ids also fail at `c4c52c110` (M, §2). The tree immediately before S1 is `c4c52c110^1` = **`e69498f6c`**, not the S0 merge `85ebf2739`: `34f0fed73`, the S1 branch's root commit, has parent `e69498f6c`, and `git merge-base c4c52c110^1 c4c52c110^2` is `e69498f6c`. Measured on `git archive` exports with `elspeth.__file__` inside each export: `test_composer_bedrock` **passes at `85ebf2739` and fails at `e69498f6c`** with the same `_MalformedLLMResponseError`, so it was introduced by `e69498f6c` ("fix(composer): reject malformed advisor checkpoint envelopes"), a direct `release/0.8.1` commit that is **not part of S1**. `p5_budget_exhaustion` already fails at the S0 merge `85ebf2739`, so it predates S1. Whether it predates S0 was not measured: `85ebf2739` is the merge itself (parents `780ef0f56` and `35d6bad86`), and no run was made at its pre-S0 parent `780ef0f56` (M, `$L/rev-85ebf2739.log`, `$L/rev-e69498f6c.log`, `$L/critique0/pre-s1-*.log`, `$L/critique-1/crit1-reds-*.log`). The other 8 are epoch and line-pin drift. No red in the set is attributable to S1. |
| D11 | **Restore checks only the projection's own structure. The value position accepts any JSON value, for row_union, coalesce and `sources` alike.** Restore still rejects: a plain map where a projection must be, a missing or foreign schema tag, extra envelope keys, a non-list `items`, an item that is not a 2-list, a non-`str` key, and a duplicate key. The row_union error strings stay byte-identical. | The dispatch audit is opened before any schema gate, and a malformed `set_pipeline` is still projected (`tool_batch.py:1120-1135`, `audit.py:639-640`). Persistence then restores it (`audit_storage.py:103`). Today the row_union arm's `type(item[1]) is not str` check turns an ordinary planner argument error into `AuditIntegrityError` at persistence: M, `$L/critique0/probe_malformed_e2e.log` through the real compose loop ("row_union_int_connection: status=arg_error ... AuditIntegrityError"), and `$L/critique0/probe_malformed_roundtrip.log` for `{'a': 1}` and `{'a': None}`. Extending that check to coalesce and `sources` would spread the defect, and it breaks R2 condition 1 (lossless), because restore would reject its own projection's output. The value check was never what guards integrity: both restore callers compare `primitive_canonical_json(restored)` with the stored generic `arguments_canonical` (`pipeline_commit.py:112`, `audit_storage.py:106`), which catches any value tamper. No preimage changes, because `project` is untouched. T3 therefore fixes the row_union arm as well. |
| D12 | **The frontend content-equality check is recorded, not changed** (§1.4 item 10). | It is a UI freshness heuristic, not a hash consumer; the server re-runs preflight at execute. It already disagrees with the backend on row_union order since `49825dd86`. Changing it adds a frontend rebuild and a surface the brief does not name. The lead or John decides whether to widen scope. |
| D13 | **Plugin-owned option maps are not projected. This plan binds the order of the topology maps the composer owns (`branches`, `sources`) and no others; the order-semantic option maps Codex found are recorded (§1.4 item 11) for John's scope ruling.** | The brief asks for "any other order-semantic map found", and two are found: an LLM transform's `options.queries` (`plugins/transforms/llm/multi_query.py:306-307` builds the specs in map order; `transform.py:915-945` runs them in sequence and stops at the first non-retryable failure, recording `discarded_successful_queries`) and `field_mapper`'s `options.mapping` (`field_mapper.py:678-730` writes output fields in map order, and an observed-schema CSV sink takes its columns from `list(row.keys())`, `csv_sink.py:534-536`). A reorder of either leaves `composer_authority_hash` equal (M, `$L/codex/recheck_f1_f2.log`, with the row_union reorder as the known positive). Binding them is not a projection of the same kind: the composer cannot tell an order-semantic option map from an order-free one without a per-plugin declaration, `project_composer_authority_payload` deliberately inspects only top-level `nodes` so that it never assigns topology meaning to plugin-owned nested dictionaries (its docstring, `authority_hashing.py:25-27`), and projecting every option dictionary would make every hash sensitive to key order the planner happens to emit in maps where order means nothing. It needs a plugin-contract design (which option fields declare an ordered map), which is out of this plan's reach; Codex's own fix offers exactly this choice. The handover raises it with §1.4 item 1, because the durable execution envelope alphabetises these maps too. T2 already keeps their order in the transcript. |

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
   - Nothing detects it, because `validate_run_execution_input` (`envelope.py:599`), the restore-path comparison
     `payload.executable_config != deep_thaw(frozen.executable_config)` (`envelope.py:559`) and the digests all
     compare without regard to order.
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
4. **`_pending_interpretation_validation_candidate_digest`** (`sessions/pending_interpretation.py:1561`) is an
   order-blind raw hash over nodes. Resolving an interpretation does not reorder branches, so the gap is practically
   unreachable. Low priority.
5. **`pipeline_planner.py:3870` sorts the planner's `current_state`.** It is inert on freeform, which only plans an
   empty pipeline. Guided-full still sends it a non-empty state (`composer/service.py:4435`), so on guided-full it is
   a live second Defect-A site (D2). Guided mode is being removed; if it is not, this needs its own fix.
6. **The table comment on `composition_rejection_events` is false for ARG_ERROR rows.** The comment is at
   `sessions/models.py:1679` and says the table holds the "EXACT serialized tool response the planner saw". For those
   rows the table holds `{error_class, error_message}`, and its `error_code` column holds the exception class
   (`understand-docs.md` §3, F1).
7. **The S-gate `(loc, code)` pairs are persisted nowhere**, so the option-tool split that T1 defines cannot yet be
   measured. Persisting them is redaction-safe, but it touches S1 ruling 6 ("RejectionRecord untouched"). The lead
   decides. Persisting the pairs alone still cannot separate options sent as a string from other wrong types
   (`invalid_type` covers every JSON-schema `type` failure); that needs the sent JSON type as well, which is also
   value-free.
8. **Six epoch doc, website and receipt pins already fail on base (65 against 66).** The brief forbids editing them.
   After T6 they will expect 67; their count does not change.
9. **Planner-turn cost has no adopted definition.** `understand-docs.md` §3 proposes one, for John to accept.
10. **The frontend content-equality check stays order-blind (D12).** `frontend/src/lib/compositionContent.ts:17-22`
    says its field set is the one the backend hashes into `composition_content_hash` and must be kept in step, but its
    `deepEqual` (`:40-66`) ignores object key order. `stores/subscriptions.ts:70` uses it to skip the clear and
    re-validate on a version bump. After T3 and T4 the backend treats a reorder of coalesce branches or of `sources`
    as a content change; the frontend keeps the old verdict on screen. That is already true of row_union order today.
    The run gate is unaffected (the server re-runs preflight at execute). Fixing it is a small frontend change plus a
    rebuild.
11. **Order-semantic plugin option maps stay unbound by the composer hashes (D13; needs John's scope ruling).**
    An LLM transform's `options.queries` sets the order in which queries run and which provider calls happen before a
    non-retryable failure; `field_mapper`'s `options.mapping` sets output field order, which an observed-schema CSV
    sink writes as its column order. A reorder of either leaves every composer authority hash equal (M,
    `$L/codex/recheck_f1_f2.log`). Binding them needs a plugin-declared ordered-map contract, not another composer
    projection. Other plugins were not surveyed for the same property. The brief asked for "any other order-semantic
    map found", so the handover states plainly that these two were found and not fixed.

### 1.5 The ordering rule (hard)

The tasks run T1 → T2 → T3 → T4 → T5 → T6. **T6 (the epoch) is the last commit**, and T3 to T6 land and deploy
together:
- after T3, an epoch-66 store cannot read its own coalesce-map proposals or dispatch envelopes;
- after T4, it cannot read any row that carries a composition content hash (every proposal with a present base,
  whatever its pipeline shape; D3), nor any row whose arguments carry a `sources` map.

No commit between T3 and T6 may be merged or deployed on its own.

---

## 2. Measured starting point

Everything in this section was measured at the base `c4c52c110`. The `release/0.8.1` tip has since moved, and the
base-red set below does not hold there; §6.1 records what changes at the merge.

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
| **Base-red set (10 ids)** | 6 epoch doc, website and receipt pins (65 against 66): `tests/unit/website/test_release_site_contract.py::test_get_started_has_runnable_cli_and_complete_composer_paths`, `tests/unit/docs/test_release_version_surfaces.py::test_operator_schema_version_examples_match_live_constants`, `tests/unit/docs/test_release_version_surfaces.py::test_scenario_b_runbook_record_matches_live_release_derivation`, `tests/unit/docs/test_readme_release_surface.py::test_readme_operational_cutover_states_the_live_schema_epochs`, `tests/unit/web/test_azure_container_apps_runbook_contract.py::test_compatibility_record_is_byte_bound_to_the_live_derivation`, `tests/unit/web/test_azure_container_apps_runbook_contract.py::test_every_epoch_literal_matches_the_live_constants`. 2 line-pinned: `tests/unit/architecture/test_session_db_mutation_authority.py::test_live_connection_domain_classification_is_exact`, `tests/unit/architecture/test_session_db_mutation_authority.py::test_session_schema_authority_is_exact_contained_and_bidirectional`. Plus `tests/unit/web/composer/test_compose_loop_interpretation_review_dispatch.py::test_end_advisor_gate_reaches_prompt_template_pipeline_p5_budget_exhaustion` and `tests/unit/web/test_composer_bedrock.py::test_bedrock_advisor_uses_default_chain_without_tools_or_gateway_overrides` (`_MalformedLLMResponseError`). **These are exactly the 10 FAILED ids of the S1 gate at `d5f8c5aac`.** Re-run at `41125ac74`: 10 failed, 3 passed, and the failing ids are exactly these 10 | `understand-hash-epochpins.log`, `probes/p5_base.log`, `plan-bedrock-base.log`, `critique0/base-red-rerun.log` (M); the S1 gate record is `summary.txt` and `pytest.log` under `.claude/worktrees/strict-tool-contracts-s1/.claude/lanes/s1/gate/20260923T221535Z-strict-tool-contracts-s1-2552647/`, relative to the main checkout root (gitignored lane state, so it may be gone) |
| **Attribution of the two non-drift reds** | The tree before S1 is `c4c52c110^1` = `e69498f6c` (the parent of S1's root commit `34f0fed73`). `test_composer_bedrock` passes at `85ebf2739` and fails at `e69498f6c`: it was introduced by `e69498f6c`, outside S1. `p5_budget_exhaustion` fails at `85ebf2739`, `e69498f6c` and `c4c52c110` with the same hash pair. Its assertion compares two computed values (`result.advisor_gate_decision == AdvisorGatePassed(completion_gate_fingerprint(state))`, `test_compose_loop_interpretation_review_dispatch.py:3773`), not a literal pin | `$L/rev-85ebf2739.log` (1 passed), `$L/rev-e69498f6c.log` (1 failed), each with `ELSPETH_FILE` inside its export; `critique0/pre-s1-*.log`; `critique-1/crit1-reds-*.log` (M) |
| Restore rejects non-string row_union branch values (base defect, D11) | A planner `set_pipeline` whose row_union branch value is an int or null is an argument error in the loop, then `AuditIntegrityError` at persistence. Coalesce and `sources` with such values persist today | `critique0/probe_malformed_e2e.log` (real compose loop, 4 passed), `critique0/probe_malformed_roundtrip.log` (M) |
| A reorder inside a stored dispatch envelope is accepted by construction | Reordering the projected row_union `items` and recomputing the envelope hash is ACCEPTED at base; a connection tamper is REJECTED, for coalesce too. Both restore callers compare against the RFC 8785 generic canonical, which sorts keys | `critique-1/probe_tamper.log` (M) |
| Every content hash moves under T4 | The empty state's `to_dict()` has `sources: {}`, and its content hash moves under a T4-shaped projection. A test builder with `sources: {}` moves its authority canonical too | `critique-1/probe_sources_always.log`, `probe_golden_moves.log`, `probe_listform_pin.log` (M) |
| Epoch docs that pass on base and must move with the constant | `docs/runbooks/staging-session-db-recreation.md` and `CHANGELOG.md:9-10`, both pinned by `tests/unit/docs/test_staging_session_recreation_policy.py` | R, and M in `understand-hash-epochpins.log` |
| Appendix A fork fix | Corrected total = tool rows in all 13 DB copies. The old join gives 26 against 14 in db10. Per-(tool, outcome) totals equal `census.py`: 496 = 496 | `docs_appendix_a_corrected_dbs.py` → `docs-appendix-a-corrected-dbs.log` (M) |
| T12 fixture on the corrected SQL | 8 groups unchanged, both negative controls unchanged. Fork phase: corrected n=2, old n=4, and removing the session condition restores the over-count | `docs_t12_corrected_test.py` → `docs-t12-corrected.log` (M, 1 passed) |
| Wrong-type options signature | `schema_shape`, with a violation whose last loc segment is `options` or `patch` and whose code is `invalid_type`. Structural control: `on_success: 5` gives `('on_success',)`. **It does not identify a string**: an int, a list and `null` in `options` give the same pair, because every JSON-schema `type` failure maps to `invalid_type` (`_dispatch.py:560-561`) | `docs-loc-probe.log`, `codex/recheck_f3.log` (M) |
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
3. Re-run the base-red set from §2 as one house-pytest invocation, using the full node ids from §2 →
   `$L/t0-base-red.log`.
   - Record the 10 failing ids and their assertion messages.
   - Later tasks diff failure sets and messages against this log. Counts alone are not enough.
4. **Golden capture for T3 and T4.** Build three fixed payloads with a small script, `$L/t0_golden.py`:
   - a row_union `set_pipeline` argument payload with map branches, using the singular `source` field and **no
     `sources` key**;
   - a `set_pipeline` argument payload using the singular `source` field, **no `sources` key**, and a list-form
     coalesce;
   - a state dict whose `sources` map has a single entry.

   Do **not** copy `_pipeline` from `test_row_union_authority_hashing.py:44-60` for the first two: it carries
   `"sources": {}`, which T4 projects (D3), so a golden built from it moves at T4 by design (M,
   `$L/critique-1/probe_golden_moves.log`).

   Print `composer_authority_canonical_json` of the first two, and the `composition_content_hash` of a
   `CompositionState` built from the third. Log to `$L/t0-golden.log`.
   - The two argument canonicals become byte pins in T3 (test 9) and T4 (test 3). They prove that the row_union
     branch projection and a `sources`-free payload did not move.
   - The single-entry `sources` hash is the **known positive for D3**. T4 must change it from the T0 value, which
     proves that single-entry maps are projected too. Record both values in the T4 log. The 4 literal pins in
     `test_state_serialisation_contract.py::test_persisted_shape_content_hashes_are_pinned` (`:155-181`) are a second
     known positive for T4 and a known negative for T3 (their coalesce and row_union shapes are list-form).
5. **Scope check for T4, by walking the registry (not by grep).** Write a fresh `$L/t0_sources_scope.py` that walks
   the registered loop tool schemas through `_registered_tool_schema`, the authority the transcript reader used. Its
   `maps.py` probe was not preserved in `$L/probes/`. List every tool whose **top-level** properties include `sources`.
   - Expected: `set_pipeline` only (already measured: 42 tools, one hit, `$L/critique0/sources_scope.log`).
   - Positive control: `set_pipeline` must be found.
   - Negative control: the walk must not list `set_source`.
   - This is a cheap control, not a live guard. Every projection caller is either gated on
     `tool_name == "set_pipeline"` (`audit.py:639-640`, `:730-731`) or given a pipeline or state dict; `tool_batch.py:577`
     is inside the `set_pipeline` required-control finalization (`:565-590`). If the walk finds another tool with a
     top-level `sources` property, **stop and report**: a future caller could then give that field topology meaning.
6. **Attribution of the two non-drift base reds: answered, no run needed** (§2, D10). `test_composer_bedrock` was
   introduced by `e69498f6c`, outside S1; `p5_budget_exhaustion` already fails at the S0 merge `85ebf2739`, so it
   predates S1 (whether it predates S0 is unmeasured; the pre-S0 parent is `780ef0f56`). The implementer may re-confirm with a
   `git archive c4c52c110^1` export (check `elspeth.__file__`), logging to `$L/t0-pre-s1-reds.log`. The comparison
   point is **`c4c52c110^1` (`e69498f6c`)**, never `85ebf2739`. E1 records the answer (T1).
7. **Finalization-path fixture for T2.** Find a compose-loop test that drives `finalization.changed` at
   `tool_batch.py:1373` or `:1477`. Candidates are `tests/unit/web/composer/test_required_control_autowire.py` and
   `tests/integration/web/composer/test_freeform_required_controls.py`. Confirm the fixture reaches that branch by
   instrumenting a copy in `$L`, not by reading the test name.

### T1 — Master plan: record the rulings, correct the evidence, fix Appendix A (docs only)

**File:** `docs/plans/2026-09-23-composer-strict-tool-contracts.md`.

`$L/understand-docs.md` §5 gives the exact old and new text of each edit, E1 to E15. Apply them with the Edit tool,
with the changes below. Re-anchor every OLD string first, because the line numbers are at `c4c52c110`.

| Edit | Where | Change from the draft |
|---|---|---|
| E1 | status line (lines 5-8) | Use **variant B**. S1 is merged as `c4c52c110` and is on `origin/release/0.8.1`. **Replace "one commit per task on `85ebf2739`"**: the S1 branch is rooted on `e69498f6c` (= `c4c52c110^1`, the parent of S1's root commit `34f0fed73`), which is the S0 merge `85ebf2739` plus `e69498f6c` "fix(composer): reject malformed advisor checkpoint envelopes". Its full-suite gate at `d5f8c5aac` recorded `RESULT=FAIL`: 10 failed, 56,862 passed, `frozen=yes`. All 10 failed ids also fail at `c4c52c110`, where they form this lane's base-red set. Six are the epoch-66 doc, website and receipt drift, two are line-pinned `test_session_db_mutation_authority`, and the last two are the `p5_budget_exhaustion` advisor-gate test and `test_composer_bedrock`. State the measured attribution (D10): `test_composer_bedrock` passes at `85ebf2739` and fails at `e69498f6c`, so it came in with `e69498f6c`, not with S1; `p5_budget_exhaustion` already fails at the S0 merge `85ebf2739`, so it predates S1 (do not write that it predates S0: that was not measured); none of the 10 is attributable to S1. Do not write that S1 introduced any of them. Still owed: the CHANGELOG line and dev deployment acceptance. Add: "R1 and R2 were ruled on 2026-09-24 (§1, §6.3): S2 and S3 are withdrawn; S4 and S5 stay optional. The branch-order defects the panel found are handled in `docs/plans/2026-09-24-composer-r1-r2-rulings-and-branch-order-fixes.md`." |
| E2 | new opening paragraph of §1 | as drafted |
| E3 | §1.1 "delivered as" | as drafted |
| E4 | §1.2 bullets | as drafted |
| E5 | §2.3 last row, replaced by three rows | As drafted, with one citation fix: write "`persist_compose_turn` (`sessions/service.py:6650`; the `record_composition_rejection` call is at `:6862`)", not `:6862` alone. This is the correction to "shape and value cannot be told apart", and the citation of `composition_rejection_events` as the unredacted store |
| E6, E7 | S2 and S3 headings, openings and closing line | as drafted (withdrawn, with the reopen trigger) |
| E8 | §5.3 table rows 1 and 3 | as drafted |
| E9 | §5.3 sample-size paragraph, plus a new **§5.4 Option-tool split** | As drafted, with one correction (Codex finding 3). The first class is renamed **"Wrong-type options/patch"**, not "Options sent as a string": the S gate maps every JSON-schema `type` failure to `invalid_type` (`web/composer/tools/_dispatch.py:560-561`), so a string, an int, a list and `null` in `options` all give the same `(('options',), 'invalid_type')` pair (M, `$L/codex/recheck_f3.log`). Say that the `a5d9e5414` options-as-string signature is a subset of this class, and that telling it apart needs a value-free observed-type discriminator (the JSON type that was sent) persisted beside the `(loc, code)` pairs; the pairs alone cannot. The other classes (other-field shape, inside-options content, other rule) are as drafted. Say plainly that the first two classes are not measurable until the `(loc, code)` pairs are persisted (§1.4 item 7). |
| E10 | §6.1 risk 1 | as drafted |
| E11 | §5.2 `wire_decode` row | as drafted |
| E12 | §6.3 items 1-2 | as drafted (R1 ruled no with the trigger; R2 ruled yes with the five conditions) |
| E13 | "S1 as implemented" bullet | **Required, not optional.** Map each pre-merge hash to its hash on `release/0.8.1`, and drop "the merge on John's word" from the owed list. Also correct "based on `85ebf2739`" (master plan line 1008) to "based on `e69498f6c` (`c4c52c110^1`: the S0 merge `85ebf2739` plus the advisor checkpoint envelope fix)". Leave the T11 byte comparison at line 1066 ("a clean export of `85ebf2739`") as recorded: that comparison was made against `85ebf2739`, and the `src` changes in `e69498f6c` are the advisor envelope parsing in `web/composer/service.py` and a `RecursionError` catch in `plugins/infrastructure/clients/json_utils.py`, neither of which declares a tool (`git show --stat e69498f6c`) |
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
   `corrected=14` in db10, and parity `496 = 496`. `$DBS` is a per-session scratch directory and may be gone. If it
   is, step 1 is the sufficient control (the T12 fixture, including its fork phase), and the T1 log records step 2 as
   not re-run.
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
3. **Compose-loop test, finalization path (callers `:1377`/`:1481`).** Use the T0 step 7 fixture, with a coalesce
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
| `pipeline_planner.py:3870` | planner `current_state` | leave. Inert on freeform only; **live on guided-full** (`composer/service.py:4428-4435` passes a non-empty state), deferred under the guided-removal ruling (D2, §1.4 item 5) |
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
- **Guard the discriminant before the lookup, in both `project` and `restore`:** `type(node["node_type"]) is str`
  first, then membership in `_ORDERED_BRANCH_SCHEMAS`. Projection runs on raw planner arguments before any schema
  gate (`audit.py:639-640`), and today's `node["node_type"] != "row_union"` tolerates any type, but a mapping
  lookup with a list or dict raises `TypeError: unhashable type` (M, `$L/codex/recheck_f1_f2.log`). Without the
  guard, a planner `set_pipeline` with `node_type: []` would crash dispatch auditing instead of becoming an
  auditable argument error (Codex finding 1). A non-`str` `node_type` passes through unprojected, as today.
- **`restore_composer_authority_payload`:** for the same node types, a `dict` `branches` must be exactly the
  projection carrying **that node type's** tag. Each item is a 2-list whose first element is a `str` alias, with no
  duplicate alias. **The second element may be any JSON value (D11).** Anything else raises `ValueError`.
  - This removes today's `type(item[1]) is not str` check from the row_union arm too. That is a **base defect fix**:
    today a planner row_union branch value that is an int or null is an argument error in the loop but
    `AuditIntegrityError` at persistence (§2). Integrity does not depend on the check: both restore callers compare
    the restored payload with the stored generic canonical (`pipeline_commit.py:112`, `audit_storage.py:106`).
  - Keep the row_union error strings byte-identical ("row-union authority projection branches are malformed" and
    "...branch item is malformed"). Use "coalesce authority projection ..." for coalesce.
  - This is R2 conditions 1 to 4 on an audit-internal surface. Condition 5 (reject an empty `[]`) is a wire-decode
    rule and does **not** apply here: restore of `items: []` gives `{}` today and must keep doing so, because an
    empty map is a legal projected value (M, `$L/critique-1/probe_listform_pin.log`).
- **Order inside a stored dispatch envelope is bound only by the envelope's own hash, by design.** Both restore
  callers check that the restored payload's RFC 8785 canonical equals the stored generic canonical (key order is
  lost there) and that re-projecting it reproduces the stored authority canonical. A reorder of the projected
  `items` with a recomputed envelope hash therefore passes, at base for row_union too (M,
  `$L/critique-1/probe_tamper.log`). Order is bound where a stored hash is compared with one recomputed from an
  order-preserving copy: `sessions/service.py:2373` (`row.tool_arguments_hash != composer_authority_hash(row.arguments_json)`)
  and `pipeline_commit.py:413`. Test 6 targets those.
- **Docstrings:** update the module docstring, both function docstrings, and `pipeline_proposal.composition_content_hash`.
  Its sentence "Non-row-union content retains the historical preimage" becomes false and must change. The module
  docstring states the order-binding note above.
- Record the pre-existing latent ambiguity in the module docstring; do not fix it. A real branch map whose keys are
  literally `schema` and `items` would look like a projection. It is harmless, because restore runs only on stored
  projections, and a real map with those keys is itself projected before it is stored.
- **Composer coalesce always runs under the default `union_collision_policy=last_wins`** (D4); the tests say so in
  their docstring rather than pretending to vary it.

**New test file:** `tests/unit/web/composer/test_coalesce_authority_hashing.py`. Mirror
`test_row_union_authority_hashing.py`, which came in with `49825dd86`. Copy its small builders locally instead of
importing a private helper across test modules, but **build argument payloads with the singular `source` field and no
`sources` key** (T0 step 4), so that T4 does not move this file's canonicals. `CompositionState`-based tests are
comparative (two states that differ only in order) and are unaffected by T4.

| # | Test | RED expectation |
|---|---|---|
| 1 | Reordering a non-first coalesce map changes `composition_content_hash`, parametrised over the arrival `policy` ∈ {`require_all`, `best_effort`, `first`} crossed with `merge` ∈ {`union`, `nested`, `select`}. `policy` is the arrival policy, not the collision rule (D4); do not put `last_wins`/`first_wins` in it | equal hashes (the gap) |
| 2 | ... changes `pipeline_draft_hash` | equal |
| 3 | ... changes `composer_authority_hash`, `_pipeline_private_arguments_hash`, `_pipeline_audit_payload_hash` and `_composition_state_data_content_hash` | equal |
| 4 | ... changes the `set_pipeline` dispatch binding (`begin_dispatch` authority canonical and hash) without mutating the arguments | equal |
| 5 | Redacted storage keeps the tool's map shape in the generic canonical, and the authority canonical is the projection (`audit_storage.py:250`) | the authority canonical is still a map |
| 6a | **Characterization:** a content tamper on the projected coalesce items (a changed connection, envelope hash recomputed) is rejected by `PipelineDispatchAuditBinding.from_persisted_envelope` | green on arrival: already rejected at base via the generic canonical (M, `probe_tamper.log`) |
| 6b | **Reorder detection where it exists:** a proposal row whose `arguments_json` has its coalesce branches reordered, against the original `tool_arguments_hash`, is rejected with "pipeline proposal row arguments hash mismatch" (`sessions/service.py:2373`); mirror it at `pipeline_commit.py:413` ("authoritative pipeline arguments do not match the proposal row"). The sessions-service proposal fixtures live in `tests/unit/web/sessions/test_composer_proposals.py` | accepted (the two hashes are equal at base) |
| 7 | Restore rejects: a plain dict on coalesce, the row_union tag on a coalesce node, a duplicate alias, a non-`str` alias, a non-list item and a 3-item entry | the plain dict and the row_union tag pass through a coalesce node unchanged |
| 7c | **Characterization:** restore rejects the coalesce tag on a row_union node | green on arrival: the row_union arm already rejects any foreign tag (`authority_hashing.py:74`) |
| 8 | **Characterization:** list-form coalesce in a `sources`-free payload gives `arguments_canonical == authority_arguments_canonical`, byte for byte | green on arrival |
| 9 | **Characterization:** the row_union argument canonical equals the T0 golden byte for byte | green on arrival |
| 10 | **Characterization of R2 conditions 1-2:** `restore(project(p)) == p`, with coalesce key order preserved (compare `list(...)`, not `==`) | green on arrival |
| 11 | The runtime preflight cache key (`RuntimePreflightKey.state_content_hash`) differs for two states that differ only in coalesce order | equal (I, confirm in RED) |
| 12 | **Base defect (D11):** restore accepts a non-`str` branch value (int, null, object) for row_union and for coalesce, and `restore(project(p)) == p` for it | row_union raises `ValueError` |
| 13 | **Base defect (D11), end to end:** an argument-error `set_pipeline` whose row_union branch value is `1`, and one whose coalesce branch value is `null`, both go through `redacted_tool_invocation_content_and_envelope` without error, and `from_persisted_envelope` round-trips them. Lift `$L/critique0/test_probe_malformed_e2e.py` (real compose loop, run with `-p tests.unit.web.composer.conftest`) | the row_union case raises `AuditIntegrityError` (the coalesce case is green at base and must stay green) |
| 14 | **Characterization, malformed discriminant (Codex finding 1):** a `set_pipeline` whose node has `node_type` `[]`, `{}` or `1` and a map `branches` goes through `project_composer_authority_payload`, `restore_composer_authority_payload(project(p))` and `begin_dispatch` without raising, and the branches pass through unprojected; parametrise over the three values. Add one end-to-end case through the compose loop (the test 13 harness) asserting an argument error, not a crash | green on arrival (today's `!=` tolerates any type); the mutation below proves it guards the new lookup |

**Mutation controls:**
- Remove `"coalesce"` from the map. Tests 1 to 5, 6b, the RED sub-cases of 7, and 11 must go RED. (6a and 7c stay
  green by construction; say so in the log.)
- Change one character of the row_union tag. Test 9 must go RED.
- In restore, drop the duplicate-alias check. The duplicate case of test 7 must go RED.
- In restore, put back `type(item[1]) is not str`. Tests 12 and 13 must go RED.
- In `project`, iterate over `sorted(branches.items())`. Tests 1 to 4, 6b and 10 must go RED.
- Drop the `type(node["node_type"]) is str` guard, in `project` and then separately in `restore`. Test 14 must go RED
  with `TypeError: unhashable type` each time.

**Known negative:** `test_state_serialisation_contract.py::test_persisted_shape_content_hashes_are_pinned` pins 4
content hashes whose coalesce and row_union shapes are list-form (`:120-133`, `:155-181`). T3 must **not** move them.

**Re-run** (they build coalesce nodes and touch hashes):
- `test_row_union_authority_hashing.py`, `test_sparse_argument_presence.py`, `test_owned_composition_state_authority.py`,
  `test_pipeline_commit_operation_authority.py`, `test_compose_loop_persistence.py`, `test_state.py`,
  `test_state_serialisation_contract.py` (all 4 pins unchanged) and `test_pipeline_planner.py`;
- `tests/unit/web/sessions/test_tool_invocation_redaction.py`, `test_composer_proposals.py` and `test_routes.py`;
- `tests/integration/web/composer/test_pipeline_proposal_lifecycle.py`;
- the `guided/` set in `understand-hash.md` §7.

Any literal hash pin that moves must be a hash of a payload containing a mapping-form coalesce. Regenerate it with
its producer's documented method, and list it in the commit body. A moved pin whose payload has no mapping-form
coalesce is a **stop condition**.

**Gates:** the every-task set.

**Commit:** `fix(composer): bind coalesce mapping-branch order in the authority projection (Defect B)`. The body
states that restore now accepts any JSON branch value for row_union too (D11), which fixes the base
`AuditIntegrityError` on a persisted argument error, and that no preimage moves for list-form or row_union payloads.

### T4 — Defect B, part 2: bind multi-source `sources` order

**Production change:** `authority_hashing.py`.
- **`project`:** if the top-level `sources` is a `dict`, replace it with
  `{"schema": "composer.ordered-sources.v1", "items": [[name, spec], ...]}`. This applies to every dict, including
  `{}` and single-entry maps (D3). A top-level `source` is untouched.
- **`restore`:** a top-level `dict` `sources` must be exactly that projection. Each item is a 2-list whose first
  element is a `str` name, with no duplicate name. **The spec position accepts any JSON value (D11)**: a planner
  `sources: {"main": null}` or `{"main": "csv"}` is an argument error in the loop, and it must still persist (M, both
  persist today: `$L/critique0/probe_malformed_e2e.log` "sources_non_dict_spec ... OK",
  `probe_malformed_roundtrip.log`). Anything else raises `ValueError`. `items: []` restores to `{}`.
- The T0 step 5 scope check must have passed. **Every** caller of the four authority functions, from
  `grep -rn "project_composer_authority_payload\|restore_composer_authority_payload\|composer_authority_hash\|composer_authority_canonical_json" src/elspeth`
  (imports excluded; positive control: the grep must hit `audit.py:639`), passes a flat pipeline or state dict or is
  gated on `set_pipeline`: `audit.py:639/640/730/731`, `audit_storage.py:103/108/250`, `tool_batch.py:577`,
  `pipeline_commit.py:109/114/413`, `pipeline_planner.py:3427/3454/3533`, `pipeline_custody.py:263`,
  `pipeline_proposal.py:610/797`, `redaction.py:2499/2515`, and `sessions/service.py:575/593/646/682/2373/7735/11882/12571`
  (M, 40 hits outside `authority_hashing.py` at `41125ac74`: 13 import lines and the 27 call sites listed).

**New test file:** `tests/unit/web/composer/test_sources_authority_hashing.py`.

1. Reordering a two-entry `sources` map changes `composition_content_hash`, the draft hash, the private-arguments hash
   and the dispatch binding. RED expectation: equal.
2. Restore rejects: a plain dict, a wrong tag (including either branch tag), a duplicate name, a non-`str` name, a
   non-list item and a 3-item entry. RED expectation: the plain dict passes.
3. **Characterization:** a `sources`-free payload using the singular `source` has a canonical byte-equal to the T0
   golden.
4. **Characterization:** `restore(project(p)) == p`, with order preserved, including `{}` and a single entry.
5. **Characterization, end to end (D11):** an argument-error `set_pipeline` with `sources: {"main": null}` goes through
   `redacted_tool_invocation_content_and_envelope` and `from_persisted_envelope` without error. It is green at base
   and must stay green; the mutation below proves it guards something.
6. **Known positive for D3:** the single-entry `sources` content hash from T0 step 4 differs from its T0 value. This is
   an assertion recorded in the T4 log against the T0 literal, not a durable test; the re-pinned
   `test_state_serialisation_contract.py` literals are the durable form.

**Mutation controls:**
- Remove the `sources` arm. Tests 1, 2 and 6 must go RED.
- Project with `sorted(...)`. Test 1 must go RED.
- In restore, require a `dict` spec. Test 5 must go RED.

**Expected changes.** Every `CompositionState` has a `sources` dict (D3), so **every** `composition_content_hash`
moves, and so does every argument canonical, draft hash and dispatch binding whose payload carries a `sources` map
(including `sources: {}`).

- **`tests/unit/web/composer/test_state_serialisation_contract.py::test_persisted_shape_content_hashes_are_pinned`**:
  all 4 literals (`:159`, `:164`, `:169`, `:174`) move. Re-pin them in this commit. The test's own failure message
  says "Re-pin only after deciding what happens to already-persisted states"; the answer is T6 (epoch cut, stores
  recreated), and the commit body says so.
- **`tests/unit/web/composer/test_row_union_authority_hashing.py::test_set_pipeline_list_branches_round_trips_through_generic_and_authority_audit_pairs`**
  (`:316`) asserts `persisted["arguments_canonical"] == persisted["authority_arguments_canonical"]` over a payload
  with `sources: {}`. It goes red at T4 (M, `$L/critique-1/probe_listform_pin.log`). Rewrite it, do not weaken it:
  assert that the list-form `branches` are identical in both canonicals, that the authority `sources` is the
  `composer.ordered-sources.v1` projection, and that
  `canonical_json(restore_composer_authority_payload(json.loads(authority_canonical))) == arguments_canonical`.
  That is the only test in `tests/` asserting generic == authority canonical (R, grep for both orders of the
  comparison; `test_owned_composition_state_authority.py:228` compares with a projection it computes, so it follows
  the change without edits).
- The other tests in `test_row_union_authority_hashing.py` build `sources: {}` too; they compare two reorders, or
  compute their expectation through the projection, and should stay green (I, from reading `:138-310`). Any that
  does not is a stop condition.

- **Frontend fixture:** `src/elspeth/web/frontend/src/api/__fixtures__/gateProposalProjection.json:9` pins a
  `draft_hash`. The pipeline it is built from uses `sources: {"primary": ...}`
  (`tests/unit/web/composer/guided/test_gate_projection_fixture.py:90-97`), so the fixture **will** regenerate (I).
  Confirm that it goes red, then regenerate it by the method that test documents (`json.dumps(..., indent=2,
  sort_keys=True)`, per its docstring at `:17-19`).
- Then run the frontend test that reads the fixture. The worktree has no `node_modules` (the main checkout does), so
  install first: `cd $W/src/elspeth/web/frontend && npm ci > $L/t4-npm-ci.log 2>&1; echo exit=$?`, then
  `npx vitest run src/api/guidedDecoder.gate.test.ts > $L/t4-vitest.log 2>&1; echo exit=$?`. `node_modules` is
  excluded through `.git/info/exclude`, so it cannot be staged. Do not symlink the main checkout's `node_modules`:
  vitest writes its cache under it. If `npm ci` cannot run (no network), record that and stop the frontend step; the
  lead runs it. No frontend source changes, so no frontend rebuild or deploy step is needed.
- Every other moved literal must trace to a payload with a `sources` map or a `CompositionState`. Regenerate it by its
  producer's method and list it in the commit body.
- `test_end_advisor_gate_reaches_prompt_template_pipeline_p5_budget_exhaustion` is already red on its `for_graph`
  hash. Diff its message against `$L/t0-base-red.log`. T5 will move it again.

**Re-run:** the T3 set, plus `test_gate_projection_fixture.py`.

**Gates:** the every-task set.

**Commit:** `fix(composer): bind multi-source ingest order in the authority projection (Defect B)`. The body lists
every re-pinned literal and rewritten assertion, each traced to a payload or state that carries `sources`.

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
The p5 test is **not** a literal pin: it asserts
`result.advisor_gate_decision == AdvisorGatePassed(completion_gate_fingerprint(state))`
(`test_compose_loop_interpretation_review_dispatch.py:3773`), comparing the pre-turn state's fingerprint with the
decision taken after the turn's `set_metadata` renamed the pipeline (the fingerprint binds metadata since
`d7541609c`). T5 cannot re-pin it. It stays red for its base reason; record its new hash pair against the T0 log.

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

   `test_staging_session_recreation_policy.py:31-35` pins each of these except line 7 ("from 53 to 66"), which no
   test pins; change it anyway.
10. `CHANGELOG.md:9-10`, in the 0.8.1 section:
    - `` `SESSION_SCHEMA_EPOCH` advances from 53\nto 66 `` → `to 67`. **The line break between "53" and "to" is part
      of the asserted string (`test_replica_schema_cutover_belongs_to_0_8_1`).**
    - Add "ordered coalesce branches and source order in composer authority hashes" to the epoch's reason list.
    - Add one "Fixed" line for Defect A (the transcript keeps planner key order) and one for Defect B.
    - **Also `CHANGELOG.md:55-59`**, two present-tense statements that no test pins (a grep over `tests/` for
      `session 66|below epoch|including epoch` finds nothing), so nothing turns red if they go stale:
      - `:55-56` "Session databases below epoch 66 (including epoch 65)" → "below epoch 67 (including epoch 66)";
      - `:59` "(session 66, Landscape 43)" → "(session 67, Landscape 43)".
    - The target version is 0.8.1. The base is `release/0.8.1`, and the test pins the clause to the 0.8.1 section.

**Do not touch** the 6 base-red doc, website and receipt pins (§2). After T6 they fail against 67 instead of 66. That
is the same ids, with a new expected value. **This holds on the branch only.** `release/0.8.1` has since moved and
made those pins green at 66; at the merge they become this branch's reds and must be fixed in the merge commit
(§6.1).

**Tests:**
- the 6 pin files from items 3 to 8;
- `tests/unit/docs/test_staging_session_recreation_policy.py`, which must pass;
- the base-red set, where the failure-set diff against `$L/t0-base-red.log` must show the same 10 ids;
- `tests/unit/architecture/test_session_db_mutation_authority.py`. It is line-pinned and already red. The comment in
  `schema.py` shifts lines, so diff its messages and do not re-pin it;
- the stale-epoch refusals, which prove the operational claim that an epoch-66 store is refused at 67:
  `tests/unit/web/sessions/test_engine.py::test_initialize_session_schema_rejects_stale_user_version` (sets
  `PRAGMA user_version = SESSION_SCHEMA_EPOCH - 1`, `:218`) and
  `tests/unit/web/sessions/test_schema.py::test_previous_epoch_rejection_does_not_rewrite_store` (`:519`). Both must
  pass. Their PostgreSQL siblings are testcontainer ids and run in the lead's §6 stage.

There is no RED/GREEN cycle here: the pins are the tests. As the mutation control, set only `models.py` to 67. Every
session-DB test must then fail at `schema.py:534` ("coordination schema epoch mismatch"), which proves item 2 is
load-bearing. Revert, then apply both.

**Gates:** the every-task set.

**Commit:** `chore(sessions): session schema epoch 67 for ordered coalesce and source authority hashes`. The body
names the deploy obligation from §5.2.

---

## 4. Invariants to prove (in the handover)

1. **The transcript keeps the model's order.** T2 tests 1 to 4, with the mutation log.
2. **The row_union branch projection did not move, and a `sources`-free payload using the singular `source` did not
   move.** The T3 test 9 and T4 test 3 goldens, captured at base in T0 from payloads with no `sources` key. (A payload
   that carries `sources`, even `{}`, moves at T4 by design, D3.)
3. **Each order-semantic topology map the composer owns (mapping-form coalesce and row_union `branches`, top-level
   `sources`) is bound by every stored hash that re-derives it.** Plugin-owned option maps are not covered (D13, §1.4
   item 11). This covers content, draft,
   private-arguments, audit-payload, state-data, dispatch binding and fingerprint (T3, T4, T5).
4. **Restore is strict about structure and lossless about values.** The tag for one kind is not accepted for
   another, duplicate keys are rejected, and a non-projection map is rejected (T3 tests 7 and 7c, T4 test 2). Any JSON
   value survives the round trip, so a planner argument error persists instead of raising `AuditIntegrityError` (T3
   tests 12 and 13, T4 test 5). A non-`str` `node_type` passes through unprojected instead of crashing (T3 test 14).
5. **The trust-tier corpus is unchanged** (G-tier diff), except for lines that are accounted for.
6. **No tool, schema or wire byte changed.** G-wire and G-skill stay green, and no test file of tool declarations is
   edited.
7. **Order inside a stored dispatch envelope is bound only by the envelope's own hash** (by design, T3). Reorder
   detection happens where a stored hash meets an order-preserving copy (T3 test 6b).

## 5. Epoch and deploy consequences (plain statement)

### 5.1 Why an epoch is required

These stored hashes are recomputed from stored payloads and compared, and a mismatch fails hard
(`AuditIntegrityError`):
- the `PipelineProposal.draft_hash` in `__post_init__`;
- the private-arguments and audit-payload bindings (`service.py:2340/2343`);
- `tool_arguments_hash` (`service.py:2373`, `pipeline_commit.py:413`);
- the dispatch-binding restore and its byte-equality check (`pipeline_commit.py:109-115`, read at `service.py:895` and
  `:13427`);
- `semantic_redacted_pipeline_arguments_hash` (`redaction.py:2513`);
- **the stored `PresentBase.composition_content_hash`** of a proposal, re-derived from the base state record and
  compared at `sessions/service.py:2620`, `:2622`, `:2788` and `:2908` (`AuditIntegrityError`), at `:3329` and
  `:8071` (`StaleComposeStateError`), and at `pipeline_commit.py:405` (`BASE_CONFLICT`);
- the executor content hash persisted in the dispatch result (`pipeline_content_hash`, `audit_storage.py:236`),
  recovered through `PipelineDispatchRecovery` (`service.py:911`) and compared with the recomputed state hash at
  `service.py:7881` ("pipeline candidate/executor/state content hash mismatch");
- the `committed_state_content_hash` in the `proposal.accepted` terminal event (`_pipeline_accepted_payload`,
  `sessions/service.py:756`, persisted at `:8086-8100`). On an exact retry of a committed proposal it is recomputed from
  the committed state row and the whole payload is compared (`:967-976`, "committed pipeline proposal exact retry
  binding mismatch", `AuditIntegrityError`). T4 moves its preimage; the T6 epoch covers it (Codex finding 4).

After T4, **every** stored row that carries a composition content hash fails one of these, whatever its pipeline
shape, because every state's `to_dict()` has a `sources` map (D3; M, `$L/critique-1/probe_sources_always.log`). So
does every row whose arguments contain a mapping-form coalesce (T3) or a `sources` map (T4). An operator must not
reason "no coalesce maps in our store, so the recreate can wait": every pending proposal with a present base becomes
unreadable. The advisor `for_graph` fingerprint (T5) also moves; a stored one no longer matches, and the fact is
downgraded to pending (a soft failure). The no-tech-debt rule forbids a legacy path, so the store is recreated.

**Why the row_union precedent took no epoch and this change does.** `49825dd86` introduced the row_union projection
and moved `_DRAFT_HASH_SCHEMA` from v2 to v3, and `SESSION_SCHEMA_EPOCH` was 40 both before and after it (M:
`git show 49825dd86^:src/elspeth/web/sessions/models.py` and `git show 49825dd86:...` both give 40). The commit
message records no reason. Whether that was right then is not this plan's question. Today, §5.1 names stored hashes
that are re-derived and fail hard, and John's rule forbids a legacy path, so the epoch is taken.

Consumers that are **not** reasons for the epoch:
- the frontend, where the hashes are opaque. Its content-equality check mirrors the hashed field set but is
  order-blind, so it diverges from the backend on a reorder (§1.4 item 10); that is a UI freshness issue, not a
  stored-hash failure;
- Landscape, which never receives them. There is no Landscape epoch change;
- the process-local preflight cache;
- custody `creating_arguments_hash`, which is compared row against snapshot and never re-derived.

### 5.2 Deploy obligation (for the lead's handover)

- **Session DB rename on deploy.** The epoch-66 `sessions.db` must be moved aside, and the service starts on a fresh
  epoch-67 store. The served config is `deploy/elspeth-web.env`. This applies to every store, not only ones holding
  coalesce maps or multi-source pipelines (§5.1).
- **Then run `elspeth composer users bootstrap-admin local <user> --note ...`** for every local account. Identities
  live in the session DB, so a rename leaves local accounts pending (401).
- No Landscape epoch change, and no frontend rebuild (T4 changes a test fixture only).
- **Merge-conflict risk:** at the start of this session, the main checkout had an uncommitted `CHANGELOG.md` edit made
  by another session. The lead's merge in the main checkout must reconcile it. The tip has also moved Landscape to
  epoch 45, which conflicts with T6 in `CHANGELOG.md` and the staging runbook (§6.1).
- If another lane lands an epoch bump on `release/0.8.1` first, T6 is redone at the next free number, with its history
  line folded in. It is never merged over.

## 6. Full-suite gate and merge procedure (the lead)

1. Check host capacity: no other suite running, and acceptable load. Then run
   `cd $W && scripts/full-suite-gate.sh --execute --detach --stages ruff,mypy,contracts,lints,pytest,testcontainer`.
   - Read the `stages :` launch line; the default is only ruff and pytest.
   - Wait on the `.done` artefact, never with a `pgrep -f` loop.
   - Read `summary.txt`, and require `frozen=yes`.
   - **Testcontainer is included**, because T6 changes the session schema epoch. The stale-epoch refusals it must
     show green are
     `tests/testcontainer/web/test_external_deployment_postgres.py::test_validate_only_startup_rejects_missing_and_stale_schema_without_leaking_credentials`
     and the `session`/`schema_epoch` case of
     `tests/testcontainer/web/test_schema_probe_postgres.py::test_postgres_schema_identity_drift_is_stale_and_not_repaired`.
2. **Attribute every red.** Re-run each failing id with `-n 0`.
   - On the branch as it stands (base `c4c52c110`): diff the failure set against the 10-id base-red set (§2) and its
     messages (`$L/t0-base-red.log`). The p5 test and the 6 doc pins are expected, with new hash or epoch values.
   - **On the merged tree, that rule no longer applies (§6.1).** At the tip `ffd704d1a` all 10 of those ids pass, so
     a red among them after the merge is this branch's, not a base red. With §6.1 applied, none is expected.
   - Anything else belongs to this branch. So does a known flaky family whose failures differ from a base run.
3. `lints`: diff as a multiset against `lints-base.norm`, and list every addition for the operator's sign-bundle.
   Stage nothing.
4. **Handover, not merge.** Stop at ready-for-merge and report:
   - the commit list;
   - the §4 evidence;
   - the pin moves (T3, T4, T5, T6);
   - the §5.2 deploy obligation;
   - the §1.4 tickets owed. Item 1, the execution envelope, is the one to raise first; item 11 (plugin option maps
     found order-semantic and not bound, D13) needs John's scope ruling, because the brief asked for every
     order-semantic map found;
   - the attribution of the two non-drift base reds (D10): `test_composer_bedrock` came in with `e69498f6c`, not S1.

   Merge only on John's word, after running `git merge-tree` against the current `release/0.8.1` tip and
   `scripts/branch-safety-check.sh --intent merge`. Push only when asked.

### 6.1 Merging into the moved tip (added after the branch review)

The branch review (`$L/review.md`, F1 and F2) found that `release/0.8.1` moved from `c4c52c110` to `ffd704d1a` while
the branch was built. The tip is still at session epoch 66 (`git show release/0.8.1:src/elspeth/web/sessions/models.py`
gives `SESSION_SCHEMA_EPOCH = 66`), so T6's number stands. But the tip moved Landscape from 43 to 45, brought the
epoch docs up to session 66, and re-pinned the architecture line pins. As a result (M):

- At the tip, all 10 ids of the §2 base-red set pass, including p5 and bedrock.
- `git merge-tree --write-tree release/0.8.1 HEAD` conflicts in `CHANGELOG.md` and
  `docs/runbooks/staging-session-db-recreation.md`.
- After the conflicts are resolved, 6 doc pins and 2 line pins are red on the merged tree, and all of them are this
  branch's: the docs name session 66 and the `schema.py` comment in T6 shifts the pinned lines by one
  (`$L/fix/f1-merged-before.log`: 8 failed, 323 passed).

The implementer may not merge or rebase, and the tip keeps moving, so this work goes into the merge commit. It is part
of T6 in substance (§1.5): the merge commit must carry all of it, and no merge without it may be deployed.

**Conflicts (additive).** Every present-tense literal takes both new numbers, session 67 and Landscape 45:
- `CHANGELOG.md`: keep the branch's "Session epoch 67 binds the order ..." paragraph, followed by the tip's
  "Landscape `SQLITE_SCHEMA_EPOCH` advances from 38 to 45 ..." line. Then "Session databases below epoch 67 (including
  epoch 66) and Landscape databases below epoch 45" and "(session 67, Landscape 45)". Keep the
  `` `SESSION_SCHEMA_EPOCH` advances from 53\nto 67 `` line break that `test_replica_schema_cutover_belongs_to_0_8_1`
  asserts.
- Staging runbook: the tip's heading with the branch's number, "... prompt provenance and call mode audit (session
  epoch 67 and Landscape epoch 45)"; "from 53 to 67 and Landscape `SQLITE_SCHEMA_EPOCH` from 38 to 45";
  "session-epoch-67/Landscape-epoch-45 record"; and the `PRAGMA user_version` expectations 67 and 45.

**Epoch sweep (the tip's session-66 literals that the constant now makes stale):**

| File | Literal at the tip | Becomes | Pinned by |
|---|---|---|---|
| `README.md` (0.8.1 section) | "session epoch 53\nto 66 and Landscape epoch 38 to 45" | `to 67` | `test_readme_operational_cutover_states_the_live_schema_epochs` |
| `website/get-started.html` | "changes 53 → 66 and Landscape" | `53 → 67` | `test_get_started_has_runnable_cli_and_complete_composer_paths` |
| `docs/guides/sharing-pipelines.md` | `` `SESSION_SCHEMA_EPOCH=66` `` | `=67` | `test_operator_schema_version_examples_match_live_constants` |
| `docs/runbooks/azure-container-apps-deployment.md` | "The epoch-66 image (session epoch 66, ...", "at session epoch 66" (2), `"candidate": {"session_epoch": 66, ...` | 67 in each | `test_every_epoch_literal_matches_the_live_constants`, `test_compatibility_record_is_byte_bound_to_the_live_derivation` |
| `docs/runbooks/azure-container-apps-cold-install.md` | "session epoch 66 and Landscape epoch 45 initialized;" | 67 | `test_every_epoch_literal_matches_the_live_constants` |
| `docs/runbooks/aws-ecs-deployment.md` (scenario-B record) | `"candidate": {"session_epoch": 66, ...`, `"session_epoch_35_to_66_landscape_epoch_29_to_45_..."`, "session epoch 66, Landscape epoch 45 and `run_web_plugin_policy` presence" | 67 in each | `test_scenario_b_runbook_record_matches_live_release_derivation` |

**Line re-pin** (re-pin; do not reshape the comment to avoid the churn). In
`tests/unit/architecture/test_session_db_mutation_authority.py`, every `WriterIdentity` on
`src/elspeth/web/sessions/schema.py` moves down by one line: `stamp_sentinels` 274 → 275 (two places), `_stamp_on`
`stamp_sqlite_sentinel` 292 → 293 and 293 → 294, `_stamp_on` `insert` 296 → 297, `assert_sentinels` 309 → 310 (two
places) and `validate_required_triggers` 422 → 423 (two places). That is 9 literals, and they make
`test_live_connection_domain_classification_is_exact` and
`test_session_schema_authority_is_exact_contained_and_bidirectional` green.

**Measured on the resolved merge tree** (a `git archive` export of `git merge-tree --write-tree release/0.8.1
faf2395d7`, with the resolution above applied): the pins above, `tests/unit/docs`, `tests/unit/website`, the ACA
runbook contract, the p5 and bedrock ids, `tests/unit/web/sessions/test_schema.py` and `test_engine.py`, and the
branch's order and fingerprint tests give 737 passed and 1 failed (`$L/fix/f1-merged-after.log`). The one failure,
`test_adr_public_integrity.py::test_audited_adr_commit_citations_resolve_to_reachable_history`, is a property of the
export, which has no git history; it passes in the worktree (`$L/fix/adr-worktree.log`). The resolution is saved as
`$L/fix/merge-resolution-full.patch` (against the conflicted merge tree), `$L/fix/merge-epoch67-sweep.patch` (the
non-conflict edits only) and the two resolved files under `$L/fix/merge-resolution/`. This is a focused set, not the
full suite: §6 step 1 still runs on the merged tree, with the testcontainer stage. If the tip moves again before the
merge, repeat the `merge-tree` and the grep for `session epoch 66`, `"session_epoch": 66` and `53 → 66` rather than
trusting the table.

## 7. Risks and stop conditions

### 7.1 Risks

- **Many hash pins move in T4.** `sources` is always a dict on a state, so every content hash moves, including an
  empty state's and a singular-`source` state's (D3). Every argument canonical over a payload with `sources`, even
  `{}`, moves too, and one existing equality assertion is rewritten (T4). This is honest churn under an epoch cut,
  and John's standing rule is to take the churn rather than reshape behaviour to avoid it. The control is
  attribution: every moved literal must trace to a payload or state that holds `sources`.
- **T3 widens what restore accepts in the value position (D11).** This is not dual acceptance: there is still one
  stored form per field, and the generic-canonical comparison still rejects any value tamper (T3 test 6a). It removes
  a check that turned planner argument errors into persistence crashes.
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
- T3 moves any of the 4 list-form pins in `test_state_serialisation_contract.py`.
- The T0 goldens (built without `sources`) move in T3 or T4.
- The G-tier diff has an unaccounted `>` line.
- `release/0.8.1` is no longer at epoch 66 when T6 starts.
- Any change would need a suppression, a compatibility shim, dual acceptance or a legacy hash path.

## 8. Plan review

Two critiques were run against the first draft of this plan at `41125ac74` (base `c4c52c110`): a reality check of
every citation, premise and count (critique 0), and a tests-and-risk review (critique 1). Both were read-only: they
re-ran the plan's probes and wrote their own, but neither executed T0, so the implementer's T0 is the first execution
evidence for this plan. Each finding was re-checked against the code before it was applied. Of 19 findings, 3
repeated another critic's finding (the pre-S1 base, the `tool_batch.py:577` premise and the stale CHANGELOG lines),
leaving 16 distinct ones. All 16 held up and were applied; none was rejected. Where a finding offered alternatives,
the plan picked one and says why: the row_union restore defect is fixed in T3 rather than ticketed (D11); `{}` stays
projected, because excluding it would need dual acceptance in restore (D3); and the frontend order-blind equality is
recorded as a ticket, not added as a task (D12). A fourth overlap, critique 0's answer to the old T0 step 7 (the p5
test compares computed values), matched a note in critique 1's "checked and found sound" list. The dispositions,
with the evidence used for each, are in the lane (`$L/revise-dispositions.md`).

What changed:

- **A false attribution that would have reached John.** The tree before S1 is `c4c52c110^1` = `e69498f6c`, not the S0
  merge `85ebf2739`. `test_composer_bedrock` passes at `85ebf2739` and fails at `e69498f6c`, so it came in with the
  advisor checkpoint envelope fix, outside S1; the old T0 step 6 rule would have written "S1 introduced it". The
  revision re-ran it on fresh `git archive` exports (M, `$L/rev-*.log`). D10, §2, T0 step 6 and E1/E13 now state the
  measured answer and correct the master plan's "on `85ebf2739`".
- **A restore rule that would have spread a persistence crash.** A value-typed restore turns a planner argument error
  into `AuditIntegrityError` at persistence; row_union already does this at base (measured through the real compose
  loop). Restore now checks only the projection's own structure (D11), T3 fixes the row_union arm, and T3 tests 12-13
  and T4 test 5 pin the round trip.
- **Tests that could not pass or did not test what they named.** T3 test 6 could never go green: a reorder inside a
  stored envelope is accepted by construction, and the tamper half is already rejected at base. It is split into a
  characterization (6a) and a real RED at the stored-hash comparisons (6b). T3 test 1 varied the arrival `policy` as if
  it were the collision rule; it now crosses real arrival policies with `merge`, and D4 records that composer coalesce
  always runs under `union_collision_policy=last_wins`. Test 7's foreign-tag case on a row_union node is marked as a
  characterization, and the mutation claims now name only tests that can go red.
- **Goldens that D3 would have moved.** The T0 goldens and T3 tests 8-9 now use payloads with no `sources` key. T4
  lists the expected changes: the 4 content-hash pins in `test_state_serialisation_contract.py` (also a known negative
  for T3) and the rewritten equality assertion in `test_row_union_authority_hashing.py:316`.
- **The epoch's blast radius.** §5.1 now names the `PresentBase.composition_content_hash` and executor-hash
  re-verification sites and says plainly that after T4 every stored proposal with a present base is unreadable,
  whatever its shape. It also records that the row_union precedent took no epoch, without guessing why.
- **Smaller corrections.** T0 step 5's stated reason and T4's caller list (27 call sites, from a named grep); D2 and
  §1.4 item 5 (guided-full still reaches `pipeline_planner.py:3870` with a non-empty state); the CHANGELOG `:55-59`
  present-tense epoch lines in T6; the stale-epoch refusal tests in T6 and §6; `npm ci` before the T4 vitest step;
  the frontend equality divergence in §1.4 item 10 and §5.1; the old T0 step 7 answered in T5; and six citations
  (`envelope.py:599`/`:559`, `service.py:6650`/`:6862`, `pending_interpretation.py:1561`, full node ids for the base
  reds, the S1 gate record path, and the runbook line 7 that no test pins).

### Codex plan review

After the two critiques, Codex (`gpt-6-astra`, medium reasoning, read-only sandbox) reviewed the revised plan at
`3ba282476`. Its verdict was **"yes with fixes"**. The report is in the lane at `$L/codex/plan-review.md` and the
brief at `$L/codex/brief.md`. Codex confirmed these parts as sound: the coalesce and advisor gaps (with list-form
branches as the negative control); that both restore callers compare the generic canonical and re-project, so D11
keeps integrity; that a real `{schema, items}` map is itself projected, so there is no collision; that T4 moves every
content hash and the epoch is warranted; D6; the T1 instruments comparing two different texts; and that nothing
authors pipeline structure or needs compatibility acceptance. Each of its six findings was re-checked against the
code before it was applied. All six held. Finding 2 is applied as a recorded scope decision (D13), which is one of
the two fixes Codex offered. The dispositions, with the evidence for each, are in `$L/codex/dispositions.md`.

| # | Severity | Finding | Checked against | Applied as |
|---|---|---|---|---|
| 1 | Major | T3: membership in `_ORDERED_BRANCH_SCHEMAS` raises `TypeError: unhashable type` on a planner `node_type` of `[]` or `{}`, which today's `!= "row_union"` tolerates. Projection runs before the schema gate | M, `$L/codex/recheck_f1_f2.log`: base `project` accepts both values, and the mapping lookup raises for both. R, `authority_hashing.py:39`, `:67` | T3 guards `type(node["node_type"]) is str` before the lookup in `project` and `restore`; new T3 test 14 (characterization, including one compose-loop case) with a mutation control for each function; §4 invariant 4 |
| 2 | Major | §4 invariant 3 claimed every order-semantic map is bound, but `options.queries` (LLM multi-query) and `field_mapper` `options.mapping` are order-semantic and stay unbound | M, `$L/codex/recheck_f1_f2.log`: both reorders give equal authority hashes, and a row_union reorder does not (known positive). R, `multi_query.py:306-307`, `transform.py:915-945`, `field_mapper.py:678-730`, `csv_sink.py:534-536` | D13 and §1.4 item 11: not projected (it needs a plugin-declared ordered-map contract, and the projection deliberately never gives plugin-owned nested maps topology meaning); §1.1, §4 invariant 3 narrowed to the composer-owned topology maps; §6 handover raises it for John's scope ruling |
| 3 | Minor | T1 E9: `(loc, code)` cannot identify options sent as a string, because every JSON-schema `type` failure is `invalid_type` | M, `$L/codex/recheck_f3.log`: a string, `1`, `[]` and `null` in `set_source` options all give `[{'loc': ['options'], 'type': 'invalid_type'}]`; `{}` is admitted. R, `_dispatch.py:560-561` | E9 renames the class "Wrong-type options/patch", with the string signature as a subset that needs the sent JSON type as an extra value-free discriminator; §2 row and §1.4 item 7 |
| 4 | Minor | §5.1 omitted the `proposal.accepted` event's `committed_state_content_hash`, recomputed and compared on an exact retry | R, `sessions/service.py:756`, `:967-976` ("committed pipeline proposal exact retry binding mismatch"), `:8086-8100` | New §5.1 bullet. The T6 epoch already covers it; no change to the epoch decision |
| 5 | Minor | D10, T0 step 6, E1: a failure at the S0 merge `85ebf2739` shows the p5 red predates S1, not S0 | M, `git show -s --format='%h %p %s' 85ebf2739` → parents `780ef0f56` `35d6bad86`, "Merge strict tool-contract S0 ..." | D10, T0 step 6 and E1 now say "fails at the S0 merge, so it predates S1", and that pre-S0 was not measured |
| 6 | Minor | §3.2 still called `pipeline_planner.py:3870` "Inert", against D2 and §1.4 item 5 | R, this plan's D2; `composer/service.py:4428-4435` | The §3.2 row says "inert on freeform only; live on guided-full, deferred under the guided-removal ruling" |
