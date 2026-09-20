"""Preview dispatch must use the policy admitted for its complete request."""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

from elspeth.contracts.session_operation import SessionOperationContext, SessionOperationFence, SessionOperationKind
from elspeth.plugins.infrastructure.manager import get_shared_plugin_manager
from elspeth.web.blobs.protocol import BlobRecord
from elspeth.web.composer import service as service_module
from elspeth.web.composer._compose_loop_carriers import _CallModelOutcome, _DispatchOutcome
from elspeth.web.composer.anti_anchor import AntiAnchorTracker
from elspeth.web.composer.audit import BufferingRecorder
from elspeth.web.composer.discovery_cache import RuntimePreflightCache
from elspeth.web.composer.service import ComposerServiceImpl, _admit_composer_llm_completion
from elspeth.web.composer.state import CompositionState, OutputSpec, PipelineMetadata, SourceSpec
from elspeth.web.dependencies import create_catalog_service
from elspeth.web.execution.schemas import ValidationReadiness, ValidationReadinessBlocker, ValidationResult
from elspeth.web.plugin_policy.compiler import compile_web_plugin_policy
from elspeth.web.plugin_policy.models import PluginAvailability, PluginAvailabilitySnapshot, PluginId, PluginUnavailableReason
from elspeth.web.plugin_policy.profiles import OperatorProfileRegistry, RuntimeWebPluginConfig
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


def _service(tmp_path: Path, current_snapshot: PluginAvailabilitySnapshot) -> ComposerServiceImpl:
    settings = _make_settings(tmp_path)
    config = RuntimeWebPluginConfig.from_settings(settings)
    policy = compile_web_plugin_policy(registry=get_shared_plugin_manager(), settings=config)
    return ComposerServiceImpl(
        catalog=create_catalog_service(),
        settings=settings,
        plugin_snapshot_factory=lambda _user_id: current_snapshot,
        operator_profile_registry=OperatorProfileRegistry(policy=policy, settings=config),
    )


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
    operation_context: SessionOperationContext | None = None,
) -> _DispatchOutcome:
    from elspeth.web.catalog.policy_view import PolicyCatalogView

    policy_catalog = PolicyCatalogView(service._catalog, admitted, service._operator_profile_registry)
    completion = _admit_composer_llm_completion(
        _fake_llm_response(tool_calls=({"id": "preview-call", "name": "preview_pipeline", "arguments": {}},))
    )
    outcome, _advisor_calls = await service._dispatch_tool_batch(
        call_model=_CallModelOutcome(completion=completion),
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
    service = _service(tmp_path, subsequent)
    state = _state(tmp_path)
    cache = service._new_runtime_preflight_cache()

    first = await _preview(service, state, admitted, cache)
    strict = first.last_runtime_preflight
    assert strict is not None
    assert "plugin_not_enabled" in {error.error_code for error in strict.errors}
    key = service._runtime_preflight_key(state, session_scope="preview-snapshot-test", plugin_snapshot=admitted)
    assert cache == {key: strict}

    repeated = await _preview(service, state, admitted, cache)
    assert repeated.last_runtime_preflight is strict
    later = await _preview(service, state, subsequent, cache)
    later_strict = later.last_runtime_preflight
    assert later_strict is not None
    assert "plugin_not_enabled" not in {error.error_code for error in later_strict.errors}
    assert any(check.name == "plugin_enablement" and check.passed for check in later_strict.checks)
    later_key = service._runtime_preflight_key(state, session_scope="preview-snapshot-test", plugin_snapshot=subsequent)
    assert cache == {key: strict, later_key: later_strict}


@pytest.mark.anyio
async def test_strict_and_tolerant_preview_share_the_admitted_snapshot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Exercise both real cached-preflight branches after a pending-review result."""
    admitted = _snapshot(csv_available=True)
    subsequent = _snapshot(csv_available=False)
    service = _service(tmp_path, subsequent)
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
                        code="interpretation_review_pending",
                        component_id="source",
                        component_type="source",
                        detail="Source interpretation awaits review.",
                    )
                ],
            ),
        )

    monkeypatch.setattr(service_module, "validate_pipeline", pending_review_preflight)
    await _preview(service, _state(tmp_path), admitted, service._new_runtime_preflight_cache())
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
    service = _service(tmp_path, admitted)
    service._sessions_service = composer_service_with_real_sessions._sessions_service
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

    monkeypatch.setattr(service, "_blob_service", BlobReader())
    real_validate = service_module.validate_pipeline
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

    monkeypatch.setattr(service_module, "validate_pipeline", validate)
    await _preview(service, state, admitted, service._new_runtime_preflight_cache(), context)
    assert len(results) == 1
    assert results[0].is_valid, results[0].errors
    assert reads == [context]
