# Re-anchor, wave 2: CDA-07, CDA-08, CDA-09, CDA-18, CDA-19

Base checked: `integration/composer-remediation` at `8f5ca4292`, 254 commits
after the audit baseline `main` = `1a7ff0e63` (`git rev-list --count main..HEAD`
= 254; the dispatch said 253).

Method. Read-only. Every "verified" item below was read from code on the new
base or measured with `git log main..HEAD -- <path>`, `git diff main HEAD --
<path>` or `git grep <pattern> main`. Items marked **(inferred)** were not
read end to end. No tests were run.

Per-file churn since the baseline (measured):

| File | Commits | Diff |
|---|---|---|
| `composer/turn_effects.py` | 0 | none |
| `composer/advisor_context.py` | 0 | none |
| `composer/advisor_checkpoint.py` | 0 | none |
| `composer/advisor_abandonment.py` | 0 | none |
| `composer/advisor_policy.py` | 0 | none |
| `composer/composer_preflight.py` | 0 | none |
| `composer/no_tool_finalize.py` | 0 | none |
| `composer/withheld_replies.py` | 0 | none |
| `web/credential_guard.py` | 0 | none |
| `contracts/credential_material.py` | 0 | none |
| `web/validation.py` | 0 | none |
| `frontend/.../chat/MessageBubble.tsx` | 0 | none |
| `composer/composition_completion.py` | 1 (`1dfa5afcf`) | +32, one insertion at L925-953 |
| `composer/no_tool_policy.py` | 1 (`1dfa5afcf`) | +29/-9 |
| `composer/provider_gateway.py` | 1 | +15/-4 |
| `composer/llm_response_parsing.py` | 1 | +3 |
| `composer/planning_application.py` | 5 | +62/-16 |
| `composer/pipeline_planner.py` | 18 | +449/-57 |
| `composer/service.py` | 16 | +555/-76 |
| `composer/tool_batch.py` | 10 | +395/-127 |
| `sessions/routes/_helpers.py` | 3 | small |
| `frontend/.../chat/chat.css` | several (ARGOnite) | +69/-15 region |

The dispatch hints are all confirmed.

---

## CDA-07 — STILL-PRESENT (with two brief anchors wrong on any base, and one stale acceptance instrument)

### 1. Finding status

Nothing in 0.8.12/0.8.13 touches the advisor evidence, the END rubric, the
repair instruction or withheld carry-forward.

- No capability record on the END review. `TurnEvidence.__slots__`
  (`turn_effects.py:294-313`) holds no plugin snapshot; `review_candidate`
  (`turn_effects.py:686-688`) takes `candidate, request, current_state, scope`
  only; `binding` (`:851-860`) and `semantic_repair_key` (`:863-888`) hash no
  snapshot. The only snapshot use in the file is the abandonment path
  (`:601-681`).
- Rubric unchanged: `_ADVISOR_END_PUBLICATION_INSTRUCTIONS`
  (`advisor_context.py:90-118`) has no capability, tool-call-JSON, defect-filing
  or read-only-question sentence.
- Repair instruction unchanged in wording, moved +32:
  `composition_completion.py:2586-2594` still reads "Correct the reply's
  unsupported claims using the saved-effect and inspection evidence."
- Withheld carry-forward unchanged: a withheld candidate is written as an
  audit row (`composition_completion.py:2417-2422`) and the assistant row's
  raw content becomes the saved-scope sentence
  (`advisor_checkpoint.py:958`: `raw_content = ... if publication_admitted
  else saved_scope`). `_composer_history_content`
  (`sessions/routes/_helpers.py:1677-1693`) returns `raw_content`, so the next
  turn's history holds only that sentence.

### 2. Anchors

| Brief | New base | Note |
|---|---|---|
| `turn_effects.py` `TurnEvidence` ~L290 | `:290` | file unchanged |
| `turn_effects.py:851, :863` | `:851`, `:863` | unchanged |
| `advisor_abandonment.py:109-129` / `:109-119` | same | unchanged; catalog JSON at `:109-119`, record at `:129` |
| `advisor_context.py:314` (`_advisor_user_sections`) | function starts `:223`; the `publication_review` section is `:313-329` | unchanged; `:314` is inside the function, not its `def` |
| `advisor_context.py:526-625` | `:526-625` | unchanged; index counter at `:530`, publication block `:604-622` |
| `_ADVISOR_END_PUBLICATION_INSTRUCTIONS` | `advisor_context.py:90` | lives in `advisor_context.py`, not `advisor_checkpoint.py` |
| `composition_completion.py:949` | `:981` | +32 |
| `composition_completion.py:2259` | `:2291` | +32 |
| `composition_completion.py:2369` | `:2401` | +32 |
| `planning_application.py:915` | `:953` | +38 |
| `planning_application.py:960` | `:998` | +38 |
| BLOCKING repair instruction | `composition_completion.py:2586-2594` | +32 |
| `pipeline_planner.py` `prior_assistant_messages` ~L575-645 | `:625-743` | **wrong target, see §4** |

`review_candidate` call sites re-derived with
`grep -rn review_candidate src/elspeth`: still exactly five (three in
`composition_completion.py`, two in `planning_application.py`), plus the
definition at `turn_effects.py:686`. Count unchanged.

### 3. Tests

- `tests/unit/web/composer/test_advisor_capability_evidence.py`: absent (new,
  as the brief expects).
- `tests/unit/web/composer/test_turn_effects.py`: exists, 0 commits since main.
- `tests/unit/web/composer/test_advisor_checkpoint.py`: exists, 1 commit (+1
  line).
- `tests/unit/web/composer/test_planner_conversation_history.py`: exists (also
  on main); the brief does not name it but the carry-forward edit will hit it.
- No existing test covers the capability record on END review **(inferred
  from the absence of any snapshot use in `review_candidate`)**.

### 4. Brief corrections

1. **History construction is not in `pipeline_planner.py`.** Lines `:625-743`
   there are the `PlannerConversationContext` data classes. Since
   `11290f37d` they also carry `prior_assistant_messages_omitted` and
   `without()` for shedding. The history is built in:
   - `sessions/routes/_helpers.py:1844` `_composer_chat_history` and `:1646`
     `_composer_history_content` (shared by the tool loop and the planner);
   - `planning_application.py:195-243`
     `_freeform_planner_conversation_context` (planner projection; main
     `:192-240`).
   This was already wrong on main (main `pipeline_planner.py:575-657` is the
   same data class). Re-point the brief at those two files.
2. **Carry-forward contradicts a written module contract.**
   `withheld_replies.py:9-15` states a withheld reply "must never re-enter"
   provider context, because "advisor-cohort prose may quote or rebut
   findings the user never saw". The brief must say which origins may be
   carried forward (probably `advisor_publication_withheld` and
   `planner_prose_unadmitted` only) and must update that docstring, or choose
   the brief's other option. This is a design decision for the maintainer,
   not a lane call.
3. **New in 0.8.13: carried-forward text must be classified for shedding.**
   `service.py:3694-3703` (`_context_budget_frame`) labels a history row
   `USER_REQUEST` if it carries the authorship marker, `ASSISTANT_PROSE` if
   its role is `assistant`, and otherwise protected (`None`, never shed). The
   planner labels from `PlannerConversationContext` entries
   (`pipeline_planner.py:3482-3492`). A labelled draft carried under a
   non-assistant role would be unsheddable and count against the budget every
   turn. Carry it as assistant-role content, or add a kind deliberately.
4. **The snapshot is not available where `TurnEvidence` is built.**
   `TurnEvidence(...)` is constructed at `service.py:1328`, `service.py:2840`
   and `planning_application.py:1087` (main: `:1200`, `:2414`, `:1048`). At
   `service.py:1328` the snapshot does not exist yet; it is built at `:1356`
   (`self._policy_context.build(user_id)`). `_review_planner_decline`
   (`planning_application.py:931-946`) has no `plugin_snapshot` parameter, and
   the `composition_completion.py` sites type it `PluginAvailabilitySnapshot |
   None` (`:965`, `:2257`). The brief should name the three construction
   sites and say what a `None` snapshot renders (the existing pattern is
   `confirmed_abandonment(..., plugin_snapshot)` at `turn_effects.py:672`,
   which returns `False` on `None`).
5. `advisor_checkpoint.py` needs no rubric edit; the rubric constant is in
   `advisor_context.py`. Keep `advisor_checkpoint.py` in the lane only if the
   record is threaded through its END call.
6. "Give the composer the current gate blocker detail in its context (R15)"
   has no anchor in the brief. Not located here.

### 5. File overlap

Brief lists: `turn_effects.py`, `composition_completion.py`,
`planning_application.py`, `advisor_checkpoint.py`, `advisor_context.py`,
`pipeline_planner.py`.

A correct fix must also touch, **not listed in the brief**:

- `src/elspeth/web/sessions/routes/_helpers.py` (carry-forward);
- `src/elspeth/web/composer/withheld_replies.py` (contract docstring, possibly
  an origin filter);
- `src/elspeth/web/composer/service.py` (two `TurnEvidence` construction
  sites; 16 commits of churn, so a likely collision file);
- `src/elspeth/web/composer/advisor_abandonment.py` (extracting the shared
  renderer);
- possibly `src/elspeth/web/composer/prompts.py` if the draft gets its own
  history label **(inferred)**.

`pipeline_planner.py` is probably **not** needed, which removes the brief's
stated overlap with CDA-12 on that file.

### 6. S1 replay artifacts

Present in the composer-desk-audit worktree and tracked:
`evals/composer-live-audit/spikes/S1-advisor-replay/` has `replay.py`,
`build_record.py`, `record_broad.json`, `record_core.json`, `results/` and
`requests/` with 44 files (W1–W4 and K1–K2 in arms A/B/B2/BS; C1–C5 in arms
C/CA/CS plus candidate texts). `git ls-files` counts 52 files under
`S1-advisor-replay`.

Two problems on the new base:

- **Not on the new base.** `evals/composer-live-audit/` does not exist in the
  composer-remediation worktree, and neither does the audit doc folder. The
  audit branch (`ccr-d14093ba-glu6bw`, tip `b1ec0aacd`) must be merged or the
  folder copied before CDA-07 can run its acceptance.
- **The prebuilt requests are stale.** The spikes README says each request
  "embeds the full advisor system text from `1a7ff0e6`". The advisor system
  message is the composer skill plus the trigger instructions
  (`advisor_checkpoint.py:453-454`). The trigger instructions are unchanged,
  but `skills/pipeline_composer.md` changed by 164 lines and
  `pipeline_capabilities.md` by 61 since main. `replay.py build` regenerates
  only from live captures that are not in git. So the "arm built from the new
  code path" needs a rebuild step that swaps in the new skill text, and the
  29/30 and 20/20 thresholds were measured against the old prefix. Whether
  `record_broad.json` still matches the plugin set on the new base was not
  checked.

---

## CDA-08 — STILL-PRESENT

### 1. Finding status

- The withheld replacement is still ordinary assistant text.
  `composition_completion.py:2423`:
  `assistant_message = _AdmittedAssistantMessage(content=turn_evidence.render_saved_scope())`,
  then `advisor_checkpoint.py:958` makes that the row's `raw_content`, so
  `visible_message_segments` (`no_tool_policy.py:789-818`) returns it as an
  `AssistantTextSegment`. `MessageBubble.tsx:133` labels it "ELSPETH said:".
- The purpose-written wording is still unused in chat: "The completion review
  did not admit the reply's claims; the reply was withheld."
  appears only at `advisor_policy.py:453` (`grep` over `src/elspeth/web`, one
  hit).
- Duplicate receipt still reachable. The withheld row's raw content is the
  saved-scope sentence, and `_append_checkpoint_receipt`
  (`composition_completion.py:2657-2669`) then appends
  `Checkpoint save status: <same scope>` via
  `compose_checkpoint_receipt_message` (`no_tool_policy.py:766-786`). The
  planner path does the same at `planning_application.py:1031` and `:1040`
  (the advisor-unavailable shape in the acceptance line).
- `1dfa5afcf` (0.8.13) added one more trusted notice
  (`_REVIEW_REPLY_CONTEXT_BUDGET_NOTICE`, `no_tool_policy.py:117-121`) and
  generalised the absent-reply suffix parser (`:680-698`). It does not change
  the withheld or receipt behaviour.

### 2. Anchors

| Brief | New base | Note |
|---|---|---|
| `turn_effects.py` `render_saved_scope` ~L952 | `:952` | unchanged |
| `no_tool_policy.py` ~L170-215 | `:178-225` | +8; withheld disclosure `:180`, pending notices `:192-198`, unverified notice `:220-225` |
| `compose_checkpoint_receipt_message` ~L746 | `:766` | +20 |
| `advisor_policy.py:451-455` | `:451-455` | unchanged |
| `MessageBubble.tsx` | unchanged; author label `:133`, segment render `:243-258` | |

`render_saved_scope` callers re-derived: `composition_completion.py:2423`,
`:2556`; `planning_application.py:1031`, `:1040`; `no_tool_policy.py:776`.

### 3. Tests

- `tests/unit/web/composer/test_checkpoint_receipts.py`: exists, 0 commits.
  It has no duplicate-receipt assertion for the withheld shape **(inferred
  from the test names; bodies not read)**.
- `tests/unit/web/composer/test_no_tool_policy_segments.py`: exists, 1 commit
  (+40 lines, the context-budget notice).
- `MessageBubble.test.tsx`: exists, 1-line change (adds
  `context_budget_exceeded` to an `it.each`). A sibling
  `MessageBubble.refusals.test.tsx` exists (also on main).

### 4. Brief corrections

1. Shift the two `no_tool_policy.py` anchors as above.
2. Name `advisor_checkpoint.py:958` (`_advisor_blocked_result`, `:859`) as
   the place the withheld row's raw content is chosen. Making the withheld
   replacement a system notice means changing that line, or what its callers
   pass as `saved_scope`. The brief lists neither the file nor the line.
3. Name the planner call sites `planning_application.py:1031, :1040`; the
   acceptance case (advisor-unavailable withhold) runs through them
   **(inferred from the `reason` ladder at `:1010-1022`)**.
4. A new notice must be registered in the closed suffix grammar
   (`_canonical_base_suffix_segments`, `no_tool_policy.py:649`), which
   `1dfa5afcf` restructured; write the new branch against the new loop at
   `:680-698`, not the main shape.
5. The system-note styling is `.trusted-system-notice` in
   `frontend/src/components/chat/chat.css:129-146`, which ARGOnite rewrote.
   A collapsed secondary receipt line needs CSS there.
6. Seven Python test files pin the receipt label text
   (`grep -l "Checkpoint save status\|Checkpoint evidence"`):
   `test_advisor_tool.py`, `test_checkpoint_receipts.py`,
   `test_compose_loop_interpretation_review_dispatch.py`,
   `test_compose_loop_llm_audit.py`, `test_compose_loop_audit_wiring.py`,
   `test_service.py`, `test_upload_recovery_authority.py`. Keep the receipt
   wording unchanged, or expect edits in all seven.

### 5. File overlap

Brief lists: `turn_effects.py`, `no_tool_policy.py`, `MessageBubble.tsx`.

Also needed, **not listed**: `advisor_checkpoint.py` (CDA-07 and CDA-18 both
treat it as CDA-07's), `composition_completion.py` (`:2423`, `:2556`;
shared with CDA-07 and CDA-09), `planning_application.py` (`:1031`, `:1040`;
shared with CDA-07), `frontend/src/components/chat/chat.css`.

Coupling with CDA-07: if CDA-08 empties the withheld row's `raw_content`,
`_composer_history_content` returns an empty string and
`_freeform_planner_conversation_context` drops the turn
(`planning_application.py:227`, `if content.strip()`). That changes what
CDA-07's carry-forward has to carry. The CDA-07 → CDA-08 order is right, and
both lanes should agree on the row shape before either starts.

---

## CDA-09 — STILL-PRESENT

### 1. Finding status

`composer_preflight.py` has no commits since main.
`reuse_or_recompute_runtime_preflight` (`:329-391`) still ends:

```python
if last_runtime_preflight is not None and not _state_contains_blob_ref(state):
    return last_runtime_preflight
if _state_contains_blob_ref(state):
    return await self.cached_runtime_preflight(...)
if state.sources and state.outputs and not state.validate().is_valid:
    return await self.cached_runtime_preflight(...)
return None
```

An unchanged, blob-free, authoring-valid wired state with no prior preflight
therefore returns `None`. The gate then cannot qualify:
`composition_completion.py:2613`
`graph_preflight_qualified = runtime_preflight is not None and runtime_preflight.is_valid`,
and `AdvisorGatePassed` is granted only when that is true (`:2644-2652`).

### 2. Anchors

| Brief | New base | Note |
|---|---|---|
| `composer_preflight.py` ~L329-380 | `:329-391` | unchanged |
| `service.py` ~L1883 (review-handoff exit) | `:2252-2274`; the guard is `:2262-2264` (`runtime_result = last_runtime_preflight`; `if state.version > initial_version:`) | moved +369 |
| `composition_completion.py` ~L2582-2620 | `:2613-2655` | +32 |
| `graph_rejected` semantics | `graph_already_blocked`, `composition_completion.py:2292-2296`; consumed at `:2620`, `:2649` | +32 |

Callers of the reuse rule, re-derived (same four as main):
`composition_completion.py:1415`, `:1779`, `service.py:2664` (via
`turn_runtime_preflight`), and `no_tool_finalize.py:221` (direct).

### 3. Tests

- `tests/unit/web/composer/test_preflight_recompute_wired_state.py`: absent
  (new).
- `tests/unit/web/composer/test_runtime_preflight_pending_review_verification.py`:
  exists, 0 commits.
- No existing test covers the new assertion **(inferred: the function is
  unchanged and the new file does not exist)**.

### 4. Brief corrections

1. Update the `service.py` and `composition_completion.py` anchors as above.
2. Name `no_tool_finalize.py:221` as a fourth consumer of the changed rule.
   It is the question-only finalize path, which is exactly where the brief's
   reported extra cost lands.
3. The ABSENT-preflight copy assumes `None` on an unchanged turn:
   `no_tool_policy.py:200-225` ("the FOURTH preflight shape ...
   `runtime_preflight is None` ... a question-only turn mutates nothing") and
   the `_ADVISOR_SIGNOFF_UNVERIFIED_*` notices. After this lane, that shape
   is reachable only for states without sources and outputs. Tests pinning
   the unverified notices on wired states will go red **(inferred; the tests
   were not enumerated)**.
4. Nothing in 0.8.12/0.8.13 added a preflight mechanism to reuse. The
   context budget does not bound preflight.

### 5. File overlap

Brief lists: `composer_preflight.py`, `service.py`,
`composition_completion.py`. Possibly also `no_tool_policy.py` comments and
notices (shared with CDA-08) and `no_tool_finalize.py` if its call needs a
changed argument **(inferred)**. `service.py` now has 16 commits of churn;
the edit at `:2262` sits in `_classify_and_budget_turn` (`:2075`), away from
the context-budget code (`:1553-1815`, `:3675-3715`).

---

## CDA-18 — STILL-PRESENT

### 1. Finding status

`credential_guard.py` has no commits since main.
`require_no_credential_material_in_llm_metadata` (`:91-128`) still passes
`reasoning_content` and `reasoning_details` into one
`require_no_credential_material({...}, surface=surface)` call (`:115-128`)
with content, ids and provider fields, and raises on any hit. There is no
scrub outcome and no opaque-field exemption.

The 0.8.12 entry "Thinking from an adapter that does not declare `REASONING`
is refused" is commit `2dce56395`. It changes only
`gateway/src/elspeth_llm_gateway/core/service.py` and
`gateway/tests/test_service.py` (`git show --stat`). It is a capability check
in the standalone LLM gateway, not the composer credential guard, and fixes
none of F4. It does not conflict with the brief.

One 0.8.12 change raises the exposure: "Composer and advisor must be
reasoning models" (every composer-plane request now carries a
reasoning-effort hint; the `none` opt-out is removed). More calls now return
reasoning, so reasoning-only hits should be more frequent than the audit
measured **(inferred from the changelog; no rate measured)**.

### 2. Anchors

| Brief | New base | Note |
|---|---|---|
| `credential_guard.py:91` | `:91` | unchanged |
| `provider_gateway.py:272` | `:272` | inside `_require_no_credential_material_in_completion_fields` (`:264`) |
| `tool_batch.py:1015` | `:1122` | +107 |
| `llm_response_parsing.py:754` | `:756` | +2; `build_llm_call_record` at `:707` |
| `advisor_checkpoint.py:579`, `:646` | same | unchanged |

Direct callers of `require_no_credential_material_in_llm_metadata`,
re-derived with `grep -rn` over `src/` and `elspeth-lints/src`: still three
(`provider_gateway.py:272`, `tool_batch.py:1122`,
`llm_response_parsing.py:756`). Count unchanged.

Sites reaching `provider_gateway.py:272`:

| Brief | New base | What it is |
|---|---|---|
| L83 | `:83` | in `_MalformedLLMResponseError.__init__` |
| L157 | `:157` | metadata-only check |
| L188 | `:188` | content check |
| (not listed) | `:256` | body of the wrapper `_require_no_credential_material_in_completion` (`:250`) |
| L654 | `:654` | call to that wrapper |
| L703, L724, L734 | `:703`, `:724`, `:734` | unchanged |
| L817 | `:822` | call to the wrapper, +5 |
| L1013 | `:1019` | +6 |
| `advisor_checkpoint.py:579` | `:579` | unchanged |

Same sites as main; only the last two moved.

Callers of `build_llm_call_record`, which all reach
`llm_response_parsing.py:756`: `provider_gateway.py:907`, `:1093`;
`advisor_checkpoint.py:646`; `pipeline_planner.py:4056`, `:4215`, `:4247`,
`:4322`, `:4343`; `sessions/_auto_title.py:514`;
`_aws_ecs_acceptance/bedrock.py:217`. The brief names only the advisor one.

### 3. Tests

- `tests/unit/web/composer/test_reasoning_credential_scrub.py`: absent (new).
- `tests/unit/web/test_credential_guard.py`: exists, 0 commits. The pinned
  tests are `test_provider_reasoning_is_refused_before_release` (`:521`) and
  `test_text_provider_reasoning_is_refused_before_release` (`:545`). The
  brief's ~L520-560 is right.
- `tests/unit/web/composer/test_pipeline_planner.py` ~L5385 → the test is
  `test_planner_provider_metadata_refusal_is_atomic_before_audit_history_or_persistence`
  at `:5604`, parametrize at `:5602`, reasoning assignment at `:5620`. Its
  `combined_reasoning` case sets `response.model = credential` **and**
  `reasoning_content`, so it still refuses on `model_returned` after the fix.
  It may need no assertion change; read it before editing.
- `tests/unit/web/composer/test_compose_loop_persistence.py` ~L796-806 → the
  parametrize list is `:880-896`, for
  `test_internal_completion_control_metadata_refuses_atomically_before_any_effect`
  (`:897`). The `reasoning_content` and `reasoning_details` cases (`:886-887`)
  change; `thinking_blocks` stays.

### 4. Brief corrections

1. Update the anchors above (`tool_batch.py:1122`,
   `llm_response_parsing.py:756`, `provider_gateway.py:822` and `:1019`,
   plus the wrapper at `:250-256`).
2. `provider_gateway.py:83` runs the guard inside an exception constructor
   and returns nothing. A scrub outcome there must replace
   `provider_metadata` before `super().__init__`; the brief should say so,
   because "every caller applies the outcome" is not mechanical at that site.
3. `build_llm_call_record` gained a `context_budget` parameter (`:729`) and
   passes it to the record (`:824`). The scrubbed record must keep it.
4. Native thinking is scanned separately and that scan includes the opaque
   fields: `provider_thinking.py:54-66` (`native_thinking_blocks`) runs
   `require_no_credential_material(block, ...)` over every field of each
   block, including `signature` and `redacted_thinking.data`. The goal says
   "signature fields" are never pattern-scanned and also that a
   `thinking_blocks` hit "still fails exactly as today". Those two sentences
   conflict for a signature that happens to contain an `sk-` run. The brief
   needs one ruling. `provider_thinking.py` is not in its file list.
5. State whether the scrub applies to the auto-title and Bedrock-acceptance
   callers of `build_llm_call_record`, which the brief does not mention.
6. The dependency reason "after CDA-06 (`test_credential_guard.py`)" still
   holds; the file is unchanged.

### 5. File overlap

Brief lists: `credential_guard.py`, `provider_gateway.py`, `tool_batch.py`,
`llm_response_parsing.py`.

Possibly also, **not listed**: `provider_thinking.py` (item 4);
`_compose_loop_carriers.py` if the scrub outcome becomes a field on
`_AdmittedLLMProviderMetadata` (`:103-109`) **(inferred)**; and wherever the
scrub event is recorded, which the brief leaves open.

`tool_batch.py` has 10 commits (+395/-127) and `provider_gateway.py` and
`llm_response_parsing.py` both carry the new `context_budget` plumbing, so
recompute collisions with any lane touching the tool loop (CDA-22) or the
call record.

---

## CDA-19 — STILL-PRESENT

### 1. Finding status

`contracts/credential_material.py` has no commits since main. The
`credential_assignment` pattern (`:101-111`) is unchanged:

```python
r"password|passwd|pwd|credentials?)[\"']?\s*[:=]\s*['\"]?"
r"(?!\$\{[A-Za-z_][A-Za-z0-9_]*\}(?:['\"]?(?:\s|[,;)}\]]|$)))"
r"[^'\"\s,;(){}\[\]]+",
```

After `Credentials:` any run of non-delimiter characters matches, so
"Credentials: managed by the operator" and "**Credentials:** managed ..."
still refuse (the second matches on `**` then on `managed`) **(read from the
pattern; not executed)**.

### 2. Anchors

| Brief | New base | Note |
|---|---|---|
| `credential_material.py` ~L85 onwards | `CREDENTIAL_PATTERN_SPECS` at `:85`; `credential_assignment` at `:101-111` | unchanged |
| `detector_version` | `CREDENTIAL_DETECTOR_VERSION = "1"` at `:22`; field default `:141` | unchanged |

### 3. Tests

- `tests/unit/web/test_credential_label_prose.py`: absent (new).
- `tests/unit/contracts/test_credential_material.py`: exists, 0 commits.
  Positive example for the pattern is `"client_secret=hunter2"` (`:59`);
  related tests at `:191` and `:200`.
- `tests/unit/web/test_credential_guard.py`: exists, 0 commits.
- `tests/unit/web/test_credential_boundary_inventory.py`: exists, 1 commit
  since main (content not read).

### 4. Brief corrections

1. Anchor is fine. Add the consumers the narrowing also changes:
   - `src/elspeth/web/validation.py:180`: `_redact_sensitive_content` runs
     `pattern.sub` over the same specs. A narrower pattern redacts less on
     egress, including the advisor evidence the END review reads
     (`advisor_context.py:30, :230, :237`). The brief treats this as a
     refusal change only.
   - `credential_material.py:300` and `:351`: the two in-module searches.
2. `CREDENTIAL_DETECTOR_VERSION` is stored on diagnostic evidence
   (`composer/diagnostic_evidence.py:181`, read back at `:273`). The brief's
   "bump if the stored contract requires it" should name that reader as the
   thing to check.
3. Nothing in 0.8.12/0.8.13 touches the detector or adds a mechanism to
   reuse.

### 5. File overlap

Brief lists: `credential_material.py`. No other source file is required. The
behaviour change reaches `validation.py` and `advisor_context.py` without
editing them, so CDA-07's replay acceptance sees slightly different redacted
evidence if CDA-19 lands first **(inferred)**. Test-file overlap with CDA-06
and CDA-18 on `tests/unit/web/test_credential_guard.py` stands.

---

## Cross-lane points

- **Collision recompute.** Under the corrected file lists:
  - `advisor_checkpoint.py`: CDA-07 and CDA-08 (and CDA-18 is told not to
    touch it);
  - `composition_completion.py`: CDA-07, CDA-08, CDA-09;
  - `planning_application.py`: CDA-07, CDA-08;
  - `sessions/routes/_helpers.py`: CDA-07 (new);
  - `service.py`: CDA-07 (new) and CDA-09;
  - `no_tool_policy.py`: CDA-08, possibly CDA-09;
  - `pipeline_planner.py`: probably no longer CDA-07.
  The CDA-07 → CDA-08 → CDA-09 serial order still covers all of these.
- **Nothing in 0.8.12/0.8.13 already fixes any of the five lanes.**
- **New mechanisms a brief should respect, not rebuild:**
  - the context budget's history labelling (`service.py:3675-3715`,
    `pipeline_planner.py:3470-3500`) for CDA-07's carry-forward;
  - the closed suffix grammar as restructured by `1dfa5afcf`
    (`no_tool_policy.py:680-698`) for CDA-08's new notice;
  - the `composer_reply_stage_refusal` audit row
    (`composition_completion.py:925-953`, `reply_stage_refusal.py`) is a
    second "reply absent" shape next to the withheld reply. CDA-08's
    "system note, not ELSPETH said" rule should state whether it covers that
    notice too.
- **Not checked:** whether `record_broad.json` matches the new base's plugin
  set; the bodies of `test_checkpoint_receipts.py`; which tests pin the
  unverified-preflight notices; `test_credential_boundary_inventory.py`'s
  one commit.
