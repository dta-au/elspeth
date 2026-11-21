# Changelog

All notable changes to ELSPETH are documented here. The current release is
recorded in full. Older entries are summarised: each keeps what the release
was and what an upgrader still needs (upgrade actions, breaking changes,
schema-epoch cutovers, required or renamed settings and removed features).
The full history is in the repository's releases and commit history.

---

## 0.8.16 - 2026-11-21

Web Composer fixes for large pipelines, long sessions and prompts held in
files, bounded advisor and prompt-guard requests, less redundant per-row engine
work, and LLM gateway logging. The session epoch stays at 79 and the Landscape
epoch at 51, so existing 0.8.15 session and Landscape stores open unchanged.
The dated entry does not establish qualification, a published tag or an image.

### Upgrade notes

Schema and stored data:

- No schema epoch changed and no stored data is migrated, reset or rewritten.
  Session epoch 79 and Landscape epoch 51 are unchanged from the previous
  release; this release adds no schema-epoch cutover, and no table is added or
  altered. Existing 0.8.15 session and Landscape stores need no action, and
  the 0.8.15 cutover guidance continues to apply to older stores. This
  authorizes no migration, reset, restamping or downgrade.
- **Upgrading from 0.8.14 or earlier: apply the 0.8.15 upgrade notes first.**
- Context-budget records written from this release carry a different
  `policy_hash` (the policy's schema string changed), and compose-loop records
  may carry `over_estimate` and `max_prompt_tokens`. The record of a compose
  call retried after a provider context-length rejection carries
  `retry_after_provider_rejection: true`, and `stubbed_tool_call_ids` is
  always empty in new records. Records written by 0.8.15 still load.
- Advisor call records now fill `max_completion_tokens_requested` with the
  completion cap that was sent (it was null on advisor rows). No field is
  added.
- Prompt-guard audit records are written at `schema_version` 2, with
  `units_scanned` and `units_reused`. Version 1 records still load.

Behaviour you will see:

- **Long inline prompts belong in files.** A Composer edit that adds or changes
  an inline LLM prompt (`system_prompt`, `prompt_template` or a query
  `template`) holding more than 100 words or 1,000 characters of prose
  (template tags are not counted) is refused with `inline_prompt_too_long`,
  and the composer puts the text in a file instead. Existing long inline
  prompts still validate and run, but the composer cannot change them in
  place: copy the text (see the new Copy button) into a file and bind it.
- **An inline prompt the template check refuses is now an error.** An inline
  prompt over the size limit for one template, nested too deeply or holding a
  number literal too long to read makes the pipeline invalid at authoring, with
  an error on that step. It used to be valid with a warning that named only an
  exception class.
- **Size limits changed.** One prompt template may now be up to 64 KiB (was
  16 KiB). A pipeline may hold up to 4 MiB of text in attached files (was
  1 MiB), counting a file once for each place it is bound; each file is still
  limited to 256 KiB. A file attached at a template position must fit the
  64 KiB template limit. The resolved configuration of a run may hold up to
  16 MiB of text, counted as text rather than as escaped YAML
  (`resolved_pipeline_config_oversized`).
- **YAML import refusals.** Importing YAML whose attached text is over a size
  limit is refused with HTTP 413 `prompt_file_too_large` and nothing is
  imported. An incomplete file marker is refused with 400
  `malformed_inline_blob_content`, and a prompt file that cannot be verified
  with 409 `import_prompt_file_unverified`. These cases used to fail with a
  server error.
- **Run start.** A run whose source, sink or retrieval step holds a file
  reference where a local path belongs is refused with the usual 422
  `pipeline_validation_failure` naming the step, instead of a 500.
- **File list API.** `GET /api/sessions/{id}/blobs` now returns 100 files by
  default (was 50); the maximum is still 500.
- **Composer tools.** The composer offers 48 tools by default (was 47; adds
  `report_composer_bug`). `list_transforms` is now a five-key selection index;
  a plugin's configuration comes from `get_plugin_schema`, which gains
  `usage_when_not_to_use`. `get_pipeline_state` gains four optional arguments
  and bounds large reads. `patch_node_options` refuses a `queries` value that
  leaves out an existing query. The duplicate-consumer repair suggestion
  carries facts only. `upsert_node` gains `routing_only`, which rewires an
  existing transform without re-sending it and returns a `routing_update`
  receipt instead of the component. Clients or tests that pin these tool
  results must follow; see Web Composer below.
- **Requests that fit are sent.** A composer or planner request under the
  configured request byte maximum is now always sent, even when its token
  estimate is over `composer_context_max_prompt_tokens`. Only earlier turns of
  the conversation are left out to stay under the estimate; nothing of the
  turn in progress is left out or replaced by a stub, so a turn whose own
  content is over the byte maximum is refused rather than cut. A compose
  request the gateway or model rejects as too long is sent once more with
  every earlier turn left out. If that is rejected too, or there was no
  earlier turn to leave out, the turn reaches the user as a size refusal, not
  as a provider outage; a planner request rejected as too long is refused
  that way at once.
- **Advisor requests are bounded by age, not stopped by size.** The advisor
  now sees earlier turns by age within a fixed budget; when a review's size
  estimate is over `composer_context_max_prompt_tokens`, older turns are left
  out first. A review still over the estimate is sent, as composer and planner
  requests are, and the provider judges its own limit; see Advisor below.
- **Advisor reply cap and timeout defaults changed.**
  `ELSPETH_WEB__COMPOSER_ADVISOR_MAX_COMPLETION_TOKENS`
  (`composer_advisor_max_completion_tokens`) now defaults to 16384 (was 8192)
  and `ELSPETH_WEB__COMPOSER_ADVISOR_TIMEOUT_SECONDS`
  (`composer_advisor_timeout_seconds`) to 180 seconds (was 60). Each advisor
  call still waits at most the time left in the turn. An explicit setting is
  used as given. A cap above the advisor model's output limit may be rejected
  by the provider, and startup cannot detect it.
- **Small edits are reviewed on what they changed.** The advisor judges a
  small edit by the steps the edit added and those whose untrusted input it
  changed. A condition already present on steps the request did not name (for
  example an LLM step without a prompt-injection shield plugin) no longer
  blocks the edit or leads the composer to change those steps; the reply
  mentions it instead. Review cards already waiting from an earlier turn are
  not requested again when the edit did not change what they review.
- **Undoing a rewire does not restore an approval.** A review that a rewire
  re-opened must be approved again before a run, even after the route is put
  back.
- **A busy server no longer reports that it stopped.** A compose request that
  loses its hold on the session while the server keeps running now ends with
  HTTP 503 "This request stopped because the server was too busy to keep its
  hold on the session." and a diagnostic id. Any other request that ends
  without a result while the server is running gets the generic failure with a
  diagnostic id. "The server stopped while composing this request" is now
  reported only when the server restarted or the request belonged to another
  web process.
- **New warning log lines:** `session_operation.renewal_slow`,
  `composer_operation.event_loop_lag`,
  `composer_interpretation.surface_skipped_runtime_findings` and
  `composer_interpretation.surface_skipped`; see Operations below.
- **Prompt guard calls change shape.** The user prompt is inspected in its own
  call, and text the guard service already allowed in the session is not sent
  again, so the number of guard calls no longer grows with the conversation.
- **New setting.** `ELSPETH_WEB__COMPOSER_VALIDATION_REUSE_ENABLED` (default
  `true`) lets the composer reuse validation results within one compose call.
  Set it to `false` and restart to turn reuse off.
- **Readiness.** `/api/ready` still checks every database connection on every
  probe, but runs the schema check at most once every 60 seconds.
- **LLM gateway logs.** The gateway container now writes its log events to
  stderr unless the `elspeth_llm_gateway` logger is already configured, and the
  `completion` event carries token counts.
- **LLM audit view.** `GET /api/sessions/{id}/messages?include_llm_audit=true`
  now also returns end-of-turn advisor review rows; see Advisor below.

### Web Composer: prompts and files

- Binding a file to a step's `prompt_template` with `wire_blob_inline_ref`
  now replaces whatever prompt was there, including a structured prompt with
  defined terms. The old prompt's term reviews are superseded and the file gets
  its own review. This replaces the 0.8.15 statement that a structured prompt
  could not be switched to a file. Writing a file marker directly onto a
  structured prompt by any other route is still refused, and the refusal now
  names `wire_blob_inline_ref` as the call that does it. When asked to replace
  A with B, the composer no longer asks whether to keep A's approvals.
- A structured prompt can be replaced with new plain text: send
  `prompt_template` with `prompt_template_parts: null` through
  `patch_node_options`, `upsert_node` or `set_pipeline`. The old parts and term
  reviews are removed and listed in `superseded_review_ids`, and the new text
  gets its own review card. A step re-sent with its own prompt text keeps its
  structure, term definitions and approvals, and the result names it in
  `prompt_unchanged_node_ids`.
- A `patch_node_options` change whose `queries` value leaves out a query the
  node already has is refused with `queries_patch_omits_existing`, naming every
  omitted query, instead of silently deleting them. Send every query to change
  one, or send the whole node with `upsert_node` to remove one;
  `queries: null` still removes every query. A patch that also replaces the
  prompt is refused whole.
- A step that reads an attached file keeps its authoring guarantees. A
  file-backed LLM prompt (before and after its review is approved), a
  `file_inject` entry held in a file, and steps that take their settings from
  an operator profile no longer cause every later step to be refused with
  "Producer guarantees: (none)". Such a step is reported with a
  `contract_probe_deferred` notice naming it and the fields it adds, and its
  contract is checked on the file's bytes before the run starts. A field that
  an attached file could add is left to that check; a field nothing in the
  pipeline could supply is still an error, and the source data-contract review
  is offered for it. An output that needs such a field is refused at authoring
  as its inline twin is, instead of being shown as valid and then refused by
  the run.
- An attached file placed where a local file path belongs (a source or sink
  path, or a retrieval step's persist directory) is refused when the pipeline
  is edited, with a message naming the step and option.
- Opening a saved pipeline in which a batch step declares input fields reports
  the problem instead of failing.
- Template refusals are typed and name their position. A prompt over the
  template limit, nested too deeply or holding a number literal too long to
  read is refused with a message naming the step and option (and, for size,
  the size and the limit), never quoting the text. This applies to inline
  prompts, prompt files, query templates and the LLM source. Attaching a file
  over the template limit at a template position is refused at once. Saving a
  prompt file, or making a substring edit, that would take a template over the
  limit is refused with the size and the limit. A saved pipeline holding
  template text the engine refuses now opens and reports the step; it used to
  fail to open, and a turn on it failed with an unexplained error.
- A pipeline refused for the total size of its file-held text says the total,
  the limit and the number of file bindings, and the composer is told which
  limit applied and to shorten the named files, not that session storage ran
  out.
- Web validation judges a pipeline's size by its text, so prompt files in any
  language validate up to the file-text limit. Preflight no longer re-reads
  file-held prompt text through YAML.
- YAML import: re-importing your own export, or importing YAML whose source is
  bound without a `mode`, no longer hides the session's prompt files or fails
  with a server error. Import refusals name the whole field path (at most five,
  then a count of the rest) and, for a file that cannot be verified, the
  step's file-backed prompt fields.
- `set_pipeline` accepts an `inline_blob` at a prompt field and stores it as a
  prompt file; an exact retry reuses the stored file.
- Under explicit approval, a response carrying several pipeline changes is
  refused with guidance to send the first change alone and each next one after
  the user accepts the one before, instead of rebuilding the whole pipeline in
  one `set_pipeline` call. A pipeline between approved changes does not have to
  validate; expect one approval per change, with intermediate versions that may
  show errors. Creating a prompt file with `create_blob` comes first, in an
  earlier response.
- The composer edits an existing pipeline with the narrow tools and sends a
  whole `set_pipeline` only for a pipeline of five nodes or fewer, or when no
  narrow tool can express the change. A failed call is retried as the same
  call. Tool descriptions list every review term the model can author, and a
  validation entry's `error_code` is documented as optional.
- The composer can now clear a pipeline's description: `set_metadata` takes
  `description: null` or an empty string, and a field left out is unchanged.
  Its result lists the fields that actually changed (`changed_fields`). When
  nothing changed it says so instead of reporting a successful edit, so the
  composer no longer tells you it removed something it did not. A pipeline
  name can be replaced but not cleared: `null`, an empty name or a
  whitespace-only name is refused.
- `upsert_node` accepts `routing_only=true` to rewire a step without re-sending
  it. It changes only an existing transform's `input`, `on_success` and
  `on_error` and keeps its plugin, options (file-held prompts included) and
  description as stored. Any other field, an explicit `null` route or a step
  that is not a transform is refused with `routing_only_edit_invalid`. The
  result carries a small `routing_update` receipt instead of the component and
  names each review the rewire re-opened (`reopened_reviews`, up to 32, with
  the full `reopened_review_count`). A prompt-shield review, or the disclosure
  of an auto-wired control, re-opens when the rewire changes what it binds and
  must be approved again before a run; putting the route back does not restore
  it. The proposal card and its diff show the rewire, not a replacement. See
  `docs/reference/composer-tools.md`.
- The composer no longer requests a review card again when an earlier turn
  already raised it and this turn changed neither its step nor, for a
  prompt-shield recommendation, any step upstream of it. That card is still
  waiting. Every requirement the turn staged or changed is still requested.
- After a refused edit, the next successful edit reports only the errors and
  warnings that edit added or fixed. It no longer lists the refusal as
  resolved, or every standing finding as new after a refused full replacement.
- The duplicate-consumer validation error names every step reading the
  connection, not only the first two. Its repair suggestion now states only the
  connection and the ids of the steps reading it, with no ready tool calls or
  whole nodes, and says to change only the input bindings and keep each step's
  options; the composer builds the fork itself.
- `list_transforms` returns, per transform, only `name`, `description`,
  `capability_tags`, `web_config_authority` and `policy_capabilities`, plus the
  prohibited list and `details_tool`. `get_plugin_schema` returns a plugin's
  options and now its `usage_when_not_to_use` text.
- `get_pipeline_state` gains `field_path`, `offset_chars`, `expected_sha256`
  and `since_start`. It can return the net changes since the turn began, and a
  large read becomes a structural overview: single values are read by
  `field_path`, and long values come in pages tied to a content hash. A stale
  or missing hash is refused. A read is bounded at 32,768 bytes.
- `list_blobs` and `list_composer_blobs` still show the 50 newest files, and
  now say when more exist (a `post_call_hints` entry starting
  `Incomplete list:`) so the model does not treat an unlisted file as missing.
- A successful `get_blob_content` or `read_attachment` result no longer
  carries the pipeline's validation block, so standing validation findings no
  longer stop a prompt file from being read.
- New tool `report_composer_bug` records a suspected mismatch between the
  composer's guidance and a tool's behaviour. Each report is one session JSON
  file (UTF-8, at most 16,384 bytes; at most 20 per session, freed by deleting
  one) marked suspected and unverified, and the call changes nothing else. It
  runs immediately in approval mode, like `create_blob`.
- A question or other message that changes nothing no longer fails with "could
  not verify this session's audit trail" when the pipeline has a prompt held
  in a file with an open review card and the previous message also changed
  nothing. Once it started, it failed on every following message until the
  pipeline changed or every card was resolved.

### Web UI

- A Copy button beside each inline prompt (system prompt, prompt template and
  each query template) in the node inspector, the specification view and the
  prompt review card copies the exact stored text, so a long inline prompt can
  be moved into a file.
- The prompt editor opens, edits and saves each file on a step with several
  prompt files. Saving one keeps the step's other prompt files approved. It
  opens inside the row of the prompt it edits, follows a file that changes
  while open, and keeps unsaved text when you select another step or the
  editor closes ("Your unsaved text is kept").
- The prompt review card names each prompt's file under its own heading, and
  no longer says the LLM wrote the instruction when the whole prompt is a file
  a user supplied.
- The file list loads every file in the session (up to 10,000), not only the
  newest 50, so older prompt files no longer read as "not in this session".
- The Workflow panel gains a Files section listing each file the pipeline
  uses, with the step and fields that use it (one row per step), Show node and
  Edit file actions, and a "Filter by file or step" box. Files no step uses are
  listed only in the file manager. The file manager gains a wiring pad that sends
  your chosen prompt connections to the composer as an ordinary chat request,
  and can filter files.
- The inspector names a source or sink step's file.
- Hide or show the conversation from the left end of the title bars, or with
  Ctrl+\.
- Refusals and failures now say what happened where you are looking:
  - A YAML import refused because the paste contains a credential says so and
    tells you to use a secret reference, instead of claiming the paste is empty
    or too large.
  - A refused chat upload says why (quota, size or file type) next to the
    upload button, instead of sending you to the file manager.
  - Archiving or renaming a session that the server refuses shows the server's
    reason, for example "Cancel the run first.", instead of "Please try again."
  - If starting a run loses its connection, the message says the start could
    not be confirmed and asks you to check Run history, instead of pointing at
    results that may belong to an earlier run.
  - When your sign-in expires, the sign-in page says it expired and asks you to
    sign in again. Unsent chat and unsaved prompt text is not kept.
- Recovery in place:
  - If creating a new session fails while Composer is working, the current
    session keeps receiving that turn's updates instead of staying busy.
  - A prompt file that fails to load in the editor can be retried in place,
    and the disabled Save says why.
  - If the tutorial's audit summary fails to load, a Retry button loads it
    again.
- The session picker and its search include all your sessions, not only the 50
  most recently updated.
- Pressing Enter to confirm an input-method candidate (for example Japanese,
  Chinese or Korean) no longer sends a half-typed chat message.
- Uploads show that they are in progress: chat says "Uploading <file>…" and
  the Upload buttons are unavailable until the upload finishes.
- The Composer guide and user manual now say that approval mode cannot be set
  or seen in the web UI and is switched only through the session preferences
  API.

### Large pipelines and performance

- The composer's size estimate prices every byte at the shipped rate
  (3 bytes per token, or the provider's measured density when that is
  denser), so a large pipeline keeps its conversation history when it fits.
  Content already left out of a request can be left out again in the same
  call, so a request that fits without it is no longer refused or stopped with
  an internal error.
- Only earlier turns of the conversation are left out to fit the context
  budget. Every round of the turn in progress (its tool calls and tool
  results, on the compose loop and its reply stage, the planner and the
  planner's escape-hatch transcript) is sent whole, and a tool result is no
  longer replaced by an omission stub. A request still over
  `composer_context_max_prompt_tokens` once every earlier turn is left out is
  sent and recorded as over the estimate. Only a request over the request byte
  maximum is refused, so a turn whose own content is over that maximum is now
  refused rather than cut. The context estimate follows the latest call of the
  turn. `stubbed_tool_call_ids` in new LLM-call records is always empty;
  records written by earlier releases keep their values and still load.
- A provider "input is too long" rejection is recognised even when the gateway
  reports it under a generic or unfamiliar error code. The provider's own
  reason is still shown only when `composer_expose_provider_errors` is on, and
  the call record keeps the closed reason `context_length_exceeded` without
  provider text.
- When the provider or gateway rejects a compose request as over the model's
  context limit while earlier turns are still in it, the request is sent once
  more with every earlier turn left out, instead of the turn being refused,
  and the user sees the usual notice, once per turn, "Earlier conversation is
  no longer visible to the composer." Those turns stay out for the rest of the
  turn. Only if the retry is rejected too, or there was no earlier turn to
  leave out, is the turn refused as too large for the model. Such a turn makes
  two provider calls; both are recorded, and the retry's context-budget record
  carries `retry_after_provider_rejection: true`. The planner, the advisor,
  the reply stage and the last-chance composition call do not retry; a planner
  rejection reaches the user as a size refusal.
- Long sessions no longer fail edits with "The server stopped while composing
  this request" while the server is running. Building the advisor's
  prompt-guard request took time that grew with the square of the session's
  length and ran on the event loop, so on a long session it could hold the
  loop long enough for the session's hold to lapse. That work is now linear,
  and the request is built once per advisor call, after the transcript is
  shaped, in the worker pool; the stored history's redaction is reused while
  it is unchanged. What is redacted, scanned and sent is unchanged. A request
  that does lose its hold now says so; see Upgrade notes.
- Approving a prompt review card for a step with many file-held prompts checks
  the step once for all of them and reads each file once, instead of once for
  every file on the step. Such approvals no longer slow down sharply as files
  are added or fail with a server error on a slow host. The approval rows
  written and the refusals given are unchanged.
- Within one compose call, validation of an unchanged pipeline is not repeated:
  the runtime preflight and the policy view's authoring check are reused until
  the pipeline, a referenced file, the secret inventory or a resolved path
  changes. A state whose own options wire a secret reference is never reused.
  Run admission always validates afresh. The reuse reads all attached files'
  records in one database transaction.
- Preflight parses the rendered pipeline YAML once instead of three times.
- Template checks cache their results across a whole validation walk, and the
  caches are bounded at 128 MiB in total per process, so long-running servers
  no longer grow memory without bound.

### Advisor

- Advisor requests no longer grow with the session. The advisor sees this
  turn whole, the previous three turns as the request and the reply shown, and
  older turns as the request only, newest first, within 8,000 tokens; an older
  request over 2,000 characters is shortened to its first 1,500 and last 500.
  The request states exactly what it leaves out. When a review's size
  estimate, based on a real earlier call, is over
  `composer_context_max_prompt_tokens`, the oldest earlier turns are left out
  until it fits; if it is still over with none left, it is sent anyway and the
  request records the estimate, the limit and `over_estimate`. A provider
  rejection of it is reported like any other request rejection.
- The advisor's completion cap now defaults to 16,384 tokens (was 8,192) and
  its per-call timeout to 180 seconds (was 60); a call still waits at most the
  time left in the turn. When a review runs out of its completion cap before it
  gives a verdict, the completion check now says so ("the advisor's reply ran
  out of room before a verdict"), names
  `composer_advisor_max_completion_tokens` and the cap that was sent, and
  advises raising it, instead of reporting a malformed response. Every advisor
  call record carries that cap in `max_completion_tokens_requested` beside its
  finish reason.
- On a small edit the advisor judges what the turn changed: the steps it added
  and the LLM steps whose untrusted input it changed. Inserting, removing or
  moving a step in front of an LLM step without changing the content it
  receives does not count. A condition only on steps the turn did not change
  is pre-existing: it does not block the edit or withhold the reply, the
  composer is told not to change those steps for it, and the reply reports it
  once. Violations the turn introduced are still flagged.
- The advisor is told plainly when a prompt is held in a file: the file's name,
  size and who wrote it. A file you wrote is not sent to the advisor; a file
  written by the composer, produced by a pipeline, imported from a shared link
  or copied by a session fork is sent and reviewed like an inline prompt. A
  valid file-held prompt is no longer described as invalid.
- The advisor reviews against the user's request and binding contracts, not
  against earlier assistant choices, but counts a proposal the user explicitly
  accepted as a requirement. A finding that does not apply can be answered with
  evidence, written for the user, instead of an edit.
- A newly flagged pipeline keeps its remaining review. The extra review round
  runs only when the turn changed the pipeline; if that round fails or its
  reply is withheld, the reply the first pass admitted is published, provided
  the pipeline and the session's files are unchanged since then.
- `GET /api/sessions/{id}/messages?include_llm_audit=true` also returns
  end-of-turn advisor review rows with `phase`, `pass_index`, `verdict`,
  `source`, `provider_attempts` and `publication_verdict`. The response header
  `X-Elspeth-End-Review-Rows-Omitted` gives the number of stored rows left out
  because they did not parse.

### Prompt guard

- Within a session, text the guard service has already allowed is not sent to
  it again; only new text is inspected, and the user prompt in its own call.
  Guard calls per turn no longer grow with the conversation. Reuse is keyed on
  the session, policy, mode, role, slot and the exact text; only a complete
  allow is reused, and a refusal, timeout or error never is. The record of
  allowed text holds digests only, lives in the web process that made the
  check (at most 20,000 units per guard), and is lost on restart. With several
  web workers, a unit of text is scanned once per process. Each guard audit
  record states how many units it scanned and how many it reused
  (`schema_version` 2). One deadline still covers the whole check.

### Engine

- Less redundant per-row work on the sink and gate paths, with no change to
  stored audit records, outputs or refusals. The sink executor no longer
  computes a sink-effect identity that the reservation recomputes;
  finalization members are copied only when recovery rebinds their ordinal and
  their output data is frozen once; gates hand their input token on instead of
  copying it.

### Operations and LLM gateway

- LLM gateway: its metadata-only log events reach the container's stderr, one
  JSON line each, unless the operator configured the `elspeth_llm_gateway`
  logger. Two new events: `upstream_result` (status class and code) and
  `response_validation_rejected` (stage and a closed category; exception text
  is never logged). The `completion` event logs `prompt_tokens`,
  `completion_tokens` and `total_tokens` when the response reports them as
  non-negative integers, never content; this is a recorded exception to the
  logging policy because the gateway has no audit store of its own.
- LLM gateway: adapters may also log `stop_reason_category` and six response
  block counts (`content_block_count`, `text_block_count`, `tool_block_count`,
  `other_block_count`, `empty_text_block_count`, `malformed_tool_block_count`).
  These are categories and counts, never content; the built-in adapter does
  not emit them.
- `/api/ready` checks database connectivity on every probe and the schema
  against the current models on the first probe and then at most once every
  60 seconds, reporting the last `CURRENT` result between checks. A result
  that is not `CURRENT` is never reused, and a probe that cannot connect makes
  the next probe check the schema again.
- The `composer_operation.settled` log line carries the session id, operation
  id and the support diagnostic id. An audit-integrity failure adds one error
  line, `composer_operation.audit_integrity_failure`, with the error's class
  and a credential-filtered message of at most 512 bytes. Both point to the
  private failure record kept with the operation.
- `composer_operation.owner_unsettled` (error), written when a compose request
  ends without recording a result, now carries the session id, operation id,
  diagnostic id, the number of underlying failures and up to eight of them,
  each with its class, its cause's class and its message, credential-filtered
  and only when it is at most 512 bytes. No row data or prompt text is
  logged.
- Two new warnings show a held event loop before it costs a request its hold
  on the session. `session_operation.renewal_slow` is logged when a session
  hold renewal starts 2 seconds or more after it was due or takes 2 seconds or
  more; it carries the session, operation, epoch and kind, the hold and renewal
  interval, `start_late_seconds`, `attempt_seconds`, the worker pool's
  outstanding admissions and the outcome, never the hold's token.
  `composer_operation.event_loop_lag` (with `lag_seconds`) is logged when a
  probe that sleeps for half a second wakes more than one second late.
- The composer logs one warning when it skips creating review cards:
  `composer_interpretation.surface_skipped_runtime_findings` when the
  pipeline's runtime check reports problems (for example a stored file path
  outside the allowed directories after a data directory was moved), and
  `composer_interpretation.surface_skipped` when a single card could not be
  created. Each line names the session, state and, for a single card, the
  step and review kind, with the error codes or exception class but not their
  text. Before this, a review could stay pending with no card and nothing in
  the log to say why.

### Tooling

- New operator tools for acceptance runs against a deployment: a Composer
  edit battery (a planned run of small edits, each judged on its own, with
  50-edit plans and a structural add/swap/remove cycle around an LLM step), a
  generator for a wired-up large-graph fixture and its setup through the real
  routes, a real-graph battery, a copied-session prompt-review repair tool, a
  direct Bedrock streaming canary and a recovery-set capture. Each records
  sanitised per-call usage and timing. See
  `docs/runbooks/composer-edit-battery.md`.
- The edit battery gains a 35-edit file-binding plan
  (`composer_file_bindings.json`). Starting from an empty session, it uploads
  each file before the message that names it, has the composer connect and
  reconnect sources, reference joins, LLM prompts, system prompts, query
  templates and file-inject entries to uploaded and composer-written files,
  checks which file each binding points at and which kind of review card
  opens, and ends empty. Plans can now upload files, start from an empty
  session and use a pass rule for plans shorter than 50 edits.
- The battery's judge no longer counts an unchanged finding as new when the
  server spells it differently: a component named with or without its kind
  prefix (`node:`, `output:`, `sink:`, `source:`) and a pending review stored
  as a sentence or as its bare kind compare equal. A step whose query
  templates are held in files is judged as file-held, as the server does.

Automatic image publication from main remains held. The 0.8.16 tag and
container publication follow successful required checks and the protected
merge. Operator signing, live-provider acceptance and deployment remain
separate steps.

## 0.8.15 - 2026-11-17

Executor hot-path fixes, Web Composer fixes and a release-pipeline repair.
Session epoch advances from 78 to 79 and Landscape from 50 to 51, so
deployments need explicitly new session and Landscape stores. 0.8.15
supersedes 0.8.14, which was tagged but never published as a GitHub release or
version-tagged image: its smoke test could not run on hardened CI runners. Everything in the 0.8.14 section below therefore ships for the first
time in this release, including its upgrade actions. The dated entry and
package version do not establish qualification, a published tag or an image.

### Upgrade notes

- API clients that approve prompt review cards must now send
  `reviewed_anchor`; see Web Composer below.
- **Upgrading from 0.8.13 or earlier: apply the 0.8.14 upgrade notes first.**
  ELSPETH now requires and runs on Python 3.14 only, startup refuses
  `JSON_LOGS=true`, and a run checkpointed under 3.13 refuses to resume under
  3.14. Drain in-flight runs and pending approvals before upgrading.
- A run interrupted before upgrading cannot be resumed after it: the
  schema-contract module changed, so the runtime manifest recorded at run start
  no longer matches and resume refuses (no data is changed; re-run or finish
  interrupted runs before upgrading).

Schema and stored data:

- Session epoch advances from 78 to 79; Landscape advances from epoch 50 to
  51. File-backed LLM prompts are reviewed, admitted only on the approved
  bytes, and the run records the hash of the prompt file it read. The session
  store adds `prompt_review_snapshots` (the reviewed file text and its
  identity, kept with the review event) and `prompt_file_approvals`
  (append-only approvals, whose latest row per binding is the admission
  authority); the Landscape store adds `run_prompt_inputs` (the approved
  prompt-file identity and content hash a Web run admitted, recorded when the
  run starts, in the same transaction as its run row and before any LLM call,
  with a foreign key to the run). The new tables are additive.
- Configure an explicitly new epoch-79 session target and an explicitly new
  epoch-51 Landscape target. Preserve epoch-78 session stores and epoch-50
  Landscape stores with compatible prior application access; 0.8.15 startup
  refuses to open them, and no 0.8.14 data is read across. The 0.8.10
  cutover guidance below continues to apply to older stores. This authorizes no migration, reset,
  restamping or downgrade. Drain and isolate old/new deployments; they cannot
  share session stores or replicas.

### Engine and runtime

- Executor hot-path fixes: `SchemaContract.version_hash()` is computed once
  per contract instance, and gates reuse the input hash recorded for the node
  state instead of hashing each row a second time. Constructing a
  `SchemaContract` with `fields` that is not a plain tuple now raises
  `TypeError`.

### Web Composer

- **Approving a prompt review card is bound to the text the card showed.**
  Every LLM prompt review card stores a review snapshot (inline prompts
  included), and `POST /api/sessions/{id}/interpretations/{event_id}/resolve`
  requires `reviewed_anchor` for prompt cards: the snapshot's `anchor` from
  `GET /api/sessions/{id}/interpretations/{event_id}/prompt`. A missing anchor
  is refused with 422 `prompt_review_anchor_required`; an anchor that is not
  the card's stored snapshot is refused with 409 `prompt_review_stale`, and
  nothing is written. Other review kinds must omit it (422
  `interpretation_review_anchor_unexpected`). API clients that resolve prompt
  cards must send the anchor.
- **An LLM prompt can be backed by a session file.** Pointing the prompt at a
  different file, or a change to the file's content, reopens the prompt review,
  and the run is blocked until the new text is approved. Each run uses exactly
  the approved bytes and records their hash. File-backed prompt reviews are
  always shown, including in a session that has opted out of interpretation
  reviews, and a forked session's file-backed prompts are reviewed again in
  the new session. A resume is refused if the run's prompt files were
  re-approved with different content after it started.
- **The inspector's prompt editor edits a prompt file in place.** Saving is
  your approval of the new text. A save is refused when the file backs more
  than one prompt (`prompt_file_shared_binding`), is not UTF-8
  (`prompt_file_encoding_unsupported`), changed since you opened it or its
  binding moved (`prompt_file_stale`, `prompt_file_binding_changed`; the
  request must carry `expected_sha256`), or while the node's review card is
  still waiting (`prompt_file_review_pending_inline`, 409).
- **Typed refusals for file-backed prompts.** A prompt that points at a
  session file is refused when the file is not plain text
  (`inline_blob_not_text`) or the prompt is a structured prompt
  (`prompt_template_parts_required`); switching a structured prompt to a file
  is not supported in this release.
- **New composer tools.** `inspect_prompt_targets` reports a prompt's length
  and SHA-256 (never its text), and `change_substring` applies exact,
  all-or-none substring edits to that prompt against that hash. Changing a
  prompt reopens its prompt review. The composer now offers 47 tools by
  default (was 45). `wire_blob_inline_ref` can bind a session file to a list
  entry by position, such as `options.queries.0.template`.
- **Limits and defaults changed.** Composer provider requests scan text values
  up to 2 MiB for credential material (was 256 KiB); larger values are still
  refused unscanned. `composer_runtime_preflight_timeout_seconds` defaults to
  60 s (was 5 s). Runtime YAML import, including the browser Import YAML
  dialog, accepts documents up to 524,288 characters (was 262,144).
- **Context budget estimate.** The composer's context budget estimates a
  request's size from the provider's own token count for an earlier call in
  the same exchange, plus every byte added since; requests carrying several
  large plugin schemas that were refused as over budget are now admitted.
  Composer LLM audit records gain optional anchor and pricing fields; records
  written by earlier versions still load.
- Fixes: the pipeline graph no longer blanks and redraws its connectors,
  exporting a pipeline as YAML no longer fails with a 409 when an option is
  backed by a session file, and selecting a source or sink no longer clears on
  every composer turn.

### Release pipeline

- The image smoke test runs on CI runners with a `0077` umask, which is what
  stopped the 0.8.14 tag build before release.
- The release image scan accepts the four chromadb 1.5.5 advisories that
  `pip-audit` already accepts (CVE-2026-45829, -45830, -45831, -45833) as
  version-scoped, expiring exceptions in `.trivyignore.yaml`. They affect the
  Chroma server, which ELSPETH does not run; the gateway image takes no
  exceptions.

### Publication and acceptance

Automatic image publication from main remains held. The 0.8.15 tag and
container publication follow successful required checks and the protected
merge. Operator signing, live-provider acceptance and deployment remain
separate steps.

## 0.8.14 - 2026-11-15

Python 3.14, Web Composer remediation and plugin fixes. ELSPETH now requires
and runs on Python 3.14 only; this release moves the packages, the images and
CI to it and corrects the behaviour that changed under the new interpreter. It
also carries the first part of the Web Composer remediation (withheld replies,
version history, `diff_pipeline`, credential handling, the tutorial, the batch
plugins and `report_assemble`), a fix that stops `elspeth purge` from deleting a
live share link's data, and a broad round of plugin fixes. The dated entry and
package version do not establish qualification, a published tag or an image. Not
run for this work: the PostgreSQL testcontainer suites, a live provider,
end-to-end browser tests and CI itself on the new single lane.

### Upgrade notes

These changes are breaking:

- **Runs admitted before the upgrade refuse to resume in the Web Composer.**
  Making the three batch plugins available on every install changes the
  compiled plugin policy hash, so a web run admitted under the previous policy
  is refused on resume as `POLICY_CHANGED` (`admitted_policy_changed`). Drain
  in-flight web runs before upgrading.
- **A Web Composer graph that feeds `report_assemble` straight into a `text`
  sink is refused at semantic validation.** The text sink diverts rows that
  contain a line break, so such a graph used to pass validation and then fail
  at run time. Put `line_explode` between them. CLI YAML and `report_assemble`'s
  emitted row shapes are unchanged.
- **Deploy the Web Composer backend and frontend together.** The version
  history response gains an `is_milestone` field that the new version menu
  reads. No schema changes and no stored row is rewritten.
- **Share links minted before this release keep working, but their data stays
  exposed to `elspeth purge`.** New share data lives in a separate `shares/`
  subtree of the payload store. Never point `elspeth purge --payload-dir` at
  `shares/`. See the Sharing section below.
- **Library entries published before this release must be republished before
  they can be forked.** Library payloads now live in their own
  `<payload base>/library` subtree, with no fallback to the parent store and no
  migration. Forking an older entry returns 409 `library_payload_unavailable`
  asking for a republish. Back up `library/` with the payload store, and never
  point `elspeth purge --payload-dir` at it.

- **Python 3.14 is required** for `elspeth`, `elspeth-lints` and the LLM
  gateway (and the gateway adapter template). Python 3.12 and 3.13 are no
  longer supported or tested. Locked dependency versions are unchanged; only
  the 3.12 and 3.13 markers and wheels left the lockfiles.
- **Startup refuses `JSON_LOGS=true`.** This applies to the CLI, the web
  server and both MCP servers. LiteLLM's JSON-logs mode logs unhandled
  exception messages verbatim through its own handler, which ELSPETH's log
  scrubbing cannot reach. Unset it; ELSPETH's JSON output has its own setting.
- **Drain in-flight runs and pending approvals before upgrading.** The
  runtime-validation manifest hash changes with the interpreter, so a run
  checkpointed under 3.13 refuses to resume under 3.14, and a web approval
  recorded under 3.13 no longer matches. The `database` sink's effect plan
  format also changed, so a batch prepared but not committed before the
  upgrade is refused.
- **The release image carries its own interpreter.** The runtime base is now
  `gcr.io/distroless/cc-debian13:debug-nonroot`, and the image carries CPython
  3.14.5, installed by the pinned uv under `/opt/python`, with the venv at
  `/opt/venv`. There is no `/usr/bin/python3` in the image any more. The
  builder is `debian:trixie-slim`. The gateway image is `python:3.14-alpine`.
  3.14.5 is the newest CPython the pinned uv offers; a later patch release
  needs a uv image bump.
- **Dataverse FetchXML runs recorded before this release cannot be replayed
  in verify mode.** The `Prefer` header is now part of the recorded request,
  so the earlier records no longer match. OData runs are unaffected.
- **Plugin configs and inputs that used to load are now refused.** Each was
  either a latent failure or silently wrong:
  - `reference_join` reference data containing NaN, Infinity or integers
    beyond 2^53.
  - Dataverse endpoints that are not HTTPS origins, or that carry a path or
    query; `select` entries that are not valid logical names; FetchXML with
    zero or several entities; a sink lookup whose key is not mapped
    (previously ignored).
  - Azure blob `account_url` values with a query string or fragment; put a
    SAS token in `sas_token`.
  - `batch_classifier_metrics` with a `positive_label` whose type differs
    from the data (for example `1.0` against integer labels), and baseline
    labels in the paired-preference, effect-size and experiment-compare batch
    transforms that relied on coercion (`2.0` no longer matches `2`).
  - In observed mode, a row key the S3 or Azure blob sink cannot encode in
    the CSV header is now a config error instead of a row failure.
  - A non-text codec (base64, hex, zlib, rot13) in any plugin `encoding`
    option (sources, sinks, the blob expand transforms and `file_inject`), and
    a `"`, newline or carriage-return delimiter in any CSV `delimiter`
    option. These used to fail at run time; a CSV source could not read a row
    after the strict-decoding fix below.
  - `database` sink `if_exists: replace` and `chroma_sink` `on_duplicate:
    skip` or `error`. None of them worked, and they were refused only once
    the sink ran. `append` and `overwrite` are now the only values.
  - `truncate`'s `strict` option, which is removed (see below).
  - OpenRouter `base_url` values with a query string or fragment.
  - A `web_scrape` `auth` setting, which previously failed every row, and a
    JSON record column without a path.
  - An empty `query_template` or `query_pattern` in retrieval transforms.
  - Multi-query LLM configs whose generated field names collide across
    queries, including the raw, `_usage` and `_model` fields. Uniqueness used
    to be checked only when a query set `structured_output_prefix`; the
    legacy precedence mode is removed.
  - LLM pool delay settings that contradict each other (minimum above
    maximum, or AIMD settings that cannot hold when `pool_size` is above 1).
  - A tracing config that sets other keys while its provider is `none` or
    omitted; those keys were previously ignored.
- **`type_coerce` is stricter.** Converting to `bool` no longer turns a blank
  or whitespace-only string into `False`; it is a `not_boolean_string` row
  error. Numeric strings with underscores or non-ASCII digits, and integers
  outside the safe range (`out_of_range`), are refused instead of converted.
- **Single-query LLM `metadata.model` is now the configured model.** The
  model the provider actually served is in `metadata.served_model`
  (`<query>_served_model` for multi-query). Readers that treated
  `metadata.model` as the served model must switch fields.
- **Some outputs and error shapes change.**
  - `batch_paired_preference` 95% intervals use Student's t at n-1 degrees
    of freedom instead of z = 1.96, so they widen for small samples (for
    n = 3, ±0.34 becomes ±0.75).
  - JSONL append in the `json` sink refuses an existing file that is not
    newline-terminated or whose lines are not JSON objects; it previously
    glued the first new record onto the last line.
  - Azure blob JSONL output now ends every record with a newline, so
    restaging a JSONL body written before this release fails closed.
  - JSON, JSONL and blob inputs nested deeper than 64 levels are now
    quarantined or routed as row errors.
  - S3 JSONL objects with an oversized line now yield the rows after it.
  - Azure and Bedrock LLM batch waits can be noticeably longer (see Plugins).
  - The `blob_too_large` reason no longer carries `body_size`, and
    `S3SinkPoisonedError` has been removed. So has
    `elspeth.contracts.errors.DuplicateDocumentError`, which only
    `on_duplicate: error` raised.
  - An undecodable UTF-16 or UTF-32 record in an Azure blob CSV or JSONL
    source, or an S3 JSONL source, is quarantined on its own, with its raw
    bytes, instead of quarantining the whole object.
  - A URL containing an ASCII control character is refused as
    `malformed_url` by every SSRF and origin check, even with no allowed
    origins configured; the origin check used to report `origin_not_allowed`.
  - The `OutputPort` protocol gains `fail_pending`; in-tree implementations
    are updated, out-of-tree ones must add it.
- **`CallVerifier` and `CallReplayer` are removed** from
  `elspeth.plugins.infrastructure.clients`. `CallModeSession.verify_call` is
  the single verify authority, and replaying an unrecorded ordered query now
  raises `AuditIntegrityError`.
- **`CallModeSession` gains `has_source_call`**, a read-only lookup used by
  Textract replay and verify. Out-of-tree session implementations must add
  it.
- **Textract retries go through the engine.** Throttled and transient
  Textract and S3 HeadBucket failures raise `PluginRetryableError` and
  consume engine retry attempts; with no retry policy they end as the
  engine's retry-exhausted row failure instead of `submit_failed` or
  `analysis_failed`. `poll_timeout` stays a row error and is non-retryable.
  SSL, proxy, response-streaming and HTTP-client botocore errors are now
  retryable transport errors instead of a permanent `botocore_error`. In
  verify mode a poll is admitted against the source job id instead of always
  being refused.
- **`aws_textract_inline_analysis` limits are capped.** `max_blocks`
  (100000), `max_result_bytes` (50 MB), `request_timeout_seconds` (120) and
  `batch_wait_timeout_seconds` (480) have ceilings, and a config above them
  is refused at load. The `batch_wait_timeout_seconds` default rises from
  420 to 480, the wait that was already in effect.
- **A per-row Textract S3 key over 1024 UTF-8 bytes** fails locally as
  `invalid_input` / `invalid_s3_key` instead of `submit_failed`.
- **`request_ssrf_safe` refuses `follow_redirects=True`** except for a
  body-less GET, raising `ValueError` before any network I/O.
- **An Azure prompt shield earns `PROMPT_SHIELD` control credit only with
  `analysis_type: both`** (explicit or as the configured default). A pipeline
  that requires a `PROMPT_SHIELD` control and uses a shield set to
  `user_prompt` or `document` alone is now refused at admission.
- **Guardrail and safety error reasons change.** AWS guardrail transforms
  report empty or over-length text as `invalid_input` (`empty_field`,
  `field_too_long`) instead of `api_call_failed`. Azure safety transforms
  stop with `retry_timeout` / `row_deadline_exceeded` once the row deadline
  has passed. Textract `blob_too_large` reports its actual size as `>max`.
  AWS guardrail transforms no longer report `guardrail_service_error`: a
  terminal failure is `malformed_response` or `service_error`, retryable
  throttling, 5xx and transport failures go to the engine retry policy
  (`transient_error_no_retry` without one), and a row can end as
  `row_deadline_exceeded` or `shutdown_requested`.
- **A Bedrock guardrail ARN that contradicts `region` is refused.**
  `aws_bedrock_prompt_shield` and `aws_bedrock_content_safety` configs whose
  guardrail ARN names another region or a non-`aws` partition now fail
  config validation with `PluginConfigError`, as the operator profile
  already did.
- **A mistyped landscape export sink is refused for every run.** A `FRESH`
  run now refuses a `landscape.export` sink name that is not in `sinks`, as
  some run purposes already did, and `${VAR}` references in
  `landscape.export` are expanded before that check.
- **`truncate` 2.0.0 requires every configured field to be a string the row
  carries.** A configured field missing from a row now fails the row with
  `missing_field` through `on_error`; it used to be skipped unless `strict`
  was set, and `strict` is gone. `fields` keys must be the normalised names
  rows carry (`notes`, not `Notes`): a header spelling is refused at build,
  or routed per row as `declared_field_is_header_spelling` when the upstream
  cannot prove it. A producer whose schema types a truncated field as `int`,
  `float` or `bool` is refused at build instead of failing every row. Remove
  `strict` from configs, and re-inspect Web Composer graphs that set it.
- **Twelve more transforms declare the row fields they read as strings**, and
  move to `plugin_version` 2.0.0: `line_explode`, `blob_fetch` (`url_field`),
  `blob_csv_expand`, `blob_json_expand`, `blob_text_expand`, `pdf_rasterize`,
  `report_assemble`, `web_scrape` (row URL, query and header fields), both
  Textract transforms, and `rag_retrieval` and `azure_ai_search` (the query
  field, when no template is set). A producer that types one of those fields
  as `int`, `float` or `bool` is refused at build instead of failing every
  row. `line_explode`, `blob_csv_expand` and `blob_json_expand` read and
  remove the field under the name rows carry, so configs that named it by a
  header spelling need the normalised name.
- **An aggregation `trigger.count` above 4096 is refused** for
  `batch_outlier_annotator`, `batch_rank`, `batch_paired_preference` and
  `batch_drift_compare`, at pipeline build and in the Web Composer. Such a
  pipeline used to fail every full batch with `batch_too_large`. Lower the
  count. An end-of-source or timeout flush over the cap is still refused when
  it runs. `TransformProtocol` and `BatchTransformProtocol` gain
  `max_batch_rows`; out-of-tree implementations that do not derive from
  `BaseTransform` must add it.
- **`batch_drift_compare` `chi_square_statistic` values change.** It is now
  Pearson's chi-square test of homogeneity over every category seen in either
  cohort, so categories absent from the baseline count toward it. Re-derive
  any threshold set on the old values; the field name and type are
  unchanged.
- **Dataverse throttling waits instead of failing.** A retryable 429 or 503
  now waits and re-sends, up to 300 s of throttle waits per request; a 429
  whose `Retry-After` exceeds 60 s still fails immediately. The source
  records one extra ERROR call per waited-out throttle, so call indexes for
  that page shift, and sink commit evidence may carry `throttle_wait_ms`.
- **`web_scrape` auth is stricter.** An `api_key` `header_name` of
  `Keep-Alive`, `Proxy-Authenticate`, `Proxy-Connection`, `Forwarded` or
  `X-Forwarded-*` now fails config validation, and a basic secret without
  `:` raises `ValueError` (without echoing the secret) when the header is
  built.
- **`web_scrape` JSON records skip nulls.** `first` returns the first
  non-null value, `one` counts only non-null values and `all` omits nulls; an
  all-null array gives `None` for `first` and `one` and `[]` for `all`, and
  provenance `selected_count` counts only non-null values. A required column
  with only nulls still fails. Pages that used to fail now extract.
- **Every `web_scrape` `fingerprint_mode: full` value changes.** The `full`
  fingerprint now covers each fetched page's HTTP status, final URL,
  Content-Type and body hash as well as the content, so the first run after
  the upgrade reports every page as changed. `content` fingerprints are
  unchanged.
- **HTTP timeouts are now a total per request.** For `web_scrape`,
  `blob_fetch`, the OpenRouter and gateway LLM transforms and sources, the
  Azure content safety, prompt shield, AI Search, Document Intelligence and
  Content Understanding transforms, Power Automate and the Dataverse client,
  the configured timeout bounds the whole request, redirects included, where
  it used to bound each network operation. Slow but steady responses that
  used to finish can now time out; raise the timeout for large or slow
  responses. A rate-limiter wait has its own bound of the same length and,
  when exceeded, is recorded as a refusal with no request sent. A
  `web_scrape` page cut off by the pagination time budget returns
  `pagination_limit_exceeded` instead of a retryable timeout.
- **LLM runs that recorded an empty completion cannot be replayed.** Replay
  refuses a SUCCESS call record with empty or whitespace-only content, and a
  null-content `ContentPolicyError` record that carries a response; re-run
  such runs live. An empty completion now raises `FilteredCompletionError`
  (a `ContentPolicyError`), `EmptyCompletionError` or, for tool calls,
  `UnsupportedResponseError` on every provider, so Gateway and Bedrock empty
  replies are no longer always content-policy errors.
- **A leading UTF-8 byte-order mark is no longer data.** Every source and
  CSV, JSON, text, S3 and Azure blob source, and every blob expand transform,
  reading `utf-8` consumes it, so it no longer prefixes the first value or
  header.
- **Power Automate archived SAS VERIFY** without `ELSPETH_FINGERPRINT_KEY`
  fails with `fingerprint_key_required`, still before DNS and preflight.
- **PDF runs recorded before this release cannot be replayed or verified.**
  The `pdf_rasterize` render request format is now `pdf_rasterize/v2`; replay
  or verify of a run recorded as `pdf_rasterize/v1` fails closed with
  `AuditIntegrityError` naming the retired format, in both the replay payload
  preload and receipt restore. Live runs are unaffected.
- **`elspeth.plugins.transforms.llm.validation` loses public names.**
  `validate_json_object_response` and its result types are removed, and
  `get_llm_audit_fields()` returns one more name (`_parts_hash`). Landscape
  `too_many_images` reason text changes.
- **`batch_threshold_summary` refuses a non-numeric threshold.** A
  `value: true`/`false` or a quoted number now fails at config load; it used
  to be coerced to 1.0, 0.0 or the number. `batch_top_k` `wrong_type`
  `expected` text changes, and batches with Decimal or datetime values, or
  extreme opposite-sign values in `batch_distribution_profile`, now succeed
  where they used to fail.
- **Bedrock guardrails validate the whole row first.** A row with any invalid
  field fails with the same payload and makes no Bedrock call, so earlier
  fields leave no billed call, call record or telemetry, and a validation
  error now precedes a shutdown or deadline result. Guardrail profile aliases
  of 65 or more characters fail at web config load, naming `alias`.
- **Textract query text and aliases** containing non-ASCII whitespace are
  rejected at config load; the provider already rejected them per row. A
  provider message mentioning `max_blocks` or `max_result_bytes` is now
  recorded as `malformed_response`.
- **Template errors on row data are value-free.** A template render failure
  whose context carries a row now reports the exception class only; error
  text that used to quote row values no longer does.
- **S3 and audit export changes.** An `aws_s3` source body with a
  non-callable `read` fails as `InvalidS3Body`. An `aws_s3` sink with
  `overwrite: true` can now replace an existing object whose checksum is a
  multipart SHA-256 composite, which used to be refused as malformed. Audit
  export preflight now fails when its scratch probe cannot be removed, and
  reconcile evidence values for the staging state change.
- **A missing core dependency stops plugin discovery.** Only `jinja2` is
  treated as optional; a missing `chromadb`, `bs4` or `html2text` now raises
  instead of silently dropping plugins from the catalog.
- **CI runs one Python 3.14 lane**, which is the coverage lane. Required
  status-check names change with it, for example
  `Test (Python 3.14, shard N)` and `Timing-sensitive tests (Python 3.14)`,
  and the repository ruleset must be updated to match.

Schema and stored data:

- No schema epoch changed and no stored data is migrated, reset or rewritten.
  Session epoch 78 and Landscape epoch 50 are unchanged from the previous
  release; this release adds no schema-epoch cutover. Existing 0.8.13 session
  and Landscape stores need no action beyond the drain above, and the 0.8.10
  cutover guidance below continues to apply to older stores. This authorizes
  no migration, reset, restamping or downgrade.

### Engine and runtime

- **Behaviour under Python 3.14 matches 3.13.** Strict JSON parsing bounds
  nesting explicitly, the runtime-validation manifest again records helper
  dependencies, and runtime preflight keeps reporting an abandoned task
  failure by class only. asyncio diagnostics that carry an exception are
  logged as `asyncio_unhandled_exception` with the exception class and code
  location only.

### Plugins

- This release carries a broad round of plugin fixes across sources,
  transforms, sinks and retrieval; the ones that change behaviour are listed
  in the upgrade notes above. New: the `azure_content_understanding`
  transform, the `azure_document_intelligence` outputs `fields_field` and
  `metadata_field`, and the `json_explode` option `index_field` (default
  `item_index`). The CSV source decodes the file strictly in binary. The LLM
  transform keeps the one-hour batch-wait default as a floor for a row's
  result, and the Azure and Bedrock batch wait covers the whole batch: the
  budget plus queries × request timeout plus 30 seconds.
- Examples: the `multi_worker`, `multi_worker_showcase` and `join_refused`
  launchers require the `sqlite3` command-line tool and exit 2 with a clear
  message when it is missing.

### Web Composer

- A reply the completion review withholds is shown as a system notice, and the
  withheld text never re-enters the conversation history sent to the model.
- Credential-shaped model reasoning is dropped and recorded as a value-free
  withhold instead of failing the turn. Stored provider-error text is scrubbed
  with the shared credential patterns; existing rows are not rewritten. Redis,
  AMQP and raw-slash URL passwords are not recognised by the shared detector
  and remain a gap.
- The version menu shows the newest 50 versions and marks validated
  milestones. `diff_pipeline` compares against the last validated state
  (`baseline_kind: last_validated`, or `request_start` when there is none).
- A reviewed interpretation slot that a per-query template bypasses is refused
  at staging with the validation code `vague_term_query_override`.
- `batch_stats`, `batch_replicate` and `batch_experiment_compare` are
  available on every install and no longer depend on an operator allowlist.
- The tutorial's Build step is script-only: after the brief is sent, the
  learner can take only the card decisions the script needs.
- A forked session's copy of a model-written file stays model-written (#365).
  Fork copies made before this fix stay VERBATIM; they are not rewritten.
- The skill text change moves `composer_skill_hash`.

### Sharing

- Cancelling a shared-pipeline import or a library fork no longer leaves an
  empty session; the session is archived on cancellation.
- Share tokens are decoded strictly. Every token issued before this release
  still verifies.
- **`elspeth purge` can no longer delete a live share's data.** Shares now
  have their own store at `<payload base>/shares`, on the same mounted volume
  with no new setting. Links created before this release keep working (reads
  fall back to the parent store), but their data stays where it is and remains
  purge-exposed; there is no migration. Never point `purge --payload-dir` at
  `shares/`.
- Copying a share link again keeps its original expiry, shortened if the
  deployment maximum has since been lowered. Once that expiry has passed, the
  request returns 409 `share_link_expired`; share for review again to create a
  new link.
- Library payloads have their own store at `<payload base>/library`. A missing
  payload is reported as `library_payload_unavailable` (republish); see the
  upgrade note above.

### Known limits

- In pooled LLM mode, time a row spends queueing is still unbounded.
- `chromadb` 1.5.5 carries four advisories with no fixed release
  (CVE-2026-45829, CVE-2026-45830, CVE-2026-45831, CVE-2026-45833). All four
  are in the Chroma server, which the image never runs. A separate limitation:
  in Chroma `mode: client`, ELSPETH does not pass its own embedding function,
  so `chromadb` builds the one named in the remote collection's stored
  configuration. Point client mode only at Chroma servers you trust.
- Bedrock's readiness check still requests 32 tokens, so reasoning models may
  fail it.
- Azure OpenAI and Bedrock LLM requests go through provider SDKs and still
  bound each network operation separately rather than the whole request.
- Dataverse sink errors are not yet diverted per row, and reconciliation does
  not normalise decimal or datetime values. The Dataverse 300 s throttle-wait
  budget is not configurable, and the client retries only 429 and 503.
- Textract verify mode does not yet normalise provider-generated `JobId` and
  `NextToken` values.
- An engine retry of a guardrail row re-scans, and re-bills, fields that
  already passed.
- A fork interrupted mid-copy across the upgrade fails closed on retry with a
  modality mismatch. Pipeline `llm`-node output is still recorded VERBATIM
  and has not been checked as a route to model-written prompt text.

### Publication and acceptance

Automatic image publication from main remains held. The 0.8.14 tag and
container publication follow successful required checks and the protected
merge. Operator signing, live-provider acceptance and deployment remain
separate steps.

---

## 0.8.13 - 2026-11-11

Composer context budget. Long Composer sessions grew until the server or the
model gateway rejected a request for size; the Composer now sheds older
context to fit an operator-set budget, or refuses with a named cause. The
dated entry and package version do not establish qualification, a published
tag or an image. Not run for this work: the PostgreSQL suites, a live
provider, replay of real sessions and end-to-end browser tests.

### Upgrade notes

Two configuration changes are breaking:

- **`ELSPETH_WEB__COMPOSER_CONTEXT_MAX_PROMPT_TOKENS` is required.** It has no
  default; the web server will not start without it. Set it to the
  prompt-token limit of the model gateway in front of the Composer. The
  shipped deployment examples carry `200000` as a placeholder.
- **The default of `composer_planner_max_request_bytes`
  (`ELSPETH_WEB__COMPOSER_PLANNER_MAX_REQUEST_BYTES`) is now 960 KiB** (it
  was 2 MiB), and it now also bounds the compose loop, not only the planner.
  A deployment that needs the old value must set it explicitly and raise the
  gateway's body cap to match.

Two optional settings are new: `ELSPETH_WEB__COMPOSER_CONTEXT_SHED_ABOVE`
(default `0.85`) and `ELSPETH_WEB__COMPOSER_CONTEXT_SHED_TO` (default `0.60`),
fractions of the token cap; `SHED_TO` must be below `SHED_ABOVE`.

Other things an operator or client will notice:

- A request that cannot fit is answered with HTTP 422 and
  `context_budget_exceeded` on both the compose loop and the planner. This
  replaces the generic failure previously shown when the planner's
  request-byte ceiling refused a request.
- `composer_skill_hash` changes: the composer skill gains one paragraph
  explaining the counts of omitted earlier messages.
- Composer call records gain a `context_budget` field. Older records without
  it read as "no shedding recorded".
- Existing sessions keep working. A session already over the high mark sheds on
  its next turn, with the omissions recorded.
- No schema epoch changed and no stored data is migrated, reset or rewritten.
  Session epoch 78 and Landscape epoch 50 are unchanged from the previous
  release; this release adds no schema-epoch cutover. Existing 0.8.12 session
  and Landscape stores need no action, and the 0.8.10 cutover guidance below
  continues to apply to older stores. This authorizes no migration, reset,
  restamping or downgrade.

### Composer and audit records

- **The Composer sheds context to fit an operator-set budget.** Before each
  compose-loop and planner provider call, the server removes context in a
  fixed order until the request fits the prompt-token cap and the request-byte
  ceiling: earlier tool results in the current turn, then earlier assistant
  prose, then escape-hatch transcript events, then the oldest earlier user
  requests. The system skill, catalog, tool definitions, current request,
  pipeline state and latest tool round trip are never removed. Shedding starts
  above `SHED_ABOVE` of the token cap, or over the byte ceiling, and continues
  to `SHED_TO`. Both ceilings are judged on the wire bytes of the request as
  sent.
- **A request that cannot fit is refused with a named cause**
  (`context_budget_exceeded`) before any provider call. If only the optional
  reply after staged review cards cannot fit, the turn completes with its
  staged work intact. Users are told when earlier conversation is no longer
  visible to the composer.
- **What was shed is recorded on each budgeted call** in the call record's
  `context_budget` field. A refused reply is recorded as a
  `composer_reply_stage_refusal` audit row holding sizes and fixed names only.
  Advisor and auto-title calls are not budgeted and store a null
  `context_budget`.

### Known limits

- Advisor and review calls are not yet bounded by the context budget
  (dta-au/elspeth#364). In a long session with the advisor enabled, such a
  call can still be rejected by the gateway for size; the advisor is then
  unavailable for that turn.
- Tool-call arguments and thinking blocks count toward the budget but are
  not shed. A turn whose arguments alone cannot fit is refused.
- Token counts are estimated from request bytes, not a tokenizer. The
  bytes-per-token ratio is the recorded `request_wire_bytes` over the
  reported prompt tokens of the session's newest successful budgeted call on
  the same model (failing that, on any model), clamped to 2.0-6.0. Until the
  session has such a call the ratio is 3.0, which over-estimates tokens; this
  includes the first turn of an existing session after the upgrade.
- If the provider still rejects a request for context length, it is recorded
  as before and there is no automatic retry.

### Publication and acceptance

Automatic image publication from main remains held. The 0.8.13 tag and
container publication follow successful required checks and the protected
merge. Operator signing, live-provider acceptance and deployment remain
separate steps.

---

## 0.8.12 - 2026-11-10

Composer tool remediation. Verification status is recorded in the pull
request. The dated entry and package version do not establish qualification, a
published tag or an image.

### What changed

- **Composer.** Refusals and no-ops go back to the model as tool feedback, not
  to an approval card; approval cards are created only for changes that pass
  admission. Under explicit approval, one composition proposal is pending at a
  time: a response carrying more than one mutation is refused whole
  (`approval_batch_not_atomic`), and a mutation is refused while a composition
  proposal is pending (`approval_proposal_pending`). This contains the defect
  and does not redesign it: auto-commit sessions and blob-only proposals are
  not covered. `diff_pipeline` answers in freeform sessions, transforms can be
  spliced into planner-built pipelines, and an oversized blob read is reported
  as a size refusal (`blob_content_size_limit`). Contradictory and stale
  Composer skill text was corrected in place without restructuring it.
- **Web interface.** The web interface takes the ARGOnite look in light and
  dark themes. The share dialog closes when the active session changes and
  states what a link grants. A pipeline that uses two files with the same
  content under different names or file types is no longer shared for import.
- **Plugins.** New `file_inject` transform: it writes the content of whole
  files, bound as configuration, into row fields. Editing a bound file between
  a run and its resume refuses the resume. In the Composer its content must be
  a file the user uploaded. A file the agent rewrites with `update_blob` is
  now recorded as the agent's file; files rewritten before this release keep
  their old record.
- **MCP server.** File paths authored over MCP are resolved against the
  configured data directory, and a path outside it is refused at admission.
  The installed CLI runs the standalone MCP server under a private supervisor
  that settles the child process on shutdown, failed start and parent death.
- **Landscape.** Creating Landscape tables on PostgreSQL no longer breaks a
  later SQLite initialization in the same process.
- **LLM gateway.** Thinking from an adapter that does not declare `REASONING`
  is refused as upstream-invalid on the first turn; an operator adapter with
  this defect will now fail earlier.
- **Dependencies.** The remaining Python dependency advisories are fixed:
  OpenTelemetry moves to 1.45.2 with the Prometheus exporter at 0.66b1,
  soupsieve to 4.0, Langfuse from SDK 3 to 5.2 and Azure Monitor to
  1.8.12 / b59. The OpenTelemetry SDK and exporter versions are exact pins;
  installs must use the updated lock rather than upgrading the telemetry SDK
  independently.

### Upgrade notes

- Langfuse tracing now requires Langfuse Cloud or a self-hosted server at
  version 4.59.0 or newer; Langfuse SDK 5 no longer supports self-hosted v3 or
  older v4 servers. Upgrade an older server before upgrading ELSPETH. The
  configuration field names remain unchanged. See the
  [Langfuse SDK upgrade guide](https://langfuse.com/docs/observability/sdk/upgrade-path/python-v4-to-v5).
- An MCP client that authors a file path outside the configured data
  directory is now refused. Move the file under the data directory or change
  the configured root.
- A pending approval for a change that is now known to be refused or to be a
  no-op settles as terminal instead of remaining pending.
- Under explicit approval a model response with several mutations is refused
  and the model is asked to send one. Sessions that relied on several
  proposals being staged from one response will see one proposal per turn.
- New refusal codes a client may receive: `approval_batch_not_atomic`,
  `approval_proposal_pending`, `blob_content_size_limit`,
  `proposal_blob_reference_malformed`.
- No schema epoch changed. Existing session and Landscape stores load
  unchanged. Session epoch 78 and Landscape epoch 50 are unchanged from the
  previous release; this release adds no schema-epoch
  cutover. Existing 0.8.10 session and Landscape stores need no action, and
  the 0.8.10 cutover guidance below continues to apply to older stores. This
  authorizes no migration, reset, restamping or downgrade.

### Publication and acceptance

Automatic image publication from main remains held. The 0.8.12 tag and
container publication follow successful required checks and the protected
merge. Operator signing, live-provider acceptance and deployment remain
separate steps.

---

## 0.8.11 - 2026-11-10

This section records changes on the 0.8.11 candidate. It does not establish
qualification, a published tag or an image.

- **A shared link can let recipients import a copy, files included.** The
  "Share inspect link" dialog offers "Let recipients import a copy" (off by
  default) and a link lifespan. The deployment's
  `shareable_link_lifetime_seconds` is now the maximum: a longer lifespan sent
  to the server is refused with 422. Links minted before this release, and
  links without the opt-in, stay view-only (403 on import). Secrets never
  travel. Only files the user uploaded can be shared for import.
- **Shared, published and exported graphs drop the whole uploaded-file
  reference.** A transform's uploaded-file reference and a `blob_rows`
  source's file entries are removed whole and named in the export's redaction
  account; YAML import refuses a leftover fragment with guidance to re-attach
  the file. `POST /api/sessions/{id}/state/yaml` accepts
  `inline_blob_bindings` and `source_blob_rows` to re-attach files, and
  `sink_path_bindings` to re-base sink paths; sink paths are not exported and
  must be set after import.
- **Known cost: share snapshots and frozen files are not reclaimed.** Every
  share, and every file frozen into an import link, stays on disk after the
  link expires. Reclamation is tracked in
  [#356](https://github.com/dta-au/elspeth/issues/356).
- **Accepted gaps in import.** A process that dies mid-import can leave a
  half-built "Copy of …" session for the recipient to delete, and a deliberate
  retry after a network failure can create a second copy.
- **The LLM gateway carries thinking blocks through tool loops.** Assistant
  messages accept `thinking_blocks`, and thinking an adapter reports is
  returned on the response message in the same shape. Replayed thinking to an
  adapter without the `reasoning` capability is refused with
  `capability_unsupported`. The reference image's `adapter-fingerprint` label
  changes accordingly.

## 0.8.10 - 2026-11-09

This entry records the Composer performance remediation and the fixes landed
with it on the 0.8.10 candidate. The dated entry and package version do not
establish qualification, a published tag or an image.

### Upgrade notes

0.8.10 changes neither schema epoch, so the 0.8.9 cutover guidance continues to
apply unchanged. Session epoch advances from 77 to 78;
Landscape advances from epoch 49 to 50. The combined package retains committed
graph-review authority over immutable graph snapshots and adds durable quota
custody, aggregate container storage, rate-limit policy binding and run
provider-attempt evidence.
Preserve epoch-75 stores, both distinct epoch-76 predecessors, epoch-77 session
stores and epoch-49 Landscape stores with compatible prior application access;
configure an explicitly new epoch-78 target and an explicitly new epoch-50
Landscape target. An existing epoch-78 session store and epoch-50 Landscape
store qualified for 0.8.9 need no action. Older readers ignore those completion
blockers or cannot admit the new quota shape. This authorizes no migration,
reset, restamping or downgrade. Drain and isolate old/new deployments; they
cannot share session stores or replicas.

- Remove any `none` value from the three composer reasoning-effort settings
  before upgrading; startup refuses it.
- A derived gateway adapter must declare `reasoning` (and translate the hint)
  to serve ELSPETH's composer. Re-run the conformance kit against the derived
  image.
- Behind a chat-completions-only gateway, do not give the composer or advisor a
  GPT-5.4+-style model alias: with tools and the hint, LiteLLM bridges such
  names to `/v1/responses`, which the gateway does not serve, and the request
  fails.
- Some models accept a temperature only with reasoning disabled (for example
  OpenAI GPT-5.1/5.2). On those routes leave `ELSPETH_WEB__COMPOSER_TEMPERATURE`
  unset.
- `deploy/compose` and `deploy/linux-systemd` boot on the `gpt-5.5` default,
  which is now hinted at the code defaults (`low` discovery, `high` candidate,
  `medium` advisor). No latency measurements exist for those surfaces yet.
- Operators who relied on an editable checkout's `.env` being loaded from
  another working directory must now start from that directory or pass
  `--env-file`.

### What changed

- **Composer and advisor must be reasoning models.** Every composer-plane
  request (compose loop, planner, escape hatch and advisor) carries a
  reasoning-effort hint, and a model, provider or gateway that cannot honour
  the hint refuses the request instead of running it unhinted.
  `ELSPETH_WEB__COMPOSER_DISCOVERY_REASONING_EFFORT`,
  `ELSPETH_WEB__COMPOSER_CANDIDATE_REASONING_EFFORT` and
  `ELSPETH_WEB__COMPOSER_ADVISOR_REASONING_EFFORT` accept `low`, `medium` or
  `high`; `none` is refused at startup.
- **LLM gateway.** The inbound Chat Completions subset accepts
  `reasoning_effort` (`low`, `medium` or `high`). A request carrying the hint
  to an adapter that does not declare the new `reasoning` capability is
  refused with `capability_unsupported` before any upstream call. The
  reference image's `adapter-fingerprint` label changes accordingly.
- **Composer performance remediation.** Proposals, the `set_pipeline` schema
  and fixed tool definitions are verified once; plugin summaries, proposal
  lists and review cards read once per request; advisory validation shares a
  brief memo that durable review records never read; and operation streams
  authorize on one connection. Every composer LLM call record carries request
  byte sizes per component. Approval binding shares the cached runtime
  manifest with run start, and the transform executor reads the recorded
  input hash. Draft hashes, canonical pipelines and recorded hashes are
  byte-identical.
- **The frontend holds Run while work settles.** Run and Retry are held, with
  a short status line, while a compose turn, a proposal decision, a resolve or
  validation is still in flight.
- **`.env` discovery follows the working directory.** Discovery now starts
  from the current working directory, as documented. `--env-file` and
  `--no-dotenv` are unchanged.
- **Dependencies.** Security updates move filelock to 4.2.1, litellm to
  1.102.2, oauthlib to 4.0.1 and virtualenv to 21.14.5; the remaining
  OpenTelemetry and virtualenv advisories are resolved in 0.8.12.

### Publication and acceptance

These notes describe the 0.8.10 source package. Final-head CI and the protected
merge remain required; this entry does not establish a published tag or image.
Automatic image publication from main remains held. The 0.8.10 tag and container
publication follow successful required checks and the protected merge.
Operator signing, live-provider acceptance and deployment remain separate steps.

## 0.8.9 - 2026-10-21

This release combines the Composer work developed under 0.8.7, Azure Container
Apps work developed under 0.8.8, and engine and runtime improvements developed
under 0.8.9.

### Upgrade notes

Session epoch advances from 77 to 78; Landscape advances from epoch 49 to 50.
The combined package retains committed graph-review authority over immutable graph snapshots
and adds durable quota custody, aggregate container storage, rate-limit policy
binding and run provider-attempt evidence. Preserve epoch-75 stores, both distinct epoch-76
predecessors, epoch-77 session stores and epoch-49 Landscape stores with compatible
prior application access; configure an explicitly new epoch-78 target and an
explicitly new epoch-50 Landscape target. Older readers ignore those completion
blockers or cannot admit the new quota shape. This authorizes no migration,
reset, restamping or downgrade. Drain and isolate old/new deployments; they
cannot share session stores or replicas.

### Changes that need action

- **Quota API quantities are decimal strings** over the positive
  signed-64-bit range; request bodies still accept exact JSON integers through
  `2**53-1`, while larger values require strings. API consumers must adapt
  response parsing.
- **Forwarded client addresses are trusted only from explicit numeric
  `trusted_proxy_hosts`;** the default trusts no forwarding headers. Proxy
  deployments must configure their actual connecting peers. The Compose
  host-nginx bundle binds port 8451 to loopback and supplies its bridge
  gateway explicitly; existing networks need recreation to change their IPAM
  settings, and named data volumes must be preserved.
- **Shared request limits bind their durable policy.** A replica with a
  different configured cap refuses admission while the shared bucket remains
  active; configuration changes take effect after its expiry.
- **Web execution refuses local SQLite database sinks and persistent Chroma
  sinks, retrieval and collection probes,** because their library-owned files
  cannot obey byte reservations. Existing graphs using those modes must
  change; standalone CLI plugin behavior is unchanged.
- **Azure Container Apps enforces authenticated PostgreSQL TLS settings.**
  Startup and deployment doctor require one explicit `sslmode=verify-full` and
  one `sslrootcert`, either `system` or an absolute CA-file path. Existing
  insecure ACA configurations must be corrected before using this version.
- **Composer and advisor protection uses Azure Prompt Shields.** Each role has
  an independent Azure AI Content Safety configuration. Missing configuration,
  blocked input or unavailable inspection refuses dispatch. Unsupported
  authoring input returns `prompt_guard_unsupported_input` with status 422;
  guard service failures retain status 503.
- **Exported row-call and operation-call records add nullable
  `returned_model`;** consumers enforcing a closed export shape must admit
  this field.
- **Advisor checkpoint budgeting relaxes the previous input cost bound;**
  calls may cost more or take longer.
- **`_sync_list_blobs` is no longer re-exported** by
  `elspeth.web.composer.tools`; code using the private facade path must update
  its import.

### What else changed

- **CSV sink retention.** Opt-in `require_all_rows`, `include_audit_context`,
  `include_error_reason` and `include_disposition_reason` let a CSV sink
  require retention and carry verified audit context; these options require a
  supporting runtime beginning with 0.8.9. See
  [CSV required-row retention](docs/runbooks/csv-required-row-retention.md).
- **LLM transforms.** Output fields can explicitly admit null
  (`nullable: true`), named `input_checks` and `result_checks` run before and
  after model requests, and multi-query structured outputs can use an explicit
  `structured_output_prefix`.
- **New `batch_duplicate_annotator`** and the expression helpers
  `starts_with`, `word_count` and `decimal_within`.
- **Landscape retains provider-attempt and returned-model evidence,** and
  configured container quotas are provisioned before serving.
- **Composer correctness and recovery.** Provider request rejections retain
  safe, actionable reasons; the planner can read a complete authorized
  attachment with `read_attachment`; native thinking survives tool
  continuations intact; and advisor reviews receive the complete available
  visible conversation evidence.
- **Azure Container Apps.** First-install instructions can be rehearsed
  offline. Offline rehearsal does not establish cloud deployment, guard health
  or provider access.

### Publication and acceptance

These notes describe the 0.8.9 source package. Final-head CI and the protected
merge remain required; this entry does not establish a published tag or image.
Automatic image publication from main remains held. The 0.8.9 tag and container
publication follow successful required checks and the protected merge.
Operator signing, live-provider acceptance and deployment remain separate steps.

## 0.8.6 - Candidate (Composer correctness and recovery)

- **Dependent queries can run in one LLM node.** `query_input_mode: ordered`
  lets explicitly bound later queries read earlier validated outputs. It
  requires uniquely named list-form queries and `pool_size: 1`. See
  [ordered LLM queries](docs/guides/ordered-llm-queries.md).
- **Final replies use the existing bounded review.** Unadmitted prose is
  retained privately while trusted saved-progress and blocker text is shown.
  Reply admission does not clear graph, pending-review or execution blockers.
- Also in this candidate: a local failure-route visibility toggle on the
  graph, bounded blob inspection with continuation, operation outcomes that
  survive refreshes, and the [YAML guide](docs/guides/yaml-authoring.md).

Session epoch advances from 75 to 76; Landscape remains at epoch 49. Committed
graph-review facts now govern effective completion status without rewriting
immutable graph snapshots or invalidating pending proposal bases. Older readers
ignore these facts, so 0.8.5 and 0.8.6 cannot share session stores or replicas.
Preserve epoch-75 stores and their compatible prior application; configure an
explicitly new epoch-76 target for this candidate. This authorizes no migration,
reset, restamping or downgrade. Drain and isolate old/new deployments rather
than relying on compatibility keys to evict existing replicas automatically.

These notes describe source changes. Focused checks and source review do not
establish final exact-head or PostgreSQL qualification, live provider acceptance,
operator signature clearance, release tagging or deployment. Automatic image
publication from main remains held. The 0.8.6 tag and container publication follow
successful required checks and the protected merge.

## 0.8.5 - LLM compatibility and Composer recovery

- **Omitted LLM temperature uses the provider default.** Common source and
  transform configurations now omit temperature when absent or explicitly
  null. Previously, non-Azure configurations implicitly sent `0.0`. Set
  `temperature: 0.0` explicitly to retain that request where supported.
  Explicit numeric settings remain unchanged; unsupported settings are
  refused without silently stripping them or retrying with different sampling.
- Also in this release: tutorial execution uses the same execution obligation
  and lease as the normal execute route, the 1,024 character per-line caps are
  removed from interpretation and source-artifact review content, and
  shared-sink routes are easier to inspect.

Session epoch 75 and Landscape epoch 49 are unchanged. Existing unsupported
stores remain preserved and refused; this release does not authorize migration,
reset or downgrade. Ordered dependent LLM queries and the quarantine/discard
visibility toggle are separate 0.8.6 work and are not included.

## 0.8.4 - Unreleased (Restricted SQL and credential lifecycle hardening)

An unreleased source candidate. Live PostgreSQL analysis has a restricted
language and database profile (see the
[analysis guide](docs/guides/landscape-mcp-analysis.md)); logout revokes the
bearer family and password changes advance its credential generation; and
Compose separates runtime and schema ownership.

**Store compatibility.** Session epoch 75 combines authentication families and
bound tickets with graph recovery. Session epoch 72 adds durable Composer jobs
and ingress bindings in the preceding candidate. Landscape epoch 49 remains unchanged.
A Landscape database already at epoch 49 remains usable if its complete schema
matches current admission. Qualification uses explicitly new compatible stores.
Unsupported session, Landscape and local-auth stores are preserved and refused
before incompatible writes. Do not migrate, recreate, delete or restamp them.

## 0.8.3 - Unreleased (Archive recovery and Composer HTTPS progress candidate)

An unreleased source candidate. Session archive and cleanup recover on
filesystems without no-replace rename; the tracked LLM gateway accepts a
64,000-token completion allowance; the Web Composer delivers live progress
over an authenticated server-sent event subscription; and the Composer chat
drops its "Auto-apply on" chip and the "Create source" offer.

**Store compatibility.** Session epoch 74 adds immutable graph parks and
operation-bound recovery checkpoints. Session epoch 72 adds durable Composer
jobs and ingress bindings in the preceding candidate. Landscape epoch 49
remains unchanged. A Landscape database already at epoch 49 remains usable if
its complete schema matches current admission. Unsupported session, Landscape
and local-auth stores must be preserved and refused before incompatible
writes. Do not migrate, recreate, delete or restamp an existing store to
satisfy this candidate; select a separately provisioned new target.

## 0.8.2 - Unreleased (Bounded validation and release-check hardening)

An unreleased source candidate. Composer validation keeps resource use
bounded, and release checks verify eligible PR-only evidence through merged
lineage.

- **Reference joins reject expressions and values that can exhaust memory.**
  Materialized reference outputs have a 64 MiB canonical-byte limit in addition
  to the existing cell limit, and output expressions that can amplify text are
  refused. Pipelines using these patterns must reduce retained values or
  simplify their output expressions before running.
- **Runtime dependencies exclude two vulnerable versions.** The base install
  requires fsspec 2026.6.0 or newer (CVE-2026-104851). The LLM, Web UI and
  all-feature extras require multidict 6.9.1 or newer, below version 7
  (CVE-2026-104874).

**Upgrade and publication notes.** Session epoch 71 and Landscape epoch 49
are unchanged from 0.8.1; this release adds no schema-epoch cutover. Installations
still on 0.8.0 must follow the paired database recreation described in the
[0.8.1 notes](#081---2026-09-10-replica-recovery-and-deployment-hardening) and
[cutover runbook](docs/runbooks/staging-session-db-recreation.md). These notes
describe source changes, not a published image. An earlier GHCR publishing
attempt was denied with `write_package`; this candidate does not establish
registry access or publication. Confirm a published tag or digest for the
intended commit before using container installation instructions.

## 0.8.1 - 2026-09-10 (Replica recovery and deployment hardening)

Replica recovery, declared data contracts and deployment hardening.
PostgreSQL-backed replicas share Composer progress, run events, WebSocket
tickets and rate budgets, and an admitted run can recover after a replica is
lost. Transform outputs use a contract declared before the first row
(ADR-050) and a template sees only its declared fields (ADR-051). The release
also adds workflow governance (approval, review, a shared library and
per-person quotas, off by default), a standalone audit-export verifier and
the `azure_ai_search` and `batch_rank` plugins.

**Breaking pre-1.0 schema cutover:** `SESSION_SCHEMA_EPOCH` advances from 53
to 71, and Landscape `SQLITE_SCHEMA_EPOCH` advances from 38 to 49.
Session epoch 71 removes mode-specific operation state and adds mode-neutral
durable receipts for session fork and state revert. Earlier Sessions stores
must be recreated; there is no compatibility reader or in-place migration.
A Landscape store created from an earlier 0.8.1 pre-release build at epoch 46
is refused at startup; recreate it. These changes share one paired cutover;
the intermediate ACA epochs are not a separate deployment requirement.

ELSPETH does not migrate either predecessor database in place before 1.0.
Archive or export required evidence, stop the old service, recreate stale
session and Landscape stores, then install 0.8.1. Session databases below
epoch 71 (including epoch 70) and Landscape databases below epoch 49 must be
recreated together.
Startup accepts an empty database or an existing database matching the exact
current schema epoch (session 71, Landscape 49); these are not minimum versions.
Preserve `data/auth.db` and follow the account re-admission guidance in the
[session DB reset runbook](docs/runbooks/staging-session-db-recreation.md).
Do not roll older code back over the recreated databases; keep the service
drained and repair this release forward.

### Breaking and operator-visible changes

- **Pipeline operations require a live deployment-wide `user` role.** An
  authenticated active human must also hold this grant to author, execute,
  replay, cancel, or obtain progress tickets; administrator status alone does
  not grant workload access. Grant the appropriate workload role when
  admitting users. See the [identity guide](docs/guides/identity-providers.md).
- **Transform outputs declare, sources infer-and-lock (ADR-050).** Recorded
  transform output contracts and dependent contract/prompt hashes change.
  Resuming an older run fails with `FrameworkBugError` at the first node whose
  recorded type differs. There is no compatibility shim.
- **Azure AI Search retrieval is its own transform (breaking).** The
  `azure_search` provider is removed from `rag_retrieval`, which now serves
  Chroma only: YAML that used `provider: azure_search` must move to the new
  `azure_ai_search` plugin, and `managed_identity_client_id` is now
  `client_id`. Web-authored pipelines reach a search service only through an
  operator profile declared in the new `ELSPETH_WEB__AZURE_SEARCH_PROFILES`
  setting, whose `indexes` pin is mandatory (`"any"` is the explicit opt-out).
  See the
  [environment reference](docs/reference/environment-variables.md#azure-ai-search).
- **Enabled audit exports must state `signing_mode`;** deployments can set
  `authentication_policy: required` to reject unsigned export configuration.
  `elspeth audit-export verify` independently re-derives signed JSON exports
  and portable CSV bundles and refuses unsigned evidence by default.
- **Composer LLM steps always carry both prompts.** Composer validation
  rejects an `llm` node without a `system_prompt`
  (`llm_system_prompt_missing`) or without a user prompt
  (`llm_user_prompt_missing`). Existing Composer sessions whose LLM step has
  no system prompt now fail validation. YAML pipelines are unaffected.
- **Azure reasoning deployments.** Set `temperature: null` on LLM nodes using
  those deployments so Azure does not reject their row calls.
- **New settings.** `ELSPETH_WEB__USER_SECRETS_ENABLED=false` disables
  user-scoped secrets end to end. `ELSPETH_WEB__EXECUTION_RATE_LIMIT` controls
  the external-call rate for web-executed pipelines; the default remains 60
  calls per minute per service. The default `server_secret_allowlist` gains
  `AWS_BEARER_TOKEN_BEDROCK`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` and
  `AWS_SESSION_TOKEN`.
- **Finite file sources can opt into recovery from sealed input.** Set
  `snapshot_for_resume: true` on a single `csv` or `json` source before the
  original live run. See the
  [resume runbook](docs/runbooks/resume-failed-run.md).
- **The "Save for review" gesture is renamed "Share inspect link".** Only the
  visible name changes.
- **Web fetches refuse more address ranges by default,** including the IPv6
  unspecified address and IPv4-embedding and special-purpose ranges.
- **Failure handling.** A failed coalesce group no longer ends a healthy run
  with exit 4, and `union_collision_policy: fail` no longer ends the run on a
  collision. Failure reasons identify the check without copying row values.
- **Known limit: one local-file sink write of more than about 12,600 rows
  still stops the run.** The json, csv, text and document sinks record every
  row's ordinal in the write's recovery evidence, which is capped at 64 KiB.

### Configuration changes to check before upgrading

These checks have no compatibility path or deprecation window. Some previously
delivered rows; others failed later, left undecided tokens, or recorded false
contracts.

| Configuration | Required action or new behavior |
| --- | --- |
| Header-spelled declarations, options, or targets | Use the final field name carried by the row. Proven aliases are refused at validation; unproven aliases route rows or fail the sink. Do not re-create an alias while its renamed field remains present. Genuine literal renames such as `{Name: Name}` remain supported. |
| Undeclared template reads or tests | Declare the fields in `required_input_fields`; RAG also declares `query_field`. Static refusals and render failures enforce the same projection. A field-scoped prompt shield requires a provable list; otherwise use `fields: all`. |
| Computed template keys, including `row.source_row[k]` | These require the explicit whole-row opt-out `required_input_fields: []`. |
| Row object methods, reserved attributes, or calls on row data | Use filters, `dict(row)`, or item access for reserved-name columns. `[]` does not enable row object methods or calls on row data. |
| Whole-row single-query prompt with no declaration | Add an appropriate declaration. Omitted declarations expose no fields. |
| Multi-query variables named `source_row` or a mapping method | Rename the variable to avoid ambiguity with the query mapping API. |
| Batch `output_mode: passthrough` without the one-row-per-input capability | Use `output_mode: transform` or an eligible plugin such as `batch_rank`. Incidental one-to-one output from `batch_replicate` or `batch_outlier_annotator` does not qualify. |
| Unknown template filters/tests or invalid `truncate` literals/arguments | Correct them before validation, including conditional and dynamically named filter/test forms. Overflowing numeric literals are refused. Row-dependent checks remain per-row. |
| LLM schema types incompatible with bound outputs | Match the bound output type; usage mappings/null require `any`. A downstream consumer of a `number` output must use `float`, not `int`, including integral answers. |
| Created-field declaration incompatible with its emitted value | Correct the declaration or value. For example, `json_explode` declaring `page: int` routes a string element instead of recording it as an integer. |
| Expression that can return a set | Use a list. Set membership and `len({...})` remain valid when the set is consumed in place. Previously, reference joins could emit unordered lists while value transforms crashed. |
| Union coalesce with certainly incompatible branch types | Declare compatible types on every branch or use distinct field names; validation reports `coalesce_union_type_incompatible`. |
| `union_collision_policy: fail` with a certain collision | Choose `first_wins`, `last_wins`, `merge: nested`, or `select`. Data-dependent collisions fail the group and let the run continue. |
| `field_mapper` with `strict` | Remove the option; either value previously changed nothing. Every mapping source is required, and missing unproven fields route through `on_error` as `missing_field`. |
| CSV wider than 1,024 columns under an observed/flexible schema | Use `mode: fixed` and declare every column. Header read now refuses unsupported inference before abandoning quarantined rows. |

---

## 0.8.0 - 2026-09-07 (Unified lineage and production hardening)

0.8.0 unifies ELSPETH's group-lineage and settlement model while carrying
forward the production-path hardening prepared after 0.7.1. It adds
scope-bound collectors, first-class document and multimodal primitives,
stronger Composer data contracts and reader-focused review surfaces, a
contributor-focused repository and Composer training package, and critical
security and recovery fixes.

**Breaking pre-1.0 schema cutover:** `SESSION_SCHEMA_EPOCH` advances from 35
to 53. Epoch 36 adds retryable blob-deletion cleanup; later epochs add the
multi-replica coordination substrate, the pluggable-SSO identity substrate
and shareable blob-read admissions. Some of the coordination tables (run-start
permits, cross-replica websocket tickets, rate-limit state and bounded cleanup
claims) are schema only: no production writer exists yet.
Landscape `SQLITE_SCHEMA_EPOCH` advances from 29 to 38. Epochs 34 and 35 make
lineage frames and paths the sole lineage truth, and epoch 38 gives
`scheduler_events` an auto-incrementing `seq` primary key that is the
authoritative replay order.

ELSPETH does not migrate either predecessor database in place before 1.0.
Archive or export required evidence, stop the old service, recreate stale
session and Landscape stores, then install 0.8.0. A Landscape database below
epoch 38 is not current for 0.8.0 and must be recreated. Do not roll older code back over
the recreated databases; keep the service drained and repair this release
forward.

**Breaking operator setting rename:** `ELSPETH_WEB__TUTORIAL_LLM_PROFILE`
becomes `ELSPETH_WEB__DEFAULT_LLM_PROFILE`, and `WebSettings.tutorial_llm_profile`
becomes `default_llm_profile`. The setting never designated a tutorial-owned
profile; it names the deployment's standard profile, which the first-run
tutorial happens to use. There is no compatibility alias, so any deployment
setting the old variable must change its environment before upgrading, and the
protected AWS acceptance env-var contract moves with it or the scenario binding
hash no longer matches the deployed task definition. Leaving
`default_llm_profile` unset is a supported degraded-readiness state — ordinary
pipelines and explicit profile authoring remain available while the first-run
tutorial reports that no standard profile is configured — but a default that
names an unconfigured profile is rejected at startup.

**Breaking API rename:** `WebLLMProfileSettings` becomes `LLMProfileSettings`
and `RuntimeWebLLMProfile` becomes `RuntimeLLMProfile`, with the alias and
secret-reference regexes promoted to public `PROFILE_ALIAS_PATTERN` and
`SECRET_REF_PATTERN`. No compatibility aliases or re-exports remain.

### Removed or changed behaviour

- **Bearer tokens from the legacy `oidc` and `entra` providers are held to the
  same envelope as an ID token:** `exp`, `iat`, `iss`, `sub` and `aud` are all
  required, with a 60-second clock-skew allowance, so a provider that omits
  `iat` or `sub` from its access tokens is refused where it was accepted
  before. The header-driven bearer decoder is removed.
- **The Cognito access-token mode (`oidc_audience_claim="client_id"`) is
  removed and refused at startup;** Cognito registers as a confidential client
  through the SSO profile.
- **Composer pipeline recipes are removed:** the `list_recipes` and
  `apply_pipeline_recipe` tools and the five bundled recipe templates.
  Pipeline structure is authored by the planner through `set_pipeline` and the
  graph-mutation tools.
- **Web Textract authoring uses operator document profiles (ADR-036).** Web
  deployments grant document locations through the operator-declared
  `aws_textract_profiles` settings section. CLI/YAML `bucket_field` pipelines
  are unchanged.

### Major changes

- **Deployment profiles.** The release adds Docker Compose/PostgreSQL and
  native Linux systemd bundles, retains the AWS ECS acceptance controller, and
  adds a provider-neutral `elspeth doctor` for deployment readiness checks. A
  new AWS stack comes from the tracked AWS ECS Terraform cold-install package;
  the Azure Container Apps Bicep bundle ships in
  `deploy/azure-container-apps/`.
- **One durable lineage model.** Forks, expansions and barriers share typed
  lineage-frame paths, and new `scopes:` and `collectors:` configuration
  closes expansion groups under explicit `require_all` or `best_effort`
  policy. Correlated `row_union` barriers are first-class Composer topology.
- **Document and multimodal transforms.** `pdf_rasterize`, `blob_json_expand`,
  `blob_text_expand` and `reference_join` are added, and LLM sources and
  transforms support typed structured output.
- **Pluggable single sign-on and one identity substrate.** Every login, local
  or SSO, resolves to one `identities` row that carries admission and quota.
- **Landscape coordination.** Custody, liveness and takeover decisions read
  the Landscape database's clock (ADR-047). A Landscape mutation carries the
  authority it is written under (ADR-048): the run-lifecycle, sink-effect,
  checkpoint and audit-export verbs take a required `CoordinationToken`. This
  model is not complete in 0.8.0: the data-flow, execution and scheduler
  repository families still write under a plain run id, and completion is
  targeted for 0.8.1.
- **Critical fixes** include: blob deletion cleanup remains retryable after
  metadata commit, every token reaches a lifecycle answer when a run dies
  (ADR-038), and generated multiline text is no longer silently discarded
  (ADR-039).

---

## 0.7.1 - 2026-07-23 (Recoverable effects and Composer proposal-validation coverage)

0.7.1 makes both pipeline publication and web authoring recoverable. It adds a
durable external-effect protocol for built-in sinks and audit exports, closes
the largest guided/freeform Composer capability gaps for complex DAGs, and adds
the supported AWS ECS deployment profile. The notes below intentionally cover
only release-level changes and critical correctness or security fixes.

**Breaking pre-1.0 schema cutover:** `SESSION_SCHEMA_EPOCH` advances from 26
to 35, guided checkpoints advance from schema 7 to 10, and Landscape
`SQLITE_SCHEMA_EPOCH` advances from 22 to 29. ELSPETH does not migrate either
predecessor database in place before 1.0. Archive or export required evidence,
stop the old service, recreate stale session and Landscape stores, then install
0.7.1. Do not roll older code back over the recreated databases; keep the
service drained and repair this release forward.

- **Durable, replay-safe external effects.** Built-in file, object, database,
  Dataverse, and Chroma sinks reserve and persist an immutable publication
  plan before external publication, fence the active worker, and reconcile
  uncertain outcomes before retrying. Audit exports use the same effect
  coordinator. Operators resume a recovered export with
  `elspeth export-resume <run-id> --execute`; see the
  [sink-effect recovery runbook](docs/runbooks/sink-effect-recovery.md).
- **Shared Composer proposal and validation contract.** Guided-staged
  authoring covers seven of nine maintained parity fixtures through the
  shared proposal and validation contract used by freeform and guided-full
  authoring.
- **Supported AWS ECS runtime profile.** See the
  [AWS ECS deployment runbook](docs/runbooks/aws-ecs-deployment.md).
- Also: durable Composer operations, a principal-bound web plugin policy, and
  first-class structural queues and text source and sink endpoints.

## 0.7.0 - 2026-07-09 (LLM-primary guided pipeline creation)

Guided pipeline creation becomes LLM-primary: each stage of a pipeline
(source, sink, transforms, then the final wiring) is driven by a language
model, and the flow ends at a wiring stage gated by an advisor sign-off. The
untagged 0.6.1 maintenance line is folded into this release.

- **The web session database and Landscape audit database both reset on
  upgrade.** `SESSION_SCHEMA_EPOCH` advances to 26 and `SQLITE_SCHEMA_EPOCH`
  advances to 22. Before first start on 0.7.0, stop the service, archive and
  remove `data/sessions.db` plus sidecars and the configured Landscape audit
  DB plus sidecars, then restart so both schemas are recreated. `data/auth.db`
  is separate, so local user accounts survive.
- **Ship a frontend dist rebuilt from this release's source.** The web UI is
  served from `src/elspeth/web/frontend/dist/`, which is built out of band
  (`cd src/elspeth/web/frontend && npm run build`).
- **CLI/MCP audit database defaults consolidate under `data/`.** Ad-hoc CLI
  and MCP Landscape defaults now point at `data/audit.db` instead of the older
  `state/audit.db` path.
- **Key Vault `vault_url` is restricted to approved Azure endpoints**
  (`*.vault.azure.net`), with an optional exact-URL pin via
  `ELSPETH_KEYVAULT_ALLOWED_VAULT_URLS`.

---

## 0.6.0 - 2026-06-20 (cross-process multi-worker run coordination)

Multiple cooperating processes on one host may operate against a single run
backed by one WAL SQLite audit database: one leader and any number of
claim-only followers attached through the new `elspeth join` entry point
(ADR-030).

- The audit database schema epoch advances to 21; operators delete the prior
  database before first run on this version.
- The web session database also resets on upgrade: its schema epoch advances
  to 19 and it is not migrated in place. Before first start on 0.6.0, stop the
  service, back up and remove `data/sessions.db` (and its `-wal`/`-shm`
  sidecars), and restart. `data/auth.db` is a separate file, so local user
  accounts survive the reset.
- Ship a frontend dist rebuilt from this release's source.

---

## 0.5.4 - 2026-06-20 (maintenance fixes folded into 0.6.0)

Maintenance fixes that shipped as part of the 0.6.0 release rather than as a
standalone 0.5.4 tag.

---

## 0.5.3 - 2026-06-08 (RC-5.3 — Correctness, Audit Integrity, and Release Gating)

A correctness and hardening release on top of the RC-5.2 composer train:
operator-set sampling, a release pipeline in which images cannot publish ahead
of their required checks, the orchestrator decomposition and a broad body of
audit-integrity, trust-tier and output-contract fixes. No schema migrations
are introduced. The advisor is now mandatory, so a composer session can no
longer run un-reviewed. The Docker base moves from `python:3.12-slim` to
`python:3.13-slim`.

---

## 0.5.2 - 2026-05-19 (RC-5.2 — Guided Composer, Durable Progress, and Recovery UX)

The large Web Composer release train: the guided-mode wizard, composer
progress persistence, manifest-keyed redaction, interpretation review and
frontend recovery UX.

- The session database requires recreation (stop the service, archive the old
  `sessions.db`, restart), and the session DB and Landscape audit DB are reset
  together on schema cutover.
- `chromadb`, `html2text` and `beautifulsoup4` are mandatory dependencies; the
  `web` and `rag` extras were retired.

---

## [0.5.1] - 2026-05-11 (RC-5.1 — Composer Correctness, Validator Hardening, and Audit-Integrity Coverage)

A correctness and assurance follow-up to RC-5: Web Composer authoring-loop and
validation hardening, the `identity_node_advisory` validator, and `data_dir`
resolved to an absolute path at validation time.

## [0.5.0] (RC-5 — Web UX Composer + Systematic Hardening)

The web application platform for chat-first pipeline composition: three-provider
authentication, session management with versioning, blob storage, secret
management, background pipeline execution with WebSocket progress and a React
frontend. Also the sink failsink pattern and the pipeline composer MCP server.

## [0.4.1] (RC-4.1 — RAG Ingestion Pipeline)

RAG ingestion: ChromaSink for vector store population, pipeline `depends_on`
for run sequencing, commencement gates and readiness contracts on retrieval
providers.

## [0.4.0] (RC-4.0 — Plugins, Contracts, and Correctness)

Dataverse and RAG retrieval plugins, output schema contract enforcement and
the audit provenance boundary. Field normalization became mandatory (the
`normalize_fields` toggle was removed from `CSVSource`, `AzureBlobSource` and
`DataverseSource`), and ChaosLLM, ChaosWeb and ChaosEngine moved to the
external `errorworks` package.

## [0.3.4] (RC-3.4 — Systematic Hardening)

A hardening sprint with no new features: audit integrity, deep immutability,
construction-time validation and exception hygiene.

## [0.3.3] (RC-3.3 — Architectural Remediation)

A remediation sprint driven by full architecture analysis: audit integrity
hardening, layer enforcement, LLM plugin consolidation and the
orchestrator/processor decomposition.

## [0.3.0] - 2026-02-22 (RC-3.2)

Schema contracts, declarative DAG wiring, `PipelineRow` and strict typing at
audit boundaries. The gate plugin subsystem was removed (routing is
config-driven only) and all Alembic migrations were deleted.

## [0.1.0] - 2026-02-02 (RC-2)

Initial release candidate. Core SDA pipeline engine with audit trail,
plugin system, and CLI.

## Historical Changelogs

The root changelog is the visible release-history document. Earlier RC-1 and
RC-2 fragment files were removed during the repository cleanout because their
useful history is represented here and in git history.

<!-- Comparison links — tags created at release time -->
[0.5.1]: https://github.com/dta-au/elspeth/compare/v0.5.0-rc5.0...v0.5.1-rc5.1
[0.5.0]: https://github.com/dta-au/elspeth/compare/v0.4.1-rc4.1...v0.5.0-rc5.0
[0.4.1]: https://github.com/dta-au/elspeth/compare/v0.4.0-rc4.0...v0.4.1-rc4.1
[0.4.0]: https://github.com/dta-au/elspeth/compare/v0.3.4-rc3.4...v0.4.0-rc4.0
[0.3.4]: https://github.com/dta-au/elspeth/compare/v0.3.3-rc3.3...v0.3.4-rc3.4
[0.3.3]: https://github.com/dta-au/elspeth/compare/v0.3.0-rc3.2...v0.3.3-rc3.3
[0.3.0]: https://github.com/dta-au/elspeth/compare/v0.1.0-phase1...v0.3.0-rc3.2
[0.1.0]: https://github.com/dta-au/elspeth/releases/tag/v0.1.0-phase1
