"""Preview dispatch must use the policy admitted for its complete request."""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

from elspeth.contracts.composer_mutation import ComposerMutationCeiling
from elspeth.contracts.session_operation import SessionOperationContext, SessionOperationFence, SessionOperationKind
from elspeth.plugins.infrastructure.manager import get_shared_plugin_manager
from elspeth.web.blobs.protocol import BlobRecord
from elspeth.web.catalog.protocol import CatalogService
from elspeth.web.composer import composer_preflight as preflight_module
from elspeth.web.composer._compose_loop_carriers import _CallModelOutcome, _DispatchOutcome
from elspeth.web.composer.anti_anchor import AntiAnchorTracker
from elspeth.web.composer.audit import BufferingRecorder
from elspeth.web.composer.discovery_cache import RuntimePreflightCache
from elspeth.web.composer.provider_gateway import _admit_composer_llm_completion
from elspeth.web.composer.service import ComposerServiceImpl
from elspeth.web.composer.state import CompositionState, OutputSpec, PipelineMetadata, SourceSpec
from elspeth.web.dependencies import create_catalog_service
from elspeth.web.execution.schemas import ValidationReadiness, ValidationReadinessBlocker, ValidationResult
from elspeth.web.plugin_policy.compiler import compile_web_plugin_policy
from elspeth.web.plugin_policy.models import PluginAvailability, PluginAvailabilitySnapshot, PluginId, PluginUnavailableReason
from elspeth.web.plugin_policy.profiles import OperatorProfileRegistry, RuntimeWebPluginConfig
from elspeth.web.sessions.protocol import SessionServiceProtocol
from tests.unit.web.execution.test_validate_blob_inline import BLOB_ID, _ready_blob_record, _state_with_reference_join

from .conftest import _fake_llm_response, _make_settings


def _snapshot(*, csv_available: bool) -> PluginAvailabilitySnapshot:
    source = PluginId("source", "csv")
    return PluginAvailabilitySnapshot.create(
        policy_hash="preview-policy",
        principal_scope="local:alice",
        available=frozenset({PluginId("sink", "csv"), *([source] if csv_available else [])}),
        unavailable=() if csv_available else (PluginAvailability(source, PluginUnavailableReason.NOT_AUTHORIZED),),
        selected=(),
        usable_profile_aliases=(),
        selected_profile_aliases=(),
        binding_generation_fingerprint="preview-generation",
    )


def _registry(tmp_path: Path) -> OperatorProfileRegistry:
    settings = _make_settings(tmp_path)
    config = RuntimeWebPluginConfig.from_settings(settings)
    policy = compile_web_plugin_policy(registry=get_shared_plugin_manager(), settings=config)
    return OperatorProfileRegistry(policy=policy, settings=config)


def _service(
    tmp_path: Path,
    current_snapshot: PluginAvailabilitySnapshot,
    catalog: CatalogService,
    *,
    registry: OperatorProfileRegistry | None = None,
    sessions_service: SessionServiceProtocol | None = None,
) -> ComposerServiceImpl:
    settings = _make_settings(tmp_path)
    return ComposerServiceImpl(
        catalog=catalog,
        settings=settings,
        plugin_snapshot_factory=lambda _user_id: current_snapshot,
        operator_profile_registry=registry if registry is not None else _registry(tmp_path),
        sessions_service=sessions_service,
    )


async def _dispatch_tool_batch(
    service: ComposerServiceImpl,
    *,
    call_model: _CallModelOutcome,
    mutation_ceiling: ComposerMutationCeiling | None,
    state: CompositionState,
    last_validation: Any,
    last_runtime_preflight: ValidationResult | None,
    llm_messages: list[dict[str, Any]],
    recorder: BufferingRecorder,
    anti_anchor: AntiAnchorTracker,
    discovery_cache: dict[str, Any],
    runtime_preflight_cache: RuntimePreflightCache,
    session_id: str | None,
    user_id: str | None,
    user_message_id: str | None,
    user_message_content: str | None,
    current_state_id: str | None,
    actor: str,
    initial_version: int,
    deadline: float,
    progress: Any,
    session_scope: str,
    advisor_calls_used: int,
    composition_turns_used: int,
    discovery_turns_used: int,
    failed_turn: Any,
    cancellation_requested: asyncio.Event,
    plugin_snapshot: PluginAvailabilitySnapshot,
    policy_catalog: Any,
    session_operation_context: SessionOperationContext | None = None,
) -> tuple[_DispatchOutcome, int]:
    """Drive the shared dispatch owner ``tool_batch.run_tool_batch`` from a service.

    Builds the batch context exactly as the service did before the V1 loop
    adapter was removed; Continuation builds the same context from its
    restored runtime (``continuation_runtime.build_runtime_tool_batch_context``).
    """
    from elspeth.web.composer.tool_batch import BatchAccumulator, ToolBatchContext, ToolBatchCustody, ToolBatchProvenance, run_tool_batch

    sessions = service._require_sessions_service() if session_id is not None else None
    session_uuid = UUID(session_id) if session_id is not None else None
    preferences = await sessions.get_composer_preferences(session_uuid) if sessions is not None and session_uuid is not None else None
    review_required = False
    if session_uuid is not None:
        assert type(mutation_ceiling) is ComposerMutationCeiling and preferences is not None
        assert mutation_ceiling.session_id == str(session_uuid)
        if session_operation_context is not None:
            mutation_ceiling.validate_context(session_operation_context)
        review_required = not mutation_ceiling.permits_auto(preferences.trust_mode)
    else:
        assert mutation_ceiling is None
    ctx = ToolBatchContext(
        session_tools=service._session_tools,
        provenance=ToolBatchProvenance(
            model_identifier=service._model, provider=service._availability.provider, skill_hash=service._composer_skill_hash
        ),
        custody=ToolBatchCustody(
            data_dir=service._data_dir,
            session_engine=service._session_engine,
            secret_service=service._secret_service,
            secret_wiring_policy=service._secret_wiring_policy,
            max_blob_storage_per_session_bytes=service._settings.max_blob_storage_per_session_bytes,
        ),
        redaction_telemetry=service._redaction_telemetry,
        preflight=service._preflight,
        schema_disclosure=service._schema_disclosure,
        advisor_checkpoint=service._advisor_checkpoint,
        advisor_max_calls_per_compose=service._settings.composer_advisor_max_calls_per_compose,
        advisor_timeout_seconds=service._settings.composer_advisor_timeout_seconds,
        recorder=recorder,
        anti_anchor=anti_anchor,
        discovery_cache=discovery_cache,
        runtime_preflight_cache=runtime_preflight_cache,
        session_id=session_id,
        session_operation_context=session_operation_context,
        user_id=user_id,
        user_message_id=user_message_id,
        user_message_content=user_message_content,
        current_state_id=current_state_id,
        actor=actor,
        initial_version=initial_version,
        deadline=deadline,
        progress=progress,
        session_scope=session_scope,
        turn_sessions_service=sessions,
        turn_session_uuid=session_uuid,
        turn_preferences=preferences,
        mutation_ceiling=mutation_ceiling,
        review_required=review_required,
        cancellation_requested=cancellation_requested,
        plugin_snapshot=plugin_snapshot,
        policy_catalog=policy_catalog,
        tool_contract_dialect=service._planner_dialect,
        session_operation_authority=sessions.session_operation_authority if sessions is not None else None,
        composition_turns_used=composition_turns_used,
        discovery_turns_used=discovery_turns_used,
        failed_turn=failed_turn,
    )
    acc = BatchAccumulator(
        state=state, last_validation=last_validation, last_runtime_preflight=last_runtime_preflight, advisor_calls_used=advisor_calls_used
    )
    return await run_tool_batch(call_model=call_model, ctx=ctx, acc=acc, llm_messages=llm_messages)


def _state(tmp_path: Path) -> CompositionState:
    return CompositionState(
        source=SourceSpec(
            plugin="csv",
            on_success="main",
            options={"path": str(tmp_path / "blobs" / "input.csv"), "schema": {"mode": "observed"}},
            on_validation_failure="discard",
        ),
        nodes=(),
        edges=(),
        outputs=(
            OutputSpec(
                name="main",
                plugin="csv",
                options={"path": str(tmp_path / "outputs" / "output.csv"), "schema": {"mode": "observed"}},
                on_write_failure="discard",
            ),
        ),
        metadata=PipelineMetadata(),
        version=1,
    )


async def _preview(
    service: ComposerServiceImpl,
    state: CompositionState,
    admitted: PluginAvailabilitySnapshot,
    cache: RuntimePreflightCache,
    registry: OperatorProfileRegistry,
    catalog: CatalogService,
    operation_context: SessionOperationContext | None = None,
    mutation_ceiling: ComposerMutationCeiling | None = None,
) -> _DispatchOutcome:
    from elspeth.web.catalog.policy_view import PolicyCatalogView

    policy_catalog = PolicyCatalogView(catalog, admitted, registry)
    completion = _admit_composer_llm_completion(
        _fake_llm_response(tool_calls=({"id": "preview-call", "name": "preview_pipeline", "arguments": {}},))
    )
    outcome, _advisor_calls = await _dispatch_tool_batch(
        service,
        call_model=_CallModelOutcome(completion=completion),
        mutation_ceiling=mutation_ceiling,
        state=state,
        last_validation=None,
        last_runtime_preflight=None,
        llm_messages=[],
        recorder=BufferingRecorder(),
        anti_anchor=AntiAnchorTracker(),
        discovery_cache={},
        runtime_preflight_cache=cache,
        session_id=None if operation_context is None else operation_context.fence.session_id,
        user_id="alice",
        user_message_id=None,
        user_message_content=None,
        current_state_id=None,
        actor="composer-web:user:alice",
        initial_version=state.version,
        deadline=asyncio.get_running_loop().time() + 60,
        progress=None,
        session_scope="preview-snapshot-test",
        advisor_calls_used=0,
        composition_turns_used=0,
        discovery_turns_used=0,
        failed_turn=None,
        cancellation_requested=asyncio.Event(),
        plugin_snapshot=admitted,
        policy_catalog=policy_catalog,
        session_operation_context=operation_context,
    )
    assert outcome.plugin_crash is None
    assert len(outcome.tool_outcomes) == 1
    assert outcome.tool_outcomes[0].error_class is None
    assert outcome.last_runtime_preflight is not None
    return outcome


@pytest.mark.anyio
async def test_preview_policy_verdict_and_cache_stay_bound_to_admitted_snapshot(tmp_path: Path) -> None:
    """Real policy validation must not re-enable a plugin denied at admission."""
    admitted = _snapshot(csv_available=False)
    subsequent = _snapshot(csv_available=True)
    assert admitted.snapshot_hash != subsequent.snapshot_hash
    registry = _registry(tmp_path)
    catalog = create_catalog_service()
    service = _service(tmp_path, subsequent, catalog, registry=registry)
    state = _state(tmp_path)
    cache = service._preflight.new_cache()

    first = await _preview(service, state, admitted, cache, registry, catalog)
    strict = first.last_runtime_preflight
    assert strict is not None
    assert "plugin_not_enabled" in {error.error_code for error in strict.errors}
    key = service._preflight.key(state, session_scope="preview-snapshot-test", plugin_snapshot=admitted)
    assert cache == {key: strict}

    repeated = await _preview(service, state, admitted, cache, registry, catalog)
    assert repeated.last_runtime_preflight is strict
    later = await _preview(service, state, subsequent, cache, registry, catalog)
    later_strict = later.last_runtime_preflight
    assert later_strict is not None
    assert "plugin_not_enabled" not in {error.error_code for error in later_strict.errors}
    assert any(check.name == "plugin_enablement" and check.passed for check in later_strict.checks)
    later_key = service._preflight.key(state, session_scope="preview-snapshot-test", plugin_snapshot=subsequent)
    assert cache == {key: strict, later_key: later_strict}


@pytest.mark.anyio
async def test_strict_and_tolerant_preview_share_the_admitted_snapshot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Exercise both real cached-preflight branches after a pending-review result."""
    admitted = _snapshot(csv_available=True)
    subsequent = _snapshot(csv_available=False)
    registry = _registry(tmp_path)
    catalog = create_catalog_service()
    service = _service(tmp_path, subsequent, catalog, registry=registry)
    seen: list[tuple[bool, PluginAvailabilitySnapshot]] = []

    def pending_review_preflight(
        *_args: Any,
        plugin_snapshot: PluginAvailabilitySnapshot,
        allow_pending_interpretation_placeholders: bool = False,
        **_kwargs: Any,
    ) -> ValidationResult:
        seen.append((allow_pending_interpretation_placeholders, plugin_snapshot))
        return ValidationResult(
            is_valid=allow_pending_interpretation_placeholders,
            checks=[],
            errors=[],
            readiness=ValidationReadiness(
                authoring_valid=True,
                execution_ready=allow_pending_interpretation_placeholders,
                completion_ready=True,
                blockers=[]
                if allow_pending_interpretation_placeholders
                else [
                    ValidationReadinessBlocker(
                        suggestion=None,
                        note=None,
                        code="interpretation_review_pending",
                        component_id="source",
                        component_type="source",
                        detail="Source interpretation awaits review.",
                    )
                ],
            ),
        )

    monkeypatch.setattr(preflight_module, "validate_pipeline", pending_review_preflight)
    await _preview(service, _state(tmp_path), admitted, service._preflight.new_cache(), registry, catalog)
    assert len(seen) == 2
    assert [tolerant for tolerant, _snapshot_used in seen] == [False, True]
    assert seen[0][1] is admitted
    assert seen[1][1] is admitted


@pytest.mark.anyio
@pytest.mark.parametrize("pending_review", [False, True])
@pytest.mark.parametrize(
    ("reference_format", "table"),
    [("csv", "sku,description\nhats,A fine hat\n"), ("json", '[{"sku":"hats","description":"A fine hat"}]')],
)
async def test_preview_reads_uploaded_reference_table_with_operation_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    pending_review: bool,
    reference_format: str,
    table: str,
    composer_service_with_real_sessions: ComposerServiceImpl,
    result_session_id: str,
) -> None:
    session_id = UUID(result_session_id)
    context = SessionOperationContext(SessionOperationFence(str(session_id), str(uuid4()), str(uuid4()), 1), SessionOperationKind.COMPOSE)
    encoded = table.encode("utf-8")
    digest = hashlib.sha256(encoded).hexdigest()
    record = replace(_ready_blob_record(session_id=session_id), content_hash=digest, size_bytes=len(encoded))
    state = _state_with_reference_join(
        tmp_path,
        session_id=session_id,
        reference_format=reference_format,
        content={"blob_ref": str(BLOB_ID), "mode": "inline_content", "sha256": digest},
    )
    admitted = PluginAvailabilitySnapshot.create(
        policy_hash="preview-blob-policy",
        principal_scope="local:alice",
        available=frozenset({PluginId("source", "csv"), PluginId("transform", "reference_join"), PluginId("sink", "json")}),
        unavailable=(),
        selected=(),
        usable_profile_aliases=(),
        selected_profile_aliases=(),
        binding_generation_fingerprint="preview-blob-generation",
    )
    registry = _registry(tmp_path)
    catalog = create_catalog_service()
    service = _service(
        tmp_path,
        admitted,
        catalog,
        registry=registry,
        sessions_service=composer_service_with_real_sessions._sessions_service,
    )
    reads: list[SessionOperationContext] = []

    class BlobReader:
        def get_blob_sync(self, blob_id: UUID, operation_context: SessionOperationContext) -> BlobRecord:
            assert blob_id == BLOB_ID
            assert operation_context is context
            return record

        def read_blob_content_sync(self, blob_id: UUID, operation_context: SessionOperationContext) -> tuple[BlobRecord, bytes]:
            assert blob_id == BLOB_ID
            assert operation_context is context
            reads.append(operation_context)
            return record, encoded

    monkeypatch.setattr(service._preflight, "_blob_service", BlobReader())
    real_validate = preflight_module.validate_pipeline
    results: list[ValidationResult] = []

    def validate(*args: Any, allow_pending_interpretation_placeholders: bool = False, **kwargs: Any) -> ValidationResult:
        if pending_review and not allow_pending_interpretation_placeholders:
            return ValidationResult(
                is_valid=False,
                checks=[],
                errors=[],
                readiness=ValidationReadiness(
                    authoring_valid=True,
                    execution_ready=False,
                    completion_ready=True,
                    blockers=[
                        ValidationReadinessBlocker(
                            suggestion=None,
                            note=None,
                            code="interpretation_review_pending",
                            component_id="source",
                            component_type="source",
                            detail="Source interpretation awaits review.",
                        )
                    ],
                ),
            )
        result = real_validate(*args, allow_pending_interpretation_placeholders=allow_pending_interpretation_placeholders, **kwargs)
        results.append(result)
        return result

    monkeypatch.setattr(preflight_module, "validate_pipeline", validate)
    preferences = await service._require_sessions_service().get_composer_preferences(session_id)
    mutation_ceiling = ComposerMutationCeiling(str(session_id), str(uuid4()), preferences.trust_mode)
    await _preview(service, state, admitted, service._preflight.new_cache(), registry, catalog, context, mutation_ceiling)
    assert len(results) == 1
    assert results[0].is_valid, results[0].errors
    assert reads == [context]
