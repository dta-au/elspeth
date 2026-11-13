# CDA re-anchor, wave 4: CDA-14, 15, 16, 17, 20, 21, 22, 23

- Audit baseline: `main` = `1a7ff0e63`.
- New base: `integration/composer-remediation` = `8f5ca4292` (254 commits ahead of `main`).
- Method: read-only. No suite was run. "Verified" means I read the code or ran a
  `git diff` / `grep` / hash on the new base. "Inferred" is marked as such.
- Shorthand: `svc` = `src/elspeth/web/sessions/service.py`, `csvc` =
  `src/elspeth/web/composer/service.py`, `planner` =
  `src/elspeth/web/composer/pipeline_planner.py`, `turn` =
  `src/elspeth/web/sessions/composer_turn.py`, `helpers` =
  `src/elspeth/web/sessions/routes/_helpers.py`, `FE` =
  `src/elspeth/web/frontend/src`.

## Files byte-identical to `main` (verified with `git diff --quiet main HEAD`)

Every anchor the briefs give in these files is still exact:

`svc` (sessions/service.py), `composer/advisor_checkpoint.py`,
`composer/provider_errors.py`, `composer/turn_audit.py`,
`composer/capability_skill.py`, `composer/pipeline_proposal.py`,
`composer/proposals.py`, `coordination/composer_operation_authority.py`,
`sessions/composer_async_worker.py`, `sessions/pending_interpretation.py`,
`evals/lib/composer_rgr_score.py`, `evals/composer-battery/**`,
`FE/components/chat/{toolCallDescriptions.ts,ToolCallCard.tsx,ProposalDiff.tsx,DecisionPanel.tsx,MessageBubble.tsx,ChatPanel.tsx}`,
`tests/unit/web/sessions/{test_persist_compose_turn.py,test_run_composer_turn.py}`,
`tests/unit/web/sessions/routes/test_tool_call_outcomes.py`,
`tests/testcontainer/web/test_session_operation_fence_postgres.py`,
`tests/unit/web/composer/{test_discovery_terminal_preview.py,test_checkpoint_receipts.py}`.

Changed since `main`: `csvc` (+479 lines, 16 commits, all context budget),
`planner` (+392 lines), `composer/protocol.py`, `composer/provider_gateway.py`
(only a `context_budget` parameter threaded through), `composer/tool_batch.py`,
`turn` (+2 lines above L1499), `sessions/protocol.py`, `helpers`,
`coordination/repository.py` (blob mutations only), `sessions/proposal_authority.py`
(+1 line), `FE/types/index.ts` (+4 lines at ~L380), `FE/stores/sessionStore.ts`.

## Cross-lane facts from 0.8.12 / 0.8.13

1. **Context budget (0.8.13) sits in front of every compose-loop and planner
   provider call.** Tool loop: `_prepare_primary_delivery` (`csvc:1553`) returns
   the shed messages plus a `ComposerContextBudgetRecord`, and
   `_call_llm_before_deadline` (`csvc:3722`) now requires
   `context_budget=` as a keyword. Planner: `_fit_planner_context_budget`
   (`planner:3423`) runs inside `call_model_body` (`planner:3981`). Any new
   provider call added by CDA-17 or CDA-22 must go through these; a call that
   skips them is unbudgeted and has no `context_budget` on its record.
2. **Advisor calls are not budgeted** (CHANGELOG 0.8.13 Known limits,
   dta-au/elspeth#364). `advisor_checkpoint.py` is unchanged.
3. **A no-op admission mechanism already exists (0.8.12, `00ac53e07`,
   `1d6b7b350`).** `composer/tool_batch.py` compares
   `composition_content_hash(candidate) == composition_content_hash(state)` and
   returns the result to the model as `_UnappliedAdmission("no_change")`
   (`tool_batch.py:222-254`, decided at `:1910-1928` and `:2303-2316`), without
   advancing the in-memory state (`:3545-3548`). Scope, verified: every
   mutation tool under `explicit_approve` (block `:1936-2492`), and
   `set_pipeline` only under auto-commit (`:1856`). Settlement of a proposal
   that changes nothing is refused as `proposal_no_change`
   (`sessions/routes/composer/proposals.py:612-641`,
   `pipeline_settlement.py:496, :528-540`; `composer/pipeline_commit.py:555`
   raises `code="NO_CHANGE"`). See CDA-23.
4. **A newer closed audit-envelope precedent exists (0.8.13, `1dfa5afcf`):**
   `composer/reply_stage_refusal.py` (`COMPOSER_REPLY_STAGE_REFUSAL_KIND`, a
   `TypedDict` envelope with `_kind` and `schema`, stored on a `role="audit"`
   row). CDA-23's `composition_state_unchanged` row should follow this shape.
5. **The fixed proposal sentences changed (0.8.12, `d718110b4`):**
   `composer/protocol.py:210-244`. See CDA-14.
6. **Audit artefacts are not on the new base.** `docs/development/composer-desk-audit-2026-11-12/`
   and `evals/composer-live-audit/` exist only on the audit branch
   (`ccr-d14093ba-glu6bw`, worktree `composer-desk-audit`). CDA-23 acceptance 2
   names `evals/composer-live-audit/spikes/P10-snapshot-skip/p10_measure.py`;
   it is absent on `integration/composer-remediation`.
7. **More function-level source pins than the briefs list** (verified by
   reading the helper files; which of these a given edit trips is inferred
   until `tests/unit/architecture/test_web_reserve_reviewed_contracts.py` and
   `test_web_landscape_mutation_fencing.py` are run):
   - `tests/helpers/web_reserve_route_helper.py:90-93` holds sha256 of
     `_handle_convergence_error`, `_handle_plugin_crash`,
     `_handle_runtime_preflight_failure` and `_run_composer_turn`;
   - `tests/helpers/web_reserve_compose_proposal_transfers.py:387-399` holds a
     structural-AST hash of `_run_composer_turn` (TURN) and of
     `ComposerServiceImpl.compose` (SERVICE);
   - `tests/helpers/web_reserve_worker_caller.py:24-29` and
     `web_reserve_worker_six.py:96-101` hold sha256 of
     `ComposerAsyncWorker._job`, `._run_started`, `._run_started_under_lease`,
     `_settle_failure` and others in `composer_async_worker.py`.

---

## CDA-14: STILL-PRESENT (brief's "frontend-only" premise does not hold)

**Status.** No frontend component shows a proposal's `metadata.description`.
The chat component files are byte-identical to `main`; the ARGOnite skin
changed `chat.css` and `styles/tokens.ts`, not the components.

**Deciding code (verified).**
- The wire type has no description: `FE/types/index.ts:329-345`
  (`CompositionProposal`: `summary`, `rationale`, `affects`,
  `arguments_redacted_json`, `pipeline_metadata`), and
  `PipelineProposalMetadata` (`:312-319`) carries hashes only.
- The decoder is an exact-record check:
  `FE/api/composerOperationDecoder.ts:157` lists the 15 allowed keys.
- `summary` is server-authored counts, not model text:
  `composer/proposals.py:46-58` ("Replace the pipeline with N inputs, …").
- The metadata values are redacted before they reach the frontend:
  `composer/redaction.py:2149`
  (`metadata: Annotated[_PipelineMetadataModel, Sensitive(summarizer=_summarize_set_metadata_patch)]`)
  and `FE/components/chat/ProposalDiff.tsx:18-23, :223-229` ("Values do not
  survive").

So the description is not available to the proposal card today. This was
already true on `main`; the brief missed it.

**Anchors.** The brief names none. Current locations: proposal card =
`FE/components/chat/ToolCallCard.tsx` (the only non-test importer of
`ProposalDiff`); fixed sentences = `composer/protocol.py:210-244`, applied at
`composer/planning_application.py:908` and `turn:1157`.

**Tests.** `ToolCallCard.test.tsx`, `ProposalDiff.test.tsx`,
`MessageBubble.test.tsx` exist. `tests/e2e/composer-ab-live.staging.spec.ts:164`
matches `/prepared and validated the requested pipeline/i`, which still
matches the new sentence.

**Brief corrections.**
1. "The backend constants are unchanged" is false on the new base. All four
   sentences were rewritten in `d718110b4`; for example
   `PIPELINE_STAGED_REVIEW_MESSAGE` now ends "The proposal has not been
   applied. Review and accept it to apply the pipeline, or reject it if it is
   not what you want." Re-read F13 against the new copy before deciding the
   lane is still wanted: the sentences still say nothing about what was built.
2. Replace "frontend-only" with a decision the maintainer must make: either
   (a) add a wire field for the description (touches the proposal record,
   `sessions/schemas.py`, `composerOperationDecoder.ts:157`,
   `FE/types/index.ts:329`, and deliberately un-redacts a model-authored value
   on the approval surface), or (b) show the description only after accept,
   from `CompositionState.metadata` (frontend-only, but not "on the first
   turn" under explicit approval).
3. Under auto-commit the first-turn response already carries the committed
   state, so (b) is frontend-only there (inferred from
   `MessageWithStateResponse.state`, `FE/types/index.ts:347-353`).

**File overlap.** Option (a): `FE/types/index.ts` (**collides with CDA-23b**),
`FE/api/composerOperationDecoder.ts`, `sessions/schemas.py`,
`composer/redaction.py` or the proposal summary builder, plus
`ToolCallCard.tsx` (**collides with CDA-23b**). Option (b): `ToolCallCard.tsx`
or `MessageBubble.tsx` (CDA-08 names `MessageBubble`) and `chat.css`.

---

## CDA-15: STILL-PRESENT

**Status.** `evals/composer-battery/**` is byte-identical to `main`. None of
the six demo-shaped cases exists (20 scenario directories, verified by `ls`).

**Anchors.** `evals/composer-battery/corpus.md`,
`evals/composer-battery/scenarios/<case>/scenario.json`,
`tests/unit/evals/composer_battery/test_corpus.py`: all present.
`tests/unit/evals` changed by 2 one-line edits elsewhere
(`test_battery_planner.py`, `test_convergence_scenarios_mocked_llm.py`).

**Brief corrections (pre-existing gaps, verified in `test_corpus.py`).**
1. The roster is closed. `test_corpus.py:21-42` defines `EXPECTED_CASES` (20
   names) and `:73` asserts `set(cases) <= EXPECTED_CASES`. Adding a case
   fails that test until `EXPECTED_CASES` is extended, so the lane edits
   `test_corpus.py`, not just the corpus.
2. Every scenario must carry a single `set_pipeline` canonical payload with
   an expected topology: `test_scenario_is_sound` (`:79-101`) and
   `test_canonical_payload_commits_to_the_expected_topology` (`:127`), which
   also forbids `inline_blob` in the payload. "Capability questions", "what
   changed since I accepted" (multi-turn) and "use GPT-4o" have no canonical
   build payload. The brief must say whether the scenario schema
   (`evals/lib/battery_scenario.py`, `battery_corpus.py`) is extended or
   those cases go to a different corpus. As written the lane cannot pass its
   own test.
3. `corpus_version` is 0 (`scenarios/canary/scenario.json`), so the frozen
   "exactly 20" rule (`:74-75`) is not yet active.
4. Expected answers depend on lanes and releases: "what changed since I
   accepted" depends on the `diff_pipeline` baseline, which 0.8.12 changed
   and marks undecided ("Asked one turn after an edit it reports no change;
   the intended baseline is still to be decided") and which CDA-02 owns;
   "two-file combine" on CDA-13; "use GPT-4o" on CDA-11.

**File overlap.** `evals/composer-battery/corpus.md`, new `scenarios/*`,
`tests/unit/evals/composer_battery/test_corpus.py` (not named as an edited
file), and, if the schema is extended, `evals/lib/battery_scenario.py` /
`battery_corpus.py`. No collision with another wave-4 lane.

---

## CDA-16: STILL-PRESENT (both parts; baseline context changed)

**Advisor part.** `advisor_checkpoint.py` is unchanged, so `:453-495` is
exact. Verified layout (`:453-454`, `:478-495`): the system message is the
composer skill text plus the trigger instructions; on Anthropic routes only
the skill text block carries `cache_control`; the per-turn evidence is the
following user message. Per-turn evidence does **not** precede the stable
text. Inferred: the 52% hit rate is the ratio of the skill prefix to a large
uncached evidence message, not an ordering fault.

**Planner part.** Still present. `apply_anthropic_cache_markers`
(`composer/llm_response_parsing.py:894`) marks the first system message, a
user message starting with `CATALOG_CONTEXT_PREFIX`, and the last tool. The
planner's `authoring_aids` message (`planner:3877-3881`) is a user message
holding canonical JSON, so it gets no marker.

**Anchors.**

| Brief | New base |
|---|---|
| `advisor_checkpoint.py:453-495` | unchanged |
| planner `authoring_aids` message (no line given) | `planner:3877-3881` |
| planner marker call (`main:3600`) | `planner:3992-3996`, and inside the budget fit at `planner:3526` |

**0.8.13 interactions.**
1. The planner's context budget protects "the skill, the reference block,
   the tools" (`planner:3438-3442` docstring), so the `authoring_aids`
   message is never shed and a marker on it stays byte-stable. Verified from
   the docstring; the segment labelling code was not traced line by line.
2. The budget measures the request **with markers applied**
   (`mark_for_cache`, `planner:3990`, `:3526`). A new marker changes the
   measured wire bytes, so add it in `apply_anthropic_cache_markers` (one
   place, used by both the measure and the send), not at the call site.
   Anthropic allows four breakpoints; the planner uses two today (system,
   last tool), so one more fits.
3. Composer skill text changed in 0.8.12 and 0.8.13 (`composer_skill_hash`
   changes). That is a one-time cache rewrite, not a structural change.
4. Inferred: the tool loop's "92–94%" baseline can now drop on a turn that
   sheds, because stubbing earlier tool results changes the prefix behind the
   sliding history-tail breakpoint. The high/low-mark design ("what was shed
   stays shed") limits this to the shedding call. Re-measure the comparison
   baseline on the new base instead of reusing the audit's numbers.
5. Advisor calls are unbudgeted (#364); a change to the advisor message
   layout should not assume the context budget bounds it.

**Tests.** None named. `tests/unit/web/composer/test_pipeline_planner.py`
would pin a marker change (it is shared with CDA-17 and others).

**File overlap.** Investigation only. If the marker fix is taken:
`composer/llm_response_parsing.py` (**not in the brief**; CDA-18 names it)
and `test_pipeline_planner.py` or the cache-marker unit tests. Not `planner`
itself if done as in item 2.

---

## CDA-17: STILL-PRESENT

**Status (verified).**
- No reserve: `planner:3774-3776`
  `deadline = _planner_deadline_time() + model_config.timeout_seconds` then
  `min(deadline, request_deadline)`. Exhaustion raises `code="TIMEOUT"` at
  `planner:3781, :4147, :4245, :4270`.
- `tool_surface="full"` is still the only value passed (`planner:4027`).
- Output-cap in the hatch is unhandled: `finish_reason == "length"` raises
  `RESPONSE_TRUNCATED` (`planner:4439-4457`); on a hatch turn that becomes
  the original exhaustion (`planner:4722-4727`:
  `trail.finish_attempt("hatch", "truncated", led_to="terminal")` then
  `raise hatch_error`).

**Anchors.**

| Brief | New base |
|---|---|
| `pipeline_planner.py:3634` (`tool_surface="full"`) | `planner:4027` |
| `capability_skill.py:193-219` | unchanged |
| `capability_skill.py:39-40` | unchanged (`PLANNER_TERMINAL_TOOL_NAME` / `PLANNER_RECOVERY_TOOL_NAMES`) |
| deadline set (no line given) | `planner:3774-3776`; request deadline passed from `composer/planning_application.py:1076-1085, :1159` |
| `finish_reason=length` rule (no line given) | detect `planner:4439-4457`; non-hatch repair `:4728-4739`; hatch terminal `:4722-4727` |

**Tests.** `tests/unit/web/composer/test_pipeline_planner.py` exists
(+239 lines since `main`). Related existing tests: `:5935`, `:5992`, `:6057`
(deadline), `:7861`, `:8877`, `:8922` (truncation).

**Brief corrections.**
1. The hand-off call must be built through `call_model_body`
   (`planner:3948`), which already takes `tools_override` (`:3951`). The
   manifest call at `:4020-4028` hardcodes `tool_surface="full"`; a
   terminal-only palette fails that validation until the surface is passed
   through. State this as the edit.
2. The hand-off call is now context-budgeted
   (`_fit_planner_context_budget`, `:3981-3991`). It can be refused before
   any provider call with `CONTEXT_BUDGET_EXHAUSTED`, which surfaces as
   itself even on a hatch turn (`planner:4716-4721`). Add: "if the hand-off
   request cannot be fitted, end with the context-budget refusal, not the
   deadline error".
3. The hand-off call consumes one of `budget_policy.max_total_provider_calls`
   (`planner:4143-4144`) and the cost cap (`:4435-4438`). The reserve needs a
   call as well as time. Add: "if no provider call remains, end with the
   existing error".
4. Planner entry now requires `context_budget_history` (`planner:3769`, no
   default); the test helper `_budget(...)` takes context-policy arguments
   (`test_pipeline_planner.py:766-783`). New tests must supply both.
5. `composer_planner_max_request_bytes` default is now 960 KiB and the byte
   budget is checked on wire bytes (`planner:4032-4035`). CDA-13's "re-baseline
   the byte budget" dependency should be re-read against that.
6. The request-byte comment in the test file (`:163`, "2 MiB") is stale on
   the new base; not this lane's defect, but do not copy it.

**File overlap.** `planner`, `test_pipeline_planner.py`. Possibly
`composer/capability_skill.py` (no change needed: `terminal_only` already
validates) and `helpers` if a new planner code needs an HTTP mapping
(`_FREEFORM_PLANNER_FAILURE_HTTP`, `helpers:3071`; **collides with CDA-23b
and, if used, CDA-21**). Shares `planner` with CDA-20 (serial, as planned)
and with CDA-16's marker fix only if that is done at the call site.

---

## CDA-20: STILL-PRESENT (brief's design contradicts the code on both bases)

**Status (verified).** The defect is one line of classification, unchanged
since `main`: `composer/provider_errors.py:121-158`. `RateLimitError` is not a
litellm `APIError`, so it falls to the last line,
`ProviderFailure(ComposerLLMCallStatus.API_ERROR, "unavailable", False)`.
There is no `Retry-After` extraction anywhere in the file.

**The retry loops already exist; the brief asks for a third.**
- Tool loop: `csvc:3773-3864` (`_call_llm_before_deadline`). Retries when
  `failure.retryable`, up to `LLM_API_MAX_ATTEMPTS = 3`
  (`composer/provider_config.py:6-7`), delay `1.0 * 2**(attempt-1)`, and
  `if remaining_after_error <= delay_seconds: raise` (`csvc:3861-3863`), which
  is the brief's "too little deadline left, no extra attempt" already.
- Planner: `planner:4142-4299`. `for attempt in range(1, model_config.max_api_attempts + 1)`,
  each failure recorded with its own ordinal (`:4277-4284`), then
  `if provider_failure.retryable and attempt < model_config.max_api_attempts:`
  sleeps `min(retry_delay, remaining)` (`:4291-4295`).
- Advisor: `advisor_checkpoint.py:1348` (`attempts = 2`), honours
  `failure.retryable` at `:1455-1459`.

Both loops were on `main` (`main` `csvc:3376-3385`, `planner:3887-3889`). All
three will start retrying 429s as soon as the classifier returns
`retryable=True`.

**Anchors.**

| Brief | New base |
|---|---|
| `provider_gateway.py` `_litellm_acompletion` ~L451-569 | `:451` (unchanged above L753) |
| `pipeline_planner.py` ~L3189 | `planner:3326` |
| `pipeline_planner.py` ~L3789 | `planner:4192` |
| `advisor_checkpoint.py:1348` | unchanged |
| `provider_errors.classify_provider_failure` | `:121` (unchanged) |

**Tests.** `tests/unit/web/composer/test_provider_errors.py` exists.
`test_provider_rate_limit_retry.py` is absent (new, as the brief says).

**Brief corrections.**
1. Replace "a shared retry helper around `_litellm_acompletion`" with: make
   429 retryable in `classify_provider_failure`, add `Retry-After` to
   `ProviderFailure`, and add jitter and `Retry-After` to the two existing
   loops. A helper under `_litellm_acompletion` would nest a second retry
   inside the existing ones (up to 3 x 3 attempts) and would retry below the
   layer that writes the call record and settles provider custody
   (`csvc:3836-3842`, `ChargeableAdmissionRefused` between attempts).
2. `Files` must add `csvc` (`_call_llm_before_deadline`) and `planner`
   (`:4291-4295`) as edited, not "if the planner calls directly".
3. "Each attempt … with an attempt index": `ComposerLLMCall`
   (`src/elspeth/contracts/composer_llm_audit.py:285`) has no attempt field
   (grep for `attempt` in that file finds nothing; the same grep finds the
   class line, so the instrument works). The planner records an ordinal per
   call; the tool loop records each attempt as a separate call with no
   index. Either drop "attempt index" from the goal or add
   `contracts/composer_llm_audit.py` to the files and treat it as a stored
   record shape change (0.8.13's `context_budget` field is the precedent for
   adding a nullable field).
4. The two loops differ when the deadline is short: the tool loop raises
   without sleeping; the planner sleeps the remainder and then raises
   `TIMEOUT` on the next iteration (`:4145-4147`). The acceptance bullet
   "fails with today's message and no extra attempt" needs one stated rule
   for both.
5. Planner retries consume `max_total_provider_calls` (`planner:4143`) and
   the cost cap. Say so in the brief's cost note.
6. 0.8.13 says a provider context-length rejection has "no automatic retry".
   That is a 400 (`bad_request`, non-retryable) and is unaffected; keep it so.

**File overlap.** `composer/provider_errors.py`, `csvc` (**collides with
CDA-22 and CDA-12**; not in the brief), `planner` (after CDA-17, as
planned), optionally `advisor_checkpoint.py` (CDA-07 owns it) and
`contracts/composer_llm_audit.py`. `provider_gateway.py` is not needed under
correction 1 (CDA-18 names it).

---

## CDA-21: STILL-PRESENT (anchors exact; pin list incomplete)

**Status (verified).** Both source files are byte-identical to `main`.
`composer_operation_authority.py:401`
`deadline_at=now + timedelta(seconds=deadline_seconds)` inside `admit`
(`:318-426`).

**Anchors.** All exact: authority `~L380-405` (insert), `claim_next` `:497`;
worker `:448` (`_queued_outcome`), `:760` (`_run_started_under_lease`),
`:947` (`_watch`), `:1402-1432` (`_failure_for`, `reap_once`).

Deadline sites the brief does not list (same files, verified):
- `claim_next` filters `jobs.c.deadline_at > now` at `:506` and `:537`, and
  `list_expired_queued` (`:672-686`) expires on `deadline_at <= now`. These
  are the queue-wait checks today; with a claim-time deadline they need the
  new queue-wait bound instead.
- `start_composer_operation_on_connection` `:911`,
  `require_composer_operation_mutation_on_connection` `:981-986`,
  `settle_composer_operation_on_connection` `:1164`.
- `timeout_seconds` is reported as `deadline_at - created_at` at authority
  `:296`, `:986` and worker `:451, :1402, :1411, :1418, :1432`. Once the
  deadline starts at claim this over-reports by the queue wait.

**Pins (verified by hashing the live files).**
- `tests/unit/architecture/test_web_landscape_mutation_fencing.py:5821-5825`
  `_reserve_CANCEL_CONSUMERS`: still at those lines; holds
  `composer_async_worker.py` = `863285bd…` (matches the live file), plus
  `composer_turn.py` and `composer_watch_reads.py`.
- `tests/helpers/required_work_reserve_recipes/reviewed-full-source-guards.json`:
  12 keys, `composer_operation_authority.py` among them; all 12 sha256 values
  match the live files.
- Not in the brief: `web_reserve_worker_caller.py:27` and
  `web_reserve_worker_six.py:97` pin `_run_started_under_lease`, the function
  holding the `:760` budget read. `tests/helpers/web_reserve_terminal_sql_transfers.py:15`
  also binds the authority file. `sessions/routes/composer/operations.py`
  (a `deadline_remaining_ms` producer) is whole-file pinned as
  `_reserve_CANCEL_ROUTE_SHA256` (`…fencing.py:5819-5820`).

**Tests.** `tests/unit/web/coordination/test_composer_operation_authority.py`
and `tests/unit/architecture/test_web_reserve_reviewed_contracts.py` exist.

**Brief corrections.**
1. Add the three extra deadline sites and the `timeout_seconds` derivation
   above to the file list.
2. Add the three extra pin files to "Pinned source hashes".
3. `deadline_remaining_ms` consumers (verified by grep):
   `sessions/schemas.py`, `sessions/routes/composer/operations.py`,
   `FE/types/composerOperations.ts`, `FE/api/composerOperationDecoder.ts`,
   `FE/api/composerOperationObserver.ts`, and two acceptance probes
   (`web/azure_container_apps_observations.py`,
   `web/_acceptance_common/replica_probes.py`). The brief names none.
4. A distinct "queued too long" outcome needs a new failure code.
   `sessions/composer_operation_errors.py` is in
   `reviewed-full-source-guards.json`, and the outcome notice is
   `FE/components/chat/ComposerOperationOutcomeNotice.tsx` (changed in
   0.8.13). Inferred: both are touched.
5. Say whether the queue-wait bound is derived from `created_at` or needs a
   column. 0.8.12 and 0.8.13 changed no schema epoch (session epoch 78); the
   delivery posture requires stating it if this lane does.

**File overlap.** The two named files, the tests, the two brief-named pin
files, plus (not in the brief) `web_reserve_worker_caller.py`,
`web_reserve_worker_six.py`, possibly `composer_operation_errors.py`,
`sessions/schemas.py`, `operations.py` and the three frontend files.
Collision with CDA-23 is only on the two shared pin files, as planned. If a
new failure code is mapped in `helpers`, that file is shared with CDA-23b.

---

## CDA-22: STILL-PRESENT

**Status (verified).** `csvc:2769-2770`:

```python
        if turn_has_discovery:
            return _ClassifyOutcome(action="continue", discovery_turns_delta=1)
```

`composer_max_discovery_turns` is not read anywhere in `csvc`. `__init__`
sets only `self._max_composition_turns` (`csvc:785`).

**Anchors.**

| Brief | New base |
|---|---|
| `service.py` ~L1809 (`discovery_turns_used` charge) | `csvc:2188` |
| `service.py` ~L3151 (driver accumulates) | `csvc:3588` |
| `_classify_and_budget_turn` ~L2173-2340 | def `csvc:2075`; budget block `:2587-2770` |
| comment ~L2173 "Discovery counts are diagnostic…" | `csvc:2587-2592` |
| `__init__` ~L664 `_max_composition_turns` | `csvc:785` |
| `_surface_and_finalize_no_tools` (last-chance) | call at `csvc:2724` |
| `pipeline_planner.py:4858` | `planner:5250` |
| `composer/protocol.py:545-553` | `:553-562` |
| `sessions/routes/_helpers.py:3115` | `helpers:3130` |
| `test_pipeline_planner.py:6732` | `:6968` |
| `test_advisor_tool.py:614` | `:615` |
| `test_service.py` ~L7989 | `:8069` |
| `test_service.py:3586` | `:3637` |
| `test_service.py:3367` | `:3415` |
| `test_service.py:5442` | `:5522` |
| `test_discovery_terminal_preview.py:97` | unchanged |

**Tests.** All four pinned tests exist under the same names. `test_service.py`,
`test_progress.py`, `test_advisor_tool.py`, `test_discovery_terminal_preview.py`
exist.

**Brief corrections.**
1. "Mirror the composition path" now includes the context budget. The
   composition last-chance call is `_prepare_primary_delivery(…, budget_frame=budget_frame, …)`
   (`csvc:2599-2611`), then `_call_llm_before_deadline(…, context_budget=final_budget_record)`
   (`:2612-2626`), then `turn_evidence.confirm_delivery(final_delivery, …)`
   (`:2644-2645`). The discovery last-chance call must do all three;
   `context_budget` is a required keyword (`csvc:3734`).
2. The last-chance request can be refused by the context budget before the
   provider call (`ComposerConvergenceError` with
   `reason="context_budget_exceeded"`, `composer/protocol.py:612-613`). Add a
   test bullet: that refusal surfaces as itself, not as
   `convergence_discovery_budget`.
3. Also mirror the advisor gate and the deadline branch of the composition
   path (`csvc:2646-2711`: `_evaluate_terminal_no_tool_advisor_gate`,
   `_AdvisorCheckpointComposeDeadlineExpired` → `_persist_withheld_reply`),
   and `_terminal_recovery_result` / `_enforce_tool_call_cap`
   (`:2627-2641`). The brief mentions only the shared finalize.
4. The docstring at `csvc:2119-2122` also states the old design ("Discovery
   continues within the request deadline without an aggregate turn cap");
   replace it with the comment.
5. "Shipped profiles' value of 10" is wrong (also on `main`): systemd and
   compose examples use 10, AWS ECS uses **8**
   (`deploy/aws-ecs/terraform/modules/scenario/locals.tf:474`), Azure uses 20
   (`workload.acceptance.bicepparam:27`, `workload.production.bicepparam:29`).
   The audit ran with 24 (README L91). The acceptance line "the battery never
   exceeded 9 provider calls per tool-loop turn" does not clear the AWS value;
   the lane must count discovery-only rounds per turn, not provider calls.
6. `ComposerConvergenceError` now takes a `reason`
   (`convergence_progress_event(budget_exhausted=…, reason=exc.reason)`,
   `csvc:1428`, `turn:1586`). `budget_exhausted="discovery"` still maps to
   `convergence_discovery_budget` (`composer/protocol.py:560-561`); no change
   needed there.

**File overlap.** `csvc` and `test_service.py`,
`test_discovery_terminal_preview.py`. `_classify_and_budget_turn` and
`__init__` are not the pinned `ComposerServiceImpl.compose` method (inferred
from `web_reserve_compose_proposal_transfers.py:394-399`; run the reserve
contracts test to confirm). Collides with CDA-20 on `csvc` under CDA-20's
corrected file list, in a different function (`_call_llm_before_deadline`,
`:3722`).

---

## CDA-23: STILL-PRESENT (one narrow mid-turn sub-case closed upstream; F2 reachability changed)

**Status (verified).**
- Turn-end writer: `turn:1168` `elif result.state.version != state.version:`
  leads unconditionally to
  `service.save_composition_state(…, provenance="post_compose", …)` at
  `turn:1258-1264`. Unchanged in substance since `main` (only +2 lines of
  imports above it).
- Mid-turn writer: `svc` is byte-identical to `main`.
  `persist_compose_turn` inserts whenever a tool row carries a payload
  (`svc:2142-2160`).
- No `snapshot_noop.py`, no `composition_state_unchanged` kind, no
  `save_composition_state_unless_unchanged` (grep of `src` and `tests`).

**What 0.8.12 already does, and does not.**
- `set_pipeline` under auto-commit whose candidate equals the current state
  no longer advances the in-memory version (`tool_batch.py:1856-1928`,
  `:3545`), so it produces no mid-turn payload and no row. Commit
  `00ac53e07`.
- The other mutation tools under auto-commit are not covered. The spike's
  three mid-turn duplicates came from `upsert_node`, `upsert_edge` and
  `patch_node_options` (P10 F1), so none of the 20 measured duplicates
  (17 turn-end, 3 mid-turn) is closed by 0.8.12. Inferred from the tool
  names; the spike does not record the sessions' trust mode.
- Under `explicit_approve`, a no-op mutation is returned to the model and no
  proposal is staged. That prevents no-op proposals, not no-op state rows.
- Brief goal and acceptance still undone: everything.

**Design contradictions and reuse.**
1. **Acceptance 3 is now false for one out-of-scope writer.** "Every
   out-of-scope writer still mints on identical content" does not hold for
   proposal settlement on the new base: an accepted proposal whose result
   equals the current state is rejected as `proposal_no_change`
   (`proposals.py:612-641`, `pipeline_settlement.py:496, :528-540`,
   `pipeline_commit.py:555`; new rejection reason `"no_change"` at
   `sessions/protocol.py:129` and `proposal_authority.py:102`). Reword the
   exclusion list: proposal settlement neither mints nor reuses the head on
   identical content; it refuses. Keep the two pinned boundary tests on
   revert and interpretation resolve.
2. **"Pending proposals … no longer made stale by such a turn (F2)" is
   mostly unreachable under `explicit_approve` now.** While a composition
   proposal is pending, further mutations are refused
   (`approval_proposal_pending`, `tool_batch.py:1653-1664`,
   `_approval_containment_refusal` at `:431`), so the in-memory version does
   not move and `turn:1168` is false. The service-level test ("a
   `PresentBase` proposal stays settleable after a no-op turn") is still
   valid as a direct service test; the user-facing bullet should be limited
   to auto-commit sessions and blob-only proposals (the changelog's stated
   exclusions). Inferred from the containment code; not executed.
3. **Reuse the predicate's preimage as 0.8.12 uses it.** Admission compares
   `composition_content_hash` on in-memory states. The brief's clause 2 uses
   the same preimage against the stored head, which is right; note in the
   brief that the admission check and the writer check are two layers of one
   rule so a later change keeps them equal.
4. **Use the 0.8.13 audit-envelope shape** (`composer/reply_stage_refusal.py`)
   for `composition_state_unchanged`, alongside the `AdvisorGraphReviewFact`
   precedent the brief cites.
5. **The 23b wire value `unchanged` has a near-namesake.** The tool-result
   disposition `no_change` and the HTTP `proposal_no_change` already exist,
   and the frontend already treats `proposal_no_change` as a terminal
   proposal refusal (`FE/stores/sessionStore.ts`, `:1429`).
   State in the brief that `unchanged` (a state-row outcome) is distinct
   from `no_change` (an admission outcome), or pick one word.
6. Progress copy already exists for the admission no-op: "The pipeline is
   unchanged." (`tool_batch.py:3615-3624`). The turn-end headline change
   should not contradict it.

**Anchors.**

| Brief | New base |
|---|---|
| `composer/pipeline_proposal.py:746` | unchanged |
| `sessions/proposal_authority.py:222` | `:223` |
| `sessions/service.py:3628` vs `:3757` | unchanged |
| `sessions/service.py` ~L7201-7208 | unchanged (`:7198-7210`) |
| `persist_compose_turn` `service.py:1921`; insert ~L2142-2160; stale guard ~L2095-2113 | unchanged |
| `_run_composer_turn` `composer_turn.py:1166-1270` | def `turn:842`; branch `turn:1168-1275` |
| `save_composition_state` `service.py:6385` | unchanged |
| `append_state` `coordination/repository.py:858-955` | `:859-956` |
| revert `service.py:8020` | unchanged |
| fork `service.py:8685` | unchanged |
| `repository.py:4206 insert_child_state` | `:4325` |
| `resolve_interpretation_event` `service.py:5141` | unchanged |
| opt-out append `repository.py:1359-1380` | `:1360-1396` (insert at `:1376-1396`) |
| `pending_interpretation.py:2147` | unchanged |
| `_settle_pipeline_composition_proposal` `service.py:3582` | unchanged |
| `save_composition_state_with_interpretations` `service.py:6450` | unchanged |
| failure-path persists `routes/_helpers.py:3346-3813` | `helpers:3379-3849` (`convergence_persist` `:3382, :3390`; `plugin_crash_persist` `:3566, :3574`; `preflight_persist` `:3841, :3849`) |
| `test_routes.py` ~L11520 | `tests/unit/web/sessions/test_routes.py:11504, :11549, :11619` |
| `no_new_checkpoint` `service.py:2261-2265` | unchanged |
| private evidence row `service.py:2181-2194` | unchanged |
| `_effective_state_record_on_connection` `service.py:7146` | unchanged |
| review-fact `else` branch `composer_turn.py` ~L1273-1306 | `turn:1276-1308` |
| guard L1276-1277 | `turn:1278-1279` |
| saving copy L1245 | `turn:1245-1249` (evidence string at `:1247`) |
| completion headline L1358 | `turn:1360-1362` |
| `protocol.py:1189` (`AdvisorGraphReviewFact` precedent) | `sessions/protocol.py:1190` (the brief does not say which `protocol.py`; `composer/protocol.py` has no such symbol) |
| `_reserve_sequence_range` count L2115 | unchanged (`svc:2115-2117`) |
| fork copy filter `service.py:8429-8436` | unchanged |
| `sessionStore.ts:529` | `FE/stores/sessionStore.ts:535` |
| `_ToolCallOutcomeKind` `routes/_helpers.py:383` | `helpers:388` |
| `types/index.ts:110` | unchanged |
| `toolCallDescriptions.ts:168` (and `:168-190`) | unchanged |
| `COMPLETED` fallback `_helpers.py:556` | `helpers:561` |
| `persist_compose_turn` ~L2095-2265 | unchanged |
| `save_composition_state_with_interpretations` pattern ~L6503-6545 | unchanged |
| `required_work` ticket ~L6414-6447 | unchanged |
| fork filter ~L8429 | unchanged |
| `composer_turn.py` L1166-1306 | `turn:1168-1308` |
| `composer/turn_audit.py` ~L583-603 | unchanged |
| `routes/_helpers.py` ~L383-556 | `helpers:388-561` |
| `ToolCallCard.tsx:105-109` | unchanged |
| `evals/lib/composer_rgr_score.py:444-463`, docstring ~L93-97 | unchanged |
| `test_web_landscape_mutation_fencing.py:5821-5825` | same lines; `composer_turn.py` hash re-pinned to `cfc7cf22…` (matches live) |
| `turn_audit.py:147-151` | unchanged |
| `_helpers.py:488-507` (fallback drain) | `helpers:493-512` |
| `test_session_operation_fence_postgres.py` L94-120 | unchanged |
| `test_persist_compose_turn.py` L131, L1481 | unchanged |
| `test_checkpoint_receipts.py` L370-389 | unchanged; full path is `tests/unit/web/composer/test_checkpoint_receipts.py` |
| P10 F2 table `_helpers.py:470-477` | `helpers:475-482` |
| `evals/composer-live-audit/spikes/P10-snapshot-skip/p10_measure.py` | absent on this base; audit branch only |

**Tests.**
- Exist, unchanged: `test_persist_compose_turn.py` (both pinned tests at
  L131 and L1481), `test_run_composer_turn.py`, `test_tool_call_outcomes.py`,
  `test_session_operation_fence_postgres.py`, `toolCallDescriptions.test.ts`,
  `ToolCallCard.test.tsx`, `FE/components/header/HeaderVersionSelector.test.tsx`,
  `FE/components/header/versionLabels.test.ts`.
- Absent (new, as briefed): `tests/unit/web/sessions/test_snapshot_noop.py`.
- `tests/unit/web/sessions` gained 2,816 lines in 22 files since `main`
  (proposal accept, blob proposals, graph recovery, reply-stage refusal,
  routes state). The brief's "other tests that reuse identical states may
  surface" applies to these too; they were not in the audit's sweep.

**Pins (verified).**
- `tests/helpers/web_reserve_provider_mint_contract.py`: still holds the
  full source of `_run_composer_turn`, re-pinned on this base; it contains
  the "A new composition state version is being stored" string once.
- `reviewed-full-source-guards.json`: holds `sessions/service.py` and
  `sessions/protocol.py`; both hashes match live.
- `_reserve_CANCEL_CONSUMERS`: holds `composer_turn.py`; matches live.
- `reviewed-writer-recipes.json` (145 entries) and
  `reviewed-non-dml-recipes.json` (46 entries) exist; both changed since
  `main`.
- **Not in the brief:** `tests/helpers/web_reserve_route_helper.py:93`
  (sha256 of `_run_composer_turn`) and
  `tests/helpers/web_reserve_compose_proposal_transfers.py:388`
  (structural-AST hash of `_run_composer_turn`). Any edit to
  `_run_composer_turn` trips four pins, not two.

**Brief corrections.** Items 1-6 under "Design contradictions" above, plus:
7. Add the two extra pin files.
8. Name the `protocol.py` in the precedent citation as
   `sessions/protocol.py:1190`.
9. Acceptance 2: state where `p10_measure.py` comes from (merge the audit
   branch's `evals/composer-live-audit/` first, or run it from the audit
   worktree).
10. Line shifts: `turn` +2 through L1498; `helpers` +5 to ~L3010, +15 at L3130,
    +33 from L3380 on; `repository.py` +1 to L2120, +119 from ~L3330;
    `sessionStore.ts` +6 at L529; `proposal_authority.py` +1.

**"Shares no source file with any other lane": re-test.**
- Against the brief text (grep of §5 by lane, done on the audit worktree's
  `work-packages.md`): holds for `composer_turn.py`, `sessions/service.py`,
  `sessions/protocol.py`, `turn_audit.py`, `ToolCallCard`,
  `toolCallDescriptions`, `types/index.ts`, `repository.py`. The only other
  mention of `routes/_helpers.py` is CDA-22's, as a "not this one" reference.
- Against the corrected file lists in this report, it no longer holds:
  - `FE/types/index.ts` and `ToolCallCard.tsx`: CDA-14 option (a).
  - `helpers`: CDA-17 if it adds a planner code mapping; CDA-21 if it maps a
    new "queued too long" code.
  - `tests/helpers/web_reserve_route_helper.py` pins
    `_handle_convergence_error` as well as `_run_composer_turn`; a lane that
    edits `_handle_convergence_error` in `helpers` shares this pin file.
  - Shared pin files with CDA-21 remain as briefed.
- Not re-tested here: wave 1-3 lanes' corrected file lists (other
  re-anchor reports). CDA-07/08/09 are the likeliest to reach
  `composer_turn.py`; the briefs do not name it.

**File overlap (complete list for a correct fix).**
`sessions/snapshot_noop.py` (new), `svc`, `sessions/protocol.py`, `turn`,
`composer/turn_audit.py`, `evals/lib/composer_rgr_score.py`; 23b: `helpers`,
`FE/types/index.ts`, `FE/components/chat/toolCallDescriptions.ts`,
`FE/components/chat/ToolCallCard.tsx`. Pins: `web_reserve_provider_mint_contract.py`,
`reviewed-full-source-guards.json`, `test_web_landscape_mutation_fencing.py`,
`reviewed-writer-recipes.json` / `reviewed-non-dml-recipes.json` if the
writer recipe changes, and (not in the brief) `web_reserve_route_helper.py`,
`web_reserve_compose_proposal_transfers.py`.

---

## Limits of this check

- No test, lint or gate was run. Pin-trip predictions for function-level
  hashes are inferred from reading the helper files.
- CDA-14's option analysis rests on the wire types, the decoder and the
  redaction model; I did not trace the accept path to confirm what the
  committed state returns under each trust mode.
- CDA-16's cache-rate statements are structural readings, not measurements.
- CDA-23's F2 reachability claim rests on reading the containment refusal;
  no session was replayed.
