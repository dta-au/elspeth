"""Control actual alias/work observation without creating work for evidence."""

import asyncio
import cProfile
import json
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from functools import partial
from types import FunctionType, MappingProxyType

import pytest

from elspeth.contracts import credential_material
from elspeth.contracts.credential_material import CredentialTraversalPolicy, find_credential_material
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web import async_workers
from elspeth.web import credential_guard as guard
from elspeth.web.composer import continuation_clock as clock
from elspeth.web.composer import continuation_codec as codec
from elspeth.web.composer import diagnostic_evidence as diagnostic
from elspeth.web.composer import provider_context
from elspeth.web.composer.state import SourceSpec
from elspeth.web.continuation_retention_memo import RetentionStageMemo, current_retention_stage_memo, without_retention_stage_memo
from elspeth.web.required_sql_outcomes import RequiredSQLRaised, RequiredSQLReturned
from tests.helpers.continuation_memo import RetentionMemoTrace, UncachedValidationBudget
from tests.unit.web.composer.test_continuation_frames import initial_stack
from tests.unit.web.test_required_sql_finish_once import publication_ticket

# Capture aliases before any trace is installed. Replacing a module attribute
# alone cannot observe these exact current callables.
_DECODE = codec.decode_continuation_snapshot
_VERIFY = codec.verify_private_chunk
_SCAN = guard._retention_credential_finding
_STATE_SCAN = guard._retention_state_options_finding
_DECODE_WORK = codec._decode_continuation_snapshot_uncached
_CHUNK_WORK = codec._verify_private_chunk_canonical
_DETECTOR_WORK = find_credential_material


class _CallableProfileHook:
    def __init__(self):
        self.events = []

    def __call__(self, frame, event, arg):
        if event == "c_call" and arg is sys.getprofile:
            self.events.append("getprofile")
        elif frame.f_code is _DECODE_WORK.__code__:
            self.events.append(event)


def _chunk():
    return codec.encode_private_chunk(codec="composer.messages.v1", canonical='{"message":"ordinary"}')


def _owner():
    return SourceSpec(plugin="csv", options={"path": "input.csv"}, on_success="output", on_validation_failure="discard")


def _diagnostic_inputs():
    payload = diagnostic.DiagnosticPayload("not_returned", "json", None, None)
    return {
        "session_id": "session-café",
        "operation_id": "operation-observer",
        "operation_epoch": 1,
        "tool_call_id": "call-observer",
        "tool_name": "list_sources",
        "status": "success",
        "evidence_id": "evidence-observer",
        "arguments": payload,
        "authority_arguments": None,
        "result": payload,
        "wire_arguments": diagnostic.DiagnosticPayload("not_returned", "wire_text", None, None),
    }


def _metadata_strings(values):
    return tuple(values[name] for name in ("session_id", "operation_id", "tool_call_id", "tool_name", "status", "evidence_id"))


def _provider_fixture(*, longer=False):
    texts = ("AUTHORED SYSTEM CONTEXT CANARY", "AUTHORED USER café☃" + ("!" if longer else ""))
    messages = [{"role": "system", "content": texts[0]}, {"role": "user", "content": texts[1]}]
    expected = json.dumps(
        {
            "schema_version": "composer.provider-context.v1",
            "capture_boundary": "prepared_litellm_messages_and_tools",
            "messages": messages,
            "tools": None,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return messages, texts, expected


def _provider_limit(stage, invocations, attempts, characters):
    return UncachedValidationBudget(stage, invocations, attempts, characters, validator="provider_context_message_text")


@pytest.mark.parametrize("variation", ["ordinary", "extra-capture", "longer-scalar"])
def test_provider_context_uncached_repeated_capture_obeys_finite_control_budgets(monkeypatch, request, record_property, variation):
    _baseline_messages, baseline_texts, _baseline_canonical = _provider_fixture()
    messages, texts, expected = _provider_fixture(longer=variation == "longer-scalar")
    trace = RetentionMemoTrace(monkeypatch, request)
    with clock.retention_stage_memo_scope() as memo:
        first = provider_context.capture_provider_context(messages=messages, tools=None)
        second = provider_context.capture_provider_context(messages=messages, tools=None)
        assert first == second and first is not second and first.disposition == "retained"
        assert first.canonical == expected and not first.reasons
        if variation == "extra-capture":
            assert provider_context.capture_provider_context(messages=messages, tools=None) == first
    invocations = 3 if variation == "extra-capture" else 2
    characters = invocations * sum(len(text) for text in texts)
    totals = trace.uncached_validation_totals()
    assert [asdict(total) for total in totals] == [
        {
            "stage": 1,
            "validator": "provider_context_message_text",
            "invocations": invocations,
            "attempts": invocations * 2,
            "input_characters": characters,
        }
    ]
    for record in trace.uncached_validations:
        assert record.memo is memo and record.validator == "provider_context_message_text" and record.exited
        assert record.thread_id == threading.get_ident() and record.expected_memo_id is None
        assert [attempt.input_characters for attempt in record.attempts] == [len(text) for text in texts]
        for attempt in record.attempts:
            assert [site.qualname for site in attempt.ancestry] == [
                "_text",
                "_message_content",
                "_check_messages",
                "capture_provider_context",
            ]
            assert attempt.caller == attempt.ancestry[0] and attempt.scalar_kind == "str"
            assert attempt.policy_id == id(provider_context._POLICY)
            assert attempt.policy.env_ref_names == attempt.policy.additional_credential_fields == frozenset()
            assert (attempt.policy.max_depth, attempt.policy.max_nodes, attempt.policy.max_width, attempt.policy.max_string_bytes) == (
                64,
                10_000,
                10_000,
                1_048_576,
            )
            assert attempt.policy.detector_version == "1"
    limit = _provider_limit(1, 2, 4, 2 * sum(len(text) for text in baseline_texts))
    if variation == "ordinary":
        trace.finish(stages=1, require_snapshot=False, require_population=False, uncached_validation_limits=(limit,))
        trace.assert_complete_coverage()
    else:
        expected_failure = "attempt budget exceeded" if variation == "extra-capture" else "scalar-input budget exceeded"
        with pytest.raises(AssertionError, match=expected_failure):
            trace.finish(stages=1, require_snapshot=False, require_population=False, uncached_validation_limits=(limit,))
    assert not trace.errors and not trace.orphans and not trace.records and memo.cached_entry_count == 0
    charge = trace.detector_stage_totals()[0]
    assert charge.native_attempts == charge.uncached_validation_attempts == invocations * 2
    assert charge.memo_family_attempts == charge.orphan_attempts == 0
    copied = [asdict(attempt) for record in trace.uncached_validations for attempt in record.attempts]
    for data in copied:
        assert type(data["policy"]["env_ref_names"]) is frozenset and not data["policy"]["env_ref_names"]
        assert type(data["policy"]["additional_credential_fields"]) is frozenset and not data["policy"]["additional_credential_fields"]
        data["policy"]["env_ref_names"] = []
        data["policy"]["additional_credential_fields"] = []
    serialized = json.dumps(copied)
    assert all(text not in serialized for text in texts)
    record_property(
        "uncached_validation_expected",
        json.dumps(
            [
                {
                    "stage": 1,
                    "validator": "provider_context_message_text",
                    "invocations": invocations,
                    "attempts": invocations * 2,
                    "input_characters": characters,
                }
            ]
        ),
    )


@pytest.mark.parametrize(
    "text",
    [pytest.param("", id="empty"), pytest.param("café☃", id="unicode"), pytest.param("x" * 513, id="over512")],
)
def test_provider_context_uncached_text_keeps_its_own_scalar_domain(monkeypatch, request, text):
    messages = [{"role": "user", "content": text}]
    trace = RetentionMemoTrace(monkeypatch, request)
    with clock.retention_stage_memo_scope():
        context = provider_context.capture_provider_context(messages=messages, tools=None)
        assert context.disposition == "retained" and json.loads(context.canonical)["messages"] == messages
    assert len(trace.uncached_validations) == 1
    assert [attempt.input_characters for attempt in trace.uncached_validations[0].attempts] == [len(text)]
    trace.finish(
        stages=1,
        require_snapshot=False,
        require_population=False,
        uncached_validation_limits=(_provider_limit(1, 1, 1, len(text)),),
    )


@pytest.mark.parametrize("credential_index", [0, 1])
def test_provider_context_uncached_credential_refusal_keeps_original_order(monkeypatch, request, credential_index):
    messages, _texts, _expected = _provider_fixture()
    messages[credential_index]["content"] = "Bearer " + "a" * 24
    inspected = [message["content"] for message in messages[: credential_index + 1]]
    trace = RetentionMemoTrace(monkeypatch, request)
    with clock.retention_stage_memo_scope():
        context = provider_context.capture_provider_context(messages=messages, tools=None)
    assert context.disposition == "unavailable" and context.canonical is None and context.reasons == ("recognized_credential",)
    assert [attempt.input_characters for attempt in trace.uncached_validations[0].attempts] == [len(text) for text in inspected]
    assert trace.detector_stage_totals()[0].native_attempts == credential_index + 1
    trace.finish(
        stages=1,
        require_snapshot=False,
        require_population=False,
        uncached_validation_limits=(_provider_limit(1, 1, credential_index + 1, sum(len(text) for text in inspected)),),
    )


@pytest.mark.parametrize("failure", ["unsupported", "bounds"])
def test_provider_context_uncached_copy_refusal_has_a_real_zero_scan_invocation(monkeypatch, request, failure):
    class Hostile:
        def __repr__(self):
            raise AssertionError("copy refusal must not stringify hostile input")

        def __str__(self):
            raise AssertionError("copy refusal must not stringify hostile input")

    bad = Hostile() if failure == "unsupported" else "x" * 1_048_577
    messages = [{"role": "system", "content": "Earlier safe content"}, {"role": "user", "content": bad}]
    trace = RetentionMemoTrace(monkeypatch, request)
    with clock.retention_stage_memo_scope():
        context = provider_context.capture_provider_context(messages=messages, tools=None)
    assert context.disposition == "unavailable" and context.canonical is None and context.reasons == (failure,)
    assert len(trace.uncached_validations) == 1 and trace.uncached_validations[0].exited
    assert not trace.uncached_validations[0].attempts and trace.detector_stage_totals()[0].native_attempts == 0
    trace.finish(
        stages=1,
        require_snapshot=False,
        require_population=False,
        uncached_validation_limits=(_provider_limit(1, 1, 0, 0),),
    )


@pytest.mark.parametrize(
    "mutation",
    [
        "alias",
        "direct-text",
        "detector-wrapper",
        "masked-detector",
        "text-wrapper",
        "distant-capture",
        "memo-wrapper",
        "block-text",
        "policy-identity",
        "policy-bounds",
        "policy-collections",
        "version",
    ],
)
def test_provider_context_uncached_attribution_refuses_other_paths_policy_and_wrappers(monkeypatch, request, mutation):
    messages = [{"role": "user", "content": "Ordinary context content"}]
    original_text = provider_context._text
    if mutation.startswith("policy-"):
        changes = {}
        if mutation == "policy-bounds":
            changes["max_nodes"] = 9999
        elif mutation == "policy-collections":
            changes["env_ref_names"] = frozenset({"UNEXPECTED_REFERENCE"})
            changes["additional_credential_fields"] = frozenset({"unexpected_field"})
        monkeypatch.setattr(provider_context, "_POLICY", replace(provider_context._POLICY, **changes))
    elif mutation == "version":
        monkeypatch.setattr(credential_material, "CREDENTIAL_DETECTOR_VERSION", "changed-version")
    elif mutation in ("detector-wrapper", "masked-detector"):

        def wrapped_detector(value, policy):
            if mutation == "masked-detector":
                with without_retention_stage_memo():
                    return _DETECTOR_WORK(value, policy)
            return _DETECTOR_WORK(value, policy)

        monkeypatch.setattr(provider_context, "find_credential_material", wrapped_detector)
    elif mutation in ("text-wrapper", "memo-wrapper"):

        def wrapped_text(value):
            if mutation == "memo-wrapper":
                _SCAN(value, policy=provider_context._POLICY, surface="observer-control", scanner_role="generic")
                return value
            return original_text(value)

        monkeypatch.setattr(provider_context, "_text", wrapped_text)
    elif mutation == "distant-capture":

        def wrong_content(value):
            return original_text(value)

        monkeypatch.setattr(provider_context, "_message_content", wrong_content)
    elif mutation == "block-text":
        messages[0]["content"] = [{"type": "text", "text": "Ordinary context content"}]
    trace = RetentionMemoTrace(monkeypatch, request)
    with clock.retention_stage_memo_scope():
        if mutation == "alias":
            assert _DETECTOR_WORK("ordinary", provider_context._POLICY) is None
        elif mutation == "direct-text":
            assert original_text("ordinary") == "ordinary"
        else:
            assert provider_context.capture_provider_context(messages=messages, tools=None).disposition == "retained"
    assert sum(len(record.attempts) for record in trace.uncached_validations) == 0
    if mutation == "masked-detector":
        assert trace.errors == ["uncached validation lost its actual stage carrier"] and not trace.orphans
    else:
        assert len(trace.orphans) == trace.detector_stage_totals()[0].native_attempts == 1
    with pytest.raises(
        AssertionError,
        match="uncached validation lost its actual stage carrier" if mutation == "masked-detector" else "unattributed actual memo work",
    ):
        trace.finish(stages=1, require_snapshot=False, require_population=False)


@pytest.mark.asyncio
@pytest.mark.parametrize("carrier", ["correct", "missing", "different", "unobserved"])
async def test_provider_context_uncached_real_worker_binds_actual_stage_and_submission(monkeypatch, request, carrier):
    messages, texts, expected = _provider_fixture()
    ordinary_scope = clock.retention_stage_memo_scope
    if carrier == "missing":
        monkeypatch.setattr(async_workers, "prepare_retention_sql_callable", partial)
    trace = RetentionMemoTrace(monkeypatch, request)
    ticket = publication_ticket()

    def work():
        if carrier in ("different", "unobserved"):
            scope = clock.retention_stage_memo_scope if carrier == "different" else ordinary_scope
            with scope():
                return provider_context.capture_provider_context(messages=messages, tools=None)
        return provider_context.capture_provider_context(messages=messages, tools=None)

    with ThreadPoolExecutor(max_workers=1) as pool:
        monkeypatch.setattr(async_workers, "_SHARED_EXECUTOR", pool)
        with clock.retention_stage_memo_scope() as memo:
            outcome = await async_workers.run_required_sql_finish_once(ticket, work)
            assert isinstance(outcome, RequiredSQLReturned) and outcome.value.canonical == expected
    assert ticket.complete and async_workers.outstanding_admissions() == 0 and not memo.active
    if carrier == "correct":
        record = trace.uncached_validations[0]
        assert record.memo is memo and record.thread_id != threading.get_ident() and record.expected_memo_id == id(memo)
        trace.finish(
            stages=1,
            require_snapshot=False,
            require_population=False,
            uncached_validation_limits=(_provider_limit(1, 1, 2, sum(len(text) for text in texts)),),
        )
    else:
        assert not trace.uncached_validations
        expected_failure = (
            "target work lacked its actual stage carrier"
            if carrier == "missing"
            else "target work carried an unobserved stage"
            if carrier == "unobserved"
            else "uncached validation disagreed with its submitted stage carrier"
        )
        with pytest.raises(AssertionError, match=expected_failure):
            trace.finish(stages=2 if carrier == "different" else 1, require_snapshot=False, require_population=False)


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["correct", "missing-budget", "extra-invocation", "different", "missing", "unobserved"])
async def test_provider_context_zero_scan_real_worker_requires_carrier_and_invocation_budget(monkeypatch, request, case):
    class Hostile:
        def __repr__(self):
            raise AssertionError("copy refusal must not stringify hostile input")

        def __str__(self):
            raise AssertionError("copy refusal must not stringify hostile input")

    messages = [{"role": "system", "content": "Earlier safe content"}, {"role": "user", "content": Hostile()}]
    ordinary_scope = clock.retention_stage_memo_scope
    if case == "missing":
        monkeypatch.setattr(async_workers, "prepare_retention_sql_callable", partial)
    trace = RetentionMemoTrace(monkeypatch, request)
    ticket = publication_ticket()
    worker_carriers = []

    def capture():
        worker_carriers.append((threading.get_ident(), current_retention_stage_memo()))
        return tuple(
            provider_context.capture_provider_context(messages=messages, tools=None) for _ in range(2 if case == "extra-invocation" else 1)
        )

    def work():
        if case in ("different", "unobserved"):
            scope = clock.retention_stage_memo_scope if case == "different" else ordinary_scope
            with scope():
                return capture()
        return capture()

    with ThreadPoolExecutor(max_workers=1) as pool:
        monkeypatch.setattr(async_workers, "_SHARED_EXECUTOR", pool)
        with clock.retention_stage_memo_scope() as memo:
            outcome = await async_workers.run_required_sql_finish_once(ticket, work)
            assert isinstance(outcome, RequiredSQLReturned) and not outcome.deferred_cancellations
            assert len(outcome.value) == (2 if case == "extra-invocation" else 1)
            for context in outcome.value:
                assert context.disposition == "unavailable" and context.canonical is None and context.reasons == ("unsupported",)
    assert ticket.complete and async_workers.outstanding_admissions() == 0 and not memo.active
    assert len(worker_carriers) == 1 and worker_carriers[0][0] != threading.get_ident()
    worker_memo = worker_carriers[0][1]
    if case == "missing":
        assert worker_memo is None
    else:
        assert worker_memo is not None and not worker_memo.active and worker_memo.cached_entry_count == 0
        if case in ("different", "unobserved"):
            assert worker_memo is not memo
            assert (worker_memo in trace.memos) is (case == "different")
        else:
            assert worker_memo is memo
    assert not trace.records and not trace.orphans and not trace.excluded
    assert all(
        total.native_attempts == total.memo_family_attempts == total.uncached_validation_attempts == total.orphan_attempts == 0
        for total in trace.detector_stage_totals()
    )
    if case in ("correct", "missing-budget", "extra-invocation"):
        count = 2 if case == "extra-invocation" else 1
        assert [asdict(total) for total in trace.uncached_validation_totals()] == [
            {"stage": 1, "validator": "provider_context_message_text", "invocations": count, "attempts": 0, "input_characters": 0}
        ]
        for record in trace.uncached_validations:
            assert record.memo is memo and record.exited and not record.attempts
            assert record.thread_id == worker_carriers[0][0] and record.expected_memo_id == id(memo)
        limits = () if case == "missing-budget" else (_provider_limit(1, 1, 0, 0),)
        if case == "correct":
            trace.finish(stages=1, require_snapshot=False, require_population=False, uncached_validation_limits=limits)
            trace.assert_complete_coverage()
        else:
            failure = "budget is required" if case == "missing-budget" else "invocation budget exceeded"
            with pytest.raises(AssertionError, match=failure):
                trace.finish(stages=1, require_snapshot=False, require_population=False, uncached_validation_limits=limits)
        assert not trace.errors
    else:
        assert not trace.uncached_validations
        # A zero-detector path still has an invocation-custody obligation.
        # Do not supply a cap for the wrong stage or invent detector work.
        with pytest.raises(AssertionError, match=r"stage carrier|unobserved stage"):
            trace.finish(stages=2 if case == "different" else 1, require_snapshot=False, require_population=False)
        assert trace.errors


@pytest.mark.parametrize("budget", ["valid", "missing-category", "duplicate", "wrong-label", "separate-stages"])
def test_provider_context_and_constructor_require_distinct_charged_category_limits(monkeypatch, request, record_property, budget):
    values = _diagnostic_inputs()
    messages, texts, _expected = _provider_fixture()
    metadata_characters = sum(len(value) for value in _metadata_strings(values))
    context_characters = sum(len(text) for text in texts)
    trace = RetentionMemoTrace(monkeypatch, request)
    with clock.retention_stage_memo_scope():
        diagnostic.ComposerDiagnosticEvidence(**values)
        provider_context.capture_provider_context(messages=messages, tools=None)
    rows = [asdict(total) for total in trace.uncached_validation_totals()]
    expected_rows = [
        {
            "stage": 1,
            "validator": "diagnostic_correlation_metadata",
            "invocations": 1,
            "attempts": 6,
            "input_characters": metadata_characters,
        },
        {
            "stage": 1,
            "validator": "provider_context_message_text",
            "invocations": 1,
            "attempts": 2,
            "input_characters": context_characters,
        },
    ]
    assert rows == expected_rows and trace.detector_stage_totals()[0].native_attempts == 8
    limits = (UncachedValidationBudget(1, 1, 6, metadata_characters), _provider_limit(1, 1, 2, context_characters))
    if budget == "separate-stages":
        with clock.retention_stage_memo_scope():
            provider_context.capture_provider_context(messages=messages, tools=None)
        expected_rows.append(
            {
                "stage": 2,
                "validator": "provider_context_message_text",
                "invocations": 1,
                "attempts": 2,
                "input_characters": context_characters,
            }
        )
        trace.finish(
            stages=2,
            require_snapshot=False,
            require_population=False,
            uncached_validation_limits=(*limits, _provider_limit(2, 1, 2, context_characters)),
        )
    elif budget == "valid":
        trace.finish(stages=1, require_snapshot=False, require_population=False, uncached_validation_limits=limits)
    else:
        if budget == "missing-category":
            limits = limits[:1]
        elif budget == "duplicate":
            limits = (*limits, limits[1])
        else:
            limits = (limits[0], replace(limits[1], validator="unrecognized"))
        message = (
            "budget is required"
            if budget == "missing-category"
            else "duplicate"
            if budget == "duplicate"
            else "lost its stage or validator"
        )
        with pytest.raises(AssertionError, match=message):
            trace.finish(stages=1, require_snapshot=False, require_population=False, uncached_validation_limits=limits)
    record_property("uncached_validation_expected", json.dumps(expected_rows))


@pytest.mark.parametrize(
    "marker_type",
    [pytest.param(RuntimeError, id="error"), pytest.param(KeyboardInterrupt, id="interrupt"), pytest.param(SystemExit, id="exit")],
)
def test_provider_context_uncached_observation_preserves_original_exception(monkeypatch, request, marker_type):
    marker = marker_type("original controlled capture exception")

    def interrupted_copy(*_args, **_kwargs):
        raise marker

    monkeypatch.setattr(provider_context, "_copy_json", interrupted_copy)
    messages, _texts, _expected = _provider_fixture()
    trace = RetentionMemoTrace(monkeypatch, request)
    with clock.retention_stage_memo_scope(), pytest.raises(marker_type) as caught:
        provider_context.capture_provider_context(messages=messages, tools=None)
    assert caught.value is marker
    assert len(trace.uncached_validations) == 1 and trace.uncached_validations[0].exited
    assert not trace.uncached_validations[0].attempts and trace.detector_stage_totals()[0].native_attempts == 0
    trace.finish(
        stages=1,
        require_snapshot=False,
        require_population=False,
        uncached_validation_limits=(_provider_limit(1, 1, 0, 0),),
    )


@pytest.mark.parametrize("extra_constructor", [False, True])
def test_uncached_metadata_constructors_charge_real_attempts_and_refuse_a_third_constructor(
    monkeypatch, request, record_property, extra_constructor
):
    values = _diagnostic_inputs()
    characters = sum(len(value) for value in _metadata_strings(values))
    trace = RetentionMemoTrace(monkeypatch, request)
    with clock.retention_stage_memo_scope() as memo:
        first = diagnostic.ComposerDiagnosticEvidence(**values)
        second = diagnostic.ComposerDiagnosticEvidence(**values)
        assert first == second and first is not second
        assert len(trace.uncached_validations) == 2
        assert [len(record.attempts) for record in trace.uncached_validations] == [6, 6]
        assert trace.detector_stage_totals()[0].native_attempts == 12
        if extra_constructor:
            assert diagnostic.ComposerDiagnosticEvidence(**values) == first
    expected_invocations = 3 if extra_constructor else 2
    total = trace.uncached_validation_totals()[0]
    assert total.stage == 1 and total.validator == "diagnostic_correlation_metadata"
    assert (total.invocations, total.attempts, total.input_characters) == (
        expected_invocations,
        expected_invocations * 6,
        expected_invocations * characters,
    )
    for record in trace.uncached_validations:
        assert record.memo is memo and record.exited and record.thread_id == threading.get_ident()
        assert [attempt.input_characters for attempt in record.attempts] == [len(value) for value in _metadata_strings(values)]
        for attempt in record.attempts:
            assert attempt.caller.qualname == diagnostic.ComposerDiagnosticEvidence.__post_init__.__qualname__
            assert attempt.caller.filename == diagnostic.ComposerDiagnosticEvidence.__post_init__.__code__.co_filename
            assert attempt.policy_id == id(diagnostic._POLICY)
            assert attempt.policy.env_ref_names == attempt.policy.additional_credential_fields == frozenset()
            assert (attempt.policy.max_depth, attempt.policy.max_nodes, attempt.policy.max_width, attempt.policy.max_string_bytes) == (
                64,
                10_000,
                10_000,
                1_048_576,
            )
            assert attempt.policy.detector_version == "1" and attempt.scalar_kind == "str"
    limit = UncachedValidationBudget(1, max_invocations=2, max_attempts=12, max_input_characters=2 * characters)
    if extra_constructor:
        with pytest.raises(AssertionError, match="uncached validation attempt budget exceeded"):
            trace.finish(stages=1, require_snapshot=False, require_population=False, uncached_validation_limits=(limit,))
    else:
        trace.finish(stages=1, require_snapshot=False, require_population=False, uncached_validation_limits=(limit,))
        trace.assert_complete_coverage()
    assert not trace.errors and not trace.orphans and not trace.records and memo.cached_entry_count == 0
    assert asdict(trace.detector_stage_totals()[0]) == {
        "stage": 1,
        "native_attempts": expected_invocations * 6,
        "memo_family_attempts": 0,
        "uncached_validation_attempts": expected_invocations * 6,
        "orphan_attempts": 0,
    }
    serialized_attempts = [asdict(attempt) for record in trace.uncached_validations for attempt in record.attempts]
    for attempt_data in serialized_attempts:
        policy_data = attempt_data["policy"]
        assert type(policy_data["env_ref_names"]) is frozenset and not policy_data["env_ref_names"]
        assert type(policy_data["additional_credential_fields"]) is frozenset and not policy_data["additional_credential_fields"]
        policy_data["env_ref_names"] = []
        policy_data["additional_credential_fields"] = []
    serialized = json.dumps(serialized_attempts)
    assert all(value not in serialized for value in _metadata_strings(values))
    record_property(
        "uncached_validation_expected",
        json.dumps(
            {
                "stage": 1,
                "validator": "diagnostic_correlation_metadata",
                "invocations": expected_invocations,
                "attempts": expected_invocations * 6,
                "input_characters": expected_invocations * characters,
            }
        ),
    )


@pytest.mark.parametrize("field_index", [0, 5])
@pytest.mark.parametrize("failure", ["credential", "empty", "foreign"])
def test_uncached_metadata_refusal_retains_real_field_order_and_short_circuit(monkeypatch, request, field_index, failure):
    values = _diagnostic_inputs()
    fields = ("session_id", "operation_id", "tool_call_id", "tool_name", "status", "evidence_id")
    values[fields[field_index]] = "Bearer " + "a" * 24 if failure == "credential" else "" if failure == "empty" else 17
    inspected = _metadata_strings(values)[: field_index + (failure == "credential")]
    expected_attempts, characters = len(inspected), sum(len(value) for value in inspected)
    trace = RetentionMemoTrace(monkeypatch, request)
    with clock.retention_stage_memo_scope(), pytest.raises(AuditIntegrityError, match="Private evidence correlation metadata is invalid"):
        diagnostic.ComposerDiagnosticEvidence(**values)
    assert len(trace.uncached_validations) == 1 and trace.uncached_validations[0].exited
    assert [attempt.input_characters for attempt in trace.uncached_validations[0].attempts] == [len(value) for value in inspected]
    assert trace.detector_stage_totals()[0].native_attempts == expected_attempts
    trace.finish(
        stages=1,
        require_snapshot=False,
        require_population=False,
        uncached_validation_limits=(UncachedValidationBudget(1, 1, expected_attempts, characters),),
    )


@pytest.mark.parametrize("mutation", ["none", "hash", "correlation", "schema"])
def test_uncached_metadata_reconstruction_preserves_real_envelope_checks(monkeypatch, request, mutation):
    original = diagnostic.ComposerDiagnosticEvidence(**_diagnostic_inputs())
    envelope = original.to_envelope()
    if mutation == "hash":
        envelope["evidence_sha256"] = "0" * 64
    elif mutation == "correlation":
        envelope["operation_id"] = "another-operation"
    elif mutation == "schema":
        envelope["evidence"]["schema_version"] = "unknown-schema"
    trace = RetentionMemoTrace(monkeypatch, request)
    with clock.retention_stage_memo_scope():
        assert diagnostic.ComposerDiagnosticEvidence.from_dict(original.to_dict()) == original
        if mutation == "none":
            assert diagnostic.ComposerDiagnosticEvidence.from_envelope(envelope, expected_session_id=original.session_id) == original
        else:
            with pytest.raises(
                AuditIntegrityError, match="schema or detector version" if mutation == "schema" else "integrity binding mismatch"
            ):
                diagnostic.ComposerDiagnosticEvidence.from_envelope(envelope, expected_session_id=original.session_id)
    expected_attempts = 6 if mutation == "schema" else 12
    characters = sum(len(value) for value in _metadata_strings(_diagnostic_inputs())) * (expected_attempts // 6)
    assert [len(record.attempts) for record in trace.uncached_validations] == ([6, 0] if mutation == "schema" else [6, 6])
    trace.finish(
        stages=1,
        require_snapshot=False,
        require_population=False,
        uncached_validation_limits=(UncachedValidationBudget(1, 2, expected_attempts, characters),),
    )


def test_uncached_metadata_audit_cohort_revalidation_remains_live_and_refuses_a_dropped_draft(monkeypatch, request):
    from elspeth.web.composer.continuation_title_values import capture_title_provider_result
    from elspeth.web.composer.provider_audit_source import provider_attempt_audit_drafts
    from tests.unit.web.composer.test_continuation_title_values import title_result_inputs

    values = title_result_inputs()
    values["audit_call"] = replace(values["audit_call"], private_diagnostics=(), private_failure=None)
    original = provider_attempt_audit_drafts(values["audit_call"], values["context"])
    values["audit_drafts"] = original[:-1]
    metadata = original[-1].tool_calls[0]["evidence"]
    characters = sum(len(value) for value in _metadata_strings(metadata))
    trace = RetentionMemoTrace(monkeypatch, request)
    with clock.retention_stage_memo_scope():
        assert provider_attempt_audit_drafts(values["audit_call"], values["context"]) == original
        assert provider_attempt_audit_drafts(values["audit_call"], values["context"]) == original
        with pytest.raises(AuditIntegrityError, match="original complete audit draft cohort"):
            capture_title_provider_result(**values)
    assert [len(record.attempts) for record in trace.uncached_validations] == [6, 6, 6]
    trace.finish(
        stages=1,
        require_snapshot=False,
        require_population=False,
        uncached_validation_limits=(UncachedValidationBudget(1, 3, 18, 3 * characters),),
    )


@pytest.mark.parametrize(
    "mutation", ["unrelated-alias", "other-diagnostic-validator", "policy-identity", "policy-bounds", "caller", "memo-wrapped-caller"]
)
def test_uncached_metadata_attribution_refuses_unrelated_alias_policy_and_caller(monkeypatch, request, mutation):
    values = _diagnostic_inputs()
    if mutation in ("policy-identity", "policy-bounds"):
        monkeypatch.setattr(diagnostic, "_POLICY", replace(diagnostic._POLICY, max_nodes=1 if mutation == "policy-bounds" else 10_000))
    elif mutation in ("caller", "memo-wrapped-caller"):

        def unrelated(value, policy):
            if mutation == "memo-wrapped-caller":
                return _SCAN(value, policy=policy, surface="observer-control", scanner_role="generic")
            return _DETECTOR_WORK(value, policy)

        monkeypatch.setattr(diagnostic, "find_credential_material", unrelated)
    trace = RetentionMemoTrace(monkeypatch, request)
    with clock.retention_stage_memo_scope():
        if mutation == "unrelated-alias":
            assert _DETECTOR_WORK("ordinary", diagnostic._POLICY) is None
        elif mutation == "other-diagnostic-validator":
            assert diagnostic._project("ordinary", tool_name="list_sources", additional_fields=(), reasons=set()) == "ordinary"
        else:
            diagnostic.ComposerDiagnosticEvidence(**values)
    expected = 1 if mutation in ("unrelated-alias", "other-diagnostic-validator") else 6
    assert len(trace.orphans) == trace.detector_stage_totals()[0].native_attempts == expected
    assert sum(len(record.attempts) for record in trace.uncached_validations) == 0
    with pytest.raises(AssertionError, match="unattributed actual memo work"):
        trace.finish(stages=1, require_snapshot=False, require_population=False)


@pytest.mark.asyncio
@pytest.mark.parametrize("carrier", ["correct", "missing", "different"])
async def test_uncached_metadata_real_worker_requires_the_original_actual_stage_carrier(monkeypatch, request, carrier):
    values = _diagnostic_inputs()
    if carrier == "missing":
        monkeypatch.setattr(async_workers, "prepare_retention_sql_callable", partial)
    trace = RetentionMemoTrace(monkeypatch, request)
    ticket = publication_ticket()

    def work():
        if carrier == "different":
            with clock.retention_stage_memo_scope():
                return diagnostic.ComposerDiagnosticEvidence(**values)
        return diagnostic.ComposerDiagnosticEvidence(**values)

    with ThreadPoolExecutor(max_workers=1) as pool:
        monkeypatch.setattr(async_workers, "_SHARED_EXECUTOR", pool)
        with clock.retention_stage_memo_scope() as memo:
            result = await async_workers.run_required_sql_finish_once(ticket, work)
            assert isinstance(result, RequiredSQLReturned)
    assert ticket.complete and async_workers.outstanding_admissions() == 0 and not memo.active
    if carrier == "correct":
        record = trace.uncached_validations[0]
        assert record.memo is memo and record.thread_id != threading.get_ident() and len(record.attempts) == 6
        trace.finish(
            stages=1,
            require_snapshot=False,
            require_population=False,
            uncached_validation_limits=(UncachedValidationBudget(1, 1, 6, sum(len(value) for value in _metadata_strings(values))),),
        )
    else:
        assert not trace.uncached_validations
        with pytest.raises(
            AssertionError,
            match=(
                "target work lacked its actual stage carrier"
                if carrier == "missing"
                else "uncached validation disagreed with its submitted stage carrier"
            ),
        ):
            trace.finish(stages=1 if carrier == "missing" else 2, require_snapshot=False, require_population=False)


@pytest.mark.asyncio
@pytest.mark.parametrize("carrier", ["correct", "missing", "different", "unobserved"])
async def test_uncached_metadata_zero_scan_real_worker_requires_original_entry_carrier(monkeypatch, request, carrier):
    values = _diagnostic_inputs()
    values["session_id"] = ""
    ordinary_scope = clock.retention_stage_memo_scope
    if carrier == "missing":
        monkeypatch.setattr(async_workers, "prepare_retention_sql_callable", partial)
    trace = RetentionMemoTrace(monkeypatch, request)
    ticket = publication_ticket()
    worker_carriers = []

    def construct():
        worker_carriers.append((threading.get_ident(), current_retention_stage_memo()))
        return diagnostic.ComposerDiagnosticEvidence(**values)

    def work():
        if carrier in ("different", "unobserved"):
            scope = clock.retention_stage_memo_scope if carrier == "different" else ordinary_scope
            with scope():
                return construct()
        return construct()

    with ThreadPoolExecutor(max_workers=1) as pool:
        monkeypatch.setattr(async_workers, "_SHARED_EXECUTOR", pool)
        with clock.retention_stage_memo_scope() as memo:
            outcome = await async_workers.run_required_sql_finish_once(ticket, work)
            assert isinstance(outcome, RequiredSQLRaised) and type(outcome.error) is AuditIntegrityError
            assert outcome.error.args == ("Private evidence correlation metadata is invalid",)
            assert not outcome.deferred_cancellations
    assert ticket.complete and async_workers.outstanding_admissions() == 0 and not memo.active
    assert len(worker_carriers) == 1 and worker_carriers[0][0] != threading.get_ident()
    worker_memo = worker_carriers[0][1]
    if carrier == "missing":
        assert worker_memo is None
    else:
        assert worker_memo is not None and not worker_memo.active and worker_memo.cached_entry_count == 0
        if carrier in ("different", "unobserved"):
            assert worker_memo is not memo
            assert (worker_memo in trace.memos) is (carrier == "different")
        else:
            assert worker_memo is memo
    assert not trace.records and not trace.orphans and not trace.excluded
    assert all(
        total.native_attempts == total.memo_family_attempts == total.uncached_validation_attempts == total.orphan_attempts == 0
        for total in trace.detector_stage_totals()
    )
    if carrier == "correct":
        assert len(trace.uncached_validations) == 1
        record = trace.uncached_validations[0]
        assert record.memo is memo and record.exited and not record.attempts
        assert record.thread_id == worker_carriers[0][0] and record.expected_memo_id == id(memo)
        assert [asdict(total) for total in trace.uncached_validation_totals()] == [
            {"stage": 1, "validator": "diagnostic_correlation_metadata", "invocations": 1, "attempts": 0, "input_characters": 0}
        ]
        trace.finish(
            stages=1,
            require_snapshot=False,
            require_population=False,
            uncached_validation_limits=(UncachedValidationBudget(1, 1, 0, 0),),
        )
        trace.assert_complete_coverage()
        assert not trace.errors
    else:
        assert not trace.uncached_validations
        with pytest.raises(AssertionError, match=r"stage carrier|unobserved stage"):
            trace.finish(stages=2 if carrier == "different" else 1, require_snapshot=False, require_population=False)
        assert trace.errors


@pytest.mark.parametrize("budget", ["missing", "characters", "separate-stages"])
def test_uncached_metadata_requires_explicit_stage_limits_and_scalar_input_charge(monkeypatch, request, budget):
    values = _diagnostic_inputs()
    characters = sum(len(value) for value in _metadata_strings(values))
    trace = RetentionMemoTrace(monkeypatch, request)
    with clock.retention_stage_memo_scope():
        diagnostic.ComposerDiagnosticEvidence(**values)
    if budget == "separate-stages":
        with clock.retention_stage_memo_scope():
            diagnostic.ComposerDiagnosticEvidence(**values)
        assert [total.attempts for total in trace.uncached_validation_totals()] == [6, 6]
        trace.finish(
            stages=2,
            require_snapshot=False,
            require_population=False,
            uncached_validation_limits=(UncachedValidationBudget(1, 1, 6, characters), UncachedValidationBudget(2, 1, 6, characters)),
        )
    else:
        with pytest.raises(
            AssertionError, match="uncached validation budget is required" if budget == "missing" else "scalar-input budget exceeded"
        ):
            trace.finish(
                stages=1,
                require_snapshot=False,
                require_population=False,
                uncached_validation_limits=() if budget == "missing" else (UncachedValidationBudget(1, 1, 6, characters - 1),),
            )


@pytest.mark.parametrize("family", ["snapshot", "chunk", "credential", "state"])
@pytest.mark.parametrize("extra_work", [False, True])
def test_actual_family_aliases_accept_hits_and_refuse_extra_real_producer_work(monkeypatch, request, family, extra_work):
    encoded, chunk, owner = codec.encode_continuation_snapshot(initial_stack()), _chunk(), _owner()
    methods = {
        "snapshot": RetentionStageMemo.snapshot,
        "chunk": RetentionStageMemo.private_chunk,
        "credential": RetentionStageMemo.credential,
        "state": RetentionStageMemo.state_credentials,
    }
    method = methods[family]
    seen = set()

    def controlled(memo, key, producer):
        result = method(memo, key, producer)
        if extra_work and (memo, key) in seen:
            if family == "snapshot":
                assert producer(key.encoded) == result
            elif family == "chunk":
                assert producer(key.canonical) is result
            else:
                assert producer(key) is result
        seen.add((memo, key))
        return result

    names = {"snapshot": "snapshot", "chunk": "private_chunk", "credential": "credential", "state": "state_credentials"}
    monkeypatch.setattr(RetentionStageMemo, names[family], controlled)
    trace = RetentionMemoTrace(monkeypatch, request)

    def inspect():
        if family == "snapshot":
            return _DECODE(encoded)
        if family == "chunk":
            return _VERIFY(chunk.reference, chunk.canonical)
        if family == "state":
            return _STATE_SCAN(owner.options, owner=owner, policy=CredentialTraversalPolicy(), surface="observer-control")
        return _SCAN({"message": "ordinary"}, policy=CredentialTraversalPolicy(), surface="observer-control", scanner_role="generic")

    with clock.retention_stage_memo_scope() as memo:
        first = inspect()
        assert inspect() is first and memo.cached_entry_count == 1
    assert not trace.errors and not trace.orphans and len(trace.records) == 2
    assert trace.first_orphan is None
    expected = (
        "snapshot input recomputed"
        if family == "snapshot"
        else "chunk input reparsed"
        if family == "chunk"
        else "credential input recomputed"
    )
    if extra_work:
        with pytest.raises(AssertionError, match=expected):
            trace.finish(stages=1, require_snapshot=family == "snapshot")
    else:
        trace.finish(stages=1, require_snapshot=family == "snapshot")
    trace.assert_complete_coverage()


@pytest.mark.parametrize("family", ["snapshot", "chunk", "credential"])
def test_unwrapped_low_level_alias_is_not_mistaken_for_observed_public_work(monkeypatch, request, family):
    encoded, chunk = codec.encode_continuation_snapshot(initial_stack()), _chunk()
    trace = RetentionMemoTrace(monkeypatch, request)
    with clock.retention_stage_memo_scope():
        assert _DECODE(encoded) is _DECODE(encoded)
        if family == "snapshot":
            assert _DECODE_WORK(encoded) == _DECODE(encoded)
        elif family == "chunk":
            assert _CHUNK_WORK(chunk.canonical) is None
        else:
            assert _DETECTOR_WORK({"message": "ordinary"}, CredentialTraversalPolicy()) is None
    assert not trace.errors and len(trace.orphans) == 1
    assert trace.orphans[0][1] == {"snapshot": "snapshot_work", "chunk": "chunk_work", "credential": "detector_work"}[family]
    diagnostic = trace.first_orphan
    assert diagnostic is not None and diagnostic.work == trace.orphans[0][1]
    assert (
        diagnostic.ancestry[0].qualname
        == {"snapshot": _DECODE_WORK, "chunk": _CHUNK_WORK, "credential": _DETECTOR_WORK}[family].__qualname__
    )
    assert diagnostic.submission_expectation == "no-preexisting-carrier" and diagnostic.expected_memo_id is None
    assert diagnostic.inherited_family is None
    with pytest.raises(AssertionError, match="unattributed actual memo work"):
        trace.finish(stages=1)


def test_first_unmatched_work_diagnostic_keeps_only_bounded_code_and_policy_shape(monkeypatch, request, record_property):
    policy = CredentialTraversalPolicy(
        env_ref_names=frozenset({"diagnostic_private_environment_name"}),
        additional_credential_fields=MappingProxyType({"diagnostic_private_field_name": True}),
        max_depth=12,
        max_nodes=64,
        max_width=8,
        max_string_bytes=512,
    )
    trace = RetentionMemoTrace(monkeypatch, request)
    with clock.retention_stage_memo_scope() as memo:
        assert _DETECTOR_WORK("diagnostic_private_message_value", policy) is None
        first = trace.first_orphan
        assert _DETECTOR_WORK("another_private_message_value", CredentialTraversalPolicy()) is None
        assert trace.first_orphan is first
    with pytest.raises(AssertionError, match="unattributed actual memo work"):
        trace.finish(stages=1, require_snapshot=False, require_population=False)
    assert first is not None and first.memo_id == id(memo) and first.policy_id == id(policy)
    assert first.nominal_policy and not first.default_policy
    assert first.policy_bounds == (12, 64, 8, 512) and first.policy_collection_sizes == (1, None)
    assert first.thread_id == threading.get_ident() and first.submission_expectation == "no-preexisting-carrier"
    assert first.expected_memo_id is None and first.inherited_family is None
    assert 2 <= len(first.ancestry) <= 64
    assert first.ancestry[0].qualname == "find_credential_material"
    assert first.ancestry[1].qualname == test_first_unmatched_work_diagnostic_keeps_only_bounded_code_and_policy_shape.__qualname__
    # Serialize only after finish has restored the observation hook.
    serialized = json.dumps(asdict(first))
    for private_text in (
        "diagnostic_private_environment_name",
        "diagnostic_private_field_name",
        "diagnostic_private_message_value",
        "another_private_message_value",
    ):
        assert private_text not in serialized
    assert len(trace.orphans) == 2 and not trace.errors
    # The private exporter must preserve these independently asserted facts.
    # This expectation does not copy the diagnostic being checked.
    code = _DETECTOR_WORK.__code__
    caller = test_first_unmatched_work_diagnostic_keeps_only_bounded_code_and_policy_shape.__code__
    record_property(
        "memo_diagnostic_expected",
        json.dumps(
            {
                "orphan_count": 2,
                "memo_id": id(memo),
                "work": "detector_work",
                "thread_id": threading.get_ident(),
                "submission_expectation": "no-preexisting-carrier",
                "expected_memo_id": None,
                "inherited_family": None,
                "policy_id": id(policy),
                "nominal_policy": True,
                "default_policy": False,
                "policy_bounds": [12, 64, 8, 512],
                "policy_collection_sizes": [1, None],
                "ancestry_prefix": [
                    {"filename": code.co_filename, "qualname": code.co_qualname, "first_line": code.co_firstlineno},
                    {"filename": caller.co_filename, "qualname": caller.co_qualname, "first_line": caller.co_firstlineno},
                ],
            }
        ),
    )


@pytest.mark.parametrize("case", ["valid", "noncanonical", "length", "digest", "unsupported-reference"])
def test_chunk_binding_attempts_follow_real_native_short_circuit_branches(monkeypatch, request, case):
    chunk = _chunk()
    reference, canonical = chunk.reference, chunk.canonical
    if case == "noncanonical":
        canonical = '{ "message": "ordinary" }'
    elif case == "length":
        reference = reference.model_copy(update={"byte_length": reference.byte_length + 1})
    elif case == "digest":
        reference = reference.model_copy(update={"digest": "0" * 64})
    elif case == "unsupported-reference":
        reference = reference.model_copy(update={"digest": []})
    trace = RetentionMemoTrace(monkeypatch, request)
    with clock.retention_stage_memo_scope():
        for _ in range(2):
            if case == "valid":
                assert _VERIFY(reference, canonical) is None
            else:
                with pytest.raises(AuditIntegrityError, match="canonical strict JSON" if case == "noncanonical" else "bytes differ"):
                    _VERIFY(reference, canonical)
    assert len(trace.records) == 2
    for record in trace.records:
        expected = (0, 0, 0) if case == "noncanonical" else (1, 1, 0) if case == "length" else (1, 1, 1)
        assert tuple(record.attempts[key] for key in ("utf8", "length", "sha256")) == expected
        if case == "unsupported-reference":
            assert record.group is None and record.attempts["chunk_work"] == 1
    trace.finish(
        stages=1,
        require_snapshot=False,
        require_population=case not in ("noncanonical", "unsupported-reference"),
        require_valid_chunk_bindings=case == "valid",
    )
    trace.assert_complete_coverage()


def test_actual_internal_detector_policy_binding_and_mutation_get_distinct_data_keys(monkeypatch, request):
    trace = RetentionMemoTrace(monkeypatch, request)
    names = set()
    value = {"api_key": "${CURRENT_KEY}"}

    def inspect():
        return _SCAN(value, policy=CredentialTraversalPolicy(env_ref_names=names), surface="observer-control", scanner_role="generic")

    original = guard.find_credential_material

    def replacement(value, policy):
        return original(value, policy)

    with clock.retention_stage_memo_scope() as memo:
        first = inspect()
        assert first is not None and inspect() is first
        monkeypatch.setattr(guard, "find_credential_material", replacement)
        second = inspect()
        assert second is not None and inspect() is second
        names.add("CURRENT_KEY")
        assert inspect() is None and inspect() is None
        value["api_key"] = "literal"
        fourth = inspect()
        assert fourth is not None and inspect() is fourth
        assert memo.cached_entry_count == 4
    assert sum(record.attempts["detector_work"] for record in trace.records) == 4
    assert [policy.env_ref_names for record in trace.records for policy in record.actual_policies] == [
        frozenset(),
        frozenset(),
        frozenset({"CURRENT_KEY"}),
        frozenset({"CURRENT_KEY"}),
    ]
    assert {record.key.detector for record in trace.records} == {original, replacement}
    trace.finish(stages=1, require_snapshot=False)
    trace.assert_complete_coverage()


def test_fallback_records_actual_internal_work_without_touching_later_foreign_proxy(monkeypatch, request):
    accesses = []

    class ForeignBacking(dict):
        def __len__(self):
            accesses.append("length")
            raise AssertionError("later proxy must remain untouched on early refusal")

        def items(self):
            accesses.append("items")
            raise AssertionError("later proxy must remain untouched on early refusal")

    value = {"api_key": "literal", "later": MappingProxyType(ForeignBacking())}
    trace = RetentionMemoTrace(monkeypatch, request)
    with clock.retention_stage_memo_scope() as memo:
        for _ in range(2):
            finding = _SCAN(value, policy=CredentialTraversalPolicy(), surface="observer-control", scanner_role="generic")
            assert finding is not None and finding.category == "credential_field"
        assert memo.cached_entry_count == 0 and accesses == []
    assert len(trace.records) == 2 and all(record.group is None and record.attempts["detector_work"] == 1 for record in trace.records)
    trace.finish(stages=1, require_snapshot=False, require_population=False)
    trace.assert_complete_coverage()


@pytest.mark.asyncio
async def test_overlapping_out_of_order_scope_exits_keep_one_loop_observer(monkeypatch, request):
    encoded = codec.encode_continuation_snapshot(initial_stack())
    previous = sys.getprofile()
    trace = RetentionMemoTrace(monkeypatch, request)
    entered_a, entered_b, release_a, release_b = (asyncio.Event() for _ in range(4))

    async def a():
        with clock.retention_stage_memo_scope():
            assert _DECODE(encoded) is _DECODE(encoded)
            entered_a.set()
            await release_a.wait()

    async def b():
        await entered_a.wait()
        with clock.retention_stage_memo_scope():
            first = _DECODE(encoded)
            assert _DECODE(encoded) is first
            entered_b.set()
            await release_b.wait()
            assert _DECODE(encoded) is first

    task_a, task_b = asyncio.create_task(a()), asyncio.create_task(b())
    try:
        await entered_b.wait()
        release_a.set()
        await task_a
        assert sys.getprofile() is trace._main_hook
        release_b.set()
        await task_b
        trace.finish(stages=2)
        assert sys.getprofile() is previous
    finally:
        release_a.set()
        release_b.set()
        await asyncio.gather(task_a, task_b, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("missing_carrier", [False, True])
async def test_real_worker_observation_uses_actual_carrier_and_preserves_prior_profile(monkeypatch, request, missing_carrier):
    encoded = codec.encode_continuation_snapshot(initial_stack())
    if missing_carrier:
        monkeypatch.setattr(async_workers, "prepare_retention_sql_callable", partial)
    trace = RetentionMemoTrace(monkeypatch, request)
    ticket = publication_ticket()
    with ThreadPoolExecutor(max_workers=1) as pool:
        monkeypatch.setattr(async_workers, "_SHARED_EXECUTOR", pool)
        previous = pool.submit(sys.getprofile).result()
        with clock.retention_stage_memo_scope() as memo:

            def work():
                return _DECODE(encoded), _DECODE(encoded), current_retention_stage_memo()

            result = await async_workers.run_required_sql_finish_once(ticket, work)
            assert isinstance(result, RequiredSQLReturned) and result.value[0] == result.value[1]
            if missing_carrier:
                assert result.value[2] is None and memo.cached_entry_count == 0
            else:
                assert result.value[0] is result.value[1] and result.value[2] is memo
        assert pool.submit(sys.getprofile).result() is previous
    assert ticket.complete and async_workers.outstanding_admissions() == 0
    if missing_carrier:
        with pytest.raises(AssertionError, match="target work lacked its actual stage carrier"):
            trace.finish(stages=1)
    else:
        trace.finish(stages=1)
        trace.assert_complete_coverage()


def test_loop_profile_chains_and_restores_an_existing_python_hook(monkeypatch, request):
    saved = sys.getprofile()
    assert saved is None, "control requires an unprofiled harness"
    calls = []
    encoded = codec.encode_continuation_snapshot(initial_stack())

    def previous(frame, event, arg):
        if event == "call" and frame.f_code is _DECODE.__code__:
            calls.append(event)

    sys.setprofile(previous)
    try:
        trace = RetentionMemoTrace(monkeypatch, request)
        with clock.retention_stage_memo_scope():
            assert _DECODE(encoded) is _DECODE(encoded)
        trace.finish(stages=1)
        assert calls == ["call", "call"] and sys.getprofile() is previous
    finally:
        sys.setprofile(saved)


def test_unsupported_prior_profiler_refuses_setup_without_replacing_it(monkeypatch, request):
    previous = sys.getprofile()
    assert previous is None, "control requires an unprofiled harness"
    prior = _CallableProfileHook()
    try:
        sys.setprofile(prior)
        assert sys.getprofile() is prior and type(prior) is not FunctionType
        assert "getprofile" in prior.events
        with pytest.raises(RuntimeError, match="refuses unsupported prior profiler"):
            RetentionMemoTrace(monkeypatch, request)
        assert sys.getprofile() is prior
    finally:
        sys.setprofile(previous)
    assert sys.getprofile() is previous


def test_prior_hook_original_error_is_propagated_without_reinstalling_disabled_hook(monkeypatch, request):
    assert sys.getprofile() is None, "control requires an unprofiled harness"
    marker = RuntimeError("original previous profiler failure")
    encoded = codec.encode_continuation_snapshot(initial_stack())

    def previous(frame, event, arg):
        if event == "call" and frame.f_code is _DECODE.__code__:
            raise marker

    sys.setprofile(previous)
    try:
        trace = RetentionMemoTrace(monkeypatch, request)
        with clock.retention_stage_memo_scope() as memo, pytest.raises(RuntimeError) as caught:
            _DECODE(encoded)
        assert caught.value is marker and sys.getprofile() is None
        assert not memo.active and memo.cached_entry_count == 0 and current_retention_stage_memo() is None
        assert not trace.errors and not trace.orphans and not trace.excluded
        with pytest.raises(AssertionError, match="prior profiler failed; observation incomplete"):
            trace.finish(stages=1, require_snapshot=False, require_population=False)
        assert sys.getprofile() is None
    finally:
        sys.setprofile(None)


@pytest.mark.asyncio
async def test_unsupported_worker_profiler_invalidates_accounting_and_keeps_real_sql_outcome(monkeypatch, request):
    encoded = codec.encode_continuation_snapshot(initial_stack())
    prior = _CallableProfileHook()
    previous, installed = [], []

    def initialize():
        saved = sys.getprofile()
        previous.append(saved)
        assert saved is None, "control requires an unprofiled worker"
        sys.setprofile(prior)
        installed.append(True)

    with ThreadPoolExecutor(max_workers=1, initializer=initialize) as pool:
        monkeypatch.setattr(async_workers, "_SHARED_EXECUTOR", pool)
        try:
            assert pool.submit(sys.getprofile).result() is prior and type(prior) is not FunctionType
            assert "getprofile" in prior.events
            trace = RetentionMemoTrace(monkeypatch, request)
            ticket = publication_ticket()
            with clock.retention_stage_memo_scope() as memo:
                result = await async_workers.run_required_sql_finish_once(ticket, lambda: (_DECODE(encoded), _DECODE(encoded)))
                assert isinstance(result, RequiredSQLReturned) and result.value[0] is result.value[1]
                assert memo.cached_entry_count == 1
            assert pool.submit(sys.getprofile).result() is prior and "call" in prior.events
        finally:
            if installed:
                pool.submit(sys.setprofile, previous[0]).result()
        assert pool.submit(sys.getprofile).result() is previous[0]
    assert ticket.complete and async_workers.outstanding_admissions() == 0
    assert not trace.records and not trace.orphans
    assert trace.errors == ["unsupported worker prior profiler; native callable delegated unchanged"]
    with pytest.raises(AssertionError, match="unsupported worker prior profiler"):
        trace.finish(stages=1)


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["no-ticket", "finalizer", "watcher"])
async def test_prebound_or_no_ticket_submissions_preserve_exact_arguments_and_are_excluded(monkeypatch, request, case):
    # This control checks the observer's delegation boundary, not the native
    # capability/admission policy exercised by the real worker controls.
    received = []
    marker = object()

    async def delegate(callable_, ticket=None, finalizer=None, *, deferred_cancellations=None, watch_reader=None):
        received.append((callable_, ticket, finalizer, deferred_cancellations, watch_reader))
        return marker

    class BoundOwner:
        def invoke(self):
            raise AssertionError("identity control must not invoke this callable")

    monkeypatch.setattr(async_workers, "_submit_shared", delegate)
    trace = RetentionMemoTrace(monkeypatch, request)
    owner = BoundOwner()
    supplied = owner.invoke
    ticket = None if case == "no-ticket" else publication_ticket()
    finalizer = owner if case == "finalizer" else None
    watcher = owner if case == "watcher" else None
    cancellations = []
    with clock.retention_stage_memo_scope():
        result = await async_workers._submit_shared(supplied, ticket, finalizer, deferred_cancellations=cancellations, watch_reader=watcher)
    assert result is marker and len(received) == 1
    assert all(
        actual is expected for actual, expected in zip(received[0], (supplied, ticket, finalizer, cancellations, watcher), strict=True)
    )
    assert not trace.records and trace.excluded == {"prebound-or-no-ordinary-ticket": 1}
    with pytest.raises(AssertionError, match="excluded submissions are not observed work"):
        trace.finish(stages=1, require_snapshot=False, require_population=False)


@pytest.mark.asyncio
async def test_reused_worker_restores_new_hook_after_a_prior_hook_failed_on_first_invocation(monkeypatch, request):
    expected = initial_stack()
    encoded = codec.encode_continuation_snapshot(expected)
    trace = RetentionMemoTrace(monkeypatch, request)
    marker = RuntimeError("original first worker profiler failure")
    first_ticket, second_ticket = publication_ticket(), publication_ticket()
    calls = []

    def previous(frame, event, arg):
        if event == "call" and frame.f_code is _DECODE.__code__:
            raise marker

    def first_work():
        calls.append(threading.get_ident())
        return _DECODE(encoded)

    def second_work():
        calls.append(threading.get_ident())
        first = _DECODE(encoded)
        assert _DECODE(encoded) is first
        return first

    with ThreadPoolExecutor(max_workers=1) as pool:
        monkeypatch.setattr(async_workers, "_SHARED_EXECUTOR", pool)
        assert pool.submit(sys.getprofile).result() is None
        pool.submit(sys.setprofile, previous).result()
        try:
            with clock.retention_stage_memo_scope() as memo:
                failed = await async_workers.run_required_sql_finish_once(first_ticket, first_work)
                assert isinstance(failed, RequiredSQLRaised) and failed.error is marker
                assert pool.submit(sys.getprofile).result() is None
                assert memo.cached_entry_count == 0
                succeeded = await async_workers.run_required_sql_finish_once(second_ticket, second_work)
                assert isinstance(succeeded, RequiredSQLReturned) and succeeded.value == expected
                assert memo.cached_entry_count == 1
                assert len(calls) == 2 and calls[0] == calls[1] != threading.get_ident()
                assert pool.submit(sys.getprofile).result() is None
                assert trace._owned_hooks == {trace._main_hook}
        finally:
            pool.submit(sys.setprofile, None).result()
    assert first_ticket.complete and second_ticket.complete and async_workers.outstanding_admissions() == 0
    assert not memo.active and memo.cached_entry_count == 0 and current_retention_stage_memo() is None
    assert not trace.errors and not trace.excluded and not trace.orphans
    with pytest.raises(AssertionError, match="prior profiler failed; observation incomplete"):
        trace.finish(stages=1)


@pytest.mark.asyncio
@pytest.mark.parametrize("prior_fails", [False, True])
async def test_caught_prior_failure_preserves_real_result_but_refuses_unobserved_extra_work(monkeypatch, request, prior_fails):
    expected = initial_stack()
    encoded = codec.encode_continuation_snapshot(expected)
    main_previous = sys.getprofile()
    trace = RetentionMemoTrace(monkeypatch, request)
    marker = RuntimeError("original caught worker profiler failure")
    requests, caught, extra_results, calls = [], [], [], []
    ticket = publication_ticket()

    def previous(frame, event, arg):
        if event == "call" and frame.f_code is _DECODE.__code__:
            requests.append("public decode")
            if prior_fails and len(requests) == 3:
                raise marker

    def work():
        calls.append(threading.get_ident())
        first = _DECODE(encoded)
        assert _DECODE(encoded) is first
        try:
            assert _DECODE(encoded) is first
        except RuntimeError as error:
            assert prior_fails and error is marker
            caught.append(True)
            assert sys.getprofile() is None
            extra = _DECODE_WORK(encoded)
            assert extra == first
            extra_results.append(extra)
        return first

    with ThreadPoolExecutor(max_workers=1) as pool:
        monkeypatch.setattr(async_workers, "_SHARED_EXECUTOR", pool)
        assert pool.submit(sys.getprofile).result() is None
        pool.submit(sys.setprofile, previous).result()
        try:
            with clock.retention_stage_memo_scope() as memo:
                result = await async_workers.run_required_sql_finish_once(ticket, work)
                assert isinstance(result, RequiredSQLReturned) and result.value == expected
                assert memo.cached_entry_count == 1
                assert len(calls) == 1 and calls[0] != threading.get_ident()
            assert pool.submit(sys.getprofile).result() is (None if prior_fails else previous)
            assert trace._owned_hooks == {trace._main_hook}
        finally:
            pool.submit(sys.setprofile, None).result()
        assert pool.submit(sys.getprofile).result() is None
    assert ticket.complete and async_workers.outstanding_admissions() == 0
    assert not memo.active and memo.cached_entry_count == 0 and current_retention_stage_memo() is None
    assert requests == ["public decode"] * 3
    assert caught == ([True] if prior_fails else [])
    assert extra_results == ([expected] if prior_fails else [])
    assert not trace.errors and not trace.orphans and not trace.excluded
    assert len(trace.records) == (2 if prior_fails else 3)
    assert sum(record.attempts["snapshot_work"] for record in trace.records) == 1
    if prior_fails:
        with pytest.raises(AssertionError, match="prior profiler failed; observation incomplete"):
            trace.finish(stages=1)
        with pytest.raises(AssertionError, match="prior profiler failed; observation incomplete"):
            trace.assert_complete_coverage()
    else:
        trace.finish(stages=1)
    assert sys.getprofile() is main_previous


@pytest.mark.asyncio
@pytest.mark.parametrize("native_raises", [False, True])
async def test_own_callback_failure_invalidates_receipt_without_changing_native_outcome_or_prior_hook(monkeypatch, request, native_raises):
    expected = initial_stack()
    encoded = codec.encode_continuation_snapshot(expected)
    main_previous = sys.getprofile()
    trace = RetentionMemoTrace(monkeypatch, request)
    original_event = trace._event
    observer_error = RuntimeError("controlled own observer failure")
    native_error = RuntimeError("original admitted callable failure")
    failures, calls, prior_calls = [], [], []
    ticket = publication_ticket()

    def previous(frame, event, arg):
        if event == "call" and frame.f_code is _DECODE_WORK.__code__:
            prior_calls.append(threading.get_ident())

    def failing_event(frame, event, arg, expected_id):
        if not failures and event == "call" and frame.f_code is _DECODE_WORK.__code__:
            failures.append("actual decoder call")
            raise observer_error
        return original_event(frame, event, arg, expected_id)

    def work():
        calls.append(threading.get_ident())
        first = _DECODE(encoded)
        assert _DECODE(encoded) is first
        if native_raises:
            raise native_error
        return first

    monkeypatch.setattr(trace, "_event", failing_event)
    with ThreadPoolExecutor(max_workers=1) as pool:
        monkeypatch.setattr(async_workers, "_SHARED_EXECUTOR", pool)
        assert pool.submit(sys.getprofile).result() is None
        pool.submit(sys.setprofile, previous).result()
        try:
            with clock.retention_stage_memo_scope() as memo:
                result = await async_workers.run_required_sql_finish_once(ticket, work)
                if native_raises:
                    assert isinstance(result, RequiredSQLRaised) and result.error is native_error
                else:
                    assert isinstance(result, RequiredSQLReturned) and result.value == expected
                assert memo.cached_entry_count == 1
            assert pool.submit(sys.getprofile).result() is previous
            assert prior_calls == calls and len(calls) == 1 and calls[0] != threading.get_ident()
            assert trace._owned_hooks == {trace._main_hook}
        finally:
            pool.submit(sys.setprofile, None).result()
        assert pool.submit(sys.getprofile).result() is None
    assert ticket.complete and async_workers.outstanding_admissions() == 0
    assert failures == ["actual decoder call"]
    assert trace.errors == ["observer callback: RuntimeError"] and not trace.orphans and not trace.excluded
    with pytest.raises(AssertionError, match="observer callback: RuntimeError"):
        trace.finish(stages=1)
    assert sys.getprofile() is main_previous


@pytest.mark.asyncio
@pytest.mark.parametrize("placement", ["loop", "worker"])
async def test_native_cprofile_interaction_preserves_observed_backend_and_real_memo_work(monkeypatch, request, record_property, placement):
    # This control exercises cProfile-first activation. The API-only diagnosis
    # measured the reverse order; no project Trace claim is made for that order.
    expected = initial_stack()
    encoded = codec.encode_continuation_snapshot(expected)
    loop_prior = sys.getprofile()
    holder = {}
    trace = None
    ticket = None

    def never_called():
        raise AssertionError("negative native-stat target must not execute")

    codes = (_DECODE_WORK.__code__, _DECODE.__code__, never_called.__code__)

    def monitoring_state():
        return tuple(
            (
                tool,
                sys.monitoring.get_tool(tool),
                sys.monitoring.get_events(tool),
                tuple(sys.monitoring.get_local_events(tool, code) for code in codes),
            )
            for tool in range(6)
        )

    def owned():
        assert threading.get_ident() == holder["owner"], "native profiler operation left its owning thread"

    def begin():
        holder["owner"] = threading.get_ident()
        holder["prior"] = sys.getprofile()
        holder["before"] = monitoring_state()
        assert holder["prior"] is None, "unknown existing profile hook; refusing to displace it"
        slot = holder["before"][sys.monitoring.PROFILER_ID]
        assert slot[1] is None and slot[2] == 0 and all(value == 0 for value in slot[3]), "native profiler slot already owned"
        profiler = cProfile.Profile()
        holder["profiler"] = profiler
        profiler.enable()
        active = monitoring_state()
        current = sys.getprofile()
        slot = active[sys.monitoring.PROFILER_ID]
        if current is profiler:
            assert slot[1] in (None, "cProfile"), "unknown monitoring ownership alongside legacy cProfile"
            mode = "legacy-hook"
        elif current is holder["prior"] and slot[1] == "cProfile" and slot[2] != 0:
            mode = "monitoring"
        else:
            raise AssertionError(f"unknown native cProfile mechanism: hook_type={type(current).__name__}, slot={slot}")
        assert all(
            before == after for before, after in zip(holder["before"], active, strict=True) if before[0] != sys.monitoring.PROFILER_ID
        ), "cProfile changed a foreign monitoring tool"
        holder["active"] = active
        holder["mode"] = mode
        return mode

    def snapshot():
        owned()
        entries = holder["profiler"].getstats()
        target = tuple((entry.callcount, entry.reccallcount) for entry in entries if entry.code is _DECODE_WORK.__code__)
        negative = tuple(entry.callcount for entry in entries if entry.code is never_called.__code__)
        current = sys.getprofile()
        if trace is None:
            trace_owns_hook = False
        else:
            with trace._lock:
                trace_owns_hook = current in trace._owned_hooks
        return {
            "target": target,
            "negative": negative,
            "hook_is_prior": current is holder["prior"],
            "hook_is_cprofile": current is holder["profiler"],
            "trace_owns_hook": trace_owns_hook,
            "hook_id": id(current),
            "monitoring": monitoring_state(),
            "owner": threading.get_ident(),
        }

    def real_work():
        before = snapshot()
        first = _DECODE(encoded)
        cold = snapshot()
        second = _DECODE(encoded)
        warm = snapshot()
        return first, second, before, cold, warm

    def end():
        owned()
        if "profiler" in holder:
            profiler = holder.pop("profiler")
            profiler.disable()
        after = monitoring_state()
        current = sys.getprofile()
        restored = (current is holder["prior"], after == holder["before"])
        assert restored == (True, True), "native profiler did not restore the observed hook/tool state"
        return {"hook_id": id(current), "monitoring": after, "owner": threading.get_ident()}

    with ThreadPoolExecutor(max_workers=1) as pool:

        def on_owner(function):
            return pool.submit(function).result() if placement == "worker" else function()

        try:
            mode = on_owner(begin)
            record_property("native_cprofile_backend", mode)
            record_property("native_cprofile_placement", placement)
            if placement == "loop" and mode == "legacy-hook":
                with pytest.raises(RuntimeError, match="refuses unsupported prior profiler"):
                    RetentionMemoTrace(monkeypatch, request)
                assert sys.getprofile() is holder["profiler"]
                with clock.retention_stage_memo_scope() as memo:
                    observed = real_work()
                    assert memo.cached_entry_count == 1
            else:
                if placement == "worker":
                    monkeypatch.setattr(async_workers, "_SHARED_EXECUTOR", pool)
                trace = RetentionMemoTrace(monkeypatch, request)
                with clock.retention_stage_memo_scope() as memo:
                    if placement == "worker":
                        ticket = publication_ticket()
                        outcome = await async_workers.run_required_sql_finish_once(ticket, real_work)
                        assert isinstance(outcome, RequiredSQLReturned)
                        observed = outcome.value
                    else:
                        observed = real_work()
                    assert memo.cached_entry_count == 1
            first, second, baseline, cold, warm = observed
            assert first is second and first == expected
            assert baseline["target"] == () and cold["target"] == ((1, 0),) and warm["target"] == cold["target"]
            assert all(point["negative"] == () and point["monitoring"] == holder["active"] for point in (baseline, cold, warm))
            assert all(point["owner"] == holder["owner"] for point in (baseline, cold, warm))
            assert (holder["owner"] != threading.get_ident()) is (placement == "worker")
            assert not memo.active and memo.cached_entry_count == 0 and current_retention_stage_memo() is None
            if mode == "monitoring":
                assert trace is not None and all(point["trace_owns_hook"] for point in (baseline, cold, warm))
                assert len(trace.records) == 2 and sum(record.attempts["snapshot_work"] for record in trace.records) == 1
                trace.finish(stages=1)
            else:
                assert all(point["hook_is_cprofile"] for point in (baseline, cold, warm))
                if placement == "worker":
                    assert trace.errors == ["unsupported worker prior profiler; native callable delegated unchanged"]
                    assert not trace.records and not trace.orphans and not trace.excluded
                    with pytest.raises(AssertionError, match="unsupported worker prior profiler"):
                        trace.finish(stages=1)
            still_active = on_owner(snapshot)
            assert still_active["monitoring"] == holder["active"] and still_active["target"] == warm["target"]
            assert still_active["hook_is_prior"] if mode == "monitoring" else still_active["hook_is_cprofile"]
            if ticket is not None:
                assert ticket.complete and async_workers.outstanding_admissions() == 0
            record_property("native_decoder_statistics", str((baseline["target"], cold["target"], warm["target"])))
        finally:
            if trace is not None:
                trace.close()
            released = on_owner(end)
    assert sys.getprofile() is loop_prior
    record_property(
        "native_cprofile_observation",
        json.dumps(
            {
                "mode": mode,
                "placement": placement,
                "before": {"hook_id": id(holder["prior"]), "monitoring": holder["before"], "owner": holder["owner"]},
                "active": holder["active"],
                "cold": cold,
                "warm": warm,
                "after_trace": still_active,
                "released": released,
            }
        ),
    )


def test_native_byte_call_operand_contract_is_source_bound_and_restores_monitor_ownership():
    """Standalone installed-interpreter prerequisite; no project producer work."""
    import builtins
    import dis
    import sys
    import threading
    from concurrent.futures import ThreadPoolExecutor
    from types import BuiltinFunctionType

    def direct(value):
        return len(value)

    def inline(text):
        return len(text.encode("utf-8", errors="replace"))

    def wrong_source(value):
        return len(value)

    def wrong_callable(value):
        return bool(value)

    def no_arguments():
        return threading.get_ident()

    codes = (direct.__code__, inline.__code__, wrong_source.__code__, wrong_callable.__code__, no_arguments.__code__)
    sites = {}
    for function in (direct, inline):
        instructions = tuple(dis.get_instructions(function))
        load = next(instruction for instruction in instructions if instruction.opname == "LOAD_GLOBAL" and instruction.argval == "len")
        calls = [
            instruction.offset
            for instruction in instructions
            if instruction.opname == "CALL"
            and instruction.positions.lineno == load.positions.lineno
            and instruction.positions.col_offset == load.positions.col_offset
        ]
        assert len(calls) == 1, "native byte API calibration lost its exact len CALL site"
        sites[function.__code__] = calls[0]

    def state():
        return tuple(
            (
                sys.monitoring.get_tool(tool),
                sys.monitoring.get_events(tool),
                tuple(sys.monitoring.get_local_events(tool, code) for code in codes),
            )
            for tool in range(6)
        )

    callback_events = (sys.monitoring.events.CALL, sys.monitoring.events.C_RETURN, sys.monitoring.events.C_RAISE)
    native_register = sys.monitoring.register_callback
    assert type(native_register) is BuiltinFunctionType, "native byte API calibration refuses a replaced registration API"

    class RefusedSlot(RuntimeError):
        pass

    def clean_slot(snapshot):
        tool = next((candidate for candidate in (3, 4) if snapshot[candidate] == (None, 0, (0,) * len(codes))), None)
        if tool is None:
            raise RefusedSlot("native byte API calibration has no unowned zero-mask slot")
        # This checks names/masks only. Callbacks require owned reversible probes.
        return tool

    def admitted(observe, action, *, tool=None, before_mutation=None):
        before = state()
        prior = sys.getprofile()
        tool = clean_slot(before) if tool is None else tool
        if before[tool] != (None, 0, (0,) * len(codes)):
            raise RefusedSlot("native byte API calibration refuses existing ownership or masks")
        owner_name = "u45-byte-api-" + str(id(action))
        claimed = False
        callback_changes = []
        local_changes = []
        uncertain_callbacks = set()
        uncertain_locals = set()
        primary = None
        cleanup_errors = []
        result = None

        def replace(event, callback):
            if before_mutation is not None:
                before_mutation("callback", event)
            uncertain_callbacks.add(event)
            previous = native_register(tool, event, callback)
            # A completed replacement alone creates a rollback obligation.
            callback_changes.append((event, previous, callback))
            uncertain_callbacks.remove(event)
            return previous

        try:
            sys.monitoring.use_tool_id(tool, owner_name)
            claimed = True
            previous = [replace(event, None) for event in callback_events]
            if any(callback is not None for callback in previous):
                raise RefusedSlot("native byte API calibration refuses retained CALL-family callbacks")
            assert replace(sys.monitoring.events.CALL, observe) is None
            for code in codes:
                if before_mutation is not None:
                    before_mutation("local", code)
                previous_mask = sys.monitoring.get_local_events(tool, code)
                assert previous_mask == 0
                uncertain_locals.add(code)
                sys.monitoring.set_local_events(tool, code, sys.monitoring.events.CALL)
                local_changes.append((code, previous_mask))
                uncertain_locals.remove(code)
            result = action()
        except BaseException as error:
            primary = error
        finally:
            if claimed:
                if sys.monitoring.get_tool(tool) != owner_name:
                    cleanup_errors.append(RuntimeError("native byte API calibration lost its own tool"))
                else:
                    if uncertain_callbacks or uncertain_locals:
                        cleanup_errors.append(RuntimeError("native byte API mutation failed with uncertain completion"))
                    if sys.monitoring.get_events(tool) != before[tool][1]:
                        cleanup_errors.append(RuntimeError("native byte API calibration global mask changed without ownership"))
                    for code, previous_mask in reversed(local_changes):
                        try:
                            if sys.monitoring.get_local_events(tool, code) != sys.monitoring.events.CALL:
                                raise RuntimeError("native byte API calibration local mask changed without ownership")
                            sys.monitoring.set_local_events(tool, code, previous_mask)
                        except BaseException as error:
                            cleanup_errors.append(error)
                    unsafe_events = set(uncertain_callbacks)
                    for event, previous, installed in reversed(callback_changes):
                        if event in unsafe_events:
                            continue
                        try:
                            displaced = native_register(tool, event, previous)
                            if displaced is not installed:
                                # Preserve an unexpected callback instead of erasing it.
                                native_register(tool, event, displaced)
                                raise RuntimeError("native byte API calibration callback changed without ownership")
                        except BaseException as error:
                            unsafe_events.add(event)
                            cleanup_errors.append(error)
                    if not cleanup_errors:
                        try:
                            sys.monitoring.free_tool_id(tool)
                        except BaseException as error:
                            cleanup_errors.append(error)
            if cleanup_errors:
                # An uncertain rollback stays visibly owned and cannot qualify.
                raise BaseExceptionGroup(
                    "native byte API calibration cleanup incomplete",
                    ([primary] if primary is not None else []) + cleanup_errors,
                )
        assert state() == before and sys.getprofile() is prior, "native byte API calibration did not restore ownership"
        if primary is not None:
            raise primary
        return result

    def lifecycle(marker):
        before = state()
        prior = sys.getprofile()
        owner_thread = threading.get_ident()
        records = []
        opposed = {"source": 0, "callable": 0, "type": 0, "missing": 0}

        def observe(code, offset, callable_, argument):
            if code is no_arguments.__code__ and callable_ is threading.get_ident:
                assert argument is sys.monitoring.MISSING
                opposed["missing"] += 1
            if code is wrong_source.__code__ and callable_ is builtins.len:
                opposed["source"] += 1
                return
            if code is wrong_callable.__code__ and callable_ is builtins.bool:
                opposed["callable"] += 1
                return
            if code not in sites or offset != sites[code] or callable_ is not builtins.len:
                return
            if type(argument) is not bytes:
                opposed["type"] += 1
                return
            frame = sys._getframe(1)
            assert frame.f_code is code and frame.f_lasti == offset, "native byte API callback lost original instruction ancestry"
            # Numeric observations only: no operand or frame enters the record.
            records.append((code is direct.__code__, offset, id(argument), len(argument), threading.get_ident()))

        def native_body():
            fixtures = ((b"", ""), (b"ASCII", "ASCII"), (b"caf\xc3\xa9\xe2\x98\x83", "café☃"))
            for value, text in fixtures:
                start = len(records)
                assert direct(value) == inline(text) == len(value)
                pair = records[start:]
                assert len(pair) == 2 and pair[0][0] is True and pair[1][0] is False
                assert pair[0][2] == id(value) and pair[0][3] == pair[1][3] == len(value)
                assert pair[0][4] == pair[1][4] == owner_thread
            assert wrong_source(b"opposed") == 7
            assert wrong_callable(b"opposed") is True
            assert direct("opposed") == 7
            assert no_arguments() == owner_thread
            assert opposed == {"source": 1, "callable": 1, "type": 1, "missing": 1} and len(records) == 6
            active = state()
            assert sum(before[index] != active[index] for index in range(6)) == 1
            assert sys.getprofile() is prior
            if marker is not None:
                raise marker

        admitted(observe, native_body)

    def ownership_oppositions():
        for case in (
            "stale-call",
            "stale-return",
            "stale-raise",
            "global-mask",
            "local-mask",
            "before-first-probe",
            "partial-probe",
            "after-install",
            "partial-local",
            "observer-assertion",
        ):
            before = state()
            prior = sys.getprofile()
            tool = clean_slot(before)
            # Admission first establishes callback emptiness reversibly; a free
            # tool name and zero masks alone cannot establish that premise.
            admitted(lambda *_args: None, lambda: None, tool=tool)
            owner_name = "u45-byte-api-disposable-" + case
            foreign_calls = []
            fixture_calls = []
            marker = (
                AssertionError("controlled observer assertion") if case == "observer-assertion" else RuntimeError("pre-API mutation marker")
            )

            def sentinel(*_arguments, calls=foreign_calls):
                calls.append(1)

            seeded_event = {
                "stale-call": sys.monitoring.events.CALL,
                "stale-return": sys.monitoring.events.C_RETURN,
                "stale-raise": sys.monitoring.events.C_RAISE,
                "before-first-probe": sys.monitoring.events.CALL,
                "partial-probe": sys.monitoring.events.C_RAISE,
            }.get(case)
            expected_global = sys.monitoring.events.CALL if case == "global-mask" else 0
            expected_local = sys.monitoring.events.CALL if case == "local-mask" else 0
            fixture_callbacks = dict.fromkeys(callback_events)
            fixture_global = False
            fixture_local = False
            fixture_claimed = False
            try:
                sys.monitoring.use_tool_id(tool, owner_name)
                fixture_claimed = True
                if seeded_event is not None:
                    previous = native_register(tool, seeded_event, sentinel)
                    fixture_callbacks[seeded_event] = sentinel
                    assert previous is None
                if expected_global:
                    sys.monitoring.set_events(tool, expected_global)
                    fixture_global = True
                if expected_local:
                    sys.monitoring.set_local_events(tool, direct.__code__, expected_local)
                    fixture_local = True
                # Deliberately retain ONLY this test's known disposable state.
                sys.monitoring.free_tool_id(tool)
                fixture_claimed = False
                seeded_state = state()
                mutations = {"callback": 0, "local": 0}

                def fail_before_api(kind, _target, *, case=case, mutations=mutations, marker=marker):
                    mutations[kind] += 1
                    if (
                        (case == "before-first-probe" and kind == "callback" and mutations[kind] == 1)
                        or (case == "partial-probe" and kind == "callback" and mutations[kind] == 2)
                        or (case == "after-install" and kind == "local" and mutations[kind] == 1)
                        or (case == "partial-local" and kind == "local" and mutations[kind] == 2)
                    ):
                        # Scoped failure before the real native API operation.
                        raise marker

                def calibration_callback(*_arguments, case=case, marker=marker):
                    if case == "observer-assertion":
                        raise marker

                def fixture(calls=fixture_calls, case=case):
                    calls.append(1)
                    if case == "stale-raise":
                        try:
                            direct(17)
                        except TypeError:
                            return
                        raise AssertionError("opposed native len call did not raise")
                    assert direct(b"operand") == 7

                try:
                    admitted(calibration_callback, fixture, tool=tool, before_mutation=fail_before_api)
                except RefusedSlot:
                    assert case in ("stale-call", "stale-return", "stale-raise", "global-mask", "local-mask")
                except (RuntimeError, AssertionError) as caught:
                    assert caught is marker
                    assert case in ("before-first-probe", "partial-probe", "after-install", "partial-local", "observer-assertion")
                else:
                    raise AssertionError("native byte API admission accepted opposed ownership or failed setup")
                assert not foreign_calls
                assert fixture_calls == ([1] if case == "observer-assertion" else [])
                assert state() == seeded_state and sys.getprofile() is prior
            finally:
                if not fixture_claimed and sys.monitoring.get_tool(tool) is None:
                    sys.monitoring.use_tool_id(tool, owner_name)
                    fixture_claimed = True
                # An admission rollback failure may leave a different explicit
                # owner. Preserve that failure and do not reclaim its slot.
                if fixture_claimed and sys.monitoring.get_tool(tool) == owner_name:
                    cleanup_errors = []
                    for event in callback_events:
                        try:
                            # Deliberate teardown of this test's own retained
                            # state also proves exact callback identity survived.
                            displaced = native_register(tool, event, None)
                            if displaced is not fixture_callbacks[event]:
                                native_register(tool, event, displaced)
                                raise RuntimeError("disposable callback ownership changed")
                        except BaseException as error:
                            cleanup_errors.append(error)
                    if sys.monitoring.get_events(tool) != (expected_global if fixture_global else 0):
                        cleanup_errors.append(RuntimeError("disposable global mask ownership changed"))
                    if sys.monitoring.get_local_events(tool, direct.__code__) != (expected_local if fixture_local else 0):
                        cleanup_errors.append(RuntimeError("disposable local mask ownership changed"))
                    if not cleanup_errors:
                        if fixture_global:
                            sys.monitoring.set_events(tool, 0)
                        if fixture_local:
                            sys.monitoring.set_local_events(tool, direct.__code__, 0)
                        sys.monitoring.free_tool_id(tool)
                    else:
                        primary = sys.exception()
                        raise BaseExceptionGroup(
                            "disposable calibration cleanup incomplete",
                            ([primary] if primary is not None else []) + cleanup_errors,
                        )
            assert state() == before and sys.getprofile() is prior and not foreign_calls

    def exercise():
        for marker in (None, RuntimeError("native byte API marker"), KeyboardInterrupt("native byte API marker"), SystemExit(41)):
            if marker is None:
                lifecycle(marker)
            else:
                try:
                    lifecycle(marker)
                except (RuntimeError, KeyboardInterrupt, SystemExit) as caught:
                    assert caught is marker, "native byte API calibration replaced its original marker"
                else:
                    raise AssertionError("native byte API calibration lost its original marker")
        ownership_oppositions()

    exercise()
    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(exercise).result()


def _native_byte_limits(**charges):
    from tests.helpers.continuation_memo import NativeByteLimit

    channels = ("strict_parse", "canonical", "chunk_length", "chunk_digest", "scanner_extent", "key_preparation")
    assert not set(charges).difference(channels)
    return tuple(NativeByteLimit(channel, charges.get(channel, (0, 0))[0], *charges.get(channel, (0, 0))) for channel in channels)


def _native_scan_budget(stage, value, size, *, scans=1, requests=2):
    from elspeth.web.continuation_retention_memo import CredentialInputToken, CredentialPolicyKey, CredentialScanKey
    from tests.helpers.continuation_memo import NativeByteBudget

    key = CredentialScanKey(
        "generic",
        "native-bytes-control",
        (CredentialInputToken("string", value),),
        CredentialPolicyKey(frozenset(), frozenset(), 32, 4096, 512, 262_144, "1"),
        _DETECTOR_WORK,
    )
    return NativeByteBudget(
        stage,
        "credential",
        _native_byte_limits(scanner_extent=(scans, scans * size), key_preparation=(requests, requests * size)),
        key,
        guard._credential_finding_from_key,
    )


def _native_expected_rows(stage, family, group, *, invocation=None, **charges):
    return [
        {
            "stage": stage,
            "family": family,
            "group": group,
            "channel": channel,
            "calls": charges.get(channel, (0, 0))[0],
            "bytes": charges.get(channel, (0, 0))[1],
            "invocation": invocation,
        }
        for channel in ("strict_parse", "canonical", "chunk_length", "chunk_digest", "scanner_extent", "key_preparation")
    ]


@pytest.mark.parametrize("value,size", [("", 0), ("ordinary", 8), ("café☃", 8)])
@pytest.mark.parametrize("extra", ["none", "warm-producer", "eager-setdefault"])
def test_native_byte_scan_measures_original_encoded_operands_and_warm_key_preparation(
    monkeypatch, request, record_property, value, size, extra
):
    method = RetentionStageMemo.credential
    seen = set()

    def controlled(memo, key, producer):
        if extra == "eager-setdefault":
            # A real eager default expression executes even on a warm key.
            defaults = {}
            defaults.setdefault(key, producer(key))
        result = method(memo, key, producer)
        if extra == "warm-producer" and (memo, key) in seen:
            assert producer(key) is result
        seen.add((memo, key))
        return result

    monkeypatch.setattr(RetentionStageMemo, "credential", controlled)
    trace = RetentionMemoTrace(monkeypatch, request, require_native_bytes=True)
    with clock.retention_stage_memo_scope():
        for _ in range(2):
            assert _SCAN(value, policy=CredentialTraversalPolicy(), surface="native-bytes-control", scanner_role="generic") is None
    budget = _native_scan_budget(1, value, size)
    if extra == "none":
        trace.finish(stages=1, require_snapshot=False, native_byte_limits=(budget,))
        totals = {row.channel: (row.calls, row.bytes) for row in trace.native_byte_totals()}
        assert totals["scanner_extent"] == (1, size)
        assert totals["key_preparation"] == (2, 2 * size)
        trace.assert_complete_coverage()
        record_property(
            "native_byte_expected",
            json.dumps(_native_expected_rows(1, "credential", 1, scanner_extent=(1, size), key_preparation=(2, 2 * size))),
        )
    else:
        assert not trace.orphans and trace.records[0].attempts["detector_work"] > 0
        # This byte assertion must run before the older once-per-key guard.
        with pytest.raises(AssertionError, match="native byte call budget exceeded or missing: scanner_extent"):
            trace.finish(stages=1, require_snapshot=False, native_byte_limits=(budget,))


@pytest.mark.parametrize("mutation", ["missing", "duplicate", "stage", "source", "label", "channel", "charge", "calls"])
def test_native_byte_budgets_refuse_missing_balanced_wrong_and_altered_channels(monkeypatch, request, mutation):
    trace = RetentionMemoTrace(monkeypatch, request, require_native_bytes=True)
    with clock.retention_stage_memo_scope():
        for _ in range(2):
            assert _SCAN("ordinary", policy=CredentialTraversalPolicy(), surface="native-bytes-control", scanner_role="generic") is None
    budget = _native_scan_budget(1, "ordinary", 8)
    budgets = (budget,)
    expected = "native byte budget is required"
    if mutation == "missing":
        budgets = ()
    elif mutation == "duplicate":
        budgets = (budget, budget)
        expected = "duplicate native byte budget"
    elif mutation in ("stage", "source", "label"):
        budgets = (
            replace(
                budget, **{"stage": 2} if mutation == "stage" else {"producer": _SCAN} if mutation == "source" else {"family": "state"}
            ),
        )
        expected = "wrong stage, family, source or DATA key"
    elif mutation == "channel":
        budgets = (replace(budget, limits=budget.limits[:-1]),)
        expected = "native byte channels are required exactly once"
    else:
        channel = "scanner_extent"
        limits = tuple(
            replace(limit, **{"max_bytes": 7} if mutation == "charge" else {"min_calls": 2, "max_calls": 2})
            if limit.channel == channel
            else limit
            for limit in budget.limits
        )
        budgets = (replace(budget, limits=limits),)
        expected = "native byte extent budget exceeded" if mutation == "charge" else "native byte call budget exceeded or missing"
    with pytest.raises(AssertionError, match=expected):
        trace.finish(stages=1, require_snapshot=False, native_byte_limits=budgets)


@pytest.mark.parametrize("variation", ["normal", "extra", "direct", "bypass"])
def test_native_byte_chunk_channels_use_original_validated_loader_bytes_and_charge_each_hit(
    monkeypatch, request, record_property, variation
):
    from types import SimpleNamespace

    from elspeth.web.continuation_retention_memo import PrivateChunkVerificationKey
    from elspeth.web.sessions import continuation_selection as selection
    from tests.helpers.continuation_memo import NativeByteBudget

    # Independently authored canonical bytes; no measurement re-encoding.
    payload = b'{"message":"caf\xc3\xa9\xe2\x98\x83"}'
    canonical = payload.decode("utf-8")
    chunk = codec.encode_private_chunk(codec="composer.messages.v1", canonical=canonical)
    stack = initial_stack()
    monkeypatch.setattr(selection, "required_private_chunks", lambda _stack: (chunk.reference,))

    class Connection:
        def execute(self, _statement):
            return self

        def one_or_none(self):
            return SimpleNamespace(payload=payload, codec=chunk.reference.codec, byte_length=len(payload), sha256=chunk.reference.digest)

    method = RetentionStageMemo.private_chunk

    def controlled(memo, key, producer):
        result = method(memo, key, producer)
        if variation == "extra":
            producer(key.canonical)
        return result

    monkeypatch.setattr(RetentionStageMemo, "private_chunk", controlled)
    trace = RetentionMemoTrace(monkeypatch, request, require_native_bytes=True)
    with clock.retention_stage_memo_scope():
        for _ in range(2):
            if variation == "direct":
                assert _VERIFY(chunk.reference, chunk.canonical) is None
            else:
                assert selection.load_private_chunks(
                    Connection(), session_id=stack.logical.session_id, operation_id=stack.logical.operation_id, stack=stack
                ) == (chunk,)
        if variation == "bypass":
            assert _CHUNK_WORK(canonical) is None
    budget = NativeByteBudget(
        1,
        "chunk",
        _native_byte_limits(
            strict_parse=(1, len(payload)),
            canonical=(1, len(payload)),
            chunk_length=(2, 2 * len(payload)),
            chunk_digest=(2, 2 * len(payload)),
        ),
        PrivateChunkVerificationKey(chunk.reference, canonical),
        _CHUNK_WORK,
    )
    if variation in ("normal", "direct"):
        trace.finish(stages=1, require_snapshot=False, native_byte_limits=(budget,))
        totals = {row.channel: (row.calls, row.bytes) for row in trace.native_byte_totals()}
        assert totals["strict_parse"] == totals["canonical"] == (1, len(payload))
        assert totals["chunk_length"] == totals["chunk_digest"] == (2, 2 * len(payload))
        if variation == "normal":
            record_property(
                "native_byte_expected",
                json.dumps(
                    _native_expected_rows(
                        1,
                        "chunk",
                        1,
                        strict_parse=(1, len(payload)),
                        canonical=(1, len(payload)),
                        chunk_length=(2, 2 * len(payload)),
                        chunk_digest=(2, 2 * len(payload)),
                    )
                ),
            )
    else:
        expected = "native byte call budget exceeded or missing: strict_parse" if variation == "extra" else "native byte provenance invalid"
        with pytest.raises(AssertionError, match=expected):
            trace.finish(stages=1, require_snapshot=False, native_byte_limits=(budget,))


def _native_monitor_state():
    from tests.helpers.continuation_memo import _BYTE_CODES

    return tuple(
        (
            sys.monitoring.get_tool(tool),
            sys.monitoring.get_events(tool),
            tuple(sys.monitoring.get_local_events(tool, code) for code in _BYTE_CODES),
        )
        for tool in range(6)
    )


@pytest.mark.parametrize("marker_type", [None, RuntimeError, KeyboardInterrupt, SystemExit])
def test_native_byte_real_helper_finalizes_and_restores_ownership_after_native_markers(monkeypatch, request, marker_type):
    before, prior = _native_monitor_state(), sys.getprofile()
    marker = None if marker_type is None else marker_type("native byte helper marker")
    trace = RetentionMemoTrace(monkeypatch, request, require_native_bytes=True)
    try:
        with clock.retention_stage_memo_scope():
            for _ in range(2):
                assert _SCAN("ordinary", policy=CredentialTraversalPolicy(), surface="native-bytes-control", scanner_role="generic") is None
            if marker is not None:
                raise marker
    except (RuntimeError, KeyboardInterrupt, SystemExit) as caught:
        assert caught is marker
    trace.finish(stages=1, require_snapshot=False, native_byte_limits=(_native_scan_budget(1, "ordinary", 8),))
    assert _native_monitor_state() == before and sys.getprofile() is prior


@pytest.mark.parametrize("case", ["call", "return", "raise", "before-probe", "partial-probe", "after-install", "partial-local"])
def test_native_byte_real_helper_refuses_retained_callbacks_and_restores_completed_setup(monkeypatch, request, case):
    from tests.helpers.continuation_memo import _BYTE_CODES, _ByteMonitor

    before, prior = _native_monitor_state(), sys.getprofile()
    tools = [tool for tool in (3, 4) if before[tool] == (None, 0, (0,) * len(_BYTE_CODES))]
    assert tools, "control requires one unowned zero-mask tool"
    tool = tools[0]
    # Establish callback emptiness by the REAL helper, reversibly, before this
    # fixture seeds its own retained state. No foreign callback is removed.
    trace = RetentionMemoTrace(monkeypatch, request, require_native_bytes=True)
    trace.finish(stages=0, require_snapshot=False, require_population=False)
    assert _native_monitor_state() == before
    events = (sys.monitoring.events.CALL, sys.monitoring.events.C_RETURN, sys.monitoring.events.C_RAISE)
    event = events[{"call": 0, "return": 1, "raise": 2, "before-probe": 0, "partial-probe": 2}.get(case, 0)]
    seeded = case in ("call", "return", "raise", "before-probe", "partial-probe")
    sentinel_calls = []

    def sentinel(*_arguments):
        sentinel_calls.append(1)

    owner = "native-byte-fixture-" + case
    sys.monitoring.use_tool_id(tool, owner)
    try:
        if seeded:
            assert sys.monitoring.register_callback(tool, event, sentinel) is None
    finally:
        sys.monitoring.free_tool_id(tool)
    retained = _native_monitor_state()
    marker = RuntimeError("native byte setup marker")
    calls = {"callback": 0, "local": 0}

    def before_api(self, kind, _target):
        calls[kind] += 1
        if (
            (case == "before-probe" and kind == "callback" and calls[kind] == 1)
            or (case == "partial-probe" and kind == "callback" and calls[kind] == 2)
            or (case == "after-install" and kind == "local" and calls[kind] == 1)
            or (case == "partial-local" and kind == "local" and calls[kind] == 2)
        ):
            raise marker

    monkeypatch.setattr(_ByteMonitor, "_before_mutation", before_api)
    try:
        with pytest.raises(RuntimeError) as caught:
            RetentionMemoTrace(monkeypatch, request, require_native_bytes=True)
        if case in ("call", "return", "raise"):
            assert str(caught.value) == "byte observer refuses retained CALL-family callbacks"
        else:
            assert caught.value is marker
        assert _native_monitor_state() == retained and sys.getprofile() is prior and not sentinel_calls
    finally:
        # Remove only fixture-owned retained state, under explicit ownership.
        # An uncertain helper owner is deliberately not reclaimed or erased.
        assert sys.monitoring.get_tool(tool) is None, "helper left uncertain owned state"
        sys.monitoring.use_tool_id(tool, owner)
        unexpected = []
        for candidate in events:
            displaced = sys.monitoring.register_callback(tool, candidate, None)
            expected = sentinel if seeded and candidate == event else None
            if displaced is not expected:
                sys.monitoring.register_callback(tool, candidate, displaced)
                unexpected.append(candidate)
        assert not unexpected, "fixture refuses to erase an unexpected callback"
        sys.monitoring.free_tool_id(tool)
    assert _native_monitor_state() == before and sys.getprofile() is prior and not sentinel_calls


@pytest.mark.asyncio
@pytest.mark.parametrize("carrier", ["correct", "missing", "different", "unobserved"])
async def test_native_byte_real_worker_requires_actual_stage_thread_and_submission(monkeypatch, request, carrier):
    original_scope = clock.retention_stage_memo_scope
    before, prior = _native_monitor_state(), sys.getprofile()
    if carrier == "missing":
        monkeypatch.setattr(async_workers, "prepare_retention_sql_callable", partial)
    trace = RetentionMemoTrace(monkeypatch, request, require_native_bytes=True)
    ticket = publication_ticket()

    def scans():
        for _ in range(2):
            assert _SCAN("ordinary", policy=CredentialTraversalPolicy(), surface="native-bytes-control", scanner_role="generic") is None
        return "native-result"

    def work():
        if carrier in ("different", "unobserved"):
            scope = clock.retention_stage_memo_scope if carrier == "different" else original_scope
            with scope():
                return scans()
        return scans()

    with ThreadPoolExecutor(max_workers=1) as pool:
        monkeypatch.setattr(async_workers, "_SHARED_EXECUTOR", pool)
        worker_prior = pool.submit(sys.getprofile).result()
        with clock.retention_stage_memo_scope():
            outcome = await async_workers.run_required_sql_finish_once(ticket, work)
            assert isinstance(outcome, RequiredSQLReturned) and outcome.value == "native-result"
        assert pool.submit(sys.getprofile).result() is worker_prior
    assert ticket.complete and async_workers.outstanding_admissions() == 0
    if carrier == "correct":
        trace.finish(stages=1, require_snapshot=False, native_byte_limits=(_native_scan_budget(1, "ordinary", 8),))
    else:
        expected = (
            "target work lacked its actual stage carrier"
            if carrier == "missing"
            else "target work carried an unobserved stage"
            if carrier == "unobserved"
            else "native byte provenance invalid"
        )
        with pytest.raises(AssertionError, match=expected):
            trace.finish(stages=2 if carrier == "different" else 1, require_snapshot=False, require_population=False)
    assert _native_monitor_state() == before and sys.getprofile() is prior


def test_native_byte_uncached_and_memo_scanner_rows_remain_separately_charged(monkeypatch, request):
    from tests.helpers.continuation_memo import NativeByteBudget

    messages, texts, expected = _provider_fixture()
    native_texts = (b"AUTHORED SYSTEM CONTEXT CANARY", b"AUTHORED USER caf\xc3\xa9\xe2\x98\x83")
    trace = RetentionMemoTrace(monkeypatch, request, require_native_bytes=True)
    with clock.retention_stage_memo_scope():
        assert provider_context.capture_provider_context(messages=messages, tools=None).canonical == expected
        for _ in range(2):
            assert _SCAN("ordinary", policy=CredentialTraversalPolicy(), surface="native-bytes-control", scanner_role="generic") is None
    trace.finish(
        stages=1,
        require_snapshot=False,
        uncached_validation_limits=(_provider_limit(1, 1, 2, sum(len(text) for text in texts)),),
        native_byte_limits=(
            _native_scan_budget(1, "ordinary", 8),
            NativeByteBudget(
                1, "provider_context_message_text", _native_byte_limits(scanner_extent=(2, sum(len(text) for text in native_texts)))
            ),
            _native_capture_budget(1, 1),
        ),
    )
    totals = {(row.family, row.channel): (row.calls, row.bytes) for row in trace.native_byte_totals()}
    assert totals["provider_context_message_text", "scanner_extent"] == (2, sum(len(text) for text in native_texts))
    assert totals["credential", "scanner_extent"] == (1, 8)
    assert totals["provider_context_capture", "canonical"] == (1, len(_provider_canonical_fixture_bytes()))
    assert totals["provider_context_message_text", "canonical"] == totals["credential", "canonical"] == (0, 0)
    assert trace.detector_stage_totals()[0].native_attempts == 3


def test_native_byte_canonical_channel_refuses_a_wrapped_serializer_even_with_equal_product_bytes(monkeypatch, request):
    from types import SimpleNamespace

    from elspeth.contracts import hashing
    from elspeth.web.sessions import continuation_selection as selection

    payload = b'{"message":"ordinary"}'
    chunk = codec.encode_private_chunk(codec="composer.messages.v1", canonical=payload.decode("utf-8"))
    stack = initial_stack()
    original = hashing.rfc8785.dumps

    def wrapped(value):
        return original(value)

    class Connection:
        def execute(self, _statement):
            return self

        def one_or_none(self):
            return SimpleNamespace(payload=payload, codec=chunk.reference.codec, byte_length=len(payload), sha256=chunk.reference.digest)

    monkeypatch.setattr(selection, "required_private_chunks", lambda _stack: (chunk.reference,))
    monkeypatch.setattr(hashing.rfc8785, "dumps", wrapped)
    trace = RetentionMemoTrace(monkeypatch, request, require_native_bytes=True)
    with clock.retention_stage_memo_scope():
        assert selection.load_private_chunks(
            Connection(), session_id=stack.logical.session_id, operation_id=stack.logical.operation_id, stack=stack
        ) == (chunk,)
    assert "canonical output has no original RFC8785 buffer" in trace.byte_errors
    with pytest.raises(AssertionError, match="native byte provenance invalid"):
        trace.finish(stages=1, require_snapshot=False)


@pytest.mark.parametrize("value", ["ordinary!", "café☃!"])
def test_native_byte_longer_real_scalar_exceeds_an_unchanged_extent_cap(monkeypatch, request, value):
    trace = RetentionMemoTrace(monkeypatch, request, require_native_bytes=True)
    with clock.retention_stage_memo_scope():
        for _ in range(2):
            assert _SCAN(value, policy=CredentialTraversalPolicy(), surface="native-bytes-control", scanner_role="generic") is None
    # The changed full DATA key is explicit; the independently fixed eight-byte
    # native scanner cap is intentionally unchanged from the shorter fixture.
    with pytest.raises(AssertionError, match="native byte extent budget exceeded: scanner_extent"):
        trace.finish(stages=1, require_snapshot=False, native_byte_limits=(_native_scan_budget(1, value, 8),))


@pytest.mark.parametrize("swapped", [False, True])
def test_native_byte_distinct_stage_keys_cannot_borrow_balanced_aggregate_charges(monkeypatch, request, swapped):
    trace = RetentionMemoTrace(monkeypatch, request, require_native_bytes=True)
    for value in ("s", "café"):
        with clock.retention_stage_memo_scope():
            for _ in range(2):
                assert _SCAN(value, policy=CredentialTraversalPolicy(), surface="native-bytes-control", scanner_role="generic") is None
    budgets = (_native_scan_budget(1, "s", 1), _native_scan_budget(2, "café", 5))
    if swapped:
        budgets = (replace(budgets[0], stage=2), replace(budgets[1], stage=1))
        with pytest.raises(AssertionError, match="wrong stage, family, source or DATA key"):
            trace.finish(stages=2, require_snapshot=False, native_byte_limits=budgets)
    else:
        trace.finish(stages=2, require_snapshot=False, native_byte_limits=budgets)


@pytest.mark.parametrize("mask", ["global", "local", "both-tools"])
def test_native_byte_real_helper_preserves_retained_masks_without_claiming_their_tool(monkeypatch, request, mask):
    from tests.helpers.continuation_memo import _BYTE_CODES, _ByteMonitor

    before, prior = _native_monitor_state(), sys.getprofile()
    assert all(before[tool] == (None, 0, (0,) * len(_BYTE_CODES)) for tool in (3, 4)), "control requires two unowned zero-mask tools"
    # Probe both callback families with the real ownership implementation.
    monitor = _ByteMonitor(lambda *_arguments: None)
    monitor.close()
    sys.monitoring.use_tool_id(3, "native-byte-fixture-reservation")
    try:
        monitor = _ByteMonitor(lambda *_arguments: None)
        monitor.close()
    finally:
        sys.monitoring.free_tool_id(3)
    seeded_tools = (3, 4) if mask == "both-tools" else (3,)
    for tool in seeded_tools:
        sys.monitoring.use_tool_id(tool, "native-byte-mask-fixture")
        if mask == "local":
            sys.monitoring.set_local_events(tool, _BYTE_CODES[0], sys.monitoring.events.CALL)
        else:
            sys.monitoring.set_events(tool, sys.monitoring.events.CALL)
        sys.monitoring.free_tool_id(tool)
    retained = _native_monitor_state()
    try:
        if mask == "both-tools":
            with pytest.raises(RuntimeError, match="refuses occupied monitoring tools or masks"):
                RetentionMemoTrace(monkeypatch, request, require_native_bytes=True)
        else:
            trace = RetentionMemoTrace(monkeypatch, request, require_native_bytes=True)
            assert trace._byte_monitor.tool == 4
            trace.finish(stages=0, require_snapshot=False, require_population=False)
        assert _native_monitor_state() == retained and sys.getprofile() is prior
    finally:
        for tool in seeded_tools:
            assert sys.monitoring.get_tool(tool) is None, "mask fixture will not replace an uncertain helper owner"
            sys.monitoring.use_tool_id(tool, "native-byte-mask-fixture")
            if mask == "local":
                assert sys.monitoring.get_local_events(tool, _BYTE_CODES[0]) == sys.monitoring.events.CALL
                sys.monitoring.set_local_events(tool, _BYTE_CODES[0], 0)
            else:
                assert sys.monitoring.get_events(tool) == sys.monitoring.events.CALL
                sys.monitoring.set_events(tool, 0)
            sys.monitoring.free_tool_id(tool)
    assert _native_monitor_state() == before and sys.getprofile() is prior


@pytest.mark.parametrize("noncanonical", [False, True])
def test_native_byte_snapshot_text_and_canonical_output_do_not_forge_input_provenance(monkeypatch, request, noncanonical):
    stack = initial_stack()
    encoded = codec.encode_continuation_snapshot(stack)
    if noncanonical:
        encoded = " \n" + encoded
    trace = RetentionMemoTrace(monkeypatch, request, require_native_bytes=True)
    with clock.retention_stage_memo_scope():
        assert _DECODE(encoded) == stack
        assert _DECODE(encoded) == stack
    assert "strict parser has no bound native byte provenance" in trace.byte_errors
    # A real canonical result was produced, but it is never substituted for
    # bytes consumed by the original outer or nested strict parsers.
    assert any(row.channel == "canonical" and row.bytes > 0 for row in trace.native_byte_totals())
    with pytest.raises(AssertionError, match="native byte provenance invalid"):
        trace.finish(stages=1)


def test_native_byte_mode_is_explicit_and_legacy_work_counts_claim_no_byte_evidence(monkeypatch, request):
    before, prior = _native_monitor_state(), sys.getprofile()
    trace = RetentionMemoTrace(monkeypatch, request)
    with clock.retention_stage_memo_scope():
        assert _SCAN("ordinary", policy=CredentialTraversalPolicy(), surface="native-bytes-control", scanner_role="generic") is None
    trace.finish(stages=1, require_snapshot=False)
    assert trace.native_byte_totals() == () and trace._byte_monitor is None
    assert _native_monitor_state() == before and sys.getprofile() is prior


@pytest.mark.parametrize("bypass", ["strict-parse", "canonical", "rfc8785"])
def test_native_byte_actual_unwrapped_producers_cannot_hide_behind_balanced_memo_counts(monkeypatch, request, bypass):
    from elspeth.contracts import hashing, json_parser

    parse, canonical, dumps = json_parser.parse_json_strict, hashing.canonical_json, hashing.rfc8785.dumps
    trace = RetentionMemoTrace(monkeypatch, request, require_native_bytes=True)
    with clock.retention_stage_memo_scope():
        for _ in range(2):
            assert _SCAN("ordinary", policy=CredentialTraversalPolicy(), surface="native-bytes-control", scanner_role="generic") is None
        if bypass == "strict-parse":
            assert parse('{"message":"ordinary"}') == ({"message": "ordinary"}, None)
        elif bypass == "canonical":
            assert canonical({"message": "ordinary"}) == '{"message":"ordinary"}'
        else:
            assert dumps({"message": "ordinary"}) == b'{"message":"ordinary"}'
    assert not trace.orphans and sum(record.attempts["detector_work"] for record in trace.records) == 1
    with pytest.raises(AssertionError, match="native byte provenance invalid"):
        trace.finish(stages=1, require_snapshot=False, native_byte_limits=(_native_scan_budget(1, "ordinary", 8),))


@pytest.mark.parametrize("marker_type", [RuntimeError, KeyboardInterrupt, SystemExit])
def test_native_byte_real_helper_releases_monitor_if_finalizer_registration_fails(monkeypatch, marker_type):
    marker = marker_type("native byte finalizer registration marker")
    before, prior = _native_monitor_state(), sys.getprofile()

    class RefusedRequest:
        def addfinalizer(self, _finalizer):
            raise marker

    with pytest.raises(marker_type) as caught:
        RetentionMemoTrace(monkeypatch, RefusedRequest(), require_native_bytes=True)
    assert caught.value is marker
    assert _native_monitor_state() == before and sys.getprofile() is prior


def _provider_canonical_fixture_bytes():
    # Independently authored complete canonical fixture. This is neither an
    # observer encode nor a bound derived from the actual capture result.
    return (
        b'{"capture_boundary":"prepared_litellm_messages_and_tools","messages":['
        b'{"content":"AUTHORED SYSTEM CONTEXT CANARY","role":"system"},'
        b'{"content":"AUTHORED USER caf\xc3\xa9\xe2\x98\x83","role":"user"}],'
        b'"schema_version":"composer.provider-context.v1","tools":null}'
    )


def _native_capture_budget(stage, invocation, *, allowance=None):
    from tests.helpers.continuation_memo import NativeByteBudget

    return NativeByteBudget(
        stage,
        "provider_context_capture",
        _native_byte_limits(canonical=(1, len(_provider_canonical_fixture_bytes()) if allowance is None else allowance)),
        producer=provider_context.capture_provider_context,
        invocation=invocation,
    )


@pytest.mark.parametrize("placement", ["standalone", "nested"])
def test_native_byte_complete_context_canonical_owner_is_independent_of_enclosing_memo(monkeypatch, request, record_property, placement):
    from tests.helpers.continuation_memo import NativeByteBudget

    messages, texts, expected = _provider_fixture()
    canonical_bytes = _provider_canonical_fixture_bytes()
    assert canonical_bytes.decode("utf-8") == expected
    text_bytes = (b"AUTHORED SYSTEM CONTEXT CANARY", b"AUTHORED USER caf\xc3\xa9\xe2\x98\x83")
    original = guard._credential_finding_from_key
    captures = []

    def producing(key):
        captures.append(provider_context.capture_provider_context(messages=messages, tools=None))
        return original(key)

    if placement == "nested":
        monkeypatch.setattr(guard, "_credential_finding_from_key", producing)
    trace = RetentionMemoTrace(monkeypatch, request, require_native_bytes=True)
    with clock.retention_stage_memo_scope():
        if placement == "standalone":
            captures.append(provider_context.capture_provider_context(messages=messages, tools=None))
        for _ in range(2):
            assert _SCAN("ordinary", policy=CredentialTraversalPolicy(), surface="native-bytes-control", scanner_role="generic") is None
    assert len(captures) == 1 and captures[0].canonical == expected
    trace.finish(
        stages=1,
        require_snapshot=False,
        uncached_validation_limits=(_provider_limit(1, 1, 2, sum(len(text) for text in texts)),),
        native_byte_limits=(
            _native_scan_budget(1, "ordinary", 8),
            NativeByteBudget(
                1, "provider_context_message_text", _native_byte_limits(scanner_extent=(2, sum(len(text) for text in text_bytes)))
            ),
            _native_capture_budget(1, 1),
        ),
    )
    rows = trace.native_byte_totals()
    assert [(row.family, row.invocation, row.calls, row.bytes) for row in rows if row.channel == "canonical"] == [
        ("credential", None, 0, 0),
        ("provider_context_message_text", None, 0, 0),
        ("provider_context_capture", 1, 1, len(canonical_bytes)),
    ]
    context = trace.context_canonical_inspections[0]
    assert context.exited and context.thread_id == threading.get_ident() and context.memo is trace.memos[0]
    expected_rows = (
        _native_expected_rows(1, "credential", 1, scanner_extent=(1, 8), key_preparation=(2, 16))
        + _native_expected_rows(1, "provider_context_message_text", 2, scanner_extent=(2, sum(len(text) for text in text_bytes)))
        + _native_expected_rows(1, "provider_context_capture", 3, invocation=1, canonical=(1, len(canonical_bytes)))
    )
    record_property("native_byte_expected", json.dumps(expected_rows))


@pytest.mark.parametrize("exchanged", [False, True])
def test_native_byte_correct_capture_identities_cannot_exchange_balanced_canonical_allowances(
    monkeypatch, request, record_property, exchanged
):
    from tests.helpers.continuation_memo import NativeByteBudget

    messages, texts, expected = _provider_fixture()
    longer_messages, longer_texts, longer_expected = _provider_fixture(longer=True)
    canonical_size = len(_provider_canonical_fixture_bytes())
    scalar_size = len(b"AUTHORED SYSTEM CONTEXT CANARY") + len(b"AUTHORED USER caf\xc3\xa9\xe2\x98\x83")
    trace = RetentionMemoTrace(monkeypatch, request, require_native_bytes=True)
    with clock.retention_stage_memo_scope():
        assert provider_context.capture_provider_context(messages=messages, tools=None).canonical == expected
        assert provider_context.capture_provider_context(messages=longer_messages, tools=None).canonical == longer_expected
    scalar = NativeByteBudget(1, "provider_context_message_text", _native_byte_limits(scanner_extent=(4, 2 * scalar_size + 1)))
    caps = (canonical_size + 1, canonical_size) if exchanged else (canonical_size, canonical_size + 1)
    budgets = (scalar, _native_capture_budget(1, 1, allowance=caps[0]), _native_capture_budget(1, 2, allowance=caps[1]))
    limits = (_provider_limit(1, 2, 4, sum(len(text) for text in (*texts, *longer_texts))),)
    if exchanged:
        # Identities and aggregate allowance stay correct; only the numeric
        # caps move. The second real capture must not borrow the first's cap.
        with pytest.raises(AssertionError, match="native byte extent budget exceeded: canonical"):
            trace.finish(
                stages=1, require_snapshot=False, require_population=False, uncached_validation_limits=limits, native_byte_limits=budgets
            )
    else:
        trace.finish(
            stages=1, require_snapshot=False, require_population=False, uncached_validation_limits=limits, native_byte_limits=budgets
        )
        expected_rows = (
            _native_expected_rows(1, "provider_context_message_text", 1, scanner_extent=(4, 2 * scalar_size + 1))
            + _native_expected_rows(1, "provider_context_capture", 2, invocation=1, canonical=(1, canonical_size))
            + _native_expected_rows(1, "provider_context_capture", 3, invocation=2, canonical=(1, canonical_size + 1))
        )
        record_property("native_byte_expected", json.dumps(expected_rows))


@pytest.mark.parametrize("placement", ["standalone", "nested"])
@pytest.mark.parametrize("mutation", ["wrapper", "unrelated", "policy", "text-caller"])
def test_native_byte_context_boundary_refuses_wrappers_unrelated_work_and_changed_validation(monkeypatch, request, placement, mutation):
    messages, _texts, expected = _provider_fixture()
    original_canonical = provider_context.canonical_json
    original_text = provider_context._text
    original_producer = guard._credential_finding_from_key

    if mutation in ("wrapper", "unrelated"):

        def changed_canonical(value):
            if mutation == "unrelated":
                assert original_canonical({"unrelated": "ordinary"}) == '{"unrelated":"ordinary"}'
            return original_canonical(value)

        monkeypatch.setattr(provider_context, "canonical_json", changed_canonical)
    elif mutation == "policy":
        monkeypatch.setattr(
            provider_context, "_POLICY", replace(provider_context._POLICY, max_nodes=provider_context._POLICY.max_nodes + 1)
        )
    else:

        def changed_text(value):
            return original_text(value)

        monkeypatch.setattr(provider_context, "_text", changed_text)

    captured = []

    def producing(key):
        captured.append(provider_context.capture_provider_context(messages=messages, tools=None))
        return original_producer(key)

    if placement == "nested":
        monkeypatch.setattr(guard, "_credential_finding_from_key", producing)
    trace = RetentionMemoTrace(monkeypatch, request, require_native_bytes=True)
    with clock.retention_stage_memo_scope():
        if placement == "standalone":
            captured.append(provider_context.capture_provider_context(messages=messages, tools=None))
        for _ in range(2):
            assert _SCAN("ordinary", policy=CredentialTraversalPolicy(), surface="native-bytes-control", scanner_role="generic") is None
    assert len(captured) == 1 and captured[0].canonical == expected
    assert all(record.byte_calls["canonical"] == record.byte_counts["canonical"] == 0 for record in trace.records)
    assert all(record.byte_calls["canonical"] == record.byte_counts["canonical"] == 0 for record in trace.uncached_validations)
    if mutation in ("wrapper", "unrelated"):
        assert "provider context canonical work lost its complete capture owner" in trace.byte_errors
    else:
        assert len(trace.orphans) == 2
    with pytest.raises(AssertionError, match="native byte provenance invalid"):
        trace.finish(stages=1, require_snapshot=False)


@pytest.mark.parametrize("mutation", ["missing", "invocation", "source", "allowance", "extra-capture"])
def test_native_byte_context_canonical_work_requires_its_original_finite_invocation_allowance(monkeypatch, request, mutation):
    from tests.helpers.continuation_memo import NativeByteBudget

    messages, texts, expected = _provider_fixture()
    text_size = len(b"AUTHORED SYSTEM CONTEXT CANARY") + len(b"AUTHORED USER caf\xc3\xa9\xe2\x98\x83")
    count = 2 if mutation == "extra-capture" else 1
    trace = RetentionMemoTrace(monkeypatch, request, require_native_bytes=True)
    with clock.retention_stage_memo_scope():
        for _ in range(count):
            assert provider_context.capture_provider_context(messages=messages, tools=None).canonical == expected
    scalar = NativeByteBudget(1, "provider_context_message_text", _native_byte_limits(scanner_extent=(2 * count, text_size * count)))
    capture = _native_capture_budget(1, 1)
    budgets = (scalar, capture)
    if mutation == "missing":
        budgets = (scalar,)
    elif mutation == "invocation":
        budgets = (scalar, replace(capture, invocation=2))
    elif mutation == "source":
        budgets = (scalar, replace(capture, producer=_SCAN))
    elif mutation == "allowance":
        budgets = (scalar, _native_capture_budget(1, 1, allowance=len(_provider_canonical_fixture_bytes()) - 1))
    failure = (
        "wrong stage, family, source or DATA key"
        if mutation == "invocation"
        else "lost its complete original invocation"
        if mutation == "source"
        else "native byte extent budget exceeded: canonical"
        if mutation == "allowance"
        else "native byte budget is required"
    )
    with pytest.raises(AssertionError, match=failure):
        trace.finish(
            stages=1,
            require_snapshot=False,
            require_population=False,
            uncached_validation_limits=(_provider_limit(1, count, 2 * count, count * sum(len(text) for text in texts)),),
            native_byte_limits=budgets,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("carrier", ["correct", "missing", "different", "unobserved"])
async def test_native_byte_complete_context_worker_owner_requires_original_carrier(monkeypatch, request, carrier):
    from tests.helpers.continuation_memo import NativeByteBudget

    messages, texts, expected = _provider_fixture()
    text_size = len(b"AUTHORED SYSTEM CONTEXT CANARY") + len(b"AUTHORED USER caf\xc3\xa9\xe2\x98\x83")
    original_scope = clock.retention_stage_memo_scope
    if carrier == "missing":
        monkeypatch.setattr(async_workers, "prepare_retention_sql_callable", partial)
    before, prior = _native_monitor_state(), sys.getprofile()
    trace = RetentionMemoTrace(monkeypatch, request, require_native_bytes=True)
    ticket = publication_ticket()

    def work():
        if carrier in ("different", "unobserved"):
            scope = clock.retention_stage_memo_scope if carrier == "different" else original_scope
            with scope():
                return provider_context.capture_provider_context(messages=messages, tools=None)
        return provider_context.capture_provider_context(messages=messages, tools=None)

    with ThreadPoolExecutor(max_workers=1) as pool:
        monkeypatch.setattr(async_workers, "_SHARED_EXECUTOR", pool)
        worker_prior = pool.submit(sys.getprofile).result()
        with clock.retention_stage_memo_scope():
            outcome = await async_workers.run_required_sql_finish_once(ticket, work)
            assert isinstance(outcome, RequiredSQLReturned) and outcome.value.canonical == expected
        assert pool.submit(sys.getprofile).result() is worker_prior
    assert ticket.complete and async_workers.outstanding_admissions() == 0
    if carrier == "correct":
        owner = trace.context_canonical_inspections[0]
        assert owner.thread_id != threading.get_ident() and owner.expected_memo_id == id(trace.memos[0]) and owner.exited
        trace.finish(
            stages=1,
            require_snapshot=False,
            require_population=False,
            uncached_validation_limits=(_provider_limit(1, 1, 2, sum(len(text) for text in texts)),),
            native_byte_limits=(
                NativeByteBudget(1, "provider_context_message_text", _native_byte_limits(scanner_extent=(2, text_size))),
                _native_capture_budget(1, 1),
            ),
        )
    else:
        assert not trace.context_canonical_inspections
        failure = (
            "target work lacked its actual stage carrier"
            if carrier == "missing"
            else "target work carried an unobserved stage"
            if carrier == "unobserved"
            else "uncached validation disagreed with its submitted stage carrier"
        )
        with pytest.raises(AssertionError, match=failure):
            trace.finish(stages=2 if carrier == "different" else 1, require_snapshot=False, require_population=False)
    assert _native_monitor_state() == before and sys.getprofile() is prior


@pytest.mark.parametrize("refused", [False, True])
def test_native_byte_complete_context_zero_scalar_paths_keep_real_invocation_and_canonical_work(monkeypatch, request, refused):
    from tests.helpers.continuation_memo import NativeByteBudget

    class Unsupported:
        def __repr__(self):
            raise AssertionError("observer must not stringify an unavailable context")

    empty = b'{"capture_boundary":"prepared_litellm_messages_and_tools","messages":[],"schema_version":"composer.provider-context.v1","tools":null}'
    trace = RetentionMemoTrace(monkeypatch, request, require_native_bytes=True)
    with clock.retention_stage_memo_scope():
        result = provider_context.capture_provider_context(messages=Unsupported() if refused else [], tools=None)
    if refused:
        assert result.disposition == "unavailable" and result.canonical is None and result.reasons == ("unsupported",)
        allowance = NativeByteBudget(
            1, "provider_context_capture", _native_byte_limits(), producer=provider_context.capture_provider_context, invocation=1
        )
    else:
        assert result.disposition == "retained" and result.canonical == empty.decode("utf-8")
        allowance = _native_capture_budget(1, 1, allowance=len(empty))
    trace.finish(
        stages=1,
        require_snapshot=False,
        require_population=False,
        uncached_validation_limits=(_provider_limit(1, 1, 0, 0),),
        native_byte_limits=(NativeByteBudget(1, "provider_context_message_text", _native_byte_limits()), allowance),
    )
    assert len(trace.context_canonical_inspections) == 1 and trace.context_canonical_inspections[0].exited
    assert trace.detector_stage_totals()[0].native_attempts == 0


@pytest.mark.parametrize("transfer", [False, True])
def test_native_byte_context_nested_in_real_chunk_producer_cannot_borrow_outer_canonical_allowance(monkeypatch, request, transfer):
    from types import SimpleNamespace

    from elspeth.web.continuation_retention_memo import PrivateChunkVerificationKey
    from elspeth.web.sessions import continuation_selection as selection
    from tests.helpers.continuation_memo import NativeByteBudget

    messages, texts, expected = _provider_fixture()
    payload = b'{"message":"ordinary"}'
    chunk = codec.encode_private_chunk(codec="composer.messages.v1", canonical=payload.decode("utf-8"))
    stack = initial_stack()
    original = codec._verify_private_chunk_canonical
    captures = []

    def producing(canonical):
        captures.append(provider_context.capture_provider_context(messages=messages, tools=None))
        return original(canonical)

    class Connection:
        def execute(self, _statement):
            return self

        def one_or_none(self):
            return SimpleNamespace(payload=payload, codec=chunk.reference.codec, byte_length=len(payload), sha256=chunk.reference.digest)

    monkeypatch.setattr(codec, "_verify_private_chunk_canonical", producing)
    monkeypatch.setattr(selection, "required_private_chunks", lambda _stack: (chunk.reference,))
    trace = RetentionMemoTrace(monkeypatch, request, require_native_bytes=True)
    with clock.retention_stage_memo_scope():
        for _ in range(2):
            assert selection.load_private_chunks(
                Connection(), session_id=stack.logical.session_id, operation_id=stack.logical.operation_id, stack=stack
            ) == (chunk,)
    assert len(captures) == 1 and captures[0].canonical == expected
    context_size = len(_provider_canonical_fixture_bytes())
    scalar_size = len(b"AUTHORED SYSTEM CONTEXT CANARY") + len(b"AUTHORED USER caf\xc3\xa9\xe2\x98\x83")
    outer = NativeByteBudget(
        1,
        "chunk",
        _native_byte_limits(
            strict_parse=(1, len(payload)),
            canonical=(1, len(payload) + (context_size if transfer else 0)),
            chunk_length=(2, 2 * len(payload)),
            chunk_digest=(2, 2 * len(payload)),
        ),
        PrivateChunkVerificationKey(chunk.reference, chunk.canonical),
        producing,
    )
    budgets = (
        outer,
        NativeByteBudget(1, "provider_context_message_text", _native_byte_limits(scanner_extent=(2, scalar_size))),
        _native_capture_budget(1, 1, allowance=0 if transfer else context_size),
    )
    limits = (_provider_limit(1, 1, 2, sum(len(text) for text in texts)),)
    # The legacy once-per-key canonical check remains exact too: this capture
    # has never been part of the chunk's canonical product.
    assert sum(record.attempts["canonical_encode"] for record in trace.records) == 1
    if transfer:
        with pytest.raises(AssertionError, match="native byte extent budget exceeded: canonical"):
            trace.finish(stages=1, require_snapshot=False, native_byte_limits=budgets, uncached_validation_limits=limits)
    else:
        trace.finish(stages=1, require_snapshot=False, native_byte_limits=budgets, uncached_validation_limits=limits)
        actual = {(row.family, row.channel): (row.calls, row.bytes) for row in trace.native_byte_totals()}
        assert actual["chunk", "canonical"] == (1, len(payload))
        assert actual["provider_context_capture", "canonical"] == (1, context_size)


@pytest.mark.parametrize("mutation", ["messages", "tools", "copy", "policy"])
def test_native_byte_zero_scalar_capture_does_not_hide_changed_original_validation(monkeypatch, request, mutation):
    empty = b'{"capture_boundary":"prepared_litellm_messages_and_tools","messages":[],"schema_version":"composer.provider-context.v1","tools":null}'
    if mutation == "policy":
        monkeypatch.setattr(
            provider_context, "_POLICY", replace(provider_context._POLICY, max_nodes=provider_context._POLICY.max_nodes + 1)
        )
    else:
        original = (
            provider_context._check_messages
            if mutation == "messages"
            else provider_context._check_tools
            if mutation == "tools"
            else provider_context._copy_json
        )

        def changed(*args, **kwargs):
            return original(*args, **kwargs)

        if mutation == "messages":
            monkeypatch.setattr(provider_context, "_check_messages", changed)
        elif mutation == "tools":
            monkeypatch.setattr(provider_context, "_check_tools", changed)
        else:
            monkeypatch.setattr(provider_context, "_copy_json", changed)
    trace = RetentionMemoTrace(monkeypatch, request, require_native_bytes=True)
    with clock.retention_stage_memo_scope():
        result = provider_context.capture_provider_context(messages=[], tools=None)
    assert result.disposition == "retained" and result.canonical == empty.decode("utf-8")
    assert trace.detector_stage_totals()[0].native_attempts == 0
    assert "provider context canonical work lost its complete capture owner" in trace.byte_errors
    with pytest.raises(AssertionError, match="native byte provenance invalid"):
        trace.finish(stages=1, require_snapshot=False, require_population=False)


@pytest.mark.parametrize("depth", [0, 80])
def test_native_byte_real_stack_exhaustion_cannot_authorize_a_nearer_chunk_owner(monkeypatch, request, depth):
    from types import SimpleNamespace

    from elspeth.web.continuation_retention_memo import PrivateChunkVerificationKey
    from elspeth.web.sessions import continuation_selection as selection
    from tests.helpers.continuation_memo import NativeByteBudget

    payload = b'{"message":"ordinary"}'
    chunk = codec.encode_private_chunk(codec="composer.messages.v1", canonical=payload.decode("utf-8"))
    stack = initial_stack()
    monkeypatch.setattr(selection, "required_private_chunks", lambda _stack: (chunk.reference,))

    class Connection:
        def execute(self, _statement):
            return self

        def one_or_none(self):
            return SimpleNamespace(payload=payload, codec=chunk.reference.codec, byte_length=len(payload), sha256=chunk.reference.digest)

    def forward(remaining):
        if remaining:
            return forward(remaining - 1)
        return selection.load_private_chunks(
            Connection(), session_id=stack.logical.session_id, operation_id=stack.logical.operation_id, stack=stack
        )

    trace = RetentionMemoTrace(monkeypatch, request, require_native_bytes=True)
    with clock.retention_stage_memo_scope() as memo:
        assert forward(depth) == (chunk,)
        assert forward(depth) == (chunk,)
        assert memo.cached_entry_count == 1
    assert not trace.errors and not trace.orphans
    assert len(trace.records) == 2 and all(record.family == "chunk" for record in trace.records)
    assert sum(record.attempts["chunk_work"] for record in trace.records) == 1
    assert sum(record.attempts["strict_parse"] for record in trace.records) == 1
    assert sum(record.attempts["canonical_encode"] for record in trace.records) == 1
    budget = NativeByteBudget(
        1,
        "chunk",
        _native_byte_limits(
            strict_parse=(1, len(payload)),
            canonical=(1, len(payload)),
            chunk_length=(2, 2 * len(payload)),
            chunk_digest=(2, 2 * len(payload)),
        ),
        PrivateChunkVerificationKey(chunk.reference, chunk.canonical),
        _CHUNK_WORK,
    )
    if depth:
        # There is deliberately no active capture here. This tests the
        # decisive real-stack invariant: uncertainty cannot authorize the
        # nearer actual memo owner. It does not prove capture reentry.
        with pytest.raises(AssertionError, match="capture ancestry search exhausted; canonical ownership is unproven"):
            trace.finish(stages=1, require_snapshot=False, native_byte_limits=(budget,))
        assert trace.byte_errors == [
            "capture ancestry search exhausted; canonical ownership is unproven",
            "canonical work lacks its complete DATA owner",
            "RFC8785 work lacks its original canonical DATA owner",
        ]
        assert all(record.byte_calls["canonical"] == record.byte_counts["canonical"] == 0 for record in trace.records)
        assert not trace.uncached_validations and not trace.context_canonical_inspections
    else:
        trace.finish(stages=1, require_snapshot=False, native_byte_limits=(budget,))
        assert sum(record.byte_calls["canonical"] for record in trace.records) == 1
        assert sum(record.byte_counts["canonical"] for record in trace.records) == len(payload)


def _late_chunk_budget(chunk, size, *, requests=2):
    from elspeth.web.continuation_retention_memo import PrivateChunkVerificationKey
    from tests.helpers.continuation_memo import NativeByteBudget

    return NativeByteBudget(
        1,
        "chunk",
        _native_byte_limits(
            strict_parse=(1, size),
            canonical=(1, size),
            chunk_length=(requests, requests * size),
            chunk_digest=(requests, requests * size),
        ),
        PrivateChunkVerificationKey(chunk.reference, chunk.canonical),
        _CHUNK_WORK,
    )


@pytest.mark.parametrize("payload", [b"{}", b'{"a":"ordinary"}', b'{"a":"caf\xc3\xa9\xe2\x98\x83"}'])
@pytest.mark.parametrize("extra", [False, True])
def test_native_byte_late_chunk_buffer_charges_each_actual_parse_before_count_guard(monkeypatch, request, record_property, payload, extra):
    chunk = codec.encode_private_chunk(codec="composer.messages.v1", canonical=payload.decode("utf-8"))
    method = RetentionStageMemo.private_chunk

    def controlled(memo, key, producer):
        result = method(memo, key, producer)
        if extra:
            producer(key.canonical)
        return result

    monkeypatch.setattr(RetentionStageMemo, "private_chunk", controlled)
    before, prior = _native_monitor_state(), sys.getprofile()
    trace = RetentionMemoTrace(monkeypatch, request, require_native_bytes=True)
    with clock.retention_stage_memo_scope():
        for _ in range(2):
            assert _VERIFY(chunk.reference, chunk.canonical) is None
    budget = _late_chunk_budget(chunk, len(payload))
    if extra:
        with pytest.raises(AssertionError, match="native byte call budget exceeded or missing: strict_parse"):
            trace.finish(stages=1, require_snapshot=False, native_byte_limits=(budget,))
    else:
        trace.finish(stages=1, require_snapshot=False, native_byte_limits=(budget,))
    rows = {row.channel: (row.calls, row.bytes) for row in trace.native_byte_totals()}
    assert rows["strict_parse"] == rows["canonical"] == ((3 if extra else 1), (3 if extra else 1) * len(payload))
    assert rows["chunk_length"] == rows["chunk_digest"] == (2, 2 * len(payload))
    assert [record.byte_calls["strict_parse"] for record in trace.records] == ([2, 1] if extra else [1, 0])
    assert not trace._byte_pending_parses and not trace.byte_errors
    assert _native_monitor_state() == before and sys.getprofile() is prior
    if not extra and payload == b"{}":
        record_property(
            "native_byte_expected",
            json.dumps(
                _native_expected_rows(
                    1,
                    "chunk",
                    1,
                    strict_parse=(1, 2),
                    canonical=(1, 2),
                    chunk_length=(2, 4),
                    chunk_digest=(2, 4),
                )
            ),
        )


@pytest.mark.parametrize("case", ["wrong-text", "missing-buffer", "interrupt", "exit", "cancel"])
def test_native_byte_late_chunk_wrong_or_missing_original_source_preserves_native_outcome(monkeypatch, request, case):
    payload = b'{"a":"original"}'
    chunk = codec.encode_private_chunk(codec="composer.messages.v1", canonical=payload.decode("utf-8"))
    marker_type = {"interrupt": KeyboardInterrupt, "exit": SystemExit, "cancel": asyncio.CancelledError}.get(case, RuntimeError)
    marker = marker_type("original late chunk marker")
    method = RetentionStageMemo.private_chunk

    def controlled(memo, key, producer):
        if case == "wrong-text":
            producer('{"a":"different"}')
            return method(memo, key, producer)
        producer(key.canonical)
        raise marker

    monkeypatch.setattr(RetentionStageMemo, "private_chunk", controlled)
    before, prior = _native_monitor_state(), sys.getprofile()
    trace = RetentionMemoTrace(monkeypatch, request, require_native_bytes=True)
    with clock.retention_stage_memo_scope():
        if case == "wrong-text":
            assert _VERIFY(chunk.reference, chunk.canonical) is None
        else:
            with pytest.raises(marker_type) as caught:
                _VERIFY(chunk.reference, chunk.canonical)
            assert caught.value is marker
    with pytest.raises(AssertionError, match="native byte provenance invalid"):
        trace.finish(stages=1, require_snapshot=False, require_population=False)
    assert "strict parser has no bound native byte provenance" in trace.byte_errors
    assert not trace._byte_pending_parses
    diagnostic = next(item for item in trace.byte_error_diagnostics if item.message == "strict parser has no bound native byte provenance")
    assert diagnostic.stage == 1 and diagnostic.thread_id == threading.get_ident() and diagnostic.expected_memo_id is None
    assert diagnostic.owner_kind == "memo_request" and diagnostic.owner_ordinal == 1 and diagnostic.family == "chunk"
    assert _native_monitor_state() == before and sys.getprofile() is prior


@pytest.mark.asyncio
@pytest.mark.parametrize("carrier", ["correct", "missing", "different", "unobserved"])
async def test_native_byte_late_chunk_binding_keeps_actual_worker_and_submission(monkeypatch, request, carrier):
    payload = b'{"a":"caf\xc3\xa9"}'
    chunk = codec.encode_private_chunk(codec="composer.messages.v1", canonical=payload.decode("utf-8"))
    original_scope = clock.retention_stage_memo_scope
    if carrier == "missing":
        monkeypatch.setattr(async_workers, "prepare_retention_sql_callable", partial)
    before, prior = _native_monitor_state(), sys.getprofile()
    trace = RetentionMemoTrace(monkeypatch, request, require_native_bytes=True)
    ticket = publication_ticket()

    def verifies():
        for _ in range(2):
            assert _VERIFY(chunk.reference, chunk.canonical) is None
        return "original worker result"

    def work():
        if carrier in ("different", "unobserved"):
            scope = clock.retention_stage_memo_scope if carrier == "different" else original_scope
            with scope():
                return verifies()
        return verifies()

    with ThreadPoolExecutor(max_workers=1) as pool:
        monkeypatch.setattr(async_workers, "_SHARED_EXECUTOR", pool)
        worker_prior = pool.submit(sys.getprofile).result()
        with clock.retention_stage_memo_scope():
            outcome = await async_workers.run_required_sql_finish_once(ticket, work)
            assert isinstance(outcome, RequiredSQLReturned) and outcome.value == "original worker result"
        assert pool.submit(sys.getprofile).result() is worker_prior
    assert ticket.complete and async_workers.outstanding_admissions() == 0
    if carrier == "correct":
        trace.finish(stages=1, require_snapshot=False, native_byte_limits=(_late_chunk_budget(chunk, len(payload)),))
    else:
        failure = (
            "target work lacked its actual stage carrier"
            if carrier == "missing"
            else "target work carried an unobserved stage"
            if carrier == "unobserved"
            else "native byte provenance invalid"
        )
        with pytest.raises(AssertionError, match=failure):
            trace.finish(stages=2 if carrier == "different" else 1, require_snapshot=False, require_population=False)
    assert not trace._byte_pending_parses and _native_monitor_state() == before and sys.getprofile() is prior


def test_native_byte_refusal_diagnostic_retains_only_bounded_source_and_carrier(monkeypatch, request, record_property):
    from elspeth.contracts.hashing import canonical_json

    trace = RetentionMemoTrace(monkeypatch, request, require_native_bytes=True)
    with clock.retention_stage_memo_scope():
        for _ in range(12):
            assert canonical_json({"safe": "PRIVATE-BYTE-DIAGNOSTIC-CANARY"})
    with pytest.raises(AssertionError, match="native byte provenance invalid"):
        trace.finish(stages=1, require_snapshot=False, require_population=False)
    diagnostics = [asdict(item) for item in trace.byte_error_diagnostics]
    assert len(trace.byte_errors) == 24 and len(diagnostics) == 2
    assert [item["message"] for item in diagnostics] == [
        "canonical work lacks its complete DATA owner",
        "RFC8785 work lacks its original canonical DATA owner",
    ]
    assert [item["error_index"] for item in diagnostics] == [1, 2]
    for item in diagnostics:
        assert item["stage"] == 1 and item["thread_id"] == threading.get_ident() and item["expected_memo_id"] is None
        assert item["memo_id"] == id(trace.memos[0]) and item["memo_active"] is True
        assert item["owner_kind"] is item["owner_ordinal"] is item["family"] is None
        assert 1 <= len(item["ancestry"]) <= 64 and type(item["ancestry_truncated"]) is bool
        assert set(item["ancestry"][0]) == {"filename", "qualname", "first_line", "current_line"}
    assert diagnostics[0]["ancestry"][0]["qualname"] == "canonical_json"
    assert diagnostics[1]["ancestry"][1]["qualname"] == "canonical_json"
    encoded = json.dumps(diagnostics)
    assert "PRIVATE-BYTE-DIAGNOSTIC-CANARY" not in encoded and "f_locals" not in encoded
    record_property(
        "native_byte_diagnostic_expected",
        json.dumps(
            {
                "error_count": 24,
                "messages": [
                    "canonical work lacks its complete DATA owner",
                    "RFC8785 work lacks its original canonical DATA owner",
                ],
                "first_qualnames": ["canonical_json", "dumps"],
            }
        ),
    )


def _snapshot_artifact_fixture(variation="ascii"):
    wire = codec.encode_continuation_snapshot(initial_stack()).replace("café", "cafe")
    assert wire.isascii()
    payload = wire.encode("ascii")
    assert payload.count(b"cafe") == 1
    if variation == "unicode":
        payload = payload.replace(b"cafe", b"caf\xc3\xa9\xe2\x98\x83")
    elif variation == "escaped":
        payload = payload.replace(b"cafe", b"caf\\u00e9")
    elif variation == "whitespace":
        payload = b" \n" + payload + b"\n "
    encoded = payload.decode("utf-8")
    expected = _DECODE(encoded)
    return payload, encoded, expected


def _snapshot_artifact_expected(size, *, requests=2, work=1):
    basis = "post_observation_source_utf8_extent"
    return {
        "rows": [
            {
                "stage": 1,
                "group": 1,
                "producer": "_decode_continuation_snapshot_uncached",
                "basis": basis,
                "requests": requests,
                "work_attempts": work,
                "source_utf8_extent": size,
                "processed_source_utf8_extent": work * size,
            }
        ],
        "stages": [
            {
                "stage": 1,
                "basis": basis,
                "distinct_inputs": 1,
                "requests": requests,
                "work_attempts": work,
                "observed_work_attempts": work,
                "numerator": work * size,
                "denominator": size,
            }
        ],
    }


@pytest.mark.parametrize("variation", ["ascii", "unicode", "escaped", "whitespace"])
def test_snapshot_artifact_extent_is_exact_source_sized_once_after_observation(monkeypatch, request, record_property, variation):
    from tests.helpers import continuation_memo as accounting

    payload, encoded, expected = _snapshot_artifact_fixture(variation)
    canonical = codec.encode_continuation_snapshot(expected).encode("utf-8")
    if variation in ("escaped", "whitespace"):
        assert len(payload) != len(canonical)
    before, prior = _native_monitor_state(), sys.getprofile()
    size = accounting._snapshot_source_utf8_extent
    sized = []

    def checked_source(text):
        assert trace._closed and not trace._installed and sys.getprofile() is prior
        assert trace._byte_monitor is None
        assert any(record.key.encoded is text for record in trace.records if record.family == "snapshot")
        sized.append(id(text))
        return size(text)

    monkeypatch.setattr(accounting, "_snapshot_source_utf8_extent", checked_source)
    trace = RetentionMemoTrace(monkeypatch, request, require_snapshot_artifacts=True)
    with clock.retention_stage_memo_scope():
        assert _DECODE(encoded) == expected
        assert _DECODE(encoded) == expected
        assert not sized
    trace.finish(stages=1, snapshot_artifact_budget=True)
    assert sized == [id(encoded)] and trace.memos[0].cached_entry_count == 0
    actual = {
        "rows": [asdict(row) for row in trace.snapshot_artifact_totals()],
        "stages": [asdict(row) for row in trace.snapshot_artifact_stage_totals()],
    }
    assert actual == _snapshot_artifact_expected(len(payload))
    assert trace._snapshot_artifact_verified and not trace.snapshot_artifact_errors
    assert not trace.require_native_bytes and trace.native_byte_totals() == ()
    assert [record.snapshot_artifact_attempts for record in trace.records] == [1, 0]
    assert not trace._snapshot_artifact_requests and _native_monitor_state() == before and sys.getprofile() is prior
    exported = json.dumps(actual)
    assert encoded not in exported and "cafe" not in exported and "café" not in exported
    record_property("snapshot_artifact_expected", json.dumps(_snapshot_artifact_expected(len(payload))))


def test_snapshot_artifact_distinct_complete_input_adds_actual_work_and_extent(monkeypatch, request):
    payload, encoded, expected = _snapshot_artifact_fixture()
    assert encoded.count('"cursor":0') == 1
    different = encoded.replace('"cursor":0', '"cursor":1')
    different_expected = _DECODE(different)
    trace = RetentionMemoTrace(monkeypatch, request, require_snapshot_artifacts=True)
    with clock.retention_stage_memo_scope():
        assert _DECODE(encoded) == expected
        assert _DECODE(encoded) == expected
        assert _DECODE(different) == different_expected
    trace.finish(stages=1, snapshot_artifact_budget=True)
    rows = trace.snapshot_artifact_totals()
    assert [row.requests for row in rows] == [2, 1] and [row.work_attempts for row in rows] == [1, 1]
    total = trace.snapshot_artifact_stage_totals()[0]
    assert total.distinct_inputs == total.work_attempts == total.observed_work_attempts == 2
    assert total.numerator == total.denominator == 2 * len(payload)


def test_snapshot_artifact_same_source_is_charged_again_in_a_fresh_stage(monkeypatch, request):
    payload, encoded, expected = _snapshot_artifact_fixture("unicode")
    trace = RetentionMemoTrace(monkeypatch, request, require_snapshot_artifacts=True)
    for _ in range(2):
        with clock.retention_stage_memo_scope():
            assert _DECODE(encoded) == expected and _DECODE(encoded) == expected
    trace.finish(stages=2, snapshot_artifact_budget=True)
    rows = trace.snapshot_artifact_totals()
    assert [row.stage for row in rows] == [1, 2]
    assert [row.requests for row in rows] == [2, 2]
    assert [row.work_attempts for row in rows] == [1, 1]
    for total in trace.snapshot_artifact_stage_totals():
        assert total.distinct_inputs == total.work_attempts == total.observed_work_attempts == 1
        assert total.numerator == total.denominator == len(payload)
    assert all(not memo.active and memo.cached_entry_count == 0 for memo in trace.memos)


@pytest.mark.parametrize("variation", ["extra", "eager"])
def test_snapshot_artifact_real_extra_compute_exceeds_work_and_weighted_bounds(monkeypatch, request, variation):
    payload, encoded, expected = _snapshot_artifact_fixture("whitespace")
    lookup = RetentionStageMemo._lookup

    def extra(memo, entries, key, compute):
        result = lookup(memo, entries, key, compute)
        if entries is memo._snapshots:
            if variation == "eager":
                entries.setdefault(key, compute())
            else:
                compute()
        return result

    monkeypatch.setattr(RetentionStageMemo, "_lookup", extra)
    trace = RetentionMemoTrace(monkeypatch, request, require_snapshot_artifacts=True)
    with clock.retention_stage_memo_scope():
        assert _DECODE(encoded) == expected and _DECODE(encoded) == expected
    with pytest.raises(AssertionError, match="snapshot artifact work budget exceeded"):
        trace.finish(stages=1, snapshot_artifact_budget=True)
    with pytest.raises(AssertionError, match="snapshot artifact weighted source extent budget exceeded"):
        trace._assert_snapshot_artifact_byte_ratio()
    row = trace.snapshot_artifact_totals()[0]
    assert row.requests == 2 and row.work_attempts == 3
    assert row.processed_source_utf8_extent == 3 * len(payload)
    total = trace.snapshot_artifact_stage_totals()[0]
    assert total.numerator == 3 * total.denominator and not trace._snapshot_artifact_verified


@pytest.mark.parametrize("variation", ["key-copy", "wrong-input", "wrong-producer", "unwrapped", "canonical-global", "policy-global"])
def test_snapshot_artifact_wrong_source_cannot_borrow_complete_key(monkeypatch, request, variation):
    from elspeth.web import continuation_retention_memo as memo_module

    _payload, encoded, expected = _snapshot_artifact_fixture()
    lookup, original_value = RetentionStageMemo._lookup, memo_module._snapshot_value

    def changed(memo, entries, key, compute):
        if entries is not memo._snapshots:
            return lookup(memo, entries, key, compute)
        producer, source = key
        if variation == "key-copy":
            return original_value(replace(source), producer)
        if variation == "wrong-input":
            return original_value(replace(source, encoded=" \n" + source.encoded), producer)
        if variation == "wrong-producer":
            return original_value(source, lambda text: producer(text))
        return lookup(memo, entries, key, compute)

    monkeypatch.setattr(RetentionStageMemo, "_lookup", changed)
    if variation == "canonical-global":
        original = codec.canonical_json
        monkeypatch.setattr(codec, "canonical_json", lambda value: original(value))
    elif variation == "policy-global":
        from elspeth.web.composer import continuation_policy

        monkeypatch.setattr(continuation_policy, "decode_continuation_policy", lambda *_args: None)
    trace = RetentionMemoTrace(monkeypatch, request, require_snapshot_artifacts=True)
    with clock.retention_stage_memo_scope():
        assert _DECODE(encoded) == expected and _DECODE(encoded) == expected
        if variation == "unwrapped":
            assert _DECODE_WORK(encoded) == expected
    with pytest.raises(AssertionError, match="snapshot artifact provenance invalid"):
        trace.finish(stages=1, require_population=False, snapshot_artifact_budget=True)
    assert trace.snapshot_artifact_errors and not trace._snapshot_artifact_verified and not trace._snapshot_artifact_requests


def test_snapshot_artifact_new_bad_policy_input_keeps_original_G5_refusal(monkeypatch, request):
    payload, encoded, expected = _snapshot_artifact_fixture()
    assert encoded.count(expected.policy_hash) == 1
    invalid = encoded.replace(expected.policy_hash, "0" * 64)
    trace = RetentionMemoTrace(monkeypatch, request, require_snapshot_artifacts=True)
    with clock.retention_stage_memo_scope():
        assert _DECODE(encoded) == expected and _DECODE(encoded) == expected
        with pytest.raises(AuditIntegrityError, match="continuation policy hash mismatch"):
            _DECODE(invalid)
    trace.finish(stages=1, snapshot_artifact_budget=True)
    assert [row.work_attempts for row in trace.snapshot_artifact_totals()] == [1, 1]
    assert trace.snapshot_artifact_stage_totals()[0].numerator == 2 * len(payload)


def test_snapshot_artifact_empty_observation_cannot_qualify_zero_over_zero(monkeypatch, request):
    trace = RetentionMemoTrace(monkeypatch, request, require_snapshot_artifacts=True)
    with clock.retention_stage_memo_scope():
        pass
    with pytest.raises(AssertionError, match="snapshot artifact inputs must be nonempty"):
        trace.finish(stages=1, require_snapshot=False, require_population=False, snapshot_artifact_budget=True)
    assert not trace.snapshot_artifact_totals() and not trace._snapshot_artifact_verified
    total = trace.snapshot_artifact_stage_totals()[0]
    assert total.numerator == total.denominator == total.work_attempts == 0


def test_snapshot_artifact_warm_hit_still_requires_original_source_globals(monkeypatch, request):
    _payload, encoded, expected = _snapshot_artifact_fixture()
    canonical = codec.canonical_json
    trace = RetentionMemoTrace(monkeypatch, request, require_snapshot_artifacts=True)
    with clock.retention_stage_memo_scope():
        assert _DECODE(encoded) == expected
        monkeypatch.setattr(codec, "canonical_json", lambda value: canonical(value))
        assert _DECODE(encoded) == expected
    with pytest.raises(AssertionError, match="snapshot artifact provenance invalid"):
        trace.finish(stages=1, snapshot_artifact_budget=True)
    assert sum(row.work_attempts for row in trace.snapshot_artifact_totals()) == 1
    assert trace.snapshot_artifact_stage_totals()[0].requests == 2
    assert not trace._snapshot_artifact_verified


@pytest.mark.parametrize(
    "variation", ["omitted-assertion", "open-observation", "active-stage", "missing-metric", "universal-native", "legacy"]
)
def test_snapshot_artifact_qualification_is_explicit_and_independent(monkeypatch, request, variation):
    _payload, encoded, expected = _snapshot_artifact_fixture()
    if variation == "missing-metric":
        monkeypatch.setattr(RetentionMemoTrace, "_size_snapshot_artifacts", lambda _self: None)
    trace = RetentionMemoTrace(
        monkeypatch, request, require_snapshot_artifacts=variation != "legacy", require_native_bytes=variation == "universal-native"
    )
    with clock.retention_stage_memo_scope():
        assert _DECODE(encoded) == expected and _DECODE(encoded) == expected
        if variation == "active-stage":
            trace.close()
        if variation in ("open-observation", "active-stage"):
            with pytest.raises(AssertionError, match="snapshot artifact metric requires closed observation"):
                trace._assert_snapshot_artifact_budget(True)
    if variation in ("open-observation", "active-stage"):
        trace.finish(stages=1, snapshot_artifact_budget=True)
    else:
        message = (
            "snapshot artifact budget assertion is required"
            if variation == "omitted-assertion"
            else "snapshot artifact metric is missing"
            if variation == "missing-metric"
            else "native byte provenance invalid"
            if variation == "universal-native"
            else "snapshot artifact budgets require explicit observation mode"
        )
        with pytest.raises(AssertionError, match=message):
            trace.finish(stages=1, snapshot_artifact_budget=variation != "omitted-assertion")
        assert not trace._snapshot_artifact_verified
    if variation == "legacy":
        assert trace.snapshot_artifact_totals() == trace.snapshot_artifact_stage_totals() == ()


@pytest.mark.asyncio
@pytest.mark.parametrize("carrier", ["correct", "missing", "different", "unobserved"])
async def test_snapshot_artifact_body_work_binds_physical_worker_and_actual_carrier(monkeypatch, request, carrier):
    _payload, encoded, expected = _snapshot_artifact_fixture()
    original_scope = clock.retention_stage_memo_scope
    if carrier == "missing":
        monkeypatch.setattr(async_workers, "prepare_retention_sql_callable", partial)
    prior, before = sys.getprofile(), _native_monitor_state()
    trace = RetentionMemoTrace(monkeypatch, request, require_snapshot_artifacts=True)
    ticket = publication_ticket()

    def reads():
        assert _DECODE(encoded) == expected and _DECODE(encoded) == expected
        return "original snapshot worker result"

    def work():
        if carrier in ("different", "unobserved"):
            scope = clock.retention_stage_memo_scope if carrier == "different" else original_scope
            with scope():
                return reads()
        return reads()

    with ThreadPoolExecutor(max_workers=1) as pool:
        monkeypatch.setattr(async_workers, "_SHARED_EXECUTOR", pool)
        worker_prior = pool.submit(sys.getprofile).result()
        with clock.retention_stage_memo_scope():
            outcome = await async_workers.run_required_sql_finish_once(ticket, work)
            assert isinstance(outcome, RequiredSQLReturned) and outcome.value == "original snapshot worker result"
        assert pool.submit(sys.getprofile).result() is worker_prior
    assert ticket.complete and async_workers.outstanding_admissions() == 0
    if carrier == "correct":
        trace.finish(stages=1, snapshot_artifact_budget=True)
    else:
        message = (
            "target work lacked its actual stage carrier"
            if carrier == "missing"
            else "target work carried an unobserved stage"
            if carrier == "unobserved"
            else "snapshot artifact provenance invalid"
        )
        with pytest.raises(AssertionError, match=message):
            trace.finish(stages=2 if carrier == "different" else 1, require_population=False, snapshot_artifact_budget=True)
    assert not trace._snapshot_artifact_requests and sys.getprofile() is prior and _native_monitor_state() == before


@pytest.mark.parametrize("marker_type", [None, RuntimeError, KeyboardInterrupt, SystemExit, asyncio.CancelledError])
def test_snapshot_artifact_sizing_failure_keeps_original_native_marker(monkeypatch, request, marker_type):
    from tests.helpers import continuation_memo as accounting

    _payload, encoded, expected = _snapshot_artifact_fixture()
    marker = None if marker_type is None else marker_type("original snapshot artifact marker")
    lookup = RetentionStageMemo._lookup

    def native_marker(memo, entries, key, compute):
        result = lookup(memo, entries, key, compute)
        if entries is memo._snapshots:
            assert result == expected
            if marker is not None:
                raise marker
        return result

    def failed_size(_text):
        assert trace._closed and not trace._installed
        raise UnicodeEncodeError("utf-8", "fixture", 0, 1, "controlled source sizing refusal")

    monkeypatch.setattr(RetentionStageMemo, "_lookup", native_marker)
    monkeypatch.setattr(accounting, "_snapshot_source_utf8_extent", failed_size)
    trace = RetentionMemoTrace(monkeypatch, request, require_snapshot_artifacts=True)
    if marker_type is None:
        with clock.retention_stage_memo_scope():
            assert _DECODE(encoded) == expected
        with pytest.raises(AssertionError, match="snapshot artifact provenance invalid"):
            trace.finish(stages=1, require_snapshot=False, snapshot_artifact_budget=True)
    else:
        with pytest.raises(marker_type) as caught:
            try:
                with clock.retention_stage_memo_scope():
                    _DECODE(encoded)
            finally:
                trace.finish(stages=1, require_snapshot=False, snapshot_artifact_budget=True)
        assert caught.value is marker
    assert trace._closed and not trace._snapshot_artifact_verified
    assert trace.snapshot_artifact_errors and not trace._snapshot_artifact_requests
    row = trace.snapshot_artifact_totals()[0]
    assert row.work_attempts == 1 and row.source_utf8_extent is row.processed_source_utf8_extent is None


# Module import precedes the capture plugin's collection-finish wrapper. Direct
# assertions call the genuine original; registered teardown still uses capture.
_FINALIZE_MEMO_TRACE = RetentionMemoTrace._finalize


@pytest.mark.parametrize("warm", [False, True])
def test_snapshot_artifact_terminal_qualification_requires_complete_finish(monkeypatch, request, record_property, warm):
    payload, encoded, expected = _snapshot_artifact_fixture("whitespace")
    trace = RetentionMemoTrace(monkeypatch, request, require_snapshot_artifacts=True)
    original_assert_closed = trace.assert_closed
    original_failures = []

    def observe_original_assertion(**kwargs):
        try:
            original_assert_closed(**kwargs)
        except BaseException as error:
            assert trace._snapshot_artifact_verified is True
            original_failures.append(error)
            raise

    monkeypatch.setattr(trace, "assert_closed", observe_original_assertion)
    with clock.retention_stage_memo_scope():
        assert _DECODE(encoded) == expected
        if warm:
            assert _DECODE(encoded) == expected
    if warm:
        trace.finish(stages=1, snapshot_artifact_budget=True)
        assert not original_failures
    else:
        with pytest.raises(AssertionError, match="actual retention must exercise a warm hit") as caught:
            trace.finish(stages=1, snapshot_artifact_budget=True)
        assert len(original_failures) == 1 and caught.value is original_failures[0]
    assert trace._finished and trace.snapshot_artifact_closed() and trace.snapshot_artifact_complete()
    assert trace._snapshot_artifact_verified is warm and trace._terminal_snapshot_artifact_verified is warm
    metric = {
        "rows": [asdict(row) for row in trace.snapshot_artifact_totals()],
        "stages": [asdict(row) for row in trace.snapshot_artifact_stage_totals()],
    }
    independent = _snapshot_artifact_expected(len(payload), requests=2 if warm else 1)
    assert metric == independent
    _FINALIZE_MEMO_TRACE(trace)
    record_property("snapshot_artifact_terminal_expected", json.dumps({**independent, "verified": warm, "complete": True}))


def test_snapshot_artifact_terminal_flag_tamper_is_still_refused(monkeypatch, request):
    _payload, encoded, expected = _snapshot_artifact_fixture()
    trace = RetentionMemoTrace(monkeypatch, request, require_snapshot_artifacts=True)
    with clock.retention_stage_memo_scope():
        assert _DECODE(encoded) == expected and _DECODE(encoded) == expected
    trace.finish(stages=1, snapshot_artifact_budget=True)
    assert trace._snapshot_artifact_verified is trace._terminal_snapshot_artifact_verified is True
    trace._snapshot_artifact_verified = False
    try:
        with pytest.raises(AssertionError, match="snapshot artifact qualification changed after terminal assertion"):
            _FINALIZE_MEMO_TRACE(trace)
    finally:
        trace._snapshot_artifact_verified = True
    _FINALIZE_MEMO_TRACE(trace)


def _chunk_artifact_fixture(variation="ascii"):
    from hashlib import sha256

    payload = {
        "ascii": b'{"topic":"cafe"}',
        "unicode": b'{"topic":"caf\xc3\xa9\xe2\x98\x83"}',
        "empty": b"{}",
    }[variation]
    reference = codec.PrivateChunkRef.model_validate(
        {"codec": "composer.messages.v1", "digest": sha256(payload).hexdigest(), "byte_length": len(payload)}
    )
    return payload, codec.PrivateChunk(reference, payload.decode("utf-8"))


def _chunk_artifact_expected(size, *, requests=2, work=1):
    return {
        "rows": [
            {
                "stage": 1,
                "group": 1,
                "producer": "_verify_private_chunk_canonical",
                "basis": "post_observation_source_utf8_extent",
                "requests": requests,
                "work_attempts": work,
                "source_utf8_extent": size,
                "processed_source_utf8_extent": work * size,
            }
        ],
        "stages": [
            {
                "stage": 1,
                "basis": "post_observation_source_utf8_extent",
                "distinct_inputs": 1,
                "requests": requests,
                "work_attempts": work,
                "observed_work_attempts": work,
                "numerator": work * size,
                "denominator": size,
            }
        ],
    }


def _actual_chunk_artifact_metric(trace):
    return {
        "rows": [asdict(row) for row in trace.chunk_artifact_totals()],
        "stages": [asdict(row) for row in trace.chunk_artifact_stage_totals()],
    }


@pytest.mark.parametrize(
    "variation,native",
    [
        pytest.param("ascii", False, id="ascii-work"),
        pytest.param("unicode", False, id="unicode-work"),
        pytest.param("empty", False, id="empty-work"),
        pytest.param("ascii", True, id="ascii-native"),
        pytest.param("unicode", True, id="unicode-native"),
        pytest.param("empty", True, id="empty-native"),
    ],
)
def test_chunk_artifact_actual_complete_attempt_and_postclose_extent_preserve_each_binding(
    monkeypatch, request, record_property, variation, native
):
    from tests.helpers import continuation_memo as accounting

    payload, chunk = _chunk_artifact_fixture(variation)
    size_source = accounting._chunk_source_utf8_extent
    sized = []
    prior, monitor = sys.getprofile(), _native_monitor_state()

    def after_close(text):
        assert trace.chunk_artifact_closed() and sys.getprofile() is prior
        assert trace._byte_monitor is None or trace._byte_monitor.closed
        assert all(not memo.active and memo.cached_entry_count == 0 for memo in trace.memos)
        assert text is trace.records[0].key.canonical is chunk.canonical
        sized.append(id(text))
        return size_source(text)

    monkeypatch.setattr(accounting, "_chunk_source_utf8_extent", after_close)
    trace = RetentionMemoTrace(monkeypatch, request, require_chunk_artifacts=True, require_native_bytes=native)
    with clock.retention_stage_memo_scope():
        assert _VERIFY(chunk.reference, chunk.canonical) is None
        assert _VERIFY(chunk.reference, chunk.canonical) is None
        assert not sized
    trace.finish(
        stages=1,
        require_snapshot=False,
        chunk_artifact_budget=True,
        native_byte_limits=(_late_chunk_budget(chunk, len(payload)),) if native else (),
    )
    assert sized == [id(chunk.canonical)]
    assert _actual_chunk_artifact_metric(trace) == _chunk_artifact_expected(len(payload))
    assert [record.chunk_artifact_attempts for record in trace.records] == [1, 0]
    assert [record.attempts["chunk_work"] for record in trace.records] == [1, 0]
    assert all(tuple(record.attempts[name] for name in ("utf8", "length", "sha256")) == (1, 1, 1) for record in trace.records)
    assert trace.chunk_artifact_complete() and trace._chunk_artifact_verified and not trace.chunk_artifact_errors
    assert not trace._chunk_artifact_requests and sys.getprofile() is prior and _native_monitor_state() == monitor
    exported = json.dumps(_actual_chunk_artifact_metric(trace))
    assert chunk.canonical not in exported and "cafe" not in exported and "café" not in exported
    record_property("chunk_artifact_expected", json.dumps(_chunk_artifact_expected(len(payload))))
    if native:
        expected = _native_expected_rows(
            1,
            "chunk",
            1,
            strict_parse=(1, len(payload)),
            canonical=(1, len(payload)),
            chunk_length=(2, 2 * len(payload)),
            chunk_digest=(2, 2 * len(payload)),
        )
        assert [asdict(row) for row in trace.native_byte_totals()] == expected
        record_property("chunk_artifact_native_expected", json.dumps(expected))


def test_chunk_artifact_distinct_full_references_and_fresh_stages_keep_separate_work(monkeypatch, request):
    payload, chunk = _chunk_artifact_fixture()
    other_payload, other_chunk = _chunk_artifact_fixture("unicode")
    other = chunk.reference.model_copy(update={"codec": "composer.control.v1"})
    assert other != chunk.reference
    trace = RetentionMemoTrace(monkeypatch, request, require_chunk_artifacts=True)
    for _ in range(2):
        with clock.retention_stage_memo_scope():
            assert _VERIFY(chunk.reference, chunk.canonical) is None
            assert _VERIFY(chunk.reference.model_copy(), chunk.canonical) is None
            assert _VERIFY(other, chunk.canonical) is None
            assert _VERIFY(other_chunk.reference, other_chunk.canonical) is None
    trace.finish(stages=2, require_snapshot=False, chunk_artifact_budget=True)
    assert [(row.stage, row.requests, row.work_attempts) for row in trace.chunk_artifact_totals()] == [
        (1, 2, 1),
        (1, 1, 1),
        (1, 1, 1),
        (2, 2, 1),
        (2, 1, 1),
        (2, 1, 1),
    ]
    assert all(
        total.numerator == total.denominator == 2 * len(payload) + len(other_payload) for total in trace.chunk_artifact_stage_totals()
    )


@pytest.mark.parametrize("extra", ["repeat", "eager"])
def test_chunk_artifact_extra_real_canonical_work_fails_new_work_and_weighted_guards(monkeypatch, request, extra):
    payload, chunk = _chunk_artifact_fixture("unicode")
    lookup = RetentionStageMemo._lookup

    def changed(memo, entries, key, compute):
        result = lookup(memo, entries, key, compute)
        if entries is memo._chunks:
            if extra == "eager":
                entries.setdefault(key, compute())
            else:
                compute()
        return result

    monkeypatch.setattr(RetentionStageMemo, "_lookup", changed)
    trace = RetentionMemoTrace(monkeypatch, request, require_chunk_artifacts=True)
    with clock.retention_stage_memo_scope():
        assert _VERIFY(chunk.reference, chunk.canonical) is None and _VERIFY(chunk.reference, chunk.canonical) is None
    with pytest.raises(AssertionError, match="chunk artifact work budget exceeded"):
        trace.finish(stages=1, require_snapshot=False, chunk_artifact_budget=True)
    with pytest.raises(AssertionError, match="chunk artifact weighted source extent budget exceeded"):
        trace._assert_chunk_artifact_byte_ratio()
    actual = _actual_chunk_artifact_metric(trace)
    assert actual == _chunk_artifact_expected(len(payload), work=3)
    assert not trace._chunk_artifact_verified


@pytest.mark.parametrize(
    "change",
    [
        "key-copy",
        "reference-copy",
        "wrong-text",
        "producer",
        "unwrapped",
        "query-wrapper",
        "public",
        "canonical",
        "eligibility",
        "body-global",
        "value-global",
        "digest-global",
    ],
)
def test_chunk_artifact_wrong_source_cannot_borrow_an_owned_key(monkeypatch, request, change):
    from elspeth.web import continuation_retention_memo as memo_module

    _payload, chunk = _chunk_artifact_fixture()
    lookup, original_value = RetentionStageMemo._lookup, memo_module._chunk_value

    def changed(memo, entries, key, compute):
        if entries is not memo._chunks:
            return lookup(memo, entries, key, compute)
        producer, source = key
        if change == "key-copy":
            return original_value(replace(source), producer)
        if change == "reference-copy":
            return original_value(replace(source, reference=source.reference.model_copy()), producer)
        if change == "wrong-text":
            return original_value(replace(source, canonical="{}"), producer)
        if change == "producer":
            return original_value(source, lambda text: producer(text))
        return lookup(memo, entries, key, compute)

    monkeypatch.setattr(RetentionStageMemo, "_lookup", changed)
    if change == "query-wrapper":
        query = RetentionStageMemo.private_chunk
        monkeypatch.setattr(RetentionStageMemo, "private_chunk", lambda *args: query(*args))
    elif change == "public":
        monkeypatch.setattr(codec, "verify_private_chunk", lambda *args: _VERIFY(*args))
    elif change == "canonical":
        canonical = codec.canonical_json
        monkeypatch.setattr(codec, "canonical_json", lambda value: canonical(value))
    elif change == "eligibility":
        eligibility = codec._private_chunk_memo_reference
        monkeypatch.setattr(codec, "_private_chunk_memo_reference", lambda value: eligibility(value))
    elif change == "body-global":
        monkeypatch.setattr(codec, "_verify_private_chunk_canonical", lambda text: _CHUNK_WORK(text))
    elif change == "value-global":
        monkeypatch.setattr(memo_module, "_chunk_value", lambda key, producer: original_value(key, producer))
    elif change == "digest-global":
        digest = codec.sha256
        monkeypatch.setattr(codec, "sha256", lambda payload: digest(payload))
    trace = RetentionMemoTrace(monkeypatch, request, require_chunk_artifacts=True)
    with clock.retention_stage_memo_scope():
        assert _VERIFY(chunk.reference, chunk.canonical) is None and _VERIFY(chunk.reference, chunk.canonical) is None
        if change == "unwrapped":
            assert _CHUNK_WORK(chunk.canonical) is None
    with pytest.raises(AssertionError, match="chunk artifact provenance invalid"):
        trace.finish(stages=1, require_snapshot=False, require_population=False, chunk_artifact_budget=True)
    assert trace.chunk_artifact_errors and not trace._chunk_artifact_verified and not trace._chunk_artifact_requests


@pytest.mark.parametrize("depth", [0, 80])
def test_chunk_artifact_real_forwarding_stack_requires_proven_query_ancestry(monkeypatch, request, depth):
    _payload, chunk = _chunk_artifact_fixture()
    lookup = RetentionStageMemo._lookup

    def changed(memo, entries, key, compute):
        def forward(remaining):
            if remaining:
                return forward(remaining - 1)
            return lookup(memo, entries, key, compute)

        return forward(depth) if entries is memo._chunks else lookup(memo, entries, key, compute)

    monkeypatch.setattr(RetentionStageMemo, "_lookup", changed)
    trace = RetentionMemoTrace(monkeypatch, request, require_chunk_artifacts=True)
    with clock.retention_stage_memo_scope():
        assert _VERIFY(chunk.reference, chunk.canonical) is None and _VERIFY(chunk.reference, chunk.canonical) is None
    if depth:
        with pytest.raises(AssertionError, match="chunk artifact provenance invalid"):
            trace.finish(stages=1, require_snapshot=False, chunk_artifact_budget=True)
        assert "chunk artifact verifier lacks its original query owner" in trace.chunk_artifact_errors
        assert sum(row.work_attempts for row in trace.chunk_artifact_totals()) == 0
        assert trace.chunk_artifact_stage_totals()[0].observed_work_attempts == 1
    else:
        trace.finish(stages=1, require_snapshot=False, chunk_artifact_budget=True)


@pytest.mark.parametrize(
    "case", ["noncanonical", "invalid-json", "length", "digest", "malformed-first", "missing-first", "fallback", "foreign"]
)
def test_chunk_artifact_refusal_order_and_actual_attempts_remain_native(monkeypatch, request, case):
    _payload, chunk = _chunk_artifact_fixture()
    reference, text = chunk.reference, chunk.canonical
    if case in ("noncanonical", "malformed-first", "missing-first"):
        text = '{ "topic": "cafe" }'
    elif case == "invalid-json":
        text = "{"
    if case == "length":
        reference = reference.model_copy(update={"byte_length": reference.byte_length + 1})
    elif case == "digest":
        reference = reference.model_copy(update={"digest": "0" * 64})
    elif case in ("malformed-first", "fallback"):
        reference = reference.model_copy(update={"digest": []})
    elif case == "missing-first":
        reference = codec.PrivateChunkRef.model_construct()
    elif case == "foreign":
        reference = object()
    canonical_refusal = case in ("noncanonical", "invalid-json", "malformed-first", "missing-first")
    trace = RetentionMemoTrace(monkeypatch, request, require_chunk_artifacts=True)
    with (
        clock.retention_stage_memo_scope(),
        pytest.raises(
            AttributeError if case == "foreign" else AuditIntegrityError,
            match="byte_length" if case == "foreign" else "canonical strict JSON" if canonical_refusal else "bytes differ",
        ),
    ):
        _VERIFY(reference, text)
    record = trace.records[0]
    assert record.attempts["chunk_work"] == 1
    expected = (0, 0, 0) if canonical_refusal else (1, 1, 0) if case in ("length", "foreign") else (1, 1, 1)
    assert tuple(record.attempts[name] for name in ("utf8", "length", "sha256")) == expected
    if case in ("malformed-first", "missing-first", "fallback", "foreign"):
        with pytest.raises(AssertionError, match="chunk artifact provenance invalid"):
            trace.finish(
                stages=1, require_snapshot=False, require_population=False, require_valid_chunk_bindings=False, chunk_artifact_budget=True
            )
        assert not trace._chunk_artifact_verified and trace.chunk_artifact_stage_totals()[0].observed_work_attempts == 1
    else:
        trace.finish(
            stages=1,
            require_snapshot=False,
            require_population=not canonical_refusal,
            require_valid_chunk_bindings=False,
            chunk_artifact_budget=True,
        )
        assert trace.chunk_artifact_totals()[0].work_attempts == 1


@pytest.mark.parametrize("case", ["omitted", "missing", "open", "active", "legacy", "empty"])
def test_chunk_artifact_qualification_requires_explicit_complete_closed_measurement(monkeypatch, request, case):
    _payload, chunk = _chunk_artifact_fixture()
    if case == "missing":
        monkeypatch.setattr(RetentionMemoTrace, "_size_chunk_artifacts", lambda _self: None)
    trace = RetentionMemoTrace(monkeypatch, request, require_chunk_artifacts=case != "legacy")
    with clock.retention_stage_memo_scope():
        if case != "empty":
            assert _VERIFY(chunk.reference, chunk.canonical) is None and _VERIFY(chunk.reference, chunk.canonical) is None
        if case == "active":
            trace.close()
        if case in ("open", "active"):
            with pytest.raises(AssertionError, match="chunk artifact metric requires closed observation"):
                trace._assert_chunk_artifact_budget(True)
    if case in ("open", "active"):
        trace.finish(stages=1, require_snapshot=False, chunk_artifact_budget=True)
    else:
        message = {
            "omitted": "budget assertion is required",
            "missing": "metric is missing",
            "legacy": "budgets require explicit observation mode",
            "empty": "inputs must be nonempty",
        }[case]
        with pytest.raises(AssertionError, match="chunk artifact " + message):
            trace.finish(stages=1, require_snapshot=False, require_population=case != "empty", chunk_artifact_budget=case != "omitted")
        assert not trace._chunk_artifact_verified


@pytest.mark.asyncio
@pytest.mark.parametrize("carrier", ["correct", "missing", "different", "unobserved"])
async def test_chunk_artifact_actual_required_worker_binds_stage_and_submission(monkeypatch, request, carrier):
    _payload, chunk = _chunk_artifact_fixture()
    original_scope = clock.retention_stage_memo_scope
    if carrier == "missing":
        monkeypatch.setattr(async_workers, "prepare_retention_sql_callable", partial)
    prior, monitor = sys.getprofile(), _native_monitor_state()
    trace = RetentionMemoTrace(monkeypatch, request, require_chunk_artifacts=True)
    ticket = publication_ticket()

    def reads():
        assert _VERIFY(chunk.reference, chunk.canonical) is None and _VERIFY(chunk.reference, chunk.canonical) is None
        return "original chunk worker result"

    def work():
        if carrier in ("different", "unobserved"):
            scope = clock.retention_stage_memo_scope if carrier == "different" else original_scope
            with scope():
                return reads()
        return reads()

    with ThreadPoolExecutor(max_workers=1) as pool:
        monkeypatch.setattr(async_workers, "_SHARED_EXECUTOR", pool)
        worker_prior = pool.submit(sys.getprofile).result()
        with clock.retention_stage_memo_scope():
            result = await async_workers.run_required_sql_finish_once(ticket, work)
            assert isinstance(result, RequiredSQLReturned) and result.value == "original chunk worker result"
        assert pool.submit(sys.getprofile).result() is worker_prior
    assert ticket.complete and async_workers.outstanding_admissions() == 0
    if carrier == "correct":
        trace.finish(stages=1, require_snapshot=False, chunk_artifact_budget=True)
    else:
        message = (
            "target work lacked its actual stage carrier"
            if carrier == "missing"
            else "target work carried an unobserved stage"
            if carrier == "unobserved"
            else "chunk artifact provenance invalid"
        )
        with pytest.raises(AssertionError, match=message):
            trace.finish(
                stages=2 if carrier == "different" else 1, require_snapshot=False, require_population=False, chunk_artifact_budget=True
            )
    assert not trace._chunk_artifact_requests and sys.getprofile() is prior and _native_monitor_state() == monitor


@pytest.mark.parametrize("marker_type", [None, RuntimeError, KeyboardInterrupt, SystemExit, asyncio.CancelledError])
def test_chunk_artifact_sizing_failure_preserves_the_original_native_marker(monkeypatch, request, marker_type):
    from tests.helpers import continuation_memo as accounting

    _payload, chunk = _chunk_artifact_fixture()
    marker = None if marker_type is None else marker_type("original chunk artifact marker")
    lookup = RetentionStageMemo._lookup

    def native_marker(memo, entries, key, compute):
        result = lookup(memo, entries, key, compute)
        if entries is memo._chunks and marker is not None:
            raise marker
        return result

    def unavailable(_text):
        assert trace.chunk_artifact_closed()
        raise UnicodeEncodeError("utf-8", "fixture", 0, 1, "controlled chunk source sizing refusal")

    monkeypatch.setattr(RetentionStageMemo, "_lookup", native_marker)
    monkeypatch.setattr(accounting, "_chunk_source_utf8_extent", unavailable)
    trace = RetentionMemoTrace(monkeypatch, request, require_chunk_artifacts=True)
    if marker_type is None:
        with clock.retention_stage_memo_scope():
            assert _VERIFY(chunk.reference, chunk.canonical) is None
        with pytest.raises(AssertionError, match="chunk artifact provenance invalid"):
            trace.finish(stages=1, require_snapshot=False, chunk_artifact_budget=True)
    else:
        with pytest.raises(marker_type) as caught:
            try:
                with clock.retention_stage_memo_scope():
                    _VERIFY(chunk.reference, chunk.canonical)
            finally:
                trace.finish(stages=1, require_snapshot=False, chunk_artifact_budget=True)
        assert caught.value is marker
    row = trace.chunk_artifact_totals()[0]
    assert row.work_attempts == 1 and row.source_utf8_extent is row.processed_source_utf8_extent is None
    assert not trace._chunk_artifact_verified and not trace._terminal_chunk_artifact_verified


@pytest.mark.parametrize("case", ["success", "later-failure", "tamper"])
def test_chunk_artifact_terminal_truth_and_original_finalizer_export(monkeypatch, request, record_property, case):
    payload, chunk = _chunk_artifact_fixture("unicode")
    trace = RetentionMemoTrace(monkeypatch, request, require_chunk_artifacts=True)
    with clock.retention_stage_memo_scope():
        assert _VERIFY(chunk.reference, chunk.canonical) is None and _VERIFY(chunk.reference, chunk.canonical) is None
    original = trace.assert_closed
    failures = []

    def original_assertion(**kwargs):
        try:
            original(**kwargs)
        except BaseException as error:
            assert trace._chunk_artifact_verified
            failures.append(error)
            raise

    monkeypatch.setattr(trace, "assert_closed", original_assertion)
    if case == "later-failure":
        with pytest.raises(AssertionError, match="actual retention must request snapshot work") as caught:
            trace.finish(stages=1, chunk_artifact_budget=True)
        assert len(failures) == 1 and caught.value is failures[0]
        assert trace._chunk_artifact_verified is trace._terminal_chunk_artifact_verified is False
    else:
        trace.finish(stages=1, require_snapshot=False, chunk_artifact_budget=True)
        assert trace._chunk_artifact_verified is trace._terminal_chunk_artifact_verified is True
    if case == "tamper":
        trace._chunk_artifact_verified = False
        try:
            with pytest.raises(AssertionError, match="chunk artifact qualification changed after terminal assertion"):
                _FINALIZE_MEMO_TRACE(trace)
        finally:
            trace._chunk_artifact_verified = True
    _FINALIZE_MEMO_TRACE(trace)
    assert trace.chunk_artifact_complete() and _actual_chunk_artifact_metric(trace) == _chunk_artifact_expected(len(payload))
    record_property(
        "chunk_artifact_terminal_expected",
        json.dumps({**_chunk_artifact_expected(len(payload)), "verified": case != "later-failure", "complete": True}),
    )


@pytest.mark.parametrize("missing", [False, True])
def test_chunk_and_snapshot_artifacts_are_distinct_complete_owners(monkeypatch, request, record_property, missing):
    snapshot_bytes, encoded, expected = _snapshot_artifact_fixture("whitespace")
    chunk_bytes, chunk = _chunk_artifact_fixture("empty")
    trace = RetentionMemoTrace(monkeypatch, request, require_snapshot_artifacts=True, require_chunk_artifacts=True)
    with clock.retention_stage_memo_scope():
        assert _DECODE(encoded) == expected and _DECODE(encoded) == expected
        assert _VERIFY(chunk.reference, chunk.canonical) is None and _VERIFY(chunk.reference, chunk.canonical) is None
    if missing:
        with pytest.raises(AssertionError, match="chunk artifact budget assertion is required"):
            trace.finish(stages=1, snapshot_artifact_budget=True)
        assert not trace._snapshot_artifact_verified and not trace._chunk_artifact_verified
        assert not trace._terminal_snapshot_artifact_verified and not trace._terminal_chunk_artifact_verified
    else:
        trace.finish(stages=1, snapshot_artifact_budget=True, chunk_artifact_budget=True)
        assert trace._snapshot_artifact_verified and trace._chunk_artifact_verified
        record_property(
            "chunk_artifact_mixed_expected",
            json.dumps({"snapshot": _snapshot_artifact_expected(len(snapshot_bytes)), "chunk": _chunk_artifact_expected(len(chunk_bytes))}),
        )
    assert trace.snapshot_artifact_stage_totals()[0].denominator == len(snapshot_bytes)
    assert trace.chunk_artifact_stage_totals()[0].denominator == len(chunk_bytes)


@pytest.mark.parametrize("change", ["canonical", "length", "digest"])
def test_chunk_artifact_warm_hit_preserves_source_and_fresh_binding_refusal(monkeypatch, request, change):
    _payload, chunk = _chunk_artifact_fixture()
    trace = RetentionMemoTrace(monkeypatch, request, require_chunk_artifacts=True)
    with clock.retention_stage_memo_scope():
        assert _VERIFY(chunk.reference, chunk.canonical) is None and _VERIFY(chunk.reference, chunk.canonical) is None
        if change == "canonical":
            original = codec.canonical_json
            monkeypatch.setattr(codec, "canonical_json", lambda value: original(value))
            assert _VERIFY(chunk.reference, chunk.canonical) is None
        else:
            changed = chunk.reference.model_copy(
                update={"byte_length": chunk.reference.byte_length + 1} if change == "length" else {"digest": "0" * 64}
            )
            with pytest.raises(AuditIntegrityError, match="private chunk bytes differ from their binding"):
                _VERIFY(changed, chunk.canonical)
    assert all(record.attempts["utf8"] == record.attempts["length"] == 1 for record in trace.records)
    assert trace.records[-1].attempts["sha256"] == (0 if change == "length" else 1)
    if change == "canonical":
        with pytest.raises(AssertionError, match="chunk artifact provenance invalid"):
            trace.finish(stages=1, require_snapshot=False, chunk_artifact_budget=True)
        assert sum(row.work_attempts for row in trace.chunk_artifact_totals()) == 1
    else:
        trace.finish(stages=1, require_snapshot=False, require_valid_chunk_bindings=False, chunk_artifact_budget=True)
        assert [row.work_attempts for row in trace.chunk_artifact_totals()] == [1, 1]
        assert [record.chunk_artifact_attempts for record in trace.records] == [1, 0, 1]


def _catalog_schema_fixture():
    from elspeth.plugins.infrastructure.discovery import create_dynamic_hookimpl
    from elspeth.plugins.infrastructure.manager import PluginManager
    from elspeth.plugins.sinks.csv_sink import CSVSink
    from elspeth.plugins.sources.csv_source import CSVSource
    from elspeth.plugins.transforms.passthrough import PassThrough
    from elspeth.web.catalog.service import CatalogServiceImpl

    manager = PluginManager()
    manager.register(create_dynamic_hookimpl([CSVSource], "elspeth_get_source"))
    manager.register(create_dynamic_hookimpl([PassThrough], "elspeth_get_transforms"))
    manager.register(create_dynamic_hookimpl([CSVSink], "elspeth_get_sinks"))
    assert manager.get_sources() == [CSVSource]
    assert manager.get_transforms() == [PassThrough]
    assert manager.get_sinks() == [CSVSink]
    catalog = CatalogServiceImpl(manager)
    plugin = next(plugin for plugin in catalog._source_classes if plugin.name == "csv")
    count = sum(len(group) for group in (catalog._source_classes, catalog._transform_classes, catalog._sink_classes))
    return catalog, plugin, count


def _catalog_schema_expected(*, factory=0, plugin=0, reads=0):
    return [
        {
            "stage": 1,
            "factory_attempts": factory,
            "plugin_attempts": plugin,
            "read_attempts": reads,
            "attributed_factory_attempts": factory,
            "attributed_plugin_attempts": plugin,
            "attributed_read_attempts": reads,
        }
    ]


def _catalog_schema_facts(trace, binding_count, rows, *, verified, complete=True):
    assert [asdict(row) for row in trace.catalog_schema_totals()] == rows
    assert len(trace._catalog_schema_bindings) == binding_count
    assert trace._catalog_schema_verified is verified and trace.catalog_schema_complete() is complete
    return {
        "rows": rows,
        "binding_count": binding_count,
        "verified": verified,
        "complete": complete,
        "checked_stages": [1],
        "unassigned": {"factory": 0, "plugin": 0, "read": 0},
    }


def test_catalog_schema_zero_budget_keeps_real_views_and_copy_isolation(monkeypatch, request, record_property):
    catalog, _plugin, count = _catalog_schema_fixture()
    sources, transforms, sinks = catalog.list_sources(), catalog.list_transforms(), catalog.list_sinks()
    expected_schema = catalog.get_schema("source", "csv")
    prior, monitor = sys.getprofile(), _native_monitor_state()
    trace = RetentionMemoTrace(monkeypatch, request, require_catalog_schema=True, catalog=catalog)
    with clock.retention_stage_memo_scope():
        assert catalog.list_sources() == sources and catalog.list_transforms() == transforms and catalog.list_sinks() == sinks
        handed = catalog.get_schema("source", "csv")
        handed.json_schema["_caller_injected"] = True
        assert catalog.get_schema("source", "csv") == expected_schema
    trace.finish(stages=1, require_snapshot=False, require_population=False, catalog_schema_budget=True)
    assert sys.getprofile() is prior and _native_monitor_state() == monitor
    assert trace.catalog_schema_checked_stages() == (1,) and not trace._catalog_schema_frames and not trace._catalog_schema_scopes
    facts = _catalog_schema_facts(trace, count, _catalog_schema_expected(reads=5), verified=True)
    record_property("catalog_schema_expected", json.dumps(facts))


@pytest.mark.parametrize("work", ["factory", "alias", "alias-transform", "alias-sink", "raised-factory", "raised-alias"])
def test_catalog_schema_actual_original_work_invalidates_zero_including_raises(monkeypatch, request, record_property, work):
    catalog, plugin, count = _catalog_schema_fixture()
    if work == "alias-transform":
        plugin = catalog._transform_classes[0]
    elif work == "alias-sink":
        plugin = catalog._sink_classes[0]
    marker = RuntimeError("original schema production marker")
    if work.startswith("raised"):

        def failed_model(_cls):
            raise marker

        monkeypatch.setattr(plugin.config_model, "model_json_schema", classmethod(failed_model))
    alias = plugin.get_config_schema
    trace = RetentionMemoTrace(monkeypatch, request, require_catalog_schema=True, catalog=catalog)
    with clock.retention_stage_memo_scope():
        if work.startswith("raised"):
            with pytest.raises(RuntimeError) as caught:
                if work.endswith("factory"):
                    catalog._catalog_schema(plugin, "source")
                else:
                    alias()
            assert caught.value is marker
        elif work == "factory":
            assert type(catalog._catalog_schema(plugin, "source")) is dict
        else:
            assert type(alias()) is dict
    with pytest.raises(AssertionError, match="catalog schema construction budget exceeded"):
        trace.finish(stages=1, require_snapshot=False, require_population=False, catalog_schema_budget=True)
    facts = _catalog_schema_facts(trace, count, _catalog_schema_expected(factory=int(work.endswith("factory")), plugin=1), verified=False)
    record_property("catalog_schema_expected", json.dumps(facts))


@pytest.mark.parametrize("change", ["factory", "descriptor", "code", "classes", "registry", "owner", "class-alias"])
def test_catalog_schema_source_or_owner_loss_cannot_qualify_a_false_zero(monkeypatch, request, record_property, change):
    catalog, plugin, _count = _catalog_schema_fixture()
    other = _catalog_schema_fixture()[0] if change == "owner" else None
    defining = next(parent for parent in plugin.__mro__ if "get_config_schema" in parent.__dict__)
    descriptor = defining.__dict__["get_config_schema"]
    alias = plugin.get_config_schema
    trace = RetentionMemoTrace(monkeypatch, request, require_catalog_schema=True, catalog=catalog)
    with clock.retention_stage_memo_scope():
        if change == "owner":
            assert other._catalog_schema(plugin, "source")
        elif change == "class-alias":
            with pytest.raises(AttributeError, match="config_model"):
                alias.__func__(object)
        else:
            with monkeypatch.context() as changed:
                if change == "factory":
                    changed.setattr(type(catalog), "_catalog_schema", lambda *_args: {})
                    assert catalog._catalog_schema(plugin, "source") == {}
                elif change == "descriptor":
                    changed.setattr(defining, "get_config_schema", classmethod(lambda _cls: {}))
                    assert plugin.get_config_schema() == {}
                elif change == "code":

                    def replaced(_cls):
                        return {}

                    changed.setattr(descriptor.__func__, "__code__", replaced.__code__)
                    assert plugin.get_config_schema() == {}
                elif change == "registry":
                    changed.setattr(catalog._pm, "_sources", {})
                    assert catalog.get_schema("source", "csv")
                else:
                    changed.setattr(catalog, "_source_classes", [])
                    assert catalog.list_sources() == []
    with pytest.raises(AssertionError, match="catalog schema provenance invalid"):
        trace.finish(stages=1, require_snapshot=False, require_population=False, catalog_schema_budget=True)
    assert not trace._catalog_schema_verified and trace._catalog_schema_error_count > 0
    if change in ("factory", "descriptor", "code", "classes", "registry"):
        assert not trace._catalog_schema_source_valid
        assert all(row.factory_attempts == row.plugin_attempts == 0 for row in trace.catalog_schema_totals())
    else:
        assert trace.catalog_schema_totals()[0].plugin_attempts == 1
    record_property(
        "catalog_schema_source_refusal_expected",
        json.dumps({"source_valid": change in ("owner", "class-alias"), "complete": False, "verified": False, "has_errors": True}),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("carrier", ["correct", "missing", "different", "unobserved"])
async def test_catalog_schema_zero_requires_actual_worker_and_submission_carrier(monkeypatch, request, record_property, carrier):
    catalog, _plugin, count = _catalog_schema_fixture()
    expected = catalog.get_schema("source", "csv")
    original_scope = clock.retention_stage_memo_scope
    if carrier == "missing":
        monkeypatch.setattr(async_workers, "prepare_retention_sql_callable", partial)
    prior, monitor = sys.getprofile(), _native_monitor_state()
    trace = RetentionMemoTrace(monkeypatch, request, require_catalog_schema=True, catalog=catalog)
    ticket = publication_ticket()

    def read():
        assert catalog.get_schema("source", "csv") == expected
        return "original catalog worker result"

    def work():
        if carrier in ("different", "unobserved"):
            scope = clock.retention_stage_memo_scope if carrier == "different" else original_scope
            with scope():
                return read()
        return read()

    with ThreadPoolExecutor(max_workers=1) as pool:
        monkeypatch.setattr(async_workers, "_SHARED_EXECUTOR", pool)
        worker_prior = pool.submit(sys.getprofile).result()
        with clock.retention_stage_memo_scope():
            result = await async_workers.run_required_sql_finish_once(ticket, work)
            assert isinstance(result, RequiredSQLReturned) and result.value == "original catalog worker result"
        assert pool.submit(sys.getprofile).result() is worker_prior
    assert ticket.complete and async_workers.outstanding_admissions() == 0
    if carrier == "correct":
        trace.finish(stages=1, require_snapshot=False, require_population=False, catalog_schema_budget=True)
        record_property(
            "catalog_schema_expected", json.dumps(_catalog_schema_facts(trace, count, _catalog_schema_expected(reads=1), verified=True))
        )
    else:
        with pytest.raises(AssertionError, match="catalog schema provenance invalid"):
            trace.finish(
                stages=2 if carrier == "different" else 1, require_snapshot=False, require_population=False, catalog_schema_budget=True
            )
        record_property(
            "catalog_schema_source_refusal_expected",
            json.dumps({"source_valid": True, "complete": False, "verified": False, "has_errors": True}),
        )
    assert sys.getprofile() is prior and _native_monitor_state() == monitor and not trace._catalog_schema_frames


@pytest.mark.parametrize("case", ["omitted", "open", "active", "legacy", "no-stage", "no-owner"])
def test_catalog_schema_qualification_requires_explicit_closed_bound_observation(monkeypatch, request, case):
    catalog, _plugin, _count = _catalog_schema_fixture()
    if case == "no-owner":
        with pytest.raises(AssertionError, match="catalog schema observation requires its exact catalog owner"):
            RetentionMemoTrace(monkeypatch, request, require_catalog_schema=True)
        return
    trace = RetentionMemoTrace(monkeypatch, request, require_catalog_schema=case != "legacy", catalog=catalog if case != "legacy" else None)
    if case != "no-stage":
        with clock.retention_stage_memo_scope():
            catalog.get_schema("source", "csv")
            if case == "active":
                trace.close()
            if case in ("open", "active"):
                with pytest.raises(AssertionError, match="catalog schema observation remains active"):
                    trace._assert_catalog_schema_budget(True)
    if case == "open":
        trace.finish(stages=1, require_snapshot=False, require_population=False, catalog_schema_budget=True)
    elif case == "active":
        with pytest.raises(AssertionError, match="catalog schema provenance invalid"):
            trace.finish(stages=1, require_snapshot=False, require_population=False, catalog_schema_budget=True)
        assert not trace.catalog_schema_complete() and not trace._catalog_schema_verified
    else:
        message = {
            "omitted": "budget assertion is required",
            "legacy": "budgets require explicit observation mode",
            "no-stage": "coverage is incomplete",
        }[case]
        with pytest.raises(AssertionError, match="catalog schema " + message):
            trace.finish(
                stages=0 if case == "no-stage" else 1,
                require_snapshot=False,
                require_population=False,
                catalog_schema_budget=case != "omitted",
            )
        assert not trace._catalog_schema_verified


@pytest.mark.parametrize("marker_type", [RuntimeError, KeyboardInterrupt, SystemExit, asyncio.CancelledError])
def test_catalog_schema_original_raised_outcome_is_preserved_and_unqualified(monkeypatch, request, marker_type):
    catalog, plugin, _count = _catalog_schema_fixture()
    marker = marker_type("original catalog outcome")

    def raised(_cls):
        raise marker

    monkeypatch.setattr(plugin.config_model, "model_json_schema", classmethod(raised))
    trace = RetentionMemoTrace(monkeypatch, request, require_catalog_schema=True, catalog=catalog)
    with pytest.raises(marker_type) as caught:
        try:
            with clock.retention_stage_memo_scope():
                catalog._catalog_schema(plugin, "source")
        finally:
            trace.finish(stages=1, require_snapshot=False, require_population=False, catalog_schema_budget=True)
    assert caught.value is marker
    assert trace.catalog_schema_complete() and not trace._catalog_schema_verified
    assert [asdict(row) for row in trace.catalog_schema_totals()] == _catalog_schema_expected(factory=1, plugin=1)
    _FINALIZE_MEMO_TRACE(trace)


@pytest.mark.parametrize("case", ["success", "later-failure", "tamper", "late-source"])
def test_catalog_schema_terminal_truth_preserves_original_guards_and_capture(monkeypatch, request, record_property, case):
    catalog, plugin, count = _catalog_schema_fixture()
    trace = RetentionMemoTrace(monkeypatch, request, require_catalog_schema=True, catalog=catalog)
    with clock.retention_stage_memo_scope():
        catalog.get_schema("source", "csv")
    original = trace.assert_closed
    failures = []

    def original_assertion(**kwargs):
        try:
            original(**kwargs)
        except BaseException as error:
            assert trace._catalog_schema_verified
            failures.append(error)
            raise

    monkeypatch.setattr(trace, "assert_closed", original_assertion)
    if case == "later-failure":
        with pytest.raises(AssertionError, match="actual retention must request snapshot work") as caught:
            trace.finish(stages=1, require_population=False, catalog_schema_budget=True)
        assert len(failures) == 1 and caught.value is failures[0]
    else:
        trace.finish(stages=1, require_snapshot=False, require_population=False, catalog_schema_budget=True)
    if case == "tamper":
        trace._catalog_schema_verified = False
        try:
            with pytest.raises(AssertionError, match="catalog schema state changed after terminal assertion"):
                _FINALIZE_MEMO_TRACE(trace)
        finally:
            trace._catalog_schema_verified = True
    elif case == "late-source":
        with monkeypatch.context() as changed:
            defining = next(parent for parent in plugin.__mro__ if "get_config_schema" in parent.__dict__)
            changed.setattr(defining, "get_config_schema", classmethod(lambda _cls: {}))
            with pytest.raises(AssertionError, match="catalog schema source changed after terminal assertion"):
                _FINALIZE_MEMO_TRACE(trace)
    _FINALIZE_MEMO_TRACE(trace)
    facts = _catalog_schema_facts(trace, count, _catalog_schema_expected(reads=1), verified=case != "later-failure")
    record_property("catalog_schema_terminal_expected", json.dumps(facts))


@pytest.mark.parametrize("close_active", [False, True])
def test_catalog_schema_real_work_after_active_close_cannot_qualify_zero(monkeypatch, request, close_active):
    catalog, plugin, _count = _catalog_schema_fixture()
    expected = plugin.get_config_schema()
    alias = plugin.get_config_schema
    prior, monitor = sys.getprofile(), _native_monitor_state()
    trace = RetentionMemoTrace(monkeypatch, request, require_catalog_schema=True, catalog=catalog)
    with clock.retention_stage_memo_scope():
        assert catalog.get_schema("source", "csv").json_schema == expected
        if close_active:
            trace.close()
            assert alias() == expected
    if close_active:
        with pytest.raises(AssertionError, match=r"catalog schema (provenance invalid|coverage is incomplete)"):
            trace.finish(stages=1, require_snapshot=False, require_population=False, catalog_schema_budget=True)
        assert not trace.catalog_schema_complete() and not trace._catalog_schema_verified
    else:
        trace.finish(stages=1, require_snapshot=False, require_population=False, catalog_schema_budget=True)
        assert trace.catalog_schema_complete() and trace._catalog_schema_verified
    assert trace.catalog_schema_closed() and sys.getprofile() is prior and _native_monitor_state() == monitor
    _FINALIZE_MEMO_TRACE(trace)


@pytest.mark.parametrize("replacement", ["original", "descriptor", "code"])
def test_catalog_schema_preconstructor_replacement_cannot_redefine_original_alias(monkeypatch, request, replacement):
    catalog, plugin, _count = _catalog_schema_fixture()
    defining = next(parent for parent in plugin.__mro__ if "get_config_schema" in parent.__dict__)
    descriptor = defining.__dict__["get_config_schema"]
    alias = plugin.get_config_schema
    expected = alias()

    def replaced(_cls):
        return {}

    with monkeypatch.context() as changed:
        if replacement == "descriptor":
            changed.setattr(defining, "get_config_schema", classmethod(replaced))
        elif replacement == "code":
            changed.setattr(descriptor.__func__, "__code__", replaced.__code__)
        trace = RetentionMemoTrace(monkeypatch, request, require_catalog_schema=True, catalog=catalog)
        with clock.retention_stage_memo_scope():
            assert alias() == ({} if replacement == "code" else expected)
        message = "catalog schema construction budget exceeded" if replacement == "original" else "catalog schema provenance invalid"
        with pytest.raises(AssertionError, match=message):
            trace.finish(stages=1, require_snapshot=False, require_population=False, catalog_schema_budget=True)
        assert not trace._catalog_schema_verified
        if replacement == "original":
            assert trace.catalog_schema_complete() and trace.catalog_schema_totals()[0].plugin_attempts == 1
        else:
            assert not trace.catalog_schema_complete() and not trace._catalog_schema_source_valid
    assert plugin.get_config_schema() == expected
    _FINALIZE_MEMO_TRACE(trace)


@pytest.mark.parametrize("timing", ["before", "inside"])
@pytest.mark.parametrize("method", ["_catalog_schema", "get_schema", "list_sources", "list_transforms", "list_sinks"])
def test_catalog_schema_instance_dispatch_shadows_cannot_qualify_zero(monkeypatch, request, timing, method):
    catalog, plugin, _count = _catalog_schema_fixture()
    if method == "_catalog_schema":
        expected = catalog._catalog_schema(plugin, "source")

        def shadow(actual_plugin, kind):
            assert actual_plugin is plugin and kind == "source"
            return actual_plugin.config_model.model_json_schema()

        def invoke():
            return catalog._catalog_schema(plugin, "source")

    elif method == "get_schema":
        original = catalog.get_schema
        expected = original("source", "csv")

        def shadow(kind, name):
            return original(kind, name)

        def invoke():
            return catalog.get_schema("source", "csv")

    else:
        if method == "list_sources":
            original = catalog.list_sources

            def invoke():
                return catalog.list_sources()

        elif method == "list_transforms":
            original = catalog.list_transforms

            def invoke():
                return catalog.list_transforms()

        else:
            original = catalog.list_sinks

            def invoke():
                return catalog.list_sinks()

        expected = original()

        def shadow():
            return original()

    trace = None if timing == "before" else RetentionMemoTrace(monkeypatch, request, require_catalog_schema=True, catalog=catalog)
    with monkeypatch.context() as changed:
        changed.setattr(catalog, method, shadow)
        if timing == "before":
            trace = RetentionMemoTrace(monkeypatch, request, require_catalog_schema=True, catalog=catalog)
        with clock.retention_stage_memo_scope():
            assert invoke() == expected
    with pytest.raises(AssertionError, match="catalog schema provenance invalid"):
        trace.finish(stages=1, require_snapshot=False, require_population=False, catalog_schema_budget=True)
    assert trace.catalog_schema_closed() and not trace.catalog_schema_complete()
    assert not trace._catalog_schema_source_valid and not trace._catalog_schema_verified
    _FINALIZE_MEMO_TRACE(trace)


@pytest.mark.asyncio
@pytest.mark.parametrize("worker", [False, True])
@pytest.mark.parametrize("schema_work", [False, True])
async def test_catalog_schema_lock_held_calls_keep_real_loop_and_worker_accounting(monkeypatch, request, worker, schema_work):
    from elspeth.web import continuation_retention_memo as memo_module

    catalog, plugin, _count = _catalog_schema_fixture()
    schema = catalog.get_schema("source", "csv")
    original_schema = plugin.get_config_schema()
    alias = plugin.get_config_schema
    prior, monitor = sys.getprofile(), _native_monitor_state()
    trace = RetentionMemoTrace(monkeypatch, request, require_catalog_schema=True, catalog=catalog)
    ticket = publication_ticket()

    def work():
        memo = memo_module.current_retention_stage_memo()
        assert type(memo) is RetentionStageMemo
        with memo._lock:
            assert catalog.get_schema("source", "csv") == schema
            if schema_work:
                assert catalog._catalog_schema(plugin, "source") == original_schema
                assert alias() == original_schema
        return "original lock-held schema result"

    if worker:
        with ThreadPoolExecutor(max_workers=1) as pool:
            monkeypatch.setattr(async_workers, "_SHARED_EXECUTOR", pool)
            worker_prior = pool.submit(sys.getprofile).result()
            with clock.retention_stage_memo_scope():
                result = await async_workers.run_required_sql_finish_once(ticket, work)
                assert isinstance(result, RequiredSQLReturned) and result.value == "original lock-held schema result"
            assert pool.submit(sys.getprofile).result() is worker_prior
        assert ticket.complete and async_workers.outstanding_admissions() == 0
    else:
        with clock.retention_stage_memo_scope():
            assert work() == "original lock-held schema result"
    if schema_work:
        with pytest.raises(AssertionError, match="catalog schema construction budget exceeded"):
            trace.finish(stages=1, require_snapshot=False, require_population=False, catalog_schema_budget=True)
        assert not trace._catalog_schema_verified
    else:
        trace.finish(stages=1, require_snapshot=False, require_population=False, catalog_schema_budget=True)
        assert trace._catalog_schema_verified
    assert trace.catalog_schema_complete() and trace.catalog_schema_checked_stages() == (1,)
    assert [asdict(row) for row in trace.catalog_schema_totals()] == _catalog_schema_expected(
        factory=int(schema_work), plugin=2 * int(schema_work), reads=1
    )
    assert trace.catalog_schema_unassigned() == {"factory": 0, "plugin": 0, "read": 0}
    assert sys.getprofile() is prior and _native_monitor_state() == monitor
    assert not trace._catalog_schema_frames and not trace._catalog_schema_scopes
    _FINALIZE_MEMO_TRACE(trace)


def test_catalog_schema_lock_free_carrier_source_change_stays_unqualified(monkeypatch, request):
    from contextvars import ContextVar

    from elspeth.web import continuation_retention_memo as memo_module

    catalog, plugin, _count = _catalog_schema_fixture()
    expected = plugin.get_config_schema()
    trace = RetentionMemoTrace(monkeypatch, request, require_catalog_schema=True, catalog=catalog)
    with clock.retention_stage_memo_scope() as memo, monkeypatch.context() as changed:
        changed.setattr(memo_module, "_RETENTION_STAGE_MEMO", ContextVar("opposed schema carrier", default=None))
        with memo._lock:
            assert catalog._catalog_schema(plugin, "source") == expected
    with pytest.raises(AssertionError, match="catalog schema provenance invalid"):
        trace.finish(stages=1, require_snapshot=False, require_population=False, catalog_schema_budget=True)
    row = trace.catalog_schema_totals()[0]
    assert row.factory_attempts == row.plugin_attempts == 1
    assert row.attributed_factory_attempts == row.attributed_plugin_attempts == 0
    assert not trace._catalog_schema_source_valid and not trace.catalog_schema_complete() and not trace._catalog_schema_verified
    _FINALIZE_MEMO_TRACE(trace)


@pytest.mark.parametrize("timing", ["before", "inside"])
@pytest.mark.parametrize("method", ["_catalog_schema", "get_schema", "list_sources", "list_transforms", "list_sinks"])
def test_catalog_schema_none_instance_shadow_refuses_even_with_genuine_read_alias(monkeypatch, request, timing, method):
    catalog, _plugin, _count = _catalog_schema_fixture()
    read = catalog.get_schema
    expected = read("source", "csv")
    trace = None if timing == "before" else RetentionMemoTrace(monkeypatch, request, require_catalog_schema=True, catalog=catalog)
    with monkeypatch.context() as changed:
        changed.setattr(catalog, method, None)
        if timing == "before":
            trace = RetentionMemoTrace(monkeypatch, request, require_catalog_schema=True, catalog=catalog)
        with clock.retention_stage_memo_scope():
            assert read("source", "csv") == expected
    with pytest.raises(AssertionError, match="catalog schema provenance invalid"):
        trace.finish(stages=1, require_snapshot=False, require_population=False, catalog_schema_budget=True)
    assert trace.catalog_schema_totals()[0].read_attempts == 1
    assert not trace._catalog_schema_source_valid and not trace.catalog_schema_complete() and not trace._catalog_schema_verified
    _FINALIZE_MEMO_TRACE(trace)


@pytest.mark.asyncio
@pytest.mark.parametrize("worker", [False, True])
async def test_catalog_schema_early_close_never_recovers_stage_or_worker_coverage(monkeypatch, request, worker):
    catalog, plugin, _count = _catalog_schema_fixture()
    expected = plugin.get_config_schema()
    prior, monitor = sys.getprofile(), _native_monitor_state()
    trace = RetentionMemoTrace(monkeypatch, request, require_catalog_schema=True, catalog=catalog)
    ticket = publication_ticket()

    def work():
        assert catalog.get_schema("source", "csv").json_schema == expected
        trace.close()
        assert plugin.get_config_schema() == expected
        return "original early-close result"

    if worker:
        with ThreadPoolExecutor(max_workers=1) as pool:
            monkeypatch.setattr(async_workers, "_SHARED_EXECUTOR", pool)
            worker_prior = pool.submit(sys.getprofile).result()
            with clock.retention_stage_memo_scope():
                result = await async_workers.run_required_sql_finish_once(ticket, work)
                assert isinstance(result, RequiredSQLReturned) and result.value == "original early-close result"
            assert pool.submit(sys.getprofile).result() is worker_prior
        assert ticket.complete and async_workers.outstanding_admissions() == 0
    else:
        with clock.retention_stage_memo_scope():
            assert work() == "original early-close result"
    with pytest.raises(AssertionError, match="catalog schema provenance invalid"):
        trace.finish(stages=1, require_snapshot=False, require_population=False, catalog_schema_budget=True)
    assert trace.catalog_schema_closed() and not trace.catalog_schema_complete() and not trace._catalog_schema_verified
    assert "catalog schema observation closed before its stage or worker exited" in trace._catalog_schema_errors
    assert trace.catalog_schema_totals()[0].plugin_attempts == int(worker)
    assert sys.getprofile() is prior and _native_monitor_state() == monitor
    _FINALIZE_MEMO_TRACE(trace)


@pytest.mark.parametrize("marker_type", [RuntimeError, KeyboardInterrupt, SystemExit, asyncio.CancelledError])
def test_catalog_schema_active_close_preserves_original_primary_exception(monkeypatch, request, marker_type):
    catalog, _plugin, _count = _catalog_schema_fixture()
    marker = marker_type("original active-close marker")
    prior = sys.getprofile()
    trace = RetentionMemoTrace(monkeypatch, request, require_catalog_schema=True, catalog=catalog)
    with pytest.raises(marker_type) as caught:
        try:
            with clock.retention_stage_memo_scope():
                catalog.get_schema("source", "csv")
                trace.close()
                raise marker
        finally:
            trace.finish(stages=1, require_snapshot=False, require_population=False, catalog_schema_budget=True)
    assert caught.value is marker and sys.getprofile() is prior
    assert trace.catalog_schema_closed() and not trace.catalog_schema_complete() and not trace._catalog_schema_verified
    _FINALIZE_MEMO_TRACE(trace)


@pytest.mark.parametrize("kind", ["source", "transform"])
@pytest.mark.parametrize("schema_work", [False, True])
def test_catalog_schema_builtin_overrides_use_independent_original_anchors(monkeypatch, request, kind, schema_work):
    from elspeth.plugins.infrastructure.discovery import create_dynamic_hookimpl
    from elspeth.plugins.infrastructure.manager import PluginManager
    from elspeth.plugins.sources.llm.source import LLMSource
    from elspeth.plugins.transforms.llm.transform import LLMTransform
    from elspeth.web.catalog.service import CatalogServiceImpl

    plugin = LLMSource if kind == "source" else LLMTransform
    manager = PluginManager()
    manager.register(create_dynamic_hookimpl([plugin], "elspeth_get_source" if kind == "source" else "elspeth_get_transforms"))
    catalog = CatalogServiceImpl(manager)
    read = catalog.get_schema(kind, "llm")
    alias = plugin.get_config_schema
    expected = alias()
    trace = RetentionMemoTrace(monkeypatch, request, require_catalog_schema=True, catalog=catalog)
    with clock.retention_stage_memo_scope():
        assert catalog.get_schema(kind, "llm") == read
        if schema_work:
            assert alias() == expected
    if schema_work:
        with pytest.raises(AssertionError, match="catalog schema construction budget exceeded"):
            trace.finish(stages=1, require_snapshot=False, require_population=False, catalog_schema_budget=True)
    else:
        trace.finish(stages=1, require_snapshot=False, require_population=False, catalog_schema_budget=True)
    assert trace.catalog_schema_complete() and trace._catalog_schema_source_valid
    assert trace._catalog_schema_verified is not schema_work
    assert [asdict(row) for row in trace.catalog_schema_totals()] == _catalog_schema_expected(plugin=int(schema_work), reads=1)
    _FINALIZE_MEMO_TRACE(trace)


def test_catalog_schema_unanchored_override_refuses_but_retains_known_original_body_work(monkeypatch, request):
    from elspeth.contracts.enums import Determinism
    from elspeth.plugins.infrastructure.discovery import create_dynamic_hookimpl
    from elspeth.plugins.infrastructure.manager import PluginManager
    from elspeth.plugins.sources.csv_source import CSVSource
    from elspeth.web.catalog.service import CatalogServiceImpl

    class UnanchoredCSVSource(CSVSource):
        name = "unanchored_csv"
        determinism = Determinism.IO_READ

        @classmethod
        def get_config_schema(cls):
            return super().get_config_schema()

    manager = PluginManager()
    manager.register(create_dynamic_hookimpl([UnanchoredCSVSource], "elspeth_get_source"))
    catalog = CatalogServiceImpl(manager)
    expected = UnanchoredCSVSource.get_config_schema()
    trace = RetentionMemoTrace(monkeypatch, request, require_catalog_schema=True, catalog=catalog)
    with clock.retention_stage_memo_scope():
        assert UnanchoredCSVSource.get_config_schema() == expected
    with pytest.raises(AssertionError, match="catalog schema provenance invalid"):
        trace.finish(stages=1, require_snapshot=False, require_population=False, catalog_schema_budget=True)
    row = trace.catalog_schema_totals()[0]
    assert row.plugin_attempts == 1 and row.attributed_plugin_attempts == 0
    assert not trace._catalog_schema_source_valid and not trace.catalog_schema_complete() and not trace._catalog_schema_verified
    _FINALIZE_MEMO_TRACE(trace)


@pytest.mark.parametrize("kind", ["source", "transform", "sink"])
def test_catalog_schema_saved_original_alias_stays_raw_after_preconstructor_descriptor_replacement(monkeypatch, request, kind):
    catalog, source, _count = _catalog_schema_fixture()
    plugin = source if kind == "source" else catalog._transform_classes[0] if kind == "transform" else catalog._sink_classes[0]
    alias = plugin.get_config_schema
    expected = alias()
    defining = next(parent for parent in plugin.__mro__ if "get_config_schema" in parent.__dict__)
    with monkeypatch.context() as changed:
        changed.setattr(defining, "get_config_schema", classmethod(lambda _cls: {}))
        trace = RetentionMemoTrace(monkeypatch, request, require_catalog_schema=True, catalog=catalog)
        with clock.retention_stage_memo_scope():
            assert alias() == expected
        with pytest.raises(AssertionError, match="catalog schema provenance invalid"):
            trace.finish(stages=1, require_snapshot=False, require_population=False, catalog_schema_budget=True)
        row = trace.catalog_schema_totals()[0]
        assert row.plugin_attempts == 1 and row.attributed_plugin_attempts == 0
        assert not trace._catalog_schema_source_valid and not trace.catalog_schema_complete() and not trace._catalog_schema_verified
    _FINALIZE_MEMO_TRACE(trace)


@pytest.mark.asyncio
@pytest.mark.parametrize("worker", [False, True])
@pytest.mark.parametrize("held_lock", [False, True])
@pytest.mark.parametrize("schema_work", [False, True])
async def test_catalog_schema_production_membership_keeps_loop_worker_and_locked_original_work(
    monkeypatch, request, worker, held_lock, schema_work
):
    from elspeth.web.dependencies import create_catalog_service

    catalog = create_catalog_service()
    groups = (
        ("source", catalog._source_classes, catalog._pm._sources),
        ("transform", catalog._transform_classes, catalog._pm._transforms),
        ("sink", catalog._sink_classes, catalog._pm._sinks),
    )
    expected_bindings = tuple((plugin, kind) for kind, classes, _registered in groups for plugin in classes)
    for _kind, classes, registered in groups:
        assert len(classes) == len(registered)
        assert all(a is b for a, b in zip(classes, registered.values(), strict=True))
    plugin = next(plugin for plugin in catalog._source_classes if plugin.name == "csv")
    read = catalog.get_schema("source", "csv")
    factory, alias = catalog._catalog_schema, plugin.get_config_schema
    expected = alias()
    prior, monitor = sys.getprofile(), _native_monitor_state()
    trace = RetentionMemoTrace(monkeypatch, request, require_catalog_schema=True, catalog=catalog)
    assert len(trace._catalog_schema_bindings) == len(expected_bindings)
    assert all(
        binding.plugin is plugin and binding.kind == kind
        for binding, (plugin, kind) in zip(trace._catalog_schema_bindings, expected_bindings, strict=True)
    )
    ticket = publication_ticket()

    def calls():
        assert catalog.get_schema("source", "csv") == read
        if schema_work:
            assert factory(plugin, "source") == expected
            assert alias() == expected
        return "original production catalog result"

    def work():
        memo = current_retention_stage_memo()
        assert type(memo) is RetentionStageMemo
        if held_lock:
            with memo._lock:
                return calls()
        return calls()

    if worker:
        with ThreadPoolExecutor(max_workers=1) as pool:
            monkeypatch.setattr(async_workers, "_SHARED_EXECUTOR", pool)
            worker_prior = pool.submit(sys.getprofile).result()
            with clock.retention_stage_memo_scope():
                result = await async_workers.run_required_sql_finish_once(ticket, work)
                assert isinstance(result, RequiredSQLReturned) and result.value == "original production catalog result"
            assert pool.submit(sys.getprofile).result() is worker_prior
        assert ticket.complete and async_workers.outstanding_admissions() == 0
    else:
        with clock.retention_stage_memo_scope():
            assert work() == "original production catalog result"
    if schema_work:
        with pytest.raises(AssertionError, match="catalog schema construction budget exceeded"):
            trace.finish(stages=1, require_snapshot=False, require_population=False, catalog_schema_budget=True)
    else:
        trace.finish(stages=1, require_snapshot=False, require_population=False, catalog_schema_budget=True)
    assert trace.catalog_schema_complete() and trace._catalog_schema_source_valid
    assert trace._catalog_schema_verified is not schema_work
    assert [asdict(row) for row in trace.catalog_schema_totals()] == _catalog_schema_expected(
        factory=int(schema_work), plugin=2 * int(schema_work), reads=1
    )
    assert trace.catalog_schema_unassigned() == {"factory": 0, "plugin": 0, "read": 0}
    assert sys.getprofile() is prior and _native_monitor_state() == monitor
    _FINALIZE_MEMO_TRACE(trace)


def _catalog_schema_shared_inheritance_fixture():
    from elspeth.contracts.enums import Determinism
    from elspeth.plugins.infrastructure.discovery import create_dynamic_hookimpl
    from elspeth.plugins.infrastructure.manager import PluginManager
    from elspeth.plugins.sources.csv_source import CSVSource
    from elspeth.web.catalog.service import CatalogServiceImpl

    class SharedParent(CSVSource):
        name = "shared_parent"
        determinism = Determinism.IO_READ

    class AlternateParent(CSVSource):
        name = "alternate_parent"
        determinism = Determinism.IO_READ

    class First(SharedParent):
        name = "shared_first"
        determinism = Determinism.IO_READ

    class Second(SharedParent):
        name = "shared_second"
        determinism = Determinism.IO_READ

    manager = PluginManager()
    manager.register(create_dynamic_hookimpl([First, Second], "elspeth_get_source"))
    return CatalogServiceImpl(manager), First, Second, SharedParent, AlternateParent


@pytest.mark.parametrize("member", ["first", "second"])
@pytest.mark.parametrize("change", ["unchanged", "descriptor", "code", "inherited-slot", "bases"])
def test_catalog_schema_shared_original_and_inheritance_guards_survive_changed_restored_c_call(monkeypatch, request, member, change):
    catalog, first, second, parent, alternate = _catalog_schema_shared_inheritance_fixture()
    plugin = first if member == "first" else second
    defining = next(owner for owner in plugin.__mro__ if "get_config_schema" in owner.__dict__)
    assert defining is next(owner for owner in second.__mro__ if "get_config_schema" in owner.__dict__)
    assert parent in first.__mro__ and parent in second.__mro__
    descriptor = defining.__dict__["get_config_schema"]
    function, code = descriptor.__func__, descriptor.__func__.__code__
    replacement = classmethod(lambda _cls: {})
    replacement_code = replacement.__func__.__code__
    bases = plugin.__bases__
    expected = catalog.get_schema("source", plugin.name)
    prior = sys.getprofile()
    trace = RetentionMemoTrace(monkeypatch, request, require_catalog_schema=True, catalog=catalog)
    with clock.retention_stage_memo_scope():
        assert catalog.get_schema("source", plugin.name) == expected
        # Each interval stays in this Python frame: the ONLY explicit call
        # between mutation and restoration is a genuine builtin C call.
        if change == "descriptor":
            defining.get_config_schema = replacement
            try:
                assert len(()) == 0
            finally:
                defining.get_config_schema = descriptor
        elif change == "code":
            function.__code__ = replacement_code
            try:
                assert len(()) == 0
            finally:
                function.__code__ = code
        elif change == "inherited-slot":
            parent.get_config_schema = descriptor
            try:
                assert len(()) == 0
            finally:
                del parent.get_config_schema
        elif change == "bases":
            plugin.__bases__ = (alternate,)
            try:
                assert len(()) == 0
            finally:
                plugin.__bases__ = bases
        else:
            assert len(()) == 0
        assert catalog.get_schema("source", plugin.name) == expected
    if change == "unchanged":
        trace.finish(stages=1, require_snapshot=False, require_population=False, catalog_schema_budget=True)
        assert trace.catalog_schema_complete() and trace._catalog_schema_verified
        assert [asdict(row) for row in trace.catalog_schema_totals()] == _catalog_schema_expected(reads=2)
    else:
        with pytest.raises(AssertionError, match="catalog schema provenance invalid"):
            trace.finish(stages=1, require_snapshot=False, require_population=False, catalog_schema_budget=True)
        row = trace.catalog_schema_totals()[0]
        assert row.read_attempts == 2 and row.attributed_read_attempts == 1
        assert row.factory_attempts == row.plugin_attempts == 0
        assert not trace._catalog_schema_source_valid and not trace.catalog_schema_complete() and not trace._catalog_schema_verified
    assert sys.getprofile() is prior
    _FINALIZE_MEMO_TRACE(trace)


@pytest.mark.parametrize("change", ["unchanged", "instance-none", "catalog-vector", "registry-vector"])
def test_catalog_schema_instance_and_ordered_registry_guards_audit_changed_restored_c_call(monkeypatch, request, change):
    catalog, first, second, _parent, _alternate = _catalog_schema_shared_inheritance_fixture()
    classes, registered = catalog._source_classes, catalog._pm._sources
    assert classes[0] is first and classes[1] is second
    assert registered[first.name] is first and registered[second.name] is second
    first_name, second_name = first.name, second.name
    expected = catalog.get_schema("source", first_name)
    trace = RetentionMemoTrace(monkeypatch, request, require_catalog_schema=True, catalog=catalog)
    with clock.retention_stage_memo_scope():
        assert catalog.get_schema("source", first_name) == expected
        if change == "instance-none":
            catalog.__dict__["get_schema"] = None
            try:
                assert len(()) == 0
            finally:
                del catalog.__dict__["get_schema"]
        elif change == "catalog-vector":
            classes[0], classes[1] = second, first
            try:
                assert len(()) == 0
            finally:
                classes[0], classes[1] = first, second
        elif change == "registry-vector":
            registered[first_name], registered[second_name] = second, first
            try:
                assert len(()) == 0
            finally:
                registered[first_name], registered[second_name] = first, second
        else:
            assert len(()) == 0
        assert catalog.get_schema("source", first_name) == expected
    if change == "unchanged":
        trace.finish(stages=1, require_snapshot=False, require_population=False, catalog_schema_budget=True)
        assert trace.catalog_schema_complete() and trace._catalog_schema_verified
        assert [asdict(row) for row in trace.catalog_schema_totals()] == _catalog_schema_expected(reads=2)
    else:
        with pytest.raises(AssertionError, match="catalog schema provenance invalid"):
            trace.finish(stages=1, require_snapshot=False, require_population=False, catalog_schema_budget=True)
        row = trace.catalog_schema_totals()[0]
        assert row.read_attempts == 2 and row.attributed_read_attempts == 1
        assert row.factory_attempts == row.plugin_attempts == 0
        assert not trace._catalog_schema_source_valid and not trace.catalog_schema_complete() and not trace._catalog_schema_verified
    _FINALIZE_MEMO_TRACE(trace)


@pytest.mark.parametrize("changed", ["surface", "scanner_role"])
def test_scan_full_context_surface_and_role_keep_independent_actual_stage_work(monkeypatch, request, changed):
    from elspeth.web.continuation_retention_memo import CredentialScanKey

    policy = CredentialTraversalPolicy(env_ref_names=frozenset({"STAGE_CONTEXT"}))
    value = {"api_key": "${STAGE_CONTEXT}"}
    first = ("stage-context-first", "generic")
    second = ("stage-context-second", "generic") if changed == "surface" else (first[0], "field_mapper_projection_prevalidation")
    trace = RetentionMemoTrace(monkeypatch, request)
    with clock.retention_stage_memo_scope():
        for surface, role in (first, first, second, second, first):
            assert guard._retention_credential_finding(value, policy=policy, surface=surface, scanner_role=role) is None
    trace.finish(stages=1, require_snapshot=False)
    calls = trace.records
    assert len(calls) == 5 and all(record.family == "credential" and type(record.key) is CredentialScanKey for record in calls)
    assert [record.attempts["detector_work"] for record in calls] == [1, 0, 1, 0, 0]
    totals = trace.detector_stage_totals()
    assert len(totals) == 1 and totals[0].stage == 1
    assert totals[0].native_attempts == totals[0].memo_family_attempts == 2
    assert totals[0].uncached_validation_attempts == totals[0].orphan_attempts == 0
    assert [(record.key.surface, record.key.scanner_role) for record in calls] == [first, first, second, second, first]
    assert len({record.group for record in calls}) == 2
    assert calls[0].group == calls[1].group == calls[4].group and calls[2].group == calls[3].group
    assert all(record.key.policy.env_ref_names == frozenset({"STAGE_CONTEXT"}) for record in calls)
    assert all(actual == record.key.policy for record in calls for actual in record.actual_policies)
    assert all(record.key.detector is guard.find_credential_material for record in calls)


def _require_structure_api():
    from tests.helpers import continuation_memo as accounting

    for name in ("observe_structure", "structure_snapshot", "structure_listener_handles", "assert_structure_matches"):
        assert type(RetentionMemoTrace.__dict__.get(name)) is FunctionType, "B6 structure API unavailable: RetentionMemoTrace." + name
    code = RetentionMemoTrace.observe_structure.__code__
    parameters = code.co_varnames[: code.co_argcount + code.co_kwonlyargcount]
    assert "engines" in parameters and "entrypoint" in parameters, "B6 structure API unavailable: owned engine/entrypoint arguments"
    code = RetentionMemoTrace.assert_closed.__code__
    assert "structure_observation" in code.co_varnames[: code.co_argcount + code.co_kwonlyargcount], (
        "B6 structure API unavailable: explicit structure_observation finish assertion"
    )
    for name in ("StructureSnapshot", "StructureEvent", "StructureLifetime", "StructureEdge", "StructureSqlShape"):
        owned = accounting.__dict__.get(name)
        assert type(owned) is type and owned.__module__ == accounting.__name__, "B6 structure owned record unavailable: " + name


def _structure_events(snapshot, kind):
    return tuple(event for event in snapshot.events if event.kind == kind)


def _structure_selects(snapshot):
    return tuple(event for event in snapshot.events if event.kind == "sql_attempt" and event.sql is not None and event.sql.kind == "select")


def _structure_sql_work(engine, *, extra=False, predicate=False, arity=1, clock_read=True, context=None, window=True, canary=None):
    from sqlalchemy import bindparam, select

    from elspeth.web.coordination import database_clock
    from elspeth.web.sessions.continuation import require_continuation_mutation
    from elspeth.web.sessions.locking import locked_session_transaction
    from elspeth.web.sessions.models import sessions_table

    values = tuple(canary if canary is not None else "structure-key-" + str(index) for index in range(arity))
    condition = (
        sessions_table.c.id.not_in(bindparam("ids", expanding=True))
        if predicate
        else sessions_table.c.id.in_(bindparam("ids", expanding=True))
    )
    statement = select(sessions_table.c.id).where(condition)
    with locked_session_transaction(engine, "structure-calibration") if window else engine.begin() as conn:
        if context is not None:
            assert type(require_continuation_mutation(conn, context)) is bool
        if clock_read:
            assert database_clock.database_now(conn).utcoffset() is not None
        rows = conn.execute(statement, {"ids": values}).all()
        if extra:
            assert conn.execute(statement, {"ids": values}).all() == rows
    return len(rows)


def _structure_secondary_engine():
    from sqlalchemy.pool import StaticPool

    from elspeth.web.sessions.engine import create_session_engine
    from elspeth.web.sessions.schema import initialize_session_schema

    engine = create_session_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    initialize_session_schema(engine)
    return engine


async def _structure_trial(monkeypatch, request, engines, operation, *, phase="retention"):
    from tests.helpers.continuation_policy import minimal_continuation_policy

    _require_structure_api()
    with monkeypatch.context() as patch:
        trace = RetentionMemoTrace(patch, request)
        trace.observe_structure(engines=engines, entrypoint="calibration")
        timer = clock.ContinuationActionClock(minimal_continuation_policy().actions.session_sql)
        timer.finish_preparation()
        result = await timer.run(phase, operation)
        try:
            trace.finish(stages=int(phase == "retention"), require_snapshot=False, require_population=False, structure_observation=True)
        except BaseException as primary:
            try:
                snapshot = trace.structure_snapshot()
                if snapshot.closed:
                    windows = tuple(
                        event for event in snapshot.events if event.kind in ("window_attempt", "window_ready", "window_terminal")
                    )

                    def event_rows(events):
                        return tuple(
                            (
                                event.ordinal,
                                event.kind,
                                event.source,
                                event.phase,
                                event.clock,
                                event.stage,
                                event.expected_stage,
                                event.submission,
                                event.thread,
                                event.engine,
                                event.window,
                                event.connection,
                                event.outcome,
                            )
                            for event in events[:64]
                        )

                    primary.add_note(
                        "B6 closed structure diagnostic: "
                        + json.dumps(
                            {
                                "required": snapshot.required,
                                "closed": snapshot.closed,
                                "complete": snapshot.complete,
                                "observation_verified": snapshot.observation_verified,
                                "event_columns": (
                                    "ordinal",
                                    "kind",
                                    "source",
                                    "phase",
                                    "clock",
                                    "stage",
                                    "expected_stage",
                                    "submission",
                                    "thread",
                                    "engine",
                                    "window",
                                    "connection",
                                    "outcome",
                                ),
                                "events": {
                                    "total": len(snapshot.events),
                                    "limit": 64,
                                    "truncated": len(snapshot.events) > 64,
                                    "rows": event_rows(snapshot.events),
                                },
                                "windows": {
                                    "total": len(windows),
                                    "limit": 64,
                                    "truncated": len(windows) > 64,
                                    "rows": event_rows(windows),
                                },
                                "lifetimes": {
                                    "total": len(snapshot.lifetimes),
                                    "limit": 64,
                                    "truncated": len(snapshot.lifetimes) > 64,
                                    "rows": tuple(
                                        (row.kind, row.owner, row.suspensions, row.resumptions, row.terminal)
                                        for row in snapshot.lifetimes[:64]
                                    ),
                                },
                                "edges": {
                                    "total": len(snapshot.edges),
                                    "limit": 128,
                                    "truncated": len(snapshot.edges) > 128,
                                    "rows": tuple((row.before, row.after, row.kind) for row in snapshot.edges[:128]),
                                },
                                "errors": snapshot.errors,
                                "errors_available": len(snapshot.errors),
                                "errors_total": None,
                                "errors_upstream_sample_limit": 8,
                                "errors_upstream_truncation_unknown": len(snapshot.errors) == 8,
                            },
                            ensure_ascii=True,
                            sort_keys=True,
                        )
                    )
                else:
                    primary.add_note("B6 structure diagnostic unavailable: observation not closed")
            except BaseException:
                # Secondary diagnostics cannot replace the active native primary,
                # including when either outcome is an interruption.
                pass
            raise
    snapshot = trace.structure_snapshot()
    assert snapshot.required and snapshot.closed and snapshot.complete and snapshot.observation_verified and not snapshot.errors
    return result, trace


@pytest.mark.asyncio
async def test_structure_api_observes_one_original_clock_window_and_sql(monkeypatch, request):
    _require_structure_api()
    engine = request.getfixturevalue("engine")

    async def operation():
        result = _structure_sql_work(engine)
        await asyncio.sleep(0)
        return result

    result, trace = await _structure_trial(monkeypatch, request, (engine,), operation)
    assert result == 0
    snapshot = trace.structure_snapshot()
    assert len(_structure_events(snapshot, "clock_attempt")) == len(_structure_events(snapshot, "clock_terminal")) == 1
    assert len(_structure_events(snapshot, "window_attempt")) == len(_structure_events(snapshot, "window_ready")) == 1
    assert len(_structure_events(snapshot, "window_terminal")) == len(_structure_selects(snapshot)) == 1
    assert len(_structure_events(snapshot, "db_clock_attempt")) == len(_structure_events(snapshot, "db_clock_terminal")) == 1
    assert _structure_events(snapshot, "clock_terminal")[0].outcome == "returned"
    assert _structure_events(snapshot, "window_terminal")[0].outcome == "returned"
    trace.assert_structure_matches(snapshot)


@pytest.mark.asyncio
@pytest.mark.parametrize("suspensions", [0, 1, 3])
@pytest.mark.parametrize("result_none", [False, True])
async def test_structure_original_coroutine_resumes_are_not_new_clocks_or_windows(monkeypatch, request, suspensions, result_none):
    _require_structure_api()
    engine = request.getfixturevalue("engine")

    async def operation():
        for _ in range(suspensions):
            await asyncio.sleep(0)
        _structure_sql_work(engine)
        return None if result_none else 0

    _result, trace = await _structure_trial(monkeypatch, request, (engine,), operation)
    snapshot = trace.structure_snapshot()
    assert len(_structure_events(snapshot, "clock_attempt")) == len(_structure_events(snapshot, "window_ready")) == 1
    clocks = [row for row in snapshot.lifetimes if row.kind == "clock"]
    windows = [row for row in snapshot.lifetimes if row.kind == "window"]
    assert len(clocks) == len(windows) == 1
    assert clocks[0].suspensions == clocks[0].resumptions == suspensions
    assert windows[0].suspensions == windows[0].resumptions == 1
    assert clocks[0].terminal == windows[0].terminal == "returned"
    assert _structure_events(snapshot, "clock_terminal")[0].result_kind == ("none" if result_none else "int")


@pytest.mark.asyncio
async def test_structure_physical_work_never_borrows_a_retention_carrier_or_stage(monkeypatch, request):
    _require_structure_api()
    engine = request.getfixturevalue("engine")

    async def operation():
        return _structure_sql_work(engine)

    _result, physical = await _structure_trial(monkeypatch, request, (engine,), operation, phase="physical")
    _result, retention = await _structure_trial(monkeypatch, request, (engine,), operation)
    assert len(_structure_selects(physical.structure_snapshot())) == len(_structure_selects(retention.structure_snapshot())) == 1
    assert all(row.phase == "physical" and row.stage is None for row in _structure_selects(physical.structure_snapshot()))
    assert all(row.phase == "retention" and row.stage == 1 for row in _structure_selects(retention.structure_snapshot()))
    physical.assert_structure_matches(physical.structure_snapshot())
    with pytest.raises(AssertionError, match="structure comparison differs"):
        retention.assert_structure_matches(physical.structure_snapshot())


@pytest.mark.asyncio
@pytest.mark.parametrize("cut", ["throw", "cancel", "window-entry", "clock-entry"])
async def test_structure_original_unwind_and_entry_failure_never_claim_returned_completion(monkeypatch, request, cut):
    from sqlalchemy import event

    from elspeth.web.sessions.locking import locked_session_transaction
    from tests.helpers.continuation_policy import minimal_continuation_policy

    _require_structure_api()
    engine = request.getfixturevalue("engine")
    marker = RuntimeError("original structure lifecycle marker")
    entered = asyncio.Event()
    observed_cancellations = []

    def fail_begin(_conn):
        raise marker

    async def operation():
        with locked_session_transaction(engine, "structure-terminal"):
            entered.set()
            if cut == "cancel":
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError as error:
                    observed_cancellations.append(error)
                    raise
            raise marker

    prior = sys.getprofile()
    trace = RetentionMemoTrace(monkeypatch, request)
    trace.observe_structure(engines=(engine,), entrypoint="calibration")
    timer = clock.ContinuationActionClock(minimal_continuation_policy().actions.session_sql)
    if cut != "clock-entry":
        timer.finish_preparation()
    if cut == "window-entry":
        event.listen(engine, "begin", fail_begin)
    try:
        if cut == "cancel":
            task = asyncio.create_task(timer.run("retention", operation))
            await entered.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError) as caught:
                await task
            assert observed_cancellations == [caught.value]
        elif cut == "clock-entry":
            with pytest.raises(ValueError, match="preparation unbound"):
                await timer.run("retention", operation)
        else:
            with pytest.raises(RuntimeError) as caught:
                await timer.run("retention", operation)
            assert caught.value is marker
    finally:
        if cut == "window-entry":
            event.remove(engine, "begin", fail_begin)
    trace.finish(stages=0 if cut == "clock-entry" else 1, require_snapshot=False, require_population=False, structure_observation=True)
    snapshot = trace.structure_snapshot()
    assert snapshot.closed and snapshot.complete and sys.getprofile() is prior
    assert len(_structure_events(snapshot, "clock_attempt")) == len(_structure_events(snapshot, "clock_terminal")) == 1
    assert _structure_events(snapshot, "clock_terminal")[0].outcome == "unwound"
    assert len(_structure_events(snapshot, "window_ready")) == int(cut in ("throw", "cancel"))
    if cut != "clock-entry":
        assert len(_structure_events(snapshot, "window_attempt")) == len(_structure_events(snapshot, "window_terminal")) == 1
        assert _structure_events(snapshot, "window_terminal")[0].outcome == "unwound"


@pytest.mark.asyncio
async def test_structure_nested_original_clocks_and_windows_restore_the_outer_carrier(monkeypatch, request):
    from elspeth.web.sessions.locking import locked_session_transaction
    from tests.helpers.continuation_policy import minimal_continuation_policy

    _require_structure_api()
    engine = request.getfixturevalue("engine")
    second = _structure_secondary_engine()
    trace = RetentionMemoTrace(monkeypatch, request)
    trace.observe_structure(engines=(engine, second), entrypoint="calibration")
    policy = minimal_continuation_policy().actions.session_sql

    async def inner():
        return _structure_sql_work(second)

    async def outer():
        memo = current_retention_stage_memo()
        with locked_session_transaction(engine, "structure-outer"):
            timer = clock.ContinuationActionClock(policy)
            timer.finish_preparation()
            assert await timer.run("retention", inner) == 0
            assert current_retention_stage_memo() is memo
        return 0

    try:
        timer = clock.ContinuationActionClock(policy)
        timer.finish_preparation()
        assert await timer.run("retention", outer) == 0
        trace.finish(stages=2, require_snapshot=False, require_population=False, structure_observation=True)
        snapshot = trace.structure_snapshot()
        assert len(_structure_events(snapshot, "clock_attempt")) == len(_structure_events(snapshot, "window_ready")) == 2
        assert len({row.stage for row in _structure_events(snapshot, "window_ready")}) == 2
        assert snapshot.complete and all(row.outcome == "returned" for row in _structure_events(snapshot, "window_terminal"))
        assert any(edge.kind == "contains" for edge in snapshot.edges)
    finally:
        second.dispose()


@pytest.mark.asyncio
async def test_structure_native_identity_reuse_after_retirement_does_not_drop_invocations(monkeypatch, request):
    from elspeth.web.sessions.locking import locked_session_transaction
    from tests.helpers.continuation_policy import minimal_continuation_policy

    _require_structure_api()
    engine = request.getfixturevalue("engine")
    trace = RetentionMemoTrace(monkeypatch, request)
    trace.observe_structure(engines=(engine,), entrypoint="calibration")
    clock_ids, window_ids = [], []

    async def operation():
        manager = locked_session_transaction(engine, "structure-retirement")
        window_ids.append(id(manager.gen.gi_frame))
        with manager:
            pass
        assert manager.gen.gi_frame is None
        return 0

    for _ in range(64):
        timer = clock.ContinuationActionClock(minimal_continuation_policy().actions.session_sql)
        timer.finish_preparation()
        invocation = timer.run("retention", operation)
        clock_ids.append(id(invocation.cr_frame))
        assert await invocation == 0 and invocation.cr_frame is None
    trace.finish(stages=64, require_snapshot=False, require_population=False, structure_observation=True)
    snapshot = trace.structure_snapshot()
    assert len(set(clock_ids)) < len(clock_ids), "calibration did not exercise actual coroutine frame identity reuse"
    assert len(set(window_ids)) < len(window_ids), "calibration did not exercise actual generator frame identity reuse"
    assert len(_structure_events(snapshot, "clock_attempt")) == len(_structure_events(snapshot, "clock_terminal")) == 64
    assert len(_structure_events(snapshot, "window_ready")) == len(_structure_events(snapshot, "window_terminal")) == 64


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["same", "extra-select", "predicate", "arity", "empty-arity", "clock", "window", "read-order"])
async def test_structure_real_sql_shape_and_order_comparison_refuses_missing_or_changed_work(monkeypatch, request, change):
    from sqlalchemy import select

    from elspeth.web.sessions.locking import locked_session_transaction
    from elspeth.web.sessions.models import sessions_table

    _require_structure_api()
    engine = request.getfixturevalue("engine")

    async def baseline():
        if change == "read-order":
            with locked_session_transaction(engine, "structure-order") as conn:
                assert conn.execute(select(sessions_table.c.id)).all() == []
                assert conn.execute(select(sessions_table.c.user_id)).all() == []
            return 0
        return _structure_sql_work(engine)

    async def changed():
        if change == "read-order":
            with locked_session_transaction(engine, "structure-order") as conn:
                assert conn.execute(select(sessions_table.c.user_id)).all() == []
                assert conn.execute(select(sessions_table.c.id)).all() == []
            return 0
        return _structure_sql_work(
            engine,
            extra=change == "extra-select",
            predicate=change == "predicate",
            arity=2 if change == "arity" else 0 if change == "empty-arity" else 1,
            clock_read=change != "clock",
            window=change != "window",
        )

    _result, first = await _structure_trial(monkeypatch, request, (engine,), baseline)
    first.assert_structure_matches(first.structure_snapshot())
    if change == "window":
        with pytest.raises(AssertionError, match="structure observation incomplete"):
            await _structure_trial(monkeypatch, request, (engine,), changed)
        return
    _result, second = await _structure_trial(monkeypatch, request, (engine,), changed)
    if change == "same":
        second.assert_structure_matches(first.structure_snapshot())
    else:
        with pytest.raises(AssertionError, match="structure comparison differs"):
            second.assert_structure_matches(first.structure_snapshot())
        if change == "extra-select":
            assert len(_structure_selects(second.structure_snapshot())) == len(_structure_selects(first.structure_snapshot())) + 1
        elif change in ("arity", "empty-arity"):
            assert (
                _structure_selects(first.structure_snapshot())[0].sql.bind_arities
                != _structure_selects(second.structure_snapshot())[0].sql.bind_arities
            )


def _structure_actual_context(engine, *, continuation=True):
    from elspeth.contracts.session_operation import SessionOperationKind
    from elspeth.web.coordination.composer_operation_authority import ComposerAsyncOperationAuthority
    from elspeth.web.coordination.sqlite_authority import SQLiteLocalSessionOperationAuthority
    from tests.fixtures.identities import ensure_test_identity
    from tests.unit.web.sessions.test_continuation_cold_restore import admit_continuation, start_continuation

    with engine.begin() as conn:
        ensure_test_identity(conn, identity_id="alice")
    repository = SQLiteLocalSessionOperationAuthority(engine)
    session = repository.create_session_with_initial_fence(
        user_id="alice", title="Structure", auth_provider_type="local", owner_instance_id="test-owner", lease_seconds=30
    )
    if not continuation:
        context = repository.acquire(
            session_id=session.id, operation_kind=SessionOperationKind.COMPOSE, owner_instance_id="test-owner", lease_seconds=30
        )
        return repository, context
    authority = ComposerAsyncOperationAuthority(engine, owner_instance_id="test-owner", claim_lease_seconds=30)
    admit_continuation((engine, repository, authority, None, session.id))
    return repository, start_continuation(repository, authority).session_operation_context


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["true", "early-false", "raised"])
async def test_structure_actual_admission_records_reads_clock_and_terminal_outcome(monkeypatch, request, outcome):
    from uuid import uuid4

    from elspeth.web.sessions.composer_operations import ComposerOperationFenceLost
    from elspeth.web.sessions.continuation import require_continuation_mutation
    from elspeth.web.sessions.locking import locked_session_transaction
    from tests.helpers.continuation_policy import minimal_continuation_policy

    _require_structure_api()
    engine = request.getfixturevalue("engine")
    repository, original = _structure_actual_context(engine, continuation=outcome != "early-false")
    context = replace(original, fence=replace(original.fence, lease_token=str(uuid4()))) if outcome == "raised" else original
    trace = RetentionMemoTrace(monkeypatch, request)
    trace.observe_structure(engines=(engine,), entrypoint="calibration")

    async def operation():
        with locked_session_transaction(engine, str(context.fence.session_id)) as conn:
            return require_continuation_mutation(conn, context)

    try:
        timer = clock.ContinuationActionClock(minimal_continuation_policy().actions.session_sql)
        timer.finish_preparation()
        if outcome == "raised":
            with pytest.raises(ComposerOperationFenceLost):
                await timer.run("retention", operation)
        else:
            assert await timer.run("retention", operation) is (outcome == "true")
        trace.finish(stages=1, require_snapshot=False, require_population=False, structure_observation=True)
        snapshot = trace.structure_snapshot()
        assert len(_structure_events(snapshot, "admission_attempt")) == len(_structure_events(snapshot, "admission_terminal")) == 1
        assert _structure_selects(snapshot)
        assert len(_structure_events(snapshot, "db_clock_attempt")) == int(outcome != "early-false")
        terminal = _structure_events(snapshot, "admission_terminal")[0]
        if outcome == "raised":
            assert terminal.outcome == "unwound"
        else:
            assert terminal.outcome == "returned" and terminal.result_kind == ("bool_true" if outcome == "true" else "bool_false")
    finally:
        repository.release(original)


@pytest.mark.asyncio
async def test_structure_actual_admission_omission_is_not_an_equal_structural_pair(monkeypatch, request):
    _require_structure_api()
    engine = request.getfixturevalue("engine")
    repository, context = _structure_actual_context(engine)

    async def admitted():
        return _structure_sql_work(engine, context=context)

    async def omitted():
        return _structure_sql_work(engine)

    try:
        _result, first = await _structure_trial(monkeypatch, request, (engine,), admitted)
        _result, second = await _structure_trial(monkeypatch, request, (engine,), omitted)
        assert len(_structure_events(first.structure_snapshot(), "admission_attempt")) == 1
        assert not _structure_events(second.structure_snapshot(), "admission_attempt")
        with pytest.raises(AssertionError, match="structure comparison differs"):
            second.assert_structure_matches(first.structure_snapshot())
    finally:
        repository.release(context)


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", ["missing-carrier", "unobserved-carrier", "different-known-carrier", "engine", "source"])
async def test_structure_raw_real_work_survives_source_or_carrier_refusal(monkeypatch, request, changed):
    from elspeth.web import continuation_retention_memo as memo_module
    from elspeth.web.coordination import database_clock
    from tests.helpers.continuation_policy import minimal_continuation_policy

    _require_structure_api()
    engine = request.getfixturevalue("engine")
    foreign = _structure_secondary_engine()
    trace = RetentionMemoTrace(monkeypatch, request)
    trace.observe_structure(engines=(engine,), entrypoint="calibration")
    policy = minimal_continuation_policy().actions.session_sql
    original_clock = database_clock.database_now

    def alias(conn):
        return original_clock(conn)

    async def altered(outer):
        if changed == "engine":
            return _structure_sql_work(foreign)
        if changed == "source":
            with monkeypatch.context() as patch:
                patch.setattr(database_clock, "database_now", alias)
                return _structure_sql_work(engine)
        actual = outer if changed == "different-known-carrier" else RetentionStageMemo() if changed == "unobserved-carrier" else None
        token = memo_module._RETENTION_STAGE_MEMO.set(actual)
        try:
            return _structure_sql_work(engine)
        finally:
            memo_module._RETENTION_STAGE_MEMO.reset(token)
            if changed == "unobserved-carrier":
                actual.close()

    async def operation():
        outer = current_retention_stage_memo()
        if changed == "different-known-carrier":
            timer = clock.ContinuationActionClock(policy)
            timer.finish_preparation()

            async def inner():
                return await altered(outer)

            return await timer.run("retention", inner)
        return await altered(outer)

    try:
        timer = clock.ContinuationActionClock(policy)
        timer.finish_preparation()
        await timer.run("retention", operation)
        with pytest.raises(AssertionError, match="structure observation incomplete"):
            trace.finish(
                stages=2 if changed == "different-known-carrier" else 1,
                require_snapshot=False,
                require_population=False,
                structure_observation=True,
            )
        snapshot = trace.structure_snapshot()
        assert snapshot.closed and not snapshot.complete and not snapshot.observation_verified and snapshot.errors
        assert _structure_events(snapshot, "sql_attempt"), "actual raw SQL must remain visible before failed attribution"
        if changed == "source":
            assert len(_structure_events(snapshot, "db_clock_attempt")) == 1
        elif "carrier" in changed:
            assert any(row.stage != row.expected_stage for row in _structure_events(snapshot, "sql_attempt"))
    finally:
        foreign.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("literal_sql", [False, True])
async def test_structure_never_exports_sql_literal_parameter_or_result_values(monkeypatch, request, literal_sql):
    from sqlalchemy import text

    from elspeth.web.sessions.locking import locked_session_transaction

    _require_structure_api()
    engine = request.getfixturevalue("engine")
    canary = "STRUCTURE_PRIVATE_VALUE_MUST_NOT_BE_EXPORTED"

    async def operation():
        if literal_sql:
            with locked_session_transaction(engine, "structure-private") as conn:
                assert conn.execute(text("SELECT 'STRUCTURE_PRIVATE_VALUE_MUST_NOT_BE_EXPORTED'")).scalar_one() == canary
            return 0
        return _structure_sql_work(engine, canary=canary)

    if literal_sql:
        from tests.helpers.continuation_policy import minimal_continuation_policy

        trace = RetentionMemoTrace(monkeypatch, request)
        trace.observe_structure(engines=(engine,), entrypoint="calibration")
        timer = clock.ContinuationActionClock(minimal_continuation_policy().actions.session_sql)
        timer.finish_preparation()
        assert await timer.run("retention", operation) == 0
        with pytest.raises(AssertionError, match="structure observation incomplete"):
            trace.finish(stages=1, require_snapshot=False, require_population=False, structure_observation=True)
        assert not trace.structure_snapshot().complete
    else:
        _result, trace = await _structure_trial(monkeypatch, request, (engine,), operation)
    assert canary not in json.dumps(asdict(trace.structure_snapshot()))
    assert _structure_events(trace.structure_snapshot(), "sql_attempt")


@pytest.mark.asyncio
@pytest.mark.parametrize("worker", [False, True])
@pytest.mark.parametrize("schema_work", [False, True])
async def test_structure_listener_reentry_keeps_b4_audits_and_application_lock_safety(monkeypatch, request, worker, schema_work):
    from sqlalchemy import event
    from sqlalchemy.sql import Select

    from elspeth.web.dependencies import create_catalog_service
    from tests.helpers.continuation_policy import minimal_continuation_policy

    _require_structure_api()
    engine = request.getfixturevalue("engine")
    catalog = create_catalog_service()
    plugin = next(plugin for plugin in catalog._source_classes if plugin.name == "csv")
    factory, alias = catalog._catalog_schema, plugin.get_config_schema
    expected = alias()
    seen = []

    def foreign_listener(_conn, _cursor, _statement, _parameters, context, _executemany):
        if context.compiled is not None and type(context.compiled.statement) is Select:
            seen.append(1)
            if schema_work:
                assert factory(plugin, "source") == expected and alias() == expected

    event.listen(engine, "before_cursor_execute", foreign_listener)
    prior = sys.getprofile()
    trace = RetentionMemoTrace(monkeypatch, request, require_catalog_schema=True, catalog=catalog)
    trace.observe_structure(engines=(engine,), entrypoint="calibration")
    handles = trace.structure_listener_handles()
    assert handles and all(event.contains(target, name, callback) for target, name, callback in handles)
    ticket = publication_ticket()

    def work():
        memo = current_retention_stage_memo()
        assert type(memo) is RetentionStageMemo
        with memo._lock:
            return _structure_sql_work(engine, clock_read=False)

    async def operation():
        if worker:
            result = await async_workers.run_required_sql_finish_once(ticket, work)
            assert isinstance(result, RequiredSQLReturned)
            return result.value
        return work()

    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            monkeypatch.setattr(async_workers, "_SHARED_EXECUTOR", pool)
            timer = clock.ContinuationActionClock(minimal_continuation_policy().actions.session_sql)
            timer.finish_preparation()
            assert await timer.run("retention", operation) == 0
        assert seen == [1]
        row = trace.catalog_schema_totals()[0]
        assert row.factory_attempts == int(schema_work) and row.plugin_attempts == 2 * int(schema_work)
        if schema_work:
            with pytest.raises(AssertionError, match="catalog schema construction budget exceeded"):
                trace.finish(
                    stages=1, require_snapshot=False, require_population=False, catalog_schema_budget=True, structure_observation=True
                )
        else:
            trace.finish(stages=1, require_snapshot=False, require_population=False, catalog_schema_budget=True, structure_observation=True)
        assert trace.structure_snapshot().closed and trace.structure_snapshot().complete
        assert trace.structure_snapshot().observation_verified is not schema_work
        assert sys.getprofile() is prior and event.contains(engine, "before_cursor_execute", foreign_listener)
        assert all(not event.contains(target, name, callback) for target, name, callback in handles)
        if worker:
            assert ticket.complete and async_workers.outstanding_admissions() == 0
    finally:
        event.remove(engine, "before_cursor_execute", foreign_listener)


@pytest.mark.asyncio
async def test_structure_independent_worker_order_uses_owner_sequences_and_causal_edges(monkeypatch, request):
    _require_structure_api()
    engine = request.getfixturevalue("engine")
    second = _structure_secondary_engine()

    async def trial(reverse):
        first_done, second_done = threading.Event(), threading.Event()
        tickets = (publication_ticket(), publication_ticket())
        completions = []

        def first_work():
            if reverse:
                assert second_done.wait(2)
            result = _structure_sql_work(engine)
            completions.append(1)
            first_done.set()
            return result

        def second_work():
            if not reverse:
                assert first_done.wait(2)
            result = _structure_sql_work(second)
            completions.append(2)
            second_done.set()
            return result

        async def operation():
            results = await asyncio.gather(
                async_workers.run_required_sql_finish_once(tickets[0], first_work),
                async_workers.run_required_sql_finish_once(tickets[1], second_work),
            )
            assert all(isinstance(result, RequiredSQLReturned) and result.value == 0 for result in results)
            return 0

        with ThreadPoolExecutor(max_workers=2) as pool, monkeypatch.context() as patch:
            patch.setattr(async_workers, "_SHARED_EXECUTOR", pool)
            _result, trace = await _structure_trial(patch, request, (engine, second), operation)
        assert all(ticket.complete for ticket in tickets) and async_workers.outstanding_admissions() == 0
        assert completions == ([2, 1] if reverse else [1, 2])
        return trace

    try:
        first = await trial(False)
        second_trace = await trial(True)
        first.assert_structure_matches(first.structure_snapshot())
        second_trace.assert_structure_matches(first.structure_snapshot())
        snapshot = second_trace.structure_snapshot()
        assert len(_structure_events(snapshot, "worker_enter")) == len(_structure_events(snapshot, "submission_join")) == 2
        for kind in ("owner_order", "contains", "submit_to_worker", "worker_to_join"):
            assert any(edge.kind == kind for edge in snapshot.edges)
            changed = replace(snapshot, edges=tuple(edge for edge in snapshot.edges if edge.kind != kind))
            with pytest.raises(AssertionError, match="structure comparison"):
                second_trace.assert_structure_matches(changed)
    finally:
        second.dispose()


@pytest.mark.asyncio
async def test_structure_commit_callback_is_only_an_attempt_when_original_commit_raises(monkeypatch, request):
    from sqlalchemy import event

    from tests.helpers.continuation_policy import minimal_continuation_policy

    _require_structure_api()
    engine = request.getfixturevalue("engine")
    marker = RuntimeError("original commit callback marker")
    seen = []

    def fail_commit(_conn):
        seen.append(1)
        raise marker

    trace = RetentionMemoTrace(monkeypatch, request)
    trace.observe_structure(engines=(engine,), entrypoint="calibration")
    event.listen(engine, "commit", fail_commit)

    async def operation():
        return _structure_sql_work(engine)

    try:
        timer = clock.ContinuationActionClock(minimal_continuation_policy().actions.session_sql)
        timer.finish_preparation()
        with pytest.raises(RuntimeError) as caught:
            await timer.run("retention", operation)
        assert caught.value is marker and seen == [1]
    finally:
        event.remove(engine, "commit", fail_commit)
    trace.finish(stages=1, require_snapshot=False, require_population=False, structure_observation=True)
    snapshot = trace.structure_snapshot()
    assert len(_structure_events(snapshot, "transaction_commit_attempt")) == 1
    assert _structure_events(snapshot, "window_terminal")[0].outcome == "unwound"
    assert not any(event.kind == "transaction_committed" for event in snapshot.events)


@pytest.mark.asyncio
@pytest.mark.parametrize("after_install", [False, True])
async def test_structure_listener_install_failure_rolls_back_only_owned_callbacks(monkeypatch, request, after_install):
    from sqlalchemy import event, select

    from elspeth.web.sessions.models import sessions_table

    _require_structure_api()
    engine = request.getfixturevalue("engine")
    marker = RuntimeError("original structure listener install error")
    foreign_calls, attempted = [], []

    def foreign(_conn, _cursor, _statement, _parameters, _context, _executemany):
        foreign_calls.append(1)

    event.listen(engine, "before_cursor_execute", foreign)
    original_listen = event.listen

    def fails(target, name, callback, *args, **kwargs):
        if target is engine:
            attempted.append((target, name, callback))
            if len(attempted) == 2:
                if after_install:
                    original_listen(target, name, callback, *args, **kwargs)
                raise marker
        return original_listen(target, name, callback, *args, **kwargs)

    prior = sys.getprofile()
    trace = RetentionMemoTrace(monkeypatch, request)
    try:
        with monkeypatch.context() as patch:
            patch.setattr(event, "listen", fails)
            with pytest.raises(RuntimeError) as caught:
                trace.observe_structure(engines=(engine,), entrypoint="calibration")
            assert caught.value is marker
        assert len(attempted) == 2 and all(not event.contains(target, name, callback) for target, name, callback in attempted)
        assert event.contains(engine, "before_cursor_execute", foreign)
        with engine.connect() as conn:
            assert conn.execute(select(sessions_table.c.id)).all() == []
        assert foreign_calls
        with pytest.raises(AssertionError, match="structure observation incomplete"):
            trace.finish(stages=0, require_snapshot=False, require_population=False, structure_observation=True)
        assert sys.getprofile() is prior and not trace.structure_snapshot().observation_verified
    finally:
        event.remove(engine, "before_cursor_execute", foreign)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["omitted", "later-original", "tamper"])
async def test_structure_explicit_terminal_qualification_preserves_original_finish_failure(monkeypatch, request, failure):
    from tests.helpers.continuation_policy import minimal_continuation_policy

    _require_structure_api()
    engine = request.getfixturevalue("engine")
    trace = RetentionMemoTrace(monkeypatch, request)
    trace.observe_structure(engines=(engine,), entrypoint="calibration")

    async def operation():
        return _structure_sql_work(engine)

    timer = clock.ContinuationActionClock(minimal_continuation_policy().actions.session_sql)
    timer.finish_preparation()
    await timer.run("retention", operation)
    if failure == "omitted":
        with pytest.raises(AssertionError, match="structure observation assertion is required"):
            trace.finish(stages=1, require_snapshot=False, require_population=False)
    elif failure == "later-original":
        with pytest.raises(AssertionError, match="actual retention must request snapshot work"):
            trace.finish(stages=1, require_population=False, structure_observation=True)
    else:
        trace.finish(stages=1, require_snapshot=False, require_population=False, structure_observation=True)
        trace._structure_observation_verified = False
        with pytest.raises(AssertionError, match="terminal"):
            _FINALIZE_MEMO_TRACE(trace)
        trace._structure_observation_verified = True
    snapshot = trace.structure_snapshot()
    assert snapshot.closed and snapshot.complete and snapshot.observation_verified is (failure == "tamper")
    _FINALIZE_MEMO_TRACE(trace)


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["open", "empty", "unobserved"])
async def test_structure_missing_or_open_observation_cannot_be_a_passing_empty_pair(monkeypatch, request, case):
    _require_structure_api()
    engine = request.getfixturevalue("engine")
    trace = RetentionMemoTrace(monkeypatch, request)
    if case != "unobserved":
        trace.observe_structure(engines=(engine,), entrypoint="calibration")
        if case == "open":
            with pytest.raises(AssertionError, match="structure comparison requires closed complete observations"):
                trace.assert_structure_matches(trace.structure_snapshot())
    message = "structure observation requires explicit mode" if case == "unobserved" else "structure observation incomplete"
    with pytest.raises(AssertionError, match=message):
        trace.finish(stages=0, require_snapshot=False, require_population=False, structure_observation=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("marker_type", [RuntimeError, KeyboardInterrupt, SystemExit, asyncio.CancelledError])
async def test_structure_primary_native_exception_is_identical_and_never_budget_qualified(monkeypatch, request, marker_type):
    from tests.helpers.continuation_policy import minimal_continuation_policy

    _require_structure_api()
    engine = request.getfixturevalue("engine")
    marker = marker_type("original structure primary outcome")
    prior = sys.getprofile()
    trace = RetentionMemoTrace(monkeypatch, request)
    trace.observe_structure(engines=(engine,), entrypoint="calibration")

    async def operation():
        _structure_sql_work(engine)
        raise marker

    timer = clock.ContinuationActionClock(minimal_continuation_policy().actions.session_sql)
    timer.finish_preparation()
    with pytest.raises(marker_type) as caught:
        try:
            await timer.run("retention", operation)
        finally:
            trace.finish(stages=1, require_snapshot=False, require_population=False, structure_observation=True)
    assert caught.value is marker and sys.getprofile() is prior
    assert trace.structure_snapshot().closed and not trace.structure_snapshot().observation_verified
    _FINALIZE_MEMO_TRACE(trace)


def _structure_force_lookup(memo, entries, key, compute):
    from elspeth.web import continuation_retention_memo as memo_module

    with memo._lock:
        closed = memo._closed
        hit = entries.get(key, memo_module._MISSING) if not closed else memo_module._MISSING
    if hit is not memo_module._MISSING:
        compute()
        return hit
    value = compute()
    with memo._lock:
        if memo._closed:
            return value
        return entries.setdefault(key, value)


@pytest.mark.parametrize("close", [False, True])
@pytest.mark.parametrize("forced_error", [False, True])
def test_structure_lookup_opponent_keeps_original_warm_identity_and_forced_error(close, forced_error):
    from elspeth.web.continuation_retention_memo import SnapshotDecodeKey

    _payload, encoded, _expected = _snapshot_artifact_fixture()
    memo = RetentionStageMemo()
    key = SnapshotDecodeKey(encoded)
    winner = memo.snapshot(key, _DECODE_WORK)
    calls = []
    marker = RuntimeError("original forced snapshot error")

    def compute():
        calls.append(1)
        value = _DECODE_WORK(encoded)
        assert value == winner and value is not winner
        if close:
            memo.close()
        if forced_error:
            raise marker
        return value

    assert memo._lookup(memo._snapshots, (_DECODE_WORK, key), compute) is winner and calls == []
    if forced_error:
        with pytest.raises(RuntimeError) as caught:
            _structure_force_lookup(memo, memo._snapshots, (_DECODE_WORK, key), compute)
        assert caught.value is marker
    else:
        assert _structure_force_lookup(memo, memo._snapshots, (_DECODE_WORK, key), compute) is winner
    assert calls == [1]
    assert memo.cached_entry_count == int(not close)
    memo.close()
    later = RetentionStageMemo()
    assert later.snapshot(key, _DECODE_WORK) == winner and later.snapshot(key, _DECODE_WORK) is not winner
    later.close()


@pytest.mark.parametrize("opponent", [False, True])
def test_structure_lookup_opponent_preserves_real_cold_winning_publication_and_errors(opponent):
    from elspeth.web.continuation_retention_memo import SnapshotDecodeKey

    _payload, encoded, _expected = _snapshot_artifact_fixture()
    memo = RetentionStageMemo()
    key = SnapshotDecodeKey(encoded)
    barrier = threading.Barrier(2)
    values = []
    lock = threading.Lock()
    lookup = _structure_force_lookup if opponent else RetentionStageMemo._lookup

    def compute():
        value = _DECODE_WORK(encoded)
        with lock:
            values.append(value)
        barrier.wait(timeout=2)
        return value

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(lookup, memo, memo._snapshots, (_DECODE_WORK, key), compute) for _ in range(2)]
        results = [future.result(timeout=3) for future in futures]
    assert len(values) == 2 and values[0] == values[1] and values[0] is not values[1]
    assert results[0] is results[1] and any(results[0] is value for value in values)
    assert memo.cached_entry_count == 1
    marker = RuntimeError("original cold compute error")

    def fail():
        raise marker

    missing = SnapshotDecodeKey(encoded + " ")
    with pytest.raises(RuntimeError) as caught:
        lookup(memo, memo._snapshots, (_DECODE_WORK, missing), fail)
    assert caught.value is marker and memo.cached_entry_count == 1
    memo.close()


@pytest.mark.parametrize("opponent", [False, True])
def test_structure_lookup_opponent_cold_close_returns_computed_value_without_publication(opponent):
    from elspeth.web.continuation_retention_memo import SnapshotDecodeKey

    _payload, encoded, _expected = _snapshot_artifact_fixture()
    memo = RetentionStageMemo()
    key = SnapshotDecodeKey(encoded)
    produced = []

    def compute():
        value = _DECODE_WORK(encoded)
        produced.append(value)
        memo.close()
        return value

    lookup = _structure_force_lookup if opponent else RetentionStageMemo._lookup
    result = lookup(memo, memo._snapshots, (_DECODE_WORK, key), compute)
    assert produced == [result] and result is produced[0]
    assert not memo.active and memo.cached_entry_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("late_error", [False, True])
async def test_structure_public_request_terminal_survives_a_late_after_cursor_error(monkeypatch, request, late_error):
    from sqlalchemy import event, select
    from sqlalchemy.sql import Select

    from elspeth.web.sessions.locking import locked_session_transaction
    from elspeth.web.sessions.models import sessions_table
    from tests.helpers.continuation_policy import minimal_continuation_policy

    _require_structure_api()
    engine = request.getfixturevalue("engine")
    marker = RuntimeError("original late after-cursor marker")
    prior = sys.getprofile()
    trace = RetentionMemoTrace(monkeypatch, request)
    trace.observe_structure(engines=(engine,), entrypoint="calibration")
    handles = trace.structure_listener_handles()
    after_driver = []

    def late(_conn, _cursor, _statement, _parameters, context, _many):
        if context.compiled is not None and type(context.invoked_statement) is Select:
            # SQLAlchemy calls this only after the original driver AND the
            # observer's earlier after-cursor listener have returned.
            after_driver.append(1)
            if late_error:
                raise marker

    event.listen(engine, "after_cursor_execute", late)

    async def operation():
        with locked_session_transaction(engine, "late-request-terminal") as conn:
            if late_error:
                with pytest.raises(RuntimeError) as caught:
                    conn.execute(select(sessions_table.c.id))
                assert caught.value is marker
            else:
                assert conn.execute(select(sessions_table.c.id)).all() == []
        return 0

    try:
        timer = clock.ContinuationActionClock(minimal_continuation_policy().actions.session_sql)
        timer.finish_preparation()
        assert await timer.run("retention", operation) == 0
        assert after_driver == [1]
        trace.finish(stages=1, require_snapshot=False, require_population=False, structure_observation=True)
        snapshot = trace.structure_snapshot()
        assert snapshot.closed and snapshot.complete and snapshot.observation_verified
        assert sys.getprofile() is prior
        assert all(not event.contains(target, name, callback) for target, name, callback in handles)
        assert event.contains(engine, "after_cursor_execute", late)
        attempts = _structure_selects(snapshot)
        terminals = tuple(row for row in _structure_events(snapshot, "sql_return") if row.sql is not None and row.sql.kind == "select")
        assert len(attempts) == len(terminals) == 1
        assert attempts[0].sql == terminals[0].sql and terminals[0].sql.bind_arities == (0,)
        assert terminals[0].outcome == ("unwound" if late_error else "returned")
    finally:
        event.remove(engine, "after_cursor_execute", late)


@pytest.mark.asyncio
async def test_structure_real_select_driver_unwind_has_no_after_cursor_success(monkeypatch, request):
    import sqlite3

    from sqlalchemy import bindparam, event, select
    from sqlalchemy.exc import ProgrammingError
    from sqlalchemy.sql import Select

    from elspeth.web.sessions.locking import locked_session_transaction
    from elspeth.web.sessions.models import sessions_table
    from tests.helpers.continuation_policy import minimal_continuation_policy

    _require_structure_api()
    engine = request.getfixturevalue("engine")
    trace = RetentionMemoTrace(monkeypatch, request)
    trace.observe_structure(engines=(engine,), entrypoint="calibration")
    before, after, originals = [], [], []

    def before_cursor(_conn, _cursor, _statement, _parameters, context, _many):
        if context.compiled is not None and type(context.invoked_statement) is Select:
            before.append(1)

    def after_cursor(_conn, _cursor, _statement, _parameters, context, _many):
        if context.compiled is not None and type(context.invoked_statement) is Select:
            after.append(1)

    event.listen(engine, "before_cursor_execute", before_cursor)
    event.listen(engine, "after_cursor_execute", after_cursor)

    async def operation():
        with locked_session_transaction(engine, "real-driver-unwind") as conn:
            with pytest.raises(ProgrammingError) as caught:
                # Stock String binding has no adapter for this tuple. The
                # genuine sqlite cursor raises inside original do_execute.
                conn.execute(select(sessions_table.c.id).where(sessions_table.c.id == bindparam("invalid")), {"invalid": (1,)})
            assert type(caught.value.orig) is sqlite3.ProgrammingError
            originals.append(caught.value.orig)
        return 0

    try:
        timer = clock.ContinuationActionClock(minimal_continuation_policy().actions.session_sql)
        timer.finish_preparation()
        assert await timer.run("retention", operation) == 0
        assert before == [1] and after == [] and len(originals) == 1
        trace.finish(stages=1, require_snapshot=False, require_population=False, structure_observation=True)
        snapshot = trace.structure_snapshot()
        assert snapshot.closed and snapshot.complete and snapshot.observation_verified
        terminals = tuple(row for row in _structure_events(snapshot, "sql_return") if row.sql is not None and row.sql.kind == "select")
        assert len(_structure_selects(snapshot)) == len(terminals) == 1
        assert terminals[0].outcome == "unwound" and terminals[0].sql.bind_arities == (1,)
    finally:
        event.remove(engine, "before_cursor_execute", before_cursor)
        event.remove(engine, "after_cursor_execute", after_cursor)


@pytest.mark.asyncio
@pytest.mark.parametrize("body_error", [False, True])
async def test_structure_worker_close_restores_the_loop_and_reusable_worker(monkeypatch, request, body_error):
    from sqlalchemy import event

    from tests.helpers.continuation_policy import minimal_continuation_policy

    _require_structure_api()
    engine = request.getfixturevalue("engine")
    prior = sys.getprofile()
    marker = RuntimeError("original worker after-close error")
    body_threads = []

    def worker_prior(_frame, _event, _arg):
        return None

    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(sys.setprofile, worker_prior).result(timeout=2)
        monkeypatch.setattr(async_workers, "_SHARED_EXECUTOR", pool)
        trace = RetentionMemoTrace(monkeypatch, request)
        installed_loop = sys.getprofile()
        trace.observe_structure(engines=(engine,), entrypoint="calibration")
        handles = trace.structure_listener_handles()
        ticket = publication_ticket()

        def work():
            body_threads.append(threading.get_ident())
            assert _structure_sql_work(engine, clock_read=False) == 0
            trace.close()
            if body_error:
                raise marker
            return 7

        async def operation():
            return await async_workers.run_required_sql_finish_once(ticket, work)

        try:
            timer = clock.ContinuationActionClock(minimal_continuation_policy().actions.session_sql)
            timer.finish_preparation()
            outcome = await timer.run("retention", operation)
            if body_error:
                assert type(outcome) is RequiredSQLRaised and outcome.error is marker
            else:
                assert type(outcome) is RequiredSQLReturned and outcome.value == 7
            assert ticket.complete and async_workers.outstanding_admissions() == 0
            with pytest.raises(AssertionError, match="structure observation incomplete"):
                trace.finish(stages=1, require_snapshot=False, require_population=False, structure_observation=True)
            snapshot = trace.structure_snapshot()
            assert snapshot.closed and not snapshot.complete and not snapshot.observation_verified
            assert all(not event.contains(target, name, callback) for target, name, callback in handles)
            reused_thread, reused_profile = pool.submit(lambda: (threading.get_ident(), sys.getprofile())).result(timeout=2)
            assert body_threads == [reused_thread] and reused_profile is worker_prior
            # Decisive old-helper assertion: worker close set _closed, but
            # later owner-loop finish must still restore its installed hook.
            assert sys.getprofile() is prior
        finally:
            if sys.getprofile() is installed_loop:
                sys.setprofile(prior)
            pool.submit(sys.setprofile, None).result(timeout=2)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("boundary", "body_error", "marker_type", "prior_failure"),
    [
        ("normal", False, RuntimeError, False),
        ("normal", True, RuntimeError, False),
        ("worker_install", False, KeyboardInterrupt, False),
        ("worker_install", False, SystemExit, False),
        ("worker_exit", False, KeyboardInterrupt, False),
        ("worker_exit", False, SystemExit, False),
        ("worker_install", False, RuntimeError, True),
        ("worker_exit", False, RuntimeError, True),
    ],
)
async def test_structure_worker_observation_interrupt_preserves_outcome_and_profile(
    monkeypatch, request, boundary, body_error, marker_type, prior_failure
):
    from sqlalchemy import event

    from tests.helpers.continuation_policy import minimal_continuation_policy

    _require_structure_api()
    engine = request.getfixturevalue("engine")
    loop_thread = threading.get_ident()
    prior = sys.getprofile()
    marker = marker_type("original worker observation interruption")
    body_marker = RuntimeError("original worker body error")
    original_observe = RetentionMemoTrace._structure_profile
    entered = []

    def worker_prior(frame, event, _arg):
        if prior_failure and event == "call" and frame.f_code is interrupted.__code__ and frame.f_locals.get("event") == boundary:
            raise marker
        return None

    def interrupted(self, frame, event, arg, expected_id):
        if not prior_failure and event == boundary and threading.get_ident() != loop_thread:
            raise marker
        return original_observe(self, frame, event, arg, expected_id)

    with ThreadPoolExecutor(max_workers=1) as pool, monkeypatch.context() as patch:
        pool.submit(sys.setprofile, worker_prior).result(timeout=2)
        patch.setattr(async_workers, "_SHARED_EXECUTOR", pool)
        patch.setattr(RetentionMemoTrace, "_structure_profile", interrupted)
        trace = RetentionMemoTrace(patch, request)
        installed_loop = sys.getprofile()
        trace.observe_structure(engines=(engine,), entrypoint="calibration")
        handles = trace.structure_listener_handles()
        ticket = publication_ticket()

        def work():
            entered.append(threading.get_ident())
            assert _structure_sql_work(engine, clock_read=False) == 0
            if body_error:
                raise body_marker
            return 9

        async def operation():
            return await async_workers.run_required_sql_finish_once(ticket, work)

        leaked = []
        try:
            timer = clock.ContinuationActionClock(minimal_continuation_policy().actions.session_sql)
            timer.finish_preparation()
            outcome = await timer.run("retention", operation)
            if boundary != "normal":
                assert type(outcome) is RequiredSQLRaised and outcome.error is marker
            elif body_error:
                assert type(outcome) is RequiredSQLRaised and outcome.error is body_marker
            else:
                assert type(outcome) is RequiredSQLReturned and outcome.value == 9
            assert bool(entered) is (boundary != "worker_install")
            assert ticket.complete and async_workers.outstanding_admissions() == 0
            if boundary == "normal":
                trace.finish(stages=1, require_snapshot=False, require_population=False, structure_observation=True)
                assert trace.structure_snapshot().complete and trace.structure_snapshot().observation_verified
            else:
                with pytest.raises(AssertionError, match="observation incomplete"):
                    trace.finish(stages=1, require_snapshot=False, require_population=False, structure_observation=True)
                assert not trace.structure_snapshot().observation_verified
            assert sys.getprofile() is prior
            assert all(not event.contains(target, name, callback) for target, name, callback in handles)
            native_thread, native_profile = pool.submit(lambda: (threading.get_ident(), sys.getprofile())).result(timeout=2)
            leaked.append(native_profile)
            if entered:
                assert entered == [native_thread]
            assert native_profile is (None if prior_failure else worker_prior)
        finally:
            if sys.getprofile() is installed_loop:
                sys.setprofile(prior)

            def restore_worker():
                current = sys.getprofile()
                if current is worker_prior or (
                    type(current) is FunctionType and current.__code__ is installed_loop.__code__ and (not leaked or current is leaked[0])
                ):
                    sys.setprofile(None)

            pool.submit(restore_worker).result(timeout=2)


@pytest.mark.asyncio
@pytest.mark.parametrize("corruption", ["missing", "duplicate", "wrong-owner", "wrong-terminal"])
async def test_structure_comparison_requires_the_actual_lifetime_membership(monkeypatch, request, corruption):
    _require_structure_api()
    engine = request.getfixturevalue("engine")

    async def first():
        return _structure_sql_work(engine, clock_read=False)

    async def resumed():
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        return _structure_sql_work(engine, clock_read=False)

    _value, original = await _structure_trial(monkeypatch, request, (engine,), first)
    _value, repeated = await _structure_trial(monkeypatch, request, (engine,), resumed)
    snapshot = repeated.structure_snapshot()
    first_clock = next(life for life in original.structure_snapshot().lifetimes if life.kind == "clock")
    clock_life = next(life for life in snapshot.lifetimes if life.kind == "clock")
    window_life = next(life for life in snapshot.lifetimes if life.kind == "window")
    assert first_clock.suspensions == first_clock.resumptions == 0
    assert clock_life.suspensions == clock_life.resumptions == 2 and clock_life.terminal == window_life.terminal == "returned"
    repeated.assert_structure_matches(original.structure_snapshot())
    if corruption == "missing":
        changed = replace(snapshot, lifetimes=())
    elif corruption == "duplicate":
        changed = replace(snapshot, lifetimes=(*snapshot.lifetimes, clock_life))
    else:
        wrong = replace(clock_life, owner=window_life.owner) if corruption == "wrong-owner" else replace(clock_life, terminal="unwound")
        changed = replace(snapshot, lifetimes=tuple(wrong if life is clock_life else life for life in snapshot.lifetimes))
    with pytest.raises(AssertionError, match="structure comparison"):
        repeated.assert_structure_matches(changed)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mutation", "timing"),
    [("none", "observed")]
    + [
        (name, timing)
        for name in (
            "column-name",
            "table-name",
            "table-schema",
            "visit_column",
            "visit_select",
            "visit_binary",
            "visit_bindparam",
            "visit_expression_clauselist",
            "visit_grouping",
            "visit_unary",
            "visit_table",
            "visit_null",
            "visit_true",
            "visit_false",
        )
        for timing in ("before-trace", "observed")
    ],
)
async def test_structure_fresh_select_refuses_changed_owned_metadata_or_visitor(monkeypatch, request, mutation, timing):
    from sqlalchemy import and_, bindparam, event, false, null, or_, select, true
    from sqlalchemy.engine.interfaces import CacheStats
    from sqlalchemy.sql import Select

    from elspeth.web.sessions.locking import locked_session_transaction
    from elspeth.web.sessions.models import sessions_table
    from tests.helpers.continuation_policy import minimal_continuation_policy

    _require_structure_api()
    engine = request.getfixturevalue("engine")
    repository, context = _structure_actual_context(engine, continuation=False)
    compiler = engine.dialect.statement_compiler
    column = sessions_table.c.id
    original_name, original_schema = sessions_table.name, sessions_table.schema
    column_name = column.name
    calls, hits = [], []
    target_owner = None
    original_visitor = None
    if mutation.startswith("visit_"):
        target_owner = next(owner for owner in compiler.__mro__ if mutation in owner.__dict__)
        original_visitor = target_owner.__dict__[mutation]

    def visitor(self, node, *args, **kwargs):
        calls.append(1)
        if mutation == "visit_column" and node is column:
            node = sessions_table.c.user_id
        elif mutation == "visit_select":
            node = node.with_only_columns(sessions_table.c.user_id, true(), false(), null())
        return original_visitor(self, node, *args, **kwargs)

    def change():
        if mutation == "column-name":
            column.name = sessions_table.c.user_id.name
        elif mutation == "table-name":
            sessions_table.name = original_name.upper()
        elif mutation == "table-schema":
            sessions_table.schema = "main"
        elif target_owner is not None:
            setattr(target_owner, mutation, visitor)

    def restore():
        column.name = column_name
        sessions_table.name, sessions_table.schema = original_name, original_schema
        if target_owner is not None:
            setattr(target_owner, mutation, original_visitor)

    def fresh(_conn, _cursor, _statement, _parameters, execution, _many):
        if execution.compiled is not None and type(execution.invoked_statement) is Select:
            hits.append(execution.cache_hit)

    engine.clear_compiled_cache()
    event.listen(engine, "after_cursor_execute", fresh)
    trace = None
    try:
        if timing == "before-trace":
            change()
        trace = RetentionMemoTrace(monkeypatch, request)
        trace.observe_structure(engines=(engine,), entrypoint="calibration")
        if timing == "observed":
            change()

        async def operation():
            statement = (
                select(column, true(), false(), null())
                .where(and_(or_(column == bindparam("sid"), sessions_table.c.user_id == bindparam("actor")), column.is_not(None)))
                .order_by(column.desc())
            )
            with locked_session_transaction(engine, str(context.fence.session_id)) as conn:
                rows = conn.execute(statement, {"sid": str(context.fence.session_id), "actor": "alice"}).all()
            expected = "alice" if mutation in ("column-name", "visit_column", "visit_select") else str(context.fence.session_id)
            assert len(rows) == 1 and tuple(rows[0]) == (expected, True, False, None)
            return 0

        try:
            timer = clock.ContinuationActionClock(minimal_continuation_policy().actions.session_sql)
            timer.finish_preparation()
            assert await timer.run("retention", operation) == 0
        finally:
            restore()
        assert hits == [CacheStats.CACHE_MISS]
        if target_owner is not None:
            assert calls, "mutated original resolved visitor was not exercised"
        if mutation == "none":
            trace.finish(stages=1, require_snapshot=False, require_population=False, structure_observation=True)
            assert trace.structure_snapshot().complete and trace.structure_snapshot().observation_verified
        else:
            with pytest.raises(AssertionError, match="structure observation incomplete"):
                trace.finish(stages=1, require_snapshot=False, require_population=False, structure_observation=True)
            assert not trace.structure_snapshot().complete and not trace.structure_snapshot().observation_verified
    finally:
        restore()
        event.remove(engine, "after_cursor_execute", fresh)
        repository.release(context)


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal", ["return", "throw", "close"])
async def test_structure_native_window_lifetime_keeps_caught_reyield_separate_from_terminal(monkeypatch, request, terminal):
    from elspeth.web.coordination import database_clock
    from elspeth.web.sessions.locking import locked_session_transaction

    _require_structure_api()
    engine = request.getfixturevalue("engine")
    marker = RuntimeError("native window terminal marker")
    caught = []

    async def operation():
        manager = locked_session_transaction(engine, "native-lifetime")
        conn = manager.__enter__()
        try:
            assert database_clock.database_now(conn).utcoffset() is not None
            try:
                raise marker
            except RuntimeError as error:
                caught.append(error)
                await asyncio.sleep(0)
                await asyncio.sleep(0)
            if terminal == "return":
                assert manager.__exit__(None, None, None) is False
            elif terminal == "throw":
                with pytest.raises(RuntimeError) as error:
                    manager.gen.throw(marker)
                assert error.value is marker
            else:
                manager.gen.close()
        finally:
            manager.gen.close()
        return None

    value, trace = await _structure_trial(monkeypatch, request, (engine,), operation)
    assert value is None and caught == [marker]
    snapshot = trace.structure_snapshot()
    clocks = [row for row in snapshot.lifetimes if row.kind == "clock"]
    windows = [row for row in snapshot.lifetimes if row.kind == "window"]
    assert len(clocks) == len(windows) == 1
    assert clocks[0].suspensions == clocks[0].resumptions == 2 and clocks[0].terminal == "returned"
    assert windows[0].suspensions == windows[0].resumptions == 1
    assert windows[0].terminal == ("returned" if terminal == "return" else "unwound")
    assert len(_structure_events(snapshot, "window_ready")) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["normal", "missing", "duplicate", "wrong-frame", "carrier", "generation", "pending-close"])
async def test_structure_native_delivery_requires_the_same_actual_pending_return(monkeypatch, request, change):
    from elspeth.web import continuation_retention_memo as memo_module
    from tests.helpers.continuation_policy import minimal_continuation_policy

    _require_structure_api()
    engine = request.getfixturevalue("engine")
    trace = RetentionMemoTrace(monkeypatch, request)
    trace.observe_structure(engines=(engine,), entrypoint="calibration")
    original = trace._structure_lifetime_event
    delivered = []

    def delivery(frame, kind, code, offset, value):
        if code is clock.ContinuationActionClock.run.__code__ and kind == "yield" and not delivered:
            delivered.append(kind)
            if change in ("missing", "pending-close"):
                return
            if change == "wrong-frame":
                return original(sys._getframe(), kind, code, offset, value)
            if change == "carrier":
                token = memo_module._RETENTION_STAGE_MEMO.set(None)
                try:
                    return original(frame, kind, code, offset, value)
                finally:
                    memo_module._RETENTION_STAGE_MEMO.reset(token)
            if change == "generation":
                thread = trace._structure_threads[threading.get_ident()]
                pending = thread.pending_returns[id(frame)]
                thread.pending_returns[id(frame)] = replace(pending, generation=pending.generation + 1)
            original(frame, kind, code, offset, value)
            if change == "duplicate":
                original(frame, kind, code, offset, value)
            return
        original(frame, kind, code, offset, value)

    monkeypatch.setattr(trace, "_structure_lifetime_event", delivery)

    async def operation():
        value = _structure_sql_work(engine)
        await asyncio.sleep(0)
        if change == "pending-close":
            # Ordinary application code, outside either callback, closes while
            # the deliberately missing native delivery still owns a pending row.
            trace.close()
        return value

    timer = clock.ContinuationActionClock(minimal_continuation_policy().actions.session_sql)
    timer.finish_preparation()
    assert await timer.run("retention", operation) == 0 and delivered == ["yield"]
    if change == "normal":
        trace.finish(stages=1, require_snapshot=False, require_population=False, structure_observation=True)
        assert trace.structure_snapshot().observation_verified
    else:
        with pytest.raises(AssertionError, match="structure observation incomplete"):
            trace.finish(stages=1, require_snapshot=False, require_population=False, structure_observation=True)
        snapshot = trace.structure_snapshot()
        assert snapshot.closed and not snapshot.complete and not snapshot.observation_verified
        reasons = {
            "missing": "lifetime_resume_before_native_delivery",
            "duplicate": "lifetime_native_without_profile_return",
            "wrong-frame": "lifetime_native_without_profile_return",
            "carrier": "lifetime_native_provenance_changed",
            "generation": "lifetime_native_provenance_changed",
            "pending-close": "closed_with_active_original_owner",
        }
        assert reasons[change] in snapshot.errors


@pytest.mark.asyncio
@pytest.mark.parametrize("wrong_submission", [False, True])
async def test_structure_native_worker_window_binds_its_real_submission_and_reuses_the_thread(monkeypatch, request, wrong_submission):
    from tests.helpers import continuation_memo as helper
    from tests.helpers.continuation_policy import minimal_continuation_policy

    _require_structure_api()
    engine = request.getfixturevalue("engine")
    prior = sys.getprofile()
    threads = []
    observed = []
    with ThreadPoolExecutor(max_workers=1) as pool:
        monkeypatch.setattr(async_workers, "_SHARED_EXECUTOR", pool)
        trace = RetentionMemoTrace(monkeypatch, request)
        trace.observe_structure(engines=(engine,), entrypoint="calibration")
        original = trace._structure_lifetime_event

        def delivery(frame, kind, code, offset, value):
            if code is helper._STRUCTURE_WINDOW.__code__ and kind == "yield":
                thread = trace._structure_threads[threading.get_ident()]
                pending = thread.pending_returns[id(frame)]
                assert pending.submission is not None
                observed.append((threading.get_ident(), pending.submission))
                if wrong_submission:
                    thread.pending_returns[id(frame)] = replace(pending, submission=None)
            original(frame, kind, code, offset, value)

        monkeypatch.setattr(trace, "_structure_lifetime_event", delivery)
        ticket = publication_ticket()

        def work():
            threads.append(threading.get_ident())
            memo = current_retention_stage_memo()
            assert type(memo) is RetentionStageMemo
            with memo._lock:
                return _structure_sql_work(engine, clock_read=False)

        async def operation():
            return await async_workers.run_required_sql_finish_once(ticket, work)

        timer = clock.ContinuationActionClock(minimal_continuation_policy().actions.session_sql)
        timer.finish_preparation()
        outcome = await timer.run("retention", operation)
        assert type(outcome) is RequiredSQLReturned and outcome.value == 0
        assert ticket.complete and async_workers.outstanding_admissions() == 0 and observed
        if wrong_submission:
            with pytest.raises(AssertionError, match="structure observation incomplete"):
                trace.finish(stages=1, require_snapshot=False, require_population=False, structure_observation=True)
            assert "lifetime_native_provenance_changed" in trace.structure_snapshot().errors
        else:
            trace.finish(stages=1, require_snapshot=False, require_population=False, structure_observation=True)
            assert trace.structure_snapshot().observation_verified
        ident, profile = pool.submit(lambda: (threading.get_ident(), sys.getprofile())).result(timeout=2)
        assert threads == [ident] and all(row[0] == ident for row in observed) and profile is None
    assert sys.getprofile() is prior


@pytest.mark.asyncio
async def test_structure_native_delivery_on_an_unwrapped_physical_thread_cannot_qualify(monkeypatch, request):
    from elspeth.web.sessions.locking import locked_session_transaction
    from tests.helpers import continuation_memo as helper

    _require_structure_api()
    engine = request.getfixturevalue("engine")
    trace = RetentionMemoTrace(monkeypatch, request)
    trace.observe_structure(engines=(engine,), entrypoint="calibration")
    original = trace._structure_lifetime_event
    seen = []

    def delivery(frame, kind, code, offset, value):
        if code is helper._STRUCTURE_WINDOW.__code__:
            seen.append((threading.get_ident(), kind))
        original(frame, kind, code, offset, value)

    monkeypatch.setattr(trace, "_structure_lifetime_event", delivery)

    def unwrapped():
        with locked_session_transaction(engine, "unwrapped-lifetime"):
            return threading.get_ident()

    with ThreadPoolExecutor(max_workers=1) as pool:
        ident = pool.submit(unwrapped).result(timeout=2)
    assert ident != threading.get_ident()
    with pytest.raises(AssertionError, match="structure observation incomplete"):
        trace.finish(stages=0, require_snapshot=False, require_population=False, structure_observation=True)
    snapshot = trace.structure_snapshot()
    assert snapshot.closed and not snapshot.complete and not snapshot.observation_verified
    assert seen == [(ident, "yield"), (ident, "returned")]
    assert ident not in trace._structure_threads


@pytest.mark.asyncio
@pytest.mark.parametrize("native_bytes", [False, True])
@pytest.mark.parametrize("prior_profile", [False, True])
async def test_structure_native_lifetime_monitor_coexists_with_prior_profile_bytes_and_foreign_local_events(
    monkeypatch, request, native_bytes, prior_profile
):
    from tests.helpers import continuation_memo as helper
    from tests.helpers.continuation_policy import minimal_continuation_policy

    _require_structure_api()
    engine = request.getfixturevalue("engine")
    monitor = sys.monitoring
    foreign = 5
    code = clock.ContinuationActionClock.run.__code__
    assert monitor.get_tool(foreign) is None and monitor.get_events(foreign) == 0 and monitor.get_local_events(foreign, code) == 0
    event = monitor.events.PY_YIELD
    name = "owned-lifetime-coexistence"
    initial_profile = sys.getprofile()
    seen, profiled = [], []

    def callback(actual, _offset, _value):
        assert actual is code
        seen.append(1)

    def prior(frame, kind, _arg):
        if frame.f_code is code:
            profiled.append(kind)

    monitor.use_tool_id(foreign, name)
    previous = monitor.register_callback(foreign, event, callback)
    try:
        assert previous is None
        monitor.set_local_events(foreign, code, event)
        assert initial_profile is None
        if prior_profile:
            sys.setprofile(prior)
        trace = RetentionMemoTrace(monkeypatch, request, require_native_bytes=native_bytes)
        trace.observe_structure(engines=(engine,), entrypoint="calibration")
        lifetime = trace._structure_lifetime_monitor
        assert type(lifetime) is helper._StructureLifetimeMonitor
        if prior_profile:
            assert lifetime.unsupported_prior and lifetime.tool is None
        elif native_bytes:
            assert trace._byte_monitor.tool != lifetime.tool

        async def operation():
            value = _structure_sql_work(engine)
            await asyncio.sleep(0)
            return value

        timer = clock.ContinuationActionClock(minimal_continuation_policy().actions.session_sql)
        timer.finish_preparation()
        assert await timer.run("retention", operation) == 0
        if prior_profile:
            with pytest.raises(AssertionError, match="structure observation incomplete"):
                trace.finish(stages=1, require_snapshot=False, require_population=False, structure_observation=True)
            assert not trace.structure_snapshot().observation_verified
            assert "lifetime_prior_profile_unsupported" in trace.structure_snapshot().errors
            assert sys.getprofile() is prior and profiled.count("return") == 2
        else:
            trace.finish(stages=1, require_snapshot=False, require_population=False, structure_observation=True)
            assert trace.structure_snapshot().observation_verified and sys.getprofile() is initial_profile
        assert lifetime.closed and seen == [1]
        if native_bytes:
            assert trace._byte_monitor.closed and not trace.byte_errors
            trace._assert_native_byte_budget(())
        assert monitor.get_tool(foreign) == name and monitor.get_local_events(foreign, code) == event
        assert monitor.register_callback(foreign, event, callback) is callback
    finally:
        sys.setprofile(initial_profile)
        monitor.set_local_events(foreign, code, 0)
        monitor.register_callback(foreign, event, previous)
        monitor.free_tool_id(foreign)


@pytest.mark.parametrize("boundary", ["claim", "probe", "callback", "local", "global"])
@pytest.mark.parametrize("marker_type", [RuntimeError, KeyboardInterrupt, SystemExit])
def test_structure_native_monitor_partial_install_restores_owned_state_and_same_primary(monkeypatch, request, boundary, marker_type):
    from tests.helpers import continuation_memo as helper

    _require_structure_api()
    engine = request.getfixturevalue("engine")
    states = {
        tool: (
            sys.monitoring.get_tool(tool),
            sys.monitoring.get_events(tool),
            tuple(sys.monitoring.get_local_events(tool, code) for code in helper._STRUCTURE_LIFETIME_CODES),
        )
        for tool in (3, 4)
    }
    marker = marker_type("original lifetime install interruption")
    reached = []

    def before(_self, kind, _target):
        if kind == boundary and not reached:
            reached.append(kind)
            raise marker

    monkeypatch.setattr(helper._StructureLifetimeMonitor, "_before_mutation", before)
    trace = RetentionMemoTrace(monkeypatch, request)
    with pytest.raises(marker_type) as caught:
        try:
            trace.observe_structure(engines=(engine,), entrypoint="calibration")
        finally:
            trace.finish(stages=0, require_snapshot=False, require_population=False, structure_observation=True)
    assert reached == [boundary] and caught.value is marker
    assert trace.structure_snapshot().closed and not trace.structure_snapshot().observation_verified
    assert states == {
        tool: (
            sys.monitoring.get_tool(tool),
            sys.monitoring.get_events(tool),
            tuple(sys.monitoring.get_local_events(tool, code) for code in helper._STRUCTURE_LIFETIME_CODES),
        )
        for tool in (3, 4)
    }
    for tool in (3, 4):
        if states[tool][0] is None:
            for event in (sys.monitoring.events.PY_YIELD, sys.monitoring.events.PY_RETURN, sys.monitoring.events.PY_UNWIND):
                displaced = helper._LIFETIME_REGISTER(tool, event, None)
                try:
                    assert displaced is None
                finally:
                    helper._LIFETIME_REGISTER(tool, event, displaced)


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["close-global", "close-local", "close-callback", "free"])
@pytest.mark.parametrize("native_error", [False, True])
async def test_structure_native_monitor_cleanup_interruption_preserves_original_outcome_and_restores(
    monkeypatch, request, boundary, native_error
):
    from tests.helpers import continuation_memo as helper
    from tests.helpers.continuation_policy import minimal_continuation_policy

    _require_structure_api()
    engine = request.getfixturevalue("engine")
    initial_profile = sys.getprofile()
    cleanup = KeyboardInterrupt("original lifetime cleanup interruption")
    primary = RuntimeError("original native lifetime failure")
    reached = []

    def before(_self, kind, _target):
        if kind == boundary and not reached:
            reached.append(kind)
            raise cleanup

    monkeypatch.setattr(helper._StructureLifetimeMonitor, "_before_mutation", before)
    trace = RetentionMemoTrace(monkeypatch, request)
    trace.observe_structure(engines=(engine,), entrypoint="calibration")
    owned = trace._structure_lifetime_monitor
    tool = owned.tool

    async def operation():
        value = _structure_sql_work(engine)
        await asyncio.sleep(0)
        if native_error:
            raise primary
        return value

    timer = clock.ContinuationActionClock(minimal_continuation_policy().actions.session_sql)
    timer.finish_preparation()
    if native_error:
        with pytest.raises(RuntimeError) as caught:
            try:
                await timer.run("retention", operation)
            finally:
                trace.finish(stages=1, require_snapshot=False, require_population=False, structure_observation=True)
        assert caught.value is primary
    else:
        assert await timer.run("retention", operation) == 0
        with pytest.raises(KeyboardInterrupt) as caught:
            trace.finish(stages=1, require_snapshot=False, require_population=False, structure_observation=True)
        assert caught.value is cleanup
    assert reached == [boundary] and owned.closed and sys.getprofile() is initial_profile
    assert sys.monitoring.get_tool(tool) is None and sys.monitoring.get_events(tool) == 0
    assert all(sys.monitoring.get_local_events(tool, code) == 0 for code in helper._STRUCTURE_LIFETIME_CODES)
    for event in (sys.monitoring.events.PY_YIELD, sys.monitoring.events.PY_RETURN, sys.monitoring.events.PY_UNWIND):
        assert sys.monitoring.register_callback(tool, event, None) is None
    assert trace.structure_snapshot().closed and not trace.structure_snapshot().observation_verified


@pytest.mark.asyncio
@pytest.mark.parametrize("marker_type", [RuntimeError, KeyboardInterrupt, SystemExit])
async def test_structure_native_unwind_callback_cannot_replace_the_real_primary(monkeypatch, request, marker_type):
    from tests.helpers.continuation_policy import minimal_continuation_policy

    _require_structure_api()
    engine = request.getfixturevalue("engine")
    primary = RuntimeError("original native unwind identity")
    secondary = marker_type("observer unwind bookkeeping interruption")
    trace = RetentionMemoTrace(monkeypatch, request)
    trace.observe_structure(engines=(engine,), entrypoint="calibration")
    original = trace._structure_return
    reached = []

    def bookkeeping(thread, key, owner, outcome, value):
        if owner.role == "window" and outcome == "unwound" and not reached:
            reached.append(outcome)
            raise secondary
        original(thread, key, owner, outcome, value)

    monkeypatch.setattr(trace, "_structure_return", bookkeeping)

    async def operation():
        from elspeth.web.sessions.locking import locked_session_transaction

        with locked_session_transaction(engine, "original-primary"):
            raise primary

    timer = clock.ContinuationActionClock(minimal_continuation_policy().actions.session_sql)
    timer.finish_preparation()
    with pytest.raises(RuntimeError) as caught:
        try:
            await timer.run("retention", operation)
        finally:
            trace.finish(stages=1, require_snapshot=False, require_population=False, structure_observation=True)
    assert caught.value is primary and reached == ["unwound"]
    assert trace.structure_snapshot().closed and not trace.structure_snapshot().observation_verified


@pytest.mark.asyncio
@pytest.mark.parametrize("loss", ["callback", "name", "local-mask"])
async def test_structure_lifetime_monitor_refuses_ownership_loss_without_erasing_foreign_state(monkeypatch, request, loss):
    from tests.helpers import continuation_memo as helper
    from tests.helpers.continuation_policy import minimal_continuation_policy

    _require_structure_api()
    engine = request.getfixturevalue("engine")
    trace = RetentionMemoTrace(monkeypatch, request)
    trace.observe_structure(engines=(engine,), entrypoint="calibration")
    owned = trace._structure_lifetime_monitor
    tool = owned.tool
    event = sys.monitoring.events.PY_YIELD
    code = clock.ContinuationActionClock.run.__code__
    saved_callback = next(callback for number, callback in owned.deliveries if number == event)
    foreign_seen = []

    def foreign(_code, _offset, _value):
        foreign_seen.append(1)

    if loss == "callback":
        assert sys.monitoring.register_callback(tool, event, foreign) is saved_callback
    elif loss == "name":
        sys.monitoring.free_tool_id(tool)
        sys.monitoring.use_tool_id(tool, "controlled-foreign-lifetime-owner")
    else:
        sys.monitoring.set_local_events(tool, code, sys.monitoring.events.CALL)

    async def operation():
        value = _structure_sql_work(engine)
        await asyncio.sleep(0)
        return value

    try:
        timer = clock.ContinuationActionClock(minimal_continuation_policy().actions.session_sql)
        timer.finish_preparation()
        assert await timer.run("retention", operation) == 0
        with pytest.raises(RuntimeError):
            trace.finish(stages=1, require_snapshot=False, require_population=False, structure_observation=True)
        snapshot = trace.structure_snapshot()
        assert snapshot.closed and not snapshot.complete and not snapshot.observation_verified
        if loss == "callback":
            assert foreign_seen and sys.monitoring.register_callback(tool, event, foreign) is foreign
            assert sys.monitoring.get_tool(tool) == owned.name
        elif loss == "name":
            assert sys.monitoring.get_tool(tool) == "controlled-foreign-lifetime-owner"
        else:
            assert sys.monitoring.get_local_events(tool, code) == sys.monitoring.events.CALL
    finally:
        # Restore only the exact ownership changes made by THIS control; the
        # already-unqualified snapshot stays terminal and is never made green.
        if loss == "callback":
            assert sys.monitoring.register_callback(tool, event, saved_callback) is foreign
        elif loss == "name":
            assert sys.monitoring.get_tool(tool) == "controlled-foreign-lifetime-owner"
            sys.monitoring.free_tool_id(tool)
            sys.monitoring.use_tool_id(tool, owned.name)
        else:
            sys.monitoring.set_local_events(tool, code, owned.local_mask)
        owned.close()
    assert owned.closed and sys.monitoring.get_tool(tool) is None
    assert all(sys.monitoring.get_local_events(tool, held) == 0 for held in helper._STRUCTURE_LIFETIME_CODES)


def test_structure_lifetime_monitor_refuses_and_restores_a_retained_native_callback(monkeypatch, request):
    from tests.helpers import continuation_memo as helper

    _require_structure_api()
    engine = request.getfixturevalue("engine")
    trace = RetentionMemoTrace(monkeypatch, request, require_native_bytes=True)
    assert trace._byte_monitor.tool == 3
    tool = 4
    event = sys.monitoring.events.PY_RETURN
    assert sys.monitoring.get_tool(tool) is None and sys.monitoring.get_events(tool) == 0
    assert all(sys.monitoring.get_local_events(tool, code) == 0 for code in helper._STRUCTURE_LIFETIME_CODES)

    def retained(_code, _offset, _value):
        return None

    sys.monitoring.use_tool_id(tool, "controlled-retained-lifetime-callback")
    previous = sys.monitoring.register_callback(tool, event, retained)
    sys.monitoring.free_tool_id(tool)
    try:
        assert previous is None
        with pytest.raises(RuntimeError, match="retained lifetime callbacks"):
            try:
                trace.observe_structure(engines=(engine,), entrypoint="calibration")
            finally:
                trace.finish(stages=0, require_snapshot=False, require_population=False, structure_observation=True)
        assert sys.monitoring.get_tool(tool) is None and sys.monitoring.get_events(tool) == 0
        assert sys.monitoring.register_callback(tool, event, retained) is retained
        assert trace.structure_snapshot().closed and not trace.structure_snapshot().observation_verified
    finally:
        sys.monitoring.register_callback(tool, event, previous)


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["yield", "unwind"])
@pytest.mark.parametrize("marker_type", [RuntimeError, KeyboardInterrupt, SystemExit])
async def test_structure_audited_native_entry_failure_preserves_primary_and_clears_authority(monkeypatch, request, boundary, marker_type):
    from elspeth.web.sessions.locking import locked_session_transaction
    from tests.helpers import continuation_memo as helper
    from tests.helpers.continuation_policy import minimal_continuation_policy

    _require_structure_api()
    engine = request.getfixturevalue("engine")
    primary = RuntimeError("original native audited-entry failure")
    secondary = marker_type("audited frame acquisition refused")
    original_frame = helper._LIFETIME_FRAME
    armed = False
    reached = []

    def audited_frame(depth):
        if armed and not reached:
            reached.append(boundary)
            raise secondary
        # This is the real original frame operation with only the wrapper's
        # own stack level removed; no synthetic successful frame is supplied.
        return original_frame(depth + 1)

    trace = RetentionMemoTrace(monkeypatch, request)
    trace.observe_structure(engines=(engine,), entrypoint="calibration")
    monkeypatch.setattr(helper, "_LIFETIME_FRAME", audited_frame)

    async def operation():
        nonlocal armed
        armed = boundary == "yield"
        with locked_session_transaction(engine, "audited-native-entry"):
            if boundary == "unwind":
                armed = True
                raise primary
        return 0

    timer = clock.ContinuationActionClock(minimal_continuation_policy().actions.session_sql)
    timer.finish_preparation()
    if boundary == "unwind":
        with pytest.raises(RuntimeError) as caught:
            try:
                await timer.run("retention", operation)
            finally:
                trace.finish(stages=1, require_snapshot=False, require_population=False, structure_observation=True)
        assert caught.value is primary
    elif marker_type is RuntimeError:
        assert await timer.run("retention", operation) == 0
        with pytest.raises(AssertionError, match="structure observation incomplete"):
            trace.finish(stages=1, require_snapshot=False, require_population=False, structure_observation=True)
    else:
        with pytest.raises(marker_type) as caught:
            try:
                await timer.run("retention", operation)
            finally:
                trace.finish(stages=1, require_snapshot=False, require_population=False, structure_observation=True)
        assert caught.value is secondary
    assert reached == [boundary] and trace._structure_lifetime_monitor.closed
    assert trace.structure_snapshot().closed and not trace.structure_snapshot().observation_verified
    assert all(thread.native_entry is None for thread in trace._structure_threads.values())


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", [False, True])
async def test_structure_restored_foreign_native_callback_cannot_relabel_original_return(monkeypatch, request, changed):
    from tests.helpers import continuation_memo as helper
    from tests.helpers.continuation_policy import minimal_continuation_policy

    _require_structure_api()
    engine = request.getfixturevalue("engine")
    trace = RetentionMemoTrace(monkeypatch, request)
    trace.observe_structure(engines=(engine,), entrypoint="calibration")
    owned = trace._structure_lifetime_monitor
    event = sys.monitoring.events.PY_RETURN
    original = next(callback for number, callback in owned.deliveries if number == event)
    code = clock.ContinuationActionClock.run.__code__
    seen = []

    def foreign(actual, offset, value):
        assert actual is code
        frame = sys._getframe(1)
        assert frame.f_code is actual
        seen.append(actual is code)
        try:
            # Actual original frame/code/offset/value, no copied pending record.
            # This native foreign entry has NO original deliver authority.
            trace._structure_lifetime_event(frame, "unwound", actual, offset, value)
        finally:
            assert helper._LIFETIME_REGISTER(owned.tool, event, original) is foreign

    async def operation():
        result = _structure_sql_work(engine)
        if changed:
            assert helper._LIFETIME_REGISTER(owned.tool, event, foreign) is original
        return result

    timer = clock.ContinuationActionClock(minimal_continuation_policy().actions.session_sql)
    timer.finish_preparation()
    try:
        assert await timer.run("retention", operation) == 0
        if changed:
            assert seen == [True]
            with pytest.raises(AssertionError, match="structure observation incomplete"):
                trace.finish(stages=1, require_snapshot=False, require_population=False, structure_observation=True)
            assert "lifetime_native_entry_unowned" in trace.structure_snapshot().errors
        else:
            trace.finish(stages=1, require_snapshot=False, require_population=False, structure_observation=True)
            assert trace.structure_snapshot().observation_verified and not seen
            assert _structure_events(trace.structure_snapshot(), "clock_terminal")[0].outcome == "returned"
        assert all(thread.native_entry is None for thread in trace._structure_threads.values())
    finally:
        displaced = helper._LIFETIME_REGISTER(owned.tool, event, None)
        try:
            assert displaced is (None if owned.closed else original)
        finally:
            helper._LIFETIME_REGISTER(owned.tool, event, displaced)


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["close-global", "close-local", "free"])
@pytest.mark.parametrize("change", ["name", "mask"])
@pytest.mark.parametrize("native_error", [False, True])
async def test_structure_cleanup_composed_owner_loss_and_interrupt_retains_foreign_state(
    monkeypatch, request, boundary, change, native_error
):
    from tests.helpers import continuation_memo as helper
    from tests.helpers.continuation_policy import minimal_continuation_policy

    _require_structure_api()
    engine = request.getfixturevalue("engine")
    marker = KeyboardInterrupt("original composed cleanup interruption")
    primary = RuntimeError("original composed native failure")
    reached = []
    saved = []
    expected_foreign = []

    def state(owned):
        callbacks = []
        for event, _callback in owned.deliveries:
            displaced = helper._LIFETIME_REGISTER(owned.tool, event, None)
            try:
                callbacks.append(displaced)
            finally:
                helper._LIFETIME_REGISTER(owned.tool, event, displaced)
        return (
            helper._LIFETIME_GET_TOOL(owned.tool),
            helper._LIFETIME_GET_EVENTS(owned.tool),
            tuple(helper._LIFETIME_GET_LOCAL(owned.tool, code) for code in helper._STRUCTURE_LIFETIME_CODES),
            tuple(callbacks),
        )

    def before(owned, kind, target):
        if kind != boundary or reached:
            return
        reached.append(kind)
        if change == "name":
            helper._LIFETIME_FREE_TOOL(owned.tool)
            helper._LIFETIME_USE_TOOL(owned.tool, "controlled-composed-foreign-owner")
            saved.append(("name", owned.name))
        elif kind == "close-local":
            previous = helper._LIFETIME_GET_LOCAL(owned.tool, target)
            helper._LIFETIME_SET_LOCAL(owned.tool, target, sys.monitoring.events.CALL)
            saved.append(("local", target, previous))
        else:
            previous = helper._LIFETIME_GET_EVENTS(owned.tool)
            helper._LIFETIME_SET_EVENTS(owned.tool, previous | sys.monitoring.events.RAISE)
            saved.append(("global", previous))
        expected_foreign.append(state(owned))
        raise marker

    monkeypatch.setattr(helper._StructureLifetimeMonitor, "_before_mutation", before)
    trace = RetentionMemoTrace(monkeypatch, request)
    trace.observe_structure(engines=(engine,), entrypoint="calibration")
    owned = trace._structure_lifetime_monitor

    async def operation():
        value = _structure_sql_work(engine)
        if native_error:
            raise primary
        return value

    timer = clock.ContinuationActionClock(minimal_continuation_policy().actions.session_sql)
    timer.finish_preparation()
    try:
        if native_error:
            with pytest.raises(RuntimeError) as caught:
                try:
                    await timer.run("retention", operation)
                finally:
                    trace.finish(stages=1, require_snapshot=False, require_population=False, structure_observation=True)
            assert caught.value is primary
        else:
            assert await timer.run("retention", operation) == 0
            with pytest.raises(KeyboardInterrupt) as caught:
                trace.finish(stages=1, require_snapshot=False, require_population=False, structure_observation=True)
            assert caught.value is marker
        assert reached == [boundary] and not owned.closed
        actual_foreign = state(owned)
        assert actual_foreign[:3] == expected_foreign[0][:3]
        assert all(actual is expected for actual, expected in zip(actual_foreign[3], expected_foreign[0][3], strict=True))
        if change == "name":
            assert helper._LIFETIME_GET_TOOL(owned.tool) == "controlled-composed-foreign-owner"
        elif saved[0][0] == "local":
            assert helper._LIFETIME_GET_LOCAL(owned.tool, saved[0][1]) == sys.monitoring.events.CALL
        else:
            assert helper._LIFETIME_GET_EVENTS(owned.tool) == saved[0][1] | sys.monitoring.events.RAISE
        assert trace.structure_snapshot().closed and not trace.structure_snapshot().observation_verified
    finally:
        if saved:
            state = saved[0]
            if state[0] == "name":
                assert helper._LIFETIME_GET_TOOL(owned.tool) == "controlled-composed-foreign-owner"
                helper._LIFETIME_FREE_TOOL(owned.tool)
                helper._LIFETIME_USE_TOOL(owned.tool, state[1])
            elif state[0] == "local":
                helper._LIFETIME_SET_LOCAL(owned.tool, state[1], state[2])
            else:
                helper._LIFETIME_SET_EVENTS(owned.tool, state[1])
            owned.close()
    assert owned.closed and helper._LIFETIME_GET_TOOL(owned.tool) is None


@pytest.mark.parametrize("marker_type", [RuntimeError, KeyboardInterrupt, SystemExit])
def test_structure_install_claim_seam_preserves_a_reclaimed_foreign_slot(monkeypatch, request, marker_type):
    from tests.helpers import continuation_memo as helper

    _require_structure_api()
    engine = request.getfixturevalue("engine")
    marker = marker_type("original claim interruption")
    claimed = []

    def before(_owned, kind, target):
        if kind == "claim" and not claimed:
            helper._LIFETIME_USE_TOOL(target, "controlled-foreign-claim")
            claimed.append(target)
            raise marker

    monkeypatch.setattr(helper._StructureLifetimeMonitor, "_before_mutation", before)
    trace = RetentionMemoTrace(monkeypatch, request)
    try:
        with pytest.raises(marker_type) as caught:
            try:
                trace.observe_structure(engines=(engine,), entrypoint="calibration")
            finally:
                trace.finish(stages=0, require_snapshot=False, require_population=False, structure_observation=True)
        assert caught.value is marker and len(claimed) == 1
        assert helper._LIFETIME_GET_TOOL(claimed[0]) == "controlled-foreign-claim"
        assert helper._LIFETIME_GET_EVENTS(claimed[0]) == 0
    finally:
        if claimed:
            assert helper._LIFETIME_GET_TOOL(claimed[0]) == "controlled-foreign-claim"
            helper._LIFETIME_FREE_TOOL(claimed[0])


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["get_tool", "set_events", "free_tool_id", "register_callback"])
async def test_structure_cleanup_uses_held_monitor_apis_without_calling_replaced_public_bindings(monkeypatch, request, name):
    from tests.helpers import continuation_memo as helper
    from tests.helpers.continuation_policy import minimal_continuation_policy

    _require_structure_api()
    engine = request.getfixturevalue("engine")
    trace = RetentionMemoTrace(monkeypatch, request)
    trace.observe_structure(engines=(engine,), entrypoint="calibration")
    called = []

    def foreign(*_args):
        called.append(name)
        raise AssertionError("changed public monitoring API was called")

    async def operation():
        return _structure_sql_work(engine)

    timer = clock.ContinuationActionClock(minimal_continuation_policy().actions.session_sql)
    timer.finish_preparation()
    assert await timer.run("retention", operation) == 0
    with monkeypatch.context() as patch:
        patch.setattr(sys.monitoring, name, foreign)
        with pytest.raises(AssertionError, match="structure observation incomplete"):
            trace.finish(stages=1, require_snapshot=False, require_population=False, structure_observation=True)
    assert not called and trace._structure_lifetime_monitor.closed
    assert helper._LIFETIME_GET_TOOL(trace._structure_lifetime_monitor.tool) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("domain", ["profile", "closed-field"])
async def test_structure_pending_domains_refuse_foreign_protocols_without_retaining_capabilities(monkeypatch, request, domain):
    from elspeth.web.sessions.locking import locked_session_transaction
    from tests.helpers import continuation_memo as helper
    from tests.helpers.continuation_policy import minimal_continuation_policy

    _require_structure_api()
    engine = request.getfixturevalue("engine")
    initial = sys.getprofile()
    forbidden = []
    calls = []
    armed = False
    changed = []

    class Foreign:
        def __call__(self, _frame, _event, _arg):
            calls.append(1)

        def __hash__(self):
            forbidden.append("hash")
            return 0

        def __eq__(self, _other):
            forbidden.append("eq")
            return False

        def __bool__(self):
            forbidden.append("bool")
            return False

        def __repr__(self):
            forbidden.append("repr")
            return "controlled-foreign-protocol"

    foreign = Foreign()

    def prior(frame, event, _arg):
        if domain == "profile" and armed and not changed and event == "return" and frame.f_code is helper._STRUCTURE_WINDOW.__code__:
            changed.append(1)
            sys.setprofile(foreign)

    assert initial is None
    if domain == "profile":
        sys.setprofile(prior)
    trace = RetentionMemoTrace(monkeypatch, request)
    installed = sys.getprofile()
    trace.observe_structure(engines=(engine,), entrypoint="calibration")
    if domain == "profile":
        assert trace._structure_lifetime_monitor.unsupported_prior
        assert trace._structure_lifetime_monitor.tool is None

    async def operation():
        nonlocal armed
        memo = current_retention_stage_memo()
        assert type(memo) is RetentionStageMemo
        with memo._lock:
            if domain == "closed-field":
                memo._closed = foreign
            armed = True
            try:
                with locked_session_transaction(engine, "pending-domain"):
                    if domain == "profile":
                        sys.setprofile(installed)
                    for thread in trace._structure_threads.values():
                        for pending in thread.pending_returns.values():
                            if any(item is foreign for item in pending.carrier):
                                pytest.fail("foreign capability retained in pending carrier")
            finally:
                if domain == "closed-field":
                    memo._closed = False
                if sys.getprofile() is foreign:
                    sys.setprofile(installed)
        return 0

    try:
        timer = clock.ContinuationActionClock(minimal_continuation_policy().actions.session_sql)
        timer.finish_preparation()
        assert await timer.run("retention", operation) == 0
        with pytest.raises(AssertionError, match="structure observation incomplete"):
            trace.finish(stages=1, require_snapshot=False, require_population=False, structure_observation=True)
        assert not forbidden
        if domain == "profile":
            assert changed == [1] and calls
        assert trace.structure_snapshot().closed and not trace.structure_snapshot().observation_verified
    finally:
        sys.setprofile(initial)


@pytest.mark.asyncio
@pytest.mark.parametrize("native_error", [False, True])
@pytest.mark.parametrize("marker_type", [KeyboardInterrupt, SystemExit])
async def test_structure_prior_c_return_exchange_capability_refuses_before_losing_a_foreign_callback(
    monkeypatch, request, native_error, marker_type
):
    from tests.helpers import continuation_memo as helper
    from tests.helpers.continuation_policy import minimal_continuation_policy

    _require_structure_api()
    engine = request.getfixturevalue("engine")
    initial = sys.getprofile()
    assert initial is None
    trace = RetentionMemoTrace(monkeypatch, request)
    trace.observe_structure(engines=(engine,), entrypoint="calibration")
    owned = trace._structure_lifetime_monitor
    tool = owned.tool
    event = sys.monitoring.events.PY_RETURN
    original = next(callback for number, callback in owned.deliveries if number == event)
    interruption = marker_type("actual register callback c_return interruption")
    primary = RuntimeError("original native exchange capability primary")
    armed = False
    hit = []

    def foreign(_code, _offset, _value):
        return None

    def prior(_frame, kind, arg):
        if armed and kind == "c_return" and arg is helper._LIFETIME_REGISTER:
            hit.append(kind)
            raise interruption

    def change_chain():
        nonlocal armed
        assert helper._LIFETIME_REGISTER(tool, event, foreign) is original
        sys.setprofile(prior)
        armed = True

    async def operation():
        value = _structure_sql_work(engine)
        if native_error:
            raise primary
        return value

    timer = clock.ContinuationActionClock(minimal_continuation_policy().actions.session_sql)
    timer.finish_preparation()
    try:
        if native_error:
            with pytest.raises(RuntimeError) as caught:
                try:
                    await timer.run("retention", operation)
                finally:
                    change_chain()
                    trace.finish(stages=1, require_snapshot=False, require_population=False, structure_observation=True)
            assert caught.value is primary
        else:
            assert await timer.run("retention", operation) == 0
            change_chain()
            with pytest.raises(AssertionError):
                trace.finish(stages=1, require_snapshot=False, require_population=False, structure_observation=True)
        assert not hit and sys.getprofile() is prior and not owned.closed
        assert helper._LIFETIME_GET_TOOL(tool) == owned.name
        assert helper._LIFETIME_GET_EVENTS(tool) == owned.global_mask
        assert all(helper._LIFETIME_GET_LOCAL(tool, code) == owned.local_mask for code in helper._STRUCTURE_LIFETIME_CODES)
        assert trace.structure_snapshot().closed and not trace.structure_snapshot().observation_verified
        # Control the armed native c_return instrument on a real exchange with
        # both identities already known. Lost return cannot lose unknown state.
        with pytest.raises(marker_type) as caught:
            helper._LIFETIME_REGISTER(tool, event, foreign)
        assert caught.value is interruption and hit == ["c_return"]
        assert sys.getprofile() is None  # Genuine prior-profiler failure semantics.
        displaced = helper._LIFETIME_REGISTER(tool, event, None)
        try:
            assert displaced is foreign
        finally:
            helper._LIFETIME_REGISTER(tool, event, displaced)
    finally:
        armed = False
        # Restore only this control's exact setup, after preserving the actual
        # VM-disabled prior-hook outcome and the already-unqualified snapshot.
        if sys.getprofile() is prior:
            sys.setprofile(initial)
        displaced = helper._LIFETIME_REGISTER(tool, event, original)
        try:
            assert displaced is foreign
        finally:
            if displaced is not foreign:
                helper._LIFETIME_REGISTER(tool, event, displaced)
        owned.close()
        sys.setprofile(initial)
    assert owned.closed and helper._LIFETIME_GET_TOOL(tool) is None


@pytest.mark.asyncio
async def test_structure_prior_eligibility_uses_the_actual_original_captured_chain(monkeypatch, request):
    from tests.helpers import continuation_memo as helper

    _require_structure_api()
    engine = request.getfixturevalue("engine")
    initial = sys.getprofile()
    assert initial is None

    def prior(_frame, _event, _arg):
        return None

    sys.setprofile(prior)
    trace = RetentionMemoTrace(monkeypatch, request)
    before = {
        tool: (
            helper._LIFETIME_GET_TOOL(tool),
            helper._LIFETIME_GET_EVENTS(tool),
            tuple(helper._LIFETIME_GET_LOCAL(tool, code) for code in helper._STRUCTURE_LIFETIME_CODES),
        )
        for tool in (3, 4)
    }
    try:
        # Changing this mutable convenience field cannot rewrite the actual
        # previous closure cell or the frozen original factory birth proof.
        trace._previous = None
        assert not trace._structure_exchange_supported()
        trace.observe_structure(engines=(engine,), entrypoint="calibration")
        assert trace._structure_lifetime_monitor.unsupported_prior and trace._structure_lifetime_monitor.tool is None
        trace._previous = prior
        with pytest.raises(AssertionError, match="structure observation incomplete"):
            trace.finish(stages=0, require_snapshot=False, require_population=False, structure_observation=True)
        assert sys.getprofile() is prior
        assert before == {
            tool: (
                helper._LIFETIME_GET_TOOL(tool),
                helper._LIFETIME_GET_EVENTS(tool),
                tuple(helper._LIFETIME_GET_LOCAL(tool, code) for code in helper._STRUCTURE_LIFETIME_CODES),
            )
            for tool in (3, 4)
        }
    finally:
        trace._previous = prior
        sys.setprofile(initial)


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["close-global", "close-local", "close-callback", "free"])
@pytest.mark.parametrize("lose_capability", [False, True])
@pytest.mark.parametrize("marker_type", [None, KeyboardInterrupt, SystemExit])
@pytest.mark.parametrize("native_error", [False, True])
async def test_structure_cleanup_saved_interruption_survives_later_prior_capability_loss(
    monkeypatch, request, boundary, lose_capability, marker_type, native_error
):
    from tests.helpers import continuation_memo as helper
    from tests.helpers.continuation_policy import minimal_continuation_policy

    _require_structure_api()
    engine = request.getfixturevalue("engine")
    initial = sys.getprofile()
    assert initial is None
    marker = None if marker_type is None else marker_type("original cleanup seam interruption before capability loss")
    primary = RuntimeError("original native primary during composed capability loss")
    reached = []
    expected = []
    mutations_after_loss = []
    control_cleanup = False
    mutation_apis = (
        helper._LIFETIME_REGISTER,
        helper._LIFETIME_SET_EVENTS,
        helper._LIFETIME_SET_LOCAL,
        helper._LIFETIME_FREE_TOOL,
    )

    def foreign(_code, _offset, _value):
        return None

    def prior(_frame, kind, arg):
        if not control_cleanup and kind == "c_call" and any(arg is function for function in mutation_apis):
            mutations_after_loss.append(kind)

    def state(owned):
        callbacks = []
        for event, _callback in owned.deliveries:
            displaced = helper._LIFETIME_REGISTER(owned.tool, event, None)
            try:
                callbacks.append(displaced)
            finally:
                helper._LIFETIME_REGISTER(owned.tool, event, displaced)
        return (
            helper._LIFETIME_GET_TOOL(owned.tool),
            helper._LIFETIME_GET_EVENTS(owned.tool),
            tuple(helper._LIFETIME_GET_LOCAL(owned.tool, code) for code in helper._STRUCTURE_LIFETIME_CODES),
            tuple(callbacks),
        )

    saved = []

    def before(owned, kind, _target):
        if kind != boundary or reached:
            return
        reached.append(kind)
        if lose_capability:
            event, original = owned.deliveries[0]
            displaced = helper._LIFETIME_REGISTER(owned.tool, event, foreign)
            saved.append((event, displaced))
            # Earlier cleanup may already have restored this callback. Preserve
            # its actual displaced identity, not a guessed installed value.
            assert displaced is original or displaced is None
            expected.append(state(owned))
            sys.setprofile(prior)
        if marker is not None:
            raise marker

    monkeypatch.setattr(helper._StructureLifetimeMonitor, "_before_mutation", before)
    trace = RetentionMemoTrace(monkeypatch, request)
    trace.observe_structure(engines=(engine,), entrypoint="calibration")
    owned = trace._structure_lifetime_monitor
    tool = owned.tool

    async def operation():
        value = _structure_sql_work(engine)
        await asyncio.sleep(0)
        if native_error:
            raise primary
        return value

    timer = clock.ContinuationActionClock(minimal_continuation_policy().actions.session_sql)
    timer.finish_preparation()
    try:
        if native_error:
            with pytest.raises(RuntimeError) as caught:
                try:
                    await timer.run("retention", operation)
                finally:
                    trace.finish(stages=1, require_snapshot=False, require_population=False, structure_observation=True)
            assert caught.value is primary
        else:
            assert await timer.run("retention", operation) == 0
            if marker is not None:
                with pytest.raises(marker_type) as caught:
                    trace.finish(stages=1, require_snapshot=False, require_population=False, structure_observation=True)
                assert caught.value is marker
            elif lose_capability:
                with pytest.raises(AssertionError):
                    trace.finish(stages=1, require_snapshot=False, require_population=False, structure_observation=True)
            else:
                trace.finish(stages=1, require_snapshot=False, require_population=False, structure_observation=True)
                assert trace.structure_snapshot().observation_verified
        assert reached == [boundary]
        if lose_capability:
            assert not mutations_after_loss
            assert sys.getprofile() is prior and not owned.closed and owned.unsupported_prior
            control_cleanup = True
            actual = state(owned)
            assert actual[:3] == expected[0][:3]
            assert all(a is b for a, b in zip(actual[3], expected[0][3], strict=True))
            assert trace.structure_snapshot().closed and not trace.structure_snapshot().observation_verified
        else:
            assert owned.closed and sys.getprofile() is initial
            assert helper._LIFETIME_GET_TOOL(tool) is None
            assert helper._LIFETIME_GET_EVENTS(tool) == 0
            assert all(helper._LIFETIME_GET_LOCAL(tool, code) == 0 for code in helper._STRUCTURE_LIFETIME_CODES)
            if marker is not None or native_error:
                assert not trace.structure_snapshot().observation_verified
    finally:
        control_cleanup = True
        if lose_capability and saved:
            assert sys.getprofile() is prior
            sys.setprofile(initial)
            event, previous = saved[0]
            displaced = helper._LIFETIME_REGISTER(tool, event, previous)
            try:
                assert displaced is foreign
            finally:
                if displaced is not foreign:
                    helper._LIFETIME_REGISTER(tool, event, displaced)
            owned.close()
        sys.setprofile(initial)
    assert owned.closed and helper._LIFETIME_GET_TOOL(tool) is None


# Closed B6 publication DML controls: these use the original store producers.
# Setup reads establish lawful fixture inputs; the original append still performs
# all of its own current reads, aggregate, authority checks, and writes in scope.
def _closed_dml_seed(engine):
    from sqlalchemy import select

    from elspeth.web.sessions import continuation as store
    from elspeth.web.sessions.locking import locked_session_transaction

    repository, context = _structure_actual_context(engine)
    try:
        with locked_session_transaction(engine, str(context.fence.session_id)) as conn:
            assert store.require_continuation_mutation(conn, context) is True
            parent = conn.execute(
                select(store.jobs.c.operation_id, store.jobs.c.status_revision, store.jobs.c.attempt).where(
                    store.jobs.c.session_id == context.fence.session_id,
                    store.jobs.c.session_operation_id == context.fence.operation_id,
                    store.jobs.c.session_operation_epoch == context.fence.operation_epoch,
                )
            ).one()
            selected = store.load_snapshot(conn, session_id=str(context.fence.session_id), operation_id=parent.operation_id)
        assert parent.attempt == 1, "the genuine first claim supplies the required-work authority attempt"
        assert selected.cursor == 0, "closed DML fixture must begin with its genuine initial empty journal"
        return repository, context, parent.operation_id, selected, parent.status_revision
    except BaseException:
        repository.release(context)
        raise


async def _closed_dml_trial(monkeypatch, request, engine, context, operation_id, work, *, refused=False, listeners=()):
    from sqlalchemy import event

    from elspeth.web.required_work import RequiredAuthorityKind, RequiredWorkAuthority, RequiredWorkCoordinator, RequiredWorkSource
    from tests.helpers.continuation_policy import minimal_continuation_policy

    _require_structure_api()
    initial_profile = sys.getprofile()
    trace = RetentionMemoTrace(monkeypatch, request)
    trace.observe_structure(engines=(engine,), entrypoint="calibration")
    handles = trace.structure_listener_handles()
    coordinator = RequiredWorkCoordinator(RequiredWorkAuthority(RequiredAuthorityKind.DURABLE_COMPOSE, context, operation_id, 1))
    ticket = coordinator.reserve(RequiredWorkSource.PIPELINE_PUBLICATION_SQL)
    for target, name, callback, retval in listeners:
        event.listen(target, name, callback, retval=retval)

    async def operation():
        outcome = await async_workers.run_required_sql_finish_once(ticket, work)
        if isinstance(outcome, RequiredSQLRaised):
            raise outcome.error
        assert isinstance(outcome, RequiredSQLReturned), "real store/driver premise must return before the qualification assertion"
        assert ticket.complete and not ticket.errors and async_workers.outstanding_admissions() == 0
        return outcome.value

    try:
        timer = clock.ContinuationActionClock(minimal_continuation_policy().actions.session_sql)
        timer.finish_preparation()
        result = await timer.run("retention", operation)
    finally:
        for target, name, callback, _retval in reversed(listeners):
            event.remove(target, name, callback)
    if refused:
        with pytest.raises(AssertionError, match="structure observation incomplete"):
            trace.finish(stages=1, require_snapshot=False, require_population=False, structure_observation=True)
    else:
        # The intended SELECT-only baseline RED is HERE, after native premises.
        trace.finish(stages=1, require_snapshot=False, require_population=False, structure_observation=True)
    snapshot = trace.structure_snapshot()
    coordinator.assert_completed()
    assert snapshot.required and snapshot.closed
    assert snapshot.complete is not refused and snapshot.observation_verified is not refused
    assert bool(snapshot.errors) is refused
    assert all(not event.contains(target, name, callback) for target, name, callback in handles)
    assert sys.getprofile() is initial_profile
    assert _structure_events(snapshot, "sql_attempt"), "raw physical SQL cannot disappear on refused provenance"
    return result, snapshot


def _closed_dml_append(conn, context, operation_id, selected):
    from elspeth.web.sessions import continuation as store

    assert store.require_continuation_mutation(conn, context) is True
    return store.append_complete_snapshot(
        conn,
        session_id=str(context.fence.session_id),
        operation_id=operation_id,
        expected_checkpoint_id=selected.checkpoint_id,
        expected_cursor=selected.cursor,
        snapshot_json=selected.payload_json,
        event_batch=(store.ContinuationEvent("checkpoint", '{"owned":"closed-dml"}'),),
        handoff=False,
    )


def _closed_dml_rows(snapshot):
    return tuple(row for row in _structure_events(snapshot, "sql_return") if row.sql is not None and row.sql.kind in ("insert", "update"))


@pytest.mark.asyncio
async def test_structure_dml_original_put_chunk_is_complete_and_source_bound(monkeypatch, request):
    from sqlalchemy.sql.dml import Insert

    from elspeth.web.sessions import continuation as store
    from elspeth.web.sessions.locking import locked_session_transaction

    _require_structure_api()
    engine = request.getfixturevalue("engine")
    repository, context, operation_id, _selected, _revision = _closed_dml_seed(engine)
    native = []
    payload = b"independently authored chunk \xe2\x98\x83"

    def after(_conn, _cursor, _statement, parameters, current, many):
        if type(current.invoked_statement) is Insert:
            assert current.invoked_statement.table is store.chunks and not many
            assert type(parameters) is tuple
            native.append(len(parameters))

    def work():
        with locked_session_transaction(engine, str(context.fence.session_id)) as conn:
            assert store.require_continuation_mutation(conn, context) is True
            for _ in range(2):
                store.put_chunk(
                    conn,
                    session_id=str(context.fence.session_id),
                    operation_id=operation_id,
                    key="closed-dml-positive",
                    payload=payload,
                    codec="control.v1",
                )
            assert (
                store.read_chunk(conn, session_id=str(context.fence.session_id), operation_id=operation_id, key="closed-dml-positive")
                == payload
            )
        # A second actual producer call takes its immutable-existing-row path.
        assert native == [7]
        return len(payload)

    try:
        result, snapshot = await _closed_dml_trial(
            monkeypatch, request, engine, context, operation_id, work, listeners=((engine, "after_cursor_execute", after, False),)
        )
        assert result == len(payload)
        rows = _closed_dml_rows(snapshot)
        assert len(rows) == 1 and rows[0].sql.kind == "insert" and rows[0].sql.bind_arities == (7,)
        assert not rows[0].sql.executemany and rows[0].outcome == "returned"
    finally:
        repository.release(context)


@pytest.mark.asyncio
async def test_structure_dml_original_append_preserves_full_publication_and_revision(monkeypatch, request):
    from sqlalchemy import select
    from sqlalchemy.sql.dml import Insert, Update

    from elspeth.web.sessions import continuation as store
    from elspeth.web.sessions.locking import locked_session_transaction

    _require_structure_api()
    engine = request.getfixturevalue("engine")
    repository, context, operation_id, selected, revision = _closed_dml_seed(engine)
    native = []

    def after(_conn, _cursor, _statement, parameters, current, many):
        statement = current.invoked_statement
        if type(statement) in (Insert, Update):
            assert type(parameters) is tuple and not many
            role = next(
                name
                for table, name in (
                    (store.chunks, "chunk"),
                    (store.events, "event"),
                    (store.checkpoints, "checkpoint"),
                    (store.jobs, "head"),
                )
                if statement.table is table
            )
            native.append((role, len(parameters)))

    def work():
        with locked_session_transaction(engine, str(context.fence.session_id)) as conn:
            published = _closed_dml_append(conn, context, operation_id, selected)
            assert published.cursor == selected.cursor + 1 and published.checkpoint_id != selected.checkpoint_id
            assert store.load_snapshot(conn, session_id=str(context.fence.session_id), operation_id=operation_id) == published
            assert (
                conn.execute(
                    select(store.jobs.c.status_revision).where(
                        store.jobs.c.session_id == context.fence.session_id, store.jobs.c.operation_id == operation_id
                    )
                ).scalar_one()
                == revision + 1
            )
        # Independent native driver observations derive from the five original
        # writes: event chunk/event, complete snapshot chunk/checkpoint, head CAS.
        assert native == [("chunk", 7), ("event", 6), ("chunk", 7), ("checkpoint", 8), ("head", 9)]
        return published.cursor

    try:
        result, snapshot = await _closed_dml_trial(
            monkeypatch, request, engine, context, operation_id, work, listeners=((engine, "after_cursor_execute", after, False),)
        )
        assert result == selected.cursor + 1
        rows = _closed_dml_rows(snapshot)
        assert tuple(row.sql.kind for row in rows) == ("insert", "insert", "insert", "insert", "update")
        assert tuple(row.sql.bind_arities for row in rows) == ((7,), (6,), (7,), (8,), (9,))
        assert all(row.outcome == "returned" and not row.sql.executemany for row in rows)
    finally:
        repository.release(context)


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", ["assignment", "predicate", "revision", "insert-prefix"])
async def test_structure_dml_changed_current_assignment_predicate_revision_or_clause_refuses(monkeypatch, request, changed):
    from sqlalchemy import select
    from sqlalchemy.sql.dml import Insert, Update
    from sqlalchemy.util import immutabledict

    from elspeth.web.sessions import continuation as store
    from elspeth.web.sessions.locking import locked_session_transaction

    _require_structure_api()
    engine = request.getfixturevalue("engine")
    repository, context, operation_id, selected, revision = _closed_dml_seed(engine)
    reached = []

    def before(_conn, statement, multiparams, params, options):
        del options
        if changed == "insert-prefix" and type(statement) is Insert and statement.table is store.chunks:
            statement._prefixes = statement.prefix_with("OR IGNORE")._prefixes
            reached.append(changed)
        elif changed != "insert-prefix" and type(statement) is Update and statement.table is store.jobs:
            if changed == "assignment":
                values = dict(statement._values)
                values["started_at"] = values.pop("updated_at")
                statement._values = immutabledict(values)
            elif changed == "predicate":
                statement._where_criteria += (store.jobs.c.status == "running",)
            else:
                statement._values = statement._values.union({"status_revision": store.jobs.c.status_revision + 0})
            reached.append(changed)
        return statement, multiparams, params

    def work():
        with locked_session_transaction(engine, str(context.fence.session_id)) as conn:
            published = _closed_dml_append(conn, context, operation_id, selected)
            assert published.cursor == selected.cursor + 1
            actual_revision = conn.execute(
                select(store.jobs.c.status_revision).where(
                    store.jobs.c.session_id == context.fence.session_id, store.jobs.c.operation_id == operation_id
                )
            ).scalar_one()
            assert actual_revision == revision + int(changed != "revision")
        assert reached and set(reached) == {changed}
        return published.cursor

    try:
        result, _snapshot = await _closed_dml_trial(
            monkeypatch, request, engine, context, operation_id, work, refused=True, listeners=((engine, "before_execute", before, True),)
        )
        assert result == selected.cursor + 1
    finally:
        repository.release(context)


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", ["producer", "insert-visitor", "update-visitor", "aggregate-visitor"])
async def test_structure_dml_changed_then_restored_original_sources_refuse(monkeypatch, request, changed):
    from elspeth.web.sessions import continuation as store
    from elspeth.web.sessions.locking import locked_session_transaction

    _require_structure_api()
    engine = request.getfixturevalue("engine")
    repository, context, operation_id, selected, _revision = _closed_dml_seed(engine)
    original = store.put_chunk
    method = {"update-visitor": "visit_update", "aggregate-visitor": "visit_function"}.get(changed, "visit_insert")
    owner = next(cls for cls in engine.dialect.statement_compiler.__mro__ if method in vars(cls))
    visitor = vars(owner)[method]
    engine.clear_compiled_cache()
    reached = []

    def source(*args, **kwargs):
        reached.append("producer")
        return original(*args, **kwargs)

    def compile_original(self, *args, **kwargs):
        reached.append(changed)
        return visitor(self, *args, **kwargs)

    def work():
        with monkeypatch.context() as patch:
            if changed == "producer":
                patch.setattr(store, "put_chunk", source)
            else:
                patch.setattr(owner, method, compile_original)
            with locked_session_transaction(engine, str(context.fence.session_id)) as conn:
                if changed == "producer":
                    # The genuine saved original body still runs while its
                    # defining module dispatch is replaced; no wrapper credit.
                    assert store.require_continuation_mutation(conn, context) is True
                    original(
                        conn,
                        session_id=str(context.fence.session_id),
                        operation_id=operation_id,
                        key="restored-source",
                        payload=b"source",
                        codec="control.v1",
                    )
                    assert (
                        store.read_chunk(conn, session_id=str(context.fence.session_id), operation_id=operation_id, key="restored-source")
                        == b"source"
                    )
                else:
                    assert _closed_dml_append(conn, context, operation_id, selected).cursor == selected.cursor + 1
        assert store.put_chunk is original and vars(owner)[method] is visitor
        if changed == "producer":
            assert reached == []
        else:
            assert changed in reached
        return 1

    try:
        result, _snapshot = await _closed_dml_trial(monkeypatch, request, engine, context, operation_id, work, refused=True)
        assert result == 1
    finally:
        repository.release(context)


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", ["missing-carrier", "unobserved-engine", "unwrapped-insert"])
async def test_structure_dml_actual_current_owner_and_carrier_are_required(monkeypatch, request, changed):
    from sqlalchemy import insert

    from elspeth.web.continuation_retention_memo import without_retention_stage_memo
    from elspeth.web.sessions import continuation as store
    from elspeth.web.sessions.locking import locked_session_transaction

    _require_structure_api()
    engine = request.getfixturevalue("engine")
    physical = _structure_secondary_engine() if changed == "unobserved-engine" else engine
    repository, context, operation_id, _selected, _revision = _closed_dml_seed(physical)

    def native():
        with locked_session_transaction(physical, str(context.fence.session_id)) as conn:
            assert store.require_continuation_mutation(conn, context) is True
            if changed == "unwrapped-insert":
                # Real same-shaped SQL is adversarial extra work, not a store
                # producer and never eligible merely because its table matches.
                conn.execute(
                    insert(store.chunks).values(
                        session_id=str(context.fence.session_id),
                        operation_id=operation_id,
                        chunk_key="owner-opposed",
                        payload=b"owned",
                        sha256=store.sha256(b"owned").hexdigest(),
                        byte_length=5,
                        codec="control.v1",
                    )
                )
            else:
                store.put_chunk(
                    conn,
                    session_id=str(context.fence.session_id),
                    operation_id=operation_id,
                    key="owner-opposed",
                    payload=b"owned",
                    codec="control.v1",
                )
            assert (
                store.read_chunk(conn, session_id=str(context.fence.session_id), operation_id=operation_id, key="owner-opposed") == b"owned"
            )
        return 1

    def work():
        if changed == "missing-carrier":
            with without_retention_stage_memo():
                return native()
        return native()

    try:
        result, snapshot = await _closed_dml_trial(monkeypatch, request, engine, context, operation_id, work, refused=True)
        assert result == 1 and _structure_events(snapshot, "sql_attempt")
    finally:
        repository.release(context)
        if physical is not engine:
            physical.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal", ["returned", "late-listener", "driver-unwind", "wrong-final-arity"])
async def test_structure_dml_real_driver_layout_and_public_terminal_remain_distinct(monkeypatch, request, terminal):
    import sqlite3

    from sqlalchemy.exc import ProgrammingError
    from sqlalchemy.sql.dml import Insert

    from elspeth.web.sessions import continuation as store
    from elspeth.web.sessions.locking import locked_session_transaction

    _require_structure_api()
    engine = request.getfixturevalue("engine")
    repository, context, operation_id, _selected, _revision = _closed_dml_seed(engine)
    marker = RuntimeError("closed DML late original listener")
    entered, returned, errors = [], [], []

    def before(_conn, _cursor, statement, parameters, current, many):
        if type(current.invoked_statement) is Insert and current.invoked_statement.table is store.chunks:
            assert type(parameters) is tuple and not many
            entered.append(len(parameters))
            if terminal == "wrong-final-arity":
                return statement, parameters[:-1]
        return statement, parameters

    def after(_conn, _cursor, _statement, _parameters, current, _many):
        if type(current.invoked_statement) is Insert and current.invoked_statement.table is store.chunks:
            returned.append(1)
            if terminal == "late-listener":
                raise marker

    def work():
        with locked_session_transaction(engine, str(context.fence.session_id)) as conn:
            assert store.require_continuation_mutation(conn, context) is True
            kwargs = {
                "session_id": str(context.fence.session_id),
                "operation_id": operation_id,
                "key": "driver-terminal",
                "payload": b"terminal",
                "codec": ("invalid-driver-codec",) if terminal == "driver-unwind" else "control.v1",
            }
            if terminal in ("driver-unwind", "wrong-final-arity"):
                with pytest.raises(ProgrammingError) as caught:
                    store.put_chunk(conn, **kwargs)
                assert type(caught.value.orig) is sqlite3.ProgrammingError
                errors.append(caught.value)
            elif terminal == "late-listener":
                with pytest.raises(RuntimeError) as caught:
                    store.put_chunk(conn, **kwargs)
                assert caught.value is marker
                errors.append(caught.value)
                assert (
                    store.read_chunk(conn, session_id=str(context.fence.session_id), operation_id=operation_id, key="driver-terminal")
                    == b"terminal"
                )
            else:
                store.put_chunk(conn, **kwargs)
        assert entered == [7]
        assert returned == ([] if terminal in ("driver-unwind", "wrong-final-arity") else [1])
        assert len(errors) == int(terminal != "returned")
        return 1

    try:
        result, snapshot = await _closed_dml_trial(
            monkeypatch,
            request,
            engine,
            context,
            operation_id,
            work,
            refused=terminal == "wrong-final-arity",
            listeners=((engine, "before_cursor_execute", before, True), (engine, "after_cursor_execute", after, False)),
        )
        assert result == 1
        if terminal != "wrong-final-arity":
            rows = _closed_dml_rows(snapshot)
            assert len(rows) == 1 and rows[0].sql.bind_arities == (7,)
            assert rows[0].outcome == ("returned" if terminal == "returned" else "unwound")
        else:
            # The actual mismatched row remains raw evidence, never the cached
            # template's seven-bind claim or a successful structural shape.
            assert not snapshot.complete and not snapshot.observation_verified
            rows = _closed_dml_rows(snapshot)
            assert len(rows) == 1 and rows[0].sql.bind_arities == (6,) and rows[0].outcome == "unwound"
    finally:
        repository.release(context)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changed", ["original", "function", "column", "argument", "arity", "source", "func-binding", "missing-carrier", "unobserved-engine"]
)
async def test_structure_only_original_snapshot_sequence_aggregate_is_supported(monkeypatch, request, changed):
    from sqlalchemy import func
    from sqlalchemy.exc import OperationalError
    from sqlalchemy.sql import Select
    from sqlalchemy.sql.functions import coalesce

    from elspeth.contracts.errors import AuditIntegrityError
    from elspeth.web.continuation_retention_memo import without_retention_stage_memo
    from elspeth.web.sessions import continuation as store
    from elspeth.web.sessions.locking import locked_session_transaction

    _require_structure_api()
    engine = request.getfixturevalue("engine")
    physical = _structure_secondary_engine() if changed == "unobserved-engine" else engine
    repository, context, operation_id, selected, _revision = _closed_dml_seed(physical)
    original = store.load_snapshot
    namespace = store.func
    reached = []

    def replacement(*args, **kwargs):
        reached.append("wrapper")
        return original(*args, **kwargs)

    def before(_conn, statement, multiparams, params, options):
        del options
        if type(statement) is Select and len(statement._raw_columns) == 1 and type(statement._raw_columns[0]) is coalesce:
            assert not reached
            reached.append(changed)
            if changed == "function":
                statement._raw_columns = [func.coalesce(func.min(store.events.c.sequence), 0)]
            elif changed == "column":
                statement._raw_columns = [func.coalesce(func.max(store.events.c.session_id), 0)]
            elif changed == "argument":
                statement._raw_columns = [func.coalesce(func.max(store.events.c.sequence), 1)]
            elif changed == "arity":
                statement._raw_columns = [func.coalesce(func.max(store.events.c.sequence))]
        return statement, multiparams, params

    def read(conn):
        if changed == "missing-carrier":
            with without_retention_stage_memo():
                return original(conn, session_id=str(context.fence.session_id), operation_id=operation_id)
        return original(conn, session_id=str(context.fence.session_id), operation_id=operation_id)

    def work():
        with monkeypatch.context() as patch:
            if changed == "source":
                patch.setattr(store, "load_snapshot", replacement)
            elif changed == "func-binding":
                patch.setattr(store, "func", type(namespace)())
            with locked_session_transaction(physical, str(context.fence.session_id)) as conn:
                assert store.require_continuation_mutation(conn, context) is True
                if changed == "arity":
                    with pytest.raises(OperationalError):
                        original(conn, session_id=str(context.fence.session_id), operation_id=operation_id)
                elif changed == "argument":
                    with pytest.raises(AuditIntegrityError, match="event/snapshot cursor integrity mismatch"):
                        original(conn, session_id=str(context.fence.session_id), operation_id=operation_id)
                else:
                    assert read(conn) == selected
        assert store.load_snapshot is original and store.func is namespace and reached == [changed]
        return 1

    try:
        result, snapshot = await _closed_dml_trial(
            monkeypatch,
            request,
            engine,
            context,
            operation_id,
            work,
            refused=changed != "original",
            listeners=((physical, "before_execute", before, True),),
        )
        assert result == 1
        if changed == "original":
            assert snapshot.complete and snapshot.observation_verified
    finally:
        repository.release(context)
        if physical is not engine:
            physical.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("wrong", ["checkpoint", "cursor"])
async def test_structure_dml_original_cas_refusal_retains_reads_without_inventing_writes(monkeypatch, request, wrong):
    from sqlalchemy.sql.dml import Insert, Update

    from elspeth.web.sessions import continuation as store
    from elspeth.web.sessions.locking import locked_session_transaction

    _require_structure_api()
    engine = request.getfixturevalue("engine")
    repository, context, operation_id, selected, _revision = _closed_dml_seed(engine)
    writes, errors = [], []

    def before(_conn, statement, multiparams, params, _options):
        if type(statement) in (Insert, Update):
            writes.append(1)
        return statement, multiparams, params

    def work():
        with locked_session_transaction(engine, str(context.fence.session_id)) as conn:
            assert store.require_continuation_mutation(conn, context) is True
            with pytest.raises(store.ContinuationCASConflict, match="continuation snapshot compare-and-swap failed") as caught:
                store.append_complete_snapshot(
                    conn,
                    session_id=str(context.fence.session_id),
                    operation_id=operation_id,
                    expected_checkpoint_id=selected.checkpoint_id + "-wrong" if wrong == "checkpoint" else selected.checkpoint_id,
                    expected_cursor=selected.cursor + int(wrong == "cursor"),
                    snapshot_json=selected.payload_json,
                    event_batch=(store.ContinuationEvent("checkpoint", '{"owned":"not-published"}'),),
                    handoff=False,
                )
            errors.append(caught.value)
            assert store.load_snapshot(conn, session_id=str(context.fence.session_id), operation_id=operation_id) == selected
        assert not writes and len(errors) == 1
        return 1

    try:
        result, snapshot = await _closed_dml_trial(
            monkeypatch, request, engine, context, operation_id, work, listeners=((engine, "before_execute", before, True),)
        )
        assert result == 1 and not _closed_dml_rows(snapshot)
        assert _structure_selects(snapshot)
    finally:
        repository.release(context)


@pytest.mark.asyncio
@pytest.mark.parametrize("replacement_scope", ["slot", "container"])
async def test_structure_dml_composed_retval_copy_cannot_borrow_current_context_row(monkeypatch, request, replacement_scope):
    from sqlalchemy.sql.dml import Insert

    from elspeth.web.sessions import continuation as store
    from elspeth.web.sessions.locking import locked_session_transaction

    _require_structure_api()
    engine = request.getfixturevalue("engine")
    repository, context, operation_id, _selected, _revision = _closed_dml_seed(engine)
    payload = b"independently authored composed-row provenance control"
    replacements, after_driver, public_returns = [], [], []

    def replace_current_row(_conn, _cursor, statement, parameters, current, many):
        if type(current.invoked_statement) is Insert and current.invoked_statement.table is store.chunks:
            assert not replacements and not many and type(parameters) is tuple and len(parameters) == 7
            assert type(current.parameters) is list and len(current.parameters) == 1 and current.parameters[0] is parameters
            original_container = id(current.parameters)
            replacement = (*parameters,)
            assert replacement is not parameters
            assert all(original is copied for original, copied in zip(parameters, replacement, strict=True))
            if replacement_scope == "slot":
                current.parameters[0] = replacement
                assert id(current.parameters) == original_container
            else:
                current.parameters = [replacement]
                assert id(current.parameters) != original_container
            # Only numeric identities survive this callback. In particular the
            # control does not keep the original tuple alive for the observer.
            replacements.append((id(parameters), id(replacement), original_container, id(current.parameters)))
            return statement, replacement
        return statement, parameters

    def after(_conn, _cursor, _statement, parameters, current, many):
        if type(current.invoked_statement) is Insert and current.invoked_statement.table is store.chunks:
            assert type(parameters) is tuple and len(parameters) == 7 and not many
            assert len(replacements) == 1 and id(parameters) == replacements[0][1]
            assert id(current.parameters) == replacements[0][3] and current.parameters[0] is parameters
            after_driver.append((id(parameters), len(parameters)))

    def work():
        with locked_session_transaction(engine, str(context.fence.session_id)) as conn:
            assert store.require_continuation_mutation(conn, context) is True
            for _ in range(2):
                assert (
                    store.put_chunk(
                        conn,
                        session_id=str(context.fence.session_id),
                        operation_id=operation_id,
                        key="composed-row-copy",
                        payload=payload,
                        codec="control.v1",
                    )
                    is None
                )
                public_returns.append(1)
            assert (
                store.read_chunk(conn, session_id=str(context.fence.session_id), operation_id=operation_id, key="composed-row-copy")
                == payload
            )
        assert public_returns == [1, 1]
        assert len(replacements) == 1 and replacements[0][0] != replacements[0][1]
        assert after_driver == [(replacements[0][1], 7)]
        return len(payload)

    try:
        result, snapshot = await _closed_dml_trial(
            monkeypatch,
            request,
            engine,
            context,
            operation_id,
            work,
            refused=True,
            listeners=((engine, "before_cursor_execute", replace_current_row, True), (engine, "after_cursor_execute", after, False)),
        )
        assert result == len(payload)
        # Native insert/public return and arity remain truthful even though
        # the composed provenance replacement must invalidate qualification.
        rows = _closed_dml_rows(snapshot)
        assert len(rows) == 1 and rows[0].sql.bind_arities == (7,) and rows[0].outcome == "returned"
        assert snapshot.closed and not snapshot.complete and not snapshot.observation_verified
    finally:
        repository.release(context)


@pytest.mark.asyncio
@pytest.mark.parametrize("replacement_kind", ["distinct-quoted-name", "other-owned-column", "str-subclass"])
async def test_structure_dml_current_bind_name_requires_the_specific_original_assignment_key(monkeypatch, request, replacement_kind):
    from sqlalchemy.sql.dml import Insert
    from sqlalchemy.sql.elements import quoted_name

    from elspeth.web.sessions import continuation as store
    from elspeth.web.sessions.locking import locked_session_transaction

    class ForeignSymbolicName(str):
        pass

    _require_structure_api()
    engine = request.getfixturevalue("engine")
    repository, context, operation_id, _selected, _revision = _closed_dml_seed(engine)
    saved = []
    try:
        original_key = store.chunks.c.session_id.key
        assert type(original_key) is quoted_name
        if replacement_kind == "distinct-quoted-name":
            replacement = quoted_name("session_id", original_key.quote)
        elif replacement_kind == "other-owned-column":
            replacement = store.events.c.session_id.key
            assert type(replacement) is quoted_name
        else:
            replacement = ForeignSymbolicName("session_id")
            assert type(replacement) is ForeignSymbolicName
        assert replacement is not original_key and replacement == original_key
        reached, native, public_returns = [], [], []
        payload = b"independently authored nominal bind key control"

        def before(_conn, _cursor, statement, parameters, current, many):
            if type(current.invoked_statement) is Insert and current.invoked_statement.table is store.chunks:
                assert not saved and type(current.compiled) is engine.dialect.statement_compiler
                assert type(parameters) is tuple and len(parameters) == 7 and not many
                matches = tuple((node, name) for node, name in current.compiled.bind_names.items() if name is original_key)
                assert len(matches) == 1
                node, name = matches[0]
                # Borrow only the genuine mapping entry for guaranteed restoration.
                # No processed row/context/connection/SQL/result is retained.
                mapping = current.compiled.bind_names
                saved.append((mapping, node, name))
                mapping[node] = replacement
                assert mapping[node] is replacement
                reached.append((id(node), id(name), id(replacement)))
            return statement, parameters

        def after(_conn, _cursor, _statement, parameters, current, many):
            if type(current.invoked_statement) is Insert and current.invoked_statement.table is store.chunks:
                assert type(parameters) is tuple and len(parameters) == 7 and not many
                assert len(saved) == 1 and current.compiled.bind_names[saved[0][1]] is replacement
                native.append(7)

        def work():
            with locked_session_transaction(engine, str(context.fence.session_id)) as conn:
                assert store.require_continuation_mutation(conn, context) is True
                for _ in range(2):
                    assert (
                        store.put_chunk(
                            conn,
                            session_id=str(context.fence.session_id),
                            operation_id=operation_id,
                            key="nominal-bind-key",
                            payload=payload,
                            codec="control.v1",
                        )
                        is None
                    )
                    public_returns.append(1)
                assert (
                    store.read_chunk(conn, session_id=str(context.fence.session_id), operation_id=operation_id, key="nominal-bind-key")
                    == payload
                )
            assert public_returns == [1, 1] and native == [7] and len(reached) == 1
            assert reached[0][1] != reached[0][2]
            return len(payload)

        result, snapshot = await _closed_dml_trial(
            monkeypatch,
            request,
            engine,
            context,
            operation_id,
            work,
            refused=True,
            listeners=((engine, "before_cursor_execute", before, True), (engine, "after_cursor_execute", after, False)),
        )
        assert result == len(payload)
        rows = _closed_dml_rows(snapshot)
        assert len(rows) == 1 and rows[0].sql.bind_arities == (7,) and rows[0].outcome == "returned"
        assert "publication_bind_name_provenance" in snapshot.errors, "the specific nominal guard must be reached"
        assert snapshot.closed and not snapshot.complete and not snapshot.observation_verified
    finally:
        try:
            for mapping, node, name in reversed(saved):
                mapping[node] = name
                assert mapping[node] is original_key
        finally:
            repository.release(context)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mutation", "guard"),
    [
        ("original", None),
        ("plain-dict", "source_cache_type"),
        ("nonempty", "source_cache_nonempty"),
        ("wrong-function", "source_cache_creator"),
        ("wrong-class", "source_cache_creator"),
        ("instance-creator", "source_cache_creator_shadow"),
        ("compiler-shadow", "source_compiler_dispatch"),
        ("factory-shadow", "source_compile_state_factory"),
        ("extra-state", "source_cache_state"),
        ("descriptor", "source_cache_source"),
        ("getter-code", "source_cache_source"),
        ("creator-descriptor", "source_cache_source"),
        ("creator-code", "source_cache_source"),
        ("cache-init", "source_cache_source"),
        ("cache-missing", "source_cache_source"),
        ("cache-attribute", "source_cache_source"),
        ("descriptor-name", "source_cache_source"),
    ],
)
async def test_structure_dml_current_empty_dialect_cache_is_nominal_and_source_bound(monkeypatch, request, mutation, guard):
    from types import MethodType

    from sqlalchemy.sql import base as sql_base
    from sqlalchemy.sql.dml import Insert, Update
    from sqlalchemy.util import PopulateDict

    from elspeth.web.sessions import continuation as store
    from elspeth.web.sessions.locking import locked_session_transaction

    def foreign_getter(self):
        raise AssertionError("changed getter must not be invoked")

    def foreign_creator(cls, key):
        raise AssertionError("changed creator must not be invoked")

    def foreign_initializer(self, creator):
        raise AssertionError("changed initializer must not be invoked")

    def foreign_missing(self, key):
        raise AssertionError("changed missing-key method must not be invoked")

    def forward_attribute(self, name):
        return object.__getattribute__(self, name)

    _require_structure_api()
    engine = request.getfixturevalue("engine")
    patcher = pytest.MonkeyPatch()
    repository, context, operation_id, _selected, _revision = _closed_dml_seed(engine)
    try:
        owner = sql_base.DialectKWArgs
        descriptor = owner.__dict__["dialect_options"]
        getter = descriptor.fget
        creator_descriptor = owner.__dict__["_kw_reg_for_dialect_cls"]
        creator_function = creator_descriptor.__func__
        reached, native, public_returns = [], [], []
        payload = b"independently authored original empty dialect cache"

        def before(_conn, _cursor, statement, parameters, current, many):
            if type(current.invoked_statement) is Insert and current.invoked_statement.table is store.chunks:
                assert not reached and type(parameters) is tuple and len(parameters) == 7 and not many
                node = current.invoked_statement
                # Borrow only the actual already-materialized current cache.
                # Never access its descriptor, call its creator or compile again.
                state = object.__getattribute__(node, "__dict__")
                assert "dialect_options" in state
                cache = state["dialect_options"]
                assert type(cache) is PopulateDict and dict.__len__(cache) == 0
                cache_state = object.__getattribute__(cache, "__dict__")
                assert type(cache_state) is dict and tuple(cache_state) == ("creator",)
                creator = cache_state["creator"]
                assert type(creator) is MethodType and creator.__self__ is Insert and creator.__func__ is creator_function
                if mutation == "plain-dict":
                    patcher.setitem(state, "dialect_options", {})
                elif mutation == "nonempty":
                    patcher.setitem(cache, "owned-control-option", None)
                elif mutation == "wrong-function":
                    patcher.setitem(cache_state, "creator", MethodType(foreign_creator, Insert))
                elif mutation == "wrong-class":
                    patcher.setitem(cache_state, "creator", MethodType(creator_function, Update))
                elif mutation == "instance-creator":
                    patcher.setitem(state, "_kw_reg_for_dialect_cls", creator)
                elif mutation == "compiler-shadow":
                    patcher.setitem(state, "_compiler_dispatch", node._compiler_dispatch)
                elif mutation == "factory-shadow":
                    patcher.setitem(state, "_compile_state_factory", node._compile_state_factory)
                elif mutation == "extra-state":
                    patcher.setitem(cache_state, "__getattribute__", forward_attribute)
                elif mutation == "descriptor":
                    patcher.setattr(owner, "dialect_options", property(getter))
                elif mutation == "getter-code":
                    patcher.setattr(getter, "__code__", foreign_getter.__code__)
                elif mutation == "creator-descriptor":
                    patcher.setattr(owner, "_kw_reg_for_dialect_cls", classmethod(creator_function))
                elif mutation == "creator-code":
                    patcher.setattr(creator_function, "__code__", foreign_creator.__code__)
                elif mutation == "cache-init":
                    patcher.setattr(PopulateDict.__dict__["__init__"], "__code__", foreign_initializer.__code__)
                elif mutation == "cache-missing":
                    patcher.setattr(PopulateDict.__dict__["__missing__"], "__code__", foreign_missing.__code__)
                elif mutation == "cache-attribute":
                    patcher.setattr(PopulateDict, "__getattribute__", forward_attribute)
                elif mutation == "descriptor-name":
                    patcher.setattr(descriptor, "__name__", "owned_control_other_cache")
                else:
                    assert mutation == "original"
                reached.append(1)
            return statement, parameters

        def after(_conn, _cursor, _statement, parameters, current, many):
            if type(current.invoked_statement) is Insert and current.invoked_statement.table is store.chunks:
                assert type(parameters) is tuple and len(parameters) == 7 and not many and reached == [1]
                native.append(7)
                # The final-driver provenance audit has observed the change.
                # Restore before subsequent original SQL/public completion.
                patcher.undo()

        def work():
            with locked_session_transaction(engine, str(context.fence.session_id)) as conn:
                assert store.require_continuation_mutation(conn, context) is True
                for _ in range(2):
                    assert (
                        store.put_chunk(
                            conn,
                            session_id=str(context.fence.session_id),
                            operation_id=operation_id,
                            key="empty-dialect-cache",
                            payload=payload,
                            codec="control.v1",
                        )
                        is None
                    )
                    public_returns.append(1)
                assert (
                    store.read_chunk(conn, session_id=str(context.fence.session_id), operation_id=operation_id, key="empty-dialect-cache")
                    == payload
                )
            assert reached == [1] and native == [7] and public_returns == [1, 1]
            return len(payload)

        result, snapshot = await _closed_dml_trial(
            monkeypatch,
            request,
            engine,
            context,
            operation_id,
            work,
            refused=guard is not None,
            listeners=((engine, "before_cursor_execute", before, True), (engine, "after_cursor_execute", after, False)),
        )
        assert result == len(payload)
        rows = _closed_dml_rows(snapshot)
        assert len(rows) == 1 and rows[0].sql.bind_arities == (7,) and rows[0].outcome == "returned"
        if guard is None:
            assert snapshot.closed and snapshot.complete and snapshot.observation_verified and not snapshot.errors
        else:
            if mutation in (
                "descriptor",
                "getter-code",
                "creator-descriptor",
                "creator-code",
                "cache-init",
                "cache-missing",
                "cache-attribute",
                "descriptor-name",
            ):
                assert guard == "source_cache_source", "static source opponents retain their declared source domain"
                assert "original_source_changed" in snapshot.errors, "the owning every-event source audit must refuse changed source"
            else:
                assert guard != "source_cache_source", "only the closed static-source family uses the source-audit oracle"
                assert "publication_guard_" + guard in snapshot.errors, "the specific current-cache/source guard must be reached"
            assert snapshot.closed and not snapshot.complete and not snapshot.observation_verified
    finally:
        try:
            patcher.undo()
        finally:
            repository.release(context)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [False, True])
async def test_structure_dml_cache_creator_transient_code_change_is_audited_before_restore(monkeypatch, request, change):
    from sqlalchemy.sql import base as sql_base
    from sqlalchemy.sql.dml import Insert

    from elspeth.web.sessions import continuation as store
    from elspeth.web.sessions.locking import locked_session_transaction

    def foreign_creator(cls, key):
        raise AssertionError("transient creator must never be invoked")

    _require_structure_api()
    engine = request.getfixturevalue("engine")
    original_code = None
    creator = None
    repository, context, operation_id, _selected, _revision = _closed_dml_seed(engine)
    try:
        creator = sql_base.DialectKWArgs.__dict__["_kw_reg_for_dialect_cls"].__func__
        original_code = creator.__code__
        assert foreign_creator.__code__ is not original_code
        intervals, native, public_returns = [], [], []
        payload = b"independently authored audited cache source interval"

        def before(_conn, _cursor, statement, parameters, current, many):
            if type(current.invoked_statement) is Insert and current.invoked_statement.table is store.chunks:
                assert not intervals and type(parameters) is tuple and len(parameters) == 7 and not many
                assert creator.__code__ is original_code
                if change:
                    creator.__code__ = foreign_creator.__code__
                try:
                    assert creator.__code__ is (foreign_creator.__code__ if change else original_code)
                    # A genuine builtin C-call occurs while the code is changed.
                    # No Python helper/patcher/shape checkpoint intervenes.
                    actual_arity = len(parameters)
                    assert actual_arity == 7
                    intervals.append((change, actual_arity))
                finally:
                    creator.__code__ = original_code
                assert creator.__code__ is original_code
            return statement, parameters

        def after(_conn, _cursor, _statement, parameters, current, many):
            if type(current.invoked_statement) is Insert and current.invoked_statement.table is store.chunks:
                assert creator.__code__ is original_code and intervals == [(change, 7)]
                assert type(parameters) is tuple and len(parameters) == 7 and not many
                native.append(7)

        def work():
            with locked_session_transaction(engine, str(context.fence.session_id)) as conn:
                assert store.require_continuation_mutation(conn, context) is True
                for _ in range(2):
                    assert (
                        store.put_chunk(
                            conn,
                            session_id=str(context.fence.session_id),
                            operation_id=operation_id,
                            key="audited-cache-source",
                            payload=payload,
                            codec="control.v1",
                        )
                        is None
                    )
                    public_returns.append(1)
                assert (
                    store.read_chunk(conn, session_id=str(context.fence.session_id), operation_id=operation_id, key="audited-cache-source")
                    == payload
                )
            assert intervals == [(change, 7)] and native == [7] and public_returns == [1, 1]
            return len(payload)

        result, snapshot = await _closed_dml_trial(
            monkeypatch,
            request,
            engine,
            context,
            operation_id,
            work,
            refused=change,
            listeners=((engine, "before_cursor_execute", before, True), (engine, "after_cursor_execute", after, False)),
        )
        assert result == len(payload) and creator.__code__ is original_code
        rows = _closed_dml_rows(snapshot)
        assert len(rows) == 1 and rows[0].sql.bind_arities == (7,) and rows[0].outcome == "returned"
        assert snapshot.closed
        if change:
            assert "original_source_changed" in snapshot.errors, "the intervening audited event must invalidate source coverage"
            assert not snapshot.complete and not snapshot.observation_verified
        else:
            assert snapshot.complete and snapshot.observation_verified and not snapshot.errors
    finally:
        try:
            if creator is not None and original_code is not None:
                creator.__code__ = original_code
        finally:
            repository.release(context)
