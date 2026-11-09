"""Real-loop coverage for explicit-approval ``set_pipeline`` prevalidation."""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, func, insert, select
from sqlalchemy.pool import StaticPool

from elspeth.contracts.composer_audit import ComposerToolStatus
from elspeth.contracts.composer_llm_audit import ToolContractDialect
from elspeth.contracts.freeze import deep_thaw
from elspeth.contracts.prompt_guard import PromptGuardContentPart, PromptGuardToolArgumentParts
from elspeth.contracts.secrets import ResolvedSecret, SecretInventoryItem, WebSecretResolver
from elspeth.contracts.session_operation import SessionOperationKind
from elspeth.core.canonical import canonical_json, stable_hash
from elspeth.web.blobs.protocol import BlobPendingProposalError
from elspeth.web.blobs.service import BlobServiceImpl
from elspeth.web.catalog.policy_view import PolicyCatalogView
from elspeth.web.catalog.protocol import CatalogService
from elspeth.web.composer._compose_loop_carriers import _AdmittedLLMCompletion
from elspeth.web.composer.advisor_checkpoint import AdvisorCheckpointVerdict
from elspeth.web.composer.anti_anchor import AntiAnchorTracker
from elspeth.web.composer.audit import BufferingRecorder
from elspeth.web.composer.authority_hashing import composer_authority_hash
from elspeth.web.composer.diagnostic_evidence import (
    ComposerDiagnosticEvidence,
    diagnostic_evidence_identity,
    is_diagnostic_evidence_envelope,
)
from elspeth.web.composer.pipeline_custody import inline_custody_audit_projection
from elspeth.web.composer.protocol import ComposerConvergenceError, ComposerPluginCrashError
from elspeth.web.composer.provider_gateway import _admit_composer_llm_completion
from elspeth.web.composer.service import ComposerAvailability, ComposerServiceImpl
from elspeth.web.composer.state import CompositionState, PipelineMetadata, SourceSpec, ValidationEntry, ValidationSummary
from elspeth.web.composer.tools import ToolResult
from elspeth.web.composer.tools import execute_tool as real_execute_tool
from elspeth.web.composer.tools.sessions import build_set_pipeline_candidate as real_build_set_pipeline_candidate
from elspeth.web.composer.tools.wire_projection import encode_semantic_arguments
from elspeth.web.coordination.lifecycle import SessionOperationLease
from elspeth.web.dependencies import create_catalog_service
from elspeth.web.interpretation_state import INTERPRETATION_REQUIREMENTS_KEY
from elspeth.web.plugin_policy.models import PluginAvailabilitySnapshot
from elspeth.web.plugin_policy.validation import ProfileAwareValidationResult
from elspeth.web.secrets.wiring_policy import SecretWiringRuleSettings
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.models import (
    blobs_table,
    chat_messages_table,
    composition_proposals_table,
    composition_states_table,
    proposal_events_table,
    session_operation_fences_table,
    sessions_table,
)
from elspeth.web.sessions.schema import initialize_session_schema
from elspeth.web.sessions.service import SessionServiceImpl
from tests.fixtures.identities import ensure_test_identity, grant_test_pipeline_user
from tests.unit.web.composer.conftest import (
    _fake_llm_response as _raw_fake_llm_response,
)
from tests.unit.web.composer.conftest import (
    _make_settings,
    build_test_sessions_service,
)

_PROPOSAL_SESSION_ID = "00000000-0000-4000-8000-000000000001"


@dataclass(frozen=True, slots=True)
class _Harness:
    engine: Engine
    sessions: SessionServiceImpl
    service: ComposerServiceImpl
    catalog: CatalogService
    session_id: str
    user_message_id: str


class _ScriptedLLM:
    def __init__(self, *responses: _AdmittedLLMCompletion) -> None:
        self._responses = list(responses)
        self.message_snapshots: list[list[dict[str, Any]]] = []

    async def __call__(
        self,
        messages: list[dict[str, Any]],
        _tools: Any,
        *,
        provider_custody: object | None = None,
        credential_state: object | None = None,
        prompt_guard_trusted_message_indexes: frozenset[int] = frozenset(),
        prompt_guard_content_parts: tuple[tuple[PromptGuardContentPart, ...] | None, ...] | None = None,
        prompt_guard_tool_argument_parts: tuple[tuple[PromptGuardToolArgumentParts, ...] | None, ...] | None = None,
    ) -> _AdmittedLLMCompletion:
        self.message_snapshots.append(deepcopy(messages))
        if not self._responses:
            return _fake_llm_response(content="Done.")
        return self._responses.pop(0)


def _fake_llm_response(*, content: str | None = None, tool_calls: tuple[dict[str, Any], ...] = ()) -> _AdmittedLLMCompletion:
    return _admit_composer_llm_completion(_raw_fake_llm_response(content=content, tool_calls=tool_calls))


async def _clean_advisor_checkpoint(*_args: object, **_kwargs: object) -> AdvisorCheckpointVerdict:
    """Keep proposal-prevalidation tests independent of the real advisor."""
    if _kwargs["phase"] == "end":
        from elspeth.web.composer.turn_effects import AdvisorPublicationReview

        review = _kwargs["publication_review"]
        assert type(review) is AdvisorPublicationReview
        return AdvisorCheckpointVerdict(
            ok=True, blocking=False, findings_text="CLEAN", publication_verdict="ADMIT", publication_binding=review.binding
        )
    return AdvisorCheckpointVerdict(ok=True, blocking=False, findings_text="CLEAN")


class _FinalRejectingCatalog(PolicyCatalogView):
    """Request catalog that rejects one otherwise-valid complete candidate."""

    def validate_composition_state(self, state: CompositionState) -> ProfileAwareValidationResult:
        raw = state.validate()
        if raw.is_valid and state.metadata.name == "final-profile-reject":
            raw = ValidationSummary(
                is_valid=False,
                errors=(
                    ValidationEntry(
                        component="policy:final",
                        message="The request-scoped profile rejects this complete composition.",
                        severity="high",
                        error_code="profile_complete_state_rejected",
                    ),
                ),
                warnings=raw.warnings,
                suggestions=raw.suggestions,
                edge_contracts=raw.edge_contracts,
                semantic_contracts=raw.semantic_contracts,
            )
        return ProfileAwareValidationResult(
            authored_state=state,
            executable_state=state,
            policy_findings=(),
            validation=raw,
        )


class _PreproposalBaseSignal(BaseException):
    """Test-only shutdown-style signal outside the ``Exception`` hierarchy."""


def _incremental_base_state(tmp_path: Path) -> CompositionState:
    """Non-empty base keeps these legacy incremental-prevalidation tests scoped."""
    return CompositionState(
        source=SourceSpec(
            plugin="csv",
            on_success="existing_rows",
            options={
                "path": str(tmp_path / "blobs" / _PROPOSAL_SESSION_ID / "existing.csv"),
                "schema": {"mode": "observed"},
            },
            on_validation_failure="discard",
        ),
        nodes=(),
        edges=(),
        outputs=(),
        metadata=PipelineMetadata(),
        version=1,
    )


def _harness(
    tmp_path: Path,
    *,
    secret_service: WebSecretResolver | None = None,
    secret_rules: tuple[SecretWiringRuleSettings, ...] = (),
) -> _Harness:
    engine = create_session_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    initialize_session_schema(engine)
    with engine.begin() as conn:
        ensure_test_identity(conn, identity_id="proposal-prevalidation-user")
        grant_test_pipeline_user(conn, identity_id="proposal-prevalidation-user")
    sessions = build_test_sessions_service(engine=engine, data_dir=tmp_path)
    session_id = _PROPOSAL_SESSION_ID
    user_message_id = str(uuid4())
    now = datetime.now(UTC)
    with engine.begin() as conn:
        conn.execute(
            insert(sessions_table).values(
                id=session_id,
                user_id="proposal-prevalidation-user",
                auth_provider_type="local",
                title="Proposal prevalidation",
                trust_mode="explicit_approve",
                density_default="high",
                created_at=now,
                updated_at=now,
            )
        )
        conn.execute(
            insert(chat_messages_table).values(
                id=user_message_id,
                session_id=session_id,
                role="user",
                content="Build a reviewed pipeline.",
                raw_content=None,
                tool_calls=None,
                tool_call_id=None,
                sequence_no=1,
                writer_principal="route_user_message",
                created_at=now,
                composition_state_id=None,
                parent_assistant_id=None,
            )
        )
    catalog = create_catalog_service()
    with patch(
        "elspeth.web.composer.service.compute_availability",
        return_value=ComposerAvailability(available=True, model="test-model", provider="test"),
    ):
        service = ComposerServiceImpl.for_trained_operator(
            catalog=catalog,
            settings=_make_settings(tmp_path, secret_wiring_allowlist=secret_rules),
            sessions_service=sessions,
            session_engine=engine,
            secret_service=secret_service,
        )
    service._advisor_checkpoint._run_advisor_checkpoint = _clean_advisor_checkpoint  # type: ignore[method-assign]
    return _Harness(
        engine=engine,
        sessions=sessions,
        service=service,
        catalog=catalog,
        session_id=session_id,
        user_message_id=user_message_id,
    )


def _valid_pipeline_args(tmp_path: Path, *, metadata_name: str = "proposal-valid") -> dict[str, Any]:
    source_path = tmp_path / "blobs" / _PROPOSAL_SESSION_ID / "input.csv"
    source_path.parent.mkdir(parents=True, exist_ok=True)
    source_path.write_text("value\n1\n", encoding="utf-8")
    return {
        "source": {
            "plugin": "csv",
            "on_success": "main",
            "options": {"path": str(source_path), "schema": {"mode": "observed"}},
            "on_validation_failure": "discard",
        },
        "nodes": [],
        "edges": [],
        "outputs": [
            {
                "sink_name": "main",
                "plugin": "json",
                "options": {
                    "path": str(tmp_path / "outputs" / _PROPOSAL_SESSION_ID / "result.jsonl"),
                    "schema": {"mode": "observed"},
                    "format": "jsonl",
                    "mode": "write",
                    "collision_policy": "auto_increment",
                },
                "on_write_failure": "discard",
            }
        ],
        "metadata": {"name": metadata_name},
    }


def _tool_turn(call_id: str, tool_name: str, arguments: dict[str, Any]) -> _AdmittedLLMCompletion:
    return _fake_llm_response(
        tool_calls=(
            {
                "id": call_id,
                "name": tool_name,
                "arguments": arguments,
            },
        )
    )


def _inline_pipeline_args(tmp_path: Path) -> dict[str, Any]:
    args = _valid_pipeline_args(tmp_path, metadata_name="inline-proposal")
    args["source"] = {
        "plugin": "csv",
        "on_success": "main",
        "options": {"schema": {"mode": "observed"}},
        "on_validation_failure": "discard",
        "inline_blob": {
            "filename": "candidate.csv",
            "mime_type": "text/csv",
            "content": "value\n42\n",
            "description": "Prepared only for a pending proposal",
        },
    }
    return args


def _count_rows(engine: Engine, table: Any) -> int:
    with engine.connect() as conn:
        return int(conn.execute(select(func.count()).select_from(table)).scalar_one())


def _persisted_tool_content(harness: _Harness, tool_call_id: str) -> str:
    with harness.engine.connect() as conn:
        return str(
            conn.execute(
                select(chat_messages_table.c.content)
                .where(chat_messages_table.c.session_id == harness.session_id)
                .where(chat_messages_table.c.role == "tool")
                .where(chat_messages_table.c.tool_call_id == tool_call_id)
            ).scalar_one()
        )


@contextmanager
def _record_phase3_outcomes(service: ComposerServiceImpl) -> Iterator[list[tuple[Any, ...]]]:
    """Capture each real dispatch result without storing test state on the service."""
    batches: list[tuple[Any, ...]] = []
    dispatch = service._dispatch_tool_batch

    async def record(*args: Any, **kwargs: Any) -> Any:
        result = await dispatch(*args, **kwargs)
        batches.append(result[0].tool_outcomes)
        return result

    with patch.object(service, "_dispatch_tool_batch", new=record):
        yield batches


@pytest.mark.asyncio
async def test_semantic_rejection_reaches_next_model_turn_then_repair_creates_one_proposal(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    state = _incremental_base_state(tmp_path)
    invalid = _valid_pipeline_args(tmp_path, metadata_name="invalid-plugin")
    invalid["source"]["plugin"] = "uninstalled_canary_plugin"
    repaired = _valid_pipeline_args(tmp_path, metadata_name="repaired")
    llm = _ScriptedLLM(
        _tool_turn("call_invalid", "set_pipeline", invalid),
        _tool_turn("call_repaired", "set_pipeline", repaired),
        _fake_llm_response(content="The repaired proposal is pending approval."),
    )

    with (
        patch.object(harness.service._provider_gateway, "_call_llm", new=llm),
        _record_phase3_outcomes(harness.service) as outcome_batches,
    ):
        result = await harness.service.compose(
            "Build a reviewed pipeline.",
            [],
            state,
            session_id=harness.session_id,
            user_id="proposal-prevalidation-user",
            user_message_id=harness.user_message_id,
        )

    proposals = await harness.sessions.list_composition_proposals(UUID(harness.session_id))
    assert len(proposals) == 1
    assert proposals[0].tool_call_id == "call_repaired"
    assert result.state is state
    assert _count_rows(harness.engine, composition_proposals_table) == 1
    assert _count_rows(harness.engine, composition_states_table) == 0
    assert _count_rows(harness.engine, blobs_table) == 0

    proposal_outcome = outcome_batches[-1][0]
    assert isinstance(proposal_outcome.response, ToolResult)
    assert proposal_outcome.post_version == state.version

    second_call_tools = [message for message in llm.message_snapshots[1] if message.get("role") == "tool"]
    assert second_call_tools
    invalid_feedback = next(message for message in second_call_tools if message["tool_call_id"] == "call_invalid")
    invalid_payload = json.loads(invalid_feedback["content"])
    assert invalid_payload["success"] is False
    assert invalid_payload["version"] == state.version
    # A payload-free rejection acquires only independent proposal-status fields.
    # Its message and code remain in validation, including after serialization.
    assert invalid_payload["validation"]["errors"][0]["error_code"] == "plugin_not_installed"
    assert "candidate_data" not in invalid_payload["data"]
    assert set(invalid_payload["data"]) == {"status", "applied", "candidate_version", "message"}
    assert invalid_payload["data"]["status"] == "PREVALIDATION_REJECTED"
    assert invalid_payload["data"]["applied"] is False
    assert invalid_payload["validation"]["errors"][0]["component"] == "rejected_mutation"
    persisted_invalid_feedback = _persisted_tool_content(harness, "call_invalid")
    # Positive control: the model-authored plugin name is present in the
    # unredacted in-memory audit of the rejected call ...
    invalid_invocation = next(inv for inv in result.tool_invocations if inv.tool_call_id == "call_invalid")
    assert "uninstalled_canary_plugin" in invalid_invocation.arguments_canonical
    # ... and never reaches the persisted tool row. The rejection message stays
    # redacted; the closed, registered rejection code persists (S0: error_code
    # survives redaction; it used to be summarized to <redacted-response-text>).
    assert "uninstalled_canary_plugin" not in persisted_invalid_feedback
    assert json.loads(persisted_invalid_feedback)["validation"]["errors"][0]["error_code"] == "plugin_not_installed"

    assert len(result.tool_invocations) == 2
    assert result.tool_invocations[0].version_after == state.version
    assert result.tool_invocations[1].version_after == state.version


@pytest.mark.asyncio
async def test_postbuilder_inline_custody_quota_refusal_preserves_state_and_candidate_feedback(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    state = _incremental_base_state(tmp_path)
    assert state.validate().errors
    arguments = _inline_pipeline_args(tmp_path)
    llm = _ScriptedLLM(
        _tool_turn("call_quota", "set_pipeline", arguments),
        _fake_llm_response(content="The inline candidate needs available session storage before retrying."),
    )
    files_before = tuple(sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*") if path.is_file()))
    with (
        patch.object(harness.service._provider_gateway, "_call_llm", new=llm),
        patch("elspeth.web.composer.tool_batch._try_finalize_proposal_custody", autospec=True, return_value="quota_exceeded") as settle,
    ):
        result = await harness.service.compose(
            "Build a reviewed inline pipeline.",
            [],
            state,
            session_id=harness.session_id,
            user_id="proposal-prevalidation-user",
            user_message_id=harness.user_message_id,
        )
    settle.assert_awaited_once()
    assert result.state is state
    assert result.state.version == state.version
    assert _count_rows(harness.engine, composition_proposals_table) == 0
    assert _count_rows(harness.engine, composition_states_table) == 0
    assert _count_rows(harness.engine, blobs_table) == 0
    assert tuple(sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*") if path.is_file())) == files_before
    assert len(llm.message_snapshots) == 2
    message = next(message for message in llm.message_snapshots[1] if message.get("tool_call_id") == "call_quota")
    payload = json.loads(message["content"])
    assert payload["success"] is False
    assert payload["version"] == state.version
    assert payload["affected_nodes"] == []
    assert "validation_delta" not in payload
    assert [(entry["error_code"], entry["rejected_component"]) for entry in payload["validation"]["errors"]] == [
        ("BLOB_QUOTA_EXCEEDED", "source")
    ]
    assert payload["data"]["status"] == "PREVALIDATION_REJECTED"
    persisted = json.loads(_persisted_tool_content(harness, "call_quota"))
    assert persisted["validation"]["errors"][0]["error_code"] == "BLOB_QUOTA_EXCEEDED"


@pytest.mark.asyncio
async def test_final_profile_rejection_is_unapplied_audited_and_repairable(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    state = _incremental_base_state(tmp_path)
    invalid = _valid_pipeline_args(tmp_path, metadata_name="final-profile-reject")
    repaired = _valid_pipeline_args(tmp_path, metadata_name="profile-repaired")
    snapshot = PluginAvailabilitySnapshot.for_trained_operator(harness.catalog)
    catalog = _FinalRejectingCatalog.for_trained_operator(harness.catalog, snapshot)
    failures: list[tuple[str, str]] = []
    message_snapshots: list[list[dict[str, Any]]] = []
    responses = [
        _tool_turn("call_final_invalid", "set_pipeline", invalid),
        _tool_turn("call_final_repaired", "set_pipeline", repaired),
        _fake_llm_response(content="The repaired proposal is pending approval."),
    ]
    files_before = tuple(sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*") if path.is_file()))
    original_record_failure = AntiAnchorTracker.record_failure
    progress_events = []

    async def record_progress(event: Any) -> None:
        progress_events.append(event)

    def _record_failure(tracker: AntiAnchorTracker, tool_name: str, arguments_hash: str) -> None:
        failures.append((tool_name, arguments_hash))
        original_record_failure(tracker, tool_name, arguments_hash)

    async def _llm(
        messages: list[dict[str, Any]],
        _tools: Any,
        *,
        provider_custody: object | None = None,
        credential_state: object | None = None,
        prompt_guard_trusted_message_indexes: frozenset[int] = frozenset(),
        prompt_guard_content_parts: tuple[tuple[PromptGuardContentPart, ...] | None, ...] | None = None,
        prompt_guard_tool_argument_parts: tuple[tuple[PromptGuardToolArgumentParts, ...] | None, ...] | None = None,
    ) -> Any:
        message_snapshots.append(deepcopy(messages))
        if len(message_snapshots) == 2:
            assert _count_rows(harness.engine, composition_proposals_table) == 0
            assert _count_rows(harness.engine, composition_states_table) == 0
            assert _count_rows(harness.engine, blobs_table) == 0
            assert builder.call_count == 1
        return responses.pop(0)

    with (
        patch.object(harness.service._policy_context, "build", return_value=(snapshot, catalog)),
        patch.object(harness.service._provider_gateway, "_call_llm", new=_llm),
        patch.object(AntiAnchorTracker, "record_failure", new=_record_failure),
        patch(
            "elspeth.web.composer.tool_batch.build_set_pipeline_candidate",
            wraps=real_build_set_pipeline_candidate,
        ) as builder,
    ):
        result = await harness.service.compose(
            "Build a reviewed pipeline.",
            [],
            state,
            session_id=harness.session_id,
            user_id="proposal-prevalidation-user",
            user_message_id=harness.user_message_id,
            progress=record_progress,
        )

    proposals = await harness.sessions.list_composition_proposals(UUID(harness.session_id))
    assert len(proposals) == 1
    assert proposals[0].tool_call_id == "call_final_repaired"
    assert result.state is state
    assert builder.call_count == 2
    assert [tool_name for tool_name, _hash in failures] == ["set_pipeline"]
    assert any("visible blocker" in event.headline for event in progress_events)
    assert not any("has updated the pipeline" in event.headline for event in progress_events)

    invalid_feedback = next(message for message in message_snapshots[1] if message.get("tool_call_id") == "call_final_invalid")
    invalid_payload = json.loads(invalid_feedback["content"])
    assert invalid_payload["success"] is True
    assert invalid_payload["validation"]["is_valid"] is False
    assert invalid_payload["validation"]["errors"][0]["error_code"] == "profile_complete_state_rejected"
    assert invalid_payload["data"] == {
        "status": "PREVALIDATION_REJECTED",
        "applied": False,
        "candidate_version": state.version + 1,
        "message": (
            "The candidate pipeline failed prevalidation, was not applied, and was not submitted for approval. "
            "Repair the reported validation errors and retry."
        ),
    }
    # The envelope's version is the UNAPPLIED state, so it agrees with the audit
    # rather than contradicting it, and there is no `applied_version` twin under
    # `data` (SYS-R3-3). `candidate_version` is the one fact the envelope lacks.
    assert invalid_payload["version"] == state.version
    assert tuple(invocation.version_after for invocation in result.tool_invocations) == (state.version, state.version)
    assert invalid_payload["data"]["candidate_version"] == invalid_payload["version"] + 1
    # Nothing was applied, so no field may describe a change. Both of these came
    # from the DISCARDED candidate while ``version`` named the unchanged state:
    # ``affected_nodes`` listed the components the candidate would have touched,
    # and the delta against the candidate's prior validation reported
    # ``resolved_errors`` naming errors the unapplied state still has — the
    # field the skill tells the model to act on instead of re-reading state
    # (LLM seat LLM-R3B-1). This is now the APPROVAL_REQUIRED envelope's shape.
    assert invalid_payload["affected_nodes"] == []
    assert "validation_delta" not in invalid_payload
    assert set(invalid_payload) == {"success", "validation", "affected_nodes", "version", "data"}
    # ``validation`` deliberately stays the candidate's: it is the rejection the
    # model repairs from, and the skill now says whose it is. The unapplied
    # state has two errors of its OWN, which is what the departed delta used to
    # report as ``resolved_errors``.
    assert [error["error_code"] for error in invalid_payload["validation"]["errors"]] == ["profile_complete_state_rejected"]
    assert {entry.error_code for entry in state.validate().errors} == {"no_sinks_configured", "source_on_success_dangling"}
    files_after = tuple(sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*") if path.is_file()))
    assert files_after == files_before


@pytest.mark.asyncio
async def test_inline_candidate_materializes_one_custody_safe_proposal_without_raw_audit_surfaces(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    harness = _harness(tmp_path)
    existing_path = tmp_path / "blobs" / _PROPOSAL_SESSION_ID / "existing.csv"
    existing_path.parent.mkdir(parents=True, exist_ok=True)
    existing_path.write_text("value\n1\n", encoding="utf-8")
    state = CompositionState(
        source=SourceSpec(
            plugin="csv",
            on_success="main",
            options={"path": str(existing_path), "schema": {"mode": "observed"}},
            on_validation_failure="discard",
        ),
        nodes=(),
        edges=(),
        outputs=(),
        metadata=PipelineMetadata(name="existing non-empty pipeline"),
        version=4,
    )
    args = _inline_pipeline_args(tmp_path)
    raw_canary = "private-inline-value-7f45"
    raw_content = f"{raw_canary}\n42\n"
    args["source"]["inline_blob"]["content"] = raw_content
    raw_responses = [
        _raw_fake_llm_response(tool_calls=({"id": "call_inline", "name": "set_pipeline", "arguments": args},)),
        _raw_fake_llm_response(content="The inline pipeline proposal is pending approval."),
    ]
    message_snapshots: list[list[dict[str, Any]]] = []

    async def provider_completion(**kwargs: Any) -> Any:
        message_snapshots.append(deepcopy(kwargs["messages"]))
        return raw_responses.pop(0)

    seeded_at = datetime.now(UTC)
    with harness.engine.begin() as conn:
        conn.execute(
            insert(session_operation_fences_table).values(
                session_id=harness.session_id,
                operation_id=str(uuid4()),
                lease_token=f"seed-{uuid4()}",
                operation_kind=SessionOperationKind.CREATE.value,
                owner_instance_id="proposal-prevalidation-seed",
                operation_epoch=1,
                lease_expires_at=seeded_at,
                released_at=seeded_at,
            )
        )

    compose_lease = await SessionOperationLease.acquire(
        harness.sessions.session_operation_authority,
        session_id=UUID(harness.session_id),
        operation_kind=SessionOperationKind.COMPOSE,
        owner_instance_id=harness.sessions.session_operation_owner_instance_id,
        lease_seconds=harness.sessions.session_operation_lease_seconds,
    )
    try:
        with (
            patch("litellm.acompletion", new=provider_completion),
            patch(
                "elspeth.web.composer.tool_batch.build_set_pipeline_candidate",
                wraps=real_build_set_pipeline_candidate,
            ) as builder,
        ):
            result = await harness.service.compose(
                "Build a reviewed pipeline with generated CSV content.",
                [],
                state,
                session_id=harness.session_id,
                user_id="proposal-prevalidation-user",
                user_message_id=harness.user_message_id,
                session_operation_context=compose_lease.context,
            )
    finally:
        await compose_lease.close()

    proposals = await harness.sessions.list_composition_proposals(UUID(harness.session_id))
    assert len(proposals) == 1
    assert proposals[0].tool_call_id == "call_inline"
    assert proposals[0].status == "pending"
    safe_arguments = deep_thaw(proposals[0].arguments_json)
    safe_redacted_arguments = deep_thaw(proposals[0].arguments_redacted_json)
    assert "inline_blob" not in safe_arguments["source"]
    assert "inline_blob" not in safe_redacted_arguments["source"]
    assert UUID(safe_arguments["source"]["blob_id"]).version == 5
    assert safe_redacted_arguments["source"]["blob_id"] == safe_arguments["source"]["blob_id"]
    assert proposals[0].tool_arguments_hash == stable_hash(safe_arguments)
    assert builder.call_count == 3
    assert result.state is state
    assert _count_rows(harness.engine, blobs_table) == 1
    assert _count_rows(harness.engine, composition_states_table) == 0
    with harness.engine.connect() as conn:
        quota_bytes = conn.execute(
            select(func.coalesce(func.sum(blobs_table.c.size_bytes), 0)).where(blobs_table.c.session_id == harness.session_id)
        ).scalar_one()
        blob_row = conn.execute(select(blobs_table)).one()
        event_payloads = tuple(conn.execute(select(proposal_events_table.c.payload)).scalars())
        persisted_messages = tuple(
            conn.execute(
                select(chat_messages_table.c.content, chat_messages_table.c.raw_content, chat_messages_table.c.tool_calls).where(
                    chat_messages_table.c.session_id == harness.session_id
                )
            )
        )
    assert int(quota_bytes) == len(raw_content.encode("utf-8"))
    assert blob_row.id == safe_arguments["source"]["blob_id"]
    assert blob_row.creating_arguments_hash == stable_hash(safe_arguments)
    assert raw_canary not in json.dumps(event_payloads)
    private_rows = [row for row in persisted_messages if row.tool_calls and is_diagnostic_evidence_envelope(row.tool_calls[0])]
    public_rows = [row for row in persisted_messages if row not in private_rows]
    assert raw_canary not in json.dumps(tuple(tuple(row) for row in public_rows))
    assert len(private_rows) == 3
    evidence = [ComposerDiagnosticEvidence.from_envelope(row.tool_calls[0], expected_session_id=harness.session_id) for row in private_rows]
    by_identity = {item.evidence_id: item for item in evidence}
    assert len(by_identity) == len(evidence)
    assert len(result.llm_calls) == 2
    request_ids = {diagnostic_evidence_identity("provider-request", call.call_id) for call in result.llm_calls}
    authoring_id = diagnostic_evidence_identity("tool-authoring", "call_inline")
    assert set(by_identity) == request_ids | {authoring_id}
    for call in result.llm_calls:
        request = by_identity[diagnostic_evidence_identity("provider-request", call.call_id)]
        assert request.status == "provider_request"
        assert request.tool_name == "composer_provider_context"
        assert request.tool_call_id == call.call_id
        assert request.arguments.disposition == "retained"
        assert request.arguments.content is not None
        assert raw_canary not in request.arguments.content
    assert all(item.operation_id == compose_lease.context.fence.operation_id for item in evidence)
    assert all(item.operation_epoch == compose_lease.context.fence.operation_epoch for item in evidence)
    private = by_identity[authoring_id]
    assert private.tool_call_id == "call_inline"
    assert private.tool_name == "set_pipeline"
    assert private.status == result.tool_invocations[0].status.value
    assert private.arguments.disposition == "retained"
    assert private.arguments.encoding == "json"
    assert private.arguments.content is not None
    assert json.loads(private.arguments.content)["source"]["inline_blob"]["content"] == raw_content
    # Private proposal authority retains the exact source draft for review;
    # published tool history must use the separately redacted projection.
    requirements = safe_arguments["source"]["options"]["interpretation_requirements"]
    assert len(requirements) == 1
    assert requirements[0]["draft"] == raw_content
    assert raw_canary not in json.dumps(safe_redacted_arguments)
    assert len(result.tool_invocations) == 1
    assert result.tool_invocations[0].version_after == state.version
    assert result.tool_invocations[0].arguments_canonical == canonical_json(safe_arguments)
    assert result.tool_invocations[0].arguments_hash == stable_hash(safe_arguments)
    published_reply = {
        "message": result.message,
        "tool_invocations": [
            {
                "result_canonical": invocation.result_canonical,
                "error_message": invocation.error_message,
            }
            for invocation in result.tool_invocations
        ],
    }
    # arguments_canonical above is an internal authority carrier; the public
    # persisted projection is checked independently in public_rows.
    assert raw_canary not in json.dumps(published_reply)
    assert "[redacted inline content held for custody]" not in json.dumps(published_reply)
    assert raw_canary not in caplog.text
    assert "[redacted inline content held for custody]" not in caplog.text
    assert len(message_snapshots) == 2
    assert raw_canary not in json.dumps(message_snapshots[1])
    assert result.llm_calls[1].messages_hash == stable_hash(message_snapshots[1])

    blob_service = BlobServiceImpl(harness.engine, tmp_path)
    blob_id = UUID(safe_arguments["source"]["blob_id"])
    pending_delete_lease = await SessionOperationLease.acquire(
        harness.sessions.session_operation_authority,
        session_id=UUID(harness.session_id),
        operation_kind=SessionOperationKind.COMPOSE,
        owner_instance_id=harness.sessions.session_operation_owner_instance_id,
        lease_seconds=harness.sessions.session_operation_lease_seconds,
    )
    try:
        with pytest.raises(BlobPendingProposalError):
            await blob_service.delete_blob(
                blob_id,
                session_operation_context=pending_delete_lease.context,
            )
    finally:
        await pending_delete_lease.close()
    proposal_context = await harness.sessions._run_sync(
        lambda: harness.sessions.session_operation_authority.acquire(
            session_id=UUID(harness.session_id),
            operation_kind=SessionOperationKind.PROPOSAL,
            owner_instance_id=harness.sessions.session_operation_owner_instance_id,
            lease_seconds=harness.sessions.session_operation_lease_seconds,
        )
    )
    try:
        rejected = await harness.sessions.reject_composition_proposal(
            session_id=UUID(harness.session_id),
            proposal_id=proposals[0].id,
            actor="composer-parity-test",
            session_operation_context=proposal_context,
        )
    finally:
        await harness.sessions._run_sync(harness.sessions.session_operation_authority.release, proposal_context)
    assert rejected.status == "rejected"
    assert Path(blob_row.storage_path).exists()
    final_delete_lease = await SessionOperationLease.acquire(
        harness.sessions.session_operation_authority,
        session_id=UUID(harness.session_id),
        operation_kind=SessionOperationKind.COMPOSE,
        owner_instance_id=harness.sessions.session_operation_owner_instance_id,
        lease_seconds=harness.sessions.session_operation_lease_seconds,
    )
    try:
        await blob_service.delete_blob(
            blob_id,
            session_operation_context=final_delete_lease.context,
        )
    finally:
        await final_delete_lease.close()
    assert not Path(blob_row.storage_path).exists()


@pytest.mark.asyncio
async def test_inline_proposal_gap_retry_reuses_one_custody_blob_and_quota_charge(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    state = _incremental_base_state(tmp_path)
    arguments = _inline_pipeline_args(tmp_path)
    raw_content = "proposal-gap-private-value-2f16\n42\n"
    arguments["source"]["inline_blob"]["content"] = raw_content
    captured_safe_arguments: list[dict[str, Any]] = []

    async def _interrupt_before_proposal(**kwargs: Any) -> Any:
        captured_safe_arguments.append(deepcopy(deep_thaw(kwargs["plan"].proposal.pipeline)))
        raise RuntimeError("simulated interruption before proposal creation")

    first_llm = _ScriptedLLM(_tool_turn("call_gap", "set_pipeline", arguments))
    with (
        patch.object(harness.service._provider_gateway, "_call_llm", new=first_llm),
        patch.object(harness.sessions, "create_pipeline_composition_proposal", new=_interrupt_before_proposal),
        pytest.raises(RuntimeError, match="simulated interruption before proposal creation"),
    ):
        await harness.service.compose(
            "Build a reviewed inline pipeline.",
            [],
            state,
            session_id=harness.session_id,
            user_id="proposal-prevalidation-user",
            user_message_id=harness.user_message_id,
        )

    assert len(captured_safe_arguments) == 1
    assert "inline_blob" not in captured_safe_arguments[0]["source"]
    assert raw_content not in json.dumps(captured_safe_arguments)
    assert _count_rows(harness.engine, blobs_table) == 1
    assert _count_rows(harness.engine, composition_proposals_table) == 0
    with harness.engine.connect() as conn:
        first_blob = conn.execute(select(blobs_table)).one()
        quota_after_interruption = conn.execute(
            select(func.coalesce(func.sum(blobs_table.c.size_bytes), 0)).where(blobs_table.c.session_id == harness.session_id)
        ).scalar_one()
    assert int(quota_after_interruption) == len(raw_content.encode("utf-8"))
    assert Path(first_blob.storage_path).read_bytes() == raw_content.encode("utf-8")

    retry_llm = _ScriptedLLM(
        _tool_turn("call_gap_retry", "set_pipeline", arguments),
        _fake_llm_response(content="The retried inline proposal is pending approval."),
    )
    with patch.object(harness.service._provider_gateway, "_call_llm", new=retry_llm):
        await harness.service.compose(
            "Build a reviewed inline pipeline.",
            [],
            state,
            session_id=harness.session_id,
            user_id="proposal-prevalidation-user",
            user_message_id=harness.user_message_id,
        )

    proposals = await harness.sessions.list_composition_proposals(UUID(harness.session_id))
    assert len(proposals) == 1
    retry_arguments = deep_thaw(proposals[0].arguments_json)
    retry_redacted_arguments = deep_thaw(proposals[0].arguments_redacted_json)
    assert retry_arguments["source"]["blob_id"] == first_blob.id
    assert "inline_blob" not in retry_arguments["source"]
    assert "inline_blob" not in retry_redacted_arguments["source"]
    assert raw_content not in json.dumps(retry_arguments)
    assert _count_rows(harness.engine, blobs_table) == 1
    with harness.engine.connect() as conn:
        retried_blob = conn.execute(select(blobs_table)).one()
        quota_after_retry = conn.execute(
            select(func.coalesce(func.sum(blobs_table.c.size_bytes), 0)).where(blobs_table.c.session_id == harness.session_id)
        ).scalar_one()
    assert retried_blob.id == first_blob.id
    assert retried_blob.creating_arguments_hash == stable_hash(retry_arguments)
    assert int(quota_after_retry) == len(raw_content.encode("utf-8"))
    assert Path(retried_blob.storage_path).read_bytes() == raw_content.encode("utf-8")


def _assert_context_authority_retained(left: Any, right: Any) -> None:
    """Shallow context replacement must retain all 24 non-stage fields."""
    assert left.catalog is right.catalog
    assert left.plugin_snapshot is right.plugin_snapshot
    assert left.session_engine is right.session_engine
    assert left.session_operation_context is right.session_operation_context
    assert left.session_operation_authority is right.session_operation_authority
    assert left.secret_service is right.secret_service
    assert left.secret_wiring_policy is right.secret_wiring_policy
    assert left.baseline is right.baseline
    assert left.current_validation is right.current_validation
    assert left.runtime_preflight is right.runtime_preflight
    assert left.structural_preflight is right.structural_preflight
    assert left.reviewed_source_authority is right.reviewed_source_authority
    assert left.data_dir == right.data_dir
    assert left.require_data_dir_for_paths == right.require_data_dir_for_paths
    assert left.session_id == right.session_id
    assert left.user_id == right.user_id
    assert left.max_blob_storage_per_session_bytes == right.max_blob_storage_per_session_bytes
    assert left.user_message_id == right.user_message_id
    assert left.user_message_content == right.user_message_content
    assert left.composer_model_identifier == right.composer_model_identifier
    assert left.composer_model_version == right.composer_model_version
    assert left.composer_provider == right.composer_provider
    assert left.composer_skill_hash == right.composer_skill_hash
    assert left.executing_proposal_id == right.executing_proposal_id


def _assert_repaired_inline_builder_stages(builder: Any, *, first_valid_call: int, state: CompositionState) -> str:
    """Prove public draft, canonical review and physical blob-backed rebuild."""
    calls = builder.call_args_list[first_valid_call:]
    assert len(calls) == 3
    assert all(not call.kwargs and len(call.args) == 3 and call.args[1] is state for call in calls)
    initial, canonical, custody = calls
    initial_arguments, _initial_state, initial_context = initial.args
    canonical_arguments, _canonical_state, canonical_context = canonical.args
    custody_arguments, _custody_state, custody_context = custody.args
    assert initial_context is not canonical_context and canonical_context is not custody_context
    assert initial_context._interpretation_requirements_are_internal is False
    assert canonical_context._interpretation_requirements_are_internal is True
    assert custody_context._interpretation_requirements_are_internal is True
    _assert_context_authority_retained(initial_context, canonical_context)
    _assert_context_authority_retained(canonical_context, custody_context)
    assert initial_context.tool_arguments_hash == composer_authority_hash(inline_custody_audit_projection(initial_arguments))
    assert canonical_context.tool_arguments_hash == composer_authority_hash(inline_custody_audit_projection(canonical_arguments))
    assert custody_context.tool_arguments_hash == composer_authority_hash(custody_arguments)

    assert "inline_blob" in initial_arguments["source"]
    assert INTERPRETATION_REQUIREMENTS_KEY not in initial_arguments["source"]["options"]
    assert "inline_blob" in canonical_arguments["source"]
    (source_review,) = canonical_arguments["source"]["options"][INTERPRETATION_REQUIREMENTS_KEY]
    assert source_review["id"] == "source_review:inline_source_data"
    assert source_review["user_term"] == "inline_source_data"
    assert source_review["kind"] == "invented_source"
    assert source_review["status"] == "pending"
    assert source_review["draft"] == initial_arguments["source"]["inline_blob"]["content"]
    assert source_review["event_id"] is None
    assert source_review["accepted_value"] is None
    assert source_review["accepted_artifact_hash"] is None
    assert source_review["resolved_prompt_template_hash"] is None
    expected_canonical = deepcopy(initial_arguments)
    expected_canonical["source"]["options"][INTERPRETATION_REQUIREMENTS_KEY] = deepcopy(
        canonical_arguments["source"]["options"][INTERPRETATION_REQUIREMENTS_KEY]
    )
    assert canonical_arguments == expected_canonical

    assert "inline_blob" not in custody_arguments["source"]
    blob_id = custody_arguments["source"]["blob_id"]
    assert str(UUID(blob_id)) == blob_id
    expected_custody = deepcopy(canonical_arguments)
    del expected_custody["source"]["inline_blob"]
    expected_custody["source"]["blob_id"] = blob_id
    assert custody_arguments == expected_custody
    assert (
        custody_arguments["source"]["options"][INTERPRETATION_REQUIREMENTS_KEY]
        == canonical_arguments["source"]["options"][INTERPRETATION_REQUIREMENTS_KEY]
    )
    return blob_id


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("case", "expected_error_fragment", "rejected_fragment", "expected_invalid_builder_calls"),
    [
        (
            "disallowed_mime",
            "must be object conforming to SetPipelineArgumentsModel",
            "application/octet-stream",
            0,
        ),
        ("unsanitizable_filename", "'filename' must be", "..", 1),
    ],
)
async def test_inline_candidate_argument_error_is_audited_once_and_repairable(
    tmp_path: Path,
    case: str,
    expected_error_fragment: str,
    rejected_fragment: str,
    expected_invalid_builder_calls: int,
) -> None:
    harness = _harness(tmp_path)
    state = _incremental_base_state(tmp_path)
    invalid = _inline_pipeline_args(tmp_path)
    inline_blob = invalid["source"]["inline_blob"]
    if case == "disallowed_mime":
        inline_blob["mime_type"] = rejected_fragment
    elif case == "unsanitizable_filename":
        inline_blob["filename"] = rejected_fragment
    else:  # pragma: no cover - parametrization is a closed local table
        raise AssertionError(f"unknown case: {case}")

    repaired = _inline_pipeline_args(tmp_path)
    responses = [
        _tool_turn(f"call_{case}", "set_pipeline", invalid),
        _tool_turn(f"call_{case}_repaired", "set_pipeline", repaired),
        _fake_llm_response(content="The repaired inline proposal is pending approval."),
    ]
    message_snapshots: list[list[dict[str, Any]]] = []
    invalid_turn_outcomes: list[Any] = []

    async def _llm(
        messages: list[dict[str, Any]],
        _tools: Any,
        *,
        provider_custody: object | None = None,
        credential_state: object | None = None,
        prompt_guard_trusted_message_indexes: frozenset[int] = frozenset(),
        prompt_guard_content_parts: tuple[tuple[PromptGuardContentPart, ...] | None, ...] | None = None,
        prompt_guard_tool_argument_parts: tuple[tuple[PromptGuardToolArgumentParts, ...] | None, ...] | None = None,
    ) -> Any:
        message_snapshots.append(deepcopy(messages))
        if len(message_snapshots) == 2:
            invalid_turn_outcomes.extend(outcome_batches[0])
            assert builder.call_count == expected_invalid_builder_calls
            assert _count_rows(harness.engine, composition_proposals_table) == 0
            assert _count_rows(harness.engine, blobs_table) == 0
            assert _count_rows(harness.engine, composition_states_table) == 0
        return responses.pop(0)

    with (
        patch.object(harness.service._provider_gateway, "_call_llm", new=_llm),
        _record_phase3_outcomes(harness.service) as outcome_batches,
        patch(
            "elspeth.web.composer.tool_batch.build_set_pipeline_candidate",
            wraps=real_build_set_pipeline_candidate,
        ) as builder,
    ):
        result = await harness.service.compose(
            "Build a reviewed inline pipeline.",
            [],
            state,
            session_id=harness.session_id,
            user_id="proposal-prevalidation-user",
            user_message_id=harness.user_message_id,
        )

    proposals = await harness.sessions.list_composition_proposals(UUID(harness.session_id))
    assert len(proposals) == 1
    assert proposals[0].tool_call_id == f"call_{case}_repaired"
    assert result.state is state
    repaired_blob_id = _assert_repaired_inline_builder_stages(builder, first_valid_call=expected_invalid_builder_calls, state=state)
    assert deep_thaw(proposals[0].arguments_json)["source"]["blob_id"] == repaired_blob_id
    with harness.engine.connect() as conn:
        assert str(conn.execute(select(blobs_table.c.id)).scalar_one()) == repaired_blob_id
    assert _count_rows(harness.engine, blobs_table) == 1
    assert _count_rows(harness.engine, composition_states_table) == 0

    arg_error_invocations = [invocation for invocation in result.tool_invocations if invocation.status is ComposerToolStatus.ARG_ERROR]
    assert len(arg_error_invocations) == 1
    arg_error_invocation = arg_error_invocations[0]
    assert arg_error_invocation.tool_call_id == f"call_{case}"
    assert arg_error_invocation.error_class == "ToolArgumentError"
    assert arg_error_invocation.version_before == state.version
    assert arg_error_invocation.version_after is None
    inline_content = invalid["source"]["inline_blob"]["content"]
    assert inline_content not in arg_error_invocation.arguments_canonical
    assert inline_content not in json.dumps(message_snapshots[1])

    arg_error_outcomes = [outcome for outcome in invalid_turn_outcomes if outcome.error_class == "ToolArgumentError"]
    assert len(arg_error_outcomes) == 1
    assert arg_error_outcomes[0].pre_version == state.version
    assert arg_error_outcomes[0].post_version == state.version
    assert arg_error_outcomes[0].response is None

    feedback = next(message for message in message_snapshots[1] if message.get("tool_call_id") == f"call_{case}")
    feedback_payload = json.loads(feedback["content"])
    assert expected_error_fragment in feedback_payload["error"]
    assert rejected_fragment not in feedback["content"]
    assert "caused by" not in feedback["content"]

    persisted_feedback = _persisted_tool_content(harness, f"call_{case}")
    assert json.loads(persisted_feedback)["error_class"] == "ToolArgumentError"
    assert rejected_fragment not in persisted_feedback


@pytest.mark.asyncio
@pytest.mark.parametrize("malformed_shape", ["scalar", "list", "unknown", "named"])
async def test_malformed_inline_blob_never_survives_full_compose_surfaces(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    malformed_shape: str,
) -> None:
    harness = _harness(tmp_path)
    state = _incremental_base_state(tmp_path)
    invalid = _inline_pipeline_args(tmp_path)
    private_value = f"private malformed {malformed_shape} value 71b2"
    if malformed_shape == "scalar":
        invalid["source"]["inline_blob"] = private_value
    elif malformed_shape == "list":
        invalid["source"]["inline_blob"] = [private_value, {"nested": private_value}]
    elif malformed_shape == "unknown":
        invalid["source"]["inline_blob"]["unknown"] = {"payload": private_value}
    elif malformed_shape == "named":
        invalid["sources"] = {"named": {"inline_blob": {"unknown": private_value}}}
    else:  # pragma: no cover - closed parametrization
        raise AssertionError(malformed_shape)

    repaired = _inline_pipeline_args(tmp_path)
    llm = _ScriptedLLM(
        _tool_turn(f"call_malformed_{malformed_shape}", "set_pipeline", invalid),
        _tool_turn(f"call_repaired_{malformed_shape}", "set_pipeline", repaired),
        _fake_llm_response(content="The repaired inline proposal is pending approval."),
    )
    with patch.object(harness.service._provider_gateway, "_call_llm", new=llm):
        result = await harness.service.compose(
            "Build a reviewed inline pipeline.",
            [],
            state,
            session_id=harness.session_id,
            user_id="proposal-prevalidation-user",
            user_message_id=harness.user_message_id,
        )

    proposals = await harness.sessions.list_composition_proposals(UUID(harness.session_id))
    assert len(proposals) == 1
    assert private_value not in json.dumps(llm.message_snapshots)
    assert private_value not in json.dumps([deep_thaw(proposal.arguments_json) for proposal in proposals])
    assert private_value not in json.dumps([invocation.arguments_canonical for invocation in result.tool_invocations])
    assert private_value not in caplog.text
    with harness.engine.connect() as conn:
        persisted = tuple(conn.execute(select(chat_messages_table).where(chat_messages_table.c.session_id == harness.session_id)))
    public_rows = []
    private_records = []
    for row in persisted:
        envelopes = row.tool_calls or []
        if any(is_diagnostic_evidence_envelope(envelope) for envelope in envelopes):
            assert len(envelopes) == 1 and row.role == "audit" and row.writer_principal == "compose_loop"
            private_records.append(ComposerDiagnosticEvidence.from_envelope(envelopes[0], expected_session_id=harness.session_id))
        else:
            public_rows.append(row)
    assert private_value not in json.dumps([(row.content, row.raw_content, row.tool_calls) for row in public_rows])
    rejected_call_id = f"call_malformed_{malformed_shape}"
    rejected_public = [row for row in public_rows if row.role == "tool" and row.tool_call_id == rejected_call_id]
    assert len(rejected_public) == 1 and rejected_public[0].parent_assistant_id is not None
    rejected_records = [record for record in private_records if record.tool_call_id == rejected_call_id]
    assert len(rejected_records) == 1
    rejected = rejected_records[0]
    assert rejected.tool_name == "set_pipeline" and rejected.status == ComposerToolStatus.ARG_ERROR.value
    assert rejected.arguments.disposition == "retained" and rejected.arguments.content is not None
    assert json.loads(rejected.arguments.content) == invalid


@pytest.mark.asyncio
async def test_surrogate_inline_content_fails_closed_at_canonicalization_and_is_repairable(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    state = _incremental_base_state(tmp_path)
    invalid = _inline_pipeline_args(tmp_path)
    private_canary = "PRIVATE-SURROGATE-INLINE-CANARY"
    rejected_content = f"bad\udc80{private_canary}"
    invalid["source"]["inline_blob"]["content"] = rejected_content
    repaired = _inline_pipeline_args(tmp_path)
    responses = [
        _tool_turn("call_surrogate", "set_pipeline", invalid),
        _tool_turn("call_surrogate_repaired", "set_pipeline", repaired),
        _fake_llm_response(content="The repaired inline proposal is pending approval."),
    ]
    message_snapshots: list[list[dict[str, Any]]] = []
    invalid_turn_outcomes: list[Any] = []

    async def _llm(
        messages: list[dict[str, Any]],
        _tools: Any,
        *,
        provider_custody: object | None = None,
        credential_state: object | None = None,
        prompt_guard_trusted_message_indexes: frozenset[int] = frozenset(),
        prompt_guard_content_parts: tuple[tuple[PromptGuardContentPart, ...] | None, ...] | None = None,
        prompt_guard_tool_argument_parts: tuple[tuple[PromptGuardToolArgumentParts, ...] | None, ...] | None = None,
    ) -> Any:
        message_snapshots.append(deepcopy(messages))
        if len(message_snapshots) == 2:
            invalid_turn_outcomes.extend(outcome_batches[0])
            assert builder.call_count == 0
            assert _count_rows(harness.engine, composition_proposals_table) == 0
            assert _count_rows(harness.engine, blobs_table) == 0
            assert _count_rows(harness.engine, composition_states_table) == 0
        return responses.pop(0)

    with (
        patch.object(harness.service._provider_gateway, "_call_llm", new=_llm),
        _record_phase3_outcomes(harness.service) as outcome_batches,
        patch(
            "elspeth.web.composer.tool_batch.build_set_pipeline_candidate",
            wraps=real_build_set_pipeline_candidate,
        ) as builder,
    ):
        result = await harness.service.compose(
            "Build a reviewed inline pipeline.",
            [],
            state,
            session_id=harness.session_id,
            user_id="proposal-prevalidation-user",
            user_message_id=harness.user_message_id,
        )

    proposals = await harness.sessions.list_composition_proposals(UUID(harness.session_id))
    assert len(proposals) == 1
    assert proposals[0].tool_call_id == "call_surrogate_repaired"
    assert result.state is state
    repaired_blob_id = _assert_repaired_inline_builder_stages(builder, first_valid_call=0, state=state)
    assert deep_thaw(proposals[0].arguments_json)["source"]["blob_id"] == repaired_blob_id
    with harness.engine.connect() as conn:
        assert str(conn.execute(select(blobs_table.c.id)).scalar_one()) == repaired_blob_id
    assert _count_rows(harness.engine, blobs_table) == 1
    assert _count_rows(harness.engine, composition_states_table) == 0

    arg_error_invocations = [invocation for invocation in result.tool_invocations if invocation.status is ComposerToolStatus.ARG_ERROR]
    assert len(arg_error_invocations) == 1
    arg_error_invocation = arg_error_invocations[0]
    assert arg_error_invocation.tool_call_id == "call_surrogate"
    assert arg_error_invocation.error_class == "JsonBoundaryError"
    assert arg_error_invocation.version_before == state.version
    assert arg_error_invocation.version_after is None
    assert json.loads(arg_error_invocation.arguments_canonical) == {
        "_redaction_status": "invalid_tool_arguments",
        "error_class": "JsonBoundaryError",
    }
    assert private_canary not in arg_error_invocation.arguments_canonical
    assert private_canary not in json.dumps(message_snapshots[1])

    arg_error_outcomes = [outcome for outcome in invalid_turn_outcomes if outcome.error_class == "JsonBoundaryError"]
    assert len(arg_error_outcomes) == 1
    assert arg_error_outcomes[0].pre_version == state.version
    assert arg_error_outcomes[0].post_version == state.version
    assert arg_error_outcomes[0].response is None

    feedback = next(message for message in message_snapshots[1] if message.get("tool_call_id") == "call_surrogate")
    feedback_payload = json.loads(feedback["content"])
    assert "not valid UTF-8" in feedback_payload["error"]
    assert private_canary not in feedback["content"]

    persisted_feedback = _persisted_tool_content(harness, "call_surrogate")
    assert json.loads(persisted_feedback)["error_class"] == "JsonBoundaryError"
    assert private_canary not in persisted_feedback
    with harness.engine.connect() as conn:
        persisted_messages = tuple(
            conn.execute(
                select(chat_messages_table.c.content, chat_messages_table.c.raw_content, chat_messages_table.c.tool_calls).where(
                    chat_messages_table.c.session_id == harness.session_id
                )
            )
        )
    assert private_canary not in json.dumps(tuple(tuple(row) for row in persisted_messages))


@pytest.mark.asyncio
async def test_unexpected_candidate_finalizer_exception_uses_plugin_crash_audit_and_wrapper(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    state = _incremental_base_state(tmp_path)
    args = _inline_pipeline_args(tmp_path)
    llm = _ScriptedLLM(_tool_turn("call_finalizer_crash", "set_pipeline", args))
    unexpected = RuntimeError("candidate finalizer internal failure with private detail")

    with (
        patch.object(harness.service._provider_gateway, "_call_llm", new=llm),
        _record_phase3_outcomes(harness.service) as outcome_batches,
        patch(
            "elspeth.web.composer.tool_batch.build_set_pipeline_candidate",
            wraps=real_build_set_pipeline_candidate,
        ) as builder,
        patch("elspeth.web.composer.tool_batch.finalize_tool_result", side_effect=unexpected) as finalizer,
        pytest.raises(ComposerPluginCrashError) as exc_info,
    ):
        await harness.service.compose(
            "Build a reviewed inline pipeline.",
            [],
            state,
            session_id=harness.session_id,
            user_id="proposal-prevalidation-user",
            user_message_id=harness.user_message_id,
        )

    assert exc_info.value.original_exc is unexpected
    assert exc_info.value.__cause__ is unexpected
    assert exc_info.value.partial_state is None
    assert builder.call_count == 2
    initial_call, canonical_call = builder.call_args_list
    initial_arguments, initial_state, initial_context = initial_call.args
    canonical_arguments, canonical_state, canonical_context = canonical_call.args
    assert initial_state is canonical_state is state
    assert initial_context._interpretation_requirements_are_internal is False
    assert canonical_context._interpretation_requirements_are_internal is True
    assert INTERPRETATION_REQUIREMENTS_KEY not in initial_arguments["source"]["options"]
    (source_review,) = canonical_arguments["source"]["options"][INTERPRETATION_REQUIREMENTS_KEY]
    assert source_review["kind"] == "invented_source"
    assert source_review["status"] == "pending"
    assert finalizer.call_count == 1
    assert len(llm.message_snapshots) == 1
    assert await harness.sessions.list_composition_proposals(UUID(harness.session_id)) == []
    assert _count_rows(harness.engine, blobs_table) == 0
    assert _count_rows(harness.engine, composition_states_table) == 0

    outcomes = outcome_batches[-1]
    assert len(outcomes) == 1
    assert outcomes[0].error_class == "RuntimeError"
    assert outcomes[0].error_message == "RuntimeError"
    assert outcomes[0].pre_version == state.version
    assert outcomes[0].post_version == state.version

    assert exc_info.value.failed_turn is not None
    assert exc_info.value.failed_turn.tool_calls_attempted == 1
    persisted_feedback = _persisted_tool_content(harness, "call_finalizer_crash")
    assert json.loads(persisted_feedback) == {
        "_redaction_status": "plugin_crash",
        "error_class": "RuntimeError",
        "error_message": "<redacted-failure-message>",
    }
    assert str(unexpected) not in persisted_feedback


@pytest.mark.asyncio
async def test_preproposal_base_exception_is_audited_once_and_propagated_unchanged(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    state = _incremental_base_state(tmp_path)
    args = _inline_pipeline_args(tmp_path)
    llm = _ScriptedLLM(_tool_turn("call_finalizer_base_signal", "set_pipeline", args))
    signal = _PreproposalBaseSignal("shutdown-style private detail")
    recorded: list[Any] = []
    original_record = BufferingRecorder.record

    def _record(recorder: BufferingRecorder, invocation: Any) -> None:
        recorded.append(invocation)
        original_record(recorder, invocation)

    with (
        patch.object(harness.service._provider_gateway, "_call_llm", new=llm),
        patch(
            "elspeth.web.composer.tool_batch.build_set_pipeline_candidate",
            wraps=real_build_set_pipeline_candidate,
        ) as builder,
        patch("elspeth.web.composer.tool_batch.finalize_tool_result", side_effect=signal) as finalizer,
        patch.object(BufferingRecorder, "record", new=_record),
        pytest.raises(_PreproposalBaseSignal) as exc_info,
    ):
        await harness.service.compose(
            "Build a reviewed inline pipeline.",
            [],
            state,
            session_id=harness.session_id,
            user_id="proposal-prevalidation-user",
            user_message_id=harness.user_message_id,
        )

    assert exc_info.value is signal
    assert not isinstance(exc_info.value, ComposerPluginCrashError)
    assert builder.call_count == 2
    initial_call, canonical_call = builder.call_args_list
    initial_arguments, _initial_state, initial_context = initial_call.args
    canonical_arguments, _canonical_state, canonical_context = canonical_call.args
    assert INTERPRETATION_REQUIREMENTS_KEY not in initial_arguments["source"]["options"]
    (source_review,) = canonical_arguments["source"]["options"][INTERPRETATION_REQUIREMENTS_KEY]
    assert source_review["kind"] == "invented_source"
    assert source_review["status"] == "pending"
    assert initial_context._interpretation_requirements_are_internal is False
    assert canonical_context._interpretation_requirements_are_internal is True
    assert finalizer.call_count == 1
    assert len(llm.message_snapshots) == 1
    assert await harness.sessions.list_composition_proposals(UUID(harness.session_id)) == []
    assert _count_rows(harness.engine, blobs_table) == 0
    assert _count_rows(harness.engine, composition_states_table) == 0

    assert len(recorded) == 1
    invocation = recorded[0]
    assert invocation.tool_call_id == "call_finalizer_base_signal"
    assert invocation.status is ComposerToolStatus.PLUGIN_CRASH
    assert invocation.error_class == "_PreproposalBaseSignal"
    assert invocation.error_message == "_PreproposalBaseSignal"
    assert invocation.version_before == state.version
    assert invocation.version_after is None
    assert invocation.result_canonical is None


@pytest.mark.asyncio
async def test_candidate_prior_validation_runtime_error_uses_plugin_crash_audit_and_wrapper(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    state = _incremental_base_state(tmp_path)
    response = _tool_turn("call_prior_runtime_crash", "set_pipeline", _inline_pipeline_args(tmp_path))
    failure = RuntimeError("candidate-prior validation private detail")
    snapshot = PluginAvailabilitySnapshot.for_trained_operator(harness.catalog)
    catalog = PolicyCatalogView.for_trained_operator(harness.catalog, snapshot)
    original_validate = catalog.validate_composition_state
    armed = False
    llm_calls = 0
    recorded: list[Any] = []
    original_record = BufferingRecorder.record

    def _validate(candidate_state: CompositionState) -> Any:
        if armed:
            raise failure
        return original_validate(candidate_state)

    async def _llm(
        messages: list[dict[str, Any]],
        _tools: Any,
        *,
        provider_custody: object | None = None,
        credential_state: object | None = None,
        prompt_guard_trusted_message_indexes: frozenset[int] = frozenset(),
        prompt_guard_content_parts: tuple[tuple[PromptGuardContentPart, ...] | None, ...] | None = None,
        prompt_guard_tool_argument_parts: tuple[tuple[PromptGuardToolArgumentParts, ...] | None, ...] | None = None,
    ) -> Any:
        nonlocal armed, llm_calls
        assert messages
        llm_calls += 1
        armed = True
        return response

    def _record(recorder: BufferingRecorder, invocation: Any) -> None:
        recorded.append(invocation)
        original_record(recorder, invocation)

    with (
        patch.object(harness.service._policy_context, "build", return_value=(snapshot, catalog)),
        patch.object(harness.service._provider_gateway, "_call_llm", new=_llm),
        patch.object(catalog, "validate_composition_state", side_effect=_validate) as validation,
        patch("elspeth.web.composer.tool_batch.build_set_pipeline_candidate") as builder,
        patch("elspeth.web.composer.tool_batch.finalize_tool_result") as finalizer,
        patch.object(BufferingRecorder, "record", new=_record),
        pytest.raises(ComposerPluginCrashError) as exc_info,
    ):
        await harness.service.compose(
            "Build a reviewed inline pipeline.",
            [],
            state,
            session_id=harness.session_id,
            user_id="proposal-prevalidation-user",
            user_message_id=harness.user_message_id,
        )

    assert llm_calls == 1
    assert validation.call_count >= 1
    assert builder.call_count == 0
    assert finalizer.call_count == 0
    assert exc_info.value.original_exc is failure
    assert exc_info.value.__cause__ is failure
    assert exc_info.value.partial_state is None
    assert await harness.sessions.list_composition_proposals(UUID(harness.session_id)) == []
    assert _count_rows(harness.engine, blobs_table) == 0
    assert _count_rows(harness.engine, composition_states_table) == 0

    assert len(recorded) == 1
    invocation = recorded[0]
    assert invocation.tool_call_id == "call_prior_runtime_crash"
    assert invocation.status is ComposerToolStatus.PLUGIN_CRASH
    assert invocation.error_class == "RuntimeError"
    assert invocation.error_message == "RuntimeError"
    assert invocation.version_before == state.version
    assert invocation.version_after is None


@pytest.mark.asyncio
async def test_candidate_prior_validation_base_exception_is_audited_once_and_propagated_unchanged(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    state = _incremental_base_state(tmp_path)
    response = _tool_turn("call_prior_base_signal", "set_pipeline", _inline_pipeline_args(tmp_path))
    signal = _PreproposalBaseSignal("candidate-prior shutdown-style private detail")
    snapshot = PluginAvailabilitySnapshot.for_trained_operator(harness.catalog)
    catalog = PolicyCatalogView.for_trained_operator(harness.catalog, snapshot)
    original_validate = catalog.validate_composition_state
    armed = False
    llm_calls = 0
    recorded: list[Any] = []
    original_record = BufferingRecorder.record

    def _validate(candidate_state: CompositionState) -> Any:
        if armed:
            raise signal
        return original_validate(candidate_state)

    async def _llm(
        messages: list[dict[str, Any]],
        _tools: Any,
        *,
        provider_custody: object | None = None,
        credential_state: object | None = None,
        prompt_guard_trusted_message_indexes: frozenset[int] = frozenset(),
        prompt_guard_content_parts: tuple[tuple[PromptGuardContentPart, ...] | None, ...] | None = None,
        prompt_guard_tool_argument_parts: tuple[tuple[PromptGuardToolArgumentParts, ...] | None, ...] | None = None,
    ) -> Any:
        nonlocal armed, llm_calls
        assert messages
        llm_calls += 1
        armed = True
        return response

    def _record(recorder: BufferingRecorder, invocation: Any) -> None:
        recorded.append(invocation)
        original_record(recorder, invocation)

    with (
        patch.object(harness.service._policy_context, "build", return_value=(snapshot, catalog)),
        patch.object(harness.service._provider_gateway, "_call_llm", new=_llm),
        patch.object(catalog, "validate_composition_state", side_effect=_validate) as validation,
        patch("elspeth.web.composer.tool_batch.build_set_pipeline_candidate") as builder,
        patch("elspeth.web.composer.tool_batch.finalize_tool_result") as finalizer,
        patch.object(BufferingRecorder, "record", new=_record),
        pytest.raises(_PreproposalBaseSignal) as exc_info,
    ):
        await harness.service.compose(
            "Build a reviewed inline pipeline.",
            [],
            state,
            session_id=harness.session_id,
            user_id="proposal-prevalidation-user",
            user_message_id=harness.user_message_id,
        )

    assert llm_calls == 1
    assert validation.call_count >= 1
    assert builder.call_count == 0
    assert finalizer.call_count == 0
    assert exc_info.value is signal
    assert not isinstance(exc_info.value, ComposerPluginCrashError)
    assert await harness.sessions.list_composition_proposals(UUID(harness.session_id)) == []
    assert _count_rows(harness.engine, blobs_table) == 0
    assert _count_rows(harness.engine, composition_states_table) == 0

    assert len(recorded) == 1
    invocation = recorded[0]
    assert invocation.tool_call_id == "call_prior_base_signal"
    assert invocation.status is ComposerToolStatus.PLUGIN_CRASH
    assert invocation.error_class == "_PreproposalBaseSignal"
    assert invocation.error_message == "_PreproposalBaseSignal"
    assert invocation.version_before == state.version
    assert invocation.version_after is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("arguments", "expected_proposals", "expected_error_class"),
    [
        ({"patch": {"name": "reviewed"}}, 1, None),
        ({"patch": "not-an-object"}, 0, "ToolArgumentError"),
    ],
    ids=("valid", "invalid"),
)
async def test_incremental_explicit_approval_admits_only_valid_arguments(
    tmp_path: Path,
    arguments: dict[str, Any],
    expected_proposals: int,
    expected_error_class: str | None,
) -> None:
    harness = _harness(tmp_path)
    state = _incremental_base_state(tmp_path)
    llm = _ScriptedLLM(
        _tool_turn("call_metadata", "set_metadata", arguments),
        _fake_llm_response(content="Done."),
    )

    with (
        patch.object(harness.service._provider_gateway, "_call_llm", new=llm),
        _record_phase3_outcomes(harness.service) as outcome_batches,
        patch("elspeth.web.composer.tool_batch.build_set_pipeline_candidate") as builder,
    ):
        result = await harness.service.compose(
            "Update metadata under explicit approval.",
            [],
            state,
            session_id=harness.session_id,
            user_id="proposal-prevalidation-user",
            user_message_id=harness.user_message_id,
        )

    proposals = await harness.sessions.list_composition_proposals(UUID(harness.session_id))
    assert len(proposals) == expected_proposals
    assert builder.call_count == 0
    assert result.state is state
    outcome = outcome_batches[0][0]
    assert outcome.error_class == expected_error_class
    assert outcome.post_version == state.version


@pytest.mark.asyncio
@pytest.mark.parametrize("dialect", tuple(ToolContractDialect))
@pytest.mark.parametrize(
    ("tool_name", "arguments", "expected_success", "expected_error"),
    [
        ("set_metadata", {"patch": {}}, True, None),
        ("set_metadata", {"patch": {"name": "Untitled Pipeline"}}, True, None),
        ("set_metadata", {"patch": "invalid"}, None, "ToolArgumentError"),
        ("patch_node_options", {"node_id": "absent", "patch": {}}, False, None),
    ],
    ids=("empty-noop", "same-value-noop", "argument-error", "handler-refusal"),
)
async def test_unapplied_incremental_feedback_then_repair_stages_once(
    tmp_path: Path,
    dialect: ToolContractDialect,
    tool_name: str,
    arguments: dict[str, Any],
    expected_success: bool | None,
    expected_error: str | None,
) -> None:
    harness = _harness(tmp_path)
    harness.service._planner_dialect = dialect
    state = _incremental_base_state(tmp_path)
    repair = {"patch": {"name": "local-repair"}}
    llm = _ScriptedLLM(
        _tool_turn("call_unapplied", tool_name, encode_semantic_arguments(tool_name, dialect, arguments)),
        _tool_turn("call_repair", "set_metadata", encode_semantic_arguments("set_metadata", dialect, repair)),
        _fake_llm_response(content="The repair awaits approval."),
    )
    progress = []

    async def record_progress(event: Any) -> None:
        progress.append(event)

    with (
        patch.object(harness.service._provider_gateway, "_call_llm", new=llm),
        _record_phase3_outcomes(harness.service) as batches,
        patch("elspeth.web.composer.tool_batch.execute_tool", wraps=real_execute_tool) as execution,
    ):
        result = await harness.service.compose(
            "Update the metadata.",
            [],
            state,
            session_id=harness.session_id,
            user_id="proposal-prevalidation-user",
            user_message_id=harness.user_message_id,
            progress=record_progress,
        )
    proposals = await harness.sessions.list_composition_proposals(UUID(harness.session_id))
    assert [proposal.tool_call_id for proposal in proposals] == ["call_repair"]
    assert result.state is state
    assert len(result.tool_invocations) == 2
    assert execution.call_count == 2
    assert len(batches[0]) == 1
    outcome = batches[0][0]
    assert outcome.post_version == state.version
    assert outcome.error_class == expected_error
    feedback = [m for m in llm.message_snapshots[1] if m.get("role") == "tool" and m["tool_call_id"] == "call_unapplied"]
    assert len(feedback) == 1
    payload = json.loads(feedback[0]["content"])
    if expected_success is not None:
        assert payload["success"] is expected_success
        assert payload["version"] == state.version
        assert payload["affected_nodes"] == []
        assert "validation_delta" not in payload
        assert "applied_component" not in payload
        assert "PREVALIDATION_REJECTED" not in feedback[0]["content"]
    if expected_success:
        assert payload["validation"]["is_valid"] is False
        assert payload["validation"]["errors"]
        assert any("unchanged" in event.headline.lower() for event in progress)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "call_kind", ("noop", "refusal", "argument", "at-cap-noop", "at-cap-refusal", "at-cap-blob-refusal", "change", "inline")
)
async def test_proposal_cap_counts_only_admitted_changes(tmp_path: Path, call_kind: str) -> None:
    harness = _harness(tmp_path)
    state = _incremental_base_state(tmp_path)
    at_cap = call_kind.startswith("at-cap") or call_kind in {"change", "inline"}
    calls = [
        {"id": f"call_change_{i}", "name": "set_metadata", "arguments": {"patch": {"name": f"change-{i}"}}}
        for i in range(10 if at_cap else 0)
    ]
    tool_name, arguments = (
        ("patch_node_options", {"node_id": "absent", "patch": {}}) if "refusal" in call_kind else ("set_metadata", {"patch": {}})
    )
    if call_kind == "change":
        tool_name, arguments = "set_metadata", {"patch": {"name": "excess"}}
    if call_kind == "argument":
        tool_name, arguments = "set_metadata", {"patch": "invalid"}
    if call_kind == "inline":
        tool_name, arguments = "set_pipeline", _inline_pipeline_args(tmp_path)
    if call_kind == "at-cap-blob-refusal":
        tool_name, arguments = "delete_blob", {"blob_id": str(uuid4())}
    calls.extend({"id": f"call_tail_{i}", "name": tool_name, "arguments": arguments} for i in range(1 if at_cap else 11))
    llm = _ScriptedLLM(_fake_llm_response(tool_calls=tuple(calls)), _fake_llm_response(content="Done."))
    with patch.object(harness.service._provider_gateway, "_call_llm", new=llm):
        if call_kind in {"change", "inline"}:
            with pytest.raises(ComposerConvergenceError) as raised:
                await harness.service.compose(
                    "Review these edits.",
                    [],
                    state,
                    session_id=harness.session_id,
                    user_id="proposal-prevalidation-user",
                    user_message_id=harness.user_message_id,
                )
            assert dict(raised.value.evidence) == {"observed": 11, "cap": 10, "cap_kind": "pending_proposals"}
            assert len(raised.value.tool_invocations) == 10
        else:
            result = await harness.service.compose(
                "Review these edits.",
                [],
                state,
                session_id=harness.session_id,
                user_id="proposal-prevalidation-user",
                user_message_id=harness.user_message_id,
            )
            assert len(result.tool_invocations) == len(calls)
            assert result.state is state
    proposals = await harness.sessions.list_composition_proposals(UUID(harness.session_id))
    assert len(proposals) == (10 if at_cap else 0)
    assert _count_rows(harness.engine, blobs_table) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("named_sources", (False, True), ids=("singleton-source", "named-source"))
async def test_identical_canonical_candidate_returns_unchanged_without_proposal(tmp_path: Path, named_sources: bool) -> None:
    harness = _harness(tmp_path)
    arguments = _valid_pipeline_args(tmp_path)
    if named_sources:
        arguments["sources"] = {"orders": arguments.pop("source")}
    state = CompositionState.from_dict(
        {
            **arguments,
            "version": 7,
            "metadata": {"description": "", **arguments["metadata"]},
            "outputs": [{"name": output["sink_name"], **output} for output in arguments["outputs"]],
        }
    )
    llm = _ScriptedLLM(
        _tool_turn("call_identical", "set_pipeline", arguments),
        _fake_llm_response(content="The pipeline is unchanged."),
    )
    with (
        patch.object(harness.service._provider_gateway, "_call_llm", new=llm),
        _record_phase3_outcomes(harness.service) as batches,
    ):
        result = await harness.service.compose(
            "Keep the current pipeline.",
            [],
            state,
            session_id=harness.session_id,
            user_id="proposal-prevalidation-user",
            user_message_id=harness.user_message_id,
        )
    assert await harness.sessions.list_composition_proposals(UUID(harness.session_id)) == []
    assert result.state is state
    assert len(result.tool_invocations) == 1
    outcome = batches[0][0]
    assert outcome.response.success is True
    assert outcome.response.updated_state is state
    assert outcome.response.affected_nodes == ()
    assert outcome.response.prior_validation is None
    assert "PREVALIDATION_REJECTED" not in canonical_json(outcome.response.to_dict())
    assert _count_rows(harness.engine, blobs_table) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("family", "changed", "patch_kind"),
    [(family, changed, "empty") for family in ("metadata", "source", "node", "output", "edge", "replacement") for changed in (False, True)]
    + [(family, False, patch_kind) for family in ("source", "node", "output") for patch_kind in ("same-value", "absent-null")],
)
async def test_semantic_admission_across_incremental_mutation_families(
    tmp_path: Path,
    family: str,
    changed: bool,
    patch_kind: str,
) -> None:
    harness = _harness(tmp_path)
    pipeline = _valid_pipeline_args(tmp_path)
    pipeline["source"]["on_success"] = "rows"
    pipeline["nodes"] = [
        {
            "id": "map",
            "node_type": "transform",
            "plugin": "field_mapper",
            "input": "rows",
            "on_success": "main",
            "on_error": "discard",
            "options": {"schema": {"mode": "observed"}, "mapping": {}},
        }
    ]
    pipeline["edges"] = [{"id": "incoming", "from_node": "source", "to_node": "map", "edge_type": "on_success", "label": None}]
    state = CompositionState.from_dict(
        {
            **pipeline,
            "version": 7,
            "metadata": {"description": "", **pipeline["metadata"]},
            "outputs": [{"name": output["sink_name"], **output} for output in pipeline["outputs"]],
        }
    )
    cases = {
        "metadata": ("set_metadata", {"patch": {"name": "changed" if changed else "proposal-valid"}}),
        "source": ("patch_source_options", {"patch": {"delimiter": ";"} if changed else {}}),
        "node": ("patch_node_options", {"node_id": "map", "patch": {"select_only": True} if changed else {}}),
        "output": ("patch_output_options", {"sink_name": "main", "patch": {"format": "json"} if changed else {}}),
        "edge": ("upsert_edge", {**pipeline["edges"][0], "label": "changed" if changed else None}),
        "replacement": ("set_pipeline", {**pipeline, "metadata": {"name": "changed" if changed else "proposal-valid"}}),
    }
    tool_name, arguments = cases[family]
    if patch_kind == "same-value":
        arguments["patch"] = {"source": {"schema": {"mode": "observed"}}, "node": {"mapping": {}}, "output": {"format": "jsonl"}}[family]
    elif patch_kind == "absent-null":
        arguments["patch"] = {"not_present": None}
    llm = _ScriptedLLM(_tool_turn("call_family", tool_name, arguments), _fake_llm_response(content="Done."))
    with (
        patch.object(harness.service._provider_gateway, "_call_llm", new=llm),
        _record_phase3_outcomes(harness.service) as batches,
    ):
        result = await harness.service.compose(
            "Review this pipeline edit.",
            [],
            state,
            session_id=harness.session_id,
            user_id="proposal-prevalidation-user",
            user_message_id=harness.user_message_id,
        )
    proposals = await harness.sessions.list_composition_proposals(UUID(harness.session_id))
    assert len(proposals) == int(changed)
    assert result.state is state
    assert len(result.tool_invocations) == 1
    outcome = batches[0][0]
    assert outcome.error_class is None
    assert outcome.response.success is True
    assert outcome.post_version == state.version
    if not changed:
        payload = outcome.response.to_dict()
        assert payload["affected_nodes"] == []
        assert "validation_delta" not in payload
        assert "applied_component" not in payload
        assert any("unchanged" in hint for hint in payload["post_call_hints"])


@pytest.mark.asyncio
async def test_noop_between_failures_neither_clears_nor_increments_anti_anchor(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    state = _incremental_base_state(tmp_path)
    refusal = {"node_id": "absent", "patch": {}}
    llm = _ScriptedLLM(
        _tool_turn("call_failure_1", "patch_node_options", refusal),
        _tool_turn("call_noop", "set_metadata", {"patch": {}}),
        _tool_turn("call_failure_2", "patch_node_options", refusal),
        _tool_turn("call_failure_3", "patch_node_options", refusal),
        _fake_llm_response(content="A target must be configured."),
    )
    with patch.object(harness.service._provider_gateway, "_call_llm", new=llm):
        result = await harness.service.compose(
            "Update an existing node.",
            [],
            state,
            session_id=harness.session_id,
            user_id="proposal-prevalidation-user",
            user_message_id=harness.user_message_id,
        )
    assert await harness.sessions.list_composition_proposals(UUID(harness.session_id)) == []
    assert len(result.tool_invocations) == 4
    hints = [
        message for message in llm.message_snapshots[-1] if message.get("role") == "user" and "[ELSPETH-SYSTEM-HINT]" in message["content"]
    ]
    assert len(hints) == 1
    assert "last 3 calls to `patch_node_options`" in hints[0]["content"]


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", (False, True), ids=("version-incrementing-noop", "change"))
async def test_immediate_auto_metadata_retains_existing_version_behavior(tmp_path: Path, changed: bool) -> None:
    harness = _harness(tmp_path)
    await harness.sessions.update_composer_preferences(
        UUID(harness.session_id),
        trust_mode="auto_commit",
        density_default="high",
        actor="user:proposal-prevalidation-user",
    )
    state = _incremental_base_state(tmp_path)
    llm = _ScriptedLLM(
        _tool_turn("call_auto_metadata", "set_metadata", {"patch": {"name": "changed"} if changed else {}}),
        _fake_llm_response(content="Done."),
    )
    with patch.object(harness.service._provider_gateway, "_call_llm", new=llm):
        result = await harness.service.compose(
            "Update metadata.",
            [],
            state,
            session_id=harness.session_id,
            user_id="proposal-prevalidation-user",
            user_message_id=harness.user_message_id,
        )
    assert result.state.version == state.version + 1
    assert result.state.metadata.name == ("changed" if changed else state.metadata.name)
    assert await harness.sessions.list_composition_proposals(UUID(harness.session_id)) == []


class _AdmissionSecretInventory:
    def list_refs(self, user_id: str) -> list[SecretInventoryItem]:
        assert user_id == "proposal-prevalidation-user"
        return [SecretInventoryItem(name="PIPELINE_API_KEY", scope="user", available=True, source_kind="stored")]

    def has_ref(self, user_id: str, name: str) -> bool:
        assert user_id == "proposal-prevalidation-user"
        return name == "PIPELINE_API_KEY"

    def resolve(self, user_id: str, name: str) -> ResolvedSecret | None:
        raise AssertionError("Admission must never resolve secret values")


@pytest.mark.asyncio
@pytest.mark.parametrize("policy_match", (True, False), ids=("exact-allowlist", "near-match-denied"))
@pytest.mark.parametrize("already_wired", (False, True), ids=("unwired", "same-marker"))
async def test_secret_marker_admission_uses_policy_once_without_resolving_values(
    tmp_path: Path,
    policy_match: bool,
    already_wired: bool,
) -> None:
    from elspeth.web.composer.tools import secrets

    rules = (
        SecretWiringRuleSettings(
            secret="PIPELINE_API_KEY",
            component_type="source",
            plugin="csv" if policy_match else "csv_near_match",
            option_key="api_key",
        ),
    )
    harness = _harness(tmp_path, secret_service=_AdmissionSecretInventory(), secret_rules=rules)
    state = _incremental_base_state(tmp_path)
    if already_wired:
        source = state.sources["source"]
        state = state.with_source(replace(source, options={**deep_thaw(source.options), "api_key": {"secret_ref": "PIPELINE_API_KEY"}}))
    llm = _ScriptedLLM(
        _tool_turn("call_secret", "wire_secret_ref", {"name": "PIPELINE_API_KEY", "target": "source", "option_key": "api_key"}),
        _fake_llm_response(content="Done."),
    )
    with (
        patch.object(harness.service._provider_gateway, "_call_llm", new=llm),
        patch.object(secrets, "_reconciled_secret_wire_result", wraps=secrets._reconciled_secret_wire_result) as reconciliation,
        _record_phase3_outcomes(harness.service) as batches,
    ):
        result = await harness.service.compose(
            "Use the available secret reference.",
            [],
            state,
            session_id=harness.session_id,
            user_id="proposal-prevalidation-user",
            user_message_id=harness.user_message_id,
        )
    proposals = await harness.sessions.list_composition_proposals(UUID(harness.session_id))
    assert len(proposals) == int(policy_match and not already_wired)
    assert result.state is state
    assert len(result.tool_invocations) == 1
    assert reconciliation.call_count == int(policy_match)
    assert batches[0][0].response.success is policy_match
    if not policy_match:
        assert any(entry.error_code == "unauthorized_secret_ref" for entry in batches[0][0].response.validation.errors)


@pytest.mark.asyncio
@pytest.mark.parametrize("blob_kind", ("user", "same-marker", "not-ready", "missing", "llm"))
async def test_blob_marker_admission_checks_real_metadata_and_modality(tmp_path: Path, blob_kind: str) -> None:
    from elspeth.web.composer.tools import blobs
    from tests.helpers.session_fences import fenced_operation_context
    from tests.unit.web.composer.test_blob_inline_tools import _inline_ref_state

    harness = _harness(tmp_path)
    state = _inline_ref_state()
    blob_id = uuid4()
    if blob_kind in {"user", "same-marker", "not-ready"}:
        with fenced_operation_context(harness.engine, harness.session_id, operation_kind=SessionOperationKind.CREATE) as operation:
            blob = await BlobServiceImpl(harness.engine, tmp_path).create_blob(
                UUID(harness.session_id),
                "prompt.txt",
                b"A user supplied prompt",
                "text/plain",
                session_operation_context=operation,
            )
        blob_id = blob.id
        if blob_kind == "not-ready":
            from tests.unit.web.composer.test_blob_inline_tools import _mark_blob_pending

            _mark_blob_pending({"engine": harness.engine}, str(blob_id))
    elif blob_kind == "llm":
        snapshot = PluginAvailabilitySnapshot.for_trained_operator(harness.catalog)
        view = PolicyCatalogView.for_trained_operator(harness.catalog, snapshot)
        with fenced_operation_context(harness.engine, harness.session_id, operation_kind=SessionOperationKind.COMPOSE) as operation:
            created = real_execute_tool(
                "create_blob",
                {"filename": "prompt.txt", "mime_type": "text/plain", "content": "Generated prompt content"},
                state,
                view,
                plugin_snapshot=snapshot,
                data_dir=str(tmp_path),
                session_engine=harness.engine,
                session_id=harness.session_id,
                session_operation_context=operation,
                session_operation_authority=harness.sessions.session_operation_authority,
                user_message_id=harness.user_message_id,
                user_message_content="Generate a prompt.",
                composer_model_identifier="test-model",
                composer_model_version="test-v1",
                composer_provider="test",
                composer_skill_hash="a" * 64,
                tool_arguments_hash="b" * 64,
            )
        assert created.success is True
        blob_id = UUID(created.data["blob_id"])
        with harness.engine.connect() as conn:
            assert (
                conn.execute(select(blobs_table.c.creation_modality).where(blobs_table.c.id == str(blob_id))).scalar_one()
                == "llm_generated"
            )
    if blob_kind == "same-marker":
        snapshot = PluginAvailabilitySnapshot.for_trained_operator(harness.catalog)
        view = PolicyCatalogView.for_trained_operator(harness.catalog, snapshot)
        wired = real_execute_tool(
            "wire_blob_inline_ref",
            {"field_path": "node:classify.options.prompt_template", "blob_id": str(blob_id)},
            state,
            view,
            plugin_snapshot=snapshot,
            session_engine=harness.engine,
            session_id=harness.session_id,
        )
        assert wired.success is True
        state = wired.updated_state
    llm = _ScriptedLLM(
        _tool_turn(
            "call_blob_marker", "wire_blob_inline_ref", {"field_path": "node:classify.options.prompt_template", "blob_id": str(blob_id)}
        ),
        _fake_llm_response(content="Done."),
    )
    with (
        patch.object(harness.service._provider_gateway, "_call_llm", new=llm),
        patch.object(blobs, "_sync_get_blob", wraps=blobs._sync_get_blob) as metadata_lookup,
        _record_phase3_outcomes(harness.service) as batches,
    ):
        result = await harness.service.compose(
            "Use the supplied prompt artifact.",
            [],
            state,
            session_id=harness.session_id,
            user_id="proposal-prevalidation-user",
            user_message_id=harness.user_message_id,
        )
    assert result.state is state
    assert len(result.tool_invocations) == 1
    proposals = await harness.sessions.list_composition_proposals(UUID(harness.session_id))
    assert len(proposals) == int(blob_kind == "user")
    assert metadata_lookup.call_count == 1
    assert batches[0][0].response.success is (blob_kind in {"user", "same-marker"})
    assert _count_rows(harness.engine, blobs_table) == int(blob_kind != "missing")


@pytest.mark.asyncio
async def test_successful_noop_preserves_standing_invalid_validation_and_preflight(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    state = _incremental_base_state(tmp_path)
    llm = _ScriptedLLM(
        _tool_turn("call_preview", "preview_pipeline", {}),
        _tool_turn("call_noop", "set_metadata", {"patch": {}}),
        _fake_llm_response(content="The current draft still needs an output."),
    )
    dispatches = []
    dispatch = harness.service._dispatch_tool_batch

    async def record_dispatch(**kwargs: Any) -> Any:
        dispatched = await dispatch(**kwargs)
        dispatches.append(dispatched[0])
        return dispatched

    with (
        patch.object(harness.service._provider_gateway, "_call_llm", new=llm),
        patch.object(harness.service, "_dispatch_tool_batch", new=record_dispatch),
    ):
        result = await harness.service.compose(
            "Inspect the draft and keep its metadata.",
            [],
            state,
            session_id=harness.session_id,
            user_id="proposal-prevalidation-user",
            user_message_id=harness.user_message_id,
        )
    assert len(dispatches) == 2
    preview, noop = dispatches
    assert preview.last_validation.is_valid is False
    assert preview.last_runtime_preflight is not None
    assert noop.last_validation is preview.last_validation
    assert noop.last_runtime_preflight is preview.last_runtime_preflight
    assert noop.state is state
    assert noop.mutation_success_observed is False
    response = noop.tool_outcomes[0].response
    assert response.success is True
    assert response.validation is preview.last_validation
    assert response.runtime_preflight is preview.last_runtime_preflight
    assert response.to_dict()["affected_nodes"] == []
    assert "validation_delta" not in response.to_dict()
    assert result.state is state
    assert await harness.sessions.list_composition_proposals(UUID(harness.session_id)) == []


@pytest.mark.asyncio
async def test_equal_incremental_edit_stages_required_controls_before_hash_comparison(tmp_path: Path) -> None:
    from elspeth.web.composer.tools import execute_tool
    from tests.integration.web.composer.test_freeform_required_controls import _incremental_textract_state, _required_textract_policy

    harness = _harness(tmp_path)
    view, snapshot = _required_textract_policy(tmp_path)
    incomplete, output = await _incremental_textract_state(tmp_path, harness=harness, view=view, snapshot=snapshot)
    completed = execute_tool(
        "set_output",
        output,
        incomplete,
        view,
        plugin_snapshot=snapshot,
        data_dir=str(tmp_path),
        session_engine=harness.engine,
        session_id=harness.session_id,
        user_id="proposal-prevalidation-user",
        validate_arguments=True,
        require_data_dir_for_paths=True,
        raise_schema_argument_errors=True,
    )
    assert completed.success is True
    assert completed.validation.is_valid is True
    state = completed.updated_state
    assert not any(node.plugin.startswith("aws_bedrock_") for node in state.nodes)
    llm = _ScriptedLLM(
        _tool_turn("call_controls", "set_metadata", {"patch": {}}),
        _fake_llm_response(content="Required controls await approval."),
    )
    with (
        patch.object(harness.service._policy_context, "build", return_value=(snapshot, view)),
        patch.object(harness.service._provider_gateway, "_call_llm", new=llm),
    ):
        result = await harness.service.compose(
            "Keep metadata and prepare required controls.",
            [],
            state,
            session_id=harness.session_id,
            user_id="proposal-prevalidation-user",
            user_message_id=harness.user_message_id,
        )
    assert result.state is state
    proposals = await harness.sessions.list_composition_proposals(UUID(harness.session_id))
    assert len(proposals) == 1
    assert proposals[0].tool_name == "set_pipeline"
    authority = deep_thaw(proposals[0].arguments_json)
    assert authority["authority_kind"] == "owned_composition_state.v1"
    assert {node["plugin"] for node in authority["nodes"]} >= {"aws_bedrock_prompt_shield", "aws_bedrock_content_safety"}
    assert any(INTERPRETATION_REQUIREMENTS_KEY in node["options"] for node in authority["nodes"])


@pytest.mark.asyncio
async def test_canonical_source_order_change_remains_stageable(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    arguments = _valid_pipeline_args(tmp_path)
    source = arguments.pop("source")
    arguments["sources"] = {"orders": source, "refunds": {**source, "on_success": "refunds_out"}}
    arguments["outputs"].append(
        {
            **arguments["outputs"][0],
            "sink_name": "refunds_out",
            "options": {**arguments["outputs"][0]["options"], "path": str(tmp_path / "outputs" / harness.session_id / "refunds.jsonl")},
        }
    )
    state = CompositionState.from_dict(
        {
            **arguments,
            "version": 4,
            "metadata": {"description": "", **arguments["metadata"]},
            "outputs": [{"name": output["sink_name"], **output} for output in arguments["outputs"]],
        }
    )
    arguments["sources"] = dict(reversed(tuple(arguments["sources"].items())))
    llm = _ScriptedLLM(_tool_turn("call_reorder", "set_pipeline", arguments), _fake_llm_response(content="Order change awaits approval."))
    with patch.object(harness.service._provider_gateway, "_call_llm", new=llm):
        result = await harness.service.compose(
            "Change the source order.",
            [],
            state,
            session_id=harness.session_id,
            user_id="proposal-prevalidation-user",
            user_message_id=harness.user_message_id,
        )
    assert result.state is state
    proposals = await harness.sessions.list_composition_proposals(UUID(harness.session_id))
    assert len(proposals) == 1
    assert tuple(proposals[0].arguments_json["sources"]) == ("refunds", "orders")


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_name", ("update_blob", "delete_blob"))
@pytest.mark.parametrize("content", ("replacement bytes", "original bytes"))
async def test_durable_blob_mutations_admit_and_stage_without_executing_effect_handler(
    tmp_path: Path, tool_name: str, content: str
) -> None:
    from tests.helpers.session_fences import fenced_operation_context

    harness = _harness(tmp_path)
    state = _incremental_base_state(tmp_path)
    with fenced_operation_context(harness.engine, harness.session_id, operation_kind=SessionOperationKind.CREATE) as operation:
        blob = await BlobServiceImpl(harness.engine, tmp_path).create_blob(
            UUID(harness.session_id),
            "retained.txt",
            b"original bytes",
            "text/plain",
            session_operation_context=operation,
        )
    arguments = {"blob_id": str(blob.id)}
    if tool_name == "update_blob":
        arguments["content"] = content
    llm = _ScriptedLLM(_tool_turn("call_durable", tool_name, arguments), _fake_llm_response(content="The blob change awaits approval."))
    with (
        patch.object(harness.service._provider_gateway, "_call_llm", new=llm),
        patch("elspeth.web.composer.tool_batch.execute_tool", side_effect=AssertionError("Durable tool must never be previewed")),
    ):
        result = await harness.service.compose(
            "Review this blob change.",
            [],
            state,
            session_id=harness.session_id,
            user_id="proposal-prevalidation-user",
            user_message_id=harness.user_message_id,
        )
    assert result.state is state
    assert len(result.tool_invocations) == 1
    assert Path(blob.storage_path).read_bytes() == b"original bytes"
    proposals = await harness.sessions.list_composition_proposals(UUID(harness.session_id))
    assert [proposal.tool_name for proposal in proposals] == [tool_name]
    assert _count_rows(harness.engine, blobs_table) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tool_name,refusal",
    [(tool, refusal) for tool in ("update_blob", "delete_blob") for refusal in ("nonready", "bound", "missing")]
    + [("update_blob", "quota")],
)
async def test_blob_admission_refuses_known_ineligible_effect_once_without_proposal(tmp_path: Path, tool_name: str, refusal: str) -> None:
    from sqlalchemy import update

    from tests.helpers.session_fences import fenced_operation_context

    harness = _harness(tmp_path)
    state = _incremental_base_state(tmp_path)
    with fenced_operation_context(harness.engine, harness.session_id, operation_kind=SessionOperationKind.CREATE) as operation:
        blob = await BlobServiceImpl(harness.engine, tmp_path).create_blob(
            UUID(harness.session_id), "retained.txt", b"original bytes", "text/plain", session_operation_context=operation
        )
    if refusal == "nonready":
        with harness.engine.begin() as connection:
            connection.execute(update(blobs_table).where(blobs_table.c.id == str(blob.id)).values(status="pending"))
    elif refusal == "bound":
        state = replace(
            state,
            sources={
                name: replace(source, options={**source.options, "path": blob.storage_path}) for name, source in state.sources.items()
            },
        )
    elif refusal == "quota":
        harness.service._settings = harness.service._settings.model_copy(update={"max_blob_storage_per_session_bytes": 15})
    arguments = {"blob_id": str(uuid4()) if refusal == "missing" else str(blob.id)}
    if tool_name == "update_blob":
        arguments["content"] = "replacement bytes"
    llm = _ScriptedLLM(
        _tool_turn("call_refused_blob", tool_name, arguments),
        _tool_turn("call_repair", "set_metadata", {"patch": {"name": "repaired draft"}}),
        _fake_llm_response(content="The repair awaits approval."),
    )
    with patch.object(harness.service._provider_gateway, "_call_llm", new=llm), _record_phase3_outcomes(harness.service) as batches:
        result = await harness.service.compose(
            "Review this blob change.",
            [],
            state,
            session_id=harness.session_id,
            user_id="proposal-prevalidation-user",
            user_message_id=harness.user_message_id,
        )
    proposals = await harness.sessions.list_composition_proposals(UUID(harness.session_id))
    assert [proposal.tool_call_id for proposal in proposals] == ["call_repair"]
    feedback = [message for message in llm.message_snapshots[1] if message.get("tool_call_id") == "call_refused_blob"]
    assert len(feedback) == 1
    assert json.loads(feedback[0]["content"])["success"] is False
    outcomes = [outcome for batch in batches for outcome in batch if outcome.call.function.name == tool_name]
    assert len(outcomes) == 1
    assert outcomes[0].pre_version == outcomes[0].post_version == state.version
    assert result.state is state
    assert Path(blob.storage_path).read_bytes() == b"original bytes"


@pytest.mark.asyncio
async def test_inline_same_text_with_new_blob_authority_is_a_real_change(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    state = _incremental_base_state(tmp_path)
    arguments = _inline_pipeline_args(tmp_path)
    candidates = []

    def record_candidate(*args: Any, **kwargs: Any) -> Any:
        candidate = real_build_set_pipeline_candidate(*args, **kwargs)
        candidates.append(candidate)
        return candidate

    first_llm = _ScriptedLLM(_tool_turn("call_first_inline", "set_pipeline", arguments), _fake_llm_response(content="Ready for review."))
    with (
        patch.object(harness.service._provider_gateway, "_call_llm", new=first_llm),
        patch("elspeth.web.composer.tool_batch.build_set_pipeline_candidate", side_effect=record_candidate),
    ):
        await harness.service.compose(
            "Prepare inline data.",
            [],
            state,
            session_id=harness.session_id,
            user_id="proposal-prevalidation-user",
            user_message_id=harness.user_message_id,
        )
    current = candidates[-1].result.updated_state
    assert candidates[-1].acceptable
    first_proposals = await harness.sessions.list_composition_proposals(UUID(harness.session_id))
    assert len(first_proposals) == 1
    original_blob_id = first_proposals[0].arguments_json["source"]["blob_id"]
    arguments["source"]["inline_blob"]["filename"] = "new-authority.csv"
    second_llm = _ScriptedLLM(
        _tool_turn("call_new_inline_authority", "set_pipeline", arguments), _fake_llm_response(content="Ready for review.")
    )
    with patch.object(harness.service._provider_gateway, "_call_llm", new=second_llm):
        result = await harness.service.compose(
            "Prepare another inline artifact with identical content.",
            [],
            current,
            session_id=harness.session_id,
            user_id="proposal-prevalidation-user",
            user_message_id=harness.user_message_id,
        )
    proposals = await harness.sessions.list_composition_proposals(UUID(harness.session_id))
    assert len(proposals) == 2
    second = next(proposal for proposal in proposals if proposal.tool_call_id == "call_new_inline_authority")
    assert second.arguments_json["source"]["blob_id"] != original_blob_id
    assert result.state is current
    with harness.engine.connect() as conn:
        rows = conn.execute(select(blobs_table.c.content_hash, blobs_table.c.storage_path)).all()
    assert len(rows) == 2
    assert rows[0].content_hash == rows[1].content_hash
    assert Path(rows[0].storage_path).read_bytes() == Path(rows[1].storage_path).read_bytes()


@pytest.mark.asyncio
async def test_auto_commit_set_pipeline_uses_candidate_builder_once(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    state = _incremental_base_state(tmp_path)
    await harness.sessions.update_composer_preferences(
        UUID(harness.session_id),
        trust_mode="auto_commit",
        density_default="high",
        actor="user:proposal-prevalidation-user",
    )
    llm = _ScriptedLLM(
        _tool_turn("call_auto", "set_pipeline", _valid_pipeline_args(tmp_path, metadata_name="auto")),
        _fake_llm_response(content="Applied."),
    )

    with (
        patch.object(harness.service._provider_gateway, "_call_llm", new=llm),
        patch(
            "elspeth.web.composer.tools.sessions.build_set_pipeline_candidate",
            wraps=real_build_set_pipeline_candidate,
        ) as builder,
    ):
        result = await harness.service.compose(
            "Build and apply a pipeline.",
            [],
            state,
            session_id=harness.session_id,
            user_id="proposal-prevalidation-user",
            user_message_id=harness.user_message_id,
        )

    assert builder.call_count == 1
    assert result.state.version == state.version + 1
    assert await harness.sessions.list_composition_proposals(UUID(harness.session_id)) == []
