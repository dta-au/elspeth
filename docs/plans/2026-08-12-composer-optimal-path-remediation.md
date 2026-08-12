# Composer Optimal-Path Remediation Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Make simple, fully specified pipelines converge through the generic Composer path with work proportional to the information still missing: zero provider calls for a complete registered pattern, otherwise at most one parallel discovery turn plus one proposal turn, with no redundant state reads or repair churn.

**Architecture:** Treat the initial planner envelope as an authoritative information snapshot, derive a request-specific read-only tool palette from the facts it already contains, and return compact just-in-time contracts only for plugins the planner selects. On guided surfaces, derive a proposal schema from the current operation's mutation authority: an initial transform plan authors topology/routing only, while a correction may author only its explicitly selected target. The server materializes reviewed source/output authority before the existing canonical candidate gate. Registered patterns remain optional accelerators owned by the recipe registry; they must never be selected by tutorial identity, exact tutorial copy, provider identity, or a hard-coded tutorial graph. Persist a bounded, value-free attempt trail so efficiency and repair behavior are auditable.

**Tech Stack:** Python 3.12, Pydantic v2, FastAPI, SQLAlchemy, pytest, TypeScript/React, Vitest, Playwright, Filigree, Loomweave.

**Prerequisites:** Work from a dedicated worktree and verify `elspeth.__file__` resolves inside it. Reconcile this plan with active audit-cohort work (`elspeth-90231248dc`) before changing planner audit persistence. Existing tracker items `elspeth-5df45ca8a7`, `elspeth-73908a3df0`, `elspeth-b54dfa6568`, and `elspeth-de3638b6ac` overlap Tasks 2, 3, 6, and 7; update those rather than creating duplicate issues. Read `docs/agents/recent-code-hints.md` immediately before implementation and run the whole-tree gates at closeout.

**Baseline verified while planning:** 44 focused planner/recipe tests passed (`364 deselected`) in the dedicated worktree with `PYTHONPATH` pinned to that worktree.

**Implementation status (2026-08-12):** Implemented as generic planner,
authority, recipe, projection, audit, and acceptance slices. The ordinary
unregistered guided fixture uses one parallel discovery response plus one
accepted proposal response. A complete registered scrape/project request uses
zero provider calls. Recipe matching is conservative: incomplete, negated,
conflicting, or only partially parsed authority falls back to the planner.
Durable planner evidence records physical provider calls separately from
semantic response attempts, including retry gaps and ordinal-reset cohorts.

**Amendment (2026-08-13): Task 5's intent matching EXCISED.** The operator
ruled that prose-intent → zero-provider recipe synthesis is the banned
`recipe_match` anti-pattern (`.superpowers/sdd/INTENT-guided-stepped-orchestrator.md`
§6): the tutorial must converge because it is easy, through the composer LLM,
never through a deterministic server path keyed on user text. A multi-agent
review also confirmed three high-severity defects living entirely in the
matcher (comma-interjection negation fails open end-to-end; quadratic
authority parsing blocks the event loop ~37 s at the 64 KiB message cap;
unmodeled trailing instruction clauses silently dropped on the zero-call
route). Deleted: `match_registered_recipe_intent` and all prose-parsing
machinery in `recipes.py` (~1,200 lines), `recipe_intent_routing.py`,
`try_prepare_registered_recipe_plan` plus both `service.py` call sites, the
`no_tool_policy.py` recipe-envelope production, `GuidedPlannerConflict`
(recipe-route-only), and the harness's zero-call acceptance arm — zero
provider calls now FAILS the staging efficiency gate. Kept: the recipe
registry, slot validation, builders, `apply_recipe` (explicit invocation is
deliberate, not prose-guessed), the projection-conflict rule, and all of
Tasks 1–4 and 6–8. Tracked residue: elspeth-a0a830fc95 (guided mirror-edge
reconciliation, found by the same review), elspeth-9fd227b528 (low findings).

---

## Constraints and success criteria

The tutorial is a regression fixture, not a routing input. Production code introduced by this plan must not branch on `TUTORIAL_PROFILE`, tutorial prompt constants, the tutorial session title, the tutorial URLs, or the `url`/`summary` field names. Surface checks are permitted only where disclosure or authority genuinely differs between freeform, guided, and restricted planners.

The generic acceptance fixture should use different names, for example `document_uri -> web_scrape -> llm(response_field="abstract") -> field_mapper(retained_fields=["document_uri", "abstract"]) -> json`, so a passing test proves the implementation did not memorize the tutorial.

| Case | Required outcome |
| --- | --- |
| Complete registered pattern with all required slots proven | 0 provider calls; candidate still passes `prepare_pipeline_plan` and runtime-equivalent preflight |
| Simple unregistered linear pipeline | At most 2 provider calls: one parallel schema/assistance batch, then one terminal proposal |
| Initial envelope already supplies state/catalog fact | No state/list rediscovery for that fact |
| First terminal proposal for the generic linear fixture | Accepted with 0 repair turns |
| Explicit retained-field set conflicts with reviewed sink requirements | Typed conflict before candidate sealing (and before provider dispatch on a deterministic pattern); never silently widen or narrow the field set |
| Every planner attempt | Durable closed evidence for phase, selected tools, information gain, rejection codes, usage, cost, and latency; no raw prompt, option value, row, path, secret, or provider reasoning |

The run that motivated this plan used 7 provider calls, 188.171 seconds of model latency, 340,973 tokens, and $0.712754. Three redundant state-discovery calls alone consumed 109.566 seconds, 153,774 tokens, and about $0.314. These numbers are diagnosis evidence, not hard-coded test thresholds.

## Task 1: Lock the generic regression and request-size budgets

**Files:**

- Modify: `tests/unit/web/composer/test_pipeline_planner.py`
- Modify: `tests/unit/web/composer/test_planner_authoring_aids.py`
- Add: `tests/integration/web/composer/guided/test_generic_linear_optimal_path.py`

**Step 1: Add a failing information-reuse regression.**

Script a planner that first requests schemas for three arbitrary selected plugins, then emits a valid linear proposal. Assert that the first request carries an information manifest marking current state and catalog selection as supplied, and that no state/list tool capable only of repeating those facts is advertised.

```python
assert payload["information_manifest"]["supplied"]["pipeline_state"] == "current_projection"
assert payload["information_manifest"]["supplied"]["plugin_selection"] == "policy_snapshot"
assert "get_pipeline_state" not in declared_names
assert "list_transforms" not in declared_names
```

**Step 2: Add a failing two-call convergence regression.**

Use the generic `document_uri`/`abstract` topology, not tutorial names. The first scripted response makes one parallel batch of `get_plugin_schema` calls; the second emits the terminal proposal. Assert exactly two LLM calls, one discovery phase, zero repair phases, and an accepted proposal.

**Step 3: Add deterministic byte-budget tests.**

Measure canonical UTF-8 bytes, not provider-specific token counts. Pin separate budgets for fixed request scaffolding and selected-plugin contract evidence so large user state does not get mistaken for framework bloat. Initial targets:

```python
assert canonical_size(discovery_digest) <= 24 * 1024
assert canonical_size(fixed_planner_scaffolding) <= 96 * 1024
assert canonical_size(selected_contracts) <= 48 * 1024
```

`fixed_planner_scaffolding` excludes `intent`, `conversation_context`, `current_state`, and reviewed user facts. `selected_contracts` is the three-plugin generic fixture.

**Step 4: Run the tests and confirm they fail for the intended reasons.**

```bash
PYTHONPATH=$PWD/src /home/john/elspeth/.venv/bin/python -m pytest \
  tests/unit/web/composer/test_pipeline_planner.py \
  tests/unit/web/composer/test_planner_authoring_aids.py \
  tests/integration/web/composer/guided/test_generic_linear_optimal_path.py \
  -q -n0
```

Expected before implementation: failures showing the missing information manifest, static full palette, oversized digest/contracts, or repair-prone full guided proposal.

**Definition of done:** The tests describe only generic information, authority, and topology properties; searching them for tutorial profile/copy/field names returns no matches.

## Task 2: Make planner context compact and genuinely just-in-time

**Files:**

- Modify: `src/elspeth/web/composer/planner_authoring_aids.py`
- Modify: `src/elspeth/web/composer/pipeline_planner.py`
- Modify: `src/elspeth/web/composer/skills/pipeline_capabilities.md`
- Modify: `tests/unit/web/composer/test_planner_authoring_aids.py`
- Modify: `tests/unit/web/composer/test_schema_contract_projection_boundaries.py`

**Step 1: Remove full `composer_hints` arrays from the all-plugin discovery digest.**

The current digest is about 65 KB across 51 entries; about 43.6 KB is duplicated `composer_hints`. Keep only bounded selection facts in the initial index: kind/name, short purpose, capability tags, public required option names, prohibition summary, and usable profile aliases. The chosen plugin's schema response already carries its hints.

**Step 2: Reuse the existing closed schema projection for planner discovery.**

Promote the contract projection behind `build_schema_contract_evidence` into one public helper that accepts an admitted `PluginSchemaInfo` and returns a bounded planner contract:

```python
@dataclass(frozen=True, slots=True)
class PlannerPluginContract:
    plugin_id: str
    schema_hash: str
    json_schema: Mapping[str, object]
    knob_schema: Mapping[str, object]
    composer_hints: tuple[str, ...]
```

Use it only in `_serialize_provider_discovery_result`; retain the full authoritative `ToolResult` in the invocation audit. Do not change the public catalog API or normal Composer tool response.

**Step 3: Fail closed on unsupported projection.**

Return a closed `schema_projection_unavailable` result and direct the planner to `get_plugin_assistance`; never fall back to dumping the unbounded full model. Add boundary tests for unknown schema keys, recursive depth/size limits, and public hints.

**Step 4: Update capability guidance.**

State that the initial digest is a complete selection index for the current policy snapshot, while detailed option contracts and hints arrive only for selected plugins. Remove the instruction to call `list_*` merely because a selected plugin is absent from a worked example.

**Step 5: Run focused tests.**

```bash
PYTHONPATH=$PWD/src /home/john/elspeth/.venv/bin/python -m pytest \
  tests/unit/web/composer/test_planner_authoring_aids.py \
  tests/unit/web/composer/test_schema_contract_projection_boundaries.py \
  tests/unit/web/composer/test_pipeline_planner.py -q -n0
```

Expected: all pass; the byte budgets from Task 1 are green.

**Definition of done:** The initial request no longer pays every plugin's detailed coaching cost, and selected schema results contain every validation-relevant public fact without duplicate full catalog payloads.

## Task 3: Make discovery information-aware instead of call-hash-aware

**Files:**

- Modify: `src/elspeth/web/composer/capability_skill.py`
- Modify: `src/elspeth/web/composer/pipeline_planner.py`
- Modify: `src/elspeth/web/composer/skills/pipeline_capabilities.md`
- Modify: `tests/unit/web/composer/test_capability_skill_identity.py`
- Modify: `tests/unit/web/composer/test_pipeline_planner.py`

**Step 1: Introduce owned information keys and a request policy.**

Use closed values such as `pipeline.current`, `catalog.selection`, `recipe.index`, `plugin.schema:<kind>/<name>`, `model.catalog`, `blob.metadata:<id>`, and `validation.code:<code>`. Build `PlannerInformationManifest` from the exact initial payload and `PlannerDiscoveryPolicy` from the gaps.

**Step 2: Generate a request-specific tool palette.**

Change `planner_tool_definitions()` to accept the policy and emit an ordered subset of the registered read-only tools followed by the unchanged terminal. `build_planner_capability_manifest()` must verify that the discovery names are an order-preserving subset of `PLANNER_DISCOVERY_TOOL_NAMES`, hash their exact definitions, and still require the canonical terminal schema. Terminal-only hatch behavior remains exact.

State tools must be omitted when the initial request already supplies the same disclosed state. On restricted surfaces, never advertise `set_pipeline_arguments` or a preview whose successful response is guaranteed to become `surface_projection_unavailable`.

**Step 3: Add semantic dominance guards.**

Before dispatch, map every call to the information keys it can provide. A request is `DISCOVERY_NO_GAIN` when all of its keys are already supplied or unavailable. A full state projection dominates source/node/output subprojections; the initial exact projection dominates later reads of the same disclosed revision. Keep the existing exact repetition guard as defense in depth.

One no-gain call receives bounded typed feedback and does not execute. A second no-gain call in the same repair round engages the existing hatch/terminal path. It still counts against provider-call and request budgets, so the guard cannot create an unbounded loop.

**Step 4: Replace late turn pressure with gap-based steering.**

The initial instruction should name the unresolved information classes. After the last useful discovery result, append: “All declared information gaps are closed; emit the terminal proposal now.” Retain the hard discovery-turn maximum solely as a safety cap.

**Step 5: Add tests for equivalence and surface policy.**

Cover `source -> full`, `full -> source`, initial-current-state -> any state reread, list calls dominated by the complete catalog index, a same-snapshot schema reread remaining dominated after rejection, a legal issue-specific assistance/explanation read that adds a new fact, and restricted disclosure/redaction.

**Step 6: Run focused tests.**

```bash
PYTHONPATH=$PWD/src /home/john/elspeth/.venv/bin/python -m pytest \
  tests/unit/web/composer/test_capability_skill_identity.py \
  tests/unit/web/composer/test_pipeline_planner.py -q -n0
```

Expected: all pass; the generic two-call fixture has one useful discovery turn and no no-gain dispatches.

**Definition of done:** Discovery is governed by missing information rather than syntactically distinct tool arguments. This closes `elspeth-5df45ca8a7` without increasing timeouts or discovery budgets.

## Task 4: Stop guided planners from re-authoring locked source/output authority

**Files:**

- Modify: `src/elspeth/web/composer/pipeline_planner.py`
- Modify: `src/elspeth/web/composer/guided/planning.py`
- Modify: `src/elspeth/web/composer/service.py`
- Modify: `tests/unit/web/composer/test_pipeline_planner.py`
- Modify: `tests/integration/web/composer/guided/test_pipeline_proposal_reference.py`
- Modify: `tests/integration/web/composer/guided/test_respond.py`

**Step 1: Define an authority-derived guided delta schema from the canonical schema.**

For an initial transform plan, the model should author `source_routes`, `nodes`, `edges`, `output_targets`, `metadata`, and deferred-intent claims. Reviewed plugins, options, blob/path bindings, required fields, and failure policies remain server-owned. For a selected correction, derive the writable fragment from `GuidedCorrectionTarget`/`GuidedRevisionAuthority` and expose only that target plus the routing needed to reconnect it. Derive every fragment from `canonical_set_pipeline_schema()` so it cannot drift into a second pipeline language.

**Step 2: Materialize the canonical candidate server-side.**

Add `materialize_guided_authorized_candidate(delta, authority, guided, current_state)` in `guided/planning.py`. It must resolve reviewed components by stable ID, apply only authority-permitted changes, reject unknown/duplicate references with closed codes, and call the existing `bind_guided_reviewed_components`/candidate finalizer before `prepare_pipeline_plan`.

**Step 3: Keep full-document authoring where authority is not locked.**

Freeform and guided-full continue to use the full canonical terminal. An initial guided transform request receives the topology delta; a selected correction receives its target-specific delta. The capability manifest hashes the selected terminal contract, and tests prove each delta materializes the same `CompositionState` as an equivalent authorized full candidate.

**Step 4: Add adversarial tests.**

Prove the provider cannot submit reviewed option extras, replace an unselected source/sink plugin, alter a storage binding, or evade required fields because those properties are absent from its schema. Add positive tests for an authorized source/output correction, then prove arbitrary linear, branching, coalesce, and multi-output topology still works.

**Step 5: Run guided tests.**

```bash
PYTHONPATH=$PWD/src /home/john/elspeth/.venv/bin/python -m pytest \
  tests/unit/web/composer/test_pipeline_planner.py \
  tests/integration/web/composer/guided/test_pipeline_proposal_reference.py \
  tests/integration/web/composer/guided/test_respond.py -q -n0
```

Expected: all pass; the generic linear fixture's first proposal cannot produce `locked_input_extras` by construction.

**Definition of done:** Provider-authored bytes contain only mutable guided decisions; canonical validation, custody, reviewed-fact hashing, and runtime preflight remain unchanged downstream.

## Task 5: Make registered patterns generic, registry-owned, and surface-neutral

**Files:**

- Modify: `src/elspeth/web/composer/recipes.py`
- Modify: `src/elspeth/web/composer/recipe_intent_routing.py`
- Modify: `src/elspeth/web/composer/service.py`
- Modify: `tests/unit/web/composer/test_recipes.py`
- Modify: `tests/unit/web/composer/test_recipe_intent_routing.py`
- Modify: `tests/unit/web/composer/test_service.py`

**Step 1: Move matching ownership into `RecipeSpec`.**

Add a recipe-owned matcher/intent declaration and have one `match_registered_recipe_intent(context)` iterate the registry. `RecipeIntentContext` carries surface-neutral facts: user text, reviewed component summaries, policy snapshot, and available server-owned bindings. Delete duplicated recipe names and slot literals from `recipe_intent_routing.py`; retain a compatibility wrapper only if callers need it.

**Step 2: Respect required versus optional slots.**

Only a missing `SlotSpec.required` slot blocks a match. Omit absent optional values so `validate_slots()` applies recipe defaults. This closes `elspeth-b54dfa6568` and is enforced for every recipe, not just fork/coalesce.

**Step 3: Generalize the scrape/LLM/cleanup recipe.**

Extract a `web-scrape-llm-project-jsonl` pattern with slots for `prompt_template`, `response_field`, `required_input_fields`, `retained_fields`, profile, HTTP identity, source format, and output binding. Reject `content`/`content_fingerprint` in retained fields and require the response field to be retained. Implement the existing rating recipe as a backwards-compatible specialization of this builder rather than a separate graph.

Treat semantic slots (`prompt_template`, `response_field`, and exact `retained_fields`) as required for the generic pattern unless the recipe declaration owns an explicit, user-visible default. Server-owned reviewed bindings may be filled from authority; multiple usable profiles or any other ambiguous choice must fall back to the planner instead of being guessed.

**Step 4: Use the same pre-provider route on empty and reviewed-boundary surfaces.**

Factor `try_prepare_registered_recipe_plan(...)` in `service.py`. A proven complete match materializes a candidate and passes through `prepare_pipeline_plan`, required-control finalization, reviewed-component binding, custody, and preflight. Ambiguous or incomplete intent returns `None` and falls back to the generic planner; it never guesses a semantic slot.

**Step 5: Prove routing is not tutorial-specific.**

Tests must match at least two differently worded, differently named scrape/extract/project requests and reject a near miss with an unstated response field or conflicting retained fields. Assert no production matcher imports tutorial modules or compares profile/session/prompt identity.

**Step 6: Run recipe/service tests.**

```bash
PYTHONPATH=$PWD/src /home/john/elspeth/.venv/bin/python -m pytest \
  tests/unit/web/composer/test_recipes.py \
  tests/unit/web/composer/test_recipe_intent_routing.py \
  tests/unit/web/composer/test_web_scrape_recipe_apply.py \
  tests/unit/web/composer/test_service.py -q -n0
```

Expected: all pass; complete pattern requests take the server route and incomplete requests take the planner route.

**Definition of done:** Adding a recipe requires one registry declaration, and any surface can use it without a new service-level `if` branch. This closes `elspeth-73908a3df0`.

## Task 6: Reconcile explicit field projection with reviewed sink contracts

**Files:**

- Modify: `src/elspeth/web/composer/recipes.py`
- Modify: `src/elspeth/web/composer/guided/planning.py`
- Modify: `src/elspeth/web/composer/tools/generation.py`
- Modify: `src/elspeth/web/sessions/routes/composer/guided.py`
- Modify: `src/elspeth/web/frontend/src/components/tutorial/tutorialMachine.ts`
- Modify: `src/elspeth/web/frontend/src/components/tutorial/tutorialMachine.test.ts`
- Modify: `tests/unit/web/composer/test_recipes.py`
- Modify: `tests/unit/web/composer/test_validation_error_codes.py`
- Modify: `tests/integration/web/composer/guided/test_generic_linear_optimal_path.py`

**Step 1: Introduce one generic projection compatibility check.**

Given an explicit exact retained-field set and reviewed outputs, require every reviewed `required_field` to be present. On conflict return a closed `reviewed_output_projection_conflict` naming only field identifiers already visible in reviewed context. Do not silently add the sink-required field and do not silently weaken the sink contract.

**Step 2: Apply it at both deterministic and provider candidate seams.**

The generalized recipe validates its `retained_fields` before building. Guided candidate materialization validates an exact `field_mapper(select_only=True)` projection before provider repair. A non-exact/pass-through projection remains governed by ordinary field-contract propagation.

**Step 3: Correct the tutorial fixture to the intended contract.**

The UI and run response already expect `url + summary`; change the transform copy to say the saved rows retain `url` and `summary`, with identifiers explicit. This is a fixture correction only. Do not add those names to backend routing or validation code.

**Step 4: Test with non-tutorial names first, then the fixture.**

Assert `document_uri + abstract` is accepted and `abstract` alone conflicts when the reviewed sink requires `document_uri`. Then assert the tutorial constants say `url + summary` and the normal path produces that projection.

**Step 5: Run backend and frontend tests.**

```bash
PYTHONPATH=$PWD/src /home/john/elspeth/.venv/bin/python -m pytest \
  tests/unit/web/composer/test_recipes.py \
  tests/unit/web/composer/test_validation_error_codes.py \
  tests/integration/web/composer/guided/test_generic_linear_optimal_path.py -q -n0

cd src/elspeth/web/frontend
npm test -- src/components/tutorial/tutorialMachine.test.ts
```

Expected: all pass; the conflict test fails closed and the tutorial fixture uses the same generic rule.

**Definition of done:** There is one field-projection compatibility rule, and the tutorial no longer asks for a shape its reviewed output contract forbids.

## Task 7: Persist bounded planner-attempt evidence

**Files:**

- Add: `src/elspeth/contracts/composer_planner_audit.py`
- Modify: `src/elspeth/web/composer/audit.py`
- Modify: `src/elspeth/web/composer/pipeline_planner.py`
- Modify: `src/elspeth/web/composer/service.py`
- Modify: `src/elspeth/web/sessions/protocol.py`
- Modify: `src/elspeth/web/sessions/guided_audit.py`
- Modify: `src/elspeth/web/sessions/service.py`
- Modify: `src/elspeth/web/sessions/routes/composer/guided.py`
- Modify: `src/elspeth/web/sessions/routes/composer/guided_plan.py`
- Modify: `src/elspeth/web/sessions/routes/_helpers.py`
- Modify: `src/elspeth/web/sessions/routes/messages.py`
- Modify: `tests/unit/web/composer/test_pipeline_planner.py`
- Modify: `tests/unit/web/sessions/test_guided_atomic_settlement.py`
- Modify: `tests/unit/web/sessions/test_guided_operations_service.py`
- Modify: `tests/unit/web/sessions/test_routes.py`

**Step 1: Define a closed attempt record.**

Store exact enums/counts and hashes only. The implemented record also binds the
semantic attempt to its physical provider call and records closed retry/repair
facts:

```python
@dataclass(frozen=True, slots=True)
class ComposerPlannerAttempt:
    ordinal: int
    planner_call_ordinal: int
    phase: Literal["response", "discovery", "candidate", "repair", "hatch", "prose"]
    outcome: str
    planner_code: str | None
    selected_tools: tuple[str, ...]
    requested_information: tuple[str, ...]
    new_information: tuple[str, ...]
    rejection_codes: tuple[str, ...]
    candidate_shape_hash: str | None
    repeated_fingerprint: bool
    led_to: Literal["continue", "repair", "hatch", "terminal", "done"]
```

No raw candidate, arguments, prompt, validator message, option value, row, URI, path, secret, or reasoning is permitted.

**Step 2: Record attempts in the existing atomic planner audit cohort.**

Extend `BufferingRecorder` and `_persist_pipeline_planner_audit` after reconciling the active cohort work. Emit `_kind="planner_attempt_audit"` rows adjacent to their response-bearing LLM-call row. A physical transport failure owns no semantic attempt; physical retry gaps are valid while logical attempt ordinals remain contiguous. Persist one freeform request as a single atomic cohort in physical call/attempt order followed by its tool invocations. Preserve success, failure, cancellation, and guided fenced replay lineage. A registered zero-provider recipe produces zero call and zero attempt rows.

**Step 3: Expose the safe sidecar through the audit-grade message view.**

Include planner attempts when `include_llm_audit=true`, record the audit-grade read as today, and keep default conversation responses unchanged. This supplies the reliability harness and Landscape with a causal chain without exposing full tool results.

**Step 4: Test redaction and ordering.**

Use canary values in candidate options, paths, prompts, and validator messages. Assert none appear in the stored envelope or response. Assert the seven-attempt shape `discovery x4 -> candidate reject -> repair reject -> repair accept` remains distinguishable.

**Step 5: Run audit/message tests.**

```bash
PYTHONPATH=$PWD/src /home/john/elspeth/.venv/bin/python -m pytest \
  tests/unit/web/composer/test_pipeline_planner.py \
  tests/unit/web/sessions/test_routes.py -q -n0
```

Expected: all pass; audit opt-in returns ordered attempt sidecars, default history does not.

**Definition of done:** A future investigation can identify redundant discovery and each repair category from durable, scrubbed evidence. Coordinate closure with `elspeth-de3638b6ac`; do not claim that broader tool-result issue closed unless its own acceptance criteria are met.

## Task 8: Add efficiency acceptance and complete verification

**Files:**

- Modify: `src/elspeth/web/frontend/tests/e2e/helpers/tutorial-harness.ts`
- Modify: `src/elspeth/web/frontend/tests/e2e/tutorial-reliability.staging.spec.ts`
- Modify: `docs/agents/recent-code-hints.md` if implementation reveals a new whole-tree trap

**Step 1: Read planner attempt sidecars in the staging harness.**

Fetch every page of `/api/sessions/{id}/messages?include_llm_audit=true` and compute planner provider calls, useful discovery turns, no-gain turns, repair turns, prompt tokens, cost, and model latency. Parse physical retries and each ordinal-reset planner cohort independently; adjacency can cross a pagination boundary. Impossible evidence is unavailable, never zero-call evidence. Classify efficiency separately from functional completion.

**Step 2: Gate structural efficiency, not provider wall-clock.**

For the tutorial fixture require no state/list rediscovery, no no-gain attempt, and either the zero-call pattern route or at most two provider calls in one generic cohort with zero repairs. Multiple cohorts and transport retries remain structurally auditable but fail efficiency. Keep a generous wall-clock timeout for infrastructure variance; do not use lowering the 900-second timeout as the fix. In final cleanup, assert efficiency only when no earlier hard error exists so the gate cannot mask the functional failure.

**Step 3: Run all focused gates.**

```bash
PYTHONPATH=$PWD/src /home/john/elspeth/.venv/bin/python -m pytest \
  tests/unit/web/composer/test_pipeline_planner.py \
  tests/unit/web/composer/test_planner_authoring_aids.py \
  tests/unit/web/composer/test_recipes.py \
  tests/unit/web/composer/test_recipe_intent_routing.py \
  tests/integration/web/composer/guided/test_generic_linear_optimal_path.py \
  tests/integration/web/composer/guided/test_respond.py -q -n0

cd src/elspeth/web/frontend
npm test -- src/components/tutorial/tutorialMachine.test.ts
npm run typecheck
```

Expected: all pass.

**Step 4: Run whole-tree project gates.**

```bash
cd /path/to/worktree
PYTHONPATH=$PWD/src /home/john/elspeth/.venv/bin/python -m pytest tests/
wardline scan . --fail-on ERROR --fail-on-inert \
  --trust-pack scripts.wardline_pack --allow-custom-packs --local-only
ELSPETH_JUDGE_METADATA_SIGNATURE_VERIFY_MODE=shape-only-when-key-missing \
  /home/john/elspeth/.venv/bin/elspeth-lints check --rules all --root src/elspeth
```

Expected: `pytest tests/` passes. Wardline must recognize the current 129
boundaries; its six known active PY-WL-102 findings in untouched
`interpretation_state.py`/redaction code are an existing fail-closed baseline,
so compare exact fingerprints rather than claiming exit 0. The trust-tier lint
likewise remains the known fail-closed corpus; capture before/after output and
prove no new touched-file findings rather than expecting exit 0.

**Step 5: Run staging acceptance sequentially.**

Per `docs/agents/recent-code-hints.md`, never run concurrent Playwright commands in one worktree.

```bash
cd src/elspeth/web/frontend
npm run test:e2e:staging
```

Expected: tutorial completes through the normal runtime, output rows contain the intended URL and summary, normalization does not fire, and the structural planner-efficiency assertions pass.

**Definition of done:** Both the generic non-tutorial fixture and the tutorial regression pass; measured provider work is at the zero-call or two-call target; no implementation branches on tutorial identity; full gates and current-tree status are reported exactly.

## Delivery order

Implement Tasks 1-4 as the minimum correctness/performance slice. Task 5 is the zero-call acceleration and should land only after the generic two-call path is green. Task 6 removes the fixture contradiction and applies the shared projection rule. Task 7 is independently reviewable but must be rebased around the active audit-cohort work. Task 8 is the release gate.

Do not “fix” this by raising discovery/repair budgets, increasing the 900-second harness ceiling, lowering reasoning quality globally, selecting a cheaper provider only for the tutorial, or caching a tutorial pipeline. Those changes can mask the same protocol defects on the next simple pipeline.
