"""Proposal preparation retains the service-owned operation through blob reads."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
import structlog
from sqlalchemy import Engine

from elspeth.contracts.blobs import InlineCustodyRequest
from elspeth.contracts.composer_audit import ComposerToolStatus, ToolArgumentErrorCategory
from elspeth.contracts.enums import CreationModality
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.session_operation import SessionOperationContext, SessionOperationKind
from elspeth.core.canonical import stable_hash
from elspeth.web.blobs.service import BlobServiceImpl
from elspeth.web.catalog.policy_view import PolicyCatalogView
from elspeth.web.composer import pipeline_commit
from elspeth.web.composer.audit import BufferingRecorder
from elspeth.web.composer.authority_hashing import composer_authority_hash
from elspeth.web.composer.pipeline_commit import PipelineCommitConfig, PreparedPipelineCommit, prepare_pipeline_proposal_commit
from elspeth.web.composer.pipeline_planner import PipelinePlanResult
from elspeth.web.composer.pipeline_proposal import AbsentBase, PipelineProposal
from elspeth.web.composer.protocol import ToolArgumentError
from elspeth.web.composer.redaction import redact_tool_call_arguments
from elspeth.web.composer.redaction_telemetry import NoopRedactionTelemetry
from elspeth.web.composer.state import CompositionState, PipelineMetadata
from elspeth.web.coordination.contracts import SessionOperationFenceLost
from elspeth.web.dependencies import create_catalog_service
from elspeth.web.plugin_policy.models import PluginAvailabilitySnapshot
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.protocol import AuthoritativePipelineProposal
from elspeth.web.sessions.schema import initialize_session_schema
from elspeth.web.sessions.service import SessionServiceImpl
from elspeth.web.sessions.telemetry import build_sessions_telemetry


@dataclass(frozen=True)
class _Proposal:
    service: SessionServiceImpl
    engine: Engine
    authority: AuthoritativePipelineProposal
    policy: PolicyCatalogView
    snapshot: PluginAvailabilitySnapshot
    root: Path


@pytest_asyncio.fixture
async def proposal(tmp_path: Path) -> AsyncIterator[_Proposal]:
    engine = create_session_engine(f"sqlite:///{tmp_path / 'sessions.db'}", connect_args={"check_same_thread": False})
    initialize_session_schema(engine)
    from tests.fixtures.identities import ensure_test_identity

    with engine.begin() as conn:
        ensure_test_identity(conn, identity_id="alice")
    service = SessionServiceImpl(
        engine, data_dir=tmp_path, telemetry=build_sessions_telemetry(), log=structlog.get_logger("proposal-authority")
    )
    operations = service.session_operation_authority
    try:
        session = await service.create_session(user_id="alice", title="Blob proposal", auth_provider_type="local")
        create_context = operations.acquire(
            session_id=session.id,
            operation_kind=SessionOperationKind.COMPOSE,
            owner_instance_id=service.session_operation_owner_instance_id,
            lease_seconds=300,
        )
        try:
            message = await service.add_message(
                session.id,
                "user",
                "Prepare a one-row CSV example.",
                writer_principal="route_user_message",
                session_operation_context=create_context,
            )
            blob = await BlobServiceImpl(engine, tmp_path, session_operation_authority=operations).reserve_inline_custody(
                InlineCustodyRequest(
                    session_id=session.id,
                    filename="input.csv",
                    content=b"value\n1\n",
                    mime_type="text/csv",
                    source_description="Test planner example",
                    creation_modality=CreationModality.LLM_GENERATED,
                    created_from_message_id=str(message.id),
                    creating_model_identifier="test-model",
                    creating_model_version="v1",
                    creating_provider="test",
                    creating_composer_skill_hash=stable_hash("test-skill"),
                    creating_arguments_hash=stable_hash("example-arguments"),
                ),
                session_operation_context=create_context,
            )
        finally:
            operations.release(create_context)
        arguments = {
            "source": {
                "plugin": "csv",
                "on_success": "rows",
                "on_validation_failure": "discard",
                "blob_id": str(blob.id),
                "options": {"schema": {"mode": "observed"}},
            },
            "nodes": [],
            "edges": [],
            "outputs": [
                {
                    "sink_name": "rows",
                    "plugin": "json",
                    "on_write_failure": "discard",
                    "options": {
                        "path": str(tmp_path / "outputs" / str(session.id) / "result.jsonl"),
                        "schema": {"mode": "observed"},
                        "format": "jsonl",
                        "mode": "write",
                        "collision_policy": "auto_increment",
                    },
                }
            ],
        }
        plan = PipelinePlanResult(
            proposal=PipelineProposal.create(
                pipeline=arguments,
                base=AbsentBase(),
                repair_count=0,
                skill_hash=stable_hash("test-skill"),
            ),
            tool_call_id="proposal-call",
            custody_result="not_required",
            model_identifier="test-model",
            model_version="v1",
            provider="test",
        )
        compose_context = operations.acquire(
            session_id=session.id,
            operation_kind=SessionOperationKind.COMPOSE,
            owner_instance_id=service.session_operation_owner_instance_id,
            lease_seconds=300,
        )
        try:
            row = await service.create_pipeline_composition_proposal(
                session_id=session.id,
                plan=plan,
                summary="Review the uploaded source.",
                rationale="User requested pipeline.",
                affects=("graph", "validation"),
                actor="composer-web:user:alice",
                arguments_redacted_json=redact_tool_call_arguments("set_pipeline", arguments, telemetry=NoopRedactionTelemetry()),
                composer_model_identifier="test-model",
                composer_model_version="v1",
                composer_provider="test",
                session_operation_context=compose_context,
            )
        finally:
            operations.release(compose_context)
        authority = await service.get_authoritative_pipeline_proposal(session_id=session.id, proposal_id=row.id)
        catalog = create_catalog_service()
        snapshot = PluginAvailabilitySnapshot.for_trained_operator(catalog)
        yield _Proposal(service, engine, authority, PolicyCatalogView.for_trained_operator(catalog, snapshot), snapshot, tmp_path)
    finally:
        engine.dispose()


async def _prepare(
    proposal: _Proposal,
    context: SessionOperationContext,
    *,
    recorder: BufferingRecorder | None = None,
) -> PreparedPipelineCommit:
    result = await prepare_pipeline_proposal_commit(
        authority=proposal.authority,
        current_state=CompositionState(source=None, nodes=(), edges=(), outputs=(), metadata=PipelineMetadata(), version=1),
        current_state_id=None,
        policy_catalog=proposal.policy,
        plugin_snapshot=proposal.snapshot,
        config=PipelineCommitConfig(
            data_dir=str(proposal.root),
            session_engine=proposal.engine,
            session_operation_context=context,
            session_operation_authority=proposal.service.session_operation_authority,
            secret_service=None,
            user_id="alice",
            user_message_content=None,
            max_blob_storage_per_session_bytes=1_000_000,
            runtime_preflight=None,
            timeout_seconds=5.0,
        ),
        recorder=recorder if recorder is not None else BufferingRecorder(),
        actor="user:alice",
    )
    assert isinstance(result, PreparedPipelineCommit)
    return result


@pytest.mark.asyncio
@pytest.mark.parametrize("operation_kind", [SessionOperationKind.PROPOSAL, SessionOperationKind.COMPOSE])
async def test_candidate_and_executor_keep_the_exact_live_operation(
    proposal: _Proposal,
    monkeypatch: pytest.MonkeyPatch,
    operation_kind: SessionOperationKind,
) -> None:
    operations = proposal.service.session_operation_authority
    context = operations.acquire(
        session_id=proposal.authority.row.session_id,
        operation_kind=operation_kind,
        owner_instance_id=proposal.service.session_operation_owner_instance_id,
        lease_seconds=300,
    )
    candidate = pipeline_commit.build_set_pipeline_candidate
    execute = pipeline_commit.execute_tool
    observed: list[str] = []

    def observe_candidate(*args: Any, **kwargs: Any) -> Any:
        assert args[2].session_operation_context is context
        assert args[2].session_operation_authority is operations
        observed.append("candidate")
        return candidate(*args, **kwargs)

    def observe_execute(*args: Any, **kwargs: Any) -> Any:
        assert kwargs["session_operation_context"] is context
        assert kwargs["session_operation_authority"] is operations
        observed.append("executor")
        return execute(*args, **kwargs)

    monkeypatch.setattr(pipeline_commit, "build_set_pipeline_candidate", observe_candidate)
    monkeypatch.setattr(pipeline_commit, "execute_tool", observe_execute)
    try:
        prepared = await _prepare(proposal, context)
        assert observed == ["candidate", "executor"]
        assert prepared.result.success
        assert prepared.candidate_content_hash == prepared.executor_content_hash
        operations.compare_and_swap(context)
    finally:
        operations.release(context)


@pytest.mark.asyncio
async def test_released_proposal_operation_cannot_be_reacquired_by_preparation(proposal: _Proposal) -> None:
    operations = proposal.service.session_operation_authority
    context = operations.acquire(
        session_id=proposal.authority.row.session_id,
        operation_kind=SessionOperationKind.PROPOSAL,
        owner_instance_id=proposal.service.session_operation_owner_instance_id,
        lease_seconds=300,
    )
    operations.release(context)
    with pytest.raises(SessionOperationFenceLost):
        await _prepare(proposal, context)
    assert await proposal.service.get_current_state(proposal.authority.row.session_id) is None


@pytest.mark.asyncio
async def test_other_sessions_live_operation_cannot_prepare_this_proposal(proposal: _Proposal) -> None:
    other = await proposal.service.create_session(user_id="alice", title="Other session", auth_provider_type="local")
    operations = proposal.service.session_operation_authority
    context = operations.acquire(
        session_id=other.id,
        operation_kind=SessionOperationKind.PROPOSAL,
        owner_instance_id=proposal.service.session_operation_owner_instance_id,
        lease_seconds=300,
    )
    try:
        with pytest.raises(AuditIntegrityError, match="does not match the proposal session"):
            await _prepare(proposal, context)
    finally:
        operations.release(context)


def _coalesce_arguments(branch_order: tuple[str, ...]) -> dict[str, Any]:
    connections = {"a": "a_in", "b": "b_in", "c": "c_in"}
    return {
        "source": {"plugin": "csv", "on_success": "rows", "options": {"path": "cases.csv"}, "on_validation_failure": "discard"},
        "nodes": [
            {
                "id": "merge",
                "node_type": "coalesce",
                "plugin": None,
                "input": "a_in",
                "on_success": "merge_out",
                "on_error": None,
                "options": {},
                "branches": {alias: connections[alias] for alias in branch_order},
                "policy": "require_all",
                "merge": "union",
            }
        ],
        "edges": [],
        "outputs": [],
    }


@pytest.mark.asyncio
async def test_preparation_rejects_proposal_whose_coalesce_order_differs_from_the_row(proposal: _Proposal) -> None:
    """The row's ``tool_arguments_hash`` binds coalesce map order at commit preparation."""
    row_arguments = _coalesce_arguments(("a", "b", "c"))
    reordered = _coalesce_arguments(("a", "c", "b"))
    authority = replace(
        proposal.authority,
        row=replace(proposal.authority.row, tool_arguments_hash=composer_authority_hash(row_arguments)),
        proposal=PipelineProposal.create(
            pipeline=reordered,
            base=AbsentBase(),
            repair_count=0,
            skill_hash=stable_hash("test-skill"),
        ),
    )
    operations = proposal.service.session_operation_authority
    context = operations.acquire(
        session_id=proposal.authority.row.session_id,
        operation_kind=SessionOperationKind.PROPOSAL,
        owner_instance_id=proposal.service.session_operation_owner_instance_id,
        lease_seconds=300,
    )
    try:
        with pytest.raises(AuditIntegrityError, match="authoritative pipeline arguments do not match the proposal row"):
            await _prepare(replace(proposal, authority=authority), context)
    finally:
        operations.release(context)


@pytest.mark.asyncio
async def test_executor_argument_error_persists_its_category_as_the_error_code(
    proposal: _Proposal,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The commit dispatch's ARG_ERROR payload carries the closed category.

    ``error_code`` is the ``ToolArgumentErrorCategory`` value (a registered
    code), not the exception's own ``code`` (``SCHEMA_VALIDATION``) or a
    generic ``argument_error``; ``error_class`` is the class raised.
    """

    def reject_schema(*_args: Any, **_kwargs: Any) -> Any:
        raise ToolArgumentError(
            argument="set_pipeline arguments",
            expected="object conforming to SetPipelineArgumentsModel (arguments contains unsupported properties)",
            actual_type="invalid_schema",
            code="SCHEMA_VALIDATION",
            category=ToolArgumentErrorCategory.SCHEMA_SHAPE,
        )

    monkeypatch.setattr(pipeline_commit, "execute_tool", reject_schema)
    operations = proposal.service.session_operation_authority
    context = operations.acquire(
        session_id=proposal.authority.row.session_id,
        operation_kind=SessionOperationKind.PROPOSAL,
        owner_instance_id=proposal.service.session_operation_owner_instance_id,
        lease_seconds=300,
    )
    recorder = BufferingRecorder()
    try:
        with pytest.raises(ToolArgumentError):
            await _prepare(proposal, context, recorder=recorder)
    finally:
        operations.release(context)

    assert len(recorder.invocations) == 1
    invocation = recorder.invocations[0]
    assert invocation.status is ComposerToolStatus.ARG_ERROR
    assert (invocation.error_class, invocation.error_category) == ("ToolArgumentError", ToolArgumentErrorCategory.SCHEMA_SHAPE)
    assert invocation.result_canonical is not None
    assert json.loads(invocation.result_canonical) == {"error_class": "ToolArgumentError", "error_code": "schema_shape"}
