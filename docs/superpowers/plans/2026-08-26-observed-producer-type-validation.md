# Observed-Producer Declared-Type Validation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make a provably-wrong consumer type declaration downstream of an observed producer fail at BUILD/PREVIEW time (elspeth-e6e552ce34, P1; John's ruling 2026-08-26) so the composer planner self-repairs via ordinary validation feedback.

**Architecture:** Two new sound arms in `resolve_guaranteed_field_type` (a structural value-type fact at observed sources, and recursion through pass-through transforms that promise forwarded values unchanged), plus a new final phase-2 pass in `schema_validation.py` that type-checks a typed consumer's required fields across observed/dynamic producers using the existing `resolved_guarantee_type_mismatch` conservatism. Two new plugin contract ClassVars threaded through NodeInfo by the builder. No composer-side special path — the raise surfaces through preview Stage 2 (runtime preflight builds the graph via `builder.py:1644 → graph.validate_edge_compatibility()`) and run submission.

**Tech Stack:** Python 3.12, pydantic v2, pytest, ELSPETH DAG/contracts layers.

**Spec:** Filigree ticket `elspeth-e6e552ce34` (description carries mechanism + ruling); live-repro dossier at `/tmp/claude-1000/-home-john-elspeth/7203bac6-a5cf-4965-baf9-aedf60e077f5/scratchpad/dossier.md`.

## Global Constraints

- Composer invariants (AGENTS.md): no server-side authoring; no tutorial-special paths. This change is validation/rejection only — allowed.
- Monotonicity invariant (elspeth-85e8afa2f5): the new pass may only reproduce verdicts that fully declaring the field on the producing branches would already produce; declaring MORE must never flip a green build red or a red build green.
- Every new `raise` under `src/elspeth/core/dag/` needs a SAME-COMMIT parity adjudication: `python scripts/cicd/runtime_rejection_parity.py --write`, then adjudicate the seeded entry (recent-code-hints 2026-08-24 trap c).
- Editing a builtin plugin file requires recomputing `source_file_hash` via `scripts/cicd/plugin_hash.py::compute_source_file_hash`, AFTER ruff format; compare strictly (`Cls.source_file_hash == compute_source_file_hash(path)`), never substring (recent-code-hints 2026-08-20).
- No new serialised config keys (ClassVars only) — canonical topology hash corpus must not move.
- Shared checkout: stage by pathspec only; sibling WIP exists in `src/elspeth/plugins/llm/config_validation.py` and `src/elspeth/plugins/transforms/llm/providers/gateway.py` (do NOT stage those).
- New whole-tree conventions land in `docs/agents/recent-code-hints.md` in the same commit.
- Full `pytest tests/` runs as a BACKGROUND job at the gate, never inline (John ruling 08-25/08-26).

## Design summary (why each arm is sound)

Live repro topology: csv source (`schema: {mode: observed, guaranteed_fields: [id, question]}`) → gate `fan_out` (fork_to branch_a/branch_b) → two `llm` transforms (`schema: {mode: observed}`, `passes_through_input=True`) → coalesce `merge_branches` (union, require_all, observed) → `field_mapper tidy_columns` declaring `id: int` (flexible ⇒ strict input model). Today: coalesce effective producer schema is None/observed → `validate_single_edge` Rule-1/observed bypass; `validate_forgiven_field_ancestor_types` skips observed producers; every row dies at runtime preflight.

1. **Structural source arm.** `csv_source.py` module docstring: "observed schemas preserve parsed cells as strings." New `BaseSource`/`SourceProtocol` ClassVar `observed_value_type: str | None = None`; csv declares `"str"`. Resolution reaching a SOURCE whose config `is_observed` answers that type ONLY for fields in the source's own `guaranteed_fields` — for any other field it abstains. This makes over-recursion self-limiting: a field introduced mid-path (e.g. an llm `response_field`) is not in the source's guarantee set, so the whole resolution abstains rather than mis-attributing.
2. **Value-preserving pass-through arm.** `passes_through_input` is a PRESENCE contract only. New `BaseTransform`/`TransformProtocol` flag `preserves_input_values: bool = False`: True iff process() never changes the VALUE of any field present on the input row (adding new fields is fine). The resolution walk's abstention for undeclaring pass-throughs (guarantees.py:599) recurses instead when the flag is True. Declarers in this change: `llm` (adds response fields, never rewrites inputs; output-field collisions with inputs are already build-rejected by `validate_transform_output_field_collisions`) and `passthrough`. `type_coerce`, `value_transform`, `truncate` etc. stay False → abstention → no behavior change.
3. **New final pass** `validate_observed_producer_declared_types(graph)`: for each non-DIVERT edge whose consumer is not COALESCE/ROW_UNION, whose consumer input schema has model_fields, and whose EFFECTIVE producer schema is None or observed: for each consumer required field, `resolve_guaranteed_field_type` from the producer; on a non-None resolution, `resolved_guarantee_type_mismatch(resolved.field_type, annotation, consumer_strict=...)`; raise `EdgeContractError` on conflict. Runs last in `validate_edge_compatibility` (pre-existing-error ordering discipline). Certain-death argument: required + provable arriving type conflict ⇒ every row carrying the field dies at consumer preflight, every row lacking it dies on required-missing.

Error message headline must be DISTINCT from existing "Schema contract violation:" families (pattern lists match raw text in list order; `_VALIDATION_ERROR_PATTERNS` in `web/composer/tools/generation.py` and `frontend/src/lib/validationHumaniser.ts` both key on headline). Headline: `Observed-schema type violation: edge '<from>' → '<to>'`. Remedy text must lead with align-the-consumer-type; a coercion remedy must name `type_coerce` inserted BEFORE the consumer with the target declared in its `schema.fields` (mirror the forgiven-pass remedy block, which is truth-pinned).

---

### Task 1: Contract flags + NodeInfo threading

**Files:**
- Modify: `src/elspeth/plugins/infrastructure/base.py` (BaseTransform ~:458 after `forwards_input_fields`; BaseSource ~:2091)
- Modify: `src/elspeth/contracts/plugin_protocols.py` (TransformProtocol ~:402; SourceProtocol ~:144)
- Modify: `src/elspeth/core/dag/models.py` (NodeInfo, after `declared_string_input_fields`)
- Modify: `src/elspeth/core/dag/builder.py` (3 transform NodeInfo sites :422/:473/:735 + the source NodeInfo site — locate with `grep -n "NodeType.SOURCE" builder.py`)
- Test: `tests/unit/core/dag/test_observed_producer_type_validation.py` (new)

**Interfaces:**
- Produces: `BaseTransform.preserves_input_values: bool = False`; `BaseSource.observed_value_type: str | None = None`; `NodeInfo.preserves_input_values: bool = False`; `NodeInfo.observed_value_type: str | None = None`; builder copies both from plugin instances.

- [ ] **Step 1: Write failing tests** — construct NodeInfo directly asserting defaults; build a tiny graph through the real builder with a csv source and assert `graph.get_node_info(<source>).observed_value_type is None` (csv not yet declaring). Follow the node-type-scoped-field guard pattern: if NodeInfo `__post_init__` guards sink-only fields, mirror a guard (`observed_value_type` non-None only on SOURCE nodes; `preserves_input_values` True only on TRANSFORM nodes) and test both rejections.
- [ ] **Step 2: Run to verify failure** (`pytest tests/unit/core/dag/test_observed_producer_type_validation.py -x -q`).
- [ ] **Step 3: Implement** flags with full doc-comments in house style (state the VALUE-preservation semantics, the presence-contract contrast with `passes_through_input`, and the fail-closed default), NodeInfo fields + guards, builder threading.
- [ ] **Step 4: Run tests to green.**
- [ ] **Step 5: Commit** (pathspec-only): `git commit -m "feat(contracts): preserves_input_values + observed_value_type plugin facts threaded onto NodeInfo (elspeth-e6e552ce34)"`.

### Task 2: Resolution arms in guarantees.py

**Files:**
- Modify: `src/elspeth/core/dag/guarantees.py` (`_resolve_guaranteed_field_type_uncached`)
- Test: `tests/unit/core/dag/test_observed_producer_type_validation.py`

**Interfaces:**
- Consumes: Task 1's NodeInfo fields.
- Produces: `resolve_guaranteed_field_type` returns `ResolvedGuaranteeType("str", {source_id})` for an observed source's guaranteed field when the source NodeInfo carries `observed_value_type="str"`; recurses through an undeclaring pass-through transform iff `preserves_input_values`.

- [ ] **Step 1: Write failing tests** (hand-built graphs per existing `test_graph_validation.py` style):
  - observed source (guaranteed_fields={id}, observed_value_type="str") → resolution of `id` at source = str.
  - resolution of a field NOT in the source's guaranteed set = None (abstention pin).
  - undeclaring pass-through with `preserves_input_values=True` between source and query point → resolves str; with False → None (existing behavior pin).
  - two coalesce branches resolving to different types → None (unanimity pin, mutation kill).
- [ ] **Step 2: Verify failures.**
- [ ] **Step 3: Implement** both arms: source arm placed after the own-declaration check, before `recurses` (SOURCE nodes don't recurse); condition `node_info.node_type is NodeType.SOURCE and config is not None and config.is_observed and node_info.observed_value_type is not None and field_name in (config.guaranteed_fields or ())`. Pass-through arm: at the :599 abstention, `if not node_info.preserves_input_values: return None` (recursion continues otherwise). Update BOTH docstrings (resolve + module) to document the arms and abstention edges.
- [ ] **Step 4: Green.**
- [ ] **Step 5: Commit**: `feat(dag): resolve guaranteed field types through value-preserving pass-throughs to structural source types (elspeth-e6e552ce34)`.

### Task 3: csv + llm + passthrough plugin declarations (hash discipline)

**Files:**
- Modify: `src/elspeth/plugins/sources/csv_source.py` (`observed_value_type = "str"` + source_file_hash recompute)
- Modify: `src/elspeth/plugins/transforms/llm/transform.py` (:1230 vicinity, `preserves_input_values = True` + hash recompute)
- Modify: `src/elspeth/plugins/transforms/passthrough.py` (`preserves_input_values = True` + hash recompute)
- Test: `tests/unit/core/dag/test_observed_producer_type_validation.py` + extend `tests/invariants/test_pass_through_invariants.py` if the ADR-009 probe harness accommodates a value-equality assertion cheaply.

- [ ] **Step 1: Failing test**: builder-built graph with real csv source config (observed + guaranteed_fields) asserts `observed_value_type == "str"` on the source NodeInfo; llm-bearing graph asserts `preserves_input_values` True on the llm node.
- [ ] **Step 2: Verify failure.**
- [ ] **Step 3: Declare flags**; run `ruff format` on the three plugin files; recompute each `source_file_hash` with `python -c` against `scripts/cicd/plugin_hash.py::compute_source_file_hash`; assert strict equality in a throwaway check (the gate is CI-only).
- [ ] **Step 4: Green.** If the invariants harness extension is feasible, add: for every registered transform declaring `preserves_input_values`, probe process() with a synthetic row and assert forwarded key values are `==` input values (mirror the ADR-009 probe pattern). If the harness cannot host llm (provider dependency), probe the hostable declarers and leave llm pinned by the collision-validator argument documented in the flag's comment.
- [ ] **Step 5: Commit**: `feat(plugins): csv observed cells are structurally str; llm/passthrough forward values unchanged (elspeth-e6e552ce34)`.

### Task 4: The validation pass

**Files:**
- Modify: `src/elspeth/core/dag/schema_validation.py` (new `validate_observed_producer_declared_types`, wired last in `validate_edge_compatibility`)
- Test: `tests/unit/core/dag/test_observed_producer_type_validation.py`

**Interfaces:**
- Consumes: `resolve_guaranteed_field_type` (Task 2), `resolved_guarantee_type_mismatch` (`contracts/data.py:437`).
- Produces: `EdgeContractError` with headline `Observed-schema type violation: edge '<from>' → '<to>'`, structured `compatibility_result=CompatibilityResult(compatible=False, type_mismatches=((field, expected, actual),))`.

- [ ] **Step 1: Failing tests**:
  - REPRO SHAPE (the load-bearing case): builder-built csv(observed, guaranteed [id, question]) → gate fork → two observed `preserves_input_values` transforms → coalesce(union, require_all, observed) → consumer declaring `id: int` (flexible) → build raises EdgeContractError naming `id`, expected int, arriving str, declared_by the source.
  - same graph, consumer `id: str` → builds green (monotonicity).
  - same graph, consumer `answer_a: int` where answer_a is llm-introduced → builds green (abstention — not provable).
  - rewriting pass-through (flag False) in path → green.
  - source `mode: fixed` declaring `id: int` → green (declared arm + source coercion).
  - non-strict consumer (schema mode with lax model, if constructible) → pin whatever `_types_compatible(consumer_strict=False)` yields for str→int; assert explicitly.
  - dual-violation graph (this AND an earlier check) reports the EARLIER error (ordering pin).
- [ ] **Step 2: Verify failures.**
- [ ] **Step 3: Implement** the pass mirroring `validate_forgiven_field_ancestor_types`'s loop/caching structure (shared `type_cache`, `schema_cache`); scope condition: effective producer schema None OR observed; consumer has model_fields; skip COALESCE/ROW_UNION consumers and DIVERT edges. Iterate `consumer_schema.model_fields` required entries. Remedy text: (1) align consumer declared type; (2) insert `type_coerce` BEFORE consumer converting the field and declaring it in the transform's `schema.fields`; (3) if the source column genuinely holds the declared type, declare it on the source schema (`mode: fixed`/`flexible` with `fields: [<field>: <type>]`) so the source coerces at ingest. Wire into `validate_edge_compatibility` after `validate_forgiven_field_ancestor_types` with the house ordering comment.
- [ ] **Step 4: Green**, including the whole existing dag test package: `pytest tests/unit/core/dag -q` and `pytest tests/integration/core/dag -q` (vocabulary-adjacent rule from recent-code-hints META-39: any pass that can change build verdicts must sweep the corpus suites; a corpus case that now fails to build is an adjudication STOP — do not reshape fixtures, surface to John).
- [ ] **Step 5: Parity adjudication**: `python scripts/cicd/runtime_rejection_parity.py --write`; adjudicate the seeded entry (mirrored Stage-1 counterpart or documented unmirrored rationale). Commit both together: `feat(dag): reject provably-wrong consumer declared types across observed producers at build (elspeth-e6e552ce34)`.

### Task 5: Composer feedback surfaces

**Files:**
- Modify: `src/elspeth/web/composer/tools/generation.py` (`_VALIDATION_ERROR_PATTERNS` — new entry, specific-first ordering)
- Modify: `src/elspeth/web/frontend/src/lib/validationHumaniser.ts` (headline arm)
- Test: `tests/unit/web/composer/` existing pattern-coverage test (locate: grep `_VALIDATION_ERROR_PATTERNS` in tests); frontend `tsc`/vitest if the humaniser has a spec file.

- [ ] **Step 1: Failing test**: pattern list resolves the new headline to an entry whose `explanation`/`suggested_fix` name the three remedies; assert list-order specificity (no earlier pattern shadows it).
- [ ] **Step 2: Implement** both surfaces. Advice text imports/mirrors the raise-site constants per the two-surfaces trap (2026-08-21 entry): single ownership — put the remedy prose in a `schema_validation.py`-adjacent constant only if the import direction is legal; otherwise duplicate with a cross-reference comment and a test asserting the two stay in sync.` constants in generation.py with sync tests asserting presence in both the catalog entry and the raise-site message.*
- [ ] **Step 3: Integration test**: runtime preflight (Stage 2) on a repro-shaped composition state reports the error with actionable suggestion — extend the existing preflight formatter tests in `tests/unit/web/execution/` (grep `EdgeContractError` there for the harness).
- [ ] **Step 4: Green + commit**: `feat(composer): planner-facing advice for observed-schema type violations (elspeth-e6e552ce34)`.

### Task 6: Gate + docs + close

- [ ] **Step 1**: `docs/agents/recent-code-hints.md` entry (same commit as Task 4 if not already landed — REQUIRED; otherwise here): the two new contract flags, the abstention edges, the headline-pattern coupling, the plugin-hash recomputes.
- [ ] **Step 2**: `elspeth-lints check --rules all --root src/elspeth` corpus compare (count findings before/after; delta must be explained — expect zero new).
- [ ] **Step 3**: `wardline scan . --fail-on ERROR --fail-on-inert --trust-pack scripts.wardline_pack --allow-custom-packs --local-only` → exit 0.
- [ ] **Step 4**: Full `pytest tests/` as a background job; on green, verify no sibling breakage attributable to this change (xdist zero-collection check: the "N passed" line must exist).
- [ ] **Step 5**: Filigree: `issue_update` root_cause + severity, close `elspeth-e6e552ce34` with `close_commit`; comment the design (two arms + pass + surfaces) and the deliberate non-scope (other pass-through plugins unaudited → follow-up ticket for the flag audit).
- [ ] **Step 6**: Report to John: fix summary + preview now failing for the repro shape + what remains held for the reconciliation.

## Self-Review Notes

- Spec coverage: ticket mechanism (1) — edges into coalesce skipped — is deliberately NOT reopened; the consumer-side pass covers the defect on the coalesce→consumer edge, where the death occurs. Mechanism (2) — observed bypass — is what the new pass closes. Ruling satisfied via preview Stage 2 (graph build) + pattern catalogue.
- The `fan_out` degenerate-gate and all non-schema findings from the elspeth-24 handover stay HELD for the systems-review reconciliation — out of scope here.
- Type consistency: `preserves_input_values` / `observed_value_type` names used identically in Tasks 1–4.
- No placeholders: each step names exact files/conditions; test cases are enumerated with expected verdicts.
