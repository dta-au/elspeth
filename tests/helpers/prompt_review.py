"""Resolve interpretation events the way the review card does.

A prompt review card (``llm_prompt_template``) fetches the event's stored
review snapshot and approves by echoing that snapshot's ``anchor`` as
``reviewed_anchor``. Every other kind resolves with no anchor. These helpers
read the snapshot through the same route or service method the card uses,
so a test that resolves a prompt card still proves the snapshot exists and
is the one approved.
"""

from __future__ import annotations

from typing import Any, Protocol
from uuid import UUID

from elspeth.contracts.composer_interpretation import InterpretationKind


class _SnapshotReader(Protocol):
    async def get_prompt_review_snapshot(self, session_id: UUID, event_id: UUID) -> Any: ...


class _SyncGetClient(Protocol):
    def get(self, url: str) -> Any: ...


async def reviewed_anchor_for(service: _SnapshotReader, session_id: UUID, event_id: UUID) -> str:
    """The stored snapshot anchor a prompt card for ``event_id`` displays."""
    snapshot = await service.get_prompt_review_snapshot(session_id, event_id)
    assert snapshot is not None, f"prompt review {event_id} has no stored snapshot"
    anchor = snapshot["anchor"]
    assert type(anchor) is str
    return anchor


async def reviewed_anchor_kwargs(service: _SnapshotReader, session_id: UUID, event_id: UUID, kind: InterpretationKind) -> dict[str, str]:
    """``reviewed_anchor=`` for a prompt review, nothing for any other kind."""
    if kind is not InterpretationKind.LLM_PROMPT_TEMPLATE:
        return {}
    return {"reviewed_anchor": await reviewed_anchor_for(service, session_id, event_id)}


def accept_body(client: _SyncGetClient, session_id: object, event: Any) -> dict[str, str]:
    """The accept-as-drafted body a card posts, read through ``GET .../prompt``."""
    body = {"choice": "accepted_as_drafted"}
    if event.kind is InterpretationKind.LLM_PROMPT_TEMPLATE:
        response = client.get(f"/api/sessions/{session_id}/interpretations/{event.id}/prompt")
        assert response.status_code == 200, response.text
        body["reviewed_anchor"] = response.json()["anchor"]
    return body
