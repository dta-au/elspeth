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

## Final production-tree acceptance

The epoch-68 attempt at `261e23f2967b36237df388912d5af7d4e088f52a` executed all
ten workflows. Nine passed the complete battery; fork/coalesce produced the
expected output but exposed another incorrect lineage assertion in the harness.
The original failed checkpoint remains intact. Production, dependencies and
scenario fixtures are unchanged by its correction in `7d2ae7cbe`.

The incorrect predicate required joined sink tokens to have path `coalesced`.
After downstream transforms, the runtime legitimately records `default_flow`;
the durable join identity and two same-row fork parents remain intact. The
checker now follows that identity and requires exactly one known outcome for
every emitted token before checking row coverage and parent pairing. Both
terminal and downstream joins pass; missing, duplicate, unknown and mismatched
lineage fails. Independent review approved 156 affected checks, 28 restored
controls and two caught source mutations. The complete corrected checker also
accepts the unchanged captured run, whose 12 tokens all have terminal outcomes.
A fresh fork-only retry at `7d2ae7cbe` then exposed a separate runtime defect:
an observed CSV source feeding `type_coerce` with explicit flexible fields
passed all 24 preflight checks, but its first row raised
`SchemaConfigModeViolation` for `id` and `x` field metadata. It failed before
forking, so the lineage correction is not the cause. The earlier successful
graph used a source with explicit fields. Runtime repair and final acceptance
remain outstanding; this failed run is preserved separately as `live-fork-final`.

The nine passing workflows required no runner-triggered repair turns. The
numeric quarantine output now has the exact required column order. All nine
row-bound Sonnet calls succeeded: six complaint classifications with SLA
enrichment and three typed extractions with boolean routing. The extraction
case exercised an actual amendment: the operator replaced an invented urgency
criterion with extraction of the notes' explicit urgent/not-urgent assertions,
then inspected and accepted the unchanged prompt structure. The executable
prompt contained the exact approved definition, with no placeholder. A separate
control confirms genuine prompt-structure edits still invalidate old approval.

All 37 parameterless workflow tool calls succeeded and were wire conformant.
The final wire-only Together canary passed all ten tools with the full 42-tool
palette. The workflow planner calls were served by Novita; those two forms of
evidence are distinct. One GLM advisor request timed out at 60 seconds and the
bounded retry recovered, so this is not a claim that every provider attempt
succeeded.

The service stopped cleanly with unchanged before/after source hash
`e631eb5d43b1071539c1c0b5dfc1a92134502c5fff10d5b05a1285f622ff8755`.
Its cumulative ledger records 353 completed requests, no active requests,
$2.2561200860 in reported cost, and one unpriced timed-out request. That ledger
was carried unchanged into the fork retry. That retry stopped cleanly with
371 completed cumulative requests, $2.3622366774 reported cost, one historical
unpriced request, and unchanged source hash
`59d8660802acb77230f045a7f93e059800149725854017fdaf51d296ed7e9620`.
These are measured acceptance samples, not a statistical guarantee of convergence.

## Runtime producer contracts found by the expanded battery

The fresh fork retry isolated a producer defect in `TypeCoerce._build_output_contract`:
an observed source's optional field metadata survived a transform whose validated
explicit schema guaranteed those fields. The emitted values and converted types
were correct, but the output metadata contradicted the plugin's own declaration.
The engine correctly rejected it. The producer now reconciles declared presence
and nullability while preserving converted types, original-header aliases, and
undeclared fields. No engine validator or shared transform behavior was relaxed.
The real CSV-to-Orchestrator regression and controls passed as part of 155 checks;
removing the repair restored four failures. An observed source with explicit
`required_input_fields` still fails its independent static-presence check.

The sibling investigation found that `value_transform` could retain the same
incorrect presence metadata and could advertise an input type after an expression
changed that type. Computed fields now declare presence without an inferred type;
both its declaration and generated output model expose that fact to graph checks.
Forwarded fields retain their actual types and aliases while adopting validated
presence/nullability declarations. Null expression results retain truthful runtime
contracts. A typed downstream consumer must establish the computed type, for
example with an explicit `type_coerce`. The input schema and engine checks are
unchanged. Independent review approved 193 final focused checks and three exact
scenario/oracle checks, after 664 earlier affected checks. Four source mutations
proved the regression controls. All 56 registered plugin hashes match; only three
ValueTransform source-hash records changed in the scenario manifest, with runtime
oracle data unchanged. Final integrated tests and all ten live workflows must run
after these shared runtime changes; the earlier nine passing cases are historical
controls.

The review also reproduced separate, pre-existing limitations when configuration
uses original CSV header aliases as write targets. `type_coerce` can declare a
literal original header while updating its canonical field, and `value_transform`
can create a duplicate original-name mapping. Canonical write targets and original
header reads are covered by the current repairs. Original-header write targets
remain unresolved: configuration-time normalization cannot safely infer upstream
field mappings or whether an authored target names a new field. No alias rewrite
or declaration-check bypass is included, and these cases are not claimed fixed.

A second existing limitation is numeric admission: Pydantic accepts an integer
for a float input schema, while the executor preserves the original payload and
the output declaration verifier requires exact metadata. The new controls retain
the integer payload and integer contract and confirm the verifier still refuses
that contradiction; real float input passes and string input rejects. The repair
does not falsely stamp float metadata onto an integer, coerce pass-through values,
or broaden shared validation. This admission/declaration mismatch remains open.

## Selected-field type proof and CSV reference teaching

The next complete attempt at `e0ea1c8735aba09f4face3ab3ab29e5a78208b88`
passed nine workflows, including fork/coalesce. Complaint/SLA failed at the CSV
sink: all six native LLM responses were valid JSON with the correct categories,
but `response_sla_hours` arrived as a string while the sink required an integer.
The raw CSV lookup expression produced strings, and the observed, select-only
`field_mapper` preserved them. Strict preflight had reported all schemas compatible.
The original failed checkpoint and run remain intact.

The validator already checks sink edges, and `reference_join` already declares
the CSV lookup field as `str`. A direct edge to the integer sink correctly fails.
The intervening projection erased that proof because type resolution followed
whole-row forwarding but not a selected field. The narrow repair reuses existing
declarations: the transform preserves values, requires this same input field,
guarantees it on output, and neither creates nor removes it. Own declarations,
including explicit `Any`, retain precedence. Unknown, conflicting, diverted,
renamed and ambiguous paths retain their previous treatment. No plugin capability,
runtime coercion, pipeline authoring or general unknown-type policy was added.
Independent review approved the repair. The final restored run passed 312 checks;
removing the repair failed two controls and removing its input-field guard failed
three. Ruff, formatting, mypy, contracts and the masquerade baseline passed.

The report-only guidance audit also confirmed an omission: ReferenceJoin teaching
did not explain that numeric-looking CSV cells remain strings. Parent-reviewed
assistance and shared repair teaching now distinguish CSV strings from typed JSON
values, explain explicit conversion when required, and preserve supplied CSV data.
Behavior-backed controls exercise both formats and the actual strict sink check;
removing either teaching correction fails its controls.
The guidance correction passed 439 affected/structural checks, 192 final
wording/catalog checks and 96 direct catalog checks. Canonical regeneration changed
only the ReferenceJoin option description in its catalog golden. No scenario
manifest entry referenced this plugin, verified with positive and injected controls.
An optional HTTP test timed out inside the sandbox; the exact test passed outside
it. A minimal AnyIO-only control reproduced the sandbox's stalled worker wakeup
and passed outside it, isolating the environment cause without changing the test.

This attempt exercised the original failing provider route in full workflows:
all 94 planner calls were served by Together, all 114 strict calls were wire
conformant, and all 42 parameterless calls succeeded. The keyword workflow needed
one ordinary repair after its review claimed unmatched categories reached
quarantine unchanged while truncation preceded routing. That draft was not
approved. Composer repaired the graph, its exact replacement card was inspected,
and the original planned password edit and execution passed within three user
turns. Both LLM prompt cards were inspected individually; extraction passed all
three rows without an invented urgency interpretation.

The service stopped with exit 0 and unchanged source hash
`22020b8b38b36bc789398a5bc4f346d5a064fc9ecfc16edac0ccd14194948166`.
The cumulative ledger reached 513 completed requests, zero active requests,
$3.0967919306 reported cost and one historical unpriced timeout. Nine passing
workflows do not establish final acceptance; the graph repair needs a fresh run.


## Complete live acceptance at 72170c2

The fresh ten-workflow battery at `72170c2cf8cafcf9ea76230c61fdbacf6157a9fa`
passed every case. The final reviewed runner exited 0, and the isolated service
exited 0 with unchanged source content
`a1e60288273c7d053fbcbdba74a9c245e645b838d900b9e455af4d467ce82b68`.
No ordinary repair turns were needed. The two prompt-review cards were individually
checked against the requested semantics, source bindings and approved Sonnet
profile before exact-ID resolution. Independent audit found all 86 workflow planner
calls served by Together, all 108 strict calls conformant, and all 38 parameterless
calls successful. One semantic field-guarantee rejection was corrected by the next
planner call; it was not a wire decode failure.

Complaint/SLA completed six successful rows and six successful runtime LLM calls.
The planner kept the supplied reference CSV and its string-valued SLA contract
through the projection and fixed CSV sink. The output has exactly the requested
four columns and billing=24, outage=4, other=48 values. Structured extraction
completed all three rows with native string/integer/boolean fields and correct
urgent/normal routing. Fork/coalesce passed exact output and lineage checks with
its actual flexible source and observed expression transforms; the separate
observed-source regression remains covered by the focused real-runtime test.

The ledger reached 643 completed provider dispatches and $3.6376092598 known
cumulative cost, retaining one historical unpriced advisor timeout. These totals
include earlier preserved failed attempts. No deployed service was changed.

The broader suite exposed additional stale fixtures and inventories, plus a
shared-guidance placement error. Concrete ReferenceJoin CSV/type-conversion facts
belong in runtime plugin discovery; the shared prompt now gives generic discovery,
conversion and supplied-data-preservation instructions. Its two existing boundary
guards remain unchanged and fail when the offending static row is restored.
Other repairs preserve test intent: schema-only coalesce fixtures use a preserving
plugin, proposal-binding positives use genuine staging, corruption tests inject
explicit invalid historical state, and writer inventories update only measured
line locations while retaining fingerprints and admitted authorities. Final
integrated results after those repairs are pending.


The completed default suite at this snapshot reported 54 failures, 58,075 passes,
100 skips and two expected failures. Every failing ID was assigned to a measured
repair: 28 guided lifecycle fixtures, 15 authority inventories, two static-prompt
checks and nine schema/coalesce fixtures. Clean-release comparisons distinguish
these candidate regressions from the standing lint state. The authority repair
changes 135 measured line pins only; normalized inventory AST and every writer
fingerprint, operation and authority remain unchanged.

Restored checks passed: atomic routes 69; fork and affected guided step 172;
revert 30 plus 20 structural checks (two PostgreSQL-specific skips in the serial
selection); full authority module 266 with one existing expected failure; Composer
state and structural checks 611; runtime agreement and structural checks 115; and
static guidance/assistance checks 217. Negative controls reproduced the stale
fixtures, invalid binding behavior, stale inventory locations, and misplaced
plugin teaching. Independent review approved each scoped correction. These are
focused repair results, not a replacement for the final canonical gate.


## Final verification record

The final tested candidate is `1ef793a887f06f29feb11db3afc7762aad4f0448`.
Its fresh live battery passed all ten workflows; the reviewed runner and service
both exited 0. Source content remained
`8c2025f2e0ebbf4eba01dd9269c9f06e4243d26dc58dd7a5df1ec4dda1c05f63`.
All 88 workflow planner requests were served by Together. All 106 strict calls
were conformant, all 35 parameterless calls succeeded, and all nine row-bound
Sonnet calls succeeded. Six semantic authoring errors were corrected inside the
planner loops. One ordinary user repair corrected contradictory prompt-format
wording before its exact replacement card was approved; complaint meanings,
profile and supplied data stayed unchanged. The original SLA edit still ran and
produced all six correct output rows. Every workflow closed its audit accounting.

The earlier ten-case pass at `72170c2` remains independent evidence. The cumulative
ledger across preserved attempts reached 774 completed dispatches, zero active
calls and $4.2417953430 known cost, with one historical unpriced timeout. No key
or deployed environment was persisted in these reports.

Verification is composed from explicitly separate runs:

- The frozen six-stage `72170c2` gate: ruff, mypy and contracts passed; default
  pytest reported 58,075 passes and the 54 diagnosed failures; PostgreSQL passed
  571 tests with one skip. Its original `RESULT=FAIL` is preserved.
- Every original failed ID was rerun on frozen `1ef793a`: **54 passed**, zero
  failures or skips, exit 0. The selected IDs exactly match completed JUnit
  failure evidence; an invalid-module control rejects an invalid selection.
  The larger affected-module and mutation results are recorded above.
- The final canonical static gate on frozen `1ef793a` passed ruff, mypy and
  contracts and reported `RESULT=PASS`. Lints exited 1/nonfatal with 2,362
  findings: the same normalized corpus as `72170c2`, seven classified policy or
  binding findings above the clean-release baseline of 2,355. No judge signatures
  were edited, and this is not a claim of trusted signature verification.

Independent review approved bounded revalidation: the follow-up changes comprise
review documents, eight test modules and one generic shared-guidance Markdown row,
with no runtime Python, schema, configuration or shared-test-helper change.
The repository's reach-based testing rule supports completed broad runtime and
PostgreSQL coverage plus exact failure closure, affected whole-tree checks and
fresh live acceptance. This does not invoke the separate "full suite already
passed" exception: the earlier full suite failed. No second full-suite pass is
claimed. Host contention did not determine that verification recommendation.

Canonical summaries and exact closure evidence are retained under the ignored
session lane in `final-integrated-gate/`, `final-repaired-static-gate/`,
`final54-restored.log` and `final54-junit.xml`; read-only audit and per-defect
reports retain raw attribution and negative-control evidence. The final source
is ready for the authorized local `release/0.8.1` integration; branch containment
and preservation of unrelated main-checkout changes are checked at that transition.
No push, deployment or production service restart is included. Session/coordination
epoch 68 and the rebuilt frontend remain deployment consequences. Refresh survival
stays parked for the streaming UI rewrite. The original-header write-target and
numeric-admission limitations documented above remain open; the ten passing
workflows are not an exhaustive claim over all plugin combinations.


Local integration completed at `8191240beddfc08d7a44859b64d3e107691a3060`
on `release/0.8.1`. Post-transition checks confirmed the target index, absence of
conflicts, exact tested executable-content hash, and preservation of all 138
unrelated user files (seven modified tracked files and 131 untracked files).
All five task worktrees and the merged task branch were removed with the canonical
cleanup script after preserving and verifying the unique frontend build archive.
No push or production service restart was performed.
