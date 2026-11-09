# Planner guidance and palette parity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement task-by-task under the parent's authorized execution method. Do not spawn agents, commit, run broad suites or update the Page from this brief. Parent dispatches Sol 6.1 high implementation and independent Astra review.

**Goal:** Teach the existing new-build planner only supported authoring actions and prove palette parity across its shipped public instruction carriers.

**Architecture:** Add an explicit planner interaction overlay over the exact shared capability core. Keep freeform rendering intact, scope shared instruction sources at their owners, and prove the text against provider-captured requests and production handlers without expanding any tool palette or authoring capability.

**Tech Stack:** Existing Python Composer/planner, Markdown skills, policy catalog, pytest scripted-provider harnesses and existing producer-driven pins. No dependency, migration, transport or platform changes.

**Spec:** `guidance-parity-design.md` in this lane. Exact preparation source: `14fef5e5342ccbd79b4fdd69b276c20f77d66dc1`. Planning only; no new test results or implementation claims.

## Global constraints

- Parent supplies final immutable integration base and releases the frozen source before edits. Work only in the assigned worktree; lane-private logs stay under this lane.
- Read root AGENTS/CONTRIBUTING and both guidance documents. If editing `plugins/transforms/reference_join.py`, also read `plugins/transforms/AGENTS.md`.
- D1 direct known-refusal/no-op and retained review/custody behavior stay unchanged. D2/root admission and portable MCP remain separate.
- No D3–D38 choice, handler policy change, new tool, server-authored candidate/repair, tutorial-specific path, database write format, or plugin assistance interface change.
- Preserve completed 13.16/Q25 exact-token tests and six enduring controls. Do not duplicate them or claim they were implemented here.
- This is 13.01/.02/.04/.05 plus planner portions of .03/.15 and affected .17 pins. The parent must not close .03/.15 across all routes from this slice alone.
- Shared core remains exact prefix once, field inventory remains tied to canonical schema, schemas and provider dialect semantics do not change, and the exact rendered planner text keeps its own hash.
- Every changed instruction needs both a text/parity oracle and a meaningful behavior or preservation control. No expected hash, signature or frozen fixture hand edits.

## Review focus

1. Actual service routing still passes freeform skill despite a green isolated planner renderer test.
2. Hints re-enter through a different list/schema/assistance or operator-profile projection carrier.
3. A broad scoping exception hides an unadvertised command, or a default palette hides a narrowed request-policy failure.
4. Removing freeform text drops literal fidelity, review provenance, required controls, retention or minimization teaching from the planner.
5. Prompt text/hash/archive/manifest divergence, including deployment overlay and escape-hatch calls.

Each item has explicit tests below. A successful string assertion alone does not establish the route or behavior.

---

### Task 1: Isolate the planner interaction instructions without losing policy

**Files:**

- Create `src/elspeth/web/composer/skills/pipeline_planner.md`.
- Modify `src/elspeth/web/composer/prompts.py`, `service.py`, `planning_application.py`, and narrowly `skills/pipeline_capabilities.md` where shared operational text needs scope.
- Modify `skills/pipeline_composer.md` only for source-owned shared-policy relocation or explicit scope needed by the design; do not perform unrelated wording fixes.
- Tests: `tests/unit/web/composer/test_prompts.py`, `test_capability_skill_identity.py`, `test_skills_loader.py`, `test_service.py`; add `test_guidance_palette_parity.py` for the cross-carrier oracle.

**Interfaces:**

- Consume existing `render_system_prompt(data_dir)`, `render_with_pipeline_capabilities(interaction_skill)`, `load_skill_with_hash`, `assert_skill_hash_unchanged_on_disk`, `PlanningApplication(... composer_skill_text=...)`, and `plan_pipeline(... rendered_skill=...)`.
- Add `render_planner_system_prompt(data_dir: str | None = None) -> str` and source name/hash constants for `pipeline_planner`, following the current loader/drift pattern. Keep freeform helper defaults and `SYSTEM_PROMPT` unchanged in meaning.
- Freeze a distinct planner skill text on the service and pass it into the existing planning application dependency. Internal field/argument naming may be clarified, but the independent planner hash must match actual planner bytes and must not replace the advisor/freeform hash.

- [ ] Rebind exact source and inspect all callsites from the design inventory. Read the existing tests' bodies and actual fixtures; choose the real service empty-draft planner-dispatch harness, not a standalone `_plan` call that supplies a minimal custom skill.
- [ ] Add a failing service-to-scripted-provider test that captures the system content and tools from a real empty-draft compose request. Assert planner terminal/repair instructions, no active freeform mutation inventory/build batches, same exact capability core once, and correct actual text hash. Run both provider dialects with outbound provider guards.
- [ ] Add preservation assertions for literal prompt/A-B fidelity, named gaps, source/generated provenance, subjective review staging/parts, policy controls, failure retention, minimization and descriptions. Assert the planner does not require a freeform advisor/review call or saved-state preview/diff to submit an unsaved candidate. Pin freeform instructions and its advisor skill/hash as unchanged in role.
- [ ] Add controlled deployment-overlay and source-drift tests: the existing deployment layer is still present and its bytes affect the planner rendering/hash; changes to any loaded static skill source cause the existing fail-closed restart/drift behavior. No new runtime rejection of arbitrary operator-authored text.
- [ ] Run the new tests serially and record red evidence on the original route. Fail for the intended wrong rendering/claim, not a bad fixture or unscripted provider.
- [ ] Implement the new planner overlay/rendering and service wiring. Scope shared core operations locally, distinguishing a canonical schema-name reference from an actual call. Use only advertised tools for discovery; do not hardcode a second inventory.
- [ ] Run Task 1 tests green, including freeform preservation. Keep source frozen for each run and record actual return codes.

### Task 2: Correct planner aids, public hints and repair advice at their owners

**Files:**

- Modify `src/elspeth/web/composer/planner_authoring_aids.py` (custody rules/placeholder and any unscoped operational instruction).
- Modify `src/elspeth/web/composer/tools/generation.py` only for catalogue/declaration guidance text; retain matching/response contracts and behavior.
- Modify only public command-bearing plugin hints confirmed by the real producer inventory. Seed locations: `plugins/sources/{azure_blob_source,dataverse,null_source,blob_rows,csv_source}.py` and `plugins/transforms/reference_join.py`; projected replacements, if any, are owned in `web/plugin_policy/profiles.py`.
- Tests: `test_planner_authoring_aids.py`, `test_tools_discovery.py`, `test_validation_guidance_catalogue.py`, `test_generation_response_contracts.py`, policy-view/profile tests and `test_guidance_palette_parity.py`.

**Interfaces:**

- Keep `build_planner_authoring_aids(catalog)` and existing assistance/catalog schemas unchanged. No route-aware mutable cache or new plugin assistance metadata.
- Keep `explain_validation_code`, `explain_withheld_validation_code` and `_handle_explain_validation_error` resolution/precedence/Expected-hint behavior unchanged; alter source-authored instructions, not error semantics.
- Neutral catalogue correction: describe the configuration repair in an authored document. Where a command is retained for another surface, label its exact bounded paragraph as other-route reference. Keep freeform/MCP valid instructions available without teaching unsupported operations to the planner.

- [ ] Enumerate the live installed registry and the request-projected policy views through actual catalog/discovery producers. Record public guidance fields delivered by lists, schemas and assistance, including summaries, fixes and example prose. Do not claim completeness from regex hits over plugin source. Include an unprofiled and operator-profiled view and a policy-hidden control.
- [ ] Add red route-parity tests for the documented source-generation hints and reference-table wiring hints, using actual tool results rather than hand-built copies. Exercise all three carriers wherever the producer supplies that guidance. Profile replacement results must participate independently.
- [ ] Add a catalogue test through real planner candidate feedback for a known correction currently prescribing `upsert_node`/`upsert_edge`, plus the real explanation discovery handler for the same code. Pin semantic correction, code/attribution, withheld behavior and the absence of unscoped unavailable commands. Retain existing exact-code/regex precedence and Expected-hint controls.
- [ ] Pin that planner feedback does not carry complete `tool_sequence` graph-repair payloads. Do not touch D3's sequence producer or widen the allowlisted feedback contract.
- [ ] Add positive controls for supported `source.inline_blob`: real terminal candidate reaches custody/validation; explicitly requested generated data retains invented-source provenance; uploaded data and supplied literal content remain distinct; missing/unread content is never turned into invented identifiers/data.
- [ ] Add a real scripted rejection/repair/resubmission test using the existing planner and actual complete candidate finalizer. It must not need a `create_blob` call; retain canonical schema oneOf/source constraints, returned refusal and provider audit behavior. This does not claim to measure live-model compliance.
- [ ] Implement source-owned text fixes: remove unscoped `create_blob` fallback from planner custody rules; use terminal inline source; split shared hints into general facts and explicitly scoped freeform commands; make catalogue repairs configuration-level where feasible. Do not claim inline source solves arbitrary reference-table or plural custody authoring.
- [ ] Run the affected handlers, planner aids and scripted loop tests green. Preserve successful freeform blob creation/binding as a negative control against overbroad removal of valid behavior/instructions. Preserve ordinary empty-guidance and policy-denial cases.

### Task 3: Make the parity oracle fail on real instruction regressions; update pins

**Files:**

- `tests/unit/web/composer/test_guidance_palette_parity.py` (new cross-carrier tests/helper), `test_pipeline_planner.py`, `test_capability_skill_identity.py`, applicable teaching and boot tests.
- Existing generated skill inventory, teaching fence, declaration/wire or response fixtures only if their real producers show intentional changes.
- `scripts/cicd/generate_skill_inventory.py` only if the new overlay needs generator support. Do not change the current freeform inventory to a planner list.

**Interfaces:**

- The oracle consumes actual captured `messages`/`tools`, `planner_tool_definitions(policy, dialect=...)`, the canonical declaration emitter and real public guidance objects.
- It tokenizes identifiers exactly and reports `(origin, tool_name, active_palette)`. Narrow schema-name/other-route scopes are tied to exact source spans; no blanket tool-name allowlist and no substring matching.
- A dependency on ordinary freeform or MCP tools is legal only in locally explicit other-route reference text, never as a current planner command. A discovery instruction conditional on presence must be bound to the actual request palette, not a default route list.

- [ ] Parameterize both dialects, an initially narrowed discovery policy, ordinary candidate repair and escape hatch. Capture the exact provider call inputs, rather than asserting against a helper's pre-projection output. Reuse existing guards and trust-channel assertions.
- [ ] Check every shipped planner instruction carrier: system/core/overlay, request instruction, aids/reference context, discovery declaration and argument descriptions, actual list/schema/assistance hints and attached catalogue repair text. Exclude arbitrary user/file contents and historical echoed user values by producer field ownership, not by a greedy regex.
- [ ] Add lasting in-memory mutations independently injecting `call create_blob` into each carrier: system, aid, raw hint, profile-replaced hint and a catalogue fix delivered through candidate feedback. Call the real parity assertion and require the specific offending origin/name. Keep plain text/backticks/JSON-string cases so formatting cannot hide a token.
- [ ] Add bounded-scope controls: explicit other-route reference is accepted; removal or displacement of its scope fails. A canonical `set_pipeline` document-shape reference passes; a new planner command `call set_pipeline` fails. Narrowed palette removes a real discovery tool and exposes unguarded references; known available terminal controls pass. Empty guidance fails positive terminal/inline/policy assertions.
- [ ] Rebuild pins using existing producers. Inspect exact diffs: only intentional guidance and derived hashes/bytes change, no schema property/required-set or unrelated fixture churn. Preserve completed Q25 and all six mutation cases.
- [ ] Run focused tests and relevant whole-tree gates with parent's test-capacity permission. Existing candidates: `test_tool_declarations.py`, `test_generation_response_contracts.py`, `test_capability_skill_identity.py`, `test_planner_authoring_aids.py`, `test_planner_teaching_gate.py`, `test_tool_knob_teaching_gate.py`, `test_l4_teaching_text_drift.py`, `test_boot_probe.py`, `test_sessions_composer_attribute_contracts.py`, mock-discipline/masquerade gates for added tests, contracts census, Ruff and mypy for touched production paths. Confirm actual paths/options on the final base.
- [ ] Parent determines broad-suite scope; do not overlap its backend/PG gate. This planned slice does not modify SQL/schema/locks/session persistence implementation and does not justify an independent PG run. Any scope expansion must be reported, not hidden behind a local green result.
- [ ] Freeze the final patch for independent Astra review. Supply source/base, complete diff including new files, positive/red/green/mutation evidence, projection inventory, hash/teaching diffs and explicit limitations. Parent handles integration and any tracker updates.

## Command and evidence discipline

Use the final worktree path explicitly. Before executing tests, verify both
`elspeth.__file__` and `elspeth_lints.__file__` resolve into that worktree with
both roots on `PYTHONPATH`. Use fail-closed scripted-provider guards for loop
tests; guard controls must fail before accepting model behavior evidence.
Write each test log/exit file to a unique lane-private path, pass `-n 0` for
focused tests, wait for completion and inspect the actual exit code. Do not
derive success from grep/tail or regenerate skills while another gate runs.

The implementation report must distinguish shipped public parity from arbitrary
operator overlay content, source-established inventory from measured runtime
inventory, scripted reachability from live-model compliance, and planner-only
acceptance from whole-R8/MCP/freeform completion. No closure claim is authorized
by this preparation.
