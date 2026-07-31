"""Session-scoped composer progress snapshots.

The progress surface is a UI status channel, not a reasoning transcript.
Snapshots summarize visible composer lifecycle boundaries and tool categories;
they must never carry raw tool arguments, tool results, secrets, or provider
chain-of-thought.

The L0-suitable progress contracts (``ComposerProgressEvent``,
``ComposerProgressPhase``, ``ComposerProgressReason``,
``ComposerProgressSink``, ``COMPOSER_PROGRESS_MAX_EVIDENCE``,
``NON_TERMINAL_PROGRESS_PHASES``) live in
``elspeth.contracts.composer_progress``.  This module owns only the
L3-dependent residue: the durable ``ComposerProgressRegistry``, the snapshot
subclass that joins event with session identity, and the per-phase event
factory functions whose copy references tool names from
``elspeth.web.composer.tools``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal

from sqlalchemy import Connection, Engine, func, select
from sqlalchemy.sql.selectable import ScalarSelect

from elspeth.contracts.composer_progress import (
    ComposerProgressEvent,
    ComposerProgressSink,
)
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.session_operation import SessionOperationContext, SessionOperationKind
from elspeth.web.composer.tools import is_discovery_tool
from elspeth.web.sessions.models import (
    composer_inflight_requests_table,
    composer_progress_snapshots_table,
    session_operation_fences_table,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from elspeth.web.sessions.protocol import SessionOperationAuthority

__all__ = [
    "ComposerProgressRegistry",
    "ComposerProgressSnapshot",
    "advisor_checkpoint_progress_event",
    "client_cancelled_progress_event",
    "convergence_progress_event",
    "emit_progress",
    "model_call_progress_event",
    "tool_batch_progress_event",
    "tool_completed_progress_event",
    "tool_started_progress_event",
]


class ComposerProgressSnapshot(ComposerProgressEvent):
    """Latest composer progress snapshot for one session."""

    session_id: str
    request_id: str | None
    updated_at: datetime
    # Live count of compose requests (send_message / recompose) that have
    # acquired the exact COMPOSE operation and durably started a request.
    # Enriched at read time by the registry (see get_latest); the SPA's
    # post-abort reconciliation treats zero as its only settlement signal
    # (elspeth-06a23adfcc) because the phase alone cannot distinguish an
    # aborted-but-still-running request from full quiescence.
    inflight_requests: int = 0


class ComposerProgressRegistry:
    """Durable latest-value register for exactly fenced composer progress.

    Writes are capabilities of the owning COMPOSE operation transaction. The
    registry never opens an independent write transaction and keeps no local
    correctness cache. Reads may use any engine connected to the Sessions
    database and reconstruct liveness by joining an incomplete request to its
    exact current, live COMPOSE fence.
    """

    def __init__(
        self,
        engine: Engine,
        session_operation_authority: SessionOperationAuthority,
        notify_committed: Callable[[ComposerProgressSnapshot], Awaitable[None]] | None = None,
    ) -> None:
        self._engine = engine
        self._session_operation_authority = session_operation_authority
        self._notify_committed = notify_committed

    @staticmethod
    def _validate_write(
        session_operation_context: SessionOperationContext,
        *,
        request_id: str,
        user_id: str,
        event: ComposerProgressEvent | None,
    ) -> None:
        if type(session_operation_context) is not SessionOperationContext:
            raise TypeError("session_operation_context must be an exact SessionOperationContext")
        if session_operation_context.operation_kind is not SessionOperationKind.COMPOSE:
            raise AuditIntegrityError("composer progress writes require COMPOSE authority")
        if type(request_id) is not str or not request_id.strip():
            raise ValueError("request_id must be a nonblank exact string")
        if type(user_id) is not str or not user_id.strip():
            raise ValueError("user_id must be a nonblank exact string")
        if event is not None and type(event) is not ComposerProgressEvent:
            raise TypeError("event must be an exact ComposerProgressEvent")

    @staticmethod
    def _write_timestamp(result: object) -> datetime:
        if type(result) is not datetime:
            raise RuntimeError("composer progress mutation did not return its durable timestamp")
        return _ensure_utc(result)

    @staticmethod
    def _snapshot(
        context: SessionOperationContext,
        *,
        request_id: str,
        event: ComposerProgressEvent,
        updated_at: datetime,
        inflight_requests: int,
    ) -> ComposerProgressSnapshot:
        return ComposerProgressSnapshot(
            session_id=context.fence.session_id,
            request_id=request_id,
            phase=event.phase,
            headline=event.headline,
            evidence=event.evidence,
            likely_next=event.likely_next,
            reason=event.reason,
            updated_at=updated_at,
            inflight_requests=inflight_requests,
        )

    async def _notify(self, snapshot: ComposerProgressSnapshot) -> None:
        if self._notify_committed is not None:
            await self._notify_committed(snapshot)

    async def start_request(
        self,
        *,
        session_operation_context: SessionOperationContext,
        request_id: str,
        user_id: str,
        event: ComposerProgressEvent,
    ) -> ComposerProgressSnapshot:
        """Atomically create exact-fence liveness and the first snapshot."""
        self._validate_write(
            session_operation_context,
            request_id=request_id,
            user_id=user_id,
            event=event,
        )

        def mutate(transaction: Any) -> object:
            return transaction.composer_progress.start_request(
                request_id=request_id,
                user_id=user_id,
                event=event,
            )

        updated_at = self._write_timestamp(self._session_operation_authority.mutate(session_operation_context, mutate))
        snapshot = self._snapshot(
            session_operation_context,
            request_id=request_id,
            event=event,
            updated_at=updated_at,
            inflight_requests=1,
        )
        await self._notify(snapshot)
        return snapshot

    async def publish(
        self,
        *,
        session_operation_context: SessionOperationContext,
        request_id: str,
        user_id: str,
        event: ComposerProgressEvent,
    ) -> ComposerProgressSnapshot:
        """Publish one latest snapshot under the request's exact live fence."""
        self._validate_write(
            session_operation_context,
            request_id=request_id,
            user_id=user_id,
            event=event,
        )

        def mutate(transaction: Any) -> object:
            return transaction.composer_progress.publish_progress(
                request_id=request_id,
                user_id=user_id,
                event=event,
            )

        updated_at = self._write_timestamp(self._session_operation_authority.mutate(session_operation_context, mutate))
        snapshot = self._snapshot(
            session_operation_context,
            request_id=request_id,
            event=event,
            updated_at=updated_at,
            inflight_requests=1,
        )
        await self._notify(snapshot)
        return snapshot

    async def finish_request(
        self,
        *,
        session_operation_context: SessionOperationContext,
        request_id: str,
        user_id: str,
        terminal_event: ComposerProgressEvent | None = None,
    ) -> ComposerProgressSnapshot:
        """Atomically terminalize request liveness and optionally its snapshot."""
        self._validate_write(
            session_operation_context,
            request_id=request_id,
            user_id=user_id,
            event=terminal_event,
        )

        def mutate(transaction: Any) -> object:
            return transaction.composer_progress.finish_request(
                request_id=request_id,
                user_id=user_id,
                terminal_event=terminal_event,
            )

        updated_at = self._write_timestamp(self._session_operation_authority.mutate(session_operation_context, mutate))
        if terminal_event is None:
            snapshot = await self.get_latest(session_operation_context.fence.session_id)
            if snapshot.request_id != request_id:
                raise AuditIntegrityError("finished composer request is not the durable latest snapshot")
            snapshot = snapshot.model_copy(update={"updated_at": updated_at, "inflight_requests": 0})
        else:
            snapshot = self._snapshot(
                session_operation_context,
                request_id=request_id,
                event=terminal_event,
                updated_at=updated_at,
                inflight_requests=0,
            )
        await self._notify(snapshot)
        return snapshot

    async def get_latest(self, session_id: str) -> ComposerProgressSnapshot:
        """Return the durable latest snapshot with exact-fence liveness."""
        if type(session_id) is not str or not session_id.strip():
            raise ValueError("session_id must be a nonblank exact string")
        with self._engine.connect() as connection:
            inflight = _live_request_count_expression(connection)
            row = (
                connection.execute(
                    select(
                        composer_progress_snapshots_table,
                        inflight.label("inflight_requests"),
                    ).where(composer_progress_snapshots_table.c.session_id == session_id)
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                return _idle_snapshot(session_id)
        return _snapshot_from_row(row, inflight_requests=int(row["inflight_requests"]))

    async def list_active(self, *, user_id: str) -> tuple[ComposerProgressSnapshot, ...]:
        """Return durable snapshots with a currently live exact request."""
        if type(user_id) is not str or not user_id.strip():
            raise ValueError("user_id must be a nonblank exact string")
        with self._engine.connect() as connection:
            inflight = _live_request_count_expression(connection)
            rows = (
                connection.execute(
                    select(
                        composer_progress_snapshots_table,
                        inflight.label("inflight_requests"),
                    ).where(
                        composer_progress_snapshots_table.c.user_id == user_id,
                        inflight > 0,
                    )
                )
                .mappings()
                .all()
            )
        active = [_snapshot_from_row(row, inflight_requests=int(row["inflight_requests"])) for row in rows]
        return tuple(sorted(active, key=lambda snapshot: snapshot.updated_at))


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _live_request_count_expression(connection: Connection) -> ScalarSelect[int]:
    if connection.dialect.name not in {"postgresql", "sqlite"}:
        raise NotImplementedError(f"composer progress database time is not implemented for {connection.dialect.name}")
    database_now = func.current_timestamp()
    return (
        select(func.count())
        .select_from(
            composer_inflight_requests_table.join(
                session_operation_fences_table,
                composer_inflight_requests_table.c.session_id == session_operation_fences_table.c.session_id,
            )
        )
        .where(
            composer_inflight_requests_table.c.session_id == composer_progress_snapshots_table.c.session_id,
            composer_inflight_requests_table.c.request_id == composer_progress_snapshots_table.c.request_id,
            composer_inflight_requests_table.c.user_id == composer_progress_snapshots_table.c.user_id,
            composer_inflight_requests_table.c.completed_at.is_(None),
            composer_inflight_requests_table.c.expires_at > database_now,
            composer_inflight_requests_table.c.operation_id == composer_progress_snapshots_table.c.operation_id,
            composer_inflight_requests_table.c.operation_epoch == composer_progress_snapshots_table.c.operation_epoch,
            composer_inflight_requests_table.c.operation_id == session_operation_fences_table.c.operation_id,
            composer_inflight_requests_table.c.operation_epoch == session_operation_fences_table.c.operation_epoch,
            session_operation_fences_table.c.operation_kind == SessionOperationKind.COMPOSE.value,
            session_operation_fences_table.c.released_at.is_(None),
            session_operation_fences_table.c.lease_expires_at > database_now,
        )
        .correlate(composer_progress_snapshots_table)
        .scalar_subquery()
    )


def _snapshot_from_row(row: Any, *, inflight_requests: int) -> ComposerProgressSnapshot:
    evidence = row["evidence"]
    if type(evidence) is not list:
        raise AuditIntegrityError("durable composer progress evidence is malformed")
    return ComposerProgressSnapshot(
        session_id=str(row["session_id"]),
        request_id=str(row["request_id"]) if row["request_id"] is not None else None,
        phase=row["phase"],
        headline=row["headline"],
        evidence=tuple(evidence),
        likely_next=row["likely_next"],
        reason=row["reason"],
        updated_at=_ensure_utc(row["updated_at"]),
        inflight_requests=inflight_requests,
    )


def convergence_progress_event(
    *,
    budget_exhausted: Literal["composition", "discovery", "timeout"],
) -> ComposerProgressEvent:
    """Map a convergence budget discriminator to a discriminated progress event.

    Three sub-causes (composition turn budget, discovery turn budget, wall-clock
    timeout) used to collapse into one generic ``phase: failed`` event because
    only ``ComposerConvergenceError.budget_exhausted`` carried the discriminator
    and the emit sites discarded it. This helper is the single dispatch point
    — both the service-level catch (compose() outer try/except) and the
    route-handler catches in web/sessions/routes.py route through it so the
    per-cause headline / evidence / likely_next / reason copy is defined
    exactly once.

    Lives in this module rather than service.py because:

    - the failure-mode taxonomy is a property of the progress contract, not
      the service implementation;
    - taking a string discriminator (not the exception) avoids importing
      ``ComposerConvergenceError`` from ``composer.protocol``, which already
      imports ``ComposerProgressSink`` from contracts — keeping the helper
      here would otherwise create a circular import.

    Recovery copy is what the user can act on:

    - composition budget → split the request into smaller turns
    - discovery budget   → narrow the schema/catalog exploration
    - wall-clock timeout → retry, or ask an operator to raise the server budget
    """
    if budget_exhausted == "timeout":
        return ComposerProgressEvent(
            phase="failed",
            headline="The composer timed out before producing a final answer.",
            evidence=("The composer wall-clock budget elapsed during this request.",),
            likely_next=("Retry once the provider responds faster, or ask an operator to raise the composer wall-clock budget."),
            reason="convergence_wall_clock_timeout",
        )
    if budget_exhausted == "discovery":
        return ComposerProgressEvent(
            phase="failed",
            headline="The composer used its discovery turn budget without finishing.",
            evidence=("The discovery-turn budget was exhausted before a final answer.",),
            likely_next=("Narrow the schema or catalog exploration, or ask an operator to raise the discovery-turn budget."),
            reason="convergence_discovery_budget",
        )
    return ComposerProgressEvent(
        phase="failed",
        headline="The composer used its mutation turn budget without finishing.",
        evidence=("The mutation-turn budget was exhausted before a final answer.",),
        likely_next=("Split the request into smaller turns, or ask an operator to raise the mutation-turn budget."),
        reason="convergence_composition_budget",
    )


def client_cancelled_progress_event() -> ComposerProgressEvent:
    """Build the progress event for a client-disconnect cancellation.

    Centralised here for the same reason ``convergence_progress_event`` is —
    the failure-mode taxonomy is a property of the progress contract. Both
    composer entry points (send_message and recompose) emit this exact text
    on ``asyncio.CancelledError`` so an operator inspecting the snapshot
    cannot tell which route was cancelled from the headline alone (the
    request_id on the snapshot disambiguates if needed). Recovery copy is
    written for the user, not the operator: the user sees this through
    /composer-progress polling if they ever reconnect to the session.
    """
    return ComposerProgressEvent(
        phase="cancelled",
        headline="The request was cancelled before the composer finished.",
        evidence=("The client closed the connection before a response was returned.",),
        likely_next="Resubmit the message when ready, or try a smaller request.",
        reason="client_cancelled",
    )


def _idle_snapshot(session_id: str) -> ComposerProgressSnapshot:
    return ComposerProgressSnapshot(
        session_id=session_id,
        request_id=None,
        phase="idle",
        headline="No active composer work.",
        evidence=(),
        likely_next=None,
        reason="composer_idle",
        updated_at=datetime.now(UTC),
    )


# ---------------------------------------------------------------------------
# Progress event factories — co-located with ComposerProgressEvent so the
# per-phase headline / evidence / likely_next copy lives in one place.
# ---------------------------------------------------------------------------


async def emit_progress(
    progress: ComposerProgressSink | None,
    event: ComposerProgressEvent,
) -> None:
    """Emit provider-safe progress when a sink is available."""
    if progress is None:
        return
    await progress(event)


def model_call_progress_event(message: str) -> ComposerProgressEvent:
    headline = "I'm asking the model to choose the next safe pipeline update."
    normalized = message.lower()
    if "html" in normalized and "json" in normalized:
        headline = "I'm asking the model to choose an HTML input and JSON output."
    return ComposerProgressEvent(
        phase="calling_model",
        headline=headline,
        evidence=("The composer is using the prepared prompt and visible pipeline state.",),
        likely_next="The model may answer directly or request safe pipeline tools.",
    )


def advisor_checkpoint_progress_event(checkpoint: str) -> ComposerProgressEvent:
    """Progress for a deterministic advisor (model-distinct reviewer) checkpoint.

    Emitted so the advisor call is visible like every other model call —
    otherwise the snapshot stays frozen on its previous phase while the
    (slower, frontier) advisor model runs, which is indistinguishable from a
    stall to a poller or a watching user. ``checkpoint`` is "early" (plan
    review) or "end" (sign-off).
    """
    if checkpoint == "early":
        headline = "I'm asking the advisor model to review the plan."
        likely_next = "The advisor may suggest changes before the composer continues."
    else:
        headline = "I'm asking the advisor model to sign off on the pipeline."
        likely_next = "The advisor may approve the pipeline or flag changes before finalizing."
    return ComposerProgressEvent(
        phase="calling_model",
        headline=headline,
        evidence=("A second, model-distinct advisor is reviewing the pipeline.",),
        likely_next=likely_next,
    )


def tool_batch_progress_event(tool_names: tuple[str, ...]) -> ComposerProgressEvent:
    if any(_is_schema_or_catalog_tool(name) for name in tool_names):
        return ComposerProgressEvent(
            phase="using_tools",
            headline="The model requested plugin schemas.",
            evidence=("Checking available source, transform, and sink tools.",),
            likely_next="ELSPETH will use visible schemas to guide the pipeline shape.",
        )
    if any(name in {"get_pipeline_state", "preview_pipeline", "diff_pipeline"} for name in tool_names):
        return ComposerProgressEvent(
            phase="using_tools",
            headline="The model is checking the current pipeline.",
            evidence=("Reading the visible pipeline graph and validation summary.",),
            likely_next="ELSPETH will compare the request with the current setup.",
        )
    if any(_is_secret_tool(name) for name in tool_names):
        return ComposerProgressEvent(
            phase="using_tools",
            headline="The model is checking available secret references.",
            evidence=("Checking available secret references without reading secret values.",),
            likely_next="ELSPETH will keep any credential references deferred.",
        )
    if any(not is_discovery_tool(name) for name in tool_names):
        return ComposerProgressEvent(
            phase="using_tools",
            headline="The model is updating the pipeline graph.",
            evidence=("A pipeline-editing tool was requested.",),
            likely_next="ELSPETH will validate the result before saving it.",
        )
    return ComposerProgressEvent(
        phase="using_tools",
        headline="The model requested composer tool information.",
        evidence=("Checking visible composer tool results.",),
        likely_next="ELSPETH will continue from the tool response.",
    )


def tool_started_progress_event(tool_name: str) -> ComposerProgressEvent:
    if _is_schema_or_catalog_tool(tool_name):
        return ComposerProgressEvent(
            phase="using_tools",
            headline="I'm checking available source, transform, and sink tools.",
            evidence=("Reading plugin names and schemas only.",),
            likely_next="ELSPETH will choose compatible pipeline components.",
        )
    if _is_secret_tool(tool_name):
        return ComposerProgressEvent(
            phase="using_tools",
            headline="I'm checking available secret references.",
            evidence=("Secret names can be checked; secret values stay hidden.",),
            likely_next="ELSPETH will wire only deferred secret references if needed.",
        )
    if is_discovery_tool(tool_name):
        return ComposerProgressEvent(
            phase="using_tools",
            headline="I'm checking the current pipeline and tool context.",
            evidence=("Reading visible composer state.",),
            likely_next="ELSPETH will use the result to decide the next action.",
        )
    return ComposerProgressEvent(
        phase="using_tools",
        headline="I'm updating the pipeline graph.",
        evidence=("A pipeline-editing tool is running.",),
        likely_next="ELSPETH will validate the updated pipeline.",
    )


def tool_completed_progress_event(tool_name: str, success: bool) -> ComposerProgressEvent:
    if not success:
        return ComposerProgressEvent(
            phase="using_tools",
            headline="A composer tool reported a visible blocker.",
            evidence=("The tool result was returned without exposing raw request values.",),
            likely_next="ELSPETH will ask the model to adjust the pipeline request.",
        )
    if is_discovery_tool(tool_name):
        return ComposerProgressEvent(
            phase="using_tools",
            headline="The requested tool information is ready.",
            evidence=(_safe_tool_evidence(tool_name),),
            likely_next="ELSPETH will continue with the visible result.",
        )
    return ComposerProgressEvent(
        phase="validating",
        headline="The composer has updated the pipeline and is validating the result.",
        evidence=("A pipeline-editing tool completed successfully.",),
        likely_next="ELSPETH will save the updated pipeline if it is accepted.",
    )


def _is_schema_or_catalog_tool(tool_name: str) -> bool:
    return tool_name in {
        "list_sources",
        "list_transforms",
        "list_sinks",
        "get_plugin_schema",
        "list_models",
    }


def _is_secret_tool(tool_name: str) -> bool:
    return tool_name in {"list_secret_refs", "validate_secret_ref", "wire_secret_ref"}


def _safe_tool_evidence(tool_name: str) -> str:
    if _is_schema_or_catalog_tool(tool_name):
        return "Checking available source, transform, and sink tools."
    if _is_secret_tool(tool_name):
        return "Checking available secret references without reading secret values."
    if tool_name in {"get_pipeline_state", "preview_pipeline", "diff_pipeline"}:
        return "Reading the visible pipeline graph and validation summary."
    return "Using visible composer tool output."
