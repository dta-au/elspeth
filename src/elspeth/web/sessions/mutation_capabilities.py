"""Transaction-scoped composer mutation capabilities."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import datetime
from typing import TYPE_CHECKING, Any, final
from uuid import UUID

from sqlalchemy import Connection, desc, insert, select, update

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.freeze import deep_thaw
from elspeth.contracts.hashing import is_lower_sha256_hex, stable_hash
from elspeth.web.composer.pipeline_planner import PipelinePlanResult
from elspeth.web.composer.pipeline_proposal import (
    AbsentBase,
    PresentBase,
    composition_content_hash,
)
from elspeth.web.composer.tools import is_blob_store_only_mutation_tool
from elspeth.web.coordination.contracts import (
    SessionOperationContext,
    SessionOperationKind,
)
from elspeth.web.coordination.database_clock import database_now
from elspeth.web.sessions.converters import state_from_record
from elspeth.web.sessions.models import (
    blobs_table,
    composition_proposals_table,
    composition_states_table,
    proposal_blob_effect_receipts_table,
    proposal_events_table,
)
from elspeth.web.sessions.proposal_authority import (
    _TOOL_PROPOSAL_CREATED_SCHEMA,
    _classify_authoritative_composition_proposal,
    _pipeline_public_metadata,
    _proposal_event_record_from_row,
    _proposal_record_from_row,
)
from elspeth.web.sessions.proposal_blob_effects import blob_row_snapshot_payload, proposal_blob_arguments_hash
from elspeth.web.sessions.proposal_blob_refs import validate_proposal_blob_references
from elspeth.web.sessions.protocol import (
    AuthoritativePipelineProposal,
    CompositionProposalRecord,
    CompositionStateData,
    StaleComposeStateError,
)

if TYPE_CHECKING:
    from elspeth.web.sessions.service import SessionServiceImpl


@final
class _SessionComposerMutationState:
    """Private lifetime and exact operation-kind binding for one DB transaction."""

    __slots__ = ("__active", "__connection", "__expected_kind", "__service", "__session_context", "__session_id")

    def __init__(
        self,
        service: SessionServiceImpl,
        connection: Connection,
        *,
        session_id: str,
        session_operation_context: SessionOperationContext,
        expected_kind: SessionOperationKind,
    ) -> None:
        self.__service = service
        self.__connection = connection
        self.__session_id = session_id
        self.__session_context = session_operation_context
        self.__expected_kind = expected_kind
        self.__active = True

    def _require_active(self) -> tuple[SessionServiceImpl, Connection, str, SessionOperationContext]:
        if not self.__active:
            raise AuditIntegrityError("composer mutation transaction is not active")
        return self.__service, self.__connection, self.__session_id, self.__session_context

    def _require_exact(self) -> tuple[SessionServiceImpl, Connection, str, datetime]:
        service, connection, session_id, session_context = self._require_active()
        now = database_now(connection)
        service._require_session_operation_context_on_connection(
            connection,
            session_context,
            session_id=session_id,
            expected_kind=self.__expected_kind,
            now=now,
        )
        return service, connection, session_id, now

    def _close(self) -> None:
        self.__active = False


@final
class _SessionComposerMutations:
    """Narrow ordinary proposal writes under one exact operation authority."""

    __slots__ = ("__state",)

    def __init__(self, state: _SessionComposerMutationState) -> None:
        self.__state = state

    def create_composition_proposal(
        self,
        *,
        proposal_id: str,
        event_id: str,
        tool_call_id: str,
        tool_name: str,
        summary: str,
        rationale: str,
        affects: Sequence[str],
        arguments_json: Mapping[str, Any],
        arguments_redacted_json: Mapping[str, Any],
        base_state_id: UUID | None,
        actor: str,
        user_message_id: UUID | None,
        composer_provenance: Mapping[str, str | None],
    ) -> CompositionProposalRecord:
        _service, connection, session_id, _now = self.__state._require_exact()
        validate_proposal_blob_references(
            connection,
            session_id=session_id,
            tool_name=tool_name,
            arguments=arguments_json,
        )
        _service, connection, session_id, now = self.__state._require_exact()
        connection.execute(
            insert(proposal_events_table).values(
                id=event_id,
                session_id=session_id,
                proposal_id=proposal_id,
                event_type="proposal.created",
                actor=actor,
                payload={
                    "schema": _TOOL_PROPOSAL_CREATED_SCHEMA,
                    "tool_call_id": tool_call_id,
                    "tool_name": tool_name,
                    "status": "pending",
                },
                created_at=now,
            )
        )
        connection.execute(
            insert(composition_proposals_table).values(
                id=proposal_id,
                session_id=session_id,
                tool_call_id=tool_call_id,
                user_message_id=str(user_message_id) if user_message_id is not None else None,
                composer_model_identifier=composer_provenance["composer_model_identifier"],
                composer_model_version=composer_provenance["composer_model_version"],
                composer_provider=composer_provenance["composer_provider"],
                composer_skill_hash=composer_provenance["composer_skill_hash"],
                tool_arguments_hash=composer_provenance["tool_arguments_hash"],
                tool_name=tool_name,
                status="pending",
                summary=summary,
                rationale=rationale,
                affects=list(affects),
                arguments_json=deep_thaw(arguments_json),
                arguments_redacted_json=deep_thaw(arguments_redacted_json),
                base_state_id=str(base_state_id) if base_state_id else None,
                committed_state_id=None,
                audit_event_id=event_id,
                created_at=now,
                updated_at=now,
            )
        )
        row = connection.execute(select(composition_proposals_table).where(composition_proposals_table.c.id == proposal_id)).one()
        return _proposal_record_from_row(row)

    def create_pipeline_composition_proposal(
        self,
        *,
        proposal_id: str,
        event_id: str,
        plan: PipelinePlanResult,
        summary: str,
        rationale: str,
        affects: Sequence[str],
        arguments_redacted_json: Mapping[str, Any],
        actor: str,
        normalized_provenance: Mapping[str, str | None],
        payload: Mapping[str, Any],
        user_message_id: UUID | None,
    ) -> CompositionProposalRecord:
        service, connection, session_id, _now = self.__state._require_exact()
        proposal = plan.proposal
        current_row = connection.execute(
            select(composition_states_table)
            .where(composition_states_table.c.session_id == session_id)
            .order_by(desc(composition_states_table.c.version))
            .limit(1)
        ).one_or_none()
        if type(proposal.base) is AbsentBase:
            if current_row is not None:
                raise StaleComposeStateError("pipeline proposal absent base conflicts with current state")
            base_state_id = None
        elif type(proposal.base) is PresentBase:
            if current_row is None or current_row.id != str(proposal.base.state_id):
                raise StaleComposeStateError("pipeline proposal present base state changed before creation")
            current_record = service._row_to_state_record(current_row)
            if composition_content_hash(state_from_record(current_record)) != proposal.base.composition_content_hash:
                raise StaleComposeStateError("pipeline proposal present base content changed before creation")
            base_state_id = str(proposal.base.state_id)
        else:
            raise AuditIntegrityError("pipeline proposal base is malformed")

        validate_proposal_blob_references(
            connection,
            session_id=session_id,
            tool_name="set_pipeline",
            arguments=deep_thaw(proposal.pipeline),
        )
        service, connection, session_id, now = self.__state._require_exact()
        connection.execute(
            insert(proposal_events_table).values(
                id=event_id,
                session_id=session_id,
                proposal_id=proposal_id,
                event_type="proposal.created",
                actor=actor,
                payload=payload,
                created_at=now,
            )
        )
        connection.execute(
            insert(composition_proposals_table).values(
                id=proposal_id,
                session_id=session_id,
                tool_call_id=plan.tool_call_id,
                user_message_id=str(user_message_id) if user_message_id is not None else None,
                composer_model_identifier=normalized_provenance["composer_model_identifier"],
                composer_model_version=normalized_provenance["composer_model_version"],
                composer_provider=normalized_provenance["composer_provider"],
                composer_skill_hash=normalized_provenance["composer_skill_hash"],
                tool_arguments_hash=normalized_provenance["tool_arguments_hash"],
                tool_name="set_pipeline",
                status="pending",
                summary=summary,
                rationale=rationale,
                affects=list(affects),
                arguments_json=deep_thaw(proposal.pipeline),
                arguments_redacted_json=deep_thaw(arguments_redacted_json),
                base_state_id=base_state_id,
                committed_state_id=None,
                audit_event_id=event_id,
                created_at=now,
                updated_at=now,
            )
        )
        row = connection.execute(select(composition_proposals_table).where(composition_proposals_table.c.id == proposal_id)).one()
        record = _proposal_record_from_row(row)
        authority = AuthoritativePipelineProposal(
            row=record,
            proposal=proposal,
            creation_event_id=UUID(event_id),
            custody_result=plan.custody_result,
        )
        return replace(record, pipeline_metadata=_pipeline_public_metadata(authority))

    def _validated_blob_effect_receipt(self, proposal_row: Any) -> Any | None:
        """Read one receipt and prove its proposal and committed-result binding."""
        _service, connection, session_id, _now = self.__state._require_exact()
        receipt_rows = connection.execute(
            select(proposal_blob_effect_receipts_table).where(
                proposal_blob_effect_receipts_table.c.proposal_id == proposal_row.id,
                proposal_blob_effect_receipts_table.c.session_id == session_id,
            )
        ).fetchall()
        if len(receipt_rows) > 1:
            raise AuditIntegrityError("Tier 1: blob proposal has multiple applied-effect receipts")
        if not receipt_rows:
            return None
        receipt = receipt_rows[0]
        if proposal_row.tool_name not in {"update_blob", "delete_blob"}:
            raise AuditIntegrityError("Tier 1: non-blob proposal has an applied blob-effect receipt")
        if (
            receipt.proposal_id != proposal_row.id
            or receipt.session_id != session_id
            or receipt.tool_name != proposal_row.tool_name
            or type(receipt.blob_id) is not str
            or not receipt.blob_id
        ):
            raise AuditIntegrityError("Tier 1: blob effect receipt identity binding is malformed")
        expected_arguments_hash = proposal_blob_arguments_hash(
            tool_name=proposal_row.tool_name,
            arguments=proposal_row.arguments_json,
            blob_id=receipt.blob_id,
        )
        if receipt.arguments_hash != expected_arguments_hash:
            raise AuditIntegrityError("Tier 1: blob effect receipt arguments binding changed")
        if type(receipt.result_blob_snapshot) is not dict:
            raise AuditIntegrityError("Tier 1: blob effect receipt result snapshot is malformed")
        if (
            "id" not in receipt.result_blob_snapshot
            or receipt.result_blob_snapshot["id"] != receipt.blob_id
            or "session_id" not in receipt.result_blob_snapshot
            or receipt.result_blob_snapshot["session_id"] != session_id
            or not is_lower_sha256_hex(receipt.result_blob_snapshot_hash)
            or stable_hash(receipt.result_blob_snapshot) != receipt.result_blob_snapshot_hash
        ):
            raise AuditIntegrityError("Tier 1: blob effect receipt result hash is malformed")
        if proposal_row.status == "pending":
            live_blob = connection.execute(
                select(blobs_table).where(
                    blobs_table.c.id == receipt.blob_id,
                    blobs_table.c.session_id == session_id,
                )
            ).one_or_none()
            if receipt.tool_name == "update_blob":
                if live_blob is None or blob_row_snapshot_payload(live_blob) != receipt.result_blob_snapshot:
                    raise AuditIntegrityError("Tier 1: applied update_blob receipt no longer matches the committed blob result")
            elif live_blob is not None:
                raise AuditIntegrityError("Tier 1: applied delete_blob receipt still has live blob metadata")
            if receipt.accepted_event_id is not None or receipt.accepted_at is not None:
                raise AuditIntegrityError("Tier 1: pending blob proposal receipt is already bound to acceptance")
        elif proposal_row.status == "committed":
            if receipt.accepted_event_id != proposal_row.audit_event_id or receipt.accepted_at is None:
                raise AuditIntegrityError("Tier 1: committed blob proposal receipt lacks its exact accepted-event binding")
        else:
            raise AuditIntegrityError("Tier 1: rejected blob proposal retained an applied-effect receipt")
        return receipt

    def has_applied_blob_effect(self, *, proposal_id: str) -> bool:
        _service, connection, session_id, _now = self.__state._require_exact()
        proposal_row = connection.execute(
            select(composition_proposals_table).where(
                composition_proposals_table.c.id == proposal_id,
                composition_proposals_table.c.session_id == session_id,
            )
        ).one_or_none()
        if proposal_row is None:
            raise KeyError(proposal_id)
        return self._validated_blob_effect_receipt(proposal_row) is not None

    def reject_pending_proposal(
        self,
        *,
        proposal_id: str,
        event_id: str,
        actor: str,
    ) -> None:
        """Append and bind one terminal rejection under exact PROPOSAL authority."""
        _service, connection, session_id, now = self.__state._require_exact()
        proposal_row = connection.execute(
            select(composition_proposals_table).where(
                composition_proposals_table.c.id == proposal_id,
                composition_proposals_table.c.session_id == session_id,
            )
        ).one_or_none()
        if proposal_row is None:
            raise KeyError(proposal_id)
        if self._validated_blob_effect_receipt(proposal_row) is not None:
            raise ValueError(f"Proposal {proposal_id} has an applied blob effect and must complete acceptance")
        connection.execute(
            insert(proposal_events_table).values(
                id=event_id,
                session_id=session_id,
                proposal_id=proposal_id,
                event_type="proposal.rejected",
                actor=actor,
                payload={"status": "rejected"},
                created_at=now,
            )
        )
        updated = connection.execute(
            update(composition_proposals_table)
            .where(composition_proposals_table.c.id == proposal_id)
            .where(composition_proposals_table.c.session_id == session_id)
            .where(composition_proposals_table.c.status == "pending")
            .values(
                status="rejected",
                audit_event_id=event_id,
                updated_at=now,
            )
        )
        if updated.rowcount != 1:
            raise ValueError(f"Proposal {proposal_id} must be pending to reject")

    def accept_pending_ordinary_proposal(
        self,
        *,
        proposal_id: str,
        event_id: str,
        expected_current_state_id: UUID | None,
        state: CompositionStateData | None,
        actor: str,
    ) -> CompositionProposalRecord:
        """Atomically bind one ordinary proposal to its accepted state."""
        service, connection, session_id, _now = self.__state._require_exact()
        proposal_row = connection.execute(
            select(composition_proposals_table)
            .where(composition_proposals_table.c.id == proposal_id)
            .where(composition_proposals_table.c.session_id == session_id)
        ).one_or_none()
        if proposal_row is None:
            raise KeyError(proposal_id)
        creation_rows = connection.execute(
            select(proposal_events_table)
            .where(proposal_events_table.c.session_id == session_id)
            .where(proposal_events_table.c.proposal_id == proposal_id)
            .where(proposal_events_table.c.event_type == "proposal.created")
        ).fetchall()
        if len(creation_rows) != 1:
            raise AuditIntegrityError("ordinary proposal acceptance requires exactly one creation event")
        authority = _classify_authoritative_composition_proposal(
            row=_proposal_record_from_row(proposal_row),
            creation_event=_proposal_event_record_from_row(creation_rows[0]),
        )
        if authority.pipeline is not None:
            raise ValueError("Canonical pipeline proposals require the pipeline settlement authority")
        if authority.row.status != "pending":
            raise ValueError(f"Proposal {proposal_id} must be pending to commit; got {authority.row.status!r}")
        blob_effect_receipt = self._validated_blob_effect_receipt(proposal_row)
        terminal_rows = connection.execute(
            select(proposal_events_table.c.id)
            .where(proposal_events_table.c.session_id == session_id)
            .where(proposal_events_table.c.proposal_id == proposal_id)
            .where(proposal_events_table.c.event_type.in_(("proposal.accepted", "proposal.rejected")))
        ).fetchall()
        if terminal_rows:
            raise AuditIntegrityError("pending ordinary proposal already has a terminal event")

        current_row = connection.execute(
            select(composition_states_table)
            .where(composition_states_table.c.session_id == session_id)
            .order_by(desc(composition_states_table.c.version))
            .limit(1)
        ).one_or_none()
        actual_current_state_id = UUID(current_row.id) if current_row is not None else None
        if actual_current_state_id != expected_current_state_id:
            raise StaleComposeStateError(
                "ordinary proposal acceptance: current composition state changed "
                f"for session_id={session_id!r}; expected={expected_current_state_id!s}, "
                f"actual={actual_current_state_id!s}"
            )
        if authority.row.base_state_id != actual_current_state_id and (blob_effect_receipt is None or actual_current_state_id is None):
            raise StaleComposeStateError("ordinary proposal acceptance: proposal base no longer matches the current state")

        blob_store_only = is_blob_store_only_mutation_tool(authority.row.tool_name)
        if blob_store_only and blob_effect_receipt is None:
            raise ValueError("blob-only ordinary proposal acceptance requires its durable applied-effect receipt")
        if blob_store_only and actual_current_state_id is not None and state is not None:
            raise ValueError("blob-only ordinary proposal with an existing state must bind that state")
        if blob_store_only and actual_current_state_id is None and state is None:
            raise ValueError("blob-only ordinary proposal with no existing state requires an initial state snapshot")
        if not blob_store_only and state is None:
            raise ValueError("non-blob ordinary proposal acceptance requires a new state")

        service, connection, session_id, transaction_time = self.__state._require_exact()
        _service, _connection, _session_id, session_context = self.__state._require_active()
        service._assert_session_write_lock_held(
            connection,
            session_id,
            caller="_SessionComposerMutations.accept_pending_ordinary_proposal",
        )
        if state is None:
            if actual_current_state_id is None:
                raise ValueError("ordinary proposal acceptance requires an existing state or a new state snapshot")
            committed_state_id = str(actual_current_state_id)
        else:
            committed_state_id = service._insert_composition_checkpoint(
                connection,
                session_id=session_id,
                state=state,
                derived_from_state_id=str(actual_current_state_id) if actual_current_state_id is not None else None,
                operation_kind=SessionOperationKind.PROPOSAL,
                provenance="tool_call",
                created_at=transaction_time,
                session_operation_context=session_context,
            )

        connection.execute(
            insert(proposal_events_table).values(
                id=event_id,
                session_id=session_id,
                proposal_id=proposal_id,
                event_type="proposal.accepted",
                actor=actor,
                payload={"committed_state_id": committed_state_id},
                created_at=transaction_time,
            )
        )
        if blob_effect_receipt is not None:
            receipt_bound = connection.execute(
                update(proposal_blob_effect_receipts_table)
                .where(
                    proposal_blob_effect_receipts_table.c.proposal_id == proposal_id,
                    proposal_blob_effect_receipts_table.c.session_id == session_id,
                    proposal_blob_effect_receipts_table.c.accepted_event_id.is_(None),
                    proposal_blob_effect_receipts_table.c.accepted_at.is_(None),
                )
                .values(accepted_event_id=event_id, accepted_at=transaction_time)
            )
            if receipt_bound.rowcount != 1:
                raise AuditIntegrityError("blob effect receipt changed before acceptance binding")
        updated = connection.execute(
            update(composition_proposals_table)
            .where(composition_proposals_table.c.id == proposal_id)
            .where(composition_proposals_table.c.session_id == session_id)
            .where(composition_proposals_table.c.status == "pending")
            .values(
                status="committed",
                committed_state_id=committed_state_id,
                audit_event_id=event_id,
                updated_at=transaction_time,
            )
        )
        if updated.rowcount != 1:
            raise ValueError(f"Proposal {proposal_id} must be pending to commit")
        updated_row = connection.execute(
            select(composition_proposals_table)
            .where(composition_proposals_table.c.id == proposal_id)
            .where(composition_proposals_table.c.session_id == session_id)
        ).one()
        return _proposal_record_from_row(updated_row)


@final
class _SessionComposerMutationTransaction:
    """Handle-free ordinary Composer mutation capability."""

    __slots__ = ("__composer", "__state")

    def __init__(
        self,
        service: SessionServiceImpl,
        connection: Connection,
        *,
        session_id: str,
        session_operation_context: SessionOperationContext,
        expected_kind: SessionOperationKind,
    ) -> None:
        state = _SessionComposerMutationState(
            service,
            connection,
            session_id=session_id,
            session_operation_context=session_operation_context,
            expected_kind=expected_kind,
        )
        self.__state = state
        self.__composer = _SessionComposerMutations(state)

    @property
    def composer(self) -> _SessionComposerMutations:
        self.__state._require_active()
        return self.__composer

    def _close(self) -> None:
        self.__state._close()
