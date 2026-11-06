"""Model P4's committed checkpoint beneath a hand-built recovery carrier.

A compose-loop crash carrier's ``partial_state`` is saved only when it
re-projects the durable head, which is the ``tool_call`` checkpoint P4 commits
and verifies. A test that raises a carrier from a composer double must
therefore commit that checkpoint first, as P4 would have. Otherwise the
recovery handler withholds the capture as unverified.
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from dataclasses import replace
from typing import Any
from uuid import UUID

from elspeth.web.composer.state import CompositionState
from elspeth.web.sessions.protocol import CompositionStateData, CompositionStateRecord


async def commit_p4_checkpoint(
    service: Any,
    session_id: UUID,
    state: CompositionState,
    *,
    session_operation_context: Any,
) -> CompositionStateRecord:
    """Commit ``state`` as the authoring-lane ``tool_call`` checkpoint P4 writes."""
    data = state.to_dict()
    return await service.save_composition_state(
        session_id,
        CompositionStateData(
            sources=data["sources"],
            nodes=data["nodes"],
            edges=data["edges"],
            outputs=data["outputs"],
            metadata_=data["metadata"],
            composer_meta={"validation_lane": "authoring_only"},
        ),
        provenance="tool_call",
        session_operation_context=session_operation_context,
    )


def raise_after_p4_checkpoint(service: Any, failure: Any) -> Callable[..., Coroutine[Any, Any, Any]]:
    """A ``compose`` side effect: commit the carrier's partial as P4 would, then raise it."""

    async def compose(*_args: Any, **kwargs: Any) -> Any:
        await commit_p4_checkpoint(
            service,
            UUID(str(kwargs["session_id"])),
            failure.partial_state,
            session_operation_context=kwargs["session_operation_context"],
        )
        raise failure

    return compose


def return_after_p4_checkpoint(service: Any, result: Any) -> Callable[..., Coroutine[Any, Any, Any]]:
    """A ``compose`` side effect: commit ``result.state`` as P4 would, then return it anchored.

    The real loop sets ``final_persisted_state_id`` to the verified checkpoint
    (``CompositionCompletion._append_checkpoint_receipt``), which route
    finalization requires before it re-projects a moved head.
    """

    async def compose(*_args: Any, **kwargs: Any) -> Any:
        record = await commit_p4_checkpoint(
            service,
            UUID(str(kwargs["session_id"])),
            result.state,
            session_operation_context=kwargs["session_operation_context"],
        )
        return replace(result, final_persisted_state_id=record.id)

    return compose
