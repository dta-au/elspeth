"""Transaction-scoped composer and guided mutation capabilities."""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import datetime
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Literal, final
from uuid import UUID

from sqlalchemy import Connection, desc, func, insert, select, update
from sqlalchemy.engine import RowMapping

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.freeze import deep_thaw
from elspeth.contracts.hashing import is_lower_sha256_hex, stable_hash
from elspeth.web.composer.pipeline_commit import PipelineDispatchAuditBinding
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
from elspeth.web.coordination.repository import (
    _RepositoryInterpretationMutations,
    _RepositoryMutationState,
)
from elspeth.web.sessions.converters import state_from_record
from elspeth.web.sessions.models import (
    blobs_table,
    composition_proposals_table,
    composition_states_table,
    guided_operation_events_table,
    guided_operations_table,
    proposal_blob_effect_receipts_table,
    proposal_events_table,
    sessions_table,
)
from elspeth.web.sessions.proposal_authority import (
    _PIPELINE_CREATED_SCHEMA,
    _TOOL_PROPOSAL_CREATED_SCHEMA,
    _classify_authoritative_composition_proposal,
    _pipeline_accepted_payload,
    _pipeline_public_metadata,
    _pipeline_rejected_payload,
    _PipelineCreatedEventPayload,
    _proposal_event_record_from_row,
    _proposal_record_from_row,
    _verify_pipeline_lifecycle_authority,
)
from elspeth.web.sessions.proposal_blob_effects import blob_row_snapshot_payload, proposal_blob_arguments_hash
from elspeth.web.sessions.proposal_blob_refs import validate_proposal_blob_references
from elspeth.web.sessions.protocol import (
    GUIDED_OPERATION_FAILURE_CODE_VALUES,
    AuthoritativePipelineProposal,
    CompositionProposalRecord,
    CompositionStateData,
    GuidedFailureAuditCohort,
    GuidedFullPipelineProposalStageCommand,
    GuidedOperationCompleted,
    GuidedOperationFailed,
    GuidedOperationFailureCode,
    GuidedOperationFence,
    GuidedOperationFenceLostError,
    GuidedOperationResult,
    GuidedOperationSettlementConflictError,
    GuidedPipelineProposalStageCommand,
    GuidedProposalInvalidationReason,
    GuidedSessionResult,
    PipelineProposalRejectionReason,
    SessionOperationInterpretationMutations,
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
        now = service._guided_database_now(connection)
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
        supersedes_proposal_id: UUID | None,
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

        if supersedes_proposal_id is not None:
            superseded = connection.execute(
                select(composition_proposals_table)
                .where(composition_proposals_table.c.id == str(supersedes_proposal_id))
                .where(composition_proposals_table.c.session_id == session_id)
            ).one_or_none()
            if superseded is None:
                raise AuditIntegrityError("superseded pipeline proposal is not owned by this session")
            superseded_events = connection.execute(
                select(proposal_events_table)
                .where(proposal_events_table.c.session_id == session_id)
                .where(proposal_events_table.c.proposal_id == str(supersedes_proposal_id))
                .where(proposal_events_table.c.event_type == "proposal.created")
            ).fetchall()
            if len(superseded_events) != 1:
                raise AuditIntegrityError("superseded pipeline proposal has invalid creation authority")
            superseded_payload = superseded_events[0].payload
            superseded_schema = (
                superseded_payload["schema"]
                if type(superseded_payload) in (dict, MappingProxyType) and "schema" in superseded_payload
                else None
            )
            superseded_draft_hash = (
                superseded_payload["draft_hash"]
                if type(superseded_payload) in (dict, MappingProxyType) and "draft_hash" in superseded_payload
                else None
            )
            if (
                type(superseded_payload) not in (dict, MappingProxyType)
                or superseded_schema != _PIPELINE_CREATED_SCHEMA
                or superseded_draft_hash != proposal.supersedes_draft_hash
            ):
                raise AuditIntegrityError("superseded pipeline proposal draft binding mismatch")

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
            supersedes_proposal_id=supersedes_proposal_id,
            # Creation time: no ``proposal.rebased`` event can exist yet, so
            # the lifecycle anchor coincides with the immutable reviewed base.
            current_base=proposal.base,
        )
        return replace(record, pipeline_metadata=_pipeline_public_metadata(authority))

    def create_guided_pipeline_proposal(
        self,
        *,
        command: GuidedPipelineProposalStageCommand | GuidedFullPipelineProposalStageCommand,
        event_id: str,
        user_message_id: UUID | None,
        base_state_id: str,
        normalized_provenance: Mapping[str, str | None],
        payload: _PipelineCreatedEventPayload,
        created_at: datetime,
    ) -> CompositionProposalRecord:
        """Create the pending proposal a guided settlement stages in its own transaction.

        Unlike ``create_pipeline_composition_proposal`` the base is the
        checkpoint the caller inserted moments ago in this same transaction,
        so there is no current-head comparison here: the guided ``_sync``
        proved the checkpoint chain, validated blob references, and re-checks
        the guided fence when it binds and completes the operation. The
        proposal content comes from the exact staging command; every row of
        one settlement carries the caller's ``created_at``.
        """
        _service, connection, session_id, _now = self.__state._require_exact()
        if type(command) not in (GuidedPipelineProposalStageCommand, GuidedFullPipelineProposalStageCommand):
            raise TypeError("command must be an exact guided pipeline proposal stage command")
        if type(created_at) is not datetime:
            raise TypeError("created_at must be an exact datetime")
        proposal_id = str(command.proposal_id)
        connection.execute(
            insert(proposal_events_table).values(
                id=event_id,
                session_id=session_id,
                proposal_id=proposal_id,
                event_type="proposal.created",
                actor=command.actor,
                payload=payload,
                created_at=created_at,
            )
        )
        connection.execute(
            insert(composition_proposals_table).values(
                id=proposal_id,
                session_id=session_id,
                tool_call_id=command.plan.tool_call_id,
                user_message_id=str(user_message_id) if user_message_id is not None else None,
                composer_model_identifier=normalized_provenance["composer_model_identifier"],
                composer_model_version=normalized_provenance["composer_model_version"],
                composer_provider=normalized_provenance["composer_provider"],
                composer_skill_hash=normalized_provenance["composer_skill_hash"],
                tool_arguments_hash=normalized_provenance["tool_arguments_hash"],
                tool_name="set_pipeline",
                status="pending",
                summary=command.summary,
                rationale=command.rationale,
                affects=list(command.affects),
                arguments_json=deep_thaw(command.plan.proposal.pipeline),
                arguments_redacted_json=deep_thaw(command.arguments_redacted_json),
                base_state_id=base_state_id,
                committed_state_id=None,
                audit_event_id=event_id,
                created_at=created_at,
                updated_at=created_at,
            )
        )
        row = connection.execute(select(composition_proposals_table).where(composition_proposals_table.c.id == proposal_id)).one()
        return _proposal_record_from_row(row)

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
            conn=connection,
            row=_proposal_record_from_row(proposal_row),
            creation_event=_proposal_event_record_from_row(creation_rows[0]),
            reviewed_facts=None,
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
            committed_state_id = service._insert_checkpoint_preserving_guided_proposal(
                connection,
                session_id=session_id,
                state=state,
                derived_from_state_id=str(actual_current_state_id) if actual_current_state_id is not None else None,
                operation_kind=SessionOperationKind.PROPOSAL,
                actor=actor,
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


@final
class _GuidedSessionMutationState:
    """Private lifetime and exact dual-fence binding for one DB transaction."""

    __slots__ = ("__active", "__connection", "__guided_fence", "__service", "__session_context")

    def __init__(
        self,
        service: SessionServiceImpl,
        connection: Connection,
        *,
        guided_fence: GuidedOperationFence,
        session_operation_context: SessionOperationContext,
    ) -> None:
        self.__service = service
        self.__connection = connection
        self.__guided_fence = guided_fence
        self.__session_context = session_operation_context
        self.__active = True

    def _require_active(self) -> tuple[SessionServiceImpl, Connection, GuidedOperationFence, SessionOperationContext]:
        if not self.__active:
            raise AuditIntegrityError("guided mutation transaction is not active")
        return self.__service, self.__connection, self.__guided_fence, self.__session_context

    def _require_exact(self) -> tuple[SessionServiceImpl, Connection, GuidedOperationFence, RowMapping, datetime]:
        service, connection, guided_fence, session_context = self._require_active()
        row, now = service.require_guided_operation_authority_on_connection(
            connection,
            guided_fence,
            session_context,
        )
        return service, connection, guided_fence, row, now

    def _close(self) -> None:
        self.__active = False


@final
class _GuidedSessionMutations:
    """Narrow guided-operation writes over one exact dual-fenced lifetime."""

    __slots__ = ("__state",)

    def __init__(self, state: _GuidedSessionMutationState) -> None:
        self.__state = state

    def record_nonterminal_event(
        self,
        *,
        event_kind: Literal["claimed", "renewed", "taken_over"],
        actor: str,
        attempt: int,
        prior_attempt: int | None,
        lease_expires_at: datetime,
        request_hash: str,
        occurred_at: datetime,
    ) -> None:
        service, connection, fence, row, _now = self.__state._require_exact()
        if event_kind not in {"claimed", "renewed", "taken_over"}:
            raise ValueError("guided nonterminal event kind is unsupported")
        if row["request_hash"] != request_hash or row["attempt"] != attempt:
            raise GuidedOperationFenceLostError(fence)
        next_sequence = connection.execute(
            select(func.coalesce(func.max(guided_operation_events_table.c.sequence), 0) + 1).where(
                guided_operation_events_table.c.session_id == str(fence.session_id),
                guided_operation_events_table.c.operation_id == fence.operation_id,
            )
        ).scalar_one()
        # Revalidate immediately before the INSERT, after sequence allocation.
        self.__state._require_exact()
        connection.execute(
            insert(guided_operation_events_table).values(
                **service._guided_operation_event_values(
                    session_id=str(fence.session_id),
                    operation_id=fence.operation_id,
                    sequence=int(next_sequence),
                    event_kind=event_kind,
                    actor=actor,
                    attempt=attempt,
                    prior_attempt=prior_attempt,
                    lease_expires_at=lease_expires_at,
                    request_hash=request_hash,
                    failure_audit_cohort=None,
                    occurred_at=occurred_at,
                )
            )
        )

    def bind(
        self,
        *,
        originating_message_id: UUID | None = None,
        proposal_id: UUID | None = None,
        result_state_id: UUID | None = None,
        result_session_id: UUID | None = None,
    ) -> None:
        service, connection, fence, row, now = self.__state._require_exact()
        values = {
            "originating_message_id": service._merge_guided_binding(
                current=row["originating_message_id"], requested=originating_message_id, label="originating message"
            ),
            "proposal_id": service._merge_guided_binding(current=row["proposal_id"], requested=proposal_id, label="proposal"),
            "result_state_id": service._merge_guided_binding(
                current=row["result_state_id"], requested=result_state_id, label="result state"
            ),
            "result_session_id": service._merge_guided_binding(
                current=row["result_session_id"], requested=result_session_id, label="result session"
            ),
            "updated_at": now,
        }
        changed = connection.execute(
            update(guided_operations_table)
            .where(
                guided_operations_table.c.session_id == str(fence.session_id),
                guided_operations_table.c.operation_id == fence.operation_id,
                guided_operations_table.c.status == "in_progress",
                guided_operations_table.c.lease_token == fence.lease_token,
                guided_operations_table.c.attempt == fence.attempt,
                guided_operations_table.c.lease_expires_at > now,
            )
            .values(**values)
        ).rowcount
        if changed != 1:
            self.__state._close()
            raise GuidedOperationFenceLostError(fence)

    def require_no_active_confirmation(self, *, proposal_id: UUID, now: datetime) -> None:
        _service, connection, fence, _row, _database_now = self.__state._require_exact()
        if type(proposal_id) is not UUID:
            raise TypeError("proposal_id must be an exact UUID")
        proposal_id_str = str(proposal_id)
        connection.execute(
            update(guided_operations_table)
            .where(
                guided_operations_table.c.session_id == str(fence.session_id),
                guided_operations_table.c.proposal_id == proposal_id_str,
                guided_operations_table.c.status == "in_progress",
                guided_operations_table.c.lease_expires_at <= now,
            )
            .values(proposal_id=None, updated_at=now)
        )
        active = connection.execute(
            select(guided_operations_table.c.operation_id)
            .where(
                guided_operations_table.c.session_id == str(fence.session_id),
                guided_operations_table.c.proposal_id == proposal_id_str,
                guided_operations_table.c.status == "in_progress",
                guided_operations_table.c.lease_expires_at > now,
            )
            .limit(1)
        ).scalar_one_or_none()
        if active is not None:
            raise GuidedOperationSettlementConflictError()

    def claim_confirmation(self, *, proposal_id: UUID, now: datetime) -> None:
        service, connection, fence, row, _database_now = self.__state._require_exact()
        if row["kind"] != "guided_respond":
            raise AuditIntegrityError("guided confirmation admission requires guided_respond")
        if type(proposal_id) is not UUID:
            raise TypeError("proposal_id must be an exact UUID")
        proposal_id_str = str(proposal_id)
        connection.execute(
            update(guided_operations_table)
            .where(
                guided_operations_table.c.session_id == str(fence.session_id),
                guided_operations_table.c.proposal_id == proposal_id_str,
                guided_operations_table.c.status == "in_progress",
                guided_operations_table.c.lease_expires_at <= now,
            )
            .values(proposal_id=None, updated_at=now)
        )
        owner = connection.execute(
            select(guided_operations_table.c.operation_id)
            .where(
                guided_operations_table.c.session_id == str(fence.session_id),
                guided_operations_table.c.proposal_id == proposal_id_str,
                guided_operations_table.c.status == "in_progress",
                guided_operations_table.c.lease_expires_at > now,
            )
            .limit(1)
        ).scalar_one_or_none()
        if owner is not None and owner != fence.operation_id:
            raise GuidedOperationSettlementConflictError()
        _service, connection, fence, current, database_now = self.__state._require_exact()
        bound = service._merge_guided_binding(current=current["proposal_id"], requested=proposal_id, label="proposal")
        changed = connection.execute(
            update(guided_operations_table)
            .where(
                guided_operations_table.c.session_id == str(fence.session_id),
                guided_operations_table.c.operation_id == fence.operation_id,
                guided_operations_table.c.status == "in_progress",
                guided_operations_table.c.lease_token == fence.lease_token,
                guided_operations_table.c.attempt == fence.attempt,
                guided_operations_table.c.lease_expires_at > database_now,
            )
            .values(proposal_id=bound, updated_at=database_now)
        ).rowcount
        if changed != 1:
            self.__state._close()
            raise GuidedOperationFenceLostError(fence)

    def complete(
        self,
        *,
        result: GuidedOperationResult,
        response_hash: str,
        actor: str,
    ) -> GuidedOperationCompleted:
        service, connection, fence, row, now = self.__state._require_exact()
        service._validate_guided_actor(actor)
        service._validate_guided_hash(response_hash, label="guided operation response_hash")
        if type(result) is GuidedSessionResult:
            parent = (
                connection.execute(
                    select(sessions_table.c.user_id, sessions_table.c.auth_provider_type).where(
                        sessions_table.c.id == str(fence.session_id)
                    )
                )
                .mappings()
                .one_or_none()
            )
            child = (
                connection.execute(
                    select(
                        sessions_table.c.user_id,
                        sessions_table.c.auth_provider_type,
                        sessions_table.c.forked_from_session_id,
                    ).where(sessions_table.c.id == str(result.session_id))
                )
                .mappings()
                .one_or_none()
            )
            if (
                parent is None
                or child is None
                or child["forked_from_session_id"] != str(fence.session_id)
                or child["user_id"] != parent["user_id"]
                or child["auth_provider_type"] != parent["auth_provider_type"]
            ):
                raise AuditIntegrityError("Guided fork result session failed lineage or principal custody validation")
        locator_values, normalized = service._guided_completion_values(row=row, result=result)
        # The exact pair is checked again immediately before terminal DML.
        _service, connection, fence, row, now = self.__state._require_exact()
        try:
            changed = connection.execute(
                update(guided_operations_table)
                .where(
                    guided_operations_table.c.session_id == str(fence.session_id),
                    guided_operations_table.c.operation_id == fence.operation_id,
                    guided_operations_table.c.status == "in_progress",
                    guided_operations_table.c.lease_token == fence.lease_token,
                    guided_operations_table.c.attempt == fence.attempt,
                    guided_operations_table.c.lease_expires_at > now,
                )
                .values(
                    status="completed",
                    lease_token=None,
                    lease_expires_at=None,
                    response_hash=response_hash,
                    failure_code=None,
                    unproducible_output_fields=None,
                    failure_diagnostics=None,
                    settled_at=now,
                    updated_at=now,
                    **locator_values,
                )
            ).rowcount
            if changed != 1:
                raise GuidedOperationFenceLostError(fence)
            next_sequence = connection.execute(
                select(func.coalesce(func.max(guided_operation_events_table.c.sequence), 0) + 1).where(
                    guided_operation_events_table.c.session_id == str(fence.session_id),
                    guided_operation_events_table.c.operation_id == fence.operation_id,
                )
            ).scalar_one()
            connection.execute(
                insert(guided_operation_events_table).values(
                    **service._guided_operation_event_values(
                        session_id=str(fence.session_id),
                        operation_id=fence.operation_id,
                        sequence=int(next_sequence),
                        event_kind="completed",
                        actor=actor,
                        attempt=fence.attempt,
                        prior_attempt=None,
                        lease_expires_at=None,
                        request_hash=row["request_hash"],
                        failure_audit_cohort=None,
                        occurred_at=now,
                    )
                )
            )
        except BaseException:
            self.__state._close()
            raise
        self.__state._close()
        return GuidedOperationCompleted(result=normalized, response_hash=response_hash)

    def fail(
        self,
        *,
        failure_code: GuidedOperationFailureCode,
        actor: str,
        failure_audit_cohort: GuidedFailureAuditCohort,
        unproducible_output_fields: tuple[str, ...],
        failure_diagnostics: tuple[str, ...] = (),
    ) -> GuidedOperationFailed:
        service, connection, fence, row, now = self.__state._require_exact()
        service._validate_guided_actor(actor)
        if failure_code not in GUIDED_OPERATION_FAILURE_CODE_VALUES:
            raise ValueError("unsupported guided operation failure code")
        if type(failure_audit_cohort) is not GuidedFailureAuditCohort:
            raise AuditIntegrityError("failed guided operation event must carry exactly one failure audit cohort commitment")
        if type(unproducible_output_fields) is not tuple or any(type(field) is not str for field in unproducible_output_fields):
            raise ValueError("unproducible_output_fields must be an exact string tuple")
        failed = GuidedOperationFailed(
            failure_code=failure_code,
            unproducible_output_fields=unproducible_output_fields,
            failure_diagnostics=failure_diagnostics,
        )
        try:
            changed = connection.execute(
                update(guided_operations_table)
                .where(
                    guided_operations_table.c.session_id == str(fence.session_id),
                    guided_operations_table.c.operation_id == fence.operation_id,
                    guided_operations_table.c.status == "in_progress",
                    guided_operations_table.c.lease_token == fence.lease_token,
                    guided_operations_table.c.attempt == fence.attempt,
                    guided_operations_table.c.lease_expires_at > now,
                )
                .values(
                    status="failed",
                    lease_token=None,
                    lease_expires_at=None,
                    proposal_id=None,
                    result_kind=None,
                    result_state_id=None,
                    result_message_id=None,
                    result_session_id=None,
                    response_hash=None,
                    failure_code=failure_code,
                    unproducible_output_fields=(list(unproducible_output_fields) if unproducible_output_fields else None),
                    failure_diagnostics=list(failure_diagnostics) if failure_diagnostics else None,
                    settled_at=now,
                    updated_at=now,
                )
            ).rowcount
            if changed != 1:
                raise GuidedOperationFenceLostError(fence)
            next_sequence = connection.execute(
                select(func.coalesce(func.max(guided_operation_events_table.c.sequence), 0) + 1).where(
                    guided_operation_events_table.c.session_id == str(fence.session_id),
                    guided_operation_events_table.c.operation_id == fence.operation_id,
                )
            ).scalar_one()
            connection.execute(
                insert(guided_operation_events_table).values(
                    **service._guided_operation_event_values(
                        session_id=str(fence.session_id),
                        operation_id=fence.operation_id,
                        sequence=int(next_sequence),
                        event_kind="failed",
                        actor=actor,
                        attempt=fence.attempt,
                        prior_attempt=None,
                        lease_expires_at=None,
                        request_hash=row["request_hash"],
                        failure_audit_cohort=failure_audit_cohort,
                        occurred_at=now,
                    )
                )
            )
        except BaseException:
            self.__state._close()
            raise
        self.__state._close()
        return failed

    def mark_session_updated(self, *, updated_at: datetime) -> None:
        """Bump ``sessions.updated_at`` after this operation appended rows to its session.

        Every guided settlement stamps the rows of one cohort with one caller
        clock, so the stamp is a parameter; the dual fence is re-checked
        immediately before the UPDATE.
        """
        _service, connection, fence, _row, _now = self.__state._require_exact()
        if type(updated_at) is not datetime:
            raise TypeError("updated_at must be an exact datetime")
        changed = connection.execute(
            update(sessions_table).where(sessions_table.c.id == str(fence.session_id)).values(updated_at=updated_at)
        ).rowcount
        if changed != 1:
            self.__state._close()
            raise AuditIntegrityError("guided session touch did not find exactly one session row")


@final
class _GuidedComposerMutations:
    """Narrow Composer writes sharing one exact guided/session transaction."""

    __slots__ = ("__state",)

    def __init__(self, state: _GuidedSessionMutationState) -> None:
        self.__state = state

    def reject_pending_proposal(
        self,
        *,
        authority: AuthoritativePipelineProposal,
        actor: str,
        created_at: datetime,
        reason: GuidedProposalInvalidationReason,
    ) -> None:
        service, connection, fence, _row, database_now = self.__state._require_exact()
        if type(authority) is not AuthoritativePipelineProposal:
            raise TypeError("authority must be an exact AuthoritativePipelineProposal")
        if authority.row.session_id != fence.session_id or authority.row.status != "pending":
            raise AuditIntegrityError("guided pending proposal authority is not exact for this session")
        _verify_pipeline_lifecycle_authority(connection, service=service, authority=authority)
        proposal_id = authority.row.id
        _GuidedSessionMutations(self.__state).require_no_active_confirmation(
            proposal_id=proposal_id,
            now=database_now,
        )
        event_id = str(uuid.uuid4())
        # Revalidate after expiry cleanup and immediately before the event INSERT.
        _service, connection, fence, _row, _database_now = self.__state._require_exact()
        connection.execute(
            insert(proposal_events_table).values(
                id=event_id,
                session_id=str(fence.session_id),
                proposal_id=str(proposal_id),
                event_type="proposal.rejected",
                actor=actor,
                payload=_pipeline_rejected_payload(authority=authority, reason=reason, dispatch=None),
                created_at=created_at,
            )
        )
        updated = connection.execute(
            update(composition_proposals_table)
            .where(composition_proposals_table.c.session_id == str(fence.session_id))
            .where(composition_proposals_table.c.id == str(proposal_id))
            .where(composition_proposals_table.c.status == "pending")
            .values(
                status="rejected",
                committed_state_id=None,
                audit_event_id=event_id,
                updated_at=created_at,
            )
        )
        if updated.rowcount != 1:
            raise AuditIntegrityError("guided proposal invalidation lost the pending proposal CAS")

    def record_pending_proposal_rejection(
        self,
        *,
        authority: AuthoritativePipelineProposal,
        actor: str,
        created_at: datetime,
        reason: PipelineProposalRejectionReason,
    ) -> str:
        """Terminal ``proposal.rejected`` event and pending->rejected CAS for the proposal this operation holds.

        Unlike ``reject_pending_proposal`` there is no confirmation sweep: the
        caller's own guided operation legitimately holds the proposal it is
        settling (rejection, back-edit supersession). Returns the event id.
        """
        _service, connection, fence, _row, _database_now = self.__state._require_exact()
        if type(authority) is not AuthoritativePipelineProposal:
            raise TypeError("authority must be an exact AuthoritativePipelineProposal")
        if authority.row.session_id != fence.session_id or authority.row.status != "pending":
            raise AuditIntegrityError("guided pending proposal authority is not exact for this session")
        if type(created_at) is not datetime:
            raise TypeError("created_at must be an exact datetime")
        proposal_id = str(authority.row.id)
        event_id = str(uuid.uuid4())
        connection.execute(
            insert(proposal_events_table).values(
                id=event_id,
                session_id=str(fence.session_id),
                proposal_id=proposal_id,
                event_type="proposal.rejected",
                actor=actor,
                payload=_pipeline_rejected_payload(authority=authority, reason=reason, dispatch=None),
                created_at=created_at,
            )
        )
        updated = connection.execute(
            update(composition_proposals_table)
            .where(composition_proposals_table.c.session_id == str(fence.session_id))
            .where(composition_proposals_table.c.id == proposal_id)
            .where(composition_proposals_table.c.status == "pending")
            .values(
                status="rejected",
                committed_state_id=None,
                audit_event_id=event_id,
                updated_at=created_at,
            )
        )
        if updated.rowcount != 1:
            raise AuditIntegrityError("guided proposal rejection lost the pending proposal CAS")
        return event_id

    def record_pending_proposal_acceptance(
        self,
        *,
        authority: AuthoritativePipelineProposal,
        actor: str,
        created_at: datetime,
        committed_state_id: str,
        state_content_hash: str,
        committed_state: CompositionStateData,
        dispatch: PipelineDispatchAuditBinding,
    ) -> str:
        """Terminal ``proposal.accepted`` event and pending->committed CAS for the proposal this operation holds.

        The caller has already proved the durable dispatch and inserted
        ``committed_state`` (whose composer metadata the event commits to) in
        this transaction. Returns the event id.
        """
        _service, connection, fence, _row, _database_now = self.__state._require_exact()
        if type(authority) is not AuthoritativePipelineProposal:
            raise TypeError("authority must be an exact AuthoritativePipelineProposal")
        if authority.row.session_id != fence.session_id or authority.row.status != "pending":
            raise AuditIntegrityError("guided pending proposal authority is not exact for this session")
        if type(created_at) is not datetime:
            raise TypeError("created_at must be an exact datetime")
        if type(committed_state) is not CompositionStateData:
            raise TypeError("committed_state must be an exact CompositionStateData")
        proposal_id = str(authority.row.id)
        event_id = str(uuid.uuid4())
        terminal_payload = _pipeline_accepted_payload(
            authority=authority,
            state_id=committed_state_id,
            state_content_hash=state_content_hash,
            final_composer_metadata=committed_state.composer_meta,
            dispatch=dispatch,
        )
        connection.execute(
            insert(proposal_events_table).values(
                id=event_id,
                session_id=str(fence.session_id),
                proposal_id=proposal_id,
                event_type="proposal.accepted",
                actor=actor,
                payload=terminal_payload,
                created_at=created_at,
            )
        )
        updated = connection.execute(
            update(composition_proposals_table)
            .where(composition_proposals_table.c.session_id == str(fence.session_id))
            .where(composition_proposals_table.c.id == proposal_id)
            .where(composition_proposals_table.c.status == "pending")
            .values(
                status="committed",
                committed_state_id=committed_state_id,
                audit_event_id=event_id,
                updated_at=created_at,
            )
        )
        if updated.rowcount != 1:
            raise AuditIntegrityError("guided proposal acceptance lost the pending proposal CAS")
        return event_id


@final
class _GuidedSessionMutationTransaction:
    """Capability composition with no raw database handle on its surface."""

    __slots__ = ("__composer", "__guided", "__interpretation_state", "__interpretations", "__state")

    def __init__(
        self,
        service: SessionServiceImpl,
        connection: Connection,
        *,
        guided_fence: GuidedOperationFence,
        session_operation_context: SessionOperationContext,
    ) -> None:
        state = _GuidedSessionMutationState(
            service,
            connection,
            guided_fence=guided_fence,
            session_operation_context=session_operation_context,
        )
        self.__state = state
        self.__guided = _GuidedSessionMutations(state)
        self.__composer = _GuidedComposerMutations(state)
        _service, _connection, fence, _row, database_now = state._require_exact()
        self.__interpretation_state = _RepositoryMutationState(
            connection,
            session_id=str(fence.session_id),
            database_now=database_now,
            operation_context=session_operation_context,
        )
        self.__interpretations = _RepositoryInterpretationMutations(self.__interpretation_state)

    @property
    def guided(self) -> _GuidedSessionMutations:
        self.__state._require_active()
        return self.__guided

    @property
    def composer(self) -> _GuidedComposerMutations:
        self.__state._require_active()
        return self.__composer

    @property
    def interpretations(self) -> SessionOperationInterpretationMutations:
        self.__state._require_exact()
        self.__interpretation_state._require_active()
        return self.__interpretations

    def _close(self) -> None:
        self.__interpretation_state._close()
        self.__state._close()
