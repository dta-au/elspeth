# Composer call-efficiency review — missing shortcuts, LLM kept

**Date:** 2026-08-18 · **Tree:** `release/0.7.2` @ `78649f625`
**Prompt:** `REVIEW-PROMPT-composer-call-efficiency-2026-08-18.md`
**Status:** review only — every proposal below is a brief/context/payload/choreography
change, i.e. a behaviour change needing operator sign-off. Nothing is implemented.

Unit of cost throughout: **provider calls** (one physical LLM request = one
planner turn), never seconds. Guided skills are `@lru_cache`d — any brief
change needs an `elspeth-web.service` restart verified by
`ExecMainStartTimestamp`, not `is-active`.

---

## What this walk found, in one paragraph

The planner loop is already dense with anti-waste machinery — a request-scoped
information manifest, a palette minimized per request (`get_pipeline_state` is
never even offered), no-gain rejection of covered discovery, a cycle guard, a
blind-repeat short-circuit, multi-defect rejection feedback with inline static
fixes, one-shot nudges with omit-valves, and an escape hatch. The residual
waste is therefore not in the loop's guards; it is in **what the request fails
to carry** (session schema evidence, model catalog, expression grammar, a sink
digest on step 2), **what the briefs fail to say** (that discovery calls batch
in one turn; that assistance is conditional), **one instruction that names a
tool the palette rejects**, and **one choreography hole** (there is no honest
way to decline before the budget is spent), **and what the rejection channel
withholds on exactly one surface** (the planner never receives the inline
`plugin_schemas` payload freeform gets, and candidate prevalidation reports
one component per turn). Sixteen findings, counted below, none of which moves
authorship server-side.

---

## Invariant posture (bounds on the fix space, honored throughout)

- The `passthrough_sketch_shape` bypass (`service.py:3653`, `:3697`) is still
  in the tree. Per the prompt it is treated as the **anti-pattern**
  (`elspeth-b4a286d517`, confirmed P1), not a precedent: in the calls-per-shape
  table the 1×1 pass-through's "current 0 calls" is a violation, and the target
  is the **planner's** 1 call.
- No finding below proposes server-side authoring, output reuse across
  requests, or weakened validation. One finding (F6) proposes a new *decline*
  affordance; the decline text stays LLM-authored, so the LLM still does the
  job. Findings whose only fix would be server authoring: **none found**.
- Every context addition proposed is restating facts the same provider already
  receives through the same surface's tools (zero new egress), except where
  explicitly flagged. The privacy canaries in
  `tests/integration/web/composer/guided/test_proposal_audit_projection.py`
  are the no-egress proof for any guided-context change (they run before the
  full-dict equality pin).
- Correction-path phrasing rule respected: context prose below is phrased as
  what the server **restores/supplies**, never what the planner "owns"
  (`guided_redacted_planner_context` is shared with the correction path,
  `service.py:3600-3612`).

---

## Findings — ranked, most calls saved first

**Count: 16 findings** (F1–F16, counted, not tailed), plus 7 cross-linked open
issues that are *not* re-reported. Table order is the ranking; IDs are stable
labels, not ranks. F13–F16 were added after the three inventory sweeps
(tool payloads, rejection messages, evidence corpus) reported; their
supporting citations are the sweeps', spot-checked rather than independently
re-derived.

**Tracked in Filigree** under epic `elspeth-18a80a6aae` (labels: `composer`,
`planner-efficiency`, `source:call-efficiency-review-2026-08-18`), one child
per finding: F1 `elspeth-d4ad1a76a7` · F2 `elspeth-cb3561382e` ·
F16 `elspeth-4fad98a453` · F15 `elspeth-1d8fc3da83` · F3 `elspeth-275e05bf71`
· F4 `elspeth-9bb127d9cd` · F13 `elspeth-77b30b80bf` · F5 `elspeth-ac44757161`
· F6 `elspeth-ae8b92ea2a` · F7 `elspeth-d975043e57` · F8 `elspeth-a81b7a00b3`
· F11 `elspeth-2e9df07c69` · F12 `elspeth-989c4108ef` ·
F14 `elspeth-f14aba9686` · F9 `elspeth-41b406c9fc` ·
F10 `elspeth-31839e4966` (task, measure-first). All are proposals pending
operator sign-off.

| # | Class | Surface | Shapes hit | Est. calls wasted / occurrence | Egress delta |
|---|---|---|---|---|---|
| F1 | E | capability core discovery-order text | every multi-discovery shape | 1–3 | zero |
| F2 | A/C | planner context assembly + schema tracker | corrections, revisions, any repeat planning in a session | 1–2 | zero |
| F16 | D | candidate prevalidation (first-component-only) | any candidate with ≥2 defective components | 1 per extra defective component; ≥3 ⇒ deterministic exhaustion | zero |
| F15 | D | planner rejection payload (no `plugin_schemas`) | every `plugin_options_invalid` repair on the planner surface | 1 | zero |
| F3 | C/A | authoring aids (model catalog) | every shape with an LLM node | 1 (2 when provider unknown — `list_models` is two-mode) | zero |
| F4 | A/C | step-2 sink chat context | every guided session (step 2) | 1–2 | zero |
| F13 | C/A | `list_*` tool descriptions (payload understated) | selection/configuration on chat + freeform surfaces | ≤1 | zero |
| F5 | C/A | authoring aids (expression grammar) | every gate/condition shape | 1 | zero |
| F6 | E | planner loop choreography (decline path) | intent naming an unavailable capability | 3–6, or terminal misclassification | zero |
| F7 | F | step-3 brief (assistance clause) | step-3 planner runs, per selected plugin | ~1 per plugin | zero |
| F8 | B/E | step-3 brief + capability core (review-tool naming) | gate shapes where the model authored a threshold | 1, worst case whole-request terminal | zero |
| F11 | B | terminal tool schema (naming-rule disclosure) | any shape, first time a naming rule is tripped | 1 repair per trip | zero |
| F12 | D | guided binder fact-free rejections | guided candidates with binder-shape defects | 1–2 repairs, worst case exhaustion | zero |
| F14 | C | mutation results are ack-only (freeform) | mutate-then-read sequences on the compose loop | ≤1 per sequence | zero |
| F9 | D | rejection feedback guidance line | repair turns | ≤1 per repair | zero |
| F10 | A | discovery digest byte budget | deployment-dependent (measure first) | 1 per affected plugin selection | zero (bytes ↑) |

### F1 · Discovery batching exists but is never advertised — class E

**Surface:** `pipeline_capabilities.md:11-41` (`[capability:discovery-order]`);
loop mechanics at `pipeline_planner.py:4368-4460`.
**Evidence.** The loop dispatches up to `max_tool_calls_per_turn = 16`
(`config.py:286`) discovery calls **concurrently in one provider turn**
(`asyncio.wait` over `execute_one_discovery` tasks). Yet no provider-visible
text anywhere says several discovery calls may ride one turn: a whole-skills
grep for batch/parallel/same-turn phrasing finds only the *mutation* batching
section of `pipeline_composer.md` (freeform `set_pipeline`). The capability
core instead presents discovery as a numbered sequential protocol (manifest →
digest → schema → grammar → author). A model that follows it serially pays one
provider call per discovery item; the information manifest even names the
gaps up front (`unresolved` keys), so the model *knows* its full shopping list
on turn 1 and still has no instruction to buy it all at once.
**Was the information already in context?** Yes — the affordance exists; the
brief never states it. Brief defect.
**Fix seam:** brief text. One sentence in `[capability:discovery-order]`:
issue every remaining discovery call in a single turn; results return
together. (Optionally mirrored in the `information_manifest.provider_payload`
as a static usage line — same zero-egress class as
`reviewed_configuration_usage`.)
**Measurement:** mean `tool_calls` per `phase="discovery"` attempt row and
`discovery_turns` per shape, before/after, on the gate+fork+coalesce and
llm-transform shapes; tutorial harness `noGainTurns`/turn-count assertions
(staging-only — see Evidence notes).
**Freeform corollary:** the compose loop dispatches a turn's tool calls
sequentially against the rebinding state (`_compose_loop` P3,
`service.py:5809+`), so `set_pipeline` + `preview_pipeline` can ride one
turn; the skill mandates mutation batching but never says the preview may
share the turn — models that serialize mutate-turn → preview-turn →
finalize-turn pay 3 composer calls where 2 suffice. (Trade-off to state in
the brief: a rejected mutation wastes the in-turn preview — server compute,
not a provider call.)
**Invariant tension:** none.

### F2 · Session schema evidence never reaches the planner surface (and planner/step-2 loads are never marked) — class A/C

**Surface:** context assembly. `schema_contract_evidence` is built only in the
freeform compose-loop context (`prompts.py:332`, `:344`, keyed by
`ComposerServiceImpl._schemas_loaded_for_session`, `service.py:6392`); the
tracker is written **only** by the freeform tool batch
(`tool_batch.py:2181`). The planner's `provider_request`
(`pipeline_planner.py:3260-3272`) carries `authoring_aids` but no schema
evidence, and planner-surface `get_plugin_schema` successes are never marked
into the tracker; neither are the step-2 sink chat's.
**Evidence.** The planner-visible digest guidance itself says: *"Author
options only from schema_contract_evidence for this request or a current
get_plugin_schema result"* (`planner_authoring_aids.py:1589`) — naming an
artifact that **never exists on the planner surface**. Consequence: every
planner request must re-fetch schemas the session already loaded. Within one
request `selected_schema_contracts` accumulates (48 KiB budget,
`pipeline_planner.py:964`, `:4520+`), but a correction or prose-revision
request one minute later starts from zero — a schema the step-3 plan already
fetched is re-fetched to patch one option on the same node.
**Was the information already in context?** No, but the server knows it —
context-assembly defect (the exact class the prompt's discriminator names).
**Fix seam:** context builder. Thread `schemas_loaded` into
`plan_guided_pipeline`/`plan_pipeline` and attach
`build_schema_contract_evidence(...)` to the planner `provider_request`
(rehydrated through the current `PolicyCatalogView`, so policy rotation can
never serve stale bytes — the mechanism already guarantees this,
`planner_authoring_aids.py:1297-1400`); mark schema loads from the planner
discovery dispatch and the step-2 chat dispatch. Zero new egress: the same
provider already received these schema bytes through `get_plugin_schema` on
the same surface, and rehydration keeps policy-hidden identities out
(`unavailable_in_current_policy` omission path already exists).
**Measurement:** count `plugin.schema:*` keys in `requested_information` on
correction/revision planner attempts, before/after; expect option-patch
corrections to drop to zero discovery turns.
**Invariant tension:** none. This is not cross-request reuse of provider
*output* — it is server-side re-supply of server-owned catalog facts.

### F16 · Candidate prevalidation stops at the first defective component — class D

**Surface:** `build_set_pipeline_candidate` (`tools/sessions.py:466`) — ~58
early `return _failure_result(...)` sites inside per-component `for` loops,
all with `with_state_validation=False`; `_rejection_only_validation`
(`tools/_common.py:1157-1174`) then builds a one-entry summary, and
`_rejection_entries` (`pipeline_planner.py:1941-1942`) keeps only that entry.
**Evidence (rejection sweep, spot-checked).** The multi-error projection in
`_allowlisted_candidate_feedback` is real but is fed **one entry** on every
pre-application rejection: the first bad component returns before later
nodes/outputs are checked. Within a component, `_prevalidate_plugin_options`
(`_common.py:1876-1888`) does join every pydantic option defect — so the unit
is one *component* per turn, all option defects within it. Downstream,
`CompositionState.validate()` (`state.py:5509`) accumulates everything with
no early return, but the planner reaches it only after clearing all ~58
gates. With `composer_planner_repair_budget = 2`, a candidate with three
defective components deterministically exhausts. The pydantic
canonical-schema path is worse: `PydanticValidationError` at
`pipeline_planner.py:2438` discards the full multi-error list for one bare
`canonical_schema` code with no field names.
**Fix seam:** rejection synthesis — let the component loop continue on
failure and collect per-component rejections (bounded), and project pydantic
schema errors' field paths into the canonical-schema feedback. Zero new
egress: everything named is content the planner itself authored in the
rejected call (the settled `plugin_options_invalid` custody judgment).
**Measurement:** repair sequences whose successive rejections name different
components; REPAIR_EXHAUSTED rate on multi-component shapes, before/after.
**Invariant tension:** none — reporting more of what validation already
found is not weakened validation.

### F15 · The `plugin_schemas` anti-round-trip payload exists on freeform and never on the planner surface — class D

**Surface:** `build_plugin_schemas_for_failure` (`tools/_common.py:1076-1122`)
attaches the full `get_plugin_schema` payload — every legal option key — to
any failure whose message matches `Invalid options for <kind> '<plugin>'`.
Wired only via `tools/_dispatch.py:494-543` for the 10 tools with
`augments_on_failure=True` (including `set_pipeline`, `sessions.py:1784`).
`pipeline_planner.py` contains **zero** references to `plugin_schemas`
(verified by the sweep, count 0).
**Evidence.** Its own docstring states the intent: *"Eliminates the second
round-trip the LLM would otherwise burn calling `get_plugin_schema`
separately after each rejection."* The freeform/MCP caller gets the legal
key inventory inline; the planner gets `detail` naming only the violated
keys and must spend a `get_plugin_schema` turn for the same bytes — the
catalogue's own `plugin_options_invalid` guidance says exactly that
(`generation.py:722`, "Only if detail is absent: call get_plugin_schema…").
**Fix seam:** rejection message — attach the projected planner plugin
contract (`_project_planner_plugin_contract`, already budgeted at 48 KiB)
for plugins named in `plugin_options_invalid` entries, with the same
finalizer-owned withholding the other fact classes use. Zero new egress:
`get_plugin_schema` is in the planner palette; these are the same bytes one
turn later.
**Measurement:** repair-phase attempts whose `requested_information` is
`plugin.schema:*` immediately following a `plugin_options_invalid` rejection.
**Invariant tension:** none.

### F3 · The model catalog is never pre-supplied; `list_models` is mandatory for every LLM shape — class C/A

**Surface:** `pipeline_capabilities.md:61` ("Model identifiers come only from
`list_models`"); manifest classes at `pipeline_planner.py:270-280`
(`model.catalog` is always discoverable, never supplied); authoring aids carry
LLM *rules* (profile alias, on_error, output contract —
`planner_authoring_aids.py:529-728`) but not the catalog.
**Evidence.** Any intent involving an LLM node ("rate each page 1–10") costs
one discovery turn for a catalog that is small, static per policy snapshot,
and policy-visible to this exact provider via a palette tool. Worse, the
tool is two-mode (payload sweep, `generation.py:1686-1717`): called without
`provider` it returns provider names + counts plus a `hint` telling the model
to call again with a provider — so a model that doesn't know the provider
pays **two** turns. The aids are already memoized per snapshot hash; the
catalog belongs in them.
**Fix seam:** context builder (authoring aids section, own byte budget), plus
one conditional word in the capability core ("when not already supplied").
Keep `list_models` in the palette for parity and for oversized catalogs.
Zero new egress.
**Measurement:** attempts with `model.catalog` in `requested_information`
before/after on the 1×1+LLM-transform shape.
**Invariant tension:** none.

### F4 · Step-2 sink chat carries no sink digest — the catalog is reachable only by spending a provider round — class A/C

**Surface:** `_build_step_2_sink_tool_prompt` (`chat_solver.py:2678`) injects
current-sink revision context only; `step_2_sink.md:9-11` then instructs:
*"If you're not sure which sink fits, call `list_sinks`… Call
`get_plugin_schema` on the one you pick."* Each discovery iteration of
`maybe_resolve_step_2_sink_chat` (`chat_solver.py:2940+`) is **one full
provider call** (loop capped by `composer_max_discovery_turns`).
**Evidence.** Contrast the two siblings: step-1 chat receives
`available_source_plugins` in its dynamic block
(`guided_chat_atomic.py:420`), and the planner surface receives the whole
discovery digest. Step-2 gets neither, so the routine resolution runs
list_sinks-round → schema-round → resolve-round = 3 provider calls. And the
payload sweep sharpened the minimum: a `list_sinks` entry already carries
full `config_fields` (`{name, type, required, description, default}` per
field — `web/catalog/schemas.py:28-35, 50-90`, both policy branches
verified) plus `composer_hints`, so a digest carrying those fields makes a
flat-option sink resolvable in **one** round; `get_plugin_schema` adds only
raw `json_schema`/`knob_schema` (enums, nested shapes) and is genuinely
needed only for structured options.
**Was the information already in context?** No, but the server knows it —
context-assembly defect.
**Fix seam:** context builder — inject a sink digest (names, one-line purpose,
required-option names; the same selection facts `list_sinks` returns to this
same palette). Zero new egress. This is the shared-invariant-in-a-specialized-
component pattern: step-1 solved it locally with `available_source_plugins`,
and step-2 — the sibling the same justification covers — silently went
without.

### F13 · The `list_*` descriptions understate their own payload, steering models into redundant schema fetches — class C/A

**Surface:** `list_sources`/`list_transforms`/`list_sinks` descriptions
(`sources.py:153-158`, `transforms.py:146-151`, `:175-180`) self-describe as
returning plugins *"with name and summary"*.
**Evidence (payload sweep, spot-checked).** The actual entries are full
`PluginSummary` objects carrying `config_fields` (name, type, required,
description, default per option), `composer_hints`, `secret_requirements`,
and usage guidance — traced through both the resolver-absent and
policy-projected branches (`web/catalog/service.py:388-450`,
`web/plugin_policy/profiles.py:1096-1132`). A model told it received "name
and summary" rationally fetches the schema next even when the listing
already answered the configuration question; only enums/nested shapes
genuinely need `get_plugin_schema`.
**Fix seam:** tool payload (description text): state what the entries carry
and when a schema fetch is still required. Zero egress — the description
describes bytes already sent.
**Measurement:** `get_plugin_schema` calls whose target's options are all
flat fields already present in a prior `list_*` result in the same request.
**Invariant tension:** none.
**Measurement:** `llm_call_audit` rounds per step-2 resolution (the guided
route records one `ComposerLLMCall` per provider round), before/after.
**Invariant tension:** none.

### F5 · The expression grammar is an extra mandatory call on every condition-bearing shape — class C/A

**Surface:** `pipeline_capabilities.md:37` ("Use `get_expression_grammar`
before authoring conditions" — unconditional); `expression.grammar` is a
manifest key never pre-supplied (`pipeline_planner.py:154`).
**Evidence.** The grammar is static public text (the capability core already
paraphrases its most important rule — `row['field']` subscripting). Every
gate/fork shape pays one discovery turn to fetch a deployment-static document.
**Fix seam:** context builder (ship the authoring subset of the grammar in
`authoring_aids` under its own budget) + capability core wording becomes
conditional ("when not already supplied"). Zero new egress.
**Measurement:** attempts with `expression.grammar` in
`requested_information` on the gate+fork+coalesce shape, before/after.
**Invariant tension:** none.

### F6 · There is no honest decline before budget exhaustion; the prose-overrun path skips the hatch entirely — class E

**Surface:** `pipeline_planner.py:3308` (`allow_text_reply=False` on every
ordinary turn; `True` only on the hatch turn, `:3684`); prose-overrun path
`:3721` (`prose_nudges > _PROSE_NUDGE_BUDGET` → terminal
`MALFORMED_RESPONSE`, **with no `_hatch_available()` check** — unlike every
budget-exhaustion path); decline type `PlannerDeclined` reachable only from
the hatch turn (`:3775`); the hatch notice (`:1570`) is the only text
that ever teaches the decline format.
**Evidence.** For an intent naming an unavailable capability, the digest is
"the complete selection index" — absence is provable from context on turn 1.
The brief requires reporting a named gap (`base.md:36-38`), but the planner
has **no legal move** that expresses it: prose draws two nudges whose text
(`:1558`) offers only "call emit_pipeline_proposal … otherwise continue
discovery", then the third prose reply terminalizes as `MALFORMED_RESPONSE` —
a *misclassified* honest decline that never reaches the hatch. The
"cooperative" path is to burn discovery/composition budget until an
exhaustion engages the hatch, whose one overtime turn finally permits the
decline. Choreographed reality: 4–7 calls (and an advisor-model call) to say
"no". Decision minimum: **1** (the digest already proves absence).
**Fix seam:** choreography + brief. Two independent pieces:
(a) give ordinary turns a legal decline — either a closed
`decline_pipeline_request(reason)` terminal variant or `allow_text_reply`
once the manifest shows the relevant catalog facts supplied; the decline text
stays LLM-authored (first-sentence-cause format the hatch notice already
specifies). (b) The prose-overrun path at `:3721` should engage the hatch
when available, for parity with every other exhaustion (this is a one-line
choreography fix but is behaviour — flagged for sign-off like the rest).
**Measurement:** `journalctl` planner disposition codes (they appear nowhere
else): count `MALFORMED_RESPONSE` terminals whose final attempts were prose
(`phase="prose"` trail rows), and calls-to-decline on a synthetic
unavailable-capability intent, before/after. Adjacent open bug
`elspeth-f5a9021d2d` (exhausted-advisor branch mislabels the failure) is the
same neighborhood — linked, not duplicated.
**Invariant tension:** none — a decline authored by the model is the LLM
doing the job; the server authors nothing.

### F7 · Step-3 brief unconditionally orders schema **and assistance** loads for every selected plugin — class F

**Surface:** `step_3_transforms.md:22-23` — *"discover the policy-visible
transforms that can implement the intent and load the authoritative
schema/assistance for each selected plugin."*
**Evidence.** `get_plugin_schema` and `get_plugin_assistance` are separate
calls with separate manifest keys (`plugin.schema:*`,
`plugin.assistance:*`, `pipeline_planner.py:303-311`); the capability core
already scopes assistance to *repair* ("Use `get_plugin_assistance` and
`explain_validation_error` for structured repair when a proposal is
rejected", `pipeline_capabilities.md:34-36`) and scopes schema loads with
"whose detailed option or output contract is not already supplied". The
step-3 clause overrides both conditions with an unconditional pairing — the
same class-F pattern as the fixed discover-first defect one paragraph above
it (`e69e46070`/`78649f625`), one clause later. Cost: ~1 no-gain call per
selected plugin on the happy path (general assistance for a plugin whose
schema already carries its composer hints).
**Fix seam:** brief text — "load the live schema for each selected plugin not
already supplied; assistance is for rejection repair or when the schema names
an issue." Zero egress.
**Measurement:** attempts with `plugin.assistance:*/general` in
`requested_information` and empty `new_information` on step-3 shapes.
**Invariant tension:** none.

### F8 · Step-3 brief and the capability core instruct calling `request_interpretation_review` on surfaces whose palette rejects it — class B/E

**Surface:** `step_3_transforms.md:60-64` and `pipeline_capabilities.md:97-102`
both instruct, for a self-authored gate threshold: *"stage a pending
`pipeline_decision` interpretation requirement … and call
`request_interpretation_review(kind="pipeline_decision", …)"*. The planner
palette is the 20 read-only discovery tools plus `emit_pipeline_proposal`
(`capability_skill.py:17-41`) — `request_interpretation_review` is not in it,
and the step-3/4 *chat* palette (deferred-intent tools only,
`guided_chat_atomic.py:489-511`) lacks it too.
**Evidence.** A planner that obeys the instruction with a lone
`request_interpretation_review` call hits the `DISCOVERY_ONLY` guard
(`pipeline_planner.py:4286-4291`) — a **terminal** failure of the whole
planning request; bundled alongside the terminal call it is silently ignored
(wasted tokens, no staging). The correct planner-surface mechanics — staging
`interpretation_requirements` in the gate node's options inside the terminal
payload — is exactly what the first half of the same sentence says; the tool
call is the wrong half. The existing palette trip-wire
(`test_capability_skill_identity.py:109`) bans `list_sources` /
`list_transforms` / `list_models` and per-step extras, but does not cover
this name, which is how it survives.
**Fix seam:** brief text (surface-scoped phrasing: on planner surfaces, stage
in node options only; the review card is surfaced from the sealed proposal),
plus optionally extending the palette trip-wire to pin it. Zero egress.
**Measurement:** journal `DISCOVERY_ONLY` dispositions whose attempt rows'
`selected_tools` contain `request_interpretation_review`; a staging run of
the gate shape with a model-authored threshold.
**Invariant tension:** none. Cross-link: `elspeth-7bd0141bbe` covers the
*compose-loop* discoverability of the `user_term` registry (note: this tree
now ships `authoring_aids` — including `review_registry` — in the compose
catalog context, `prompts.py:236`, so part of that ticket's (c) is addressed;
its residue includes the `user_term` tool-schema description still
enumerating only three of the registered terms inline,
`tools/_dispatch.py:247-254`).

### F11 · Every naming rule is met by rejection: the terminal schema discloses zero authoring-time constraints — class B

**Surface:** `canonical_set_pipeline_schema()`
(`tools/schema_contract.py:302`) — dumped live: 7 960 bytes, **zero
`pattern` fields, zero `maxLength` fields**. The runtime rules it hides:
id/name charset `^[a-zA-Z][a-zA-Z0-9_-]*$` (`core/config.py:188`), length ≤ 38
(`core/config.py:74-75`), reserved labels `{continue, fork, on_success}`
(`core/config.py:68`), lowercase requirement (`core/config.py:262`).
**Evidence.** All four rules are `mirrored` sites in the parity corpus
(Stage-1 counterparts `node_id_invalid` / `connection_label_invalid`), so the
planner *does* learn them — one repair turn after tripping one. The
capability core's only naming guidance is "Use stable, descriptive
source/node/output ids". Mirrored is not discoverable: the class-B question
the prompt poses is exactly this gap. JSON Schema can carry every one of
these as `pattern` / `maxLength` / `not`-enum on the terminal tool definition
— constraints many providers enforce at generation time, turning a full
repair round-trip into an in-flight regeneration. The reserved-label trap is
the realistic case (a model naming a fork branch `fork`).
**Fix seam:** tool payload (the advertised terminal schema). Zero new egress
— the rules are public runtime facts; `assert_set_pipeline_schema_compatible`
(`schema_contract.py:280+`) polices requiredness/nullability/enum narrowing
and is not violated by adding constraints the runtime itself enforces.
**Measurement:** repair turns whose rejection codes are `node_id_invalid` /
`connection_label_invalid`, before/after.
**Invariant tension:** none — this is constraint *disclosure*, not weakened
validation; Stage-1 still enforces.

### F12 · Guided-binder rejections are largely fact-free: bare sites collapse to the schema complaint, factory sites carry one fixed message, the amend binder collapses everything to one boolean — class D

**Surface:** `bind_guided_reviewed_components`
(`guided/planning.py:2362-2742`): **9** bare
`AuditIntegrityError("guided planner candidate …")` raises (`:2399`, `:2403`,
`:2408`, `:2414`, `:2425`, `:2436`, `:2553`, `:2586`, `:2742`) vs **5** typed
`GuidedCandidateBindingRejected` raises (`:2463`, `:2483`, `:2578`, `:2597`,
`:2681`). The planner loop gives only the typed class coded feedback with
custody-safe connectivity facts; every bare site gets
`_canonical_schema_feedback()` (`pipeline_planner.py:3868-3874`) — "match the
canonical schema" — which names neither the failing component nor the
mismatch.
**Evidence.** A candidate whose source block names anything but the reviewed
source name (`:2403`) is told, in effect, "your JSON shape is wrong". The
server holds both halves of the real message (the reviewed names are already
in `guided_redacted_planner_context`, so naming the expectation is zero new
egress — the `elspeth-859e2702dd` custody judgment, already settled).
Binder rejections are also inherently first-defect-per-turn (exception
control flow); with `composer_planner_repair_budget = 2`
(`config.py:296`), two binder-shape defects exhaust the budget by
construction.
The rejection sweep widened this finding beyond my 9-vs-5 count: the
`_guided_delta_rejection` **factory** (`planning.py:1333-1341`) adds ~30 more
typed raises that all carry one fixed message ("guided planner candidate
delta violates reviewed mutation authority"), with ~22 distinct conditions
collapsed onto `guided_delta_authority_violation` and mostly **no facts at
all** (`connectivity == {}`); and the prose-revision (amend) binder
accumulates every violation across the whole reconstruction into a single
**boolean** (`planning.py:2852-2862`) that surfaces as one opaque
`guided_amend_contract_violation` with zero instance facts. Exactly one site
in the whole surface names the legal alternatives (`:2681`,
`declared_sinks` + `consumable_connections`). Typing is therefore only half
the fix; fact-carrying is the other half.
**Fix seam:** rejection message — type the 9 bare sites with the closed-code
+ connectivity pattern the 5 typed sites already use
(`elspeth-572c642dbf` precedent); give the factory codes per-condition facts
(what the delta touched vs what authority owns); split the amend binder's
boolean into per-violation facts. Aggregating multiple shape defects into
one rejection is a second-order improvement; facts come first.
**Measurement:** guided repair turns following `canonical_schema` or
fact-free `guided_delta_authority_violation` feedback whose next candidate
changes only component names/counts; REPAIR_EXHAUSTED dispositions on guided
surfaces, before/after.
**Invariant tension:** none — every fact named is either planner-authored
candidate content or a reviewed name the context already carries.

### F14 · Freeform mutation results are acknowledgments — the state itself never rides back — class C

**Surface:** `ToolResult.to_dict()` (`tools/_common.py:832-868`): emits
`success`, full `validation`, `affected_nodes` (IDs only), `version`, and
optional `data`/`validation_delta`/`post_call_hints`/`plugin_schemas`.
**`updated_state` is never serialized**; most mutations emit no `data` at
all (`_mutation_result` defaults `data=None`).
**Evidence (payload sweep, spot-checked).** After a successful mutation the
model knows it worked and which errors remain — not what the state now
contains where the server transformed its input (finalizer/materialization
effects, merged defaults). The `get_pipeline_state` description then invites
the read: *"Use this during correction loops to see what is currently
configured before patching."* `get_pipeline_state` is one of the four
`REDUNDANT_STATE_LIST_TOOLS` the tutorial harness flags — the redundant-read
pattern was observed often enough to gate against. Freeform-only: the
planner palette already omits `get_pipeline_state` entirely.
**Fix seam:** tool payload — on success, optionally carry the compact
round-trippable projection that already exists
(`get_pipeline_state(component="set_pipeline_arguments")`,
`sessions.py:1822-1831`) or a per-component echo of the applied block. Zero
new egress: it is the state the same caller just authored, on the surface
that already serves it via `get_pipeline_state`.
**Measurement:** compose-loop turns where `get_pipeline_state` follows a
successful mutation within the same request (discovery cache makes the
server side free; the provider turn is the cost).
**Invariant tension:** none.

### F9 · The rejection feedback's guidance line invites a provably no-gain `explain_validation_error` call — class D

**Surface:** `_allowlisted_candidate_feedback` (`pipeline_planner.py:2306-2311`):
*"To expand any code, call explain_validation_error with the exact code
string."*
**Evidence.** The feedback already inlines `(explanation, suggested_fix)` per
error from the closed catalogue, and that catalogue is the **single source of
truth** the tool reads: `explain_validation_code`'s docstring
(`tools/generation.py:1223-1249`) states the code-alone resolution returns
"the same guidance the `explain_validation_error` tool returns for the full
message". The only tool-side delta is `_augment_with_expected_hint`, mined
from `error_text` the model itself supplies — text already in its context. So
for every code the feedback enriched, obeying the guidance line costs one
full provider turn to receive byte-equivalent guidance; and
`validation.code:*` keys are not manifest-covered, so the no-gain guard never
flags it. The rejection sweep confirmed this independently, and added: in
withheld mode the call is *guaranteed* information-free
(`explain_withheld_validation_code` returns one fixed tuple for every code —
the same tuple already inlined). The single real delta is the
`"Expected …"` hint, which can fire only when the model passes the `detail`
string it already holds.
**Fix seam:** rejection message text — restrict the line to codes that
arrived *without* an inline explanation ("codes above without an explanation
can be expanded via explain_validation_error"), or drop it. Zero egress.
**Measurement:** repair-phase attempts whose `requested_information` is only
`validation.code:*` with empty `new_information`.
**Invariant tension:** none — this does not weaken validation; it stops
advertising a redundant read.

### F10 · Digest byte-budget overflow converts directly into forced catalog-detail turns — class A (measure first)

**Surface:** `planner_authoring_aids.py:965-966`
(`_DISCOVERY_DIGEST_MAX_CANONICAL_BYTES = 24 KiB`,
`_DISCOVERY_DIGEST_MAX_PUBLIC_TEXT_BYTES = 1 KiB`), omission mechanics
`:1455+`, and `discovery_digest_detail_tools` (`:1561-1580`) feeding
`PlannerDiscoveryPolicy.initial`'s `unresolved` set
(`pipeline_planner.py:244-281`).
**Evidence.** When a plugin's public purpose/prohibition prose does not fit,
it is replaced by `sha256` + `details_via`, the corresponding `list_*` tool
is retained in the palette, its information key seeded `unresolved`, and the
core instructs "follow `details_via` before selecting that plugin" — i.e.
each omission is one required discovery turn per affected selection, by
design (binding prohibition text must be read). The budget constant, not the
catalog, decides how often this fires.
**Fix seam:** context builder — this is a **measure-first** finding: read
`omitted_public_text_count` in the live deployment's rendered digest; if
routinely nonzero, raise the digest budget (bytes are cheaper than turns) or
prioritize prohibition prose over purpose prose under the cap. Zero new
egress (the omitted text is policy-visible public text the `list_*` tools
return to the same provider); flagged only as a per-request byte-cost
increase for the operator to weigh.
**Measurement:** rendered digest `budget.omitted_public_text_count` per
deployment; attempts whose `requested_information` is a
catalog-detail key on shapes that select an omission-affected plugin.
**Invariant tension:** none.

---

## Cross-checked open issues — linked, not duplicated

| Issue | Status | Relation |
|---|---|---|
| `elspeth-63cf3803e6` (planner-efficiency label; the calibrated 1×1 precedent) | verifying | Directions #1–#3 landed (`e69e46070`; `reviewed_configuration_usage`, step-3 no-transform branch; harness violation pre-existed). Direction #4 — the turn-count re-measure, target 1 candidate / 0 discovery — is **owed and blocked** on sketch excision. This review's table adopts that target. |
| `elspeth-b4a286d517` (server-authored pass-through, zero provider calls) | confirmed | The 1×1 shape's current "0 calls" is the anti-pattern; also the tutorial's step-2→3 cost — ADR-031 parity means the fix is composer-wide, never tutorial-special. See also `AUDIT-composer-invariants-2026-08-18.md` (V1-A) and `BUG-guided-passthrough-validation-deadlock.md` (the sketch's unrecoverable `VALIDATION_FAILED` loop — a defect the planner path cannot have, since repair varies candidates). |
| `elspeth-826765af90` (OptionValueConstraint withheld from projection) | fixing | Attributed the deferred-redemption residual repair turns in the table below. The projection (`planning.py:983-1045`) still exposes only `operator/value_type/value_present` for `OptionValueConstraint`. |
| `elspeth-d293c5d139` (contradictory deferred constraints accepted) | verifying | Unsatisfiable retained intent burns planner repair budget at redemption; linked as the other redemption-residual cause. |
| `elspeth-7bd0141bbe` (`user_term` registry undiscoverable on compose loop) | verifying | Partially addressed on this tree (aids now ride the compose catalog context, `prompts.py:236`); residue noted under F8. |
| `elspeth-f159d2394b` (wall clock cannot fund turn budget) | verifying | **Out of scope by the prompt's own boundary**: reducing turns is this review; funding them is that ticket. Every finding above reduces pressure on it. |
| `elspeth-aaa9e3f597` (planner invents `numeric_route` for routing) | verifying | Adjacent brief-sufficiency evidence: routing vocabulary discoverability. The capability core does teach gates-as-topology; that ticket tracks why the live planner missed it. Not re-reported. |

---

## Evidence & measurement notes

### Live evidence — local `sessions.db` planner attempt rows (read 2026-08-18)

The local database carries 55 `planner_attempt_audit` envelopes across **14
planner requests** (rows dated 2026-08-12 .. 2026-08-17; counted, not
estimated). Phase ×
outcome: 35 `discovery/discovery_executed`, 7 `prose/prose_nudged`,
6 `candidate/accepted`, 2 `candidate/candidate_rejected`, 2 `repair/accepted`,
1 `repair/candidate_rejected`, 1 `discovery/guard_fired(DISCOVERY_NO_GAIN)`,
1 `prose/prose_reply(MALFORMED_RESPONSE, terminal)`.

Reconstructed per-request sequences (`phase(tool_calls_in_turn)`):

- **The decision minimum is already achieved when the model happens to
  batch**: 3 of the 8 accepted requests ran exactly
  `discovery(3) → candidate(1)` = 2 provider calls.
- **The same surface pays 2× when it does not**: `discovery(3) →
  discovery(2) → discovery(1) → candidate(1)` (4 calls), and two other
  4-call accepted runs. Accepted-request call counts: 2, 2, 2, 3, 3, 4, 4, 4
  — median 3 against a minimum of 2. This is F1's entire case: the batching
  affordance demonstrably works and is used only by chance.
- **The serial tail is unbounded in practice**: one request spent **10
  consecutive discovery turns** (mostly 1 call each) and never reached a
  candidate; another spent 7 turns including a `DISCOVERY_NO_GAIN` guard
  firing; neither has a terminal attempt row (consistent with wall-clock
  death — the `f159d2394b` interplay).
- **The decline-shaped failure is live, not theoretical (F6)**: one request
  ended `discovery, discovery, prose×3 → MALFORMED_RESPONSE terminal` — an
  honest no-tool answer nudged twice and then misclassified, with no hatch
  engagement (the `:3721` path). Two more requests end at
  `discovery → prose` with no terminal row.
- **Prose choreography is ~15 % of all provider calls** here (8 prose
  attempts of 55) — each nudge is a full paid call answering a full paid
  prose call.
- Proposal provenance cross-check: `composition_proposals` shows
  11 `server/composer-guided-passthrough-synthesis` vs 11 provider-authored —
  matching `elspeth-b4a286d517`'s evidence exactly; the modal guided shape
  currently never reaches the planner at all.

### Instrumentation notes

- **Audit rows.** Planner attempts (`phase`, `selected_tools`,
  `requested_information`, `new_information`, `led_to`) are persisted
  interleaved with `ComposerLLMCall` rows by
  `_persist_pipeline_planner_audit` (`service.py:3970`) as
  `role='audit'` chat messages (`_kind: planner_attempt_audit`). A discovery
  attempt with nonempty `requested_information` and empty `new_information`
  is the no-gain signal every measurement plan above keys on.
- **Column semantics — two axes (evidence sweep, confirmed).** There is no
  `llm_call_audit` table; it is a `_kind` discriminator on `chat_messages`
  rows. *Row axis:* provider calls = `_kind:"llm_call_audit"` rows keyed by
  `planner_call_ordinal`; tool invocations = `selected_tools` inside
  `_kind:"planner_attempt_audit"` rows — deliberately not 1:1 (transport
  failures own no attempt row; `audit.py:384-437`), so counting provider
  calls off attempt rows undercounts failures. *Column axis:* `content` is a
  summary that **omits** `selected_tools`/`requested_information`/
  `new_information` and all token detail; any tool-count or token query must
  read the `tool_calls` JSON envelope. Also: `attempts` is an in-memory
  counter, never persisted (the row field is `ordinal`), and the
  `del tool_calls` at `pipeline_planner.py:1112` is a deliberate
  single-source-of-truth no-op, not a lost metric — don't chase either. The
  f159d2394b measurement (11 provider calls, 6 charged turns) illustrates
  why ordinals, not attempt rows, are the call count.
- **Tutorial harness (verified in-tree).** The generic-route gate in
  `tutorial-harness.ts` is tight: `REDUNDANT_STATE_LIST_TOOLS =
  {get_pipeline_state, list_sources, list_transforms, list_sinks}` (`:125`),
  `redundantSelections` a violation (`:543-545`, since `752e404ea`),
  `noGainTurns == 0` (`:541`), `repairTurns == 0` (`:542`), **≤ 2 provider
  calls** (`:518`), phase profile exactly `candidate` or
  `discovery,candidate` (`:524`), and — notably — **zero provider calls is
  itself a violation** (`:510-514`), so this gate would fail the current
  sketch bypass on the tutorial shape. Gating: `**/*.staging.spec.ts` is
  excluded from the CI e2e lane (`playwright.config.ts:97-101`; no local
  webServer, needs `tests/e2e/.auth/staging-user.json`) and runs only via
  `npm run test:e2e:staging` (`package.json:24`) against a live staging
  deployment. Findings F1, F3, F5, F7 — and the bypass itself — would
  register there and *only* there today; nothing in the CI-equivalent
  `pytest tests/` run measures turn counts. Every before/after above
  therefore needs either a staging deployment or the local live gate.
- **Dispositions.** Planner disposition codes (`DISCOVERY_ONLY`,
  `MALFORMED_RESPONSE`, `REPAIR_EXHAUSTED`, …) appear only in `journalctl` —
  required for F6 and F8 baselines.
- **Parity cross-check (class B), counted.**
  `config/cicd/runtime_rejection_parity.yaml` holds **289 sites**: 132
  `mirrored`, 80 `not_authorable`, 64 `structural`, 10 `unmirrored`
  (ratcheted), 3 `abstains`, 0 `unadjudicated` (counted twice independently
  — my pass and the evidence sweep agree exactly). `mirrored` means Stage 1
  rejects the same predicate — i.e. the planner meets the wall **by
  rejection**; it says nothing about pre-call discoverability. Weaker still
  (evidence sweep): the CI gate's verification is **name-presence only** —
  `scripts/cicd/runtime_rejection_parity.py:419` checks each counterpart
  code appears as a string literal somewhere under `src/elspeth/web/`; it
  cannot detect a counterpart that rejects a *different* predicate (the YAML
  header concedes this). The ratchet is **at its ceiling** (10/10 unmirrored,
  zero headroom), the script's docstring points at a gate file that does not
  exist (`tests/integration/pipeline/...` — the live gate is
  `tests/unit/scripts/cicd/test_runtime_rejection_parity_gate.py`), and one
  mirrored entry's own note says its counterpart is working-tree-only. Two
  confirmed non-discoverable mirrored families found by this walk: the
  naming rules (F11 — counterparts `node_id_invalid` (12 sites) /
  `connection_label_invalid` (16 sites), the two most common counterpart
  codes in the corpus) and the withheld `OptionValueConstraint`
  (`826765af90`). A mechanical rule-by-rule sub-audit over the 70 distinct
  counterpart codes is sized at one pass, recommended as follow-up rather
  than expanded here.

## Coverage honesty — what was walked and by whom

Walked personally, in full or in the load-bearing part: all five guided step
briefs and both loaders; the freeform skill's operating contract, batching,
integrity, tool-inventory and termination sections; the capability core in
full; both guided context builders and the deferred-constraint projection;
the planner loop end-to-end (manifest, palette, budgets, nudges, hatch,
rejection synthesis, discovery dispatch); the authoring-aids builder,
digest, and schema-evidence machinery; the compose-loop context builder and
loop head; the guided chat route and all three per-step solver entry points;
the binder; `explain_validation_error`; the canonical terminal schema
(dumped live); the parity corpus (counted); the tutorial harness and its
gating; local `sessions.db` planner audit rows.

Covered by the three inventory sweeps (reported late; key claims
spot-checked, full re-derivation not repeated): the complete 42-tool payload
inventory including the `ToolResult.to_dict()` envelope and all three
palettes (F13, F14, F15's evidence); the full rejection-message inventory
across binder, factory, policy, and tool-error channels (F12's widening,
F16's evidence); the audit-row axes and parity-gate semantics.

Residual not-determined items, inherited from the sweeps' own honest lists:
semantic correctness of the 132 mirrored counterparts (the CI gate checks
name-presence only — the rule-by-rule discoverability pass above); whether
the configured provider model emits parallel tool calls unprompted (a
runtime question F1's fix text answers by instruction); `set_pipeline`'s
full ~245-line JSON schema body; `_VALIDATION_ERROR_PATTERNS` collision
analysis; blob/secret tool payloads (7 of 11 are in the planner palette);
and whether any out-of-repo runbook fires the staging battery.

## Out of scope, stated plainly

- Turn-budget funding (`f159d2394b`) — adjacent ticket.
- The advisor checkpoint architecture (EARLY/END) — review controls; reducing
  them is weakening review, not a shortcut.
- Reuse/memoization of provider outputs across requests — invariant question;
  not tempted by any finding above (F2 re-supplies *server-owned catalog
  facts*, never provider output).

---

## Calls per canonical shape — decision minimum vs. choreographed reality

Derived statically from the briefs, context builders, and loop choreography
above; no shape-labelled live calibration exists on this tree, but the local
audit rows in the Evidence section support the choreography claims in aggregate
(2-call requests exist exactly when the model batched; serial tails reach 10
turns). "Current" assumes the model follows its brief as written and the
happy path (no repairs beyond listed nudges); repair-budget tails are noted.

| Shape | Decision minimum | Current (choreographed) | Residual gap attributed to |
|---|---|---|---|
| Rootless 1×1 pass-through | **1** (candidate only; context fully determines it) | **0 — banned sketch** (`b4a286d517`); planner path statically ≈1 since `e69e46070`, re-measure owed (`63cf3803e6` #4) | sketch excision + owed measurement |
| 1×1 + one named transform | **2** (1 batched discovery: schema; 1 candidate) | 3–4 (serial schema turn + assistance turn + candidate) | F1, F7 |
| 1×1 + LLM transform | **2** | 4–6 (+ `list_models`, twice when the provider is unknown) | F1, F3, F7 |
| Gate + fork + coalesce, user-stated thresholds | **2** (1 batched discovery: branch schemas; 1 candidate — grammar & exemplar in context) | 4–7 (grammar turn + per-plugin schema turns + assistance + candidate; one-shot threshold/nodeless nudges add ≤1 each when they fire — **accepted**: fidelity guards with omit-valves) | F1, F5, F7 |
| Multi-output routing | **2** | 3–5 | F1, F7 |
| Correction: option patch | **1** (delta candidate; schema already session-loaded) | 2–3 (schema re-fetch + candidate; +1 repair when the withheld constraint bites) | F2; `826765af90` |
| Any guided candidate with a binder-shape or naming slip | +0 (constraint visible at authoring time) | +1–2 repairs (generic or fact-free complaint; naming rule learned by rejection) | F11, F12 |
| Any candidate with ≥2 defective components | +1 repair (one rejection names all) | +1 repair per component, serially revealed; ≥3 ⇒ REPAIR_EXHAUSTED at budget 2 | F16 |
| Any `plugin_options_invalid` repair (planner surface) | +1 repair (schema facts inline) | +2 (schema fetch turn + repair) | F15 |
| Guided step-2 sink resolution, flat-option sink | **1** (digest with `config_fields` in context) | 3+ | F4, F13 |
| Correction: wire fix | **1** | 1–2 (connectivity facts now ride rejections — `5904b1683a` landed; residual repair only on genuinely new information) | — (largely closed) |
| Correction: node replacement | **2** (new plugin schema + candidate) | 3–4 | F1, F2, F7 |
| Deferred-intent redemption at target stage | **+0** (rides the stage's planner call; claim in terminal payload) | +0 happy path; +1–2 repairs when the exact `OptionValueConstraint` is withheld or the retained conjunction is unsatisfiable | `826765af90`, `d293c5d139` |
| Tutorial script (ADR-031: same backend) | same as its constituent shapes | same, **except** step-2→3 currently costs 0 via the sketch — a parity breach, not a shortcut | `b4a286d517` |
| Intent naming an unavailable capability | **1** (digest proves absence in-context; one decline) | 4–7 (probe + 2 prose nudges + exhaustion + hatch), or a *misclassified* `MALFORMED_RESPONSE` terminal | F6 |
| Guided step-2 sink resolution, structured-option sink | **2** (schema round + resolve round, digest in context) | 3+ (list_sinks round first; advisory-fallback round on empty outcomes) | F4 |
| Freeform simple build (per user message) | mutation turn + finalize turn (+ EARLY/END advisor calls) | same | **accepted with reason**: the finalize turn carries the user-visible attestation after validation results; the advisor checkpoints are review controls, and weakening review to save calls is outside the fix space by rule |
| Guided chat, steps 3/4 (per message) | 1 | 1 (deferred-management call folds prose replies) | — (already minimal) |
| Guided chat, step 1 (per message) | 1 | 1–2 (false-decline retry burns attempt 2; empty/hallucinated outcomes fall back to a second advisory call) | server-side retry heuristics compensating for brief gaps — acceptable compensators; measure retry rate before proposing brief text |
