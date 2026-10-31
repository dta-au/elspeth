# Composer tool-call contracts for 0.8.10

This versioned draft describes the call contracts being received for
[issue #327](https://github.com/dta-au/elspeth/issues/327). Use it with the
[Composer Tool Reference](composer-tools.md): the reference explains ordinary
use, while this page distinguishes intended behavior, assembled implementation,
historical nonconformance and evidence still needed for acceptance.

Named/plural attachment-root authoring and public authoring projection,
TC-001 and TC-005, are now implemented and independently source-reviewed in
the working assembly and published at `9d7f62629acbe23509998e385aa9d1a5472a1778`.
Root received 419/419 unit/current-regression cases against proposed-index
production. The recorded model/wire/redaction/teaching/AST cohort
has 1,110 passes and one separate existing Advisor initialization-provenance
failure still requiring disposition. These results do not make every affected
gate green. The later `776d25bd7480602a82817b5134668305ff9b97bd` checkpoint
includes 12 passing authenticated SDK cases, including ordinary/tutorial-follow-up
and provider-decline controls in both configured dialects. They exercise the real
application, custody, audit and lifespan with genuine SDK response objects at a
stubbed completion seam. They do not establish live-provider delivery or the full
scripted tutorial. A separate nine-case public PostgreSQL slice now passes
against immutable `776d25bd` production. It exercises actual public dispatch,
persistence and private reuse, including atomic refusal, ownership, current
metadata and cross-connection transaction visibility. Full final-source
PostgreSQL and global receiving remain owed.
See the [accepted attachment contract design](../architecture/design-notes/0.8.10-u13-u14-tool-contract-design.md).

The source checkpoints are `41fcaafced28fa23c33d82c32329ddf2599ec9bc` for the
guidance/validation/inspection assembly and
`ecd197962988a7dd5e12e6da15a1a3ff084d5458` for MCP. The inventory measurement
below was made at `63bd6e53789054a6a3bf5138cbcbc6c297c251c4` on 31 October
2026. These checkpoints identify inspected or measured inputs; they do not
freeze later source joins. The [finishing sequence](../release/0.8.10-finishing-and-landing-sequence.md#p8--finish-issue327-and-keep-u-17-tied-to-final-source)
requires the final U17 refresh after the last affected source edits.
The public U13 implementation belongs to `9d7f626`, with the bounded SDK and
worker-lifecycle receiving published at `776d25bd`;
the final dossiers still require rebinding after the remaining source joins.

## Item status

| Item | Intended contract | Current implementation | Remaining verification |
| --- | --- | --- | --- |
| DOC-001 | Name the inventory surface and measure its count. | Exported-definition count matches the scoped live measurement below. | Rebind final declarations and retain item-level regression/first-RED attribution. |
| TC-001 | Provider-authored named/plural attachment roots with original custody and atomic replacement. | Public producer published at `9d7f626`, independently reviewed and unit received; bounded authenticated SDK/dialect cases and nine public PG cases pass against `776d25bd` production. | Full final-source PG/global receiving, full scripted tutorial/live-provider delivery and final-source binding. |
| TC-003 | Exact raw source selector; no guessed destination on plural roots. | Prepared guidance and existing handler semantics are assembled. | Native guidance/behavior evidence and final-source binding. |
| TC-004 | An exact closed-code lookup returns its canonical code. | Exact-code repair and delivery controls are assembled. | Current focused native evidence and final-source binding. |
| TC-005 | Public attachment projection produces accepted authoring inputs without copied private custody. | Projection/raw-owned readmission and current-record reapplication published at `9d7f626`, independently reviewed and unit received; bounded SDK/dialect cases and nine public PG cases pass against `776d25bd` production. | Final source binding, full final-source PG/contended behavior, full scripted tutorial/live-provider delivery and global receiving. |
| TC-006 | Inspection uses the common admitted blob reader and returns bounded facts with explicit failures. | Shared-reader repair and selected response contract are assembled. | Current focused/contended-reader evidence and final-source binding. |
| TC-007 | Recovery descriptions state actual argument bounds and nonblank requirements. | Prepared descriptions and parser behavior are assembled. | Native guidance/argument evidence and final-source binding. |
| DOC-002 | Read each offered response according to its actual variant. | Shared instructions and reference distinguish ordinary, Advisor and terminal results. | Current instruction/envelope controls and final-source binding. |
| TC-018 | Admit loaded state/listed metadata before adopting or returning it. | Original failing IDs/signatures recovered; selected component assertions pass in retained Python 3.12 and 3.13 receiving. | Final source binding and joined acceptance; historical RED source binding remains partial. |
| TC-019 | An unsuccessful load preserves working graph, checkout and diff baseline. | Original failing ID/signature recovered; selected component assertions pass in retained Python 3.12 and 3.13 receiving. | Final source binding and joined acceptance; historical RED source binding remains partial. |
| TC-020 | Classify and record admitted handler outcomes without returning refused caller content. | Selected component assertions and narrow old-code oppositions are retained. | Preserve the scope of each first-RED subclaim; final joined acceptance remains open. |

“Assembled” is a source statement. A current passing control is separate from
its failing-first provenance. Neither a prepared patch nor an aggregate passing
test count closes an item. Before closure, each item needs its actual assembly
commit, relevant failing-first identifier and source-bound receiving evidence,
as required by the [release acceptance criteria](../release/0.8.10-master-plan.md).
This draft does not update the issue checkboxes.

## DOC-001 — inventory counts have a scope

**Intended.** A count names the builder or registry it measures. Registration,
exported definitions and the tools offered by the Composer loop are different
surfaces. A count says nothing by itself about successful dispatch, provider
delivery or authorization for a particular caller.

**Current.** The live, controlled producer measurement at the checkpoint above
recorded these unique-name inventories:

| Surface | Count | Relationship |
| --- | ---: | --- |
| `_REGISTERED_TOOLS` | 42 | Declared handler tools. |
| `get_tool_definitions()` | 44 | Registered tools plus `request_advisor_hint` and `request_interpretation_review`. |
| `composer_loop_tool_definitions`, `none` | 46 | Exported definitions plus `handoff_to_user` and `restore_parked_graph`. |
| `composer_loop_tool_definitions`, `openai_strict` | 46 | The same measured names under the strict transport. |

These are definition-producing APIs, not additional planner-call arguments.
Use the actual offered schema when calling a tool. The registry/exported/loop
builders are in [the registry](../../src/elspeth/web/composer/tools/_registry.py),
[dispatch](../../src/elspeth/web/composer/tools/_dispatch.py) and
[provider gateway](../../src/elspeth/web/composer/provider_gateway.py).

**Known nonconformance.** Historical prose understated an earlier exported
inventory and its blob-discovery subtotal. That was corrected by
`5fdc6d01585e628ce75a32a2aaf00d84d06f8e6a`. The historical count is not the
current measured inventory and must not be copied into new guidance. The
captured `get_tool_definitions` count of 44 is correct for that measured builder,
rather than a total for the captured 46-tool loop palette. Final declarations
and inventory must be rebound after U13 publication; unchanged tool names are
not inferred from this older measurement.

**Verification.** Retain
`test_tool_definitions_documented_count.py::test_documented_breakdown_matches_the_live_registry`,
`::test_inline_definitions_are_exactly_the_two_documented_carve_outs` and
`::test_measured_breakdown_sums_to_the_emitted_total` under
`tests/unit/web/composer/`. The immutable correction names the first selector
as its fail-before case. Its historical native first-RED receipt was not
rebound for this draft. The current controlled inventory is measured evidence;
final declaration bodies and regression acceptance still need the final join.

## TC-001 — provider-authored scalar and plural attachment roots

**Intended arguments.** A complete `set_pipeline` replacement supplies either
the default `source` object or a `sources` map keyed by provider-authored root
names. The other top-level source field must be omitted, even when its proposed
value is null. Nodes, edges and outputs are required; metadata is optional.
Each source retains
the explicit plugin, route, validation-failure choice, ordinary options and
description selected by the provider. Attachment selectors are per root:

| Root form | Scalar | Plural | Inline creation |
| --- | --- | --- | --- |
| Default `source` | Nullable `blob_id` | Nullable ordered `blob_ids`, 1–512 strings when non-null | Nullable `inline_blob`, on the existing inline-custody path |
| Named `sources.<name>` | Nullable `blob_id` | Nullable ordered `blob_ids`, 1–512 strings when non-null | Undeclared; even a supplied null is an argument-shape error |

Omission or null makes a selector absent. A single empty ID remains an invalid
identity value; an empty list is not absence. Wrong selector/container/member
types, empty/over-width lists and undeclared fields use the existing argument
channel. Two or more correctly typed non-null selectors on one root produce
the recoverable, root-attributed `source_binding_ambiguous` refusal. The default
root can choose scalar, plural or inline; a named root chooses scalar or plural.

Plural binding requires the provider to choose `plugin:"blob_rows"`; another
plugin gets `source_blob_plugin_mismatch` rather than a substituted plugin.
A one-member plural list still means one document-custody row, not scalar
CSV/text parsing. Existing scalar explicit-plugin behavior is retained.

The resolver reads current session-owned READY records and retains order.
Plural records must retain their original user-verbatim upload provenance;
LLM-authored content is not admitted as a plural document attachment.
Duplicate identity or content within one list refuses the list; reuse across
separate roots remains valid. Missing/foreign/non-READY or prohibited-modality
records get the existing precise refusal. Caller-supplied private custody is
not a substitute for record resolution. The public width of 512 derives from
the current credential traversal policy; the plugin/private storage parser's
independent 1,000-entry bound is unchanged. Other byte, node, response and
credential budgets can refuse a request before this cardinality ceiling.

**Results and atomicity.** The existing full-replacement result has one final
candidate state/version step. A valid earlier root and invalid later root do
not publish a partial graph or change the original graph/version. Candidate failures identify the actual source
and exclude unrelated standing old-graph errors; incremental binding retains
its saved-graph refusal semantics. Root names, plugins, routes and graph
structure remain provider-authored. Singular optional `data.source_blob`
does not misrepresent an arbitrary plural member as the whole operation.

**Current.** The default/named models in
[redaction.py](../../src/elspeth/web/composer/redaction.py), real declaration
and builder in [sessions.py](../../src/elspeth/web/composer/tools/sessions.py),
existing scalar/plural resolvers in
[sources.py](../../src/elspeth/web/composer/tools/sources.py), exact mismatch
guidance and capability inventory are published at `9d7f626` after source review
and unit receiving. No persisted `SourceSpec`, plugin storage bound, private
authority parser, provider entry or tutorial path changed for this grammar.
The bounded authenticated SDK/dialect cohort is published at `776d25bd`.
The additional public PG slice described above is received against that same
production checkpoint; full final-source PG and joined acceptance remain
separate obligations.

**Known nonconformance and verification.** The earlier named-selector/default-
plural model/declaration gap is repaired in source. Root's measured first RED
included the declaration/model/teaching agreement family,
`test_set_pipeline_authors_named_attachment_roots` and
`test_default_source_binds_ordered_blob_ids` in
`tests/unit/web/composer/test_set_pipeline_attachment_roots.py`. The baseline
had five healthy controls and 17 intended missing-interface failures across
producer/projection families. Current unit receiving is part of the 419/419
cohort stated above. Tests separately oppose null/shape/ambiguity channels,
wrong explicit plugin, duplicates/cross-root reuse, later-root atomicity and
the actual public-width boundary. Downstream cases formerly blocked by missing
grammar are not relabeled independent first-RED freshness or atomicity proofs.

No additional producer defect was found in the independent review. The published
12-case SDK cohort exercises authenticated ordinary/tutorial-follow-up and
provider-decline/no-authored-graph controls in both configured dialects. Its
actual lifespan now completes after the worker handoff repair, which also passes
three deterministic late-cleanup controls and 24 existing worker checks. The
completion seam is stubbed: live-provider delivery and the full scripted tutorial
remain open. The nine public PG cases in
[test_set_pipeline_attachment_roots_postgres.py](../../tests/testcontainer/web/test_set_pipeline_attachment_roots_postgres.py)
cover default/named ordered roots, pending/foreign/LLM-authored later-member
atomic refusal, filename/hash refresh, another user's ownership and committed
READY visibility while a PENDING update is uncommitted on another PostgreSQL
connection. They do not qualify the
full PostgreSQL selection or final global integration. TC-001 still needs that
final integration and source binding.

## TC-003 — `wire_secret_ref` chooses an exact component

**Intended arguments.** `name`, `target` and `option_key` are required strings;
`target` is `source`, `node` or `output`. Supply `target_id` as an exact raw
source-map name, node ID or output name when a selector is required. Extra
properties are refused. For example, when the server permits this exact
destination:

```json
{"name":"WAREHOUSE_API_KEY","target":"source","target_id":"left","option_key":"api_key"}
```

For a sole source, an unspecified selector chooses that source, including a
sole named root. With multiple roots it refuses rather than choosing the first.
The display identifier `source:left` is not an alias for the raw name `left`;
names are not trimmed or deprefixed. An explicit `source` selects only an actual
root named `source`. Node/output targets also require their exact selector.

Follow the offered wire schema. In the ordinary semantic shape, an omitted
optional selector is different from a supplied null or blank string. The strict
transport can require a nullable field whose null is decoded to omission before
semantic admission. That transport rule does not make canonical null or
whitespace an accepted source name.

**Results and errors.** Success stores a `secret_ref` marker and returns the
ordinary mutation result with the affected component and updated validation;
it does not expose the value or establish execution readiness. Wiring is
deny-by-default for the exact secret/component/plugin-option destination.
Missing context/reference, plural-source ambiguity, missing components,
unsupported targets and denied placement return saved-graph refusals such as
`secret_context_unavailable`, `missing_secret_ref`, `secret_target_required`,
`source_not_found`, `node_not_found`, `output_not_found`,
`secret_target_unsupported`, `unauthorized_secret_ref` or `disallowed_secret_ref`.
They preserve the original state and standing findings. Invalid argument shape
uses the ordinary argument-admission channel; it is not a successful mutation.

**Current.** `_WIRE_SECRET_REF_DECLARATION`, `_WireSecretRefArgumentsModel` and
`_execute_wire_secret_ref` in [secrets.py](../../src/elspeth/web/composer/tools/secrets.py)
implement this contract. The prepared guidance came through
`23be93a17a1bbc25f096c54718cd0d79a6825194` and its receiving carry
`bc5fe94641579a71e1dc72badc85fc8b5662cff4`; the inspected definitions remain in
assembly `41fcaafce`.

**Known nonconformance and verification.** The earlier general-reference row
omitted source selectors; this documentation update corrects that row. No new
runtime nonconformance was established for these scoped assertions. Retain
`test_secret_rejection_contracts.py::test_target_id_description_states_the_source_selector_contract`,
`::test_source_selector_is_an_exact_raw_name_never_an_alias` and
`::test_raw_named_source_selector_wires_exactly_that_root`, plus the transport
oppositions in `test_wire_projection.py`. These are under
`tests/unit/web/composer/`. Historical description RED and handler mutations are
reported by `0ebb07f987f4d7ed2b239e67b5a5ee81bcaf81b5`; the source-bound native
receipt and final item acceptance remain unverified here. Existing handler
semantics are retention obligations, not a missing implementation.

## TC-004 — `explain_validation_error` preserves exact-code identity

**Intended arguments.** Call with exactly `{"error_text": "<message or code>"}`.
The string is required and additional properties are refused. For example,
`{"error_text":"batch_transform_misplaced"}` asks about an existing closed
validation code; passing the actual validation entry's message is also supported.

**Results.** The ordinary discovery result leaves the draft unchanged.
`data` carries `error_text`, `explanation` and `suggested_fix`. It includes a
string `error_code` only on a closed-code branch; absence is omission, not null.
A successful explanation is not proof that the pipeline is valid.

| Selection order | Meaning of `error_code` |
| --- | --- |
| Exact runtime-only `union_field_collision` reason | Omitted; it is not promoted into the build-time code catalogue. |
| Exact, case-sensitive closed catalogue key | The same canonical key is returned. |
| First matching prose pattern | Omitted; this branch precedes code-in-noise lookup. |
| Closed code embedded in other text, case-insensitive | Canonical key returned; multiple candidates follow catalogue order. |
| No match | Omitted; explanation and suggested fix provide usage guidance. |

There is no uniqueness/ambiguity refusal for text containing several codes.
Recognized prose can win over an embedded code. Expected-value hints may extend
the selected suggestion; there is no separate large `known_codes` payload.
Wrong argument shape follows `ToolArgumentError` admission.

**Current.** `ExplainValidationErrorArgumentsModel`,
`_EXPLAIN_VALIDATION_ERROR_DECLARATION` and `_execute_explain_validation_error`
in [generation.py](../../src/elspeth/web/composer/tools/generation.py), together
with the selected explanation response, implement this contract. Exact-key
feedback was repaired by `b3667996586f821feeb499ea2b7470739c0f23d3` and is
present in assembly `41fcaafce`.

**Known nonconformance and verification.** The historical exact-key branch
omitted the canonical code while the noisy-code branch supplied it. That gap
is repaired in the assembled source. Historical failing-first identifiers
reported by the immutable repair are:

- `tests/unit/web/composer/test_validation_guidance_catalogue.py::test_exact_closed_code_emits_expected_canonical_code`
- `tests/unit/web/composer/test_validation_guidance_catalogue.py::test_explanation_branch_precedence_keeps_code_only_on_closed_code_branches`
- `tests/unit/web/composer/test_generation_response_contracts.py::test_exact_code_survives_discovery_delivery`

These cover catalogue equality, branch precedence and actual discovery delivery.
The historical attribution does not replace current source-bound native
receiving. No additional runtime nonconformance was established by the bounded
source review; current focused acceptance and the final source join remain open.

## TC-005 — public projection strips custody and re-resolves current records

**Intended call.**
`get_pipeline_state({"component":"set_pipeline_arguments"})` returns the
public flat authoring document in the ordinary result's `data` for
reapplication through `set_pipeline`.
Default/named scalar custody becomes `blob_id`; `blob_rows` custody becomes
ordered `blob_ids`. The document preserves plugin, route, description,
ordinary/reconstructible review options and the output `sink_name` transport.
It removes the corresponding storage paths, private references/authoring
markers and stored plural entry facts instead of asking the caller to copy
those facts back as authority.

Reapplication resolves each ID against current owned records. Admissible
current hash/name/MIME/size changes are refreshed; unavailable, foreign,
non-READY or prohibited records refuse without changing the existing state.
This public rule differs from exact private reviewed reuse, which refuses
changed recorded facts and requires re-review. Public projection never mints
private reviewed authority; reapplication retains the actual saved owner's
guarantee/review checks and does not authorize copied server facts on another
component. Metadata resolution is not attachment-byte-integrity clearance.

**Shape and failure variants.** Raw admission and owned readmission both
retain final strict `SetPipelineArgumentsModel` admission. Ordered public IDs
are immutable internally and encode as fresh JSON arrays. Malformed stored
entries, duplicate identity/content, mixed scalar/plural or plugin custody,
and lists over the public 512 bound refuse projection rather than dropping,
refreshing or truncating entries. The private storage parser still accepts
its independent bound of 1,000; that is not a promise of public reauthorability.
Whole-state credential admission still precedes projection: a storage-valid
wide state can get `credential_material_rejected` before a serializer-level
`round_trip_unavailable`. No budget or refusal priority is weakened to make a
round trip succeed. Ordinary successful projection leaves graph/version
unchanged; a successful public replacement has its one candidate version step.

**Current.** The public serializer in
[_common.py](../../src/elspeth/web/composer/tools/_common.py) and closed
raw/owned response in
[state_responses.py](../../src/elspeth/web/composer/tools/state_responses.py)
now carry default/named scalar and plural selectors. The provider-facing
capability/declaration/model changes are received with them as one coherent
increment published at `9d7f626`. The private phase-one authority, unit and
PG paths retain their separately recorded evidence.

**Known nonconformance and verification.** The historical named-scalar
unavailability and plural projection/input mismatch are repaired in source.
Root's actual first-RED family
`tests/unit/web/composer/test_attachment_authoring_projection.py::test_scalar_and_plural_authoring_projection_round_trip`
included named scalar/reviewed scalar and default/named plural/mixed forms;
`::test_attachment_authoring_raw_and_owned_readmit` opposed the then-missing
raw shape. Current receiving is included in the 419/419 unit cohort. The same
file contains meaningful current-record refresh/unavailability, first/later
malformed entry, duplicate/mixed/width, raw/owned corruption and detached-order
encoding controls. Their current passes are not invented historical first REDs.

The independent review found no blocking producer/projection or fixture defect.
The bounded authenticated SDK ordinary/tutorial-follow-up and both-dialect
cohort is published at `776d25bd`. The nine-case public PG slice described under
TC-001 also exercises private reuse of genuinely persisted public output,
metadata freshness and transaction visibility against that production source.
Full final-source PG/contended behavior, live-provider delivery, the full scripted
tutorial, global receiving and final-source binding remain owed. TC-005 is not
closed by these bounded results.

## TC-006 — `inspect_source` returns bounded facts from an admitted read

**Intended arguments.** Required `blob_id` names an existing session blob.
Optional `encoding` is a string of 1–128 characters or null; omission/null
chooses default inspection. Extra properties are refused. For example:

```json
{"blob_id":"11111111-1111-4111-8111-111111111111","encoding":"utf-8"}
```

Explicit encoding applies to CSV/TSV inspection only. It is canonicalized and
reported with a warning: inspection does not configure the source's encoding.
The caller still authors the intended source/plugin/options. This tool takes
no storage path and does not fetch URLs or construct a graph.

**Success data.** `source_kind`, `redacted_identity`, `byte_range_inspected`,
`sample_row_count`, `observed_headers`, `inferred_types`, `url_candidates`,
`warnings`, `runtime_headers` and `field_name_mapping` form the selected facts
contract. Headers/types may be null; runtime headers and raw-label mapping are
both present or both null. `redacted_identity` carries filename, MIME, a string
`byte_size`, blob ID and hash prefix, without the storage path or full hash.
`byte_range_inspected` names the bounded parsing window; `byte_size` reports
the actual total bytes streamed for integrity verification.

Structural parsing retains at most 8 KiB and samples at most 100 records. The
common reader still streams the whole file in bounded chunks to verify its
integrity hash. The parsing bound is not an 8 KiB total-I/O or constant-time
promise. Observed headers/types are sampled facts, not guaranteed fields,
whole-file schema validation, runtime coercions or source-review approval.
Warnings report uncertainty; unresolved runtime headers and their mapping are
null rather than fabricated. A partial data sample can still have a complete
admitted header. The graph and version remain unchanged.

**Failures.** Missing session context, malformed/unknown/foreign IDs, non-ready blobs,
missing backing content and unsupported encoding are repairable failures.
Foreign blobs do not disclose another session's metadata. Failed results use
`validation.errors`; callers cannot assume success-only `data` is present.
Malformed argument shape uses `ToolArgumentError`, distinct from a malformed
identifier value. A missing or mismatched dispatcher-owned storage root,
operation context or authority is an integrity fault. Integrity mismatch,
stale/released operation authority, malformed owned metadata and other I/O
faults propagate through their proper fault channels;
they do not become partial successful facts. Credential admission can refuse
facts with `credential_material_rejected`. Internal custody/recovery belongs
to the common reader, so discovery is not a promise of zero storage effects.

**Current.** `InspectSourceArgumentsModel`, `_INSPECT_SOURCE_DECLARATION` and
`_execute_inspect_source` in [sources.py](../../src/elspeth/web/composer/tools/sources.py)
use the common `_fenced_blob_reader` and
`BlobServiceImpl.read_blob_content_prefix_verified_sync`. The selected facts
contract lives in [source_inspection.py](../../src/elspeth/web/composer/source_inspection.py).
The repair is `9aff4e441059a06e1dfeb5228b18662c84914809`, present in assembly
`41fcaafce`; it retains the existing async prefix-reader API as well.

**Known nonconformance and verification.** The historical inspection adapter
bypassed the common custody/recovery/fence seam. That gap is repaired in the
assembled source. Historical failing-first selectors reported by the receiving
commits include
`tests/unit/web/composer/tools/test_tools_preview.py::TestInspectSourceTool::test_inspect_source_recovers_committed_version`
and `::test_inspect_source_rejects_stale_context_before_storage`. Keep the
same file's full-file integrity, bounded-chunk, missing/foreign-content,
explicit-encoding and selected-response controls. Current PostgreSQL lock and
post-read revocation controls are in
`tests/testcontainer/web/test_blob_read_custody_race.py`. Current focused and
PostgreSQL acceptance are unverified by this draft; source assembly is not a
substitute for either.

## TC-007 — terminal recovery arguments state their real limits

**Intended calls.** Use one recovery control alone in its response:

```json
{"action":"park","message":"I need one more product decision before continuing."}
```

That is a `handoff_to_user` argument object. Its action is `park`,
`reset_to_turn_start` or `reset_to_safe_checkpoint`. `restore_parked_graph`
instead takes `park_id` and `message`; use the exact advertised parked ID.
All fields are required, extra properties/null are refused, messages are
1–16,000 characters and parked IDs are 1–128 characters. Whitespace-only
values fail. Nonblank strings are preserved exactly rather than trimmed into
a different identity. A mixed recovery/discovery/mutation batch is refused
before normal dispatch.

**Results and errors.** These calls end model authoring; they do not return an
ordinary mutation `ToolResult`. The server reports the actual requested
recovery outcome. An unavailable explicit parked target is a refusal, not
permission to substitute another graph. Safe-checkpoint recovery can select
an empty editable workspace if no eligible checkpoint exists. A recovered or
parked graph does not imply execution readiness/approval, and recovery does
not undo external effects. Invalid JSON/object/model/blank arguments are
invalid calls, not recovery success. Model prose cannot override a trusted
server refusal notice.

**Current.** `HandoffArguments`, `RestoreArguments`,
`recovery_tool_definitions`, `parse_recovery_tool_call` and
`parse_recovery_tool_batch` in [recovery_controls.py](../../src/elspeth/web/composer/recovery_controls.py)
implement admission. The wire builder omits length keywords where transport
grammar support varies; descriptions and application admission retain the
limits. `_terminal_recovery_result` returns the admitted recovery intent;
session recovery settlement determines and reports the actual outcome.
Prepared guidance is present in assembly `41fcaafce`, from
the TC-003/007 carry identified above.

**Known nonconformance and verification.** The earlier guidance did not fully
state application limits/blank rejection. Prepared descriptions and parser
behavior are present; no additional runtime nonconformance was established in
this bounded review. Retain the actual tests under
`tests/unit/web/composer/test_recovery_control_arguments.py`:
`test_recovery_descriptions_state_bounds_and_whitespace_rejection`,
`test_handoff_rejects_whitespace_only_message_in_the_handler`,
`test_restore_rejects_whitespace_only_selector_or_message_in_the_handler` and
`test_padded_but_nonblank_values_are_admitted_exactly`. The first is the
historical description-RED candidate; its native first-RED/acceptance receipt
is unverified here. Existing palette/mixed-batch and actual trusted-refusal
controls remain relevant; constructor-only tests do not replace parser or
settlement receiving.

## DOC-002 — result variants are part of the call contract

**Intended.** Read the result variant for the offered surface. Do not infer
successful mutation, validation or recovery from an unrelated status field.
No common argument or flag changes an ordinary tool into another variant.

| Offered call | Model-facing result |
| --- | --- |
| Registered Composer tools and `request_interpretation_review` | Ordinary `ToolResult`: `success`, `validation`, `affected_nodes`, `version`, with optional tool-specific fields such as `data` and `validation_delta`. A discovery success can accompany an invalid draft. |
| `request_advisor_hint` | Flat feedback keyed by `status`; only `SUCCESS` carries advice in `guidance`. No `version`, `affected_nodes` or `validation`; advice is not a graph mutation. |
| `handoff_to_user`, `restore_parked_graph` | No tool result to the model; the turn ends and the server reports actual recovery outcome. Arguments and refusal semantics are in TC-007. |

Advisor arguments require `trigger`, `problem_summary`, `recent_errors` and
`attempted_actions`, with optional `schema_excerpt`, according to the offered
schema. The current non-advice statuses include `ARG_ERROR`,
`BUDGET_EXHAUSTED`, `DEADLINE_TOO_CLOSE` and `ADVISOR_ERROR`; none confirms a
stop or a valid pipeline. An offered proposal-only surface has its own terminal
instruction: rejection supplies repair feedback, while acceptance ends that
planning operation. A retired planner terminal is not part of the measured
44/46-tool inventories above and is not reintroduced by this reference.

**Current.** The shared [Composer instruction](../../src/elspeth/web/composer/skills/pipeline_composer.md#reading-a-tool-result)
and [result reference](composer-tools.md#tool-result-format) distinguish these
forms. The ordinary vocabulary is owned by
[tool_result_envelope.py](../../src/elspeth/web/composer/tool_result_envelope.py).
Advisor interception and feedback projection, and recovery handling, remain
their actual producers. The historical correction is
`e9802177fcba89a7a65cb69cd439fda154401f97`, carried into `41fcaafce` and later
route-retirement work.

**Known nonconformance and verification.** The earlier instruction incorrectly
said every tool returned one framing. The current shared instruction corrects
that claim. Historical evidence referred to an additional planner terminal;
copying its old count or tool name into current guidance would be wrong.
Current regressions in
`tests/unit/web/composer/test_tool_result_variant_teaching.py` include
`test_derived_non_envelope_set_is_the_audited_three`,
`test_skill_result_section_names_every_non_envelope_tool`,
`test_skill_result_section_does_not_claim_one_framing_for_every_tool`,
`test_skill_teaches_the_advisor_status_discriminant` and
`test_operator_reference_names_every_non_envelope_tool`. Retain the separate
`test_tool_result_envelope_gate.py` producer/teaching checks as well. Historical
first-RED is reported in the immutable correction; exact native case attribution,
current source-bound acceptance and final instruction/producer binding remain
unverified here.

## TC-018 — local MCP session data is admitted before adoption or output

These are local MCP sessions, separate from Web session UUIDs and HTTP user
ownership. Use local session IDs returned by this server.

**Intended arguments and results.** `load_session` takes exactly
`{"session_id":"0123456789ab"}` with a canonical 12-lowercase-hex ID;
`list_sessions` takes `{}`. Extra properties, missing/noncanonical IDs and
path arguments are refused.

- Successful load returns JSON `success:true`, `data` with `session_id`, and
  `state`. It admits the saved state before adopting or returning it and does
  not rewrite the file. Public state can redact storage locators while the
  server retains private custody.
- A missing load returns JSON `success:false`, `error`, and unchanged working
  `state`. Inspect that JSON success flag; a normal SDK result can still carry
  this business refusal. Corrupt saved content is a fault, not fabricated
  not-found data.
- Successful listing returns JSON `success:true`, `data.sessions` and
  unchanged working `state`. Each admitted entry contains `session_id`,
  `name` and `version`, ordered by local session filename. Listing adopts no
  graph. The manager skips invalid filenames/files its declared parser cannot
  read; the MCP dispatcher admits each metadata entry the manager would
  otherwise return.

Recognized credential material must not be adopted or returned from these
paths. A refused listing entry refuses that listing rather than exposing a
partial unsafe result. Admission is per entry, so a large benign list is not
treated as one detector-width-limited object.

**Current.** `_SESSION_TOOL_DEFS`, strict argument validators and
`_dispatch_session_tool` in [server.py](../../src/elspeth/composer_mcp/server.py)
implement these contracts at assembly `ecd197962`. `SessionManager` owns local
saved-state parsing and checkout facts. No current nonconformance was
demonstrated for the selected assertions.

**Verification.** These exact families are present and passing in each retained
Python 3.12/3.13 final component cohort:

- `tests/unit/composer_mcp/test_load_session_contract.py::test_tc018_loaded_state_is_admitted_before_adoption_response_and_audit`
- `tests/unit/composer_mcp/test_list_sessions_contract.py::test_tc018_disk_name_is_admitted_before_list_response_and_audit`
- `tests/unit/composer_mcp/test_list_sessions_contract.py::test_tc018_listing_admission_is_per_entry_and_not_bounded_by_listing_width`

They include real benign/refused and active/pre-resolution controls. The original
locally retained `u15-tc018-red.log` has four actual `AssertionError` failures:
the load family above with `[pre-resolution-tc018-recognized]` and
`[active-session-tc018-recognized]`, and the disk-name family with `[buffered]`
and `[active]`. Each observes `response.isError` false where refusal requires
true. Companion records bound that historical run after `58d439fba` and before
the `091fd951e` admission fix. The separate `u15-f1-red.log` records the per-entry
listing family above failing with `listed.isError` true where false is required;
its fix is recorded as `82be27486`.

These recover the original failing IDs and signatures. The logs lack modern
supervisor receipts, selected-input freeze proofs and full Git-object bindings;
their historical source bounds come from companion handovers and pre-commit
headers. The later old-code audit oppositions remain separate TC-020 evidence.
Final joined acceptance remains open; local receiving is not a general
data-loss-prevention or external-host transport guarantee.

## TC-019 — failed load preserves the existing diff baseline

**Intended.** Successful `new_session` or `load_session` establishes the
`diff_pipeline` baseline. An unsuccessful load preserves working graph,
active checkout/session and baseline, including unsaved edits that must remain
visible in the next diff. A missing or malformed requested ID never means
create, reset, save or switch to a different session.

Use the `load_session` argument shape above. `diff_pipeline` takes `{}` to
inspect baseline-relative changes. A missing load's `success:false` is an
application refusal even when the SDK call completed normally. Corrupt input
and invalid arguments do not authorize replacing the baseline either.

**Current.** The local session dispatcher returns unchanged state on a missing
load. The server's `call_tool` updates active identity/checkout resolution and
baseline for new/load only after application `success:true`. This is assembled
at `ecd197962`; successful loading still adopts the real saved snapshot.

**Known nonconformance and verification.** The historical unsuccessful-load
baseline change is corrected in the assembled source. Current native component
evidence on Python 3.12 and 3.13 explicitly includes:

- `tests/unit/composer_mcp/test_load_session_contract.py::test_tc019_missing_load_preserves_graph_checkout_and_diff_baseline`
- `tests/unit/composer_mcp/test_load_session_contract.py::test_corrupt_file_is_crash_not_notfound_and_preserves_old_baseline`
- `tests/unit/composer_mcp/test_load_session_contract.py::test_invalid_arguments_preserve_state_checkout_file_and_diff`

The first creates a real baseline and unsaved edit, proves a nonzero diff,
attempts a missing load, then requires the exact old diff/working graph and a
usable old checkout. The original locally retained `u15-tc019-red.log` names
that exact first selector and reaches `assert after == before`: after the missing
load, `from_version` is 2 and `metadata_changed` is false, where the prior diff
has version 1 and true. Its terminal result is one `AssertionError` failure.
Companion records place that RED after test carry `4c5b19853` and before the
`58d439fba` baseline-preservation fix. This is recovered historical failure
attribution, not an inference from a source comment. As for TC-018, exact RED
working-byte freeze and modern supervisor proof are absent; the source sequence
is historically bounded. Current component passes and final joined acceptance
remain separate claims.

## TC-020 — local MCP call outcomes preserve safe evidence

**Intended boundary.** This is a shared call contract, not a new tool or argument.
Calls admitted to the MCP application handler must be classified and recorded
before normal completion. Refused caller content must not reappear in returned
errors or recorded arguments. Faults while preparing owned evidence remain
faults; a later tool failure retains already admitted ordinary arguments as
exact evidence.

Local session tool objects are closed. `new_session` has optional string
`name`, defaulting on omission to `Untitled Pipeline`; save/load/delete require
the canonical local `session_id`; list and YAML generation take `{}`. These
tools can return local JSON `state`, `data` and `error` variants rather than
the Web `ToolResult` shape. Success data can be a mapping or YAML text. A missing
load/delete can be JSON `success:false` within a normal SDK result; argument,
credential or internal faults use the SDK error channel. YAML validation refusal
has its own narrower `validation` payload.

**Current.** The registered `call_tool` in
[server.py](../../src/elspeth/composer_mcp/server.py) owns capture/admission,
dispatch, classification and final recording at assembly `ecd197962`.
Refusals withhold caller argument evidence. A fault before admission completes
retains unavailable evidence; later dispatched failures retain the admitted
pair. Call names are treated according to registration rather than reflected
as trusted names. This draft does not expose private diagnostic payloads or
reproduction procedures.

The guarantee begins at handler admission. It does not promise a durable
record for a call canceled before entry, persistence of a pre-session volatile
buffer before session resolution, or rollback of an earlier filesystem effect
when a later recording operation fails. Those limits do not turn an unproved
outcome into successful work.

**Known nonconformance and verification.** Selected withholding and outcome
classification gaps have assembled repairs. The retained current Python
3.12/3.13 component cohorts explicitly include the TC-020 refusal controls in
`test_new_session_contract.py`, `test_load_session_contract.py`,
`test_list_sessions_contract.py` and `test_delete_session_contract.py` under
`tests/unit/composer_mcp/`. Narrow measured first-RED families now passing are:

- `tests/unit/composer_mcp/test_argument_admission_audit.py::test_unexpected_credential_admission_fault_withholds_unadmitted_input`
- `tests/unit/composer_mcp/test_argument_admission_audit.py::test_refusal_evidence_fault_is_audited_before_propagation`

Keep the separate current
`test_argument_admission_audit.py::test_later_dispatch_fault_preserves_exact_admitted_argument_evidence`
opposition as well. No first-RED claim is made for that latter selector.
Retained immutable-old audit oppositions support their actual audit subclaims;
they do not establish TC-018/019 first-RED history or broad MCP acceptance by
their count. Final joined item acceptance, external provider/host transport and
unmeasured lifecycle cases remain unverified.
