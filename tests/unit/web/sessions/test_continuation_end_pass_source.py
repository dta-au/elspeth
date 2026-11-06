"""The accepted END pass locator replays a real model audit row."""

import pytest
from sqlalchemy import select

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.continuation import ComposeFrame
from elspeth.web.composer.continuation_checkpoint_end_terminal import EndCheckpointCompleteReview
from elspeth.web.composer.continuation_selection import load_selected_continuation
from elspeth.web.sessions.continuation_end_pass_source import read_original_accepted_end_pass_on_connection
from elspeth.web.sessions.models import chat_messages_table
from tests.unit.web.sessions import test_continuation_end_shared_execution as shared_fixture

operation_store = shared_fixture.operation_store


@pytest.mark.asyncio
async def test_original_accepted_end_pass_reloads_actual_model_row_and_refuses_foreign_locator(operation_store, tmp_path, monkeypatch):
    async with shared_fixture.actual_shared_end_case(operation_store, tmp_path, monkeypatch, outcome="clean") as case:
        execution = case["execution"]
        assert type(execution) is EndCheckpointCompleteReview
        review = execution.review
        binding = review.publication_binding
        assert type(binding) is str
        selected = await load_selected_continuation(
            authority=case["phases"].authority,
            running=case["running"],
            selected=review.completed,
            required_work=case["work"],
        )
        current = next(frame for frame in selected.stack.frames if type(frame) is ComposeFrame)
        index = current.advisor_checkpoint_passes
        assert index > case["passes_before"]
        session_id = case["running"].claim.session_id
        with operation_store[0].connect() as conn:
            row = conn.execute(
                select(chat_messages_table.c.id).where(
                    chat_messages_table.c.session_id == str(session_id),
                    chat_messages_table.c.role == "audit",
                    chat_messages_table.c.writer_principal == "compose_loop",
                    chat_messages_table.c.tool_calls[0]["pass"]["publication_binding"].as_string() == binding,
                    chat_messages_table.c.tool_calls[0]["pass"]["pass_index"].as_integer() == index,
                )
            ).one()
            proved = read_original_accepted_end_pass_on_connection(
                conn,
                session_id=session_id,
                message_id=row.id,
                pass_index=index,
                publication_binding=binding,
            )
            assert proved.message_id == row.id
            assert proved.record.pass_index == index
            with pytest.raises(AuditIntegrityError):
                read_original_accepted_end_pass_on_connection(
                    conn,
                    session_id=session_id,
                    message_id=row.id,
                    pass_index=index + 1,
                    publication_binding=binding,
                )
            with pytest.raises(AuditIntegrityError):
                read_original_accepted_end_pass_on_connection(
                    conn,
                    session_id=session_id,
                    message_id="foreign-model-pass",
                    pass_index=index,
                    publication_binding=binding,
                )
