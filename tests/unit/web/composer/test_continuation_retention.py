"""Exact mandatory-retention sizing; diagnostic/provider budgets are separate."""

import math
from types import MappingProxyType, MemberDescriptorType

import pytest
import rfc8785

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.web.composer.bounded_json import JSON_MAX_TOTAL_UTF8_BYTES
from elspeth.web.composer.continuation_retention import (
    OrderedObjectJSON,
    _ModelProjection,
    _RetainedArrayView,
    _RetainedObjectView,
    bind_retained_json_encoding,
    preflight_retained_json,
    retained_model_json_view,
)
from elspeth.web.composer.protocol import ComposerContextUnsupported


@pytest.mark.parametrize("reason", ("retained_history_bytes", "retained_chunk_bytes"))
def test_complete_canonical_retention_exact_limit_and_one_byte_over(reason):
    source = {"complete": 'é\\"\n\u0001', "received": "first", "effective": "second"}
    complete = canonical_json(source)
    value = {
        "messages_json": (complete, complete),
        "observed": ({"event_json": complete, "pending_adoption": False},),
        "history": {"events_json": canonical_json((source, source))},
        "producer_recipes": ({"kind": "initial", "index": 0}, None),
        "padding": "",
    }
    overhead = len(canonical_json(value).encode("utf-8"))
    value["padding"] = "x" * (JSON_MAX_TOTAL_UTF8_BYTES - overhead)
    expected = canonical_json(value)
    assert len(expected.encode("utf-8")) == JSON_MAX_TOTAL_UTF8_BYTES
    admitted = preflight_retained_json(value, reason=reason)
    assert admitted == JSON_MAX_TOTAL_UTF8_BYTES
    assert bind_retained_json_encoding(expected, expected_bytes=admitted) == expected
    value["padding"] += "x"
    with pytest.raises(ComposerContextUnsupported) as refused:
        preflight_retained_json(value, reason=reason)
    assert refused.value.reason == reason
    assert "first" not in str(refused.value) and "second" not in str(refused.value)
    assert refused.value.to_payload()["failure_code"] == "context_unsupported"


@pytest.mark.parametrize("tail", (math.nan, math.inf, 2**53, object(), "\ud800"))
def test_invalid_complete_value_outranks_retention_excess_after_large_prefix(tail):
    value = ["x" * (JSON_MAX_TOTAL_UTF8_BYTES + 1), tail]
    with pytest.raises((ValueError, rfc8785.CanonicalizationError)):
        canonical_json(value)
    with pytest.raises((ValueError, rfc8785.CanonicalizationError)):
        preflight_retained_json(value, reason="retained_chunk_bytes")


def test_cycle_after_large_prefix_remains_a_canonical_failure():
    value = ["x" * (JSON_MAX_TOTAL_UTF8_BYTES + 1)]
    value.append(value)
    with pytest.raises(RecursionError):
        preflight_retained_json(value, reason="retained_history_bytes")


@pytest.mark.parametrize("changed", ('{"content":"changed"}', ' {"content":"same"}'))
def test_encoding_binding_keeps_projection_mismatch_as_integrity(changed):
    value = {"content": "same"}
    measured = preflight_retained_json(value, reason="retained_chunk_bytes")
    expected = canonical_json(value)
    assert bind_retained_json_encoding(expected, expected_bytes=measured) == expected
    with pytest.raises(AuditIntegrityError, match="retention preflight"):
        bind_retained_json_encoding(changed, expected_bytes=measured)


def test_overlarge_encoder_output_cannot_bypass_the_preflight_binding():
    measured = preflight_retained_json({}, reason="retained_chunk_bytes")
    with pytest.raises(AuditIntegrityError, match="retention preflight"):
        bind_retained_json_encoding('"' + "x" * JSON_MAX_TOTAL_UTF8_BYTES + '"', expected_bytes=measured)


@pytest.mark.parametrize("number", (1e-7, 1e-6, 1.0, -0.0, 2**53 - 1))
def test_borrowed_ordered_json_counts_actual_inner_and_outer_escaping(number):
    from elspeth.web.composer.continuation_message_shelf import encode_transcript_object_json

    value = MappingProxyType({"second": (number, 'é\\"\n\u0001'), "first": {"tail": "complete"}})
    encoded = encode_transcript_object_json(value)
    projected = OrderedObjectJSON(value)
    expected = len(canonical_json(encoded).encode("utf-8"))
    assert projected.canonical_string_bytes(limit=expected) == expected
    assert projected.canonical_string_bytes(limit=expected - 1) == expected
    assert projected.matches(encoded)
    assert not projected.matches(encoded + " ")
    assert not projected.matches(encoded.replace("complete", "changed!"))


@pytest.mark.parametrize("change", ("source", "delete_source", "reinitialize", "extra", "reset_initialized", "delete_initialized"))
def test_ordered_json_view_keeps_live_source_and_closed_bindings(change):
    from elspeth.web.composer.continuation_message_shelf import encode_transcript_object_json

    children = ['é\\"\n']
    source = {"second": children, "first": "original"}
    view = OrderedObjectJSON(source=source)
    encoded = encode_transcript_object_json(source)
    assert view.source is source and view.matches(encoded)
    assert view.canonical_string_bytes(limit=10_000) == len(canonical_json(encoded).encode("utf-8"))
    assert type(OrderedObjectJSON.__dict__["source"]) is MemberDescriptorType
    assert OrderedObjectJSON.__getattribute__ is object.__getattribute__
    equal_owner = dict(source)
    equal_view = OrderedObjectJSON(equal_owner)
    assert equal_view == view and equal_view.source is equal_owner and equal_view.source is not source

    class OtherOrderedJSON(OrderedObjectJSON):
        __slots__ = ()

    assert view != OtherOrderedJSON(source)
    assert OtherOrderedJSON(source) == OtherOrderedJSON(equal_owner)
    assert view != source
    with pytest.raises(AttributeError):
        if change == "source":
            view.source = equal_owner
        elif change == "delete_source":
            del view.source
        elif change == "reinitialize":
            view.__init__(equal_owner)
        elif change == "extra":
            view.__setattr__("unexpected", source)
        elif change == "reset_initialized":
            view._initialized = False
        else:
            del view._initialized
    assert view.source is source and view.matches(encoded)
    with pytest.raises(AttributeError):
        view.__init__(source)
    # The inherited ABC dictionary remains, but cannot shadow a public slot.
    view.__dict__["source"] = equal_owner
    assert view.source is source

    children.append("later")
    source["third"] = "new field"
    current = encode_transcript_object_json(source)
    assert current != encoded and view.matches(current) and not view.matches(encoded)
    assert view.canonical_string_bytes(limit=10_000) == len(canonical_json(current).encode("utf-8"))


@pytest.mark.parametrize("change", ("source", "projection", "delete_source", "delete_projection", "reinitialize", "extra"))
def test_retained_object_view_keeps_live_projection_aliases_and_cycles(change):
    children = ["first"]
    source = {"array": children}
    projection = _ModelProjection()
    view = projection.value(source)
    assert type(view) is _RetainedObjectView
    assert view.source is source and view.projection is projection
    assert list(view) == ["array"] and len(view) == 1
    array = view["array"]
    assert type(array) is _RetainedArrayView
    assert array.source is children and array.projection is projection
    assert list(array) == children
    assert preflight_retained_json(view, reason="retained_chunk_bytes") == len(canonical_json(source).encode("utf-8"))
    assert type(_RetainedObjectView.__dict__["source"]) is MemberDescriptorType
    assert type(_RetainedObjectView.__dict__["projection"]) is MemberDescriptorType
    assert _RetainedObjectView.__getattribute__ is object.__getattribute__
    foreign_source = dict(source)
    foreign_projection = _ModelProjection()
    equal_view = _RetainedObjectView(foreign_source, projection)
    assert equal_view == view and equal_view.source is not view.source

    class OtherObjectView(_RetainedObjectView):
        __slots__ = ()

    assert view.__eq__(OtherObjectView(source, projection)) is NotImplemented
    assert OtherObjectView(source, projection) == OtherObjectView(foreign_source, projection)
    assert view != source
    with pytest.raises(AttributeError):
        if change == "source":
            view.source = foreign_source
        elif change == "projection":
            view.projection = foreign_projection
        elif change == "delete_source":
            del view.source
        elif change == "delete_projection":
            del view.projection
        elif change == "reinitialize":
            view.__init__(foreign_source, foreign_projection)
        else:
            view.__setattr__("unexpected", source)
    assert view.source is source and view.projection is projection
    assert view["array"] is array
    with pytest.raises(AttributeError):
        view._initialized = False
    with pytest.raises(AttributeError):
        del view._initialized
    with pytest.raises(AttributeError):
        view.__init__(source, projection)
    with pytest.raises(AuditIntegrityError, match="non-string key"):
        view[1]

    source["alias"] = children
    children.append("later")
    assert len(view) == 2 and list(view) == ["array", "alias"]
    assert view["alias"] is array and list(array) == ["first", "later"]
    assert preflight_retained_json(view, reason="retained_chunk_bytes") == len(canonical_json(source).encode("utf-8"))
    children.append(source)
    assert list(array)[-1] is view
    with pytest.raises(RecursionError):
        preflight_retained_json(view, reason="retained_chunk_bytes")


@pytest.mark.parametrize("change", ("source", "projection", "delete_source", "delete_projection", "reinitialize", "extra"))
def test_retained_array_view_keeps_live_projection_aliases_and_cycles(change):
    child = {"value": "first"}
    source = [child]
    projection = _ModelProjection()
    view = projection.value(source)
    assert type(view) is _RetainedArrayView
    assert view.source is source and view.projection is projection and len(view) == 1
    projected_child = next(iter(view))
    assert type(projected_child) is _RetainedObjectView and projected_child.source is child
    assert projected_child["value"] == "first"
    assert preflight_retained_json(view, reason="retained_chunk_bytes") == len(canonical_json(source).encode("utf-8"))
    assert type(_RetainedArrayView.__dict__["source"]) is MemberDescriptorType
    assert type(_RetainedArrayView.__dict__["projection"]) is MemberDescriptorType
    assert _RetainedArrayView.__getattribute__ is object.__getattribute__
    foreign_source = list(source)
    foreign_projection = _ModelProjection()
    equal_view = _RetainedArrayView(foreign_source, projection)
    assert equal_view == view and equal_view.source is not view.source

    class OtherArrayView(_RetainedArrayView):
        __slots__ = ()

    assert view.__eq__(OtherArrayView(source, projection)) is NotImplemented
    assert OtherArrayView(source, projection) == OtherArrayView(foreign_source, projection)
    assert view != source
    with pytest.raises(AttributeError):
        if change == "source":
            view.source = foreign_source
        elif change == "projection":
            view.projection = foreign_projection
        elif change == "delete_source":
            del view.source
        elif change == "delete_projection":
            del view.projection
        elif change == "reinitialize":
            view.__init__(foreign_source, foreign_projection)
        else:
            view.__setattr__("unexpected", source)
    assert view.source is source and view.projection is projection
    with pytest.raises(AttributeError):
        view._initialized = False
    with pytest.raises(AttributeError):
        del view._initialized
    with pytest.raises(AttributeError):
        view.__init__(source, projection)

    child["value"] = "later"
    source.append(child)
    assert len(view) == 2 and all(item is projected_child for item in view)
    assert projected_child["value"] == "later"
    assert preflight_retained_json(view, reason="retained_chunk_bytes") == len(canonical_json(source).encode("utf-8"))
    source.append(source)
    assert list(view)[-1] is view
    with pytest.raises(RecursionError):
        preflight_retained_json(view, reason="retained_chunk_bytes")


def test_fresh_shelf_exact_limit_cold_roundtrip_then_refuses_before_inner_serialization(monkeypatch):
    from elspeth.web.composer import advisor_transcript
    from elspeth.web.composer.advisor_transcript import AdvisorTranscriptOwner
    from elspeth.web.composer.continuation_message_shelf import decode_message_shelf, encode_message_shelf

    owner = AdvisorTranscriptOwner(request="")
    messages = [{"role": "user", "content": 'é\\"\n\u0001' * 7_000 + f"tail-{number}"} for number in range(3)]
    owner.observe_messages(messages)
    base = owner.snapshot_for_continuation(messages)
    overhead = len(canonical_json(base.model_dump(mode="json")).encode("utf-8"))
    assert overhead < JSON_MAX_TOTAL_UTF8_BYTES
    owner.request = "x" * (JSON_MAX_TOTAL_UTF8_BYTES - overhead)
    actual_encoder = advisor_transcript.encode_transcript_object_json
    calls = []

    def observed_encoder(value):
        calls.append(value)
        return actual_encoder(value)

    monkeypatch.setattr(advisor_transcript, "encode_transcript_object_json", observed_encoder)
    snapshot = owner.snapshot_for_continuation(messages, mandatory_retention=True)
    assert calls, "positive control: fitting retention invokes the actual inner encoder"
    chunk = encode_message_shelf(snapshot, mandatory_retention=True)
    assert chunk.reference.byte_length == JSON_MAX_TOTAL_UTF8_BYTES
    cold = decode_message_shelf(chunk)
    restored, current = AdvisorTranscriptOwner.for_continuation(
        cold, session_id="session", actor_user_id="actor", request_id="request", request=owner.request
    )
    assert current == messages and restored.snapshot() == owner.snapshot()
    assert preflight_retained_json(retained_model_json_view(cold), reason="retained_history_bytes") == chunk.reference.byte_length
    calls.clear()
    owner.request += "x"
    with pytest.raises(ComposerContextUnsupported) as refused:
        owner.snapshot_for_continuation(messages, mandatory_retention=True)
    assert refused.value.reason == "retained_history_bytes"
    assert calls == [], "excess is refused before inner ordered JSON is allocated"
    with pytest.raises(AuditIntegrityError, match="admission bound"):
        encode_message_shelf(snapshot.model_copy(update={"request": owner.request}))


def test_invalid_shelf_data_keeps_integrity_precedence_over_oversize():
    from elspeth.web.composer.advisor_transcript import AdvisorTranscriptOwner
    from elspeth.web.composer.continuation_message_shelf import encode_message_shelf

    message = {"role": "user", "content": "complete source"}
    owner = AdvisorTranscriptOwner(request="request")
    owner.observe_messages((message,))
    snapshot = owner.snapshot_for_continuation((message,))
    corrupt = snapshot.model_copy(update={"messages_json": ('{"duplicate":1,"duplicate":2}',), "request": "x" * JSON_MAX_TOTAL_UTF8_BYTES})
    with pytest.raises(AuditIntegrityError, match="invalid complete owned model"):
        encode_message_shelf(corrupt, mandatory_retention=True)


def test_complete_credential_after_shelf_excess_precedes_retention_refusal():
    from elspeth.web.composer.advisor_transcript import AdvisorTranscriptOwner
    from elspeth.web.credential_guard import CredentialMaterialRefused

    owner = AdvisorTranscriptOwner(request="request")
    messages = [{"role": "user", "content": 'é\\"\n' * 24_000} for _ in range(3)]
    messages.append({"role": "user", "content": "sk-" + "syntheticcredential" * 3})
    owner.observe_messages(messages)
    with pytest.raises(CredentialMaterialRefused) as refused:
        owner.snapshot_for_continuation(messages, mandatory_retention=True)
    assert refused.value.finding.category == "token_shape"


def test_native_above_one_mib_has_an_independent_complete_retention_refusal():
    from elspeth.web.composer.advisor_transcript import AdvisorTranscriptOwner

    owner = AdvisorTranscriptOwner(request="request")
    native = [{"type": "redacted_thinking", "data": "A" * (1100 * 1024)}]
    message = {"role": "assistant", "content": "", "thinking_blocks": native}
    owner.observe_messages((message,))
    with pytest.raises(ComposerContextUnsupported) as refused:
        owner.snapshot_for_continuation((message,), mandatory_retention=True)
    assert refused.value.reason == "retained_history_bytes"
    assert message["thinking_blocks"] is native


def test_fresh_private_chunk_exact_limit_binds_real_encoder_and_default_excess_stays_integrity():
    from elspeth.web.composer.continuation_codec import encode_mandatory_private_chunk, encode_private_chunk, verify_private_chunk

    value = {"padding": ""}
    overhead = len(canonical_json(value).encode("utf-8"))
    value["padding"] = "x" * (JSON_MAX_TOTAL_UTF8_BYTES - overhead)
    chunk = encode_mandatory_private_chunk(codec="composer.tools.v1", value=value)
    assert chunk.reference.byte_length == JSON_MAX_TOTAL_UTF8_BYTES
    verify_private_chunk(chunk.reference, chunk.canonical)
    value["padding"] += "x"
    with pytest.raises(ComposerContextUnsupported) as refused:
        encode_mandatory_private_chunk(codec="composer.tools.v1", value=value)
    assert refused.value.reason == "retained_chunk_bytes"
    with pytest.raises(AuditIntegrityError, match="admission bound"):
        encode_private_chunk(codec="composer.tools.v1", canonical=canonical_json(value))
    with pytest.raises(AuditIntegrityError, match="binding"):
        verify_private_chunk(chunk.reference, chunk.canonical.replace("x", "y", 1))


def test_borrowed_shelf_measurement_does_not_call_the_full_model_dump(monkeypatch):
    from elspeth.web.composer.advisor_transcript import AdvisorTranscriptOwner
    from elspeth.web.composer.continuation_message_shelf import MessageShelfSnapshot, encode_message_shelf

    owner = AdvisorTranscriptOwner(request="request")
    message = {"role": "user", "content": 'é\\"\n'}
    owner.observe_messages((message,))
    snapshot = owner.snapshot_for_continuation((message,))
    expected = len(canonical_json(snapshot.model_dump(mode="json")).encode("utf-8"))
    calls = []
    actual_dump = MessageShelfSnapshot.model_dump

    def dump(self, *args, **kwargs):
        calls.append(True)
        return actual_dump(self, *args, **kwargs)

    monkeypatch.setattr(MessageShelfSnapshot, "model_dump", dump)
    assert preflight_retained_json(retained_model_json_view(snapshot), reason="retained_history_bytes") == expected
    assert calls == []
    assert encode_message_shelf(snapshot).reference.byte_length == expected
    assert calls, "positive control: the actual encoder still uses model_dump"
