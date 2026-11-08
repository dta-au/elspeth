# Prepared pipeline candidate contract — source-only design

Inspected root HEAD ad773c656ff6009bf13e7916e1f249076e8da9b2. No root/source edits, imports, tests, profiling or deployment. This finding remains open; no bounded whole-candidate reuse patch is justified yet.

## Actual duplicate and integrity boundaries

- pipeline_commit.py:375–418 checks exact proposal type/session/pending status, catalog/snapshot identity, base state ID/content, and authoritative pipeline argument hash. Owned-state authority is restored and lowered to execution arguments separately from original audited arguments.
- :436–466 builds ToolContext with current validation, operation authority/context, source authority, paths, user/model/skill provenance and internal interpretation flag.
- :468–479 builds candidate, requires acceptable/no prepared inline blob and checks its hash against owned proposed content when available.
- :481–492 recovery path rebuilds candidate and compares durable prior executor hash; it does not dispatch again.
- :494–572 fresh path begins audit using ORIGINAL pipeline arguments, invokes execute_tool with EXECUTION arguments, requires exactly one captured invocation with exact tool_call_id/authority_arguments_hash, hashes final executor state, binds that hash into dispatch evidence and compares independently computed candidate hash. Both hashes reach settlement (routes/composer/pipeline_settlement.py:609–610).
- tools/sessions.py:2153 builds candidate again inside _execute_set_pipeline. tools/_dispatch.py:982–995 then applies interpretation gate and finalize_tool_result; _common.py:3838–3872 explicitly performs current policy normalization after handler output. Those outward checks are not duplicate builder work to delete.
- Candidate builder is about1500 lines and combines typed argument parsing(:876), component canonicalization, blob/source authority and policy-dependent checks, state construction and final validation. SetPipelineCandidate(:591) contains ToolResult plus optional unsettled inline blob, not a complete binding/lifetime proof. A frozen dataclass alone does not demonstrate recursive immutable ownership.

Returning the first candidate.result from the executor would compare two hashes derived from the same construction, losing the present independent construction check. Even a rehash detects subsequent mutation but cannot detect an originally misconstructed graph. Do not call it equivalent proof.

## Smallest defensible first step: prepared input, not cached candidate

A narrow optional internal prepared-input object may reuse only pure schema parsing/canonical immutable argument material while BOTH candidate and executor constructions still run. This preserves independent structural comparison and fresh outward policy checks. It does not resolve the whole duplicate-candidate finding or promise material speedup; first attribute parsing versus graph/policy cost.

Candidate contract to design/review:
- Exact nominal internal type, issuer restricted to proposal preparation; never accepted through model/tool JSON, SDK arguments, public schemas, or generic discovery paths.
- Deep-frozen original authority arguments and derived execution arguments are distinct. Bind both authority_arguments_hash and execution_arguments_hash; no coercion between bool/int/string, no lossy serialization, no shared mutable SDK dictionaries. Fresh detached mutable args for execute_tool if required.
- Bind proposal/session/tool_call IDs; exact absent/present base state ID, version/content hash; exact live operation context/authority and operation kind/epoch; catalog identity+snapshot/binding fingerprint; immutable reviewed-source authority identity; path-policy flags, data_dir and internal interpretation mode; user/provenance and source/blob context influencing construction.
- Lifetime one preparation invocation, synchronous immutable parse artifact only, consumed within original awaited dispatch; discard on timeout/cancellation, rollback, authority retry, changed head/policy/source/operation, or recovered dispatch. No process/global/session cache and no authority/validation success bit.
- Revalidate hash/bindings at actual executor entry. If a field differs, refuse/rebuild through existing normal path with fresh admission; never silently accept prepared output. Both builders still create independent owned state/results and compare final hash.
- Source owner must prove parsing truly pure: any default/provider/catalog/blob/secret lookup remains outside prepared parse and executes fresh. Keep error classification/redaction/argument validation identical.

This requires extracting a typed parser seam from the large builder; a generic Mapping cache or optional ToolResult kwarg is not sufficient. Current SetPipelineCandidate cannot serve as this object.

## Whole prepared candidate: concrete blockers

1. **Independent derivation:** there is no independent compact verifier proving original exact arguments lower to every candidate node/edge/source/review requirement/metadata. Hashing supplied candidate proves identity, not correct derivation. A verifier covering those rules likely duplicates much of builder logic; owned-state proposals have a separate proposed-content hash, legacy argument proposals do not. Start by proving scope differences, not assuming all proposals owned-state.
2. **Fresh external facts:** builder examines reviewed source/blob authority and current policy; outward normalization stays fresh but is not proven equivalent to every source/shape/path check skipped by reuse. Need dependency inventory and recapture rules. No stored availability verdict.
3. **Context parity:** preparation explicitly sets require_data_dir_for_paths=True; execute_tool call currently omits that keyword (dispatcher defaultFalse). This is not proven an exploit because candidate already enforces stricter policy, but it disproves an unexamined identical-context assumption. Binding audit must cover all actual args/defaults and retain stricter checks.
4. **Mutable output ownership:** ToolResult/state nested data must not be shared with provider capture, SDK dictionaries, or a dispatch mutator. A frozen wrapper cannot supply that proof. Every physical dispatch and receipt must still occur independently.
5. **Boundary timing:** preparation crosses awaited worker/dispatch boundaries. Policy identity and live operation/head checks at preparation are not a substitute for settlement's fresh checks. Neither dispatch receipts nor candidate hash authorize a stale commit.
6. **Recovery:** recovered dispatch must rebuild/validate against durable executor hash without creating another physical attempt. A transient prepared object must never become recovery authority.

Thus no implementation drafted. Removing independent construction is a contract change requiring a specific verified equivalence boundary, not a local memo optimization.

## Required controls before qualification

- Old/new diagnostic and final-state parity for owned-state and legacy argument proposals, absent/present base, single/multi-source, gates/forks/aggregation/LLM review terms, explicit/default ADI paths. Reject inline-blob prepared settlement as today.
- Spy/instrument proves pure parse once but independent candidate/executor constructions remain; intentional executor mutation still produces PipelineCommitMismatchError and exact audit binding. Corrupt first candidate with valid args must not become accepted by circular comparison.
- Tamper original args versus execution args independently; bool/int/string differences, nested options, tool ID, proposal ID, session, operation object/epoch/kind, base ID/content/version, policy snapshot/default rotation, reviewed source authority and path flags. Each mismatch refuses or follows audited fresh path.
- Revoked credentials, deleted/changed blob, newer head or expired/replaced operation between preparation and dispatch/settlement still refuse at existing boundaries. Keep current tests test_pipeline_commit_operation_authority.py:202–239 for exact live context and existing schema-error auditing.
- Exactly one independent dispatch invocation per actual attempt, correct original authority_arguments_hash and final executor hash; failures retain invocation/binding; recovery does not redispatch. Required physical capture/availability receipts remain.
- Deep mutation isolation across args, prepared artifact, candidate result, executor result and SDK/recorded capture. No reuse after cancellation/retry/rollback or across proposals/sessions.
- Final outward interpretation gate and policy normalization still execute even if parse reused; independent final state/base checks unchanged.
- Factorized large args/graphs measurement on identical source except this change, counting builds/parses/current validations and SQL plus CPU/bytes; existing whole-feature23%CPU result predates this design and cannot justify it.

Recommended next step after gates: source inventory/extraction design for immutable parsed arguments and one controlled component attribution. If parsing is minor, do not add contract complexity for negligible gain; retain independent builds until a stronger equivalence proof is reviewed.
