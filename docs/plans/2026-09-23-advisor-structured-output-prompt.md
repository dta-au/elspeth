# Implementation prompt — advisor checkpoint on structured output

Paste everything below the rule into a fresh agent session in this repository.

---

## Task

Move the Web Composer's **advisor checkpoint** (the early and END sign-off reviews) from a free-text
reply parsed with regexes to a **JSON-schema structured output**, and ship the three related changes
below in the same branch. All four are operator-approved (2026-09-23, follow-up to elspeth-032ec69c41):

- **A. Structured output.** The advisor returns JSON validated against a strict schema, not prose.
- **B. Output rules in the system message.** The advisor's output contract moves out of the user
  message into its system instructions.
- **C. URL and email stripping.** Remove URLs and email addresses from the user-facing note before any
  surface or row sees it.
- **D. Conformance recording.** Record, per checkpoint pass, how the advisor's reply conformed: schema
  validity, retries, step ids dropped, note present, redactions made.

Read `AGENTS.md` and `CONTRIBUTING.md § Whole-tree gates` before writing code. Load the
`logging-telemetry-policy` skill before item D, and use TDD throughout (a failing test first, and
confirm it fails for the right reason — an assertion on the behaviour, not an import error).

## Why

The advisor's verdict, `CATEGORY:`/`STEPS:` lines and user-facing note are all extracted from prose
by line scans and regexes (`_parse_advisor_checkpoint_guidance`, `_advisor_note_text`,
`_ADVISOR_NOTE_VERDICT_LEAD_RE`, `_ADVISOR_CATEGORY_LINE_RE`, `_ADVISOR_STEPS_LINE_RE`). A single
review found three extraction bugs in that code: a `**Clean:**` sub-heading replaced the actual
finding in a blocked pipeline's note; U+2028/U+2029 were deleted and glued words together; the verdict
token survived for mid-line and en-dash shapes. Each was patched, but they are one defect class, and
every new reply shape the model invents can open another. A schema replaces the prose grammar with an
explicit contract, enforced locally on every response. Native constrained decoding can improve
conformance, but support varies by provider and endpoint; parameter acceptance or one valid response
does not prove universal enforcement. The note is now shown to users, so extraction errors are
user-visible.

Measured 2026-09-23 from the OpenRouter model catalogue: the deployed advisor (`z-ai/glm-5.3`) lists
`structured_outputs`, `response_format`, `reasoning` and `include_reasoning` in its supported
parameters, as does the deployed planner. Re-measure before relying on it.

## Starting point

- Create a worktree under `.claude/worktrees/` on a new branch cut from
  **`fix/advisor-review-followups-2`** (`4afd73169`), not from `release/0.8.1`. That branch carries
  unmerged fixes to the same functions (retry copy that follows the block's cause, the note's CLEAN
  lead). Branching from it avoids conflicts; this work supersedes its note-extraction changes.
- Symlink `.venv` by hand. Put both source roots on `PYTHONPATH` and verify `elspeth.__file__` and
  `elspeth_lints.__file__` resolve inside the worktree.
- **Do not merge, push or fast-forward any integration branch.** Hand back a branch.
- `release/0.8.1` moves often and other sessions work on this code. Re-check overlap
  (`git log`/`git diff --name-only` against the branch tip) before every commit, and compare with a
  `git merge-tree --write-tree` preview before handing back.

## What exists today (verify each against the tree — line numbers drift)

All in `src/elspeth/web/composer/service.py` unless noted.

- **The call:** `_call_advisor_with_audit` builds `kwargs` (`model`, `messages`, `max_tokens`, optional
  temperature/seed), then `apply_reasoning_kwargs` and `_apply_endpoint_kwargs` (the advisor can use a
  custom endpoint: `composer_advisor_endpoint_base_url` / `_api_key`), then `litellm.acompletion`.
  The advisor text is `response.choices[0].message.content`; empty or non-string content is already a
  malformed response.
- **Two callers.** `_run_advisor_checkpoint` (early and END phases) parses the reply into an
  `AdvisorCheckpointVerdict`. The LLM-initiated `request_advisor_hint` tool also calls
  `_call_advisor_with_audit` and must **stay prose** — apply the schema to checkpoint calls only (for
  example a keyword argument on `_call_advisor_with_audit`), never globally.
- **Retry:** `_run_advisor_checkpoint` makes 2 attempts. A transport-successful reply with no usable
  verdict consumes a retry with `_advisor_arguments_with_format_reprompt`; twice in a row becomes
  `failure_class="malformed"`. Schema-invalid output must route through this same path — no new
  failure class, no new user wording.
- **Prompt:** `_build_checkpoint_arguments` puts the output contract ("Start your reply with CLEAN or
  FLAGGED…", the CATEGORY/STEPS lines, "Your FLAGGED prose is shown to the user as your note…") inside
  `problem_summary`, which `_build_advisor_user_message` renders into the **user** message.
  `_advisor_system_instructions_for_trigger` already selects checkpoint system instructions that
  require a leading CLEAN/FLAGGED verdict. Replace those instructions as well as removing the user
  message's contract; do not append contradictory JSON instructions beside the old prose contract.
  Untrusted sections of the user message are fenced with sentinels and neutralised — keep that.
- **Consumers of the verdict:** `verdict.findings_text` feeds the composer's repair injection
  (`_fence_advisor_findings`, and a truncated copy for the next pass's arguments) and the blocker
  wording helpers; `verdict.note`, `.category`, `.affected_step_ids` feed `_advisor_blocked_result`,
  where `_validated_advisor_step_ids` keeps only ids present in the live state and
  `_advisor_flagged_header` picks one backend-authored sentence per category.
- **Deterministic pre-scan:** a backend-authored FLAGGED (`findings_backend_authored=True`) never calls
  the model. Preserve that behavior and record its conformance as not applicable, as defined in D.
- **Boot probe:** `probe_composer_config` in `web/composer/boot_probe.py`, driven from
  `web/app.py`, probes the planner and advisor, each against its own endpoint. A LiteLLM
  `BadRequestError` fails boot (`ComposerBootConfigError`); a timeout is non-fatal.
- **Audit and telemetry:** `persist_advisor_checkpoint_pass` (`advisor_audit.py`) writes an
  `advisor_checkpoint_pass_audit` JSON envelope; `record_advisor_checkpoint_pass`
  (`advisor_checkpoint_telemetry.py`) mirrors it only after the row is durable, and carries no text.
  At the specified base, this envelope has owned construction/serialization but no dedicated replay
  decoder. Generic chat/history consumers exclude audit-role rows without decoding their payload.
- **Redaction:** `_redact_sensitive_content` (`web/validation.py`) substitutes credential- and
  PII-shaped substrings with fixed sentinels for egress surfaces.

## A. Structured output

1. **Schema.** Define it once, from an owned pydantic model with `extra="forbid", strict=True`:
   - `verdict`: `"CLEAN"` | `"FLAGGED"`
   - `category`: the five values of `ADVISOR_FINDING_CATEGORIES`
   - `steps`: array of strings
   - `findings`: string — the technical explanation the composer's repair loop needs
   - `note`: string or null — the explanation written for the end user

   Two text fields is deliberate: today one prose reply serves both the repair loop and the user, and
   the prompt now tells the advisor to write for the user, which works against repair precision. Keep
   them separate. Use strict mode: every property required (nullable where absent is legitimate),
   `additionalProperties: false`.
2. **Boundary parse.** The reply is Tier 3 (ADR-032): reject duplicate object keys and non-finite JSON
   constants before validating with the pydantic model, then construct the owned
   `AdvisorCheckpointVerdict`. Reuse `parse_json_strict` from
   `plugins/infrastructure/clients/json_utils.py` if the dependency direction permits, or a shared
   equivalent with the same rejection behavior. Plain `json.loads` and a Pydantic JSON shortcut that
   silently accepts duplicate keys are not sufficient: a later CLEAN must never overwrite FLAGGED.
   No `.get` chains, no defaults filled from missing keys. A decode or validation error is "no usable
   verdict" and takes the existing format-retry path. Runtime and boot probe share this boundary.
3. **Invariants — fail closed.** Treat as unparseable (retry, then malformed): `verdict="CLEAN"` with a
   non-null `note` or non-empty `steps`; `verdict="FLAGGED"` with empty or whitespace-only `findings`.
   The old parser's "any FLAGGED anywhere blocks" rule protected against ambiguous prose; the enum
   replaces it, and duplicate-key rejection and inconsistency checks preserve the fail-closed
   direction. Say so in a comment. Apply the same semantic checks in the boot probe.
4. **Retire the prose parser completely** — no fallback path (operator ruling 2026-09-14: upgrade
   without old pathways). Delete `_parse_advisor_checkpoint_guidance`'s prose scanning, the verdict-line,
   emphasis, category, steps and verdict-lead regexes, and the verdict-lead stripping in
   `_advisor_note_text`. Preserve line-separator normalization (`splitlines()` joined with LF) before
   the Unicode category filter: JSON strings still admit U+2028/U+2029, which must not be deleted and
   join adjacent words. Keep ANSI stripping before control filtering, fence-sentinel removal,
   blank-run collapse, and the final `ADVISOR_NOTE_MAX_CHARS` cap, in the order specified in C.
   Keep `_validated_advisor_step_ids` and the category-to-sentence header exactly as they are.
5. **Request.** Pass the schema through LiteLLM's `response_format` (`type: json_schema`, strict) on
   checkpoint calls only. Keep `apply_reasoning_kwargs`: reasoning arrives in a separate field on the
   deployed provider, and the content must be the JSON object alone. Do not add chain-of-thought or
   worked examples to the prompt — the model reasons internally, and the schema, not examples, sets the
   output shape.
6. **Provider routing.** The advisor model is an operator-set LiteLLM model string with an optional
   custom endpoint, so it can reach OpenRouter, Azure OpenAI, Bedrock or a gateway. Check, against the
   installed LiteLLM version and each provider's current docs, how `response_format` json_schema is
   honoured on each. On OpenRouter, confirm whether provider routing can silently drop the parameter
   and, if so, set the routing option that requires providers to support every requested parameter
   (`provider.require_parameters` at the time of writing — verify the name). Distinguish native
   schema enforcement from SDK translation to tool use or prompt guidance, including behavior with
   reasoning enabled. Record what you found; retain local validation for every supported route.
7. **Boot probe.** Extend `probe_composer_config` with explicit role identity; never infer the advisor
   role from model equality because both roles can share a model. For the advisor, send the production
   schema and use the same strict parser, semantic checks, endpoint, routing requirements, sampling
   and reasoning options as checkpoint calls. Share request-option construction so probe/runtime
   settings cannot drift. Use a short fixed prompt requesting a valid CLEAN object with all required
   fields, `category="other"`, `steps=[]`, `findings=""`, and `note=null`. Replace the advisor's
   current `reply with ok` prompt and `max_tokens=16` allowance; use the configured advisor completion
   budget so there is room for the JSON and configured reasoning. Keep a bounded request timeout.

   A `BadRequestError` and a response failing JSON/schema/semantic admission both fail boot with a
   message naming the advisor model and structured-output capability. Preserve non-fatal transport
   timeout behavior. A passing probe records observed conformance, not proof that a gateway can never
   ignore the schema. Test rejection, ignored schema returning prose, duplicate keys, truncation,
   reasoning-only replies, and valid output; assert the actual outbound prompt/options as well as the
   result. Cover identical planner/advisor model ids with different endpoints. The planner probe's
   prompt, budget and request options remain unchanged.

## B. Output rules in the system message

Consolidate the output contract in `_advisor_system_instructions_for_trigger` for checkpoint triggers,
replacing its existing leading-verdict instructions:
what CLEAN and FLAGGED mean (keep the existing wording that CLEAN is not certification of withheld or
truncated evidence), the category vocabulary, that `steps` names step ids from the pipeline excerpt,
and the two audiences (`findings` for the composer's repair, `note` for the user, written plainly,
naming the step and option, never quoting user text or row data). Keep it short. `problem_summary`
keeps only the situation for this pass. Update `_advisor_arguments_with_format_reprompt` so the
re-prompt refers to the schema rather than to a leading word. The untrusted fences stay in the user
message; nothing untrusted moves into the system message. `_build_advisor_user_message` is shared with
prompt-size accounting — keep the two in step.

## C. Strip URLs and emails from the note

After schema validation and before the note reaches `AdvisorCheckpointVerdict`, replace URLs
(`scheme://…`, `www.…`) and email addresses in `note` with fixed sentinels (for example
`[link removed]`, `[address removed]`). This is server-side redaction of model output, which the
composer invariants permit. Scope it to `note` — `findings` goes to the composer inside the untrusted
fence, not to a person. Check whether `_redact_sensitive_content` already covers either shape; reuse it
if it does, but do not widen it, because other egress surfaces depend on its current behaviour. Count
the substitutions for item D.

Apply these operations in order: normalize line separators to LF; strip ANSI sequences and fence
sentinels; filter control/format characters; replace URLs and emails; collapse blank runs and trim;
then apply the final note cap. Match links after removable format characters have been removed so a
zero-width character cannot conceal `www.` until after redaction. Redact before truncation so the cap
cannot destroy a match. Preserve an empty sanitized note as `None`.

Control the patterns: a known URL and email, including a URL containing a removable format character,
must be replaced; ordinary prose containing dots, colons and `@`-free text (`node.option`, `rate: 0.5`)
must survive untouched. Use distinct findings/note canaries to prove sanitization happens before the
persisted blocker and DTO receive the note, while technical findings retain the existing repair fence.

## D. Conformance recording

**Per-pass meanings.** Add required fields to the owned pass record; no advisor text enters them:

- `provider_attempts`: number of provider calls actually started during this pass, from 0 to 2.
- `first_attempt_schema_valid`: whether the first provider attempt returned text passing strict JSON
  and field-schema validation. `null` means no text response was received or no call was made;
  malformed text (including empty text) is `false`. This describes the first attempt, even when a
  later attempt supplies the first received reply.
- `first_attempt_accepted`: whether that same text also passed A.3's semantic checks. It is `null`
  exactly when `first_attempt_schema_valid` is `null`; schema-invalid text is `false` here too.
- `format_reprompt_sent`: true only when a provider call using the format re-prompt actually starts.
  Preparing arguments and then expiring the deadline does not count.
- `step_ids_offered`, `step_ids_kept`, `note_present`, `url_redactions`, `email_redactions`: values for
  the accepted model response that produced this pass's verdict, not totals across rejected attempts.
  All five are `null` when there is no accepted model response, including prescan. For an accepted
  response, count offered entries before deduplication and kept entries after live-ID validation;
  `note_present` means the input note was non-null before sanitization; redaction counts are actual
  substitutions before the final cap. An accepted CLEAN has zero step/redaction counts and false note
  presence. A note sanitized to nothing can have `note_present=true` and a final published note of null.

Compute step counts with the existing `_validated_advisor_step_ids` helper against the checkpoint's
supplied state **before** `completed()` writes the pass. Its current terminal-only call occurs too
late and misses early and repairable END passes. Preserve terminal revalidation against live state.
Validate exact types and cross-field invariants in the owned record before persistence; never fill
missing conformance fields with defaults.

Pin the following cases in tests (final counts are applicable only for an accepted model response):

| Path | Attempts | First schema / accepted | Re-prompt sent | Final counts |
| --- | --- | --- | --- | --- |
| Prescan FLAGGED | 0 | null / null | false | null |
| Timeout, then valid response | 2 | null / null | false | applicable |
| Invalid JSON, then valid response | 2 | false / false | true | applicable |
| Schema-valid but semantically invalid, then valid | 2 | true / false | true | applicable |
| Invalid JSON, then timeout | 2 | false / false | true | null |
| Invalid JSON, then deadline before retry | 1 | false / false | false | null |

Preserve existing final failure classification for mixed transport/malformed outcomes and existing
initial-deadline behavior; do not invent a completed pass row when the checkpoint never completes.

**Persistence and compatibility decision.** Extend the producer-owned record and serialized
`advisor_checkpoint_pass_audit` envelope. At the specified base there is no dedicated checkpoint
envelope decoder to update: generic history readers exclude these audit-role rows and carry stored
audit content opaquely. This change keeps that behavior and adds no historical replay decoder or
backfill. Require the new fields at owned construction and verify the serialized new row exactly.
An old row remains its original audit fact; no conformance values are invented for it.

Prove that choice with a fixture containing a base-format checkpoint row: load the session through
the actual existing read/history path and verify the row remains readable and excluded from model
conversation history. Pair it with a new-format fixture and a missing-field failure at new-record
construction. With those consumers unchanged and old rows readable, the additive producer change
does not require an epoch cut.

JSON-only changes **can** require an epoch cut: `web/sessions/schema.py` documents epoch 65 for a new
required JSON key that made old envelopes unreadable. Re-check actual readers at implementation time.
If a reader now requires these fields, or a new required reader becomes necessary, the compatibility
assumption above is invalid: identify the reader, failing base fixture and proposed semantic epoch
cut before requesting approval for the expanded scope. Do not add missing-field defaults or a legacy
parser. Likewise, a new session column is outside this producer-envelope change. Any deployed
database rename/reset remains an operator action; do not perform one as part of this task.

**Audit first.** Persist the extended row before `record_advisor_checkpoint_pass` mirrors the fields.
Keep exact counts in the durable record and event. Use numerical measurements or bounded buckets for
metrics, not arbitrary model-controlled counts as metric attributes. Retain existing audit-write
failure propagation and prove that a failed write prevents the telemetry mirror.

## Tests

- Delete obsolete prose-protocol parser tests (verdict shapes, emphasis, preamble, token survival,
  CLEAN sub-heading). Port sanitizer properties to structured `note` fixtures, including U+2028/U+2029
  becoming LF without joining words, ANSI/control removal, fence removal, blank collapse, and the cap.
  List deleted and migrated test names in the commit message.
- New tests at the schema boundary: valid CLEAN; valid FLAGGED; invalid JSON; missing field; extra
  field; unknown category; wrong field types; CLEAN with a note; CLEAN with steps; FLAGGED with empty
  or whitespace-only findings; prose despite the schema; duplicate verdict keys in both orders;
  duplicate other fields; non-finite constants. Each invalid case must take the format-retry path,
  then end as `malformed` on a second failure. Include malformed-to-valid recovery.
- Keep step-id validation, header, retry-copy and whole-surface egress assertions. Migrate their prose
  response mocks to JSON and use distinct technical-findings and user-note canaries; unchanged
  behavior does not mean retaining obsolete prose fixtures.
- `request_advisor_hint` still sends no `response_format` (assert on the captured kwargs).
- Boot probe: valid output passes; rejection or invalid output fails boot; timeout remains non-fatal.
  Include A.7's request-parity and response-shape cases, not just mocked complete JSON replies.
- C and D as described above, each with a positive and a negative control. For both early FLAG and
  repairable END FLAG, offer one known ID, one unknown ID and a duplicate: assert offered=3 and kept=1
  in the durable row and telemetry. Exercise every conformance-table row, old/new stored-envelope
  reads, missing new-record fields, and audit-write failure ordering.

## Verification before hand-back

- Focused tests for every touched module, then `scripts/full-suite-gate.sh --execute --detach` with
  `--stages ruff,mypy,contracts,lints,pytest` named explicitly (the default runs only ruff and pytest).
  Read `summary.txt`; `frozen=yes` is required. Before launching, check host load and whether another
  session's full suite is running; run only one broad suite at a time.
- Compare the trust-tier lint corpus before and after, keyed on rule, path and message rather than line
  numbers, with a control that the comparison detects a synthetic difference. Report the counts.
- The two `test_freeform_planner_failure_translation` failures are pre-existing on the base; confirm
  they still fail identically on the base rather than assuming it.
- Run `pytest tests/ -m testcontainer -n 0`: D changes the persisted session audit contract, so this
  gate is required even when no SQL column or schema epoch changes.
- Do not deploy or restart the service. Write down the live-check steps for the operator instead: one
  blocked turn and one clean turn against the deployed advisor, and the boot log line showing the
  structured-output probe passed.

## Constraints

- Composer invariants hold: the provider stays in the path; no tutorial-specific code.
- No `# noqa`, `# type: ignore`, or lint suppressions. No `git stash`. Commit by pathspec after
  `scripts/branch-safety-check.sh --intent commit`.
- Never write a real account name or credential into a tracked doc; a docs test hashes every word.

## Hand-back

Report: the branch and head commit; what each of A–D changed, by file; the provider-routing findings
from A.6; the test names deleted and added; gate `summary.txt` path and each stage's exit; the lint
corpus counts; the merge-tree preview result; the actual durable-envelope read path and old/new-row
compatibility evidence; anything you could not do and why.
