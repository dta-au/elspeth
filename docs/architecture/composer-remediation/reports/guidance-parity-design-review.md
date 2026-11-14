# Bounded planner guidance design review

Verdict: **usable**, with the focused clarifications below required in the implementation brief/evidence. No architectural or unresolved-product-decision blocker was found for the stated planner-only slice. This is a source/design review, not implementation approval or a completion claim.

Reviewed `guidance-parity-design.md`, `guidance-parity-implementation-brief.md`, and `guidance-tracker-excerpt.md`. Production evidence was read with `git show` at `14fef5e5342ccbd79b4fdd69b276c20f77d66dc1` in `/workspace/elspeth/.claude/worktrees/composer-remediation-wave1`. R8 and Q19/Q20 were read from the main checkout's `docs/architecture/composer-tool-assessment/remediation-plan.md`: that audit path is absent from the pinned production commit, so it is a separate working-document authority, not immutable source evidence. No tests, scans, provider calls, source edits, index/HEAD changes, or Page changes were performed.

## Why the design is usable

The proposed service wiring addresses the real defect. `service.py:733–752` checks the two current source hashes and freezes `render_system_prompt(data_dir)`; `service.py:789–801` passes that same freeform text into `PlanningApplication`; `planning_application.py:1093` forwards it as `rendered_skill`. `pipeline_planner.py:3392` hashes that exact string and `:3494` places it in the provider system message. A distinct planner rendering with its own source drift check and exact-byte hash is the right correction. Retaining the original freeform/advisor rendering avoids changing another route to fix this one.

Actual palette authority is correctly located at the provider boundary. `planner_tool_definitions` (`pipeline_planner.py:1652–1674`) takes an ordered policy subset, uses real wire declarations, and appends recovery and terminal controls. Testing captured messages and tools together, across both dialects, repair and hatch, is stronger than an isolated renderer assertion. The design correctly avoids adding missing mutation tools.

The shared core/overlay split can preserve fidelity without a second canonical schema. The current freeform policy contains material request and provenance obligations (for example `pipeline_composer.md:111–126`, supplied-but-unread normative references and model-authored judgements), so the explicit preservation audit is necessary. Existing source-custody aids already distinguish exact existing bindings, missing data, generated data and planner refusal (`planner_authoring_aids.py:348–405`). Keeping those semantics while removing the unscoped `create_blob` fallback is appropriately bounded. Singular inline source custody must remain distinct from reference-table and plural custody limitations.

Owner-side wording changes are also appropriate. `_allowlisted_candidate_feedback` attaches catalogue explanations and fixes directly (`pipeline_planner.py:2444–2446`); fixing only a standalone explanation renderer would miss the actual repair path. The design retains withholding, attribution and sequence exclusion, and deliberately avoids D3's repair producer. Locally scoped freeform hints and configuration-level repairs need neither regex rewriting of arbitrary text nor a new guidance framework.

## Required clarifications before accepting implementation evidence

1. **Classify the narrowed-palette exercise as a synthetic control, not an existing production request mode.** `PlannerDiscoveryPolicy.initial` (`pipeline_planner.py:355–385`) always selects `PLANNER_DISCOVERY_TOOL_NAMES`; `with_manifest` (`:387–397`) preserves that tuple, explicitly retaining reread tools. A valid narrower subset is representable and the emitter supports it, but the reviewed production initializer does not choose one. Specify a controlled test injection at that boundary, capture the resulting actual outbound request, and label it as mutation robustness. Do not change production policy selection to make this test possible. Keep ordinary service-route coverage separate.

2. **Make the oracle's bounded exception convention concrete.** Tokenization recognizes names; it does not infer English scope. Define exact reviewed paragraph/source-span exceptions for schema references, explicit other-route instructions and conditional discovery instructions. Require their expected text/anchor to match, fail if stale or ambiguous, and reject a second unscoped occurrence outside that span. The existing removal/displacement mutation requirement should also cover a conditional guard. Use the complete registered declaration/control vocabulary to find unavailable names, then compare against the captured request palette; deriving the vocabulary only from that active palette would make `create_blob` invisible. This should remain a small test-only checker, not a prose interpreter or production renderer framework.

3. **Turn preservation categories into an auditable owner map before replacing the overlay.** For each listed obligation, record the old source span, retained/new source owner and the meaningful preservation or behavioral assertion. Include conversational request history versus assistant claims, supplied-but-unread evidence, exact literals/A-B behavior, generated-source first-person provenance, required controls/retention/minimization and interpretation-review staging. Keyword presence alone cannot establish preservation. Scripted candidate tests establish reachability/custody behavior, not that the new prompt causes a live model to obey. The design already states that limitation; retain it in the report.

These are bounded implementation/evidence requirements, not reasons to redesign the approach or request a product decision. A patch that omits them should not be accepted as satisfying the intended parity/preservation claim.

## D7/D8 and closure boundaries

D7 is a dependency of correcting the retained false-reread/cap claims, not of selecting the planner overlay. The source explicitly keeps rereads available while the current core says they are refused. Preserve and report that known inconsistency for this slice; do not treat it as a newly verified behavioral claim. R8/Q20 distinguish correction of the false claim from the eventual freeform policy choice. The proposed slice may proceed without resolving that choice, provided 13.08/.09 remain open.

D8 is a dependency of changing diff semantics or its baseline, not of explaining that a saved-state preview/diff does not validate an unsaved candidate. Both discovery tools remain in the actual planner palette. Removing the misleading completion prerequisite is truthful containment and does not settle D8 or make preview/diff a candidate validator.

The proposed restriction to 13.01/.02/.04/.05 and planner portions of .03/.15, plus intentional pins, is sound. Whole-R8, MCP/freeform parity and unresolved-policy closure would overstate this work. Preserve completed 13.16 evidence without renewing its implementation claim.

## Optional improvement

Keep the service construction test and the exhaustive producer/projection checks as separate fixtures sharing one parity assertion. This makes a route-wiring regression diagnosable without multiplying expensive service setup across every catalogue entry. No test-runtime claim is made here.

## Scoped fix1 re-review

Reviewed `guidance-parity-design-fix1.diff` and
`guidance-parity-design-fix1-notes.md`, limited to the three requested
clarifications and new breakage introduced by this documentation diff.
The original review above is retained as the historical basis; its three
clarification requests have the following disposition.

1. **Synthetic narrowed-palette classification — ADDRESSED.** The inventory,
   acceptance section and implementation task now distinguish ordinary
   full-policy primary/repair/hatch requests from a test-only wrapper around
   the real `PlannerDiscoveryPolicy.initial`. They require capture of actual
   outbound messages/tools, preserve ordering and all other policy fields,
   prohibit changing production selection, and explicitly limit the evidence
   claim to synthetic robustness.
2. **Bounded oracle exceptions — ADDRESSED.** The complete registered tool
   vocabulary is now independent of the captured available palette. The finite
   fixture specifies origin, scope kind, exact paragraph and surrounding anchor,
   and the permitted names within one selected span. Missing, ambiguous,
   duplicated, overlapping and drifted spans fail closed. Guard deletion and
   displacement, a second unscoped occurrence, and stale/duplicate anchors are
   explicit controls. The design prohibits production parsing/metadata or a
   generic rendering framework. These are implementable test conventions,
   subject to review of the final actual fixture spans.
3. **Auditable preservation map — ADDRESSED.** The new owner map records old
   source spans, named retained/new owners, complete-rule assertions and
   applicable context/candidate/custody controls for the requested obligations.
   It expressly covers user versus assistant authority, supplied-but-unread
   references, literal/adaptation and A/B fidelity, generated-source provenance,
   review staging, controls, retention and minimization. Final source spans
   and exact test IDs remain required implementation deliverables. Scripted
   fixtures are explicitly reachability/preservation evidence, not proof of
   live-model instruction compliance or new runtime enforcement.

**Updated verdict: usable; all three design clarifications addressed.** No
new blocking design breakage was identified in this scoped documentation diff.
The added map and finite exception fixture remain implementation acceptance
obligations, not evidence that the behavior has already been preserved or the
oracle has been built. No production policy, D7/D8 classification or closure
boundary changes are authorized by this re-review. No tests, scans, provider
calls, production/test edits, index/HEAD changes or Page updates were performed;
only this lane review document was updated. No implementation approval or
completion claim is made.
