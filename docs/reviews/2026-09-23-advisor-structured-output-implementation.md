# Advisor structured-output implementation

The approved plan, `docs/plans/2026-09-23-advisor-structured-output-prompt.md`,
replaces checkpoint prose parsing with strict JSON admission, separates technical
findings from the user note, and records provider conformance in the durable
checkpoint pass. Manual advisor hints retain their prose contract. The operator's
subsequent instruction removes backward-compatibility requirements: only the new
checkpoint contract is supported. The plan was supplied from the primary checkout.

Implementation and review fixes are complete. The follow-up full suite retains
the two known base failures; its PostgreSQL run recorded one readiness deadline
failure. The exact results and follow-up checks are disclosed below. This
document does not claim deployment or live-provider acceptance.

## Review follow-up: items 1–5

The follow-up repairs the boot probe, boot diagnostics, retry wording and note
sanitizer. The independent quality review's final verdict is GO for these bounded
changes; its earlier markdown-whitespace finding was repaired and re-reviewed.
No backward-compatibility parser, missing-field default, historical-row upgrade
or backfill was added. Deployment and live-provider acceptance remain unperformed.

1. **Probe schema conformance with a neutral prompt.**
   `src/elspeth/web/composer/boot_probe.py` sends only
   `This is a configuration check. Reply with ok.` in the advisor probe messages.
   The production schema remains in `response_format` through the shared request
   builder, with the existing completion budget and provider options. The probe
   checks strict JSON and field-schema admission, not checkpoint semantics.
   Schema-valid CLEAN replies containing a note or steps, and FLAGGED replies
   containing blank findings, therefore pass this configuration check while
   runtime checkpoints continue to reject them. Prose, fenced/truncated JSON,
   duplicate keys, empty/reasoning-only content, wrong field types, malformed
   envelopes and tool calls still fail the probe. A neutral prompt can expose an
   ignored schema when the provider returns prose; one conforming response cannot
   prove provider enforcement across future requests.

2. **Use the approved advisor timeout and disclose unverified boots.**
   `src/elspeth/web/app.py` gives the advisor 60 seconds and retains the planner's
   5 seconds, selecting by explicit role. Archived runtime evidence contained
   20 successful advisor checkpoint calls: nearest-rank p50 **4.228 s**, p95
   **11.018 s**, maximum **50.813 s**; four exceeded 5 seconds. These observations
   predate the structured-output probe and are not a boot-latency or outage
   reliability measurement. After disclosure of the **55-second additional
   maximum startup delay** (sequential probe caps increase from 10 to 65 seconds),
   the user approved: **“yes 60 seconds is fine”**. Advisor timeout and other
   nonfatal provider/transport warnings carry
   `structured_output_conformance_verified=False` and state that conformance was
   not verified this boot. Planner warnings omit that advisor-specific field.
   Timeout remains nonfatal; the longer allowance cannot guarantee verification.

3. **Report rejected requests without guessing the cause or copying provider text.**
   Both planner and advisor `BadRequestError` branches now name the role and model
   and report actual outbound presence of temperature, seed, reasoning effort,
   response format and provider routing. Presence is derived from request kwargs,
   including zero-valued temperature and provider-specific reasoning keys.
   The former planner message interpolated raw SDK exception text without
   scrubbing. That was unsafe under the secret-scrub policy because provider
   errors can contain request data, endpoints or credentials; both roles now
   apply the same omission rule.
   Neither public error message interpolates raw provider exception text.
   Both retain `raise ... from exc`; tests assert the original cause identity.
   This removes raw text from the public message, not from the preserved exception
   object or diagnostic chain. The local gateway rejection test checks the new
   neutral wording, option-presence facts and existing cause contract.

4. **Distinguish schema failures from checkpoint-contract failures.**
   `src/elspeth/web/composer/service.py` selects one of two fixed backend
   re-prompts using explicit schema validity. The existing schema-invalid wording
   remains; the semantic wording says the schema passed but the output contract
   failed and states the CLEAN/FLAGGED invariants. Both use the existing
   `problem_summary` channel. Rejected provider text is never echoed. Runtime
   acceptance, retry bounds, final classification and conformance accounting are
   unchanged.

5. **Repair sanitization and inline the category schema.**
   `src/elspeth/web/composer/advisor_output.py` removes fence sentinels after
   removable Unicode characters, catching sentinels reconstructed by filtering.
   It redacts dotted hosts followed by a slash/path and counts those substitutions.
   Bare hosts without paths remain out of scope because they overlap option paths;
   filenames, relative file paths, versions, dotted step IDs and option paths have
   unchanged negative controls. Markdown URL redaction preserves its outer closing
   parenthesis while handling nested and escaped URL parentheses, adjacent links
   and whitespace after the opening `](`. Non-URL markdown destinations remain
   unchanged. The category vocabulary now comes from one ordinary Literal alias;
   Pydantic emits its enum inline, and tests reject `$ref` and `$defs` anywhere in
   the emitted schema while checking exact vocabulary parity.

### Latency source and measurement limits

The latency instrument opened SQLite databases read-only (`mode=ro` and
`PRAGMA query_only=on`) and expanded the audit envelope list with
`json_each(chat_messages.tool_calls)`. The current `data/sessions.db` contained
zero chat messages. These archived databases supplied the corroborated sample:

| Source relative to the primary checkout | Successful target-model calls | Calls matched to checkpoint passes |
| --- | ---: | ---: |
| `data/sessions.db.pre-advisor-note.20260922T132824Z/sessions.db` | 4 | 4 |
| `data/sessions.db.pre-epoch64-reset.20260921T173323Z/sessions.db` | 4 | 4 |
| `data/archives/local-refresh-20260921T122615Z/session/sessions.db` | 13 | 12 |

The selected model was `openrouter/z-ai/glm-5.3`. All 21 calls were unique by
model/start/finish/message hash, successful, without tools or planner policy,
and carried positive reasoning-token counts. Twenty matched a model-source
checkpoint pass in the same session within one second of call completion;
only these twenty form the reported checkpoint sample. The window spans
2026-09-20 07:18 through 2026-09-21 17:50 UTC. Quantiles use nearest rank
(`sorted_values[ceil(n*p)-1]`).

Known-positive synthetic audit rows were selected; planner-model, wrong-envelope,
wrong-role and timeout controls were excluded. Mutating the positive model
removed the match, and the `1..20` fixture yielded p50=10 and p95=19.
No session identities, message bodies or credentials were included in the
measurement report. No deployment environment file was read and no live
provider request was made.

These archived calls predate the new structured-output boot prompt. Reasoning
tokens show reasoning occurred, but do not establish the configured effort.
No failed target-model calls were present. The 60-second allowance covers the
observed successful maximum with 9.187 seconds of headroom; this small sample
cannot establish a future timeout rate or guarantee verification at startup.

## Follow-up TDD and focused verification

The following results are the lane-reported completed exits and raw summaries.
Selections overlap and must not be summed. Artifact names are relative to
`.claude/lanes/advisor-review-fixes/`.

| Selection / behavior | RED evidence | GREEN evidence |
| --- | --- | --- |
| Boot admission and symmetric rejection diagnostics | `boot-red.log`: exit 1, **9 failed, 23 passed** | `boot-green.log`: exit 0, **32 passed** |
| Neutral outbound advisor prompt | `boot-prompt-red.log`: exit 1, **1 failed** | Included in the 32 passing boot tests |
| Schema-valid but semantically invalid boot responses | `boot-semantic-red.log`: exit 1, **3 failed, 1 passed, 28 deselected** | Included in the 32 passing boot tests |
| Advisor warning signal | `app-warning-red.log`: exit 1, **one assertion failure**; planner absence control passed | `app-green.log`: exit 0, **9 passed, 225 deselected** |
| Explicit role timeout selection | `app-timeout-red.log`: exit 1, **one assertion failure**, observed `[5.0, 5.0]` instead of `[5.0, 60.0]` | Included in the 9 passing app cases; real cancellation uses shortened test constants |
| Schema-versus-semantic retry wording | `reprompt-red.log`: exit 1, **3 failed, 2 passed, 43 deselected** | `reprompt-green-final.log`: exit 0, **316 passed** |
| Sanitizer repairs and inline enum | `boundary-red.log`: exit 1, **11 failed, 92 passed** | `boundary-green.log`: exit 0, **103 passed** |
| Markdown destination opening whitespace | `boundary-whitespace-red.log`: exit 1, **4 failed, 1 passed, 103 deselected** | `boundary-whitespace-green.log`: exit 0, **108 passed** |
| Local gateway rejection integration | Updated existing gateway assertion | `boot-gateway-green.log`: exit 0, **3 passed, 6 deselected, 3 warnings** |

RED failures were behavior/schema assertions. The first `reprompt-green.log`
attempt exited 4 without collecting tests because a concurrent sanitizer edit
temporarily had a syntax error; it is not pass evidence. The completed final run
above supersedes it. Focused Ruff checks and formatting checks passed. Focused
boundary mypy passed both before and after the whitespace follow-up. Gateway
verification used a local mock server, not a live provider.

The independent reviewer re-ran a controlled markdown reproducer with no, space,
tab, newline and mixed opening whitespace: every URL retained a balanced wrapper
with one substitution, while the non-URL control remained unchanged with zero.
The reviewer inspected item 2's role-timeout and warning tests but did not repeat
the coordinator's archived latency measurement or app TDD history.

The combined boot/checkpoint/mock-discipline/masquerade selection also completed:
`scoped.log` and `scoped.exit` record exit **0**, **595 passed in 305.50s
(0:05:05)**. The 108 boundary cases and 9 app cases above are separate selections.

## Follow-up candidate and gates

Production candidate: `1d5be5e9f41ef3c48df1fa788855af1c12b4f406`.
Canonical full-gate evidence:
`/tmp/advisor-review-fixes-gates/20260923T005132Z-advisor-structured-output-1654288/summary.txt`.
The gate recorded `frozen=yes`, with the same candidate and tracked-tree state
before and after.

| Stage | Exit | Result |
| --- | --- | --- |
| Ruff | 0 | Passed |
| Mypy | 0 | Passed |
| Contracts | 0 | Passed |
| All-rule lints | 1 | 2,286 findings; unchanged exact corpus against the fresh baseline |
| Default pytest | 1 | `2 failed, 55921 passed, 100 skipped, 2 xfailed, 98 warnings in 1296.63s (0:21:36)` |
| PostgreSQL testcontainer | 1 | `1 failed, 559 passed, 1 skipped, 56166 deselected, 12 warnings in 990.71s (0:16:30)`; `frozen=yes` |
| Current target SHA and merge-tree preview | 0 | `release/0.8.1` at `b6945b49c0861d0292c4e8af1dfb56e0b11fe9f0`; candidate merge tree `6864b2353e876a4275660fa6904d9def9a26d7c8` |

The full gate is **not green**. JUnit identifies only the two known planner
translation failures:

- `tests/integration/web/composer/test_freeform_planner_failure_translation.py::test_send_message_freeform_planner_failure_is_translated[malformed]`
- `tests/integration/web/composer/test_freeform_planner_failure_translation.py::test_recompose_freeform_planner_failure_is_translated`

Both were freshly reproduced serially at immutable baseline
`e4ad02c7264358a08fd1ba2cf704b55bc47157c7` and the production candidate. The base
run exited **1**, **2 failed in 10.24s**; the candidate exited **1**, **2 failed in
9.61s**. All four executions failed the same stored-row canary assertion in
`_assert_no_sentinel_leak` at line 320. The response-body absence assertion
completed first. This attributes the two failures to behavior also present on the
base; it does not establish a new root cause. Logs, exits and source/import
provenance are recorded in `known-reds-report.md` and its companion artifacts.

The fresh lint comparison also uses `e4ad02c72` as its immutable baseline. Exact
`(path, rule_id, message)` multiset comparison, including multiplicities, found:

```text
all findings: before=2286 after=2286 added=0 removed=0
gating findings: before=606 after=606 added=0 removed=0
note findings: before=1680 after=1680 added=0 removed=0
controls=12 passed
comparator_exit=0
```

The lint scanner exited **1** on both trees. All positive, negative,
line/column-relocation, multiplicity and synthetic-addition controls passed.
There are no added or removed findings to classify for this follow-up; the
standing corpus and signature gate remain unresolved. The existing nonfatal
boot-probe catch has the same diagnostic identity despite moving lines. The
complete comparison and provenance are in `lint-comparison-report.md` and
`lint-comparison.json`. No signing metadata was changed by these lanes.

PostgreSQL evidence:
`/tmp/advisor-review-fixes-gates/20260923T011844Z-advisor-structured-output-1874848/summary.txt`.
The single failure was
`tests/testcontainer/web/test_aws_ecs_readiness_postgres.py::test_ready_returns_200_for_current_postgres`:
the endpoint returned 503 instead of 200. Captured warnings identify
`session_db`, `session_schema`, `landscape_db`, and `landscape_schema` with
`probe timed out`. The readiness runner has an unchanged two-second probe
deadline, and this test sets `composer_boot_probe_enabled=False`.
The readiness implementation, test module and PostgreSQL conftest have no diff
from `e4ad02c72`. The exact failing test passed serially on both trees: candidate
exit 0, **1 passed in 7.46s**; base exit 0, **1 passed in 7.69s**. The complete
four-test readiness module also passed on each: candidate exit 0, **4 passed in
16.92s**; base exit 0, **4 passed in 17.41s**. That module includes a deliberately
held probe, bounded failed refreshes, and recovery after releasing the probe.
Logs and exits are `pg-readiness-{base,candidate}-docker.*` and
`pg-readiness-{base,candidate}-module.*` in the follow-up lane. An initial
sandbox Docker-permission setup error is separately recorded and is not test
evidence; the reported reruns had Docker access.

The observed failure is a non-reproduced readiness probe timeout. Its specific
cause is unproven; these checks do not establish host load as the cause. The
completed PostgreSQL gate remains failed. Focused passes do not replace the
full-gate result. No production code changed after either gate, and the full
PostgreSQL suite was not repeated merely to obtain a green run.

The merge-tree preview is conflict detection only: no merge, push or signing was
performed. Deployment, service restart and live-provider acceptance were not
performed. Earlier report gates describe earlier candidates, not this follow-up
production tree.

## Follow-up test-function inventory

This supplements the report's **first-pass inventory** with the measured
`e4ad02c72` → `1d5be5e9f` changes. AST comparison found **5 touched test modules,
12 added, 1 deleted and 6 modified test functions**. It compares full structural
definitions, including bodies, signatures and decorators, without source-position
attributes. Relocation/comments do not create changes; parameterization does.
Helpers, fixtures and functions outside `tests/` are excluded. These are function
counts, not collected parameterized-case counts. Positive mutation/addition/
deletion/decorator controls and negative relocation/helper controls passed.

```text
tests/integration/web/composer/test_composer_against_gateway.py
Modified:
  test_boot_probe_rejects_incompatible_reasoning_model_sampling

tests/unit/web/composer/test_advisor_output.py
Added:
  test_bare_host_rule_preserves_option_paths_files_versions_and_plain_hosts
  test_markdown_non_url_destination_with_whitespace_is_unchanged
  test_markdown_redaction_preserves_non_url_parentheses_and_finds_adjacent_links
  test_markdown_url_redaction_preserves_balanced_wrapper
  test_markdown_url_redaction_preserves_wrapper_after_opening_whitespace
  test_note_preserves_prose_resembling_a_fence_sentinel
  test_note_redacts_bare_dotted_host_with_path
  test_note_removes_sentinels_reassembled_by_format_filter
Modified:
  test_response_format_uses_the_required_strict_owned_schema

tests/unit/web/composer/test_advisor_structured_checkpoint.py
Added:
  test_retry_wording_distinguishes_schema_from_contract_violation
Modified:
  test_every_invalid_contract_uses_format_retry_then_malformed

tests/unit/web/composer/test_boot_probe.py
Added:
  test_advisor_probe_accepts_schema_valid_without_requiring_checkpoint_semantics
  test_probe_rejection_reports_only_sent_option_presence
Deleted:
  test_advisor_probe_names_structured_capability_on_provider_rejection
Modified:
  test_advisor_probe_rejects_nonconforming_content
  test_advisor_probe_uses_production_request_options

tests/unit/web/test_app.py
Added:
  TestLifespanShutdown.test_lifespan_uses_role_specific_composer_probe_timeouts
Modified:
  TestLifespanShutdown.test_lifespan_records_transient_failure_when_composer_probe_times_out
```

## Initial implementation gates (historical)

| Item | Evidence |
| --- | --- |
| Branch | `fix/advisor-structured-output` |
| Starting commit | `4afd73169719954aeac8661629c5b58caf67ff14` |
| Implementation commit | `45707f7cab6fcc765ddeb53445652cfc9833858d` |
| Compatibility-removal commit | `1d99a03a1` |
| Production candidate measured by the full gate | `3f5b8bbb958d9fd4f8bffa11af6b0a4c258b6b7a` |
| Subsequent Bedrock test-only correction | `e4ad02c72` |
| Initial documentation commit | `3989303630d35e96c0b01cf9ccffe024fbf531cb` |
| Target branch and preview base | `release/0.8.1` at `40d5da05b2c8a0793d1b09df58cb4a7dea548cf5` |
| Validated candidate merge-tree preview | Exit `0` for `e4ad02c7264358a08fd1ba2cf704b55bc47157c7`; tree `3d4b359abcfe5acfeb76175be0794c69eaffbd50`; `merge-preview-final-code.log` and `.exit` in lane evidence. This is conflict detection, not a merge or integrated test run. |
| Full-gate `summary.txt` | `/tmp/advisor-structured-output-gates/20260922T235210Z-advisor-structured-output-1297943/summary.txt` |
| Frozen tree | `frozen=yes` at `3f5b8bbb9`; the only untracked path was this report draft |
| Ruff / mypy / contracts / lints / pytest exits | `0 / 0 / 0 / 1 / 1`; pytest reported `3 failed, 55883 passed, 100 skipped, 2 xfailed, 101 warnings in 1190.55s (0:19:50)` |
| PostgreSQL testcontainer exit and summary | Exit `0`, `frozen=yes` at `e4ad02c7264358a08fd1ba2cf704b55bc47157c7`; `560 passed, 1 skipped, 56129 deselected, 12 warnings in 926.16s (0:15:26)`; `/tmp/advisor-structured-output-gates/20260923T002050Z-advisor-structured-output-1501223/summary.txt` |
| All-lint corpus before / after / added / removed | `2278 / 2286 / 8 / 0`; all additions are tier-model binding churn, detailed below |
| Reproduced base planner-translation failures | Base and candidate reproduce the two cases named below; candidate serial exit 1, same `_assert_no_sentinel_leak` assertion |
| Merge / remote publication / signing | Not merged, not pushed, no signing metadata modified |
| Live-provider and deployed acceptance | Not performed by this implementation task |

The required full gate names `ruff,mypy,contracts,lints,pytest` explicitly;
PostgreSQL validation runs separately with `-m testcontainer -n 0`. Scoped
results below do not replace either gate. The standing trust-tier signing state
must be reported independently from newly introduced lint findings.

The initial full gate is **not green**. Its two planner-translation failures match the
measured base. The third failure was the Bedrock test's mock observing the private
wrapper callback as if it were a provider argument. Commit `e4ad02c72` moves that
test to the actual SDK boundary and uses structured checkpoint output. The
affected selection passed 54 tests. A deliberate callback-leak mutation failed
the exact request-key assertion, and the restored test passed. This correction
changes only a test; production files are identical to the full-gate candidate.
The full Python suite was not repeated for that bounded test-only repair.

The first gate attempt at
`/tmp/advisor-structured-output-gates/20260922T234952Z-advisor-structured-output-1284884`
was stopped before pytest after full mypy found the automatic-title call-site
typing error. It is not completed-gate evidence; the later run above replaces it.

### Lint comparison

`lints-final-diff.txt` in the lane evidence compares complete base and candidate
outputs as multisets of `(path, rule, message)`, ignoring line/column relocation.
Its known-positive, known-negative, relocation and synthetic-addition controls
all passed. Raw comparison summary:

```text
before=2278 after=2286 added=8 removed=0
```

The additions are four stale entries in `config/cicd/enforce_tier_model/web.yaml`
and their four exposed sites: two existing `except Exception` telemetry handlers,
the existing nonfatal boot-probe transport handler, and the existing final
provider-failure classification in `_run_advisor_checkpoint`. Each construct was
checked against the base; its behavior is unchanged. Function changes invalidate
their bindings. The other lint findings remain in the base corpus, including
non-tier findings; these totals include diagnostic notes. No allowlist entries or
signatures were edited. Operator signing remains a separate package-level step.

## A–D changes

| Plan item | Files and resulting behavior |
| --- | --- |
| A: strict response admission | [advisor_output.py](../../src/elspeth/web/composer/advisor_output.py) owns the frozen, strict model and its required `verdict`, `category`, `steps`, `findings`, `note` fields. Extra properties, duplicate keys, non-finite constants and invalid Unicode scalar strings are rejected. Schema admission remains distinguishable from semantic acceptance: CLEAN cannot carry notes/steps, and FLAGGED requires nonblank findings. |
| A: requests and boot | [advisor_request.py](../../src/elspeth/web/composer/advisor_request.py) builds shared runtime/probe options. Checkpoints receive strict `response_format`; hints do not. [boot_probe.py](../../src/elspeth/web/composer/boot_probe.py) sends a neutral prose-seeking prompt and checks strict JSON and field-schema admission, using the configured completion budget. It does not require checkpoint semantic acceptance. [app.py](../../src/elspeth/web/app.py) supplies explicit role, endpoint and reasoning settings even when model IDs match; the approved advisor timeout is 60 seconds and the planner remains at 5 seconds. Planner request bytes remain unchanged. |
| B: prompt contract | [service.py](../../src/elspeth/web/composer/service.py) places checkpoint output rules in `_advisor_system_instructions_for_trigger`; the user-message summary carries the situation, preserving existing untrusted fences. Fixed re-prompts distinguish invalid schema from valid schema that violates checkpoint rules. The old prose scan and verdict-line stripping are removed. |
| C: user-note sanitization | `advisor_output.py` normalizes line separators, removes ANSI/control/format characters, removes fence sentinels, redacts links and email addresses, collapses blank runs, trims and finally caps the note. Path-bearing hosts are redacted and markdown URL wrappers stay balanced as described in the follow-up above. Empty sanitized notes become null. `service.py` publishes this sanitized note while preserving distinct technical findings for fenced repair input. URL/email substitution counts are measured before truncation. |
| D: conformance | `service.py` tracks physical SDK dispatch after quota admission, the first attempt's schema/semantic result, whether a format retry actually started, and final accepted-response counts. [advisor_audit.py](../../src/elspeth/web/composer/advisor_audit.py) requires exact types and cross-field invariants before serializing the extended pass. [advisor_checkpoint_telemetry.py](../../src/elspeth/web/composer/advisor_checkpoint_telemetry.py) mirrors it only after persistence; unbounded counts stay out of metric attributes. |

Review found and repaired two boundary defects: escaped unpaired surrogates could
reach canonical hashing, and underscore-emphasized URLs could evade replacement.
Paired non-BMP text and ordinary dotted prose remain positive controls. Review also
moved attempt accounting to the physical dispatch boundary: quota failure before
SDK dispatch truthfully records zero attempts and no response conformance.

Full mypy also required the existing automatic-title caller in
`src/elspeth/web/sessions/_auto_title.py` to pass the private dispatch callback
explicitly as null. Title requests retain their existing provider options. This
call-site typing correction passed all 81 focused title/admission tests and full
mypy over 1,005 source files before restarting the full gate.

The existing live-step validator runs before pass persistence for early and END
passes and still runs again before terminal publication. Accepted response counts
exclude rejected attempts. A deadline before the initial checkpoint completion
creates no invented completed-pass row.

## Provider routing and enforcement

Research used installed LiteLLM **1.102.0**, official provider documentation and
local SDK parameter shaping. It did not exercise live cloud credentials or prove
that every deployment honors every schema.

The public [OpenRouter model catalogue](https://openrouter.ai/api/v1/models) was
queried again during implementation: `z-ai/glm-5.3` advertised
`structured_outputs`, `response_format`, `reasoning` and `include_reasoning`.
This establishes the advertised capability, not live acceptance for a deployment.

- **OpenRouter:** strict `response_format` is supported for compatible models.
  Default routing can select providers that ignore unsupported parameters;
  checkpoint requests therefore set `provider.require_parameters=true`. The
  builder recognizes both the `openrouter/` model prefix and the exact
  `openrouter.ai` custom-endpoint hostname. Arbitrary private gateway hostnames
  cannot establish upstream identity. The installed adapter rebuilds caller
  `extra_body`, so the routing object is supplied at top level; tests prove it
  survives schema and reasoning shaping. See [structured outputs](https://openrouter.ai/docs/guides/features/structured-outputs)
  and [provider routing](https://openrouter.ai/docs/guides/routing/provider-selection).
- **Reasoning:** existing `apply_reasoning_kwargs` remains authoritative.
  OpenRouter uses its native reasoning object; Azure/Bedrock use the existing
  reasoning-effort mapping. Bare/OpenAI-prefixed gateway aliases retain the
  existing unhinted compatibility policy. The schema does not request reasoning
  text. See [OpenRouter reasoning fields](https://openrouter.ai/docs/guides/best-practices/reasoning-tokens).
- **Azure:** compatible deployments support native Chat Completions
  `response_format`. Installed LiteLLM can translate unsupported model/API
  combinations into a synthetic function/tool request. That translation is
  distinct from provider-native constrained decoding; local admission remains
  mandatory. See [Azure structured outputs](https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/structured-outputs).
- **Bedrock:** native Converse structured output uses `outputConfig.textFormat`.
  Installed LiteLLM selects this path when its registry reports native support;
  otherwise it injects a synthetic JSON tool. Forced tool selection is omitted
  while thinking is enabled, so older registry entries may depend on voluntary
  tool use. The boot probe observes the returned content rather than assuming
  enforcement. See [Bedrock structured outputs](https://docs.aws.amazon.com/bedrock/latest/userguide/structured-output.html).

The advisor probe fails boot on provider bad requests and on invalid response
shape, JSON or field schema. Checkpoint semantic acceptance remains a runtime
requirement, separate from this configuration check. Advisor transport timeouts
remain nonfatal and explicitly report that structured-output conformance was not
verified this boot. A passing probe establishes that this neutral prose-seeking
prompt produced a response admitted by strict local JSON and field validation.
It exposes an ignored schema when the reply is prose; it cannot prove native
schema enforcement on that request or across future requests.

## Current audit contract and audit primacy

The producer requires all nine conformance fields. It has no legacy constructor,
missing-field defaults, prose parser fallback, historical-row upgrade or backfill.
The old import-location reexports for advisor constants were removed; consumers
import directly from `advisor_output.py`. The generic session store remains an
opaque audit transport, not a versioned checkpoint decoder. No session column or
schema reader changed, and no database reset was performed.

An independent read-only review of the production diff returned GO for the
no-backward-compatibility requirement. Optional arguments and transient verdict
state still describe current hint, prescan and provider-failure paths; they do not
admit missing fields from old checkpoint formats.

The test
`TestRowsInARealSessionStore.test_checkpoint_row_is_exact_and_excluded_from_model_history`
in [test_advisor_audit.py](../../tests/unit/web/composer/test_advisor_audit.py)
creates a real fenced SQLite session and persists an assistant-message control
and the current envelope through `persist_advisor_checkpoint_pass`.
It reads them through
[SessionService.get_messages](../../src/elspeth/web/sessions/service.py).
Assertions require the content and envelope to exactly equal
`advisor_checkpoint_pass_audit_envelope(record)`. The base-format fixture and its
compatibility guarantee were removed at the operator's request.

The actual history functions in
[sessions/routes/_helpers.py](../../src/elspeth/web/sessions/routes/_helpers.py)
are exercised: `_composer_chat_history`, `_composer_conversation_messages` and
`_composer_conversation_or_llm_audit_messages` all exclude the checkpoint row
while retaining the assistant control. Model-history projection continues through
`replay_composer_control_message` and the existing audit-role filter. Missing
conformance fields fail owned-record construction.

`TestAuditPrimacy.test_checkpoint_pass_row_commits_before_its_event` and
`test_audit_write_failure_propagates_and_no_event_fires` retain the ordering proof.
The telemetry test also preserves exact large counts in the event while limiting
metric dimensions to the existing phase/verdict/source vocabulary. The SQLite
proof does not replace the required PostgreSQL gate.

## Scoped verification evidence

These are completed lane results, reproduced here with their raw pytest summaries.
They cover different selections and must not be added into a whole-suite count.
Lane artifacts are under `.claude/lanes/advisor-structured-output/` unless a
`/tmp/` path is shown.

| Selection | Exit | Recorded summary / log |
| --- | --- | --- |
| Final strict boundary and sanitizer | 0 | `84 passed in 0.14s`; `boundary-split-green.log` |
| Audit producer and dispatch invariants before compatibility removal | 0 | `103 passed in 1.91s`; `audit-dispatch-green.log` |
| Migrated checkpoint and CLEAN tables | 0 | `276 passed in 11.61s`; `checkpoint-tests-dispatch-green.log` |
| Request options, boot and affected app cases | 0 | `43 passed, 225 deselected in 7.22s`; `/tmp/advisor-boot-focused-green.log` |
| Local gateway planner probes | 0 | `3 passed, 6 deselected`; `/tmp/advisor-gateway-probe-unsandboxed.log` |
| Remaining service fixture repairs | 0 | `3 passed, 253 deselected`; `/tmp/advisor-remaining-fixtures-after.log` |
| Final audit, boot and structured-checkpoint hook repair | 0 | `171 passed in 2.55s`; `hook-fix-tests-green.log` and `.exit` |
| Audit and checkpoint selection after compatibility removal | 0 | `371 passed in 13.81s`; `no-compat-tests.log` and `.exit` |
| Automatic title and chargeable admission after shared-wrapper typing repair | 0 | `81 passed, 1 warning in 2.21s`; `auto-title-callback-tests.log` and `.exit` |
| Bedrock SDK boundary, provider quota and structured checkpoints | 0 | `54 passed in 4.11s`; `bedrock-sdk-green.log` and `.exit` |
| Mock discipline and masquerade gates after the Bedrock test correction | 0 | `247 passed in 204.40s (0:03:24)`; `bedrock-test-gates.log` and `.exit` |

The two known base failures also failed serially on the candidate:

- `tests/integration/web/composer/test_freeform_planner_failure_translation.py::test_send_message_freeform_planner_failure_is_translated[malformed]`
- `tests/integration/web/composer/test_freeform_planner_failure_translation.py::test_recompose_freeform_planner_failure_is_translated`

`candidate-known-failures.log` records `2 failed in 8.86s`, with exit 1 in its
companion `.exit`; the coordinator compared the same `_assert_no_sentinel_leak`
assertion against `baseline-tests.log`. These failures remain disclosed when
assessing the final suite.

Behavior-first RED evidence includes structured findings parsed as the entire JSON
object (`boundary-red.log`), absent audit serialization keys and missing-field
acceptance (`audit-red.log`), retired prose accepted/JSON rejected
(`checkpoint-tests-red.log`), and missing explicit role in the app's outbound probe
(`advisor-app-red.log` in `/tmp/`). Dispatch and sanitizer review fixes have their
own failing controls in the lane reports.

The initial all-app run was interrupted with exit 130 and is not pass evidence.
The gateway's initial sandbox run failed local-listener setup; the approved rerun
permitted only the test's local mock-server execution and passed. No live provider
acceptance is inferred from those tests.

## Operator acceptance steps — not executed

After a separately authorized deployment, use the configured advisor and ordinary
Composer path for both checks; do not introduce tutorial-specific behavior.

1. Verify boot telemetry `composer.boot_config` records `probed_role=advisor`,
   `structured_output=true` and `probe_status=success` for the configured model.
   Preserve the deployment/candidate identity with the result. A transient probe
   failure does not satisfy this check. In particular,
   `structured_output_conformance_verified=False` leaves acceptance unverified.
   Probe success establishes field-schema admission, separate from runtime
   checkpoint semantic acceptance.
2. Exercise one known blocked turn against the deployed advisor. Confirm the
   user sees the category header and plain sanitized note, and technical findings
   remain confined to the repair/audit path. Inspect its durable checkpoint pass:
   physical attempts, first-attempt schema/semantic facts, retry flag, live-step
   counts, input-note presence and actual URL/email replacement counts must match
   the observed path. Confirm the corresponding telemetry follows persistence.
3. Exercise one clean turn against the same configured advisor. Confirm accepted
   CLEAN has no note or step IDs, zero applicable final counts, and first-attempt
   facts matching the actual response; record whether any retry was needed.
4. Record both session/pass references and the boot telemetry result. Keep model
   output, row data and credentials out of this report. Treat a provider change,
   gateway that ignores schema, or unexpected response as an acceptance failure
   to investigate; do not infer success from a prior local test.

The plan asked for a positive boot log line. The
[logging-telemetry policy](../../.agents/skills/logging-telemetry-policy/SKILL.md)
forbids infrastructure startup/config logging, so the implementation extends the
existing counter and latency telemetry with role/capability attributes instead.
No new successful-startup log is introduced.

## Initial implementation test-name inventory (historical)

The following first-pass names come from the coordinator's AST comparison against
the starting HEAD and its explicit migration inventory through `e4ad02c72`.
The follow-up inventory above records subsequent additions, removals and changes.
Parameterized cases retain their function name here; this is a change inventory,
not a collected-test count.

### Added and deleted functions

```text
tests/integration/web/composer/test_composer_against_gateway.py
Deleted:

Added:


tests/unit/web/composer/test_advisor_audit.py
Deleted:

Added:
test_checkpoint_row_is_exact_and_excluded_from_model_history
test_checkpoint_boolean_fields_require_exact_bools
test_checkpoint_counts_require_nonnegative_exact_ints
test_checkpoint_cross_field_invariants_reject_inconsistent_facts
test_checkpoint_event_mirrors_exact_counts_without_unbounded_metric_dimensions
test_checkpoint_pass_constructor_requires_every_conformance_field
test_checkpoint_pass_factory_rejects_missing_conformance
test_checkpoint_pass_serialization_requires_explicit_conformance
test_checkpoint_record_accepts_conformance_table
test_clean_record_requires_zero_counts_and_no_note
test_undispatched_model_failure_has_zero_attempts_and_no_conformance
test_undispatched_model_failure_rejects_invented_response_facts

tests/unit/web/composer/test_advisor_checkpoint.py
Deleted:
test_missing_machine_lines_fall_back_to_other_and_no_steps
test_note_collapses_the_blank_lines_the_machine_lines_leave
test_parse_advisor_verdict_anchored_lowercase_arm_survives_tightening
test_parse_advisor_verdict_clean_acceptance_stays_window_bounded
test_parse_advisor_verdict_flagged_below_window_beats_quoted_clean
test_parse_advisor_verdict_flagged_dominates_within_scan_window
test_parse_advisor_verdict_lowercase_flagged_with_terminator_blocks
test_parse_advisor_verdict_negation_cannot_mint_a_signoff
test_parse_advisor_verdict_still_declares_malformed
test_parse_advisor_verdict_tolerates_real_model_formatting
test_parse_advisor_verdict_unaccompanied_clean_reference_cannot_mint_signoff
test_the_verdict_token_never_survives_into_the_note
test_unicode_line_separators_stay_line_breaks_when_no_verdict_line_is_found
test_unknown_category_normalises_to_other
Added:
test_missing_required_category_is_malformed
test_note_collapses_blank_runs
test_unicode_line_separators_stay_line_breaks_in_structured_note
test_unknown_category_is_malformed

tests/unit/web/composer/test_advisor_clean_verdict_table.py
Deleted:
test_clean_verdict_terminator_table
Added:
test_clean_json_verdict_preserves_technical_findings
test_prose_verdict_cannot_clear_or_block_as_an_accepted_response

tests/unit/web/composer/test_boot_probe.py
Deleted:

Added:
test_advisor_probe_names_structured_capability_on_provider_rejection
test_advisor_probe_rejects_malformed_provider_response
test_advisor_probe_rejects_nonconforming_content
test_advisor_probe_timeout_remains_nonfatal
test_advisor_probe_uses_production_request_options

tests/unit/web/composer/test_service.py
Deleted:

Added:


tests/unit/web/test_app.py
Deleted:

Added:


tests/unit/web/composer/test_advisor_output.py
Deleted:

Added:
test_all_owned_categories_are_admitted
test_checkpoint_model_is_frozen
test_duplicate_fields_never_overwrite_a_verdict
test_each_schema_field_is_required
test_escaped_unpaired_surrogates_are_rejected
test_non_finite_json_values_are_rejected
test_non_object_or_malformed_reply_is_not_schema_valid
test_note_redactions_are_counted_before_final_cap
test_note_sanitization_order_and_negative_controls
test_paired_surrogate_escapes_are_admitted_as_unicode_scalars
test_redaction_counts_measure_substitutions_and_not_preexisting_sentinels
test_response_format_uses_the_required_strict_owned_schema
test_schema_rejects_wrong_types_values_and_extra_fields
test_semantic_failures_remain_distinguishable_from_schema_failures
test_valid_clean_structured_response_is_admitted
test_valid_flagged_structured_response_is_admitted

tests/unit/web/composer/test_advisor_request.py
Deleted:

Added:
test_checkpoint_options_require_schema_and_route_support
test_hint_options_preserve_prose_request
test_installed_litellm_preserves_schema_routing_and_reasoning

tests/unit/web/composer/test_advisor_structured_checkpoint.py
Deleted:

Added:
test_conformance_counts_only_physical_dispatch_after_quota_admission
test_deadline_before_format_retry_does_not_record_unsent_reprompt
test_empty_text_is_distinct_from_absent_text_at_real_call_boundary
test_every_invalid_contract_uses_format_retry_then_malformed
test_first_attempt_and_retry_conformance
test_initial_deadline_does_not_create_pass
test_prescan_conformance_is_not_applicable
test_rejected_attempt_does_not_contribute_final_counts
test_structured_reply_records_counts_before_any_terminal_publication
```

### Migrated existing functions

```text

tests/integration/web/composer/test_composer_against_gateway.py
Migrated: test_boot_probe_rejects_incompatible_reasoning_model_sampling
Migrated: test_boot_probe_succeeds_against_gateway
Migrated: test_boot_probe_with_operator_sampling_succeeds_against_gateway

tests/unit/web/composer/test_advisor_audit.py
Migrated: test_checkpoint_pass_rejects_values_outside_the_closed_vocabulary
Migrated: test_envelopes_carry_distinct_kinds_and_nothing_but_closed_fields
Migrated: test_publication_rejects_inconsistent_records

tests/unit/web/composer/test_advisor_checkpoint.py
Migrated: test_a_clean_subheading_never_replaces_the_finding_in_the_note
Migrated: test_advisor_checkpoint_telemetry_counter_uses_phase_verdict_and_source
Migrated: test_advisor_prompt_explains_withheld_values_are_present_and_not_defects
Migrated: test_advisor_recovery_real_prescan_accepts_reworded_message
Migrated: test_advisor_user_message_marks_schema_excerpt_as_untrusted
Migrated: test_build_advisor_user_message_fences_and_redacts_user_message
Migrated: test_build_advisor_user_message_neutralizes_begin_end_spoof_in_schema_excerpt
Migrated: test_build_advisor_user_message_neutralizes_begin_end_spoof_in_user_message
Migrated: test_build_advisor_user_message_neutralizes_embedded_end_sentinel_in_schema_excerpt
Migrated: test_build_advisor_user_message_neutralizes_embedded_end_sentinel_in_user_message
Migrated: test_checkpoint_deadline_cancels_provider_and_retains_timeout_audit
Migrated: test_checkpoint_deadline_preserves_malformed_attempt_before_retry_expiry
Migrated: test_checkpoint_deadline_preserves_unparseable_attempt_before_retry_expiry
Migrated: test_checkpoint_pass_telemetry_discriminates_prescan_from_model
Migrated: test_checkpoint_wire_uses_verdict_contract_not_stuck_hint_contract
Migrated: test_clean_and_unrendered_verdicts_carry_no_note
Migrated: test_end_advisor_prompt_scopes_bounded_and_withheld_evidence
Migrated: test_end_checkpoint_blocks_balanced_quoted_user_message_injection
Migrated: test_end_checkpoint_blocks_prompt_template_advisor_injection_before_provider
Migrated: test_end_checkpoint_blocks_single_family_clean_imperative_injection
Migrated: test_end_checkpoint_blocks_user_message_advisor_injection_before_provider
Migrated: test_end_checkpoint_problem_summary_carries_degeneracy_rubric
Migrated: test_end_gate_compose_deadline_keeps_the_completed_reply_recoverable
Migrated: test_end_gate_final_flag_never_exposes_advisor_findings_on_human_surfaces
Migrated: test_end_gate_flags_user_stated_schema_mode_mismatch
Migrated: test_end_gate_starts_no_advisor_attempt_after_compose_deadline
Migrated: test_end_prescan_state_option_verdict_stays_repair_actionable
Migrated: test_end_prescan_user_message_verdict_is_repair_unactionable
Migrated: test_flagged_verdict_parses_category_steps_and_note
Migrated: test_malformed_response_consumes_retry_with_format_reprompt
Migrated: test_manual_advisor_hint_wire_retains_stuck_hint_contract
Migrated: test_note_is_bounded_and_sanitised
Migrated: test_note_strips_unicode_format_characters
Migrated: test_run_advisor_checkpoint_clean_verdict
Migrated: test_run_advisor_checkpoint_early_ignores_user_message
Migrated: test_run_advisor_checkpoint_emits_one_bounded_pass_event
Migrated: test_run_advisor_checkpoint_emits_progress
Migrated: test_run_advisor_checkpoint_end_returns_verdict
Migrated: test_run_advisor_checkpoint_end_threads_user_message
Migrated: test_run_advisor_checkpoint_telemetry_failure_does_not_replace_completed_verdict

tests/unit/web/composer/test_advisor_clean_verdict_table.py

tests/unit/web/composer/test_boot_probe.py
Migrated: test_bedrock_probe_uses_default_aws_chain_without_overrides
Migrated: test_probe_fatal_on_seed_bad_request_without_phrase_matching
Migrated: test_probe_is_graceful_on_litellm_provider_error
Migrated: test_probe_is_graceful_on_transient
Migrated: test_probe_omits_endpoint_kwargs_when_unset
Migrated: test_probe_passes_through_on_success
Migrated: test_probe_propagates_programmer_errors
Migrated: test_probe_raises_boot_config_error_on_bad_request
Migrated: test_probe_sends_configured_endpoint

tests/unit/web/composer/test_service.py
Migrated: test_advisor_rereview_carries_prior_finding_mutation_and_current_evidence
Migrated: test_advisor_rereview_without_mutation_is_explicit_not_a_blind_replay
Migrated: test_flagged_repair_clean_replaces_every_public_and_persisted_echo_surface

tests/unit/web/test_app.py
Migrated: test_lifespan_emits_composer_boot_config_attributes
Migrated: test_lifespan_probes_each_role_against_its_own_endpoint

tests/unit/web/test_composer_bedrock.py
Migrated: test_bedrock_advisor_uses_default_chain_without_tools_or_gateway_overrides
```
