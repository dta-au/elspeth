"""R2 projection-disagreement control against the actual shelf producer.

Only the first borrowed size observation is changed. Actual ordered encoders,
snapshot validation, source content and the later binding are left intact.
"""

from dataclasses import replace

import pytest

from elspeth.contracts import bounded_serialization
from elspeth.contracts.bounded_serialization import BoundedCanonicalSize
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.contracts.prompt_guard import PROMPT_GUARD_CONTENT_PARTS_KEY
from elspeth.web.composer.advisor_transcript import AdvisorTranscriptOwner
from elspeth.web.composer.bounded_json import JSON_MAX_TOTAL_UTF8_BYTES
from elspeth.web.composer.continuation_message_family_recipes import ToolResultMessageRecipe
from elspeth.web.composer.continuation_message_shelf import encode_message_shelf
from elspeth.web.composer.protocol import ComposerContextUnsupported
from tests.unit.web.sessions.test_continuation_live_phases import _advance, _phase_case
from tests.unit.web.sessions.test_continuation_live_phases import operation_store as _operation_store
from tests.unit.web.sessions.test_continuation_primary_result import stored_rows

operation_store = _operation_store


def test_admitted_borrowed_observation_cannot_reclassify_actual_encoder_excess(monkeypatch):
    """Intentional observation mismatch; no natural encoder-drift claim."""
    owner = AdvisorTranscriptOwner(request="")
    messages = [{"role": "user", "content": 'é\\"\n\u0001' * 7_000 + f"tail-{number}"} for number in range(3)]
    owner.observe_messages(messages)
    base = owner.snapshot_for_continuation(messages)
    overhead = len(canonical_json(base.model_dump(mode="json")).encode("utf-8"))
    assert overhead < JSON_MAX_TOTAL_UTF8_BYTES
    owner.request = "x" * (JSON_MAX_TOTAL_UTF8_BYTES - overhead)

    fitting = owner.snapshot_for_continuation(messages, mandatory_retention=True)
    assert len(canonical_json(fitting.model_dump(mode="json")).encode("utf-8")) == JSON_MAX_TOTAL_UTF8_BYTES
    assert encode_message_shelf(fitting, mandatory_retention=True).reference.byte_length == JSON_MAX_TOTAL_UTF8_BYTES

    owner.request += "x"
    actually_excessive = owner.snapshot_for_continuation(messages)
    assert len(canonical_json(actually_excessive.model_dump(mode="json")).encode("utf-8")) == JSON_MAX_TOTAL_UTF8_BYTES + 1
    with pytest.raises(ComposerContextUnsupported) as natural:
        owner.snapshot_for_continuation(messages, mandatory_retention=True)
    assert natural.value.reason == "retained_history_bytes"

    actual_measure = bounded_serialization.bounded_canonical_json_size
    injected = []

    def observe(value, *, limit):
        measured = actual_measure(value, limit=limit)
        if not injected and type(value) is dict and "schema_version" in value and value["schema_version"] == "composer.transcript.v1":
            assert measured.exceeded and limit == JSON_MAX_TOTAL_UTF8_BYTES
            injected.append(measured)
            return BoundedCanonicalSize(admitted_bytes=JSON_MAX_TOTAL_UTF8_BYTES, limit=limit)
        return measured

    monkeypatch.setattr(bounded_serialization, "bounded_canonical_json_size", observe)
    with pytest.raises(Exception) as raised:
        owner.snapshot_for_continuation(messages, mandatory_retention=True)
    assert len(injected) == 1 and injected[0].exceeded, "alter only the actual first borrowed shelf observation"
    assert type(raised.value) is AuditIntegrityError, (
        "an already admitted borrowed observation that disagrees with the actual produced shelf is an integrity defect; "
        f"received {type(raised.value).__name__}"
    )
    assert "borrowed retention preflight" in str(raised.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("natural_excess", (False, True), ids=("fitting", "natural-excess"))
@pytest.mark.parametrize("fault", ("none", "equal_body", "equal_partition", "equal_part", "repartitioned_body", "changed_body"))
async def test_actual_tool_result_provenance_precedes_retention_excess(operation_store, tmp_path, monkeypatch, natural_excess, fault):
    """Use the actual ordinary result's private registration and real SQL cut."""
    async with _phase_case(operation_store, tmp_path, monkeypatch) as (owner, _running, _primary, sdk):
        await _advance(owner)
        runtime = owner.runtime
        assert runtime is not None
        producer = runtime.materialization
        registered = tuple((message, recipe) for message, recipe, _wire in producer._produced if type(recipe) is ToolResultMessageRecipe)
        assert len(registered) == 1
        message, recipe = registered[0]
        assert any(actual is message for actual in runtime.live.llm_messages)
        content, parts = message["content"], message[PROMPT_GUARD_CONTENT_PARTS_KEY]
        assert type(content) is str and len(content) > 1 and type(parts) is tuple and parts
        if natural_excess:
            amendments = [{"role": "user", "content": f"Amendment {number}: " + 'é\\"\n' * 24_000} for number in range(3)]
            runtime.live.llm_messages.extend(amendments)
            runtime.live.turn_evidence.transcript.observe_messages(amendments)
        transcript = runtime.live.turn_evidence.transcript
        original = transcript.snapshot_for_continuation(runtime.live.llm_messages, materialization=producer)
        size = len(canonical_json(original.model_dump(mode="json")).encode("utf-8"))
        assert (size > JSON_MAX_TOTAL_UTF8_BYTES) is natural_excess
        before = stored_rows(operation_store[0])

        if fault == "equal_body":
            message["content"] = (" " + content)[1:]
            assert message["content"] == content and message["content"] is not content
        elif fault == "equal_partition":
            message[PROMPT_GUARD_CONTENT_PARTS_KEY] = tuple(part for part in parts)
            assert message[PROMPT_GUARD_CONTENT_PARTS_KEY] == parts and message[PROMPT_GUARD_CONTENT_PARTS_KEY] is not parts
        elif fault == "equal_part":
            message[PROMPT_GUARD_CONTENT_PARTS_KEY] = (replace(parts[0]), *parts[1:])
            assert message[PROMPT_GUARD_CONTENT_PARTS_KEY] == parts and message[PROMPT_GUARD_CONTENT_PARTS_KEY][0] is not parts[0]
        elif fault == "repartitioned_body":
            # Different ordered part ownership, same complete untrusted text.
            split = len(parts[0].text) // 2
            assert 0 < split < len(parts[0].text)
            message[PROMPT_GUARD_CONTENT_PARTS_KEY] = (
                replace(parts[0], text=parts[0].text[:split]),
                replace(parts[0], text=parts[0].text[split:]),
                *parts[1:],
            )
            assert "".join(part.text for part in message[PROMPT_GUARD_CONTENT_PARTS_KEY]) == content
        elif fault == "changed_body":
            message["content"] = content + " "
        else:
            assert fault == "none"

        # Fresh retention must not call allocating ordinary capture to discover
        # provenance. The existing actual message/recipe/body stays decisive.
        capture_calls = []

        def must_not_capture(self, actual):
            capture_calls.append(actual)
            raise AssertionError("retention preflight entered allocating capture_message")

        monkeypatch.setattr(type(producer), "capture_message", must_not_capture)
        if fault != "none":
            with pytest.raises(AuditIntegrityError):
                transcript.snapshot_for_continuation(runtime.live.llm_messages, materialization=producer, mandatory_retention=True)
        elif natural_excess:
            with pytest.raises(ComposerContextUnsupported) as raised:
                transcript.snapshot_for_continuation(runtime.live.llm_messages, materialization=producer, mandatory_retention=True)
            assert raised.value.reason == "retained_history_bytes"
        else:
            # Fitting shelf production legitimately performs ordinary capture
            # after preflight; check the borrowed view without that later step.
            projected, actual_recipe = producer.retention_message_view(message)
            assert projected.matches(next(wire for actual, _recipe, wire in producer._produced if actual is message))
            assert actual_recipe is recipe
        assert capture_calls == []
        assert stored_rows(operation_store[0]) == before and len(sdk) == 1
        assert registered[0][0] is message and producer._tool_result_provenance[0][0] is message
