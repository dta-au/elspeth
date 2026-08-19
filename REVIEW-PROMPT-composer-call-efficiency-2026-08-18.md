# Composer call-efficiency review — find the missing shortcuts, keep the LLM

## Mission

Walk the ELSPETH Web Composer end to end — session start to sealed proposal,
guided and freeform, per-step chat, corrections, deferred-intent redemption —
and find every place the provider is forced to spend a call it should not
need. The unit of cost is the **provider call / planner turn, never
wall-clock seconds**. A planner that interrogates state tools, or hammers a
mutating tool until something validates, is reporting that its brief,
context, or tool affordances are insufficient — that is the defect, and
latency is only its symptom.

Your deliverable is a ranked findings list of **missing shortcuts**: changes
to briefs, context builders, tool payloads, rejection messages, and turn
choreography that reduce call counts **while the LLM remains the author of
every candidate**. You are reviewing and proposing, not implementing.

## Hard invariants — these bound the fix space, read before anything else

The two composer invariants in `AGENTS.md` are non-negotiable and no
latency/cost/convenience argument touches them:

1. **The LLM does the job.** Never propose a fix in which the server
   synthesizes, templates, routes, matches, or otherwise authors pipeline
   structure in place of the planner — regardless of name (sketch, recipe,
   router, fallback, fast path, cache) and regardless of whether the planner
   later supersedes it. Two prior violations were built and excised
   (`recipe_match` at `9700470e2`; the `passthrough_sketch_shape` bypass
   `b073d248e`, ticketed for removal as `elspeth-b4a286d517`). If the sketch
   bypass is still in the tree when you review, it is the **anti-pattern**,
   not a precedent. Note the nuance: the *explicit* recipe tools on the MCP
   surface (`list_recipes` / `apply_pipeline_recipe`) are the LLM doing the
   job with a tool; the banned thing is server-side auto-routing around the
   planner.
2. **No tutorial-special paths.** The tutorial runs the same backend
   (ADR-031). A shortcut that helps the tutorial must be a composer
   shortcut.

Additional fix-space rules:

- **Explain a redaction rather than lifting it.** Any proposal that adds
  data to the provider-visible context must show **zero new egress**
  (restating what the server already sends or already enforces), or be
  flagged as an egress decision for the operator — never silently proposed.
- **Do not weaken validation to save repair turns.** Accepting more junk is
  not a shortcut.
- **Reuse/memoization of provider outputs across requests** (replaying a
  previous LLM's structure for a "same-shaped" request) is an invariant
  question, not an efficiency fix. Surface it if tempting; do not propose it.
- Findings whose only possible fix is server-side authoring must be
  reported as exactly that and left unproposed.

## Method: derive the decision minimum, then explain every call above it

For each canonical flow (list below), derive the **decision minimum**: with a
fully sufficient context and ideally-shaped tools, how many provider calls
does this flow require? That equals the number of genuine product decisions
the server cannot make — intent interpretation, transform selection,
threshold/mapping choices the user stated in prose. Then count the
**choreographed reality** (from code + evidence). Every call above the
minimum must be attributed to a defect class below or explicitly accepted
with a reason.

The discriminating question for every extra call: **was the information this
call sought already in the model's context?**

- **Yes** → brief defect. The model wasn't told it has it, or wasn't told it
  is sufficient/authoritative. (Precedent: `elspeth-63cf3803e6` — the
  rootless 1×1 pass-through cost four discovery turns because nothing said
  reviewed option values are server-restored.)
- **No, but the server knows it** → context-assembly defect. Add the fact or
  a usage line naming it, under the zero-new-egress rule. (Precedent:
  `unproducible_output_fields` — the server names the gap instead of letting
  the planner rediscover it by rejection.)
- **No, and the server doesn't know it either** → genuine discovery. Now
  check the **tool affordance shape**: could one call return what currently
  takes three?

And always check the other direction: **what does the server do with the
model's output after the call?** Any field the binder overwrites
(`bind_guided_reviewed_components` restores reviewed source/output
plugin+options; correction flows server-own untouched nodes) is a field the
model never needed to supply — the context should say so, and the brief
should not require work to produce it.

## Where to walk

- Guided briefs: `src/elspeth/web/composer/guided/skills/` (`base.md`,
  `step_1_source.md`, `step_2_sink.md`, `step_3_transforms.md`,
  `step_4_wire.md`) and their loaders in `guided/prompts.py`.
- Freeform skill: `src/elspeth/web/composer/skills/pipeline_composer.md` and
  `pipeline_capabilities.md`, assembled in `composer/prompts.py`.
- Provider-visible context builders: `guided_redacted_planner_context`,
  `guided_redacted_current_state_context`, `_provider_safe_deferred_constraint`
  and friends in `guided/planning.py`; the `reviewed_context` enrichments in
  `service.py` (`unproducible_output_fields`, usage lines).
- The planner loop: `pipeline_planner.py` — phases
  (discovery/candidate/repair), rejection synthesis, nudges, repeat-rejection
  handling, escape hatches, turn/wall budgets.
- Tool result payloads: `get_pipeline_state`, `list_sources`,
  `list_transforms`, `list_sinks`, `get_plugin_schema`,
  `get_plugin_assistance`, `explain_validation_error`, and every mutating
  tool's return value (does each mutation return enough updated state that a
  follow-up read is never needed?).
- Rejection/repair text: every `GuidedCandidateBindingRejected` /
  `PipelineCandidatePolicyRejection` / planner ToolResult error — does the
  message name the minimal fix, and does one call report **all** defects or
  only the first (each first-error-only cycle is a full provider call)?
- The chat↔planner seam: does the per-step chat solver re-ask or re-derive
  what the planner context already carries? Do staged handoffs re-establish
  facts?
- The interpretation-review loop (`request_interpretation_review`) and its
  discoverability (see open bug `elspeth-7bd0141bbe`: the `user_term`
  registry is undiscoverable on the compose loop).

## Shortcut classes to hunt (house precedents in parentheses)

- **A. Context sufficiency** — facts present but not marked sufficient;
  redactions with no stated contract (`option_keys` →
  `reviewed_configuration_usage`); server-derived facts the model is left to
  re-derive.
- **B. Constraint disclosure** — walls the model meets by rejection instead
  of by statement: binding-time constraints withheld from the projection
  (open: `elspeth-826765af90`, the exact `OptionValueConstraint` is
  withheld); closed vocabularies and naming rules (leading letter, ≤38
  chars, reserved `fork`/`continue`/`on_success`, lowercase sinks) — is each
  authoring-time rule *visible to the planner* or only enforced after the
  call? Cross-check the runtime-rejection parity corpus
  (`config/cicd/runtime_rejection_parity.yaml`): every `mirrored` runtime
  rule should also be *discoverable*, not merely mirrored.
- **C. Tool affordance shape** — results that force follow-ups (schema and
  assistance as two calls; no batched multi-plugin fetch); mutations that
  don't return updated state; catalog lists that omit the field needed to
  select without a second read.
- **D. Rejection quality** — rejections that name symptoms not causes,
  report one defect per turn, or omit the legal alternatives (precedent:
  `elspeth-859e2702dd` — withheld component names doomed candidates to
  "unknown node" rejections the model couldn't see through; fixed by naming
  them in `6a54abbdc`).
- **E. Choreography** — turns that exist for protocol, not information:
  discovery-then-candidate when discovery predictably adds nothing; prose
  nudges as standalone turns; confirmation round-trips that could fold into
  the same call.
- **F. Brief ordering** — expensive default paths with no cheap-path branch
  (precedent: step-3's discover-first with no no-transform branch, fixed in
  `e69e46070`/`78649f625`); unconditional "load the schema for each
  selected plugin" instructions where the context already carries the needed
  facts for reviewed components.

## Canonical shapes to cost

Derive minimum vs. actual for at least: rootless 1×1 pass-through (minimum:
1 candidate call, 0 discovery — this is the calibrated target from
`elspeth-63cf3803e6`); 1×1 with a single named transform; gate + fork +
coalesce with user-stated thresholds; multi-output routing; each correction
kind (option patch, wire correction, node replacement); deferred-intent
redemption at its target stage; the tutorial script (same backend, no
special paths); an intent naming an unavailable capability (the named-gap
path should not cost extra probing turns).

## Evidence and measurement

- Planner LLM audit rows: `attempts` with `phase`, `selected_tools`,
  `requested_information`, `new_information`, `led_to` — a discovery turn
  with `requested_information` nonempty and `new_information` empty is a
  no-gain turn, the strongest single signal. Verify which
  `llm_call_audit` columns count provider calls vs. tool invocations before
  aggregating (the column semantics split; do not sum blindly).
- The tutorial harness efficiency evidence
  (`tutorial-reliability.staging.spec.ts` + `tutorial-harness.ts`):
  `REDUNDANT_STATE_LIST_TOOLS`, `redundantSelections` (a violation since
  `752e404ea`), `noGainTurns`, `repairTurns`, the
  `candidate`/`discovery,candidate` phase assertion. Note it is a **staging**
  spec — an assertion that exists is not an assertion that runs; say which
  findings only that gate would catch.
- `journalctl` for planner disposition codes (they appear nowhere else).
- Static analysis is acceptable where no runs exist: trace what the model is
  told against what the binder/validator does with its response, as
  `elspeth-63cf3803e6` did.
- Every finding must carry a measurement plan: which shape, which counter,
  what number before/after proves the shortcut landed.

## Trip-wires (verified 2026-08-18; re-verify before relying on them)

- `test_guided_chat_prompts_name_only_tools_in_their_actual_palette` bans
  the literal strings `list_sources`/`list_transforms`/`list_models` from
  every step prompt (and more per step). Say what to do, never which tool
  not to call.
- `test_proposal_audit_projection.py` pins the planner context by full-dict
  equality with pre-equality privacy canaries: any context addition is a
  deliberate test update, and the canaries are your no-egress proof.
- The correction path shares `guided_redacted_planner_context`: phrase any
  context prose as what the server **restores**, never what the planner
  "owns" (during corrections the server owns more).
- Guided skills are `@lru_cache`d; live-testing a brief change requires
  restarting `elspeth-web.service`, verified by `ExecMainStartTimestamp` and
  the journal, not `is-active`.
- Brief changes are behaviour changes: propose, rank, and mark each finding
  as needing operator sign-off; do not implement in this review.

## Deliverable

A findings table, most-calls-saved first. Per finding: ID; surface
(file:line); shape(s) affected; estimated calls wasted per occurrence;
defect class (A–F); evidence (what information the extra call sought, and
where it already existed / should exist / genuinely lives); proposed fix
seam (brief text | context builder | tool payload | rejection message |
palette | choreography); egress delta (zero, or flagged for the operator);
measurement plan; and any invariant tension, stated plainly. Cross-check
open Filigree issues (label `planner-efficiency`, plus `elspeth-826765af90`,
`elspeth-d293c5d139`, `elspeth-7bd0141bbe`, `elspeth-f159d2394b`,
`elspeth-aaa9e3f597`) — link, don't duplicate. Count your findings; never
report a number you didn't count. Close with the calls-per-shape table:
decision minimum vs. current, per canonical shape, with the residual gap
attributed finding-by-finding.

Adjacent but out of scope: the turn-budget/wall-clock mismatch
(`elspeth-f159d2394b`) — reducing turns is this review; funding them is that
ticket.
