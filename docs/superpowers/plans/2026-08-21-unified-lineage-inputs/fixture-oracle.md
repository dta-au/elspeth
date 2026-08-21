# Fixture-oracle scout report — unified lineage / barrier scopes (spec rev 3.2)

**Date:** 2026-08-21. **Scout scope:** `tests/fixtures/dag_scenario_corpus/`, its manifest
(`docs/architecture/dag/scenario-corpus/v1/manifest.yaml`, schema v2, 4,434 lines),
`tests/golden/`, `examples/`, and `tests/unit/core/test_canonical.py`. Authority:
`docs/superpowers/specs/2026-08-21-barrier-scopes-full-nesting-spec.md` (rev 3.2,
rulings 1–28 final).

Classification vocabulary (per §11 of the spec):

- **FROZEN** — group-id-normalized stable projections must diff byte-identical across WS1.
- **REGENERATED** — representation-bearing; rebuilt under the frozen-oracle protocol with
  the delta adjudicated against the §4.1a table.
- **RULING-CASUALTY** — topology rejected by the new §7 rules; stays frozen through the
  WS1 diff, leaves the frozen set at WS2 with an adjudicated migration (never silently).

---

## 1. `tests/fixtures/dag_scenario_corpus/` inventory (41 pipeline YAMLs, 15 scenario dirs)

The corpus manifest declares 15 scenarios (`schema.py:74` `EXPECTED_SCENARIOS`); the 15th
(`multi-worker-lease-reclaim-late-completion`) has **no v1 fixture directory** — it is
pytest-evidence only. Expectation kinds across the manifest: 10 `exact` (full inline
`StableRunProjection` including audit_records), 21 `semantic_runtime` (counts +
`projection_sha256` over the full projection), 14 `summary` (some with
`resumed_full_projection_sha256`), plus 63 pytest evidence references and 12+ typed
recovery-evidence blocks.

| Fixture (v1/…) | Topology (one line) | Class |
|---|---|---|
| `linear/happy-path.yaml` | csv → passthrough → json sink | FROZEN |
| `multiple-independent-sources/independent-roots.yaml` | 2 csv sources → shared `output` queue path → json sink (no gates/forks) | FROZEN |
| `multi-source-queue-fan-in/queued-fan-in.yaml` | 2 csv sources → one `inbound` queue → passthrough → sink | FROZEN |
| `conditional-routing/two-way-gate.yaml` | csv → gate routes true/false → 2 sinks | FROZEN |
| `conditional-routing/error-route-and-discard.yaml` | csv → gate → always-error transform, `on_error: errors` sink / discard | FROZEN |
| `conditional-routing/route-reopen-resume.yaml` | same gate shape, recovery case | FROZEN |
| `fork-multiple-terminals-partial-failure/one-terminal-fails.yaml` | gate fork_to [failing, survivor] → both branches DIRECT to sinks, no closer | RULING-CASUALTY per spec §11 (cites r23 "mixed fork") — **contested, see Risk 1** |
| `fork-multiple-terminals-partial-failure/reopen-after-survivor-boundary.yaml` | identical topology, sink-boundary recovery fault | same as above |
| `fork-coalesce-policies/*.yaml` (19 files: require-all/quorum/best-effort/first × nested/select/union merge × lost-c/all-lost/collision variants) | single 3-branch fork (`path_a/c/b`) → per-branch value_transform (or `dag_corpus_branch_loss`/`dag_corpus_always_error`) → ONE coalesce `merge_paths` whose `branches:` equals the fork list → sink | FROZEN (whole-roster closure already satisfied; outermost bound group, no enclosing frame ⇒ ruling 19 keeps failure behaviour verbatim). See Risk 10 for WS3 ledger-count churn |
| `sequential-nested-fork-coalesce/two-sequential-require-all.yaml` | fork_a → merge_a → fork_b → merge_b → sink — **sequential, NOT nested** (two depth-1 regions in series) | FROZEN |
| `parallel-coalesces/two-parallel-require-all.yaml` | ONE fork `fork_to [left_a, left_b, right_a, right_b]` closing at TWO coalesces (`merge_left` over the left pair, `merge_right` over the right pair) | RULING-CASUALTY — **r23** (whole-roster: a fork closes entirely at ONE closer). Migration: rewrite as nested forks. See Risk 2 |
| `aggregation-immutable-batch/eof-immutable-membership.yaml` | csv → top-level aggregation (`dag_corpus_eof_batch_sum`, count trigger) → sink | FROZEN (aggregator outside any bound region — legal) |
| `aggregation-immutable-batch/resume-after-eof-flush-fault.yaml` | same + fail-once flush fault | FROZEN |
| `checkpoint-deterministic-resume/reopen-resume.yaml` | csv → top-level aggregation (fail-once EOF batch) → sink | FROZEN |
| `row-expansion-parent-child-recovery/json-explode-parent-child.yaml` | json source → `json_explode` (undeclared expand, TOP-LEVEL ⇒ inert frame, legal outside bound regions) → sink | FROZEN |
| `row-expansion-parent-child-recovery/json-explode-recovery.yaml` | same, observed schema, recovery case | FROZEN |
| `row-union-interleave/two-variant-ab.yaml` | fork [control_branch, treatment_branch] → per-branch value_transform → **row_union** `variant_union` (whole roster) → downstream aggregation `batch_experiment_compare` (AFTER release, outside region — legal) → sink | **REGENERATED at WS1** — ruling 27 pops branch frames on release: released tokens' `branch_name` → None. This is the one fixture the spec itself tables as a WS1 delta (§4.1a last row). See Risk 7: the delta is whole-projection, not one field |
| `retry-quarantine-discard-routed-errors/retry-then-success.yaml` | csv → retry-once transform → sink | FROZEN |
| `retry-quarantine-discard-routed-errors/source-quarantine-routed.yaml` | csv with validation-failure routing → 2 sinks | FROZEN |
| `retry-quarantine-discard-routed-errors/transform-discard.yaml` | csv → always-error transform, on_error discard | FROZEN |
| `retry-quarantine-discard-routed-errors/transform-error-route.yaml` | csv → gate → always-error transform, on_error routed to sink | FROZEN |
| `sink-write-pending-redrive/write-once.yaml`, `pending-redrive-reopen.yaml` | csv → json sink (sink-effect fault cases) | FROZEN |

**No corpus fixture is an r25 casualty** (no aggregator sits inside a fork→coalesce or
fork→row_union region; all aggregations are top-level or after a closer's release) and
**none is an r28 casualty** (the only multi-row transform, `json_explode`, is top-level).
The corpus loss plugins (`dag_corpus_branch_loss`, `dag_corpus_always_error`) route
`on_error: discard` *inside* bound regions — see Risk 3 on the SESE walk's treatment of
error edges.

**Harness rewrite is itself WS1 scope.** `harness.py::_stable_projection` reads the
retired tri-columns in four places, all of which die with the §4.3 retirement matrix:

- token sort discriminator `record.get("expand_group_id") is None` (`harness.py:944`)
  and `branch_name` in both sort keys (`:925`, `:933`, `:951`);
- `StableTokenProjection.branch_name` from the durable token record (`:971`);
- expansion-children gathering by `child_record.get("expand_group_id")` (`:1146`);
- `expand_parent` outcome gate on `token_outcome.expand_group_id` +
  `expected_branches_json` (`:1111`, `:1134-1136`).

All four must re-derive from `token_lineage_frames`/`group_records` while reproducing
byte-identical projections for every FROZEN fixture. See Risk 6 (self-oracle).

## 2. Projection classes in `tests/fixtures/dag_scenario_corpus/schema.py`

Exact fields (all `ClosedModel`: `extra="forbid"`, frozen):

- **`StableTokenProjection`** (`:186`): `key` (normalized `source:rowidx#ordinal`),
  `row_key`, `parents: tuple[StableParentProjection(ordinal, parent_key), ...]`
  (validated unique + ordinal-sorted), `branch_name: NonEmpty | None = None`.
- **`SinkOutputProjection`** (`:153`): `sink_name`, `rows: tuple[NonEmpty, ...]` — each
  row validated to be canonical JSON, byte-exact.
- **`StableTerminalDisposition`** (`:264`): `key`, `token_key`, `outcome`
  (`success|failure|transient`), `path` (closed 14-value Literal: `default_flow`,
  `gate_routed`, `gate_discarded`, `gate_error_discarded`, `on_error_routed`,
  `filter_dropped`, `coalesced`, `unrouted`, `quarantined_at_source`,
  `sink_fallback_to_failsink`, `sink_discarded`, `fork_parent`, `expand_parent`,
  `batch_consumed`), `sink_name | None`, `error_hash` (16-hex, excluded when None).
- **`StableExpansionProjection`** (`:351`): `key` = `f"expand|{parent_token_key}"`,
  `parent_token_key`, `expected_child_count: PositiveCount` (**ge=1**), `children:
  tuple[StableExpansionChildProjection(ordinal, token_key), ...]` (dense-from-zero,
  count must equal `expected_child_count`).

**Group-id leakage: none.** All four classes carry only normalized keys; no raw
`fork_group_id`/`expand_group_id`/`join_group_id` appears in any projection or anywhere
in the manifest (verified by grep — zero `*_group_id` hits in the manifest). Group ids
are used internally by the harness for ordering/gathering only.

**Values the §4.1a accessor deltas change:**

- `StableTokenProjection.branch_name` (and its `SemanticTokenProjection` sibling at
  `:215`) is the ONE projected field the deltas touch. Ruling 27 flips it to None on
  every row_union released token — that hits `row-union-interleave` (the only row_union
  fixture; the `fork-coalesce-policies` "union" variants are coalesce `merge: union`,
  not row_union, and are unaffected). Manifest `branch_name` values in exact
  projections today: `failing`/`survivor` (fork-multiple-terminals), `path_a/b/c`
  (fork-coalesce) — all fork children whose accessor value is identical pre/post.
- The §4.1a rows for expand-child-inside-fork-branch (`branch_name` None→branch on the
  durable row) and fork-under-expand (`expand_group_id` regained) would also surface in
  this field — but **no fixture has either topology** (see §6), so no frozen fixture
  moves on those rows.
- Second-order: `branch_name` is a component of the harness token SORT key, so a
  changed value can reassign `#ordinal` token keys, which cascade into every keyed
  projection (node_states, dispositions, routes, scheduler work) for that fixture —
  Risk 7.

Two schema gaps for new WS1 fixtures: `expected_child_count: PositiveCount` cannot
represent the spec-required `member_count=0` empty-expansion record (§4.3), and the
`path` Literal lacks `scope_group_failed`/`all_members_lost`/`empty_expansion`. Both are
deliberate schema extensions to land WITH the new fixtures, not silent edits.

Also load-bearing: projection node keys embed a 12-hex semantic-config sha suffix
(`sink:failing@5a3f478d7f7a`, `harness.py:399`). Frozen bytes therefore depend on
node-level canonical serialization not moving — the §3 omitted-when-None rule for the
collector binding key protects the corpus itself, not just composer hashes (Risk 5).

## 3. `tests/golden/` JSON inventory — 56 files

Count confirmed: **56** (`tests/golden/state_engine/plugin_lifecycle_matrix.json` = 1;
`tests/golden/web/catalog/policy_view/` = 4; `tests/golden/web/catalog/knob_schema/` =
51). Matches the spec's "56 golden JSONs".

- **`plugin_lifecycle_matrix.json` — FROZEN.** It pins the closed builtin plugin/variant
  set (PB-09 three-way contract). The campaign adds no builtin plugin: collectors reuse
  the existing batch-transform plugin contract (§2), so the variant set is untouched.
  It moves only if a builtin collector-flavoured plugin is later added — that would be a
  deliberate reviewed-golden update, out of this campaign's specced scope.
- **51 `knob_schema/` + 4 `policy_view/` — FROZEN.** These derive from per-plugin config
  schemas. The spec changes no plugin schema (coalesce/row_union config UNCHANGED, §3;
  `collectors:`/`scopes:` are settings-level node structures, not plugin knobs;
  aggregation plugins untouched, §5).
- **Not adjudicated (stated honestly):** whether the composer catalog generator will
  emit NEW golden files for the collector node kind / scope surface. That adds files;
  it does not change the existing 56. I could not adjudicate it because the catalog
  generation path for non-plugin node kinds (gates/coalesce have no goldens today,
  which suggests node kinds without plugins produce none) was not traced end-to-end.

## 4. `examples/` sweep — migration worklist

Fork-bearing settings (9 files): `fork_coalesce/{settings,settings_per_branch,
settings_union_fail,settings_union_first_wins,settings_union_last_wins}.yaml` and
`row_union_ab_experiment/{settings,settings_identity_branches,settings_paired_preference,
settings_screened}.yaml`.

- **Mixed fork closure (r23): NONE.** Every example fork's declared branch list closes
  whole-roster at exactly one coalesce or row_union.
- **Aggregation inside a coalesce/union branch (r25): NONE.** All aggregation-bearing
  examples are either fork-free (`batch_aggregation`, `statistical_batch_plugins/*`,
  `deaggregation`, `concurrent_scheduler`, `report_assemble`) or place the aggregation
  AFTER the row_union release (`row_union_ab_experiment/*`) — outside the bound region,
  legal.
- **Multi-row transform inside a bound branch (r28): NONE.** The expanders
  (`json_explode`, `multi_worker*`, `blob_transforms/settings_expand_csv_blobs`) are
  all in fork-free pipelines.
- **ONE casualty, different rule — `row_union_ab_experiment/settings_screened.yaml`
  (§7 rule 4, forward SESE):** the `quality_screen` gate sits INSIDE the control branch
  (between `tag_control` and the union) and routes `'false'` to the `screened_out`
  SINK — a path from the opener reaching a sink before the closer, "rejected flat"
  under rule 4. There is **no mechanical migration**: the example's entire pedagogical
  point is a mid-branch screen leaving union groups incomplete, which the new build
  rules make unbuildable. Needs a redesigned replacement (screen before the fork, or
  screen-as-loss via the settlement channel) — human decision.
- `fork_coalesce/settings_union_fail.yaml` deliberately raises
  `CoalesceCollisionError` at an outermost bound group with no enclosing frame —
  ruling 19 preserves that failure behaviour verbatim; stays as-is.

## 5. Canonical-hash state

`tests/unit/core/test_canonical.py` (906 lines, 83 tests) pins **value-level**
canonicalization only: numpy scalar/array conversion + NaN/Inf rejection, pandas
Timestamp/NaT/NA, naive/aware datetime→UTC ISO, date/time/UUID/Decimal handling, bytes
base64 envelopes + collision escaping, set/custom-class rejection, `canonical_json`
delegation, and `stable_hash` determinism. Nothing in it hashes a pipeline, a settings
document, or a graph.

**Confirmed: no whole-pipeline canonical-hash corpus exists anywhere in `tests/`.**
Searched for pipeline/settings/graph hash pins over `examples/`; the nearest existing
artifacts are (a) `tests/unit/web/composer/test_state_serialisation_contract.py`'s
pinned `composition_content_hash` values for representative composer-authored shapes,
and (b) the corpus harness's own per-node config fingerprints + `semantic_settings_sha256`
embedded in projection keys. The §3/quality-F7 corpus test (canonical hashes of
`examples/` settings + composer shapes, recorded at pre-WS2 HEAD) must be built from
scratch as the early-WS2 item the spec demands.

## 6. Deepest nesting today

**Depth 1, everywhere.** No fixture or example nests one bound region inside another,
and no token in any corpus run carries more than one lineage-relevant identity:

- `sequential-nested-fork-coalesce` is sequential (fork→coalesce→fork→coalesce), two
  depth-1 regions in series — the merged token re-forks at top level.
- `parallel-coalesces` is one fork split across two SIBLING closers (and dies at WS2
  under r23) — parallel, not nested.
- The `fork-coalesce-policies` "nested" variants refer to `merge: nested` (a merge
  strategy), not topology.
- No expand-under-fork, no fork-under-expand, no fork-in-fork exists in the corpus or
  `examples/` (the expanders and the forks live in disjoint pipelines).

Consequence: §4.1a table rows 2–4 (expand child inside a fork branch, fork child inside
an expand, merged token under outer frames) have **zero existing coverage**, and the
depth-5 supported-guarantee matrix (settlement, escalation-to-quarantine, resume at
depth 5) requires entirely new fixtures — including at least one true depth-2 fixture
for the WS1 differential accessor-equivalence tests, since neither fixture the spec
names for that purpose is actually nested (Risk 2).

---

## RISK NOTES — human adjudication needed

1. **`fork-multiple-terminals-partial-failure` classification contradiction.** Spec §11
   names it "the mixed-fork corpus fixture" rejected by rulings 23/25 — but its actual
   topology (both YAMLs verified identical) is `fork_to [failing, survivor]` with BOTH
   branches direct to sinks and no closer anywhere: §7 rule 2's own text makes that
   "fully unbound (pure fan-out)" and LEGAL. Either the spec's example is wrong (fixture
   stays FROZEN permanently, and the fork-parent/branch-token audit shape for unbound
   forks must survive WS1 byte-identical) or "fully unbound" is narrower than written
   (in which case pure fan-out forks — routing to multiple terminals — become
   inexpressible, a much bigger break than r23 states). This changes both the oracle
   set and the migration worklist; adjudicate before the WS1 freeze.
2. **The WS1 nested differential tests have no substrate.** §11 designates
   `sequential-nested-fork-coalesce` and `parallel-coalesces` as "the nested corpus
   fixtures" for §4.1a case-by-case equivalence — neither is nested, and
   `parallel-coalesces` is itself an r23 casualty (usable through WS1, gone at WS2).
   New genuinely-nested fixtures (fork-in-fork at minimum; fork-in-expand and
   expand-in-fork once `scopes:` exists) must be authored and frozen BEFORE WS1 starts,
   or the checkpoint's hardest surface (accessor semantics under nesting — the failure
   §11 says the suite alone cannot catch) is tested against nothing.
3. **SESE forward-walk scope over error/discard edges must be pinned.** Every
   fork-coalesce loss fixture terminates tokens in-region via `on_error: discard`
   (`dag_corpus_branch_loss` etc.) — that IS the settlement system's input. If §7 rule
   4's "every path from the opener reaches the closer before any sink/terminal" counts
   `on_error` edges, all 8 lost-branch fixtures and the loss machinery itself are
   build-rejected — plainly unintended (rule 9 treats in-region `on_error` as legal and
   derivable). The plan must state explicitly that the forward walk covers
   success-path/route edges only; otherwise implementers will read rule 4 literally.
4. **Manifest sha pins are NOT restricted to the stable projections.** `exact`
   expectations embed full audit_records; `semantic_runtime`'s `projection_sha256`
   hashes the full projection. If `token_lineage_frames`/`group_records`/`group_losses`
   surface in the portable export, every sha and `audit_record_counts` block churns —
   including for FROZEN fixtures. That churn is an *allowed* §11 delta ("new audit
   rows"), but the oracle diff must therefore run on the four stable projection classes
   extracted separately, never on the manifest blobs, and the manifest regeneration
   must be mechanically attributable to new-record-types-only. Decide the export-surface
   question (do the new tables export in WS1?) before freezing.
5. **Node-key config-hash suffixes make the whole corpus canonicalization-sensitive.**
   Every frozen key embeds a 12-hex per-node semantic-config sha. Any change to node
   canonical dicts (a default that starts serializing, a reordered key) rewrites every
   key in every frozen projection. The §3 omitted-when-None rule is load-bearing for
   the corpus, not only for `composition_content_hash`.
6. **The harness is part of the oracle and must itself be rewritten in WS1** (four
   retired-column read sites, §1 above). A rebuilt harness verifying its own rebuild is
   the self-oracle shape §11 forbids for fixtures. Mitigation: emit and commit the
   pre-WS1 stable-projection outputs as plain files at freeze time, so the rewritten
   harness is checked against stored bytes, not against its own regeneration.
7. **Ruling 27's row-union delta cascades beyond `branch_name`.** The harness token
   ordering sorts on `branch_name`; popping it to None on released tokens can reassign
   `#ordinal` token keys and hence rename every keyed record in the row-union
   projection. Adjudicate the regenerated fixture as a whole-projection delta with the
   sink bytes and dispositions (paths/outcomes) required identical, rather than
   expecting a one-field diff.
8. **Two deliberate schema.py extensions ride WS1:** `PositiveCount` on
   `expected_child_count` blocks the required `member_count=0` empty-expansion fixture,
   and the `StableTerminalDisposition.path` Literal lacks the new disposition vocabulary
   (`scope_group_failed`, `empty_expansion`, `all_members_lost`). Both are closed
   contracts (`extra="forbid"`) — extending them is a reviewed change, and existing
   frozen fixtures must not be touched by it.
9. **`settings_screened.yaml` has no mechanical migration** (§4 above) — its
   demonstrated behaviour is exactly what the new rules prohibit. A human must choose
   the replacement story before WS2 lands rule 4.
10. **WS3 will churn manifest counts for the loss fixtures even though rulings 19/§6.1
    preserve behaviour.** Retiring the three raw `record_token_outcome` bypasses and the
    `coalesce_branch_losses` ledger into settle-member/`group_losses` can change
    scheduler_event/record counts pinned in `semantic_runtime`/`summary` expectations
    while dispositions stay identical. That regeneration belongs to WS3's adjudication,
    separate from WS1's — the plan should version the manifest pins per workstream, as
    §11 already does for the frozen set.
