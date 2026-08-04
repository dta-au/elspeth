# ADR-037: Interpretation Review Caps Govern LLM Churn Only — Server-Shaped Obligations Are Bounded by Dedup and Graph Size

**Date:** 2026-08-04
**Status:** Accepted
**Deciders:** ELSPETH maintainers
**Tags:** composer, interpretation-review, rate-limits, closed-enum-growth

## Context

`request_interpretation_review` is capped twice per session
(`composer_interpretation_rate_limit_per_term`, default 3;
`composer_interpretation_rate_limit_per_session_day`, default 10). Both caps
were designed around the first member of `InterpretationKind`,
`vague_term`, where every premise of a rate limit holds:

- the `user_term` is the **user's own ambiguous phrase** lifted from their
  prompt, so repeated cards are perceptible nagging;
- the documented fallback is **available to the LLM** — the cap error says
  "use a direct interpretation in the prompt template instead", and the LLM
  can simply decide, write it down, and drop the requirement.

The enum then grew to five members (`vague_term`, `invented_source`,
`llm_prompt_template`, `pipeline_decision`, `llm_model_choice`) and the caps
kept applying to all of them. For the four newer kinds neither premise
holds. Their `user_term` is a **server-derived constant**, not a user
phrase — `required_control_auto_wired` rides on every auto-wired control,
`inline_source_data` on every invented source — and the requirement is
staged on the node by the server, so the LLM cannot bake it away. A cap hit
there writes a terminal `AUTO_INTERPRETED_NO_SURFACES` event while the node
requirement stays **pending**, which wedges the session permanently:
execution validation fails forever and the review API has nothing left to
resolve.

This was not theoretical. Acceptance battery round 2 (2026-08-04, session
`e445e8e2`) produced exactly that state: six auto-wired control disclosures
across six distinct nodes all carrying the constant `required_control_auto_wired`,
spending one shared 3-ticket budget. The 4th control in each composition
state was refused, and the session could never validate again.
`elspeth-558fa5a321` and `elspeth-9c01c943a5` are two failure modes of the
same divergence; this is the sixth confirmed instance of the house pattern
"one predicate reused across opposite-safety contexts".

## Decision

**The interpretation rate caps are an allow-list of one: they apply to
`vague_term` and nothing else.** A cap is coherent exactly where the user
authored the term *and* the LLM has the bake-into-the-prompt fallback.

**The counters measure the same population the check governs**: LLM-authored
`vague_term` rows only. Two exclusions matter:

- rows of any other kind — they are obligations, not churn, and must not
  drain a budget they are not subject to;
- rows carrying the `BACKEND_AUTO_SURFACE_TOOL_CALL_PREFIX`
  (`backend_auto_surface:`) provenance sentinel — the backend surfacer calls
  `create_pending_interpretation_event` directly and so **bypasses the check
  entirely**, but its rows would otherwise still *spend* budget. Measured on
  the battery session, a third of the consumed per-term budget had been spent
  by the server against an allowance documented as throttling the composer
  LLM.

The general rule this encodes: **check who *spends* a budget, not only who is
*checked against* it.** An asymmetry between those two populations is a bug
even when neither half looks wrong alone.

## What now bounds review churn on the uncapped kinds

This is the load-bearing part of the decision, and it is deliberately
recorded here rather than left in a commit message: removing the caps for
four of the five kinds means the caps are **no longer** part of the churn
argument for those kinds. Two mechanisms remain, and they are sufficient
because the uncapped kinds are server-shaped:

1. **The per-site dedup gate.** Staging is idempotent per
   `(kind, user_term, affected_node_id)` site — a second request for a site
   that already has a live pending event does not create a second card. An
   LLM that re-requests the same review in a loop produces no additional
   user-visible work.

2. **The graph itself.** For the server-shaped kinds the set of review sites
   is a function of the authored graph, not of LLM discretion: one
   prompt-template review per LLM node, one disclosure per auto-wired
   control, one per invented source. The LLM cannot mint sites without
   authoring components, and component count is already bounded by the
   composition budgets (`max_composition_turns`, tool-call caps) and by
   validation.

Together these bound the worst case at *one card per real review site in the
graph* — which is exactly the number the user must resolve anyway, because
execution validation demands every one of them. There is no configuration in
which throttling that number is safe: a card withheld is not a card deferred,
it is a session that can never run.

**This argument is a premise, not a guarantee.** It fails if either
mechanism changes. Re-derive it if the dedup key ever loses a component
(especially `affected_node_id`), if a future `InterpretationKind` member is
LLM-minted rather than graph-derived, or if a surfacing path is added that
can stage sites without a corresponding graph component. A new enum member
must be assessed against the two coherence premises above before it is
allowed to inherit — or escape — the cap.

## Consequences

- `WebSettings.composer_interpretation_rate_limit_*` now govern `vague_term`
  exclusively; both field descriptions say so, so operator-visible
  configuration no longer claims to throttle what it does not.
- The `backend_auto_surface:` sentinel is load-bearing beyond provenance —
  it is now a cap discriminator. It is a shared constant
  (`BACKEND_AUTO_SURFACE_TOOL_CALL_PREFIX`) used by every stamping site and
  by the counter; a hardcoded copy of that string would silently
  re-introduce server-spent budget.
- Sessions already wedged by the old behaviour need no data surgery: one
  further compose message re-surfaces the disclosures uncapped.
- A separate arm remains open (`elspeth-03f5728c33`): a compose cancelled
  mid-loop skips finalization, leaving `llm_prompt_template` requirements
  pending with zero events. That is a surfacing-path defect, not a cap
  defect, and no cap change addresses it.

## Related

- ADR-032 — validate by trust domain (the parse/nominally-type split these
  boundary writers sit on)
- `elspeth-558fa5a321`, `elspeth-9c01c943a5`, `elspeth-03f5728c33`
- `docs/acceptance/2026-08-04-battery-round-2-report.md` — the incident that
  produced the evidence
