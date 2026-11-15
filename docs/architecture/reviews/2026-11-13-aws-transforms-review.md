# AWS transform plugins — code review, 2026-11-13

Scope: `src/elspeth/plugins/transforms/aws/` (14 modules) at commit `f585ee70`,
including the `p3-cloud-transforms` lane changes (`6ac92501` … `f897d4d2`).
Read-only review; no source or test file was changed.

## Summary

The directory is in better shape than its scrutiny history suggests. Tier-3
parsing is strict and fail-closed almost everywhere, every SDK call goes through
an audited adapter, provider text and document bytes are kept out of telemetry,
and the guardrail transforms have no fail-open path that I could find: an
error, a skipped field, partial coverage, or a malformed response all end in a
row error, never in a "validated" success.

The real defects are in what happens *around* the calls, not in the calls:

- Transient Textract failures are never retried. The plugins signal them with
  `TransformResult.error(retryable=True)`, which the engine does not read.
- Verify mode cannot work for the asynchronous Textract transform, and replay
  of it depends on wall-clock timing and thread scheduling.
- The guardrail transforms have no row deadline, no shutdown check and no
  in-place handling of throttling, all of which the Azure siblings have.

Counts: 1 × P1, 3 × P2, 7 × P3, 6 notes, 5 items needing confirmation.

How each finding was checked is stated per finding. "Probe" means a throwaway
script run against this worktree (`elspeth.__file__` confirmed inside it);
"read" means both ends of the code path were read but nothing was executed.
The existing suites for these modules pass: `pytest -n 0
tests/unit/plugins/transforms/aws tests/unit/plugins/transforms/test_cloud_transforms_p3_sweep.py`
exited 0 with 559 passed. No live AWS call was made, so anything that depends on
real provider behaviour is in "Needs confirmation".

## Findings

| # | Sev | Where | Finding | Checked by |
|---|-----|-------|---------|------------|
| 1 | P1 | `textract_document_analysis.py:828,910,1006,1012,1023,1034`; `textract_inline_analysis.py:584` | Retryable Textract failures are permanent row errors: `retryable=True` on a result is dead | read (engine + Azure sibling) |
| 2 | P2 | `textract_client.py:809-817,869` | Verify mode always fails at the first `GetDocumentAnalysis`: the request hash contains the live job id | read |
| 3 | P2 | `textract_document_analysis.py:1005,1036`; `textract_bucket_region.py:579-605` | Replay of the async transform depends on wall-clock time and on which thread wins the HeadBucket single-flight | read |
| 4 | P2 | `_guardrail_transform.py:233-284` | Guardrail rows have no deadline, no shutdown check, and treat throttling as a permanent, undifferentiated failure | read |
| 5 | P3 | `guardrails_client.py:381,386` | Unhashable provider value raises `TypeError` out of the Tier-3 parser; the call is made but not recorded | probe |
| 6 | P3 | `textract_client.py:306-307`; `textract_bucket_region.py:110,223-228` | Attempt counts are still under-recorded for some transport failures and for every HeadBucket failure | probe |
| 7 | P3 | `textract_inline_analysis.py:169-176` | Inline transform has none of the ceilings `6978dbeb` gave the async one | probe |
| 8 | P3 | `_guardrail_transform.py:65-78` | Transform config accepts a guardrail ARN whose region or partition contradicts `region` | probe |
| 9 | P3 | `textract_result.py:255-286` | `_project_pages` is O(pages × blocks) under the GIL | probe (timed) |
| 10 | P3 | `textract_document_analysis.py:697` | Per-row S3 key is bounded in characters, not bytes | read |
| 11 | P3 | tests (see below) | Several tests pin behaviour that does not hold in the engine | read |

### 1. P1 — retryable Textract failures are never retried

`AWSTextractDocumentAnalysis` and `AWSTextractInlineAnalysis` report transient
failures by returning `TransformResult.error(..., retryable=error.retryable)`:

- `submit_failed` (`textract_document_analysis.py:910`)
- `poll_failed` (`:1012-1015`)
- `poll_timeout`, hard-coded `retryable=True` (`:1006,1023,1034`)
- `bucket_region_unverified` (`:828`)
- inline `analysis_failed` (`textract_inline_analysis.py:584-587`)

Nothing in the engine reads that flag. `RowProcessor` retries only when the
transform *raises* `PluginRetryableError`, `ConnectionError`, `TimeoutError` or
`CapacityError` (`engine/processor.py:2542-2552`); a grep for a consumer of
`result.retryable` under `src/elspeth/engine` finds none. The Azure sibling
says so in as many words (`azure/base.py:439-442`: "`TransformResult.error(retryable=True)`
was dead code: the engine retries by catching PluginRetryableError, not by
inspecting results") and `azure/analyze_lro.py:537,540` raises accordingly.

Concrete effect: 30 workers each polling `GetDocumentAnalysis` at a 1 s initial
interval. One poll draws `ThrottlingException` after botocore's three attempts.
The row returns `poll_failed` and is diverted to `on_error` for good, whatever
retry policy the pipeline configures, and the Textract job it already started
(and will be billed for) is abandoned. The same happens for a submit-side
`LimitExceededException`, a transport error, and a poll timeout. The
deterministic `ClientRequestToken` (`:750-780`) exists precisely so a row retry
re-attaches to the same job, but that retry never happens.

The plugin's own guidance is also wrong as a result: the inline transform's
composer hint says "SDK and engine retries can each repeat a billable provider
call" (`textract_inline_analysis.py:792`); there are no engine retries.

Suggested fix:
- Raise `PluginRetryableError(retryable=True)` for the submit-side and inline
  cases, as `analyze_lro.py` does, after the audited client has recorded the
  call (it already records before raising its own error).
- In the poll loop, treat a retryable `TextractServiceError` as "sleep and poll
  again until the deadline" instead of ending the row. The Azure LRO has this
  exact behaviour and a test for it
  (`test_f61_poll_network_error_is_retried_in_place_without_resubmitting`).
- Decide deliberately whether `poll_timeout` should escalate to a row retry;
  today it claims to and does not.

### 2. P2 — verify mode cannot pass for `aws_textract_document_analysis`

`on_start` supports `RunMode.VERIFY` (`textract_document_analysis.py:544-563`),
but the adapter cannot get past the first poll.

`get_document_analysis` builds its audited request with the live `job_id`
(`textract_client.py:809-817`) and admits it with no source-run substitution
(`:869`, `lookup_request` is not passed). `AuditedCallModeSession.admit_verify_call`
requires an exact request-hash match at the parent-local index
(`engine/orchestrator/call_mode_session.py`, `_source_call`). In a verify run
the `ClientRequestToken` is derived from the *verify* run id, so Textract starts
a new job with a new `JobId`; the hash cannot match the source run's and
admission raises `AuditIntegrityError("No exact source http call …")`.

`start_document_analysis` handles the same problem correctly for its token by
swapping in `source_client_request_token_fingerprint` (`:677-687`); the job id
and `next_token_fingerprint` (also provider-generated) need the same treatment.
Even with that fixed, a live job will not take the same number of
`IN_PROGRESS` polls as the source run, so index-exact matching of poll calls
needs a design decision (for example: verify the start and the terminal result
pages, and treat `IN_PROGRESS` polls as non-compared).

This is unnoticed because the only verify test for the adapter covers
`start_document_analysis` and uses a `Session` fake whose `admit_verify_call`
accepts anything (`tests/unit/plugins/transforms/aws/test_replay_sdk_adapters.py:47-53,329-363`).

### 3. P2 — replay of the async transform is timing-dependent

Replay matches each call by audit parent, request hash and parent-local call
index. Two parts of the async transform make that sequence depend on timing.

**Poll loop.** `_poll_and_collect` ends on `time.monotonic() >= deadline`
(`:1005`) and really sleeps between polls (`:1036`) in every mode. Take a
source row with `poll_timeout_seconds: 60`, a fixed 10 s interval and 2 s of
latency per live poll: it polls at 0, 12, 24, 36, 48 s and times out — five
recorded calls. Replay answers each poll in milliseconds, so it polls at 0, 10,
20, 30, 40, 50 s; the sixth `replay_call` has no source record and the run
dies with `AuditIntegrityError`. A row that ended in `poll_timeout` or
`shutdown_requested` in the source run is therefore not reliably replayable.
Separately, replay of a successful row sleeps for as long as the original job
took, up to the one-hour ceiling per row.

**HeadBucket single-flight.** `BucketRegionCoordinator.verify` makes whichever
row arrives first the leader; only the leader's state gets a HeadBucket call
record, and its later calls are shifted by one index. When a leader's call
fails, followers already waiting inherit the failure with no call of their own
(`textract_bucket_region.py:607-616`), and the next arrival becomes a new leader. In the source run the
leader's failing call takes seconds, so rows 2…k are followers. In replay the
leader's call returns at once, so row 2 arrives after the flight has finished,
becomes a leader, and asks the session for a HeadBucket call that its source
state never made — `AuditIntegrityError`. Even with no failure, which of two
near-simultaneous rows leads is a thread race in both runs.

Suggested fix: in replay, drive both decisions from the archive rather than
from the clock and the lock — skip the sleeps, end the poll loop when the
source state's calls are exhausted, and decide leader/follower by whether the
source state has a HeadBucket call at that index. Recording the leader's
`state_id` or call id in the follower's `bucket_region_verification` evidence
would also make a "cached" or `shared_failure` row traceable to the call that
justified it; today the follower's record names no source.

### 4. P2 — guardrail rows: no deadline, no shutdown check, throttling is terminal

`BedrockGuardrailTransformBase.process` (`_guardrail_transform.py:233-284`)
loops over up to 32 fields and makes one synchronous call per field with
connect 5 s / read 15 s × 3 attempts. Compared with `azure/base.py`:

- **No row deadline.** Azure stops scanning once the row budget is spent
  (`azure/base.py:385-394`, `row_deadline_exceeded`). AWS has no bound: a
  degraded endpoint costs roughly a minute per field, about half an hour for a
  32-field row, in the engine's row thread.
- **No shutdown check** anywhere in the field loop. Azure checks its shutdown
  event inside its capacity-retry loop (`azure/base.py:472-487`).
- **Throttling is permanent.** `GuardrailServiceError.retryable` is computed
  and audited by the client, then dropped; the row gets
  `api_call_failed` / `retryable=False`. Azure retries capacity errors in place
  within the row budget. Under a Bedrock quota this fails rows permanently that
  a short wait would have passed.
- **One error type for everything.** A malformed response
  (`GuardrailResponseError`) and a service failure both become
  `error_type: "guardrail_service_error"` (`:273-284`). An operator reading the
  row cannot tell a throttle from an access-denied from a response the parser
  rejected without opening the call record.

None of this is fail-open: every path ends in a row error. It is a
reliability and supportability gap in a required control. Suggested fix: carry
`malformed_response` / `service_error` and the retryable bit into the row
reason, add a row deadline and shutdown check as in the Azure base, and either
retry throttles in place or raise `PluginRetryableError` (see finding 1).

### 5. P3 — unhashable provider value escapes the Tier-3 parser

`parse_guardrail_response` tests `confidence`, the filter `action` and
`filterStrength` with `not in <frozenset>` before checking their type
(`guardrails_client.py:381,386`). A list or dict there raises `TypeError`, which
is not a `GuardrailResponseError`, so it bypasses every `except` in
`apply_guardrail` and the audit write after it.

Probe: a fake SDK returning `confidence: ["HIGH"]` produced `TypeError`, one SDK
call, zero audit records. The control (`confidence: "BOGUS"`) produced
`GuardrailResponseError` and one audit record.

The run crashes rather than passing the row, so this is not fail-open, and
botocore would have to hand back a wrong-typed member for it to happen. But it
is a provider-data path that crashes the pipeline and leaves a made call
unrecorded. Fix: `type(x) is not str or x not in …`, as the same function
already does for `filter_type`.

### 6. P3 — attempt counts still under-recorded

`ec42701c` counts started HTTP attempts with a `before-send` hook. Two gaps
remain:

- `textract_client._provider_error` returns a hard-coded `1` for any
  `BotoCoreError` that is not one of four named classes (`:306-307`).
  botocore's standard mode retries every `ConnectionError` and
  `HTTPClientError`, which includes `SSLError`, `ProxyConnectionError`,
  `ResponseStreamingError` and the generic `HTTPClientError`. Probe with a real
  botocore Textract client: `SSLError` → 3 HTTP attempts, audited `attempts: 1`,
  `retryable: False`. The control (`EndpointConnectionError`) audited 3. The
  guardrail client gets this right (`guardrails_client.py:444-445` returns the
  observed count). For `AnalyzeDocument`, where each attempt may bill, the
  attempt count is the billing evidence.
- `HeadBucketClient` has no hook at all: `build_s3_head_bucket_sdk_client`
  registers nothing and `_provider_outcome` returns `1` for transport failures
  (`textract_bucket_region.py:110,223-228`).

Fix: return `observed_attempts` in the non-`ClientError` branch, classify by
`isinstance(error, (ConnectionError, HTTPClientError))` to match botocore's own
retry rule, and give the S3 client the same hook.

### 7. P3 — inline transform has no configuration ceilings

`6978dbeb` capped the async transform's `poll_timeout_seconds`,
`batch_wait_timeout_seconds`, `max_result_pages`, `max_blocks` and
`max_result_bytes`. The inline transform was not touched. Probe: its config
accepts `max_blocks=10**12`, `max_result_bytes=10**15`,
`request_timeout_seconds=1e9` and `batch_wait_timeout_seconds=1e12`
(`textract_inline_analysis.py:169-176`); the async config rejects an oversized
`max_result_bytes`. An author can therefore hold an inline worker on one
request indefinitely. The 5 MiB input bound limits real response size, so the
practical exposure is the two timeouts. Fix: add `le=` ceilings, defaults at
the ceiling, as in the async config.

### 8. P3 — transform config does not cross-check a guardrail ARN

`BedrockGuardrailProfileSettings` rejects an ARN whose partition is not `aws`
or whose region differs from `region` (`guardrail_profiles.py:148-153`).
`BedrockGuardrailTransformConfig` reuses the three field validators but not
that model validator (`_guardrail_transform.py:65-78`). Probe: the transform
config accepts `arn:aws:bedrock:us-west-2:…` with `region: us-east-1`, and an
`arn:aws-us-gov:…` ARN; the profile model rejects the first. A YAML-authored
pipeline with that mistake validates cleanly and then fails every row with
`api_call_failed`. Fix: move the check into a shared function used by both.

### 9. P3 — `_project_pages` is quadratic

For each PAGE block the function walks every block and formats an f-string
path for each (`textract_result.py:262-266`). Timed in this worktree: 100
pages / 6,100 blocks 0.17 s; 200 / 12,200 0.72 s; 400 / 24,400 3.05 s — four
times per doubling, about 0.3 µs per page-block pair. At the configured
ceilings (3,000 pages, 200,000 blocks) that is on the order of minutes of
GIL-held CPU for one document, during which the other 29 workers and the
engine thread stall. Fix: bucket LINE blocks by page in one pass.

### 10. P3 — per-row S3 key bounded in characters, not bytes

Static-bucket mode checks `len(executable_key.encode("utf-8")) > S3_MAX_KEY_BYTES`
(`:737`). Per-row bucket mode checks `1 <= len(key) <= 1024` characters
(`:697`). A 600-character key of two-byte characters passes local validation
and is rejected by AWS, surfacing as `submit_failed` with a provider code
instead of `invalid_input`. Fix: use the byte check in both branches.

### 11. P3 — tests that pin behaviour the system does not have

- `test_poll_timeout_is_retryable`, `test_submit_service_error_preserves_retryability`,
  `test_poll_service_error_preserves_retryability`,
  `test_unverified_bucket_region_stops_textract_and_preserves_retryability`
  (`tests/unit/plugins/transforms/aws/test_textract_document_analysis.py:694,886,1014,1053`)
  assert `result.retryable`. They pass, and the rows are still never retried
  (finding 1). No test drives a Textract transform through the engine's retry
  path.
- `test_textract_verify_records_live_call_and_compares_source_request_identity`
  uses a session fake that admits any request and covers only the start call
  (finding 2). There is no verify-mode test for `get_document_analysis` and no
  multi-row replay test for the bucket-region coordinator (finding 3).
- The Textract attempt-count tests invoke `_record_send_attempt` by hand from a
  fake SDK (`test_textract_client.py:442,447`) or emit the event manually
  (`:564`). Only the guardrail client has a test that lets real botocore fire
  the hook (`test_f50_…`). The Textract hook does work — the probe in finding 6
  confirmed it — but no test would notice if the event name stopped matching.
- `test_malformed_responses_fail_closed` has no wrong-*type* mutators for the
  filter enum members, which is how finding 5 went unseen.

## Notes

- **Guardrail calls are not bound to a field.** The audited request is
  `{operation, target_fingerprint, source, required_filters}`
  (`guardrails_client.py:618-625`), identical for every field of a row. Which
  field a call covered is recoverable only from call order. Adding the field
  name (not the text) would cost nothing and make each record self-describing.
- **`target_fingerprint` is not secret.** Its HMAC key is
  `sha256("elspeth-bedrock-guardrail:" + run_id)` (`_guardrail_transform.py:222`),
  derivable by anyone who can read the run id. It works as a per-run
  pseudonym, which may be all that is intended; it is not confidentiality.
- **Latency includes rate-limiter wait.** `started = time.perf_counter()` is
  taken before `_acquire_rate_limit()` in all three Textract methods
  (`textract_client.py:729-733,870-874,1080-1084`).
- **`poll_interval_seconds` has no lower bound** (`gt=0`). `1e-9` with
  multiplier 1 is accepted and polls as fast as the SDK allows for up to an
  hour, one audit record per poll. The Azure LRO config has the same shape.
- **Followers re-raise the leader's exception object.** For a non-
  `BucketRegionUnverifiedError` failure, every follower does
  `raise flight.error` on the same instance from different threads
  (`textract_bucket_region.py:617-618`).
- **`shutdown_requested` is a permanent row error** in both Textract
  transforms, as it is in the Azure LRO. Rows in flight at a graceful stop are
  diverted rather than left for resume. Consistent with the sibling; flagged
  only so it is a known choice.
- **Field selection differs from Azure.** Azure accepts `fields: "all"`; the
  AWS transforms require an explicit list, yet the prompt-shield composer hint
  refers to "a dominating all-fields shield" that this plugin cannot be.

## Needs confirmation

These rest on provider behaviour I could not observe without a live call.

1. **Geometry bounds may reject real documents.** `_geometry` requires every
   `BoundingBox` and `Polygon` value in `[0, 1]` and `RotationAngle` in
   `[0, 360]` (`textract_result.py:160-192`). My recollection is that Textract
   returns slightly negative or >1 coordinates for skewed or rotated content,
   and possibly negative rotation angles. If so, such documents fail whole as
   `malformed_response`. Worth checking against a rotated scan.
2. **Guardrails with more than content filters.** The parser rejects an
   assessment containing any key other than `contentPolicy`, `invocationMetrics`
   and `appliedGuardrailDetails` (`guardrails_client.py:355`), and requires the
   filter set to equal the required set exactly (`:396`). If Bedrock returns
   `topicPolicy`, `wordPolicy` or `sensitiveInformationPolicy` for a guardrail
   that has them configured, or returns `PROMPT_ATTACK` alongside the content
   filters, every row fails closed as malformed. The live check should catch
   this at profile approval, but a YAML-authored pipeline has no live check.
3. **`appliedGuardrailDetails` is validated but not compared.** The guardrail
   id, version and origin in the response are shape-checked and discarded
   (`:268-306`). If an account- or organization-enforced guardrail can be the
   single assessment returned, the decision would be attributed to the
   configured guardrail without proof it was the one applied.
4. **Row-supplied bucket names and S3 Express.** Per-row bucket mode allows any
   3–255 character name matching `[0-9A-Za-z._-]` (`textract_document_analysis.py:87,692`).
   A name ending `--x-s3` may make botocore route to an S3 Express endpoint and
   issue a `CreateSession` call that no adapter records.
5. **Batch wait versus SDK worst case.** The async transform's effective batch
   wait is `poll_timeout + 120 s + 90 s` (`:428-431`). One SDK call can take up
   to 3 × (10 s + 30 s) plus backoff, and the rate limiter wait is unbounded, so
   a worker can outlive its waiter. The mixin handles a late result, but a late
   *exception* becomes a run-ending `FrameworkBugError`
   (`batching/mixin.py`, `_raise_orphaned_late_failure`). I did not construct
   the timing.

## What I checked and found sound

- Guardrail fail-closed behaviour: missing field, non-string, empty, over-long,
  partial coverage, malformed response, service error, detect-only positive and
  intervention all return a row error; success is reachable only after every
  configured field returned a parsed, non-detected decision.
- `source: INPUT` on the content-safety transform correctly loses output-control
  credit (`bedrock_content_safety.py`, `is_effective_blocking_control`).
- Provider-authored text (guardrail outputs, action reason) is validated and
  discarded; guardrail input text, document bytes and credentials do not reach
  audit request payloads or telemetry. Textract result content is recorded in
  the audit response by design, which replay requires.
- Replay stand-ins (`ReplayOnlySDK`, `DeferredAWSClient`) cannot dispatch
  before admission, and replayed evidence is re-validated before use.
- Bounded inline reads: `retrieve_bounded` reads at most `max + 1` bytes and
  verifies the hash of accepted content.
- The private shutdown event: `close()` sets only the plugin's own event and
  the engine's event is only read.
- Block-graph validation: duplicate ids, dangling relationships, cycles, PAGE
  numbering and the FORMS `value_selection_status` projection.
- Call indices come from the recorder, so the HeadBucket and Textract adapters
  sharing one state do not collide.
