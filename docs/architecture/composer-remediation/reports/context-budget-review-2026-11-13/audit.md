# Composer context budget (0.8.13): what is recorded and what is shown

Adversarial review, read-only. Tree `integration/composer-remediation` at
`ca8b36001` (the range `43e5f66cb..be60978f9` plus the CDA-05/06 merges on
top). Scope: audit record, progress events, refusal path to HTTP and the
frontend, operator configuration and docs. The core arithmetic and the
provider call sites were treated as given.

## Verdict

No high-severity defect. The record and the wire path are well pinned (43
mutants, 4 real survivors, all in one place). Three medium findings: a
refusal copy that is false for one class of cause, fatal refusals that
record no sizes, and planner failure-path records that no test protects.
The rest is low: lenient read-side validation, a hash-only budget, and
documentation gaps.

Labels: VERIFIED = demonstrated by execution or by a direct grep/diff of the
tree. READ = reasoned from code, not executed.

## Real defects

### D1 (medium, VERIFIED): the refusal copy is false when the fixed prefix is the cause

- `src/elspeth/contracts/composer_progress.py:56` (`CONTEXT_BUDGET_REFUSAL_DETAIL`),
  `src/elspeth/web/composer/progress.py:462-474`,
  `src/elspeth/web/sessions/routes/_helpers.py:3098`,
  `src/elspeth/web/frontend/src/config/composer.ts:19`.
- Every cause gets one sentence: "This conversation is too large for the
  composer to continue. Start a new session, or shorten your request."
- Scenario: the operator sets `ELSPETH_WEB__COMPOSER_CONTEXT_MAX_PROMPT_TOKENS`
  below what the system skill, catalog and tool definitions need (any value
  from 1 up is accepted). The first message of a brand-new session is refused
  with `largest_component_system`, and the user is told to start a new
  session. Every new session fails the same way. Only an operator can fix it.
- Evidence: the project's own passing test
  `tests/integration/web/composer/test_freeform_planner_failure_translation.py:1022-1063`
  builds exactly this (cap 1,000 tokens, fresh session, first message) and
  asserts both `detail_codes == ["context_ceiling_tokens", "largest_component_system"]`
  and the "Start a new session" detail and `likely_next`.
- The backend already knows the cause (`detail_codes`). The frontend never
  reads `detail_codes` (no reference outside tests), so the named component
  reaches the wire and stops there.
- "Shorten your request" is also untrue when the largest component is
  `state` or `latest_tool_round`; "start a new session" does hold for those.
- Smallest fix: when the largest component is `system`, `catalog`, `tools`
  or `reference`, return a second fixed detail ("The composer's context
  budget is set too low for this deployment. Ask an administrator to raise
  it.") from the same two server sites. Optionally refuse at startup or in
  doctor when the fixed prefix alone exceeds the cap.

### D2 (medium, VERIFIED by grep): a refusal that fails the turn records no sizes

- Compose loop: `src/elspeth/web/composer/service.py:1804-1812` builds
  evidence with `estimated_tokens`, `request_bytes`, `max_prompt_tokens`,
  `max_request_bytes`. The only consumers of `exc.evidence` are
  `_helpers.py:3325` (ceiling and component only) and the reply-stage branch
  at `service.py:2391-2396`. The four numbers are dropped.
- Planner: `pipeline_planner.py:3415-3420` never carries the numbers at all;
  the `planner_failure_disposition` row holds the two codes.
- Result: the optional reply-stage refusal (turn succeeds) writes a full
  `composer_reply_stage_refusal` row with estimate, bytes and both ceilings.
  The refusals that actually fail a turn record only "tokens|bytes" and a
  component name. A reviewer cannot check that the refusal was correct, or
  by how much the request missed.
- The compose-loop refusal also writes no audit row in the session
  transcript; its only trace is the sealed operation body
  (`composer_async_operations.result_json`). The planner one does write a row.
- The design spec states the codes-only shape, so this is a gap against the
  audit goal, not against the spec.
- Smallest fix: write one audit row for the fatal refusal with the same
  envelope shape the reply stage uses (the evidence dict already has every
  field), and give `PipelinePlannerError` the same numbers.

## Test gaps

### T1 (medium, VERIFIED): planner failure-path records can drop `context_budget` unnoticed

- `src/elspeth/web/composer/pipeline_planner.py:4075` (provider failure),
  `:4233` (cancelled), `:4265` (timeout), `:4361` (credential refused).
- Mutant: `context_budget=context_budget` to `context_budget=None` at each
  site, one at a time. All four survive 483 tests
  (`test_planner_context_budget.py`, `test_pipeline_planner.py`,
  `test_provider_context_planner_integration.py`,
  `test_context_budget_history.py`). The success site (`:4339`) is killed.
- Why it matters: a planner request that was shed, sent, and then failed
  would store no shedding record, and it would read as "not a budgeted
  call". `ContextBudgetHistory.from_records` takes the sticky shed set from
  the newest budgeted call of any status, so the same loss also changes what
  the next turn keeps shed.
- Smallest fix: one parametrised planner test per failure status asserting
  `recorder.llm_calls[-1].context_budget` is the shed record. The compose
  loop has a single record site (`provider_gateway.py:923`), so it is not
  exposed the same way.

### T2 (low, VERIFIED): `tests/unit/web/composer/test_llm_context_budget_audit.py` pins the dataclass, not the call sites

18 tests, all on `ComposerContextBudgetRecord` and the public envelope. The
call-site behaviour is pinned elsewhere (`test_compose_loop_context_budget.py`,
`test_planner_context_budget.py`), and well: see the mutant table.

## Lower-severity findings

### L1 (low, VERIFIED): stored `context_budget` is validated only where it is consumed

- `src/elspeth/web/composer/context_budget_history.py:121-164`. The module
  docstring says a present-but-malformed envelope raises.
- Executed probe (`ContextBudgetHistory.from_records` on one stored row):
  accepted without error with `estimated_tokens_after` a string,
  `estimated_tokens_before` negative, `policy_hash` missing,
  `bytes_per_token` the text "nan", `stubbed_tool_call_ids` `[1, None]`,
  `shed_hatch_event_indices` `[-1]`, an unknown extra key holding a nested
  object, an unknown `status` word (silently treated as not successful), and
  a record holding only `shed_history_indices`.
- It does raise on a bool index, a non-mapping budget, and a bad
  `request_wire_bytes` (the fields it uses).
- There is no `from_dict` for `ComposerContextBudgetRecord`, so nothing reads
  the whole record strictly. No reader exists that would mis-act on the
  unchecked fields today; the cost is that corruption in them is never
  noticed.
- Smallest fix: add a strict `ComposerContextBudgetRecord.from_dict` (exact
  key set, exact types, status in the closed enum) and call it from
  `_budgeted_call`, keeping the "absent `request_wire_bytes`" allowance if
  pre-release records must stay readable.

### L2 (low, VERIFIED by grep): the budget in force is recorded only as a hash

- `ComposerContextBudgetRecord.policy_hash`
  (`contracts/composer_llm_audit.py:181`), from
  `context_budget.py:100-111`. Nothing stores or logs the values behind it.
- A reviewer holding only the record cannot say what the token cap, byte cap
  or marks were, so cannot check `estimated_tokens_after` against the cap.
  The reply-stage refusal row does carry both ceilings.
- Same pattern as the existing `planner_policy_hash`. Smallest fix: add
  `max_prompt_tokens` and `max_request_bytes` to the record (integers, no
  content).

### L3 (low, READ): shed messages are identified by a derived position

- Keys are `history:<index into _composer_chat_history(records)>`
  (`service.py:1689`, list built at `sessions/routes/_helpers.py:1844`).
  The index is a position in a filtered projection of the stored rows, not a
  row id or sequence number. User rows already carry their message id
  (`_elspeth_user_message_id`).
- To learn which messages were dropped, a reviewer must re-run that filter
  on the rows as they stood. If the filter's rules change in a later
  release, old indices point at different messages, for the reviewer and for
  the hysteresis read.
- The hysteresis state itself (released or held) is not recorded; it can be
  derived by comparing consecutive records.

### L4 (low, VERIFIED): doctor does not name the missing setting

- `src/elspeth/cli.py:321-324`. With the variable unset, the doctor check
  detail is exactly `web settings could not be loaded (ValidationError)`.
- Server startup itself does name the field: `composer_context_max_prompt_tokens
  Field required` (the field name, not the environment variable name).
- An operator upgrading to 0.8.13 who runs doctor first gets no pointer to
  the new required variable. The sanitiser is older than this change; the
  new required setting is what makes it bite.
- Smallest fix: for `type=missing` errors, include the field locations
  (they are setting names, not values).

## Documentation errors

- **Null `context_budget` is wider than documented** (low, READ).
  `CHANGELOG.md:105` says advisor and auto-title calls store null. The
  graph-recovery call (`service.py:1184`) and the run-diagnostics
  explanation (`service.py:1132`, gateway record at
  `provider_gateway.py:1093`) are also unbudgeted and store null. Neither is
  in Known limits.
- **`estimated_tokens_before` is not quite "the estimate of the unshed
  request"** (low, VERIFIED by the tests that pin it). It includes the
  256-byte omission allowance whenever anything is sheddable
  (`service.py:1726`; `test_compose_loop_context_budget.py:752` asserts
  `wire + CONTEXT_BUDGET_OMISSION_ALLOWANCE_BYTES`). The docstring and
  changelog say otherwise. The error is at most 128 tokens.
- **Operator guidance omits how the cap is measured** (low, READ).
  `docs/reference/environment-variables.md:165` says to set the gateway's
  prompt-token limit. It does not say the count is an estimate from request
  bytes (ratio 3.0 until measured, then clamped 2.0-6.0), or that completion
  tokens are outside it. The changelog's Known limits says the first; nothing
  says the second. An operator who enters a model's whole context window
  leaves no room for the reply.
- `docs/reference/configuration.md` has no entry for these settings; it does
  not document `ELSPETH_WEB__` settings generally, so this is consistent, not
  an omission.

## Design questions

- The cap accepts any integer from 1 (`config.py:375`). There is no check
  that the fixed prefix fits. See D1.
- The "earlier conversation is no longer visible" notice is a transient
  progress event. After the turn, or on reload, nothing user-visible says
  history was shed; the call record does.
- A request that is shed and then stopped by the prompt guard leaves no call
  record (`provider_gateway.py`, `except PromptGuardRefused: raise` with no
  status). Nothing was sent, so nothing false is recorded, but the shed
  notice may already have been shown.

## Surfaces found sound, and what was tried

**Audit honesty.** `request_wire_bytes` is measured on the cache-marked,
guard-detached request and re-set after shedding; tests compare it to the
bytes the fake provider received. Mutants that kept the decision's bound,
added one byte, swapped before/after estimates, recorded a wrong ratio, or
dropped stubbed ids or hatch indices were all killed. A refused request
leaves no call record and is distinguishable (disposition row, sealed
operation body, or reply-stage row).

**Old records.** Executed: a pre-0.8.13 call envelope with no
`context_budget` key reads as empty history, no ratio, no error. A null
value reads the same. No reader enforces a closed key set on stored call
envelopes, so old and new rows coexist.

**Leak check.** The record holds integers, a hash, a float repr, positions
and tool-call ids. Tool-call ids are provider text, but they pass the
credential scan at receipt (`provider_gateway.py:258`) and already appear in
tool rows. Refusal payloads carry closed-vocabulary codes only;
`ReplyStageRefusal` rejects a free-text component (mutants killed). Progress
events are fixed strings.

**Refusal wire path.** Both backend sources (`CONTEXT_BUDGET_EXHAUSTED`,
`REQUEST_BYTES_EXHAUSTED`, and the compose loop's convergence carrier) map to
HTTP 422, `failure_code: context_budget_exceeded`, sealed category
`context_budget_exceeded`. No cause falls through to "operation failed". The
frontend decoder throws on an unknown category and on an unknown progress
reason. A stale client that meets `context_reduced` drops to durable polling
(`composerOperationObserver.ts:222`), it does not fail the turn. The store
keeps the failed user row with its text and shows no Retry.

**Operator configuration.** Executed against `settings_from_env` with the
shipped systemd example as the base: the example loads (200000, 0.85, 0.6,
983040). Unset, empty, `200000.0`, `200k`, `0` and `null` are all rejected.
`SHED_ABOVE=1` is accepted; `nan` is rejected; `SHED_TO` equal to or above
`SHED_ABOVE` is rejected with a clear message. Every deployment surface
carries the variable: compose, systemd example, Terraform locals, Bicep
(parameter, both parameter files, validator, resolver, example JSON),
Docker guide, local-development and first-pipeline guides, website,
Playwright config, acceptance server. The second breaking change (960 KiB
default, now also bounding the compose loop) is in the changelog, README,
environment reference and the AI risk assessment; no shipped surface pins
the old 2 MiB value.

## Mutation results

Python, 33 mutants in a scratch copy, targeted tests with `-n 2`:

| Group | Mutants | Killed | Survived |
|---|---|---|---|
| Omit the shed record (loop, planner success, public envelope field) | 3 | 3 | 0 |
| Omit the shed record on planner failure paths | 4 | 0 | 4 (T1) |
| Pre-shed estimate recorded as post-shed, and the reverse | 3 | 3 | 0 |
| `request_wire_bytes` stale, off by one, or pre-shed | 4 | 3 | 1 (equivalent: the value is always overwritten at `service.py:1623`) |
| What was shed, ratio, policy hash | 4 | 4 | 0 |
| Drop or corrupt a field of a wire shape (record, HTTP bodies, reply-stage row, sealed category, code mappings) | 10 | 10 | 0 |
| Accept an unknown cause (reply-stage ceiling, component) | 2 | 2 | 0 |
| Skip a malformed stored budget instead of raising | 1 | 1 | 0 |
| Shed notice never sent, or sent on every call | 2 | 2 | 0 |

Frontend, 10 mutants, vitest: all killed (accept unknown category, accept
unknown progress reason, category not decoded, each new reason not decoded,
store drops the verbatim detail, Retry offered, notice drops the detail,
observer uses the operation's code, client ignores the nested
`failure_code`; the last needed `src/api/client.execution-errors.test.ts`).

## Runs

- `tests/unit/web/composer/test_llm_context_budget_audit.py`: 18 passed, exit 0.
- Baseline against the scratch copy, 14 files (budget, planner, reply stage,
  history, routes, translation, progress, parity, carriers, config): 1166
  passed, exit 0.
- Frontend decoder, notice, store, config and message-bubble tests: 262
  passed, exit 0.
- Not run: the full suite, the PostgreSQL suites, a live provider, browser
  end-to-end tests. Nothing here speaks to suite-level health.

Scratch harness and logs:
`<scratchpad>/ctx-audit/`
(`mutants.py`, `mut2.py`, `femut.py`, `cfg_probe.py`, `read_probe.py`,
`doctor_probe.py`, `m2_chunk*.txt`). No tracked file was edited.

```json
{"findings": [
  {"title": "Context budget refusal tells the user to start a new session when the operator's cap is below the fixed prefix",
   "severity": "medium",
   "confidence": "confirmed",
   "files": ["src/elspeth/contracts/composer_progress.py", "src/elspeth/web/composer/progress.py", "src/elspeth/web/sessions/routes/_helpers.py", "src/elspeth/web/frontend/src/config/composer.ts"],
   "repro": "Run tests/integration/web/composer/test_freeform_planner_failure_translation.py::test_send_message_planner_context_budget_refusal_names_its_cause_on_the_operation_record -n 0: cap 1000 tokens, fresh session, first message; it asserts detail_codes ['context_ceiling_tokens','largest_component_system'] together with the detail 'Start a new session, or shorten your request.'",
   "detail": "One fixed sentence serves every cause. When the largest component is system, catalog, tools or reference, neither a new session nor a shorter request can help; only an operator can. detail_codes names the component on the wire but the frontend never reads it, and the cap accepts any integer from 1 with no startup check."},
  {"title": "Refusals that fail a turn record no estimate, size or ceiling values",
   "severity": "medium",
   "confidence": "confirmed",
   "files": ["src/elspeth/web/composer/service.py", "src/elspeth/web/sessions/routes/_helpers.py", "src/elspeth/web/composer/pipeline_planner.py"],
   "repro": "grep -rn '\\.evidence\\b' src/elspeth/web: the only readers of the refusal evidence are _helpers.py:3325 (ceiling, largest_component) and the reply-stage branch at service.py:2391-2396. pipeline_planner.py:3415-3420 builds the planner refusal with two detail codes and no numbers.",
   "detail": "The compose loop computes estimated_tokens, request_bytes and both ceilings at service.py:1804-1812 and drops them. The optional reply-stage refusal writes all of them to an audit row; the fatal refusals keep only 'tokens|bytes' and a component name, and the compose-loop one writes no transcript audit row at all. A reviewer cannot verify a fatal refusal from the record."},
  {"title": "No test protects context_budget on the planner's four failure-path call records",
   "severity": "medium",
   "confidence": "confirmed",
   "files": ["src/elspeth/web/composer/pipeline_planner.py", "tests/unit/web/composer/test_planner_context_budget.py"],
   "repro": "In a scratch copy set context_budget=None at pipeline_planner.py:4075, 4233, 4265 or 4361 (one at a time) and run test_planner_context_budget.py, test_pipeline_planner.py, test_provider_context_planner_integration.py and test_context_budget_history.py with -n 2: 483 passed each time.",
   "detail": "A planner request that was shed, sent and then failed, timed out, was cancelled or was credential-refused could store no shedding record. It would read as an unbudgeted call, and ContextBudgetHistory.from_records, which takes the sticky shed set from the newest budgeted call of any status, would see a different history."},
  {"title": "Stored context_budget records are validated only in the two fields the history reader uses",
   "severity": "low",
   "confidence": "confirmed",
   "files": ["src/elspeth/web/composer/context_budget_history.py", "src/elspeth/contracts/composer_llm_audit.py"],
   "repro": "Call ContextBudgetHistory.from_records on one audit row whose context_budget has estimated_tokens_after='lots', or stubbed_tool_call_ids=[1, None], or an unknown extra key, or whose call status is an unknown word: each is accepted without error.",
   "detail": "The module docstring says a present-but-malformed envelope raises. Only shed_history_indices and request_wire_bytes are checked; there is no strict from_dict for the record, and an unknown status is silently treated as not successful."},
  {"title": "The budget in force is recorded only as policy_hash",
   "severity": "low",
   "confidence": "confirmed",
   "files": ["src/elspeth/contracts/composer_llm_audit.py", "src/elspeth/web/composer/context_budget.py"],
   "repro": "grep -rn 'audit_hash\\|composer.context-budget.v1' src/elspeth/web: the policy values are hashed at context_budget.py:100-111 and never stored or logged.",
   "detail": "From the call record alone a reviewer cannot state the token cap, byte cap or marks, so cannot check estimated_tokens_after against the cap. The reply-stage refusal row does carry both ceilings."},
  {"title": "Doctor reports the missing required setting as a bare ValidationError",
   "severity": "low",
   "confidence": "confirmed",
   "files": ["src/elspeth/cli.py", "src/elspeth/web/doctor.py"],
   "repro": "With the shipped systemd env example loaded and ELSPETH_WEB__COMPOSER_CONTEXT_MAX_PROMPT_TOKENS removed, sanitize_error('web settings could not be loaded', exc) yields 'web settings could not be loaded (ValidationError)'.",
   "detail": "The upgrade makes one setting required with no default. Server startup names the field; the doctor check an operator runs first does not."},
  {"title": "Documentation: null context_budget, estimated_tokens_before and the cap's measure are described inexactly",
   "severity": "low",
   "confidence": "probable",
   "files": ["CHANGELOG.md", "docs/reference/environment-variables.md", "src/elspeth/contracts/composer_llm_audit.py"],
   "repro": "Read CHANGELOG.md:105 against service.py:1132 and :1184 (graph-recovery and run-diagnostics calls are also unbudgeted); read service.py:1726 against the estimated_tokens_before docstring; read environment-variables.md:165.",
   "detail": "Two more call kinds store a null context_budget than the changelog lists. estimated_tokens_before includes a 256-byte allowance when anything is sheddable. The environment reference does not say the token count is a byte-based estimate or that completion tokens sit outside the cap."}
]}
```
