# Spike S3: multi-file custody on the planner (CDA-13, R4(a), F1)

Spike date 2026-11-13. Tree `c987b2b2`. Prototype worktree
`.claude/worktrees/S3-multifile-custody` (branch `spike/S3-multifile-custody`,
uncommitted). Raw files are in
`.claude/lanes/composer-desk-audit-20261112/spikes/S3-multifile-custody/`
(below, `SP/`).

## 1. Question and why it matters

F1: on an empty session the planner cannot build a pipeline over two or more
uploaded files. The s10 request ("combine these two quarters into one file and
add a column saying which quarter each row came from", `q1.csv` + `q2.csv`)
spent the whole 240 s deadline and returned a 504. Only the singular
`source.blob_id` can bind an upload. Named `sources` entries forbid `blob_id`,
and a planner-authored `path` is refused (`source_path_policy_invalid`). D2
approves named `sources` binding uploads, with no backwards compatibility.
Lane **CDA-13** builds it. The spike asks for the smallest design that does
this without weakening blob custody, review or audit, and whether it converges
with the tool loop's state shape.

## 2. Method

- **Read** the canonical schema (`tools/sessions.py`), the candidate builder's
  singular and named branches, `_resolve_source_blob` (`tools/sources.py:797`),
  the reviewed-source branch, the planner custody settlement
  (`pipeline_planner.py:3047-3165`), proposal persistence
  (`sessions/mutation_capabilities.py:180-260`), the proposal blob-reference
  extractor (`sessions/proposal_blob_refs.py`), the commit path
  (`pipeline_commit.py:515-545`) and the set_pipeline round-trip serializer
  (`tools/_common.py:4001-4095`).
- **Read the live capture.** `live/sessions/s10-two-files/turn01/llm_calls.json`:
  - seq 10 and seq 12: DeepSeek authored two named sources with `blob_id`;
  - seq 11: it tried `options.path: blobs/q1.csv` instead.
  - The seq-12 document is extracted verbatim to `SP/s10_seq12_pipeline.json`.
- **Prototype** in the worktree (`SP/prototype.patch`, 783 lines; 5 source
  files plus 1 new test file).
- **Test first.** I wrote
  `tests/unit/web/composer/test_named_sources_blob_binding.py` (15 tests)
  before the code and ran it against the base tree and the prototype.
  - Instrument control: my first base run reported 15 passed. That was
    false: `pyproject.toml:482` sets `pythonpath = ["src", …]`, so pytest
    imported the worktree's `elspeth` despite the exported `PYTHONPATH`.
  - The valid base run overrides it: `-o pythonpath="<main>/src <main>/elspeth-lints/src ."`.
    I printed `elspeth.__file__` under both settings to confirm which tree
    each run loads.
- **Blast radius.** I ran 23 neighbouring test files (`-n 2`), ruff, mypy, the
  two teaching gates and a keyless `elspeth-lints` corpus diff between base
  and prototype.
- **Live s10 run: NOT RUN.** OpenRouter credit was exhausted (HTTP 402; the
  coordinator's instruction). Spend: **US$0.00** (no provider calls). See §5
  for the resume command.

## 3. Results

### 3.1 Test file, base and prototype (EXECUTED)

| Run | Result | Raw |
|---|---|---|
| Base tree (`c987b2b2`, pythonpath overridden to the main checkout) | exit 1, **14 failed, 1 passed** | `SP/pytest_base_final.log` |
| Prototype | exit 0, **15 passed** | `SP/pytest_proto_final.log` |

The one test that passes on base is the negative control
`test_named_inline_blob_stays_out_of_named_sources`, which must stay green. On
base, 46 lines in the log are `extra_forbidden` on `sources.<n>.blob_id`,
which is F1's exact rejection.

What the 15 tests show:

| # | Claim | Test | Prototype |
|---|---|---|---|
| 1a | The **verbatim** live s10 seq-12 document now gets past custody. Its only remaining errors are ordinary graph-naming errors the repair loop already names: `connection_sink_name_overlap`, `node_id_collides_with_source_or_sink`, `queue_no_consumer`. The model named both the queue and the sink `combined`. | `test_verbatim_s10_document_now_fails_only_on_ordinary_graph_naming` | pass |
| 1b | With either repair (drop the queue so both tag transforms feed the sink; or queue → passthrough → sink): `build_set_pipeline_candidate` is acceptable, and `validate_pipeline_for_trained_operator` (the runtime preflight body) is `is_valid` | `test_s10_two_uploaded_quarters_build_and_pass_runtime_preflight[direct\|queue]` | pass ×2 |
| 1c | The per-row `quarter` column needs no new feature: `value_transform` with `expression: "'Q1'"`, one per source | same | pass |
| 2a | A named binding's `SourceSpec` equals what the tool loop's `set_source_from_blob(source_name=…)` produces. plugin, on_success, on_validation_failure and options are all equal | `test_named_blob_binding_matches_tool_loop_set_source_from_blob` | pass |
| 2b | Named binding options equal singular `source.blob_id` options for the same blob | `test_named_blob_binding_options_match_singular_blob_binding` | pass |
| 2c | The proposal blob-reference extractor returns both named blob ids. Ownership validation refuses a foreign-session blob | `test_proposal_blob_references_cover_named_sources` | pass |
| 2d | A named blob-bound state round-trips through `set_pipeline_arguments` (`blob_id` out, path withheld) and rebuilds identical sources | `test_named_blob_bound_state_round_trips_through_set_pipeline_arguments` | pass |
| 3a | `blob_id` + `options.path` → `source_binding_ambiguous` on `source:q1` | `test_named_blob_id_with_path_is_ambiguous` | pass |
| 3b | `inline_blob` in a named source → `ToolArgumentError` (schema) | `test_named_inline_blob_stays_out_of_named_sources` | pass (base too) |
| 3c | Unknown or foreign blob → `source_blob_unavailable` on `source:q2` only | `test_named_foreign_or_unknown_blob_is_refused_per_component` | pass |
| 3d | `blob_id` + manual `blob_ref` → `plugin_options_invalid` on `source:q2` | `test_named_blob_id_with_manual_blob_ref_is_refused` | pass |
| 3e | Uploads (verbatim) stage no INVENTED_SOURCE review | `test_uploaded_blobs_stage_no_invented_source_review` | pass |
| 3f | An LLM-authored blob (made by the real tool-loop `create_blob` under a fenced COMPOSE context) bound as `sources.q2.blob_id` gets `source_authoring` on q2 only, and `interpretation_sites` yields exactly one INVENTED_SOURCE site at `source:q2`. This closes RV1 condition 5, previously READ-inferred | `test_llm_authored_blob_on_named_source_stages_invented_source_review` | pass |
| 3g | A hash-matched reviewed binding plus `blob_id` → `source_binding_ambiguous`. Control: the same reviewed pipeline without `blob_id` is acceptable | `test_reviewed_source_binding_and_named_blob_id_are_ambiguous` | pass |
| 0 | Canonical schema: named sources list `blob_id` and not `inline_blob` | `test_canonical_named_source_schema_admits_blob_id_but_not_inline_blob` | pass |

Other topologies for the same request, run in-process (`SP/topology_variants.log`):

| Variant | Acceptable | Errors |
|---|---|---|
| verbatim (queue id = sink name `combined`) | no | the three naming codes above |
| no queue, both tag transforms → sink | **yes** | — |
| queue with `on_success` naming the sink | no | `queue_config_invalid`: a queue does not accept `on_success` |
| queue id = sink name `all_quarters` | no | the same three naming codes |

### 3.2 Does the planner shape converge with the tool loop? (EXECUTED)

Yes, for `set_source_from_blob(source_name=…)`. Test 2a compares the
`SourceSpec`s field by field.

`set_source_from_blobs` is a different design and does **not** converge. It
yields one `blob_rows` source whose rows are blobs, which then need
`blob_csv_expand`. That is R4(b), which this spike does not recommend
(heavier, and it refuses LLM-authored blobs, `sources.py:1424-1438`).

### 3.3 Blast radius (EXECUTED)

23 neighbouring files, `-n 2`: **4 failed, 1544 passed, 1 skipped**
(`SP/pytest_affected.log`). All 4 failures are expected pins:

| Failing test | Why | Lane action |
|---|---|---|
| `test_set_pipeline_candidate.py::test_no_source_internal_defense_uses_prior_source_shape_for_repair_guidance[named-blob-backed-round-trip-unavailable]` and `[multiple-sources-with-blob-round-trip-unavailable]` | These pin the old "named or multiple blob-backed sources cannot round-trip" behaviour. Those states now round-trip | Flip both cases to expect the re-supply guidance |
| `test_pipeline_planner.py::test_initial_request_declares_supplied_information_and_keeps_discovery_available[live\|small]` | The fixed planner scaffolding grew from ≤135,730 to 136,641 canonical bytes (+911) because of the teaching paragraph and the schema descriptions | Add a named increment constant to `_FIXED_SCAFFOLDING_MAX_CANONICAL_BYTES` (the file's existing pattern, `test_pipeline_planner.py:268-279`), or trim the teaching. Measure after CDA-12, which changes this budget |

Passed unchanged: the named-source strategy in the redaction completeness and
hypothesis tests (`conftest.py:322`; `blob_id` defaults to `None`),
`test_capability_skill_identity.py` (the inventory row was updated with the
schema; `validate_capability_field_contract` agrees),
`test_tool_*_wire_parity`, `test_tool_redaction_policy`,
`test_proposal_blob_refs.py`, `test_owned_composition_state_authority.py`,
`test_set_source_from_blobs.py`, `test_pipeline_planner.py` (apart from the
budget) and `test_planner_authoring_aids.py`.

Gates:

| Gate | Result | Raw |
|---|---|---|
| `ruff check` / `ruff format --check` on the touched files | exit 0 after formatting the new test | `SP/gate_ruff.log` |
| `mypy` on the 4 touched source files | exit 0 | `SP/gate_mypy.log` |
| `generate_skill_inventory.py --check` | exit 0. Not controlled: I did not prove it would catch a skill edit | `SP/gate_skill_inventory.log` |
| `composer_wire_census.py --scorecard` | exit 0 | `SP/gate_wire_census.log` |
| Keyless `elspeth-lints check --rules all --root src/elspeth` | Both trees exit 1. The diff, with paths and line numbers normalized, is **one added line**: an `R_TB_SUPPRESSED` R5 inside `build_set_pipeline_candidate`'s existing `@trust_boundary` (`sessions.py`). That is informational, not a new finding | `SP/lints_base.log`, `SP/lints_proto.log`, `SP/lints_diff.txt` |

Not run: the full suite and the testcontainer suite. No SQL, schema,
persistence-layer or lock code changed; `proposal_blob_refs.py` only reads
JSON that is already stored.

## 4. Recommendation for CDA-13

### 4.1 Design

1. **Schema.**
   - Add `blob_id: str | None` to `_SetPipelineNamedSourceModel`
     (`redaction.py:1777`), moving it out of the singular-only subclass.
   - Add `blob_id` (`["string","null"]`) to the named-source item in the
     canonical schema (`tools/sessions.py`, named `sources` block).
   - `inline_blob` stays singular-only.
   - Update the descriptions: named sources share source's shape "minus
     inline_blob"; the singular source says "For two or more uploaded files,
     use named sources instead, one blob_id each".
   - Update the `named_source` row of the `canonical-field-inventory` in
     `skills/pipeline_capabilities.md`.
2. **Custody flow.** No new primitive. In the named-source loop of
   `build_set_pipeline_candidate`:
   - Run the existing echo normalization, resolver-owned-requirement check and
     canonicalization first.
   - Then call `_resolve_source_blob(blob_id, explicit_plugin, caller_options, rejection_operation=CANDIDATE, source_name=<name>, existing_options=<stored options for that name>)`.
     That is the exact call the singular branch and the tool loop's
     `set_source_from_blob` already make. It already refuses manual
     `blob_ref`/`source_authoring`/`blobs`, checks session ownership, ready
     status and the locked content hash, derives `path`/`blob_ref`/`mode`,
     stamps `source_authoring`, stages INVENTED_SOURCE for LLM-authored blobs
     and runs the guarantee-stamp check with the blob's real modality.
   - A resolver rejection is recorded per component (`source:<name>`), so
     other components still report in the same turn.
   - The resolved source then goes through the same canonical, endpoint,
     plugin, credential, path and prevalidation gates.
   - The manual-key gates are skipped when `reviewed_source or blob_bound`,
     the same way they are already skipped for reviewed sources.
3. **Validation rules.**
   - `blob_id` together with `options.path` or `options.file` →
     `source_binding_ambiguous` on `source:<name>`. Without this rule the
     resolver would silently overwrite the path.
   - **Precedence against reviewed custody:** a source that hash-matches a
     reviewed binding *and* sets `blob_id` → `source_binding_ambiguous`. A
     reviewed binding is never overridden by `blob_id`, and `blob_id` is never
     silently dropped in its favour.
     - For file sources rule 1 fires first, because reviewed bindings carry a
       `blob:` path token.
     - The planner passes `reviewed_source_authority=None`
       (`pipeline_planner.py:3453`). Commit passes a real authority
       (`pipeline_commit.py:528`), and a `blob_id` proposal never hash-matches
       a reviewed binding there, so it re-resolves via the resolver.
   - `inline_blob` in a named source is a schema rejection (unchanged).
4. **Retention and ownership (required, not optional).**
   - `proposal_blob_reference_ids("set_pipeline", …)` must also collect
     `sources.<n>.blob_id` and walk named `options`.
   - On base it reads only `source.blob_id`, so a pending planner proposal
     over q1 + q2 would neither have its blobs ownership-checked at creation
     (`mutation_capabilities.py:217`) nor block their deletion
     (`blobs/service.py:4211`, `coordination/repository.py:3163`).
   - This is the one custody hole the change would otherwise open; the
     prototype closes it.
5. **Round trip.** `_serialize_set_pipeline_arguments` serializes named
   blob-bound sources as `blob_id` (`allow_blob_binding=True` for named). The
   tool loop's `get_pipeline_state(component='set_pipeline_arguments')` then
   stops returning `round_trip_unavailable` for multi-file states.
   - The lane should delete the now-constant `allow_blob_binding` parameter.
   - It should also reword its "default source" error string.
6. **Audit data.**
   - The singular arm emits `data.source_blob` (the payload has no
     `storage_path`). The prototype emits
     `data.named_source_blobs: {name: payload}`.
   - Planner proposals persist `arguments_json` with the `blob_id`s
     (`mutation_capabilities.py:236-256`), and commit re-resolves them.
     Persisted custody evidence therefore matches the singular path.
   - The lane decides the result key name. Check it against
     `test_tool_result_envelope_gate.py` if that gate pins data keys; it
     passed unchanged here, but I did not prove it inspects this key.
7. **Teaching text** (prototype, appended to the named-sources paragraph of
   `pipeline_capabilities.md`):

   > A named source binds an uploaded session file with its own `blob_id`,
   > resolved exactly like `source.blob_id`; never pair `blob_id` with
   > `options.path` or `options.file`. For two or more uploaded files, author
   > one named source per file, each with its own `blob_id` and `on_success`.
   > To combine them into one output, give each source's rows any per-file
   > field the user asked for (for example a `value_transform` setting a
   > literal `quarter`) and point every branch's `on_success` at the same
   > sink; a `queue` fan-in must be consumed by a downstream node, never by a
   > sink of the same name.

   This is teaching, not server-authored structure. Invariant 1 holds: the
   model authors every node, and the server only resolves custody and
   validates. Invariant 2 holds: there is no tutorial path.

### 4.2 Files

- `src/elspeth/web/composer/redaction.py`
- `src/elspeth/web/composer/tools/sessions.py`
- `src/elspeth/web/composer/tools/_common.py`
- `src/elspeth/web/sessions/proposal_blob_refs.py`
- `src/elspeth/web/composer/skills/pipeline_capabilities.md`
- the new test file

### 4.3 Pinned tests and inventories that change

- `test_set_pipeline_candidate.py`: 2 parametrizations of
  `test_no_source_internal_defense_uses_prior_source_shape_for_repair_guidance`.
- `test_pipeline_planner.py`: the fixed-scaffolding byte ceiling (+911 bytes
  measured on this tree).
- The `canonical-field-inventory` `named_source` row (done in the prototype).
- Skill hash consumers: the core skill bytes changed. No pinned hash failed in
  the 23 files run; the lane should run the full skill-hash consumers.
- `tests/unit/web/sessions/test_proposal_blob_refs.py`: add named-source
  cases. Existing cases still pass.

### 4.4 Size estimate

**Small to medium: about 0.5 to 1 engineer-day.**

- The prototype changes 144 source lines (5 files, `git diff --shortstat -- src`: 116 insertions, 28 deletions) and is complete apart from
  the cleanup in §4.1 items 5 and 6.
- The rest is the two pin flips, the budget constant, extractor test cases and
  one full-suite run, since the change touches the shared `set_pipeline`
  contract.
- The new test file can be taken from the prototype almost as is.

### 4.5 Edits to `work-packages.md` CDA-13 (stated, not made)

- **Files:** add `src/elspeth/web/sessions/proposal_blob_refs.py`
  (retention/ownership extractor; required) and
  `src/elspeth/web/composer/tools/_common.py` (`_serialize_set_pipeline_arguments`).
- **Conditions:** add "Define precedence: a hash-matched reviewed binding plus
  `blob_id` is `source_binding_ambiguous`" in place of "Define precedence
  against reviewed-source custody". Add "The proposal blob-reference
  extractor collects `sources.<n>.blob_id`".
- **Test files:** change "a row-union combining topology validates" to "the
  s10 combining topology (two named csv sources with a per-source
  `value_transform` literal into one sink, and the queue → passthrough
  variant) passes `build_set_pipeline_candidate` and
  `validate_pipeline_for_trained_operator`".
  - `row_union` is the wrong primitive here: it is a correlated barrier over
    *fork branches* (`pipeline_capabilities.md`, `[capability-node:row_union]`),
    not a fan-in of independent sources.
- **Also run:** `test_pipeline_planner.py` (scaffolding budget) and
  `test_set_pipeline_candidate.py`. The testcontainer suite is not needed
  unless persistence code changes.
- **Test-command note:** in a worktree, override pytest's configured
  `pythonpath` when proving a test red on base. The pyproject setting
  silently wins over `PYTHONPATH` (measured above).

## 5. Open questions and limits

- **Live s10 not run.** OpenRouter returned 402 for every call. The capture
  already shows DeepSeek authoring this exact shape unprompted (seq 10 and
  12), but whether it repairs the queue/sink name collision within the repair
  budget is UNMEASURED. Resume command from the worktree root, after
  re-funding (cap US$2):

  ```bash
  cd <repo root>/.claude/worktrees/S3-multifile-custody && \
    export PYTHONPATH=$PWD/src:$PWD/elspeth-lints/src && \
    evals/composer-live-audit/instance.sh 8511 S3-proto && \
    evals/composer-live-audit/run_scenarios.sh 8511 S3-proto s10-two-files
  ```

  Run it n≥3 and record per-run terminal status, repair count and wall
  clock. Kill the instance PID afterwards.
- **What would change the recommendation:**
  - If live runs show the model keeps naming the queue and the sink the same,
    the fix is teaching or rejection text (already partly in the paragraph),
    not custody.
  - If CDA-12 restructures the planner overlay, re-place the paragraph and
    re-measure the byte budget.
- **Not measured:**
  - an end-to-end accept/commit of a planner proposal with named `blob_id`
    through `pipeline_commit.py` (READ: commit re-runs
    `build_set_pipeline_candidate` and compares content hashes, so it should
    hold);
  - actual engine execution of the run (only the preflight ran);
  - the frontend proposal diff (READ: `ProposalDiff.tsx:392` skips `blob_id`
    keys generically);
  - the full and testcontainer suites.
