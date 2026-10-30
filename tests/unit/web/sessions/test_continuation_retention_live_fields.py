"""Other complete live field checks retain precedence at shelf saturation.

These controls keep selected storage valid and vary only current owned fields;
they supplement the measured retained-history regression rather than replace it.
"""

from dataclasses import replace

import pytest
from pydantic import ValidationError

from elspeth.contracts.hashing import canonical_json
from elspeth.web.composer import advisor_transcript
from elspeth.web.composer.bounded_json import JSON_MAX_TOTAL_UTF8_BYTES
from elspeth.web.composer.continuation_runtime import capture_continuation_runtime_owners, restore_continuation_runtime
from elspeth.web.credential_guard import CredentialMaterialRefused
from tests.unit.web.coordination import test_composer_operation_authority as operations
from tests.unit.web.sessions.test_continuation_runtime_provider_history import full_rows, prepared_runtime

operation_store = operations.operation_store


@pytest.mark.asyncio
@pytest.mark.parametrize("natural_excess", (False, True), ids=("fitting", "natural-excess"))
@pytest.mark.parametrize("live_fault", ("registration_revision", "authored_graph_hash", "metadata_credential"))
async def test_complete_live_field_validation_precedes_shelf_resource_refusal(
    operation_store, tmp_path, monkeypatch, natural_excess, live_fault
):
    arguments, runtime, sdk_calls = await prepared_runtime(operation_store, tmp_path, monkeypatch)
    running = arguments["inventory"].running
    try:
        actual_encoder = advisor_transcript.encode_transcript_object_json
        encoded_objects = []

        def encode(value):
            encoded_objects.append(value)
            return actual_encoder(value)

        monkeypatch.setattr(advisor_transcript, "encode_transcript_object_json", encode)
        fitting = await capture_continuation_runtime_owners(runtime, selected=arguments["selected"], inventory=arguments["inventory"])
        assert fitting.selected == arguments["selected"] and encoded_objects
        owner = runtime.live.turn_evidence
        if natural_excess:
            amendments = [{"role": "user", "content": f"Amendment {number}: " + 'é\\"\n' * 24_000} for number in range(3)]
            assert all(len(canonical_json(message).encode("utf-8")) < 262_144 for message in amendments)
            runtime.live.llm_messages.extend(amendments)
            owner.transcript.observe_messages(amendments)
        actual_shelf = owner.transcript.snapshot_for_continuation(runtime.live.llm_messages, materialization=runtime.materialization)
        size = len(canonical_json(actual_shelf.model_dump(mode="json")).encode("utf-8"))
        assert (size > JSON_MAX_TOTAL_UTF8_BYTES) is natural_excess
        encoded_objects.clear()
        before = full_rows(operation_store[0])

        if live_fault == "registration_revision":
            owner._registration_revision = -1
            expected = "registration_revision"
        elif live_fault == "authored_graph_hash":
            owner.effects = replace(owner.effects, initial_authored_state_hash="0" * 64)
            expected = "evidence altered its actual authored graph hashes"
        else:
            owner._abandonment_transcript_binding = "sk-" + "syntheticcredential" * 3
            expected = "composer_continuation_evidence_metadata"

        if live_fault == "metadata_credential":
            with pytest.raises(CredentialMaterialRefused) as raised:
                await capture_continuation_runtime_owners(runtime, selected=arguments["selected"], inventory=arguments["inventory"])
            assert raised.value.surface == expected and raised.value.finding.category == "token_shape"
        else:
            with pytest.raises(ValidationError, match=expected):
                await capture_continuation_runtime_owners(runtime, selected=arguments["selected"], inventory=arguments["inventory"])
        if natural_excess:
            assert encoded_objects == [], "validate current fields without materializing the excessive shelf"
        assert full_rows(operation_store[0]) == before and sdk_calls == []
        restored = await restore_continuation_runtime(**arguments)
        assert restored.source.selected == arguments["selected"]
        assert restored.live.turn_evidence._registration_revision == runtime.source.evidence.registration_revision
        assert (
            restored.live.turn_evidence.effects.initial_authored_state_hash == runtime.source.evidence.effects.initial_authored_state_hash
        )
        assert restored.live.turn_evidence._abandonment_transcript_binding == runtime.source.evidence.abandonment_transcript_binding
        assert full_rows(operation_store[0]) == before and sdk_calls == []
        arguments["required_work"].coordinator.assert_completed()
    finally:
        operation_store[1].release(running.session_operation_context)
