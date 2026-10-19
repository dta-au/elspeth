# Planner guidance review for 0.8.9

The revised fixed planner guidance is source- and contract-clear after an independent line-by-line fact-check. Sixteen groups of inaccurate or ambiguous instructions were corrected. Independent systems-thinking and LLM-diagnostics reviewers approved the actual corrections, including the matching just-in-time tool help. This review does not establish model obedience, provider efficacy, full local qualification or acceptance-gate success.

## Scope and method

The baseline contained 1,772 rendered guidance rows. The revised native capture contains 1,779: 1,743 pointer/text pairs were unchanged and 36 revised rows were reread against the implementation across 24 changed blocks. The final ledger classifies 1,082 non-claims and 697 source/contract-verified rows, with no unresolved findings in this scope. All message-content string leaves and tool-declaration strings are covered. Four unchanged message role/cache protocol strings were separately classified as transport values.

The reviewer independently reconstructed both row exports and native canonical identities. Exact ID multiset comparison rejected deliberate omission, invention and duplication; exporter controls detected known prose and excluded numeric leaves. Original and revised IDs remain distinct. The capture covers its declared policy posture. A later review found that the separate content-safety teaching branch still contained the old sink-only restriction and advice to switch execution surfaces. The original capture did not establish that branch was correct; it is corrected below.

## Corrected teaching

| Finding | Correction | Primary source |
|---|---|---|
| GF01: unauthorable coalesce modes | Teach require_all, best_effort with timeout, or first; union/nested. Distinguish engine-only fields. | composer/state.py |
| GF02: mandatory node-level user template | Require an effective user template; allow per-query templates and node fallback. | composer/state.py; transforms/llm/base.py |
| GF03: unconditional multi-query preference | Require compatible node-wide settings and preserve requested separate branches/calls. | transforms/llm/multi_query.py |
| GF04: node-input routing | Distinguish published connections, valid implicit IDs and restricted loss diversion. | composer/_producer_resolver.py; composer/state.py |
| GF05: sink input restricted to success | Include eligible gate and failure-route destinations. | composer/_producer_resolver.py; composer/state.py |
| GF06: unavailable review/discovery tools | Use supplied facts first and call only tools offered on the current surface; preserve pending proposal reviews. | composer/capability_skill.py; composer/tools/_common.py |
| GF07: universal structured-output guarantee | Scope enum to closed string labels; preserve numeric/open-domain types and explain local validation/miss policy. | transforms/llm/validation.py; transforms/reference_join.py |
| GF08: whole-run document publication promise | Disclose first single-value publication and later diversion without retraction; aggregate for a complete report. | sinks/document_sink.py |
| GF09: every row passes value_transform | Successful rows pass through; evaluation/type failures follow on_error. | transforms/value_transform.py |
| GF10: globally exactly-once terminal call | Submit alone per terminal turn, then allow bounded repair/resubmission after rejection. | composer/pipeline_planner.py |
| GF11: raw missing-key exception | Say expression evaluation fails; do not promise an unwrapped KeyError. | core/expression_parser.py |
| GF12: inspection I/O bound | Separate the inspected 8 KiB prefix/100-row bound from full-file integrity hashing. | composer/tools/sources.py |
| GF13: sink-only error policy | Explain eligible enclosing-closer loss diversion, aggregation restriction and unchanged required-control coverage. | composer/state.py; core/dag/builder.py; plugin_policy/coverage.py |
| GF14: every paraphrase rejected | Explain the required raw-HTML/fingerprint markers while retaining canonical-copy advice. | web/interpretation_state.py |
| GF15: universal historical diff baseline | Describe the baseline supplied by the surface, which may be request-start state. | composer/tools/generation.py; composer/pipeline_planner.py |
| GF16: sets limited to membership | Allow sets consumed inside expressions, including len; prohibit stored row sets. | core/expression_parser.py |

Paths in the table are relative to `src/elspeth/`; `composer/` means `web/composer/` and `transforms/` and `sinks/` mean their directories under `plugins/`.

The corrections change guidance, descriptions and corresponding source-file hash metadata. They do not introduce server-authored graphs, tutorial-specific behavior, unavailable tools, weakened validation or a way around required controls. The controlled-error tradeoff retains operator custody of workflow/policy decisions and expressly forbids changing execution surfaces to evade a control.

## Follow-up corrections

The additional content-safety branch now reuses the same controlled-error rules for automatic and manual wiring. Eligible enclosing-closer diversion and required-control coverage remain explicit; changing policy or execution surfaces to evade a control is prohibited.

Blob discovery distinguishes independent calls from dependent pages: use the returned continuation offset and content hash, rather than guessing offsets. Supplied but unread instructions are not missing product facts. Advisor repair advice must use current schema evidence; it cannot establish a valid option value by itself. CSV retention teaching distinguishes strict row admission, audit context, readable reasons and rollback limits. The Advisor can see the two nonsecret retention flags without exposing unrelated option values.

A scaffold remains incomplete when a complete build was requested. Continue when a supported next action is available; report concrete tooling or capability blockers rather than repeating unsupported attempts. Truthful partial handoff is allowed: preserve progress, identify completed and remaining or unverified work, explain the observed time boundary, and request continuation. The operator's acceptance ceiling is five minutes per request; configured work must reserve response and persistence time. This is not a change to deployment defaults or evidence that a particular reserve is sufficient. Completion is not forced into one request.

Advisor inspection now retains decoded prompt text separately from the unchanged provider wire text. Source-inspection metadata reaches review only after its exact admitted response was delivered in a successful provider call. Bounded source inspection does not imply complete file-content coverage or semantic understanding; rereading the same facts does not manufacture repair progress.

## Final request sizing

A finite offline capture of the final request builders measured 116 variants: 24 freeform, 12 planner and 80 Advisor requests, including required-control automatic/manual postures and supported deployment-overlay boundaries. The largest individual default system string was 135,022 Unicode code points. Adding the existing 65,536-byte overlay's maximum code-point count and seven-character separator gives 200,565; a rounded-up 5% reserve adds 10,029, yielding the shipped example allowance of 210,594 characters.

Installed SDK serialization produced request bodies from 137,513 to 673,908 bytes in this matrix. All measured gateway variants fit the proposed character limit and the unchanged 1,048,576-byte body limit. Sixteen string-boundary controls and two independent streaming-body controls passed. These are offline construction and admission checks, not provider acceptance or a guarantee that every independently maximal state, history and schema fits together.

## Historical size and validation boundary

The earlier corrected fixed native scaffold measured **129,849 bytes**, within its **131,593-byte** test ceiling, leaving **1,744 bytes**. The pre-clarification capture was 127,334 bytes; clarification added 2,515 bytes. These are historical measurements of that capture, not final sizes for the follow-up variants. Required guidance is retained before sizing: the final gateway allowance must cover the measured complete individual system string, the supported deployment overlay and separator, plus a rounded-up 5% reserve. The separate request-body byte cap remains independent.

That native capture deliberately lowered the target test assertion for one measurement, which exited 1 under a frozen-source wrapper; that is measurement provenance, not a passing-test result. The exact real assertion was restored before normal validation. Its fixed canonical identity is `f404b60dfe57715af61e349db744912ad0cc769e16bf1caf8235a9f03f2c0f3b`.

The complete row ledger and native evidence remain in the custodian's local working evidence. Source review does not claim a full audit of uncaptured future tool results or variable user/session context. Historical sink-only comments and diagnostics outside the reviewed guidance remain outside this bounded change; no runtime defect is inferred from them. Final local qualification, provider-backed Composer assemblies and repository example execution are separate release requirements.
