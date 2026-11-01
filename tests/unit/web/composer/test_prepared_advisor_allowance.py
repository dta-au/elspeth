"""Natural cap refusal at manual/checkpoint owners preserves prior attempts.

The full manual loop uses the established independent END stub. The EARLY/END
controls invoke the captured real checkpoint method, real preparation and real
SQL accounting, replacing only SDK transport and observing current inputs.
"""

from __future__ import annotations

from dataclasses import replace

import litellm
import pytest
import rfc8785
from litellm.exceptions import ServiceUnavailableError
from sqlalchemy import select

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer import advisor_checkpoint, advisor_context
from elspeth.web.composer.advisor_checkpoint import AdvisorCheckpointOwner
from elspeth.web.composer.audit import BufferingRecorder
from elspeth.web.composer.prompt_guard import prompt_guard_user_scope
from elspeth.web.composer.protocol import ComposerAdmissionRefused, ComposerContextUnsupported
from elspeth.web.composer.provider_context import capture_provider_context
from elspeth.web.composer.state import PipelineMetadata
from elspeth.web.credential_guard import CredentialMaterialRefused
from elspeth.web.sessions.models import chat_messages_table, quota_provider_attempts_table
from tests.unit.web.composer import _helpers as composer_helpers
from tests.unit.web.composer import test_advisor_checkpoint as checkpoint_fixtures
from tests.unit.web.composer import test_advisor_tool as advisor_fixtures

_stub_advisor_end_gate_clean = composer_helpers._stub_advisor_end_gate_clean
_composer_available_by_default = advisor_fixtures._composer_available_by_default
make_service = checkpoint_fixtures.make_service
simple_state = checkpoint_fixtures.simple_state
physical_advisor = checkpoint_fixtures.physical_advisor
_REAL_CHECKPOINT = AdvisorCheckpointOwner._run_advisor_checkpoint
_USER = "Review the ordinary declared inputs."


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["early", "end"])
@pytest.mark.parametrize("admission", ["context", "credential", "native", "integrity", "serialization"])
async def test_actual_checkpoint_transport_retry_reprepares_and_preparation_exit_keeps_first_call(
    make_service, simple_state, physical_advisor, monkeypatch, phase, admission
):
    service = make_service()
    state = replace(simple_state, metadata=PipelineMetadata(description="Complete ordinary graph description. " * 100))
    fenced = physical_advisor.bind(service, review_state=state, request=_USER)
    if phase == "early":
        fenced.pop("publication_review")
    materialize = advisor_context.materialize_advisor_complete_values
    source_reads, limits, packets, physical = [], [], [], []
    prepare = advisor_checkpoint.prepare_provider_input
    originals = []

    def observe_source(*arguments, **keywords):
        values = materialize(*arguments, **keywords)
        source_reads.append(values)
        return values

    def observe_prepare(**arguments):
        limits.append(arguments["max_bytes"])
        if len(limits) == 2 and admission != "context":
            if admission == "serialization":
                source = arguments["messages"]
                arguments["messages"] = [{**source[0], "controlled_number": 2**53}, *source[1:]]
            else:
                if admission == "credential":
                    tail = {
                        "role": "assistant",
                        "content": "",
                        "thinking_blocks": [{"type": "redacted_thinking", "data": "https://fixture-user:fixture-pass@example.test"}],
                    }
                elif admission == "native":
                    tail = {"role": "assistant", "content": "", "thinking_blocks": [{"type": "redacted_thinking", "data": ""}]}
                else:
                    tail = {"role": "user", "content": "", "thinking_blocks": [{"type": "redacted_thinking", "data": "opaque"}]}
                arguments["messages"] = [*arguments["messages"], tail]
        try:
            packet = prepare(**arguments)
        except (ComposerAdmissionRefused, AuditIntegrityError, ValueError) as original:
            originals.append(original)
            raise
        packets.append(packet)
        return packet

    monkeypatch.setattr(advisor_context, "materialize_advisor_complete_values", observe_source)
    monkeypatch.setattr(advisor_checkpoint, "prepare_provider_input", observe_prepare)

    async def fail_first_physical(**kwargs):
        physical.append(kwargs)
        assert len(physical) == 1, "second preparation must refuse before physical SDK entry"
        assert "prepared_input" not in kwargs and "prepared_advisor_content" not in kwargs
        current = service._advisor_checkpoint._settings.model_copy(update={"composer_max_prepared_request_bytes": 1})
        service._advisor_checkpoint._settings = current
        raise ServiceUnavailableError("PRIVATE controlled retryable outage", model=kwargs["model"], llm_provider="openai")

    monkeypatch.setattr(litellm, "acompletion", fail_first_physical)
    recorder = BufferingRecorder()
    expected = {
        "context": ComposerContextUnsupported,
        "credential": CredentialMaterialRefused,
        "native": ComposerAdmissionRefused,
        "integrity": AuditIntegrityError,
        "serialization": rfc8785.IntegerDomainError,
    }[admission]
    with prompt_guard_user_scope(_USER), pytest.raises(expected) as caught:
        await _REAL_CHECKPOINT(service._advisor_checkpoint, phase=phase, state=state, recorder=recorder, **fenced)
    assert len(originals) == 1
    if admission == "context":
        assert caught.value.reason == "prepared_request_bytes"
    else:
        assert caught.value is originals[0]
    assert len(source_reads) == len(limits) == 2 and all(source_reads)
    assert limits[0] == 2 * 1024 * 1024 and limits[1] == 1
    assert len(packets) == len(physical) == len(recorder.llm_calls) == 1
    call = recorder.llm_calls[0]
    assert caught.value.llm_calls == (call,) and call.status.name == "API_ERROR"
    assert call.private_request == capture_provider_context(messages=physical[0]["messages"], tools=None)
    assert packets[0].evidence.messages == physical[0]["messages"]
    with physical_advisor.engine.connect() as connection:
        attempts = connection.execute(select(quota_provider_attempts_table)).all()
        audit = connection.execute(select(chat_messages_table.c.tool_calls).where(chat_messages_table.c.role == "audit")).all()
    assert len(attempts) == 1 and attempts[0].attempt_id == call.call_id and attempts[0].settled_at is not None
    kinds = [envelope["_kind"] for row in audit for envelope in row.tool_calls or ()]
    assert kinds.count("prompt_guard_audit") == 1
    assert "advisor_checkpoint_pass_audit" not in kinds and "advisor_terminal_publication_audit" not in kinds
