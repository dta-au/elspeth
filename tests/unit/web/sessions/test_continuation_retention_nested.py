"""Reused nested decision fields retain integrity precedence at shelf excess.

The real review/acceptance owner establishes both current and limitation aliases.
Only the copied nested reason and the natural amendment cohort then vary.
"""

import pytest
from pydantic import ValidationError

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.web.composer import advisor_transcript
from elspeth.web.composer.advisor_output import AdvisorAbandonmentResponse
from elspeth.web.composer.bounded_json import JSON_MAX_TOTAL_UTF8_BYTES
from elspeth.web.composer.continuation_evidence import decode_turn_evidence
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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "malformed_nested,natural_excess",
    (
        pytest.param(False, False, id="valid-nested-fitting"),
        pytest.param(True, False, id="malformed-nested-fitting"),
        pytest.param(True, True, id="malformed-nested-natural-excess"),
        pytest.param(False, True, id="valid-nested-natural-excess"),
    ),
)
async def test_reused_nested_decision_validation_precedes_natural_shelf_refusal(
    operation_store, tmp_path, monkeypatch, malformed_nested, natural_excess
):
    arguments, runtime, sdk_calls = await prepared_runtime(operation_store, tmp_path, monkeypatch)
    running = arguments["inventory"].running
    try:
        paid = await publish(operation_store, arguments, runtime)
        arguments, runtime = await append_actual_native_and_capture(operation_store, arguments, runtime, paid)
        prior = arguments["selected"]
        original_messages = tuple(runtime.live.llm_messages)
        owner = runtime.live.turn_evidence
        policy = runtime.bindings.plugin_snapshot
        review = owner.abandonment_context(
            request=runtime.bindings.message,
            current_state=runtime.live.state,
            plugin_snapshot=policy,
            messages=runtime.live.llm_messages,
        )
        decision = AdvisorAbandonmentResponse(
            decision="CONTINUE_WITH_LIMITATION",
            reason="Continue with the observed range limitation",
            next_action="Inspect the remaining supported range",
            question=None,
        )
        owner.accept_abandonment(review, decision, current_state=runtime.live.state, plugin_snapshot=policy)

        def require_actual_aliases(expected_decision):
            assert owner._abandonment_review[0] is review
            assert owner._abandonment_review[1] is expected_decision
            assert owner._abandonment_limitations[review.binding][0] is review
            assert owner._abandonment_limitations[review.binding][1] is expected_decision

        require_actual_aliases(decision)
        actual_encoder = advisor_transcript.encode_transcript_object_json
        encoded_objects = []

        def encode(value):
            encoded_objects.append(value)
            return actual_encoder(value)

        monkeypatch.setattr(advisor_transcript, "encode_transcript_object_json", encode)
        fitting = await capture_continuation_runtime_owners(runtime, selected=prior, inventory=arguments["inventory"])
        assert fitting.selected == prior and encoded_objects, "actual accepted nested owner reaches fitting inner encoding"
        evidence_chunk = next(chunk for chunk in fitting.chunks if chunk.reference.codec == "composer.evidence.v2")
        retained = decode_turn_evidence(evidence_chunk, env_ref_names=runtime.env_ref_names, source_chunks=fitting.chunks).snapshot
        assert retained.abandonment_review is not None
        assert retained.abandonment_limitations[0].value == retained.abandonment_review
        assert retained.decision_shelf[retained.abandonment_review.decision_index].reason == decision.reason
        require_actual_aliases(decision)
        before = full_rows(operation_store[0])

        selected_decision = decision
        if malformed_nested:
            selected_decision = decision.model_copy(update={"reason": ""})
            assert selected_decision is not decision and type(selected_decision) is AdvisorAbandonmentResponse
            # Keep the same accepted review and share the same invalid copied
            # decision in BOTH aliases. No selected stored source is changed.
            owner._abandonment_review = (review, selected_decision)
            owner._abandonment_limitations[review.binding] = (review, selected_decision)
        if natural_excess:
            amendments = [{"role": "user", "content": f"Amendment {number}: " + 'é\\"\n' * 24_000} for number in range(3)]
            assert all(len(canonical_json(message).encode("utf-8")) < 262_144 for message in amendments)
            runtime.live.llm_messages.extend(amendments)
            owner.transcript.observe_messages(amendments)
        require_actual_aliases(selected_decision)

        # Real complete size calibration is outside the measured capture
        # attempt and does not inspect or replace the nested decision fields.
        actual_shelf = owner.transcript.snapshot_for_continuation(runtime.live.llm_messages, materialization=runtime.materialization)
        size = len(canonical_json(actual_shelf.model_dump(mode="json")).encode("utf-8"))
        assert (size > JSON_MAX_TOTAL_UTF8_BYTES) is natural_excess
        encoded_objects.clear()

        if malformed_nested or natural_excess:
            with pytest.raises(Exception) as raised:
                await capture_continuation_runtime_owners(runtime, selected=prior, inventory=arguments["inventory"])
            observed = raised.value
            encoded_at_capture = tuple(encoded_objects)
            projected = project_composer_operation_error(observed, request_id="r1-nested-live")
            if malformed_nested:
                assert type(observed) is AuditIntegrityError, (
                    "a reused nested decision must retain its existing integrity category before shelf refusal; "
                    f"received {type(observed).__name__}"
                )
                assert isinstance(observed.__cause__, ValidationError)
                assert any(error["loc"] == ("reason",) and error["type"] == "string_too_short" for error in observed.__cause__.errors()), (
                    "reach the existing reason field's minimum-length validator"
                )
                assert projected.http_status == 500 and projected.error_type == "audit_integrity_error"
            else:
                assert type(observed) is ComposerContextUnsupported and observed.reason == "retained_history_bytes"
                assert projected.http_status == 422 and projected.error_type == "composer_context_unsupported"
            if natural_excess:
                assert encoded_at_capture == (), "do not serialize an excessive inner shelf to validate the nested owner"
        else:
            captured = await capture_continuation_runtime_owners(runtime, selected=prior, inventory=arguments["inventory"])
            assert captured.selected == prior and encoded_objects

        require_actual_aliases(selected_decision)
        assert full_rows(operation_store[0]) == before and len(sdk_calls) == 1
        restored = await restore_continuation_runtime(**arguments)
        assert restored.source.selected == prior and tuple(restored.live.llm_messages) == original_messages
        retained_paid = await read_continuation_primary(
            authority=arguments["authority"], selected=prior, required_work=arguments["required_work"]
        )
        assert retained_paid.response == paid.response and retained_paid.outcome.audit_call == paid.outcome.audit_call
        assert full_rows(operation_store[0]) == before and len(sdk_calls) == 1
        arguments["required_work"].coordinator.assert_completed()
    finally:
        operation_store[1].release(running.session_operation_context)
