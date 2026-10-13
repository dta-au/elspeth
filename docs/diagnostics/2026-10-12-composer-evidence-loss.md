# D20: preserve Composer authoring evidence separately from display

**D20 is implemented locally; final qualification is pending.** At the
investigated base revision, Composer persistence destroys ordinary
authored tool arguments and results by storing their display projections.
The smallest complete repair is a separate credential-filtered diagnostic
record in the existing private session message store, written atomically
with the display records and available through an explicit diagnostic read.
Changing the options summarizer alone would expose authored content through
existing conversation responses and would miss the cancellation path.

The evidence below describes that base revision. Design A has since been
authorized and implemented on the combined repair branch. The
implementation adds private evidence siblings and an attributed read route;
it does not retain encrypted credential-bearing originals. The historical
helper measurements below are not acceptance evidence for the new writers.

## Implementation checkpoint

The builder retains admitted ordinary semantic JSON and separately labels
eligible original wire, filtered values, and unavailable input. It preserves
valid secret references and checks actual output-tool credential slots,
including the database sink's `url`. Because `patch_output_options` can omit
the sink plugin identity, its exact `patch.url` slot is conservatively
filtered; unrelated reference URLs are retained. This is a finite credential
policy, not proof that every opaque string is free of secrets.

Private `composer_diagnostic_evidence` audit rows accompany normal
turns, alternate/cancellation cohorts, and one-shot planner candidate
receipt/settlement. They use the existing session store and operation fence.
The explicit `GET /api/sessions/{session_id}/diagnostic-evidence` route verifies
session ownership and records access before returning evidence. Generic
conversation/audit views, provider history, and forks exclude these records;
malformed public/private mixtures fail closed.

Original authoring is captured before display projection and finalizer
rebinding. Normal P4 commits private siblings atomically; typed cancellation
and fatal-failure carriers transfer only the current undurable batch to an
audit-only drain. The drain preserves failure/cancellation precedence without
publishing an invalid graph. Explicit cancellation closure covers the direct
async tool stages outside the existing audited dispatch `finally`.

At committed baseline `48764972c`, planner payload coverage begins at admitted
parsed candidates. The authorized follow-up extends private evidence to
bounded filtered received slots before admission and useful filtered failure
facts. This extension is implemented with independent security acceptance.
Final helper/persistence controls pass 37 tests; three corrected fatal and
cancellation controls also pass. Combined qualification remains pending.
Physical receipt siblings settle atomically with the existing provider
checkpoint and remain explicitly unbound to graph state during later replay.
Received payloads have separate shared 1 MiB raw-input, canonical-input and
final filtered-argument bounds, with at most 32 slots and an omission summary.
Failure facts have at most five cause/context nodes and 8 KiB canonical facts.
Unavailable content has an explicit disposition, never a raw fallback.
It retains neither a complete raw-response
archive nor reconstructable provider request snapshots. Cooperative failure
tests do not establish process-death or resource-exhaustion durability.

The explicit diagnostic route reads one storage row at a time, returns one
private record by default, and permits at most five per page. It validates
the scanned prefix and stops at the requested page, so it does not certify an
unread suffix. Each record's four content slots have separate 1 MiB admission
bounds; JSON serialization adds overhead. Existing ordinary conversation
preparation still loads session history before excluding audit rows. This
work does not establish a global per-session memory bound or change that
existing history-loading architecture.

Completed focused runs include 43 builder tests, 82 private read/access/fork
tests, 38 conversation/history/control-message tests, a subsequent 14-test
bounded-route run, and the final 33-test writer failure/cancellation slice;
each exited 0. These selections overlap and are not an aggregate test count.
Independent security review accepts the frozen bounded implementation.
Combined qualification remains in progress.
These focused results do not replace the final combined-tree gates or
PostgreSQL coverage required for persistence changes.

## Evidence and limits

The current-source inspection used commit
`fdaa670394fd311fc5df61befb1a7b1941c01182` on the isolated emergency diagnostic
branch. The supplied historical report identifies deployed source
`1dec423d306513f28325573cd07e164196cb5686` and an October 11 incident. These
are different revisions. Historical observations below are attributed to
that report; this investigation did not read its live session database,
reproduce its production failure, or establish revision equivalence.

| Question | Supplied historical report | Current verified evidence |
|---|---|---|
| Were rejected classifier options recoverable? | Reports lost prompt/review inputs and no recoverable candidate in inspected persistence surfaces. | Options summarizer removes keys and nested contents; both persistence paths store projections. This confirms the mechanism, not the historical database inventory. |
| Does rejection storage retain the rejected input? | Reports rejection rows retained the error response rather than the candidate. | `turn_audit.build_rejection_records` stores serialized rejection responses; its DTO has no candidate input field. |
| Does cancellation retain authoring evidence? | Reports a final cancelled provider call and a deadline terminal. | Cancelled tool invocations retain arguments in memory, but cancellation cohort serialization uses the same lossy storage projector. No returned result is available for an interrupted dispatch. |
| Was the failure a provider refusal or an output limit? | Report attributes the incident to a repair loop ending at the operation deadline. | Not independently tested here. D20 is a local evidence-loss mechanism regardless of the incident's other causes. |
| Does an ordinary prompt survive current options projection? | Report says it does not. | A small synthetic execution of the actual current helper removed the prompt and review term; its assertions passed with exit 0. |

No paid provider calls, broad tests, credentials, production state, or
restricted SQL/image implementation were used. The synthetic check imported
both `elspeth` and `elspeth_lints` from the intended worktree source roots;
it did not rebuild an environment or modify source/tests.

## Where the evidence is lost

1. [redaction.py](../../src/elspeth/web/composer/redaction.py), lines
   1334–1405: `_summarize_option_shape` retains only container shape,
   immediate entry count, and child-shape counts. It removes option key
   names and nested contents. `_summarize_set_source_options` deliberately
   groups prompt text, paths, and credentials together. `_LlmJsonObject`
   attaches this summarizer to source/node/output option surfaces and their
   nested pipeline/patch forms. `redact_tool_call_arguments`, lines
   2511–2529, identifies the result as the representation stored in
   `chat_messages.tool_calls`.
2. [turn_audit.py](../../src/elspeth/web/composer/turn_audit.py), lines
   276–359: decoded arguments or original wire arguments are still
   available before persistence. Argument errors become class/count
   summaries; other arguments pass through the manifest walker. Lines
   368–391 project tool responses separately. Both persistence calls,
   lines 410–430, receive these projected structures. `raw_content` is
   assistant prose, not a copy of tool arguments.
3. [audit_storage.py](../../src/elspeth/web/composer/audit_storage.py),
   lines 190–205 and 246–254: the alternate serializer replaces canonical
   argument/result payloads with redacted canonical payloads and recomputes
   their hashes. Its docstring explicitly limits exact argument/result
   retention to memory. A digest cannot reconstruct the deleted payload.
4. [turn_audit.py](../../src/elspeth/web/composer/turn_audit.py), lines
   175–202, and [_persist_payload.py](../../src/elspeth/web/sessions/_persist_payload.py),
   lines 141–157: rejection evidence retains the response the planner saw,
   including its explanation, but not the input candidate that caused it.
5. [composer_turn.py](../../src/elspeth/web/sessions/composer_turn.py),
   lines 573–607 and 637–647: cancellation/recovery cohorts serialize tool
   invocations through `redacted_tool_invocation_content_and_envelope`.
   [audit.py](../../src/elspeth/web/composer/audit.py), lines 847–874,
   already retains cancelled-dispatch arguments in its in-memory
   invocation. Its result is explicitly absent. A normal-turn-only repair
   would leave this loss in place.

## Synthetic positive and negative controls

The check executed the actual `_summarize_set_source_options` and
`scrub_credential_material` functions with synthetic data. The ordinary
options contained:

```json
{
  "prompt_template": "Classify {{ row.case_study }} using {{ row.category }}.",
  "interpretation_requirements": [{
    "user_term": "service type",
    "llm_draft": "PCS/LH/Unknown",
    "review_id": "service type:classify_pcs_lh"
  }],
  "temperature": 0,
  "api_key": {"secret_ref": "classification-provider"}
}
```

The credential control replaced `api_key` with a constructed, non-live
credential-shaped string and retained the ordinary prompt. The check
asserted both ordinary-content loss and synthetic-credential exclusion;
it did not print the constructed credential. Observed output:

```json
{
  "ordinary_options_projection": {
    "_option_shape": "mapping",
    "entry_count": 4,
    "value_shape_counts": {
      "mapping": 1, "scalar": 2, "sequence": 1, "set": 0
    }
  },
  "credential_options_projection": {
    "_option_shape": "mapping",
    "entry_count": 2,
    "value_shape_counts": {
      "mapping": 0, "scalar": 2, "sequence": 0, "set": 0
    }
  },
  "ordinary_prompt_retained_by_options_projection": false,
  "ordinary_review_term_retained_by_options_projection": false,
  "synthetic_secret_retained_by_options_projection": false,
  "ordinary_prompt_retained_by_shared_scrubber": true,
  "safe_secret_ref_retained_by_shared_scrubber": false,
  "synthetic_secret_retained_by_shared_scrubber": false
}
```

This demonstrates a policy loss rather than a detector identifying actual
credentials in those ordinary options. It also shows why replacing the
walker with the shared scrubber is incomplete: that scrubber replaces
secret-named parent values before inspecting reference shape
([credential_material.py](../../src/elspeth/contracts/credential_material.py),
lines 495–502). A validated `secret_ref` under `api_key` disappears too.
The admission classifier already distinguishes valid references at lines
263–280; a diagnostic projection must preserve that distinction.

The synthetic check covers these pure helper behaviors only. It does not
establish database durability, cancellation correctness, public-response
isolation, full detector coverage, or production incident causation.

## Exact wire, canonical evidence, and display evidence

These are separate records and must have separate names and bindings:

- **Original argument wire** is the provider-authored tool argument string.
  It retains whitespace, escaping, key order, duplicate-key syntax, and
  malformed JSON. Parsing and reserialization cannot reconstruct it.
- **Canonical authored arguments/results** are normalized semantic objects.
  Existing invocation hashes bind these canonical payloads, including a
  separate reversible pipeline authority representation. Retaining them
  preserves the candidate's ordinary semantic values, not necessarily the
  original wire syntax.
- **Credential-filtered diagnostic evidence** preserves ordinary authored
  content and replaces actual credential values or spans. If filtering
  changed a payload, call it a filtered copy; never label it an exact
  original. Record a bounded filtering disposition without recording the
  removed credential or a recoverable secret-derived identifier.
- **Display projection** is the current summarized public representation.
  Its digest must not silently replace an original semantic binding.

Keep independent bindings for available originals/canonical bytes,
retained filtered bytes, and display bytes. A pre-filter digest beside a
filtered-only record commits to unavailable input; it is not independently
recomputable from that record. Do not expose secret-containing input digests
as public locators or treat them as unlabelled replay authority.
A missing result means no result was returned; a filtering or
parse disposition must not masquerade as a successful empty result. Bounds
must have explicit outcomes: truncating an admitted candidate silently
would recreate D20. If inline limits are insufficient, retain the complete
admitted safe payload in a bound private artifact rather than discard it.

## Existing storage and access seams

The session message store already distinguishes display from private
evidence through assistant `raw_content` and rejection records. Their
semantics are specific; neither should be overloaded with diagnostic JSON.
[messages.py](../../src/elspeth/web/sessions/routes/messages.py), lines
74–117, verifies session ownership and records audit-grade reads for
existing opt-ins.

[routes/_helpers.py](../../src/elspeth/web/sessions/routes/_helpers.py),
lines 1742–1766, excludes all `role="audit"` rows from ordinary conversation
and prompt history. Lines 1770–1784 expose only the closed LLM/planner
evidence kinds in the LLM audit opt-in. A distinct diagnostic kind therefore
has a useful existing exclusion seam. It still needs an explicit private
read path with ownership and read attribution.

The existing message response deep-thaws stored `tool_calls` and adds
outcome metadata, then returns them at lines 592–619 and 650. Preserving
full authored options in the existing assistant display envelope would
publish them through that response. Diagnostic storage must be separate.

[FilesystemPayloadStore](../../src/elspeth/core/payload_store.py) provides
content-addressed, integrity-verified storage with restricted directory/file
modes. It is plaintext and has no Composer session/operation binding or
diagnostic authorization API. Using it adds cross-store durability,
retention, and ownership work. Source inspection found Fernet encryption
for the user secret store and authenticated encryption for SSO transactions;
neither is a generic encrypted Composer evidence service. No deployed
encryption or access settings were inspected.

## Two concrete designs

| Design | Durable content and retrieval | Work and limitations |
|---|---|---|
| **A. Credential-filtered private evidence in existing messages — recommended** | A nominal, versioned evidence DTO in separate `role="audit"` rows with a distinct diagnostic kind. Contains safe original wire where eligible, canonical arguments, available results, status, tool-call/session/operation/request correlation, versions, timestamps, binding hashes, and explicit filter/parse dispositions. An explicit private diagnostic read verifies ownership and records access. | No new table/column is inherently required. Extend normal-turn and alternate atomic writers so evidence siblings commit with their display cohort. Preserve safe references and ordinary prompts/review content; exclude resolved credentials. This preserves complete ordinary authoring evidence but cannot promise exact original bytes where credentials were removed. |
| **B. Encrypted exact originals** | Exact wire/canonical payloads in a distinct sealed evidence envelope or private artifact. Authenticated encryption binds session, operation, tool-call and evidence version. Authorized retrieval decrypts originals separately from every display/history path. | Requires an explicit owner decision to retain accidental credential bytes, who may decrypt them, retention/export behavior, and application key custody/rotation. Encryption alone does not authorize raw-secret retention. Requires fresh nonces, key IDs, integrity failure behavior and tested rotation/recovery. Existing secret-store keys must not be silently repurposed. No such diagnostic service is currently wired. |

Design A targets the emergency requirement to retain prompts, terms,
IDs, ordinary options, rejected candidates and available results while
excluding declared credential fields, resolved secrets and finite recognized
credential forms. Syntax cannot distinguish every unlabelled opaque secret
from ordinary authored data. Retention eligibility must therefore state its
credential/reference policy explicitly; detector success cannot promise
that arbitrary opaque strings contain no credentials. Design B is appropriate
only if exact originals containing accidental credential bytes are an
explicitly accepted product requirement. Do not implement B under the guise
of disabling redaction or hide raw originals in unrelated fields.

For A, the evidence builder must distinguish validated reference markers
from actual credential values, inspect decoded JSON when available, and
handle malformed wire without an escape-based bypass. It must also inspect
results and failure paths: earlier argument admission does not prove a
first-party response contains no resolved secret. Keep secret resolution in
its owned execution boundary; never serialize resolved plugin configs,
secret-service objects, exception reprs, locals or transport state as
authoring evidence. Detection is a backstop, not proof that an arbitrary
opaque string cannot be a credential.

"Safe original wire where eligible" must be an explicit disposition,
not an undecoded-text scan followed by a raw fallback. Escaped property
names/values, duplicate keys, malformed tails and traversal-limit failures
need defined outcomes. Retain a supported structured projection where its
eligibility can be established, and mark raw wire unavailable or filtered
when it cannot. Design A cannot guarantee both exact arbitrary malformed
bytes and absence of every secret; exact-original retention has the separate
policy boundary described in Design B.

## Smallest complete implementation slices

1. **Evidence contract and credential projection.** Add one shared nominal
   DTO/builder and explicit wire/canonical/filter dispositions. Preserve
   ordinary nested values and validated reference markers. Establish
   synthetic positive/negative controls before persistence wiring. This
   slice is independently testable but does not fix D20 by itself.
2. **Atomic persistence across outcomes.** Add private evidence siblings to
   the normal turn writer and alternate/cancelled cohort writer. Include
   successful and rejected calls, ARG_ERROR/malformed arguments, plugin
   failures and cancelled dispatches. Share the builder; avoid a second
   divergent projection. Preserve required-work settlement, deduplication,
   fencing and durable-cohort semantics. This is persistence/contract work
   even if the existing schema is retained.
3. **Private retrieval and public isolation.** Add an explicit diagnostic
   read with session ownership and access attribution. Keep evidence out of
   default conversation, tool display, LLM audit views, provider history,
   and ordinary exports unless separately authorized. Return enough
   correlation and filter disposition for operators to replay the ordinary
   candidate and understand any intentional omission.

An end-to-end repair is not a sub-30-minute summarizer change. The slices
can be reviewed separately, but D20 remains open until all three are wired
and validated. A new table or encrypted artifact would expand persistence
scope further. No schema or restricted SQL/image work is authorized by this
design note.

## Concrete acceptance checks

- Roundtrip exact ordinary semantic values for nested options, prompt
  templates/parts, review terms/drafts/IDs, and rejected candidates. Verify
  original wire separately with whitespace, escapes, duplicate keys and
  malformed input; do not assert wire equivalence from canonical equality.
- Retain safe `secret_ref` markers while excluding synthetic resolved
  credentials in arguments, responses, errors and malformed wire. Verify
  ordinary content surrounding a removed credential remains legible where
  a safe span projection can establish that separation. Mark any whole
  value removal explicitly.
- Exercise escaped sensitive keys/values, duplicate-key alternatives,
  opaque strings, malformed tails and bounds failures. An unclassified raw
  original is never a fallback for a failed projection.
- Cover normal success, semantic rejection without state mutation,
  ARG_ERROR, plugin crash, cancelled dispatch, cancellation during an
  executing tool, and repeated cancellation during persistence. Preserve
  available input with an absent unreturned result. Do not invent a
  candidate for provider output that never arrived.
- Exercise cancellation after provider return/before decode, before tool
  dispatch, and after tool completion/before cohort commit. Retain received
  but undispatched calls and completed available results, distinguishing
  received/not-dispatched from dispatched/cancelled status.
- Force writer and evidence-construction failures. No successful terminal
  or durable-cohort marker may claim private evidence committed when it did
  not. Retries must not duplicate or cross-bind evidence.
- Refuse cross-session diagnostic reads, attribute authorized reads, and
  assert that default/display/provider-history paths contain no private
  evidence. Keep original and display digest semantics distinct.
- Treat possession of an evidence/message/payload ID as insufficient read
  authority. Refuse disclosure if access attribution fails. Keep the
  private kind excluded from generic audit views, forks and exports unless
  explicitly designed otherwise; caller-controlled kind selection cannot
  reclassify a public row as private evidence.
- Verify restart roundtrips and filtered-payload integrity. Mutating retained
  filtered content while preserving its stored digest must fail, separately
  from session/operation/tool-call binding mismatch controls.

Relevant existing tests include `test_redact_set_source.py` for public
option projection, `test_compose_loop_persistence.py` for manifest
persistence and cancellation while a tool is executing, and
`test_composer_required_audit_custody.py` /
`test_composer_failure_audit_owner.py` for required cohort ownership.
Those suites were read but not executed for this note. Persistence changes
require the repository's affected checks, relevant whole-tree gates and
PostgreSQL contention coverage before integration; qualification must be
coordinated with the active integration owner. The one executed synthetic
helper check is narrower evidence and does not replace those gates.
