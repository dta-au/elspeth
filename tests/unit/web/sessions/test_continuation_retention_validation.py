"""Opposed current-live validation controls for U22 R1.

Selected stored evidence stays valid. Only the current live history field and
the natural amendment cohort vary after a fitting paid checkpoint is proven.
"""

import pytest
from pydantic import ValidationError

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.web.composer import advisor_transcript
from elspeth.web.composer.bounded_json import JSON_MAX_TOTAL_UTF8_BYTES
from elspeth.web.composer.continuation_primary import read_continuation_primary
from elspeth.web.composer.continuation_runtime import capture_continuation_runtime_owners, restore_continuation_runtime
from elspeth.web.composer.protocol import ComposerContextUnsupported
from elspeth.web.sessions.composer_operation_errors import project_composer_operation_error
from tests.unit.web.coordination import test_composer_operation_authority as operations
from tests.unit.web.sessions.test_continuation_runtime_provider_history import (
    append_actual_native_and_capture,
    full_rows,
    prepared_runtime,
    publish,
)

operation_store = operations.operation_store


def _retained_history_validation(error: BaseException) -> bool:
    """Require the existing retained-history validator, even if wrapped."""
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, ValidationError) and "evidence retained history is not its complete object" in str(current):
            return True
        current = current.__cause__
    return False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "malformed_live,natural_excess",
    (
        pytest.param(False, False, id="valid-fitting"),
        pytest.param(True, False, id="malformed-live-fitting"),
        pytest.param(True, True, id="malformed-live-with-natural-excess"),
        pytest.param(False, True, id="valid-live-with-natural-excess"),
    ),
)
async def test_current_live_history_validation_precedes_natural_shelf_classification(
    operation_store, tmp_path, monkeypatch, malformed_live, natural_excess
):
    arguments, runtime, sdk_calls = await prepared_runtime(operation_store, tmp_path, monkeypatch)
    running = arguments["inventory"].running
    try:
        paid = await publish(operation_store, arguments, runtime)
        arguments, runtime = await append_actual_native_and_capture(operation_store, arguments, runtime, paid)
        prior = arguments["selected"]
        original_messages = tuple(runtime.live.llm_messages)
        original_history = runtime.live.turn_evidence._retained_history
        assert original_history == runtime.source.evidence.retained_history
        assert len(sdk_calls) == 1

        actual_encoder = advisor_transcript.encode_transcript_object_json
        encoded_objects = []

        def encode(value):
            encoded_objects.append(value)
            return actual_encoder(value)

        monkeypatch.setattr(advisor_transcript, "encode_transcript_object_json", encode)
        fitting = await capture_continuation_runtime_owners(runtime, selected=prior, inventory=arguments["inventory"])
        assert fitting.selected == prior
        assert encoded_objects, "positive control: valid fitting capture reaches the actual inner encoder"
        encoded_objects.clear()
        before = full_rows(operation_store[0])

        if malformed_live:
            # The selected SQL source and its bytes are not changed. This bad
            # live field is rejected by TurnEvidenceSnapshot.complete_links.
            runtime.live.turn_evidence._retained_history = "{}"
        if natural_excess:
            amendments = [{"role": "user", "content": f"Amendment {number}: " + 'é\\"\n' * 24_000} for number in range(3)]
            assert all(len(canonical_json(message).encode("utf-8")) < 262_144 for message in amendments)
            runtime.live.llm_messages.extend(amendments)
            runtime.live.turn_evidence.transcript.observe_messages(amendments)

        # Calibration uses the actual complete encoder outside the measured
        # capture attempt. The malformed live retained-history field is a
        # separate evidence member, absent from this transcript-only oracle.
        actual_shelf = runtime.live.turn_evidence.transcript.snapshot_for_continuation(
            runtime.live.llm_messages, materialization=runtime.materialization
        )
        shelf_bytes = len(canonical_json(actual_shelf.model_dump(mode="json")).encode("utf-8"))
        assert (shelf_bytes > JSON_MAX_TOTAL_UTF8_BYTES) is natural_excess
        encoded_objects.clear()

        if malformed_live or natural_excess:
            with pytest.raises(Exception) as raised:
                await capture_continuation_runtime_owners(runtime, selected=prior, inventory=arguments["inventory"])
            observed = raised.value
            encoded_at_capture = tuple(encoded_objects)
            projected = project_composer_operation_error(observed, request_id="r1-current-live")
            if malformed_live:
                assert isinstance(observed, (ValidationError, AuditIntegrityError)), (
                    "malformed current live retained history must keep defect precedence over shelf excess; "
                    f"received {type(observed).__name__}"
                )
                assert _retained_history_validation(observed), "reach the existing complete retained-history validator"
                assert projected.http_status == 500 and projected.error_type != "composer_context_unsupported"
            else:
                assert type(observed) is ComposerContextUnsupported and observed.reason == "retained_history_bytes"
                assert projected.http_status == 422 and projected.error_type == "composer_context_unsupported"
            if natural_excess:
                assert encoded_at_capture == (), "validate/refuse without serializing the excessive inner shelf"
        else:
            captured = await capture_continuation_runtime_owners(runtime, selected=prior, inventory=arguments["inventory"])
            assert captured.selected == prior and encoded_objects

        assert full_rows(operation_store[0]) == before
        assert len(sdk_calls) == 1, "no new SDK call follows any capture outcome"
        # Prove the unchanged selected source is still valid independently of
        # the deliberately malformed live owner, including the paid response.
        restored = await restore_continuation_runtime(**arguments)
        assert restored.source.selected == prior
        assert restored.live.turn_evidence._retained_history == original_history
        assert tuple(restored.live.llm_messages) == original_messages
        retained = await read_continuation_primary(
            authority=arguments["authority"], selected=prior, required_work=arguments["required_work"]
        )
        assert retained.response == paid.response and retained.outcome.audit_call == paid.outcome.audit_call
        assert full_rows(operation_store[0]) == before and len(sdk_calls) == 1
        arguments["required_work"].coordinator.assert_completed()
    finally:
        operation_store[1].release(running.session_operation_context)
