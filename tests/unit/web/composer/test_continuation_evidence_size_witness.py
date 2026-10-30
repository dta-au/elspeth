"""Independent small wire oracles for the deferred evidence diagnostic.

These controls do not qualify a natural Advisor run or its original outcome.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator

import pytest
import rfc8785

from elspeth.contracts.bounded_serialization import BoundedCanonicalSize, bounded_canonical_json_size
from elspeth.contracts.hashing import canonical_json
from elspeth.web.composer.bounded_json import JSON_MAX_TOTAL_UTF8_BYTES
from elspeth.web.composer.continuation import ContinuationValue, PrivateChunkRef
from elspeth.web.composer.continuation_evidence import TurnEvidenceFields, TurnEvidenceSnapshot
from elspeth.web.composer.continuation_message_shelf import MessageShelfSnapshot
from elspeth.web.composer.continuation_retention import OrderedObjectJSON, _RetainedModelView, retained_model_json_view
from tests.helpers import continuation_evidence_size_witness as diagnostic
from tests.helpers.continuation_evidence_size_witness import (
    TurnEvidenceSizeWitness,
    measure_retained_view,
    publish_turn_evidence_size_witness,
)
from tests.unit.web.composer.test_continuation_evidence_graph import rich_evidence, snapshot


class _Leaf(ContinuationValue):
    text: str
    nested: tuple[object, ...]


class _Envelope(ContinuationValue):
    effects: tuple[_Leaf, ...]
    transcript: str
    application_entry: PrivateChunkRef | None = None


def _small_view(text: str) -> tuple[_Envelope, object]:
    shared_text = (text + " owner end").encode().decode()
    equal_text = bytearray(shared_text.encode()).decode()
    assert shared_text == equal_text and shared_text is not equal_text
    shared = _Leaf(text=shared_text, nested=(True, None, 0, -1, 1.25, ("inner",)))
    equal = _Leaf(text=equal_text, nested=(True, None, 0, -1, 1.25, ("inner",)))
    assert shared == equal and shared is not equal
    envelope = _Envelope(effects=(shared, shared, equal), transcript=text)
    return envelope, retained_model_json_view(envelope)


def _oracle(model: ContinuationValue) -> int:
    # Complete encoding is confined to these deliberately small calibration
    # values. It is independent of the helper's primitive/syntax traversal.
    return len(canonical_json(model.model_dump(mode="json")).encode("utf-8"))


@pytest.mark.parametrize("text", ["ascii", 'é漢😀 quote" slash\\ newline\n control\x01', "\x00" * 25])
def test_same_view_witness_counts_exact_wire_and_preserves_guard(text: str) -> None:
    envelope, view = _small_view(text)
    expected = _oracle(envelope)
    measured = measure_retained_view(view)
    assert measured.status == "exact"
    assert measured.total_bytes == expected
    # Independent category oracle from the literal fixture structure: root
    # object, three-element effects array, then three leaf objects containing
    # a six-element nested array and a one-element inner array.
    expected_syntax = (2 + 3 + 2) + (2 + 2) + 3 * ((2 + 2 + 1) + (2 + 5) + 2)
    strings = (
        ("effects", "transcript", "application_entry")
        + 3
        * (
            "text",
            "nested",
            text + " owner end",
            "inner",
        )
        + (text,)
    )
    expected_raw = sum(len(value.encode("utf-8")) for value in strings)
    expected_string_wire = sum(len(canonical_json(value).encode("utf-8")) for value in strings)
    nonstrings = 3 * (True, None, 0, -1, 1.25) + (None,)
    expected_scalar = expected_string_wire + sum(len(canonical_json(value).encode("utf-8")) for value in nonstrings)
    expected_escaping = expected_string_wire - expected_raw - 2 * len(strings)
    assert expected_syntax + expected_scalar == expected
    assert measured.syntax_bytes == expected_syntax
    assert measured.scalar_bytes == expected_scalar
    assert measured.raw_utf8_bytes == expected_raw
    assert measured.string_wire_bytes == expected_string_wire
    assert measured.string_wire_bytes - measured.raw_utf8_bytes == expected_escaping + 2 * len(strings)
    assert measured.string_wire_bytes - measured.raw_utf8_bytes - 2 * len(strings) == expected_escaping
    transcript = next(item for item in measured.fields if item.path == "$.transcript")
    transcript_raw = len(text.encode("utf-8"))
    transcript_wire = len(canonical_json(text).encode("utf-8"))
    transcript_escaping = transcript_wire - transcript_raw - 2
    assert transcript.raw_utf8_bytes == transcript_raw
    assert transcript.string_wire_bytes == transcript.value_bytes == transcript_wire
    assert transcript.string_wire_bytes - transcript.raw_utf8_bytes == 2 + transcript_escaping
    assert transcript.key_bytes == len(canonical_json("transcript").encode("utf-8"))
    assert transcript.colon_bytes == transcript.comma_bytes == 1
    assert measured.scalar_bytes + measured.syntax_bytes == expected
    assert 2 + sum(item.member_bytes for item in measured.fields if item.path.count(".") == 1) == expected
    assert measured.string_wire_bytes > measured.raw_utf8_bytes
    repeated = next(item for item in measured.repeated_owners if item.paths == ("$.effects[0]", "$.effects[1]"))
    leaf_bytes = _oracle(envelope.effects[0])
    assert repeated.wire_bytes == (leaf_bytes, leaf_bytes)
    assert all("$.effects[2]" not in item.paths for item in measured.repeated_owners if item.kind == "_Leaf")
    repeated_text = next(item for item in measured.repeated_owners if item.paths == ("$.effects[0].text", "$.effects[1].text"))
    assert len(repeated_text.paths) == 2

    results: list[BoundedCanonicalSize] = []

    def original(value: object, *, limit: int) -> BoundedCanonicalSize:
        result = bounded_canonical_json_size(value, limit=limit)
        results.append(result)
        return result

    witness = TurnEvidenceSizeWitness(_counter=original)
    admitted = witness.observe_counter_call(view, limit=expected)
    assert admitted is results[0] and admitted.admitted_bytes == expected
    refused = witness.observe_counter_call(view, limit=expected - 1)
    assert refused is results[1] and refused.exceeded
    assert len(results) == 2
    assert witness.matching_calls == 0


def test_oracle_rejects_deliberately_omitted_repeated_string(monkeypatch: pytest.MonkeyPatch) -> None:
    envelope, view = _small_view("repeat calibration")
    shared_text = envelope.effects[0].text
    original = diagnostic._REAL_COUNTER
    calls = 0

    def omit_second_occurrence(value: object, *, limit: int) -> BoundedCanonicalSize:
        nonlocal calls
        result = original(value, limit=limit)
        if value is shared_text:
            calls += 1
            if calls == 2:
                return BoundedCanonicalSize(0, limit)
        return result

    monkeypatch.setattr(diagnostic, "_REAL_COUNTER", omit_second_occurrence)
    corrupted = measure_retained_view(view)
    assert calls == 2
    assert corrupted.status == "exact"
    omitted = bounded_canonical_json_size(shared_text, limit=2 + 6 * len(shared_text)).admitted_bytes
    assert omitted is not None
    assert corrupted.total_bytes == _oracle(envelope) - omitted
    # This must go RED under the planted omission, while internal additive
    # reconciliation alone cannot detect the lost occurrence.
    with pytest.raises(AssertionError):
        assert corrupted.total_bytes == _oracle(envelope)


@pytest.mark.parametrize("mutable", [{"payload": "borrowed"}, ["borrowed"]])
def test_mutable_borrowed_source_is_explicitly_incomplete(mutable: object) -> None:
    class Borrowed(ContinuationValue):
        payload: object

    source = Borrowed(payload=mutable)
    measured = measure_retained_view(retained_model_json_view(source))
    assert measured.status == "incomplete"
    assert measured.total_bytes is None
    assert measured.reason == f"$.payload: unsupported or mutable borrowed {type(mutable).__name__}"


def test_lazy_borrowed_string_is_explicitly_incomplete() -> None:
    class Borrowed(ContinuationValue):
        payload: object

    source = Borrowed(payload=OrderedObjectJSON({"message": "opaque"}))
    measured = measure_retained_view(retained_model_json_view(source))
    assert measured.status == "incomplete"
    assert measured.reason == "$.payload: unsupported or mutable borrowed OrderedObjectJSON"


def _target_view(snapshot_type: type[TurnEvidenceFields] = TurnEvidenceSnapshot) -> object:
    # Reuse the existing lawful source factory, then run the real constructors
    # for the changed shelf and snapshot. This tests the fixed observer budget,
    # not a natural Advisor payload or a bypass of source validation.
    evidence, messages, recorder, logical, _policy = rich_evidence()
    original = snapshot(evidence, messages, recorder, logical).snapshot
    shelf_values = {name: original.transcript.__dict__[name] for name in MessageShelfSnapshot.model_fields}
    shelf_values["request"] = "x" * JSON_MAX_TOTAL_UTF8_BYTES
    shelf = MessageShelfSnapshot(**shelf_values)
    values = {name: original.__dict__[name] for name in snapshot_type.model_fields}
    values["transcript"] = shelf
    return retained_model_json_view(snapshot_type(**values))


def test_observer_captures_only_exact_snapshot_excess_and_releases() -> None:
    results: list[BoundedCanonicalSize] = []

    def original(value: object, *, limit: int) -> BoundedCanonicalSize:
        result = bounded_canonical_json_size(value, limit=limit)
        results.append(result)
        return result

    witness = TurnEvidenceSizeWitness(_counter=original)
    target = _target_view()
    result = witness.observe_counter_call(target, limit=JSON_MAX_TOTAL_UTF8_BYTES)
    assert result is results[0] and result.exceeded
    assert witness.matching_calls == 1
    assert witness._view is target
    assert witness._snapshot is target.source
    assert witness._result is result
    report = witness.measure_captured_view()
    assert report["original_limit"] == JSON_MAX_TOTAL_UTF8_BYTES
    assert report["original_admitted_bytes"] is None
    assert report["measurement"]["status"] == "exact"
    assert report["measurement"]["total_bytes"] > JSON_MAX_TOTAL_UTF8_BYTES
    assert witness._view is witness._snapshot is witness._result is None


def test_admitted_exact_snapshot_at_actual_limit_is_not_captured() -> None:
    evidence, messages, recorder, logical, _policy = rich_evidence()
    source = snapshot(evidence, messages, recorder, logical).snapshot
    target = retained_model_json_view(source)
    assert type(target) is _RetainedModelView
    assert type(target.source) is TurnEvidenceSnapshot
    results: list[BoundedCanonicalSize] = []

    def original(value: object, *, limit: int) -> BoundedCanonicalSize:
        assert value is target
        assert limit == JSON_MAX_TOTAL_UTF8_BYTES
        result = bounded_canonical_json_size(value, limit=limit)
        results.append(result)
        return result

    witness = TurnEvidenceSizeWitness(_counter=original)
    result = witness.observe_counter_call(target, limit=JSON_MAX_TOTAL_UTF8_BYTES)
    assert len(results) == 1
    assert result is results[0]
    assert result.admitted_bytes is not None
    assert result.exceeded is False
    # Removing only the observer's excess predicate must fail these assertions:
    # this value satisfies every other exact-target capture condition.
    assert witness.matching_calls == 0
    assert witness._view is witness._snapshot is witness._result is None


@pytest.mark.parametrize("case", ["wrong-snapshot", "wrong-limit", "plain-object", "admitted", "snapshot-subclass"])
def test_non_targets_are_not_captured(case: str) -> None:
    class SnapshotSubclass(TurnEvidenceSnapshot):
        pass

    witness = TurnEvidenceSizeWitness()
    limit = JSON_MAX_TOTAL_UTF8_BYTES
    if case == "wrong-snapshot":
        value = retained_model_json_view(_Envelope(effects=(), transcript="x" * limit))
    elif case == "wrong-limit":
        value, limit = _target_view(), limit - 1
    elif case == "plain-object":
        value = {"large": "x" * limit}
    elif case == "admitted":
        value = retained_model_json_view(_Envelope(effects=(), transcript="small"))
    else:
        value = _target_view(SnapshotSubclass)
    result = witness.observe_counter_call(value, limit=limit)
    assert result.exceeded is (case != "admitted")
    assert witness.matching_calls == 0
    assert witness.measure_captured_view()["measurement"]["status"] == "incomplete"


def test_multiple_matching_calls_are_incomplete() -> None:
    witness = TurnEvidenceSizeWitness()
    target = _target_view()
    witness.observe_counter_call(target, limit=JSON_MAX_TOTAL_UTF8_BYTES)
    witness.observe_counter_call(target, limit=JSON_MAX_TOTAL_UTF8_BYTES)
    assert witness.matching_calls == 2
    report = witness.measure_captured_view()
    assert report["measurement"]["status"] == "incomplete"
    assert witness._view is witness._snapshot is witness._result is None


@pytest.mark.parametrize(
    "invalid,exception", [(object(), rfc8785.CanonicalizationError), (float("inf"), ValueError), ("\ud800", rfc8785.CanonicalizationError)]
)
def test_late_invalid_keeps_original_counter_exception(invalid: object, exception: type[Exception]) -> None:
    errors: list[Exception] = []

    def original(value: object, *, limit: int) -> BoundedCanonicalSize:
        try:
            return bounded_canonical_json_size(value, limit=limit)
        except exception as error:
            errors.append(error)
            raise

    witness = TurnEvidenceSizeWitness(_counter=original)
    with pytest.raises(exception) as raised:
        witness.observe_counter_call(["already excessive", invalid], limit=1)
    assert errors == [raised.value]
    assert errors[0] is raised.value
    assert witness.matching_calls == 0


def test_cycle_keeps_original_counter_exception() -> None:
    cycle: list[object] = ["already excessive"]
    cycle.append(cycle)
    witness = TurnEvidenceSizeWitness()
    with pytest.raises(RecursionError, match="Circular reference in canonical JSON input"):
        witness.observe_counter_call(cycle, limit=1)
    assert witness.matching_calls == 0


def test_nested_field_and_typed_reference_attribution() -> None:
    reference = PrivateChunkRef(codec="composer.state.v1", digest="a" * 64, byte_length=37)
    envelope = _Envelope(effects=(), transcript="opaque JSON remains text", application_entry=reference)
    measured = measure_retained_view(retained_model_json_view(envelope))
    assert measured.total_bytes == _oracle(envelope)
    assert {item.path for item in measured.fields if item.path.startswith("$.application_entry.")} == {
        "$.application_entry.codec",
        "$.application_entry.digest",
        "$.application_entry.byte_length",
    }
    assert measured.references == (diagnostic.ReferenceOccurrence("$.application_entry", reference.codec, reference.digest, 37),)
    assert "overlap" in measured.attribution


@pytest.fixture
def _output_custody_report(
    request: pytest.FixtureRequest,
    record_testsuite_property: Callable[[str, object], None],
) -> Iterator[list[str]]:
    phases = ["setup"]
    yield phases
    assert phases == ["setup", "call"]
    # A literal, payload-free small report exercises publication only. Its
    # illustrative wire value is {"transcript": ["é", "é"]}; no capture or
    # scalar measurement is substituted or repeated for this output control.
    report: dict[str, object] = {
        "measurement": {
            "status": "exact",
            "reason": None,
            "total_bytes": 26,
            "syntax_bytes": 6,
            "scalar_bytes": 20,
            "raw_utf8_bytes": 14,
            "string_wire_bytes": 20,
            "value_occurrences": 4,
            "owner_count": 3,
            "fields": [
                {
                    "path": "$.transcript",
                    "value_bytes": 11,
                    "key_bytes": 12,
                    "colon_bytes": 1,
                    "comma_bytes": 0,
                    "raw_utf8_bytes": 4,
                    "string_wire_bytes": 8,
                }
            ],
            "repeated_owners": [
                {
                    "label": "owner-3",
                    "kind": "str",
                    "paths": ["$.transcript[0]", "$.transcript[1]"],
                    "wire_bytes": [4, 4],
                }
            ],
            "references": [],
            "attribution": "parent/child and repeated-owner totals overlap; do not add them",
        },
        "matching_calls": 1,
        "original_limit": 25,
        "original_admitted_bytes": None,
        "diagnostic_seconds": 0.125,
        "scope": "known small output-custody calibration only",
        "terminal_member": {"marker": "café 漢 😀", "nested_arrays": [[], [1, None, True], ["terminal"]]},
    }
    publish_turn_evidence_size_witness(request, record_testsuite_property, report)


def test_output_custody_fixture_publishes_complete_report_in_teardown(_output_custody_report: list[str]) -> None:
    assert _output_custody_report == ["setup"]
    _output_custody_report.append("call")
