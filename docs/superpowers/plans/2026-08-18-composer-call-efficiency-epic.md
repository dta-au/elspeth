# Composer call-efficiency epic — implementation plan (elspeth-18a80a6aae)

Implements the 16 findings of `REVIEW-composer-call-efficiency-2026-08-18.md`
(repo root), ticketed as the children of epic `elspeth-18a80a6aae`. Operator
sign-off recorded on the epic (comment 7798, 2026-08-18). Each child ticket IS
the task spec — its SURFACE/DEFECT/FIX SEAM/EGRESS/MEASUREMENT/GUARDS sections
are binding. This plan adds only: task grouping, file ownership, sequencing,
and the controller rulings that resolve each ticket's open design choice.

Base: `release/0.7.2` @ `ba8726798`.

## Global constraints (bind every task)

1. **The LLM does the job.** No server-side synthesis, templating, routing,
   matching, or authoring of pipeline structure, under any name. Shortcuts
   change what the model is TOLD or what tools RETURN — never who authors.
2. **No tutorial-special paths** (ADR-031).
3. **Zero new egress.** Every context/payload addition restates facts the same
   provider already receives on the same surface via a palette tool, or is
   flagged in the report as an operator egress decision. Never include
   redacted option VALUES or sample values — names/keys/types/descriptions
   only, always built from the policy-projected catalog view.
4. **Do not weaken validation.** Reporting more of what validation already
   found is fine; accepting more is not.
5. **Trust-tier rules** (docs/agents/recent-code-hints.md is REQUIRED reading
   before writing code): no new `getattr`/`dict.get()` on boundary data —
   membership-test + subscript idiom; ADR-032 validate-by-trust-domain; raw
   strings for `pytest.raises(match=...)` (RUF043).
6. **Shared checkout, concurrent agents.** Wave-1 tasks run in parallel in ONE
   checkout. Edit ONLY the files your task owns. Never `git add`, `git
   commit`, `git stash`, or `git checkout`. If a test failure traces to a file
   outside your ownership, re-run once; if it persists, report it as
   environmental — do not fix other agents' files.
7. **Tests**: add/adjust tests that fail on revert of your change (mutation
   evidence). Run only tests scoped to your surface, `-n 2` maximum. Do not
   run the full suite; the controller runs it once at the end.
8. **lru_cache'd guided skills**: brief changes are not live until
   `elspeth-web.service` restarts — controller handles; do not restart.

## Wave 1 — parallel, disjoint file ownership

### Task 1 — briefs batch: F1 + F7 + F8 + F3/F5 conditional wording
Tickets: elspeth-d4ad1a76a7 (F1), elspeth-d975043e57 (F7), elspeth-a81b7a00b3 (F8),
wording halves of elspeth-275e05bf71 (F3) and elspeth-ac44757161 (F5).
Owns: `src/elspeth/web/composer/skills/pipeline_capabilities.md`,
`src/elspeth/web/composer/guided/skills/step_3_transforms.md`, plus any tests
that pin the affected prompt content or skill hashes.
Changes:
- F1: one sentence in `[capability:discovery-order]`: when the information
  manifest names several unresolved facts, issue every remaining discovery
  call in a single turn — results return together (up to the per-turn tool
  budget). Freeform corollary sentence where the compose loop is described:
  a turn's calls dispatch sequentially against rebinding state, so a mutation
  and its `preview_pipeline` may share a turn. (F1's optional
  information-manifest usage line is Task 10's, not yours.)
- F7: step_3_transforms.md item 2 → "load the live schema for each selected
  plugin not already supplied; assistance is for rejection repair or when the
  schema names an issue."
- F8: surface-scope both interpretation-review instructions. step_3 gate
  section: stage the requirement in the gate node's options inside the
  terminal proposal; the review card is surfaced from the sealed proposal —
  remove the instruction to call the tool (it is absent from both the planner
  and step-chat palettes). pipeline_capabilities.md: phrase per-surface — the
  tool call is for palettes that carry it (the compose loop); planner
  surfaces stage in node options only. Never delete the freeform instruction.
- F3/F5 wording: ":61 Model identifiers come only from list_models" → prefer
  the supplied model catalog when present, else list_models; ":37 use
  get_expression_grammar before authoring conditions" → only when the grammar
  is not already supplied. (The aids that supply them are Task 6.)
Guards: composed-prompt palette gates ban naming
`list_sources`/`list_transforms`/`list_models` in guided step prompts — say
what to do, not which tool. Skill content may be identity-hashed; update
pinned hashes/fixtures deliberately and list them in your report.

### Task 2 — F4: step-2 sink digest
Ticket: elspeth-9bb127d9cd.
Owns: `src/elspeth/web/composer/guided/chat_solver.py`,
`src/elspeth/web/composer/guided/guided_chat_atomic.py` (only if the digest
data must thread through it), plus their tests.
Change: inject a sink digest (names, one-line purpose, `config_fields`) into
the step-2 dynamic block of `_build_step_2_sink_tool_prompt`, mirroring
step-1's `available_source_plugins` pattern. Build it from the same
policy-projected catalog view `list_sinks` serves. Bound its size; on
overflow, prefer completeness of names over completeness of fields.
Guards: the injected text is part of the COMPOSED prompt — it must not name
banned tool literals. No sample values, no secret material — catalog facts
only. Do not edit any `.md` skill file (Task 1 owns those); if wording there
must change, report it instead.

### Task 3 — F12: guided binder rejection facts
Ticket: elspeth-989c4108ef.
Owns: `src/elspeth/web/composer/guided/planning.py` plus its tests.
Change: type the 9 bare `AuditIntegrityError` sites with the closed-code +
connectivity pattern (elspeth-572c642dbf precedent); give
`_guided_delta_rejection` factory codes per-condition facts; split the amend
binder's accumulated boolean into per-violation facts. Facts name the
component/field/name that mismatched and the legal alternatives already
present in the redacted context (reviewed names — the settled
elspeth-859e2702dd custody judgment). Facts first; aggregation second-order.
Guards: PREFER existing closed codes carrying richer facts over new codes; a
new code obligates every closed-vocab consumer (grep the code maps and any TS
mirrors first) — list any addition in your report. Facts may name option KEYS
and component names, never redacted VALUES.

### Task 4 — F11: terminal schema constraint disclosure
Ticket: elspeth-2e9df07c69.
Owns: `src/elspeth/web/composer/tools/schema_contract.py` plus its tests.
Change: encode in `canonical_set_pipeline_schema()` the naming rules the
runtime already enforces — node charset/length/reserved labels, connection
charset, any lowercase rule — each verified against `core/config.py` (node
regex :188 differs from connection regex :192; confirm what :262's lowercase
rule binds) and placed at the correct JSON path (`pattern`, `maxLength`,
reserved labels via `not`/`enum` shape). `assert_set_pipeline_schema_compatible`
must stay green. Do not edit `core/config.py`.
Guards: check whether the canonical schema bytes are snapshot-pinned anywhere
(wire-shape templates are AST-gated in this repo); update pins deliberately
and list them.

### Task 5 — F13: list_* description truth
Ticket: elspeth-77b30b80bf.
Owns: `src/elspeth/web/composer/tools/sources.py`,
`src/elspeth/web/composer/tools/transforms.py` plus tests pinning those
descriptions.
Change: descriptions state what entries actually carry (full config_fields —
name/type/required/description/default — composer_hints, secret_requirements,
usage guidance) and when `get_plugin_schema` is still required (enums, nested
shapes, raw json_schema). Verify the payload claim against
`web/catalog/schemas.py` / `web/catalog/service.py` /
`web/plugin_policy/profiles.py` (read-only) before writing.

### Task 6 — F3 + F5 payload: authoring-aids supply
Tickets: elspeth-275e05bf71 (F3), elspeth-ac44757161 (F5) — payload halves.
Owns: `src/elspeth/web/composer/planner_authoring_aids.py` plus its tests.
Change: include in authoring aids (memoized per snapshot hash) (a) the model
catalog — the same policy-visible facts `list_models` returns, provider →
identifiers, under its own byte budget with an explicit truncation marker
that defers to `list_models` when over budget; (b) the authoring subset of
the expression grammar `get_expression_grammar` serves, under its own budget.
Guards: DO NOT touch `pipeline_planner.py` — the manifest supplied-key flip
is Task 10. Policy-projected identifiers only. Budgets follow the existing
aid-budget idiom in the file.

## Wave 2 — serial (contended files: pipeline_planner.py, tools/_common.py, tools/sessions.py)

### Task 7 — F16 + F9: multi-component rejections; drop the no-gain explain line
Tickets: elspeth-4fad98a453 (F16), elspeth-41b406c9fc (F9).
Owns: `src/elspeth/web/composer/tools/sessions.py`,
`src/elspeth/web/composer/tools/_common.py`,
`src/elspeth/web/composer/pipeline_planner.py` (rejection synthesis region),
plus tests.
Change (F16): `build_set_pipeline_candidate`'s per-component loop continues on
failure and collects per-component rejections, bounded (controller ruling:
cap 8 components per turn with an explicit "+N more components withheld"
marker — a reporting cap, not a validation change); `_rejection_only_validation`
/ `_rejection_entries` carry the full set; project pydantic schema-error field
paths into the canonical-schema feedback instead of one bare code.
Change (F9): `_allowlisted_candidate_feedback`'s guidance line offers
`explain_validation_error` only for codes that arrived WITHOUT an inline
explanation.
Guards: every collected rejection is planner-authored content (settled
custody judgment) — still zero egress. Validation still rejects everything it
rejected before.

### Task 8 — F14 + F15: mutation echo; planner failure contract attach
Tickets: elspeth-f14aba9686 (F14), elspeth-1d8fc3da83 (F15).
Owns: `src/elspeth/web/composer/tools/_common.py`,
`src/elspeth/web/composer/tools/sessions.py`, `src/elspeth/web/composer/tools/_dispatch.py`
(only if the augment wiring requires), `src/elspeth/web/composer/pipeline_planner.py`
(failure-payload region), plus tests.
Change (F14): on SUCCESS, mutation ToolResults carry a compact per-component
echo of the applied block post-finalizer (controller ruling: the applied
component's round-trippable projection, not whole-state).
Change (F15): attach the projected planner plugin contract
(`_project_planner_plugin_contract`, existing 48KiB budget) for plugins named
in `plugin_options_invalid` rejection entries on the PLANNER surface, under
the same finalizer-owned withholding the other fact classes use.
Guards: F14 note — the CORRECTED ticket: the planner palette includes
get_pipeline_state but has no mutating tools; the echo is freeform-only by
construction. Zero egress both: same bytes the same surface already serves.

### Task 9 — F6: an honest decline
Ticket: elspeth-ae8b92ea2a.
Owns: `src/elspeth/web/composer/pipeline_planner.py` plus tests.
Controller ruling (blast radius): NO new terminal tool. (a) On an ordinary
turn whose information manifest shows the relevant catalog facts supplied, a
prose reply that parses as a decline (the same first-sentence-cause format
the hatch notice teaches, same parser as the hatch turn) resolves to
PlannerDeclined instead of drawing a nudge; prose that does not parse as a
decline nudges exactly as today. (b) The prose-overrun path engages the hatch
when `_hatch_available()`, for parity with every other exhaustion; terminal
MALFORMED_RESPONSE only when it is not.
Guards: the decline text is LLM-authored — the server classifies, never
writes. Disposition codes are a closed vocabulary — reuse
PlannerDeclined/existing codes; add none. Cross-link elspeth-f5a9021d2d in
the report if touched territory overlaps; do not fix it here.

### Task 10 — F2 + manifest flips (F3/F5) + F1's manifest usage line
Tickets: elspeth-cb3561382e (F2); supplied-key flips for F3/F5; F1's optional line.
Owns: `src/elspeth/web/composer/pipeline_planner.py`,
`src/elspeth/web/composer/prompts.py`, `src/elspeth/web/composer/service.py`,
`src/elspeth/web/composer/tools/tool_batch.py`, plus tests including
`test_proposal_audit_projection.py`.
Change (F2): thread `schemas_loaded` into `plan_guided_pipeline`/`plan_pipeline`;
attach `build_schema_contract_evidence(...)` (rehydrated through the current
PolicyCatalogView — never stored bytes) to the planner provider_request beside
authoring_aids; mark schema loads from the planner discovery dispatch and the
step-2 chat dispatch. Update the pinned full-dict projection test
deliberately; its pre-equality privacy canaries are the no-egress proof and
must stay.
Change (flips): with Task 6's aids now carrying the model catalog and grammar
subset, flip `model.catalog` / `expression.grammar` manifest classes to
supplied-when-present (falling back to discoverable when a budget deferred).
Change (F1 line): the optional static usage line in
`information_manifest.provider_payload` naming the batching affordance (same
zero-egress class as `reviewed_configuration_usage`).
Guards: this is server-owned catalog fact re-supply, NOT cross-request reuse
of provider output. Guided-context additions update the projection pin
deliberately, canaries intact.

## Task 11 — F10: measure the digest omission rate (controller-run)
Ticket: elspeth-31839e4966. Read `budget.omitted_public_text_count` from the
live deployment's rendered digest; report; propose a budget raise only if
routinely nonzero — the byte-cost increase is an operator decision.

## Verification (controller)
Per-task review (spec + quality) → wave-2 serial → full `pytest tests/` with
HEAD recorded before/after → trust-tier corpus compare vs `ba8726798` full
`git archive` baseline (count, never tail) → wardline gate with
`--fail-on-inert` → final whole-branch review (most capable model) →
`elspeth-web.service` restart verified by ExecMainStartTimestamp + socket
probe → filigree: children → verifying with fix evidence, epic comment.
