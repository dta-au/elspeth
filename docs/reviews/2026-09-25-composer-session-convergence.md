# Composer session convergence repairs

Session: `ed3c015b-a2f7-4322-9f39-1716541c5796`. Initial base: `ee96258ca` on
`release/0.8.1`. This report distinguishes historical evidence, deterministic
regressions, and real-provider acceptance. Integration verification and the
maintainer-requested ten-workflow regression battery are in progress.

## Historical evidence

A SQLite backup of the live session, inspected read-only, contains 80 messages
and 24 provider calls: 23 successful and one cancelled. Nine `preview_pipeline`
calls and one `list_blobs` call failed argument validation. The first execution
processed six rows successfully. The later draft exposed an `Any` to `str` edge
contract failure only after interpretation reviews were resolved. Its error used
compiled node IDs, causing the planner's first patch attempt to fail.

The cancelled call lasted 123,083 ms and advertised all 42 tools. It was not the
reply-only call. Historical progress state was not retained, so the missing event
found in the reply-only path cannot explain that particular wait.

## Repairs and measured controls

### Strict parameterless tools

Live tests disproved the proposed `strict:false` workaround. Omitting the strict
stamp only on parameterless tools also failed when other tools remained strict.
Raw HTTP reproduced the problem before LiteLLM. Full non-strict automatic tool
selection succeeded; forced selection was a separate confounding factor.

`tools/wire_projection.py` now gives strict parameterless tools exactly
`{"_elspeth_no_arguments": true}` on the wire. The decoder validates that exact
object and returns the canonical empty argument object. Missing, false,
wrong-typed and extra properties fail. This changes no pipeline semantics. The
strict partition remains 32/10; non-strict request bytes remain unchanged.

Production-schema live acceptance, using all 42 tools and automatic selection
through OpenRouter with Together pinned, passed two previews and two blob-list
calls. All ten parameterless tools passed local codec round trips and 50 negative
shape checks. The production source fingerprint was unchanged during this probe.
The provider-generated historical stray key is unknown and is not persisted.

Focused coverage: 438 tests passed. A mutation accepting numeric `1` as `true`
failed the negative controls. Non-strict tool bytes match the base: 63,872 bytes,
SHA-256 `6c7f60dde72b3858b93f3fa2eaebc5700663d341d8f1e362fdb8b8bfd962e499`.

### Argument-error repair

`tool_error_payloads.py` supplies closed repair instructions from the declared
schema or wire decode plan. It never echoes unknown argument names. Both the
compose loop and pipeline planner recover from malformed marker calls; discovery
rejections consume discovery budget and do not poison corrected-call cycle checks.
Audits retain closed failure facts rather than unknown key names or values.
Rejected wire calls also use the same closed placeholder in subsequent provider
requests on both callers. Corrected wire calls retain their exact framing. The
planner gives an identical-error hint after three failures and ends the attempt
after six, independently of its wider discovery budget; successful discovery and
candidate-repair transitions reset that sequence.

The earlier schema repair detail did reach the model: persisted rejection events
reconstruct a smaller payload, which explained the misleading bare historical
record. Existing anti-anchor guidance and turn budgets bound repeat errors.

Focused results: 44 schema-repair tests, 83 wire/dispatch tests, eight strict-flip
tests, and 81 planner tests passed. Reverting correction text, budget attribution,
and planner decoder recovery made their respective regression controls fail.

### Actionable graph errors

`core/dag/graph.py` captures authored endpoint names from owned graph mappings
before an edge-contract failure unwinds. Execution diagnostics use those names
for repair tools and retain compiled IDs only as secondary detail. No graph or
plugin object is retained in the exception.

The actual complaint-triage graph reproduced the failure, with valid earlier and
repaired draft controls. 698 affected graph/validation tests and 12 diagnostic
parity tests passed. Removing captured names failed both authored-ID controls.

### Validation before interpretation handoff

Pending wording reviews no longer hide independent graph failures. Mutation
feedback and review-card admission run interpretation-tolerant preflight; only
its failures are published, so a masked green never authorizes execution.
Terminal paths withhold new review cards until the graph is sound. Fresh advisor
verdicts and publication remain authoritative, and preflight shares the existing
compose deadline. The server validates and rejects; the planner authors repairs.
Preflight runs outside the session-aware tool-handler crash boundary. Its typed
failure preserves partial state and audit records; deadline expiry propagates to
its existing owner instead of being mislabeled as a plugin crash.

94 focused preflight/dispatch tests and 69 review-dispatch tests passed. An
isolated source mutation disabling the guards failed all three invalid-graph
cases while three valid controls passed. Two advisor controls passed; independent
mutations caught both dropped verdicts and ignored expiry.
The review-boundary follow-up passed 26 focused tests and three final exception
controls. Removing the exception boundary failed both typed-preflight and
deadline controls. Planner replay and repeated-argument recovery passed 461
affected tests; redaction, retry-cap and reset mutations each failed their tests.

### Empty first-turn recovery

The live replay exposed another blocker: the ordinary compose loop could return
prose claiming it had bound generated data while no tool had run and no source
existed. `service.py` now gives that structurally empty, build-related turn one
neutral retry through the provider, within the existing deadline and repair
budget. The message reports actual state and preserves explanation-only or
revoked requests. It authors no graph. Specific uploaded-blob recovery retains
precedence. Greetings add no call; eligible turns add at most one.

154 affected tests and 16 final controls passed. Disabling recovery and allowing
an extra retry independently failed the controls. Fresh live acceptance of this
additional repair produced the requested six-row generated CSV, persisted and
bound to the source with matching downloaded-content hashes. That sample used
tools on its first call, so it does not prove that the new retry fired. Seven
audited provider calls cost $0.1105702636; three interpretation reviews remained
pending. Later live explanation-only and revoked-build controls passed: neither
created a graph, blob, or proposal. Their runner exited zero; these controls used
the `6bb7c62fb` source snapshot.

### Explicit repair after review resolution

Graph blockers expose **Ask composer to repair** in the freeform editor. This
sends current validation diagnostics through one ordinary composer turn while
preserving accepted interpretations and user requirements. Review resolution,
rendering and revalidation add zero provider calls; the explicit action invokes
the existing provider-backed loop. No server-authored graph is introduced.

325 frontend tests passed, including a real-store test with controlled API
responses. Removing the action wiring failed its regression. Type checking,
linting and the coordinated frontend production build passed.

### Long model waits

The UI reports a model call in progress and measures elapsed time from the
server's progress timestamp, preserving age across remounts. The reply-only
provider path now publishes the missing `calling_model` event. No additional
provider call is introduced. Backend and frontend mutation controls failed when
the event and timestamp behavior were independently removed; nine backend and
40 frontend focused tests passed.

## Full live replay

The isolated replay at `4f6d47149` used the original two prompts, a new database,
the recorded planner/advisor models, and the authorized OpenRouter credential.
No production session records were sent. The runtime `sonnet` alias was bound to
`anthropic/claude-sonnet-4.6`; the original session records only the alias, so
exact historical runtime-model parity is unproven.

The first turn, served by Krea, returned generated CSV prose and falsely claimed
a bound source, but persisted no blob or pipeline. The trusted empty-state notice
was correct. This is a measured first-turn convergence failure, not a pass. It
prompted a separate bounded neutral retry repair and deterministic regression.

The unchanged second request, served by Together, built the classifier, lookup
and CSV pipeline in four planner calls plus a clean advisor call. After three
review cards were accepted, strict validation passed with no blockers. Execution
`83fa2fda-7a10-47a1-9a86-73132cc66bee` completed in approximately 14 seconds:
six rows read, processed and succeeded, zero failed/rejected/discarded/quarantined,
and closed accounting. The output reader verified every category and SLA pair
and the exact ordered four columns. No repair prompt or manual graph edit was
needed for that build.

A third, explicitly read-only preview request exercised the repaired tool inside
the real loop: strict marker conformance passed, preview succeeded, and state
remained unchanged at version 5. Progress polling observed the stable start
timestamp while a model call elapsed and a new timestamp on the next call.
This verifies the frontend's data contract, not browser visual acceptance.

The replay's nine Composer calls reported $0.116335761. Startup added $0.0060354
known cost and two five-second cancelled probes without cost data. Seven runtime
LLM calls succeeded (six row calls and one operation-scoped preflight), with
1,793 input and 53 output tokens; their cost was not measured. Earlier codec
experiments used 21 calls and reported $0.033576972. These figures are measured
subtotals, not a complete billing total. Production source hashes were unchanged
throughout the replay, and its isolated service was stopped cleanly.

## Expanded live acceptance

At `4c4294399`, the full-palette AUTO Together canary passed all ten parameterless
tools. Its production and harness fingerprint remained unchanged; ten physical
provider requests reported $0.006701724. This is a wire-contract check, separate
from executing pipelines.

The ten-workflow runner's first attempt exposed an isolated-service configuration
gap: six optional transform plugins were disabled by the default Web policy.
The first cleanup case completed but correctly failed its output oracle for
untruncated notes and the missing `truncate` transform. The second case was
stopped after its in-flight turn finished. These are not acceptance passes.
The original artifacts remain under `live-battery-final` in the lane. That
service stopped cleanly with a frozen source fingerprint; its 44 total requests
reported $0.355126269, including the canary and rootless probe above. The revised
test service explicitly enables the required optional plugins, retaining the
same global provider ledger across the restart. No deployed policy changed.

The second attempt at `6bb7c62fb` exposed two acceptance-harness defects and one
production warning defect. The cleanup graph was previewed successfully, but
bookkeeping and metadata saves changed its record ID. The harness incorrectly
required that ID to remain unchanged. It now binds the successful preview to
ordered executable content, retaining controls that reject changed nodes,
options, sources, and outputs. The numeric-quarantine run correctly published a
successor quarantine artifact, while the harness attempted to download its old
24-byte descriptor from the current 34-byte URI. Selection now follows the full
finalized sink-effect predecessor chain, rejects an incomplete manifest, and
still requires the download endpoint's content hash check. These failures remain
recorded as failed attempts, not retrospectively declared live passes.

The keyword workflow exhausted discovery after receiving a false warning about
a missing `keywords` option. The real option is `blocked_patterns`. The same
warning inventory had a stale `json_explode.field` name and wrongly required a
top-level LLM template when individual `queries` supplied templates. All three
were corrected against actual plugin parsers: 605 focused tests, 20 structural
checks, and three revert controls passed. The request budget itself was working.

That attempt stopped with 94 completed requests, no unfinished requests, and
$0.8025377984 in reported cumulative cost. The next attempt used an immutable
`e32366035` checkout, carried that ledger forward, and raised the request ceiling
to 400 while retaining the $10 known-cost stop.

At `e32366035`, eight workflows passed through actual execution and output/audit
checks: cleanup/edit, keyword routing/edit, reference/default edit, complaint/SLA,
typed LLM extraction/routing, grouped frequency, JSON expansion, and multiline
expansion. The two LLM cases resumed only after their exact prompt cards were
inspected and approved. All six complaint rows and all three extraction rows
matched their expected values, types, and routes.

The numeric case preserved and routed all five rows, but its observed quarantine
schema produced `id,price,qty`, failing the stricter `id,qty,price` oracle. The
request had specified success-column order explicitly and quarantine order less
clearly. Runtime behavior followed the authored schema. CSV assistance also
incorrectly advised using `headers` to pin order; it now distinguishes display
names from fixed `schema.fields` order. The fixture's quarantine order is now
explicit, with expected outputs unchanged. A fresh live retry remains required.

The fork/coalesce case exhausted discovery immediately after its final successful
preview. Its nine planner turns comprised six discovery and three composition
turns, including a correctly charged malformed mutation. A subsequent public
strict-validation request passed all checks with execution and completion ready.
The runner's generic HTTP-error bucket does not make this a harness defect: the
product prevented finalization. The repair now permits one reply-only planner
transition after an actual successful current-state final preview, retaining
the original deadline, counters, advisor and completion gates. Missing, invalid,
stale or error-bearing evidence does not qualify, and returned tools cannot
execute. All 559 affected tests and 20 structural checks passed; three real
mutations were caught and the 15 final controls passed after restoration.
Execution acceptance for this case remains outstanding.

The service stopped cleanly with unchanged production/harness hashes. Its ledger
ended at 213 completed requests, zero unpriced or unfinished requests, and
$1.5147100296 cumulative reported cost. The aggregate runner exit remains 1 because
the two failed cases are preserved. This sample is not a ten-of-ten pass.

## Related identity and error-boundary repairs

The [systems review](2026-09-25-composer-identity-systems-review.md) distinguishes
checkpoint identity, content evidence, and transition authority. The cross-turn
repair ledger now ignores bookkeeping version changes while retaining authored
content and user/session/settings/plugin/control context. Runtime validation
still runs independently. Verification includes 155 affected tests, 108 restored
checks, and a mutation that made four bookkeeping-version controls fail.

Successful mutations are now audited before interpretation-tolerant preflight.
A later infrastructure failure retains the successful mutation/version and its
typed failure cause, calls, and failed-turn metadata. It no longer relabels the
tool as a plugin crash. Forty-three focused tests and 20 structural checks passed;
removing the fix failed all three mutation controls.

Narrative results now use the explicitly selected run and retain that identity
on loaded summaries and download targets. Completed history no longer depends
on a currently active execution, and late responses from a previous run cannot
populate the new selection. All 190 affected frontend tests, two revert controls,
type checking, lint, and the production build passed.

Guided proposal anchors now advance atomically with equivalent checkpoints,
including per-tool persistence and ordinary proposal acceptance. Each caller
retains its exact operation authority; changed content or review facts are
rejected, and a live confirmation cannot lose custody. Positive, rollback, stale
authority, and PostgreSQL contention controls passed independent review. The
stored event reasons require session epoch 68; final integration verification
remains in progress.

The maintainer-requested [read-only guidance audit](2026-09-25-composer-guidance-audit.md)
reviewed all 56 registered plugins and the 42-tool Composer palette. It records
eight findings with controlled evidence and scope limits. Parent-reviewed
corrections align discovery and guided teaching with actual option names,
review ownership, emitted fields, blank-line handling, schema rejection, and
source-review/default behavior. Independent review approved all eight corrections:
512 affected checks passed, followed by 132 controls covering the final CSV
post-call wording, 156 catalog checks, and 20 structural gates. Old-guidance
mutations were rejected. Generated CSV provenance passed 106 checks with two
existing retired-case skips; expected runtime output values were unchanged.

The narrative artifact follow-up offers a download only after its existing
preview endpoint verifies the artifact bytes. Historical cumulative descriptors
that fail that check cannot become the convenience download target. The unchanged
download endpoint still verifies integrity. All 193 affected frontend tests,
mutation controls, type checking, lint and the production build passed.

## Scope and deployment

Refresh survival is explicitly parked by the maintainer for the streaming UI
redesign. No disconnect or explicit-cancel lifecycle change is included. There is
session epoch 68 for the guided checkpoint event reasons; the old closed reader
cannot accept them. Existing session stores require the documented pre-release
recreation procedure before a future deployment. Frontend changes require
rebuilt assets; the task build completed. The original session and deployed service were not
modified by the diagnostic probes.

Detailed lane evidence, command logs, exit codes, read-only snapshot and live
probe results are under `.claude/lanes/session-ed3c015b/` in the main checkout.
These local diagnostic artifacts are intentionally not committed and may contain
session data. Final suite, broader live battery and release-containment results
will be added after they finish.
