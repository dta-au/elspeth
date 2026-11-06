# Composer Continuation Physical Outcomes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Preserve the actual complete provider audit with each retained response so recovery does not invent a call, duration, physical identity or successful inspection.

**Architecture:** The real audited provider owner freezes its complete `ComposerLLMCall` before the single response-retention transaction. The response codec retains that exact record, while current-authorized SQL verifies its physical identity and usage. Restored recorders accept the retained historical record once; they do not mint custody or rebuild an audit from financial counters.

**Tech Stack:** Python, Pydantic strict JSON, existing SQLAlchemy continuation and quota owners, ordinary pytest.

This packet amends frames F1/F10/F19/F21 and storage C5.6. It is proposed implementation, not executed validation. Known failure and optional-title dispositions must also be bound before the assembled executable plan receives GO. No provider response or audit row is synthesized by the application.

## Files and execution dependencies

| Path | Responsibility |
| --- | --- |
| `src/elspeth/web/composer/continuation_provider_outcomes.py` | Strict complete audit codec and physical response/audit consistency check. |
| `src/elspeth/web/composer/continuation_codec.py` | F1's `audit_call_json` and `decode_retained_physical_audit_call`. |
| `src/elspeth/web/composer/audit.py` | Once-only historical buffer restoration without new custody. |
| `src/elspeth/web/composer/provider_gateway.py` | Freeze primary/text audit after complete admission, before response SQL. |
| `src/elspeth/web/composer/advisor_checkpoint.py` | Freeze the actual Advisor record at the same owned boundary. |
| `src/elspeth/web/sessions/_auto_title.py` | Freeze the actual title record before received-title retention. |
| `src/elspeth/web/composer/provider_quota.py` | Encode the already frozen actual call inside the single V stage. |
| `src/elspeth/web/sessions/continuation.py` | Verify exact actual effect, call identity, anchored finish and usage before response retention. |
| `tests/unit/web/composer/test_continuation_physical_outcomes.py` | Complete codec and consistency counterexamples. |
| `tests/integration/web/composer/test_continuation_driver_process_death.py` | D6 actual post-response/pre-finally death and historical-record equality. |

Run the physical hooks after frames F1 and F6 and storage C5 types exist. O1 is pure and can precede those hooks; production response retention is enabled only after all corresponding actual callers supply their complete audit. F1 pure codec fixtures may omit `audit_call_json`; C5 SQL never accepts that omission for a real retained response.

The finite initial pure unit installs O1, only O2's in-memory
`restore_received_llm_call` method, and O4's complete ordinary tests alongside
frames F1/F6. It does not install O2's F19/F21 call site, O3 or O5+, or any
bootstrap, selected-source, SQL, read/writer or physical dispatch hook. Their
complete definitions remain below as integration material for their actual
owners. A restored buffer is historical data, not custody or permission to
apply a response. Future physical integration must preserve the current
cost-budget forwarding and separate post-admission dispatch recheck; the
pure cost/allowance roundtrip below does not qualify that integration.

## O1: Retain the complete nominal audit without public-DTO loss

- [ ] Create this complete codec module. Its schema is the actual contract dataclass, including private failure/diagnostic/request fields and native metadata. It does not call `ComposerLLMCall.to_dict()`, whose public envelope intentionally omits private fields. JSON round-trip checks cover all retained fields. The codec's use of owned JSON admits no SDK object or arbitrary Python object.

```python
from __future__ import annotations

from datetime import datetime

from pydantic import TypeAdapter, ValidationError

from elspeth.contracts.composer_llm_audit import ComposerLLMCall, ComposerLLMCallStatus
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.contracts.json_parser import parse_json_strict
from elspeth.web.composer._compose_loop_carriers import _AdmittedLLMProviderMetadata
from elspeth.web.composer.continuation_codec import owned_json


_PHYSICAL_AUDIT = TypeAdapter(ComposerLLMCall)


def encode_physical_audit_call(call: ComposerLLMCall) -> str:
    if type(call) is not ComposerLLMCall:
        raise TypeError("physical audit retention requires its complete actual nominal record")
    projected = _PHYSICAL_AUDIT.dump_python(call, mode="json", warnings="error", fallback=owned_json)
    return canonical_json(owned_json(projected))


def decode_physical_audit_call(canonical: str) -> ComposerLLMCall:
    if type(canonical) is not str:
        raise AuditIntegrityError("physical audit lost its complete canonical JSON")
    value, error = parse_json_strict(canonical)
    if error is not None or type(value) is not dict:
        raise AuditIntegrityError("physical audit is not one strict JSON object")
    try:
        call = _PHYSICAL_AUDIT.validate_json(canonical, strict=True, extra="forbid")
    except (ValidationError, ValueError, TypeError) as exc:
        raise AuditIntegrityError("physical audit violates its owned complete contract") from exc
    if type(call) is not ComposerLLMCall or encode_physical_audit_call(call) != canonical:
        raise AuditIntegrityError("physical audit omitted, changed or normalized retained fields")
    return call


def require_successful_response_audit(
    call: ComposerLLMCall, metadata: _AdmittedLLMProviderMetadata, *, attempt_id: str,
    started_at: datetime, finished_at: datetime,
) -> None:
    if type(call) is not ComposerLLMCall or type(metadata) is not _AdmittedLLMProviderMetadata:
        raise AuditIntegrityError("received response lacks its actual complete nominal audit/metadata")
    usage = metadata.usage
    if (call.status is not ComposerLLMCallStatus.SUCCESS
        or call.call_id != attempt_id or call.started_at != started_at or call.finished_at != finished_at
        or (call.model_returned, call.provider_request_id, call.finish_reason, call.provider_served)
            != (metadata.model_returned, metadata.provider_request_id, metadata.finish_reason, metadata.provider_served)
        or (call.prompt_tokens, call.completion_tokens, call.total_tokens, call.cached_prompt_tokens,
            call.cache_creation_input_tokens, call.cache_read_input_tokens, call.reasoning_tokens)
            != (usage.prompt_tokens, usage.completion_tokens, usage.total_tokens, usage.cached_prompt_tokens,
                usage.cache_creation_input_tokens, usage.cache_read_input_tokens, usage.reasoning_tokens)
        or (call.provider_cost, call.provider_cost_source) != (metadata.provider_cost, metadata.provider_cost_source)
        or canonical_json(owned_json((call.reasoning_content, call.reasoning_details, call.thinking_blocks)))
            != canonical_json(owned_json((metadata.reasoning_content, metadata.reasoning_details, metadata.thinking_blocks)))):
        raise AuditIntegrityError("received response changed its actual physical identity, finish, usage or native audit")
```

To avoid an import cycle, import `encode_physical_audit_call` locally inside F1's encoder, and `decode_physical_audit_call` locally inside its decoder. This module may import the already defined pure `owned_json`; the codec must not import this module at module initialization.

- [ ] Add the exact F1 fields and arguments supplied in the frames packet: `RetainedCompletion.audit_call_json: str | None = None`, `encode_continuation_response(..., audit_call: ComposerLLMCall | None = None)`, and forwarding from the text encoder. Set the field to `encode_physical_audit_call(audit_call)` when non-null. `decode_retained_physical_audit_call(chunk)` rejects missing audit and returns the complete nominal record. These fields describe actual runtime product data, not a plan seal.

## O2: Restore a received historical audit once without granting custody

- [ ] Add this complete method to `BufferingRecorder`. The caller first obtains the exact retained response from C5.9's current-authorized selected-source validator. A call ID is necessary for deduplication but never sufficient to replace different bytes. Existing live `record_llm_call` remains tied to its actual custody.

```python
def restore_received_llm_call(self, call: ComposerLLMCall) -> None:
    from elspeth.web.composer.continuation_provider_outcomes import encode_physical_audit_call
    if type(call) is not ComposerLLMCall or call.call_id is None:
        raise AuditIntegrityError("historical received audit has no actual complete physical identity")
    canonical = encode_physical_audit_call(call)
    with self._lock:
        existing = tuple(item for item in self._llm_calls if item.call_id == call.call_id)
        if existing:
            if len(existing) != 1 or encode_physical_audit_call(existing[0]) != canonical:
                raise AuditIntegrityError("one physical identity contains different complete historical audits")
            return
        self._llm_calls.append(call)
```

- [ ] At F19/F21's received-response branch, after the actual effect/physical/settled-ledger reconciliation and before capturing the restored runtime, insert:

```python
from elspeth.web.composer.continuation_codec import decode_retained_physical_audit_call
runtime.live.recorder.restore_received_llm_call(decode_retained_physical_audit_call(response))
```

Here `response` is the exact selected `PrivateChunk` returned by that branch's real observed/reconciled owner, not a chunk selected by matching response content. Use the same method on the warm captured owner if it already contains the original record; canonical equality makes that a no-op. The subsequent capture uses `commit_received_provider_capture(kind="capture")`, which allows only the owed private owner refs after Stop or segment expiry. No original audit duration is calculated on restore.

## O3: Freeze success before its single response-retention transaction

- [ ] In each actual audited body, move its existing complete `build_llm_call_record(...)` construction (including primary recovery-response diagnostics) into a local zero-argument `build_terminal_call() -> ComposerLLMCall`. The body is the existing owned construction without changed arguments. Its returned value already passes `ProviderCallCustody.bind_call`; the original requested model, pricing, exact detached messages/tools, actual metadata/private request, active monotonic latency and original admission clock remain the inputs. Replace the original finally construction with `call = build_terminal_call()`.

Insert this complete local function after `_call_llm_with_audit_body` initializes its audit locals and before its `try`. Import `ComposerLLMCall` from its contract alongside the existing status import. The existing response-diagnostics imports and `dataclasses.replace` stay in their actual owner module. Replace only the original finally construction/diagnostics span with the call to this function; keep the custody/recorder/exception suffix.

```python
def build_terminal_call() -> ComposerLLMCall:
    if status is None:
        raise AuditIntegrityError("primary audit has no actual terminal outcome")
    call = build_llm_call_record(
        model_requested=self._model, pricing_model=self._settings.composer_pricing_model,
        messages=wire_messages, tools=wire_tools or None, status=status,
        started_at=started_at, started_ns=started_ns,
        temperature=self._settings.composer_temperature, seed=self._settings.composer_seed,
        response_metadata=response_metadata, error_class=error_class, error_message=error_message,
        provider_custody=provider_custody, failure=failure_to_capture, private_request=private_request,
    )
    if recovery_batch is not None:
        if call.call_id is None:
            raise AuditIntegrityError("Received recovery control has no physical provider call identity")
        received = capture_received_response({"choices": [{"message": {"tool_calls": [
            {"id": tool.id, "function": {"name": tool.function.name, "arguments": tool.function.arguments}}
            for tool in recovery_batch.calls
        ]}}]}, namespace=call.call_id, ordinal=1,
            known_tools=frozenset(tool["function"]["name"] for tool in wire_tools))
        call = replace(call, private_diagnostics=bind_received_response(received,
            physical_call_id=call.call_id,
            refusal_code=None if status is ComposerLLMCallStatus.SUCCESS else error_message))
    return call
```

Insert this complete local function in `_call_text_llm_with_audit_body` after its actual audit locals. Replace that body's original finally construction with `call = build_terminal_call()`.

```python
def build_terminal_call() -> ComposerLLMCall:
    if status is None:
        raise AuditIntegrityError("diagnostics audit has no actual terminal outcome")
    return build_llm_call_record(
        model_requested=self._model, pricing_model=self._settings.composer_pricing_model,
        messages=messages, tools=None, status=status, started_at=started_at, started_ns=started_ns,
        temperature=self._settings.composer_temperature, seed=self._settings.composer_seed,
        response_metadata=response_metadata, error_class=error_class, error_message=error_message,
        provider_custody=provider_custody, failure=failure_to_capture, private_request=private_request,
    )
```

Insert this complete local function in the actual `AdvisorCheckpointOwner._call_advisor_with_audit_body` after the audit locals are initialized. Use the same function in its original finally construction.

```python
def build_terminal_call() -> ComposerLLMCall:
    if status is None:
        raise AuditIntegrityError("Advisor audit has no actual terminal outcome")
    return build_llm_call_record(
        model_requested=advisor_model, pricing_model=self._settings.composer_advisor_pricing_model,
        messages=messages, tools=None, status=status, started_at=started_at, started_ns=started_ns,
        temperature=self._settings.composer_temperature, seed=self._settings.composer_seed,
        response=response, response_metadata=response_metadata,
        error_class=error_class, error_message=error_message,
        credential_surface="composer_advisor_response", provider_custody=provider_custody,
        private_request=private_request,
    )
```

- [ ] Add this complete branch immediately after successful complete admission and setting `status = ComposerLLMCallStatus.SUCCESS`, before the successful return. It runs only for actual v2 custody. The complete record is built once and warm-recorded before the SQL await. The existing finally `needs_terminal_audit()` guard sees that actual bound record and does not rebuild it. SQL/cancellation errors preserve the original actual call instead of overwriting successful received evidence with a later retention failure.

```python
if provider_custody is not None and provider_custody.continuation_binding is not None:
    call = build_terminal_call()
    provider_custody.retain_audit(call)
    if recorder is not None:
        recorder.record_llm_call(call, provider_custody=provider_custody)
    await provider_custody.retain_continuation_response(completion)
```

This is the primary `_call_llm_with_audit_body` branch. Remove C5's superseded retention hook from inner `_call_llm`; the inner admitted response has not yet passed the outer recovery-only/reply-only validation. Retaining before that validation could store a false successful completion.

For `_call_text_llm_with_audit_body`, use the same complete branch with its actual admitted `content` and `response_metadata`:

```python
if provider_custody is not None and provider_custody.continuation_binding is not None:
    call = build_terminal_call()
    provider_custody.retain_audit(call)
    if recorder is not None:
        recorder.record_llm_call(call, provider_custody=provider_custody)
    await provider_custody.retain_continuation_text_response(content=content.strip(), provider_metadata=response_metadata)
```

Remove the earlier inner `_call_text_llm` retention hook. Preserve the current actual successful return's stripping behavior: the retained `content` must be exactly the returned admitted text (`content.strip()` for diagnostics). Do not claim an unstripped value was returned.

For Advisor, the corresponding branch follows the actual complete `guidance`, `metadata` and SUCCESS assignment, after all content/native/credential checks. Its actual metadata dict was already measured by the real provider body. Freeze it without recalculation:

```python
if provider_custody is not None and provider_custody.continuation_binding is not None:
    from elspeth.web.composer.continuation_codec import RetainedAdvisorResultMetadata
    call = build_terminal_call()
    provider_custody.retain_audit(call)
    if recorder is not None:
        recorder.record_llm_call(call, provider_custody=provider_custody)
    if continuation_budget is not None:
        continuation_budget.record_call(call)
    await provider_custody.retain_continuation_text_response(content=guidance,
        provider_metadata=response_metadata,
        advisor_result_metadata=RetainedAdvisorResultMetadata(**metadata))
```

The generic v2 Advisor body must use its actual variables rather than invoking the provider again. Preserve its measured `continuation_budget.record_call(call)` once at the same original successful record point; it must not disappear merely because finally skips a prebuilt call. This records the real already completed call, not an allowance for a second SDK invocation.

### O3 TITLE: Freeze the complete actual title audit before retention

**Source binding:** `src/elspeth/web/sessions/_auto_title.py` at immutable
`9f321eb8da5b5f2214caf163561875626b247954`,
`_maybe_auto_title_session_required:440–556`; its actual audit constructor
is `composer/llm_response_parsing.py:669–789`. Apply after F10's prepared
request/persistence/outcome amendments. The original request, observed
response, status, errors, monotonic start and actual custody remain the
source of every field. There is no title recorder argument.

- [ ] **TITLE.1 (2–5 minutes): Add these exact imports to `_auto_title.py`.**

```python
from elspeth.contracts.composer_llm_audit import ComposerLLMCall
from elspeth.web.composer._compose_loop_carriers import _AdmittedLLMCompletion
```

- [ ] **TITLE.2 (2–5 minutes): Insert the complete local builder below
  immediately after `admitted: _AdmittedAutoTitleCompletion | None = None`
  and before the outer `try`.** The durable successful record uses the
  completely admitted metadata. V1 and pre-admission failure retain the
  existing actual response parsing path; no missing usage becomes zero.

```python
    full: _AdmittedLLMCompletion | None = None

    def build_terminal_call() -> ComposerLLMCall:
        if status is None:
            raise AuditIntegrityError("Auto-title audit has no actual observed terminal status")
        return build_llm_call_record(
            model_requested=model,
            messages=wire_messages,
            tools=None,
            status=status,
            started_at=started_at,
            started_ns=started_ns,
            temperature=temperature,
            seed=seed,
            response=response,
            response_metadata=None if full is None else full.provider_metadata,
            error_class=error_class,
            error_message=error_message,
            provider_custody=provider_custody,
        )
```

- [ ] **TITLE.3 (2–5 minutes): Replace the original consecutive
  `admitted = _admit_auto_title_completion(response)` / SUCCESS assignment
  at 490–491, including F10's superseded early-retention block if already
  applied, with this complete block.** F10 has already defined the exact
  `durable` boolean from actual custody. The full content/native/credential
  admission happens before SUCCESS; a tool-bearing response is malformed.
  The actual record builder binds the original dispatched physical
  identity and admitted start. Retention consumes that already bound call
  inside V; no later audit or duration is reconstructed.

```python
                if durable:
                    full = provider_gateway._admit_composer_llm_completion(response, pricing_model=model)
                    provider_gateway._require_no_credential_material_in_completion(
                        full, surface="composer_auto_title_response")
                    if full.tool_batch.calls:
                        raise _MalformedAutoTitleResponseError("auto-title response contains tool calls")
                    admitted = _AdmittedAutoTitleCompletion(full.message.content, full.provider_metadata.finish_reason)
                else:
                    admitted = _admit_auto_title_completion(response)
                status = ComposerLLMCallStatus.SUCCESS
                if durable:
                    if full is None:
                        raise AuditIntegrityError("Durable title success lost its complete admitted response")
                    call = build_terminal_call()
                    provider_custody.retain_audit(call)
                    await provider_custody.retain_continuation_response(full)
```

`full` is assigned on this exact durable branch before retention. The
custody's unchanged public method accepts that admitted completion; its
internal encoder receives `audit_call=self.call` and SQL receives
`finished_at=self.call.finished_at`. An SDK/native/admission failure
cannot reach the SUCCESS retention branch. A response-retention failure
after the actual call was bound leaves that actual SUCCESS record owned
for financial settlement; it is not overwritten by the outer catch's
later local error status.

- [ ] **TITLE.4 (2–5 minutes): Replace the original complete `finally`
  block at 511–528 with this literal block.** It preserves the exact
  required-provider-audit scope and its original/body failure primacy.
  For a successful durable title `needs_terminal_audit()` is false because
  the actual call was already bound. Ordinary v1 and failures still build
  their actual observed audit in this one original path.

```python-finally-fragment
            finally:
                with required_provider_audit_scope(provider_custody):
                    if status is not None and provider_custody.needs_terminal_audit():
                        call = build_terminal_call()
                        provider_custody.retain_audit(call)
```

F10's outer optional-return/title-shape/write branches remain in place.
This success packet does not infer a no-SDK refusal, authorize a failed
title retry or supply the still separately owned terminal-outcome facade.
F10's real SDK/SQL title fixture must now assert the retained
`decode_retained_physical_audit_call` equals actual custody's complete
call, with the actual identity/start/finish, exact known usage and native
metadata. Its title publication/no-mutation tests continue to run through
the real owner and ordinary `-n 0` commands. O1's full audit roundtrip and
D6's actual post-response/pre-finally death tests remain required; this
source edit does not claim them executed.

- [ ] Apply storage C5.6's exact response/audit guards and one-V-stage amendments. `_retain_continuation_response_value` passes `audit_call=self.call` to its selected encoder, and `finished_at=self.call.finished_at` to SQL. The response transaction requires the full record to match its actual effect's original attempt. Both service and SQL use the existing current audit-only/allow-cancel/allow-expired guard because received response bookkeeping is owed. The pre-SDK guard remains strict.

## O4: Check complete bytes and once-only restoration

- [ ] Create `tests/unit/web/composer/test_continuation_physical_outcomes.py`
  with this complete pure test module after F1's response fixture is present.
  Its nominal sample comes from the actual call builder. The full codec
  preserves private failure/diagnostic/request channels and current pricing,
  provider-cost and planner-allowance values without renewing them. A
  non-SUCCESS fixture supplies the real mandatory error fields before testing
  the response-consistency refusal. The pure response may omit an audit, but
  requesting a physical audit then refuses; a retained full audit roundtrips.
  These are in-memory byte/identity checks, not SQL/SDK or physical-eligibility
  proof. The later process-death test remains mandatory for its transaction
  claim.

```python
import json
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from elspeth.contracts.composer_llm_audit import PROVIDER_COST_SOURCE_RESPONSE_USAGE_COST, ComposerLLMCallStatus
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.web.composer.audit import BufferingRecorder
from elspeth.web.composer.continuation_codec import (
    decode_retained_physical_audit_call,
    encode_continuation_response,
)
from elspeth.web.composer.continuation_provider_outcomes import (
    decode_physical_audit_call,
    encode_physical_audit_call,
    require_successful_response_audit,
)
from elspeth.web.composer.llm_response_parsing import build_llm_call_record
from tests.unit.web.composer.test_continuation_response import completion


def actual_record():
    received = completion()
    call = build_llm_call_record(
        model_requested="actual-test-model",
        messages=[{"role": "user", "content": "Keep the complete native and usage tail café"}],
        tools=None,
        status=ComposerLLMCallStatus.SUCCESS,
        started_at=datetime.now(UTC),
        started_ns=time.monotonic_ns() - 17_000_000,
        temperature=0.0,
        seed=7,
        response_metadata=received.provider_metadata,
    )
    return replace(call, call_id=str(UUID(int=41))), received.provider_metadata


def test_complete_actual_record_survives_json_without_rebuilding_clocks():
    call, metadata = actual_record()
    canonical = encode_physical_audit_call(call)
    restored = decode_physical_audit_call(canonical)
    assert encode_physical_audit_call(restored) == canonical
    assert restored.latency_ms == call.latency_ms >= 17
    assert restored.started_at == call.started_at and restored.finished_at == call.finished_at
    assert restored.messages_hash == call.messages_hash and restored.call_id == call.call_id
    assert restored.temperature == 0.0 and restored.seed == 7
    require_successful_response_audit(restored, metadata, attempt_id=call.call_id, started_at=call.started_at, finished_at=call.finished_at)


def private_record():
    from elspeth.contracts.composer_audit import PrivateFailureFacts, PrivateReceivedDiagnostic
    from elspeth.contracts.composer_provider_context import PrivateProviderContext

    call, _metadata = actual_record()
    return replace(
        call,
        status=ComposerLLMCallStatus.MALFORMED_RESPONSE,
        error_class="MalformedProviderResponse",
        error_message="Retained malformed café result",
        private_failure=PrivateFailureFacts(canonical_json({"stage": "response_admission", "detail": "Complete café tail"})),
        private_diagnostics=(
            PrivateReceivedDiagnostic(
                "actual-evidence",
                "actual-call",
                "set_metadata",
                canonical_json({"patch": {"description": "Exact café"}}),
                canonical_json({"refused": True}),
                '{"patch":{"description":"Exact café"}}',
            ),
        ),
        private_request=PrivateProviderContext(
            "retained", canonical_json({"messages": [{"role": "user", "content": "Exact retained café input"}]})
        ),
    )


def test_complete_private_failure_diagnostic_and_request_fields_survive():
    call = private_record()
    restored = decode_physical_audit_call(encode_physical_audit_call(call))
    assert restored == call
    assert restored.private_failure == call.private_failure
    assert restored.private_diagnostics == call.private_diagnostics
    assert restored.private_request == call.private_request


@pytest.mark.parametrize("field", ("private_failure", "private_diagnostics", "private_request"))
def test_omitted_private_audit_channel_cannot_default_to_an_empty_record(field):
    value = json.loads(encode_physical_audit_call(private_record()))
    del value[field]
    with pytest.raises(AuditIntegrityError, match=r"omitted|owned complete contract"):
        decode_physical_audit_call(canonical_json(value))


@pytest.mark.parametrize(
    "field,value",
    (
        ("call_id", str(UUID(int=42))),
        ("model_returned", "another-model"),
        ("provider_request_id", "another-provider-request"),
        ("finish_reason", "another-finish"),
        ("prompt_tokens", 991),
        ("completion_tokens", 992),
        ("total_tokens", 1983),
        ("cached_prompt_tokens", 993),
        ("reasoning_tokens", 994),
        ("reasoning_content", "Changed full native reasoning tail"),
    ),
)
def test_success_audit_must_match_its_actual_physical_response(field, value):
    call, metadata = actual_record()
    altered = replace(call, **{field: value})
    with pytest.raises(AuditIntegrityError, match="actual physical"):
        require_successful_response_audit(
            altered, metadata, attempt_id=call.call_id, started_at=call.started_at, finished_at=call.finished_at
        )


def test_finish_and_status_are_actual_received_evidence():
    call, metadata = actual_record()
    for altered in (
        replace(call, finished_at=call.finished_at + timedelta(seconds=1)),
        replace(call, status=ComposerLLMCallStatus.TIMEOUT, error_class="TimeoutError", error_message="timed out"),
    ):
        with pytest.raises(AuditIntegrityError, match="actual physical"):
            require_successful_response_audit(
                altered, metadata, attempt_id=call.call_id, started_at=call.started_at, finished_at=call.finished_at
            )


def test_codec_refuses_omitted_unknown_or_noncanonical_fields():
    call, _metadata = actual_record()
    canonical = encode_physical_audit_call(call)
    source = json.loads(canonical)
    omitted = {key: value for key, value in source.items() if key != "latency_ms"}
    for changed in (canonical_json(omitted), canonical_json(source | {"invented": True}), json.dumps(source, indent=2)):
        with pytest.raises(AuditIntegrityError):
            decode_physical_audit_call(changed)


def test_restored_historical_call_is_once_only_and_conflicting_bytes_refuse():
    call, _metadata = actual_record()
    recorder = BufferingRecorder()
    recorder.restore_received_llm_call(call)
    recorder.restore_received_llm_call(decode_physical_audit_call(encode_physical_audit_call(call)))
    assert recorder.llm_calls == (call,)
    with pytest.raises(AuditIntegrityError, match="different complete"):
        recorder.restore_received_llm_call(replace(call, latency_ms=call.latency_ms + 1))
    assert recorder.llm_calls == (call,)


def test_complete_current_cost_and_allowance_fields_survive_without_renewal():
    call, _metadata = actual_record()
    measured = replace(
        call,
        pricing_model="catalog/original-model",
        provider_cost=0.004,
        provider_cost_source=PROVIDER_COST_SOURCE_RESPONSE_USAGE_COST,
        max_completion_tokens_requested=512,
        planner_policy_hash="b" * 64,
        planner_call_ordinal=3,
    )
    cold = decode_physical_audit_call(encode_physical_audit_call(measured))
    assert cold == measured
    assert cold.pricing_model == "catalog/original-model"
    assert cold.provider_cost == 0.004
    assert cold.provider_cost_source == PROVIDER_COST_SOURCE_RESPONSE_USAGE_COST
    assert cold.max_completion_tokens_requested == 512
    assert cold.planner_policy_hash == "b" * 64
    assert cold.planner_call_ordinal == 3


def test_response_requires_complete_audit_only_when_physical_audit_is_requested():
    call, _metadata = actual_record()
    received = completion()
    pure = encode_continuation_response(received)
    with pytest.raises(AuditIntegrityError, match="actual complete physical audit"):
        decode_retained_physical_audit_call(pure)
    retained = encode_continuation_response(received, audit_call=call)
    assert decode_retained_physical_audit_call(retained) == call
```

- [ ] Establish behavioral RED by locally removing only `self._llm_calls.append(call)` from O2. `test_restored_historical_call_is_once_only_and_conflicting_bytes_refuse` must fail its actual one-call assertion. Restore it. Establish the metadata RED by omitting the usage comparison from `require_successful_response_audit`; the token counterexamples must fail their refusal expectations. Restore it before the normal green run. Do not use import/collection failure as RED.

```bash
cd "$COMPOSER_WORKTREE" && "$COMPOSER_PYTHON" -m pytest tests/unit/web/composer/test_continuation_physical_outcomes.py tests/unit/web/composer/test_continuation_response.py tests/unit/web/composer/test_continuation_audit_buffer.py -n 0 -v > "$COMPOSER_LOG_DIR/physical-outcomes.log" 2>&1
COMPOSER_RC=$?
cat "$COMPOSER_LOG_DIR/physical-outcomes.log"
test "$COMPOSER_RC" -eq 0
```

The O2 import inside `BufferingRecorder` is a source dependency, not a trust witness. F19/F21's exact SQL join remains the authorization boundary for historical response application. The actual subprocess death test must compare the original retained full audit with the restored buffer after the actual original-owner revocation and settlement; a pure buffer test cannot prove that transaction behavior.

## O5: Retain the actual failed or explicitly undispatched outcome

**Modify:** `composer/continuation_provider_outcomes.py`, `sessions/continuation.py`, `composer/provider_quota.py`, actual gateway/Advisor/title finally owners and service/protocol.

The existing effect `result_chunk_key` retains one of three closed provider variants: `composer.response.v1`, `composer.provider-outcome.v1`, or the existing `provider-cancellation.v1`. No new schema column or public response field is required. A known failed physical call is a refused action with its actual failed audit; its ledger may still contain unknown measures. A dispatched attempt with no outcome remains `unknown`. Neither state authorizes retry of that physical call.

- [ ] Add this complete pure outcome codec after O1. `audit_call_json` is the complete actual record already measured by the owner. A no-SDK outcome contains no invented call. The existing cancellation event is normalized only after O6 verifies that actual owner event and ledger.

```python
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator
from elspeth.contracts.provider_rejection import SafeRequestRejection
from elspeth.web.composer.continuation_progress import ContinuationProtocolError


class RetainedContinuationProviderOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    schema_version: Literal["composer.provider-outcome.v1"] = "composer.provider-outcome.v1"
    kind: Literal["failed", "not_dispatched"]
    effect_key: str = Field(pattern=r"^provider:[0-3]:(0|[1-9][0-9]*)$")
    input_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    attempt_id: str | None
    audit_call_json: str | None
    reason: Literal["admission_refused", "stopped_before_admission", "cancelled_before_dispatch"] | None
    request_rejection: SafeRequestRejection | None = None
    failure_kind: Literal["provider", "first_party", "cancelled"] | None = None

    @model_validator(mode="after")
    def require_actual_outcome(self) -> Self:
        if self.kind == "failed":
            if (self.reason is not None or self.attempt_id is None or self.audit_call_json is None
                or self.failure_kind is None):
                raise ValueError("failed outcome requires its actual physical audit only")
            call = decode_physical_audit_call(self.audit_call_json)
            if call.status is ComposerLLMCallStatus.SUCCESS or call.call_id != self.attempt_id:
                raise ValueError("failed outcome substituted success or another physical call")
            if self.request_rejection is not None and (self.failure_kind != "provider"
                    or call.status is not ComposerLLMCallStatus.BAD_REQUEST_ERROR):
                raise ValueError("request rejection changed its actual typed provider failure")
        elif (self.audit_call_json is not None or self.reason is None or self.request_rejection is not None
              or self.failure_kind is not None):
            raise ValueError("no-SDK outcome cannot manufacture a call")
        elif (self.reason in ("admission_refused", "stopped_before_admission")) != (self.attempt_id is None):
            raise ValueError("no-SDK gate and actual cancelled admission disagree")
        return self


def encode_provider_outcome(value: RetainedContinuationProviderOutcome) -> str:
    from elspeth.web.credential_guard import require_no_credential_material
    if type(value) is not RetainedContinuationProviderOutcome:
        raise TypeError("provider outcome requires its closed nominal value")
    encoded = canonical_json(value.model_dump(mode="json"))
    require_no_credential_material(encoded, surface="composer_continuation_provider_outcome",
        max_string_bytes=len(encoded.encode("utf-8")))
    return encoded


def decode_provider_outcome(encoded: str) -> RetainedContinuationProviderOutcome:
    try:
        value = RetainedContinuationProviderOutcome.model_validate_json(encoded, strict=True)
    except (ValidationError, ValueError, TypeError) as error:
        raise AuditIntegrityError("provider outcome violates its closed actual shape") from error
    if encode_provider_outcome(value) != encoded:
        raise AuditIntegrityError("provider outcome omitted or normalized retained facts")
    return value


class ContinuationRetainedProviderFailure(ContinuationProtocolError):
    def __init__(self, outcome: RetainedContinuationProviderOutcome) -> None:
        if type(outcome) is not RetainedContinuationProviderOutcome:
            raise TypeError("retained provider failure requires its actual SQL outcome")
        self.outcome = outcome
        super().__init__("The saved provider action failed; its actual audit and usage remain retained.")
```

- [ ] Add the following complete low-level helpers to `sessions/continuation.py`. Import `sha256` normally there. `provider_attempts`, `attempts`, `chunks`, `effects`, `usage` and `chat_messages_table` are the exact existing C5 aliases; import `_undispatched_cancellation_event_id` and `_usage_entry_id` from the existing quota authority locally inside the cancellation branch. This helper supplies no new current authority; callers hold the existing current session transaction and mutation/read guard.

```python
def read_provider_outcome_on_connection(
    conn: Connection, binding: ContinuationProviderBinding, *, require_settled: bool,
) -> RetainedContinuationProviderOutcome | None:
    from hashlib import sha256
    from elspeth.web.composer.continuation_provider_outcomes import (
        RetainedContinuationProviderOutcome, decode_physical_audit_call, decode_provider_outcome,
    )
    from elspeth.web.coordination.quota_authority import _undispatched_cancellation_event_id, _usage_entry_id
    sid, op = str(binding.running.claim.session_id), binding.running.claim.operation_id
    if (type(binding.family) is not int or binding.family not in (0, 1, 2, 3)
        or type(binding.ordinal) is not int or binding.ordinal < 0
        or binding.effect_key != f"provider:{binding.family}:{binding.ordinal}"):
        raise AuditIntegrityError("saved failure changed its canonical actual provider identity")
    effect = conn.execute(select(effects).where(effects.c.session_id == sid,
        effects.c.operation_id == op, effects.c.effect_key == binding.effect_key)).one()
    if effect.owner != "provider" or effect.input_sha256 != binding.input_sha256:
        raise AuditIntegrityError("saved failure substituted another actual prepared provider input")
    if effect.result_chunk_key is None:
        return None
    row = conn.execute(select(chunks).where(chunks.c.session_id == sid, chunks.c.operation_id == op,
        chunks.c.chunk_key == effect.result_chunk_key)).one()
    if row.codec == "composer.response.v1":
        return None
    encoded = read_chunk(conn, session_id=sid, operation_id=op, key=effect.result_chunk_key).decode("utf-8", errors="strict")
    if row.codec == "composer.provider-outcome.v1":
        outcome = decode_provider_outcome(encoded)
        if (outcome.effect_key, outcome.input_sha256, outcome.attempt_id) != (
                binding.effect_key, binding.input_sha256, effect.provider_attempt_id):
            raise AuditIntegrityError("saved failure changed its actual effect or physical admission")
    elif row.codec == "provider-cancellation.v1":
        outcome = RetainedContinuationProviderOutcome(kind="not_dispatched", effect_key=binding.effect_key,
            input_sha256=binding.input_sha256, attempt_id=effect.provider_attempt_id,
            audit_call_json=None, reason="cancelled_before_dispatch")
    else:
        raise AuditIntegrityError("provider result has an unfamiliar owned codec")
    if outcome.attempt_id is None:
        if effect.disposition != "refused" or outcome.reason not in ("admission_refused", "stopped_before_admission"):
            raise AuditIntegrityError("no-SDK outcome lost its explicit refusal owner")
        return outcome
    physical = conn.execute(select(provider_attempts).where(
        provider_attempts.c.attempt_id == outcome.attempt_id)).one()
    creator = conn.execute(select(attempts).where(attempts.c.session_id == sid, attempts.c.operation_id == op,
        attempts.c.sequence == effect.physical_sequence, attempts.c.generation == effect.physical_generation)).one()
    if (physical.session_id, physical.operation_id, physical.operation_epoch, physical.lease_token) != (
            sid, creator.session_operation_id, creator.session_operation_epoch, creator.session_operation_lease_token):
        raise AuditIntegrityError("saved failure changed the original actual physical creator")
    if physical.source != ("auto_title" if binding.family == 3 else "composer") or physical.run_id is not None:
        raise AuditIntegrityError("saved failure changed its quota source/run")
    ledger = None
    if physical.settled_at is not None:
        ledger = conn.execute(select(usage).where(usage.c.entry_id == physical.ledger_entry_id)).one()
        if (ledger.identity_id, ledger.session_id, ledger.source, ledger.run_id) != (
                physical.identity_id, sid, physical.source, physical.run_id):
            raise AuditIntegrityError("saved failure lost its actual identity/source ledger")
    elif require_settled:
        raise AuditIntegrityError("saved failure still owes its actual financial settlement")
    if outcome.kind == "failed":
        if outcome.audit_call_json is None:
            raise AuditIntegrityError("failed outcome lost its actual complete call")
        call = decode_physical_audit_call(outcome.audit_call_json)
        if (effect.response_finished_at is None or call.started_at != restore_utc(physical.started_at)
            or call.finished_at != restore_utc(effect.response_finished_at)):
            raise AuditIntegrityError("failed outcome changed its measured actual admission/finish")
        if ledger is not None and (
            (ledger.model, ledger.prompt_tokens, ledger.completion_tokens, ledger.cached_prompt_tokens,
                ledger.reasoning_tokens, restore_utc(ledger.recorded_at)) != (
            call.model_returned or call.model_requested, call.prompt_tokens, call.completion_tokens,
                call.cached_prompt_tokens, call.reasoning_tokens, call.finished_at)):
            raise AuditIntegrityError("failed outcome contradicts its actual known or unknown usage")
    else:
        if ledger is None:
            raise AuditIntegrityError("cancelled-before-dispatch lacks its actual settled ledger")
        event_id = _undispatched_cancellation_event_id(physical.attempt_id)
        entry_id = _usage_entry_id(session_id=sid, source=physical.source, run_id=physical.run_id,
            call_id=physical.attempt_id)
        event = conn.execute(select(chat_messages_table).where(chat_messages_table.c.id == event_id)).one()
        if row.codec == "provider-cancellation.v1":
            base = {"attempt_id": physical.attempt_id, "ledger_entry_id": physical.ledger_entry_id,
                "disposition": "cancelled_before_dispatch"}
            if encoded not in (canonical_json(base), canonical_json(base | {"cancellation_event_id": event_id})):
                raise AuditIntegrityError("cancelled provider result changed the actual owner receipt bytes")
        expected_content = canonical_json({"kind": "provider_attempt.cancelled_before_dispatch",
            "attempt_id": physical.attempt_id, "source": physical.source, "run_id": physical.run_id,
            "requested_model": effect.requested_model, "operation_id": physical.operation_id,
            "operation_epoch": physical.operation_epoch,
            "lease_token_sha256": sha256(physical.lease_token.encode("utf-8")).hexdigest()})
        if (effect.disposition != "refused" or physical.ledger_entry_id != entry_id
            or (event.session_id, event.role, event.writer_principal, event.content) != (
                sid, "audit", "compose_loop", expected_content)
            or any(value is not None for value in (event.tool_calls, event.raw_content, event.composition_state_id,
                event.tool_call_id, event.parent_assistant_id))
            or (ledger.model, ledger.prompt_tokens, ledger.completion_tokens, ledger.cached_prompt_tokens,
                ledger.reasoning_tokens) != (effect.requested_model, 0, 0, 0, 0)
            or restore_utc(ledger.recorded_at) != restore_utc(event.created_at)
            or restore_utc(physical.settled_at) != restore_utc(event.created_at)):
            raise AuditIntegrityError("no-SDK outcome contradicts the actual cancellation owner receipt")
    return outcome


def provider_result_disposition_on_connection(conn: Connection, binding: ContinuationProviderBinding) -> str:
    sid, op = str(binding.running.claim.session_id), binding.running.claim.operation_id
    effect = conn.execute(select(effects).where(effects.c.session_id == sid,
        effects.c.operation_id == op, effects.c.effect_key == binding.effect_key)).one()
    if effect.result_chunk_key is None:
        return "unknown"
    outcome = read_provider_outcome_on_connection(conn, binding, require_settled=False)
    return "refused" if outcome is not None else "committed"
```

Import `RetainedContinuationProviderOutcome` normally into `sessions/continuation.py` after its pure codec dependency exists. O1 imports no sessions module, so that direction does not create a module cycle. Keep the pure root codec's O1 imports local as already specified.

- [ ] Add this complete retention owner to `sessions/continuation.py`. Its body derives the complete next root from the actual selected SQL root. No caller-precomputed snapshot is accepted. A no-SDK refusal is issued only by the exact explicit custody path below and SQL verifies that no physical admission exists; a received failure proves the actual physical attempt and complete audit before retaining it. The action's original prepared owner and unknown ledger measures remain unchanged.

```python
def retain_provider_outcome_on_connection(
    conn: Connection, binding: ContinuationProviderBinding, outcome: RetainedContinuationProviderOutcome,
) -> None:
    from elspeth.web.composer.continuation_provider_outcomes import encode_provider_outcome, decode_physical_audit_call
    running = binding.running
    if not require_continuation_mutation(conn, running.session_operation_context, allow_cancel=True, allow_expired_segment=True):
        raise AuditIntegrityError("provider outcome requires actual continuation attempt history")
    sid, op = str(running.claim.session_id), running.claim.operation_id
    effect = conn.execute(select(effects).where(effects.c.session_id == sid,
        effects.c.operation_id == op, effects.c.effect_key == binding.effect_key)).one()
    if (type(outcome) is not RetainedContinuationProviderOutcome or effect.owner != "provider"
        or (outcome.effect_key, outcome.input_sha256, outcome.attempt_id) != (
            binding.effect_key, binding.input_sha256, effect.provider_attempt_id)):
        raise AuditIntegrityError("provider outcome lost its actual prepared input/admission")
    encoded = encode_provider_outcome(outcome)
    if effect.result_chunk_key is not None:
        previous = read_provider_outcome_on_connection(conn, binding, require_settled=False)
        if previous != outcome:
            raise AuditIntegrityError("provider outcome attempted to replace a different received result")
        return
    stored = load_snapshot(conn, session_id=sid, operation_id=op)
    root = decode_continuation_snapshot(stored.payload_json)
    top = root.frames[-1]
    if (not isinstance(top, ContinuationProviderFrame)
        or (top.provider_effect_key, top.provider_input_sha256) != (binding.effect_key, binding.input_sha256)):
        raise AuditIntegrityError("provider outcome lost its exact selected phase source")
    finished_at = None
    if outcome.kind == "failed":
        if outcome.audit_call_json is None or effect.disposition != "dispatched":
            raise AuditIntegrityError("failed outcome lacks actual SDK-entry intent and audit")
        call = decode_physical_audit_call(outcome.audit_call_json)
        physical = conn.execute(select(provider_attempts).where(
            provider_attempts.c.attempt_id == outcome.attempt_id)).one()
        creator = conn.execute(select(attempts).where(attempts.c.session_id == sid, attempts.c.operation_id == op,
            attempts.c.sequence == effect.physical_sequence, attempts.c.generation == effect.physical_generation)).one()
        if (physical.session_id, physical.operation_id, physical.operation_epoch, physical.lease_token) != (
                sid, creator.session_operation_id, creator.session_operation_epoch, creator.session_operation_lease_token):
            raise AuditIntegrityError("failed response substituted another original physical creator")
        if call.started_at != restore_utc(physical.started_at) or call.finished_at < call.started_at:
            raise AuditIntegrityError("failed response changed its actual admitted clock")
        finished_at = call.finished_at
        receipt_id = physical.attempt_id
    else:
        if (outcome.reason not in ("admission_refused", "stopped_before_admission")
            or effect.disposition != "prepared" or effect.provider_attempt_id is not None):
            raise AuditIntegrityError("explicit admission refusal cannot dispose an admitted or dispatched call")
        if outcome.reason == "stopped_before_admission":
            stopped = conn.execute(select(jobs.c.cancel_requested_at).where(
                jobs.c.session_id == sid, jobs.c.operation_id == op)).scalar_one()
            if stopped is None:
                raise AuditIntegrityError("unadmitted Stop disposition has no actual durable Stop marker")
        receipt_id = "no-sdk:" + binding.effect_key
    key = "outcome:" + binding.effect_key
    put_chunk(conn, session_id=sid, operation_id=op, key=key,
        payload=encoded.encode("utf-8"), codec="composer.provider-outcome.v1")
    conn.execute(update(effects).where(effects.c.session_id == sid, effects.c.operation_id == op,
        effects.c.effect_key == binding.effect_key).values(disposition="refused", receipt_key=receipt_id,
            result_chunk_key=key, response_finished_at=finished_at))
    snapshot = _effect_snapshot(stored.payload_json, effect_key=binding.effect_key,
        disposition="refused", receipt_id=receipt_id)
    snapshot = retain_action_execution_receipt_on_connection(conn, session_id=sid, operation_id=op,
        effect_key=binding.effect_key, disposition="refused", snapshot_json=snapshot)
    append_complete_snapshot(conn, session_id=sid, operation_id=op,
        expected_checkpoint_id=stored.checkpoint_id, expected_cursor=stored.cursor, snapshot_json=snapshot,
        event_batch=(ContinuationEvent("effect_refused", canonical_json({"effect_key": binding.effect_key,
            "kind": outcome.kind, "attempt_id": outcome.attempt_id})),), handoff=False)
```

- [ ] Add this complete service method and the identical protocol signature. Keep DTO imports in `TYPE_CHECKING` in `sessions/protocol.py`. The submitted required future owns the same actual current audit-only transaction and is joined by the custody owner before cancellation is delivered.

```python
async def retain_continuation_provider_outcome(
    self, *, binding: ContinuationProviderBinding, outcome: RetainedContinuationProviderOutcome,
    required_work: RequiredWorkTicket,
) -> None:
    context, sid = binding.running.session_operation_context, str(binding.running.claim.session_id)
    _validate_required_sql_ticket(required_work, sources=(RequiredWorkSource.CONTINUATION_STATE_SQL,),
        session_id=sid, context=context)
    def _sync() -> None:
        with self._session_process_locked_begin(sid) as conn, self._session_write_lock(conn, sid):
            self._require_session_operation_context_on_connection(conn, context, session_id=sid,
                expected_kind=SessionOperationKind.COMPOSE, now=database_now(conn), audit_only=True)
            retain_provider_outcome_on_connection(conn, binding, outcome)
    await run_required_sql_in_worker(required_work, _sync)
```

- [ ] Add these complete custody methods. They encode inside the single V stage, while the already measured actual audit was frozen by its real owner. The explicit no-SDK method receives the actual quota refusal exception and requires the exact live custody to confirm SDK entry never happened; no absence of a log supplies that fact. The service independently checks the SQL admission. Ordinary v1 is unchanged.

```python
async def _retain_continuation_provider_outcome_value(
    self, *, refusal: ChargeableAdmissionRefused | None, request_rejection: SafeRequestRejection | None,
    failure_kind: Literal["provider", "first_party", "cancelled"] | None,
) -> None:
    from elspeth.web.composer.continuation_provider_outcomes import (
        RetainedContinuationProviderOutcome, encode_physical_audit_call,
    )
    binding = self._continuation_binding
    if binding is None or self._clock is None:
        raise AuditIntegrityError("durable failure retention lacks its actual action clock/binding")
    if refusal is None:
        if self.call is None or self.attempt is None or not self._sdk_entered:
            raise AuditIntegrityError("failed physical outcome lacks its actual terminal audit")
        if request_rejection is not None:
            from elspeth.web.composer.diagnostic_evidence import diagnostic_evidence_identity
            request_rejection = request_rejection.model_copy(update={"diagnostic_reference":
                diagnostic_evidence_identity("provider-failure", self.call.call_id)})
        value = RetainedContinuationProviderOutcome(kind="failed", effect_key=binding.effect_key,
            input_sha256=binding.input_sha256, attempt_id=self.attempt.attempt_id,
            audit_call_json=encode_physical_audit_call(self.call), reason=None, request_rejection=request_rejection,
            failure_kind=failure_kind)
    else:
        if not isinstance(refusal, ChargeableAdmissionRefused) or refusal.decision.refusal_reason is None:
            raise AuditIntegrityError("no-SDK retention lacks its actual chargeable refusal")
        if self._sdk_entered or self.attempt is not None or self.call is not None:
            raise AuditIntegrityError("quota refusal cannot replace an actual physical attempt")
        value = RetainedContinuationProviderOutcome(kind="not_dispatched", effect_key=binding.effect_key,
            input_sha256=binding.input_sha256, attempt_id=None, audit_call_json=None, reason="admission_refused")
    sql, projection = self.required_work.reserve_pair(RequiredWorkSource.CONTINUATION_STATE_SQL,
        RequiredWorkSource.CONTINUATION_STATE_PROJECTION)
    try:
        _, cancellations = await self._join(self.service.retain_continuation_provider_outcome(
            binding=binding, outcome=value, required_work=sql))
    except BaseException:
        if sql.complete:
            projection.complete_without_submission()
        raise
    projection.begin_projection()
    projection.complete_owned()
    self.raise_deferred_cancellations(cancellations)


async def retain_continuation_failure(
    self, *, failure_kind: Literal["provider", "first_party", "cancelled"],
    request_rejection: SafeRequestRejection | None = None,
) -> None:
    if self._continuation_binding is None:
        return
    if self._clock is None:
        raise AuditIntegrityError("failed durable call lost its actual stage clock")
    async def retain() -> None:
        await self._retain_continuation_provider_outcome_value(refusal=None, request_rejection=request_rejection,
            failure_kind=failure_kind)
    await self._clock.run("retention", retain)


async def retain_continuation_admission_refusal(self, refusal: ChargeableAdmissionRefused) -> None:
    if self._continuation_binding is None:
        return
    if self._clock is None:
        raise AuditIntegrityError("refused durable action lost its actual stage clock")
    async def retain() -> None:
        await self._retain_continuation_provider_outcome_value(refusal=refusal, request_rejection=None, failure_kind=None)
    await self._clock.run("retention", retain)
```

- [ ] Import `SafeRequestRejection` from `contracts.provider_rejection` normally into `provider_quota.py` and the four actual audit-body modules. Add this exact local initialization alongside each body's existing error locals before `try`; after each existing `failure = classify_provider_failure(exc)` assignment in that body, add the exact second statement below. This captures only its actual closed classifier output, never the SDK exception or response object.

```python-statement
retained_request_rejection: SafeRequestRejection | None = None
continuation_failure_kind: Literal["provider", "first_party", "cancelled"] = "first_party"
```

```python-statement
retained_request_rejection = None if failure is None else failure.request_rejection
```

Import `Literal` from `typing` in those modules. In each actual `except TimeoutError`, `except OpenAIError`, `except _MalformedLLMResponseError` and gateway `_BadRequestLLMError` arm insert the first exact assignment below; in each actual `except asyncio.CancelledError` arm insert the second. Other first-party arms retain their initialized classification. In the gateway's broad `except Exception as exc` arm use the third expression, because its actual nominal SDK/transport families are defined there. The Advisor and title modules use the fourth expression (the title module additionally accepts its exact `_AutoTitleProviderTransportError` in that tuple). Classification uses actual exception types at the owning boundary, never an error-class string after restart.

```python-statement
continuation_failure_kind = "provider"
```

```python-statement
continuation_failure_kind = "cancelled"
```

```python-statement
continuation_failure_kind = "provider" if isinstance(exc, advisor_provider_failure_types()) else "first_party"
```

```python-statement
continuation_failure_kind = "provider" if isinstance(exc, provider_gateway.advisor_provider_failure_types()) else "first_party"
```

The title broad arm uses this exact closed extension instead of the preceding fourth expression:

```python-statement
continuation_failure_kind = "provider" if isinstance(exc, (
    *provider_gateway.advisor_provider_failure_types(), _AutoTitleProviderTransportError,
)) else "first_party"
```

In title's own `_MalformedAutoTitleResponseError` arm insert `continuation_failure_kind = "provider"` too. The newly added paired `_MalformedLLMResponseError, CredentialMaterialRefused` arm sets `continuation_failure_kind = "provider" if type(exc) is _MalformedLLMResponseError else "first_party"`. A first-party failure can be retained for honest recovery without being reclassified an optional provider outage.

- [ ] In all four actual audit-body finally owners named in O3 (primary, text, Advisor and title), insert this exact branch immediately after their existing actual `retain_audit(call)` and existing recorder/budget consumption, inside the `needs_terminal_audit` construction arm. It follows that complete arm's real call, never the already retained SUCCESS branch. The actual `required_provider_audit_scope` remains around it and preserves the original body exception together with an owed retention error. The existing provider-call scope then performs the actual ledger settlement; do not replace that with this private retention transaction.

```python-statement
if (provider_custody is not None and provider_custody.continuation_binding is not None
    and call.status is not ComposerLLMCallStatus.SUCCESS):
    await provider_custody.retain_continuation_failure(failure_kind=continuation_failure_kind,
        request_rejection=retained_request_rejection)
```

In title's actual outer `except ChargeableAdmissionRefused as exc`, after its real metric and before its F10 durable return, insert this exact statement. It is reached only from that actual typed quota gate. For other guarded/precondition no-SDK errors the original typed error propagates; it is not relabelled an optional successful title.

```python-statement
if durable:
    await provider_custody.retain_continuation_admission_refusal(exc)
```

No handler catches a first-party bug merely to produce a successful optional outcome. For a title `_MalformedLLMResponseError` or `CredentialMaterialRefused` raised by F10's new complete admission, the actual inner title catch must classify them precisely before the broad catch. Add these exact imports to `_auto_title.py`, and add the complete arm below before its existing `_MalformedAutoTitleResponseError` arm:

```python
from elspeth.web.composer.provider_gateway import _MalformedLLMResponseError
from elspeth.web.credential_guard import CredentialMaterialRefused
```

```python-except-fragment
            except (_MalformedLLMResponseError, CredentialMaterialRefused) as exc:
                continuation_failure_kind = "provider" if type(exc) is _MalformedLLMResponseError else "first_party"
                status = ComposerLLMCallStatus.MALFORMED_RESPONSE
                error_class = type(exc).__name__
                error_message = "credential_material_rejected" if isinstance(exc, CredentialMaterialRefused) else "malformed_response"
                response = None
                if isinstance(exc, CredentialMaterialRefused):
                    full = None
                raise
```

Append those same two nominal exceptions to title's existing optional provider-failure tuple in F10. A live SDK/admission failure retains the actual original exception until that established optional boundary; cold failure application uses the closed descriptor, never a fabricated original exception object.

## O6: Reconcile each closed result codec and apply optional title failure

**Modify:** actual C5.7 quota recovery and provider action facets, `sessions/continuation.py`, `coordination/composer_operation_authority.py`; frames consumes these exact nominal results.

- [ ] Import `ContinuationProviderFrame` from `composer.continuation` and `RetainedContinuationProviderOutcome` from `composer.continuation_provider_outcomes` normally into `sessions/continuation.py`. Import `Literal` from `typing`. Change the return annotation of O5's `provider_result_disposition_on_connection` to `Literal["committed", "refused", "unknown"]`. Append this exact field to both `ProviderActionObservation` and `ProviderEffectReconciliation` dataclasses; existing positional constructors retain their previous fields and default.

```python-statement
outcome: RetainedContinuationProviderOutcome | None = None
```

- [ ] Add this complete usage reader to `sessions/continuation.py`. It derives actual accounting from the retained actual complete call or admitted metadata. It never derives success, zero usage or a fake call from a ledger. It is called only inside C5.7's already authenticated current transaction after the original physical attempt/history tuple was verified.

```python
def retained_provider_usage_on_connection(
    conn: Connection, binding: ContinuationProviderBinding,
) -> TokenUsageEntry | None:
    from elspeth.web.composer.continuation_codec import decode_retained_provider_metadata
    from elspeth.web.composer.continuation_provider_outcomes import decode_physical_audit_call
    from elspeth.web.coordination.quota_authority import TokenUsageEntry
    sid, op = str(binding.running.claim.session_id), binding.running.claim.operation_id
    effect = conn.execute(select(effects).where(effects.c.session_id == sid,
        effects.c.operation_id == op, effects.c.effect_key == binding.effect_key)).one()
    if effect.result_chunk_key is None:
        return None
    outcome = read_provider_outcome_on_connection(conn, binding, require_settled=False)
    if outcome is not None:
        if outcome.kind != "failed" or outcome.audit_call_json is None:
            raise AuditIntegrityError("a no-SDK receipt must use its actual cancellation owner, not a call settlement")
        call = decode_physical_audit_call(outcome.audit_call_json)
        return TokenUsageEntry(model=call.model_returned or call.model_requested,
            prompt_tokens=call.prompt_tokens, completion_tokens=call.completion_tokens,
            cached_prompt_tokens=call.cached_prompt_tokens, reasoning_tokens=call.reasoning_tokens,
            recorded_at=call.finished_at, call_id=call.call_id)
    row = conn.execute(select(chunks).where(chunks.c.session_id == sid, chunks.c.operation_id == op,
        chunks.c.chunk_key == effect.result_chunk_key)).one()
    canonical = read_chunk(conn, session_id=sid, operation_id=op, key=effect.result_chunk_key).decode("utf-8", errors="strict")
    metadata = decode_retained_provider_metadata(PrivateChunk(
        PrivateChunkRef(codec=row.codec, digest=row.sha256, byte_length=row.byte_length), canonical))
    if effect.response_finished_at is None or effect.provider_attempt_id is None:
        raise AuditIntegrityError("retained successful usage lost its actual measured finish/admission")
    actual = metadata.usage
    return TokenUsageEntry(model=metadata.model_returned or effect.requested_model,
        prompt_tokens=actual.prompt_tokens, completion_tokens=actual.completion_tokens,
        cached_prompt_tokens=actual.cached_prompt_tokens, reasoning_tokens=actual.reasoning_tokens,
        recorded_at=restore_utc(effect.response_finished_at), call_id=effect.provider_attempt_id)
```

Add `TokenUsageEntry` to this module's existing quota-authority import under `TYPE_CHECKING`; the helper's local import constructs the real type. Its annotation is deferred by the existing module future import.

- [ ] In the existing complete C5.7 `reconcile_interrupted_provider_attempt_on_connection`, add the following exact local imports and binding immediately after its actual physical creator/source checks, before `if actual.settled_at is not None:`. The caller already selected the actual effect under the same session lock; this identity parser is closed and refuses noncanonical keys.

```python-statement
from elspeth.web.sessions.continuation import (
    ContinuationProviderBinding, read_provider_outcome_on_connection,
    retained_provider_usage_on_connection, provider_result_disposition_on_connection,
)
parts = effect_key.split(":")
if (len(parts) != 3 or parts[0] != "provider" or parts[1] not in ("0", "1", "2", "3")
    or not parts[2].isdigit() or str(int(parts[2])) != parts[2]):
    raise AuditIntegrityError("recovered physical outcome has a noncanonical provider key")
binding = ContinuationProviderBinding(running, effect_key, int(parts[1]), int(parts[2]), effect.input_sha256)
```

Inside that function's already-settled branch, after its existing actual cancellation-event branch and ledger identity/source/run checks, insert this exact branch before its existing successful response/metadata check:

```python-statement
known = read_provider_outcome_on_connection(connection, binding, require_settled=True)
if known is not None:
    return RecoveredProviderSettlement(actual.attempt_id, actual.ledger_entry_id, "refused", None)
```

Replace only its later complete contiguous `if effect.result_chunk_key is not None: ... else: ...` span beginning after `now = database_now(connection)` and ending before `event_id = ...` with this complete body. Keep its actual audit callback, ledger writer and receipt return below it unchanged. The old owner must already have been revoked/interrupted, as checked by the unchanged preceding branch.

```python-statement
entry = retained_provider_usage_on_connection(connection, binding)
if entry is None:
    entry = TokenUsageEntry(model=effect.requested_model, prompt_tokens=None, completion_tokens=None,
        cached_prompt_tokens=None, reasoning_tokens=None, recorded_at=now, call_id=actual.attempt_id)
    disposition = "unknown"
else:
    disposition = provider_result_disposition_on_connection(connection, binding)
```

The prior `effect.disposition == "prepared"` cancellation branch stays earlier and unchanged. An explicit cancelled-before-dispatch result is checked through its actual event branch, never decoded as a completion. A retained failed call's exact null measures/time/model pass to the real ledger writer. A genuinely missing outcome creates the existing explicit recovered-unknown event with null measures and no fake audit.

- [ ] In the complete C5.6 `join_provider_effect_settlement`, replace only its `disposition = "committed" if effect.result_chunk_key is not None else "unknown"` assignment with the following. Preserve its actual original physical creator/ledger verification and late terminal behavior.

```python-statement
disposition = provider_result_disposition_on_connection(conn, binding)
```

- [ ] In C5.7's `reconcile_provider_effect_on_connection`, replace only the full `if effect.provider_attempt_id is None:` arm with this complete branch. It prevents a real no-SDK refusal from being misclassified ready and physically retried.

```python-statement
if effect.provider_attempt_id is None:
    if effect.disposition == "refused":
        known = read_provider_outcome_on_connection(conn, binding, require_settled=True)
        if (known is None or known.kind != "not_dispatched"
            or known.reason not in ("admission_refused", "stopped_before_admission")):
            raise AuditIntegrityError("unadmitted refusal lost its actual explicit no-SDK outcome")
        return ProviderEffectReconciliation(binding, "refused", None, None, None,
            load_snapshot(conn, session_id=sid, operation_id=operation_id), known)
    if effect.disposition != "prepared":
        raise AuditIntegrityError("unadmitted provider effect has an impossible disposition")
    return ProviderEffectReconciliation(binding, "ready", None, None, None,
        load_snapshot(conn, session_id=sid, operation_id=operation_id))
```

In `_reconcile_continuation_provider_effect_sync`, replace its existing single `return reconcile_provider_effect_on_connection(...)` statement with `result = reconcile_provider_effect_on_connection(...)`, preserving all arguments and the actual callback unchanged. Append the following exact suffix inside the same transaction. Add `replace` from `dataclasses` to the service module's existing imports.

```python-statement
known = (read_provider_outcome_on_connection(conn, result.binding, require_settled=True)
    if result.disposition == "refused" else None)
return replace(result, outcome=known)
```

In the authority's actual `observe_provider_action` return, append one final positional argument after its actual ledger ID: `read_provider_outcome_on_connection(conn, binding, require_settled=True) if effect.disposition == "refused" else None`. This reads the outcome in that same current-authorized transaction. Its existing successful response branch already checks `row.codec == "composer.response.v1"` and remains unchanged.

- [ ] Add this complete nominal read result to `sessions/continuation.py` and current SQL read facet to the actual authority. Only this current selected-source read supplies a failure for private caller application. A descriptor decoded from arbitrary caller bytes is not execution authority.

```python
@dataclass(frozen=True, slots=True)
class AuthorizedContinuationProviderOutcome:
    snapshot: StoredContinuationSnapshot
    outcome: RetainedContinuationProviderOutcome
    ledger_entry_id: str | None


def require_selected_provider_outcome_on_connection(
    conn: Connection, binding: ContinuationProviderBinding, *, stored: StoredContinuationSnapshot,
) -> AuthorizedContinuationProviderOutcome:
    running = binding.running
    if not require_continuation_mutation(conn, running.session_operation_context, allow_cancel=True, allow_expired_segment=True):
        raise AuditIntegrityError("provider failure application requires actual continuation history")
    current = require_snapshot_cas(conn, session_id=str(running.claim.session_id),
        operation_id=running.claim.operation_id, expected_checkpoint_id=stored.checkpoint_id, expected_cursor=stored.cursor)
    if current != stored:
        raise AuditIntegrityError("failure application changed the selected complete caller snapshot")
    root = decode_continuation_snapshot(stored.payload_json)
    frame = root.frames[-1]
    if (not isinstance(frame, ContinuationProviderFrame)
        or (frame.provider_effect_key, frame.provider_input_sha256) != (binding.effect_key, binding.input_sha256)):
        raise AuditIntegrityError("failure application substituted an unselected provider phase")
    known = read_provider_outcome_on_connection(conn, binding, require_settled=True)
    if known is None:
        raise AuditIntegrityError("failure application has no actual closed outcome")
    if known.attempt_id is not None and known.attempt_id in root.pending_settlement_ids:
        raise AuditIntegrityError("failure application still owes actual provider accounting")
    ledger_id = None
    if known.attempt_id is not None:
        ledger_id = conn.execute(select(provider_attempts.c.ledger_entry_id).where(
            provider_attempts.c.attempt_id == known.attempt_id)).scalar_one()
    return AuthorizedContinuationProviderOutcome(stored, known, ledger_id)
```

```python
def load_continuation_provider_outcome(
    self, binding: ContinuationProviderBinding, *, expected_checkpoint_id: str, expected_cursor: int,
) -> AuthorizedContinuationProviderOutcome:
    running = binding.running
    with locked_session_transaction(self._engine, str(running.claim.session_id)) as conn:
        stored = require_snapshot_cas(conn, session_id=str(running.claim.session_id),
            operation_id=running.claim.operation_id, expected_checkpoint_id=expected_checkpoint_id,
            expected_cursor=expected_cursor)
        return require_selected_provider_outcome_on_connection(conn, binding, stored=stored)


def complete_optional_continuation_title(
    self, binding: ContinuationProviderBinding, *, expected_checkpoint_id: str, expected_cursor: int,
) -> StoredContinuationSnapshot:
    from elspeth.web.composer.continuation import InitializeFrame
    from elspeth.web.composer.continuation_provider_outcomes import ContinuationRetainedProviderFailure
    running = binding.running
    sid, op = str(running.claim.session_id), running.claim.operation_id
    with locked_session_transaction(self._engine, sid) as conn:
        stored = require_snapshot_cas(conn, session_id=sid, operation_id=op,
            expected_checkpoint_id=expected_checkpoint_id, expected_cursor=expected_cursor)
        auth = require_selected_provider_outcome_on_connection(conn, binding, stored=stored)
        if auth.outcome.kind == "failed" and auth.outcome.failure_kind != "provider":
            raise ContinuationRetainedProviderFailure(auth.outcome)
        root = decode_continuation_snapshot(stored.payload_json)
        frame = root.frames[-1]
        if (binding.family != 3 or type(frame) is not InitializeFrame or frame.ingress_phase != "title_pending"
            or frame.title_response is not None):
            raise AuditIntegrityError("optional failure can only complete its exact pending TITLE phase")
        changed = frame.model_copy(update={"ingress_phase": "ready", "title_attempt_id": auth.outcome.attempt_id,
            "provider_effect_key": None, "provider_input_sha256": None})
        following = root.model_copy(update={"frames": (*root.frames[:-1], changed)})
        return append_complete_snapshot(conn, session_id=sid, operation_id=op,
            expected_checkpoint_id=stored.checkpoint_id, expected_cursor=stored.cursor,
            snapshot_json=encode_continuation_snapshot(following),
            event_batch=(ContinuationEvent("checkpoint", canonical_json({"phase": "optional_title_failed",
                "effect_key": binding.effect_key, "kind": auth.outcome.kind,
                "attempt_id": auth.outcome.attempt_id, "ledger_entry_id": auth.ledger_entry_id})),), handoff=False)
```

Both current facets use audit-only/allow-cancel/expired authority only to project already owed private outcomes. They authorize no provider retry, title write or graph mutation. Frame F10's actual successful title write remains its separate exact retained-response+settled-ledger transaction. A missing actual outcome, owner-active attempt or unknown dispatched outcome cannot enter this optional failure method.

- [ ] Add this exact current SQL Stop owner to the same authority. It is the explicit disposition for a selected prepared call before any quota admission, and is reached only on the actual durable Stop marker. It proves no physical admission can already have occurred under that transaction's current selected effect lock. It never guesses no-SDK for an admitted/dispatched row.

```python
def cancel_unadmitted_continuation_provider(
    self, binding: ContinuationProviderBinding, *, expected_checkpoint_id: str, expected_cursor: int,
) -> StoredContinuationSnapshot:
    from elspeth.web.composer.continuation_provider_outcomes import RetainedContinuationProviderOutcome
    running = binding.running
    sid, op = str(running.claim.session_id), running.claim.operation_id
    with locked_session_transaction(self._engine, sid) as conn:
        require_continuation_mutation(conn, running.session_operation_context,
            allow_cancel=True, allow_expired_segment=True)
        stored = require_snapshot_cas(conn, session_id=sid, operation_id=op,
            expected_checkpoint_id=expected_checkpoint_id, expected_cursor=expected_cursor)
        root = decode_continuation_snapshot(stored.payload_json)
        frame = root.frames[-1]
        if (not isinstance(frame, ContinuationProviderFrame)
            or (frame.provider_effect_key, frame.provider_input_sha256) != (binding.effect_key, binding.input_sha256)):
            raise AuditIntegrityError("unadmitted Stop changed the selected exact provider phase")
        stopped = conn.execute(select(jobs.c.cancel_requested_at).where(
            jobs.c.session_id == sid, jobs.c.operation_id == op)).scalar_one()
        effect = conn.execute(select(effects).where(effects.c.session_id == sid,
            effects.c.operation_id == op, effects.c.effect_key == binding.effect_key)).one()
        if (stopped is None or effect.owner != "provider" or effect.input_sha256 != binding.input_sha256
            or effect.disposition != "prepared" or effect.provider_attempt_id is not None
            or effect.result_chunk_key is not None):
            raise AuditIntegrityError("unadmitted Stop cannot declare an admitted or dispatched attempt free")
        retained = RetainedContinuationProviderOutcome(kind="not_dispatched", effect_key=binding.effect_key,
            input_sha256=binding.input_sha256, attempt_id=None, audit_call_json=None,
            reason="stopped_before_admission")
        retain_provider_outcome_on_connection(conn, binding, retained)
        return load_snapshot(conn, session_id=sid, operation_id=op)
```

Frames F36 submits this exact method only when its actual boundary says Stop and actual reconciliation says `ready`. It projects the complete return, then calls the same optional-title failure application. No custody/SDK is minted on that branch. For an actual admitted current owner, F36 joins or waits for that owner; C5.7's existing interrupted-attempt cancellation remains the only recovered admitted no-SDK path.

- [ ] The frames title owner calls this exact method through its existing registered `CONTINUATION_STATE_SQL/PROJECTION` finish-once pair, then loads the returned complete selected snapshot. It invokes it only when actual reconciliation returns `refused` with non-null `outcome`; an `unknown` outcome stays `ContinuationProviderWait("unknown_effect")`. Fresh F10 `admission_refused`/`provider_failed` descriptions alone cannot authorize this transition: the driver discards them in favor of this SQL readback. For failed audits, restore the actual complete `decode_physical_audit_call(auth.outcome.audit_call_json)` into the existing buffer once using O2 only after `load_continuation_provider_outcome` has returned this nominal proof. No physical title call is repeated after that cut.

## O7: Exercise actual failed-title retention, settlement recovery and no-SDK gates

**Modify:** `tests/unit/web/composer/test_continuation_title.py` and the O4 pure test module. These tests run after F10 and O5/O6 are importable.

- [ ] Extract the exact complete setup prefix of F10's `retained_title` fixture, from `_engine, repository, authority, service, sid = store` through `custody = owner.mint_continuation(...)`, into `async def prepare_title_owner(store):`. Preserve every existing ingress/bootstrap/request/actual admission-preparation line unchanged. Append `return running, owner, custody, actual.binding, prepared, request, coordinator`. Replace that prefix in `retained_title` with the following complete bindings; its actual SDK/owner success/assertions suffix stays unchanged. This helper is actual fixture setup, not a production owner or fake result factory.

```python-statement
_engine, repository, authority, service, sid = store
running, owner, custody, binding, prepared, request, coordinator = await prepare_title_owner(store)
```

In that existing helper's suffix replace `actual.binding` with its local `binding`. F10's actual complete audit observation correction remains: compare the real SQL-decoded audit to the exact call captured when the actual owner invoked `retain_audit`, since normal scope settlement clears `custody.call`.

- [ ] Add the following complete pure counterexamples to O4's module. They use actual nominal audit construction and distinguish failed physical evidence from no-SDK evidence.

```python
def test_failed_outcome_roundtrip_keeps_the_actual_complete_call_and_null_usage():
    from elspeth.web.composer.continuation_provider_outcomes import (
        RetainedContinuationProviderOutcome, decode_provider_outcome, encode_provider_outcome,
    )
    call, _metadata = actual_record()
    actual = replace(call, status=ComposerLLMCallStatus.TIMEOUT, prompt_tokens=None,
        completion_tokens=None, total_tokens=None, cached_prompt_tokens=None, reasoning_tokens=None,
        error_class="TimeoutError", error_message="TimeoutError")
    retained = RetainedContinuationProviderOutcome(kind="failed", effect_key="provider:3:0",
        input_sha256="a" * 64, attempt_id=actual.call_id,
        audit_call_json=encode_physical_audit_call(actual), reason=None, failure_kind="provider")
    decoded = decode_provider_outcome(encode_provider_outcome(retained))
    assert decoded == retained and decoded.audit_call_json is not None
    restored = decode_physical_audit_call(decoded.audit_call_json)
    assert restored == actual and restored.latency_ms == actual.latency_ms
    assert restored.prompt_tokens is None and restored.finished_at == actual.finished_at
    with pytest.raises(ValueError):
        RetainedContinuationProviderOutcome.model_validate(retained.model_dump() | {
            "kind": "not_dispatched", "reason": "admission_refused"})
    with pytest.raises(ValueError):
        RetainedContinuationProviderOutcome.model_validate(retained.model_dump() | {
            "audit_call_json": encode_physical_audit_call(call)})


@pytest.mark.parametrize("reason,attempt", (
    ("admission_refused", None), ("stopped_before_admission", None),
    ("cancelled_before_dispatch", str(UUID(int=91))),
))
def test_no_sdk_outcome_never_contains_a_fabricated_audit(reason, attempt):
    from elspeth.web.composer.continuation_provider_outcomes import (
        RetainedContinuationProviderOutcome, decode_provider_outcome, encode_provider_outcome,
    )
    actual = RetainedContinuationProviderOutcome(kind="not_dispatched", effect_key="provider:3:0",
        input_sha256="a" * 64, attempt_id=attempt, audit_call_json=None, reason=reason)
    assert decode_provider_outcome(encode_provider_outcome(actual)) == actual
    call, _metadata = actual_record()
    with pytest.raises(ValueError):
        RetainedContinuationProviderOutcome.model_validate(actual.model_dump() | {
            "audit_call_json": encode_physical_audit_call(call)})
```

- [ ] Add these complete real-owner tests to F10's title module. The only replacement for authoring is the paid SDK function. The optional settlement fault replaces one real service call with an explicit failure before its write, after the actual private failure transaction has committed. Recovery uses a new repository/authority/service and actual generation takeover; it never mints an old live lease or reconstructs a call from the ledger. Use the normal fixture's existing SQLite engine and actual current authentication.

```python
import asyncio

import structlog
from sqlalchemy import select, update

from elspeth.contracts.chargeable_admission import ChargeableAdmissionPolicy
from elspeth.contracts.composer_llm_audit import ComposerLLMCallStatus
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.continuation_provider_outcomes import decode_physical_audit_call
from elspeth.web.coordination.composer_operation_authority import ComposerAsyncOperationAuthority
from elspeth.web.coordination.sqlite_authority import SQLiteLocalSessionOperationAuthority
from elspeth.web.secrets.wiring_policy import EMPTY_SECRET_WIRING_POLICY
from elspeth.web.sessions.models import (
    composer_continuation_effects_table, quota_policies_table, quota_provider_attempts_table,
    token_usage_ledger_table,
)
from elspeth.web.sessions.service import SessionServiceImpl
from elspeth.web.sessions.telemetry import build_sessions_telemetry
from tests.helpers.fenced_session import seed_token_policies


def new_title_sql_owners(store, tmp_path, *, configured=False):
    engine, _repository, _authority, _service, sid = store
    repository = SQLiteLocalSessionOperationAuthority(engine)
    authority = ComposerAsyncOperationAuthority(engine, owner_instance_id="test-owner", claim_lease_seconds=30,
        continuation_policy=minimal_continuation_policy())
    service = SessionServiceImpl(engine, data_dir=tmp_path, telemetry=build_sessions_telemetry(),
        log=structlog.get_logger("test.continuation-physical-outcomes"), session_operation_authority=repository,
        owner_instance_id="test-owner", chargeable_admission_policy=ChargeableAdmissionPolicy(
            identity_token_quota_configured=configured, secret_wiring_hash=EMPTY_SECRET_WIRING_POLICY.canonical_hash))
    return engine, repository, authority, service, sid


```

Add this complete claim-preserving helper and the actual failure tests:

```python
async def next_title_sql_owner(repository, authority):
    from elspeth.web.sessions.composer_operations import ComposerOperationRunning
    deadline = asyncio.get_running_loop().time() + 5
    while True:
        claims = authority.claim_segment(limit=1)
        if claims:
            (claim,) = claims
            context = repository.start_composer_segment(claim, owner_instance_id="test-owner", lease_seconds=30,
                auth_provider_type="local", segment_policy=minimal_continuation_policy().segment)
            return ComposerOperationRunning(claim=claim, session_operation_context=context)
        assert asyncio.get_running_loop().time() < deadline, "actual recovered segment never became claimable"
        await asyncio.sleep(0.02)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ("timeout", "malformed"))
@pytest.mark.parametrize("settlement_fault", (False, True))
async def test_actual_title_failure_survives_new_owner_without_retry_or_unknown_usage_zeroing(
    operation_store, tmp_path, monkeypatch, mode, settlement_fault,
):
    engine, repository, authority, service, sid = operation_store
    original_title = (await service.get_session(sid)).title
    running, _owner, custody, binding, prepared, request, _coordinator = await prepare_title_owner(operation_store)
    calls = []
    original = TimeoutError("actual SDK transport test failure")
    async def sdk(**kwargs):
        calls.append(kwargs)
        if mode == "timeout":
            raise original
        return ModelResponse(model="test-model", choices=[{"index": 0, "finish_reason": "tool_calls",
            "message": {"role": "assistant", "content": "Wrong Tool Response", "tool_calls": [
                {"id": "unexpected-title-call", "type": "function", "function": {"name": "set_metadata", "arguments": "{}"}}]}}],
            usage={"prompt_tokens": 9, "completion_tokens": 3, "total_tokens": 12})
    monkeypatch.setattr(litellm, "acompletion", sdk)
    actual_finish = service.finish_provider_attempt
    async def fail_before_real_settlement(**kwargs):
        raise OSError("controlled loss before actual ledger write")
    if settlement_fault:
        monkeypatch.setattr(service, "finish_provider_attempt", fail_before_real_settlement)
    try:
        with prompt_guard_user_scope(request.content):
            operation = call_continuation_title(service=service, session_id=sid, user_message=request.content,
                model="test-model", temperature=None, seed=None, session_operation_context=running.session_operation_context,
                prompt_guard_mount=None, prompt_guard_mode="debug", prepared=prepared, provider_custody=custody)
            if settlement_fault:
                with pytest.raises(BaseExceptionGroup) as caught:
                    await operation
                def leaves(error):
                    return tuple(leaf for child in error.exceptions for leaf in leaves(child)) if isinstance(error, BaseExceptionGroup) else (error,)
                if mode == "timeout":
                    assert any(leaf is original for leaf in leaves(caught.value))
            else:
                outcome = await operation
                assert outcome.disposition == "provider_failed"
        assert len(calls) == 1
        with engine.connect() as conn:
            effect = conn.execute(select(composer_continuation_effects_table).where(
                composer_continuation_effects_table.c.session_id == str(sid),
                composer_continuation_effects_table.c.operation_id == running.claim.operation_id,
                composer_continuation_effects_table.c.effect_key == binding.effect_key)).one()
            physical = conn.execute(select(quota_provider_attempts_table).where(
                quota_provider_attempts_table.c.attempt_id == effect.provider_attempt_id)).one()
        assert effect.disposition == "refused" and effect.result_chunk_key == "outcome:" + binding.effect_key
        assert (physical.settled_at is None) is settlement_fault
        repository.release(running.session_operation_context)
        new_store = new_title_sql_owners(operation_store, tmp_path / "new-owner")
        _engine, new_repository, new_authority, new_service, _sid = new_store
        assert new_repository.recover_composer_segment(session_id=sid, operation_id=running.claim.operation_id,
            expected_generation=running.claim.ownership_generation, actor_user_id="alice", auth_provider_type="local")
        current = await next_title_sql_owner(new_repository, new_authority)
        try:
            reconciled = await new_service.reconcile_continuation_provider_effect(
                running=current, effect_key=binding.effect_key)
            assert reconciled.disposition == "refused" and reconciled.response is None and reconciled.outcome is not None
            assert reconciled.ledger_entry_id is not None and reconciled.outcome.kind == "failed"
            assert reconciled.outcome.audit_call_json is not None and effect.response_finished_at is not None
            actual = decode_physical_audit_call(reconciled.outcome.audit_call_json)
            assert actual.call_id == physical.attempt_id and actual.finished_at == effect.response_finished_at.replace(tzinfo=actual.finished_at.tzinfo)
            assert actual.status is (ComposerLLMCallStatus.TIMEOUT if mode == "timeout" else ComposerLLMCallStatus.MALFORMED_RESPONSE)
            with engine.connect() as conn:
                ledger = conn.execute(select(token_usage_ledger_table).where(
                    token_usage_ledger_table.c.entry_id == reconciled.ledger_entry_id)).one()
            expected = (None, None) if mode == "timeout" else (9, 3)
            assert (ledger.prompt_tokens, ledger.completion_tokens) == expected
            auth = new_authority.load_continuation_provider_outcome(reconciled.binding,
                expected_checkpoint_id=reconciled.snapshot.checkpoint_id, expected_cursor=reconciled.snapshot.cursor)
            assert auth.outcome == reconciled.outcome
            completed = new_authority.complete_optional_continuation_title(reconciled.binding,
                expected_checkpoint_id=auth.snapshot.checkpoint_id, expected_cursor=auth.snapshot.cursor)
            top = decode_continuation_snapshot(completed.payload_json).frames[-1]
            assert top.ingress_phase == "ready" and top.title_response is None
            assert top.provider_effect_key is None and top.title_attempt_id == physical.attempt_id
            assert (await new_service.get_session(sid)).title == original_title and len(calls) == 1
            with pytest.raises(ContinuationCASConflict):
                new_authority.complete_optional_continuation_title(reconciled.binding,
                    expected_checkpoint_id=auth.snapshot.checkpoint_id, expected_cursor=auth.snapshot.cursor)
            assert new_authority.load_continuation_snapshot(current) == completed
        finally:
            new_repository.release(current.session_operation_context)
    finally:
        monkeypatch.setattr(service, "finish_provider_attempt", actual_finish)
        repository.release(running.session_operation_context)


@pytest.mark.asyncio
async def test_actual_quota_refusal_retains_no_sdk_outcome_and_no_attempt_or_ledger(operation_store, tmp_path, monkeypatch):
    engine, _repository, _authority, _service, sid = operation_store
    with engine.begin() as conn:
        identity_policy, _container_policy = seed_token_policies(conn, identity_id="alice")
        conn.execute(update(quota_policies_table).where(quota_policies_table.c.policy_id == identity_policy).values(tokens_per_day=0))
    store = new_title_sql_owners(operation_store, tmp_path / "configured", configured=True)
    _engine, repository, authority, service, _sid = store
    running, _owner, custody, binding, prepared, request, _coordinator = await prepare_title_owner(store)
    calls = []
    async def sdk(**kwargs):
        calls.append(kwargs)
        raise AssertionError("quota-refused title entered SDK")
    monkeypatch.setattr(litellm, "acompletion", sdk)
    try:
        with prompt_guard_user_scope(request.content):
            live = await call_continuation_title(service=service, session_id=sid, user_message=request.content,
                model="test-model", temperature=None, seed=None, session_operation_context=running.session_operation_context,
                prompt_guard_mount=None, prompt_guard_mode="debug", prepared=prepared, provider_custody=custody)
        assert live.disposition == "admission_refused" and calls == []
        actual = await service.reconcile_continuation_provider_effect(running=running, effect_key=binding.effect_key)
        assert actual.disposition == "refused" and actual.outcome is not None
        assert actual.outcome.reason == "admission_refused"
        assert actual.attempt_id is None and actual.ledger_entry_id is None and actual.outcome.audit_call_json is None
        with engine.connect() as conn:
            assert conn.execute(select(quota_provider_attempts_table.c.attempt_id).where(
                quota_provider_attempts_table.c.session_id == str(sid))).all() == []
            assert conn.execute(select(token_usage_ledger_table.c.entry_id).where(
                token_usage_ledger_table.c.session_id == str(sid))).all() == []
        completed = authority.complete_optional_continuation_title(actual.binding,
            expected_checkpoint_id=actual.snapshot.checkpoint_id, expected_cursor=actual.snapshot.cursor)
        assert decode_continuation_snapshot(completed.payload_json).frames[-1].ingress_phase == "ready"
    finally:
        repository.release(running.session_operation_context)


@pytest.mark.asyncio
async def test_unadmitted_stop_requires_actual_marker_and_never_dispatches_title(operation_store):
    from tests.unit.web.coordination.test_composer_operation_authority import cancelled
    _engine, repository, authority, _service, sid = operation_store
    running, _owner, _custody, binding, _prepared, _request, _coordinator = await prepare_title_owner(operation_store)
    before = authority.load_continuation_snapshot(running)
    try:
        with pytest.raises(AuditIntegrityError, match="unadmitted Stop"):
            authority.cancel_unadmitted_continuation_provider(binding,
                expected_checkpoint_id=before.checkpoint_id, expected_cursor=before.cursor)
        assert authority.load_continuation_snapshot(running) == before
        authority.request_cancel(session_id=sid, operation_id=running.claim.operation_id, cancelled_failure=cancelled)
        current = authority.cancel_unadmitted_continuation_provider(binding,
            expected_checkpoint_id=before.checkpoint_id, expected_cursor=before.cursor)
        auth = authority.load_continuation_provider_outcome(binding,
            expected_checkpoint_id=current.checkpoint_id, expected_cursor=current.cursor)
        assert auth.outcome.kind == "not_dispatched" and auth.outcome.reason == "stopped_before_admission"
        assert auth.outcome.attempt_id is None and auth.ledger_entry_id is None
        ready = authority.complete_optional_continuation_title(binding,
            expected_checkpoint_id=current.checkpoint_id, expected_cursor=current.cursor)
        assert decode_continuation_snapshot(ready.payload_json).frames[-1].ingress_phase == "ready"
    finally:
        repository.release(running.session_operation_context)


@pytest.mark.asyncio
async def test_actual_dispatched_title_without_outcome_remains_unknown_and_cannot_skip(operation_store, tmp_path, monkeypatch):
    _engine, repository, authority, service, sid = operation_store
    running, _owner, custody, binding, prepared, request, _coordinator = await prepare_title_owner(operation_store)
    class ControlledPhysicalOwnerLoss(BaseException):
        pass
    lost = ControlledPhysicalOwnerLoss("controlled loss before an audit outcome")
    calls = []
    async def sdk(**kwargs):
        calls.append(kwargs)
        raise lost
    monkeypatch.setattr(litellm, "acompletion", sdk)
    try:
        with prompt_guard_user_scope(request.content), pytest.raises(BaseExceptionGroup):
            await call_continuation_title(service=service, session_id=sid, user_message=request.content,
                model="test-model", temperature=None, seed=None, session_operation_context=running.session_operation_context,
                prompt_guard_mount=None, prompt_guard_mode="debug", prepared=prepared, provider_custody=custody)
        assert len(calls) == 1
        before = authority.load_continuation_snapshot(running)
        with pytest.raises(AuditIntegrityError, match="no actual closed outcome"):
            authority.complete_optional_continuation_title(binding,
                expected_checkpoint_id=before.checkpoint_id, expected_cursor=before.cursor)
        assert authority.load_continuation_snapshot(running) == before
        repository.release(running.session_operation_context)
        engine, new_repository, new_authority, new_service, _sid = new_title_sql_owners(operation_store, tmp_path / "unknown")
        assert new_repository.recover_composer_segment(session_id=sid, operation_id=running.claim.operation_id,
            expected_generation=running.claim.ownership_generation, actor_user_id="alice", auth_provider_type="local")
        current = await next_title_sql_owner(new_repository, new_authority)
        try:
            actual = await new_service.reconcile_continuation_provider_effect(running=current, effect_key=binding.effect_key)
            assert actual.disposition == "unknown" and actual.outcome is None and actual.response is None
            assert actual.ledger_entry_id is not None
            with engine.connect() as conn:
                ledger = conn.execute(select(token_usage_ledger_table).where(
                    token_usage_ledger_table.c.entry_id == actual.ledger_entry_id)).one()
            assert ledger.prompt_tokens is None and ledger.completion_tokens is None
            with pytest.raises(AuditIntegrityError, match="no actual closed outcome"):
                new_authority.complete_optional_continuation_title(actual.binding,
                    expected_checkpoint_id=actual.snapshot.checkpoint_id, expected_cursor=actual.snapshot.cursor)
            assert new_authority.load_continuation_snapshot(current) == actual.snapshot and len(calls) == 1
        finally:
            new_repository.release(current.session_operation_context)
    finally:
        repository.release(running.session_operation_context)
```

- [ ] Behavioral RED controls on the importable packet: omit only the O5 actual finally retention call and require the real failed-title test's `effect.result_chunk_key` assertion to fail; restore it. Treat every non-null result as `composer.response.v1` in recovery and require the new-owner failed outcome recovery to fail its exact nominal/usage assertions; restore the closed variant branch. Remove the explicit quota-refusal retention and require `actual.disposition == "refused"` to fail while the SDK count remains zero. Restore it. No import, collection or configuration failure counts as behavioral RED.

### O7.1: Keep a retained failure finite without recreating an SDK exception

**Modify:** `sessions/composer_operation_errors.py`; test its existing unit module. The new error is a closed v2 saved-outcome projection; ordinary v1 exception projections remain unchanged. Its finite `ContinuationProtocolError` base enters W4's existing actual-cohort-joined terminal failure path. Current durable Stop and integrity failures retain their higher priority. A manual child applies/persists its owed prefix before propagating this error.

- [ ] Add this complete pure projector to `composer_operation_errors.py`, importing the two physical outcome types normally. Insert the exact branch below in `project_composer_operation_error` immediately before its existing `if isinstance(leaf, HTTPException):` branch, after its stronger integrity/storage/principal cases.

```python
def project_retained_provider_failure(
    failure: ContinuationRetainedProviderFailure, *, request_id: str | None,
) -> ComposerOperationError:
    from elspeth.web.composer.continuation_provider_outcomes import decode_physical_audit_call
    outcome = failure.outcome
    if outcome.kind != "failed" or outcome.audit_call_json is None:
        return _http_error(409, {"error_type": "composer_action_refused",
            "detail": "The saved action was explicitly refused before provider dispatch.", "request_id": request_id},
            "composer_action_refused")
    call = decode_physical_audit_call(outcome.audit_call_json)
    status = (500 if outcome.failure_kind == "first_party" else 503 if outcome.failure_kind == "cancelled"
        else 504 if call.status is ComposerLLMCallStatus.TIMEOUT else 502)
    body: dict[str, JsonValue] = {"error_type": "composer_provider_failed",
        "detail": "The saved provider action failed. The draft, actual audit and token accounting have been retained.",
        "request_id": request_id, "physical_call_id": outcome.attempt_id,
        "failure_kind": outcome.failure_kind, "error_class": call.error_class}
    if outcome.request_rejection is not None:
        body["request_rejection"] = dict(outcome.request_rejection.to_wire())
    return _http_error(status, body, "composer_provider_failed")
```

```python-statement
if isinstance(leaf, ContinuationRetainedProviderFailure):
    return project_retained_provider_failure(leaf, request_id=request_id)
```

Import `ComposerLLMCallStatus` from its contract; `ComposerOperationError`, `JsonValue` and `_http_error` are the existing exact owner types/helper in this module. Do not construct a fake SDK exception or pass a cold descriptor to `classify_provider_failure`. This v2 typed saved-failure body deliberately states what survived; it does not assert the original Python exception object survived process loss.

- [ ] Add this complete test to O4's module, alongside its existing actual-record fixture:

```python
def test_saved_failure_projection_is_finite_and_uses_the_actual_retained_call():
    from elspeth.web.composer.continuation_provider_outcomes import (
        ContinuationRetainedProviderFailure, RetainedContinuationProviderOutcome,
    )
    from elspeth.web.sessions.composer_operation_errors import project_composer_operation_error
    call, _metadata = actual_record()
    actual = replace(call, status=ComposerLLMCallStatus.TIMEOUT, error_class="TimeoutError", error_message="TimeoutError")
    outcome = RetainedContinuationProviderOutcome(kind="failed", effect_key="provider:0:0",
        input_sha256="a" * 64, attempt_id=actual.call_id, audit_call_json=encode_physical_audit_call(actual),
        reason=None, failure_kind="provider")
    error = project_composer_operation_error(ContinuationRetainedProviderFailure(outcome), request_id="actual-request")
    assert error.http_status == 504 and error.error_type == "composer_provider_failed"
    assert error.body["physical_call_id"] == actual.call_id
    assert error.body["request_id"] == "actual-request" and error.body["error_class"] == "TimeoutError"
```

```bash
cd "$COMPOSER_WORKTREE" && "$COMPOSER_PYTHON" -m pytest -n 0 tests/unit/web/composer/test_continuation_physical_outcomes.py tests/unit/web/composer/test_continuation_title.py tests/unit/web/composer/test_continuation_provider_accounting.py > "$COMPOSER_LOG_DIR/continuation-physical-failures.log" 2>&1
COMPOSER_RC=$?
cat "$COMPOSER_LOG_DIR/continuation-physical-failures.log"
test "$COMPOSER_RC" -eq 0
```

Run the storage SQLite and serial PostgreSQL packets with these new tests imported in the existing testcontainer module. Run the main plan's affected whole-tree gates and complete schema/session persistence qualification before integration. Stage only the actual modified provider/SQL/codec/service/protocol/test paths after normal branch safety and inspect the exact staged set; commit with normal hooks as `fix(composer): retain actual failed physical outcomes across continuation`.

## O8: Freeze the actual manual failure invocation before its private SQL receipt

**Modify:** `composer/continuation_provider_outcomes.py`, `composer/provider_quota.py`, the actual F21 Advisor caller, B5's retained manual application body and authority.

The v2 manual call's outer audit closes at its real provider-failure retention boundary. Its prepared dispatch audit retains the already measured active prefix; a new process reanchors that known elapsed prefix before the new physical owner starts. The actual failure builder measures the current active work once, before SQL. Both warm and cold application reuse that exact complete invocation. No recovery wall time, invented original exception or ledger-derived timing enters it.

- [ ] Add `manual_budget_limit:int|None=Field(default=None,ge=0)` to F13's `ContinuationAdvisorInvocation`. Add `or self.manual_budget_limit is None` to its manual-kind guard, and `or self.manual_budget_limit is not None` to the automatic-kind guard. Import `Field` from Pydantic normally there. Replace its manual factory with this complete body, and pass `budget_limit=self.ctx.advisor_max_calls_per_compose` in B5 `push_manual_advisor`'s sole factory call. The exact admitted budget survives configuration changes while the provider result is owed.

```python
def make_continuation_manual_advisor_invocation(
    *, arguments: RequestAdvisorHintArgumentsModel, tool_call_id: str,
    stack: ContinuationFrameStack, budget_limit: int,
) -> ContinuationAdvisorInvocation:
    if (type(arguments) is not RequestAdvisorHintArgumentsModel or type(tool_call_id) is not str or not tool_call_id
        or type(budget_limit) is not int or budget_limit < 0):
        raise TypeError("manual Advisor source requires its actual admitted input/identity/budget")
    frame = nearest_compose_frame(stack)
    coverage = stable_hash({"logical": stack.logical.model_dump(mode="json"), "state": frame.state.digest,
        "evidence": frame.evidence.digest, "transcript": frame.transcript.digest,
        "tool_call_id": tool_call_id, "budget_limit": budget_limit})
    return ContinuationAdvisorInvocation(kind="manual", state=frame.state, evidence=frame.evidence,
        transcript=frame.transcript, iteration=frame.iteration, coverage_binding=coverage,
        manual_arguments_json=canonical_json(arguments.model_dump(mode="json")), manual_tool_call_id=tool_call_id,
        candidate=None, premise=None, manual_budget_limit=budget_limit)
```

- [ ] Add paired optional `manual_invocation_json: str | None = None` and `manual_payload_json: str | None = None`, plus `manual_budget_limit: int | None = Field(default=None, ge=0)` to `RetainedContinuationProviderOutcome`. Provider failures have an actual payload and invocation; first-party/cancelled failures have an actual invocation and null payload. No-SDK outcomes carry none. Add this exact check to `require_actual_outcome` immediately before its return:

```python-statement
if self.manual_invocation_json is None:
    if self.manual_payload_json is not None or self.manual_budget_limit is not None:
        raise ValueError("manual failure metadata lost its actual complete invocation")
elif (self.kind not in ("failed", "not_dispatched") or self.manual_budget_limit is None
      or (self.kind == "not_dispatched" and self.reason != "admission_refused")
      or (self.failure_kind == "provider") != (self.manual_payload_json is not None)):
    raise ValueError("manual failure payload disagrees with its actual physical disposition")
```

- [ ] Add these complete process-local context and codec functions to `continuation_provider_outcomes.py`. The context is never serialized. Its actual dispatch audit is the same complete selected prepared audit, freshly reanchored using B1's existing owned restore. Its budget comes from the actual service configuration, not a model answer.

```python
import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING

from elspeth.contracts.composer_audit import ComposerToolInvocation
from elspeth.web.composer.audit import DispatchAudit, finish_cancelled, finish_plugin_crash, finish_success

_MANUAL_INVOCATION = TypeAdapter(ComposerToolInvocation)

if TYPE_CHECKING:
    from elspeth.web.composer.service import ComposerServiceImpl
    from elspeth.web.composer.continuation_execution import SelectedContinuationState


def encode_manual_failure_invocation(value: ComposerToolInvocation) -> str:
    if type(value) is not ComposerToolInvocation:
        raise TypeError("manual failure requires its actual complete invocation")
    return canonical_json(owned_json(_MANUAL_INVOCATION.dump_python(value, mode="json", warnings="error", fallback=owned_json)))


def decode_manual_failure_invocation(encoded: str) -> ComposerToolInvocation:
    try:
        value = _MANUAL_INVOCATION.validate_json(encoded, strict=True, extra="forbid")
    except (ValidationError, ValueError, TypeError) as error:
        raise AuditIntegrityError("manual failure invocation violates its complete actual contract") from error
    if encode_manual_failure_invocation(value) != encoded:
        raise AuditIntegrityError("manual failure invocation omitted or changed actual fields")
    return value


@dataclass(frozen=True, slots=True)
class ContinuationManualFailureContext:
    audit: DispatchAudit
    version: int
    budget_used: int
    budget_limit: int


def make_continuation_manual_failure_context(
    *, service: ComposerServiceImpl, selected: SelectedContinuationState,
) -> ContinuationManualFailureContext | None:
    from elspeth.web.composer.service import ComposerServiceImpl
    from elspeth.web.composer.continuation import AdvisorFrame, ToolBatchFrame
    from elspeth.web.composer.continuation_advisor import decode_continuation_advisor_invocation
    from elspeth.web.composer.continuation_execution import SelectedContinuationState, continuation_chunk
    from elspeth.web.composer.continuation_tool_batch import restore_dispatch_audit, validate_batch_frame
    if type(service) is not ComposerServiceImpl or type(selected) is not SelectedContinuationState:
        raise TypeError("manual failure context requires its actual current service/selected state")
    root = selected.stack
    child = root.frames[-1]
    if type(child) is not AdvisorFrame:
        return None
    source = decode_continuation_advisor_invocation(continuation_chunk(selected.chunks, child.review))
    if source.kind != "manual":
        return None
    if len(root.frames) < 2 or type(root.frames[-2]) is not ToolBatchFrame:
        raise AuditIntegrityError("manual failure context lost its paused actual batch")
    value = validate_batch_frame(root.frames[-2], continuation_chunk(selected.chunks, root.frames[-2].accumulator))
    call = value.current_call
    if (call is None or call.call_id != source.manual_tool_call_id or call.tool_name != "request_advisor_hint"
        or source.manual_budget_limit is None):
        raise AuditIntegrityError("manual failure context changed its actual issued call")
    return ContinuationManualFailureContext(restore_dispatch_audit(call.audit), call.pre_version,
        value.advisor_calls_used, source.manual_budget_limit)


def manual_failure_payload(*, error_class: str | None, used: int, limit: int,
                           rejection: SafeRequestRejection | None) -> dict[str, object]:
    payload: dict[str, object] = {"status": "ADVISOR_ERROR",
        "error": "Advisor call failed; no guidance returned." if rejection is None else rejection.explanation,
        "error_class": error_class, "budget_used": used, "budget_remaining": limit - used}
    if rejection is not None:
        payload["request_rejection"] = rejection.to_wire()
    return payload


def freeze_manual_failure(
    context: ContinuationManualFailureContext, outcome: RetainedContinuationProviderOutcome,
    *, original: BaseException,
) -> RetainedContinuationProviderOutcome:
    if type(context) is not ContinuationManualFailureContext:
        raise AuditIntegrityError("manual failure lost its actual physical/context owners")
    payload = None
    if outcome.kind == "not_dispatched":
        from elspeth.contracts.chargeable_admission import ChargeableAdmissionRefused
        if outcome.reason != "admission_refused" or not isinstance(original, ChargeableAdmissionRefused):
            raise AuditIntegrityError("manual no-SDK failure lost its actual typed refusal")
        invocation = finish_plugin_crash(context.audit, exc=original)
    elif outcome.audit_call_json is None:
        raise AuditIntegrityError("manual failure lost its complete actual physical audit")
    elif outcome.failure_kind == "provider":
        actual = decode_physical_audit_call(outcome.audit_call_json)
        if actual.error_class != type(original).__name__:
            raise AuditIntegrityError("manual failure builder lost its actual original exception")
        payload = manual_failure_payload(error_class=actual.error_class, used=context.budget_used,
            limit=context.budget_limit, rejection=outcome.request_rejection)
        invocation = finish_success(context.audit, result_payload=payload, version_after=context.version)
    elif outcome.failure_kind == "cancelled":
        actual = decode_physical_audit_call(outcome.audit_call_json)
        if actual.error_class != type(original).__name__:
            raise AuditIntegrityError("cancelled manual failure changed its actual original class")
        if not isinstance(original, asyncio.CancelledError):
            raise AuditIntegrityError("cancelled manual failure lost its actual original cancellation")
        invocation = finish_cancelled(context.audit, exc=original)
    else:
        actual = decode_physical_audit_call(outcome.audit_call_json)
        if actual.error_class != type(original).__name__:
            raise AuditIntegrityError("manual host failure changed its actual original class")
        invocation = finish_plugin_crash(context.audit, exc=original)
    return outcome.model_copy(update={"manual_invocation_json": encode_manual_failure_invocation(invocation),
        "manual_payload_json": None if payload is None else canonical_json(payload),
        "manual_budget_limit": context.budget_limit})
```

Use explicit annotations on the context factory: `service:ComposerServiceImpl`, `selected:SelectedContinuationState`, importing those two names under `TYPE_CHECKING` in this module. Its existing future import defers the annotations; runtime nominal imports remain local as shown.

- [ ] Add `_continuation_manual_failure_context` to custody slots and initialize it to `None`, with annotation `ContinuationManualFailureContext | None`. Import that type normally from the pure outcome module. Add this complete one-time binder; F21 calls it immediately after `mint_continuation` and before entering any provider call, using the complete selected state returned by preparation.

```python
def bind_continuation_manual_failure_context(self, value: ContinuationManualFailureContext | None) -> None:
    if value is None:
        return
    if (type(value) is not ContinuationManualFailureContext or self._continuation_binding is None
        or self._continuation_binding.family != 1 or self._sdk_entered or self.attempt is not None
        or self._continuation_manual_failure_context is not None):
        raise AuditIntegrityError("manual failure context changed its actual pre-dispatch provider owner")
    self._continuation_manual_failure_context = value
```

```python-statement
custody.bind_continuation_manual_failure_context(
    make_continuation_manual_failure_context(service=service, selected=selected))
```

- [ ] Add `original_exception:BaseException|None` to `_retain_continuation_provider_outcome_value` and `retain_continuation_failure` keyword parameters. Forward it from the latter's V closure; `retain_continuation_admission_refusal` passes its actual `refusal` as `original_exception`. The actual finally hook passes `original_exception=sys.exception()` in addition to its already shown classification/rejection arguments. Immediately after the complete failed/no-SDK `if/else` has constructed `value`, before reserving SQL tickets, insert:

```python-statement
if self._continuation_manual_failure_context is not None:
    if original_exception is None:
        raise AuditIntegrityError("manual failure retention lost its actual original body outcome")
    from elspeth.web.composer.continuation_provider_outcomes import freeze_manual_failure
    value = freeze_manual_failure(self._continuation_manual_failure_context, value, original=original_exception)
```

This receives the actual currently unwinding exception only inside the current process. It is used by the actual existing audit builder and never encoded. The complete immutable invocation, safe payload and full physical audit commit together in O5's existing single V SQL transaction.

- [ ] Add this complete current SQL validation to `sessions/continuation.py`. Call it from `retain_provider_outcome_on_connection` immediately before `put_chunk`, using that already selected `stored`, and again in `require_selected_provider_outcome_on_connection` before returning its nominal authorization. The latter verifies the retained immutable outer invocation against the exact still-paused caller before any cold application.

```python
def require_manual_failure_packet_on_connection(
    conn: Connection, binding: ContinuationProviderBinding, *, stored: StoredContinuationSnapshot,
    outcome: RetainedContinuationProviderOutcome,
) -> None:
    from elspeth.contracts.composer_audit import ComposerToolStatus
    from elspeth.web.composer.continuation import AdvisorFrame, ToolBatchFrame
    from elspeth.web.composer.continuation_advisor import decode_continuation_advisor_invocation
    from elspeth.web.composer.continuation_execution import continuation_chunk
    from elspeth.web.composer.continuation_provider_outcomes import (
        decode_manual_failure_invocation, decode_physical_audit_call, manual_failure_payload,
    )
    from elspeth.web.composer.continuation_tool_batch import validate_batch_frame
    root = decode_continuation_snapshot(stored.payload_json)
    values = load_private_chunks(conn, session_id=str(binding.running.claim.session_id),
        operation_id=binding.running.claim.operation_id, stack=root)
    child = root.frames[-1]
    source = (decode_continuation_advisor_invocation(continuation_chunk(values, child.review))
        if type(child) is AdvisorFrame else None)
    if source is None or source.kind != "manual":
        if outcome.manual_invocation_json is not None:
            raise AuditIntegrityError("nonmanual physical failure claimed an outer tool invocation")
        return
    if (len(root.frames) < 2 or type(root.frames[-2]) is not ToolBatchFrame
        or outcome.manual_invocation_json is None or outcome.manual_budget_limit is None
        ):
        raise AuditIntegrityError("manual physical failure lacks its complete actual outer owner receipt")
    batch = validate_batch_frame(root.frames[-2], continuation_chunk(values, root.frames[-2].accumulator))
    call = batch.current_call
    actual = None if outcome.audit_call_json is None else decode_physical_audit_call(outcome.audit_call_json)
    invocation = decode_manual_failure_invocation(outcome.manual_invocation_json)
    if call is None:
        raise AuditIntegrityError("manual failure no longer owns its actual prepared call")
    audit = call.audit
    if (call.call_id != source.manual_tool_call_id or call.tool_name != "request_advisor_hint"
        or (invocation.tool_call_id, invocation.tool_name, invocation.arguments_canonical, invocation.arguments_hash,
            invocation.version_before, invocation.started_at, invocation.actor,
            invocation.authority_arguments_canonical, invocation.authority_arguments_hash,
            invocation.strict_sent, invocation.wire_conformant) != (
            call.call_id, call.tool_name, audit.arguments_canonical, audit.arguments_hash,
            audit.version_before, audit.started_at, audit.actor,
            audit.authority_arguments_canonical, audit.authority_arguments_hash, audit.strict_sent, audit.wire_conformant)
        or invocation.latency_ms < audit.elapsed_active_ns // 1_000_000
        or outcome.manual_budget_limit != source.manual_budget_limit
        or outcome.manual_budget_limit < batch.advisor_calls_used):
        raise AuditIntegrityError("manual failure changed its issued call/audit/current budget")
    if outcome.failure_kind == "provider":
        if actual is None:
            raise AuditIntegrityError("manual provider failure lost its actual physical call")
        expected = manual_failure_payload(error_class=actual.error_class, used=batch.advisor_calls_used,
            limit=outcome.manual_budget_limit, rejection=outcome.request_rejection)
        if (outcome.manual_payload_json != canonical_json(expected)
            or invocation.status is not ComposerToolStatus.SUCCESS or invocation.version_after != call.pre_version
            or invocation.result_canonical != canonical_json(expected) or invocation.result_hash != stable_hash(expected)
            or invocation.error_class is not None or invocation.error_message is not None):
            raise AuditIntegrityError("manual provider failure changed its actual fixed feedback/invocation")
    elif (outcome.manual_payload_json is not None or invocation.result_canonical is not None
        or invocation.result_hash is not None or invocation.version_after is not None
        or invocation.error_class != ("ChargeableAdmissionRefused" if actual is None else actual.error_class)
        or invocation.status is not (ComposerToolStatus.CANCELLED if outcome.failure_kind == "cancelled" else ComposerToolStatus.PLUGIN_CRASH)):
        raise AuditIntegrityError("manual controlled-code failure changed its actual immutable invocation")
```

The exact inserted call at both seams is:

```python-statement
require_manual_failure_packet_on_connection(conn, binding, stored=stored, outcome=outcome)
```

In `require_selected_provider_outcome_on_connection` its local outcome is named `known`; pass `outcome=known` there. The same-connection selected-CAS/current physical/settled ledger checks remain the authority boundary. This packet grants no new dispatch or business mutation after cancellation.

- [ ] Add this complete child-pop facade to the actual authority. It accepts only the exact actual saved failure, same paused call and one-result prefix extension. The complete recorder snapshot must contain the exact immutable invocation once. No result is inferred from a generic `ToolResult` or final row count.

```python
def commit_retained_provider_failure_application(
    self, binding: ContinuationProviderBinding, *, expected_checkpoint_id: str, expected_cursor: int,
    snapshot_json: str, chunks: tuple[PrivateChunk, ...],
) -> StoredContinuationSnapshot:
    from elspeth.web.composer.continuation import AdvisorFrame, ComposeFrame, ToolBatchFrame
    from elspeth.web.composer.continuation_audit_buffer import decode_recorder_snapshot
    from elspeth.web.composer.continuation_execution import continuation_chunk
    from elspeth.web.composer.continuation_provider_outcomes import decode_manual_failure_invocation
    from elspeth.web.composer.continuation_tool_batch import validate_batch_frame
    running = binding.running
    sid, op = str(running.claim.session_id), running.claim.operation_id
    with locked_session_transaction(self._engine, sid) as conn:
        stored = require_snapshot_cas(conn, session_id=sid, operation_id=op,
            expected_checkpoint_id=expected_checkpoint_id, expected_cursor=expected_cursor)
        auth = require_selected_provider_outcome_on_connection(conn, binding, stored=stored)
        before, after = decode_continuation_snapshot(stored.payload_json), decode_continuation_snapshot(snapshot_json)
        if (len(before.frames) < 2 or type(before.frames[-1]) is not AdvisorFrame
            or type(before.frames[-2]) is not ToolBatchFrame or len(after.frames) != len(before.frames)-1
            or type(after.frames[-1]) is not ToolBatchFrame or auth.outcome.manual_invocation_json is None):
            raise AuditIntegrityError("manual failure application changed its exact selected child/pop")
        old_values = load_private_chunks(conn, session_id=sid, operation_id=op, stack=before)
        old = validate_batch_frame(before.frames[-2], continuation_chunk(old_values, before.frames[-2].accumulator))
        new = validate_batch_frame(after.frames[-1], continuation_chunk(chunks, after.frames[-1].accumulator))
        if old.current_call is None:
            raise AuditIntegrityError("manual failure lost its exact prepared parent call")
        expected_kind = "advisor" if auth.outcome.failure_kind == "provider" else "none"
        expected_error = None if expected_kind == "advisor" else decode_manual_failure_invocation(auth.outcome.manual_invocation_json).error_class
        if (new.state_json != old.state_json or new.entry_state_json != old.entry_state_json
            or new.advisor_calls_used != old.advisor_calls_used or new.next_tool_index != old.next_tool_index+1
            or new.current_call is not None or new.tool_outcomes[:-1] != old.tool_outcomes
            or len(new.tool_outcomes) != len(old.tool_outcomes)+1
            or (new.tool_outcomes[-1].call_id, new.tool_outcomes[-1].response_kind,
                new.tool_outcomes[-1].advisor_json, new.tool_outcomes[-1].error_class) != (
                old.current_call.call_id, expected_kind, auth.outcome.manual_payload_json, expected_error)
            or (expected_kind == "none" and (not new.stop_issued_batch or new.phase != "dispatch_ready"))):
            raise AuditIntegrityError("manual failure application changed its actual prefix/result or dispatched another tool")
        if (after.logical != before.logical or after.cursor != before.cursor or after.policy_hash != before.policy_hash
            or after.admission_policy_json != before.admission_policy_json
            or after.effect_frontier != before.effect_frontier or after.pending_settlement_ids != before.pending_settlement_ids
            or after.pending_owner_results != before.pending_owner_results or after.semantic_frontier != before.semantic_frontier
            or after.active_action_json != before.active_action_json or after.active_action_source != before.active_action_source):
            raise AuditIntegrityError("manual failure application changed another actual owner frontier")
        compose = tuple(frame for frame in after.frames if type(frame) is ComposeFrame)
        if len(compose) != 1:
            raise AuditIntegrityError("manual failure application lost its exact caller recorder")
        recorder = decode_recorder_snapshot(continuation_chunk(chunks, compose[0].audit_buffer))
        actual = decode_manual_failure_invocation(auth.outcome.manual_invocation_json)
        retained = tuple(item for item in recorder.invocations
            if (item.tool_call_id, item.started_at) == (actual.tool_call_id, actual.started_at))
        if retained != (actual,):
            raise AuditIntegrityError("manual failure application changed or duplicated its complete actual invocation")
        put_private_chunks(conn, session_id=sid, operation_id=op, values=chunks)
        load_private_chunks(conn, session_id=sid, operation_id=op, stack=after)
        return append_complete_snapshot(conn, session_id=sid, operation_id=op,
            expected_checkpoint_id=stored.checkpoint_id, expected_cursor=stored.cursor, snapshot_json=snapshot_json,
            event_batch=(ContinuationEvent("checkpoint", canonical_json({"phase": "manual_failure_applied",
                "effect_key": binding.effect_key, "tool_call_id": actual.tool_call_id})),), handoff=False)
```

- [ ] Add `retained_failure:ContinuationProviderBinding|None=None` to the B8 public `checkpoint` and its retained `_checkpoint_value` signatures, forwarding it unchanged to that body. Insert this exact first branch before `if admitted_primary is not None:` and change the latter to `elif admitted_primary is not None:`. Keep the existing registered outcome/projection/checkpoint reload/deferred-cancellation suffix unchanged. This includes the actual SQL write and projection in B8's existing cumulative V stage.

```python-statement
if retained_failure is not None:
    if admitted_primary is not None or retained_provider is not None or consumed_effect_key is not None or completed_memory:
        raise AuditIntegrityError("manual failure application cannot consume a different owner")
    outcome = await run_required_sql_finish_once(sql, self.authority.commit_retained_provider_failure_application,
        retained_failure, expected_checkpoint_id=self.checkpoint_id, expected_cursor=self.stack.cursor,
        snapshot_json=encode_continuation_snapshot(root), chunks=values)
```

- [ ] Add this complete actual application owner to `tool_batch.py`. It restores only the original admitted primary batch and actual selected current failure. The invocation and provider audit were already measured and frozen at their actual owner; both are appended once with complete-byte equality. Original warm first-party exceptions stay live and are returned through the real dispatch; cold application names its retained failure instead of recreating the original exception.

```python
async def _apply_continuation_manual_failure_value(
    *, ctx: ToolBatchContext, acc: BatchAccumulator, llm_messages: list[dict[str, Any]],
    stack: ContinuationFrameStack, chunks: tuple[PrivateChunk, ...], checkpoint_id: str,
    required_work: RequiredWorkBinding, provider_owner: ProviderInvocationOwner,
    warm_original: Exception | None,
) -> ContinuationToolBatchAdvance:
    from elspeth.web.composer.continuation import AdvisorFrame
    from elspeth.web.composer.continuation_advisor import decode_continuation_advisor_invocation
    from elspeth.web.composer.continuation_codec import decode_continuation_response
    from elspeth.web.composer.continuation_execution import continuation_chunk
    from elspeth.web.composer.continuation_provider_outcomes import (
        ContinuationRetainedProviderFailure, decode_manual_failure_invocation, decode_physical_audit_call,
    )
    from elspeth.web.sessions.continuation import AuthorizedContinuationProviderOutcome, ContinuationProviderBinding
    if (len(stack.frames) < 2 or type(stack.frames[-1]) is not AdvisorFrame
        or type(stack.frames[-2]) is not ToolBatchFrame or required_work.running is None):
        raise AuditIntegrityError("manual failed response lost its actual paused caller")
    child, frame = stack.frames[-1], stack.frames[-2]
    if child.provider_effect_key is None or child.provider_input_sha256 is None or child.response is not None:
        raise AuditIntegrityError("manual failed response lost its exact physical failure source")
    source = decode_continuation_advisor_invocation(continuation_chunk(chunks, child.review))
    if source.kind != "manual":
        raise AuditIntegrityError("manual failure selected an automatic review")
    call_model = _CallModelOutcome(decode_continuation_response(continuation_chunk(chunks, frame.response)))
    batch = restore_continuation_batch_value(validate_batch_frame(frame, continuation_chunk(chunks, frame.accumulator)),
        call_model.completion.tool_batch.calls)
    call = batch.current_call
    if call is None or call.tool_call.id != source.manual_tool_call_id or call.audit is None:
        raise AuditIntegrityError("manual failure changed its actual prepared call")
    key_parts = child.provider_effect_key.split(":")
    if len(key_parts) != 3 or key_parts[0] != "provider" or key_parts[1] != "1" or not key_parts[2].isdigit():
        raise AuditIntegrityError("manual failure selected a noncanonical Advisor effect")
    binding = ContinuationProviderBinding(required_work.running, child.provider_effect_key, 1,
        int(key_parts[2]), child.provider_input_sha256)
    execution = ContinuationBatchExecution(call_model, ctx, acc, llm_messages, batch, stack, chunks,
        checkpoint_id, required_work, provider_owner)
    execution.ctx = replace(ctx, continuation=execution)
    async def authorize() -> AuthorizedContinuationProviderOutcome:
        sql, projection = required_work.reserve_pair(RequiredWorkSource.CONTINUATION_READ_SQL,
            RequiredWorkSource.CONTINUATION_READ_PROJECTION)
        result = await run_required_sql_finish_once(sql, execution.authority.load_continuation_provider_outcome,
            binding, expected_checkpoint_id=checkpoint_id, expected_cursor=stack.cursor)
        actual = execution.accept_sql_result(sql=sql, projection=projection,
            source=RequiredWorkSource.CONTINUATION_READ_SQL, outcome=result)
        if type(actual) is not AuthorizedContinuationProviderOutcome:
            raise AuditIntegrityError("manual failure read returned no actual selected SQL authorization")
        return actual
    authorization = await execution.local_clock().run("retention", authorize)
    retained = authorization.outcome
    if retained.manual_invocation_json is None:
        raise AuditIntegrityError("manual failure lacks its actual complete immutable owner records")
    invocation = decode_manual_failure_invocation(retained.manual_invocation_json)
    if retained.audit_call_json is not None:
        physical_call = decode_physical_audit_call(retained.audit_call_json)
        ctx.recorder.restore_received_llm_call(physical_call)
    matches = tuple(item for item in ctx.recorder.invocations
        if (item.tool_call_id, item.started_at) == (invocation.tool_call_id, invocation.started_at))
    if matches and matches != (invocation,):
        raise AuditIntegrityError("manual failure conflicts with a different complete invocation")
    if not matches:
        ctx.recorder.record(invocation)
    if retained.failure_kind == "provider":
        if retained.manual_payload_json is None:
            raise AuditIntegrityError("manual provider failure lost its actual fixed tool feedback")
        payload, error = parse_json_strict(retained.manual_payload_json)
        if error is not None or type(payload) is not dict:
            raise AuditIntegrityError("manual provider failure payload is not its closed actual mapping")
        _append_continuation_tool_outcome(batch, call, response=payload, error_class=None,
            error_category=None, error_message=None, post_version=batch.state.version)
        ctx.anti_anchor.record_failure(call.tool_name, call.audit.arguments_hash)
        content = json.dumps(advisor_feedback_payload(payload))
        llm_messages.append({"role": "tool", "tool_call_id": call.tool_call.id, "content": content,
            PROMPT_GUARD_CONTENT_PARTS_KEY: json_content_parts(content)})
        batch.turn_has_discovery = True
    else:
        if invocation.error_class is None:
            raise AuditIntegrityError("manual failed invocation lost its actual class")
        _append_continuation_tool_outcome(batch, call, response=None, error_class=invocation.error_class,
            error_category=None, error_message=invocation.error_message, post_version=batch.state.version)
        if warm_original is not None:
            if type(warm_original).__name__ != invocation.error_class:
                raise AuditIntegrityError("warm manual failure replaced its actual original exception")
            batch.advisor_failure = warm_original
        else:
            batch.advisor_failure = ContinuationRetainedProviderFailure(retained)
            batch.retained_failure = RetainedBatchFailure(kind="advisor_failure", error_class=invocation.error_class)
        batch.stop_issued_batch = True
    batch.next_tool_index += 1
    batch.current_call = None
    batch.phase = "dispatch_ready" if batch.stop_issued_batch or batch.next_tool_index == len(call_model.completion.tool_batch.calls) else "call_ready"
    execution.stack = stack.model_copy(update={"frames": stack.frames[:-1]})
    await execution.checkpoint(retained_failure=binding)
    dispatch = None
    if batch.phase == "dispatch_ready":
        dispatch, _ = _finish_continuation_batch_live(call_model=call_model, ctx=execution.ctx, batch=batch)
    return ContinuationToolBatchAdvance(execution.stack, execution.chunks, execution.checkpoint_id,
        dispatch, batch.advisor_calls_used)


async def apply_continuation_manual_failure(
    *, ctx: ToolBatchContext, acc: BatchAccumulator, llm_messages: list[dict[str, Any]],
    stack: ContinuationFrameStack, chunks: tuple[PrivateChunk, ...], checkpoint_id: str,
    required_work: RequiredWorkBinding, provider_owner: ProviderInvocationOwner,
    warm_original: Exception | None = None,
) -> ContinuationToolBatchAdvance:
    async def execute(bounded: ToolBatchContext) -> ContinuationToolBatchAdvance:
        return await _apply_continuation_manual_failure_value(ctx=bounded, acc=acc,
            llm_messages=llm_messages, stack=stack, chunks=chunks, checkpoint_id=checkpoint_id,
            required_work=required_work, provider_owner=provider_owner, warm_original=warm_original)
    return await _run_batch_entry(ctx=ctx, stack=stack, chunks=chunks, local_sql_only=True, operation=execute)
```

Import `parse_json_strict` from its existing contract parser in `tool_batch.py`. B1's existing `RetainedBatchFailure.error_class` requires nonempty text; the actual first-party/cancelled invocation has that exact value. Add the explicit `if invocation.error_class is None: raise AuditIntegrityError("manual failed invocation lost its actual class")` immediately before the controlled-code branch's `_append_continuation_tool_outcome` call.

In B1 `capture_continuation_batch_value`, change only `elif batch.advisor_failure is not None:` to `elif batch.advisor_failure is not None and batch.retained_failure is None:`. The actual supplied warm exception remains primary in the live dispatch. A cold retained failure preserves its already selected actual class and never overwrites it with the recovery carrier's class name. B6/F23 still persist the actual P4 prefix and classification before rethrowing a first-party failure; no generic provider-outage catch swallows it.

- [ ] Add the complete failure caller below to `composer/continuation_driver.py`. Each SQL result is projected through its registered owner before any deferred cancellation leaves. Only an actual settled failed/no-SDK receipt can reach the saved-outcome path. A live unrelated SQL/first-party failure is rethrown unchanged. Automatic Advisor failures terminate finitely; manual provider failures retain the original fixed feedback, while manual controlled-code failures finish their actual P4 prefix before rethrowing.

```python
from elspeth.contracts.chargeable_admission import ChargeableAdmissionRefused
from elspeth.web.composer.continuation_advisor import decode_continuation_advisor_invocation
from elspeth.web.composer.continuation_local_clock import ContinuationLocalActionClock
from elspeth.web.composer.continuation_provider_outcomes import (
    ContinuationRetainedProviderFailure, decode_physical_audit_call,
    make_continuation_manual_failure_context,
)
from elspeth.web.composer.llm_response_parsing import attach_llm_calls
from elspeth.web.composer.tool_batch import apply_continuation_manual_failure
from elspeth.web.sessions.continuation import AuthorizedContinuationProviderOutcome, ContinuationSnapshotBundle


def _project_advisor_failure_sql[T](
    *, required_work: RequiredWorkBinding, sql: RequiredWorkTicket, projection: RequiredWorkTicket,
    source: RequiredWorkSource, outcome: RequiredSQLFinishOnce[T],
) -> tuple[T, tuple[asyncio.CancelledError, ...]]:
    required_work.coordinator.validate_continuation_sql_outcome(ticket=sql,
        expected_source=source, actual_outcome=outcome)
    if type(outcome) is RequiredSQLRaised:
        projection.complete_without_submission()
        if outcome.deferred_cancellations:
            raise BaseExceptionGroup("Advisor failure SQL and original cancellation",
                [outcome.error, *outcome.deferred_cancellations])
        raise outcome.error
    if type(outcome) is not RequiredSQLReturned:
        error = AuditIntegrityError("Advisor failure lost its registered actual SQL outcome")
        projection.complete_without_submission()
        raise error
    projection.begin_projection()
    projection.complete_owned()
    return outcome.value, outcome.deferred_cancellations


async def apply_retained_continuation_provider_failure(
    *, service: ComposerServiceImpl, operation_authority: ComposerAsyncOperationAuthority,
    selected: SelectedContinuationState, binding: ContinuationProviderBinding,
    required_work: RequiredWorkBinding, provider_owner: ProviderInvocationOwner,
    progress: ComposerProgressSink | None, warm_original: BaseException | None = None,
) -> SelectedContinuationState:
    if required_work.running != binding.running or type(selected.stack.frames[-1]) not in (AdvisorFrame, ComposeFrame):
        raise AuditIntegrityError("Advisor failure caller lost its actual current selected child")
    cancellations: list[asyncio.CancelledError] = []
    policy = decode_continuation_policy(selected.stack.admission_policy_json, selected.stack.policy_hash)
    clock = ContinuationLocalActionClock(policy.actions.session_sql)

    async def read_actual() -> AuthorizedContinuationProviderOutcome:
        nonlocal selected
        sql, projection = required_work.reserve_pair(RequiredWorkSource.CONTINUATION_STATE_SQL,
            RequiredWorkSource.CONTINUATION_STATE_PROJECTION)
        outcome = await service._require_sessions_service().reconcile_continuation_provider_effect_finish_once(
            running=binding.running, effect_key=binding.effect_key, required_work=sql)
        receipt, deferred = _project_advisor_failure_sql(required_work=required_work, sql=sql,
            projection=projection, source=RequiredWorkSource.CONTINUATION_STATE_SQL, outcome=outcome)
        cancellations.extend(deferred)
        if type(receipt) is not ProviderEffectReconciliation:
            raise AuditIntegrityError("Advisor failure reconciliation returned another nominal owner")
        if receipt.disposition == "owner_active":
            raise ContinuationProviderWait("owner_unavailable")
        if receipt.disposition == "unknown":
            raise ContinuationProviderWait("unknown_effect")
        if receipt.disposition != "refused" or receipt.outcome is None:
            raise AuditIntegrityError("Advisor failure has no actual retained finite disposition")
        sql, projection = required_work.reserve_pair(RequiredWorkSource.CONTINUATION_READ_SQL,
            RequiredWorkSource.CONTINUATION_READ_PROJECTION)
        outcome = await run_required_sql_finish_once(sql, operation_authority.read_continuation_bundle, binding.running)
        bundle, deferred = _project_advisor_failure_sql(required_work=required_work, sql=sql,
            projection=projection, source=RequiredWorkSource.CONTINUATION_READ_SQL, outcome=outcome)
        cancellations.extend(deferred)
        if type(bundle) is not ContinuationSnapshotBundle or bundle.snapshot != receipt.snapshot:
            raise ContinuationCASConflict("Advisor failure head changed before its exact private owner read")
        selected = SelectedContinuationState(bundle.snapshot.checkpoint_id,
            decode_continuation_snapshot(bundle.snapshot.payload_json), bundle.chunks)
        sql, projection = required_work.reserve_pair(RequiredWorkSource.CONTINUATION_READ_SQL,
            RequiredWorkSource.CONTINUATION_READ_PROJECTION)
        outcome = await run_required_sql_finish_once(sql, operation_authority.load_continuation_provider_outcome,
            binding, expected_checkpoint_id=selected.checkpoint_id, expected_cursor=selected.stack.cursor)
        authorization, deferred = _project_advisor_failure_sql(required_work=required_work, sql=sql,
            projection=projection, source=RequiredWorkSource.CONTINUATION_READ_SQL, outcome=outcome)
        cancellations.extend(deferred)
        if type(authorization) is not AuthorizedContinuationProviderOutcome or authorization.outcome != receipt.outcome:
            raise AuditIntegrityError("Advisor failure changed between actual settled and selected proof")
        return authorization

    try:
        authorization = await clock.run("retention", read_actual)
        retained = authorization.outcome
        actual = None if retained.audit_call_json is None else decode_physical_audit_call(retained.audit_call_json)
        expected_class = "ChargeableAdmissionRefused" if actual is None else actual.error_class
        if warm_original is not None and type(warm_original).__name__ != expected_class:
            raise warm_original
        child = selected.stack.frames[-1]
        source = (decode_continuation_advisor_invocation(continuation_chunk(selected.chunks, child.review))
            if type(child) is AdvisorFrame else None)
        runtime = await restore_continuation_runtime(service=service, operation_authority=operation_authority,
            checkpoint_id=selected.checkpoint_id, stack=selected.stack, chunks=selected.chunks,
            required_work=required_work, provider_owner=provider_owner, progress=progress)
        if actual is not None:
            runtime.live.recorder.restore_received_llm_call(actual)
        if source is None or source.kind != "manual":
            failure = ContinuationRetainedProviderFailure(retained)
            attach_llm_calls(failure, runtime.live.recorder)
            raise failure
        owners = await continuation_batch_owners(service=service, operation_authority=operation_authority,
            selected=selected, required_work=required_work, provider_owner=provider_owner,
            progress=progress, runtime=runtime)
        advanced = await apply_continuation_manual_failure(ctx=owners.context, acc=owners.accumulator,
            llm_messages=runtime.live.llm_messages, stack=selected.stack, chunks=selected.chunks,
            checkpoint_id=selected.checkpoint_id, required_work=required_work, provider_owner=provider_owner,
            warm_original=warm_original if isinstance(warm_original, Exception) else None)
        selected = await finish_continuation_issued_batch(service=service, operation_authority=operation_authority,
            advanced=advanced, owners=owners, required_work=required_work, provider_owner=provider_owner,
            progress=progress, allow_early=False)
    except BaseException as error:
        original = warm_original if warm_original is not None else error
        values = [original]
        if error is not original:
            values.append(error)
        values.extend(item for item in cancellations if all(item is not prior for prior in values))
        if len(values) == 1:
            raise values[0]
        raise BaseExceptionGroup("Actual Advisor failure/application ownership", values)
    if isinstance(warm_original, asyncio.CancelledError):
        cancellations.insert(0, warm_original)
    if cancellations:
        unique = []
        for item in cancellations:
            if all(item is not prior for prior in unique):
                unique.append(item)
        if len(unique) == 1:
            raise unique[0]
        raise BaseExceptionGroup("Advisor original cancellations after owed P4", unique)
    return selected
```

`ProviderEffectReconciliation` carries the actual `snapshot:StoredContinuationSnapshot` from C5.7. The following registered bundle read requires exact equality with that receipt before exposing its complete private chunks. It never reads a newer checkpoint and attaches it to precomputed frames. Import `RequiredSQLFinishOnce`, its two closed variants and `RequiredWorkTicket` from their existing owners as in F17.

- [ ] Replace F21's exact `disposition == "refused"` arm with the following complete return. Wrap its current quota/user scopes and actual provider await span in the second block. The original actual provider bodies remain unchanged inside `invoke`; this simply gives the current caller the actual original outcome after their real scope has settled. Construct and bind the manual failure context before the scopes, as shown above.

```python-statement
if reconciled.receipt.disposition == "refused":
    return await apply_retained_continuation_provider_failure(service=service,
        operation_authority=operation_authority, selected=selected, binding=binding,
        required_work=required_work, provider_owner=provider_owner, progress=progress)
```

```python-statement
custody.bind_continuation_manual_failure_context(
    make_continuation_manual_failure_context(service=service, selected=selected))
try:
    with (composer_quota_scope(service._require_sessions_service(), running.session_operation_context),
          prompt_guard_user_scope(runtime.bindings.message)):
        if rebuilt.continuation is not None:
            if rebuilt.publication_review is None:
                raise AuditIntegrityError("semantic Advisor source lost its actual current publication review")
            await owner._call_continuation_advisor(context=rebuilt.continuation, state=rebuilt.state,
                publication_review=rebuilt.publication_review, recorder=runtime.live.recorder,
                custody=custody, timeout=policy.actions.advisor.physical, prepared=prepared)
        else:
            async with provider_call_scope(custody):
                await owner._call_advisor_with_audit_body(rebuilt.arguments, recorder=runtime.live.recorder,
                    timeout=policy.actions.advisor.physical, structured_output=rebuilt.structured_output,
                    provider_custody=custody, prepared=prepared)
except ChargeableAdmissionRefused as original:
    await custody.retain_continuation_admission_refusal(original)
    return await apply_retained_continuation_provider_failure(service=service,
        operation_authority=operation_authority, selected=selected, binding=binding,
        required_work=required_work, provider_owner=provider_owner, progress=progress,
        warm_original=original)
except BaseException as original:
    return await apply_retained_continuation_provider_failure(service=service,
        operation_authority=operation_authority, selected=selected, binding=binding,
        required_work=required_work, provider_owner=provider_owner, progress=progress,
        warm_original=original)
```

- [ ] In F32, immediately after its actual `run_continuation_advisor_provider` return, add this complete guard before either successful manual/EARLY application. Preserve the existing Stop branch and B9's prepared-effect selector.

```python-statement
if type(selected.stack.frames[-1]) is not AdvisorFrame:
    # Failed manual application already retained its actual prefix and
    # either returned a ready suffix or finished/classified actual P4.
    continue
```

The `continue` above must still enforce F32's actual advancement check. Replace it with this exact guarded advancement before continuing:

```python-statement
if type(selected.stack.frames[-1]) is not AdvisorFrame:
    if selected.checkpoint_id == before.checkpoint_id or selected.stack.cursor <= before.stack.cursor:
        raise AuditIntegrityError("manual failure did not advance its actual selected caller")
    continue
```

### O8.1: Prove the actual failed manual prefix survives a new process

**Append:** `tests/integration/web/composer/test_continuation_provider_capture.py`, after its actual F33 fixture/imports. D1–D3 own the real environment, worker, lease, coordinator, quota and SDK boundary. The only provider replacement below is LiteLLM's external call. The SQL wrapper forwards the actual method and copies the committed SQLite database before the provider body's `finally` resumes; it returns no fabricated receipt.

- [ ] Add this complete helper and test. Its warm branch consumes the actual fixed error feedback and completes the suffix. Its cold branch starts with no old service/custody/recorder/Task, reconciles the actual pending financial owner and reuses the exact measured outer invocation. A repeated manual SDK call is an assertion failure.

```python
import sqlite3
from copy import deepcopy
from pathlib import Path

from tests.helpers.continuation_driver import (
    DeterministicDriverSDK, SDKInvocation, clone_admitted_database, fresh_driver_environment,
    ordinary_driver_script, read_driver_outcome, recover_copied_driver_owner,
    run_driver_attempt, sdk_role, seed_driver_case,
)
from elspeth.web.composer.continuation_provider_outcomes import (
    decode_manual_failure_invocation, decode_provider_outcome,
)
from elspeth.web.sessions.continuation import read_chunk
from elspeth.web.sessions.models import (
    composer_continuation_chunks_table, composer_continuation_effects_table,
)


def copy_actual_running_store(engine, destination: Path) -> None:
    database = engine.url.database
    assert type(database) is str and database != ":memory:"
    with sqlite3.connect(database) as source, sqlite3.connect(destination) as target:
        source.backup(target)


def actual_failed_outcomes(engine):
    effects, chunks = composer_continuation_effects_table, composer_continuation_chunks_table
    with engine.connect() as conn:
        selected = conn.execute(select(effects).join(chunks,
            (chunks.c.session_id == effects.c.session_id) & (chunks.c.operation_id == effects.c.operation_id)
            & (chunks.c.chunk_key == effects.c.result_chunk_key))
            .where(chunks.c.codec == "composer.provider-outcome.v1").order_by(effects.c.effect_key)).all()
        return tuple(decode_provider_outcome(read_chunk(conn, session_id=row.session_id,
            operation_id=row.operation_id, key=row.result_chunk_key).decode("utf-8")) for row in selected)


@pytest.mark.asyncio
async def test_actual_manual_failure_before_finally_cold_reuses_complete_invocation_and_feedback(
    operation_store, tmp_path, monkeypatch,
):
    case = await seed_driver_case(operation_store, tmp_path / "manual-failure-data")
    baseline = fresh_driver_environment(clone_admitted_database(case, tmp_path / "manual-warm.db"), case)
    script = ordinary_driver_script(case)
    original = TimeoutError("controlled external Advisor timeout")
    copied = tmp_path / "manual-cold.db"
    retained = []
    original_retain = baseline.sessions.retain_continuation_provider_outcome
    sdk_prefix = []
    sdk_consumed = {}

    async def sdk(**kwargs):
        if sdk_role(kwargs) != "manual":
            return await script.complete(**kwargs)
        ordinal = script.consumed.get("manual", 0)
        assert ordinal == 0, "A saved failed manual effect must never re-enter the SDK"
        script.consumed["manual"] = 1
        safe = {key: deepcopy(value) for key, value in kwargs.items()
            if key not in ("api_key", "extra_headers", "timeout")}
        script.invocations.append(SDKInvocation("manual", ordinal, canonical_json(safe), None))
        raise original

    async def retain(*, binding, outcome, required_work):
        result = await original_retain(binding=binding, outcome=outcome, required_work=required_work)
        if outcome.manual_invocation_json is not None:
            assert not retained
            retained.append(outcome)
            assert outcome.failure_kind == "provider" and outcome.manual_payload_json is not None
            assert actual_failed_outcomes(baseline.sessions._engine) == (outcome,)
            copy_actual_running_store(baseline.sessions._engine, copied)
            sdk_prefix.extend(script.invocations)
            sdk_consumed.update(script.consumed)
        return result

    try:
        with monkeypatch.context() as patch:
            patch.setattr(litellm, "acompletion", sdk)
            patch.setattr(baseline.sessions, "retain_continuation_provider_outcome", retain)
            await run_driver_attempt(baseline, script, patch)
        assert retained and copied.exists() and script.consumed["manual"] == 1
        warm = read_driver_outcome(baseline, script)
        frozen = decode_manual_failure_invocation(retained[0].manual_invocation_json)
        assert frozen.tool_call_id == "advisor:manual" and frozen.error_class is None
        assert json.loads(frozen.result_canonical)["status"] == "ADVISOR_ERROR"
        cold = fresh_driver_environment(f"sqlite:///{copied}", case)
        try:
            recover_copied_driver_owner(cold)
            cold_sdk = DeterministicDriverSDK(replies=ordinary_driver_script(case).replies,
                consumed=dict(sdk_consumed), invocations=list(sdk_prefix))
            with monkeypatch.context() as patch:
                patch.setattr(litellm, "acompletion", cold_sdk.complete)
                await run_driver_attempt(cold, cold_sdk, patch)
            assert read_driver_outcome(cold, cold_sdk) == warm
            saved = actual_failed_outcomes(cold.sessions._engine)
            assert saved == tuple(retained)
            assert decode_manual_failure_invocation(saved[0].manual_invocation_json) == frozen
            assert cold_sdk.consumed["manual"] == 1
        finally:
            cold.dispose()
    finally:
        baseline.dispose()
```

- [ ] Add this complete actual failing-worker helper and controlled-code test in the same module. The test spies on the actual P4 call and proves the live original exception reaches it unchanged. A new owner reads the persisted failure after the warm owner has joined and released; it must terminate without repeating an SDK call. The helper inspects actual SQL and completed actual Tasks, never a waiter timeout as evidence of closure.

```python
from tests.helpers.continuation_driver import driver_worker, failure_objects
from elspeth.web.sessions.routes._helpers import composer_session_lock_registry
from elspeth.web.sessions.models import composer_async_operations_table


async def finish_actual_failed_driver(env):
    worker = driver_worker(env)
    until = asyncio.get_running_loop().time() + 10
    claim = None
    while claim is None:
        claims = env.authority.claim_segment(limit=1)
        if claims:
            (claim,) = claims
        else:
            assert asyncio.get_running_loop().time() < until
            await asyncio.sleep(0.02)
    key = (claim.session_id, claim.operation_id)
    worker._continuation_claims[key] = claim
    worker._local_cancels[key] = asyncio.Event()
    task = asyncio.create_task(worker._job(claim), name="test-actual-failed-Advisor-driver")
    worker._jobs[key] = task
    try:
        escaping = None
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=30)
        except BaseException as caught:
            escaping = caught
        assert task.done()
        worker._required_coordinators[key].assert_completed()
        for coordinator in worker._recovery_coordinators.get(key, ()):
            coordinator.assert_completed()
        for lease in worker._continuation_adoptions[key]:
            assert lease.closed and not lease._owned_tasks
        parent = env.authority.get(session_id=claim.session_id, operation_id=claim.operation_id)
        assert parent is not None
        lock = await composer_session_lock_registry(env.app).get_lock(str(claim.session_id))
        assert not lock.locked()
        worker._job_finished(key, task)
        worker._stopping = True
        worker.assert_shutdown_complete()
        return escaping, parent
    finally:
        if not task.done():
            task.cancel("test-owned failure cleanup")
            await asyncio.gather(task, return_exceptions=True)
        await worker._process_recovery.join_escalation()


@pytest.mark.asyncio
@pytest.mark.parametrize("rollback", (False, True))
async def test_actual_manual_host_failure_finishes_p4_with_identical_original_then_cold_fails_finitely(
    operation_store, tmp_path, monkeypatch, rollback,
):
    case = await seed_driver_case(operation_store, tmp_path / "manual-host-data")
    database = clone_admitted_database(case, tmp_path / "manual-host.db")
    env = fresh_driver_environment(database, case)
    script = ordinary_driver_script(case)
    original = TypeError("controlled first-party SDK adapter defect")
    actual_p4 = env.composer.persist_continuation_tool_batch
    p4_inputs = []

    async def sdk(**kwargs):
        if sdk_role(kwargs) != "manual":
            return await script.complete(**kwargs)
        assert script.consumed.get("manual", 0) == 0
        script.consumed["manual"] = 1
        safe = {key: deepcopy(value) for key, value in kwargs.items()
            if key not in ("api_key", "extra_headers", "timeout")}
        script.invocations.append(SDKInvocation("manual", 0, canonical_json(safe), None))
        raise original

    async def p4(**kwargs):
        if kwargs["dispatch"].advisor_failure is not None:
            assert kwargs["dispatch"].advisor_failure is original
            p4_inputs.append(kwargs["dispatch"])
        if rollback and kwargs["dispatch"].advisor_failure is not None:
            from sqlalchemy.exc import OperationalError
            actual_insert = env.sessions._insert_chat_message
            database_fault = OperationalError("controlled actual P4 insert", {}, RuntimeError("controlled SQL fault"))
            inserted = []
            def fail_after_insert(*args, **values):
                result = actual_insert(*args, **values)
                if values["role"] == "assistant":
                    inserted.append(result)
                    raise database_fault
                return result
            with monkeypatch.context() as patch:
                patch.setattr(env.sessions, "_insert_chat_message", fail_after_insert)
                result = await actual_p4(**kwargs)
            assert inserted and result.persist is not None and result.persist.failed_turn is not None
            with env.sessions._engine.connect() as conn:
                actual = conn.execute(select(chat_messages_table.c.id).where(chat_messages_table.c.id.in_(inserted))).all()
            assert actual == [], "A rolled-back P4 assistant must never become its receipt"
            return result
        return await actual_p4(**kwargs)

    try:
        with monkeypatch.context() as patch:
            patch.setattr(litellm, "acompletion", sdk)
            patch.setattr(env.composer, "persist_continuation_tool_batch", p4)
            escaping, parent = await finish_actual_failed_driver(env)
        assert any(item is original for item in failure_objects(escaping))
        assert len(p4_inputs) == 1
        saved = actual_failed_outcomes(env.sessions._engine)
        assert len(saved) == 1 and saved[0].failure_kind == "first_party"
        invocation = decode_manual_failure_invocation(saved[0].manual_invocation_json)
        assert invocation.error_class == "TypeError" and invocation.result_canonical is None
        prefix = tuple(script.invocations)
    finally:
        env.dispose()
    if parent.status != "failed":
        cold = fresh_driver_environment(database, case)
        try:
            with monkeypatch.context() as patch:
                patch.setattr(litellm, "acompletion", script.complete)
                _escaping, parent = await finish_actual_failed_driver(cold)
            assert parent.status == "failed"
            assert tuple(script.invocations) == prefix
            assert actual_failed_outcomes(cold.sessions._engine) == saved
        finally:
            cold.dispose()
    assert parent.failure_code is not None
```

`DriverEnvironment`'s actual Composer field is named `composer`; use that existing D1 field above. If its constructor has a source-compatible local alias, keep this one canonical name across D1 and these tests. This is an explicit integration-field check, not permission to manufacture a service.

## O9: Prove canceled P4 drains its actual prefix through all three authority gates

**Append:** the same actual integration module. **Supersede:** B7's synthetic `selected_empty_p4`/`write_empty_p4` helper packet and its three tests (`test_p4_actual_rows_withheld_receipt_and_private_head_share_one_transaction`, `test_p4_readback_rejects_changed_actual_assistant_bytes`, `test_p4_actual_rollback_retains_unavailable_without_fabricating_assistant`) with the source-grounded tests here and O8. Those manually installed response chunks lack the actual physical/ledger proof now correctly required by C3.8c. The real repair/withheld cold-cohort test in D6 remains required for that actual channel.

- [ ] Add this complete parameterized full-driver test. Stop at received-batch admission produces the zero-call prefix; Stop after a real first read produces the one-call prefix; Stop at P4 produces the full prefix. The same run exercises checkpoint, preparation and actual nested row writers. Forged prefix and duplicate selected-head writes refuse without changing any table bytes.

```python
from elspeth.web.composer.continuation import ToolBatchFrame
from elspeth.web.composer.continuation_codec import decode_continuation_snapshot, encode_continuation_snapshot
from elspeth.web.composer.continuation_tool_batch import decode_batch_chunk, encode_batch_chunk
from elspeth.web.composer.continuation_execution import continuation_chunk, merge_continuation_chunks
from elspeth.web.sessions.composer_operations import ComposerOperationCancelledDuringTurn


@pytest.mark.asyncio
@pytest.mark.parametrize("prefix", (0, 1, 2))
async def test_actual_stop_drains_only_the_issued_p4_prefix_through_all_sql_gates(
    operation_store, tmp_path, monkeypatch, prefix,
):
    case = await seed_driver_case(operation_store, tmp_path / "owed-p4-data")
    env = fresh_driver_environment(clone_admitted_database(case, tmp_path / "owed-p4.db"), case)
    script = ordinary_driver_script(case)
    actual_capture = env.authority.commit_received_provider_capture
    actual_transition = env.authority.commit_transition
    actual_application = env.authority.commit_owner_application
    actual_checkpoint = env.authority.commit_owed_continuation_p4_checkpoint
    actual_prepare = env.authority.prepare_owed_continuation_p4
    stopped = []
    checkpoints, preparations, writers = [], [], []
    actual_writer = env.sessions.persist_continuation_compose_turn

    def stop():
        if not stopped:
            receipt = env.authority.request_v2_cancel(session_id=case.session_id, operation_id=case.operation_id,
                actor_user_id="alice", auth_provider_type="local")
            assert receipt is not None and receipt[0].cancel_requested
            stopped.append(receipt)

    def capture(running, **kwargs):
        if prefix == 0 and kwargs["kind"] == "batch_admitted":
            stop()
        return actual_capture(running, **kwargs)

    def transition(running, **kwargs):
        result = actual_transition(running, **kwargs)
        root = decode_continuation_snapshot(result.payload_json)
        if prefix == 1 and type(root.frames[-1]) is ToolBatchFrame:
            value = decode_batch_chunk(continuation_chunk(kwargs["chunks"], root.frames[-1].accumulator))
            if value.phase == "call_ready" and value.next_tool_index == 1:
                stop()
        return result

    def application(running, **kwargs):
        result = actual_application(running, **kwargs)
        root = decode_continuation_snapshot(result.payload_json)
        if prefix == 1 and type(root.frames[-1]) is ToolBatchFrame:
            value = decode_batch_chunk(continuation_chunk(kwargs["chunks"], root.frames[-1].accumulator))
            if value.phase == "call_ready" and value.next_tool_index == 1:
                stop()
        return result

    def checkpoint(running, **kwargs):
        stop()
        following = decode_continuation_snapshot(kwargs["snapshot_json"])
        frame = following.frames[-1]
        value = decode_batch_chunk(continuation_chunk(kwargs["chunks"], frame.accumulator))
        assert value.next_tool_index == prefix and len(value.tool_outcomes) == prefix
        assert value.current_call is None and value.phase == "p4_pending"
        before = capture_tables(env.sessions._engine)
        bad_chunk = encode_batch_chunk(value.model_copy(update={"advisor_calls_used": value.advisor_calls_used+1}))
        bad = following.model_copy(update={"frames": (*following.frames[:-1],
            frame.model_copy(update={"accumulator": bad_chunk.reference}))})
        with pytest.raises(AuditIntegrityError, match="completed prefix|counter"):
            actual_checkpoint(running, **(kwargs | {"snapshot_json": encode_continuation_snapshot(bad),
                "chunks": merge_continuation_chunks(kwargs["chunks"], (bad_chunk,))}))
        assert capture_tables(env.sessions._engine) == before
        result = actual_checkpoint(running, **kwargs)
        committed = capture_tables(env.sessions._engine)
        with pytest.raises(ContinuationCASConflict):
            actual_checkpoint(running, **kwargs)
        assert capture_tables(env.sessions._engine) == committed
        checkpoints.append(result)
        return result

    def prepare(running, **kwargs):
        before = capture_tables(env.sessions._engine)
        selected = env.authority.load_continuation_snapshot(running)
        with pytest.raises(ComposerOperationCancelledDuringTurn):
            env.authority.prepare_tool_action(running, effect_key="new:business:after-stop",
                input_sha256="a"*64, owner="session_sql", snapshot_json=selected.payload_json,
                expected_checkpoint_id=selected.checkpoint_id, expected_cursor=selected.cursor)
        assert capture_tables(env.sessions._engine) == before
        result = actual_prepare(running, **kwargs)
        preparations.append(result)
        return result

    async def writer(**kwargs):
        assert stopped and len(kwargs["redacted_tool_rows"]) == prefix
        result = await actual_writer(**kwargs)
        assert type(result) is RequiredSQLReturned and result.value.assistant_id is not None
        selected = env.authority.load_continuation_snapshot(kwargs["continuation"].running)
        receipt = env.authority.read_continuation_p4_result(kwargs["continuation"].running,
            expected_checkpoint_id=selected.checkpoint_id, expected_cursor=selected.cursor)
        assert str(receipt.assistant_id) == result.value.assistant_id
        assert len(receipt.invocation_ids) == prefix
        from sqlalchemy import update
        with env.sessions._engine.connect() as conn:
            original_row = conn.execute(select(chat_messages_table).where(
                chat_messages_table.c.id == str(receipt.assistant_id))).one()
        head_before = env.authority.load_continuation_snapshot(kwargs["continuation"].running)
        with env.sessions._engine.begin() as conn:
            conn.execute(update(chat_messages_table).where(chat_messages_table.c.id == original_row.id)
                .values(raw_content="An unretained actual P4 assistant original"))
        with pytest.raises(AuditIntegrityError, match="assistant row"):
            env.authority.read_continuation_p4_result(kwargs["continuation"].running,
                expected_checkpoint_id=selected.checkpoint_id, expected_cursor=selected.cursor)
        assert env.authority.load_continuation_snapshot(kwargs["continuation"].running) == head_before
        with env.sessions._engine.begin() as conn:
            conn.execute(update(chat_messages_table).where(chat_messages_table.c.id == original_row.id)
                .values(raw_content=original_row.raw_content))
        writers.append(receipt)
        return result

    try:
        with monkeypatch.context() as patch:
            patch.setattr(litellm, "acompletion", script.complete)
            patch.setattr(env.authority, "commit_received_provider_capture", capture)
            patch.setattr(env.authority, "commit_transition", transition)
            patch.setattr(env.authority, "commit_owner_application", application)
            patch.setattr(env.authority, "commit_owed_continuation_p4_checkpoint", checkpoint)
            patch.setattr(env.authority, "prepare_owed_continuation_p4", prepare)
            patch.setattr(env.sessions, "persist_continuation_compose_turn", writer)
            escaping, parent = await finish_actual_failed_driver(env)
        assert parent.status == "failed" and parent.failure_code == "request_cancelled"
        assert checkpoints and len(preparations) == len(writers) == 1
        assert script.consumed == {"primary": 1}
        assert tuple(item.role for item in script.invocations) == ("primary",)
    finally:
        env.dispose()
```

The existing F33 `capture_tables` reads `metadata.sorted_tables`, so its no-mutation comparison covers chunks, events, checkpoint pointers, quota and business rows together. SQL insertion/readback results above are actual nominal finish-once values. Add the ordinary `RequiredSQLReturned` import from `elspeth.web.required_sql_outcomes` in this test module.

## O10: Retain a real PromptGuard refusal before any quota/SDK admission

**Source:** immutable `f0844ebe` `composer/prompt_guard.py:_persist` and `enforce_pre_dispatch_prompt_guard`, including the actual awaited `prompt_guard_audit` chat envelope; `PromptGuardRefused.to_payload` is the current exact safe public projection. This is a separate no-SDK gate. It must never enter the failed-physical audit path or infer zero use from a missing attempt.

- [ ] Extend the actual guard envelope immediately before its existing `_join_audit(_persist(...))` with this exact v2-only binding. Its already owned required SQL/projection and session fence stay unchanged. V1 envelope bytes stay unchanged.

```python-statement
if provider_custody.continuation_binding is not None:
    actual_binding = provider_custody.continuation_binding
    envelope["check"]["continuation_effect_key"] = actual_binding.effect_key
    envelope["check"]["continuation_input_sha256"] = actual_binding.input_sha256
```

Add `continuation_effect_key:NotRequired[str]` and `continuation_input_sha256:NotRequired[str]` to the actual `_GuardAuditCheck` TypedDict, importing `NotRequired` from `typing`. Their absence remains the v1 shape; the v2 SQL verifier requires both.

- [ ] Add `guard_refused` to the outcome reason Literal and to its two no-attempt reason tuples. Add these fields and exact validator statements to `RetainedContinuationProviderOutcome`. Expand O8's manual no-SDK reason guard to allow only `admission_refused|guard_refused`.

```python-class-members
guard_audit_id: str | None = None
guard_http_status: Literal[422, 503] | None = None
guard_payload_json: str | None = None
```

```python-statement
guard_values = (self.guard_audit_id, self.guard_http_status, self.guard_payload_json)
if self.reason == "guard_refused":
    if any(value is None for value in guard_values) or not self.guard_audit_id:
        raise ValueError("guard refusal lost its actual gate audit/public projection")
elif any(value is not None for value in guard_values):
    raise ValueError("nonguard outcome claimed a guard audit")
```

- [ ] Add the following complete same-connection gate verifier. It accepts only the actual one-row guard audit joined to this exact effect/input and immutable actual committing attempt. The caller does not supply a claim of no SDK dispatch. The no-SDK proof is the actual protected-dispatch refusal owner and its awaited audit.

```python
def require_guard_refusal_on_connection(
    conn: Connection, binding: ContinuationProviderBinding, *, effect: Row[Any], outcome: RetainedContinuationProviderOutcome,
) -> str:
    from elspeth.web.sessions.models import chat_messages_table
    if outcome.reason != "guard_refused" or outcome.attempt_id is not None or effect.provider_attempt_id is not None:
        raise AuditIntegrityError("guard refusal cannot replace an admitted physical attempt")
    rows = conn.execute(select(chat_messages_table).where(
        chat_messages_table.c.session_id == str(binding.running.claim.session_id),
        chat_messages_table.c.role == "audit", chat_messages_table.c.writer_principal == "compose_loop",
        chat_messages_table.c.tool_calls[0]["_kind"].as_string() == "prompt_guard_audit",
        chat_messages_table.c.tool_calls[0]["check"]["audit_id"].as_string() == outcome.guard_audit_id)).all()
    if len(rows) != 1 or type(rows[0].tool_calls) is not list or len(rows[0].tool_calls) != 1:
        raise AuditIntegrityError("guard refusal lacks its exact actual single audit row")
    row, envelope = rows[0], rows[0].tool_calls[0]
    if type(envelope) is not dict or set(envelope) != {"_kind", "check"} or type(envelope["check"]) is not dict:
        raise AuditIntegrityError("guard refusal audit lost its complete owned envelope")
    check = envelope["check"]
    expected_keys = {"schema_version", "audit_id", "operation_id", "operation_epoch", "role", "adapter", "mode",
        "policy_fingerprint", "request_hash", "disposition", "reason", "started_at", "duration_ms",
        "continuation_effect_key", "continuation_input_sha256"}
    if set(check) != expected_keys:
        raise AuditIntegrityError("guard refusal audit omitted or invented binding fields")
    if effect.disposition == "prepared":
        sequence, generation = binding.running.claim.segment_sequence, binding.running.claim.ownership_generation
    elif effect.disposition == "refused" and effect.committer_sequence is not None and effect.committer_generation is not None:
        sequence, generation = effect.committer_sequence, effect.committer_generation
    else:
        raise AuditIntegrityError("guard refusal lost its actual original committing attempt")
    creator = conn.execute(select(attempts).where(attempts.c.session_id == str(binding.running.claim.session_id),
        attempts.c.operation_id == binding.running.claim.operation_id, attempts.c.sequence == sequence,
        attempts.c.generation == generation)).one()
    if (check["schema_version"] != 1 or type(check["schema_version"]) is not int
        or check["audit_id"] != outcome.guard_audit_id
        or (check["operation_id"], check["operation_epoch"]) != (creator.session_operation_id, creator.session_operation_epoch)
        or check["role"] != ("advisor" if binding.family == 1 else "composer")
        or (check["continuation_effect_key"], check["continuation_input_sha256"]) != (binding.effect_key, binding.input_sha256)
        or check["disposition"] not in ("blocked", "unavailable")
        or type(check["duration_ms"]) is not int or check["duration_ms"] < 0
        or row.content != json.dumps(envelope, sort_keys=True)
        or row.raw_content is not None or row.tool_call_id is not None or row.parent_assistant_id is not None):
        raise AuditIntegrityError("guard refusal changed its actual gate/current source")
    blocked = check["disposition"] == "blocked"
    unsupported = check["reason"] == "unsupported_input"
    expected = {"error_type": "prompt_guard_unsupported_input" if unsupported else "prompt_guard_blocked" if blocked else "prompt_guard_unavailable",
        "detail": "The authoring input is outside the supported prompt guard input envelope. The protected dispatch was not sent." if unsupported
            else "The configured prompt guard blocked this authoring request. Review the input before resubmitting." if blocked
            else "The configured prompt guard could not check this authoring request. Contact the deployment operator.",
        "audit_id": outcome.guard_audit_id}
    if (outcome.guard_payload_json != canonical_json(expected)
        or outcome.guard_http_status != (422 if blocked or unsupported else 503)
        or effect.disposition == "refused" and effect.receipt_key != row.id):
        raise AuditIntegrityError("guard refusal changed its actual safe public projection/receipt")
    return row.id
```

Import `Row` from SQLAlchemy, `Any` from typing, and `json` normally in this module. The argument is the actual owned SQL row already read by O5. The complete guard envelope is produced locally and read as owned SQL data; no foreign SDK object is inspected here.

- [ ] In O5 `retain_provider_outcome_on_connection`, extend only its explicit no-attempt reason tuple to include `guard_refused`. After the existing `stopped_before_admission` check, set the actual receipt with this full expression; add the two committer fields below to the existing effect update. In the reader's no-attempt branch, before returning, call the same gate verifier for `guard_refused`. All failed-physical/cancellation-ledger validation remains unchanged.

```python-statement
receipt_id = (require_guard_refusal_on_connection(conn, binding, effect=effect, outcome=outcome)
    if outcome.reason == "guard_refused" else "no-sdk:" + binding.effect_key)
```

```python-expression
committer_sequence=binding.running.claim.segment_sequence if outcome.reason == "guard_refused" else effect.committer_sequence,
committer_generation=binding.running.claim.ownership_generation if outcome.reason == "guard_refused" else effect.committer_generation,
```

```python-statement
if outcome.reason == "guard_refused":
    require_guard_refusal_on_connection(conn, binding, effect=effect, outcome=outcome)
```

- [ ] Extend the common custody refusal argument type to `ChargeableAdmissionRefused|PromptGuardRefused|None`, importing the latter normally. Insert this complete first branch before its existing quota-refusal `else` body; the existing quota branch becomes the final `else`. O8's common freeze of the actual manual invocation still runs after the whole selection, inside the same V operation.

```python-statement
elif type(refusal) is PromptGuardRefused:
    if self._sdk_entered or self.attempt is not None or self.call is not None:
        raise AuditIntegrityError("guard refusal cannot relabel a dispatched/admitted physical call")
    value = RetainedContinuationProviderOutcome(kind="not_dispatched", effect_key=binding.effect_key,
        input_sha256=binding.input_sha256, attempt_id=None, audit_call_json=None, reason="guard_refused",
        guard_audit_id=refusal.audit_id, guard_http_status=refusal.http_status,
        guard_payload_json=canonical_json(refusal.to_payload()))
```

```python
async def retain_continuation_guard_refusal(self, refusal: PromptGuardRefused) -> None:
    if self._continuation_binding is None:
        return
    if self._clock is None or type(refusal) is not PromptGuardRefused:
        raise AuditIntegrityError("guard refusal lost its actual durable custody/stage owner")
    async def retain() -> None:
        await self._retain_continuation_provider_outcome_value(refusal=refusal, request_rejection=None,
            failure_kind=None, original_exception=refusal)
    await self._clock.run("retention", retain)
```

The actual return is also needed by a terminal title/primary gate. Change `_retain_continuation_provider_outcome_value` to `->RetainedContinuationProviderOutcome` and append `return value` after its existing joined-SQL/projection/deferred-cancellation suffix. Change the guard method to this complete body; the quota/ordinary-failure callers deliberately ignore that actual returned nominal value.

```python
async def retain_continuation_guard_refusal(self, refusal: PromptGuardRefused) -> RetainedContinuationProviderOutcome:
    if self._continuation_binding is None or self._clock is None or type(refusal) is not PromptGuardRefused:
        raise AuditIntegrityError("guard refusal lost its actual durable custody/stage owner")
    async def retain() -> RetainedContinuationProviderOutcome:
        return await self._retain_continuation_provider_outcome_value(refusal=refusal, request_rejection=None,
            failure_kind=None, original_exception=refusal)
    return await self._clock.run("retention", retain)
```

- [ ] Replace only the exact `except PromptGuardRefused: raise` arm in both gateway audit bodies and the actual Advisor audit body with this complete arm. Add it before the ordinary timeout/transport catches in the title body and C6 `_call_continuation_advisor`. The current actual audit record remains absent for this gate; the awaited guard row is the distinct real receipt. No later `finally` should manufacture a failed physical call for it.

```python-except-fragment
except PromptGuardRefused as original:
    if provider_custody is not None and provider_custody.continuation_binding is not None:
        await provider_custody.retain_continuation_guard_refusal(original)
    raise
```

C6's actual parameter is `custody`, so its literal arm is:

```python-except-fragment
except PromptGuardRefused as original:
    await custody.retain_continuation_guard_refusal(original)
    raise
```

For the title, primary/text gateway and C6 semantic body, use this finite variant instead of the preceding plain rethrow. The actual generic Advisor body retains the plain rethrow so O8 can finish its real manual prefix first. This applies only to durable custody; ordinary v1 rethrows its original guard exception unchanged.

```python-except-fragment
except PromptGuardRefused as original:
    if provider_custody is not None and provider_custody.continuation_binding is not None:
        saved = await provider_custody.retain_continuation_guard_refusal(original)
        raise ContinuationRetainedProviderFailure(saved) from original
    raise
```

C6's exact variant is:

```python-except-fragment
except PromptGuardRefused as original:
    saved = await custody.retain_continuation_guard_refusal(original)
    raise ContinuationRetainedProviderFailure(saved) from original
```

Import the saved-failure type normally in these actual audit-body modules. No failed audit constructor runs for the guard gate, and the actual `PromptGuardRefused` remains its warm cause.

In O8 `freeze_manual_failure`, the no-SDK original check becomes the complete closed condition below; all actual invocation builders stay unchanged. In its SQL manual verifier and failure caller, replace the literal no-attempt expected class with `"PromptGuardRefused" if outcome.reason == "guard_refused" else "ChargeableAdmissionRefused"` (the caller's local outcome is named `retained`).

```python-statement
if not ((outcome.reason == "admission_refused" and isinstance(original, ChargeableAdmissionRefused))
        or (outcome.reason == "guard_refused" and type(original) is PromptGuardRefused)):
    raise AuditIntegrityError("manual no-SDK failure lost its exact actual gate refusal")
```

Import `PromptGuardRefused` locally alongside `ChargeableAdmissionRefused` in that pure function, avoiding an outcome/guard import cycle at module load. F21's existing generic actual-original catch now consumes the already durable gate receipt; it does not call the quota-refusal method a second time.

- [ ] Add this exact first branch to the saved-failure projector before its generic no-SDK branch. Decode only the strict actual stored payload; its full equality was already checked against the real guard audit. Add `auth.outcome.reason == "guard_refused"` to `complete_optional_continuation_title`'s finite refusal condition, so optional title cannot bypass a protected-authoring refusal.

```python-statement
if outcome.reason == "guard_refused":
    payload, error = parse_json_strict(outcome.guard_payload_json)
    if (error is not None or type(payload) is not dict or outcome.guard_http_status not in (422, 503)
        or type(payload.get("error_type")) is not str):
        raise AuditIntegrityError("saved guard refusal lost its actual safe error projection")
    return _http_error(outcome.guard_http_status, payload | {"request_id": request_id}, payload["error_type"])
```

Import the existing strict JSON parser and `AuditIntegrityError` normally. A cold caller receives the same exact safe error type/status/audit ID; it does not construct `PromptGuardRefused` from saved strings.

- [ ] Replace F17's primary `disposition=="refused"` arm with this exact return. The shared actual failure adapter from O8 accepts only `AdvisorFrame|ComposeFrame`; it restores the current runtime and exact saved audit when present. Wrap only F17's actual gateway invocation as below, preserving the existing post-success observation/capture suffix.

```python-statement
if reconciled.receipt.disposition == "refused":
    return await apply_retained_continuation_provider_failure(service=service,
        operation_authority=operation_authority, selected=selected, binding=binding,
        required_work=required_work, provider_owner=provider_owner, progress=progress)
```

```python-statement
try:
    with (composer_quota_scope(service._require_sessions_service(), running.session_operation_context),
          prompt_guard_user_scope(runtime.bindings.message)):
        await service._provider_gateway._call_llm_with_audit(runtime.live.llm_messages, runtime.live.tools,
            timeout=policy.actions.primary.physical, recorder=runtime.live.recorder,
            provider_custody=custody, credential_state=runtime.live.state)
except ChargeableAdmissionRefused as original:
    await custody.retain_continuation_admission_refusal(original)
    return await apply_retained_continuation_provider_failure(service=service,
        operation_authority=operation_authority, selected=selected, binding=binding,
        required_work=required_work, provider_owner=provider_owner, progress=progress, warm_original=original)
except BaseException as original:
    return await apply_retained_continuation_provider_failure(service=service,
        operation_authority=operation_authority, selected=selected, binding=binding,
        required_work=required_work, provider_owner=provider_owner, progress=progress, warm_original=original)
```

### O10.1: Use the actual configured guard owner and refuse forged saved gate evidence

**Append:** the same F33 integration test module. The external guard client is the existing nominal `_GuardClient` from `test_prompt_guard_admission.py`; it returns an external guard decision while the real runtime performs request preparation, gate enforcement, required audit SQL, explicit outcome retention, manual prefix application, P4 and terminal settlement.

- [ ] Add this complete cold guard test. It copies only after the actual gate/outcome transaction, before unwinding the current caller. The current store remains live and all actual owners close normally; the cold branch opens the copied database with fresh owners and no SDK-call fabrication. Tampering one real saved guard audit must fail the actual reader without changing any other store bytes.

```python
from sqlalchemy import update
from elspeth.contracts.prompt_guard import PromptGuardDecision
from elspeth.web.composer.prompt_guard import PromptGuardMount
from elspeth.web.composer.continuation_provider_outcomes import ContinuationRetainedProviderFailure
from tests.unit.web.composer.test_prompt_guard_admission import _GuardClient


@pytest.mark.asyncio
@pytest.mark.parametrize("disposition,reason,http_status", (
    ("blocked", "policy_blocked", 422), ("unavailable", "service_error", 503),
))
async def test_actual_guard_refusal_has_no_sdk_attempt_and_cold_reuses_exact_gate_audit(
    operation_store, tmp_path, monkeypatch, disposition, reason, http_status,
):
    case = await seed_driver_case(operation_store, tmp_path / "guard-outcome-data")
    database = clone_admitted_database(case, tmp_path / "guard-warm.db")
    env = fresh_driver_environment(database, case)
    script = ordinary_driver_script(case)
    guard = _GuardClient(PromptGuardDecision(disposition, reason))
    mount = PromptGuardMount(guard)
    env.composer._advisor_prompt_guard_mount = mount
    env.composer._advisor_checkpoint._prompt_guard_mount = mount
    copied = tmp_path / "guard-cold.db"
    actual_retain = env.sessions.retain_continuation_provider_outcome
    retained, bindings, prefix = [], [], []
    consumed = {}

    async def retain(*, binding, outcome, required_work):
        result = await actual_retain(binding=binding, outcome=outcome, required_work=required_work)
        if outcome.reason == "guard_refused":
            assert not retained and outcome.attempt_id is None and outcome.audit_call_json is None
            assert outcome.manual_invocation_json is not None
            retained.append(outcome)
            bindings.append(binding)
            copy_actual_running_store(env.sessions._engine, copied)
            prefix.extend(script.invocations)
            consumed.update(script.consumed)
        return result

    try:
        with monkeypatch.context() as patch:
            patch.setattr(litellm, "acompletion", script.complete)
            patch.setattr(env.sessions, "retain_continuation_provider_outcome", retain)
            _escaping, parent = await finish_actual_failed_driver(env)
        assert retained and len(guard.requests) == 1 and "manual" not in script.consumed
        assert parent.status in ("running", "failed")
    finally:
        env.dispose()
    cold = fresh_driver_environment(f"sqlite:///{copied}", case)
    cold.composer._advisor_prompt_guard_mount = mount
    cold.composer._advisor_checkpoint._prompt_guard_mount = mount
    cold_sdk = DeterministicDriverSDK(replies=ordinary_driver_script(case).replies,
        consumed=dict(consumed), invocations=list(prefix))
    try:
        recover_copied_driver_owner(cold)
        with monkeypatch.context() as patch:
            patch.setattr(litellm, "acompletion", cold_sdk.complete)
            _escaping, parent = await finish_actual_failed_driver(cold)
        assert parent.status == "failed" and tuple(cold_sdk.invocations) == tuple(prefix)
        assert len(guard.requests) == 1 and "manual" not in cold_sdk.consumed
        snapshot = cold.authority.get_v2_snapshot(session_id=case.session_id, operation_id=case.operation_id,
            actor_user_id="alice", auth_provider_type="local")
        assert snapshot.error is not None and snapshot.error.http_status == http_status
        assert snapshot.error.body["audit_id"] == retained[0].guard_audit_id
        assert actual_failed_outcomes(cold.sessions._engine) == tuple(retained)
        with cold.sessions._engine.connect() as conn:
            effect = conn.execute(select(composer_continuation_effects_table).where(
                composer_continuation_effects_table.c.effect_key == retained[0].effect_key)).one()
            assert effect.provider_attempt_id is None and effect.receipt_key is not None
            guard_row = conn.execute(select(chat_messages_table).where(chat_messages_table.c.id == effect.receipt_key)).one()
        envelope = deepcopy(guard_row.tool_calls[0])
        envelope["check"]["continuation_input_sha256"] = "f"*64
        with cold.sessions._engine.begin() as conn:
            conn.execute(update(chat_messages_table).where(chat_messages_table.c.id == guard_row.id)
                .values(tool_calls=[envelope], content=json.dumps(envelope, sort_keys=True)))
        before = capture_tables(cold.sessions._engine)
        # Inspect historical proof with the actual saved binding; no
        # current execution authority is minted by this diagnostic read.
        from elspeth.web.sessions.continuation import read_provider_outcome_on_connection
        with cold.sessions._engine.connect() as conn:
            with pytest.raises(AuditIntegrityError, match="gate/current source"):
                read_provider_outcome_on_connection(conn, bindings[0], require_settled=True)
        assert capture_tables(cold.sessions._engine) == before
    finally:
        cold.dispose()
```

`ComposerOperationError` exposes `http_status` and `body` exactly as in the existing error module; `get_v2_snapshot` is the C3.11 authorized DTO facet. The historical diagnostic uses a saved primitive binding only to select and validate retained SQL; it cannot authorize a mutation or reconstruct a live lease.

## O11: Run behavioral RED/GREEN and the repository's ordinary gates

- [ ] After all packets are importable, temporarily route only the B6 prepared-input checkpoint through ordinary strict `commit_transition` instead of the new P4-specific authority. Run the exact first command below. Require native exit 1 from the assertion that a real P4 receipt/writer completed after Stop; import/collection/setup errors are prerequisite failures and do not count as RED. Restore the actual owed-P4 branch, then run the GREEN command and require native exit 0. Separately temporarily omit only `manual_invocation_json` from the saved manual receipt construction: the real current SQL validator/cold equivalence test must refuse it before application. Restore it before the final GREEN run.

```bash
cd "$COMPOSER_WORKTREE" && "$COMPOSER_PYTHON" -m pytest -n 0 tests/integration/web/composer/test_continuation_provider_capture.py -k actual_stop_drains_only >"$COMPOSER_LOG_DIR/physical-p4-red.log" 2>&1
composer_exit=$?
cat "$COMPOSER_LOG_DIR/physical-p4-red.log"
test "$composer_exit" -eq 1
```

```bash
cd "$COMPOSER_WORKTREE" && "$COMPOSER_PYTHON" -m pytest -n 0 tests/unit/web/composer/test_continuation_physical_outcomes.py tests/unit/web/composer/test_continuation_title.py tests/integration/web/composer/test_continuation_provider_capture.py >"$COMPOSER_LOG_DIR/physical-owner-green.log" 2>&1
composer_exit=$?
cat "$COMPOSER_LOG_DIR/physical-owner-green.log"
test "$composer_exit" -eq 0
```

- [ ] Run the exact existing guard/wire/error regression packet after the actual guard-envelope additions. Require native exit 0; retain the real scope and exact four-branch configured/unconfigured guard behavior. Use the ordinary main Q gates for schema/session/AST/type/lint/full/default selection and the serial PostgreSQL testcontainer selection before integration; these focused tests do not replace them.

```bash
cd "$COMPOSER_WORKTREE" && "$COMPOSER_PYTHON" -m pytest -n 0 tests/unit/web/composer/test_prompt_guard_admission.py tests/unit/web/composer/test_prompt_guard_wiring.py tests/unit/web/composer/test_advisor_audit.py tests/unit/web/composer/test_wire_fidelity_matrix.py tests/unit/web/sessions/test_composer_operation_errors.py >"$COMPOSER_LOG_DIR/physical-existing-regressions.log" 2>&1
composer_exit=$?
cat "$COMPOSER_LOG_DIR/physical-existing-regressions.log"
test "$composer_exit" -eq 0
```

- [ ] Review the actual touched source in full and commit only the implementation/test paths named by this packet, using the repository's normal branch-safety check and hooks. No runtime/test/commit was executed while authoring this plan.

```bash
cd "$COMPOSER_WORKTREE" && git status --short
cd "$COMPOSER_WORKTREE" && scripts/branch-safety-check.sh --intent commit --base HEAD
cd "$COMPOSER_WORKTREE" && git add src/elspeth/web/composer/continuation_provider_outcomes.py src/elspeth/web/composer/provider_quota.py src/elspeth/web/composer/provider_gateway.py src/elspeth/web/composer/advisor_checkpoint.py src/elspeth/web/composer/continuation_driver.py src/elspeth/web/composer/tool_batch.py src/elspeth/web/composer/prompt_guard.py src/elspeth/web/sessions/_auto_title.py src/elspeth/web/sessions/continuation.py src/elspeth/web/sessions/composer_operation_errors.py tests/unit/web/composer/test_continuation_physical_outcomes.py tests/unit/web/composer/test_continuation_title.py tests/integration/web/composer/test_continuation_provider_capture.py
cd "$COMPOSER_WORKTREE" && git diff --cached --stat
cd "$COMPOSER_WORKTREE" && git commit -m "feat(composer): retain actual failed outcomes and drain owed P4"
```

## O12: Keep the actual typed pre-SDK credential refusal finite

**Modify:** `composer/continuation_provider_outcomes.py`, `composer/provider_quota.py`, `composer/provider_gateway.py`, O5's explicit no-SDK reason checks, O8's actual manual failure classification and O7's safe error projector. This packet catches only the actual two credential-check calls before guard/quota/SDK. A `CredentialMaterialRefused` from response admission after SDK entry continues through the actual failed-physical audit path unchanged.

- [ ] Add this complete value-free descriptor to the pure outcome module, before `RetainedContinuationProviderOutcome`. It retains the existing safe public payload, including its original detector version, and never retains the finding's candidate/location or request wire. Replace the outcome's reason declaration and add the paired descriptor with the two exact class members below. Extend the three explicit no-attempt membership checks in O5 (the model, SQL retainer and reconciliation reader) to `("admission_refused", "stopped_before_admission", "guard_refused", "credential_refused")`; keep `cancelled_before_dispatch` in its existing actual-attempt branch. In O8's manual metadata check the exact allowed no-SDK reasons are `("admission_refused", "guard_refused", "credential_refused")`.

```python-statement
reason: Literal["admission_refused", "stopped_before_admission", "cancelled_before_dispatch", "guard_refused", "credential_refused"] | None
credential_refusal: RetainedCredentialRefusal | None = None
```

```python
from elspeth.contracts.credential_material import CREDENTIAL_REFUSAL_DETAIL, CredentialCategory


class RetainedCredentialRefusal(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    error_type: Literal["credential_material_rejected"]
    failure_code: Literal["credential_material_rejected"]
    detail: str
    category: CredentialCategory
    surface: Literal["composer_provider_request", "composer_advisor_complete_content"]
    detector_version: str = Field(min_length=1)

    @model_validator(mode="after")
    def require_safe_detail(self) -> Self:
        if self.detail != CREDENTIAL_REFUSAL_DETAIL:
            raise ValueError("credential refusal cannot retain candidate-derived detail")
        return self


def predispatch_failure_class(outcome: RetainedContinuationProviderOutcome) -> str:
    if outcome.reason == "admission_refused":
        return "ChargeableAdmissionRefused"
    if outcome.reason == "guard_refused":
        return "PromptGuardRefused"
    if outcome.reason == "credential_refused":
        return "CredentialMaterialRefused"
    raise AuditIntegrityError("manual failure has no exact supported actual no-SDK refusal")
```

The module's existing `from __future__ import annotations` defers the later outcome annotation. Add this exact check before the outcome validator returns:

```python-statement
if (self.reason == "credential_refused") != (self.credential_refusal is not None):
    raise ValueError("credential refusal lost its exact safe typed descriptor")
```

- [ ] Extend only the common custody refusal argument type with `CredentialMaterialRefused`, importing it normally. Add this complete branch before O10's guard-refusal branch. Its actual owned custody proves no SDK entry or quota admission; the actual SQL transaction still requires its exact selected provider pair and immutable prepared input.

```python-statement
elif type(refusal) is CredentialMaterialRefused:
    if self._sdk_entered or self.attempt is not None or self.call is not None:
        raise AuditIntegrityError("pre-SDK credential refusal cannot relabel an admitted or dispatched call")
    from elspeth.web.composer.continuation_provider_outcomes import RetainedCredentialRefusal
    safe = RetainedCredentialRefusal.model_validate(refusal.to_payload(), strict=True)
    value = RetainedContinuationProviderOutcome(kind="not_dispatched", effect_key=binding.effect_key,
        input_sha256=binding.input_sha256, attempt_id=None, audit_call_json=None,
        reason="credential_refused", credential_refusal=safe)
```

```python
async def retain_continuation_credential_refusal(
    self, refusal: CredentialMaterialRefused,
) -> RetainedContinuationProviderOutcome:
    if self._continuation_binding is None or self._clock is None or type(refusal) is not CredentialMaterialRefused:
        raise AuditIntegrityError("credential refusal lost its actual durable custody/stage owner")
    async def retain() -> RetainedContinuationProviderOutcome:
        return await self._retain_continuation_provider_outcome_value(refusal=refusal,
            request_rejection=None, failure_kind=None, original_exception=refusal)
    return await self._clock.run("retention", retain)
```

- [ ] In `_litellm_acompletion`, move only C5.8's two pure OpenRouter enrichers and exact actual-input digest comparison to immediately before `credential_messages:...=[]`, after complete prompt-evidence correspondence checks. Keep one copy. This proves the credential-refused wire was the exact prepared physical input before taking its no-dispatch disposition. Replace the consecutive `if prepared_advisor_content is not None` / `scan_prepared_advisor_content(...)` / `require_no_credential_material(...)` span with this complete block:

```python-statement
try:
    if prepared_advisor_content is not None:
        credential_messages = scan_prepared_advisor_content(credential_messages,
            prepared_advisor_content, role=prompt_guard_role)
    require_no_credential_material(credential_messages, surface="composer_provider_request")
except CredentialMaterialRefused as original:
    if provider_custody.continuation_binding is None:
        raise
    saved = await provider_custody.retain_continuation_credential_refusal(original)
    if provider_custody.continuation_binding.family == 1:
        # The real manual/automatic Advisor caller owns its suffix. It
        # receives the identical original after this actual SQL receipt.
        raise
    from elspeth.web.composer.continuation_provider_outcomes import ContinuationRetainedProviderFailure
    raise ContinuationRetainedProviderFailure(saved) from original
```

The actual audit body's existing `needs_terminal_audit()` is false: this custody has no admitted attempt and SDK entry never occurred. Its ordinary `finally` therefore constructs no physical call and submits no ledger. The extra retained safe outcome is the actual gate receipt; it is not an invented zero-use provider call.

- [ ] In O8 `freeze_manual_failure`'s no-SDK branch, import `CredentialMaterialRefused` locally and use this complete actual-type condition. The real `finish_plugin_crash` still receives that original object before the one V SQL transaction. In O8's SQL verifier and actual failure caller replace the no-attempt expected-class expression with `predispatch_failure_class(outcome)` / `predispatch_failure_class(retained)` respectively, importing that pure helper. Add `credential_refused` beside `guard_refused` to optional-title's finite refusal condition.

```python-statement
if not ((outcome.reason == "admission_refused" and isinstance(original, ChargeableAdmissionRefused))
        or (outcome.reason == "guard_refused" and type(original) is PromptGuardRefused)
        or (outcome.reason == "credential_refused" and type(original) is CredentialMaterialRefused)):
    raise AuditIntegrityError("manual no-SDK failure lost its exact actual typed refusal")
```

- [ ] Add this exact first branch to `project_retained_provider_failure`. V2 now returns a precise finite 422 for this retained input refusal; it does not recreate the original exception or change ordinary v1 error projection.

```python-statement
if outcome.reason == "credential_refused":
    if outcome.credential_refusal is None:
        raise AuditIntegrityError("saved credential refusal lost its safe typed descriptor")
    return _http_error(422, {**outcome.credential_refusal.model_dump(mode="json"), "request_id": request_id},
        "credential_material_rejected")
```

### O12.1: Prove the actual before-SDK gate and a fresh financial/SQL owner

**Append:** C4b's `tests/unit/web/composer/test_continuation_provider_cancellation.py`. Extend its existing `make_custody(store)` signature to `make_custody(store,*,messages:list[dict[str,str]]|None=None,family:int=0)`; default to its exact existing message when `None`. Replace its original input-hash block with the complete selection below, pass the selected `family` to its existing `prepare_provider_effect` call, and replace only its local clock construction as shown. Preserve every actual admission/current fence/coordinator/preparation line. This fixture exercises the direct physical boundary; it does not claim an unsafe message passed the public request/initializer admission or authored a graph.

```python-statement
selected_messages = ([{"role": "user", "content": "Read the actual plugin contract"}]
    if messages is None else messages)
input_sha256 = continuation_provider_input_hash({"model": "test-model", "messages": selected_messages})
if type(family) is not int or family not in (0, 1, 2, 3):
    raise ValueError("test custody requires an actual physical family")
actions = minimal_continuation_policy().actions
clock = ContinuationActionClock((actions.primary, actions.advisor, actions.primary, actions.title)[family])
```

- [ ] Add this complete actual no-SDK/cold test. It first controls the real detector with safe/unsafe inputs. Only the external SDK is replaced. The original unsafe value never enters a retained chunk, guard audit or token ledger. The next actual generation reads the exact refused effect through fresh current authority and cannot prepare another copy of that ordinal.

```python
@pytest.mark.parametrize("family", [0, 1, 2, 3])
@pytest.mark.asyncio
async def test_actual_credential_refusal_before_sdk_is_retained_without_attempt_and_cold_is_finite(
    operation_store, monkeypatch, family,
):
    import litellm
    from elspeth.contracts.errors import AuditIntegrityError
    from elspeth.web.credential_guard import CredentialMaterialRefused, require_no_credential_material
    from elspeth.web.composer import provider_gateway
    from elspeth.web.composer.continuation_provider_outcomes import ContinuationRetainedProviderFailure
    from elspeth.web.sessions.composer_operation_errors import project_composer_operation_error
    from elspeth.web.sessions.continuation import ContinuationProviderBinding, read_chunk
    from elspeth.web.sessions.models import composer_continuation_chunks_table
    from elspeth.web.sessions.models import metadata
    unsafe = "https://" + "fixture-user" + ":" + "fixture-password" + "@credential.invalid/input"
    messages = [{"role": "user", "content": unsafe}]
    require_no_credential_material([{"role": "user", "content": "Read the actual plugin contract"}],
        surface="composer_provider_request")
    with pytest.raises(CredentialMaterialRefused) as detector:
        require_no_credential_material(messages, surface="composer_provider_request")
    engine, repository, authority, service, sid = operation_store
    def capture():
        with engine.connect() as conn:
            return {table.name: tuple(conn.execute(select(table)).all()) for table in metadata.sorted_tables}
    running, coordinator, binding, custody = make_custody(operation_store, messages=messages, family=family)
    entered = []
    originals = []
    actual_retain = type(custody).retain_continuation_credential_refusal
    async def retain(self, original):
        if self is custody:
            originals.append(original)
        return await actual_retain(self, original)
    async def sdk(**kwargs):
        entered.append(kwargs)
        raise AssertionError("credential-refused actual wire reached external SDK")
    try:
        with monkeypatch.context() as patch:
            patch.setattr(litellm, "acompletion", sdk)
            patch.setattr(type(custody), "retain_continuation_credential_refusal", retain)
            expected = CredentialMaterialRefused if family == 1 else ContinuationRetainedProviderFailure
            with pytest.raises(expected) as caught:
                async with provider_call_scope(custody):
                    await provider_gateway._litellm_acompletion(provider_custody=custody,
                        prompt_guard_mount=None, prompt_guard_mode="debug-when-unconfigured",
                        prompt_guard_role="advisor" if family == 1 else "composer",
                        model="test-model", messages=messages)
        assert not entered and custody.attempt is None and custody.call is None
        assert len(originals) == 1 and originals[0].to_payload() == detector.value.to_payload()
        selected = authority.load_continuation_snapshot(running)
        authorization = authority.load_continuation_provider_outcome(binding,
            expected_checkpoint_id=selected.checkpoint_id, expected_cursor=selected.cursor)
        outcome = authorization.outcome
        if family == 1:
            assert caught.value is originals[0]
        else:
            assert caught.value.outcome == outcome and caught.value.__cause__ is originals[0]
        assert outcome.kind == "not_dispatched" and outcome.reason == "credential_refused"
        assert outcome.attempt_id is None and outcome.audit_call_json is None
        assert outcome.credential_refusal.surface == "composer_provider_request"
        projected = project_composer_operation_error(ContinuationRetainedProviderFailure(outcome),
            request_id="credential-input")
        assert projected.http_status == 422 and projected.failure_code == "credential_material_rejected"
        with engine.connect() as conn:
            assert conn.execute(select(attempts)).all() == []
            assert conn.execute(select(ledger)).all() == []
            retained = conn.execute(select(composer_continuation_chunks_table)).all()
            assert all(unsafe.encode() not in read_chunk(conn, session_id=row.session_id,
                operation_id=row.operation_id, key=row.chunk_key) for row in retained)
        coordinator.assert_completed()
    finally:
        repository.release(running.session_operation_context)
    assert repository.recover_composer_segment(session_id=sid, operation_id=running.claim.operation_id,
        expected_generation=running.claim.ownership_generation, actor_user_id="alice", auth_provider_type="local")
    fresh = start_continuation(repository, authority)
    try:
        selected = authority.load_continuation_snapshot(fresh)
        new_binding = ContinuationProviderBinding(fresh, binding.effect_key, binding.family, binding.ordinal, binding.input_sha256)
        authorization = authority.load_continuation_provider_outcome(new_binding,
            expected_checkpoint_id=selected.checkpoint_id, expected_cursor=selected.cursor)
        assert authorization.outcome == outcome and authorization.ledger_entry_id is None
        before = capture()
        with pytest.raises(AuditIntegrityError, match="unapplied selected effect"):
            authority.prepare_provider_action(fresh, family=binding.family, input_sha256=binding.input_sha256,
                snapshot_json=selected.payload_json, expected_checkpoint_id=selected.checkpoint_id,
                expected_cursor=selected.cursor)
        assert capture() == before
        cold = project_composer_operation_error(ContinuationRetainedProviderFailure(authorization.outcome),
            request_id="credential-input")
        assert cold == projected and not entered
    finally:
        repository.release(fresh.session_operation_context)
```

The actual C4b module already imports table aliases `attempts=quota_provider_attempts_table` and `ledger=token_usage_ledger_table`. The chunk assertion uses C2's checksum-verifying decompression reader. For behavioral RED temporarily omit only the exact typed catch in `_litellm_acompletion`; require native exit 1 because the actual refusal is not a finite retained saved outcome. Run this exact command, then restore the catch before GREEN. Import/collection/setup errors are prerequisite failures, not a behavioral RED.

```bash
cd "$COMPOSER_WORKTREE" && "$COMPOSER_PYTHON" -m pytest -n 0 tests/unit/web/composer/test_continuation_provider_cancellation.py::test_actual_credential_refusal_before_sdk_is_retained_without_attempt_and_cold_is_finite >"$COMPOSER_LOG_DIR/physical-credential-red.log" 2>&1
composer_exit=$?
cat "$COMPOSER_LOG_DIR/physical-credential-red.log"
test "$composer_exit" -eq 1
```

Run this complete GREEN command after restoring the catch. The existing `test_service_pricing_identity.py` tests `test_malformed_completion_credential_metadata_is_refused_before_audit` and `test_diagnostics_malformed_response_credential_metadata_is_refused_before_audit` exercise actual response admission and require its real failed audit without candidate bytes. They preserve that ordinary post-response behavior alongside the new v2 no-SDK proof; they do not themselves claim a v2 SQL recovery test.

```bash
cd "$COMPOSER_WORKTREE" && "$COMPOSER_PYTHON" -m pytest -n 0 tests/unit/web/composer/test_continuation_provider_cancellation.py::test_actual_credential_refusal_before_sdk_is_retained_without_attempt_and_cold_is_finite tests/unit/web/composer/test_continuation_physical_outcomes.py tests/unit/web/composer/test_continuation_title.py tests/unit/web/composer/test_service_pricing_identity.py tests/integration/web/composer/test_continuation_provider_capture.py >"$COMPOSER_LOG_DIR/physical-final-green.log" 2>&1
composer_exit=$?
cat "$COMPOSER_LOG_DIR/physical-final-green.log"
test "$composer_exit" -eq 0
```
