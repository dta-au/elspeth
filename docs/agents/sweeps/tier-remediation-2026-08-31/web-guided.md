# Tier remediation — lane web-guided (elspeth-73d9cb31f1)

Scope: the 46 blocked findings of bundle `sign-2026-08-30-w1` in
`src/elspeth/web/composer/guided/`. Every disposition below was re-derived
from the live tree; the staged `judge_rationale_best_effort` pairings in the
lane worklist were heuristic-ordered and frequently misattributed, so they
were used only as leads, never as evidence.

Dispositions: **45 fixed, 1 restage, 0 abandoned.**

Branch: `lane/tier-rem-web-guided`. Commits:

| sha | scope |
| --- | --- |
| 9fced5485 | planning.py + envelope/bind tests |
| edac51c46 | protocol.py |
| 13dc18a36 | resolved.py + freeze boundary tests |
| 73b8e6103 | stage_transitions.py + reorder test |
| 901973489 | stage_subjects.py + missing-kind tests |
| ba21b2719 | emitters.py + knob-tier test pin |
| 78483a17e | prompts.py |
| 2da416ab2 | deferred_intents.py |
| 311a328d7 | shape_repair_telemetry.py |

Verification: `trust_boundary.tests` gate exits 0 tree-wide;
`trust_tier.tier_model` live findings in `web/composer/guided/` went from 46
(the lane corpus) to 0 lane-owned live sites except the one deliberate
restage below. `tests/unit/web`: 14319+ passed, 1 pre-existing-shape test
updated (knob tier, see ba21b2719), 13 skipped (PostgreSQL-gated), 0 failed.

Composer invariants: no authoring-path behavior changed. The behavioral
deltas are all validation/rejection/projection strengthening:

1. Amend binding now records a node entry without a string `id` as
   `node_entry_malformed` (rejection disposition) instead of silently
   classifying it as an added node.
2. `subject_from_dict`/`constraint_from_dict` raise a named InvariantError
   for a record missing `kind` (previously collapsed into the
   unsupported-kind arm).
3. The synthesized `on_write_failure` knob now carries its KnobField-required
   `tier: "common"` (wire payload gains one key — flagged for the hub's
   whole-tree wire-shape gates).

---

## planning.py (22 findings — all fixed)

### Proposal-correction envelope family — fixed @ 9fced5485

Keys:

- `web/composer/guided/planning.py:R1:resolve_guided_proposal_correction_target:fp=13d4934cc5e618ce`
- `web/composer/guided/planning.py:R1:resolve_guided_proposal_correction_target:fp=8c64ebef94451fdc`
- `web/composer/guided/planning.py:R1:resolve_guided_proposal_correction_target:fp=fa388b805bbe3ed4`
- `web/composer/guided/planning.py:R1:resolve_guided_proposal_correction_target:fp=ffa9d8ff97965178`
- `web/composer/guided/planning.py:R1:require_guided_proposal_correction_target_changed:fp=0fbf634b1365157d`
- `web/composer/guided/planning.py:R1:require_guided_proposal_correction_target_changed:fp=3130ae95f6e41ca1`
- `web/composer/guided/planning.py:R1:require_guided_proposal_correction_target_changed:fp=399a7da8ea286e13`
- `web/composer/guided/planning.py:R1:require_guided_proposal_correction_target_changed:fp=e04c2fa46871cc71`

`proposal_payload` is first-party at every call site: the route passes
`current_turn["payload"]` (durable custody,
`web/sessions/routes/composer/guided.py:3759`, `:3894`) and the service
passes `build_guided_proposal_projection(...)` output
(`web/composer/service.py:3907`) — matching the validated judge verdicts
(fp=3130ae9, fp=399a7da). Both functions now normalize through one shared
Tier-1 helper (`_guided_proposal_correction_wire_payload`) that reads
required keys directly into a closed TypedDict and raises
`AuditIntegrityError` on any envelope deviation; no `.get` remains. Pinned
by `tests/unit/web/composer/guided/test_correction_projection_envelope.py`
(well-formed normalization + 10 malformation arms).

### bind_guided_prose_revision_candidate family — fixed @ 9fced5485

Keys (function scope):

- `web/composer/guided/planning.py:R1:bind_guided_prose_revision_candidate:fp=441d044cd3268690`
- `web/composer/guided/planning.py:R1:bind_guided_prose_revision_candidate:fp=683178f6984f872a`
- `web/composer/guided/planning.py:R1:bind_guided_prose_revision_candidate:fp=6f6644754289e993`
- `web/composer/guided/planning.py:R1:bind_guided_prose_revision_candidate:fp=7500dd43abf43ce2`
- `web/composer/guided/planning.py:R1:bind_guided_prose_revision_candidate:fp=7ff4e1306fcf8f09`
- `web/composer/guided/planning.py:R1:bind_guided_prose_revision_candidate:fp=f264cd64a8f975b1`

Keys (connection_values scope):

- `web/composer/guided/planning.py:R1:bind_guided_prose_revision_candidate:connection_values:fp=7faba19f3a5b5df5`
- `web/composer/guided/planning.py:R1:bind_guided_prose_revision_candidate:connection_values:fp=a98f195c5e1d7ed4`
- `web/composer/guided/planning.py:R1:bind_guided_prose_revision_candidate:connection_values:fp=dc13c2c9d42db444`
- `web/composer/guided/planning.py:R1:bind_guided_prose_revision_candidate:connection_values:fp=e99dcfe6921ec5c9`

One boundary hoist plus an owned/foreign split retires the family:

- The function is now the raising structural boundary
  `@trust_boundary(source_param="pipeline", suppresses=("R1",))` the judge
  prescribed (validated verdicts fp=dc13c2c, fp=e99dcfe), mirroring the
  existing decorator on `bind_guided_reviewed_components`;
  `test_ref=tests/unit/web/composer/guided/test_bind_reviewed_components.py::test_prose_binder_non_list_nodes_names_the_predecessor_node_ids`
  with the gate-emitted canonical fingerprint. Candidate-side reads
  (`bound`, `candidate_nodes`, `added_nodes`, insertion/order/type/plugin
  comparisons) are Tier-3 rooted at `pipeline` and suppressed structurally.
- The genuine violation inside the family (validated verdict fp=441d044/
  f424e45: an id-less dict silently classified as "added" while
  `malformed_node` recorded only non-dict entries) is fixed:
  `candidate_nodes` now admits only dicts with a string `id`, everything
  else is recorded as `node_entry_malformed` and excluded from the
  deterministic rejected-candidate topology. Pinned by the new
  parametrized `test_bind_prose_amend_records_unidentifiable_node_entry_as_malformed`
  (non-dict / missing id / non-string id).
- `connection_values` is only ever fed `predecessor.to_dict()` nodes, so it
  now reads the owned `CompositionState.to_dict()` node contract directly
  (`id`/`input` always present, `on_success` nullable, `routes`/`branches`/
  `fork_to` present only when authored) — corruption of owned state crashes
  instead of shrinking the harvested connection set. The owned sides of the
  `node_type`/`plugin`/rewiring comparisons likewise use direct access, so a
  candidate omission can no longer compare equal to a hidden owned absence
  (judge verdict fp=7faba19).

### `web/composer/guided/planning.py:R1:require_guided_prose_revision_successor:fp=6e41fc452e7bbb2c` — fixed @ 9fced5485

The post-binding node comparison indexed `binding.pipeline["nodes"]` through
a type/id filter after the `rejection_code` gate had already passed —
defensive revalidation of a successful binding (judge verdict fp=b7459a2).
With the binder now rejecting every non-dict/id-less entry, a clean amend
binding proves every predecessor id resolves in both indexes, so both
lookups are direct subscripts; the two `.get`-vs-`.get` comparisons that
could silently compare two Nones equal are also gone.

### `web/composer/guided/planning.py:R1:_sink_options_with_declared_required_fields:fp=727ddf56bdc1b54d` — fixed @ 9fced5485

The `options` interior is authored plugin configuration the composer stores
verbatim and never schema-validates (its own docstring: malformed schema
blocks are left for candidate validation to reject as
`contract_config_invalid`). That is honest non-raising Tier-3 optional
extraction, now declared as `@observation_boundary(source_param="options",
suppresses=("R1",))` — the structural form the doctrine prescribes — with
the `required_fields` read rooted at the authored `raw_schema` block so the
dataflow walk covers it.

### `web/composer/guided/planning.py:R1:_candidate_topology_reference_names:fp=f424e45163f4dfb1` — fixed @ 9fced5485

The function already carried a non-raising `@observation_boundary` rooted at
`bound` justifying exactly this behavior for R5; the `bound.get("nodes")`
read is the same boundary parse, so the fix is widening `suppresses` to
`("R1", "R5")` rather than a per-line entry. (The worklist rationale
paired with this key described the bind-family id-recording defect — that
defect is fixed above.)

## protocol.py (4 findings — all fixed @ edac51c46)

### `web/composer/guided/protocol.py:R1:_validate_single_select_payload:fp=920abb658d5da971`

Non-raising payload validator over Tier-3 turn content; declared
`@trust_boundary(source_param="payload", suppresses=("R1",),
non_raising=True)` following the file's existing validator boundaries. The
defaulted `source_blob_compatible_option_ids` read preserves schema-defined
absence semantics: the key is absent from `_REQUIRED_KEYS`, present in
`_ALLOWED_KEYS`, both enforced by `validate_payload` before dispatch (judge
verdict fp=6e41fc4).

### `web/composer/guided/protocol.py:R5:_validate_knob_schema:fp=4e278e4d05da9cb4`
### `web/composer/guided/protocol.py:R5:_validate_knob_schema:fp=f528d1e716465ef6`

Non-raising knob-schema validator over a Tier-3 payload fragment; declared
`@trust_boundary(source_param="value", suppresses=("R5",),
non_raising=True)`. The Mapping ABC checks are the parse (durable-load and
replay values arrive as MappingProxyType); the name pre-pass drops malformed
entries only for forward-reference collection and the per-field loop rejects
each of them. (Both staged rationales for these keys described unrelated
sites — the telemetry suppress and a persisted-record read; re-derived from
code.)

### `web/composer/guided/protocol.py:R1:_validate_propose_pipeline_payload:branch_producer_is_compatible:fp=1608a82b43ec79bc`

`_validate_propose_pipeline_payload` is now a non-raising
`@trust_boundary(source_param="payload", suppresses=("R1",))` (judge verdict
fp=e04c2fa prescribed exactly this). The nested `branch_producer_is_compatible`
closure sits outside the boundary walk's suppression scope, so its
node-index lookup was additionally rewritten as an explicit membership test
plus direct subscript: absence of a producer id from the node-only index is
the meaningful result "not exclusively branch-derived" (sources are legal
producers indexed separately), now stated in code instead of a `.get(None)`
default.

## resolved.py (5 findings — all fixed @ 13dc18a36)

Keys:

- `web/composer/guided/resolved.py:R5:_validate_and_freeze_guided_json:fp=70c834a8c9944af6`
- `web/composer/guided/resolved.py:R5:freeze_guided_json_mapping:fp=2052cf42ed8d3d59`
- `web/composer/guided/resolved.py:R5:freeze_guided_str_sequence:fp=9d872f20b224eac9`
- `web/composer/guided/resolved.py:R5:freeze_guided_str_sequence:fp=f84b825f7a1c2efd`
- (family also covers the recursive freezer's Mapping dispatch at line 62,
  flagged in the live corpus)

These are the genuine Tier-3 promotion boundaries for externally submitted
guided JSON — every construction site (`SchemaFormResponse.options`,
`SchemaFormAuthority.*`, `PluginSelectionResponse.chosen`, …) feeds them
client/LLM wire values, and malformed input raises (`TypeError` /
`InvariantError`), never defaults. Declared as raising `@trust_boundary`
functions rooted at `value`, each pinned by a direct rejection test in the
new `tests/unit/web/composer/guided/test_resolved_freeze_boundaries.py`
with gate-emitted canonical fingerprints. (The fp=70c834a staged rationale
argued about a persisted first-party `subject_from_dict` record — that
concern is the stage_subjects fix below; the freezer itself takes wire
input.)

## stage_subjects.py (2 findings — both fixed @ 901973489)

- `web/composer/guided/stage_subjects.py:R1:constraint_from_dict:fp=d8947f88cccfdb2c`
- `web/composer/guided/stage_subjects.py:R1:subject_from_dict:fp=2d6675570d76f5ac`

Both decoders read ELSPETH-authored persisted records (their own `to_dict`
output); serialization does not demote Tier-1 data, so `value.get("kind")`
converting a missing discriminator into the unsupported-kind arm was
defensive access on owned state. Now: membership check + direct access,
with a named `InvariantError` ("record is missing 'kind'") for the absent
discriminator. Pinned by two new tests in `test_stage_subjects.py`.

## stage_transitions.py (2 findings — both fixed @ 73b8e6103)

- `web/composer/guided/stage_transitions.py:R5:reorder_reviewed_components:fp=4002f8df1b76ea42`
- `web/composer/guided/stage_transitions.py:R5:reorder_reviewed_components:fp=b9187c5abaf7f0ff`

`stable_ids` is client-submitted wire input typed `object`; the sequence /
character-sequence-trap / exact-UUID checks are the raising Tier-3 parse.
Declared as a raising `@trust_boundary(source_param="stable_ids",
suppresses=("R5",))` with a new direct rejection test
(`test_reorder_reviewed_components_rejects_non_sequence_and_non_uuid_stable_ids`)
and gate-emitted fingerprint. (Both staged rationales for these keys
described a `suppress(BaseException)` in pipeline_planner — misattributed;
re-derived from code.)

## emitters.py (4 findings — 3 fixed @ ba21b2719, 1 restage)

### `web/composer/guided/emitters.py:R5:build_step_1_schema_form_turn_from_resolved:fp=6722ffed8d530e40` — fixed (drift_repair superseded)

The stale per-line entry is superseded by a non-raising
`@observation_boundary(source_param="source", suppresses=("R1","R5"))`:
`source.options` is authored plugin configuration of unguaranteed shape;
`blob_ref` absence is the normal non-blob shape and the `path` string check
gates only the blob-sentinel masking, leaving operator-typed path knobs
untouched.

### `web/composer/guided/emitters.py:R8:build_step_2_schema_form_turn:fp=b25b7d174cc2a0f1` — fixed

The `on_write_failure` prefill seed is a deliberate form default rendered
for operator review, not a hidden missing-key recovery; the value-discarding
`setdefault` is now an explicit membership check + assignment that states
the first-wins choice. (While touching the file, mypy surfaced that the
synthesized wrapper knob omitted the KnobField-required `tier`; it now
carries `"common"` like the catalog's own synthesized knobs — one added
wire-payload key, test pin updated.)

### `web/composer/guided/emitters.py:R6:_wire_schema:fp=0cc3659594180b2c` — RESTAGE

R6 fires on `except ValueError: continue` around `FieldDefinition.parse` of
one authored field entry. The `@trust_boundary` mechanism cannot suppress
R6, and the omission is not a silent first-party failure: it is the
declared sentinel arm of `_wire_schema`'s existing `@observation_boundary`
contract. A per-line entry is the only honest form. Proposed rationale:

> R6 fires on the `except ValueError: continue` inside `_wire_schema`
> (`web/composer/guided/emitters.py`), which is declared
> `@observation_boundary(source_param="options")` with the invariant
> "returns the weakest honest business-schema projection ('observed' mode,
> empty field/name lists) for any option shape it cannot read, and never
> raises on a malformed options payload". The guarded call is exactly
> `FieldDefinition.parse(field)` on one entry of the authored, never
> schema-validated `options["schema"]["fields"]` sequence (Tier-3 composer
> content); `ValueError` is that parser's documented rejection of a
> malformed field definition, so the `continue` is the boundary's declared
> omission semantics — the malformed entry contributes nothing to the wire
> projection and is rejected authoritatively by candidate validation, which
> owns `contract_config_invalid`. No first-party failure can reach this
> handler: the parse input is external-origin by construction. Invalidated
> if the except widens beyond `ValueError`, if the guarded call stops being
> a foreign-data parse, or if `_wire_schema`'s result ever becomes the
> authority that admits a schema rather than a display projection.

## prompts.py (5 findings — all fixed @ 78483a17e)

- `web/composer/guided/prompts.py:R5:_summarize_sample_value:fp=1ce973a0c70ea4d5`
- `web/composer/guided/prompts.py:R5:_summarize_sample_value:fp=44b32db2c13ebe58`
- `web/composer/guided/prompts.py:R5:_summarize_sample_value:fp=6bce9751e24a64e7`
- `web/composer/guided/prompts.py:R5:_summarize_sample_value:fp=7223ecbb324ff4ba`
- `web/composer/guided/prompts.py:R5:_summarize_sample_value:fp=96378065f95c3dc2`

The masker observes one uploaded Tier-3 sample value solely to collapse it
to a bounded `<sample:...>` type tag (never emitting the value, a key, or a
member) and cannot raise. Declared as a non-raising
`@observation_boundary(source_param="value", suppresses=("R5",))`,
superseding all five per-line entries (which had been staged as drift
repairs).

## deferred_intents.py (3 findings — all fixed @ 2da416ab2)

### `web/composer/guided/deferred_intents.py:R5:_json_literal:fp=643a3218bb1c7dbd`
### `web/composer/guided/deferred_intents.py:R5:_json_literal:fp=c71fcab3c8ee8883`

`_json_literal` walks the AST parsed from a composer-authored predicate
expression string — Tier-3 authored content whose parse-tree shape is
unconstrained beyond Python syntax. The isinstance dispatch is nominal
typing over the `ast` module's concrete node classes (the only correct way
to walk a foreign parse tree) with a `(False, None)` sentinel otherwise.
Declared as a non-raising `@observation_boundary(source_param="node",
suppresses=("R5",))`, per the judge's structural-boundary verdict on
fp=643a321. (The fp=c71fcab staged rationale described predecessor
`routes`/`branches` reads in the planning binder — that defect is fixed in
the bind family above.)

### `web/composer/guided/deferred_intents.py:R8:_constraint_conjunction_contradiction:require_subject:fp=e17522744e0de681`

The representative-subject insert used a value-discarding
`required_subjects.setdefault(subject_key, subject)`; it is now an explicit
membership check + assignment stating the first-wins choice. (The adjacent
accumulator `setdefault(...).add(...)` patterns in the same function carry
their own signed, still-valid entries — see rotation list below.)

## shape_repair_telemetry.py (1 finding — fixed @ 311a328d7)

### `web/composer/guided/shape_repair_telemetry.py:R7:record_guided_shape_repair:fp=daadedb027c5f366`

The blocked suppression wrapped the structlog **event** emission — the
attribution record for a repair resolution. The telemetry policy forbids
silent failure at emission points, so that `suppress(Exception)` is removed
and a failing event sink now propagates. The **counter**'s separate
suppression is untouched: it carries a signed ACCEPTED verdict
(fp=d8b45de40b02d8a5, "best-effort aggregate increment off the correctness
path") and stays best-effort by design — this split exactly matches the
judge's accepted/blocked pattern on the sibling advisor telemetry file.

---

## Shared-baseline changes needed

No masquerade or dynamic-attribute pinned-set updates: the lane added no
`getattr`/masquerade sites.

**Wire-shape note for the hub's whole-tree gates:** the step-2 schema_form
`on_write_failure` knob now carries `"tier": "common"` (KnobField requires
it; surfaced by mypy on touch). If a pinned wire-shape template covers that
turn, it needs the corresponding one-key update.

**Signed-entry drift repair (hub `rotate`, mechanical, no judge) — 20
still-valid signed entries went fingerprint-stale from line/AST shifts in
this lane's edits; their rationales are untouched by the changes:**

- `web/composer/guided/shape_repair_telemetry.py:R7:record_guided_shape_repair:fp=d8b45de40b02d8a5`
  (the ACCEPTED counter suppression; ast_path shifted when the sibling
  suppress was removed)
- `web/composer/guided/deferred_intents.py:R1:_constraint_conjunction_contradiction:` fp=
  `04dd2417bd739a73`, `25f0d71c3e442c67`, `2cb5ed1e1ba6d27d`,
  `2f57c1fa18ab3672`, `36375baa99dde377`, `48db3fdb7d8e4add`,
  `641cf6f274084eaa`, `66f52ff15947c391`, `82fe46b0430c27e5`,
  `8ffb15e90d94e4ab`, `9dd2322461cf0b3e` (11 entries)
- `web/composer/guided/deferred_intents.py:R8:_constraint_conjunction_contradiction:` fp=
  `39af40305a8161c9`, `6ee265612e1bd572`, `7f6ce408e83bcb67`,
  `9f48fbb542c0cbab`, `a3a5d5fdb10b4fc2`, `cda6f82ce8b1f670` (6 entries)
- `web/composer/guided/deferred_intents.py:R8:_constraint_conjunction_contradiction:require_subject:` fp=
  `6f2620458359a577`, `deb7bc992a8ca83b` (2 entries)

Until rotation, those sites report as live findings + stale entries in the
raw corpus — measure this lane against the list above, not against zero.

**Allowlist deletions (hub-applied; entries superseded by structural
boundaries or by code that no longer contains the pattern) — 26 entries,
these are the full stale set minus the rotation list above:**

- `web/composer/guided/deferred_intents.py:R5:_json_literal:fp=124a0e1c1f88d364`
- `web/composer/guided/deferred_intents.py:R5:_json_literal:fp=77f279297826a4d2`
- `web/composer/guided/deferred_intents.py:R5:_json_literal:fp=dda4592388d2f748`
- `web/composer/guided/emitters.py:R5:build_step_1_schema_form_turn_from_resolved:fp=6722ffed8d530e40`
- `web/composer/guided/planning.py:R1:bind_guided_prose_revision_candidate:fp=0bf29b6f1ccac933`
- `web/composer/guided/planning.py:R1:bind_guided_prose_revision_candidate:fp=3ed3f0c6d6b8ead0`
- `web/composer/guided/planning.py:R1:bind_guided_prose_revision_candidate:fp=4f289f54b65ace4a`
- `web/composer/guided/planning.py:R1:bind_guided_prose_revision_candidate:fp=82c911b0555033f1`
- `web/composer/guided/planning.py:R1:bind_guided_prose_revision_candidate:fp=e04d2dc5b91f618c`
- `web/composer/guided/planning.py:R5:bind_guided_prose_revision_candidate:connection_values:fp=0bafaca6dc0fc258`
- `web/composer/guided/planning.py:R1:_candidate_topology_reference_names:fp=041a323df50dded0`
- `web/composer/guided/planning.py:R1:_candidate_topology_reference_names:fp=a1363c96a8d140eb`
- `web/composer/guided/planning.py:R1:require_guided_proposal_correction_target_changed:fp=c0be8cbc91f2e8c0`
- `web/composer/guided/planning.py:R1:require_guided_prose_revision_successor:fp=33a0ecd028877570`
- `web/composer/guided/planning.py:R1:require_guided_prose_revision_successor:fp=baffd58445483c24`
- `web/composer/guided/planning.py:R1:resolve_guided_proposal_correction_target:fp=c6e4b862eef44ecf`
- `web/composer/guided/planning.py:R1:_sink_options_with_declared_required_fields:fp=9bc8e924a452f0b3`
- `web/composer/guided/prompts.py:R5:_summarize_sample_value:fp=1ce973a0c70ea4d5`
- `web/composer/guided/prompts.py:R5:_summarize_sample_value:fp=44b32db2c13ebe58`
- `web/composer/guided/prompts.py:R5:_summarize_sample_value:fp=6bce9751e24a64e7`
- `web/composer/guided/prompts.py:R5:_summarize_sample_value:fp=7223ecbb324ff4ba`
- `web/composer/guided/prompts.py:R5:_summarize_sample_value:fp=96378065f95c3dc2`
- `web/composer/guided/protocol.py:R1:_validate_propose_pipeline_payload:fp=530a40dc09814a99`
- `web/composer/guided/protocol.py:R1:_validate_propose_pipeline_payload:fp=5326bf5a04e51e4b`
- `web/composer/guided/protocol.py:R1:_validate_propose_pipeline_payload:fp=95acaa71368f5d03`
- `web/composer/guided/protocol.py:R1:_validate_propose_pipeline_payload:fp=b9265560c87dfc5a`

**Out-of-lane observations (no action taken):**

- `web/composer/guided/_discovery.py` carries 3 live R5 findings that predate
  this lane and are not in its worklist.
- `web/composer/guided/chat_solver.py` trips the per-file `max_hits` (13/11)
  — pre-existing at the lane baseline, file untouched here.
- The new restage above (`emitters.py:R6:_wire_schema`) needs hub staging
  with the rationale text quoted in its section.
