"""Tests for ExecutionServiceImpl — background execution with thread safety.

Each test class targets a specific review fix:
- TestExecutionFlow: Basic lifecycle (pending -> running -> completed)
- TestB2ShutdownEvent: shutdown_event always passed to Orchestrator.run()
- TestB3Construction: LandscapeDB/PayloadStore from WebSettings
- TestB7ExceptionHandling: BaseException catch + done_callback safety net
- TestB8AsyncBridging: _call_async() bridges sync thread to async event loop
- TestCancelMechanism: Event-based cancellation
- TestOneActiveRun: B6 constraint enforcement
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import hashlib
import json
import threading
from collections.abc import Callable, Coroutine, Iterator
from concurrent.futures import Future
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, create_autospec, patch
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError

from elspeth.contracts import CallType, NodeStateStatus, NodeType
from elspeth.contracts.audit import TokenRef
from elspeth.contracts.enums import CreationModality, RunStatus
from elspeth.contracts.errors import AuditIntegrityError, ExecutionError
from elspeth.contracts.hashing import stable_hash
from elspeth.contracts.plugin_policy_audit import WebPluginPolicyEvidence
from elspeth.contracts.run_result import RunResult
from elspeth.contracts.schema import SchemaConfig
from elspeth.contracts.sink_effects import (
    SINK_EFFECT_PROTOCOL_VERSION,
    ResolvedSinkEffectMode,
    SinkEffectContract,
    SinkEffectExecutionPurpose,
    SinkEffectInputKind,
)
from elspeth.core.config import (
    CheckpointSettings,
    ConcurrencySettings,
    RateLimitSettings,
    TelemetrySettings,
)
from elspeth.core.dag.graph import ExecutionGraph
from elspeth.core.landscape import LandscapeDB
from elspeth.core.landscape.factory import RecorderFactory
from elspeth.core.landscape.schema import run_attributions_table, runs_table, tokens_table
from elspeth.telemetry.manager import TelemetryManager
from elspeth.web.blobs.protocol import (
    BlobFinalizationResult,
    BlobIntegrityError,
    BlobNotFoundError,
    BlobRecord,
    BlobServiceProtocol,
    BlobStateError,
)
from elspeth.web.dependencies import create_catalog_service
from elspeth.web.deployment_contract import resolve_deployment_state_mode
from elspeth.web.execution.errors import (
    BlobRowsSourceAdmissionError,
    CompletionGateIntegrityError,
    ExecutionReadinessError,
    PipelineValidationError,
)
from elspeth.web.execution.progress import ProgressBroadcaster
from elspeth.web.execution.protocol import FrozenRunSettings
from elspeth.web.execution.schemas import (
    CHECK_OUTCOME_SECRET_REFS_NO_REFS,
    RunAccounting,
    RunAccountingIntegrity,
    RunAccountingRouting,
    RunAccountingSource,
    RunAccountingTokens,
    ValidationCheck,
    ValidationError,
    ValidationReadiness,
    ValidationReadinessBlocker,
    ValidationResult,
)
from elspeth.web.execution.service import _MAX_FRAME_PATH_PARTS, ExecutionServiceImpl
from elspeth.web.execution.validation import validate_pipeline as _real_validate_pipeline
from elspeth.web.interpretation_state import INTERPRETATION_REQUIREMENTS_KEY, PROMPT_TEMPLATE_PARTS_KEY
from elspeth.web.plugin_policy.models import (
    PluginAvailability,
    PluginAvailabilitySnapshot,
    PluginId,
    PluginUnavailableReason,
)
from elspeth.web.sessions.converters import state_from_record
from elspeth.web.sessions.protocol import (
    LEGAL_RUN_TRANSITIONS,
    CompositionStateRecord,
    IllegalRunTransitionError,
    RunAlreadyActiveError,
    SessionRunStatus,
    SessionServiceProtocol,
)
from elspeth.web.sessions.telemetry import build_sessions_telemetry, observed_value

# ── Fixtures ───────────────────────────────────────────────────────────

_TEST_PIPELINE_YAML = "source:\n  plugin: csv\n  options: {}\n"
_RESOLVED_TEST_PIPELINE_YAML = "source:\n  options: {}\n  plugin: csv\n"


class _WebSettingsStub:
    """Settings collaborator for execution-service tests."""

    def __init__(self) -> None:
        self.deployment_target = "default"
        self.deployment_state_mode = "sqlite-single"
        self.landscape_url = "sqlite:///test_audit.db"
        self.payload_store_path = Path("/tmp/test_payloads")
        self.landscape_passphrase = None
        self.data_dir: str | Path = "/tmp/data"

    def get_session_db_url(self) -> str:
        return f"sqlite:///{Path(self.data_dir) / 'sessions.db'}"

    def get_landscape_url(self) -> str:
        return self.landscape_url

    def get_payload_store_path(self) -> Path:
        return self.payload_store_path


class _YamlGeneratorStub:
    def __init__(self, result: Any = _TEST_PIPELINE_YAML) -> None:
        self.result = result

    def generate_yaml(self, _state: Any) -> Any:
        return self.result


def _run_record_stub(**overrides: Any) -> SimpleNamespace:
    values = {
        "id": uuid4(),
        "session_id": uuid4(),
        "state_id": uuid4(),
        "status": "pending",
        "started_at": datetime.now(tz=UTC),
        "finished_at": None,
        "rows_processed": 0,
        "rows_succeeded": 0,
        "rows_failed": 0,
        "rows_routed_success": 0,
        "rows_routed_failure": 0,
        "rows_quarantined": 0,
        "error": None,
        "landscape_run_id": None,
        "pipeline_yaml": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _orchestrator_result_stub(
    *,
    run_id: str = "r1",
    status: RunStatus = RunStatus.COMPLETED,
    rows_processed: int = 10,
    rows_succeeded: int = 10,
    rows_failed: int = 0,
    rows_routed_success: int = 0,
    rows_routed_failure: int = 0,
    rows_quarantined: int = 0,
) -> RunResult:
    return RunResult(
        run_id=run_id,
        status=status,
        rows_processed=rows_processed,
        rows_succeeded=rows_succeeded,
        rows_failed=rows_failed,
        rows_routed_success=rows_routed_success,
        rows_routed_failure=rows_routed_failure,
        rows_quarantined=rows_quarantined,
    )


def _blob_record_stub(
    *,
    blob_id: UUID | None = None,
    session_id: UUID | None = None,
    filename: str = "blob.txt",
    mime_type: str = "text/plain",
    size_bytes: int = 0,
    content_hash: str | None = None,
    storage_path: str = "/tmp/data/blobs/blob.txt",
    status: str = "ready",
    creation_modality: CreationModality = CreationModality.VERBATIM,
) -> BlobRecord:
    return BlobRecord(
        id=blob_id or uuid4(),
        session_id=session_id or uuid4(),
        filename=filename,
        mime_type=cast(Any, mime_type),
        size_bytes=size_bytes,
        content_hash=content_hash,
        storage_path=storage_path,
        created_at=datetime.now(UTC),
        created_by="user",
        source_description=None,
        status=cast(Any, status),
        creation_modality=creation_modality,
        created_from_message_id=None,
        creating_model_identifier=None,
        creating_model_version=None,
        creating_provider=None,
        creating_composer_skill_hash=None,
        creating_arguments_hash=None,
    )


class _EffectCapableSinkStub(SinkEffectContract):
    effect_call_type = CallType.FILESYSTEM
    name = "effect-capable"
    _on_write_failure = "discard"
    effect_protocol_version = SINK_EFFECT_PROTOCOL_VERSION
    supported_effect_modes = frozenset({"write"})
    supported_effect_input_kinds = frozenset({SinkEffectInputKind.PIPELINE_MEMBERS})
    effect_mode_remediation: str | None = None

    @classmethod
    def _resolve_sink_effect_mode(
        cls,
        config: dict[str, object],
        *,
        purpose: SinkEffectExecutionPurpose,
    ) -> ResolvedSinkEffectMode:
        del cls, config, purpose
        return ResolvedSinkEffectMode("write")

    def _validate_sink_effect_capability_configuration(
        self,
        *,
        mode: str,
        required_input_kind: SinkEffectInputKind,
    ) -> None:
        del mode, required_input_kind

    def inspect_effect(self, _request: object, _ctx: object) -> None: ...

    def prepare_effect(self, _request: object, _ctx: object) -> None: ...

    def commit_effect(self, _plan: object, _ctx: object) -> None: ...

    def reconcile_effect(self, _plan: object, _ctx: object) -> None: ...


class _LegacySinkStub:
    """Deliberately lacks the sink-effect protocol for raw-gate refusal tests."""

    name = "legacy"


def _plugin_bundle_stub() -> SimpleNamespace:
    from elspeth.engine.orchestrator.preflight import (
        ResolvedSinkEffectMode,
        SinkEffectExecutionPurpose,
        SinkEffectRuntimeBinding,
    )

    source = object()
    sink = _EffectCapableSinkStub()
    return SimpleNamespace(
        source=source,
        sources={"source": source},
        source_settings=object(),
        source_settings_map={"source": object()},
        transforms=(),
        sinks={"primary": sink},
        aggregations={},
        collectors={},
        sink_effect_bindings={
            "primary": SinkEffectRuntimeBinding(
                sink_name="primary",
                sink=sink,
                sink_type=type(sink),
                config_fingerprint=stable_hash({}),
                purpose=SinkEffectExecutionPurpose.FRESH,
                effect_mode=ResolvedSinkEffectMode("write"),
            )
        },
    )


def _blob_service_stub() -> Any:
    blob_service = create_autospec(BlobServiceProtocol, instance=True, spec_set=True)
    content = b"amount\n"
    blob_service.read_blob_content_prefix_verified.return_value = (
        content,
        hashlib.sha256(content).hexdigest(),
        len(content),
    )
    return blob_service


def _ready_csv_blob_for_execution(*, blob_ref: str, session_id: UUID, storage_path: str) -> BlobRecord:
    content = b"amount\n"
    return _blob_record_stub(
        blob_id=UUID(blob_ref),
        session_id=session_id,
        filename=Path(storage_path).name,
        mime_type="text/csv",
        size_bytes=len(content),
        content_hash=hashlib.sha256(content).hexdigest(),
        storage_path=storage_path,
        status="ready",
    )


def _execution_graph_stub() -> Any:
    return create_autospec(ExecutionGraph, instance=True)


def _orchestrator_stub(result: RunResult | None = None) -> MagicMock:
    mock_orch = MagicMock(spec=["run"])
    mock_orch.run.return_value = result or _orchestrator_result_stub()
    return mock_orch


def _configure_runtime_success(
    *,
    mock_load: MagicMock,
    mock_instantiate: MagicMock,
    mock_graph_cls: MagicMock,
    mock_orch_cls: MagicMock | None = None,
    result: RunResult | None = None,
) -> MagicMock | None:
    mock_load.return_value = _mock_pipeline_settings()
    mock_instantiate.return_value = _plugin_bundle_stub()
    mock_graph = _execution_graph_stub()
    mock_graph_cls.from_plugin_instances.return_value = mock_graph
    if mock_orch_cls is None:
        return None
    mock_orch = _orchestrator_stub(result)
    mock_orch_cls.return_value = mock_orch
    return mock_orch


@contextlib.contextmanager
def _admitted_runtime_setup() -> Iterator[None]:
    """Give status/resource-ordering tests a fully admitted runtime boundary."""
    with (
        patch("elspeth.web.execution.service.load_settings_from_yaml_string", return_value=_mock_pipeline_settings()),
        patch("elspeth.web.execution.preflight.instantiate_plugins_from_config", return_value=_plugin_bundle_stub()),
        patch("elspeth.web.execution.preflight.ExecutionGraph") as graph_cls,
    ):
        graph_cls.from_plugin_instances.return_value = _execution_graph_stub()
        yield


@pytest.fixture
def mock_pipeline_config_assembly() -> Iterator[MagicMock]:
    """Opt-in patch for ``assemble_and_validate_pipeline_config``.

    Service tests that exercise ``_run_pipeline`` and mock the plugin bundle
    and graph would otherwise have the helper's route-target validators fire
    against MagicMock attributes and raise ``RouteValidationError``
    spuriously. Tests using real settings + real plugin instantiation must
    NOT use this fixture — the real helper is required for those flows.
    Issue elspeth-127de6865a introduced the helper.
    """
    with patch(
        "elspeth.web.execution.service.assemble_and_validate_pipeline_config",
        return_value=SimpleNamespace(),
    ) as mock_assemble:
        yield mock_assemble


@pytest.fixture
def mock_loop() -> MagicMock:
    return MagicMock(spec=asyncio.AbstractEventLoop)


@pytest.fixture
def broadcaster(mock_loop: MagicMock) -> ProgressBroadcaster:
    return ProgressBroadcaster(mock_loop)


@pytest.fixture
def mock_settings() -> _WebSettingsStub:
    return _WebSettingsStub()


def _mock_pipeline_settings() -> SimpleNamespace:
    """Return settings-shaped test data for patched pipeline loading.

    _run_pipeline() now builds the same runtime infrastructure as the CLI path,
    so tests that patch YAML loading must still provide real config-contract
    objects for the runtime conversion boundary.
    """
    return SimpleNamespace(
        sources={},
        transforms=[],
        aggregations=[],
        sinks={"primary": SimpleNamespace(plugin="json", options={})},
        gates=[],
        coalesce=[],
        row_unions=[],
        scopes=[],
        max_bound_region_depth=5,
        queues={},
        landscape=SimpleNamespace(export=SimpleNamespace(enabled=False, sink=None)),
        rate_limit=RateLimitSettings(enabled=False),
        concurrency=ConcurrencySettings(),
        checkpoint=CheckpointSettings(enabled=False),
        telemetry=TelemetrySettings(enabled=False),
    )


def _run_accounting_for_status(status: RunStatus) -> RunAccounting:
    if status == RunStatus.EMPTY:
        return RunAccounting(
            source=RunAccountingSource(rows_processed=0, rows_rejected=0, rows_read=0),
            tokens=RunAccountingTokens(emitted=0, terminal=0, succeeded=0, failed=0, structural=0, pending=0, abandoned=0),
            routing=RunAccountingRouting(routed_success=0, routed_failure=0, quarantined=0, discarded=0),
            integrity=RunAccountingIntegrity(closure="closed", missing_terminal_outcomes=0, duplicate_terminal_outcomes=0),
        )
    if status == RunStatus.COMPLETED_WITH_FAILURES:
        return RunAccounting(
            source=RunAccountingSource(rows_processed=10, rows_rejected=0, rows_read=10),
            tokens=RunAccountingTokens(emitted=10, terminal=10, succeeded=8, failed=2, structural=0, pending=0, abandoned=0),
            routing=RunAccountingRouting(routed_success=0, routed_failure=0, quarantined=0, discarded=0),
            integrity=RunAccountingIntegrity(closure="closed", missing_terminal_outcomes=0, duplicate_terminal_outcomes=0),
        )
    return RunAccounting(
        source=RunAccountingSource(rows_processed=10, rows_rejected=0, rows_read=10),
        tokens=RunAccountingTokens(emitted=10, terminal=10, succeeded=10, failed=0, structural=0, pending=0, abandoned=0),
        routing=RunAccountingRouting(routed_success=0, routed_failure=0, quarantined=0, discarded=0),
        integrity=RunAccountingIntegrity(closure="closed", missing_terminal_outcomes=0, duplicate_terminal_outcomes=0),
    )


def _with_resolved_model_choice(node: dict[str, Any]) -> dict[str, Any]:
    """Pre-stage a resolved ``llm_model_choice`` interpretation requirement.

    Tests that construct an LLM node by raw dict bypass the composer's
    mutation-time auto-stager (which would create a pending
    requirement). Without this resolution, the validator's interpretation
    gate short-circuits before any downstream check runs. Tests
    exercising downstream behavior (fanout guard, blob-inline,
    placeholder gate, etc.) get the gate resolved here so the test stays
    focused on its actual subject.
    """
    if node.get("plugin") != "llm":
        return node
    options = node.get("options")
    if not isinstance(options, dict):
        return node
    model = options.get("model")
    if not isinstance(model, str) or not model:
        return node
    requirements = list(options.get(INTERPRETATION_REQUIREMENTS_KEY) or ())
    requirements.append(
        {
            "id": f"model_choice_review:{node['id']}",
            "kind": "llm_model_choice",
            "user_term": f"llm_model_choice:{node['id']}",
            "status": "resolved",
            "draft": model,
            "event_id": f"model-choice-accepted:{node['id']}",
            "accepted_value": model,
            "accepted_artifact_hash": None,
            "resolved_prompt_template_hash": stable_hash(model),
        }
    )
    return {
        **node,
        "options": {**options, INTERPRETATION_REQUIREMENTS_KEY: requirements},
    }


def _composition_state_record(
    *,
    session_id: UUID,
    source_path: Path,
    output_path: Path,
    nodes: list[dict[str, Any]],
    sources: dict[str, dict[str, Any]] | None = None,
) -> CompositionStateRecord:
    if source_path.parent.name == "blobs":
        scoped_source_path = source_path.parent / str(session_id) / source_path.name
        scoped_source_path.parent.mkdir(parents=True, exist_ok=True)
        if source_path.exists():
            scoped_source_path.write_bytes(source_path.read_bytes())
        source_path = scoped_source_path
    if output_path.parent.name == "outputs":
        output_path = output_path.parent / str(session_id) / output_path.name
    source = {
        "plugin": "text",
        "on_success": "source_rows",
        "on_validation_failure": "discard",
        "options": {
            "path": str(source_path),
            "column": "body",
            "schema": {"mode": "observed"},
        },
    }
    return CompositionStateRecord(
        id=uuid4(),
        session_id=session_id,
        version=1,
        source=source if sources is None else next(iter(sources.values())),
        sources=sources,
        nodes=[_with_resolved_model_choice(node) for node in nodes],
        edges=[],
        outputs=[
            {
                "name": "out",
                "plugin": "json",
                "options": {
                    "path": str(output_path),
                    "format": "jsonl",
                    "mode": "write",
                    "schema": {"mode": "observed"},
                },
                "on_write_failure": "discard",
            }
        ],
        metadata_={"name": "Test", "description": ""},
        is_valid=True,
        validation_errors=None,
        created_at=datetime.now(UTC),
        derived_from_state_id=None,
        composer_meta=None,
    )


def _successful_core_validation_result() -> ValidationResult:
    """The real validator's successful 24-check prefix, without advisories."""
    from elspeth.web.execution._validation_ledger import CORE_VALIDATION_CHECK_NAMES

    return ValidationResult(
        is_valid=True,
        checks=[
            ValidationCheck(
                name=name,
                passed=True,
                detail=f"{name} passed",
                affected_nodes=(),
                outcome_code=CHECK_OUTCOME_SECRET_REFS_NO_REFS if name == "secret_refs" else None,
            )
            for name in CORE_VALIDATION_CHECK_NAMES
        ],
        errors=[],
        readiness=ValidationReadiness(
            authoring_valid=True,
            execution_ready=True,
            completion_ready=True,
            blockers=[],
        ),
    )


def _proof_gate_state(
    *,
    source_path: Path | None,
    blob_id: UUID | None,
    schema_mode: str = "observed",
    condition: str = "row['amount'] > 500",
) -> Any:
    """Direct CSV -> gate state exercising the observed-row proof."""
    from elspeth.web.composer.state import CompositionState, NodeSpec, OutputSpec, PipelineMetadata, SourceSpec

    source_options: dict[str, Any] = {"schema": {"mode": schema_mode}}
    if schema_mode in {"fixed", "flexible"}:
        source_options["schema"]["fields"] = ["amount: float"]
    if source_path is not None:
        source_options["path"] = str(source_path)
    if blob_id is not None:
        source_options["blob_ref"] = str(blob_id)
    return CompositionState(
        sources={
            "source": SourceSpec(
                plugin="csv",
                on_success="rows",
                options=source_options,
                on_validation_failure="discard",
            )
        },
        nodes=(
            NodeSpec(
                id="amount_gate",
                node_type="gate",
                plugin=None,
                input="rows",
                on_success=None,
                on_error=None,
                options={},
                condition=condition,
                routes={"true": "high_value", "false": "standard"},
                fork_to=None,
                branches=None,
                policy=None,
                merge=None,
            ),
        ),
        edges=(),
        outputs=(
            OutputSpec(name="high_value", plugin="json", options={}, on_write_failure="discard"),
            OutputSpec(name="standard", plugin="json", options={}, on_write_failure="discard"),
        ),
        metadata=PipelineMetadata(name="Proof gate"),
        version=1,
    )


def _guided_sentinel_proof_gate_state(*, source_path: Path, blob_id: UUID) -> Any:
    """Observed CSV numeric gate whose reviewed source claims blob custody."""
    from dataclasses import replace

    from elspeth.web.composer.guided.resolved import SourceResolved
    from elspeth.web.composer.guided.state_machine import GuidedSession

    live_state = _proof_gate_state(source_path=source_path, blob_id=None)
    stable_id = str(uuid4())
    guided = replace(
        GuidedSession.initial(),
        source_order=(stable_id,),
        reviewed_sources={
            stable_id: SourceResolved(
                name="source",
                plugin="csv",
                options={
                    "path": f"blob:{blob_id}",
                    "schema": {"mode": "observed"},
                },
                observed_columns=("amount",),
                sample_rows=(),
                on_validation_failure="discard",
            )
        },
    )
    return replace(live_state, guided_session=guided)


def _install_ready_proof_blob(
    service: ExecutionServiceImpl,
    *,
    session_id: UUID,
    blob_id: UUID,
    source_path: Path,
) -> MagicMock:
    """Install the session-owned ready blob record the proof resolver must use."""
    content = source_path.read_bytes()
    content_digest = hashlib.sha256(content).hexdigest()
    blob_service = create_autospec(BlobServiceProtocol, instance=True)
    blob_service.get_blob.return_value = _blob_record_stub(
        blob_id=blob_id,
        session_id=session_id,
        filename=source_path.name,
        mime_type="text/csv",
        size_bytes=source_path.stat().st_size,
        content_hash=content_digest,
        storage_path=str(source_path),
        status="ready",
    )
    blob_service.read_blob_content_prefix_verified.return_value = (
        content[: 8 * 1024],
        content_digest,
        len(content),
    )
    service._blob_service = blob_service
    return blob_service


def _install_ready_proof_blobs(
    service: ExecutionServiceImpl,
    *,
    session_id: UUID,
    bindings: dict[UUID, Path],
) -> MagicMock:
    """Install multiple session-owned ready blobs for multi-source proof."""
    records: dict[UUID, BlobRecord] = {}
    verified: dict[UUID, tuple[bytes, str, int]] = {}
    for blob_id, source_path in bindings.items():
        content = source_path.read_bytes()
        content_digest = hashlib.sha256(content).hexdigest()
        records[blob_id] = _blob_record_stub(
            blob_id=blob_id,
            session_id=session_id,
            filename=source_path.name,
            mime_type="text/csv",
            size_bytes=len(content),
            content_hash=content_digest,
            storage_path=str(source_path),
            status="ready",
        )
        verified[blob_id] = (content[: 8 * 1024], content_digest, len(content))

    blob_service = create_autospec(BlobServiceProtocol, instance=True)
    blob_service.get_blob.side_effect = records.__getitem__
    blob_service.read_blob_content_prefix_verified.side_effect = lambda blob_id, *, prefix_bytes: verified[blob_id]
    service._blob_service = blob_service
    return blob_service


@pytest.fixture
def mock_session_service() -> MagicMock:
    svc = create_autospec(SessionServiceProtocol, instance=True)
    # state_record needs fields that state_from_record() accesses
    state = SimpleNamespace(
        id=uuid4(),
        session_id=uuid4(),
        version=1,
        source=None,  # No source -> path allowlist check skips
        sources=None,
        nodes=None,
        edges=None,
        outputs=None,
        metadata_={"name": "Test", "description": ""},
        is_valid=True,
        validation_errors=None,
        created_at=datetime.now(UTC),
        derived_from_state_id=None,
        composer_meta=None,
    )
    svc.get_state.return_value = state
    svc.get_current_state.return_value = state
    svc.get_active_run.return_value = None
    svc.create_run.return_value = _run_record_stub(id=uuid4())
    svc.get_run.return_value = _run_record_stub(status="pending")
    svc.update_run_status.return_value = None
    next_event_sequence = 0

    async def append_run_event(**_kwargs: Any) -> SimpleNamespace:
        nonlocal next_event_sequence
        next_event_sequence += 1
        return SimpleNamespace(sequence=next_event_sequence)

    svc.append_run_event.side_effect = append_run_event
    svc.list_run_events.return_value = []
    svc.record_blob_inline_resolutions.return_value = None
    return svc


@pytest.fixture(autouse=True)
def isolate_raw_sink_effect_eligibility_gate(request: pytest.FixtureRequest) -> Iterator[None]:
    """Keep unrelated service mechanics tests behind an admitted raw gate.

    The two raw-gate regressions install their own validator behavior.  The
    rejection-ordering test intentionally exercises the real legacy adapter
    rejection and therefore opts out of this isolation fixture.
    """
    if request.node.name == "test_sink_effect_rejection_precedes_status_landscape_and_payload_resources":
        yield
        return
    with patch(
        "elspeth.web.execution.service.validate_sink_effect_eligibility_from_raw_config",
        return_value={},
    ):
        yield


@pytest.fixture
def service(
    mock_loop: MagicMock,
    broadcaster: ProgressBroadcaster,
    mock_settings: MagicMock,
    mock_session_service: MagicMock,
) -> Iterator[ExecutionServiceImpl]:
    # AC #17: All Run CRUD goes through SessionService — no direct DB access.
    yaml_generator = _YamlGeneratorStub()
    svc = ExecutionServiceImpl.for_trained_operator(
        loop=mock_loop,
        broadcaster=broadcaster,
        settings=mock_settings,
        session_service=mock_session_service,
        yaml_generator=yaml_generator,
        telemetry=build_sessions_telemetry(),
    )
    # Patch _call_async for tests that call _run_pipeline directly (sync).
    # The real _call_async uses asyncio.run_coroutine_threadsafe which needs
    # a running event loop. In unit tests, we bridge by running the coroutine
    # synchronously via asyncio.get_event_loop().run_until_complete().
    # TestB8AsyncBridging tests _call_async itself with its own mocking.
    _real_loop = asyncio.new_event_loop()

    def _mock_call_async(coro: Coroutine[Any, Any, Any]) -> Any:
        try:
            return _real_loop.run_until_complete(coro)
        except RuntimeError:
            # If no event loop is available, just close the coroutine
            coro.close()
            return None

    cast(Any, svc)._call_async = _mock_call_async
    # The fail-closed pre-run validation gate (execute() -> validate_pipeline, added
    # 2026-06-08) runs on every execute. These tests exercise execute MECHANICS (run
    # creation, blob/path gates, cancel) with a minimal mock state that is NOT a
    # runnable pipeline, so stub validate_pipeline to VALID to reach the mechanics.
    # Gate-behavior tests (TestExecutionFlow::*_pipeline_*) override this with their
    # own patch; validate_pipeline's own correctness is covered in test_validation.py.
    _gate_valid = ValidationResult(
        is_valid=True,
        checks=[],
        errors=[],
        readiness=ValidationReadiness(
            authoring_valid=True,
            execution_ready=True,
            completion_ready=True,
            blockers=[],
        ),
    )
    with patch("elspeth.web.execution.validation.validate_pipeline", return_value=_gate_valid):
        yield svc
    _real_loop.close()


# ── Basic Lifecycle ────────────────────────────────────────────────────


class TestExecutionFlow:
    def test_queued_run_rejects_rotated_plugin_binding_before_runtime_config_use(self, service: ExecutionServiceImpl) -> None:
        base = PluginAvailabilitySnapshot.for_trained_operator(create_catalog_service())

        def with_generation(value: str) -> PluginAvailabilitySnapshot:
            return PluginAvailabilitySnapshot.create(
                policy_hash=base.policy_hash,
                principal_scope="local:alice",
                available=base.available,
                unavailable=base.unavailable,
                selected=base.selected,
                usable_profile_aliases=base.usable_profile_aliases,
                selected_profile_aliases=base.selected_profile_aliases,
                binding_generation_fingerprint=value,
                control_modes=base.control_modes,
            )

        frozen = FrozenRunSettings(
            plugin_snapshot=with_generation("generation-before-queue"),
            executable_config={},
            audit_safe_config={},
        )
        service._plugin_snapshot_factory = lambda _user_id: with_generation("generation-after-queue")

        with pytest.raises(RuntimeError, match="binding changed while execution was queued"):
            service._require_current_binding_generation(frozen, user_id="alice")

    @pytest.mark.asyncio
    async def test_execute_returns_run_id_immediately(self, service: ExecutionServiceImpl) -> None:
        """execute() returns a UUID without blocking on pipeline completion."""
        with patch.object(service, "_run_pipeline"):
            run_id = await service.execute(session_id=uuid4())
        assert isinstance(run_id, UUID)

    @pytest.mark.asyncio
    async def test_execute_rejects_non_string_yaml_generator_output(self, service: ExecutionServiceImpl) -> None:
        """YamlGenerator contract violations must fail fast, not spin in PyYAML."""
        cast(_YamlGeneratorStub, service._yaml_generator).result = object()

        with pytest.raises(TypeError, match="must return str"):
            await service.execute(session_id=uuid4())

    @pytest.mark.asyncio
    async def test_execute_creates_run_via_session_service(self, service: ExecutionServiceImpl, mock_session_service: MagicMock) -> None:
        """AC #17: Run creation delegates to session_service.create_run()
        with R6 expanded params (session_id, state_id, pipeline_yaml)."""
        session_id = uuid4()
        state_record = mock_session_service.get_current_state.return_value
        with patch.object(service, "_run_pipeline"):
            await service.execute(session_id=session_id)
        mock_session_service.create_run.assert_awaited_once()
        create_call = mock_session_service.create_run.await_args
        assert create_call.args == ()
        assert create_call.kwargs == {
            "session_id": session_id,
            "state_id": state_record.id,
            "pipeline_yaml": _RESOLVED_TEST_PIPELINE_YAML,
        }

    @pytest.mark.asyncio
    async def test_execute_freezes_snapshot_and_distinct_configs_before_submission(
        self,
        service: ExecutionServiceImpl,
        mock_session_service: MagicMock,
        mock_settings: _WebSettingsStub,
        tmp_path: Path,
    ) -> None:
        session_id = uuid4()
        (tmp_path / "blobs").mkdir()
        (tmp_path / "outputs").mkdir()
        source_path = tmp_path / "blobs" / "input.txt"
        output_path = tmp_path / "outputs" / "output.jsonl"
        source_path.write_text("Ada\n", encoding="utf-8")
        mock_settings.data_dir = tmp_path
        state_record = _composition_state_record(
            session_id=session_id,
            source_path=source_path,
            output_path=output_path,
            nodes=[],
        )
        mock_session_service.get_current_state.return_value = state_record
        snapshot = PluginAvailabilitySnapshot.for_trained_operator(create_catalog_service())
        service._plugin_snapshot_factory = lambda _user_id: snapshot
        completed: Future[None] = Future()
        completed.set_result(None)

        with patch.object(service._executor, "submit", return_value=completed) as submit:
            await service.execute(session_id=session_id, user_id="alice")

        submitted_args = submit.call_args.args
        assert submitted_args[4].plugin_snapshot is snapshot
        assert submitted_args[4].executable_config is not submitted_args[4].audit_safe_config

    @pytest.mark.asyncio
    async def test_execute_lowers_profile_into_fanout_guard_and_frozen_executable_config(
        self,
        service: ExecutionServiceImpl,
        mock_session_service: MagicMock,
        mock_settings: _WebSettingsStub,
        tmp_path: Path,
    ) -> None:
        from elspeth.plugins.infrastructure.manager import get_shared_plugin_manager
        from elspeth.web.composer import yaml_generator
        from elspeth.web.config import WebSettings
        from elspeth.web.execution.fanout_guard import ExecutionFanoutGuardRequired, evaluate_execution_fanout_guard
        from elspeth.web.interpretation_state import InterpretationReviewPending, materialize_state_for_execution
        from elspeth.web.plugin_policy.compiler import compile_web_plugin_policy
        from elspeth.web.plugin_policy.profiles import OperatorProfileRegistry, RuntimeWebPluginConfig
        from elspeth.web.plugin_policy.validation import validate_plugin_policy

        private_model = "bedrock/anthropic.claude-3-haiku-20240307-v1:0"
        private_region = "ap-southeast-2"
        web_settings = WebSettings(
            data_dir=tmp_path,
            composer_max_composition_turns=4,
            composer_max_discovery_turns=4,
            composer_timeout_seconds=60,
            composer_rate_limit_per_minute=20,
            shareable_link_signing_key=b"0123456789abcdef0123456789abcdef",
            llm_profiles={
                "tutorial": {
                    "provider": "bedrock",
                    "model": private_model,
                    "region_name": private_region,
                }
            },
            default_llm_profile="tutorial",
        )
        runtime_policy = RuntimeWebPluginConfig.from_settings(web_settings)
        policy = compile_web_plugin_policy(registry=get_shared_plugin_manager(), settings=runtime_policy)
        profiles = OperatorProfileRegistry(policy=policy, settings=runtime_policy)
        unrestricted = PluginAvailabilitySnapshot.for_trained_operator(create_catalog_service())
        snapshot = PluginAvailabilitySnapshot.create(
            policy_hash=policy.policy_hash,
            principal_scope="test:alice",
            available=unrestricted.available,
            unavailable=(),
            selected=unrestricted.selected,
            usable_profile_aliases=((PluginId("transform", "llm"), ("tutorial",)),),
            selected_profile_aliases=((PluginId("transform", "llm"), "tutorial"),),
            binding_generation_fingerprint="runtime-policy-generation",
        )
        session_id = uuid4()
        (tmp_path / "blobs").mkdir()
        (tmp_path / "outputs").mkdir()
        source_path = tmp_path / "blobs" / "input.txt"
        source_path.write_text("Ada\n" * 34, encoding="utf-8")
        mock_settings.data_dir = tmp_path
        state_record = _composition_state_record(
            session_id=session_id,
            source_path=source_path,
            output_path=tmp_path / "outputs" / "output.jsonl",
            nodes=[
                {
                    "id": "summarise",
                    "node_type": "transform",
                    "plugin": "llm",
                    "input": "source_rows",
                    "on_success": "out",
                    "on_error": "discard",
                    "options": {
                        "profile": "tutorial",
                        "prompt_template": "Summarise {{ row }}",
                        "schema": {"mode": "observed", "fields": None},
                        "required_input_fields": [],
                        "queries": [
                            {"name": "summary", "input_fields": {"text": "body"}},
                            {"name": "sentiment", "input_fields": {"text": "body"}},
                            {"name": "topics", "input_fields": {"text": "body"}},
                        ],
                        INTERPRETATION_REQUIREMENTS_KEY: [
                            {
                                "id": "prompt_template_review:summarise",
                                "kind": "llm_prompt_template",
                                "user_term": "llm_prompt_template:summarise",
                                "status": "resolved",
                                "draft": "Summarise {{ row }}",
                                "event_id": "prompt-template-accepted:summarise",
                                "accepted_value": "Summarise {{ row }}",
                                "accepted_artifact_hash": None,
                                "resolved_prompt_template_hash": stable_hash("Summarise {{ row }}"),
                            }
                        ],
                    },
                }
            ],
        )
        mock_session_service.get_current_state.return_value = state_record
        service._yaml_generator = yaml_generator
        service._plugin_snapshot_factory = lambda _user_id: snapshot
        service._operator_profile_registry = profiles
        completed: Future[None] = Future()
        completed.set_result(None)

        with patch.object(service._executor, "submit", return_value=completed) as submit:
            with pytest.raises(ExecutionFanoutGuardRequired) as raised:
                await service.execute(session_id=session_id, user_id="alice")

            risk = raised.value.guard.risks[0]
            assert risk.provider == "bedrock"
            assert risk.model == private_model
            assert risk.provider_calls_per_row == 3
            assert risk.estimated_provider_calls == 102
            materialized_state = materialize_state_for_execution(state_from_record(state_record))
            assert not isinstance(materialized_state, InterpretationReviewPending)
            executable_state = validate_plugin_policy(
                materialized_state,
                snapshot=snapshot,
                profile_registry=profiles,
                catalog=create_catalog_service(),
            ).executable_state
            expected_guard = evaluate_execution_fanout_guard(executable_state, data_dir=tmp_path)
            assert expected_guard is not None
            assert raised.value.guard.ack_token == expected_guard.ack_token
            assert private_region in json.dumps(executable_state.to_dict(), default=dict)
            assert private_region not in json.dumps(raised.value.guard.to_dict())

            await service.execute(
                session_id=session_id,
                user_id="alice",
                fanout_ack_token=raised.value.guard.ack_token,
            )

        frozen = submit.call_args.args[4]
        executable = json.dumps(frozen.executable_config, default=dict)
        audit_safe = json.dumps(frozen.audit_safe_config, default=dict)
        assert private_model in executable
        assert private_region in executable
        assert "tutorial" in audit_safe
        assert private_model not in audit_safe
        assert private_region not in audit_safe

    @pytest.mark.asyncio
    async def test_execute_rejects_invalid_pipeline_before_run_creation(
        self, service: ExecutionServiceImpl, mock_session_service: MagicMock
    ) -> None:
        """Fix 1 (notes/composer-advisor-surface-map-2026-06-08.md): execute() must
        fail CLOSED on a pipeline that fails validate_pipeline — raising a structured
        PipelineValidationError BEFORE create_run — instead of launching an opaque
        ``status=failed`` run. This closes the tutorial bypass (tutorial_service calls
        execute() directly with no pre-run validation) and the advisory-gate gap.
        """
        invalid = ValidationResult(
            is_valid=False,
            checks=[],
            errors=[
                ValidationError(
                    component_id="rate",
                    component_type="transform",
                    message="Graph validation failed: 'rate' requires field 'content' not emitted upstream",
                    suggestion=None,
                    error_code=None,
                )
            ],
            readiness=ValidationReadiness(
                authoring_valid=True,
                execution_ready=False,
                completion_ready=False,
                blockers=[
                    ValidationReadinessBlocker(
                        code="graph_structure",
                        component_id="rate",
                        component_type="transform",
                        detail="Graph validation failed.",
                    )
                ],
            ),
        )
        with (
            patch("elspeth.web.execution.validation.validate_pipeline", return_value=invalid),
            pytest.raises(PipelineValidationError) as exc_info,
        ):
            await service.execute(session_id=uuid4())

        # No opaque failed-run: the gate refuses BEFORE create_run.
        assert mock_session_service.create_run.await_count == 0
        # Carries the structured errors for the route to surface as a 422.
        assert exc_info.value.errors
        assert exc_info.value.errors[0].component_id == "rate"

    @pytest.mark.asyncio
    async def test_execute_rejects_backend_execution_readiness_before_run_creation(
        self,
        service: ExecutionServiceImpl,
        mock_session_service: MagicMock,
    ) -> None:
        """Execution admission follows readiness even when validation is green."""
        blocker = ValidationReadinessBlocker(
            code="runtime_admission",
            component_id="pipeline",
            component_type="pipeline",
            detail="The selected runtime policy does not admit this pipeline.",
        )
        not_execution_ready = ValidationResult(
            is_valid=True,
            checks=[],
            errors=[],
            readiness=ValidationReadiness(
                authoring_valid=True,
                execution_ready=False,
                completion_ready=False,
                blockers=[blocker],
            ),
        )

        with (
            patch("elspeth.web.execution.validation.validate_pipeline", return_value=not_execution_ready),
            pytest.raises(ExecutionReadinessError) as exc_info,
        ):
            await service.execute(session_id=uuid4())

        assert exc_info.value.blockers == (blocker,)
        mock_session_service.create_run.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_execute_rejects_backend_execution_readiness_without_blockers(
        self,
        service: ExecutionServiceImpl,
        mock_session_service: MagicMock,
    ) -> None:
        not_execution_ready = ValidationResult(
            is_valid=True,
            checks=[],
            errors=[],
            readiness=ValidationReadiness(
                authoring_valid=True,
                execution_ready=False,
                completion_ready=False,
                blockers=[],
            ),
        )

        with (
            patch("elspeth.web.execution.validation.validate_pipeline", return_value=not_execution_ready),
            pytest.raises(ExecutionReadinessError) as exc_info,
        ):
            await service.execute(session_id=uuid4())

        assert exc_info.value.blockers == ()
        mock_session_service.create_run.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_execute_rejects_saved_now_disabled_plugin_before_run_or_constructor(
        self,
        service: ExecutionServiceImpl,
        mock_session_service: MagicMock,
        mock_settings: _WebSettingsStub,
        tmp_path: Path,
    ) -> None:
        session_id = uuid4()
        (tmp_path / "blobs").mkdir()
        (tmp_path / "outputs").mkdir()
        source_path = tmp_path / "blobs" / "input.txt"
        source_path.write_text("Ada\n", encoding="utf-8")
        mock_settings.data_dir = tmp_path
        mock_session_service.get_current_state.return_value = _composition_state_record(
            session_id=session_id,
            source_path=source_path,
            output_path=tmp_path / "outputs" / "output.jsonl",
            nodes=[],
        )
        unrestricted = PluginAvailabilitySnapshot.for_trained_operator(create_catalog_service())
        disabled = PluginId("sink", "json")
        snapshot = PluginAvailabilitySnapshot.create(
            policy_hash="runtime-policy",
            principal_scope="test:alice",
            available=unrestricted.available - {disabled},
            unavailable=(PluginAvailability(disabled, PluginUnavailableReason.NOT_AUTHORIZED),),
            selected=unrestricted.selected,
            usable_profile_aliases=(),
            selected_profile_aliases=(),
            binding_generation_fingerprint="runtime-policy-generation",
        )
        service._plugin_snapshot_factory = lambda _user_id: snapshot

        with (
            patch("elspeth.web.execution.validation.validate_pipeline", side_effect=_real_validate_pipeline),
            patch("elspeth.web.execution.validation.instantiate_runtime_plugins") as instantiate,
            patch.object(service._executor, "submit") as submit,
            pytest.raises(PipelineValidationError),
        ):
            await service.execute(session_id=session_id, user_id="alice")

        mock_session_service.create_run.assert_not_awaited()
        instantiate.assert_not_called()
        submit.assert_not_called()

    @pytest.mark.asyncio
    async def test_execute_rejects_aws_s3_endpoint_url_before_run_or_provider_instantiation(
        self,
        service: ExecutionServiceImpl,
        mock_session_service: MagicMock,
    ) -> None:
        endpoint_sentinel = "https://provider-canary.attacker.invalid/private"
        state_record = mock_session_service.get_current_state.return_value
        state_record.source = {
            "plugin": "aws_s3",
            "on_success": "out",
            "options": {"endpoint_url": endpoint_sentinel},
            "on_validation_failure": "discard",
        }
        state_record.sources = None
        state_record.nodes = []
        state_record.edges = []
        state_record.outputs = [
            {
                "name": "out",
                "plugin": "json",
                "options": {},
                "on_write_failure": "discard",
            }
        ]
        unrestricted = PluginAvailabilitySnapshot.for_trained_operator(create_catalog_service())
        snapshot = PluginAvailabilitySnapshot.create(
            policy_hash="runtime-policy",
            principal_scope="test:alice",
            available=unrestricted.available,
            unavailable=(),
            selected=unrestricted.selected,
            usable_profile_aliases=(),
            selected_profile_aliases=(),
            binding_generation_fingerprint="runtime-policy-generation",
        )
        service._plugin_snapshot_factory = lambda _user_id: snapshot

        with (
            patch("elspeth.web.execution.validation.validate_pipeline", side_effect=_real_validate_pipeline),
            patch("elspeth.web.execution.validation.load_settings_from_yaml_string") as mock_load,
            patch("elspeth.web.execution.validation.instantiate_runtime_plugins") as mock_instantiate,
            patch.object(service._executor, "submit") as mock_submit,
            pytest.raises(PipelineValidationError) as exc_info,
        ):
            await service.execute(session_id=state_record.session_id)

        assert mock_session_service.create_run.await_count == 0
        mock_load.assert_not_called()
        mock_instantiate.assert_not_called()
        mock_submit.assert_not_called()
        assert exc_info.value.errors[0].error_code == "aws_s3_endpoint_url_not_allowed"
        assert endpoint_sentinel not in str(exc_info.value)
        assert endpoint_sentinel not in repr(exc_info.value)

    @pytest.mark.asyncio
    async def test_execute_allows_valid_pipeline_through_the_gate(
        self, service: ExecutionServiceImpl, mock_session_service: MagicMock
    ) -> None:
        """Fix 1: a valid pipeline passes the new pre-run gate and still creates a run."""
        valid = ValidationResult(
            is_valid=True,
            checks=[],
            errors=[],
            readiness=ValidationReadiness(
                authoring_valid=True,
                execution_ready=True,
                completion_ready=True,
                blockers=[],
            ),
        )
        with (
            patch("elspeth.web.execution.validation.validate_pipeline", return_value=valid),
            patch.object(service, "_run_pipeline"),
        ):
            run_id = await service.execute(session_id=uuid4())
        assert isinstance(run_id, UUID)
        mock_session_service.create_run.assert_called_once()

    @pytest.mark.asyncio
    async def test_execute_merges_selected_state_fact_against_authored_state(
        self,
        service: ExecutionServiceImpl,
        mock_session_service: MagicMock,
    ) -> None:
        """Completion facts bind to the persisted graph, not runtime materialization."""
        from elspeth.web.composer.state import SourceSpec
        from elspeth.web.execution.completion_gates import (
            AdvisorSignoffGateFact,
            CompletionGateFacts,
            completion_gate_fingerprint,
        )
        from elspeth.web.execution.completion_gates import (
            merge_completion_gates as real_merge_completion_gates,
        )

        selected_record = mock_session_service.get_state.return_value
        session_id = selected_record.session_id
        authored_state = state_from_record(selected_record)
        fact = AdvisorSignoffGateFact(
            detail="The advisor sign-off could not be obtained; the pipeline cannot complete.",
            for_graph=completion_gate_fingerprint(authored_state),
        )
        selected_record.composer_meta = {
            "completion_gates": {
                "advisor_signoff": {
                    "status": "blocked",
                    "detail": fact.detail,
                    "for_graph": fact.for_graph,
                }
            }
        }
        runtime_state = authored_state.with_named_source(
            "runtime_materialized",
            SourceSpec(
                plugin="text",
                on_success="runtime_rows",
                options={},
                on_validation_failure="discard",
            ),
        )
        valid = ValidationResult(
            is_valid=True,
            checks=[],
            errors=[],
            readiness=ValidationReadiness(
                authoring_valid=True,
                execution_ready=True,
                completion_ready=True,
                blockers=[],
            ),
        )

        with (
            patch("elspeth.web.execution.service.materialize_state_for_execution", return_value=runtime_state),
            patch("elspeth.web.execution.validation.validate_pipeline", return_value=valid),
            patch(
                "elspeth.web.execution.service.merge_completion_gates",
                wraps=real_merge_completion_gates,
            ) as merge,
            patch.object(service, "_run_pipeline"),
        ):
            run_id = await service.execute(session_id=session_id, state_id=selected_record.id)

        assert isinstance(run_id, UUID)
        merge.assert_called_once()
        proof_result, merged_facts, fingerprint_state = merge.call_args.args
        assert proof_result.checks[-1].name == "proof_diagnostics"
        assert proof_result.checks[-1].passed is True
        assert merged_facts == CompletionGateFacts(advisor_signoff=fact)
        assert fingerprint_state == authored_state
        mock_session_service.get_current_state.assert_not_awaited()
        mock_session_service.create_run.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_execute_rejects_corrupt_completion_gate_before_run_creation(
        self,
        service: ExecutionServiceImpl,
        mock_session_service: MagicMock,
    ) -> None:
        selected_record = mock_session_service.get_current_state.return_value
        private_persisted_detail = "private-persisted-advisor-detail"
        selected_record.composer_meta = {
            "completion_gates": {
                "advisor_signoff": {
                    "status": "blocked",
                    "detail": private_persisted_detail,
                    # Required for_graph intentionally absent: Tier-1 corruption.
                }
            }
        }

        with pytest.raises(CompletionGateIntegrityError) as exc_info:
            await service.execute(session_id=selected_record.session_id)

        assert private_persisted_detail not in str(exc_info.value)
        assert private_persisted_detail not in repr(exc_info.value)
        mock_session_service.create_run.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_validate_returns_structured_failure_when_no_current_state(
        self,
        service: ExecutionServiceImpl,
        mock_session_service: MagicMock,
    ) -> None:
        """validate() must return a typed no-state blocker instead of raising."""
        session_id = uuid4()
        mock_session_service.get_current_state.return_value = None

        result = await service.validate(session_id, user_id="alice")

        assert result.is_valid is False
        assert result.checks[0].name == "state_exists"
        assert result.checks[0].passed is False
        assert result.errors[0].message == "No composition state exists for this session"
        assert result.readiness.authoring_valid is False
        assert result.readiness.blockers[0].code == "state_exists"
        mock_session_service.get_current_state.assert_awaited_once_with(session_id)

    @pytest.mark.asyncio
    async def test_validate_delegates_current_state_to_worker_backed_validate_state(
        self,
        service: ExecutionServiceImpl,
        mock_session_service: MagicMock,
    ) -> None:
        """validate() materializes the current state and delegates scoped validation."""
        session_id = uuid4()
        expected = ValidationResult(
            is_valid=True,
            checks=[],
            errors=[],
            readiness=ValidationReadiness(
                authoring_valid=True,
                execution_ready=True,
                completion_ready=True,
                blockers=[],
            ),
        )
        validate_state = AsyncMock(spec=service.validate_state, return_value=expected)
        service.validate_state = validate_state  # type: ignore[method-assign]

        result = await service.validate(session_id, user_id="alice")

        assert result is expected
        validate_state.assert_awaited_once()
        delegated_state = validate_state.await_args.args[0]
        assert delegated_state.version == mock_session_service.get_current_state.return_value.version
        assert validate_state.await_args.kwargs == {
            "user_id": "alice",
            "session_id": session_id,
            "completion_gates": None,
        }

    @pytest.mark.asyncio
    async def test_validate_state_runs_validation_in_worker_with_secret_context(
        self,
        service: ExecutionServiceImpl,
        mock_session_service: MagicMock,
    ) -> None:
        """validate_state() keeps sync validation off the event loop."""
        state = state_from_record(mock_session_service.get_current_state.return_value)
        expected = ValidationResult(
            is_valid=True,
            checks=[],
            errors=[],
            readiness=ValidationReadiness(
                authoring_valid=True,
                execution_ready=True,
                completion_ready=True,
                blockers=[],
            ),
        )

        with patch("elspeth.web.execution.service.run_sync_in_worker", new_callable=AsyncMock) as run_worker:
            run_worker.return_value = expected
            result = await service.validate_state(state, user_id="alice", session_id=uuid4())

        assert result is expected
        run_worker.assert_awaited_once()
        worker_call = run_worker.await_args.args[0]
        assert isinstance(worker_call, partial)
        assert worker_call.func == service._authoritative_state_preflight_sync
        assert worker_call.args == (state,)
        assert worker_call.keywords is not None
        assert worker_call.keywords["user_id"] == "alice"
        assert worker_call.keywords["session_id"] is not None
        assert worker_call.keywords["plugin_snapshot"] is not None

    @pytest.mark.asyncio
    async def test_validate_state_merges_persisted_completion_gate(
        self,
        service: ExecutionServiceImpl,
        mock_session_service: MagicMock,
    ) -> None:
        """A persisted advisor sign-off blocker survives the fresh recompute."""
        from elspeth.web.execution.completion_gates import (
            AdvisorSignoffGateFact,
            CompletionGateFacts,
            completion_gate_fingerprint,
        )
        from elspeth.web.execution.schemas import ADVISOR_SIGNOFF_BLOCKED_CODE

        state = state_from_record(mock_session_service.get_current_state.return_value)
        facts = CompletionGateFacts(
            advisor_signoff=AdvisorSignoffGateFact(
                detail="The advisor sign-off could not be obtained; the pipeline cannot complete.",
                for_graph=completion_gate_fingerprint(state),
            )
        )

        with patch(
            "elspeth.web.execution.validation.validate_pipeline",
            return_value=_successful_core_validation_result(),
        ):
            result = await service.validate_state(state, user_id="alice", session_id=uuid4(), completion_gates=facts)

        assert result.is_valid is True
        assert result.readiness.authoring_valid is True
        assert result.readiness.execution_ready is True
        assert result.readiness.completion_ready is False
        assert [blocker.code for blocker in result.readiness.blockers] == [ADVISOR_SIGNOFF_BLOCKED_CODE]
        assert len(result.checks) == 26
        assert [check.name for check in result.checks[24:]] == ["advisor_signoff", "proof_diagnostics"]

    @pytest.mark.asyncio
    async def test_validate_passes_record_completion_gates_to_validate_state(
        self,
        service: ExecutionServiceImpl,
        mock_session_service: MagicMock,
    ) -> None:
        """validate() parses the record's composer_meta and threads the facts through."""
        from elspeth.web.execution.completion_gates import AdvisorSignoffGateFact, CompletionGateFacts

        session_id = uuid4()
        mock_session_service.get_current_state.return_value.composer_meta = {
            "completion_gates": {
                "advisor_signoff": {
                    "status": "blocked",
                    "detail": "The advisor sign-off could not be obtained; the pipeline cannot complete.",
                    "for_graph": "0" * 64,
                }
            }
        }
        expected = ValidationResult(
            is_valid=True,
            checks=[],
            errors=[],
            readiness=ValidationReadiness(
                authoring_valid=True,
                execution_ready=True,
                completion_ready=True,
                blockers=[],
            ),
        )
        validate_state = AsyncMock(spec=service.validate_state, return_value=expected)
        service.validate_state = validate_state  # type: ignore[method-assign]

        await service.validate(session_id, user_id="alice")

        validate_state.assert_awaited_once()
        assert validate_state.await_args.kwargs["completion_gates"] == CompletionGateFacts(
            advisor_signoff=AdvisorSignoffGateFact(
                detail="The advisor sign-off could not be obtained; the pipeline cannot complete.",
                for_graph="0" * 64,
            )
        )

    @pytest.mark.asyncio
    async def test_get_status_returns_run_status(self, service: ExecutionServiceImpl, mock_session_service: MagicMock) -> None:
        run_id = uuid4()
        mock_session_service.get_run.return_value = _run_record_stub(
            id=run_id,  # B7: RunRecord uses `id`, not `run_id`
            status="running",
            started_at=datetime.now(tz=UTC),
            finished_at=None,
            rows_processed=50,
            rows_succeeded=48,
            rows_failed=2,
            rows_routed_success=0,
            rows_routed_failure=0,
            rows_quarantined=0,
            error=None,
            landscape_run_id=None,
        )
        status = await service.get_status(run_id)
        assert status.status == "running"
        assert status.accounting is None


class TestAuthoritativeProofDiagnostics:
    @pytest.mark.asyncio
    async def test_guided_sentinel_with_valid_custody_reads_once_and_rejects_numeric_gate(
        self,
        service: ExecutionServiceImpl,
        tmp_path: Path,
    ) -> None:
        session_id = uuid4()
        blob_id = uuid4()
        source_path = tmp_path / "guided-sentinel-amounts.csv"
        source_path.write_text("amount\n250.00\n750.00\n", encoding="utf-8")
        state = _guided_sentinel_proof_gate_state(source_path=source_path, blob_id=blob_id)
        blob_service = _install_ready_proof_blob(
            service,
            session_id=session_id,
            blob_id=blob_id,
            source_path=source_path,
        )

        with patch(
            "elspeth.web.execution.validation.validate_pipeline",
            return_value=_successful_core_validation_result(),
        ):
            result = await service.validate_state(state, user_id="alice", session_id=session_id)

        assert result.is_valid is False
        assert result.checks[24].name == "proof_diagnostics"
        assert result.checks[24].passed is False
        assert [error.error_code for error in result.errors] == ["gate_expression_type_mismatch_against_source_schema"]
        blob_service.get_blob.assert_awaited_once_with(blob_id)
        blob_service.read_blob_content_prefix_verified.assert_awaited_once_with(
            blob_id,
            prefix_bytes=8 * 1024,
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("terminal_kind", ["live", "completed", "exited_to_freeform"])
    async def test_guided_terminal_kind_never_removes_the_authoritative_source_proof(
        self,
        service: ExecutionServiceImpl,
        tmp_path: Path,
        terminal_kind: str,
    ) -> None:
        """elspeth-3b45cdb41e: the three-terminal table must reject identically.

        Varying ONLY the guided terminal on an otherwise identical state, the
        admission proof must resolve custody and reject the numeric gate the
        same way. EXITED_TO_FREEFORM previously hit the export-family identity
        return, ran zero resolver calls, and recorded a fabricated passing
        proof check.
        """
        from dataclasses import replace

        from elspeth.web.composer.guided.state_machine import TerminalKind, TerminalReason, TerminalState

        session_id = uuid4()
        blob_id = uuid4()
        source_path = tmp_path / f"terminal-{terminal_kind}-amounts.csv"
        source_path.write_text("amount\n250.00\n750.00\n", encoding="utf-8")
        state = _guided_sentinel_proof_gate_state(source_path=source_path, blob_id=blob_id)
        if terminal_kind == "completed":
            terminal = TerminalState(kind=TerminalKind.COMPLETED, reason=None, pipeline_yaml="pipeline: {}")
        elif terminal_kind == "exited_to_freeform":
            terminal = TerminalState(
                kind=TerminalKind.EXITED_TO_FREEFORM,
                reason=TerminalReason.USER_PRESSED_EXIT,
                pipeline_yaml=None,
            )
        else:
            terminal = None
        if terminal is not None:
            assert state.guided_session is not None
            state = replace(state, guided_session=replace(state.guided_session, terminal=terminal))
        blob_service = _install_ready_proof_blob(
            service,
            session_id=session_id,
            blob_id=blob_id,
            source_path=source_path,
        )

        with patch(
            "elspeth.web.execution.validation.validate_pipeline",
            return_value=_successful_core_validation_result(),
        ):
            result = await service.validate_state(state, user_id="alice", session_id=session_id)

        assert result.is_valid is False
        assert result.checks[24].name == "proof_diagnostics"
        assert result.checks[24].passed is False
        assert [error.error_code for error in result.errors] == ["gate_expression_type_mismatch_against_source_schema"]
        blob_service.get_blob.assert_awaited_once_with(blob_id)
        blob_service.read_blob_content_prefix_verified.assert_awaited_once_with(
            blob_id,
            prefix_bytes=8 * 1024,
        )

    @pytest.mark.asyncio
    async def test_exited_history_that_cannot_bind_fails_closed_without_reading(
        self,
        service: ExecutionServiceImpl,
        tmp_path: Path,
    ) -> None:
        """A diverged exited session blocks with a FAILED proof check, never a pass."""
        from dataclasses import replace

        from elspeth.web.composer.guided.state_machine import TerminalKind, TerminalReason, TerminalState

        session_id = uuid4()
        blob_id = uuid4()
        source_path = tmp_path / "exited-renamed-amounts.csv"
        source_path.write_text("amount\n250.00\n750.00\n", encoding="utf-8")
        base = _guided_sentinel_proof_gate_state(source_path=source_path, blob_id=blob_id)
        assert base.guided_session is not None
        state = replace(
            base,
            sources={"renamed": base.sources["source"]},
            guided_session=replace(
                base.guided_session,
                terminal=TerminalState(
                    kind=TerminalKind.EXITED_TO_FREEFORM,
                    reason=TerminalReason.USER_PRESSED_EXIT,
                    pipeline_yaml=None,
                ),
            ),
        )
        blob_service = _install_ready_proof_blob(
            service,
            session_id=session_id,
            blob_id=blob_id,
            source_path=source_path,
        )

        with patch(
            "elspeth.web.execution.validation.validate_pipeline",
            return_value=_successful_core_validation_result(),
        ):
            result = await service.validate_state(state, user_id="alice", session_id=session_id)

        assert result.is_valid is False
        assert result.checks[24].name == "proof_diagnostics"
        assert result.checks[24].passed is False
        assert "unavailable" in result.checks[24].detail
        assert [error.error_code for error in result.errors] == ["source_inspection_failed"]
        assert result.readiness.execution_ready is False
        blob_service.get_blob.assert_not_awaited()
        blob_service.read_blob_content_prefix_verified.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "custody_failure",
        ["wrong_session", "wrong_path", "not_ready", "not_found"],
    )
    async def test_guided_sentinel_with_failed_custody_is_invalid_without_reading(
        self,
        service: ExecutionServiceImpl,
        tmp_path: Path,
        custody_failure: str,
    ) -> None:
        session_id = uuid4()
        blob_id = uuid4()
        source_path = tmp_path / f"guided-sentinel-{custody_failure}.csv"
        source_path.write_text("amount\n250.00\n750.00\n", encoding="utf-8")
        state = _guided_sentinel_proof_gate_state(source_path=source_path, blob_id=blob_id)
        blob_service = create_autospec(BlobServiceProtocol, instance=True)
        if custody_failure == "not_found":
            blob_service.get_blob.side_effect = BlobNotFoundError(str(blob_id))
        else:
            blob_service.get_blob.return_value = _blob_record_stub(
                blob_id=blob_id,
                session_id=uuid4() if custody_failure == "wrong_session" else session_id,
                filename=source_path.name,
                mime_type="text/csv",
                size_bytes=source_path.stat().st_size,
                content_hash=hashlib.sha256(source_path.read_bytes()).hexdigest(),
                storage_path=(str(tmp_path / "different-custody-path.csv") if custody_failure == "wrong_path" else str(source_path)),
                status="pending" if custody_failure == "not_ready" else "ready",
            )
        service._blob_service = blob_service

        with patch(
            "elspeth.web.execution.validation.validate_pipeline",
            return_value=_successful_core_validation_result(),
        ):
            result = await service.validate_state(state, user_id="alice", session_id=session_id)

        assert result.is_valid is False
        assert result.checks[24].name == "proof_diagnostics"
        assert result.checks[24].passed is False
        assert [error.error_code for error in result.errors] == ["source_inspection_failed"]
        blob_service.get_blob.assert_awaited_once_with(blob_id)
        blob_service.read_blob_content_prefix_verified.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_observed_csv_numeric_gate_after_declared_queue_is_rejected(
        self,
        service: ExecutionServiceImpl,
        tmp_path: Path,
    ) -> None:
        from dataclasses import replace

        from elspeth.web.composer.state import SourceSpec

        session_id = uuid4()
        blob_id = uuid4()
        source_path = tmp_path / "queued-amounts.csv"
        source_path.write_text("amount\n250.00\n750.00\n", encoding="utf-8")
        direct_state = _proof_gate_state(source_path=source_path, blob_id=blob_id)
        state = replace(
            direct_state,
            sources={
                "source": replace(
                    direct_state.sources["source"],
                    on_success="inbound",
                ),
                "other": SourceSpec(
                    plugin="csv",
                    on_success="inbound",
                    options={
                        "path": str(tmp_path / "other.csv"),
                        "schema": {"mode": "fixed", "fields": ["amount: float"]},
                    },
                    on_validation_failure="discard",
                ),
            },
            nodes=(
                _queue_node("inbound"),
                replace(direct_state.nodes[0], input="inbound"),
            ),
        )
        _install_ready_proof_blob(
            service,
            session_id=session_id,
            blob_id=blob_id,
            source_path=source_path,
        )

        with patch(
            "elspeth.web.execution.validation.validate_pipeline",
            return_value=_successful_core_validation_result(),
        ):
            result = await service.validate_state(state, user_id="alice", session_id=session_id)

        assert result.is_valid is False
        assert [error.error_code for error in result.errors] == ["gate_expression_type_mismatch_against_source_schema"]

    @pytest.mark.asyncio
    async def test_more_than_256_blob_sources_blocks_instead_of_partially_passing(
        self,
        service: ExecutionServiceImpl,
        tmp_path: Path,
    ) -> None:
        from dataclasses import replace

        from elspeth.web.composer.state import SourceSpec

        base = _proof_gate_state(source_path=tmp_path / "source-0.csv", blob_id=uuid4())
        sources = {
            f"source_{index}": SourceSpec(
                plugin="csv",
                on_success=f"rows_{index}",
                options={
                    "path": str(tmp_path / f"source-{index}.csv"),
                    "blob_ref": str(uuid4()),
                    "schema": {"mode": "observed"},
                },
                on_validation_failure="discard",
            )
            for index in range(257)
        }
        state = replace(base, sources=sources, nodes=())
        service._blob_service = None

        with patch(
            "elspeth.web.execution.validation.validate_pipeline",
            return_value=_successful_core_validation_result(),
        ):
            result = await service.validate_state(state, user_id="alice", session_id=uuid4())

        assert result.is_valid is False
        assert [error.error_code for error in result.errors] == ["source_inspection_failed"]

    @pytest.mark.asyncio
    async def test_observed_csv_numeric_gate_after_passthrough_is_rejected(
        self,
        service: ExecutionServiceImpl,
        tmp_path: Path,
    ) -> None:
        from dataclasses import replace

        from elspeth.web.composer.state import NodeSpec

        session_id = uuid4()
        blob_id = uuid4()
        source_path = tmp_path / "passthrough-amounts.csv"
        source_path.write_text("amount\n250.00\n750.00\n", encoding="utf-8")
        direct_state = _proof_gate_state(source_path=source_path, blob_id=blob_id)
        gate = direct_state.nodes[0]
        passthrough = NodeSpec(
            id="retain_fields",
            node_type="transform",
            plugin="passthrough",
            input="rows",
            on_success="preserved_rows",
            on_error=None,
            options={"schema": {"mode": "observed"}},
            condition=None,
            routes=None,
            fork_to=None,
            branches=None,
            policy=None,
            merge=None,
        )
        state = replace(
            direct_state,
            nodes=(passthrough, replace(gate, input="preserved_rows")),
        )
        _install_ready_proof_blob(
            service,
            session_id=session_id,
            blob_id=blob_id,
            source_path=source_path,
        )

        with patch(
            "elspeth.web.execution.validation.validate_pipeline",
            return_value=_successful_core_validation_result(),
        ):
            result = await service.validate_state(state, user_id="alice", session_id=session_id)

        assert result.is_valid is False
        assert [error.error_code for error in result.errors] == ["gate_expression_type_mismatch_against_source_schema"]

    @pytest.mark.asyncio
    async def test_second_observed_csv_source_feeding_numeric_gate_is_rejected(
        self,
        service: ExecutionServiceImpl,
        tmp_path: Path,
    ) -> None:
        from dataclasses import replace

        from elspeth.web.composer.state import SourceSpec

        session_id = uuid4()
        safe_blob_id = uuid4()
        risky_blob_id = uuid4()
        safe_path = tmp_path / "first-safe.csv"
        risky_path = tmp_path / "second-risky.csv"
        safe_path.write_text("label\nready\n", encoding="utf-8")
        risky_path.write_text("amount\n250.00\n750.00\n", encoding="utf-8")
        risky_state = _proof_gate_state(source_path=risky_path, blob_id=risky_blob_id)
        risky_source = replace(risky_state.sources["source"], on_success="risky_rows")
        state = replace(
            risky_state,
            sources={
                "source": SourceSpec(
                    plugin="csv",
                    on_success="safe_rows",
                    options={
                        "path": str(safe_path),
                        "blob_ref": str(safe_blob_id),
                        "schema": {"mode": "observed"},
                    },
                    on_validation_failure="discard",
                ),
                "risky": risky_source,
            },
            nodes=(replace(risky_state.nodes[0], input="risky_rows"),),
        )
        blob_service = _install_ready_proof_blobs(
            service,
            session_id=session_id,
            bindings={safe_blob_id: safe_path, risky_blob_id: risky_path},
        )

        with patch(
            "elspeth.web.execution.validation.validate_pipeline",
            return_value=_successful_core_validation_result(),
        ):
            result = await service.validate_state(state, user_id="alice", session_id=session_id)

        assert result.is_valid is False
        assert [error.error_code for error in result.errors] == ["gate_expression_type_mismatch_against_source_schema"]
        assert blob_service.read_blob_content_prefix_verified.await_count == 2

    @pytest.mark.asyncio
    async def test_sources_sharing_blob_evaluate_both_topologies_with_one_verified_read(
        self,
        service: ExecutionServiceImpl,
        tmp_path: Path,
    ) -> None:
        from dataclasses import replace

        session_id = uuid4()
        blob_id = uuid4()
        source_path = tmp_path / "shared-amounts.csv"
        source_path.write_text("amount\n250.00\n750.00\n", encoding="utf-8")
        base = _proof_gate_state(source_path=source_path, blob_id=blob_id)
        base_source = base.sources["source"]
        base_gate = base.nodes[0]
        state = replace(
            base,
            sources={
                "orders": replace(base_source, on_success="order_rows"),
                "refunds": replace(base_source, on_success="refund_rows"),
            },
            nodes=(
                replace(base_gate, id="order_gate", input="order_rows"),
                replace(base_gate, id="refund_gate", input="refund_rows"),
            ),
        )
        blob_service = _install_ready_proof_blob(
            service,
            session_id=session_id,
            blob_id=blob_id,
            source_path=source_path,
        )

        with patch(
            "elspeth.web.execution.validation.validate_pipeline",
            return_value=_successful_core_validation_result(),
        ):
            result = await service.validate_state(state, user_id="alice", session_id=session_id)

        assert result.is_valid is False
        assert [error.component_id for error in result.errors] == ["order_gate", "refund_gate"]
        blob_service.read_blob_content_prefix_verified.assert_awaited_once_with(
            blob_id,
            prefix_bytes=8 * 1024,
        )

    @pytest.mark.asyncio
    async def test_validate_state_rejects_observed_csv_numeric_gate_after_canonical_core(
        self,
        service: ExecutionServiceImpl,
        tmp_path: Path,
    ) -> None:
        session_id = uuid4()
        blob_id = uuid4()
        source_path = tmp_path / "amounts.csv"
        source_path.write_text("amount\n250.00\n750.00\n", encoding="utf-8")
        state = _proof_gate_state(source_path=source_path, blob_id=blob_id)
        _install_ready_proof_blob(
            service,
            session_id=session_id,
            blob_id=blob_id,
            source_path=source_path,
        )

        with patch(
            "elspeth.web.execution.validation.validate_pipeline",
            return_value=_successful_core_validation_result(),
        ):
            result = await service.validate_state(state, user_id="alice", session_id=session_id)

        assert [check.name for check in result.checks[:24]] == [check.name for check in _successful_core_validation_result().checks]
        assert result.checks[24].name == "proof_diagnostics"
        assert result.checks[24].passed is False
        assert [error.error_code for error in result.errors] == ["gate_expression_type_mismatch_against_source_schema"]
        assert result.is_valid is False
        assert result.readiness.authoring_valid is False
        assert result.readiness.execution_ready is False
        assert result.readiness.completion_ready is False

    @pytest.mark.asyncio
    async def test_authoritative_proof_uses_verified_prefix_without_direct_path_read(
        self,
        service: ExecutionServiceImpl,
        tmp_path: Path,
    ) -> None:
        session_id = uuid4()
        blob_id = uuid4()
        source_path = tmp_path / "verified-prefix.csv"
        source_path.write_text("amount\n250.00\n", encoding="utf-8")
        state = _proof_gate_state(source_path=source_path, blob_id=blob_id)
        blob_service = _install_ready_proof_blob(
            service,
            session_id=session_id,
            blob_id=blob_id,
            source_path=source_path,
        )

        with (
            patch(
                "elspeth.web.execution.validation.validate_pipeline",
                return_value=_successful_core_validation_result(),
            ),
            patch.object(Path, "read_bytes", side_effect=AssertionError("proof must use verified prefix API")),
        ):
            result = await service.validate_state(state, user_id="alice", session_id=session_id)

        assert result.is_valid is False
        blob_service.read_blob_content_prefix_verified.assert_awaited_once_with(
            blob_id,
            prefix_bytes=8 * 1024,
        )

    @pytest.mark.asyncio
    async def test_execute_rejects_observed_csv_numeric_gate_before_create_run(
        self,
        service: ExecutionServiceImpl,
        mock_session_service: MagicMock,
        mock_settings: _WebSettingsStub,
        tmp_path: Path,
    ) -> None:
        session_id = uuid4()
        blob_id = uuid4()
        source_dir = tmp_path / "blobs" / str(session_id)
        source_dir.mkdir(parents=True)
        source_path = source_dir / "amounts.csv"
        source_path.write_text("amount\n250.00\n750.00\n", encoding="utf-8")
        state = _proof_gate_state(source_path=source_path, blob_id=blob_id)
        state_dict = state.to_dict()
        state_record = mock_session_service.get_current_state.return_value
        state_record.session_id = session_id
        state_record.source = state_dict["sources"]["source"]
        state_record.sources = state_dict["sources"]
        state_record.nodes = state_dict["nodes"]
        state_record.edges = state_dict["edges"]
        state_record.outputs = state_dict["outputs"]
        mock_settings.data_dir = tmp_path
        _install_ready_proof_blob(
            service,
            session_id=session_id,
            blob_id=blob_id,
            source_path=source_path,
        )
        snapshot_calls = 0
        snapshot = PluginAvailabilitySnapshot.for_trained_operator(create_catalog_service())

        def _snapshot_for_user(_user_id: str) -> PluginAvailabilitySnapshot:
            nonlocal snapshot_calls
            snapshot_calls += 1
            return snapshot

        service._plugin_snapshot_factory = _snapshot_for_user

        with (
            patch(
                "elspeth.web.execution.validation.validate_pipeline",
                return_value=_successful_core_validation_result(),
            ),
            patch("elspeth.web.execution.service.validate_semantic_contracts", return_value=((), (), ())),
            pytest.raises(PipelineValidationError) as exc_info,
        ):
            await service.execute(session_id=session_id, user_id="alice")

        assert exc_info.value.errors[0].error_code == "gate_expression_type_mismatch_against_source_schema"
        assert snapshot_calls == 1
        mock_session_service.create_run.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_guided_reviewed_source_binding_is_inspected_without_live_blob_ref(
        self,
        service: ExecutionServiceImpl,
        tmp_path: Path,
    ) -> None:
        from dataclasses import replace

        from elspeth.web.composer.guided.resolved import SourceResolved
        from elspeth.web.composer.guided.state_machine import GuidedSession

        session_id = uuid4()
        blob_id = uuid4()
        source_path = tmp_path / "guided-amounts.csv"
        source_path.write_text("amount\n250.00\n", encoding="utf-8")
        live_state = _proof_gate_state(source_path=source_path, blob_id=None)
        stable_id = str(uuid4())
        guided = replace(
            GuidedSession.initial(),
            source_order=(stable_id,),
            reviewed_sources={
                stable_id: SourceResolved(
                    name="source",
                    plugin="csv",
                    options={
                        "path": str(source_path),
                        "blob_ref": str(blob_id),
                        "schema": {"mode": "observed"},
                    },
                    observed_columns=("amount",),
                    sample_rows=(),
                    on_validation_failure="discard",
                )
            },
        )
        state = replace(live_state, guided_session=guided)
        blob_service = _install_ready_proof_blob(
            service,
            session_id=session_id,
            blob_id=blob_id,
            source_path=source_path,
        )

        with patch(
            "elspeth.web.execution.validation.validate_pipeline",
            return_value=_successful_core_validation_result(),
        ):
            result = await service.validate_state(state, user_id="alice", session_id=session_id)

        assert result.is_valid is False
        assert result.checks[24].name == "proof_diagnostics"
        assert result.errors[0].error_code == "gate_expression_type_mismatch_against_source_schema"
        blob_service.get_blob.assert_awaited_once_with(blob_id)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("schema_mode", ["fixed", "flexible"])
    async def test_explicit_numeric_source_schema_passes_proof(
        self,
        service: ExecutionServiceImpl,
        tmp_path: Path,
        schema_mode: str,
    ) -> None:
        session_id = uuid4()
        blob_id = uuid4()
        source_path = tmp_path / f"{schema_mode}-amounts.csv"
        source_path.write_text("amount\n250.00\n", encoding="utf-8")
        state = _proof_gate_state(
            source_path=source_path,
            blob_id=blob_id,
            schema_mode=schema_mode,
        )
        _install_ready_proof_blob(
            service,
            session_id=session_id,
            blob_id=blob_id,
            source_path=source_path,
        )

        with patch(
            "elspeth.web.execution.validation.validate_pipeline",
            return_value=_successful_core_validation_result(),
        ):
            result = await service.validate_state(state, user_id="alice", session_id=session_id)

        assert result.is_valid is True
        assert result.checks[24].name == "proof_diagnostics"
        assert result.checks[24].passed is True
        assert result.errors == []

    @pytest.mark.asyncio
    async def test_observed_string_comparison_passes_proof(
        self,
        service: ExecutionServiceImpl,
        tmp_path: Path,
    ) -> None:
        session_id = uuid4()
        blob_id = uuid4()
        source_path = tmp_path / "regions.csv"
        source_path.write_text("region\nNSW\nVIC\n", encoding="utf-8")
        state = _proof_gate_state(
            source_path=source_path,
            blob_id=blob_id,
            condition="row['region'] == 'NSW'",
        )
        _install_ready_proof_blob(
            service,
            session_id=session_id,
            blob_id=blob_id,
            source_path=source_path,
        )

        with patch(
            "elspeth.web.execution.validation.validate_pipeline",
            return_value=_successful_core_validation_result(),
        ):
            result = await service.validate_state(state, user_id="alice", session_id=session_id)

        assert result.is_valid is True
        assert result.checks[24].name == "proof_diagnostics"
        assert result.checks[24].passed is True

    @pytest.mark.asyncio
    async def test_uninspectable_blob_source_abstains_with_passing_proof_check(
        self,
        service: ExecutionServiceImpl,
        tmp_path: Path,
    ) -> None:
        blob_id = uuid4()
        source_path = tmp_path / "not-authoritatively-resolved.csv"
        state = _proof_gate_state(
            source_path=source_path,
            blob_id=blob_id,
        )
        service._blob_service = None

        with patch(
            "elspeth.web.execution.validation.validate_pipeline",
            return_value=_successful_core_validation_result(),
        ):
            result = await service.validate_state(state, user_id="alice", session_id=uuid4())

        assert result.is_valid is True
        assert result.checks[24].name == "proof_diagnostics"
        assert result.checks[24].passed is True
        assert result.errors == []

    @pytest.mark.asyncio
    async def test_uninspectable_blob_source_with_exited_guided_claim_fails_closed(
        self,
        service: ExecutionServiceImpl,
        tmp_path: Path,
    ) -> None:
        """elspeth-3b45cdb41e: exited review custody that cannot resolve must block.

        Before the fix the exited sentinel claim was excluded from the
        resolver's custody census (39c7fc635's parity with the export-family
        skip), so an unresolvable claimed source abstained into a passing
        proof check. Admission now keeps exited claims in the census and the
        unresolvable claim surfaces as the blocking custody diagnostic.
        """
        from dataclasses import replace

        from elspeth.web.composer.guided.state_machine import TerminalKind, TerminalReason, TerminalState

        blob_id = uuid4()
        source_path = tmp_path / "not-authoritatively-resolved.csv"
        state = _proof_gate_state(
            source_path=source_path,
            blob_id=blob_id,
        )
        historical_guided = _guided_sentinel_proof_gate_state(
            source_path=source_path,
            blob_id=blob_id,
        ).guided_session
        assert historical_guided is not None
        state = replace(
            state,
            guided_session=replace(
                historical_guided,
                terminal=TerminalState(
                    kind=TerminalKind.EXITED_TO_FREEFORM,
                    reason=TerminalReason.USER_PRESSED_EXIT,
                    pipeline_yaml=None,
                ),
            ),
        )
        service._blob_service = None

        with patch(
            "elspeth.web.execution.validation.validate_pipeline",
            return_value=_successful_core_validation_result(),
        ):
            result = await service.validate_state(state, user_id="alice", session_id=uuid4())

        assert result.is_valid is False
        assert result.checks[24].name == "proof_diagnostics"
        assert result.checks[24].passed is False
        assert [error.error_code for error in result.errors] == ["source_inspection_failed"]

    @pytest.mark.asyncio
    async def test_ambiguous_blob_path_binding_abstains_without_reading_bytes(
        self,
        service: ExecutionServiceImpl,
        tmp_path: Path,
    ) -> None:
        from dataclasses import replace

        session_id = uuid4()
        blob_id = uuid4()
        source_path = tmp_path / "canonical.csv"
        source_path.write_text("amount\n250.00\n", encoding="utf-8")
        base = _proof_gate_state(source_path=source_path, blob_id=blob_id)
        source = base.sources["source"]
        state = replace(
            base,
            sources={
                "source": replace(
                    source,
                    options={
                        **source.options,
                        "file": str(tmp_path / "conflicting.csv"),
                    },
                )
            },
        )
        blob_service = _install_ready_proof_blob(
            service,
            session_id=session_id,
            blob_id=blob_id,
            source_path=source_path,
        )

        with patch(
            "elspeth.web.execution.validation.validate_pipeline",
            return_value=_successful_core_validation_result(),
        ):
            result = await service.validate_state(state, user_id="alice", session_id=session_id)

        assert result.is_valid is True
        assert result.checks[24].name == "proof_diagnostics"
        assert result.checks[24].passed is True
        blob_service.read_blob_content_prefix_verified.assert_not_awaited()


def _text_source(path: Path, on_success: str) -> Any:
    """A row-per-line text source rooted at ``path`` publishing ``on_success``."""
    from elspeth.web.composer.state import SourceSpec

    return SourceSpec(
        plugin="text",
        on_success=on_success,
        options={"path": str(path), "column": "body", "schema": {"mode": "observed"}},
        on_validation_failure="discard",
    )


def _queue_node(queue_id: str = "inbound") -> Any:
    """Canonical structural queue NodeSpec (id == input, plugin None)."""
    from elspeth.web.composer.state import NodeSpec

    return NodeSpec(
        id=queue_id,
        node_type="queue",
        plugin=None,
        input=queue_id,
        on_success=None,
        on_error=None,
        options={},
        condition=None,
        routes=None,
        fork_to=None,
        branches=None,
        policy=None,
        merge=None,
    )


def _fanout_llm_node(input_label: str, node_id: str = "classify") -> Any:
    """LLM transform consuming ``input_label`` (one provider call per row)."""
    from elspeth.web.composer.state import NodeSpec

    return NodeSpec(
        id=node_id,
        node_type="transform",
        plugin="llm",
        input=input_label,
        on_success="out",
        on_error="errors",
        options={
            "provider": "openrouter",
            "model": "openai/gpt-4o-mini",
            "api_key": {"secret_ref": "OPENROUTER_API_KEY"},
        },
        condition=None,
        routes=None,
        fork_to=None,
        branches=None,
        policy=None,
        merge=None,
    )


def _coalesce_node(
    *,
    node_id: str,
    on_success: str,
    branches: dict[str, str],
) -> Any:
    """Coalesce node whose branch identities may differ from connections."""
    from elspeth.web.composer.state import NodeSpec

    return NodeSpec(
        id=node_id,
        node_type="coalesce",
        plugin=None,
        input=f"{node_id}_compatibility_input",
        on_success=on_success,
        on_error=None,
        options={},
        condition=None,
        routes=None,
        fork_to=None,
        branches=branches,
        policy="all",
        merge="row_union",
    )


def _row_union_node(
    *,
    branches: dict[str, str],
    on_success: str = "unioned_rows",
) -> Any:
    from elspeth.web.composer.state import NodeSpec

    return NodeSpec(
        id="variant_union",
        node_type="row_union",
        plugin=None,
        input=next(iter(branches.values())),
        on_success=on_success,
        on_error=None,
        options={},
        condition=None,
        routes=None,
        fork_to=None,
        branches=branches,
        policy=None,
        merge=None,
    )


def _queue_fan_in_state(*, sources: dict[str, Any], extra_nodes: tuple[Any, ...] = ()) -> Any:
    """CompositionState: ``sources`` fan into queue ``inbound`` feeding an LLM."""
    from elspeth.web.composer.state import CompositionState, PipelineMetadata

    return CompositionState(
        sources=sources,
        nodes=(*extra_nodes, _queue_node(), _fanout_llm_node("inbound")),
        edges=(),
        outputs=(),
        metadata=PipelineMetadata(),
        version=1,
    )


class TestExecutionFanoutGuard:
    def test_source_native_llm_bounds_downstream_llm_transform_to_one_row(self, tmp_path: Path) -> None:
        """A source:llm may emit zero or one row, never unknown fanout."""
        from elspeth.web.composer.state import CompositionState, PipelineMetadata, SourceSpec
        from elspeth.web.execution.fanout_guard import evaluate_execution_fanout_guard

        state = CompositionState(
            source=SourceSpec(
                plugin="llm",
                on_success="generated",
                options={
                    "provider": "openrouter",
                    "model": "openai/gpt-4o-mini",
                    "prompt_template": "Write one briefing.",
                    "schema": {"mode": "observed"},
                },
                on_validation_failure="discard",
            ),
            nodes=(_fanout_llm_node("generated", node_id="refine"),),
            edges=(),
            outputs=(),
            metadata=PipelineMetadata(),
            version=1,
        )

        assert evaluate_execution_fanout_guard(state, data_dir=tmp_path) is None

    @pytest.mark.asyncio
    async def test_line_explode_to_llm_requires_ack_before_run_creation(
        self,
        service: ExecutionServiceImpl,
        mock_session_service: MagicMock,
        mock_settings: MagicMock,
        tmp_path: Path,
    ) -> None:
        """A deaggregation transform upstream of LLM must stop at launch."""
        from elspeth.web.execution.fanout_guard import ExecutionFanoutGuardRequired

        data_dir = tmp_path
        session_id = uuid4()
        blob_dir = data_dir / "blobs" / str(session_id)
        output_dir = data_dir / "outputs" / str(session_id)
        blob_dir.mkdir(parents=True)
        output_dir.mkdir(parents=True)
        source_path = blob_dir / "input.txt"
        source_path.write_text("alpha\nbeta\n", encoding="utf-8")
        mock_settings.data_dir = data_dir
        mock_session_service.get_current_state.return_value = _composition_state_record(
            session_id=session_id,
            source_path=source_path,
            output_path=output_dir / "out.jsonl",
            nodes=[
                {
                    "id": "explode_lines",
                    "node_type": "transform",
                    "plugin": "line_explode",
                    "input": "source_rows",
                    "on_success": "line_rows",
                    "on_error": "errors",
                    "options": {"source_field": "body", "schema": {"mode": "observed"}},
                },
                {
                    "id": "classify_line",
                    "node_type": "transform",
                    "plugin": "llm",
                    "input": "line_rows",
                    "on_success": "out",
                    "on_error": "errors",
                    "options": {
                        "provider": "openrouter",
                        "model": "openai/gpt-4o-mini",
                        "api_key": {"secret_ref": "OPENROUTER_API_KEY"},
                    },
                },
            ],
        )

        with (
            patch.object(service, "_run_pipeline"),
            patch("elspeth.web.execution.service.validate_semantic_contracts", return_value=((), (), ())),
            pytest.raises(ExecutionFanoutGuardRequired) as raised,
        ):
            await service.execute(session_id=session_id)

        guard = raised.value.guard
        assert guard.ack_token
        assert guard.risks[0].node_id == "classify_line"
        assert guard.risks[0].provider == "openrouter"
        assert guard.risks[0].model == "openai/gpt-4o-mini"
        assert guard.risks[0].credential_ref == "secret_ref:OPENROUTER_API_KEY"
        assert guard.risks[0].estimated_provider_calls is None
        assert mock_session_service.create_run.await_count == 0

    @pytest.mark.asyncio
    async def test_acknowledged_line_explode_to_llm_records_guard_in_run_yaml(
        self,
        service: ExecutionServiceImpl,
        mock_session_service: MagicMock,
        mock_settings: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Accepted fanout warnings are persisted with the run launch record."""
        from elspeth.web.execution.fanout_guard import ExecutionFanoutGuardRequired

        data_dir = tmp_path
        blob_dir = data_dir / "blobs"
        output_dir = data_dir / "outputs"
        blob_dir.mkdir()
        output_dir.mkdir()
        source_path = blob_dir / "input.txt"
        source_path.write_text("alpha\nbeta\n", encoding="utf-8")
        session_id = uuid4()
        mock_settings.data_dir = data_dir
        mock_session_service.get_current_state.return_value = _composition_state_record(
            session_id=session_id,
            source_path=source_path,
            output_path=output_dir / "out.jsonl",
            nodes=[
                {
                    "id": "explode_lines",
                    "node_type": "transform",
                    "plugin": "line_explode",
                    "input": "source_rows",
                    "on_success": "line_rows",
                    "on_error": "errors",
                    "options": {"source_field": "body", "schema": {"mode": "observed"}},
                },
                {
                    "id": "classify_line",
                    "node_type": "transform",
                    "plugin": "llm",
                    "input": "line_rows",
                    "on_success": "out",
                    "on_error": "errors",
                    "options": {
                        "provider": "openrouter",
                        "model": "openai/gpt-4o-mini",
                        "api_key": {"secret_ref": "OPENROUTER_API_KEY"},
                    },
                },
            ],
        )

        with (
            patch.object(service, "_run_pipeline"),
            patch("elspeth.web.execution.service.validate_semantic_contracts", return_value=((), (), ())),
            pytest.raises(ExecutionFanoutGuardRequired) as raised,
        ):
            await service.execute(session_id=session_id)

        run_id = uuid4()
        mock_session_service.create_run.return_value = _run_record_stub(id=run_id)
        # This test isolates fanout acknowledgement. Direct source ->
        # line_explode now correctly fails the independent UNKNOWN semantic
        # contract gate, so keep that sibling gate stubbed on the accepted
        # retry just as it is on the initial guard-producing call above.
        with patch("elspeth.web.execution.service.validate_semantic_contracts", return_value=((), (), ())):
            await service.execute(
                session_id=session_id,
                fanout_ack_token=raised.value.guard.ack_token,
            )

        create_call = mock_session_service.create_run.await_args_list[-1]
        persisted_yaml = create_call.kwargs["pipeline_yaml"]
        assert "elspeth_execution_fanout_guard" in persisted_yaml
        assert '"accepted":true' in persisted_yaml
        assert raised.value.guard.ack_token in persisted_yaml
        assert '"node_id":"classify_line"' in persisted_yaml

    @pytest.mark.asyncio
    async def test_direct_small_text_source_to_llm_executes_without_ack(
        self,
        service: ExecutionServiceImpl,
        mock_session_service: MagicMock,
        mock_settings: MagicMock,
        tmp_path: Path,
    ) -> None:
        """A direct low-cardinality source->LLM path remains a one-click run."""
        data_dir = tmp_path
        blob_dir = data_dir / "blobs"
        output_dir = data_dir / "outputs"
        blob_dir.mkdir()
        output_dir.mkdir()
        source_path = blob_dir / "input.txt"
        source_path.write_text("alpha\nbeta\n", encoding="utf-8")
        session_id = uuid4()
        mock_settings.data_dir = data_dir
        mock_session_service.get_current_state.return_value = _composition_state_record(
            session_id=session_id,
            source_path=source_path,
            output_path=output_dir / "out.jsonl",
            nodes=[
                {
                    "id": "classify_row",
                    "node_type": "transform",
                    "plugin": "llm",
                    "input": "source_rows",
                    "on_success": "out",
                    "on_error": "errors",
                    "options": {
                        "provider": "openrouter",
                        "model": "openai/gpt-4o-mini",
                        "api_key": {"secret_ref": "OPENROUTER_API_KEY"},
                    },
                },
            ],
        )

        with (
            patch.object(service, "_run_pipeline"),
            patch("elspeth.web.execution.service.validate_semantic_contracts", return_value=((), (), ())),
        ):
            run_id = await service.execute(session_id=session_id)

        assert isinstance(run_id, UUID)
        persisted_yaml = mock_session_service.create_run.await_args.kwargs["pipeline_yaml"]
        assert "elspeth_execution_fanout_guard" not in persisted_yaml

    @pytest.mark.asyncio
    async def test_non_first_named_source_to_llm_uses_its_own_cardinality(
        self,
        service: ExecutionServiceImpl,
        mock_session_service: MagicMock,
        mock_settings: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Named source fanout accounting must not inspect only the compatibility source."""
        from elspeth.web.execution.fanout_guard import ExecutionFanoutGuardRequired

        data_dir = tmp_path
        session_id = uuid4()
        blob_dir = data_dir / "blobs" / str(session_id)
        output_dir = data_dir / "outputs" / str(session_id)
        blob_dir.mkdir(parents=True)
        output_dir.mkdir(parents=True)
        orders_path = blob_dir / "orders.txt"
        refunds_path = blob_dir / "refunds.txt"
        orders_path.write_text("one\n", encoding="utf-8")
        refunds_path.write_text("\n".join(f"refund-{i}" for i in range(101)) + "\n", encoding="utf-8")
        mock_settings.data_dir = data_dir
        mock_session_service.get_current_state.return_value = _composition_state_record(
            session_id=session_id,
            source_path=orders_path,
            output_path=output_dir / "out.jsonl",
            sources={
                "orders": {
                    "plugin": "text",
                    "on_success": "orders_rows",
                    "on_validation_failure": "discard",
                    "options": {"path": str(orders_path), "column": "body", "schema": {"mode": "observed"}},
                },
                "refunds": {
                    "plugin": "text",
                    "on_success": "refunds_rows",
                    "on_validation_failure": "discard",
                    "options": {"path": str(refunds_path), "column": "body", "schema": {"mode": "observed"}},
                },
            },
            nodes=[
                {
                    "id": "classify_refund",
                    "node_type": "transform",
                    "plugin": "llm",
                    "input": "refunds_rows",
                    "on_success": "out",
                    "on_error": "errors",
                    "options": {
                        "provider": "openrouter",
                        "model": "openai/gpt-4o-mini",
                        "api_key": {"secret_ref": "OPENROUTER_API_KEY"},
                    },
                },
            ],
        )

        with (
            patch.object(service, "_run_pipeline"),
            patch("elspeth.web.execution.service.validate_semantic_contracts", return_value=((), (), ())),
            pytest.raises(ExecutionFanoutGuardRequired) as raised,
        ):
            await service.execute(session_id=session_id)

        risk = raised.value.guard.risks[0]
        assert risk.estimated_provider_calls == 101
        assert risk.upstream_fanout == ("source:refunds:text:estimated_rows=101",)
        assert mock_session_service.create_run.await_count == 0

    def test_coalesce_mapping_traverses_values_once_across_nesting_and_cycles(self, tmp_path: Path) -> None:
        """Mapping values remain reachable and duplicate/cyclic paths are visited once."""
        from elspeth.web.composer.state import CompositionState, PipelineMetadata
        from elspeth.web.execution.fanout_guard import evaluate_execution_fanout_guard

        rows = tmp_path / "rows.txt"
        rows.write_text("\n".join(f"row-{i}" for i in range(101)) + "\n", encoding="utf-8")
        inner = _coalesce_node(
            node_id="inner",
            on_success="inner_rows",
            branches={
                "primary_branch": "actual_source_rows",
                "duplicate_branch": "actual_source_rows",
                "cycle_branch": "outer_rows",
            },
        )
        outer = _coalesce_node(
            node_id="outer",
            on_success="outer_rows",
            branches={"nested_branch": "inner_rows"},
        )
        state = CompositionState(
            sources={"rows": _text_source(rows, "actual_source_rows")},
            nodes=(inner, outer, _fanout_llm_node("outer_rows")),
            edges=(),
            outputs=(),
            metadata=PipelineMetadata(),
            version=1,
        )

        guard = evaluate_execution_fanout_guard(state, data_dir=tmp_path)

        assert guard is not None, "101 reachable rows must require a guard"
        risk = guard.risks[0]
        assert risk.estimated_provider_calls == 101
        assert risk.upstream_fanout == ("source:rows:text:estimated_rows=101",)

    def test_row_union_traverses_every_branch_for_provider_cost(self, tmp_path: Path) -> None:
        """A downstream LLM sees the summed cardinality of every released branch."""
        from elspeth.web.composer.state import CompositionState, PipelineMetadata
        from elspeth.web.execution.fanout_guard import evaluate_execution_fanout_guard

        control = tmp_path / "control.txt"
        treatment = tmp_path / "treatment.txt"
        control.write_text("\n".join(f"control-{i}" for i in range(60)) + "\n", encoding="utf-8")
        treatment.write_text("\n".join(f"treatment-{i}" for i in range(60)) + "\n", encoding="utf-8")
        state = CompositionState(
            sources={
                "control": _text_source(control, "control_done"),
                "treatment": _text_source(treatment, "treatment_done"),
            },
            nodes=(
                _row_union_node(
                    branches={
                        "control": "control_done",
                        "treatment": "treatment_done",
                    }
                ),
                _fanout_llm_node("unioned_rows"),
            ),
            edges=(),
            outputs=(),
            metadata=PipelineMetadata(),
            version=1,
        )

        guard = evaluate_execution_fanout_guard(state, data_dir=tmp_path)

        assert guard is not None
        assert guard.risks[0].estimated_provider_calls == 120
        assert set(guard.risks[0].upstream_fanout) == {
            "source:control:text:estimated_rows=60",
            "source:treatment:text:estimated_rows=60",
        }

    # ── Queue fan-in provider-cost guard (elspeth-6421ffa028) ───────────
    # A declared queue fans multiple upstream sources into one LLM. The
    # cost guard MUST traverse EVERY predecessor and combine cardinalities
    # conservatively — never resolve to whichever source registered last.

    def test_queue_fan_in_combines_known_source_cardinalities(self, tmp_path: Path) -> None:
        """Two known source predecessors of a queue are summed, not last-writer-wins."""
        from elspeth.web.execution.fanout_guard import evaluate_execution_fanout_guard

        orders = tmp_path / "orders.txt"
        refunds = tmp_path / "refunds.txt"
        orders.write_text("\n".join(f"order-{i}" for i in range(60)) + "\n", encoding="utf-8")
        refunds.write_text("\n".join(f"refund-{i}" for i in range(60)) + "\n", encoding="utf-8")
        state = _queue_fan_in_state(
            sources={
                "orders": _text_source(orders, "inbound"),
                "refunds": _text_source(refunds, "inbound"),
            }
        )

        guard = evaluate_execution_fanout_guard(state, data_dir=tmp_path)

        assert guard is not None, "60 + 60 = 120 > threshold must require a guard"
        risk = guard.risks[0]
        assert risk.node_id == "classify"
        # Summed conservatively across both predecessors, not a single source's 60.
        assert risk.estimated_provider_calls == 120
        assert set(risk.upstream_fanout) == {
            "source:orders:text:estimated_rows=60",
            "source:refunds:text:estimated_rows=60",
        }

    def test_queue_fan_in_one_unknown_predecessor_guards_with_unknown_calls(self, tmp_path: Path) -> None:
        """Any unknown-cardinality predecessor keeps the sum None and risk HIGH."""
        from elspeth.web.composer.state import SourceSpec
        from elspeth.web.execution.fanout_guard import evaluate_execution_fanout_guard

        orders = tmp_path / "orders.txt"
        orders.write_text("\n".join(f"order-{i}" for i in range(5)) + "\n", encoding="utf-8")
        # A csv source with no path/limit has an unknowable cardinality.
        unknown = SourceSpec(
            plugin="csv",
            on_success="inbound",
            options={"schema": {"mode": "observed"}},
            on_validation_failure="discard",
        )
        state = _queue_fan_in_state(sources={"orders": _text_source(orders, "inbound"), "refunds": unknown})

        guard = evaluate_execution_fanout_guard(state, data_dir=tmp_path)

        assert guard is not None, "an unknown predecessor must never silently return no guard"
        risk = guard.risks[0]
        assert risk.estimated_provider_calls is None
        assert risk.risk_level == "high"
        assert "source:refunds:csv:estimated_rows=unknown" in risk.upstream_fanout

    def test_queue_fan_in_token_creating_predecessor_stays_visible(self, tmp_path: Path) -> None:
        """A token-creating transform feeding the queue is not lost behind the queue."""
        from elspeth.web.composer.state import NodeSpec
        from elspeth.web.execution.fanout_guard import evaluate_execution_fanout_guard

        orders = tmp_path / "orders.txt"
        refunds = tmp_path / "refunds.txt"
        orders.write_text("alpha\nbeta\n", encoding="utf-8")
        refunds.write_text("gamma\ndelta\n", encoding="utf-8")
        explode = NodeSpec(
            id="explode_lines",
            node_type="transform",
            plugin="line_explode",
            input="orders_rows",
            on_success="inbound",
            on_error="errors",
            options={"source_field": "body", "schema": {"mode": "observed"}},
            condition=None,
            routes=None,
            fork_to=None,
            branches=None,
            policy=None,
            merge=None,
        )
        state = _queue_fan_in_state(
            sources={
                "orders": _text_source(orders, "orders_rows"),
                "refunds": _text_source(refunds, "inbound"),
            },
            extra_nodes=(explode,),
        )

        guard = evaluate_execution_fanout_guard(state, data_dir=tmp_path)

        assert guard is not None
        risk = guard.risks[0]
        # Token-creating fanout upstream of a queue is unbounded, not summable.
        assert risk.estimated_provider_calls is None
        assert any(marker.startswith("transform:explode_lines:line_explode") for marker in risk.upstream_fanout)

    def test_queue_fan_in_guard_is_source_order_invariant(self, tmp_path: Path) -> None:
        """Reversing predecessor order yields an identical guard decision."""
        from elspeth.web.execution.fanout_guard import evaluate_execution_fanout_guard

        orders = tmp_path / "orders.txt"
        refunds = tmp_path / "refunds.txt"
        orders.write_text("\n".join(f"order-{i}" for i in range(60)) + "\n", encoding="utf-8")
        refunds.write_text("\n".join(f"refund-{i}" for i in range(60)) + "\n", encoding="utf-8")
        forward = _queue_fan_in_state(
            sources={
                "orders": _text_source(orders, "inbound"),
                "refunds": _text_source(refunds, "inbound"),
            }
        )
        reverse = _queue_fan_in_state(
            sources={
                "refunds": _text_source(refunds, "inbound"),
                "orders": _text_source(orders, "inbound"),
            }
        )

        guard_forward = evaluate_execution_fanout_guard(forward, data_dir=tmp_path)
        guard_reverse = evaluate_execution_fanout_guard(reverse, data_dir=tmp_path)

        assert guard_forward is not None and guard_reverse is not None
        assert guard_forward.risks[0].estimated_provider_calls == 120
        assert guard_reverse.risks[0].estimated_provider_calls == 120
        assert sorted(guard_forward.risks[0].upstream_fanout) == sorted(guard_reverse.risks[0].upstream_fanout)


class TestWebRuntimeInfrastructure:
    """Regression coverage for web execution's orchestrator runtime wiring."""

    def test_raw_pipeline_and_export_eligibility_precede_secret_resolution_without_shape_skip(
        self,
        service: ExecutionServiceImpl,
    ) -> None:
        from elspeth.engine.orchestrator.preflight import SinkEffectCapabilityError, SinkEffectExecutionPurpose

        pipeline_yaml = """
sinks:
  pipeline:
    plugin: json
    options: {}
  audit:
    plugin: json
    options: {}
landscape:
  export:
    enabled: true
    sink: audit
    total_record_limit: 10
    total_byte_limit: 1000
    chunk_limit: 2
    per_chunk_record_limit: 5
    per_chunk_byte_limit: 500
    spool_root: .elspeth/audit-export-spool/web-test
    content_store:
      content_store_id: web-test
      namespace: web-test
      root: .elspeth/audit-export-content-store/web-test
      policy_version: v1
      retention_days: 30
      durability: fsync
"""
        secret_service = MagicMock(spec=["list_refs"])
        secret_service.list_refs.return_value = []
        service._secret_service = secret_service
        purposes: list[SinkEffectExecutionPurpose] = []

        def validate(_raw: object, *, purpose: SinkEffectExecutionPurpose) -> dict[str, object]:
            purposes.append(purpose)
            if purpose is SinkEffectExecutionPurpose.AUDIT_EXPORT:
                raise SinkEffectCapabilityError("export lane rejected")
            return {}

        with (
            patch(
                "elspeth.web.execution.service.validate_sink_effect_eligibility_from_raw_config",
                side_effect=validate,
            ),
            patch("elspeth.core.secrets.resolve_secret_refs") as resolve_secret_refs,
            pytest.raises(SinkEffectCapabilityError, match="export lane"),
        ):
            service._run_pipeline(str(uuid4()), pipeline_yaml, threading.Event(), user_id="alice")

        assert purposes == [SinkEffectExecutionPurpose.FRESH, SinkEffectExecutionPurpose.AUDIT_EXPORT]
        secret_service.list_refs.assert_not_called()
        resolve_secret_refs.assert_not_called()

    def test_sink_effect_rejection_precedes_status_landscape_and_payload_resources(
        self,
        service: ExecutionServiceImpl,
        mock_session_service: MagicMock,
    ) -> None:
        from elspeth.engine.orchestrator.preflight import SinkEffectCapabilityError

        pipeline_yaml = """
sinks:
  output:
    plugin: legacy
    on_write_failure: discard
    options: {}
"""
        mock_session_service.update_run_status.reset_mock()
        manager = MagicMock(spec=["get_sink_by_name"])
        manager.get_sink_by_name.return_value = _LegacySinkStub
        with (
            patch("elspeth.plugins.infrastructure.manager.get_shared_plugin_manager", return_value=manager),
            patch("elspeth.web.execution.service.open_landscape_db") as open_database,
            patch("elspeth.web.execution.service.FilesystemPayloadStore") as make_payload_store,
            pytest.raises(SinkEffectCapabilityError, match="effect protocol"),
        ):
            service._run_pipeline(str(uuid4()), pipeline_yaml, threading.Event())

        mock_session_service.update_run_status.assert_not_called()
        open_database.assert_not_called()
        make_payload_store.assert_not_called()

    def test_run_pipeline_records_web_user_attribution_in_landscape(
        self,
        service: ExecutionServiceImpl,
        mock_session_service: MagicMock,
        mock_settings: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Web execution must persist who initiated the Landscape run."""
        source_path = tmp_path / "input.txt"
        source_path.write_text("alpha\n", encoding="utf-8")
        output_path = tmp_path / "out.jsonl"
        run_id = str(uuid4())
        mock_settings.landscape_url = f"sqlite:///{tmp_path / 'audit.db'}"
        mock_settings.payload_store_path = tmp_path / "payloads"

        pipeline_yaml = f"""
sources:
  primary:
    plugin: text
    on_success: output
    options:
      path: {source_path}
      column: value
      on_validation_failure: discard
      schema:
        mode: fixed
        fields:
        - "value: str"
sinks:
  output:
    plugin: json
    on_write_failure: discard
    options:
      path: {output_path}
      format: jsonl
      mode: write
      schema:
        mode: observed
"""

        service._run_pipeline(
            run_id,
            pipeline_yaml,
            threading.Event(),
            user_id="alice",
            auth_provider_type="local",
        )

        db = LandscapeDB.from_url(mock_settings.landscape_url, create_tables=False)
        try:
            with db.read_only_connection() as conn:
                attribution_row = conn.execute(select(run_attributions_table).where(run_attributions_table.c.run_id == run_id)).one()
                run_row = conn.execute(select(runs_table.c.settings_json).where(runs_table.c.run_id == run_id)).one()
        finally:
            db.close()

        settings_json = json.loads(run_row.settings_json)
        assert settings_json["sources"]["primary"]["plugin"] == "text"
        assert settings_json["sinks"]["output"]["plugin"] == "json"
        assert attribution_row.initiated_by_user_id == "alice"
        assert attribution_row.auth_provider_type == "local"
        assert output_path.exists()

    def test_aws_web_run_persists_effective_operator_telemetry_before_events(
        self,
        service: ExecutionServiceImpl,
        mock_settings: MagicMock,
        tmp_path: Path,
    ) -> None:
        source_path = tmp_path / "input.txt"
        source_path.write_text("alpha\n", encoding="utf-8")
        output_path = tmp_path / "out.jsonl"
        run_id = str(uuid4())

        def _external_session_db_url() -> str:
            return "postgresql+psycopg://db.invalid/elspeth_sessions"

        mock_settings.deployment_target = "aws-ecs"
        mock_settings.deployment_state_mode = "external-postgresql"
        mock_settings.get_session_db_url = _external_session_db_url
        mock_settings.landscape_url = "postgresql+psycopg2://db.invalid/elspeth_landscape"
        assert resolve_deployment_state_mode(mock_settings) == "external-postgresql"
        mock_settings.operator_telemetry_service_name = "elspeth-web-test"
        mock_settings.operator_telemetry_environment = "test"
        mock_settings.operator_telemetry_release = "git-test"
        mock_settings.operator_telemetry_ecs_cluster = "elspeth-test"
        mock_settings.operator_telemetry_ecs_service = "elspeth-web"
        mock_settings.operator_telemetry_task_definition_family = "elspeth-web-task"
        mock_settings.operator_telemetry_task_definition_revision = "1"
        mock_settings.operator_pipeline_telemetry_granularity = "lifecycle"
        test_landscape_url = f"sqlite:///{tmp_path / 'audit.db'}"
        mock_settings.payload_store_path = tmp_path / "payloads"
        initialized_db = LandscapeDB.from_url(test_landscape_url)
        initialized_db.close()
        pipeline_yaml = f"""
sources:
  primary:
    plugin: text
    on_success: output
    options:
      path: {source_path}
      column: value
      on_validation_failure: discard
      schema:
        mode: fixed
        fields:
        - "value: str"
sinks:
  output:
    plugin: json
    on_write_failure: discard
    options:
      path: {output_path}
      format: jsonl
      mode: write
      schema:
        mode: observed
telemetry:
  enabled: true
  granularity: full
  fail_on_total_exporter_failure: true
  exporters:
  - name: otlp
    options:
      endpoint: https://authored.invalid:4317
      headers:
        authorization: authored-secret
"""

        # This integration test exercises Landscape persistence and ordering;
        # transport delivery itself is covered by test_operator_telemetry.py.
        telemetry_manager = create_autospec(TelemetryManager, instance=True)
        telemetry_manager.health_metrics = {"events_dropped": 3, "queue_drops": 1}

        def _open_test_landscape_db(settings: _WebSettingsStub) -> LandscapeDB:
            assert settings is mock_settings
            assert resolve_deployment_state_mode(settings) == "external-postgresql"
            return LandscapeDB.from_url(test_landscape_url, create_tables=False)

        with (
            patch(
                "elspeth.web.execution.service.open_landscape_db",
                side_effect=_open_test_landscape_db,
            ),
            patch("elspeth.telemetry.create_telemetry_manager", return_value=telemetry_manager),
            patch("elspeth.web.operator_telemetry.record_operator_pipeline_queue_drops") as record_queue_drops,
        ):
            service._run_pipeline(run_id, pipeline_yaml, threading.Event())

        telemetry_manager.close.assert_called_once_with()
        record_queue_drops.assert_called_once_with(1)

        db = LandscapeDB.from_url(test_landscape_url, create_tables=False)
        try:
            with db.read_only_connection() as conn:
                row = conn.execute(select(runs_table.c.settings_json, runs_table.c.config_hash).where(runs_table.c.run_id == run_id)).one()
        finally:
            db.close()

        persisted = json.loads(row.settings_json)
        telemetry = persisted["telemetry"]
        assert telemetry["enabled"] is True
        assert telemetry["granularity"] == "lifecycle"
        assert telemetry["fail_on_total_exporter_failure"] is False
        assert telemetry["exporters"] == [
            {
                "name": "otlp",
                "options": {
                    "batch_size": 100,
                    "cloud_provider": "aws",
                    "deployment_environment": "test",
                    "endpoint": "http://127.0.0.1:4317",
                    "headers": {},
                    "aws_ecs_cluster_name": "elspeth-test",
                    "aws_ecs_service_name": "elspeth-web",
                    "aws_ecs_task_family": "elspeth-web-task",
                    "aws_ecs_task_revision": "1",
                    "service_name": "elspeth-web-test",
                    "service_version": "git-test",
                },
            }
        ]
        assert row.config_hash == stable_hash(persisted)
        assert "authored.invalid" not in row.settings_json
        assert "authored-secret" not in row.settings_json

    def test_web_scrape_pipeline_receives_rate_limit_registry(
        self,
        service: ExecutionServiceImpl,
        mock_session_service: MagicMock,
        mock_settings: MagicMock,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Web execution must provide runtime infrastructure required by external-call transforms."""
        import socket
        from datetime import UTC, datetime

        import httpx

        from elspeth.contracts import CallStatus, CallType
        from elspeth.contracts.audit import Call
        from elspeth.contracts.contexts import TransformContext
        from elspeth.core.security.web import SSRFSafeRequest
        from elspeth.plugins.transforms.web_scrape import WebScrapeTransform

        source_path = tmp_path / "input.txt"
        source_path.write_text("https://example.com/page\n", encoding="utf-8")
        output_path = tmp_path / "out.jsonl"
        mock_settings.landscape_url = f"sqlite:///{tmp_path / 'audit.db'}"
        mock_settings.payload_store_path = tmp_path / "payloads"

        def fake_getaddrinfo(
            host: str,
            port: object,
            family: int = 0,
            type: int = 0,
            proto: int = 0,
            flags: int = 0,
        ) -> list[tuple[object, ...]]:
            assert host == "example.com"
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]

        def fake_fetch_url(
            self: WebScrapeTransform,
            safe_request: SSRFSafeRequest,
            ctx: TransformContext,
        ) -> tuple[httpx.Response, str, Call]:
            del self
            return (
                httpx.Response(
                    200,
                    text="<html><body><h1>ok</h1></body></html>",
                    request=httpx.Request("GET", safe_request.connection_url),
                ),
                safe_request.original_url,
                Call(
                    call_id="call-web-runtime",
                    call_index=0,
                    call_type=CallType.HTTP,
                    status=CallStatus.SUCCESS,
                    request_hash="request-hash",
                    created_at=datetime.now(UTC),
                    state_id=ctx.state_id or "state-web-runtime",
                    request_ref="request-ref",
                    response_hash="response-hash",
                    response_ref="response-ref",
                    latency_ms=1.0,
                ),
            )

        monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
        monkeypatch.setattr(WebScrapeTransform, "_fetch_url", fake_fetch_url)

        pipeline_yaml = f"""
sources:
  primary:
    plugin: text
    on_success: scrape_in
    options:
      path: {source_path}
      column: url
      on_validation_failure: discard
      schema:
        mode: fixed
        fields:
        - "url: str"
transforms:
- name: scrape_page
  plugin: web_scrape
  input: scrape_in
  on_success: scraped
  on_error: errors
  options:
    schema:
      mode: flexible
      fields:
      - "url: str"
    required_input_fields:
    - url
    url_field: url
    content_field: html
    fingerprint_field: html_fingerprint
    format: raw
    fingerprint_mode: content
    strip_elements: []
    http:
      abuse_contact: tests@example.com
      scraping_reason: test runtime wiring
      timeout: 30
      allowed_hosts: public_only
sinks:
  scraped:
    plugin: json
    on_write_failure: discard
    options:
      path: {output_path}
      format: jsonl
      mode: write
      schema:
        mode: observed
  errors:
    plugin: json
    on_write_failure: discard
    options:
      path: {tmp_path / "errors.jsonl"}
      format: jsonl
      mode: write
      schema:
        mode: observed
"""

        service._run_pipeline(str(uuid4()), pipeline_yaml, threading.Event())

        completed_calls = [
            call for call in mock_session_service.update_run_status.await_args_list if call.kwargs.get("status") == "completed"
        ]
        assert completed_calls
        assert output_path.exists()


# ── B2: shutdown_event Always Passed ───────────────────────────────────


@pytest.mark.usefixtures("mock_pipeline_config_assembly")
class TestB2ShutdownEvent:
    """B2 fix: _run_pipeline() MUST pass shutdown_event to orchestrator.run().

    If shutdown_event is omitted, the Orchestrator calls signal.signal()
    from the worker thread, raising ValueError: signal only works in main thread.
    """

    @patch("elspeth.web.execution.service.Orchestrator")
    @patch("elspeth.web.execution.service.load_settings_from_yaml_string")
    @patch("elspeth.web.execution.preflight.instantiate_plugins_from_config")
    @patch("elspeth.web.execution.preflight.ExecutionGraph")
    @patch("elspeth.web.execution.service.open_landscape_db")
    @patch("elspeth.web.execution.service.FilesystemPayloadStore")
    def test_shutdown_event_passed_to_orchestrator_run(
        self,
        mock_payload: MagicMock,
        mock_landscape: MagicMock,
        mock_graph_cls: MagicMock,
        mock_instantiate: MagicMock,
        mock_load: MagicMock,
        mock_orch_cls: MagicMock,
        service: ExecutionServiceImpl,
        mock_session_service: MagicMock,
    ) -> None:
        mock_load.return_value = _mock_pipeline_settings()
        mock_bundle = _plugin_bundle_stub()
        mock_instantiate.return_value = mock_bundle
        mock_graph = _execution_graph_stub()
        mock_graph_cls.from_plugin_instances.return_value = mock_graph
        shutdown_event = threading.Event()
        run_id = uuid4()

        mock_orch = _orchestrator_stub(_orchestrator_result_stub(run_id=str(run_id)))
        mock_orch_cls.return_value = mock_orch

        with patch(
            "elspeth.web.execution.service.load_run_accounting_from_db",
            return_value=_run_accounting_for_status(RunStatus.COMPLETED),
        ):
            service._run_pipeline(str(run_id), "source:\n  plugin: csv", shutdown_event)

        # B2 invariant: shutdown_event was passed
        orch_run_call = mock_orch.run.call_args
        assert orch_run_call[1].get("shutdown_event") is shutdown_event, (
            "B2 VIOLATION: shutdown_event not passed to orchestrator.run(). This will cause ValueError: signal only works in main thread."
        )
        assert orch_run_call[1].get("run_id") == str(run_id), (
            "Run diagnostics require the web run UUID to be the Landscape run_id while the run is still active."
        )
        evidence = orch_run_call[1].get("web_plugin_policy_evidence")
        assert isinstance(evidence, WebPluginPolicyEvidence)
        assert evidence.snapshot_hash
        assert evidence.decision_codes == ("policy_allowed",)
        assert "audit_export_content_store" in orch_run_call.kwargs
        assert "audit_export_content_store_resolver" in orch_run_call.kwargs

        running_calls = [call for call in mock_session_service.update_run_status.await_args_list if call.kwargs.get("status") == "running"]
        assert running_calls
        assert running_calls[0].kwargs.get("landscape_run_id") == str(run_id)

    @patch("elspeth.web.execution.service.Orchestrator")
    @patch("elspeth.web.execution.service.load_settings_from_yaml_string")
    @patch("elspeth.web.execution.preflight.instantiate_plugins_from_config")
    @patch("elspeth.web.execution.preflight.ExecutionGraph")
    @patch("elspeth.web.execution.service.open_landscape_db")
    @patch("elspeth.web.execution.service.FilesystemPayloadStore")
    def test_enabled_web_export_constructs_and_threads_production_store(
        self,
        mock_payload: MagicMock,
        mock_landscape: MagicMock,
        mock_graph_cls: MagicMock,
        mock_instantiate: MagicMock,
        mock_load: MagicMock,
        mock_orch_cls: MagicMock,
        service: ExecutionServiceImpl,
    ) -> None:
        del mock_payload, mock_landscape
        settings = _mock_pipeline_settings()
        export_settings = SimpleNamespace(enabled=True, sink="primary")
        settings.landscape.export = export_settings
        mock_load.return_value = settings
        mock_instantiate.return_value = _plugin_bundle_stub()
        mock_graph_cls.from_plugin_instances.return_value = _execution_graph_stub()
        run_id = str(uuid4())
        mock_orch = _orchestrator_stub(_orchestrator_result_stub(run_id=run_id))
        mock_orch_cls.return_value = mock_orch
        store = object()
        resolver = object()

        with (
            patch(
                "elspeth.core.audit_export_content_store.create_audit_export_content_store",
                return_value=(store, resolver),
            ) as create_store,
            patch(
                "elspeth.web.execution.service.load_run_accounting_from_db",
                return_value=_run_accounting_for_status(RunStatus.COMPLETED),
            ),
        ):
            service._run_pipeline(run_id, "source:\n  plugin: csv", threading.Event())

        create_store.assert_called_once_with(export_settings)
        assert mock_orch.run.call_args.kwargs["audit_export_content_store"] is store
        assert mock_orch.run.call_args.kwargs["audit_export_content_store_resolver"] is resolver


# ── B3: LandscapeDB and PayloadStore Construction ─────────────────────


@pytest.mark.usefixtures("mock_pipeline_config_assembly")
class TestB3Construction:
    """B3 fix: Construct LandscapeDB and FilesystemPayloadStore from WebSettings.

    _run_pipeline() does NOT use hardcoded paths. It calls
    self._settings.get_landscape_url() and self._settings.get_payload_store_path().
    """

    @patch("elspeth.web.execution.service.Orchestrator")
    @patch("elspeth.web.execution.service.load_settings_from_yaml_string")
    @patch("elspeth.web.execution.preflight.instantiate_plugins_from_config")
    @patch("elspeth.web.execution.preflight.ExecutionGraph")
    @patch("elspeth.web.execution.service.open_landscape_db")
    @patch("elspeth.web.execution.service.FilesystemPayloadStore")
    def test_landscape_db_constructed_from_settings(
        self,
        mock_payload_cls: MagicMock,
        mock_open_landscape: MagicMock,
        mock_graph_cls: MagicMock,
        mock_instantiate: MagicMock,
        mock_load: MagicMock,
        mock_orch_cls: MagicMock,
        service: ExecutionServiceImpl,
        mock_settings: MagicMock,
    ) -> None:
        _configure_runtime_success(
            mock_load=mock_load,
            mock_instantiate=mock_instantiate,
            mock_graph_cls=mock_graph_cls,
            mock_orch_cls=mock_orch_cls,
        )

        with patch(
            "elspeth.web.execution.service.load_run_accounting_from_db",
            return_value=_run_accounting_for_status(RunStatus.COMPLETED),
        ):
            service._run_pipeline(str(uuid4()), _TEST_PIPELINE_YAML, threading.Event())

        # B3: LandscapeDB opened through the deployment-gated factory.
        mock_open_landscape.assert_called_once_with(service._settings)
        # B3: PayloadStore constructed from settings path
        mock_payload_cls.assert_called_once_with(base_path=Path("/tmp/test_payloads"))

    @patch("elspeth.web.execution.service.Orchestrator")
    @patch("elspeth.web.execution.service.load_settings_from_yaml_string")
    @patch("elspeth.web.execution.preflight.instantiate_plugins_from_config")
    @patch("elspeth.web.execution.preflight.ExecutionGraph")
    @patch("elspeth.web.execution.service.open_landscape_db")
    @patch("elspeth.web.execution.service.FilesystemPayloadStore")
    def test_rate_limit_config_uses_web_data_dir(
        self,
        mock_payload_cls: MagicMock,
        mock_landscape_cls: MagicMock,
        mock_graph_cls: MagicMock,
        mock_instantiate: MagicMock,
        mock_load: MagicMock,
        mock_orch_cls: MagicMock,
        service: ExecutionServiceImpl,
        mock_settings: _WebSettingsStub,
    ) -> None:
        """Rate-limit persistence must be confined to the web app state root."""
        _configure_runtime_success(
            mock_load=mock_load,
            mock_instantiate=mock_instantiate,
            mock_graph_cls=mock_graph_cls,
            mock_orch_cls=mock_orch_cls,
        )
        mock_settings.data_dir = Path("/tmp/custom-web-state")

        with (
            patch(
                "elspeth.contracts.config.runtime.RuntimeRateLimitConfig.from_settings", return_value=SimpleNamespace()
            ) as mock_from_settings,
            patch("elspeth.core.rate_limit.RateLimitRegistry", return_value=SimpleNamespace(close=lambda: None)),
            patch(
                "elspeth.web.execution.service.load_run_accounting_from_db",
                return_value=_run_accounting_for_status(RunStatus.COMPLETED),
            ),
        ):
            service._run_pipeline(str(uuid4()), _TEST_PIPELINE_YAML, threading.Event())

        mock_from_settings.assert_called_once_with(mock_load.return_value.rate_limit, state_dir=Path("/tmp/custom-web-state"))


@pytest.mark.usefixtures("mock_pipeline_config_assembly")
class TestInlineBlobRuntimePreflight:
    """Inline-content blob refs resolve before plugin construction.

    Bug verification: remove the ``record_blob_inline_resolutions`` call
    from ``ExecutionServiceImpl._run_pipeline`` and this class loses the
    audit-before-settings invariant.
    """

    @patch("elspeth.web.execution.service.Orchestrator")
    @patch("elspeth.web.execution.service.build_validated_runtime_graph")
    @patch("elspeth.web.execution.service.load_settings_from_config_dict")
    @patch("elspeth.web.execution.service.open_landscape_db")
    @patch("elspeth.web.execution.service.FilesystemPayloadStore")
    def test_run_pipeline_resolves_inline_content_and_records_audit_before_settings_load(
        self,
        mock_payload_cls: MagicMock,
        mock_landscape_cls: MagicMock,
        mock_load: MagicMock,
        mock_runtime_graph: MagicMock,
        mock_orch_cls: MagicMock,
        service: ExecutionServiceImpl,
        mock_session_service: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Blob bytes are attacker-controllable. A ${VAR} smuggled inside them is
        # invisible to preflight (the blob is an opaque ref at validation time),
        # so it must NOT be expanded against the host environment at execution.
        monkeypatch.setenv("ELSPETH_INLINE_BLOB_SECRET", "server-secret-value")
        content = b"You are an audited prompt with literal ${ELSPETH_INLINE_BLOB_SECRET}."
        blob_id = uuid4()
        run_id = uuid4()
        owner_session = uuid4()
        sha256 = hashlib.sha256(content).hexdigest()
        order: list[str] = []
        # The valid same-session path: run and blob share an owning session, so
        # the cross-session guard added for elspeth-195ecb1d58 passes through.
        mock_session_service.get_run.return_value = _run_record_stub(status="running", session_id=owner_session)
        blob_record = _blob_record_stub(
            blob_id=blob_id,
            session_id=owner_session,
            content_hash=sha256,
            size_bytes=len(content),
        )

        async def link_blob_to_run(*_args: Any, **_kwargs: Any) -> None:
            order.append("link")

        async def read_blob_content(_blob_id: UUID) -> bytes:
            order.append("read")
            return content

        async def get_blob(_blob_id: UUID) -> Any:
            order.append("metadata")
            return blob_record

        async def record_blob_inline_resolutions(*_args: Any, **_kwargs: Any) -> None:
            order.append("record")

        blob_service = _blob_service_stub()
        blob_service.link_blob_to_run.side_effect = link_blob_to_run
        blob_service.read_blob_content.side_effect = read_blob_content
        blob_service.get_blob.side_effect = get_blob
        blob_service.finalize_run_output_blobs.return_value = BlobFinalizationResult(finalized=(), errors=())
        cast(Any, service)._blob_service = blob_service
        mock_session_service.record_blob_inline_resolutions.side_effect = record_blob_inline_resolutions

        def load_settings(config_dict: dict[str, Any], *, expand_env_vars: bool = True) -> SimpleNamespace:
            assert "record" in order, "audit row must be recorded before settings/plugin construction"
            # Inline blobs were substituted -> env expansion must be disabled so
            # the smuggled ${VAR} stays literal and the host secret never resolves.
            assert expand_env_vars is False
            system_prompt = config_dict["transforms"][0]["options"]["system_prompt"]
            assert system_prompt == "You are an audited prompt with literal ${ELSPETH_INLINE_BLOB_SECRET}."
            assert "server-secret-value" not in system_prompt
            order.append("load")
            return _mock_pipeline_settings()

        mock_load.side_effect = load_settings

        mock_runtime_graph.return_value = SimpleNamespace(
            plugin_bundle=_plugin_bundle_stub(),
            graph=_execution_graph_stub(),
        )

        mock_orch = _orchestrator_stub(
            _orchestrator_result_stub(
                run_id=str(run_id),
                rows_processed=1,
                rows_succeeded=1,
            )
        )
        mock_orch_cls.return_value = mock_orch

        pipeline_yaml = f"""
source:
  plugin: csv
  options:
    path: input.csv
transforms:
  - name: classify
    plugin: llm
    options:
      system_prompt:
        blob_ref: {blob_id}
        mode: inline_content
        sha256: {sha256}
sinks:
  primary:
    plugin: json
    options:
      path: output.jsonl
"""

        with patch(
            "elspeth.web.execution.service.load_run_accounting_from_db",
            return_value=_run_accounting_for_status(RunStatus.COMPLETED),
        ):
            service._run_pipeline(str(run_id), pipeline_yaml, threading.Event())

        assert order.index("link") < order.index("read")
        assert order.index("metadata") < order.index("record") < order.index("load")
        blob_service.link_blob_to_run.assert_awaited_once_with(blob_id=blob_id, run_id=run_id, direction="input")
        blob_service.read_blob_content.assert_awaited_once_with(blob_id)
        mock_session_service.record_blob_inline_resolutions.assert_awaited_once()
        resolutions = mock_session_service.record_blob_inline_resolutions.await_args.kwargs["resolutions"]
        assert len(resolutions) == 1
        assert resolutions[0].field_path == "node:classify.options.system_prompt"
        assert resolutions[0].content_hash == sha256

    @patch("elspeth.web.execution.service.Orchestrator")
    @patch("elspeth.web.execution.service.build_validated_runtime_graph")
    @patch("elspeth.web.execution.service.load_settings_from_yaml_string")
    @patch("elspeth.web.execution.service.open_landscape_db")
    @patch("elspeth.web.execution.service.FilesystemPayloadStore")
    def test_audit_write_failure_prevents_settings_load(
        self,
        mock_payload_cls: MagicMock,
        mock_landscape_cls: MagicMock,
        mock_load: MagicMock,
        mock_runtime_graph: MagicMock,
        mock_orch_cls: MagicMock,
        service: ExecutionServiceImpl,
        mock_session_service: MagicMock,
    ) -> None:
        del mock_payload_cls, mock_landscape_cls, mock_runtime_graph
        content = b"You are an audited prompt."
        blob_id = uuid4()
        run_id = uuid4()
        owner_session = uuid4()
        sha256 = hashlib.sha256(content).hexdigest()
        mock_session_service.get_run.return_value = _run_record_stub(status="running", session_id=owner_session)
        blob_record = _blob_record_stub(
            blob_id=blob_id,
            session_id=owner_session,
            content_hash=sha256,
            size_bytes=len(content),
        )

        blob_service = _blob_service_stub()
        blob_service.link_blob_to_run.return_value = None
        blob_service.read_blob_content.return_value = content
        blob_service.get_blob.return_value = blob_record
        blob_service.finalize_run_output_blobs.return_value = BlobFinalizationResult(finalized=(), errors=())
        cast(Any, service)._blob_service = blob_service
        mock_session_service.record_blob_inline_resolutions.side_effect = AuditIntegrityError("audit write refused")

        pipeline_yaml = f"""
source:
  plugin: csv
  options:
    path: input.csv
transforms:
  - name: classify
    plugin: llm
    options:
      system_prompt:
        blob_ref: {blob_id}
        mode: inline_content
        sha256: {sha256}
sinks:
  primary:
    plugin: json
    options:
      path: output.jsonl
"""

        with pytest.raises(AuditIntegrityError, match="audit write refused"):
            service._run_pipeline(str(run_id), pipeline_yaml, threading.Event())

        mock_load.assert_not_called()
        mock_orch_cls.assert_not_called()

    @patch("elspeth.web.execution.service.Orchestrator")
    @patch("elspeth.web.execution.service.build_validated_runtime_graph")
    @patch("elspeth.web.execution.service.load_settings_from_yaml_string")
    @patch("elspeth.web.execution.service.open_landscape_db")
    @patch("elspeth.web.execution.service.FilesystemPayloadStore")
    def test_oversized_inline_content_metadata_fails_before_blob_read(
        self,
        mock_payload_cls: MagicMock,
        mock_landscape_cls: MagicMock,
        mock_load: MagicMock,
        mock_runtime_graph: MagicMock,
        mock_orch_cls: MagicMock,
        service: ExecutionServiceImpl,
        mock_session_service: MagicMock,
    ) -> None:
        from elspeth.contracts.blobs_inline import BlobContentResolutionError

        del mock_payload_cls, mock_landscape_cls, mock_runtime_graph
        blob_id = uuid4()
        run_id = uuid4()
        owner_session = uuid4()
        sha256 = hashlib.sha256(b"small prompt").hexdigest()
        mock_session_service.get_run.return_value = _run_record_stub(status="running", session_id=owner_session)
        blob_record = _blob_record_stub(
            blob_id=blob_id,
            session_id=owner_session,
            content_hash=sha256,
            size_bytes=256 * 1024 + 1,
        )

        blob_service = _blob_service_stub()
        blob_service.link_blob_to_run.return_value = None
        blob_service.read_blob_content.return_value = b"small prompt"
        blob_service.get_blob.return_value = blob_record
        blob_service.finalize_run_output_blobs.return_value = BlobFinalizationResult(finalized=(), errors=())
        cast(Any, service)._blob_service = blob_service
        mock_session_service.record_blob_inline_resolutions.return_value = None

        pipeline_yaml = f"""
source:
  plugin: csv
  options:
    path: input.csv
transforms:
  - name: classify
    plugin: llm
    options:
      system_prompt:
        blob_ref: {blob_id}
        mode: inline_content
        sha256: {sha256}
sinks:
  primary:
    plugin: json
    options:
      path: output.jsonl
"""

        with pytest.raises(BlobContentResolutionError) as exc_info:
            service._run_pipeline(str(run_id), pipeline_yaml, threading.Event())

        assert exc_info.value.oversized == (("node:classify.options.system_prompt", 256 * 1024 + 1, 256 * 1024),)
        blob_service.read_blob_content.assert_not_awaited()
        blob_service.link_blob_to_run.assert_not_awaited()
        mock_session_service.record_blob_inline_resolutions.assert_not_called()
        mock_load.assert_not_called()
        mock_orch_cls.assert_not_called()

    @patch("elspeth.web.execution.service.Orchestrator")
    @patch("elspeth.web.execution.service.build_validated_runtime_graph")
    @patch("elspeth.web.execution.service.load_settings_from_yaml_string")
    @patch("elspeth.web.execution.service.open_landscape_db")
    @patch("elspeth.web.execution.service.FilesystemPayloadStore")
    def test_aggregate_inline_content_metadata_fails_before_blob_read(
        self,
        mock_payload_cls: MagicMock,
        mock_landscape_cls: MagicMock,
        mock_load: MagicMock,
        mock_runtime_graph: MagicMock,
        mock_orch_cls: MagicMock,
        service: ExecutionServiceImpl,
        mock_session_service: MagicMock,
    ) -> None:
        from elspeth.contracts.blobs_inline import BlobContentResolutionError

        del mock_payload_cls, mock_landscape_cls, mock_runtime_graph
        blob_ids = [uuid4() for _ in range(5)]
        run_id = uuid4()
        owner_session = uuid4()
        hashes = [hashlib.sha256(f"blob-{index}".encode()).hexdigest() for index in range(5)]
        mock_session_service.get_run.return_value = _run_record_stub(status="running", session_id=owner_session)

        records_by_id: dict[UUID, Any] = {}
        for blob_id, blob_hash in zip(blob_ids, hashes, strict=True):
            records_by_id[blob_id] = _blob_record_stub(
                blob_id=blob_id,
                session_id=owner_session,
                content_hash=blob_hash,
                size_bytes=220 * 1024,
            )

        async def get_blob(blob_id: UUID) -> Any:
            if blob_id in records_by_id:
                return records_by_id[blob_id]
            raise AssertionError(f"unexpected blob_id {blob_id}")

        blob_service = _blob_service_stub()
        blob_service.link_blob_to_run.return_value = None
        blob_service.read_blob_content.return_value = b"content"
        blob_service.get_blob.side_effect = get_blob
        blob_service.finalize_run_output_blobs.return_value = BlobFinalizationResult(finalized=(), errors=())
        cast(Any, service)._blob_service = blob_service
        mock_session_service.record_blob_inline_resolutions.return_value = None

        inline_options = "\n".join(
            f"""      prompt_{index}:
        blob_ref: {blob_id}
        mode: inline_content
        sha256: {blob_hash}"""
            for index, (blob_id, blob_hash) in enumerate(zip(blob_ids, hashes, strict=True))
        )
        pipeline_yaml = f"""
source:
  plugin: csv
  options:
    path: input.csv
transforms:
  - name: classify
    plugin: llm
    options:
{inline_options}
sinks:
  primary:
    plugin: json
    options:
      path: output.jsonl
"""

        with pytest.raises(BlobContentResolutionError) as exc_info:
            service._run_pipeline(str(run_id), pipeline_yaml, threading.Event())

        assert exc_info.value.oversized == (("(aggregate)", 5 * 220 * 1024, 1024 * 1024),)
        blob_service.read_blob_content.assert_not_awaited()
        blob_service.link_blob_to_run.assert_not_awaited()
        mock_session_service.record_blob_inline_resolutions.assert_not_called()
        mock_load.assert_not_called()
        mock_orch_cls.assert_not_called()

    @patch("elspeth.web.execution.service.Orchestrator")
    @patch("elspeth.web.execution.service.build_validated_runtime_graph")
    @patch("elspeth.web.execution.service.load_settings_from_yaml_string")
    @patch("elspeth.web.execution.service.open_landscape_db")
    @patch("elspeth.web.execution.service.FilesystemPayloadStore")
    def test_hash_mismatch_increments_zero_threshold_counter_without_run_id_label(
        self,
        mock_payload_cls: MagicMock,
        mock_landscape_cls: MagicMock,
        mock_load: MagicMock,
        mock_runtime_graph: MagicMock,
        mock_orch_cls: MagicMock,
        service: ExecutionServiceImpl,
        mock_session_service: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from elspeth.web.blobs.protocol import BlobIntegrityError
        from elspeth.web.execution import service as service_module

        del mock_payload_cls, mock_landscape_cls, mock_runtime_graph
        content = b"actual prompt bytes"
        blob_id = uuid4()
        run_id = uuid4()
        owner_session = uuid4()
        mock_session_service.get_run.return_value = _run_record_stub(status="running", session_id=owner_session)
        hash_counter = MagicMock(spec=["add"])
        monkeypatch.setattr(service_module, "_BLOB_INLINE_HASH_MISMATCH_TOTAL", hash_counter)

        blob_record = _blob_record_stub(
            blob_id=blob_id,
            session_id=owner_session,
            content_hash=hashlib.sha256(content).hexdigest(),
            size_bytes=len(content),
        )

        blob_service = _blob_service_stub()
        blob_service.link_blob_to_run.return_value = None
        blob_service.read_blob_content.return_value = content
        blob_service.get_blob.return_value = blob_record
        blob_service.finalize_run_output_blobs.return_value = BlobFinalizationResult(finalized=(), errors=())
        cast(Any, service)._blob_service = blob_service

        pipeline_yaml = f"""
source:
  plugin: csv
  options:
    path: input.csv
transforms:
  - name: classify
    plugin: llm
    options:
      system_prompt:
        blob_ref: {blob_id}
        mode: inline_content
        sha256: {"b" * 64}
sinks:
  primary:
    plugin: json
    options:
      path: output.jsonl
"""

        with pytest.raises(BlobIntegrityError):
            service._run_pipeline(str(run_id), pipeline_yaml, threading.Event())

        hash_counter.add.assert_called_once_with(1)
        mock_load.assert_not_called()
        mock_orch_cls.assert_not_called()

    @pytest.mark.parametrize(
        "case",
        ["missing", "cross_session_ready_hash_match", "cross_session_ready_hash_mismatch"],
    )
    @patch("elspeth.web.execution.service.Orchestrator")
    @patch("elspeth.web.execution.service.build_validated_runtime_graph")
    @patch("elspeth.web.execution.service.load_settings_from_yaml_string")
    @patch("elspeth.web.execution.service.open_landscape_db")
    @patch("elspeth.web.execution.service.FilesystemPayloadStore")
    def test_run_pipeline_rejects_cross_session_inline_blob_uniformly(
        self,
        mock_payload_cls: MagicMock,
        mock_landscape_cls: MagicMock,
        mock_load: MagicMock,
        mock_runtime_graph: MagicMock,
        mock_orch_cls: MagicMock,
        service: ExecutionServiceImpl,
        mock_session_service: MagicMock,
        case: str,
    ) -> None:
        """IDOR/metadata-oracle regression (elspeth-195ecb1d58).

        A crafted inline_content marker naming another session's blob must be
        rejected uniformly as ``BlobNotFoundError`` — byte-identical *type* to a
        genuinely-missing blob — BEFORE any status/hash/size comparison and
        before ``link_blob_to_run`` / ``read_blob_content`` are reached, so the
        metadata (status/hash/size) of another session's blob is never an oracle.

        Pre-fix, the three cases surface differently: ``missing`` raises
        ``BlobNotFoundError`` (control); ``cross_session_ready_hash_match`` sails
        through (link/read awaited, run proceeds); ``cross_session_ready_hash_mismatch``
        raises ``BlobIntegrityError``. The cross-session cases therefore do NOT
        match the missing control pre-fix. Post-fix all three collapse to
        ``BlobNotFoundError`` with link/read never awaited.
        """
        from elspeth.web.blobs.protocol import BlobNotFoundError

        del mock_payload_cls, mock_landscape_cls, mock_load, mock_runtime_graph, mock_orch_cls

        owner_session = uuid4()
        other_session = uuid4()
        content = b"another session's secret prompt"
        marker_sha = hashlib.sha256(content).hexdigest()
        blob_id = uuid4()
        run_id = uuid4()

        # The run is owned by ``owner_session``.
        mock_session_service.get_run.return_value = _run_record_stub(status="running", session_id=owner_session)
        mock_session_service.record_blob_inline_resolutions.return_value = None

        if case == "missing":
            # get_blob raises for a genuinely-missing blob (the control surface).
            async def get_blob(_blob_id: UUID) -> Any:
                raise BlobNotFoundError(str(_blob_id))

            blob_service = _blob_service_stub()
            blob_service.get_blob.side_effect = get_blob
        else:
            blob_record = _blob_record_stub(
                blob_id=blob_id,
                session_id=other_session,  # owned by a DIFFERENT session
                content_hash=marker_sha if case == "cross_session_ready_hash_match" else "b" * 64,
                size_bytes=len(content),
            )
            blob_service = _blob_service_stub()
            blob_service.get_blob.return_value = blob_record

        blob_service.link_blob_to_run.return_value = None
        blob_service.read_blob_content.return_value = content
        blob_service.finalize_run_output_blobs.return_value = BlobFinalizationResult(finalized=(), errors=())
        cast(Any, service)._blob_service = blob_service

        pipeline_yaml = f"""
source:
  plugin: csv
  options:
    path: input.csv
transforms:
  - name: classify
    plugin: llm
    options:
      system_prompt:
        blob_ref: {blob_id}
        mode: inline_content
        sha256: {marker_sha}
sinks:
  primary:
    plugin: json
    options:
      path: output.jsonl
"""

        with pytest.raises(BlobNotFoundError):
            service._run_pipeline(str(run_id), pipeline_yaml, threading.Event())

        # No metadata of the cross-session blob is ever consumed: the run never
        # links or reads it, and never records an inline resolution.
        blob_service.link_blob_to_run.assert_not_awaited()
        blob_service.read_blob_content.assert_not_awaited()
        mock_session_service.record_blob_inline_resolutions.assert_not_called()


def _blob_rows_pipeline_yaml(entries: list[dict[str, Any]], *, singular: bool = False) -> str:
    """Build a blob_rows pipeline config as JSON (a YAML subset)."""
    source_block = {
        "plugin": "blob_rows",
        "on_success": "docs",
        "options": {
            "blobs": entries,
            "schema": {"mode": "observed"},
            "on_validation_failure": "discard",
        },
    }
    config: dict[str, Any] = {
        "sinks": {"primary": {"plugin": "json", "options": {"path": "output.jsonl"}}},
    }
    if singular:
        config["source"] = source_block
    else:
        config["sources"] = {"documents": source_block}
    return json.dumps(config)


def _blob_rows_entry(content: bytes, *, blob_id: UUID, filename: str = "page-1.png", mime_type: str = "image/png") -> dict[str, Any]:
    return {
        "blob_id": str(blob_id),
        "payload_ref": hashlib.sha256(content).hexdigest(),
        "filename": filename,
        "mime_type": mime_type,
        "size_bytes": len(content),
    }


def _blob_rows_record_for_entry(entry: dict[str, Any], *, session_id: UUID, **overrides: Any) -> BlobRecord:
    values: dict[str, Any] = {
        "blob_id": UUID(entry["blob_id"]),
        "session_id": session_id,
        "filename": entry["filename"],
        "mime_type": entry["mime_type"],
        "size_bytes": entry["size_bytes"],
        "content_hash": entry["payload_ref"],
        "status": "ready",
    }
    values.update(overrides)
    return _blob_record_stub(**values)


@pytest.mark.usefixtures("mock_pipeline_config_assembly")
class TestBlobRowsRuntimeAdmission:
    """Run admission re-resolves persisted blob_rows entries and stages bytes.

    The sessions DB persists the plural binding's entries, but the session's
    blob records stay the run-time authority: every entry is re-resolved
    session-scoped (foreign == missing), any divergence fails admission
    before linking, and admitted content is staged into the run's payload
    store — integrity-checked at read and hash-bound at store — before the
    orchestrator runs (elspeth-0c6a343921).
    """

    @patch("elspeth.web.execution.service.Orchestrator")
    @patch("elspeth.web.execution.service.build_validated_runtime_graph")
    @patch("elspeth.web.execution.service.load_settings_from_config_dict")
    @patch("elspeth.web.execution.service.open_landscape_db")
    @patch("elspeth.web.execution.service.FilesystemPayloadStore")
    def test_admits_links_and_stages_blob_rows_in_order(
        self,
        mock_payload_cls: MagicMock,
        mock_landscape_cls: MagicMock,
        mock_load: MagicMock,
        mock_runtime_graph: MagicMock,
        mock_orch_cls: MagicMock,
        service: ExecutionServiceImpl,
        mock_session_service: MagicMock,
    ) -> None:
        del mock_landscape_cls
        owner_session = uuid4()
        run_id = uuid4()
        contents = [b"\x89PNG\r\n\x1a\npayload-one", b"\xff\xd8\xff\xe0payload-two"]
        blob_ids = [uuid4(), uuid4()]
        entries = [
            _blob_rows_entry(contents[0], blob_id=blob_ids[0], filename="page-1.png", mime_type="image/png"),
            _blob_rows_entry(contents[1], blob_id=blob_ids[1], filename="page-2.jpg", mime_type="image/jpeg"),
        ]
        records_by_id = {blob_ids[i]: _blob_rows_record_for_entry(entries[i], session_id=owner_session) for i in range(2)}
        content_by_id = dict(zip(blob_ids, contents, strict=True))
        order: list[str] = []

        mock_session_service.get_run.return_value = _run_record_stub(status="running", session_id=owner_session)

        async def get_blob(blob_id: UUID) -> BlobRecord:
            order.append("metadata")
            return records_by_id[blob_id]

        async def link_blob_to_run(**_kwargs: Any) -> None:
            order.append("link")

        async def read_blob_content(blob_id: UUID) -> bytes:
            order.append("read")
            return content_by_id[blob_id]

        blob_service = _blob_service_stub()
        blob_service.get_blob.side_effect = get_blob
        blob_service.link_blob_to_run.side_effect = link_blob_to_run
        blob_service.read_blob_content.side_effect = read_blob_content
        blob_service.finalize_run_output_blobs.return_value = BlobFinalizationResult(finalized=(), errors=())
        cast(Any, service)._blob_service = blob_service

        stored_contents: list[bytes] = []

        def store(content: bytes) -> str:
            order.append("store")
            stored_contents.append(content)
            return hashlib.sha256(content).hexdigest()

        mock_payload_cls.return_value.store.side_effect = store

        def load_settings(_config_dict: dict[str, Any], *, expand_env_vars: bool = True) -> SimpleNamespace:
            del expand_env_vars
            order.append("load")
            return _mock_pipeline_settings()

        mock_load.side_effect = load_settings
        mock_runtime_graph.return_value = SimpleNamespace(
            plugin_bundle=_plugin_bundle_stub(),
            graph=_execution_graph_stub(),
        )
        mock_orch_cls.return_value = _orchestrator_stub(_orchestrator_result_stub(run_id=str(run_id), rows_processed=2, rows_succeeded=2))

        with patch(
            "elspeth.web.execution.service.load_run_accounting_from_db",
            return_value=_run_accounting_for_status(RunStatus.COMPLETED),
        ):
            service._run_pipeline(str(run_id), _blob_rows_pipeline_yaml(entries), threading.Event())

        # Both blobs linked as inputs, both staged, in authoring order.
        assert blob_service.link_blob_to_run.await_count == 2
        linked_ids = [call.kwargs["blob_id"] for call in blob_service.link_blob_to_run.await_args_list]
        assert sorted(linked_ids, key=str) == sorted(blob_ids, key=str)
        assert all(call.kwargs["direction"] == "input" for call in blob_service.link_blob_to_run.await_args_list)
        assert all(call.kwargs["run_id"] == run_id for call in blob_service.link_blob_to_run.await_args_list)
        assert stored_contents == contents
        # Admission (metadata + link) precedes settings/plugin construction;
        # staging (read + store) happens after, against the run's own store.
        assert max(i for i, step in enumerate(order) if step == "link") < order.index("load")
        assert order.index("load") < order.index("read") < order.index("store")

    @pytest.mark.parametrize("case", ["missing", "cross_session"])
    @patch("elspeth.web.execution.service.Orchestrator")
    @patch("elspeth.web.execution.service.build_validated_runtime_graph")
    @patch("elspeth.web.execution.service.load_settings_from_config_dict")
    @patch("elspeth.web.execution.service.open_landscape_db")
    @patch("elspeth.web.execution.service.FilesystemPayloadStore")
    def test_cross_session_blob_rows_entry_reads_as_missing(
        self,
        mock_payload_cls: MagicMock,
        mock_landscape_cls: MagicMock,
        mock_load: MagicMock,
        mock_runtime_graph: MagicMock,
        mock_orch_cls: MagicMock,
        service: ExecutionServiceImpl,
        mock_session_service: MagicMock,
        case: str,
    ) -> None:
        """IDOR contract: a foreign blob is byte-identical in *type* to a
        genuinely-missing one, and no foreign metadata (status/hash/size) is
        ever compared, linked, or read — so nothing about another session's
        blob is an oracle."""
        del mock_landscape_cls, mock_load, mock_runtime_graph, mock_orch_cls
        owner_session = uuid4()
        content = b"\x89PNG\r\n\x1a\nforeign-payload"
        blob_id = uuid4()
        entry = _blob_rows_entry(content, blob_id=blob_id)

        mock_session_service.get_run.return_value = _run_record_stub(status="running", session_id=owner_session)
        blob_service = _blob_service_stub()
        if case == "missing":

            async def get_blob(_blob_id: UUID) -> BlobRecord:
                raise BlobNotFoundError(str(_blob_id))

            blob_service.get_blob.side_effect = get_blob
        else:
            # Fully consistent record — hash, filename, size all match — but
            # owned by a DIFFERENT session. Must be indistinguishable from missing.
            blob_service.get_blob.return_value = _blob_rows_record_for_entry(entry, session_id=uuid4())
        blob_service.finalize_run_output_blobs.return_value = BlobFinalizationResult(finalized=(), errors=())
        cast(Any, service)._blob_service = blob_service

        with pytest.raises(BlobNotFoundError):
            service._run_pipeline(str(uuid4()), _blob_rows_pipeline_yaml([entry]), threading.Event())

        blob_service.link_blob_to_run.assert_not_awaited()
        blob_service.read_blob_content.assert_not_awaited()
        mock_payload_cls.return_value.store.assert_not_called()

    @pytest.mark.parametrize(
        ("overrides", "expected_error", "match"),
        [
            ({"content_hash": "b" * 64}, BlobIntegrityError, None),
            ({"status": "pending"}, BlobStateError, "expected 'ready'"),
            ({"filename": "renamed.png"}, BlobRowsSourceAdmissionError, "filename"),
            ({"mime_type": "image/jpeg"}, BlobRowsSourceAdmissionError, "mime_type"),
            ({"size_bytes": 1}, BlobRowsSourceAdmissionError, "size_bytes"),
            # The plural resolver refuses LLM-authored blobs at authoring
            # time, but generic writers (set_source/set_pipeline/patch) can
            # also produce blob_rows options — admission is the authority
            # that holds for every authoring path (elspeth-0c6a343921 review).
            ({"creation_modality": CreationModality.LLM_GENERATED}, BlobRowsSourceAdmissionError, "LLM-authored"),
            ({"creation_modality": CreationModality.LLM_GENERATED_THEN_AMENDED}, BlobRowsSourceAdmissionError, "LLM-authored"),
        ],
    )
    @patch("elspeth.web.execution.service.Orchestrator")
    @patch("elspeth.web.execution.service.build_validated_runtime_graph")
    @patch("elspeth.web.execution.service.load_settings_from_config_dict")
    @patch("elspeth.web.execution.service.open_landscape_db")
    @patch("elspeth.web.execution.service.FilesystemPayloadStore")
    def test_divergent_blob_rows_entry_fails_admission_before_link(
        self,
        mock_payload_cls: MagicMock,
        mock_landscape_cls: MagicMock,
        mock_load: MagicMock,
        mock_runtime_graph: MagicMock,
        mock_orch_cls: MagicMock,
        service: ExecutionServiceImpl,
        mock_session_service: MagicMock,
        overrides: dict[str, Any],
        expected_error: type[Exception],
        match: str | None,
    ) -> None:
        del mock_landscape_cls, mock_load, mock_runtime_graph, mock_orch_cls
        owner_session = uuid4()
        content = b"\x89PNG\r\n\x1a\ndiverged-payload"
        entry = _blob_rows_entry(content, blob_id=uuid4())

        mock_session_service.get_run.return_value = _run_record_stub(status="running", session_id=owner_session)
        blob_service = _blob_service_stub()
        blob_service.get_blob.return_value = _blob_rows_record_for_entry(entry, session_id=owner_session, **overrides)
        blob_service.finalize_run_output_blobs.return_value = BlobFinalizationResult(finalized=(), errors=())
        cast(Any, service)._blob_service = blob_service

        with pytest.raises(expected_error, match=match):
            service._run_pipeline(str(uuid4()), _blob_rows_pipeline_yaml([entry]), threading.Event())

        blob_service.link_blob_to_run.assert_not_awaited()
        blob_service.read_blob_content.assert_not_awaited()
        mock_payload_cls.return_value.store.assert_not_called()

    @patch("elspeth.web.execution.service.Orchestrator")
    @patch("elspeth.web.execution.service.build_validated_runtime_graph")
    @patch("elspeth.web.execution.service.load_settings_from_config_dict")
    @patch("elspeth.web.execution.service.open_landscape_db")
    @patch("elspeth.web.execution.service.FilesystemPayloadStore")
    def test_malformed_blob_rows_entry_fails_before_any_blob_access(
        self,
        mock_payload_cls: MagicMock,
        mock_landscape_cls: MagicMock,
        mock_load: MagicMock,
        mock_runtime_graph: MagicMock,
        mock_orch_cls: MagicMock,
        service: ExecutionServiceImpl,
        mock_session_service: MagicMock,
    ) -> None:
        del mock_landscape_cls, mock_load, mock_runtime_graph, mock_orch_cls
        content = b"\x89PNG\r\n\x1a\nmalformed-entry"
        entry = _blob_rows_entry(content, blob_id=uuid4())
        del entry["payload_ref"]

        mock_session_service.get_run.return_value = _run_record_stub(status="running", session_id=uuid4())
        blob_service = _blob_service_stub()
        blob_service.finalize_run_output_blobs.return_value = BlobFinalizationResult(finalized=(), errors=())
        cast(Any, service)._blob_service = blob_service

        with pytest.raises(BlobRowsSourceAdmissionError, match=r"blobs\[0\] failed validation"):
            service._run_pipeline(str(uuid4()), _blob_rows_pipeline_yaml([entry]), threading.Event())

        blob_service.get_blob.assert_not_awaited()
        blob_service.link_blob_to_run.assert_not_awaited()
        mock_payload_cls.return_value.store.assert_not_called()

    @patch("elspeth.web.execution.service.Orchestrator")
    @patch("elspeth.web.execution.service.build_validated_runtime_graph")
    @patch("elspeth.web.execution.service.load_settings_from_config_dict")
    @patch("elspeth.web.execution.service.open_landscape_db")
    @patch("elspeth.web.execution.service.FilesystemPayloadStore")
    def test_singular_source_form_is_admitted(
        self,
        mock_payload_cls: MagicMock,
        mock_landscape_cls: MagicMock,
        mock_load: MagicMock,
        mock_runtime_graph: MagicMock,
        mock_orch_cls: MagicMock,
        service: ExecutionServiceImpl,
        mock_session_service: MagicMock,
    ) -> None:
        del mock_landscape_cls
        owner_session = uuid4()
        run_id = uuid4()
        content = b"\x89PNG\r\n\x1a\nsingular-form"
        blob_id = uuid4()
        entry = _blob_rows_entry(content, blob_id=blob_id)

        mock_session_service.get_run.return_value = _run_record_stub(status="running", session_id=owner_session)
        blob_service = _blob_service_stub()
        blob_service.get_blob.return_value = _blob_rows_record_for_entry(entry, session_id=owner_session)
        blob_service.link_blob_to_run.return_value = None
        blob_service.read_blob_content.return_value = content
        blob_service.finalize_run_output_blobs.return_value = BlobFinalizationResult(finalized=(), errors=())
        cast(Any, service)._blob_service = blob_service
        mock_payload_cls.return_value.store.return_value = hashlib.sha256(content).hexdigest()
        mock_load.return_value = _mock_pipeline_settings()
        mock_runtime_graph.return_value = SimpleNamespace(
            plugin_bundle=_plugin_bundle_stub(),
            graph=_execution_graph_stub(),
        )
        mock_orch_cls.return_value = _orchestrator_stub(_orchestrator_result_stub(run_id=str(run_id), rows_processed=1, rows_succeeded=1))

        with patch(
            "elspeth.web.execution.service.load_run_accounting_from_db",
            return_value=_run_accounting_for_status(RunStatus.COMPLETED),
        ):
            service._run_pipeline(str(run_id), _blob_rows_pipeline_yaml([entry], singular=True), threading.Event())

        blob_service.link_blob_to_run.assert_awaited_once_with(blob_id=blob_id, run_id=run_id, direction="input")
        mock_payload_cls.return_value.store.assert_called_once_with(content)

    @patch("elspeth.web.execution.service.Orchestrator")
    @patch("elspeth.web.execution.service.build_validated_runtime_graph")
    @patch("elspeth.web.execution.service.load_settings_from_config_dict")
    @patch("elspeth.web.execution.service.open_landscape_db")
    @patch("elspeth.web.execution.service.FilesystemPayloadStore")
    def test_store_hash_divergence_fails_the_run(
        self,
        mock_payload_cls: MagicMock,
        mock_landscape_cls: MagicMock,
        mock_load: MagicMock,
        mock_runtime_graph: MagicMock,
        mock_orch_cls: MagicMock,
        service: ExecutionServiceImpl,
        mock_session_service: MagicMock,
    ) -> None:
        """Tier-1 bind between the two stores: if the payload store reports a
        different content hash than the admitted payload_ref, the run fails
        before the orchestrator ever starts."""
        owner_session = uuid4()
        content = b"\x89PNG\r\n\x1a\nstore-divergence"
        entry = _blob_rows_entry(content, blob_id=uuid4())

        mock_session_service.get_run.return_value = _run_record_stub(status="running", session_id=owner_session)
        blob_service = _blob_service_stub()
        blob_service.get_blob.return_value = _blob_rows_record_for_entry(entry, session_id=owner_session)
        blob_service.link_blob_to_run.return_value = None
        blob_service.read_blob_content.return_value = content
        blob_service.finalize_run_output_blobs.return_value = BlobFinalizationResult(finalized=(), errors=())
        cast(Any, service)._blob_service = blob_service
        mock_payload_cls.return_value.store.return_value = "c" * 64
        mock_load.return_value = _mock_pipeline_settings()
        mock_runtime_graph.return_value = SimpleNamespace(
            plugin_bundle=_plugin_bundle_stub(),
            graph=_execution_graph_stub(),
        )
        orchestrator = _orchestrator_stub()
        mock_orch_cls.return_value = orchestrator

        with pytest.raises(BlobIntegrityError):
            service._run_pipeline(str(uuid4()), _blob_rows_pipeline_yaml([entry]), threading.Event())

        orchestrator.run.assert_not_called()


class TestWebRuntimeConfigLoading:
    """Web execution rejects file-backed config options before runtime graph construction."""

    @patch("elspeth.web.execution.service.build_validated_runtime_graph")
    @patch("elspeth.web.execution.service.open_landscape_db")
    @patch("elspeth.web.execution.service.FilesystemPayloadStore")
    def test_file_backed_template_options_fail_before_runtime_graph(
        self,
        mock_payload_cls: MagicMock,
        mock_landscape_cls: MagicMock,
        mock_runtime_graph: MagicMock,
        service: ExecutionServiceImpl,
    ) -> None:
        del mock_payload_cls, mock_landscape_cls
        pipeline_yaml = """
sources:
  source:
    plugin: csv
    on_success: transform_in
    options: {}
transforms:
  - name: classify
    plugin: llm
    input: transform_in
    on_success: results
    on_error: results
    options:
      template_file: prompt.txt
      lookup_file: lookup.yaml
      system_prompt_file: system.txt
sinks:
  primary:
    plugin: json
    on_write_failure: discard
    options:
      path: output.jsonl
"""
        mock_runtime_graph.side_effect = AssertionError("runtime graph must not be built")

        with (
            patch("elspeth.web.execution.service.validate_sink_effect_eligibility_from_raw_config"),
            pytest.raises(ValueError, match="template_file"),
        ):
            service._run_pipeline(str(uuid4()), pipeline_yaml, threading.Event())

        mock_runtime_graph.assert_not_called()


# ── B7: BaseException + Done Callback ─────────────────────────────────


class TestB7ExceptionHandling:
    """B7 fix: _run_pipeline() catches BaseException, not Exception.

    Layer 1: try/except BaseException updates run to failed status.
    Layer 2: future.add_done_callback() logs as safety net.
    """

    @patch("elspeth.web.execution.service.open_landscape_db")
    @patch("elspeth.web.execution.service.FilesystemPayloadStore")
    def test_keyboard_interrupt_skips_failed_status_update(
        self,
        mock_payload: MagicMock,
        mock_landscape: MagicMock,
        service: ExecutionServiceImpl,
        mock_session_service: MagicMock,
    ) -> None:
        """R6 fix: KeyboardInterrupt skips _call_async for the 'failed' update.

        The initial 'running' update succeeds (before LandscapeDB raises).
        The except block skips the 'failed' update — orphan cleanup handles it.
        """
        mock_landscape.side_effect = KeyboardInterrupt("ctrl-c")

        with _admitted_runtime_setup(), pytest.raises(KeyboardInterrupt):
            service._run_pipeline(str(uuid4()), _TEST_PIPELINE_YAML, threading.Event())

        # R6: The 'running' update went through, but the 'failed' update was skipped
        calls = mock_session_service.update_run_status.call_args_list
        assert len(calls) == 1  # Only the initial "running" call
        assert calls[0][1].get("status") == "running"

    @patch("elspeth.web.execution.service.open_landscape_db")
    @patch("elspeth.web.execution.service.FilesystemPayloadStore")
    def test_system_exit_skips_failed_status_update(
        self,
        mock_payload: MagicMock,
        mock_landscape: MagicMock,
        service: ExecutionServiceImpl,
        mock_session_service: MagicMock,
    ) -> None:
        """R6 fix: SystemExit skips _call_async for the 'failed' update."""
        mock_landscape.side_effect = SystemExit(1)

        with _admitted_runtime_setup(), pytest.raises(SystemExit):
            service._run_pipeline(str(uuid4()), _TEST_PIPELINE_YAML, threading.Event())

        # R6: The 'running' update went through, but the 'failed' update was skipped
        calls = mock_session_service.update_run_status.call_args_list
        assert len(calls) == 1  # Only the initial "running" call
        assert calls[0][1].get("status") == "running"

    def test_shutdown_event_cleaned_up_in_finally(
        self,
        service: ExecutionServiceImpl,
    ) -> None:
        """finally clause removes shutdown event from _shutdown_events dict."""
        run_id = str(uuid4())
        event = threading.Event()
        service._shutdown_events[run_id] = event

        with _admitted_runtime_setup(), patch("elspeth.web.execution.service.open_landscape_db") as mock_db:
            mock_db.side_effect = RuntimeError("boom")
            with pytest.raises(RuntimeError):
                service._run_pipeline(run_id, _TEST_PIPELINE_YAML, event)

        # finally must have removed the event
        assert run_id not in service._shutdown_events

    def test_done_callback_logs_last_resort_on_exception(self, service: ExecutionServiceImpl) -> None:
        """Callback logs a last-resort diagnostic when the pipeline future
        carries an exception.  This covers the edge case where _run_pipeline's
        own except block failed (e.g. update_run_status raised).
        """
        future: Future[None] = Future()
        future.set_exception(RuntimeError("unhandled"))

        with patch("elspeth.web.execution.service.slog") as mock_slog:
            service._on_pipeline_done(future)
            mock_slog.error.assert_called_once()
            call_kwargs = mock_slog.error.call_args
            assert call_kwargs[0][0] == "pipeline_done_callback_exception"
            assert call_kwargs[1]["exc_type"] == "RuntimeError"
            # Redaction contract: the slog emits ONLY class names via
            # ``exc_class_chain``. ``exc_msg`` (length-truncated
            # ``str(exc)``) is forbidden because pipeline exceptions may
            # chain SQLAlchemyError payloads, Tier-3 sanitizer text, or
            # source-rendering fragments through ``__cause__`` /
            # ``__context__``.
            assert "exc_msg" not in call_kwargs[1]
            assert call_kwargs[1]["exc_class_chain"] == ["RuntimeError"]

    def test_done_callback_walks_exception_chain(self, service: ExecutionServiceImpl) -> None:
        """Chained exceptions surface as a class-name chain — no payloads.

        Regression: ``exc_msg=str(exc)[:200]`` leaked truncated-but-still-
        sensitive text. The chain walk visits ``__cause__`` / ``__context__``
        and records only ``type(current).__name__``.
        """
        try:
            try:
                raise ValueError("secret=deadbeef")  # Tier-3-ish payload
            except ValueError as inner:
                raise RuntimeError("outer") from inner
        except RuntimeError as outer:
            future: Future[None] = Future()
            future.set_exception(outer)

        with patch("elspeth.web.execution.service.slog") as mock_slog:
            service._on_pipeline_done(future)
            call_kwargs = mock_slog.error.call_args[1]
            assert call_kwargs["exc_type"] == "RuntimeError"
            assert call_kwargs["exc_class_chain"] == ["RuntimeError", "ValueError"]
            # No ``str(exc)`` text should appear in any field.
            for value in call_kwargs.values():
                if isinstance(value, str):
                    assert "secret" not in value
                    assert "deadbeef" not in value

    def test_done_callback_noop_on_success(self, service: ExecutionServiceImpl) -> None:
        """done_callback does not log on successful completion."""
        future: Future[None] = Future()
        future.set_result(None)

        with patch("elspeth.web.execution.service.slog") as mock_slog:
            service._on_pipeline_done(future)
            mock_slog.error.assert_not_called()

    @patch("elspeth.web.execution.service.open_landscape_db")
    @patch("elspeth.web.execution.service.FilesystemPayloadStore")
    def test_pydantic_validation_error_emits_schema_contract_diagnostic(
        self,
        mock_payload: MagicMock,
        mock_landscape: MagicMock,
        service: ExecutionServiceImpl,
        mock_session_service: MagicMock,
    ) -> None:
        """Strict schema crashes need operator diagnostics, not just generic failure."""
        from pydantic import BaseModel
        from pydantic import ValidationError as PydanticValidationError

        class SchemaContractProbe(BaseModel):
            internal_required_field: int

        try:
            SchemaContractProbe()
        except PydanticValidationError as exc:
            validation_error = exc
        else:
            raise AssertionError("expected SchemaContractProbe() to raise")

        run_id = str(uuid4())
        mock_session_service.get_run.return_value = _run_record_stub(status="running")

        with (
            patch(
                "elspeth.web.execution.service.load_settings_from_yaml_string",
                side_effect=validation_error,
            ),
            patch("elspeth.web.execution.service.slog") as mock_slog,
            pytest.raises(PydanticValidationError),
        ):
            service._run_pipeline(run_id, "source:\n  plugin: csv\n", threading.Event())

        schema_calls = [call for call in mock_slog.error.call_args_list if call.args[0] == "run_schema_contract_violation"]
        assert len(schema_calls) == 1
        schema_kwargs = schema_calls[0].kwargs
        assert schema_kwargs["run_id"] == run_id
        assert schema_kwargs["exc_class"] == "ValidationError"
        assert schema_kwargs["error_count"] == 1
        assert schema_kwargs["schema_errors"] == [{"loc": "internal_required_field", "type": "missing"}]

        failed_calls = [call for call in mock_session_service.update_run_status.call_args_list if call.kwargs.get("status") == "failed"]
        assert failed_calls
        # F15 split: the CLIENT surface stays sanitized, the OPERATOR surface
        # carries the detail.  Before the split both were the same 39-char
        # string, so this test could only assert one of them; the
        # "validation structure must not reach the client" intent now lives
        # on the ``failed`` event's ``detail`` where it actually applies.
        failed_events = [
            call.kwargs for call in mock_session_service.append_run_event.call_args_list if call.kwargs.get("event_type") == "failed"
        ]
        assert failed_events
        assert failed_events[-1]["data"]["detail"] == "Pipeline execution failed (ValidationError)"
        assert "internal_required_field" not in failed_events[-1]["data"]["detail"]

        # runs.error is the operator surface: it names the class and, because
        # a schema-contract breach IS the diagnostic, the offending field.
        operator_error = failed_calls[-1].kwargs["error"]
        assert operator_error.startswith("Pipeline execution failed (ValidationError)")
        assert "internal_required_field" in operator_error


# ── Operator failure diagnostics (F15) ─────────────────────────────────

_DIAGNOSTIC_OBSERVED_SCHEMA = SchemaConfig.from_dict({"mode": "observed"})
# A secret-shaped token, kept in one place so the scrub assertions and the
# raised exception can never drift apart.
_SECRET_SHAPED_TOKEN = "sk-" + "abcd1234efgh5678ijkl9012mnop3456"  # secret-scan: allow-this-line


def _seed_run_with_node_state(
    db: LandscapeDB,
    *,
    run_id: str,
    node_id: str = "extract",
    state_id: str = "state-0",
    token_id: str = "token-0",
    row_id: str = "row-0",
    ingest_sequence: int = 0,
    status: NodeStateStatus = NodeStateStatus.FAILED,
    begin_run: bool = True,
) -> None:
    """Write a real Landscape node_states row for the failing-node lookup.

    Deliberately goes through ``RecorderFactory`` — the same writer the
    engine uses — so the persisted ``status`` string comes from
    ``NodeStateStatus`` rather than a literal duplicated in the test. A test
    that hardcoded the same wrong literal as the query would pass while
    production returned ``None`` forever.
    """
    factory = RecorderFactory(db)
    if begin_run:
        factory.run_lifecycle.begin_run(config={}, canonical_version="v1", run_id=run_id)
        factory.data_flow.register_node(
            run_id=run_id,
            node_id="source",
            plugin_name="csv",
            node_type=NodeType.SOURCE,
            plugin_version="1.0",
            config={},
            schema_config=_DIAGNOSTIC_OBSERVED_SCHEMA,
        )
    factory.data_flow.register_node(
        run_id=run_id,
        node_id=node_id,
        plugin_name="llm_extract",
        node_type=NodeType.TRANSFORM,
        plugin_version="1.0",
        config={},
        schema_config=_DIAGNOSTIC_OBSERVED_SCHEMA,
    )
    row = factory.data_flow.create_row(
        run_id,
        "source",
        ingest_sequence,
        {"html": "<h1>A</h1>"},
        row_id=row_id,
        source_row_index=ingest_sequence,
        ingest_sequence=ingest_sequence,
    )
    token = factory.data_flow.create_token(row.row_id, token_id=token_id)
    state = factory.execution.begin_node_state(
        token.token_id,
        node_id,
        run_id,
        1,
        {"html": "<h1>A</h1>"},
        state_id=state_id,
    )
    factory.execution.complete_node_state(
        state.state_id,
        status,
        output_data={},
        duration_ms=0.0,
        error=(
            ExecutionError(exception="node blew up", exception_type="ValueError", phase="transform")
            if status is NodeStateStatus.FAILED
            else None
        ),
    )


class TestFailedNodeIdLookup:
    """``_lookup_failed_node_id`` reads the failing node back from Landscape.

    The engine already writes a FAILED ``node_states`` row naming the node
    that died; before F15 the web layer never read it, so both ``runs.error``
    and the ``failed`` SSE event reported only *that* the run failed.

    These run against a real SQLite Landscape DB so the SQL, the persisted
    status literal, and the ordering are all genuinely exercised.
    """

    def test_returns_failed_node_id(self, service: ExecutionServiceImpl, tmp_path: Path) -> None:
        db = LandscapeDB.from_url(f"sqlite:///{tmp_path / 'audit.db'}")
        try:
            _seed_run_with_node_state(db, run_id="web-run-1", node_id="extract")
            assert service._lookup_failed_node_id(db, run_id="web-run-1").node_id == "extract"
        finally:
            db.close()

    def test_returns_newest_failed_node_when_several_failed(self, service: ExecutionServiceImpl, tmp_path: Path) -> None:
        """Ordering contract: the most recently completed FAILED state wins.

        ``completed_at`` is set explicitly rather than relying on wall-clock
        ordering — two writes in the same millisecond would otherwise make
        this assertion decide nothing.
        """
        db = LandscapeDB.from_url(f"sqlite:///{tmp_path / 'audit.db'}")
        try:
            _seed_run_with_node_state(db, run_id="web-run-1", node_id="early", state_id="state-early")
            _seed_run_with_node_state(
                db,
                run_id="web-run-1",
                node_id="late",
                state_id="state-late",
                token_id="token-1",
                row_id="row-1",
                ingest_sequence=1,
                begin_run=False,
            )
            with db.write_connection() as conn:
                conn.execute(text("UPDATE node_states SET completed_at = '2026-01-01 00:00:00' WHERE state_id = 'state-early'"))
                conn.execute(text("UPDATE node_states SET completed_at = '2026-01-02 00:00:00' WHERE state_id = 'state-late'"))

            assert service._lookup_failed_node_id(db, run_id="web-run-1").node_id == "late"
        finally:
            db.close()

    def test_returns_none_when_no_node_state_failed(self, service: ExecutionServiceImpl, tmp_path: Path) -> None:
        """A COMPLETED-only run has no failing node — the answer is None, not a guess."""
        db = LandscapeDB.from_url(f"sqlite:///{tmp_path / 'audit.db'}")
        try:
            _seed_run_with_node_state(db, run_id="web-run-1", status=NodeStateStatus.COMPLETED)
            outcome = service._lookup_failed_node_id(db, run_id="web-run-1")
            assert outcome.node_id is None
            # A healthy read that simply found no FAILED node is NOT a
            # degraded audit read — the counterpart to the
            # ``failed is True`` assertion below.
            assert outcome.failed is False
        finally:
            db.close()

    def test_returns_none_for_other_runs_failed_node(self, service: ExecutionServiceImpl, tmp_path: Path) -> None:
        """Run scoping: another run's failure must never be attributed here."""
        db = LandscapeDB.from_url(f"sqlite:///{tmp_path / 'audit.db'}")
        try:
            _seed_run_with_node_state(db, run_id="web-run-1", node_id="extract")
            assert service._lookup_failed_node_id(db, run_id="web-run-2").node_id is None
        finally:
            db.close()

    def test_returns_none_without_landscape_db(self, service: ExecutionServiceImpl) -> None:
        """Pre-init failures (raise before ``open_landscape_db``) degrade cleanly."""
        assert service._lookup_failed_node_id(None, run_id="web-run-1").node_id is None

    def test_degrades_to_none_when_audit_db_unavailable(self, service: ExecutionServiceImpl) -> None:
        """Audit-system degradation must not replace the original pipeline exception."""
        broken_db = MagicMock(spec=LandscapeDB)
        broken_db.read_only_connection.side_effect = SQLAlchemyError("audit db gone")

        with patch("elspeth.web.execution.service.slog") as mock_slog:
            outcome = service._lookup_failed_node_id(broken_db, run_id="web-run-1")

        assert outcome.node_id is None
        # The explicit ``failed`` flag is what distinguishes audit-read failure
        # from "no FAILED node recorded" — a bare None would conflate them.
        assert outcome.failed is True

        warnings = [call for call in mock_slog.warning.call_args_list if call.args and call.args[0] == "failed_node_id_lookup_failed"]
        assert len(warnings) == 1
        assert warnings[0].kwargs["run_id"] == "web-run-1"


@pytest.mark.usefixtures("mock_pipeline_config_assembly")
class TestOperatorFailureDiagnostic:
    """F15: web run failures must persist the operator diagnostic, not the 39-char client string.

    Before this fix, ``runs.error``, the ``failed`` SSE ``detail``, and the
    structured log all collapsed to ``"Pipeline execution failed (X)"`` — the
    exception message, the failing node, and the fault location were all
    discarded even though Landscape already held them. The client surface
    stays sanitized; the operator surface does not.
    """

    @staticmethod
    def _run_failing_pipeline(
        service: ExecutionServiceImpl,
        mock_session_service: MagicMock,
        *,
        mock_load: MagicMock,
        mock_instantiate: MagicMock,
        mock_graph_cls: MagicMock,
        mock_orch_cls: MagicMock,
        mock_landscape: MagicMock,
        exc: BaseException,
        landscape_db: LandscapeDB | None = None,
        run_id: str | None = None,
    ) -> tuple[str, str, list[Any], MagicMock]:
        """Drive ``_run_pipeline`` to the except-BaseException path.

        Returns ``(run_id, runs_error, failed_event_payloads, slog_mock)``.
        """
        _configure_runtime_success(
            mock_load=mock_load,
            mock_instantiate=mock_instantiate,
            mock_graph_cls=mock_graph_cls,
            mock_orch_cls=mock_orch_cls,
        )

        def raise_from_named_frame(*_args: Any, **_kwargs: Any) -> None:
            raise exc

        mock_orch_cls.return_value.run.side_effect = raise_from_named_frame
        if landscape_db is not None:
            mock_landscape.return_value = landscape_db
        # Run still 'running' — the non-terminal recovery branch, where the
        # failed status update and the failed SSE event both fire.
        mock_session_service.get_run.return_value = _run_record_stub(status="running")

        run_id = run_id or str(uuid4())
        with patch("elspeth.web.execution.service.slog") as mock_slog, pytest.raises(type(exc)):
            service._run_pipeline(run_id, _TEST_PIPELINE_YAML, threading.Event())

        failed_status_calls = [
            call for call in mock_session_service.update_run_status.call_args_list if call.kwargs.get("status") == "failed"
        ]
        assert failed_status_calls, "the non-terminal recovery branch must record status=failed"
        failed_events = [
            call.kwargs for call in mock_session_service.append_run_event.call_args_list if call.kwargs.get("event_type") == "failed"
        ]
        return run_id, failed_status_calls[-1].kwargs["error"], failed_events, mock_slog

    @patch("elspeth.web.execution.service.Orchestrator")
    @patch("elspeth.web.execution.preflight.ExecutionGraph")
    @patch("elspeth.web.execution.preflight.instantiate_plugins_from_config")
    @patch("elspeth.web.execution.service.load_settings_from_yaml_string")
    @patch("elspeth.web.execution.service.open_landscape_db")
    @patch("elspeth.web.execution.service.FilesystemPayloadStore")
    def test_runs_error_carries_class_message_and_structural_frames(
        self,
        mock_payload: MagicMock,
        mock_landscape: MagicMock,
        mock_load: MagicMock,
        mock_instantiate: MagicMock,
        mock_graph_cls: MagicMock,
        mock_orch_cls: MagicMock,
        service: ExecutionServiceImpl,
        mock_session_service: MagicMock,
    ) -> None:
        """Test (a): the persisted error names the class, the message, and where it surfaced."""
        _run_id, runs_error, failed_events, _slog = self._run_failing_pipeline(
            service,
            mock_session_service,
            mock_load=mock_load,
            mock_instantiate=mock_instantiate,
            mock_graph_cls=mock_graph_cls,
            mock_orch_cls=mock_orch_cls,
            mock_landscape=mock_landscape,
            exc=ValueError("boom"),
        )

        assert runs_error.startswith("Pipeline execution failed (ValueError)")
        assert "Message: boom" in runs_error
        assert "Structural traceback (most recent call last):" in runs_error

        frame_lines = [line.strip().lstrip("• ").strip() for line in runs_error.splitlines() if line.startswith("  • ")]
        assert frame_lines, "the diagnostic must carry at least one structural frame"
        # Every frame is file:line:function — code structure, no source text.
        for frame in frame_lines:
            path, lineno, func = frame.rsplit(":", 2)
            assert path and func
            assert lineno.isdigit()
        assert any(frame.endswith(":raise_from_named_frame") for frame in frame_lines), frame_lines
        assert any("execution/service.py" in frame and frame.endswith(":_run_pipeline") for frame in frame_lines), frame_lines

        # Path-disclosure guarantee: no absolute paths, no deployment layout.
        assert "/home/" not in runs_error
        assert ".venv" not in runs_error
        for frame in frame_lines:
            assert not frame.startswith("/"), frame
            assert frame.count("/") < _MAX_FRAME_PATH_PARTS, frame

        # The client surface is unchanged by all of the above.
        assert failed_events[-1]["data"]["detail"] == "Pipeline execution failed (ValueError)"

    @patch("elspeth.web.execution.service.Orchestrator")
    @patch("elspeth.web.execution.preflight.ExecutionGraph")
    @patch("elspeth.web.execution.preflight.instantiate_plugins_from_config")
    @patch("elspeth.web.execution.service.load_settings_from_yaml_string")
    @patch("elspeth.web.execution.service.open_landscape_db")
    @patch("elspeth.web.execution.service.FilesystemPayloadStore")
    def test_failed_node_id_reaches_event_and_runs_error(
        self,
        mock_payload: MagicMock,
        mock_landscape: MagicMock,
        mock_load: MagicMock,
        mock_instantiate: MagicMock,
        mock_graph_cls: MagicMock,
        mock_orch_cls: MagicMock,
        service: ExecutionServiceImpl,
        mock_session_service: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Test (b), present: a FAILED node_states row populates FailedData.node_id."""
        run_id = str(uuid4())
        db = LandscapeDB.from_url(f"sqlite:///{tmp_path / 'audit.db'}")
        try:
            _seed_run_with_node_state(db, run_id=run_id, node_id="extract")
            _run_id, runs_error, failed_events, mock_slog = self._run_failing_pipeline(
                service,
                mock_session_service,
                mock_load=mock_load,
                mock_instantiate=mock_instantiate,
                mock_graph_cls=mock_graph_cls,
                mock_orch_cls=mock_orch_cls,
                mock_landscape=mock_landscape,
                exc=ValueError("boom"),
                landscape_db=db,
                run_id=run_id,
            )
        finally:
            db.close()

        assert failed_events, "a non-terminal failure must broadcast a failed event"
        assert failed_events[-1]["data"]["node_id"] == "extract"
        assert "Most recent recorded node failure: extract" in runs_error

        failure_logs = [call for call in mock_slog.error.call_args_list if call.args and call.args[0] == "run_pipeline_failed"]
        assert len(failure_logs) == 1
        assert failure_logs[0].kwargs["failed_node_id"] == "extract"

    @patch("elspeth.web.execution.service.Orchestrator")
    @patch("elspeth.web.execution.preflight.ExecutionGraph")
    @patch("elspeth.web.execution.preflight.instantiate_plugins_from_config")
    @patch("elspeth.web.execution.service.load_settings_from_yaml_string")
    @patch("elspeth.web.execution.service.open_landscape_db")
    @patch("elspeth.web.execution.service.FilesystemPayloadStore")
    def test_absent_failed_node_state_yields_none_not_a_guess(
        self,
        mock_payload: MagicMock,
        mock_landscape: MagicMock,
        mock_load: MagicMock,
        mock_instantiate: MagicMock,
        mock_graph_cls: MagicMock,
        mock_orch_cls: MagicMock,
        service: ExecutionServiceImpl,
        mock_session_service: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Test (b), absent: no FAILED node state → node_id None, and the text says so."""
        run_id = str(uuid4())
        db = LandscapeDB.from_url(f"sqlite:///{tmp_path / 'audit.db'}")
        try:
            _seed_run_with_node_state(db, run_id=run_id, status=NodeStateStatus.COMPLETED)
            _run_id, runs_error, failed_events, mock_slog = self._run_failing_pipeline(
                service,
                mock_session_service,
                mock_load=mock_load,
                mock_instantiate=mock_instantiate,
                mock_graph_cls=mock_graph_cls,
                mock_orch_cls=mock_orch_cls,
                mock_landscape=mock_landscape,
                exc=ValueError("boom"),
                landscape_db=db,
                run_id=run_id,
            )
        finally:
            db.close()

        assert failed_events[-1]["data"]["node_id"] is None
        assert "Most recent recorded node failure: none recorded" in runs_error

        failure_logs = [call for call in mock_slog.error.call_args_list if call.args and call.args[0] == "run_pipeline_failed"]
        assert len(failure_logs) == 1
        assert failure_logs[0].kwargs["failed_node_id"] is None

    @patch("elspeth.web.execution.service.Orchestrator")
    @patch("elspeth.web.execution.preflight.ExecutionGraph")
    @patch("elspeth.web.execution.preflight.instantiate_plugins_from_config")
    @patch("elspeth.web.execution.service.load_settings_from_yaml_string")
    @patch("elspeth.web.execution.service.open_landscape_db")
    @patch("elspeth.web.execution.service.FilesystemPayloadStore")
    def test_secret_shaped_message_is_scrubbed_before_persist_and_log(
        self,
        mock_payload: MagicMock,
        mock_landscape: MagicMock,
        mock_load: MagicMock,
        mock_instantiate: MagicMock,
        mock_graph_cls: MagicMock,
        mock_orch_cls: MagicMock,
        service: ExecutionServiceImpl,
        mock_session_service: MagicMock,
    ) -> None:
        """Test (c): a candidate secret in the message never reaches the audit row OR the log.

        The whole message is replaced rather than partially masked — partial
        redaction leaks structure, and truncation is not redaction.
        """
        _run_id, runs_error, _failed_events, mock_slog = self._run_failing_pipeline(
            service,
            mock_session_service,
            mock_load=mock_load,
            mock_instantiate=mock_instantiate,
            mock_graph_cls=mock_graph_cls,
            mock_orch_cls=mock_orch_cls,
            mock_landscape=mock_landscape,
            exc=RuntimeError(f"provider rejected token {_SECRET_SHAPED_TOKEN}"),
        )

        assert _SECRET_SHAPED_TOKEN not in runs_error
        assert "Message: <redacted-secret>" in runs_error
        # The class chain still identifies the fault.
        assert runs_error.startswith("Pipeline execution failed (RuntimeError)")

        failure_logs = [call for call in mock_slog.error.call_args_list if call.args and call.args[0] == "run_pipeline_failed"]
        assert len(failure_logs) == 1
        assert failure_logs[0].kwargs["exc_message"] == "<redacted-secret>"
        for value in failure_logs[0].kwargs.values():
            assert _SECRET_SHAPED_TOKEN not in str(value)

    @patch("elspeth.web.execution.service.Orchestrator")
    @patch("elspeth.web.execution.preflight.ExecutionGraph")
    @patch("elspeth.web.execution.preflight.instantiate_plugins_from_config")
    @patch("elspeth.web.execution.service.load_settings_from_yaml_string")
    @patch("elspeth.web.execution.service.open_landscape_db")
    @patch("elspeth.web.execution.service.FilesystemPayloadStore")
    def test_pydantic_input_values_never_reach_runs_error(
        self,
        mock_payload: MagicMock,
        mock_landscape: MagicMock,
        mock_load: MagicMock,
        mock_instantiate: MagicMock,
        mock_graph_cls: MagicMock,
        mock_orch_cls: MagicMock,
        service: ExecutionServiceImpl,
        mock_session_service: MagicMock,
    ) -> None:
        """Test (d): a Pydantic ``input_value`` payload never reaches ``runs.error``.

        ``str(ValidationError)`` interleaves ``input_value=...`` into every
        error line, and the sentinel below is deliberately NOT secret-shaped,
        so scrubbing alone would wave it straight through into the audit row.
        The persisted diagnostic must carry the field name and error type
        instead of the input echo.
        """
        from pydantic import BaseModel
        from pydantic import ValidationError as PydanticValidationError

        sentinel = "ROW-PAYLOAD-SENTINEL-9c4e1"

        class _ContractProbe(BaseModel):
            rows: int

        try:
            _ContractProbe(rows=sentinel)  # type: ignore[arg-type]
        except PydanticValidationError as exc:
            pydantic_exc = exc
        else:
            raise AssertionError("expected _ContractProbe to raise")
        # Precondition for the guard to be non-vacuous: the raw str() DOES
        # embed the sentinel, and the scrubber does NOT redact it.
        assert sentinel in str(pydantic_exc)
        from elspeth.web.execution.service import _scrubbed_exception_message

        assert sentinel in _scrubbed_exception_message(pydantic_exc)

        _run_id, runs_error, failed_events, mock_slog = self._run_failing_pipeline(
            service,
            mock_session_service,
            mock_load=mock_load,
            mock_instantiate=mock_instantiate,
            mock_graph_cls=mock_graph_cls,
            mock_orch_cls=mock_orch_cls,
            mock_landscape=mock_landscape,
            exc=pydantic_exc,
        )

        assert sentinel not in runs_error
        assert runs_error.startswith("Pipeline execution failed (ValidationError)")
        # Field-name diagnostics survive: the offending loc and error type.
        assert "rows" in runs_error
        assert "int_parsing" in runs_error
        assert "schema contract violation" in runs_error

        # The operator log channel carries the same input-free message.
        failure_logs = [call for call in mock_slog.error.call_args_list if call.args and call.args[0] == "run_pipeline_failed"]
        assert len(failure_logs) == 1
        for value in failure_logs[0].kwargs.values():
            assert sentinel not in str(value)

        # The client surface stays sanitized as ever.
        assert failed_events[-1]["data"]["detail"] == "Pipeline execution failed (ValidationError)"
        assert sentinel not in failed_events[-1]["data"]["detail"]
        for payload in failed_events:
            assert _SECRET_SHAPED_TOKEN not in str(payload)

    @patch("elspeth.web.execution.service.Orchestrator")
    @patch("elspeth.web.execution.preflight.ExecutionGraph")
    @patch("elspeth.web.execution.preflight.instantiate_plugins_from_config")
    @patch("elspeth.web.execution.service.load_settings_from_yaml_string")
    @patch("elspeth.web.execution.service.open_landscape_db")
    @patch("elspeth.web.execution.service.FilesystemPayloadStore")
    def test_slog_record_carries_chain_message_node_and_frames(
        self,
        mock_payload: MagicMock,
        mock_landscape: MagicMock,
        mock_load: MagicMock,
        mock_instantiate: MagicMock,
        mock_graph_cls: MagicMock,
        mock_orch_cls: MagicMock,
        service: ExecutionServiceImpl,
        mock_session_service: MagicMock,
    ) -> None:
        """Test (d): the structured log gains the fields the audit row gained."""
        try:
            raise ValueError("inner cause")
        except ValueError as inner:
            wrapped = RuntimeError("outer wrapper")
            wrapped.__cause__ = inner

        run_id, _runs_error, _failed_events, mock_slog = self._run_failing_pipeline(
            service,
            mock_session_service,
            mock_load=mock_load,
            mock_instantiate=mock_instantiate,
            mock_graph_cls=mock_graph_cls,
            mock_orch_cls=mock_orch_cls,
            mock_landscape=mock_landscape,
            exc=wrapped,
        )

        failure_logs = [call for call in mock_slog.error.call_args_list if call.args and call.args[0] == "run_pipeline_failed"]
        assert len(failure_logs) == 1
        kwargs = failure_logs[0].kwargs
        assert kwargs["run_id"] == run_id
        assert kwargs["exc_class"] == "RuntimeError"
        assert kwargs["exc_class_chain"] == ["RuntimeError", "ValueError"]
        assert kwargs["exc_message"] == "outer wrapper"
        assert kwargs["failed_node_id"] is None
        assert isinstance(kwargs["traceback_frames"], list)
        assert kwargs["traceback_frames"], "frames must be present, not an empty list"
        for frame in kwargs["traceback_frames"]:
            assert not frame.startswith("/"), frame

    @patch("elspeth.web.execution.service.Orchestrator")
    @patch("elspeth.web.execution.preflight.ExecutionGraph")
    @patch("elspeth.web.execution.preflight.instantiate_plugins_from_config")
    @patch("elspeth.web.execution.service.load_settings_from_yaml_string")
    @patch("elspeth.web.execution.service.open_landscape_db")
    @patch("elspeth.web.execution.service.FilesystemPayloadStore")
    def test_diagnostic_log_fires_even_when_audit_row_is_already_terminal(
        self,
        mock_payload: MagicMock,
        mock_landscape: MagicMock,
        mock_load: MagicMock,
        mock_instantiate: MagicMock,
        mock_graph_cls: MagicMock,
        mock_orch_cls: MagicMock,
        service: ExecutionServiceImpl,
        mock_session_service: MagicMock,
    ) -> None:
        """The diagnostic log is the ONLY channel on the post-audit-terminal path.

        When the probe finds the audit row already terminal, audit primacy
        forbids both the ``failed`` status update and the ``failed`` SSE
        event — so ``runs.error`` never receives the diagnostic. This test
        pins the claim that justifies not adding a log at that branch:
        ``run_pipeline_failed`` fires BEFORE the terminality split, so the
        message, node, and frames are recorded against the run regardless.
        """
        _configure_runtime_success(
            mock_load=mock_load,
            mock_instantiate=mock_instantiate,
            mock_graph_cls=mock_graph_cls,
            mock_orch_cls=mock_orch_cls,
        )
        mock_orch_cls.return_value.run.side_effect = ValueError("boom")
        mock_session_service.get_run.return_value = _run_record_stub(status="completed")

        run_id = str(uuid4())
        with patch("elspeth.web.execution.service.slog") as mock_slog, pytest.raises(ValueError, match="boom"):
            service._run_pipeline(run_id, _TEST_PIPELINE_YAML, threading.Event())

        # Audit primacy holds: no failed status update, no failed SSE event.
        statuses = [call.kwargs.get("status") for call in mock_session_service.update_run_status.call_args_list]
        assert "failed" not in statuses, statuses
        failed_events = [
            call.kwargs for call in mock_session_service.append_run_event.call_args_list if call.kwargs.get("event_type") == "failed"
        ]
        assert failed_events == []

        # ...and the diagnostic still reached the operator via the log.
        failure_logs = [call for call in mock_slog.error.call_args_list if call.args and call.args[0] == "run_pipeline_failed"]
        assert len(failure_logs) == 1
        assert failure_logs[0].kwargs["run_id"] == run_id
        assert failure_logs[0].kwargs["exc_message"] == "boom"
        assert failure_logs[0].kwargs["traceback_frames"]

    @patch("elspeth.web.execution.service.open_landscape_db")
    @patch("elspeth.web.execution.service.FilesystemPayloadStore")
    def test_signal_failure_skips_landscape_read_and_diagnostic_log(
        self,
        mock_payload: MagicMock,
        mock_landscape: MagicMock,
        service: ExecutionServiceImpl,
    ) -> None:
        """Signals are excluded deliberately: the event loop is shutting down.

        Pairs with ``test_keyboard_interrupt_skips_failed_status_update`` —
        the same posture that skips the status update also skips the audit
        read, so a signal-killed run does no avoidable work during teardown.
        """
        mock_landscape.side_effect = KeyboardInterrupt("ctrl-c")

        with (
            _admitted_runtime_setup(),
            patch.object(service, "_lookup_failed_node_id", side_effect=AssertionError("must not query Landscape on a signal")),
            patch("elspeth.web.execution.service.slog") as mock_slog,
            pytest.raises(KeyboardInterrupt),
        ):
            service._run_pipeline(str(uuid4()), _TEST_PIPELINE_YAML, threading.Event())

        assert [call for call in mock_slog.error.call_args_list if call.args and call.args[0] == "run_pipeline_failed"] == []


class TestStructuralFramePath:
    """``_structural_frame_path`` must never emit an absolute or unbounded path."""

    @pytest.mark.parametrize(
        ("filename", "expected"),
        [
            # Package frames render from the package root.
            ("/opt/venv/lib/python3.12/site-packages/elspeth/web/execution/service.py", "elspeth/web/execution/service.py"),
            # A checkout directory sharing the package name must not widen the path.
            ("/home/dev/elspeth/src/elspeth/engine/coalesce_executor.py", "elspeth/engine/coalesce_executor.py"),
            # Non-package frames degrade to the bare filename.
            ("/usr/lib/python3.12/json/decoder.py", "decoder.py"),
            ("/opt/venv/lib/python3.12/site-packages/sqlalchemy/engine/base.py", "base.py"),
        ],
    )
    def test_renders_bounded_relative_paths(self, filename: str, expected: str) -> None:
        from elspeth.web.execution.service import _structural_frame_path

        assert _structural_frame_path(filename) == expected

    def test_caps_components_when_anchor_over_matches(self) -> None:
        """The trailing-component cap, not the anchor, is the disclosure guarantee."""
        from elspeth.web.execution.service import _MAX_FRAME_PATH_PARTS, _structural_frame_path

        rendered = _structural_frame_path("/home/dev/elspeth/.claude/worktrees/wt/tests/unit/web/test_x.py")
        assert not rendered.startswith("/")
        assert "/home/" not in rendered
        assert ".claude" not in rendered
        assert len(rendered.split("/")) <= _MAX_FRAME_PATH_PARTS


# ── Cancel Mechanism ───────────────────────────────────────────────────


@pytest.mark.usefixtures("mock_pipeline_config_assembly")
class TestCancelMechanism:
    @pytest.mark.asyncio
    async def test_cancel_active_run_sets_event(self, service: ExecutionServiceImpl) -> None:
        run_id = uuid4()
        event = threading.Event()
        service._shutdown_events[str(run_id)] = event

        await service.cancel(run_id)

        assert event.is_set(), "cancel() must set the threading.Event so the Orchestrator detects it during row processing"

    @pytest.mark.asyncio
    async def test_get_status_marks_active_set_event_as_cancel_requested(
        self,
        service: ExecutionServiceImpl,
        mock_session_service: MagicMock,
    ) -> None:
        run_id = uuid4()
        event = threading.Event()
        event.set()
        service._shutdown_events[str(run_id)] = event
        mock_session_service.get_run.return_value = _run_record_stub(
            id=run_id,
            status="running",
            started_at=datetime.now(UTC),
            finished_at=None,
            error=None,
            landscape_run_id=None,
        )

        status = await service.get_status(run_id)

        assert status.status == "running"
        assert status.cancel_requested is True

    @pytest.mark.asyncio
    async def test_cancel_pending_run_updates_status(
        self,
        service: ExecutionServiceImpl,
        mock_session_service: MagicMock,
    ) -> None:
        """When no shutdown event exists (pending), update status directly."""
        run_id = uuid4()
        # No event in _shutdown_events — run is pending
        await service.cancel(run_id)
        mock_session_service.update_run_status.assert_called()

    @pytest.mark.parametrize("terminal_status", ["completed", "completed_with_failures", "failed", "empty", "cancelled"])
    @pytest.mark.asyncio
    async def test_cancel_terminal_run_is_noop(
        self,
        service: ExecutionServiceImpl,
        mock_session_service: MagicMock,
        terminal_status: str,
    ) -> None:
        """Cancelling any terminal run does nothing."""
        run_id = uuid4()
        mock_session_service.get_run.return_value = _run_record_stub(status=terminal_status)
        await service.cancel(run_id)
        mock_session_service.update_run_status.assert_not_called()

    @pytest.mark.asyncio
    async def test_cancel_idempotent_on_set_event(self, service: ExecutionServiceImpl) -> None:
        """Setting an already-set event is safe."""
        run_id = uuid4()
        event = threading.Event()
        event.set()
        service._shutdown_events[str(run_id)] = event

        # Should not raise
        await service.cancel(run_id)
        assert event.is_set()

    @patch("elspeth.web.execution.service.Orchestrator")
    @patch("elspeth.web.execution.service.load_settings_from_yaml_string")
    @patch("elspeth.web.execution.preflight.instantiate_plugins_from_config")
    @patch("elspeth.web.execution.preflight.ExecutionGraph")
    @patch("elspeth.web.execution.service.open_landscape_db")
    @patch("elspeth.web.execution.service.FilesystemPayloadStore")
    def test_cancelled_run_broadcasts_cancelled_event(
        self,
        mock_payload: MagicMock,
        mock_landscape: MagicMock,
        mock_graph_cls: MagicMock,
        mock_instantiate: MagicMock,
        mock_load: MagicMock,
        mock_orch_cls: MagicMock,
        service: ExecutionServiceImpl,
        mock_session_service: MagicMock,
    ) -> None:
        """When orchestrator raises GracefulShutdownError, _run_pipeline
        broadcasts 'cancelled' and updates status accordingly."""
        from elspeth.contracts.errors import GracefulShutdownError

        mock_orch = _configure_runtime_success(
            mock_load=mock_load,
            mock_instantiate=mock_instantiate,
            mock_graph_cls=mock_graph_cls,
            mock_orch_cls=mock_orch_cls,
        )
        assert mock_orch is not None
        # Orchestrator raises GracefulShutdownError on actual cancellation
        mock_orch.run.side_effect = GracefulShutdownError(
            rows_processed=50,
            run_id="test-run-001",
            rows_succeeded=48,
            rows_failed=2,
            rows_routed_success=0,
            rows_routed_failure=0,
            rows_quarantined=0,
        )

        shutdown_event = threading.Event()
        shutdown_event.set()
        run_id = str(uuid4())

        with patch(
            "elspeth.web.execution.service.load_run_accounting_from_db",
            return_value=_run_accounting_for_status(RunStatus.COMPLETED_WITH_FAILURES),
        ):
            service._run_pipeline(run_id, "source:\n  plugin: csv", shutdown_event)

        # The test sets shutdown_event BEFORE _run_pipeline, so the early
        # shutdown check (line 534) fires — no orchestrator runs, no row counts.
        # Verify status updated to "cancelled" via the early-exit path.
        status_calls = mock_session_service.update_run_status.call_args_list
        final_status_call = status_calls[-1]
        assert final_status_call.kwargs["status"] == "cancelled"

    @patch("elspeth.web.execution.service.Orchestrator")
    @patch("elspeth.web.execution.service.load_settings_from_yaml_string")
    @patch("elspeth.web.execution.preflight.instantiate_plugins_from_config")
    @patch("elspeth.web.execution.preflight.ExecutionGraph")
    @patch("elspeth.web.execution.service.open_landscape_db")
    @patch("elspeth.web.execution.service.FilesystemPayloadStore")
    def test_graceful_shutdown_forwards_row_counts(
        self,
        mock_payload: MagicMock,
        mock_landscape: MagicMock,
        mock_graph_cls: MagicMock,
        mock_instantiate: MagicMock,
        mock_load: MagicMock,
        mock_orch_cls: MagicMock,
        service: ExecutionServiceImpl,
        mock_session_service: MagicMock,
    ) -> None:
        """GracefulShutdownError row counts are forwarded to update_run_status.

        Regression: prior test only asserted status=='cancelled' but did
        not verify that rows_processed, rows_succeeded, rows_failed, and
        rows_quarantined were propagated from the GSE to the session service.
        """
        from elspeth.contracts.errors import GracefulShutdownError

        mock_orch = _configure_runtime_success(
            mock_load=mock_load,
            mock_instantiate=mock_instantiate,
            mock_graph_cls=mock_graph_cls,
            mock_orch_cls=mock_orch_cls,
        )
        assert mock_orch is not None
        mock_orch.run.side_effect = GracefulShutdownError(
            rows_processed=50,
            run_id="test-run-gse",
            rows_succeeded=48,
            rows_failed=2,
            rows_routed_success=0,
            rows_routed_failure=0,
            rows_quarantined=0,
        )

        # Do NOT set shutdown_event — let _run_pipeline proceed past the
        # early check so orchestrator.run() fires and raises the GSE.
        shutdown_event = threading.Event()
        run_id = str(uuid4())

        with patch(
            "elspeth.web.execution.service.load_run_accounting_from_db",
            return_value=_run_accounting_for_status(RunStatus.COMPLETED_WITH_FAILURES),
        ):
            service._run_pipeline(run_id, "source:\n  plugin: csv", shutdown_event)

        status_calls = mock_session_service.update_run_status.call_args_list
        # Second call is the GSE handler (first is running transition)
        gse_call = status_calls[-1]
        assert gse_call.kwargs["status"] == "cancelled"
        assert gse_call.kwargs["rows_processed"] == 50
        assert gse_call.kwargs["rows_succeeded"] == 48
        assert gse_call.kwargs["rows_failed"] == 2
        assert gse_call.kwargs["rows_routed_success"] == 0
        assert gse_call.kwargs["rows_routed_failure"] == 0
        assert gse_call.kwargs["rows_quarantined"] == 0

    @patch("elspeth.web.execution.service.Orchestrator")
    @patch("elspeth.web.execution.service.load_settings_from_yaml_string")
    @patch("elspeth.web.execution.preflight.instantiate_plugins_from_config")
    @patch("elspeth.web.execution.preflight.ExecutionGraph")
    @patch("elspeth.web.execution.service.open_landscape_db")
    @patch("elspeth.web.execution.service.FilesystemPayloadStore")
    def test_completed_run_not_misclassified_when_event_set_late(
        self,
        mock_payload: MagicMock,
        mock_landscape: MagicMock,
        mock_graph_cls: MagicMock,
        mock_instantiate: MagicMock,
        mock_load: MagicMock,
        mock_orch_cls: MagicMock,
        service: ExecutionServiceImpl,
        mock_session_service: MagicMock,
    ) -> None:
        """Race guard: if shutdown_event is set AFTER orchestrator completes
        (returns normally), the run must still be classified as 'completed',
        not 'cancelled'."""
        run_result = _orchestrator_result_stub(
            run_id="landscape-late-cancel",
            status=RunStatus.COMPLETED_WITH_FAILURES,
            rows_processed=50,
            rows_succeeded=48,
            rows_failed=2,
        )
        mock_orch = _configure_runtime_success(
            mock_load=mock_load,
            mock_instantiate=mock_instantiate,
            mock_graph_cls=mock_graph_cls,
            mock_orch_cls=mock_orch_cls,
            result=run_result,
        )
        assert mock_orch is not None

        # Simulate late cancel: event is set DURING orchestrator.run()
        # (after it returns its result), not before _run_pipeline starts.
        # This tests the race where cancel() fires after the orchestrator
        # finishes but before status is persisted.
        shutdown_event = threading.Event()

        original_return = run_result

        def set_event_on_run(*args: object, **kwargs: object) -> SimpleNamespace:
            shutdown_event.set()
            return original_return

        mock_orch.run.side_effect = set_event_on_run
        run_id = str(uuid4())

        with patch(
            "elspeth.web.execution.service.load_run_accounting_from_db",
            return_value=_run_accounting_for_status(RunStatus.COMPLETED_WITH_FAILURES),
        ):
            service._run_pipeline(run_id, "source:\n  plugin: csv", shutdown_event)

        # Must be "completed", NOT "cancelled"
        status_calls = mock_session_service.update_run_status.call_args_list
        final_status_call = status_calls[-1]
        assert "completed" in str(final_status_call), f"Expected 'completed' status update, got: {final_status_call}"

    # ── Race condition: cancel() before _run_pipeline starts ──────────

    @patch("elspeth.web.execution.service.open_landscape_db")
    @patch("elspeth.web.execution.service.FilesystemPayloadStore")
    def test_run_pipeline_exits_gracefully_when_already_cancelled(
        self,
        mock_payload: MagicMock,
        mock_landscape: MagicMock,
        service: ExecutionServiceImpl,
        mock_session_service: MagicMock,
    ) -> None:
        """Race fix: if cancel() set DB to 'cancelled' before _run_pipeline
        starts, the pending→running transition fails. _run_pipeline must
        detect this and exit cleanly — no Orchestrator, no crash."""
        run_id = str(uuid4())

        # Simulate: update_run_status("running") raises because status is "cancelled"
        mock_session_service.update_run_status.side_effect = IllegalRunTransitionError("cancelled", "running", frozenset())
        mock_session_service.get_run.return_value = _run_record_stub(status="cancelled")

        # Should NOT raise — graceful exit
        with _admitted_runtime_setup():
            service._run_pipeline(run_id, _TEST_PIPELINE_YAML, threading.Event())

        # No Orchestrator or LandscapeDB instantiated (early return)
        mock_landscape.assert_not_called()
        mock_payload.assert_not_called()

        # Only the one failed "running" attempt — no "failed" status update
        assert mock_session_service.update_run_status.call_count == 1

    @patch("elspeth.web.execution.service.open_landscape_db")
    @patch("elspeth.web.execution.service.FilesystemPayloadStore")
    def test_run_pipeline_early_shutdown_skips_setup(
        self,
        mock_payload: MagicMock,
        mock_landscape: MagicMock,
        service: ExecutionServiceImpl,
        mock_session_service: MagicMock,
    ) -> None:
        """If shutdown_event is already set when _run_pipeline starts,
        skip all setup and immediately transition to cancelled."""
        run_id = str(uuid4())

        shutdown_event = threading.Event()
        shutdown_event.set()

        service._run_pipeline(run_id, "source:\n  plugin: csv", shutdown_event)

        # No LandscapeDB or PayloadStore constructed (skipped setup)
        mock_landscape.assert_not_called()
        mock_payload.assert_not_called()

        # Status updated to "cancelled"
        status_calls = mock_session_service.update_run_status.call_args_list
        assert len(status_calls) == 1
        assert "cancelled" in str(status_calls[0])

    @patch("elspeth.web.execution.service.open_landscape_db")
    @patch("elspeth.web.execution.service.FilesystemPayloadStore")
    def test_run_pipeline_reraises_valueerror_when_not_cancelled(
        self,
        mock_payload: MagicMock,
        mock_landscape: MagicMock,
        service: ExecutionServiceImpl,
        mock_session_service: MagicMock,
    ) -> None:
        """If update_run_status raises ValueError for a reason other than
        'already cancelled', _run_pipeline must re-raise (offensive programming)."""
        run_id = str(uuid4())

        mock_session_service.update_run_status.side_effect = IllegalRunTransitionError("completed", "running", frozenset())
        mock_session_service.get_run.return_value = _run_record_stub(status="completed")

        with _admitted_runtime_setup(), pytest.raises(ValueError, match="completed"):
            service._run_pipeline(run_id, _TEST_PIPELINE_YAML, threading.Event())

    @patch("elspeth.web.execution.service.open_landscape_db")
    @patch("elspeth.web.execution.service.FilesystemPayloadStore")
    def test_running_transition_does_not_swallow_non_illegal_value_errors(
        self,
        mock_payload: MagicMock,
        mock_landscape: MagicMock,
        service: ExecutionServiceImpl,
        mock_session_service: MagicMock,
    ) -> None:
        """Tier-1 invariant: the four non-illegal-transition ValueError sites in
        update_run_status (run-not-found, landscape_run_id overwrite,
        completed-without-landscape, failed-without-error) must NOT be caught by
        the cancelled-race recovery at the running-transition site (originally
        684).  The only catchable class is IllegalRunTransitionError.

        Discriminator (regression-resistant): under the *old* broad
        ``except ValueError`` the cancelled-race recovery would (a) consult
        get_run, (b) see status == "cancelled", (c) broadcast a "cancelled" SSE
        event, and (d) ``return`` silently — masking the Tier-1 breach as a
        normal cancellation.  The bare ValueError would never propagate.

        Under the narrowed catch, the bare ValueError propagates verbatim
        (preserving the original message), no "cancelled" SSE is emitted from
        the cancelled-race path, and the only get_run call comes from the
        downstream BaseException post-terminal recovery (separate audit-primacy
        machinery — its get_run is correct and expected).

        Without this test a future maintainer could re-widen
        ``except IllegalRunTransitionError`` back to ``except ValueError`` and
        the recovery-path tests would still pass.
        """
        run_id = str(uuid4())

        sentinel_message = "landscape_run_id already set to 'sentinel-existing-id'; cannot overwrite"
        mock_session_service.update_run_status.side_effect = ValueError(sentinel_message)
        mock_session_service.get_run.return_value = _run_record_stub(status="cancelled")

        broadcast_calls: list[tuple[str, Any]] = []
        original_broadcast = service._broadcaster.broadcast

        def spy_broadcast(rid: str, event: Any) -> None:
            broadcast_calls.append((rid, event))
            original_broadcast(rid, event)

        service._broadcaster.broadcast = spy_broadcast  # type: ignore[assignment]

        with _admitted_runtime_setup(), pytest.raises(ValueError, match="sentinel-existing-id") as exc_info:
            service._run_pipeline(run_id, _TEST_PIPELINE_YAML, threading.Event())

        # Discriminator #1: the propagating exception is bare ValueError, not the
        # narrow subclass — proves the catch did not match.
        assert not isinstance(exc_info.value, IllegalRunTransitionError)
        # Discriminator #2: no "cancelled" SSE was broadcast.  Old broad-catch
        # behaviour would emit one before silently returning.
        cancelled_events = [event for (_, event) in broadcast_calls if event.event_type == "cancelled"]
        assert cancelled_events == [], f"unexpected cancelled SSE — masking window re-opened: {cancelled_events}"

    @pytest.mark.asyncio
    async def test_shutdown_event_registered_before_blob_linkage(
        self,
        service: ExecutionServiceImpl,
        mock_session_service: MagicMock,
    ) -> None:
        """Race fix part 2: _shutdown_events registration must happen before
        blob linkage, so cancel() finds the event during the blob window."""
        session_id = uuid4()
        run_id = uuid4()
        blob_ref = str(uuid4())
        canonical_path = f"/tmp/data/blobs/{session_id}/{blob_ref}_input.csv"
        mock_session_service.create_run.return_value = _run_record_stub(id=run_id)

        blob_service = _blob_service_stub()
        blob_service.get_blob.return_value = _ready_csv_blob_for_execution(
            blob_ref=blob_ref,
            session_id=session_id,
            storage_path=canonical_path,
        )

        async def tracking_link(*args: Any, **kwargs: Any) -> None:
            # At the time blob linkage runs, the event MUST already exist
            assert str(run_id) in service._shutdown_events, "RACE: _shutdown_events not registered before blob linkage"

        blob_service.link_blob_to_run.side_effect = tracking_link
        cast(Any, service)._blob_service = blob_service

        # Set up state record with a source containing a blob_ref.
        # Use a real dict so state_from_record → deep_thaw works correctly.
        # path must equal blob.storage_path to satisfy the Tier 1 read
        # guard for blob-backed sources.
        state = mock_session_service.get_current_state.return_value
        state.source = {
            "plugin": "csv",
            "on_success": "continue",
            "options": {"blob_ref": blob_ref, "path": canonical_path},
            "on_validation_failure": "quarantine",
        }

        with patch.object(service, "_run_pipeline"):
            await service.execute(session_id=session_id)

    @pytest.mark.asyncio
    async def test_shutdown_event_cleaned_up_on_blob_linkage_failure(
        self,
        service: ExecutionServiceImpl,
        mock_session_service: MagicMock,
    ) -> None:
        """If blob linkage raises after event registration, the event must
        be cleaned up to avoid leaking into _shutdown_events."""
        session_id = uuid4()
        run_id = uuid4()
        blob_ref = str(uuid4())
        canonical_path = f"/tmp/data/blobs/{session_id}/{blob_ref}_input.csv"
        mock_session_service.create_run.return_value = _run_record_stub(id=run_id)

        blob_service = _blob_service_stub()
        blob_service.get_blob.return_value = _ready_csv_blob_for_execution(
            blob_ref=blob_ref,
            session_id=session_id,
            storage_path=canonical_path,
        )
        blob_service.link_blob_to_run.side_effect = RuntimeError("blob storage unavailable")
        cast(Any, service)._blob_service = blob_service

        # Use a real dict so state_from_record → deep_thaw works correctly.
        # path must equal blob.storage_path to satisfy the Tier 1 read
        # guard for blob-backed sources.
        state = mock_session_service.get_current_state.return_value
        state.source = {
            "plugin": "csv",
            "on_success": "continue",
            "options": {"blob_ref": blob_ref, "path": canonical_path},
            "on_validation_failure": "quarantine",
        }

        with pytest.raises(RuntimeError, match="blob storage unavailable"):
            await service.execute(session_id=session_id)

        assert str(run_id) not in service._shutdown_events


class TestP2aCleanupCatchNarrowing:
    """Regression (P2a): cleanup catches in ExecutionServiceImpl must not
    launder exception strings into slog.

    ``except Exception`` over ``update_run_status`` previously logged
    ``cleanup_error=str(cleanup_err)``. On SQLAlchemyError subclasses that
    expands to ``[SQL: ...] [parameters: ...]`` plus a ``__cause__`` chain
    that can carry DB URLs / credentials. Canonical pattern (commits
    b8ba2214/127417cb): narrow to ``(SQLAlchemyError, OSError)`` and log
    ``exc_class`` only.
    """

    @pytest.mark.asyncio
    async def test_setup_failure_cleanup_slog_uses_exc_class_not_str(
        self,
        service: ExecutionServiceImpl,
        mock_session_service: MagicMock,
    ) -> None:
        """When a setup failure triggers cleanup and cleanup's own
        ``update_run_status`` raises a ``SQLAlchemyError``, the slog
        record must carry ``cleanup_exc_class`` + ``original_exc_class``
        (class names) — not the legacy ``cleanup_error``/``original_error``
        string fields."""
        from sqlalchemy.exc import OperationalError

        session_id = uuid4()
        run_id = uuid4()
        mock_session_service.create_run.return_value = _run_record_stub(id=run_id)

        # First update_run_status call (in cleanup) raises OperationalError.
        mock_session_service.update_run_status.side_effect = OperationalError(
            "UPDATE runs ...",
            {"id": str(run_id), "error": "Setup failed: SuperSecretDSN://u:p@h/d"},
            Exception("lock wait timeout exceeded — __cause__ carries DSN"),
        )

        # Force the setup path to fail so the cleanup catch fires.
        # _executor.submit raising is the simplest route.
        def submit_raises(*_args: Any, **_kwargs: Any) -> Future[Any]:
            raise RuntimeError("pool shutdown")

        service._executor.submit = submit_raises  # type: ignore[method-assign]

        with (
            patch("elspeth.web.execution.service.slog") as mock_slog,
            pytest.raises(RuntimeError, match="pool shutdown"),
        ):
            await service.execute(session_id=session_id)

        # slog.error was called for the cleanup failure.
        slog_calls = [c for c in mock_slog.error.call_args_list if c[0] and c[0][0] == "run_cleanup_status_update_failed"]
        assert len(slog_calls) == 1, mock_slog.error.call_args_list
        kwargs = slog_calls[0][1]

        # The narrow-catch kwargs are class names, not strings.
        assert kwargs["cleanup_exc_class"] == "OperationalError"
        assert kwargs["original_exc_class"] == "RuntimeError"

        # Legacy string-valued fields are GONE — this is the redaction
        # regression guard. Any reintroduction re-opens the str(exc) leak.
        assert "cleanup_error" not in kwargs
        assert "original_error" not in kwargs

    @pytest.mark.asyncio
    async def test_setup_cleanup_narrow_catch_lets_runtimeerror_escape(
        self,
        service: ExecutionServiceImpl,
        mock_session_service: MagicMock,
    ) -> None:
        """Narrow catch semantics: a RuntimeError from update_run_status
        (programmer bug, not a DB/filesystem failure) MUST propagate
        instead of being swallowed. Pre-narrowing, the broad
        ``except Exception`` masked such bugs."""
        session_id = uuid4()
        run_id = uuid4()
        mock_session_service.create_run.return_value = _run_record_stub(id=run_id)

        # First update_run_status (in cleanup) raises RuntimeError — outside
        # the narrow (SQLAlchemyError, OSError) catch. It must escape.
        mock_session_service.update_run_status.side_effect = RuntimeError("dataclass contract violated inside update_run_status")

        # Force setup to fail so cleanup fires.
        def submit_raises(*_args: Any, **_kwargs: Any) -> Future[Any]:
            raise RuntimeError("pool shutdown")

        service._executor.submit = submit_raises  # type: ignore[method-assign]

        # The RuntimeError from update_run_status escapes the narrow catch.
        # The outer `raise` is bypassed — the cleanup RuntimeError wins
        # (Python's implicit exception chaining preserves both via
        # __context__, but the foreground exception is the cleanup one).
        # We accept either RuntimeError here — the key invariant is that
        # a RuntimeError propagates rather than being swallowed.
        with pytest.raises(RuntimeError):
            await service.execute(session_id=session_id)


# ── Completion-Path Guard ─────────────────────────────────────────────


@pytest.mark.usefixtures("mock_pipeline_config_assembly")
class TestCompletionPathExternalCancellation:
    """Defence-in-depth: if the DB says 'cancelled' when _run_pipeline
    tries to write 'completed', exit gracefully — no 'failed' broadcast,
    no BaseException cascade, no re-raise."""

    @patch("elspeth.web.execution.service.Orchestrator")
    @patch("elspeth.web.execution.preflight.ExecutionGraph")
    @patch("elspeth.web.execution.preflight.instantiate_plugins_from_config")
    @patch("elspeth.web.execution.service.load_settings_from_yaml_string")
    @patch("elspeth.web.execution.service.open_landscape_db")
    @patch("elspeth.web.execution.service.FilesystemPayloadStore")
    def test_run_pipeline_exits_gracefully_when_completed_but_db_cancelled(
        self,
        mock_payload: MagicMock,
        mock_landscape: MagicMock,
        mock_load: MagicMock,
        mock_instantiate: MagicMock,
        mock_graph_cls: MagicMock,
        mock_orch_cls: MagicMock,
        service: ExecutionServiceImpl,
        mock_session_service: MagicMock,
    ) -> None:
        """Pipeline completes, but orphan cleanup already set DB to 'cancelled'.
        _run_pipeline must detect this and return cleanly."""
        _configure_runtime_success(
            mock_load=mock_load,
            mock_instantiate=mock_instantiate,
            mock_graph_cls=mock_graph_cls,
            mock_orch_cls=mock_orch_cls,
            result=_orchestrator_result_stub(
                run_id="landscape-run-123",
                status=RunStatus.COMPLETED_WITH_FAILURES,
                rows_processed=100,
                rows_succeeded=95,
                rows_failed=5,
            ),
        )

        run_id = str(uuid4())

        # First call: update_run_status("running") succeeds.
        # Second call: update_run_status("completed") raises ValueError
        # because the DB was externally set to "cancelled".
        call_count = 0

        async def status_side_effect(*args: Any, **kwargs: Any) -> None:
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                raise IllegalRunTransitionError("cancelled", "completed", frozenset())

        mock_session_service.update_run_status.side_effect = status_side_effect
        mock_session_service.get_run.return_value = _run_record_stub(status="cancelled")

        # Should NOT raise — graceful exit
        service._run_pipeline(run_id, "source:\n  plugin: csv", threading.Event())

        # The "failed" path should NOT have been entered: check that
        # update_run_status was called exactly twice (running + completed),
        # NOT three times (running + completed + failed).
        assert mock_session_service.update_run_status.call_count == 2

    @patch("elspeth.web.execution.service.Orchestrator")
    @patch("elspeth.web.execution.preflight.ExecutionGraph")
    @patch("elspeth.web.execution.preflight.instantiate_plugins_from_config")
    @patch("elspeth.web.execution.service.load_settings_from_yaml_string")
    @patch("elspeth.web.execution.service.open_landscape_db")
    @patch("elspeth.web.execution.service.FilesystemPayloadStore")
    def test_cancelled_compensating_event_broadcast_on_external_cancel(
        self,
        mock_payload: MagicMock,
        mock_landscape: MagicMock,
        mock_load: MagicMock,
        mock_instantiate: MagicMock,
        mock_graph_cls: MagicMock,
        mock_orch_cls: MagicMock,
        service: ExecutionServiceImpl,
        mock_session_service: MagicMock,
    ) -> None:
        """When pipeline completes but DB says 'cancelled', exactly one
        terminal event must be broadcast: 'cancelled' (the DB is authoritative).
        No 'completed' event should be emitted — finalize-first ordering
        ensures the terminal broadcast reflects the actual DB state."""
        _configure_runtime_success(
            mock_load=mock_load,
            mock_instantiate=mock_instantiate,
            mock_graph_cls=mock_graph_cls,
            mock_orch_cls=mock_orch_cls,
            result=_orchestrator_result_stub(
                run_id="landscape-run-789",
                status=RunStatus.COMPLETED_WITH_FAILURES,
                rows_processed=100,
                rows_succeeded=95,
                rows_failed=5,
            ),
        )

        run_id = str(uuid4())

        call_count = 0

        async def status_side_effect(*args: Any, **kwargs: Any) -> None:
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                raise IllegalRunTransitionError("cancelled", "completed", frozenset())

        mock_session_service.update_run_status.side_effect = status_side_effect
        mock_session_service.get_run.return_value = _run_record_stub(status="cancelled")

        broadcast_calls: list[tuple[str, Any]] = []
        original_broadcast = service._broadcaster.broadcast

        def spy_broadcast(rid: str, event: Any) -> None:
            broadcast_calls.append((rid, event))
            original_broadcast(rid, event)

        service._broadcaster.broadcast = spy_broadcast  # type: ignore[assignment]

        service._run_pipeline(run_id, "source:\n  plugin: csv", threading.Event())

        event_types = [call[1].event_type for call in broadcast_calls]
        terminal_types = [et for et in event_types if et in ("completed", "failed", "cancelled")]
        assert terminal_types == ["cancelled"], f"Expected exactly one 'cancelled' terminal, got: {terminal_types}"

    @patch("elspeth.web.execution.service.Orchestrator")
    @patch("elspeth.web.execution.preflight.ExecutionGraph")
    @patch("elspeth.web.execution.preflight.instantiate_plugins_from_config")
    @patch("elspeth.web.execution.service.load_settings_from_yaml_string")
    @patch("elspeth.web.execution.service.open_landscape_db")
    @patch("elspeth.web.execution.service.FilesystemPayloadStore")
    def test_external_cancel_finalizes_output_blobs_as_error(
        self,
        mock_payload: MagicMock,
        mock_landscape: MagicMock,
        mock_load: MagicMock,
        mock_instantiate: MagicMock,
        mock_graph_cls: MagicMock,
        mock_orch_cls: MagicMock,
        service: ExecutionServiceImpl,
        mock_session_service: MagicMock,
    ) -> None:
        """Cancelled runs must not leave output blobs finalized as ready."""
        from elspeth.web.blobs.protocol import BlobFinalizationResult

        _configure_runtime_success(
            mock_load=mock_load,
            mock_instantiate=mock_instantiate,
            mock_graph_cls=mock_graph_cls,
            mock_orch_cls=mock_orch_cls,
            result=_orchestrator_result_stub(
                run_id="landscape-run-blob-cancel",
                rows_processed=7,
                rows_succeeded=7,
            ),
        )

        blob_state = {"status": "pending"}
        blob_calls: list[bool] = []

        async def finalize_run_output_blobs(run_id: UUID, success: bool) -> BlobFinalizationResult:
            del run_id
            blob_calls.append(success)
            if blob_state["status"] == "pending":
                blob_state["status"] = "ready" if success else "error"
            return BlobFinalizationResult(finalized=[], errors=[])

        blob_service = _blob_service_stub()
        blob_service.finalize_run_output_blobs.side_effect = finalize_run_output_blobs
        cast(Any, service)._blob_service = blob_service

        async def status_side_effect(*args: Any, **kwargs: Any) -> None:
            if kwargs.get("status") == "completed":
                raise IllegalRunTransitionError("cancelled", "completed", frozenset())

        mock_session_service.update_run_status.side_effect = status_side_effect
        mock_session_service.get_run.return_value = _run_record_stub(status="cancelled")

        with patch(
            "elspeth.web.execution.service.load_run_accounting_from_db",
            return_value=_run_accounting_for_status(RunStatus.COMPLETED),
        ):
            service._run_pipeline(str(uuid4()), "source:\n  plugin: csv", threading.Event())

        assert blob_calls == [False]
        assert blob_state["status"] == "error"

    @patch("elspeth.web.execution.service.Orchestrator")
    @patch("elspeth.web.execution.preflight.ExecutionGraph")
    @patch("elspeth.web.execution.preflight.instantiate_plugins_from_config")
    @patch("elspeth.web.execution.service.load_settings_from_yaml_string")
    @patch("elspeth.web.execution.service.open_landscape_db")
    @patch("elspeth.web.execution.service.FilesystemPayloadStore")
    def test_completion_guard_reraises_for_non_cancelled_status(
        self,
        mock_payload: MagicMock,
        mock_landscape: MagicMock,
        mock_load: MagicMock,
        mock_instantiate: MagicMock,
        mock_graph_cls: MagicMock,
        mock_orch_cls: MagicMock,
        service: ExecutionServiceImpl,
        mock_session_service: MagicMock,
    ) -> None:
        """If update_run_status('completed') raises ValueError for a reason
        other than 'already cancelled', the error must propagate (offensive)."""
        _configure_runtime_success(
            mock_load=mock_load,
            mock_instantiate=mock_instantiate,
            mock_graph_cls=mock_graph_cls,
            mock_orch_cls=mock_orch_cls,
            result=_orchestrator_result_stub(run_id="landscape-run-456"),
        )

        run_id = str(uuid4())

        call_count = 0

        async def status_side_effect(*args: Any, **kwargs: Any) -> None:
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                raise IllegalRunTransitionError("completed", "completed", frozenset())

        mock_session_service.update_run_status.side_effect = status_side_effect
        # DB says "completed" (not "cancelled") — this should re-raise
        mock_session_service.get_run.return_value = _run_record_stub(status="completed")

        with pytest.raises(ValueError, match="completed"):
            service._run_pipeline(run_id, "source:\n  plugin: csv", threading.Event())

    @patch("elspeth.web.execution.service.Orchestrator")
    @patch("elspeth.web.execution.preflight.ExecutionGraph")
    @patch("elspeth.web.execution.preflight.instantiate_plugins_from_config")
    @patch("elspeth.web.execution.service.load_settings_from_yaml_string")
    @patch("elspeth.web.execution.service.open_landscape_db")
    @patch("elspeth.web.execution.service.FilesystemPayloadStore")
    def test_completion_guard_does_not_swallow_non_illegal_value_errors(
        self,
        mock_payload: MagicMock,
        mock_landscape: MagicMock,
        mock_load: MagicMock,
        mock_instantiate: MagicMock,
        mock_graph_cls: MagicMock,
        mock_orch_cls: MagicMock,
        service: ExecutionServiceImpl,
        mock_session_service: MagicMock,
    ) -> None:
        """Tier-1 invariant for the completion-transition catch (originally
        ~915): a bare ValueError raised for a non-illegal-transition reason
        (run-not-found, landscape_run_id overwrite, completed-without-landscape,
        failed-without-error) must propagate verbatim and must NOT trigger the
        cancelled-race silent-swallow path.

        Discriminator (regression-resistant): under the old broad
        ``except ValueError`` the cancelled-race recovery would (a) consult
        get_run, (b) see status == "cancelled", (c) broadcast a "cancelled"
        SSE event, and (d) ``return`` silently — masking the Tier-1 breach as
        a normal cancellation.  The bare ValueError would never propagate.

        Under the narrowed catch the bare ValueError propagates and no
        "cancelled" SSE is emitted from the cancelled-race path.  (The
        downstream BaseException post-terminal recovery may consult get_run
        as part of separate audit-primacy machinery; that's expected and
        correct, so we assert on broadcast shape rather than get_run call
        count.)

        Without this test a future widening of
        ``except IllegalRunTransitionError`` back to ``except ValueError`` would
        silently re-open the masking window identified by silent-failure-hunter
        (H1) — and the existing recovery-path tests would still pass.
        """
        _configure_runtime_success(
            mock_load=mock_load,
            mock_instantiate=mock_instantiate,
            mock_graph_cls=mock_graph_cls,
            mock_orch_cls=mock_orch_cls,
            result=_orchestrator_result_stub(run_id="landscape-run-sentinel"),
        )

        run_id = str(uuid4())
        call_count = 0
        sentinel_message = "landscape_run_id already set to 'sentinel-existing-id'; cannot overwrite"

        async def status_side_effect(*args: Any, **kwargs: Any) -> None:
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                # Bare ValueError simulating one of the four non-illegal-transition
                # invariant breaches in update_run_status.
                raise ValueError(sentinel_message)

        mock_session_service.update_run_status.side_effect = status_side_effect
        mock_session_service.get_run.return_value = _run_record_stub(status="cancelled")

        broadcast_calls: list[tuple[str, Any]] = []
        original_broadcast = service._broadcaster.broadcast

        def spy_broadcast(rid: str, event: Any) -> None:
            broadcast_calls.append((rid, event))
            original_broadcast(rid, event)

        service._broadcaster.broadcast = spy_broadcast  # type: ignore[assignment]

        with pytest.raises(ValueError, match="sentinel-existing-id") as exc_info:
            service._run_pipeline(run_id, "source:\n  plugin: csv", threading.Event())

        # Discriminator #1: bare ValueError, not the narrow subclass.
        assert not isinstance(exc_info.value, IllegalRunTransitionError)
        # Discriminator #2: no "cancelled" SSE — old broad-catch behaviour
        # would emit one before silently returning.
        cancelled_events = [event for (_, event) in broadcast_calls if event.event_type == "cancelled"]
        assert cancelled_events == [], f"unexpected cancelled SSE — masking window re-opened: {cancelled_events}"


# ── Post-Completion Exception Recovery (elspeth-879f6de6bd) ───────────


@pytest.mark.usefixtures("mock_pipeline_config_assembly")
class TestPostCompletionExceptionRecovery:
    """Defence-in-depth: when a BaseException fires AFTER ``update_run_status``
    has already committed a terminal state, the recovery must not attempt an
    illegal terminal→failed transition.

    elspeth-879f6de6bd: ``LEGAL_RUN_TRANSITIONS`` makes all five terminal
    statuses (``completed``, ``completed_with_failures``, ``failed``,
    ``empty``, ``cancelled``) outgoing-empty.  The pre-fix recovery at
    ``_run_pipeline``'s ``except BaseException`` handler attempted
    ``update_run_status(status="failed", ...)`` unconditionally, raising
    ``ValueError("Illegal run transition: 'completed' → 'failed'. Allowed: []")``
    and losing the original exception in ``__context__``.

    The fix consults ``get_run`` first; if the run is already terminal it
    skips both the status update and the misleading ``failed`` SSE broadcast
    (audit primacy: SSE must not contradict the audit row).
    """

    @staticmethod
    def _make_completed_orchestrator(status: RunStatus = RunStatus.COMPLETED) -> MagicMock:
        """Build an orchestrator stub whose ``run`` returns a terminal result.

        Counts are status-aware so the SSE-payload status/count cross-consistency
        validator (``CompletedData._check_status_consistency``) accepts the
        result.  COMPLETED_WITH_FAILURES requires both a success and a failure
        indicator; EMPTY requires zero rows; COMPLETED requires success only;
        FAILED tolerates any shape.
        """
        if status == RunStatus.COMPLETED_WITH_FAILURES:
            result = _orchestrator_result_stub(
                run_id="landscape-run-postcompletion",
                status=status,
                rows_processed=10,
                rows_succeeded=8,
                rows_failed=2,
            )
        elif status == RunStatus.EMPTY:
            result = _orchestrator_result_stub(
                run_id="landscape-run-postcompletion",
                status=status,
                rows_processed=0,
                rows_succeeded=0,
                rows_failed=0,
            )
        else:
            result = _orchestrator_result_stub(
                run_id="landscape-run-postcompletion",
                status=status,
                rows_processed=10,
                rows_succeeded=10,
                rows_failed=0,
            )
        return _orchestrator_stub(result)

    @staticmethod
    def _wrap_broadcaster_to_raise(
        service: ExecutionServiceImpl,
        *,
        on_event_type: str,
        exc: BaseException,
    ) -> list[tuple[str, Any]]:
        """Replace the broadcaster with a spy that records all calls and raises
        ``exc`` when the broadcast event_type matches ``on_event_type``.

        Returns the list that will be appended to as broadcasts occur.  Tests
        can assert on event_types after ``_run_pipeline`` returns.
        """
        original_broadcast = service._broadcaster.broadcast
        broadcast_calls: list[tuple[str, Any]] = []

        def crashing_broadcast(rid: str, event: Any) -> None:
            broadcast_calls.append((rid, event))
            if event.event_type == on_event_type:
                raise exc
            original_broadcast(rid, event)

        service._broadcaster.broadcast = crashing_broadcast  # type: ignore[assignment]
        return broadcast_calls

    @patch("elspeth.web.execution.service.Orchestrator")
    @patch("elspeth.web.execution.preflight.ExecutionGraph")
    @patch("elspeth.web.execution.preflight.instantiate_plugins_from_config")
    @patch("elspeth.web.execution.service.load_settings_from_yaml_string")
    @patch("elspeth.web.execution.service.open_landscape_db")
    @patch("elspeth.web.execution.service.FilesystemPayloadStore")
    def test_post_completion_broadcast_crash_skips_failed_status_update(
        self,
        mock_payload: MagicMock,
        mock_landscape: MagicMock,
        mock_load: MagicMock,
        mock_instantiate: MagicMock,
        mock_graph_cls: MagicMock,
        mock_orch_cls: MagicMock,
        service: ExecutionServiceImpl,
        mock_session_service: MagicMock,
    ) -> None:
        """Run completes; success-path ``broadcast("completed", ...)`` raises
        ``RuntimeError``.  The recovery must NOT attempt a third
        ``update_run_status(status="failed", ...)`` because the audit row is
        already ``completed`` (illegal terminal→failed transition).
        """
        _configure_runtime_success(
            mock_load=mock_load,
            mock_instantiate=mock_instantiate,
            mock_graph_cls=mock_graph_cls,
            mock_orch_cls=mock_orch_cls,
            result=self._make_completed_orchestrator(RunStatus.COMPLETED).run.return_value,
        )

        # update_run_status is permissive (just records calls); the audit row
        # is conceptually "completed" after the second call.  The recovery's
        # third call (with status="failed") MUST NOT happen.
        mock_session_service.get_run.return_value = _run_record_stub(status="completed")

        broadcast_calls = self._wrap_broadcaster_to_raise(
            service,
            on_event_type="completed",
            exc=RuntimeError("simulated SSE crash"),
        )

        run_id = str(uuid4())
        with (
            patch("elspeth.web.execution.service.slog") as mock_slog,
            patch(
                "elspeth.web.execution.service.load_run_accounting_from_db",
                return_value=_run_accounting_for_status(RunStatus.COMPLETED),
            ),
            pytest.raises(RuntimeError, match="simulated SSE crash"),
        ):
            service._run_pipeline(run_id, "source:\n  plugin: csv", threading.Event())

        # Exactly two status updates: "running" (line 650) and the terminal
        # "completed" (line 857).  No third "failed" call.
        statuses = [c.kwargs.get("status") for c in mock_session_service.update_run_status.call_args_list]
        assert statuses == ["running", "completed"], f"Expected [running, completed], got {statuses}"

        # Audit-primacy guarantees enforced at this site:
        #   1. No third update_run_status (asserted above).
        #   2. No "failed" SSE broadcast (asserted below) — would contradict
        #      the audit row's true terminal status.
        # Post-audit-exception observability is provided by three channels
        # (see service.py post-terminal-exception comment block):
        #   - The audit ``runs`` row (status="completed" — verified by the
        #     ``statuses`` assertion above by transitive proof).
        #   - ``run_pipeline_failed`` — emitted at the TOP of the except
        #     handler, before the terminality split, so it carries the
        #     diagnostic even here.  Pinned by
        #     ``test_diagnostic_log_fires_even_when_audit_row_is_already_terminal``.
        #   - ``_on_pipeline_done``'s safety-net slog
        #     (``pipeline_done_callback_exception``) — fires against the
        #     re-raised exc once the Future completes.  Tested separately
        #     in the _on_pipeline_done test class.
        # This test must NOT pin a slog at the post-terminal-exception
        # branch itself: per ``logging-telemetry-policy`` the logger is
        # not the correct surface for post-audit operational signal.
        post_terminal_logs = [
            c for c in mock_slog.error.call_args_list if c.args and c.args[0] == "post_terminal_exception_in_run_pipeline"
        ]
        assert post_terminal_logs == [], (
            "post_terminal_exception_in_run_pipeline slog has been removed "
            "(audit-primacy fix); the post-audit signal is captured by the "
            "audit row + _on_pipeline_done safety-net log."
        )

        # No "failed" SSE event must be emitted from the recovery — the run
        # actually completed; broadcasting "failed" would diverge from audit.
        recovery_failed_events = [event for (_, event) in broadcast_calls if event.event_type == "failed"]
        assert recovery_failed_events == [], f"Recovery must not broadcast 'failed' when run is terminal; got: {recovery_failed_events}"

    @patch("elspeth.web.execution.service.Orchestrator")
    @patch("elspeth.web.execution.preflight.ExecutionGraph")
    @patch("elspeth.web.execution.preflight.instantiate_plugins_from_config")
    @patch("elspeth.web.execution.service.load_settings_from_yaml_string")
    @patch("elspeth.web.execution.service.open_landscape_db")
    @patch("elspeth.web.execution.service.FilesystemPayloadStore")
    def test_post_completion_with_failures_also_skips_failed_status_update(
        self,
        mock_payload: MagicMock,
        mock_landscape: MagicMock,
        mock_load: MagicMock,
        mock_instantiate: MagicMock,
        mock_graph_cls: MagicMock,
        mock_orch_cls: MagicMock,
        service: ExecutionServiceImpl,
        mock_session_service: MagicMock,
    ) -> None:
        """Regression catcher for the partial-tuple bug pattern.

        Several call sites in the codebase use a hardcoded
        ``("completed", "failed", "cancelled")`` tuple to detect terminality,
        which omits ``completed_with_failures`` and ``empty``.  The fix MUST
        use the full terminal set derived from ``LEGAL_RUN_TRANSITIONS`` /
        ``SESSION_TERMINAL_RUN_STATUS_VALUES``.  This test proves the guard
        fires for ``completed_with_failures`` — a state the partial tuple
        would miss.
        """
        _configure_runtime_success(
            mock_load=mock_load,
            mock_instantiate=mock_instantiate,
            mock_graph_cls=mock_graph_cls,
            mock_orch_cls=mock_orch_cls,
            result=self._make_completed_orchestrator(RunStatus.COMPLETED_WITH_FAILURES).run.return_value,
        )

        mock_session_service.get_run.return_value = _run_record_stub(status="completed_with_failures")

        self._wrap_broadcaster_to_raise(
            service,
            on_event_type="completed",
            exc=RuntimeError("simulated SSE crash"),
        )

        run_id = str(uuid4())
        with (
            patch("elspeth.web.execution.service.slog") as mock_slog,
            patch(
                "elspeth.web.execution.service.load_run_accounting_from_db",
                return_value=_run_accounting_for_status(RunStatus.COMPLETED_WITH_FAILURES),
            ),
            pytest.raises(RuntimeError),
        ):
            service._run_pipeline(run_id, "source:\n  plugin: csv", threading.Event())

        statuses = [c.kwargs.get("status") for c in mock_session_service.update_run_status.call_args_list]
        assert statuses == ["running", "completed_with_failures"], f"Expected [running, completed_with_failures], got {statuses}"

        # Audit-primacy fix: the post-audit slog has been removed from
        # this site.  See test_post_completion_broadcast_crash_skips_failed_status_update
        # for the rationale; the assertion here pins the same invariant for
        # the COMPLETED_WITH_FAILURES branch (the non-completed branch the
        # partial-tuple bug pattern would have missed).
        post_terminal_logs = [
            c for c in mock_slog.error.call_args_list if c.args and c.args[0] == "post_terminal_exception_in_run_pipeline"
        ]
        assert post_terminal_logs == [], "post_terminal_exception_in_run_pipeline slog has been removed (audit-primacy fix)."

    @patch("elspeth.web.execution.service.Orchestrator")
    @patch("elspeth.web.execution.preflight.ExecutionGraph")
    @patch("elspeth.web.execution.preflight.instantiate_plugins_from_config")
    @patch("elspeth.web.execution.service.load_settings_from_yaml_string")
    @patch("elspeth.web.execution.service.open_landscape_db")
    @patch("elspeth.web.execution.service.FilesystemPayloadStore")
    def test_post_completion_get_run_probe_failure_falls_through(
        self,
        mock_payload: MagicMock,
        mock_landscape: MagicMock,
        mock_load: MagicMock,
        mock_instantiate: MagicMock,
        mock_graph_cls: MagicMock,
        mock_orch_cls: MagicMock,
        service: ExecutionServiceImpl,
        mock_session_service: MagicMock,
    ) -> None:
        """When ``get_run`` itself raises during the recovery probe, fall
        through to the existing best-effort recovery (attempt
        ``update_run_status("failed", ...)``).

        Documents a **known gap**: this test exercises the fall-through with
        a permissive ``update_run_status`` mock that does NOT enforce
        ``LEGAL_RUN_TRANSITIONS``.  In production, if the run is genuinely
        terminal AND the probe fails AND ``update_run_status`` therefore
        raises ``ValueError``, the original exception is still lost — same
        as today's behaviour.  Closing that gap requires a larger design
        change (fail-closed on probe failure, or post-hoc reconcile of the
        ValueError) and is out of scope for elspeth-879f6de6bd.
        """
        _configure_runtime_success(
            mock_load=mock_load,
            mock_instantiate=mock_instantiate,
            mock_graph_cls=mock_graph_cls,
            mock_orch_cls=mock_orch_cls,
            result=self._make_completed_orchestrator(RunStatus.COMPLETED).run.return_value,
        )

        # Probe raises a generic SQLAlchemy-family-ish error.  Use the actual
        # SQLAlchemyError to match the recovery's narrow catch.
        mock_session_service.get_run.side_effect = SQLAlchemyError("simulated DB hiccup")

        self._wrap_broadcaster_to_raise(
            service,
            on_event_type="completed",
            exc=RuntimeError("simulated SSE crash"),
        )

        run_id = str(uuid4())
        with (
            patch("elspeth.web.execution.service.slog") as mock_slog,
            patch(
                "elspeth.web.execution.service.load_run_accounting_from_db",
                return_value=_run_accounting_for_status(RunStatus.COMPLETED),
            ),
            pytest.raises(RuntimeError, match="simulated SSE crash"),
        ):
            service._run_pipeline(run_id, "source:\n  plugin: csv", threading.Event())

        # The probe failure must surface as a structured log so it's observable.
        probe_failed_logs = [c for c in mock_slog.error.call_args_list if c.args and c.args[0] == "post_exception_run_state_probe_failed"]
        assert len(probe_failed_logs) == 1, f"Expected one post_exception_run_state_probe_failed log, got {len(probe_failed_logs)}"

        # Fall-through: recovery DID attempt the failed update.  Three calls
        # total: running, completed, failed.
        statuses = [c.kwargs.get("status") for c in mock_session_service.update_run_status.call_args_list]
        assert statuses == ["running", "completed", "failed"], (
            f"Probe-failure path must fall through to update_run_status('failed', ...); got {statuses}"
        )

        # The post_terminal_exception_in_run_pipeline log must NOT have
        # been emitted (we couldn't determine the run was terminal).
        post_terminal_logs = [
            c for c in mock_slog.error.call_args_list if c.args and c.args[0] == "post_terminal_exception_in_run_pipeline"
        ]
        assert post_terminal_logs == []

    @patch("elspeth.web.execution.service.Orchestrator")
    @patch("elspeth.web.execution.preflight.ExecutionGraph")
    @patch("elspeth.web.execution.preflight.instantiate_plugins_from_config")
    @patch("elspeth.web.execution.service.load_settings_from_yaml_string")
    @patch("elspeth.web.execution.service.open_landscape_db")
    @patch("elspeth.web.execution.service.FilesystemPayloadStore")
    def test_post_completion_probe_failure_with_legal_transitions_preserves_original_exception(
        self,
        mock_payload: MagicMock,
        mock_landscape: MagicMock,
        mock_load: MagicMock,
        mock_instantiate: MagicMock,
        mock_graph_cls: MagicMock,
        mock_orch_cls: MagicMock,
        service: ExecutionServiceImpl,
        mock_session_service: MagicMock,
    ) -> None:
        """Lock the residual data-loss path into the test surface (pr-test-analyzer IG-2).

        Sister to ``test_post_completion_get_run_probe_failure_falls_through``,
        but wires real ``LEGAL_RUN_TRANSITIONS`` semantics into the
        ``update_run_status`` mock so the gap acknowledged in the production
        comment block on the post-exception probe (``run_already_terminal``
        false-on-probe-failure) is actually exercised by the test surface —
        not just documented in prose.

        Scenario:
          1. Pipeline runs to completion → ``update_run_status('completed', ...)``
             commits the audit row.
          2. Success-path ``broadcast('completed', ...)`` raises ``RuntimeError``.
          3. Recovery probes ``get_run`` → SQLAlchemyError (probe failure).
          4. ``run_already_terminal`` stays ``False`` (probe couldn't determine).
          5. Recovery falls through to ``update_run_status('failed', ...)`` against
             a row whose true status is ``completed``.
          6. Real ``LEGAL_RUN_TRANSITIONS`` mock raises
             ``IllegalRunTransitionError`` (matching production
             ``SessionService.update_run_status``'s transition guard).
          7. Production recovery's narrow ``except (SQLAlchemyError, OSError)``
             does NOT catch the ``ValueError`` subclass; it propagates and
             shadows the original ``RuntimeError``.

        Correct behaviour: the ``RuntimeError("simulated SSE crash")`` is the
        operationally-relevant signal and MUST be the surfacing exception —
        the ``IllegalRunTransitionError`` is an artefact of a recovery attempt
        that should never have been made against an already-terminal row.
        """
        _configure_runtime_success(
            mock_load=mock_load,
            mock_instantiate=mock_instantiate,
            mock_graph_cls=mock_graph_cls,
            mock_orch_cls=mock_orch_cls,
            result=self._make_completed_orchestrator(RunStatus.COMPLETED).run.return_value,
        )

        # Stateful mock that mirrors SessionService.update_run_status's
        # transition validation (sessions/service.py:665-667). Driven by the
        # real ``LEGAL_RUN_TRANSITIONS`` table so the test stays in lockstep
        # with the production validator without duplicating its terminal-set
        # closure.
        audit_row_status: dict[str, SessionRunStatus] = {"current": "pending"}

        def _legal_transitions_update(_run_id: Any, *, status: str, **__: Any) -> None:
            current = audit_row_status["current"]
            allowed = LEGAL_RUN_TRANSITIONS[current]
            if status not in allowed:
                raise IllegalRunTransitionError(current, status, allowed)
            audit_row_status["current"] = cast(SessionRunStatus, status)

        mock_session_service.update_run_status.side_effect = _legal_transitions_update

        # Probe fails — same as the sibling test.  Drives the recovery into
        # the fall-through branch where the residual data-loss occurs.
        mock_session_service.get_run.side_effect = SQLAlchemyError("simulated DB hiccup")

        self._wrap_broadcaster_to_raise(
            service,
            on_event_type="completed",
            exc=RuntimeError("simulated SSE crash"),
        )

        run_id = str(uuid4())
        # Closes the elspeth-879f6de6bd gap: the IllegalRunTransitionError
        # raised by the fall-through update_run_status('failed', ...) against
        # an already-terminal row is now caught narrowly in
        # ``ExecutionServiceImpl._run_pipeline``'s BaseException recovery
        # (branch 3 — see the prelude comment block at that site).  The
        # narrow catch promotes the run into the audit-primacy stance via
        # ``irte.current_status`` as in-band proof of terminality, so the
        # original SSE-crash RuntimeError surfaces and the ``failed`` SSE
        # broadcast is suppressed.
        with (
            patch("elspeth.web.execution.service.slog") as mock_slog,
            patch(
                "elspeth.web.execution.service.load_run_accounting_from_db",
                return_value=_run_accounting_for_status(RunStatus.COMPLETED),
            ),
            pytest.raises(RuntimeError, match="simulated SSE crash"),
        ):
            service._run_pipeline(run_id, "source:\n  plugin: csv", threading.Event())

        # Audit-primacy completion: branch 3 must NOT have broadcast a
        # ``failed`` SSE event (the audit row is in a real terminal status
        # — broadcasting ``failed`` would contradict it).  The branch-3 log
        # ``post_exception_recovery_aborted_run_terminal`` is the
        # SRE-discoverable resolution of the probe-failure ambiguity.
        recovery_aborted_logs = [
            c for c in mock_slog.error.call_args_list if c.args and c.args[0] == "post_exception_recovery_aborted_run_terminal"
        ]
        assert len(recovery_aborted_logs) == 1, f"branch-3 recovery slog must fire exactly once; got {len(recovery_aborted_logs)}"
        # The probe-failure slog still fires upstream — the IRTE catch is
        # the *resolution*, not a replacement for the probe-failure record.
        probe_failed_logs = [c for c in mock_slog.error.call_args_list if c.args and c.args[0] == "post_exception_run_state_probe_failed"]
        assert len(probe_failed_logs) == 1, f"probe-failure slog must still fire upstream of the IRTE catch; got {len(probe_failed_logs)}"
        # Three update_run_status attempts: running, completed, failed (the
        # third one raises IRTE which is caught).  The attempt is recorded
        # on the mock even though the side_effect raised.
        statuses = [c.kwargs.get("status") for c in mock_session_service.update_run_status.call_args_list]
        assert statuses == ["running", "completed", "failed"], (
            f"branch-3 recovery path must attempt the failed update (caught by IRTE); got {statuses}"
        )

    @patch("elspeth.web.execution.service.Orchestrator")
    @patch("elspeth.web.execution.preflight.ExecutionGraph")
    @patch("elspeth.web.execution.preflight.instantiate_plugins_from_config")
    @patch("elspeth.web.execution.service.load_settings_from_yaml_string")
    @patch("elspeth.web.execution.service.open_landscape_db")
    @patch("elspeth.web.execution.service.FilesystemPayloadStore")
    def test_post_completion_get_run_probe_value_error_propagates(
        self,
        mock_payload: MagicMock,
        mock_landscape: MagicMock,
        mock_load: MagicMock,
        mock_instantiate: MagicMock,
        mock_graph_cls: MagicMock,
        mock_orch_cls: MagicMock,
        service: ExecutionServiceImpl,
        mock_session_service: MagicMock,
    ) -> None:
        """ValueError from the post-exception ``get_run`` probe must propagate,
        not be absorbed.

        Audit-primacy contract (CLAUDE.md tier model): ``get_run`` can raise
        ``ValueError`` only via Tier 1 audit-data corruption — "Run not found"
        (the row vanished mid-run), malformed UUID columns, or non-UTC
        ``started_at`` / ``finished_at``.  All three are Tier 1 invariant
        violations that MUST crash immediately.

        Pre-fix behaviour absorbed ValueError in the probe catch alongside
        ``SQLAlchemyError``/``OSError`` and fell through to the best-effort
        ``update_run_status`` recovery, which would re-encounter the same
        corruption.  The narrow catch (mirroring the sibling pattern at the
        ``update_run_status`` recovery — commits b8ba2214/127417cb) keeps
        Tier 1 corruption visible at the call site.

        This test pins that contract so future re-widening of the catch is
        caught at review time.
        """
        _configure_runtime_success(
            mock_load=mock_load,
            mock_instantiate=mock_instantiate,
            mock_graph_cls=mock_graph_cls,
            mock_orch_cls=mock_orch_cls,
            result=self._make_completed_orchestrator(RunStatus.COMPLETED).run.return_value,
        )

        # Probe raises ValueError — the canonical Tier 1 signal from get_run
        # ("Run not found", malformed UUID, or non-UTC datetime).  All three
        # share this exception class and must surface, not be absorbed.
        mock_session_service.get_run.side_effect = ValueError("Run not found: simulated Tier 1 corruption")

        self._wrap_broadcaster_to_raise(
            service,
            on_event_type="completed",
            exc=RuntimeError("simulated SSE crash"),
        )

        run_id = str(uuid4())
        # The visible exception MUST be the probe's ValueError (Tier 1
        # corruption surfaces as itself).  The original RuntimeError is
        # preserved on ``__context__`` by Python's normal exception chaining.
        with (
            patch("elspeth.web.execution.service.slog") as mock_slog,
            patch(
                "elspeth.web.execution.service.load_run_accounting_from_db",
                return_value=_run_accounting_for_status(RunStatus.COMPLETED),
            ),
            pytest.raises(ValueError, match="Run not found") as exc_info,
        ):
            service._run_pipeline(run_id, "source:\n  plugin: csv", threading.Event())

        # Exception chain pins the original cause — the probe ValueError
        # is raised while handling the RuntimeError, so __context__ MUST
        # carry the original SSE crash.  Without this, debugging the
        # post-completion exception gets harder, not easier.
        assert isinstance(exc_info.value.__context__, RuntimeError)
        assert "simulated SSE crash" in str(exc_info.value.__context__)

        # Probe-failure slog MUST NOT fire — the ValueError exits the try
        # block via propagation, not through the narrow except.  If a future
        # change re-widens the catch to include ValueError, this assertion
        # will fail and surface the regression.
        probe_failed_logs = [c for c in mock_slog.error.call_args_list if c.args and c.args[0] == "post_exception_run_state_probe_failed"]
        assert probe_failed_logs == [], (
            "ValueError from get_run must propagate (Tier 1 corruption); "
            f"probe-failed slog should not fire, got {len(probe_failed_logs)} call(s)."
        )

        # The fall-through update_run_status("failed", ...) MUST NOT have
        # been called — control left the BaseException handler before the
        # recovery branch.  Only the "running" and "completed" updates from
        # the happy path are present.
        statuses = [c.kwargs.get("status") for c in mock_session_service.update_run_status.call_args_list]
        assert statuses == ["running", "completed"], f"ValueError-propagation path must skip the recovery update_run_status; got {statuses}"

    @patch("elspeth.web.execution.service.Orchestrator")
    @patch("elspeth.web.execution.preflight.ExecutionGraph")
    @patch("elspeth.web.execution.preflight.instantiate_plugins_from_config")
    @patch("elspeth.web.execution.service.load_settings_from_yaml_string")
    @patch("elspeth.web.execution.service.open_landscape_db")
    @patch("elspeth.web.execution.service.FilesystemPayloadStore")
    def test_running_state_exception_still_records_failed(
        self,
        mock_payload: MagicMock,
        mock_landscape: MagicMock,
        mock_load: MagicMock,
        mock_instantiate: MagicMock,
        mock_graph_cls: MagicMock,
        mock_orch_cls: MagicMock,
        service: ExecutionServiceImpl,
        mock_session_service: MagicMock,
    ) -> None:
        """Negative control: when the orchestrator raises BEFORE the terminal
        transition (run is still ``running``), the existing happy-path
        recovery must still land — the new guard MUST NOT short-circuit
        non-terminal states.
        """
        _configure_runtime_success(
            mock_load=mock_load,
            mock_instantiate=mock_instantiate,
            mock_graph_cls=mock_graph_cls,
            mock_orch_cls=mock_orch_cls,
        )
        mock_orch = mock_orch_cls.return_value
        mock_orch.run.side_effect = RuntimeError("orchestrator blew up mid-run")

        # DB still holds "running" because we never reached the terminal
        # transition.
        mock_session_service.get_run.return_value = _run_record_stub(status="running")

        run_id = str(uuid4())
        with patch("elspeth.web.execution.service.slog") as mock_slog, pytest.raises(RuntimeError, match="orchestrator blew up"):
            service._run_pipeline(run_id, "source:\n  plugin: csv", threading.Event())

        statuses = [c.kwargs.get("status") for c in mock_session_service.update_run_status.call_args_list]
        assert statuses == ["running", "failed"], f"Non-terminal path must still record failure; got {statuses}"

        post_terminal_logs = [
            c for c in mock_slog.error.call_args_list if c.args and c.args[0] == "post_terminal_exception_in_run_pipeline"
        ]
        assert post_terminal_logs == [], "Guard must NOT fire for non-terminal current status"


# ── Liveness Registry ─────────────────────────────────────────────────


class TestGetLiveRunIds:
    """Tests for get_live_run_ids — used by periodic orphan cleanup."""

    def test_returns_empty_when_no_active_runs(
        self,
        service: ExecutionServiceImpl,
    ) -> None:
        """No runs registered → empty frozenset."""
        assert service.get_live_run_ids() == frozenset()

    def test_returns_registered_run_ids(
        self,
        service: ExecutionServiceImpl,
    ) -> None:
        """Manually registered shutdown events appear in live run IDs."""
        event = threading.Event()
        with service._shutdown_events_lock:
            service._shutdown_events["run-abc"] = event
            service._shutdown_events["run-def"] = event
        assert service.get_live_run_ids() == frozenset({"run-abc", "run-def"})

    def test_includes_signalled_events_until_worker_exits(
        self,
        service: ExecutionServiceImpl,
    ) -> None:
        """Signalled runs stay live until _run_pipeline() removes them.

        A set shutdown event means cancellation was requested, not that the
        worker thread has finished its GracefulShutdownError unwinding.
        Periodic orphan cleanup must keep excluding the run until the
        worker's finally block removes the registry entry.
        """
        live_event = threading.Event()
        signalled_event = threading.Event()
        signalled_event.set()
        with service._shutdown_events_lock:
            service._shutdown_events["run-live"] = live_event
            service._shutdown_events["run-signalled"] = signalled_event
        assert service.get_live_run_ids() == frozenset({"run-live", "run-signalled"})

    def test_returns_snapshot_not_live_reference(
        self,
        service: ExecutionServiceImpl,
    ) -> None:
        """Returned frozenset is a snapshot — later changes don't affect it."""
        event = threading.Event()
        with service._shutdown_events_lock:
            service._shutdown_events["run-1"] = event
        snapshot = service.get_live_run_ids()
        with service._shutdown_events_lock:
            service._shutdown_events["run-2"] = event
        # Snapshot should not include run-2
        assert snapshot == frozenset({"run-1"})


# ── Blob Ref Pre-Validation ───────────────────────────────────────────


class TestBlobRefPreValidation:
    """Malformed blob_ref must raise BEFORE create_run() to avoid
    orphaning a pending run that blocks future executions."""

    @pytest.mark.asyncio
    async def test_malformed_blob_ref_raises_before_run_creation(
        self,
        service: ExecutionServiceImpl,
        mock_session_service: MagicMock,
    ) -> None:
        """A non-UUID blob_ref raises typed validation before create_run()
        is called, so no pending run is orphaned."""
        from elspeth.web.execution.errors import MalformedBlobRefError

        state = mock_session_service.get_current_state.return_value
        state.source = {
            "plugin": "csv",
            "on_success": "continue",
            "options": {"blob_ref": "not-a-uuid"},
            "on_validation_failure": "quarantine",
        }

        blob_service = _blob_service_stub()
        cast(Any, service)._blob_service = blob_service

        with pytest.raises(MalformedBlobRefError):
            await service.execute(session_id=uuid4())

        # The critical invariant: create_run() was never called,
        # so no stale pending run exists.
        mock_session_service.create_run.assert_not_called()

    @pytest.mark.asyncio
    async def test_valid_blob_ref_still_links_correctly(
        self,
        service: ExecutionServiceImpl,
        mock_session_service: MagicMock,
    ) -> None:
        """Valid UUID blob_ref is parsed early and passed to link_blob_to_run."""
        session_id = uuid4()
        run_id = uuid4()
        blob_ref = str(uuid4())
        canonical_path = f"/tmp/data/blobs/{session_id}/{blob_ref}_input.csv"
        mock_session_service.create_run.return_value = _run_record_stub(id=run_id)

        blob_service = _blob_service_stub()
        # get_blob returns a record matching the executing session
        blob_service.get_blob.return_value = _ready_csv_blob_for_execution(
            blob_ref=blob_ref,
            session_id=session_id,
            storage_path=canonical_path,
        )
        cast(Any, service)._blob_service = blob_service

        # path must equal blob.storage_path to satisfy the Tier 1 read
        # guard for blob-backed sources.
        state = mock_session_service.get_current_state.return_value
        state.source = {
            "plugin": "csv",
            "on_success": "continue",
            "options": {"blob_ref": blob_ref, "path": canonical_path},
            "on_validation_failure": "quarantine",
        }

        with patch.object(service, "_run_pipeline"):
            await service.execute(session_id=session_id)

        blob_service.link_blob_to_run.assert_called_once_with(
            blob_id=UUID(blob_ref),
            run_id=run_id,
            direction="input",
        )

    @pytest.mark.asyncio
    async def test_second_named_source_malformed_blob_ref_raises_before_run_creation(
        self,
        service: ExecutionServiceImpl,
        mock_session_service: MagicMock,
    ) -> None:
        """Malformed blob_ref on any named source is rejected before create_run()."""
        from elspeth.web.execution.errors import MalformedBlobRefError

        session_id = uuid4()
        state = mock_session_service.get_current_state.return_value
        state.source = {
            "plugin": "csv",
            "on_success": "orders_rows",
            "options": {"path": f"/tmp/data/blobs/{session_id}/orders.csv"},
            "on_validation_failure": "quarantine",
        }
        state.sources = {
            "orders": state.source,
            "refunds": {
                "plugin": "csv",
                "on_success": "refunds_rows",
                "options": {"blob_ref": "not-a-uuid", "path": f"/tmp/data/blobs/{session_id}/refunds.csv"},
                "on_validation_failure": "quarantine",
            },
        }

        blob_service = _blob_service_stub()
        cast(Any, service)._blob_service = blob_service

        with pytest.raises(MalformedBlobRefError, match=r"sources\.refunds\.blob_ref"):
            await service.execute(session_id=session_id)

        mock_session_service.create_run.assert_not_called()
        blob_service.get_blob.assert_not_called()

    @pytest.mark.asyncio
    async def test_named_blob_sources_all_link_to_run(
        self,
        service: ExecutionServiceImpl,
        mock_session_service: MagicMock,
    ) -> None:
        """Every valid named blob source gets an input blob_run_links row."""
        session_id = uuid4()
        run_id = uuid4()
        orders_blob = str(uuid4())
        refunds_blob = str(uuid4())
        orders_path = f"/tmp/data/blobs/{session_id}/{orders_blob}_orders.csv"
        refunds_path = f"/tmp/data/blobs/{session_id}/{refunds_blob}_refunds.csv"
        mock_session_service.create_run.return_value = _run_record_stub(id=run_id)

        blob_service = _blob_service_stub()
        blob_service.get_blob.side_effect = lambda blob_id: _ready_csv_blob_for_execution(
            blob_ref=str(blob_id),
            session_id=session_id,
            storage_path={orders_blob: orders_path, refunds_blob: refunds_path}[str(blob_id)],
        )
        cast(Any, service)._blob_service = blob_service

        state = mock_session_service.get_current_state.return_value
        state.source = {
            "plugin": "csv",
            "on_success": "orders_rows",
            "options": {"blob_ref": orders_blob, "path": orders_path},
            "on_validation_failure": "quarantine",
        }
        state.sources = {
            "orders": state.source,
            "refunds": {
                "plugin": "csv",
                "on_success": "refunds_rows",
                "options": {"blob_ref": refunds_blob, "path": refunds_path},
                "on_validation_failure": "quarantine",
            },
        }

        with patch.object(service, "_run_pipeline"):
            await service.execute(session_id=session_id)

        linked_blob_ids = [call.kwargs["blob_id"] for call in blob_service.link_blob_to_run.await_args_list]
        assert linked_blob_ids == [UUID(orders_blob), UUID(refunds_blob)]
        assert all(call.kwargs["run_id"] == run_id for call in blob_service.link_blob_to_run.await_args_list)
        assert all(call.kwargs["direction"] == "input" for call in blob_service.link_blob_to_run.await_args_list)


# ── Blob Ownership (Cross-Session IDOR) ──────────────────────────────


class TestBlobOwnership:
    """P2 defense-in-depth: blob_ref must belong to the executing session.

    Without this, a crafted composition state could reference another
    session's blob path — the shared-root path allowlist would pass it.
    """

    @pytest.mark.asyncio
    async def test_cross_session_blob_ref_rejected(
        self,
        service: ExecutionServiceImpl,
        mock_session_service: MagicMock,
    ) -> None:
        """Cross-session blob_ref raises ``BlobNotFoundError`` (IDOR collapse).

        The exception type is load-bearing: the route handler relies
        on cross-session and nonexistent blobs BOTH surfacing as
        ``BlobNotFoundError`` so they produce byte-identical 404
        responses.  Earlier this branch raised ``ValueError`` with a
        "does not belong to session" message — a distinguishable
        body AND a distinguishable status (404 vs the 500 that an
        uncaught ``BlobNotFoundError`` produced for the nonexistent
        case).  Do not revert to ``ValueError`` or add a specialised
        subclass without also updating the route handler in
        lockstep.
        """
        from elspeth.web.blobs.protocol import BlobNotFoundError

        executing_session_id = uuid4()
        other_session_id = uuid4()
        blob_ref = str(uuid4())

        blob_service = _blob_service_stub()
        # Blob belongs to other_session_id, not executing_session_id
        blob_service.get_blob.return_value = _ready_csv_blob_for_execution(
            blob_ref=blob_ref,
            session_id=other_session_id,
            storage_path=f"/tmp/data/blobs/{other_session_id}/{blob_ref}_input.csv",
        )
        cast(Any, service)._blob_service = blob_service

        state = mock_session_service.get_current_state.return_value
        state.source = {
            "plugin": "csv",
            "on_success": "continue",
            "options": {"blob_ref": blob_ref},
            "on_validation_failure": "quarantine",
        }

        with pytest.raises(BlobNotFoundError):
            await service.execute(session_id=executing_session_id)

        # Critical: create_run was never called (rejected before run creation)
        mock_session_service.create_run.assert_not_called()

    @pytest.mark.asyncio
    async def test_second_named_source_cross_session_blob_ref_rejected(
        self,
        service: ExecutionServiceImpl,
        mock_session_service: MagicMock,
    ) -> None:
        """Cross-session blob_ref on a non-first named source preserves IDOR collapse."""
        from elspeth.web.blobs.protocol import BlobNotFoundError

        executing_session_id = uuid4()
        other_session_id = uuid4()
        orders_blob = str(uuid4())
        refunds_blob = str(uuid4())
        orders_path = f"/tmp/data/blobs/{executing_session_id}/{orders_blob}_orders.csv"
        refunds_path = f"/tmp/data/blobs/{executing_session_id}/{refunds_blob}_refunds.csv"

        blob_service = _blob_service_stub()
        blob_service.get_blob.side_effect = lambda blob_id: _ready_csv_blob_for_execution(
            blob_ref=str(blob_id),
            session_id=executing_session_id if str(blob_id) == orders_blob else other_session_id,
            storage_path=orders_path if str(blob_id) == orders_blob else refunds_path,
        )
        cast(Any, service)._blob_service = blob_service

        state = mock_session_service.get_current_state.return_value
        state.source = {
            "plugin": "csv",
            "on_success": "orders_rows",
            "options": {"blob_ref": orders_blob, "path": orders_path},
            "on_validation_failure": "quarantine",
        }
        state.sources = {
            "orders": state.source,
            "refunds": {
                "plugin": "csv",
                "on_success": "refunds_rows",
                "options": {"blob_ref": refunds_blob, "path": refunds_path},
                "on_validation_failure": "quarantine",
            },
        }

        with pytest.raises(BlobNotFoundError):
            await service.execute(session_id=executing_session_id)

        mock_session_service.create_run.assert_not_called()
        blob_service.link_blob_to_run.assert_not_called()

    @pytest.mark.asyncio
    async def test_same_session_blob_ref_accepted(
        self,
        service: ExecutionServiceImpl,
        mock_session_service: MagicMock,
    ) -> None:
        """Blob belonging to the same session passes ownership check."""
        session_id = uuid4()
        blob_ref = str(uuid4())
        canonical_path = f"/tmp/data/blobs/{session_id}/{blob_ref}_input.csv"

        blob_service = _blob_service_stub()
        blob_service.get_blob.return_value = _ready_csv_blob_for_execution(
            blob_ref=blob_ref,
            session_id=session_id,
            storage_path=canonical_path,
        )
        cast(Any, service)._blob_service = blob_service

        # path must equal blob.storage_path to satisfy the Tier 1 read
        # guard for blob-backed sources.
        state = mock_session_service.get_current_state.return_value
        state.source = {
            "plugin": "csv",
            "on_success": "continue",
            "options": {"blob_ref": blob_ref, "path": canonical_path},
            "on_validation_failure": "quarantine",
        }

        with patch.object(service, "_run_pipeline"):
            run_id = await service.execute(session_id=session_id)
        assert isinstance(run_id, UUID)


# ── Blob Source Path Read Guard (Tier 1) ─────────────────────────────


class TestBlobSourcePathReadGuard:
    """Runtime read guard for composer-stored blob source paths.

    The composer's write-side defenses make wrong-shape blob source paths
    impossible to persist going forward, but the audit-integrity contract
    also requires that runtime crash informatively if a previously-
    persisted state row carries a path that disagrees with the canonical
    ``BlobRecord.storage_path``.  Per CLAUDE.md "no defensive programming",
    the runtime must not silently coerce or fall back to ``FileNotFoundError``.

    Bug-verification protocol (cf.
    ``tests/integration/pipeline/test_composer_runtime_agreement.py``
    module docstring lines 76-88): manually revert the
    ``if stored_path != canonical_path: raise BlobSourcePathMismatchError``
    block in ``ExecutionServiceImpl._execute_locked`` and confirm the
    mismatch test below fails with the canonical-path branch silently
    accepting the divergent stored path.  Then restore.
    """

    @pytest.mark.asyncio
    async def test_diverging_stored_path_raises_structured_error(
        self,
        service: ExecutionServiceImpl,
        mock_session_service: MagicMock,
    ) -> None:
        """Tier 1: stored path != blob.storage_path crashes at execute time.

        Reproduces the captured staging defect (session
        588b94c8-919c-43ab-ae2c-8a3033de8109): the persisted
        ``source.options.path`` does not match the canonical
        ``BlobRecord.storage_path``.  The captured shape was
        ``data/blobs/<bid>/<filename>`` (rejected first by the source
        path allowlist after the legacy resolver was removed); this test
        exercises the divergence case where the path is allowlist-valid
        but still not the canonical one (e.g. a stale absolute path
        pointing at a different file under ``data_dir/blobs/``).  The
        guard fires before the run record is created so the session is
        not poisoned with a pending run.
        """
        from elspeth.web.execution.errors import BlobSourcePathMismatchError

        session_id = uuid4()
        blob_ref = str(uuid4())
        canonical_path = f"/tmp/data/blobs/{session_id}/{blob_ref}_input.csv"
        # Allowlist-valid (under /tmp/data/blobs/) but not equal to
        # canonical_path — the divergence the read guard targets.
        diverging_path = f"/tmp/data/blobs/{session_id}/{blob_ref}_OTHER.csv"

        blob_service = _blob_service_stub()
        blob_service.get_blob.return_value = _ready_csv_blob_for_execution(
            blob_ref=blob_ref,
            session_id=session_id,
            storage_path=canonical_path,
        )
        cast(Any, service)._blob_service = blob_service

        state = mock_session_service.get_current_state.return_value
        state.source = {
            "plugin": "csv",
            "on_success": "continue",
            "options": {"blob_ref": blob_ref, "path": diverging_path},
            "on_validation_failure": "quarantine",
        }

        with pytest.raises(BlobSourcePathMismatchError) as exc_info:
            await service.execute(session_id=session_id)

        assert exc_info.value.stored_path == diverging_path
        assert exc_info.value.canonical_path == canonical_path
        assert exc_info.value.blob_id == blob_ref
        assert "bug in composer persistence" in str(exc_info.value)

        # Critical: create_run was never called — the session is not
        # poisoned with a pending run that the operator must clean up.
        mock_session_service.create_run.assert_not_called()
        # link_blob_to_run was never called either — guard fires before
        # any side effects.
        blob_service.link_blob_to_run.assert_not_called()

    @pytest.mark.asyncio
    async def test_missing_stored_path_raises_structured_error(
        self,
        service: ExecutionServiceImpl,
        mock_session_service: MagicMock,
    ) -> None:
        """Tier 1: stored path is None for a blob-backed source crashes.

        A composition state with ``blob_ref`` set but no ``path`` is
        structurally invalid — the blob binding requires the canonical
        path to be present.  This branch protects against a regression
        where a future composer-side bug omits the path entirely while
        still persisting the blob_ref.
        """
        from elspeth.web.execution.errors import BlobSourcePathMismatchError

        session_id = uuid4()
        blob_ref = str(uuid4())
        canonical_path = f"/tmp/data/blobs/{session_id}/{blob_ref}_input.csv"

        blob_service = _blob_service_stub()
        blob_service.get_blob.return_value = _ready_csv_blob_for_execution(
            blob_ref=blob_ref,
            session_id=session_id,
            storage_path=canonical_path,
        )
        cast(Any, service)._blob_service = blob_service

        state = mock_session_service.get_current_state.return_value
        state.source = {
            "plugin": "csv",
            "on_success": "continue",
            # No path key at all
            "options": {"blob_ref": blob_ref},
            "on_validation_failure": "quarantine",
        }

        with pytest.raises(BlobSourcePathMismatchError) as exc_info:
            await service.execute(session_id=session_id)

        assert exc_info.value.stored_path is None
        assert exc_info.value.canonical_path == canonical_path

    @pytest.mark.asyncio
    async def test_named_blob_source_path_mismatch_raises_structured_error(
        self,
        service: ExecutionServiceImpl,
        mock_session_service: MagicMock,
    ) -> None:
        """Every named source blob_ref gets the same ownership/path guard."""
        from elspeth.web.execution.errors import BlobSourcePathMismatchError

        session_id = uuid4()
        blob_ref = str(uuid4())
        canonical_path = f"/tmp/data/blobs/{session_id}/{blob_ref}_orders.csv"
        diverging_path = f"/tmp/data/blobs/{session_id}/{blob_ref}_OTHER.csv"

        blob_service = _blob_service_stub()
        blob_service.get_blob.return_value = _ready_csv_blob_for_execution(
            blob_ref=blob_ref,
            session_id=session_id,
            storage_path=canonical_path,
        )
        cast(Any, service)._blob_service = blob_service

        state = mock_session_service.get_current_state.return_value
        state.source = None
        state.sources = {
            "orders": {
                "plugin": "csv",
                "on_success": "orders_rows",
                "options": {"blob_ref": blob_ref, "path": diverging_path},
                "on_validation_failure": "quarantine",
            }
        }

        with pytest.raises(BlobSourcePathMismatchError) as exc_info:
            await service.execute(session_id=session_id)

        assert exc_info.value.stored_path == diverging_path
        assert exc_info.value.canonical_path == canonical_path
        mock_session_service.create_run.assert_not_called()
        blob_service.link_blob_to_run.assert_not_called()

    @pytest.mark.asyncio
    async def test_second_named_blob_source_path_mismatch_raises_structured_error(
        self,
        service: ExecutionServiceImpl,
        mock_session_service: MagicMock,
    ) -> None:
        """A non-first named source must also match the canonical blob path."""
        from elspeth.web.execution.errors import BlobSourcePathMismatchError

        session_id = uuid4()
        orders_blob = str(uuid4())
        refunds_blob = str(uuid4())
        orders_path = f"/tmp/data/blobs/{session_id}/{orders_blob}_orders.csv"
        refunds_canonical_path = f"/tmp/data/blobs/{session_id}/{refunds_blob}_refunds.csv"
        refunds_diverging_path = f"/tmp/data/blobs/{session_id}/{refunds_blob}_OTHER.csv"

        blob_service = _blob_service_stub()
        blob_service.get_blob.side_effect = lambda blob_id: _ready_csv_blob_for_execution(
            blob_ref=str(blob_id),
            session_id=session_id,
            storage_path=orders_path if str(blob_id) == orders_blob else refunds_canonical_path,
        )
        cast(Any, service)._blob_service = blob_service

        state = mock_session_service.get_current_state.return_value
        state.source = {
            "plugin": "csv",
            "on_success": "orders_rows",
            "options": {"blob_ref": orders_blob, "path": orders_path},
            "on_validation_failure": "quarantine",
        }
        state.sources = {
            "orders": state.source,
            "refunds": {
                "plugin": "csv",
                "on_success": "refunds_rows",
                "options": {"blob_ref": refunds_blob, "path": refunds_diverging_path},
                "on_validation_failure": "quarantine",
            },
        }

        with pytest.raises(BlobSourcePathMismatchError) as exc_info:
            await service.execute(session_id=session_id)

        assert exc_info.value.blob_id == refunds_blob
        assert exc_info.value.stored_path == refunds_diverging_path
        assert exc_info.value.canonical_path == refunds_canonical_path
        mock_session_service.create_run.assert_not_called()
        blob_service.link_blob_to_run.assert_not_called()


# ── One Active Run (B6) ───────────────────────────────────────────────


class TestOneActiveRun:
    @pytest.mark.asyncio
    async def test_second_execute_raises_run_already_active(
        self,
        service: ExecutionServiceImpl,
        mock_session_service: MagicMock,
    ) -> None:
        """B6: Only one pending/running run per session."""
        session_id = uuid4()
        mock_session_service.get_active_run.return_value = _run_record_stub(status="running")

        with pytest.raises(RunAlreadyActiveError):
            await service.execute(session_id=session_id)

    @pytest.mark.asyncio
    async def test_execute_after_completed_run_succeeds(
        self,
        service: ExecutionServiceImpl,
        mock_session_service: MagicMock,
    ) -> None:
        """After a run completes, a new one can start."""
        mock_session_service.get_active_run.return_value = None
        with patch.object(service, "_run_pipeline"):
            run_id = await service.execute(session_id=uuid4())
        assert isinstance(run_id, UUID)


# ── EventBus Bridge ───────────────────────────────────────────────────


class TestEventBusBridge:
    """Verify that ProgressEvent from the Orchestrator's EventBus
    is translated to RunEvent and broadcast via the ProgressBroadcaster."""

    def test_progress_event_translated_to_run_event(self, service: ExecutionServiceImpl) -> None:
        """_to_run_event maps ProgressEvent fields to RunEvent.data.

        elspeth-5069612f3c — assert the routed split (MOVE / DIVERT) is
        plumbed verbatim through the translator. Pre-fix the engine emitter
        folded ``rows_routed_success`` into ``rows_succeeded`` and dropped
        ``rows_routed_failure`` entirely; the wire payload then lacked the
        fields. This test guards against regression to that shape.
        """
        from elspeth.contracts.cli import ProgressEvent
        from elspeth.web.execution.schemas import ProgressData

        progress = ProgressEvent(
            rows_processed=100,
            rows_succeeded=92,
            rows_failed=5,
            rows_quarantined=3,
            rows_routed_success=7,
            rows_routed_failure=2,
            elapsed_seconds=10.5,
        )
        run_id = "run-123"
        run_event = service._to_run_event(run_id, progress)

        assert run_event.event_type == "progress"
        assert isinstance(run_event.data, ProgressData)
        # S-8: assert every counter passes through with its real producer
        # value.  Non-zero values in every slot guard against a future
        # producer that hardcodes any single counter to 0 (which Pydantic
        # cannot detect — making the upstream ProgressEvent require all six
        # is the structural defense, this assertion is the test surface).
        assert run_event.data.source_rows_processed == 100
        assert run_event.data.tokens_succeeded == 92
        assert run_event.data.tokens_failed == 5
        assert run_event.data.tokens_quarantined == 3
        assert run_event.data.tokens_routed_success == 7
        assert run_event.data.tokens_routed_failure == 2
        assert run_event.run_id == "run-123"

    def test_progress_event_persisted_even_without_subscriber(
        self,
        service: ExecutionServiceImpl,
        mock_session_service: MagicMock,
    ) -> None:
        from elspeth.contracts.cli import ProgressEvent

        service._broadcast_progress_event(
            "4a8ebbb1-dc97-4875-8b3d-1890ec7ead87",
            ProgressEvent(
                rows_processed=100,
                rows_succeeded=92,
                rows_failed=5,
                rows_quarantined=3,
                rows_routed_success=7,
                rows_routed_failure=2,
                elapsed_seconds=10.5,
            ),
        )

        mock_session_service.append_run_event.assert_awaited_once()
        call = mock_session_service.append_run_event.await_args
        assert call.kwargs["run_id"] == UUID("4a8ebbb1-dc97-4875-8b3d-1890ec7ead87")
        assert call.kwargs["event_type"] == "progress"
        assert call.kwargs["data"]["source_rows_processed"] == 100

    def test_progress_broadcast_closed_loop_records_drop_telemetry(self, service: ExecutionServiceImpl) -> None:
        """Loop-closed progress drops are operational telemetry, not slog-only."""
        from elspeth.contracts.cli import ProgressEvent

        loop = asyncio.new_event_loop()
        try:
            broadcaster = ProgressBroadcaster(loop)
            run_id = "4a8ebbb1-dc97-4875-8b3d-1890ec7ead87"
            broadcaster.subscribe(run_id)
            loop.close()
            service._broadcaster = broadcaster

            service._broadcast_progress_event(
                run_id,
                ProgressEvent(
                    rows_processed=100,
                    rows_succeeded=92,
                    rows_failed=5,
                    rows_quarantined=3,
                    rows_routed_success=7,
                    rows_routed_failure=2,
                    elapsed_seconds=10.5,
                ),
            )
        finally:
            if not loop.is_closed():
                loop.close()

        assert observed_value(service._telemetry.progress_broadcast_dropped_total) == 1


# ── B10: _call_async() Bridge Tests ──────────────────────────────────


class TestB8AsyncBridging:
    """B8/C1 fix: _call_async() bridges sync thread to async event loop.

    These tests need the REAL _call_async (not the test fixture's mock),
    so they construct a fresh service with a mock loop whose
    run_coroutine_threadsafe is controlled.
    """

    def test_call_async_returns_coroutine_result(
        self,
        broadcaster: ProgressBroadcaster,
        mock_settings: MagicMock,
        mock_session_service: MagicMock,
    ) -> None:
        """_call_async() schedules coroutine and returns its result."""
        mock_loop = MagicMock(spec=asyncio.AbstractEventLoop)
        svc = ExecutionServiceImpl.for_trained_operator(
            loop=mock_loop,
            broadcaster=broadcaster,
            settings=mock_settings,
            session_service=mock_session_service,
            yaml_generator=_YamlGeneratorStub(),
            telemetry=build_sessions_telemetry(),
        )
        mock_future = MagicMock(spec=Future)
        mock_future.result.return_value = "test_result"

        async def dummy_coro() -> str:
            return "test_result"

        coro = dummy_coro()
        with patch("asyncio.run_coroutine_threadsafe", return_value=mock_future):
            result = svc._call_async(coro)
        coro.close()
        assert result == "test_result"
        mock_future.result.assert_called_once_with(timeout=30.0)

    def test_call_async_propagates_coroutine_exception(
        self,
        broadcaster: ProgressBroadcaster,
        mock_settings: MagicMock,
        mock_session_service: MagicMock,
    ) -> None:
        """If the coroutine raises, _call_async re-raises from future.result()."""
        mock_loop = MagicMock(spec=asyncio.AbstractEventLoop)
        svc = ExecutionServiceImpl.for_trained_operator(
            loop=mock_loop,
            broadcaster=broadcaster,
            settings=mock_settings,
            session_service=mock_session_service,
            yaml_generator=_YamlGeneratorStub(),
            telemetry=build_sessions_telemetry(),
        )
        mock_future = MagicMock(spec=Future)
        mock_future.result.side_effect = ValueError("db error")

        async def failing_coro() -> None:
            raise ValueError("db error")

        coro = failing_coro()
        with patch("asyncio.run_coroutine_threadsafe", return_value=mock_future), pytest.raises(ValueError, match="db error"):
            svc._call_async(coro)
        coro.close()

    def test_call_async_raises_timeout_error(
        self,
        broadcaster: ProgressBroadcaster,
        mock_settings: MagicMock,
        mock_session_service: MagicMock,
    ) -> None:
        """R6 fix: _call_async raises TimeoutError after 30s, preventing deadlock."""
        mock_loop = MagicMock(spec=asyncio.AbstractEventLoop)
        svc = ExecutionServiceImpl.for_trained_operator(
            loop=mock_loop,
            broadcaster=broadcaster,
            settings=mock_settings,
            session_service=mock_session_service,
            yaml_generator=_YamlGeneratorStub(),
            telemetry=build_sessions_telemetry(),
        )
        mock_future = MagicMock(spec=Future)
        mock_future.result.side_effect = concurrent.futures.TimeoutError()

        async def hanging_coro() -> None:
            pass

        coro = hanging_coro()
        with patch("asyncio.run_coroutine_threadsafe", return_value=mock_future), pytest.raises(concurrent.futures.TimeoutError):
            svc._call_async(coro)
        coro.close()


class TestAsyncShutdown:
    """Shutdown must keep the event loop available for worker cleanup."""

    @pytest.mark.asyncio
    async def test_shutdown_keeps_loop_available_for_worker_cleanup(
        self,
        mock_settings: MagicMock,
        mock_session_service: MagicMock,
    ) -> None:
        """Regression: draining the executor must not strand worker _call_async calls."""
        loop = asyncio.get_running_loop()
        svc = ExecutionServiceImpl.for_trained_operator(
            loop=loop,
            broadcaster=ProgressBroadcaster(loop),
            settings=mock_settings,
            session_service=mock_session_service,
            yaml_generator=_YamlGeneratorStub(),
            telemetry=build_sessions_telemetry(),
        )

        run_id = str(uuid4())
        shutdown_event = threading.Event()
        with svc._shutdown_events_lock:
            svc._shutdown_events[run_id] = shutdown_event

        cleanup_applied = asyncio.Event()

        async def update_run_status(*args: Any, **kwargs: Any) -> None:
            cleanup_applied.set()

        mock_session_service.update_run_status.side_effect = update_run_status

        def short_call_async(coro: Coroutine[Any, Any, Any]) -> Any:
            future = asyncio.run_coroutine_threadsafe(coro, loop)
            try:
                return future.result(timeout=1.0)
            except concurrent.futures.TimeoutError:
                future.cancel()
                raise

        cast(Any, svc)._call_async = short_call_async

        worker_done = threading.Event()
        worker_errors: list[str] = []

        def worker() -> None:
            shutdown_event.wait()
            try:
                svc._call_async(mock_session_service.update_run_status(uuid4(), status="cancelled"))
            except BaseException as exc:
                worker_errors.append(type(exc).__name__)
            finally:
                worker_done.set()

        svc._executor.submit(worker)

        await svc.shutdown()

        assert worker_done.is_set()
        assert worker_errors == []
        assert cleanup_applied.is_set()


# ── W15: Running Status Failure Path ─────────────────────────────────


class TestRunningStatusFailure:
    """W15: What happens when the initial status update to 'running' fails."""

    @patch("elspeth.web.execution.service.open_landscape_db")
    @patch("elspeth.web.execution.service.FilesystemPayloadStore")
    def test_running_status_failure_marks_run_failed(
        self,
        mock_payload: MagicMock,
        mock_landscape: MagicMock,
        service: ExecutionServiceImpl,
        mock_session_service: MagicMock,
    ) -> None:
        """If update_run_status('running') fails, the except BaseException
        block attempts to set 'failed'. Run stays 'pending' if both fail."""
        # Make the first _call_async raise (simulating event loop issues)
        original_call_async = service._call_async
        call_count = 0

        def failing_call_async(coro: Coroutine[Any, Any, Any]) -> Any:
            nonlocal call_count
            call_count += 1
            if call_count == 1:  # First call = update to "running"
                coro.close()
                raise ConnectionError("DB connection lost")
            return original_call_async(coro)

        cast(Any, service)._call_async = failing_call_async

        with _admitted_runtime_setup(), pytest.raises(ConnectionError):
            service._run_pipeline(str(uuid4()), _TEST_PIPELINE_YAML, threading.Event())

        # The except block tried to set "failed" via the second _call_async call
        assert call_count >= 2


# ── IDOR Protection: verify_run_ownership ─────────────────────────────


class TestVerifyRunOwnership:
    """IDOR protection — verify_run_ownership checks user_id + auth_provider.

    Criticality 9/10: This is the gate between "attacker can watch other
    users' pipeline progress via WebSocket" and "access denied."
    """

    @pytest.fixture
    def idor_service(
        self,
        mock_loop: MagicMock,
        broadcaster: ProgressBroadcaster,
    ) -> tuple[ExecutionServiceImpl, Any]:
        """ExecutionServiceImpl with controllable session service."""
        session_svc = create_autospec(SessionServiceProtocol, instance=True)
        settings = _WebSettingsStub()
        settings.auth_provider = "local"
        settings.landscape_url = "sqlite:///test.db"
        settings.payload_store_path = Path("/tmp/test")

        svc = ExecutionServiceImpl.for_trained_operator(
            loop=mock_loop,
            broadcaster=broadcaster,
            settings=settings,
            session_service=session_svc,
            yaml_generator=_YamlGeneratorStub(),
            telemetry=build_sessions_telemetry(),
        )
        return svc, session_svc

    @pytest.mark.asyncio
    async def test_owner_match_returns_true(self, idor_service) -> None:
        """Correct user + correct provider → access granted."""
        svc, session_svc = idor_service
        session_id = uuid4()
        run = SimpleNamespace(session_id=session_id)
        session = SimpleNamespace(user_id="alice", auth_provider_type="local", archived_at=None)
        session_svc.get_run.return_value = run
        session_svc.get_session.return_value = session

        user = SimpleNamespace(user_id="alice")
        assert await svc.verify_run_ownership(user, str(uuid4())) is True

    @pytest.mark.asyncio
    async def test_wrong_user_returns_false(self, idor_service) -> None:
        """Wrong user_id → access denied."""
        svc, session_svc = idor_service
        run = SimpleNamespace(session_id=uuid4())
        session = SimpleNamespace(user_id="alice", auth_provider_type="local", archived_at=None)
        session_svc.get_run.return_value = run
        session_svc.get_session.return_value = session

        user = SimpleNamespace(user_id="eve")
        assert await svc.verify_run_ownership(user, str(uuid4())) is False

    @pytest.mark.asyncio
    async def test_cross_provider_returns_false(self, idor_service) -> None:
        """Same user_id but different auth provider → access denied.

        This prevents "alice" in local auth from accessing runs belonging
        to "alice" in OIDC. Cross-provider user_id collision is the
        non-obvious IDOR vector.
        """
        svc, session_svc = idor_service
        run = SimpleNamespace(session_id=uuid4())
        # Session was created under OIDC, but server is now configured for "local"
        session = SimpleNamespace(user_id="alice", auth_provider_type="oidc", archived_at=None)
        session_svc.get_run.return_value = run
        session_svc.get_session.return_value = session

        user = SimpleNamespace(user_id="alice")
        assert await svc.verify_run_ownership(user, str(uuid4())) is False

    @pytest.mark.asyncio
    async def test_archived_session_returns_false_for_websocket_gate(self, idor_service) -> None:
        """A run's matching principal cannot observe an archived session over WebSocket."""
        svc, session_svc = idor_service
        run = SimpleNamespace(session_id=uuid4())
        session = SimpleNamespace(
            user_id="alice",
            auth_provider_type="local",
            archived_at=datetime.now(UTC),
        )
        session_svc.get_run.return_value = run
        session_svc.get_session.return_value = session

        user = SimpleNamespace(user_id="alice")
        assert await svc.verify_run_ownership(user, str(uuid4())) is False

    @pytest.mark.asyncio
    async def test_nonexistent_run_raises(self, idor_service) -> None:
        """Run not found → ValueError propagates (caller handles)."""
        svc, session_svc = idor_service
        session_svc.get_run.side_effect = ValueError("Run not found")

        user = SimpleNamespace(user_id="alice")
        with pytest.raises(ValueError, match="Run not found"):
            await svc.verify_run_ownership(user, str(uuid4()))

    @pytest.mark.asyncio
    async def test_dangling_session_fk_raises_integrity_error(self, idor_service) -> None:
        """Existing run whose session_id FK has no sessions row → Tier-1
        integrity error, NOT a benign ValueError/not-found.

        Regression: a dangling parent-session reference is referential
        corruption of our own sessions DB, not hostile client input. It must
        NOT collapse into the Tier-3 "Run not found" (ValueError → 4004) path.
        ``get_session`` signals the missing row as ``SessionNotFoundError``
        (a ValueError subclass); ``verify_run_ownership`` must surface it as a
        non-ValueError ``RunSessionIntegrityError`` so the ownership-check
        caller's broad ``except ValueError`` cannot swallow Tier-1 corruption.
        """
        from elspeth.web.execution.errors import RunSessionIntegrityError
        from elspeth.web.sessions.protocol import SessionNotFoundError

        svc, session_svc = idor_service
        dangling_session_id = uuid4()
        run = SimpleNamespace(session_id=dangling_session_id)
        session_svc.get_run.return_value = run
        session_svc.get_session.side_effect = SessionNotFoundError(dangling_session_id)

        user = SimpleNamespace(user_id="alice")
        with pytest.raises(RunSessionIntegrityError) as exc_info:
            await svc.verify_run_ownership(user, str(uuid4()))
        # It must NOT be catchable as the benign Tier-3 not-found path even
        # though SessionNotFoundError itself subclasses ValueError.
        assert not isinstance(exc_info.value, ValueError)
        assert exc_info.value.session_id == str(dangling_session_id)

    @pytest.mark.asyncio
    async def test_str_vs_non_str_user_id_rejects(self, idor_service) -> None:
        """Regression: if session.user_id were stored as UUID, str comparison must reject."""
        svc, session_svc = idor_service
        run = SimpleNamespace(session_id=uuid4())
        user_uuid = uuid4()
        session = SimpleNamespace(user_id=user_uuid, auth_provider_type="local", archived_at=None)
        session_svc.get_run.return_value = run
        session_svc.get_session.return_value = session

        user = SimpleNamespace(user_id=str(user_uuid))
        assert await svc.verify_run_ownership(user, str(uuid4())) is False


# ── Sink Path Restriction ─────────────────────────────────────────────


class TestSinkPathRestriction:
    """P1 security fix: Sink output paths must be confined to allowed directories.

    Without this, a client can set sink options.path to an arbitrary absolute
    or ../ path and /execute will write there — turning the executor into an
    arbitrary file-write surface.
    """

    @pytest.mark.asyncio
    async def test_sink_path_outside_allowed_dirs_raises(
        self,
        service: ExecutionServiceImpl,
        mock_session_service: MagicMock,
        mock_settings: MagicMock,
    ) -> None:
        """Sink with path pointing outside data_dir/outputs must be rejected."""
        mock_settings.data_dir = "/tmp/elspeth_data"
        state = mock_session_service.get_current_state.return_value
        state.source = None
        state.outputs = [
            {
                "name": "primary",
                "plugin": "csv",
                "options": {"path": "/etc/cron.d/backdoor.csv"},
                "on_write_failure": "discard",
            }
        ]
        state.nodes = None
        state.edges = None

        from elspeth.web.execution.errors import PathAllowlistViolationError

        with pytest.raises(PathAllowlistViolationError, match="resolves outside allowed output directories"):
            await service.execute(session_id=uuid4())

    @pytest.mark.asyncio
    async def test_sink_path_traversal_rejected(
        self,
        service: ExecutionServiceImpl,
        mock_session_service: MagicMock,
        mock_settings: MagicMock,
    ) -> None:
        """Sink with ../ traversal in path must be rejected."""
        mock_settings.data_dir = "/tmp/elspeth_data"
        state = mock_session_service.get_current_state.return_value
        state.source = None
        state.outputs = [
            {
                "name": "results",
                "plugin": "json",
                "options": {"path": "/tmp/elspeth_data/outputs/../../etc/passwd"},
                "on_write_failure": "discard",
            }
        ]
        state.nodes = None
        state.edges = None

        from elspeth.web.execution.errors import PathAllowlistViolationError

        with pytest.raises(PathAllowlistViolationError, match="resolves outside allowed output directories"):
            await service.execute(session_id=uuid4())

    @pytest.mark.asyncio
    async def test_sink_path_under_outputs_accepted(
        self,
        service: ExecutionServiceImpl,
        mock_session_service: MagicMock,
        mock_settings: MagicMock,
    ) -> None:
        """Sink with path under data_dir/outputs is allowed."""
        mock_settings.data_dir = "/tmp/elspeth_data"
        session_id = uuid4()
        state = mock_session_service.get_current_state.return_value
        state.source = None
        state.outputs = [
            {
                "name": "primary",
                "plugin": "csv",
                "options": {"path": f"/tmp/elspeth_data/outputs/{session_id}/result.csv"},
                "on_write_failure": "discard",
            }
        ]
        state.nodes = None
        state.edges = None

        with patch.object(service, "_run_pipeline"):
            run_id = await service.execute(session_id=session_id)
        assert isinstance(run_id, UUID)

    @pytest.mark.asyncio
    async def test_sink_path_in_other_session_blobs_rejected(
        self,
        service: ExecutionServiceImpl,
        mock_session_service: MagicMock,
        mock_settings: MagicMock,
    ) -> None:
        """elspeth-bdc17cfdb1: a sink path inside ANOTHER session's blob
        subtree must be rejected at run start — the write primitive this
        fix closes."""
        mock_settings.data_dir = "/tmp/elspeth_data"
        other_session = uuid4()
        state = mock_session_service.get_current_state.return_value
        state.source = None
        state.outputs = [
            {
                "name": "exfil",
                "plugin": "csv",
                "options": {"path": f"/tmp/elspeth_data/blobs/{other_session}/poison.csv"},
                "on_write_failure": "discard",
            }
        ]
        state.nodes = None
        state.edges = None

        from elspeth.web.execution.errors import PathAllowlistViolationError

        with pytest.raises(PathAllowlistViolationError, match="resolves outside allowed output directories"):
            await service.execute(session_id=uuid4())

    @pytest.mark.asyncio
    async def test_sink_path_in_other_session_outputs_rejected(
        self,
        service: ExecutionServiceImpl,
        mock_session_service: MagicMock,
        mock_settings: MagicMock,
    ) -> None:
        """A shared output root is infrastructure, not cross-session write authority."""
        mock_settings.data_dir = "/tmp/elspeth_data"
        executing_session = uuid4()
        other_session = uuid4()
        state = mock_session_service.get_current_state.return_value
        state.source = None
        state.outputs = [
            {
                "name": "append_foreign",
                "plugin": "csv",
                "options": {
                    "path": f"/tmp/elspeth_data/outputs/{other_session}/shared.csv",
                    "mode": "append",
                },
                "on_write_failure": "discard",
            }
        ]
        state.nodes = None
        state.edges = None

        from elspeth.web.execution.errors import PathAllowlistViolationError

        with pytest.raises(PathAllowlistViolationError, match="resolves outside allowed output directories"):
            await service.execute(session_id=executing_session)

    @pytest.mark.asyncio
    async def test_sink_path_in_own_session_blobs_accepted(
        self,
        service: ExecutionServiceImpl,
        mock_session_service: MagicMock,
        mock_settings: MagicMock,
    ) -> None:
        """A sink path inside the executing session's own blob subtree is
        allowed — the pending-output-blob flow writes exactly there."""
        mock_settings.data_dir = "/tmp/elspeth_data"
        own_session = uuid4()
        state = mock_session_service.get_current_state.return_value
        state.source = None
        state.outputs = [
            {
                "name": "primary",
                "plugin": "csv",
                "options": {"path": f"/tmp/elspeth_data/blobs/{own_session}/out.csv"},
                "on_write_failure": "discard",
            }
        ]
        state.nodes = None
        state.edges = None

        with patch.object(service, "_run_pipeline"):
            run_id = await service.execute(session_id=own_session)
        assert isinstance(run_id, UUID)

    @pytest.mark.asyncio
    async def test_sink_without_path_option_passes(
        self,
        service: ExecutionServiceImpl,
        mock_session_service: MagicMock,
        mock_settings: MagicMock,
    ) -> None:
        """Sink with no path/file options (e.g. database sink) passes check."""
        mock_settings.data_dir = "/tmp/elspeth_data"
        state = mock_session_service.get_current_state.return_value
        state.source = None
        state.outputs = [
            {
                "name": "db_sink",
                "plugin": "database",
                "options": {"connection_string": "sqlite:///out.db"},
                "on_write_failure": "discard",
            }
        ]
        state.nodes = None
        state.edges = None

        with patch.object(service, "_run_pipeline"):
            run_id = await service.execute(session_id=uuid4())
        assert isinstance(run_id, UUID)


class TestTransformProviderConfigPathRestriction:
    """Nested transform provider_config persist_directory must be confined to
    the allowed output directories.

    RAG retrieval transforms carry a local Chroma persist_directory under
    options.provider_config. Without this, a client can set it to an arbitrary
    absolute or ../ path and /execute will read/write Chroma files there —
    escaping the data_dir sandbox.
    """

    @staticmethod
    def _resolved_llm_reviews(*, node_id: str, prompt_template: str, model: str) -> list[dict[str, object]]:
        return [
            {
                "id": f"prompt_template_review:{node_id}",
                "kind": "llm_prompt_template",
                "user_term": f"llm_prompt_template:{node_id}",
                "status": "resolved",
                "draft": prompt_template,
                "event_id": f"prompt-template-accepted:{node_id}",
                "accepted_value": prompt_template,
                "accepted_artifact_hash": None,
                "resolved_prompt_template_hash": stable_hash(prompt_template),
            },
            {
                "id": f"model_choice_review:{node_id}",
                "kind": "llm_model_choice",
                "user_term": f"llm_model_choice:{node_id}",
                "status": "resolved",
                "draft": model,
                "event_id": f"model-choice-accepted:{node_id}",
                "accepted_value": model,
                "accepted_artifact_hash": None,
                "resolved_prompt_template_hash": stable_hash(model),
            },
        ]

    @pytest.mark.asyncio
    async def test_transform_persist_directory_outside_allowed_dirs_raises(
        self,
        service: ExecutionServiceImpl,
        mock_session_service: MagicMock,
        mock_settings: MagicMock,
    ) -> None:
        """Transform with provider_config.persist_directory outside data_dir/outputs must be rejected."""
        mock_settings.data_dir = "/tmp/elspeth_data"
        state = mock_session_service.get_current_state.return_value
        state.source = None
        state.outputs = None
        state.nodes = [
            {
                "id": "rag",
                "node_type": "transform",
                "plugin": "rag_retrieval",
                "input": "transform_in",
                "on_success": "results",
                "on_error": "discard",
                "options": {
                    "provider": "chroma",
                    "provider_config": {"persist_directory": "/etc/cron.d/backdoor"},
                },
            }
        ]
        state.edges = None

        from elspeth.web.execution.errors import PathAllowlistViolationError

        with pytest.raises(PathAllowlistViolationError, match="resolves outside allowed output directories"):
            await service.execute(session_id=uuid4())

    @pytest.mark.asyncio
    async def test_transform_persist_directory_traversal_rejected(
        self,
        service: ExecutionServiceImpl,
        mock_session_service: MagicMock,
        mock_settings: MagicMock,
    ) -> None:
        """Transform with ../ traversal in provider_config.persist_directory must be rejected."""
        mock_settings.data_dir = "/tmp/elspeth_data"
        state = mock_session_service.get_current_state.return_value
        state.source = None
        state.outputs = None
        state.nodes = [
            {
                "id": "rag",
                "node_type": "transform",
                "plugin": "rag_retrieval",
                "input": "transform_in",
                "on_success": "results",
                "on_error": "discard",
                "options": {
                    "provider": "chroma",
                    "provider_config": {"persist_directory": "/tmp/elspeth_data/outputs/../../etc/secret"},
                },
            }
        ]
        state.edges = None

        from elspeth.web.execution.errors import PathAllowlistViolationError

        with pytest.raises(PathAllowlistViolationError, match="resolves outside allowed output directories"):
            await service.execute(session_id=uuid4())

    @pytest.mark.asyncio
    async def test_transform_persist_directory_under_outputs_accepted(
        self,
        service: ExecutionServiceImpl,
        mock_session_service: MagicMock,
        mock_settings: MagicMock,
    ) -> None:
        """Transform with provider_config.persist_directory under data_dir/outputs is allowed."""
        mock_settings.data_dir = "/tmp/elspeth_data"
        session_id = uuid4()
        state = mock_session_service.get_current_state.return_value
        state.source = None
        state.outputs = None
        state.nodes = [
            {
                "id": "rag",
                "node_type": "transform",
                "plugin": "rag_retrieval",
                "input": "transform_in",
                "on_success": "results",
                "on_error": "discard",
                "options": {
                    "provider": "chroma",
                    "provider_config": {"persist_directory": f"/tmp/elspeth_data/outputs/{session_id}/chroma"},
                },
            }
        ]
        state.edges = None

        with patch.object(service, "_run_pipeline"):
            run_id = await service.execute(session_id=session_id)
        assert isinstance(run_id, UUID)

    @pytest.mark.asyncio
    async def test_azure_search_managed_identity_provider_config_rejected_before_run(
        self,
        service: ExecutionServiceImpl,
        mock_session_service: MagicMock,
        mock_settings: MagicMock,
    ) -> None:
        """Web execution must not run user-authored RAG configs with server managed identity."""
        mock_settings.data_dir = "/tmp/elspeth_data"
        state = mock_session_service.get_current_state.return_value
        state.source = None
        state.outputs = None
        state.nodes = [
            {
                "id": "rag",
                "node_type": "transform",
                "plugin": "rag_retrieval",
                "input": "transform_in",
                "on_success": "results",
                "on_error": "discard",
                "options": {
                    "provider": "azure_search",
                    "provider_config": {
                        "endpoint": "https://tenant-b.search.windows.net",
                        "index": "payroll",
                        "use_managed_identity": True,
                    },
                },
            }
        ]
        state.edges = None

        with (
            patch.object(service, "_run_pipeline") as run_pipeline,
            pytest.raises(PipelineValidationError, match="managed identity"),
        ):
            await service.execute(session_id=uuid4())

        run_pipeline.assert_not_called()

    @pytest.mark.asyncio
    async def test_sequential_multi_query_llm_default_retry_budget_rejected_before_run(
        self,
        service: ExecutionServiceImpl,
        mock_session_service: MagicMock,
        mock_settings: MagicMock,
    ) -> None:
        """Web execution must not run LLM configs that inherit the hour-long sequential retry default."""
        mock_settings.data_dir = "/tmp/elspeth_data"
        state = mock_session_service.get_current_state.return_value
        node_id = "llm_review"
        prompt_template = "Classify {{ text }}."
        model = "openai/gpt-4o-mini"
        state.source = None
        state.outputs = None
        state.nodes = [
            {
                "id": node_id,
                "node_type": "transform",
                "plugin": "llm",
                "input": "transform_in",
                "on_success": "results",
                "on_error": "discard",
                "options": {
                    "provider": "openrouter",
                    "model": model,
                    "api_key": "test-key",
                    "prompt_template": prompt_template,
                    "schema": {"mode": "observed"},
                    "required_input_fields": [],
                    "queries": [{"name": "classify", "input_fields": {"text": "body"}}],
                    INTERPRETATION_REQUIREMENTS_KEY: self._resolved_llm_reviews(
                        node_id=node_id,
                        prompt_template=prompt_template,
                        model=model,
                    ),
                },
            }
        ]
        state.edges = None

        with (
            patch.object(service, "_run_pipeline") as run_pipeline,
            pytest.raises(PipelineValidationError, match="sequential multi-query LLM"),
        ):
            await service.execute(session_id=uuid4())

        run_pipeline.assert_not_called()

    @pytest.mark.asyncio
    async def test_pooled_multi_query_llm_numeric_string_pool_size_reaches_run_submission(
        self,
        service: ExecutionServiceImpl,
        mock_session_service: MagicMock,
        mock_settings: MagicMock,
    ) -> None:
        """Exact integer strings accepted by LLMConfig must not be blocked by the retry policy."""
        mock_settings.data_dir = "/tmp/elspeth_data"
        state = mock_session_service.get_current_state.return_value
        node_id = "llm_review"
        prompt_template = "Classify {{ text }}."
        model = "openai/gpt-4o-mini"
        state.source = None
        state.outputs = None
        state.nodes = [
            {
                "id": node_id,
                "node_type": "transform",
                "plugin": "llm",
                "input": "transform_in",
                "on_success": "results",
                "on_error": "discard",
                "options": {
                    "provider": "openrouter",
                    "model": model,
                    "api_key": "test-key",
                    "prompt_template": prompt_template,
                    "schema": {"mode": "observed"},
                    "required_input_fields": [],
                    "queries": [{"name": "classify", "input_fields": {"text": "body"}}],
                    "pool_size": "2.0",
                    INTERPRETATION_REQUIREMENTS_KEY: self._resolved_llm_reviews(
                        node_id=node_id,
                        prompt_template=prompt_template,
                        model=model,
                    ),
                },
            }
        ]
        state.edges = None

        with patch.object(service, "_run_pipeline"):
            run_id = await service.execute(session_id=uuid4())
        assert isinstance(run_id, UUID)

    @pytest.mark.asyncio
    async def test_non_rag_transform_without_provider_config_passes(
        self,
        service: ExecutionServiceImpl,
        mock_session_service: MagicMock,
        mock_settings: MagicMock,
    ) -> None:
        """Transform without provider_config (non-RAG) skips the nested check cleanly."""
        mock_settings.data_dir = "/tmp/elspeth_data"
        state = mock_session_service.get_current_state.return_value
        state.source = None
        state.outputs = None
        state.nodes = [
            {
                "id": "vt",
                "node_type": "transform",
                "plugin": "value_transform",
                "input": "transform_in",
                "on_success": "results",
                "on_error": "discard",
                "options": {"some_field": "value"},
            }
        ]
        state.edges = None

        with patch.object(service, "_run_pipeline"):
            run_id = await service.execute(session_id=uuid4())
        assert isinstance(run_id, UUID)


# ── Transform Framing Restriction ─────────────────────────────────────


class TestExecuteSemanticContractViolation:
    """Execution must reject transform pairings that violate semantic contracts.

    Replaces the legacy TestTransformFramingRestriction. The new
    SemanticContractViolationError carries structured ``entries`` and
    ``contracts`` records; the regex assertions still anchor on
    ``line_explode``/``Semantic contract`` because the diagnostic now
    names the consumer plugin and the contract code in the message,
    not the option that the operator must edit (``text_separator``).
    """

    @staticmethod
    def _set_web_scrape_line_explode_state(
        mock_session_service: MagicMock,
        *,
        session_id: UUID,
        scrape_options: dict[str, Any] | None = None,
    ) -> None:
        state = mock_session_service.get_current_state.return_value
        web_scrape_options = {
            "schema": {"mode": "flexible", "fields": ["url: str"]},
            "required_input_fields": ["url"],
            "url_field": "url",
            "content_field": "content",
            "fingerprint_field": "content_fingerprint",
            "format": "text",
            "fingerprint_mode": "content",
            "http": {
                "abuse_contact": "pipeline@example.com",
                "scraping_reason": "test scrape",
                "allowed_hosts": "public_only",
            },
        }
        web_scrape_options.update(scrape_options or {})
        state.source = {
            "plugin": "text",
            "on_success": "scrape_in",
            "options": {
                "path": f"blobs/{session_id}/urls.txt",
                "column": "url",
                "schema": {"mode": "fixed", "fields": ["url: str"]},
            },
            "on_validation_failure": "discard",
        }
        state.nodes = [
            {
                "id": "scrape_page",
                "node_type": "transform",
                "plugin": "web_scrape",
                "input": "scrape_in",
                "on_success": "explode_in",
                "on_error": "discard",
                "options": web_scrape_options,
            },
            {
                "id": "split_lines",
                "node_type": "transform",
                "plugin": "line_explode",
                "input": "explode_in",
                "on_success": "results",
                "on_error": "discard",
                "options": {
                    "schema": {
                        "mode": "flexible",
                        "fields": [
                            "url: str",
                            "content: str",
                            "content_fingerprint: str",
                        ],
                    },
                    "required_input_fields": ["content"],
                    "source_field": "content",
                    "output_field": "line",
                    "include_index": True,
                    "index_field": "line_index",
                },
            },
        ]
        state.edges = None
        state.outputs = [
            {
                "name": "results",
                "plugin": "json",
                "options": {"path": "outputs/lines.json", "format": "json"},
                "on_write_failure": "discard",
            }
        ]

    @pytest.mark.asyncio
    async def test_execute_rejects_compact_web_scrape_text_before_creating_run(
        self,
        service: ExecutionServiceImpl,
        mock_session_service: MagicMock,
        mock_settings: MagicMock,
    ) -> None:
        mock_settings.data_dir = "/tmp/elspeth_data"
        session_id = uuid4()
        self._set_web_scrape_line_explode_state(mock_session_service, session_id=session_id)

        # SemanticContractViolationError IS a ValueError, so legacy
        # ``except ValueError`` paths still catch it. New callers should
        # catch the specific type and read .entries/.contracts.
        with pytest.raises(ValueError, match="line_explode"):
            await service.execute(session_id=session_id)

        mock_session_service.create_run.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_execute_compact_text_raises_structured_exception(
        self,
        service: ExecutionServiceImpl,
        mock_session_service: MagicMock,
        mock_settings: MagicMock,
    ) -> None:
        """Verify the structured payload — the whole point of the new exception.

        Frontend banners and MCP error renderers consume entries and
        contracts directly; falling back to ``str(exc)`` parsing would
        make this surface as fragile as the pre-Phase-4 string concat.
        """
        from elspeth.web.execution.errors import SemanticContractViolationError

        mock_settings.data_dir = "/tmp/elspeth_data"
        session_id = uuid4()
        self._set_web_scrape_line_explode_state(mock_session_service, session_id=session_id)

        with pytest.raises(SemanticContractViolationError) as excinfo:
            await service.execute(session_id=session_id)

        exc = excinfo.value
        assert len(exc.entries) >= 1
        assert any("Semantic contract" in e.message for e in exc.entries)
        assert any(c.outcome.value == "conflict" for c in exc.contracts)
        assert any(c.consumer_plugin == "line_explode" for c in exc.contracts)

    @pytest.mark.asyncio
    async def test_execute_allows_newline_framed_web_scrape_text(
        self,
        service: ExecutionServiceImpl,
        mock_session_service: MagicMock,
        mock_settings: MagicMock,
    ) -> None:
        mock_settings.data_dir = "/tmp/elspeth_data"
        session_id = uuid4()
        self._set_web_scrape_line_explode_state(
            mock_session_service,
            session_id=session_id,
            scrape_options={"text_separator": "\n"},
        )

        with patch.object(service, "_run_pipeline"):
            run_id = await service.execute(session_id=session_id)

        assert isinstance(run_id, UUID)


# ── F-17 / F-21: Unresolved Interpretation Placeholder Gate ─────────────


class TestExecuteUnresolvedInterpretationPlaceholderGate:
    """``/execute`` must refuse to run an LLM transform whose prompt_template
    still carries ``{{interpretation:<term>}}`` placeholders (F-17 / F-21 —
    Phase 5b Task 5 follow-on).

    Operates under the operator-acknowledged assumption that 18a Task 0
    (empirical LLM gate ≥ 8/10 staging runs emit
    ``{{interpretation:<term>}}``) passes; this gate is the runtime-safety
    net catching cases where the LLM under-fires.
    """

    @staticmethod
    def _set_unresolved_placeholder_state(
        mock_session_service: MagicMock,
        *,
        term: str = "cool",
        node_id: str = "rate_node",
    ) -> None:
        state = mock_session_service.get_current_state.return_value
        state.source = {
            "plugin": "csv",
            "on_success": "rate_in",
            "options": {"path": "blobs/rows.csv"},
            "on_validation_failure": "discard",
        }
        state.nodes = [
            {
                "id": node_id,
                "node_type": "transform",
                "plugin": "llm",
                "input": "rate_in",
                "on_success": "results",
                "on_error": "discard",
                "options": {
                    "prompt_template": f"Rate {{{{interpretation:{term}}}}} aspects.",
                    "model": "test-model",
                    # Pre-resolve the model-choice review so this fixture
                    # exercises ONLY the unresolved-vague-term /
                    # unresolved-prompt-template gates the test class
                    # targets. Without this, the auto-enumerated
                    # model-choice site shows up as a third pending
                    # interpretation and contaminates the gate's
                    # observed telemetry list.
                    INTERPRETATION_REQUIREMENTS_KEY: [
                        {
                            "id": f"model_choice_review:{node_id}",
                            "kind": "llm_model_choice",
                            "user_term": f"llm_model_choice:{node_id}",
                            "status": "resolved",
                            "draft": "test-model",
                            "event_id": "model-choice-accepted",
                            "accepted_value": "test-model",
                            "accepted_artifact_hash": None,
                            "resolved_prompt_template_hash": stable_hash("test-model"),
                        }
                    ],
                },
            }
        ]
        state.edges = None
        state.outputs = [
            {
                "name": "results",
                "plugin": "json",
                "options": {"path": "outputs/scored.json", "format": "json"},
                "on_write_failure": "discard",
            }
        ]

    @staticmethod
    def _set_structured_pending_interpretation_state(
        mock_session_service: MagicMock,
        *,
        term: str = "cool",
        node_id: str = "rate_node",
    ) -> None:
        state = mock_session_service.get_current_state.return_value
        state.source = {
            "plugin": "csv",
            "on_success": "rate_in",
            "options": {"path": "blobs/rows.csv"},
            "on_validation_failure": "discard",
        }
        state.nodes = [
            {
                "id": node_id,
                "node_type": "transform",
                "plugin": "llm",
                "input": "rate_in",
                "on_success": "results",
                "on_error": "discard",
                "options": {
                    "prompt_template": "Rate pending interpretation aspects.",
                    "model": "test-model",
                    PROMPT_TEMPLATE_PARTS_KEY: [
                        {"kind": "text", "text": "Rate "},
                        {"kind": "interpretation_ref", "requirement_id": term},
                        {"kind": "text", "text": " aspects."},
                    ],
                    INTERPRETATION_REQUIREMENTS_KEY: [
                        {
                            "id": term,
                            "kind": "vague_term",
                            "user_term": term,
                            "status": "pending",
                            "draft": "visually appealing",
                            "event_id": "event-1",
                            "accepted_value": None,
                            "accepted_artifact_hash": None,
                            "resolved_prompt_template_hash": None,
                        },
                        # Pre-resolve the model-choice review so this
                        # fixture exercises only the structured pending
                        # vague_term scenario.
                        {
                            "id": f"model_choice_review:{node_id}",
                            "kind": "llm_model_choice",
                            "user_term": f"llm_model_choice:{node_id}",
                            "status": "resolved",
                            "draft": "test-model",
                            "event_id": "model-choice-accepted",
                            "accepted_value": "test-model",
                            "accepted_artifact_hash": None,
                            "resolved_prompt_template_hash": stable_hash("test-model"),
                        },
                    ],
                },
            }
        ]
        state.edges = None
        state.outputs = [
            {
                "name": "results",
                "plugin": "json",
                "options": {"path": "outputs/scored.json", "format": "json"},
                "on_write_failure": "discard",
            }
        ]

    @pytest.mark.asyncio
    async def test_execute_rejects_unresolved_placeholder_before_creating_run(
        self,
        service: ExecutionServiceImpl,
        mock_session_service: MagicMock,
        mock_settings: MagicMock,
    ) -> None:
        """F-17: an unresolved placeholder blocks execution and raises a typed error.

        The detector runs AFTER semantic-contract validation and BEFORE
        path-allowlist / YAML generation, so the gate fires before any
        ``Run`` row is created in the sessions DB.
        """
        from elspeth.web.execution.errors import UnresolvedInterpretationPlaceholderError

        mock_settings.data_dir = "/tmp/elspeth_data"
        self._set_unresolved_placeholder_state(mock_session_service)

        with pytest.raises(UnresolvedInterpretationPlaceholderError) as excinfo:
            await service.execute(session_id=uuid4())

        # The typed payload carries (node_id, term) — no prompt_template.
        assert excinfo.value.placeholders == (("rate_node", "cool"),)

        # The actionable message names both the term and the node so the
        # frontend banner / MCP error renderer can echo it directly.
        assert "{{interpretation:cool}}" in str(excinfo.value)
        assert "rate_node" in str(excinfo.value)

        # No Run was created (fail-fast before run record persistence).
        mock_session_service.create_run.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_execute_rejects_structured_pending_interpretation_before_creating_run(
        self,
        service: ExecutionServiceImpl,
        mock_session_service: MagicMock,
        mock_settings: MagicMock,
    ) -> None:
        from elspeth.web.execution.errors import UnresolvedInterpretationPlaceholderError

        mock_settings.data_dir = "/tmp/elspeth_data"
        self._set_structured_pending_interpretation_state(mock_session_service)

        with patch.object(service, "_run_pipeline"), pytest.raises(UnresolvedInterpretationPlaceholderError) as excinfo:
            await service.execute(session_id=uuid4())

        assert excinfo.value.placeholders == (("rate_node", "cool"),)
        mock_session_service.create_run.assert_not_awaited()

    @staticmethod
    def _set_drifted_invented_source_state(
        mock_session_service: MagicMock,
        *,
        node_id: str = "rate_node",
    ) -> None:
        """LLM-authored source whose content_hash drifted after its review.

        The invented_source requirement is RESOLVED, but the accepted artifact
        hash no longer matches the current source content_hash. This is a
        readiness blocker — it must surface as a structured interpretation
        review (UnresolvedInterpretationPlaceholderError -> HTTP 422), NOT as a
        bare ValueError that the route layer mis-maps to a 404. The transform
        node carries no pending interpretation so the source drift is the only
        site (elspeth-5a94855935).
        """
        from elspeth.web.interpretation_state import SOURCE_AUTHORING_KEY

        state = mock_session_service.get_current_state.return_value
        state.source = {
            "plugin": "json",
            "on_success": "rate_in",
            "on_validation_failure": "discard",
            "options": {
                "path": "blobs/rows.json",
                "format": "json",
                SOURCE_AUTHORING_KEY: {
                    "modality": "llm_generated",
                    "content_hash": "a" * 64,
                    "review_event_id": "event-1",
                    "resolved_kind": "invented_source",
                },
                INTERPRETATION_REQUIREMENTS_KEY: [
                    {
                        "id": "source-urls",
                        "kind": "invented_source",
                        "user_term": "inline_source_url_list",
                        "status": "resolved",
                        "draft": "https://example.gov.au",
                        "event_id": "event-1",
                        "accepted_value": "accepted source artifact",
                        "accepted_artifact_hash": "b" * 64,
                        "resolved_prompt_template_hash": None,
                    }
                ],
            },
        }
        state.nodes = [
            {
                "id": node_id,
                "node_type": "transform",
                "plugin": "llm",
                "input": "rate_in",
                "on_success": "results",
                "on_error": "discard",
                "options": {
                    "prompt_template": "Rate the rows.",
                    "model": "test-model",
                    INTERPRETATION_REQUIREMENTS_KEY: [
                        # Both node-level reviews are pre-resolved so the source
                        # drift is the ONLY pending site. Pre-fix this means
                        # materialize reaches _materialize_source_for_execution
                        # and raises a bare ValueError (the defect), rather than
                        # short-circuiting on a node site.
                        {
                            "id": f"model_choice_review:{node_id}",
                            "kind": "llm_model_choice",
                            "user_term": f"llm_model_choice:{node_id}",
                            "status": "resolved",
                            "draft": "test-model",
                            "event_id": "model-choice-accepted",
                            "accepted_value": "test-model",
                            "accepted_artifact_hash": None,
                            "resolved_prompt_template_hash": stable_hash("test-model"),
                        },
                        {
                            "id": f"prompt_template_review:{node_id}",
                            "kind": "llm_prompt_template",
                            "user_term": f"llm_prompt_template:{node_id}",
                            "status": "resolved",
                            "draft": "Rate the rows.",
                            "event_id": "prompt-template-accepted",
                            "accepted_value": "Rate the rows.",
                            "accepted_artifact_hash": None,
                            "resolved_prompt_template_hash": stable_hash("Rate the rows."),
                        },
                    ],
                },
            }
        ]
        state.edges = None
        state.outputs = [
            {
                "name": "results",
                "plugin": "json",
                "options": {"path": "outputs/scored.json", "format": "json"},
                "on_write_failure": "discard",
            }
        ]

    @pytest.mark.asyncio
    async def test_execute_rejects_drifted_invented_source_as_pending_review(
        self,
        service: ExecutionServiceImpl,
        mock_session_service: MagicMock,
        mock_settings: MagicMock,
    ) -> None:
        """elspeth-5a94855935: a resolved invented_source whose content_hash
        drifted after review is rejected with the same contract as any other
        pending interpretation review (typed error -> HTTP 422), NOT a bare
        ValueError that the route layer mis-maps to a 404."""
        from elspeth.web.execution.errors import UnresolvedInterpretationPlaceholderError

        mock_settings.data_dir = "/tmp/elspeth_data"
        self._set_drifted_invented_source_state(mock_session_service)

        with patch.object(service, "_run_pipeline"), pytest.raises(UnresolvedInterpretationPlaceholderError) as excinfo:
            await service.execute(session_id=uuid4())

        source_sites = [s for s in excinfo.value.sites if s.component_type == "source"]
        assert len(source_sites) == 1
        assert source_sites[0].kind.value == "invented_source"
        assert source_sites[0].component_id == "source"
        mock_session_service.create_run.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_execute_rejects_pending_named_invented_source_before_creating_run(
        self,
        service: ExecutionServiceImpl,
        mock_session_service: MagicMock,
        mock_settings: MagicMock,
    ) -> None:
        from elspeth.web.execution.errors import UnresolvedInterpretationPlaceholderError
        from elspeth.web.interpretation_state import SOURCE_AUTHORING_KEY

        mock_settings.data_dir = "/tmp/elspeth_data"
        state = mock_session_service.get_current_state.return_value
        state.source = None
        state.sources = {
            "orders": {
                "plugin": "json",
                "on_success": "rows",
                "on_validation_failure": "discard",
                "options": {
                    SOURCE_AUTHORING_KEY: {
                        "modality": "llm_generated",
                        "content_hash": "a" * 64,
                        "review_event_id": None,
                        "resolved_kind": None,
                    },
                    INTERPRETATION_REQUIREMENTS_KEY: [
                        {
                            "id": "source-review",
                            "kind": "invented_source",
                            "user_term": "inline_source_data",
                            "status": "pending",
                            "draft": "generated rows",
                            "event_id": None,
                            "accepted_value": None,
                            "accepted_artifact_hash": None,
                            "resolved_prompt_template_hash": None,
                        }
                    ],
                },
            }
        }
        state.nodes = []
        state.edges = []
        state.outputs = []

        with patch.object(service, "_run_pipeline"), pytest.raises(UnresolvedInterpretationPlaceholderError) as excinfo:
            await service.execute(session_id=uuid4())

        assert [(site.component_id, site.kind.value) for site in excinfo.value.sites] == [("source:orders", "invented_source")]
        mock_session_service.create_run.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_execute_emits_telemetry_per_unresolved_site(
        self,
        service: ExecutionServiceImpl,
        mock_session_service: MagicMock,
        mock_settings: MagicMock,
    ) -> None:
        """F-21: each unresolved interpretation site emits one counter increment.

        Attributes MUST identify kind and component without including the
        prompt_template value (which may carry user-supplied content —
        operational telemetry must be PII-clean).
        """
        from elspeth.web.execution.errors import UnresolvedInterpretationPlaceholderError
        from elspeth.web.sessions.telemetry import _FakeCounter

        mock_settings.data_dir = "/tmp/elspeth_data"
        self._set_unresolved_placeholder_state(mock_session_service)

        with pytest.raises(UnresolvedInterpretationPlaceholderError):
            await service.execute(session_id=uuid4())

        counter = service._telemetry.interpretation_placeholder_unresolved_at_runtime_total
        # Test fixture uses fake counters — type-narrow to access ``calls``.
        assert isinstance(counter, _FakeCounter)
        assert len(counter.calls) == 2
        observed = []
        for amount, attrs, _context in counter.calls:
            assert amount == 1
            observed.append(attrs)
        assert observed == [
            {
                "component_id": "rate_node",
                "component_type": "transform",
                "kind": "vague_term",
            },
            {
                "component_id": "rate_node",
                "component_type": "transform",
                "kind": "llm_prompt_template",
            },
        ]
        # Explicit negative assertion: prompt/user-authored text must
        # never appear in telemetry attributes.
        for attrs in observed:
            assert attrs is not None
            assert "prompt_template" not in attrs
            assert "user_term" not in attrs
            assert "cool" not in attrs.values()

    @pytest.mark.asyncio
    async def test_execute_passes_when_placeholder_resolved(
        self,
        service: ExecutionServiceImpl,
        mock_session_service: MagicMock,
        mock_settings: MagicMock,
    ) -> None:
        """An LLM transform whose prompt_template has no placeholder runs normally.

        Negative-space test: confirms the gate does not fire spuriously
        when the compose loop did its job and the placeholder was
        replaced by a concrete term via the interpretation_events
        resolve flow.
        """
        from elspeth.web.sessions.telemetry import _FakeCounter

        mock_settings.data_dir = "/tmp/elspeth_data"
        session_id = uuid4()
        prompt = "Rate visually-appealing aspects."
        state = mock_session_service.get_current_state.return_value
        state.source = {
            "plugin": "csv",
            "on_success": "rate_in",
            "options": {"path": f"blobs/{session_id}/rows.csv"},
            "on_validation_failure": "discard",
        }
        state.nodes = [
            {
                "id": "rate_node",
                "node_type": "transform",
                "plugin": "llm",
                "input": "rate_in",
                "on_success": "results",
                "on_error": "discard",
                "options": {
                    # Placeholder resolved — no ``{{interpretation:…}}`` text.
                    "prompt_template": prompt,
                    "model": "test-model",
                    "resolved_prompt_template_hash": stable_hash(prompt),
                    INTERPRETATION_REQUIREMENTS_KEY: [
                        {
                            "id": "prompt-template-review",
                            "kind": "llm_prompt_template",
                            "user_term": "rating prompt",
                            "status": "resolved",
                            "draft": prompt,
                            "event_id": "event-2",
                            "accepted_value": prompt,
                            "accepted_artifact_hash": None,
                            "resolved_prompt_template_hash": stable_hash(prompt),
                        },
                        # Model-choice review also resolved — the gate fires
                        # on any unresolved llm_model_choice site so the
                        # "all reviews resolved" negative-space test must
                        # cover this requirement explicitly.
                        {
                            "id": "model-choice-review",
                            "kind": "llm_model_choice",
                            "user_term": "llm_model_choice:rate_node",
                            "status": "resolved",
                            "draft": "test-model",
                            "event_id": "event-3",
                            "accepted_value": "test-model",
                            "accepted_artifact_hash": None,
                            "resolved_prompt_template_hash": stable_hash("test-model"),
                        },
                    ],
                },
            }
        ]
        state.edges = None
        state.outputs = [
            {
                "name": "results",
                "plugin": "json",
                "options": {"path": "outputs/scored.json", "format": "json"},
                "on_write_failure": "discard",
            }
        ]

        # The mock csv source resolves to no real file, so its cardinality is
        # statically unknown — which now (correctly) trips the conservative
        # fanout guard (elspeth-6421ffa028). This test covers the interpretation
        # placeholder gate, not fanout, so isolate it from the guard here; the
        # unknown-cardinality guard behavior is covered by TestExecutionFanoutGuard.
        with (
            patch.object(service, "_run_pipeline"),
            patch("elspeth.web.execution.service.evaluate_execution_fanout_guard", return_value=None),
        ):
            run_id = await service.execute(session_id=session_id)

        assert isinstance(run_id, UUID)
        # Counter was NOT incremented.
        counter = service._telemetry.interpretation_placeholder_unresolved_at_runtime_total
        assert isinstance(counter, _FakeCounter)
        assert counter.calls == []


# ── Relative Path Resolution ──────────────────────────────────────────


class TestRelativePathResolution:
    """Path resolution must use data_dir as the base for relative paths.

    Without this, ``Path(value).resolve()`` resolves against the server's CWD,
    which diverges from the validation layer's behaviour.
    """

    @pytest.mark.asyncio
    async def test_relative_sink_path_resolves_against_data_dir(
        self,
        service: ExecutionServiceImpl,
        mock_session_service: MagicMock,
        mock_settings: MagicMock,
    ) -> None:
        """Sink with a relative path under outputs/ passes when resolved against data_dir."""
        mock_settings.data_dir = "/tmp/elspeth_data"
        state = mock_session_service.get_current_state.return_value
        state.source = None
        state.outputs = [
            {
                "name": "primary",
                "plugin": "csv",
                "options": {"path": "outputs/result.csv"},
                "on_write_failure": "discard",
            }
        ]
        state.nodes = None
        state.edges = None

        with patch.object(service, "_run_pipeline"):
            run_id = await service.execute(session_id=uuid4())
        assert isinstance(run_id, UUID)

    @pytest.mark.asyncio
    async def test_relative_source_path_resolves_against_data_dir(
        self,
        service: ExecutionServiceImpl,
        mock_session_service: MagicMock,
        mock_settings: MagicMock,
    ) -> None:
        """Source with a relative path under blobs/ passes when resolved against data_dir."""
        mock_settings.data_dir = "/tmp/elspeth_data"
        session_id = uuid4()
        state = mock_session_service.get_current_state.return_value
        state.source = {
            "plugin": "csv",
            "on_success": "continue",
            "options": {"path": f"blobs/{session_id}/data.csv"},
            "on_validation_failure": "quarantine",
        }
        state.outputs = None
        state.nodes = None
        state.edges = None

        with patch.object(service, "_run_pipeline"):
            run_id = await service.execute(session_id=session_id)
        assert isinstance(run_id, UUID)

    @pytest.mark.asyncio
    async def test_second_named_source_path_outside_allowed_dirs_raises(
        self,
        service: ExecutionServiceImpl,
        mock_session_service: MagicMock,
        mock_settings: MagicMock,
    ) -> None:
        """Every named source path must pass the direct /execute allowlist."""
        mock_settings.data_dir = "/tmp/elspeth_data"
        session_id = uuid4()
        state = mock_session_service.get_current_state.return_value
        state.source = {
            "plugin": "csv",
            "on_success": "orders_out",
            "options": {"path": f"blobs/{session_id}/orders.csv"},
            "on_validation_failure": "quarantine",
        }
        state.sources = {
            "orders": state.source,
            "refunds": {
                "plugin": "csv",
                "on_success": "refunds_out",
                "options": {"path": "/etc/passwd"},
                "on_validation_failure": "quarantine",
            },
        }
        state.outputs = None
        state.nodes = None
        state.edges = None

        from elspeth.web.execution.errors import PathAllowlistViolationError

        with pytest.raises(PathAllowlistViolationError, match=r"Source 'refunds'.*resolves outside allowed directories"):
            await service.execute(session_id=session_id)

    @pytest.mark.asyncio
    async def test_relative_traversal_still_blocked(
        self,
        service: ExecutionServiceImpl,
        mock_session_service: MagicMock,
        mock_settings: MagicMock,
    ) -> None:
        """Source with ../ traversal is rejected even when relative."""
        mock_settings.data_dir = "/tmp/elspeth_data"
        state = mock_session_service.get_current_state.return_value
        state.source = {
            "plugin": "csv",
            "on_success": "continue",
            "options": {"path": "../etc/passwd"},
            "on_validation_failure": "quarantine",
        }
        state.outputs = None
        state.nodes = None
        state.edges = None

        from elspeth.web.execution.errors import PathAllowlistViolationError

        with pytest.raises(PathAllowlistViolationError, match="resolves outside allowed directories"):
            await service.execute(session_id=uuid4())


# ── Edge Compatibility in _run_pipeline ───────────────────────────────


@pytest.mark.usefixtures("mock_pipeline_config_assembly")
class TestEdgeCompatibility:
    """P2 fix: _run_pipeline must call validate_edge_compatibility() so that
    schema-incompatible pipelines are rejected before execution begins."""

    @patch("elspeth.web.execution.service.Orchestrator")
    @patch("elspeth.web.execution.service.load_settings_from_yaml_string")
    @patch("elspeth.web.execution.preflight.instantiate_plugins_from_config")
    @patch("elspeth.web.execution.preflight.ExecutionGraph")
    @patch("elspeth.web.execution.service.open_landscape_db")
    @patch("elspeth.web.execution.service.FilesystemPayloadStore")
    def test_validate_edge_compatibility_called(
        self,
        mock_payload: MagicMock,
        mock_landscape: MagicMock,
        mock_graph_cls: MagicMock,
        mock_instantiate: MagicMock,
        mock_load: MagicMock,
        mock_orch_cls: MagicMock,
        service: ExecutionServiceImpl,
    ) -> None:
        """_run_pipeline must call graph.validate_edge_compatibility()
        after graph.validate() to catch schema mismatches."""
        mock_orch = _configure_runtime_success(
            mock_load=mock_load,
            mock_instantiate=mock_instantiate,
            mock_graph_cls=mock_graph_cls,
            mock_orch_cls=mock_orch_cls,
        )
        mock_graph = mock_graph_cls.from_plugin_instances.return_value
        assert mock_orch is not None

        with patch(
            "elspeth.web.execution.service.load_run_accounting_from_db",
            return_value=_run_accounting_for_status(RunStatus.COMPLETED),
        ):
            service._run_pipeline(str(uuid4()), "source:\n  plugin: csv", threading.Event())

        mock_graph.validate.assert_called_once()
        mock_graph.validate_edge_compatibility.assert_called_once()

    @patch("elspeth.web.execution.service.load_settings_from_yaml_string")
    @patch("elspeth.web.execution.preflight.instantiate_plugins_from_config")
    @patch("elspeth.web.execution.preflight.ExecutionGraph")
    @patch("elspeth.web.execution.service.open_landscape_db")
    @patch("elspeth.web.execution.service.FilesystemPayloadStore")
    def test_edge_compatibility_failure_crashes_pipeline(
        self,
        mock_payload: MagicMock,
        mock_landscape: MagicMock,
        mock_graph_cls: MagicMock,
        mock_instantiate: MagicMock,
        mock_load: MagicMock,
        service: ExecutionServiceImpl,
    ) -> None:
        """If edge compatibility fails, the pipeline must not execute."""
        from elspeth.core.dag.models import GraphValidationError

        mock_load.return_value = _mock_pipeline_settings()
        mock_instantiate.return_value = _plugin_bundle_stub()
        mock_graph = _execution_graph_stub()
        mock_graph_cls.from_plugin_instances.return_value = mock_graph
        mock_graph.validate_edge_compatibility.side_effect = GraphValidationError(
            "Schema mismatch: source outputs str but transform expects int"
        )

        with pytest.raises(GraphValidationError, match="Schema mismatch"):
            service._run_pipeline(str(uuid4()), _TEST_PIPELINE_YAML, threading.Event())


# ── Blob Finalization Catch Widening ──────────────────────────────────


def _make_strict_call_async() -> tuple[Callable[[Coroutine[Any, Any, Any]], Any], asyncio.AbstractEventLoop]:
    """Create a _call_async bridge that propagates all exceptions faithfully.

    The standard test fixture's _mock_call_async catches RuntimeError to
    handle "event loop is closed" issues. For finalize tests, that masks
    the exact exception we're trying to test. This version propagates
    everything.
    """
    loop = asyncio.new_event_loop()

    def _call_async(coro: Coroutine[Any, Any, Any]) -> Any:
        return loop.run_until_complete(coro)

    return _call_async, loop


class TestFinalizeOutputBlobsCatchWidening:
    """Bug: elspeth-25df1be367 — _finalize_output_blobs only catches
    OSError and SQLAlchemyError, but finalize_run_output_blobs can raise
    BlobNotFoundError and RuntimeError from _finalize_blob_sync.

    These escaping exceptions trigger a second terminal event via the
    outer except BaseException, violating the "exactly one terminal state"
    invariant.

    Uses a strict _call_async that does NOT swallow RuntimeError (unlike
    the standard test fixture).
    """

    @pytest.fixture(autouse=True)
    def _cleanup_loops(self) -> Iterator[None]:
        self._loops_to_close: list[asyncio.AbstractEventLoop] = []
        yield
        for loop in self._loops_to_close:
            loop.close()

    def _make_service_with_blob(
        self, blob_service: BlobServiceProtocol, mock_settings: MagicMock, mock_session_service: MagicMock
    ) -> ExecutionServiceImpl:
        svc = ExecutionServiceImpl.for_trained_operator(
            loop=MagicMock(spec=asyncio.AbstractEventLoop),
            broadcaster=MagicMock(spec=ProgressBroadcaster),
            settings=mock_settings,
            session_service=mock_session_service,
            yaml_generator=_YamlGeneratorStub(),
            telemetry=build_sessions_telemetry(),
            blob_service=blob_service,
        )
        call_async, loop = _make_strict_call_async()
        self._loops_to_close.append(loop)
        cast(Any, svc)._call_async = call_async
        return svc

    def test_suppresses_blob_not_found_error(self, mock_settings: MagicMock, mock_session_service: MagicMock) -> None:
        from elspeth.web.blobs.protocol import BlobNotFoundError

        blob_service = _blob_service_stub()
        blob_service.finalize_run_output_blobs.side_effect = BlobNotFoundError("missing-blob")
        svc = self._make_service_with_blob(blob_service, mock_settings, mock_session_service)
        svc._finalize_output_blobs(str(uuid4()), success=True)

    def test_propagates_runtime_error_from_blob_lifecycle(self, mock_settings: MagicMock, mock_session_service: MagicMock) -> None:
        """RuntimeError is no longer suppressed — it's too broad and would
        catch Tier 1 anomaly signals.  Blob lifecycle errors should use
        BlobStateError or BlobNotFoundError instead.
        """
        blob_service = _blob_service_stub()
        blob_service.finalize_run_output_blobs.side_effect = RuntimeError("Cannot finalize — status is 'ready', expected 'pending'")
        svc = self._make_service_with_blob(blob_service, mock_settings, mock_session_service)
        with pytest.raises(RuntimeError, match="Cannot finalize"):
            svc._finalize_output_blobs(str(uuid4()), success=True)

    def test_suppresses_blob_quota_exceeded_error(self, mock_settings: MagicMock, mock_session_service: MagicMock) -> None:
        from elspeth.web.blobs.protocol import BlobQuotaExceededError

        blob_service = _blob_service_stub()
        blob_service.finalize_run_output_blobs.side_effect = BlobQuotaExceededError("sess-1", current_bytes=100, limit_bytes=50)
        svc = self._make_service_with_blob(blob_service, mock_settings, mock_session_service)
        svc._finalize_output_blobs(str(uuid4()), success=True)

    def test_propagates_type_error(self, mock_settings: MagicMock, mock_session_service: MagicMock) -> None:
        """Programmer bugs (TypeError, AttributeError, etc.) must still crash."""
        blob_service = _blob_service_stub()
        blob_service.finalize_run_output_blobs.side_effect = TypeError("unexpected keyword argument")
        svc = self._make_service_with_blob(blob_service, mock_settings, mock_session_service)
        with pytest.raises(TypeError, match="unexpected keyword argument"):
            svc._finalize_output_blobs(str(uuid4()), success=True)

    def test_propagates_attribute_error(self, mock_settings: MagicMock, mock_session_service: MagicMock) -> None:
        """AttributeError is a programmer bug — must crash."""
        blob_service = _blob_service_stub()
        blob_service.finalize_run_output_blobs.side_effect = AttributeError("'NoneType' object has no attribute 'id'")
        svc = self._make_service_with_blob(blob_service, mock_settings, mock_session_service)
        with pytest.raises(AttributeError):
            svc._finalize_output_blobs(str(uuid4()), success=True)


# ── Terminal Ordering Invariant ───────────────────────────────────────


def _collect_terminal_types(mock_broadcaster: MagicMock) -> list[str]:
    """Extract terminal event types from a mock broadcaster's call log."""
    terminals = []
    for call in mock_broadcaster.broadcast.call_args_list:
        _, event = call[0]
        if event.event_type in ("completed", "failed", "cancelled"):
            terminals.append(event.event_type)
    return terminals


@pytest.mark.usefixtures("mock_pipeline_config_assembly")
class TestTerminalOrderingInvariant:
    """Bug: elspeth-25df1be367 — run termination is published before output
    blob finalization. A late finalize failure triggers a second terminal event
    via except BaseException.

    CLAUDE.md invariant: "Every row reaches exactly one terminal state."
    """

    @patch("elspeth.web.execution.service.Orchestrator")
    @patch("elspeth.web.execution.service.load_settings_from_yaml_string")
    @patch("elspeth.web.execution.preflight.instantiate_plugins_from_config")
    @patch("elspeth.web.execution.preflight.ExecutionGraph")
    @patch("elspeth.web.execution.service.open_landscape_db")
    @patch("elspeth.web.execution.service.FilesystemPayloadStore")
    def test_single_terminal_when_finalize_raises_blob_not_found(
        self,
        mock_payload: MagicMock,
        mock_landscape: MagicMock,
        mock_graph_cls: MagicMock,
        mock_instantiate: MagicMock,
        mock_load: MagicMock,
        mock_orch_cls: MagicMock,
        mock_settings: MagicMock,
        mock_session_service: MagicMock,
    ) -> None:
        """When finalize_run_output_blobs raises BlobNotFoundError after
        a successful orchestrator.run(), exactly one terminal event must
        be broadcast — not completed-then-failed."""
        from elspeth.web.blobs.protocol import BlobNotFoundError

        _configure_runtime_success(
            mock_load=mock_load,
            mock_instantiate=mock_instantiate,
            mock_graph_cls=mock_graph_cls,
            mock_orch_cls=mock_orch_cls,
            result=_orchestrator_result_stub(
                run_id="landscape-run-1",
                status=RunStatus.COMPLETED_WITH_FAILURES,
                rows_processed=10,
                rows_succeeded=9,
                rows_failed=1,
            ),
        )

        mock_broadcaster = MagicMock(spec=ProgressBroadcaster)
        blob_service = _blob_service_stub()
        blob_service.finalize_run_output_blobs.side_effect = BlobNotFoundError("blob-vanished")

        svc = ExecutionServiceImpl.for_trained_operator(
            loop=MagicMock(spec=asyncio.AbstractEventLoop),
            broadcaster=mock_broadcaster,
            settings=mock_settings,
            session_service=mock_session_service,
            yaml_generator=_YamlGeneratorStub(),
            telemetry=build_sessions_telemetry(),
            blob_service=blob_service,
        )
        _real_loop = asyncio.new_event_loop()
        try:
            cast(Any, svc)._call_async = lambda coro: _real_loop.run_until_complete(coro)

            with contextlib.suppress(Exception):
                svc._run_pipeline(str(uuid4()), _TEST_PIPELINE_YAML, threading.Event())

            terminals = _collect_terminal_types(mock_broadcaster)
            assert len(terminals) == 1, (
                f"Exactly one terminal event expected, got {terminals}. A finalize failure must not trigger a second terminal broadcast."
            )
        finally:
            _real_loop.close()

    @patch("elspeth.web.execution.service.Orchestrator")
    @patch("elspeth.web.execution.service.load_settings_from_yaml_string")
    @patch("elspeth.web.execution.preflight.instantiate_plugins_from_config")
    @patch("elspeth.web.execution.preflight.ExecutionGraph")
    @patch("elspeth.web.execution.service.open_landscape_db")
    @patch("elspeth.web.execution.service.FilesystemPayloadStore")
    def test_externally_cancelled_run_emits_single_cancelled_terminal(
        self,
        mock_payload: MagicMock,
        mock_landscape: MagicMock,
        mock_graph_cls: MagicMock,
        mock_instantiate: MagicMock,
        mock_load: MagicMock,
        mock_orch_cls: MagicMock,
        mock_settings: MagicMock,
        mock_session_service: MagicMock,
    ) -> None:
        """When a run completes but the DB status is already 'cancelled'
        (external orphan cleanup raced), exactly one terminal event must
        be emitted — not completed-then-cancelled."""
        _configure_runtime_success(
            mock_load=mock_load,
            mock_instantiate=mock_instantiate,
            mock_graph_cls=mock_graph_cls,
            mock_orch_cls=mock_orch_cls,
            result=_orchestrator_result_stub(
                run_id="landscape-run-2",
                rows_processed=5,
                rows_succeeded=5,
            ),
        )

        mock_broadcaster = MagicMock(spec=ProgressBroadcaster)

        # Simulate external cancel: update_run_status("running") succeeds,
        # then update_run_status("completed") raises ValueError because
        # orphan cleanup already set the DB status to "cancelled".
        async def _selective_update(run_id, *, status="", **kwargs):
            if status == "completed":
                raise IllegalRunTransitionError("cancelled", "completed", frozenset())
            return None

        mock_session_service.update_run_status.side_effect = _selective_update
        mock_session_service.get_run.return_value = _run_record_stub(status="cancelled")

        svc = ExecutionServiceImpl.for_trained_operator(
            loop=MagicMock(spec=asyncio.AbstractEventLoop),
            broadcaster=mock_broadcaster,
            settings=mock_settings,
            session_service=mock_session_service,
            yaml_generator=_YamlGeneratorStub(),
            telemetry=build_sessions_telemetry(),
        )
        _real_loop = asyncio.new_event_loop()
        try:
            cast(Any, svc)._call_async = lambda coro: _real_loop.run_until_complete(coro)

            svc._run_pipeline(str(uuid4()), _TEST_PIPELINE_YAML, threading.Event())

            terminals = _collect_terminal_types(mock_broadcaster)
            assert len(terminals) == 1, (
                f"Exactly one terminal event expected, got {terminals}. "
                "External cancellation must produce a single 'cancelled', "
                "not 'completed' followed by 'cancelled'."
            )
            assert terminals[0] == "cancelled", f"Terminal should be 'cancelled' (DB is authoritative), got '{terminals[0]}'."
        finally:
            _real_loop.close()


# ── Session Lock Cleanup ──────────────────────────────────────────────


class TestSessionLockCleanup:
    """Tests that cleanup_session_lock removes per-session asyncio.Lock entries."""

    def test_cleanup_removes_existing_lock(self, service: ExecutionServiceImpl) -> None:
        """cleanup_session_lock removes the lock for a known session."""
        session_id = str(uuid4())
        service._session_locks[session_id] = asyncio.Lock()
        service.cleanup_session_lock(session_id)
        assert session_id not in service._session_locks

    def test_cleanup_noop_for_unknown_session(self, service: ExecutionServiceImpl) -> None:
        """cleanup_session_lock is a no-op for an unknown session."""
        service.cleanup_session_lock("nonexistent")  # Should not raise

    def test_cleanup_does_not_affect_other_sessions(self, service: ExecutionServiceImpl) -> None:
        """Cleaning up one session leaves other sessions' locks intact."""
        session_a = str(uuid4())
        session_b = str(uuid4())
        service._session_locks[session_a] = asyncio.Lock()
        service._session_locks[session_b] = asyncio.Lock()
        service.cleanup_session_lock(session_a)
        assert session_a not in service._session_locks
        assert session_b in service._session_locks


# ── T1: _sanitize_error_for_client ────────────────────────────────────


class TestSanitizeErrorForClient:
    """Security boundary: error messages exposed to WebSocket clients
    and persisted in runs.error must not leak internal details."""

    def test_secret_resolution_error_returns_safe_message(self) -> None:
        """SecretResolutionError must NEVER leak secret names."""
        from elspeth.core.secrets import SecretResolutionError
        from elspeth.web.execution.service import _sanitize_error_for_client

        exc = SecretResolutionError(["DB_PASSWORD", "API_KEY"])
        result = _sanitize_error_for_client(exc)
        assert "DB_PASSWORD" not in result
        assert "API_KEY" not in result
        assert "secret" in result.lower()

    def test_value_error_does_not_leak_validation_structure(self) -> None:
        """ValueError can carry Pydantic/config internals and must be generic."""
        from elspeth.web.execution.service import _sanitize_error_for_client

        exc = ValueError("2 validation errors for PipelineSettings\nsource.options.internal_token_path\n  Field required [type=missing]")
        result = _sanitize_error_for_client(exc)
        assert result == "Pipeline execution failed (ValueError)"
        assert "PipelineSettings" not in result
        assert "internal_token_path" not in result

    def test_type_error_does_not_leak_function_signature(self) -> None:
        """TypeError can carry function signatures and must be generic."""
        from elspeth.web.execution.service import _sanitize_error_for_client

        exc = TypeError("build_pipeline() got an unexpected keyword argument 'internal_model_state'")
        result = _sanitize_error_for_client(exc)
        assert result == "Pipeline execution failed (TypeError)"
        assert "build_pipeline" not in result
        assert "internal_model_state" not in result

    def test_key_error_does_not_leak_internal_names(self) -> None:
        """KeyError is NOT allowlisted — str(KeyError) leaks dict key names."""
        from elspeth.web.execution.service import _sanitize_error_for_client

        exc = KeyError("_SCOPE_TO_AUDIT_SOURCE")
        result = _sanitize_error_for_client(exc)
        assert "_SCOPE_TO_AUDIT_SOURCE" not in result
        assert "KeyError" in result

    def test_runtime_error_returns_generic_message(self) -> None:
        """Unexpected exceptions get a generic message with class name only."""
        from elspeth.web.execution.service import _sanitize_error_for_client

        exc = RuntimeError("internal traceback details here /home/john/elspeth/src")
        result = _sanitize_error_for_client(exc)
        assert "/home/john" not in result
        assert "RuntimeError" in result

    def test_os_error_returns_generic_message(self) -> None:
        """OSError with file paths must not leak."""
        from elspeth.web.execution.service import _sanitize_error_for_client

        exc = OSError("[Errno 13] Permission denied: '/var/secrets/key.pem'")
        result = _sanitize_error_for_client(exc)
        assert "/var/secrets" not in result
        assert "OSError" in result


# ── T2: _resolve_yaml_paths ───────────────────────────────────────────


class TestResolveYamlPaths:
    """Path rewriting from relative to absolute before YAML reaches plugins."""

    def test_source_relative_path_rewritten(self) -> None:
        from elspeth.web.execution.preflight import resolve_runtime_yaml_paths as _resolve_yaml_paths

        yaml_str = "source:\n  plugin: csv\n  options:\n    path: data/input.csv\n"
        result = _resolve_yaml_paths(yaml_str, "/srv/data")
        assert "/srv/data/data/input.csv" in result

    def test_source_absolute_path_unchanged(self) -> None:
        from elspeth.web.execution.preflight import resolve_runtime_yaml_paths as _resolve_yaml_paths

        yaml_str = "source:\n  plugin: csv\n  options:\n    path: /absolute/input.csv\n"
        result = _resolve_yaml_paths(yaml_str, "/srv/data")
        assert "/absolute/input.csv" in result

    def test_sink_relative_path_rewritten(self) -> None:
        from elspeth.web.execution.preflight import resolve_runtime_yaml_paths as _resolve_yaml_paths

        yaml_str = "source:\n  plugin: csv\n  options:\n    path: /abs/in.csv\nsinks:\n  primary:\n    plugin: csv\n    options:\n      file: output/results.csv\n"
        result = _resolve_yaml_paths(yaml_str, "/srv/data")
        assert "/srv/data/output/results.csv" in result

    def test_sink_persist_directory_relative_path_rewritten(self) -> None:
        from elspeth.web.execution.preflight import resolve_runtime_yaml_paths as _resolve_yaml_paths

        yaml_str = (
            "source:\n"
            "  plugin: csv\n"
            "  options:\n"
            "    path: /abs/in.csv\n"
            "sinks:\n"
            "  chroma:\n"
            "    plugin: chroma_sink\n"
            "    options:\n"
            "      mode: persistent\n"
            "      persist_directory: outputs/chroma-store\n"
        )
        result = _resolve_yaml_paths(yaml_str, "/srv/data", session_id="sess-a")
        assert "/srv/data/outputs/sess-a/chroma-store" in result

    def test_transform_provider_persist_directory_relative_path_rewritten(self) -> None:
        from elspeth.web.execution.preflight import resolve_runtime_yaml_paths as _resolve_yaml_paths

        yaml_str = (
            "source:\n"
            "  plugin: csv\n"
            "  options:\n"
            "    path: /abs/in.csv\n"
            "transforms:\n"
            "  - name: rag\n"
            "    plugin: rag_retrieval\n"
            "    options:\n"
            "      provider: chroma\n"
            "      provider_config:\n"
            "        persist_directory: outputs/chroma-index\n"
        )
        result = _resolve_yaml_paths(yaml_str, "/srv/data", session_id="sess-a")
        assert "/srv/data/outputs/sess-a/chroma-index" in result

    def test_transform_provider_persist_directory_absolute_path_unchanged(self) -> None:
        from elspeth.web.execution.preflight import resolve_runtime_yaml_paths as _resolve_yaml_paths

        yaml_str = (
            "transforms:\n"
            "  - name: rag\n"
            "    plugin: rag_retrieval\n"
            "    options:\n"
            "      provider: chroma\n"
            "      provider_config:\n"
            "        persist_directory: /srv/data/outputs/chroma-index\n"
        )
        result = _resolve_yaml_paths(yaml_str, "/srv/data")
        assert "/srv/data/outputs/chroma-index" in result

    def test_non_rag_transform_without_provider_config_is_noop(self) -> None:
        from elspeth.web.execution.preflight import resolve_runtime_yaml_paths as _resolve_yaml_paths

        yaml_str = "transforms:\n  - name: vt\n    plugin: value_transform\n    options:\n      some_field: value\n"
        result = _resolve_yaml_paths(yaml_str, "/srv/data")
        assert "plugin: value_transform" in result

    def test_sink_without_options_is_noop(self) -> None:
        from elspeth.web.execution.preflight import resolve_runtime_yaml_paths as _resolve_yaml_paths

        yaml_str = "sinks:\n  primary:\n    plugin: csv\n"
        result = _resolve_yaml_paths(yaml_str, "/srv/data")
        assert "plugin: csv" in result

    def test_non_string_input_raises_type_error(self) -> None:
        from elspeth.web.execution.preflight import resolve_runtime_yaml_paths as _resolve_yaml_paths

        with pytest.raises(TypeError, match="must return str"):
            _resolve_yaml_paths(123, "/srv/data")  # type: ignore[arg-type]

    def test_non_dict_yaml_raises_type_error(self) -> None:
        """YAML that parses to a scalar (not a dict) is a generator bug.

        Also the bound raising test for the @trust_boundary on
        resolve_runtime_yaml_paths (source_param='pipeline_yaml'): the malformed
        Tier-3 input is passed positionally as pipeline_yaml and the boundary
        rejects it with TypeError. Imported unaliased so the trust_boundary.tests
        rule matches the call to the decorated symbol.
        """
        from elspeth.web.execution.preflight import resolve_runtime_yaml_paths

        with pytest.raises(TypeError, match="non-dict top-level"):
            resolve_runtime_yaml_paths("just a string", "/srv/data")

    def test_aliased_yaml_rejected_before_path_rewrite(self) -> None:
        """The first execution-path YAML parse rejects anchors/aliases."""
        from elspeth.web.execution.preflight import resolve_runtime_yaml_paths

        yaml_str = """
source:
  plugin: csv
  options:
    path: &path data/input.csv
    copied_path: *path
"""

        with pytest.raises(ValueError, match=r"^YAML parse failed: \w+Error$"):
            resolve_runtime_yaml_paths(yaml_str, "/srv/data")

    def test_deep_yaml_rejected_before_path_rewrite(self) -> None:
        """Depth is bounded before recursive path-rewrite traversal."""
        from elspeth.web.execution.preflight import resolve_runtime_yaml_paths

        lines = ["metadata:"]
        for depth in range(70):
            lines.append(f"{'  ' * (depth + 1)}level_{depth}:")
        lines.append(f"{'  ' * 71}value: done")

        with pytest.raises(ValueError, match=r"^YAML parse failed: \w+Error$"):
            resolve_runtime_yaml_paths("\n".join(lines), "/srv/data")

    def test_no_source_or_sinks_is_noop(self) -> None:
        """YAML with no source/sinks passes through without error."""
        from elspeth.web.execution.preflight import resolve_runtime_yaml_paths as _resolve_yaml_paths

        yaml_str = "metadata:\n  name: test\n"
        result = _resolve_yaml_paths(yaml_str, "/srv/data")
        assert "name: test" in result

    def test_source_without_options_raises_type_error(self) -> None:
        """A present ``source`` missing its ``options`` key is a generator-contract
        violation, not optional data.

        ``yaml_generator`` emits ``source.options`` unconditionally
        (yaml_generator.py:92) and both production callers feed
        ``generate_yaml()`` output here — never hand-authored YAML. So a
        ``source`` without ``options`` can only mean a generator bug, and
        ``resolve_runtime_yaml_paths`` asserts it loudly rather than masking
        the absence with ``.get()`` (sinks differ — the generator emits sink
        options conditionally, so the sink path tolerates absence by design).
        """
        from elspeth.web.execution.preflight import resolve_runtime_yaml_paths as _resolve_yaml_paths

        yaml_str = "source:\n  plugin: csv\n"
        with pytest.raises(TypeError, match="without required 'options'"):
            _resolve_yaml_paths(yaml_str, "/srv/data")

    def test_plural_source_relative_paths_rewritten(self) -> None:
        from elspeth.web.execution.preflight import resolve_runtime_yaml_paths as _resolve_yaml_paths

        yaml_str = (
            "sources:\n"
            "  orders:\n"
            "    plugin: csv\n"
            "    options:\n"
            "      path: data/orders.csv\n"
            "  refunds:\n"
            "    plugin: csv\n"
            "    options:\n"
            "      file: /absolute/refunds.csv\n"
        )
        result = _resolve_yaml_paths(yaml_str, "/srv/data")
        assert "/srv/data/data/orders.csv" in result
        assert "/absolute/refunds.csv" in result

    @pytest.mark.parametrize(
        ("yaml_str", "message"),
        [
            ("sources: []\n", "non-dict 'sources'"),
            ("sources:\n  orders: csv\n", "non-dict source 'sources.orders'"),
            ("sources:\n  orders:\n    plugin: csv\n    options: []\n", "non-dict 'sources.orders.options'"),
        ],
    )
    def test_plural_source_malformed_shapes_fail_closed(self, yaml_str: str, message: str) -> None:
        from elspeth.web.execution.preflight import resolve_runtime_yaml_paths as _resolve_yaml_paths

        with pytest.raises(TypeError, match=message):
            _resolve_yaml_paths(yaml_str, "/srv/data")


# ── Phase 2.2 propagation: _partial_completion_message ───────────────


class TestPartialCompletionMessage:
    """Sibling to ``_structural_failure_message`` for COMPLETED_WITH_FAILURES.

    Populated into ``RunRecord.error`` so the frontend can render failure
    evidence for partial-success runs without re-implementing the L0
    ``failure_indicator`` predicate.  The RunRecord invariant at
    ``sessions/protocol.py:237-238`` permits ``error`` on any status; only
    ``failed`` *requires* it.
    """

    def test_returns_non_empty_string(self) -> None:
        from elspeth.web.execution.service import _partial_completion_message

        msg = _partial_completion_message(
            rows_succeeded=7,
            rows_failed=3,
            rows_routed_failure=1,
            rows_quarantined=2,
        )
        assert msg
        assert isinstance(msg, str)

    def test_includes_all_count_fields(self) -> None:
        """Operator must be able to read the failure breakdown directly from
        the runs row.  The four counts are the structural failure-evidence
        surface in ``RunRecord``."""
        from elspeth.web.execution.service import _partial_completion_message

        msg = _partial_completion_message(
            rows_succeeded=7,
            rows_failed=3,
            rows_routed_failure=1,
            rows_quarantined=2,
        )
        assert "rows_succeeded=7" in msg
        assert "rows_failed=3" in msg
        assert "rows_routed_failure=1" in msg
        assert "rows_quarantined=2" in msg

    def test_points_at_user_visible_affordance_when_no_samples(self) -> None:
        """Without enrichment samples, the message must direct the operator to
        the actual UI surface — the in-page expand panel — not a backend API
        path the user cannot navigate to (was '/diagnostics')."""
        from elspeth.web.execution.service import _partial_completion_message

        msg = _partial_completion_message(
            rows_succeeded=1,
            rows_failed=1,
            rows_routed_failure=0,
            rows_quarantined=0,
        )
        assert "Expand this run" in msg
        assert "/diagnostics" not in msg

    def test_inlines_failure_samples_when_supplied(self) -> None:
        """When the caller supplies a pre-formatted samples block, the
        message inlines it under a 'Top per-row failures' heading so the
        runs view shows the dominant cause without needing the expand."""
        from elspeth.web.execution.service import _partial_completion_message

        samples = "  • 3x SSRFBlockedError: URL is missing a scheme"
        msg = _partial_completion_message(
            rows_succeeded=0,
            rows_failed=3,
            rows_routed_failure=0,
            rows_quarantined=0,
            failure_samples=samples,
        )
        assert "Top per-row failures:" in msg
        assert samples in msg
        assert "Expand this run" not in msg

    def test_deterministic_given_inputs(self) -> None:
        """No timestamps, no random IDs — the message is a pure function of
        its counts so audit-trail comparisons are stable."""
        from elspeth.web.execution.service import _partial_completion_message

        a = _partial_completion_message(rows_succeeded=5, rows_failed=2, rows_routed_failure=1, rows_quarantined=0)
        b = _partial_completion_message(rows_succeeded=5, rows_failed=2, rows_routed_failure=1, rows_quarantined=0)
        assert a == b

    def test_does_not_echo_user_row_data(self) -> None:
        """Same security posture as ``_structural_failure_message`` — the
        message is structural facts only; no row keys, no LLM prompts, no
        secret-resolution candidates."""
        from elspeth.web.execution.service import _partial_completion_message

        msg = _partial_completion_message(
            rows_succeeded=1,
            rows_failed=1,
            rows_routed_failure=0,
            rows_quarantined=0,
        )
        # Sanity: only structural words.  If a future change inlines a row
        # value, this assertion would fail loudly.
        forbidden_substrings = ["row_id=", "key=", "value=", "prompt=", "secret"]
        for forbidden in forbidden_substrings:
            assert forbidden not in msg.lower(), f"_partial_completion_message must not include {forbidden!r} (row-data leak)"


class TestSetOpenrouterCatalogSnapshotValidation:
    """Pin the sha256-hex validator at the snapshot setter site.

    ``set_openrouter_catalog_snapshot()`` is called once by the FastAPI
    lifespan; a non-hex string passing the old ``not sha256`` guard would
    propagate into the runs row and corrupt the audit trail. The validator
    now uses the canonical ``is_valid_sha256_hex`` shared with the
    Landscape write-side guards.
    """

    def test_setter_rejects_non_hex_sha256(self, service: ExecutionServiceImpl) -> None:
        """A non-empty non-hex string fails the hex shape check."""
        with pytest.raises(RuntimeError, match="64 lowercase hex chars"):
            service.set_openrouter_catalog_snapshot(sha256="not-a-sha", source="bundled")

    def test_setter_accepts_canonical_digest(self, service: ExecutionServiceImpl) -> None:
        """A real hashlib.sha256 hex digest passes."""
        import hashlib

        digest = hashlib.sha256(b"catalog-anchor").hexdigest()
        service.set_openrouter_catalog_snapshot(sha256=digest, source="bundled")
        # No exception — the setter accepted the value.

    def test_setter_rejects_bad_source(self, service: ExecutionServiceImpl) -> None:
        with pytest.raises(RuntimeError, match="must be 'live' or 'bundled'"):
            service.set_openrouter_catalog_snapshot(sha256="0" * 64, source="oops")


# ── elspeth-30416e67cc: Tier-3 per-row failure text must not egress ───

_EGRESS_CANARY_ROW = "CANARY_ROW_SECRET_9f3a"
_EGRESS_CANARY_PROVIDER = "CANARY_PROVIDER_ERROR_7b21"


@pytest.mark.usefixtures("mock_pipeline_config_assembly")
class TestFailureSampleClientEgress:
    """The inline run-level summary is category + node + count, never row text.

    ``transform_errors.error_details_json`` is written through
    ``canonical_or_recorded_error_details_json``, which self-declares a
    **Tier-3** boundary: the payload "may carry arbitrary row-derived data"
    and nothing in it is validated at write time.  Before elspeth-30416e67cc
    the run-level enrichment inlined that free text verbatim (truncated only)
    into three surfaces that all live OUTSIDE the audit boundary:

    1. the non-audit sessions DB ``runs.error`` column,
    2. ``RunStatusResponse.error`` on ``GET /api/runs/{id}``, and
    3. the SSE ``failed`` event's ``FailedData.detail``.

    All three derive from one ``session_error`` string, so all three are
    asserted here — the mechanism, not one symptom.  Per-row free text stays
    available only behind the authenticated, ownership-verified
    ``GET /api/runs/{id}/diagnostics``, which reads its own tables.
    """

    @staticmethod
    def _seed_run_with_canary_failures(
        db: LandscapeDB,
        *,
        transform_id: str = "fetch",
        rows: int = 3,
    ) -> str:
        """Record ``rows`` per-row transform errors whose text carries canaries."""
        factory = RecorderFactory(db)
        run = factory.run_lifecycle.begin_run(config={}, canonical_version="v1")
        dynamic_schema = SchemaConfig.from_dict({"mode": "observed"})
        factory.data_flow.register_node(
            run_id=run.run_id,
            plugin_name="test_source",
            node_type=NodeType.SOURCE,
            plugin_version="1.0",
            config={},
            schema_config=dynamic_schema,
            node_id="source_test",
            sequence=0,
        )
        factory.data_flow.register_node(
            run_id=run.run_id,
            plugin_name="web_scrape",
            node_type=NodeType.TRANSFORM,
            plugin_version="1.0",
            config={},
            schema_config=dynamic_schema,
            node_id=transform_id,
            sequence=1,
        )
        for index in range(rows):
            row = factory.data_flow.create_row(
                run_id=run.run_id,
                source_node_id="source_test",
                row_index=index,
                data={"url": f"row-{index}"},
                source_row_index=index,
                ingest_sequence=index,
            )
            token_id = f"canary_tok_{index}"
            with db.write_connection() as conn:
                conn.execute(
                    tokens_table.insert().values(
                        token_id=token_id,
                        row_id=row.row_id,
                        run_id=run.run_id,
                        step_in_pipeline=0,
                        created_at=datetime.now(UTC),
                    )
                )
                conn.commit()
            factory.data_flow.record_transform_error(
                ref=TokenRef(token_id=token_id, run_id=run.run_id),
                transform_id=transform_id,
                row_data={"url": f"row-{index}"},
                error_details={
                    "reason": "decode_failed",
                    # Distinct per row: the pre-fix aggregation keyed on the
                    # message, so distinct texts also mean the top-N slice was
                    # taken over messages rather than over categories.
                    "error": f"gzip: incorrect header check for {_EGRESS_CANARY_ROW}-{index}",
                    "error_type": f"BadGzipFile: {_EGRESS_CANARY_PROVIDER}",
                },
                destination="discard",
            )
        return run.run_id

    @staticmethod
    def _drive_terminal_run(
        service: ExecutionServiceImpl,
        mock_session_service: MagicMock,
        *,
        mock_load: MagicMock,
        mock_instantiate: MagicMock,
        mock_graph_cls: MagicMock,
        mock_orch_cls: MagicMock,
        mock_landscape: MagicMock,
        landscape_db: LandscapeDB,
        result: RunResult,
        session_status: str,
    ) -> tuple[str, str, list[Any]]:
        """Run ``_run_pipeline`` to the engine-decided terminal branch.

        Returns ``(run_id, persisted_runs_error, failed_event_payloads)``.
        """
        _configure_runtime_success(
            mock_load=mock_load,
            mock_instantiate=mock_instantiate,
            mock_graph_cls=mock_graph_cls,
            mock_orch_cls=mock_orch_cls,
            result=result,
        )
        mock_landscape.return_value = landscape_db
        run_id = str(uuid4())
        with patch(
            "elspeth.web.execution.service.load_run_accounting_from_db",
            return_value=_run_accounting_for_status(result.status),
        ):
            service._run_pipeline(run_id, _TEST_PIPELINE_YAML, threading.Event())

        status_calls = [
            call for call in mock_session_service.update_run_status.call_args_list if call.kwargs.get("status") == session_status
        ]
        assert status_calls, f"the terminal branch must record status={session_status!r}"
        persisted_error = status_calls[-1].kwargs["error"]
        assert persisted_error is not None, "the terminal branch must persist a run-level error summary"
        failed_events = [
            call.kwargs for call in mock_session_service.append_run_event.call_args_list if call.kwargs.get("event_type") == "failed"
        ]
        return run_id, persisted_error, failed_events

    @patch("elspeth.web.execution.service.Orchestrator")
    @patch("elspeth.web.execution.preflight.ExecutionGraph")
    @patch("elspeth.web.execution.preflight.instantiate_plugins_from_config")
    @patch("elspeth.web.execution.service.load_settings_from_yaml_string")
    @patch("elspeth.web.execution.service.open_landscape_db")
    @patch("elspeth.web.execution.service.FilesystemPayloadStore")
    def test_failed_run_egresses_category_and_node_but_never_row_text(
        self,
        mock_payload: MagicMock,
        mock_landscape: MagicMock,
        mock_load: MagicMock,
        mock_instantiate: MagicMock,
        mock_graph_cls: MagicMock,
        mock_orch_cls: MagicMock,
        service: ExecutionServiceImpl,
        mock_session_service: MagicMock,
        tmp_path: Path,
    ) -> None:
        """FAILED: all three client surfaces carry the summary, none the canary."""
        db = LandscapeDB.from_url(f"sqlite:///{tmp_path / 'audit.db'}")
        try:
            landscape_run_id = self._seed_run_with_canary_failures(db)
            run_id, persisted_error, failed_events = self._drive_terminal_run(
                service,
                mock_session_service,
                mock_load=mock_load,
                mock_instantiate=mock_instantiate,
                mock_graph_cls=mock_graph_cls,
                mock_orch_cls=mock_orch_cls,
                mock_landscape=mock_landscape,
                landscape_db=db,
                result=_orchestrator_result_stub(
                    run_id=landscape_run_id,
                    status=RunStatus.FAILED,
                    rows_processed=3,
                    rows_succeeded=0,
                    rows_failed=3,
                ),
                session_status="failed",
            )
        finally:
            db.close()

        # Surface 1 — the non-audit sessions DB ``runs.error`` column.
        assert _EGRESS_CANARY_ROW not in persisted_error, persisted_error
        assert _EGRESS_CANARY_PROVIDER not in persisted_error, persisted_error
        assert "incorrect header check" not in persisted_error, persisted_error
        # …and it still names the category, the node, and the count.
        assert "decode_failed" in persisted_error, persisted_error
        assert "fetch" in persisted_error, persisted_error
        assert "3x" in persisted_error, persisted_error

        # Surface 2 — the SSE ``failed`` event detail is the SAME string.
        assert failed_events, "a FAILED terminal branch must broadcast a failed event"
        sse_detail = failed_events[-1]["data"]["detail"]
        assert sse_detail == persisted_error
        assert _EGRESS_CANARY_ROW not in sse_detail
        assert _EGRESS_CANARY_PROVIDER not in sse_detail

        # Surface 3 — ``RunStatusResponse.error`` on GET /api/runs/{id}.
        run_uuid = UUID(run_id)
        record = _run_record_stub(
            id=run_uuid,
            status="failed",
            error=persisted_error,
            finished_at=datetime.now(tz=UTC),
        )
        loop = asyncio.new_event_loop()
        try:
            status_response = loop.run_until_complete(service.get_status(run_uuid, run_record=record))
        finally:
            loop.close()
        assert status_response.error == persisted_error
        assert status_response.error is not None
        assert _EGRESS_CANARY_ROW not in status_response.error
        assert _EGRESS_CANARY_PROVIDER not in status_response.error

    @patch("elspeth.web.execution.service.Orchestrator")
    @patch("elspeth.web.execution.preflight.ExecutionGraph")
    @patch("elspeth.web.execution.preflight.instantiate_plugins_from_config")
    @patch("elspeth.web.execution.service.load_settings_from_yaml_string")
    @patch("elspeth.web.execution.service.open_landscape_db")
    @patch("elspeth.web.execution.service.FilesystemPayloadStore")
    def test_completed_with_failures_run_never_egresses_row_text(
        self,
        mock_payload: MagicMock,
        mock_landscape: MagicMock,
        mock_load: MagicMock,
        mock_instantiate: MagicMock,
        mock_graph_cls: MagicMock,
        mock_orch_cls: MagicMock,
        service: ExecutionServiceImpl,
        mock_session_service: MagicMock,
        tmp_path: Path,
    ) -> None:
        """The partial-completion sibling leaks identically and is fixed with it.

        This branch emits no ``failed`` SSE event, so it is two surfaces
        (``runs.error`` and ``RunStatusResponse.error``) rather than three.
        """
        db = LandscapeDB.from_url(f"sqlite:///{tmp_path / 'audit.db'}")
        try:
            landscape_run_id = self._seed_run_with_canary_failures(db, transform_id="summarise", rows=2)
            _run_id, persisted_error, _failed_events = self._drive_terminal_run(
                service,
                mock_session_service,
                mock_load=mock_load,
                mock_instantiate=mock_instantiate,
                mock_graph_cls=mock_graph_cls,
                mock_orch_cls=mock_orch_cls,
                mock_landscape=mock_landscape,
                landscape_db=db,
                result=_orchestrator_result_stub(
                    run_id=landscape_run_id,
                    status=RunStatus.COMPLETED_WITH_FAILURES,
                    rows_processed=10,
                    rows_succeeded=8,
                    rows_failed=2,
                ),
                session_status="completed_with_failures",
            )
        finally:
            db.close()

        assert _EGRESS_CANARY_ROW not in persisted_error, persisted_error
        assert _EGRESS_CANARY_PROVIDER not in persisted_error, persisted_error
        assert "incorrect header check" not in persisted_error, persisted_error
        assert "decode_failed" in persisted_error, persisted_error
        assert "summarise" in persisted_error, persisted_error
        assert "2x" in persisted_error, persisted_error
