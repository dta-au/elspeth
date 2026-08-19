# Composer Invariant Audit — release/0.7.2 @ 07c803703

**Date:** 2026-08-18 · **Scope:** the Web Composer authoring path
**Method:** 15-agent workflow — 7 surface-scoped finders → 7 adversarial verifiers
(instructed to default to REFUTED) → 1 completeness critic — plus lead-confirmed
findings established directly. 363 pipeline-structure construction sites were
catalogued and given a provenance verdict.

**Audited against** the two invariants added to `AGENTS.md` today in `fc85f7930`.

---

## Verdict

**Both invariants are breached.** They are breached by *one root defect, in two
moves*:

1. The guided composer **removed the planner** from the step-2→3 transition and
   synthesizes the proposal server-side (Invariant 1).
2. When that server-authored proposal proved unacceptable in the tutorial, a
   **tutorial-only frontend branch was added to hide it** rather than fixing the
   general surface (Invariant 2).

Move 2 is the exact failure ADR-031 exists to prevent. The tutorial went red on
a real machinery defect — which is its designed purpose — and the canary was
silenced instead of heeded.

Alongside that spine, the sweep confirmed **two further Invariant-1 breaches**
and surfaced **three questions that are yours to rule on, not the audit's**.

### Counts

| | |
|---|---|
| Raw candidates raised by finders | 35 |
| CONFIRMED by adversarial verify | 5 (+2 by the critic, +1 lead-confirmed) |
| GREY ZONE — needs a developer ruling | 14 |
| REFUTED on independent code reading | 16 |
| Construction sites catalogued | 363 (121 server-authored, 95 provider, 80 user, 58 replay, 9 unattributed) |

Distinct defects after merging duplicate reports of one mechanism: **4 confirmed
Invariant-1 breaches, 1 confirmed Invariant-2 breach, 3 items for ruling.**

---

## Confirmed — Invariant 1

### V1-A · CRITICAL · The guided "Starting sketch" is a server-authored graph
`src/elspeth/web/composer/service.py:3653-3782` · `pipeline_planner.py:2902-3019`

A gate (`passthrough_sketch_shape`, :3653) selects rootless single-source /
single-output guided turns. On that path the server builds `sketch_pipeline`
(:3707-3742) as a literal dict — source plugin+options from
`guided.reviewed_sources[...]`, `"nodes": []`, `"edges": []`, sink from
`guided.reviewed_outputs[...]`, `metadata.name = "Starting sketch"` — and seals
it with `provider="server"`,
`model_identifier="composer-guided-passthrough-synthesis"`,
`tool_call_id="server-passthrough-<hash>"`. **Zero provider calls.**

The invariant names this exactly: banned "regardless of what it is called
(sketch, … synthesis) and regardless of whether it is later superseded", and
`provider="server"` must not author pipeline structure.

**Both clauses are breached, not just the second.** The plan returned to
`post_guided_respond` (`routes/composer/guided.py:4876`) becomes the session's
`active_proposal` (STEP_3_TRANSFORMS requires one), is sealed with custody and an
authority hash into the audit trail, and — the completeness critic verified this
independently — **is committable; no downstream gate withholds it.**

The in-code justification is a latency argument: *"tutorial final3 spent 222s of
provider time producing it (op 424021cd)"*. The invariant preamble forecloses
that class of argument in its first sentence.

**It is a pathway, not a line.** `prepare_pipeline_plan` is an exported,
`__all__`-listed API whose docstring reads *"Prepare a server-derived pipeline
through the planner's final gate."* Its `:2927` comment names two consumers,
"recipe router, guided sketch". The recipe router was **excised on 2026-08-13**
(`9700470e2`, *"Excise the prose-intent zero-LLM recipe path (banned
recipe_match pattern)"*). A whole-tree grep confirms `provider="server"` appears
on exactly **two lines, both this sketch**, and `prepare_pipeline_plan` has
exactly **one production caller**. The sketch is the surviving sibling of an
already-condemned class, and the seam has since grown its own client-visible
error vocabulary (`VALIDATION_FAILED` "on the server-derived path" —
`routes/_helpers.py:2308`, `guided_plan.py:213`, planner `:3009-3018`).

**It is test-locked.** `tests/integration/web/composer/guided/test_respond.py:890`
— `test_rootless_step_3_entry_synthesizes_the_sketch_without_a_provider_call`,
parametrized `("live","tutorial")` — asserts the breach as a requirement: *"It
must now seal server-side through the same canonical final gate
(prepare_pipeline_plan) with zero provider calls."* The suite will go red on the
fix. Remediation footprint is small: **12 references across 4 test files.**

**Correction to the existing bug note.** `BUG-guided-passthrough-validation-deadlock.md`
diagnoses the deadlock as "structural, at `pipeline_planner.py:2985-3018`". It
is not. The deadlock is a *consequence* of this breach: with no provider in the
path, nothing can vary between retries, so there is nothing to repair. The note
observed the mechanism and assigned the cause one layer too low.

### V1-B · HIGH · Guided chat derives the source **plugin** from uploaded file content
`routes/composer/guided.py:497, :646` · `guided_chat_atomic.py:741, :774, :1534`

On the SINGLE_SELECT lane, `guided_chat_atomic.py:1088-1099` gates on the
**request payload** alone; `:1156` takes the `uploaded_bind` branch and
`provider_runner` (`:1188`) is unreachable. `_step_1_plugin_hint` returns None,
so `guided.py:601-605` falls through to
`_step_1_plugin_for_uploaded_inspection`, which picks the plugin by inverting a
hardcoded content-kind table (`emitters.py:189-196`: csv→`csv`, json/jsonl→`json`,
text→`text`).

Authored: `SourceResolved(name="source", plugin="csv"|"json"|"text",
options={"path": "blob:<uuid>", "schema": <derived from inspected headers>,
"on_validation_failure": "discard"})`, written into guided authority via
`transition_source_plugin_selection(...)`. The plugin identity came from neither
the user nor the provider. Server prefill of *values* is the accepted wizard
pattern; the *plugin choice* is not — on the control path the user picks it,
here the server does.

### V1-C · HIGH · Required-control auto-wire splices server-authored nodes into the proposal
`required_controls.py:425/435/489/550/623/713/872` — **two triggers**

- **Every planner candidate**, via the finalizer seam (`service.py:393/412`,
  `pipeline_planner.py:2935`, `tool_batch.py:329`).
- **Any ordinary incremental mutation tool** — `upsert_node`, `upsert_edge`,
  `splice_transform`, `set_output`, `patch_node_options`, `set_source` — via
  `tool_batch.py:1956-1968` → `wire_required_controls_state`.

Authored, concretely: node ids minted as `f"{capability.value}_auto_{n}"`
(`prompt_shield_auto_1`); `node_type: "transform"`, `on_error: "discard"`;
plugin from `_selected_control_profile` (a deployment-catalog lookup keyed on a
capability — no planner input reaches it); synthetic streams `{id}_in`/`{id}_out`;
and **three rewrites of the planner's own edges** (`input`, `on_success`, and the
source's `on_success`). Options are wholly server-written, including literals
from `_DIRECT_CONTROL_OPTION_EXEMPLARS`.

On the incremental trigger the effect is starkest: the provider response names
**one** component, and the state returned carries that component **plus a node
whose identity appears in no tool call, no message, and no provider payload** —
then `tool_batch.py:1261-1274` rebuilds the proposal arguments and **relabels the
tool `set_pipeline`**, so the user approves the server-inserted node under a
label the planner never emitted.

> **This one needs your ruling** — see *Questions for you*, Q1. The mechanism is a
> deliberate, disclosed safety feature protecting runtime data. The audit's
> position is narrow: the invariant's carve-out says *admission gates*, and this
> is an injector, not a gate. The genuine gate exists separately and fail-closed
> (`CHECK_REQUIRED_CONTROL_COVERAGE`, `execution/_validation_authoring.py:82-87`,
> applied at `execution/validation.py:341`).

---

## Confirmed — Invariant 2

### V2-A · HIGH · A tutorial-only branch hides the V1-A defect from the canary
`frontend/src/components/chat/guided/ProposePipelineTurn.tsx:472`

```
{!(isTutorial && payload.supersedes_draft_hash === null) && (<Button … "Review wiring">)}
```

That button is the file's only emitter of the `review_wiring` advance. On an
**identical server payload**, a tutorial session cannot reach `confirm_wiring`
and a live session can.

The backend states the workaround as policy. `guided/protocol.py:316-318`,
in the `ProposePipelinePayload` docstring:

> *"The tutorial frontend withholds 'Review wiring' until the frozen-prompt
> revision arrives (tutorial run 18: the pre-Send auto-proposal is a
> source→sink passthrough and must not be acceptable **there**)."*

The word **"there"** is the finding. The passthrough is unacceptable in the
tutorial and remains acceptable in every live session. ADR-031 bans
"tutorial-only normalisation, shortcuts, or defect workarounds" by name and
rejects "tutorial-only normalisation to keep the walk green" in its
Alternatives. This is that, precisely — and the defect being hidden is V1-A.

---

## Questions for you — not the audit's to rule

**Q1 · Does "required-control admission gates" cover auto-wiring?**
V1-C is a documented operator decision (`elspeth-f99655f540`, R2-F10),
deployment-gated, idempotent, creditability-checked, and every inserted node
stages an acknowledgeable `required_control_auto_wired` decision. If you read the
carve-out broadly enough to cover *wiring* the control rather than only
*refusing* an uncovered graph, V1-C drops entirely. If not, the pass must become
a rejection that hands the finding back to the planner as a repair turn — which
is currently *structurally excluded*: the pass runs after the terminal tool call
is parsed and is contractually forbidden from raising
(`required_controls.py:722-727`). Either the invariant wording or the mechanism
has to move.

**Q2 · Recipes: the planner chooses the name, the server writes the graph.**
`apply_pipeline_recipe` is **live in the freeform composer's LLM tool palette** —
`_get_litellm_tools` (`service.py:6479`) passes the unfiltered tool universe to
the model at `:5880` — and is also advertised to any external MCP client
(`composer_mcp/server.py:123`). A recipe *name* expands server-side into a
complete 3-to-6 component graph: `_build_classify_recipe` (node `classifier`,
plugin `llm`, sink `labelled`), `_build_threshold_recipe`,
`_build_fork_coalesce_truncate_recipe`, `_build_web_scrape_project_recipe`, and
`_build_legacy_web_rating_recipe` — the last of which **hardcodes slot values the
caller never supplied**. The planner choosing a recipe is the LLM doing the job;
the server materialising nodes the LLM never uttered is "template … in place of
the planner".

**The audit's position is that this is a breach**, and the question to you is
whether you rule the tool-callable form carved out — not which of two equal
readings wins. Two facts tip it. First, the composer's **own skill prompt steers the model
toward recipes**: `skills/pipeline_composer.md:892` — *"Use `apply_pipeline_recipe`
when `list_recipes` returns a recipe that matches the requested shape. If no
recipe matches a complex multi-path shape, use advisor help when available
**before hand-authoring**."* Hand-authoring — the planner doing the job — is
positioned as the last resort. Second, the precedent: the *prose-intent* recipe
path was excised on 2026-08-13 as a banned `recipe_match` pattern; this is its
tool-callable sibling, and it is not merely reachable but recommended.

That is not the planner choosing a tool. That is the system instructing the
planner to prefer server-side expansion over authoring.

**Q3 · Does Invariant 2 govern the frontend, and how far?**
The tutorial sweep established a decisive structural fact: **the backend grants
the tutorial zero prompt privilege** (`guided/prompts.py:77` keys the planner
skill on the guided *step*; the frozen prompt lives entirely in
`TutorialGuidedShell.tsx:40-43`). Under ADR-031 that means any *backend* tutorial
branch has no permitted category to fall into — and the sweep found none that
survived verification (see Cleared). But the *frontend* carries a substantial
`isTutorial` surface, 8 sites of it, ranging from clearly-defect-masking (V2-A,
confirmed) down to plain chrome. The middle band needs your line:

- `ChatPanel.tsx:2253` — `suppressTutorialSingleSelect` hides a server-emitted
  `single_select` widget in tutorial sessions only. *(closest to V2-A)*
- `ChatPanel.tsx:2747` — tutorial-only short-circuit replaces the freeform
  fall-through with a placeholder surface.
- `ChatPanel.tsx:2229` — tutorial-only derived build state selects which
  composer surface renders.
- `AcknowledgementStack.tsx:254/274` — tutorial sessions cannot amend or opt out
  of LLM interpretations; composition-mutating controls are suppressed.
- `TutorialTurn4Run.tsx:41` — run narration is **fabricated from fixed client
  timers** (2s/6s `setTimeout`), not backend progress. Flagged as an honesty
  issue independent of the invariant.
- `GuidedTurn.tsx:72` and `SchemaFormTurn.tsx` — `isTutorial` threaded as a prop
  through shared components; census says every site is a render branch.
- `profile.py:77` — `WorkflowProfile(coaching, bookends)` gives tutorial sessions
  two non-prompt behaviour toggles. Small, but it is *behaviour*, not prompt.

---

## Cleared — notable negatives

These were suspected and came back clean on independent reading. Recording them
because a negative result is data.

- **The no-tool path is not a fallback.** `finalize_no_tool_response`
  (`no_tool_finalize.py:59`) returns the assistant's own text; it authors no
  pipeline. This was the single highest-prior suspicion going in.
- **No second synthesis path exists.** Whole-tree: `provider="server"` on exactly
  two lines, both V1-A; `prepare_pipeline_plan` has one production caller.
- **The frontend constructs no pipeline structure that crosses the wire.**
  Zero non-test hits for local composition mutation; `guidedDecoder.ts` is a
  strict validating decoder that throws rather than defaulting on any identity
  field.
- **`composer_mcp` is not a parallel implementation** — it filters the shared
  tool registry. *(But it never runs `wire_required_controls`: a two-surface
  parity defect, not an invariant breach.)*
- **The catalog describes, it does not select** (`catalog/policy_view.py`).
- **`_producer_resolver` resolves the producer *of* an existing connection**; it
  never chooses a producer node. **`state_claim_grounding` corrects prose**; it
  fabricates nothing.
- **The tutorial run path uses the shared executor.** `POST /api/tutorial/run` →
  `run_tutorial_pipeline` → the same `ExecutionService.execute`. The endpoint is
  an HTTP envelope, not a divergent path.
- **`_tutorial_launch_blocker`** returns `tuple[str,str] | None` → a 409. It is a
  rejection (carve-out), and it exists specifically to **refuse** the
  zero-transform passthrough.
- **`redaction.py` (4,425 lines), `execution/_validation_authoring.py`,
  `execution/preview.py`** — zero hits on the entire construction shape set.

Two real bugs surfaced that are **not** invariant findings, recorded so they are
not lost:
- `tutorial_service.py:712` selects sessions by bare title-string equality, so a
  user who titles any session exactly `"First-run tutorial (in progress)"` gets
  it renamed by the orphan sweep.
- `tutorial_service.py:73` and `audit_readiness/service.py:62` hold **divergent
  copies** of the tutorial transform-shape template.

---

## Coverage and limits — read this before treating the list as exhaustive

The completeness critic states plainly that it **cannot** account for every
construction site, and neither can this report.

**The modality that was not run:** the sweep was file-list-driven, so directories
outside the seven assigned lists were invisible regardless of content. Four were
opened by no finder — `web/plugin_policy/` (3,566 lines),
`web/shareable_reviews/` (1,274), `web/interpretation_state.py` (2,188),
`web/sessions/converters.py`. The critic read all four: **all clean.** Their
absence was the failure, not their content.

**Grep-cleared but not read:** `web/blobs/`, `web/secrets/`, `web/coordination/`,
the `execution/_validation_*` family, the websocket/SSE surface
(`execution/websocket_ticket.py`, `execution/progress.py`), `src/elspeth/mcp/`,
`src/elspeth/tui/`, `core/dag/builder.py`. The construction shape set returned no
composer-authoring hits in any of them, but a site using none of those shapes
could hide there.

**Not read in full:** `web/sessions/service.py` (13,396 lines) was read by
region, not linearly — its slice says so explicitly. `state.py` (6,769),
`guided/chat_solver.py` (3,564), `guided/protocol.py` (2,352) and
`guided/deferred_intents.py` (2,244) were cleared by construction-shape grep plus
reading every enclosing function the grep hit.

**Nine sites remain `unattributed`** — chiefly envelope constructors
(`PipelineProposal.create`, `pipeline_planner.py:2758/2882`) that inherit their
caller's provenance rather than having one of their own, and the three
`post_guided_respond` staging sites, one of which the critic then attributed
(that attribution became the V1-A propagation confirmation).

The frontend was audited once and not re-audited by the critic.

**Independently re-verified by the lead** (not inherited from an agent): the
`protocol.py:316-318` "there" quote; `ProposePipelineTurn.tsx:472`;
`_allocate_node_id`; `tool_batch.py:1963`; the `CHECK_REQUIRED_CONTROL_COVERAGE`
gate; `apply_pipeline_recipe`'s registration and skill-prompt instruction; that
**no prompt or skill loader takes a profile** (every one is keyed on
`GuidedStep`; `prompts.py`'s only `profile` hits are unrelated plugin *profile
aliases*), which is what makes Q3's backend/frontend split sound; and that
`pipeline_commit.py` **only reads** `composer_provider` for audit projection —
there is no conditional on it and no `supersedes_draft_hash` reference in the
module, so nothing at commit withholds a `provider="server"` proposal.

---

## Recommended sequence

1. **Rule Q1 and Q2 first.** They determine whether V1-C and the recipe expansion
   are findings at all, and Q1 may require an invariant-wording change rather
   than a code change.
2. **Fix V1-A at the pathway, not the call site.** Deleting the sketch block
   alone orphans `prepare_pipeline_plan`, its `VALIDATION_FAILED` class and its
   SPA vocabulary — standing and ready for the next caller. Removing the pathway
   also dissolves the deadlock in `BUG-guided-passthrough-validation-deadlock.md`.
3. **V2-A dies with V1-A.** The tutorial branch exists only to hide the sketch;
   remove the sketch and the branch has nothing to withhold. If you remove the
   branch first instead, the tutorial will go red on V1-A — and under ADR-031
   **that red is the designed output, not a cost**: tutorial reds are machinery
   investigations, paid deliberately. Treating the red as something to avoid is
   the same reasoning that produced V2-A.
4. **Invert the test.** `test_rootless_step_3_entry_synthesizes_the_sketch_without_a_provider_call`
   currently asserts the breach. It must become an assertion that the planner
   *is* called.
5. **V1-B** is independent of the above and can be fixed on its own.
6. **Close the coverage gap** on the grep-cleared directories before treating
   this finding list as exhaustive.
