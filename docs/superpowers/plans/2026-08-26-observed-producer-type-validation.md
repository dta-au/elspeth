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

- [x] **Step 1: Write failing tests** — construct NodeInfo directly asserting defaults; build a tiny graph through the real builder with a csv source and assert `graph.get_node_info(<source>).observed_value_type is None` (csv not yet declaring). Follow the node-type-scoped-field guard pattern: if NodeInfo `__post_init__` guards sink-only fields, mirror a guard (`observed_value_type` non-None only on SOURCE nodes; `preserves_input_values` True only on TRANSFORM nodes) and test both rejections.
- [x] **Step 2: Run to verify failure** (`pytest tests/unit/core/dag/test_observed_producer_type_validation.py -x -q`).
- [x] **Step 3: Implement** flags with full doc-comments in house style (state the VALUE-preservation semantics, the presence-contract contrast with `passes_through_input`, and the fail-closed default), NodeInfo fields + guards, builder threading.
- [x] **Step 4: Run tests to green.**
- [x] **Step 5: Commit** (pathspec-only): `git commit -m "feat(contracts): preserves_input_values + observed_value_type plugin facts threaded onto NodeInfo (elspeth-e6e552ce34)"`.

### Task 2: Resolution arms in guarantees.py

**Files:**
- Modify: `src/elspeth/core/dag/guarantees.py` (`_resolve_guaranteed_field_type_uncached`)
- Test: `tests/unit/core/dag/test_observed_producer_type_validation.py`

**Interfaces:**
- Consumes: Task 1's NodeInfo fields.
- Produces: `resolve_guaranteed_field_type` returns `ResolvedGuaranteeType("str", {source_id})` for an observed source's guaranteed field when the source NodeInfo carries `observed_value_type="str"`; recurses through an undeclaring pass-through transform iff `preserves_input_values`.

- [x] **Step 1: Write failing tests** (hand-built graphs per existing `test_graph_validation.py` style):
  - observed source (guaranteed_fields={id}, observed_value_type="str") → resolution of `id` at source = str.
  - resolution of a field NOT in the source's guaranteed set = None (abstention pin).
  - undeclaring pass-through with `preserves_input_values=True` between source and query point → resolves str; with False → None (existing behavior pin).
  - two coalesce branches resolving to different types → None (unanimity pin, mutation kill).
- [x] **Step 2: Verify failures.**
- [x] **Step 3: Implement** both arms: source arm placed after the own-declaration check, before `recurses` (SOURCE nodes don't recurse); condition `node_info.node_type is NodeType.SOURCE and config is not None and config.is_observed and node_info.observed_value_type is not None and field_name in (config.guaranteed_fields or ())`. Pass-through arm: at the :599 abstention, `if not node_info.preserves_input_values: return None` (recursion continues otherwise). Update BOTH docstrings (resolve + module) to document the arms and abstention edges.
- [x] **Step 4: Green.**
- [x] **Step 5: Commit**: `feat(dag): resolve guaranteed field types through value-preserving pass-throughs to structural source types (elspeth-e6e552ce34)`.

### Task 3: csv + llm + passthrough plugin declarations (hash discipline)

**Files:**
- Modify: `src/elspeth/plugins/sources/csv_source.py` (`observed_value_type = "str"` + source_file_hash recompute)
- Modify: `src/elspeth/plugins/transforms/llm/transform.py` (:1230 vicinity, `preserves_input_values = True` + hash recompute)
- Modify: `src/elspeth/plugins/transforms/passthrough.py` (`preserves_input_values = True` + hash recompute)
- Test: `tests/unit/core/dag/test_observed_producer_type_validation.py` + extend `tests/invariants/test_pass_through_invariants.py` if the ADR-009 probe harness accommodates a value-equality assertion cheaply.

- [x] **Step 1: Failing test**: builder-built graph with real csv source config (observed + guaranteed_fields) asserts `observed_value_type == "str"` on the source NodeInfo; llm-bearing graph asserts `preserves_input_values` True on the llm node.
- [x] **Step 2: Verify failure.**
- [x] **Step 3: Declare flags**; run `ruff format` on the three plugin files; recompute each `source_file_hash` with `python -c` against `scripts/cicd/plugin_hash.py::compute_source_file_hash`; assert strict equality in a throwaway check (the gate is CI-only).
- [x] **Step 4: Green.** If the invariants harness extension is feasible, add: for every registered transform declaring `preserves_input_values`, probe process() with a synthetic row and assert forwarded key values are `==` input values (mirror the ADR-009 probe pattern). If the harness cannot host llm (provider dependency), probe the hostable declarers and leave llm pinned by the collision-validator argument documented in the flag's comment.
- [x] **Step 5: Commit**: `feat(plugins): csv observed cells are structurally str; llm/passthrough forward values unchanged (elspeth-e6e552ce34)`.

### Task 4: The validation pass

**Files:**
- Modify: `src/elspeth/core/dag/schema_validation.py` (new `validate_observed_producer_declared_types`, wired last in `validate_edge_compatibility`)
- Test: `tests/unit/core/dag/test_observed_producer_type_validation.py`

**Interfaces:**
- Consumes: `resolve_guaranteed_field_type` (Task 2), `resolved_guarantee_type_mismatch` (`contracts/data.py:437`).
- Produces: `EdgeContractError` with headline `Observed-schema type violation: edge '<from>' → '<to>'`, structured `compatibility_result=CompatibilityResult(compatible=False, type_mismatches=((field, expected, actual),))`.

- [x] **Step 1: Failing tests**:
  - REPRO SHAPE (the load-bearing case): builder-built csv(observed, guaranteed [id, question]) → gate fork → two observed `preserves_input_values` transforms → coalesce(union, require_all, observed) → consumer declaring `id: int` (flexible) → build raises EdgeContractError naming `id`, expected int, arriving str, declared_by the source.
  - same graph, consumer `id: str` → builds green (monotonicity).
  - same graph, consumer `answer_a: int` where answer_a is llm-introduced → builds green (abstention — not provable).
  - rewriting pass-through (flag False) in path → green.
  - source `mode: fixed` declaring `id: int` → green (declared arm + source coercion).
  - non-strict consumer (schema mode with lax model, if constructible) → pin whatever `_types_compatible(consumer_strict=False)` yields for str→int; assert explicitly.
  - dual-violation graph (this AND an earlier check) reports the EARLIER error (ordering pin).
- [x] **Step 2: Verify failures.**
- [x] **Step 3: Implement** the pass mirroring `validate_forgiven_field_ancestor_types`'s loop/caching structure (shared `type_cache`, `schema_cache`); scope condition: effective producer schema None OR observed; consumer has model_fields; skip COALESCE/ROW_UNION consumers and DIVERT edges. Iterate `consumer_schema.model_fields` required entries. Remedy text: (1) align consumer declared type; (2) insert `type_coerce` BEFORE consumer converting the field and declaring it in the transform's `schema.fields`; (3) if the source column genuinely holds the declared type, declare it on the source schema (`mode: fixed`/`flexible` with `fields: [<field>: <type>]`) so the source coerces at ingest. Wire into `validate_edge_compatibility` after `validate_forgiven_field_ancestor_types` with the house ordering comment.
- [x] **Step 4: Green**, including the whole existing dag test package: `pytest tests/unit/core/dag -q` and `pytest tests/integration/core/dag -q` (vocabulary-adjacent rule from recent-code-hints META-39: any pass that can change build verdicts must sweep the corpus suites; a corpus case that now fails to build is an adjudication STOP — do not reshape fixtures, surface to John).
- [x] **Step 5: Parity adjudication**: `python scripts/cicd/runtime_rejection_parity.py --write`; adjudicate the seeded entry (mirrored Stage-1 counterpart or documented unmirrored rationale). Commit both together: `feat(dag): reject provably-wrong consumer declared types across observed producers at build (elspeth-e6e552ce34)`.

### Task 5: Composer feedback surfaces

**Files:**
- Modify: `src/elspeth/web/composer/tools/generation.py` (`_VALIDATION_ERROR_PATTERNS` — new entry, specific-first ordering)
- Modify: `src/elspeth/web/frontend/src/lib/validationHumaniser.ts` (headline arm)
- Test: `tests/unit/web/composer/` existing pattern-coverage test (locate: grep `_VALIDATION_ERROR_PATTERNS` in tests); frontend `tsc`/vitest if the humaniser has a spec file.

**WITHDRAWN 2026-08-26 — the task rested on a false premise; implementing it
would have shipped code that can never execute.** Both surfaces were to be
keyed on the raise site's `Observed-schema type violation: edge …` headline.
That headline never reaches either surface. On the composer path,
`web/execution/_validation_diagnostics.py::_format_edge_contract_message`
REBUILDS the message from `exc.compatibility_result` (a generic "Edge contract
violation between producer node … Type mismatches: …"), and both handlers in
`web/execution/_validation_runtime.py` (:371 build-time, :492
edge-compatibility) construct their `ValidationError` with a hardcoded
`error_code=None`. So the planner sees neither the headline nor a code, and a
`_VALIDATION_ERROR_PATTERNS` entry keyed on either is dead. The raise-site
prose survives only as `failed_check.detail = str(exc)`. Keying the pattern on
the REBUILT text was rejected too: that string is generic across every edge
failure, so observed-schema-specific advice attached to it would be wrong for
ordinary type mismatches.

The planner is not left without advice, because elspeth-24's Stage-1 arm
`declared_input_type_mismatch_against_source_schema` (edeb498b3) blocks this
class at preview, BEFORE Stage-2 build, and carries the same three remedies.
The gap between the two arms was enumerated rather than assumed, and is empty:
`observed_value_type` is declared by `csv_source` alone (`BaseSource` defaults
it to `None`), so the engine pass can only fire where the chain bottoms out at
an observed CSV — exactly Stage-1's scope. The one structural asymmetry worth
checking was that Stage-1 reads the AUTHORED `schema:` block
(`get_raw_schema_config(options)`) while the engine pass reads the consumer's
`input_schema.model_fields`; enumerating every builtin transform and sink
found ZERO plugins declaring a code-level `input_schema`, so both arms read
the same authored declaration and Stage-1 simply reaches it first.

Division of labor therefore holds as agreed with elspeth-24: the composer path
is served by their Stage-1 arm; the engine's rich prose serves the YAML/CLI
operator path, where `str(exc)` reaches the operator intact and is pinned by
`tests/unit/core/dag/test_observed_producer_type_validation.py`.

Threading a real `error_code` through the `EdgeContractError` seam would close
the theoretical gap, but `error_code=None` is shared by EVERY `EdgeContractError`
path — that is a parity sweep across both handlers plus every consumer of the
code, and it is not justified by a gap measured as empty. Not filed as a
ticket for that reason; revisit if a second `observed_value_type` declarer ever
lands, which would widen the engine pass beyond Stage-1's CSV scope.

### Task 6: Gate + docs + close

- [x] **Step 1**: `docs/agents/recent-code-hints.md` entries — landed across 50734a515 and b2c446af3. Three, not one: the frozen-corpus provenance trap (a plugin edit moves manifest bytes), the two contract facts plus the presence-vs-value rule and both fake failure modes, and the `input_schema` census trap (`__init_subclass__` moves it to `_declared_input_schema`, so a naive probe measures nothing). The headline-pattern coupling item is void — see Task 5's withdrawal.
- [x] **Step 2**: Lint corpus compared as SETS, path-normalized, base b825ac4ad vs HEAD — not as counts, which would have hidden it. 3210 → 3211. Every apparent delta is a line-number shift; the one genuine new finding (`generation.py` R6 `except ValueError:`) belongs to the composer arm and was removed by 1493fc69d. This arm adds zero.
- [x] **Step 3**: `wardline` run — exit 1, NOT 0, and the plan's "→ exit 0" expectation was wrong for this tree. 6 active ERROR, all pre-existing and tracked as elspeth-5a322bd5ca; 129 recognized boundaries so `--fail-on-inert` passed. This arm adds none. (I first mis-attributed one to the composer arm by inferring from `git log -1 -- <file>`; disproved from wardline's own archived scans — the function had moved and re-fingerprinted.)
- [x] **Step 4**: Full suite in a git WORKTREE, background — an archive export is the WRONG harness here: ~50 tests shell out to `git ls-files`/`check-ignore` and cannot run without `.git`, inflating an export run to 72 failures against a worktree's 18. 18 failed at 237264a28, set-differenced against a worktree at base: 8 pre-existing (elspeth-9c1595f3f1), 10 new, of which exactly 2 were mine (fixed in b2c446af3) and 8 belong to the llm structured-output lane (now elspeth-8d31b9fabc). Final targeted run at b2c446af3: 8631 passed, 1 failed, that one being the llm lane's.
- [x] **Step 5**: Filigree — `root_cause` set; design, non-scope, and engine-arm verification commented, including a correction to the composer arm's attribution (2 of its 10 "pre-existing" were mine, introduced before its comparison anchor). Follow-ups filed: elspeth-a8b438f534 (audit remaining pass-through plugins), elspeth-98b238bb3c (Stage-1 mirror; ratchet-back for UNMIRRORED_CEILING 13→14), elspeth-8d31b9fabc (unowned llm-lane reds). NOT closed: the ticket is another lane's claim and sits at `verifying` with both arms' evidence recorded.
- [x] **Step 6**: Reported to John, and to the composer lane directly.

## Self-Review Notes

- Spec coverage: ticket mechanism (1) — edges into coalesce skipped — is deliberately NOT reopened; the consumer-side pass covers the defect on the coalesce→consumer edge, where the death occurs. Mechanism (2) — observed bypass — is what the new pass closes. Ruling satisfied via preview Stage 2 (graph build) + pattern catalogue.
- The `fan_out` degenerate-gate and all non-schema findings from the elspeth-24 handover stay HELD for the systems-review reconciliation — out of scope here.
- Type consistency: `preserves_input_values` / `observed_value_type` names used identically in Tasks 1–4.
- No placeholders: each step names exact files/conditions; test cases are enumerated with expected verdicts.
