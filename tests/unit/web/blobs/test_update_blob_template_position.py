"""update_blob over a file bound at a prompt template position: refused, saying why, and the file keeps its text.

The size check the prompt-file save route makes needs no twin in update_blob:
a file bound anywhere in the current composition is refused outright
(``blob_bound_update``) before its content is looked at, so text over the
template bound can never reach a template position this way. This pins that,
for oversized text at each template position. It passes on the tree before the
change by design.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from uuid import UUID

import pytest

from elspeth.web.sessions.converters import state_from_record
from tests.helpers.session_fences import fenced_operation_context
from tests.unit.web.blobs.test_prompt_editor import _live_prompt_setup

LIMIT = 64 * 1024


@pytest.mark.parametrize("field_path", ["prompt_template", "queries.q.template", "queries.0.template"])
def test_oversized_text_for_a_file_bound_at_a_template_position_is_refused_and_the_file_stands(tmp_path: Path, field_path: str) -> None:
    from tests.unit.web.composer.test_blob_inline_tools import _catalog, execute_tool

    client, service, blob_service, session_id, first, _second, _events = _live_prompt_setup(tmp_path, field_path=field_path)
    record = asyncio.run(service.get_current_state(UUID(session_id)))
    assert record is not None
    oversized = "Rate {{ row.text }}. " + "x" * LIMIT

    with fenced_operation_context(blob_service._engine, session_id) as operation:
        refused = execute_tool(
            "update_blob",
            {"blob_id": first["id"], "content": oversized},
            state_from_record(record),
            _catalog(),
            data_dir=str(tmp_path),
            session_engine=blob_service._engine,
            session_id=session_id,
            session_operation_context=operation,
            session_operation_authority=service.session_operation_authority,
            user_message_id="agent-message",
            # The user pasted the text: the write is verbatim, not model-authored.
            user_message_content=oversized,
        )

    assert refused.success is False
    error = refused.validation.errors[0]
    assert error.error_code == "blob_bound_update"
    assert error.message == f"Blob '{first['id']}' is referenced by the current composition and cannot be updated; unbind it first."
    assert client.get(f"/api/sessions/{session_id}/blobs/{first['id']}/content").content == b"first before"
