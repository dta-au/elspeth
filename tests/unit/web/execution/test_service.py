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
from collections.abc import Callable, Coroutine, Iterator, Mapping
from concurrent.futures import Future
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from types import MappingProxyType, SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, create_autospec, patch
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError

from elspeth.contracts import CallType, NodeStateStatus, NodeType
from elspeth.contracts.audit import TokenRef
from elspeth.contracts.chargeable_admission import (
    AdmissionPolicyEvidence,
    AdmissionRefusalReason,
    ChargeableAdmissionDecision,
    ChargeableAdmissionRefused,
    QuotaDisposition,
)
from elspeth.contracts.enums import CreationModality, RunMode, RunStatus, TerminalOutcome, TerminalPath
from elspeth.contracts.errors import AuditIntegrityError, ExecutionError
from elspeth.contracts.freeze import deep_thaw
from elspeth.contracts.hashing import stable_hash
from elspeth.contracts.run_result import RunResult
from elspeth.contracts.schema import SchemaConfig
from elspeth.contracts.session_operation import SessionOperationContext
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
from elspeth.core.landscape.schema import run_attributions_table, runs_table
from elspeth.telemetry.manager import TelemetryManager
from elspeth.web.blobs.protocol import (
    BlobFinalizationResult,
    BlobIntegrityError,
    BlobNotFoundError,
    BlobRecord,
    BlobServiceProtocol,
    BlobStateError,
)
from elspeth.web.coordination.approval_authority import ApprovalGateInputs
from elspeth.web.coordination.contracts import StartPermitState
from elspeth.web.coordination.lifecycle import SessionOperationLease
from elspeth.web.coordination.quota_authority import ProviderAttempt, TokenUsageEntry
from elspeth.web.dependencies import create_catalog_service
from elspeth.web.deployment_contract import resolve_deployment_state_mode
from elspeth.web.execution.errors import (
    BlobRowsSourceAdmissionError,
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
    ValidationReadiness,
    ValidationResult,
)
from elspeth.web.execution.service import (
    _MAX_FRAME_PATH_PARTS,
    ExecutionServiceImpl,
    InlineBlobPromptSurfaceAdmissionError,
    _build_web_plugin_policy_evidence,
    _discover_blob_rows_sources,
    close_execute_lease_before_transfer,
)
from elspeth.web.execution.validation import validate_pipeline as _real_validate_pipeline
from elspeth.web.interpretation_state import (
    INTERPRETATION_REQUIREMENTS_KEY,
    PROMPT_TEMPLATE_PARTS_KEY,
    approved_prompt_artifact_hash_from_options,
    prompt_review_anchor_hash_from_options,
)
from elspeth.web.plugin_policy.models import (
    PluginAvailabilitySnapshot,
)
from elspeth.web.sessions.protocol import (
    CompositionStateRecord,
    RunStartPermitRecord,
    SessionServiceProtocol,
)
from elspeth.web.sessions.telemetry import build_sessions_telemetry
from tests.fixtures.landscape import claim_test_work_item, leader_coordination_token, make_landscape_db
from tests.helpers import execution_custody
from tests.helpers.execution_custody import CanonicalExecutionAuthorityObservation, ExecutionTestCustody
from tests.helpers.session_fences import (
    adopt_execute_lease,
    close_adopted_lease,
)

# ── Fixtures ───────────────────────────────────────────────────────────

execution_fixture = execution_custody.execution_fixture


_TEST_PIPELINE_YAML = "source:\n  plugin: csv\n  options: {}\n"
_RESOLVED_TEST_PIPELINE_YAML = "source:\n  options: {}\n  plugin: csv\n"


class _WebSettingsStub:
    """Settings collaborator for execution-service tests."""

    def __init__(self) -> None:
        self.deployment_target = "default"
        self.auth_provider = "local"
        self.deployment_state_mode = "sqlite-single"
        self.workflow_governance = "off"
        self.compartment_id: str | None = None
        self.landscape_url = "sqlite:///test_audit.db"
        self.payload_store_path = Path("/tmp/test_payloads")
        self.landscape_passphrase = None
        self.data_dir: str | Path = "/tmp/data"
        # Secret wiring is deny-by-default (elspeth-f3c1aafd25); tests that
        # exercise wired secrets construct their own authorizing policy.
        self.secret_wiring_allowlist: tuple[Any, ...] = ()
        self.execution_rate_limit = RateLimitSettings()

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
        "cancel_requested_at": None,
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
    graph = create_autospec(ExecutionGraph, instance=True)
    # Model the real contract for a collector-free graph: an EMPTY id map.
    # autospec's default (a truthy MagicMock) would trip the preflight's WS4
    # collector-runnability refusal on every mocked run — fix the fake, never
    # the production check.
    graph.get_collector_id_map.return_value = {}
    return graph


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


class _ModelCopyNamespace(SimpleNamespace):
    """Settings test double with Pydantic's immutable update shape."""

    def model_copy(self, *, update: dict[str, Any]) -> _ModelCopyNamespace:
        return type(self)(**{**vars(self), **update})


def _mock_pipeline_settings() -> _ModelCopyNamespace:
    """Return settings-shaped test data for patched pipeline loading.

    _run_pipeline() now builds the same runtime infrastructure as the CLI path,
    so tests that patch YAML loading must still provide real config-contract
    objects for the runtime conversion boundary.
    """
    return _ModelCopyNamespace(
        run_mode=RunMode.LIVE,
        sources={},
        transforms=[],
        aggregations=[],
        collectors=[],
        sinks={"primary": SimpleNamespace(plugin="json", options={})},
        gates=[],
        coalesce=[],
        row_unions=[],
        scopes=[],
        max_bound_region_depth=5,
        queues={},
        landscape=_ModelCopyNamespace(export=SimpleNamespace(enabled=False, sink=None)),
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
    """The real validator's successful 23-check prefix, without advisories."""
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
    blob_service.get_blob.side_effect = lambda blob_id, *, session_operation_context: records[blob_id]
    blob_service.read_blob_content_prefix_verified.side_effect = lambda blob_id, *, prefix_bytes, session_operation_context: verified[
        blob_id
    ]
    service._blob_service = blob_service
    return blob_service


@pytest.fixture
def mock_session_service() -> MagicMock:
    svc = create_autospec(SessionServiceProtocol, instance=True)
    admission = ChargeableAdmissionDecision(
        evidence=AdmissionPolicyEvidence(quota_disposition=QuotaDisposition.NOT_CONFIGURED, secret_wiring_hash="d" * 64),
        refusal_reason=None,
    )
    svc.assess_chargeable_operation.return_value = admission

    async def issue_run_start_permit(
        run_id: UUID, *, session_operation_context: SessionOperationContext, approval: ApprovalGateInputs | None = None
    ) -> RunStartPermitRecord:
        return RunStartPermitRecord(
            run_id=str(run_id),
            state=StartPermitState.START_PERMITTED,
            permit_id="test-permit",
            permit_epoch=1,
            subject_hash="a" * 64,
            issued_at=datetime.now(UTC),
            cancelled_at=None,
            admission_decision=admission,
        )

    svc.issue_run_start_permit.side_effect = issue_run_start_permit

    async def assess_run_start_admission(
        run_id: UUID, *, session_operation_context: SessionOperationContext, approval: ApprovalGateInputs | None = None
    ) -> RunStartPermitRecord:
        return RunStartPermitRecord(
            run_id=str(run_id),
            state=StartPermitState.PENDING,
            permit_id=None,
            permit_epoch=None,
            subject_hash=None,
            issued_at=None,
            cancelled_at=None,
        )

    svc.assess_run_start_admission.side_effect = assess_run_start_admission
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
def real_loop() -> Iterator[asyncio.AbstractEventLoop]:
    """A test-owned loop for the service's ``_call_async`` bridge and the worker lease."""
    loop = asyncio.new_event_loop()
    try:
        yield loop
    finally:
        loop.close()


_worker_lease: SessionOperationLease | None = None
_worker_lease_loop: asyncio.AbstractEventLoop | None = None


def _execute_lease() -> SessionOperationLease:
    """Allocate the direct-internal worker lease only for its actual consumer.

    Canonical ``_execute`` acquisition owns a different SQLite lease. Merely
    requesting ``service`` must not allocate this recording-authority lease.
    Every returned adopted lease is still closed by the owning fixture.
    """
    global _worker_lease
    if _worker_lease_loop is None:
        raise RuntimeError("live EXECUTE lease fixture is not active")
    if _worker_lease is None:
        _worker_lease = adopt_execute_lease(_worker_lease_loop, uuid4())
    return _worker_lease


@pytest.fixture(autouse=True)
def _live_execute_lease(real_loop: asyncio.AbstractEventLoop, execution_fixture: ExecutionTestCustody) -> Iterator[None]:
    """Retain lazy direct-internal ownership and close every allocated lease."""
    global _worker_lease, _worker_lease_loop
    if _worker_lease is not None or _worker_lease_loop is not None:
        raise RuntimeError("direct-internal lease fixture overlapped another owner")
    _worker_lease_loop = real_loop
    try:
        yield
    finally:
        lease = _worker_lease
        _worker_lease = None
        _worker_lease_loop = None
        if lease is not None and not lease.closed:
            close_adopted_lease(real_loop, lease)


async def _join_loop_cleanup_worker(
    service: ExecutionServiceImpl,
    execution_fixture: ExecutionTestCustody,
    lease: SessionOperationLease,
    *,
    worker_originals: list[BaseException],
    primary: BaseException | None = None,
) -> None:
    """Join the actual private owner before closing a worker's unused lease.

    The worker normally releases in its loop coroutine before returning.
    A literal submit error, or failure before that coroutine enters, cannot
    establish absence of a physical worker. The registered executor's actual
    joined receipt is the sole fallback handoff; registry join runs alongside
    it and is allowed to wait for this exact canonical RELEASE.
    """
    originals: list[BaseException] = [] if primary is None else [primary]
    cancellation_start = len(execution_fixture._caller_cancellations)

    async def invocation() -> None:
        await execution_fixture.shutdown_service(service)

    execution_fixture._issue_join(invocation, "fixture-loop-cleanup-worker-shutdown")
    owner = execution_fixture._joins[-1]
    assert owner.invocation is invocation
    registry = service.execution_lease_release_registry
    while not registry.executor_join_physically_observed:
        try:
            await asyncio.sleep(0.01)
        except asyncio.CancelledError as original:
            if not any(original is retained for retained in originals):
                originals.append(original)
    for original in worker_originals:
        if not any(original is retained for retained in originals):
            originals.append(original)
    if not lease.closed:
        try:
            await close_execute_lease_before_transfer(lease, primary=primary)
        except BaseException as original:
            originals.append(original)
    # A failed/Unknown RELEASE remains registry-owned and cannot become a
    # successful retirement. The external native controller owns its finite
    # inconclusive bound; no timeout or exception grants completion here.
    await execution_fixture._join_issued()
    assert owner.entered and owner.task is not None and owner.task.done() and owner.outcome_recorded
    if owner.outcome is not None:
        originals.append(owner.outcome)
    for original in execution_fixture._caller_cancellations[cancellation_start:]:
        if not any(original is retained for retained in originals):
            originals.append(original)
    execution_custody.async_workers._raise_lifecycle_originals([], originals)


async def _execute(
    service: ExecutionServiceImpl,
    *,
    session_id: UUID,
    authority: CanonicalExecutionAuthorityObservation | None = None,
    execution_fixture: ExecutionTestCustody,
    **kwargs: Any,
) -> UUID:
    """Call ``execute()`` the way the route does: under a fresh EXECUTE lease for ``session_id``.

    ``execute`` requires an exact ``SessionOperationLease`` carrying EXECUTE
    authority for this session (service.py, the head of ``execute``). Mirrors
    ``routes.py``'s transfer contract: on success the lease belongs to the
    worker, which closes it after terminal cleanup via ``_on_pipeline_done``;
    when ``execute`` fails before transfer the caller closes it. Pass
    ``authority`` to recover the exact context the lease carries
    (``authority.calls[0]`` is ``("acquire", context)``).
    """
    observed = authority if authority is not None else execution_fixture.observe_authority(session_id)
    if type(observed) is not CanonicalExecutionAuthorityObservation:
        raise TypeError("execute fixture requires its actual canonical authority observation")
    lease = await execution_fixture.acquire(
        service.execution_lease_release_registry,
        observed.authority,
        session_id=session_id,
        owner_instance_id="execution-service-test",
        lease_seconds=30,
    )
    try:
        return await service.execute(session_id, session_operation_lease=lease, **kwargs)
    except BaseException:
        obligation = lease.execution_obligation
        assert obligation is not None
        if not obligation.completion_required:
            await close_execute_lease_before_transfer(lease)
        raise


@pytest.fixture
def service(
    mock_loop: MagicMock,
    broadcaster: ProgressBroadcaster,
    mock_settings: MagicMock,
    mock_session_service: MagicMock,
    real_loop: asyncio.AbstractEventLoop,
    execution_fixture: ExecutionTestCustody,
) -> Iterator[ExecutionServiceImpl]:
    # AC #17: All Run CRUD goes through SessionService — no direct DB access.
    yaml_generator = _YamlGeneratorStub()
    svc = execution_fixture.bind(
        ExecutionServiceImpl.for_trained_operator(
            loop=execution_fixture.loop,
            broadcaster=broadcaster,
            settings=mock_settings,
            session_service=mock_session_service,
            yaml_generator=yaml_generator,
            telemetry=build_sessions_telemetry(),
            execution_lease_release_registry=execution_fixture.registry(execution_fixture.loop),
        )
    )
    # Patch _call_async for tests that call _run_pipeline directly (sync).
    # The real _call_async uses asyncio.run_coroutine_threadsafe which needs
    # a running event loop. In unit tests, we bridge by running the coroutine
    # synchronously on the test-owned ``real_loop`` (the loop the worker
    # lease from ``_execute_lease()`` is adopted onto).
    # TestB8AsyncBridging tests _call_async itself with its own mocking.

    def _mock_call_async(coro: Coroutine[Any, Any, Any]) -> Any:
        try:
            return real_loop.run_until_complete(coro)
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


# ── Basic Lifecycle ────────────────────────────────────────────────────


@pytest.mark.parametrize("terminal_status", [RunStatus.COMPLETED, RunStatus.COMPLETED_WITH_FAILURES, RunStatus.FAILED, RunStatus.EMPTY])
@pytest.mark.parametrize(
    "finalization", ["none", "success", "failure", "partial", "stale_owner", "settlement_error", "accounting_error", "event_error"]
)
@pytest.mark.usefixtures("mock_pipeline_config_assembly")
def test_worker_settles_terminal_saga_only_after_output_finalization(
    service: ExecutionServiceImpl,
    mock_session_service: MagicMock,
    real_loop: asyncio.AbstractEventLoop,
    terminal_status: RunStatus,
    finalization: str,
) -> None:
    from elspeth.web.sessions.protocol import SessionOperationMutationTransaction, SessionOperationRunMutations

    run_id = str(uuid4())
    result = _orchestrator_result_stub(
        run_id=run_id,
        status=terminal_status,
        rows_processed=0 if terminal_status is RunStatus.EMPTY else 10,
        rows_succeeded=10 if terminal_status is RunStatus.COMPLETED else (9 if terminal_status is RunStatus.COMPLETED_WITH_FAILURES else 0),
        rows_failed=10 if terminal_status is RunStatus.FAILED else (1 if terminal_status is RunStatus.COMPLETED_WITH_FAILURES else 0),
    )
    terminal = _run_record_stub(id=UUID(run_id), status=terminal_status.value)
    mock_session_service.get_run.return_value = terminal
    transaction = MagicMock(spec_set=SessionOperationMutationTransaction)
    transaction.runs = create_autospec(SessionOperationRunMutations, instance=True, spec_set=True)
    mock_session_service.session_operation_authority.mutate.side_effect = lambda context, mutation: mutation(transaction)
    settlement_error: Exception | None = None
    if finalization == "stale_owner":
        from elspeth.web.coordination.contracts import FenceLossReason, SessionOperationFenceLost

        settlement_error = SessionOperationFenceLost(FenceLossReason.OWNER_INACTIVE)
    elif finalization == "settlement_error":
        settlement_error = OSError("terminal settlement unavailable")
    if settlement_error is not None:
        mock_session_service.session_operation_authority.mutate.side_effect = settlement_error
    post_status_error: Exception | None = None
    if finalization == "event_error":
        post_status_error = OSError("terminal event persistence unavailable")
        mock_session_service.append_run_event.side_effect = post_status_error
    elif finalization == "accounting_error" and terminal_status is not RunStatus.FAILED:
        post_status_error = OSError("accounting projection unavailable")
    # Do not use the standard fixture's RuntimeError-swallowing bridge.
    cast(Any, service)._call_async = real_loop.run_until_complete
    if finalization != "none":
        blob_service = _blob_service_stub()
        blob_service.finalize_run_output_blobs.return_value = BlobFinalizationResult(finalized=(), errors=())
        if finalization == "failure":
            blob_service.finalize_run_output_blobs.side_effect = OSError("output storage unavailable")
        elif finalization == "partial":
            from elspeth.contracts.blobs import BlobFinalizationError

            blob_service.finalize_run_output_blobs.return_value = BlobFinalizationResult(
                finalized=(), errors=(BlobFinalizationError(blob_id=uuid4(), exc_type="OSError", detail="storage unavailable"),)
            )
        service._blob_service = blob_service
    with (
        patch("elspeth.web.execution.service.Orchestrator") as orchestrator,
        patch("elspeth.web.execution.service.load_settings_from_yaml_string") as load,
        patch("elspeth.web.execution.preflight.instantiate_plugins_from_config") as instantiate,
        patch("elspeth.web.execution.preflight.ExecutionGraph") as graph,
        patch("elspeth.web.execution.service.open_landscape_db"),
        patch("elspeth.web.execution.service.FilesystemPayloadStore"),
        patch(
            "elspeth.web.execution.service.load_run_accounting_from_db",
            return_value=_run_accounting_for_status(terminal_status),
            side_effect=post_status_error if finalization == "accounting_error" else None,
        ),
    ):
        _configure_runtime_success(
            mock_load=load,
            mock_instantiate=instantiate,
            mock_graph_cls=graph,
            mock_orch_cls=orchestrator,
            result=result,
        )
        expected_error = settlement_error or post_status_error
        if expected_error is not None:
            with pytest.raises(type(expected_error)) as raised:
                service._run_pipeline(run_id, "source:\n  plugin: csv", threading.Event(), session_operation_lease=_execute_lease())
            assert raised.value is expected_error
        else:
            service._run_pipeline(run_id, "source:\n  plugin: csv", threading.Event(), session_operation_lease=_execute_lease())
    assert mock_session_service.update_run_status.call_args.kwargs["status"] == terminal_status.value
    if finalization in {"failure", "partial", "stale_owner", "settlement_error"} or post_status_error is not None:
        transaction.runs.mark_recovery_outputs_finalized.assert_not_called()
    else:
        transaction.runs.mark_recovery_outputs_finalized.assert_called_once_with(run_id=UUID(run_id))


@pytest.mark.parametrize("finalization_succeeds", [True, False])
def test_cancelled_worker_settles_saga_after_terminal_status(
    service: ExecutionServiceImpl, mock_session_service: MagicMock, real_loop: asyncio.AbstractEventLoop, finalization_succeeds: bool
) -> None:
    from elspeth.web.sessions.protocol import SessionOperationMutationTransaction, SessionOperationRunMutations

    run_id = str(uuid4())
    mock_session_service.get_run.return_value = _run_record_stub(id=UUID(run_id), status="cancelled")
    transaction = MagicMock(spec_set=SessionOperationMutationTransaction)
    transaction.runs = create_autospec(SessionOperationRunMutations, instance=True, spec_set=True)
    mock_session_service.session_operation_authority.mutate.side_effect = lambda context, mutation: mutation(transaction)
    cast(Any, service)._call_async = real_loop.run_until_complete
    shutdown = threading.Event()
    shutdown.set()
    if not finalization_succeeds:
        blob_service = _blob_service_stub()
        blob_service.finalize_run_output_blobs.side_effect = OSError("output storage unavailable")
        service._blob_service = blob_service
    service._run_pipeline(run_id, "source:\n  plugin: csv", shutdown, session_operation_lease=_execute_lease())
    assert mock_session_service.update_run_status.call_args.kwargs["status"] == "cancelled"
    if finalization_succeeds:
        transaction.runs.mark_recovery_outputs_finalized.assert_called_once_with(run_id=UUID(run_id))
    else:
        transaction.runs.mark_recovery_outputs_finalized.assert_not_called()


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


def _fork_gate_node(*, node_id: str, input_label: str, fork_to: tuple[str, ...]) -> Any:
    """Fork gate duplicating each row into every ``fork_to`` branch."""
    from elspeth.web.composer.state import NodeSpec

    return NodeSpec(
        id=node_id,
        node_type="gate",
        plugin=None,
        input=input_label,
        on_success=None,
        on_error=None,
        options={},
        condition="True",
        routes={"true": "fork", "false": "fork"},
        fork_to=list(fork_to),
        branches=None,
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


class TestWebRuntimeInfrastructure:
    """Regression coverage for web execution's orchestrator runtime wiring."""

    @pytest.mark.parametrize("resume_existing", [False, True])
    def test_raw_pipeline_and_export_eligibility_precede_secret_resolution_without_shape_skip(
        self,
        service: ExecutionServiceImpl,
        resume_existing: bool,
    ) -> None:
        from elspeth.engine.orchestrator.preflight import SinkEffectCapabilityError, SinkEffectExecutionPurpose

        service._settings.compartment_id = "operator-a"
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
    signing_mode: unsigned
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

        def validate(raw: object, *, purpose: SinkEffectExecutionPurpose) -> dict[str, object]:
            assert type(raw) is dict
            assert raw["landscape"]["export"]["compartment_id"] == "operator-a"
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
            service._run_pipeline(
                str(uuid4()),
                pipeline_yaml,
                threading.Event(),
                user_id="alice",
                session_operation_lease=_execute_lease(),
                resume_existing=resume_existing,
            )

        assert purposes == [
            SinkEffectExecutionPurpose.RESUME if resume_existing else SinkEffectExecutionPurpose.FRESH,
            SinkEffectExecutionPurpose.AUDIT_EXPORT,
        ]
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
            service._run_pipeline(str(uuid4()), pipeline_yaml, threading.Event(), session_operation_lease=_execute_lease())

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
            session_operation_lease=_execute_lease(),
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
            service._run_pipeline(run_id, pipeline_yaml, threading.Event(), session_operation_lease=_execute_lease())

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

        service._run_pipeline(str(uuid4()), pipeline_yaml, threading.Event(), session_operation_lease=_execute_lease())

        completed_calls = [
            call for call in mock_session_service.update_run_status.await_args_list if call.kwargs.get("status") == "completed"
        ]
        assert completed_calls
        assert output_path.exists()


# ── B2: shutdown_event Always Passed ───────────────────────────────────


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
            service._run_pipeline(str(uuid4()), _TEST_PIPELINE_YAML, threading.Event(), session_operation_lease=_execute_lease())

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
        """Rate limits are the operator's block, persisted under the web app state root.

        A composition cannot carry ``rate_limit``, so the loaded pipeline's value
        is only ever the engine default; building the limiter from it pinned
        every web run to 60 calls/minute with no operator override.
        """
        _configure_runtime_success(
            mock_load=mock_load,
            mock_instantiate=mock_instantiate,
            mock_graph_cls=mock_graph_cls,
            mock_orch_cls=mock_orch_cls,
        )
        mock_settings.data_dir = Path("/tmp/custom-web-state")
        operator_rate_limit = RateLimitSettings(default_requests_per_minute=6000)
        mock_settings.execution_rate_limit = operator_rate_limit

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
            service._run_pipeline(str(uuid4()), _TEST_PIPELINE_YAML, threading.Event(), session_operation_lease=_execute_lease())

        mock_from_settings.assert_called_once_with(operator_rate_limit, state_dir=Path("/tmp/custom-web-state"))
        assert mock_from_settings.call_args.args[0] is not mock_load.return_value.rate_limit


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

        async def read_blob_content(_blob_id: UUID, *, session_operation_context: SessionOperationContext) -> bytes:
            order.append("read")
            return content

        async def get_blob(_blob_id: UUID, *, session_operation_context: SessionOperationContext) -> Any:
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
            service._run_pipeline(str(run_id), pipeline_yaml, threading.Event(), session_operation_lease=_execute_lease())

        assert order.index("link") < order.index("read")
        assert order.index("metadata") < order.index("record") < order.index("load")
        blob_service.link_blob_to_run.assert_awaited_once_with(
            blob_id=blob_id, run_id=run_id, direction="input", session_operation_context=_execute_lease().context
        )
        blob_service.read_blob_content.assert_awaited_once_with(blob_id, session_operation_context=_execute_lease().context)
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
            service._run_pipeline(str(run_id), pipeline_yaml, threading.Event(), session_operation_lease=_execute_lease())

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
            service._run_pipeline(str(run_id), pipeline_yaml, threading.Event(), session_operation_lease=_execute_lease())

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

        async def get_blob(blob_id: UUID, *, session_operation_context: SessionOperationContext) -> Any:
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
            service._run_pipeline(str(run_id), pipeline_yaml, threading.Event(), session_operation_lease=_execute_lease())

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
            service._run_pipeline(str(run_id), pipeline_yaml, threading.Event(), session_operation_lease=_execute_lease())

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
            async def get_blob(_blob_id: UUID, *, session_operation_context: SessionOperationContext) -> Any:
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
            service._run_pipeline(str(run_id), pipeline_yaml, threading.Event(), session_operation_lease=_execute_lease())

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


class TestBlobRowsDiscoveryReadsFrozenConfig:
    """``_discover_blob_rows_sources`` decides whether ADMISSION runs at all.

    Its answer gates the whole ``blob_rows`` admission block — session
    ownership, ``ready`` status, payload-hash and metadata-divergence checks.
    A source block it fails to recognise is therefore not a conservative
    abstention; it is a silent skip of every one of those controls, and one
    that no existing test would catch, because a skipped block asserts
    nothing.

    ``FrozenRunSettings.__post_init__`` deep-freezes ``executable_config``,
    which renders every nested source block as a ``mappingproxy``. These pins
    are built through that real producer (never ``deep_freeze`` on a literal,
    which would keep passing if the producer stopped freezing) and assert the
    frozen and thawed forms of the SAME config yield the SAME discovery — so
    the pin survives any future improvement rather than encoding today's bug.
    """

    @staticmethod
    def _frozen_and_thawed(config: dict[str, Any]) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
        settings = FrozenRunSettings(
            plugin_snapshot=PluginAvailabilitySnapshot.for_trained_operator(create_catalog_service()),
            executable_config=config,
            audit_safe_config={},
        )
        frozen = settings.executable_config
        # The producer really did freeze: without this the pin degrades to
        # comparing two thawed mappings and proves nothing.
        assert type(frozen) is MappingProxyType
        return frozen, cast(Mapping[str, Any], deep_thaw(frozen))

    @pytest.mark.parametrize("singular", [False, True])
    def test_declared_blob_rows_source_is_discovered_frozen_and_thawed(self, singular: bool) -> None:
        entry = _blob_rows_entry(b"page", blob_id=uuid4())
        config = json.loads(_blob_rows_pipeline_yaml([entry], singular=singular))
        frozen, thawed = self._frozen_and_thawed(config)

        from_frozen = _discover_blob_rows_sources(frozen)
        from_thawed = _discover_blob_rows_sources(thawed)

        expected_label = "source" if singular else "source:documents"
        assert [label for label, _options in from_frozen] == [expected_label]
        assert [label for label, _options in from_thawed] == [expected_label]
        assert [(label, deep_thaw(options)) for label, options in from_frozen] == from_thawed

    @pytest.mark.parametrize("singular", [False, True])
    def test_config_without_blob_rows_discovers_nothing_frozen_or_thawed(self, singular: bool) -> None:
        """The untouched arm: recognising the frozen form must not invent sources."""
        source_block = {"plugin": "csv", "on_success": "rows", "options": {"path": "in.csv"}}
        config: dict[str, Any] = {"sinks": {"primary": {"plugin": "json", "options": {"path": "out.jsonl"}}}}
        if singular:
            config["source"] = source_block
        else:
            config["sources"] = {"documents": source_block}
        frozen, thawed = self._frozen_and_thawed(config)

        assert _discover_blob_rows_sources(frozen) == []
        assert _discover_blob_rows_sources(thawed) == []


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

        async def get_blob(blob_id: UUID, *, session_operation_context: SessionOperationContext) -> BlobRecord:
            order.append("metadata")
            return records_by_id[blob_id]

        async def link_blob_to_run(**_kwargs: Any) -> None:
            order.append("link")

        async def read_blob_content(blob_id: UUID, *, session_operation_context: SessionOperationContext) -> bytes:
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
            service._run_pipeline(
                str(run_id), _blob_rows_pipeline_yaml(entries), threading.Event(), session_operation_lease=_execute_lease()
            )

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

            async def get_blob(_blob_id: UUID, *, session_operation_context: SessionOperationContext) -> BlobRecord:
                raise BlobNotFoundError(str(_blob_id))

            blob_service.get_blob.side_effect = get_blob
        else:
            # Fully consistent record — hash, filename, size all match — but
            # owned by a DIFFERENT session. Must be indistinguishable from missing.
            blob_service.get_blob.return_value = _blob_rows_record_for_entry(entry, session_id=uuid4())
        blob_service.finalize_run_output_blobs.return_value = BlobFinalizationResult(finalized=(), errors=())
        cast(Any, service)._blob_service = blob_service

        with pytest.raises(BlobNotFoundError):
            service._run_pipeline(
                str(uuid4()), _blob_rows_pipeline_yaml([entry]), threading.Event(), session_operation_lease=_execute_lease()
            )

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
            service._run_pipeline(
                str(uuid4()), _blob_rows_pipeline_yaml([entry]), threading.Event(), session_operation_lease=_execute_lease()
            )

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
            service._run_pipeline(
                str(uuid4()), _blob_rows_pipeline_yaml([entry]), threading.Event(), session_operation_lease=_execute_lease()
            )

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
            service._run_pipeline(
                str(run_id), _blob_rows_pipeline_yaml([entry], singular=True), threading.Event(), session_operation_lease=_execute_lease()
            )

        blob_service.link_blob_to_run.assert_awaited_once_with(
            blob_id=blob_id, run_id=run_id, direction="input", session_operation_context=_execute_lease().context
        )
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
            service._run_pipeline(
                str(uuid4()), _blob_rows_pipeline_yaml([entry]), threading.Event(), session_operation_lease=_execute_lease()
            )

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
            service._run_pipeline(str(uuid4()), pipeline_yaml, threading.Event(), session_operation_lease=_execute_lease())

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
            service._run_pipeline(str(uuid4()), _TEST_PIPELINE_YAML, threading.Event(), session_operation_lease=_execute_lease())

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
            service._run_pipeline(str(uuid4()), _TEST_PIPELINE_YAML, threading.Event(), session_operation_lease=_execute_lease())

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
                service._run_pipeline(run_id, _TEST_PIPELINE_YAML, event, session_operation_lease=_execute_lease())

        # finally must have removed the event
        assert run_id not in service._shutdown_events

    def test_done_callback_logs_last_resort_on_exception(
        self, service: ExecutionServiceImpl, real_loop: asyncio.AbstractEventLoop, execution_fixture: ExecutionTestCustody
    ) -> None:
        """Callback logs a last-resort diagnostic when the pipeline future
        carries an exception.  This covers the edge case where _run_pipeline's
        own except block failed (e.g. update_run_status raised).
        """
        service._loop = real_loop
        stage = real_loop.run_until_complete(execution_fixture.callback_stage(service, error=RuntimeError("unhandled")))

        with patch("elspeth.web.execution.service.slog") as mock_slog:
            real_loop.run_until_complete(stage.join())
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

    @pytest.mark.parametrize(
        "signal_factory",
        [lambda: KeyboardInterrupt("ctrl-c"), lambda: SystemExit(1)],
        ids=["keyboard_interrupt", "system_exit"],
    )
    def test_done_callback_skips_the_last_resort_diagnostic_for_shutdown_signals(
        self,
        service: ExecutionServiceImpl,
        real_loop: asyncio.AbstractEventLoop,
        signal_factory: Callable[[], BaseException],
        execution_fixture: ExecutionTestCustody,
    ) -> None:
        """A signal is an interpreter-directed shutdown, not a run failure.

        ``_run_pipeline`` already treats KeyboardInterrupt / SystemExit as
        shutdown: it skips the failed-status write and the
        ``run_pipeline_failed`` operator diagnostic and emits
        ``skipping_status_update_on_signal`` instead. This callback mirrors
        that decision, and the mirroring is only safe because authority
        release does NOT hang off the classification — the lease is closed on
        every path before the branch is reached.
        """
        service._loop = real_loop
        stage = real_loop.run_until_complete(execution_fixture.callback_stage(service, error=signal_factory()))
        lease = stage.lease

        with patch("elspeth.web.execution.service.slog") as mock_slog:
            real_loop.run_until_complete(stage.join())
            mock_slog.error.assert_not_called()

        assert lease.closed

    def test_done_callback_walks_exception_chain(
        self, service: ExecutionServiceImpl, real_loop: asyncio.AbstractEventLoop, execution_fixture: ExecutionTestCustody
    ) -> None:
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
            original = outer

        service._loop = real_loop
        stage = real_loop.run_until_complete(execution_fixture.callback_stage(service, error=original))
        with patch("elspeth.web.execution.service.slog") as mock_slog:
            real_loop.run_until_complete(stage.join())
            call_kwargs = mock_slog.error.call_args[1]
            assert call_kwargs["exc_type"] == "RuntimeError"
            assert call_kwargs["exc_class_chain"] == ["RuntimeError", "ValueError"]
            # No ``str(exc)`` text should appear in any field.
            for value in call_kwargs.values():
                if isinstance(value, str):
                    assert "secret" not in value
                    assert "deadbeef" not in value

    def test_done_callback_noop_on_success(
        self, service: ExecutionServiceImpl, real_loop: asyncio.AbstractEventLoop, execution_fixture: ExecutionTestCustody
    ) -> None:
        """done_callback does not log on successful completion."""
        service._loop = real_loop
        stage = real_loop.run_until_complete(execution_fixture.callback_stage(service))

        with patch("elspeth.web.execution.service.slog") as mock_slog:
            real_loop.run_until_complete(stage.join())
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
            service._run_pipeline(run_id, "source:\n  plugin: csv\n", threading.Event(), session_operation_lease=_execute_lease())

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
            coordination_token=leader_coordination_token(factory, run_id),
            node_id="source",
            plugin_name="csv",
            node_type=NodeType.SOURCE,
            plugin_version="1.0",
            config={},
            schema_config=_DIAGNOSTIC_OBSERVED_SCHEMA,
        )
    factory.data_flow.register_node(
        coordination_token=leader_coordination_token(factory, run_id),
        node_id=node_id,
        plugin_name="llm_extract",
        node_type=NodeType.TRANSFORM,
        plugin_version="1.0",
        config={},
        schema_config=_DIAGNOSTIC_OBSERVED_SCHEMA,
    )
    _row, token = factory.data_flow.create_row_with_token(
        "source",
        ingest_sequence,
        {"html": "<h1>A</h1>"},
        row_id=row_id,
        token_id=token_id,
        coordination_token=leader_coordination_token(factory, run_id),
        source_row_index=ingest_sequence,
        ingest_sequence=ingest_sequence,
    )
    state = factory.execution.begin_node_state(
        token.token_id,
        node_id,
        1,
        {"html": "<h1>A</h1>"},
        state_id=state_id,
        member_token=leader_coordination_token(factory, run_id).membership,
    )
    factory.execution.complete_node_state(
        state.state_id,
        status,
        member_token=leader_coordination_token(factory, run_id).membership,
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
            service._run_pipeline(run_id, _TEST_PIPELINE_YAML, threading.Event(), session_operation_lease=_execute_lease())

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
            service._run_pipeline(run_id, _TEST_PIPELINE_YAML, threading.Event(), session_operation_lease=_execute_lease())

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
            service._run_pipeline(str(uuid4()), _TEST_PIPELINE_YAML, threading.Event(), session_operation_lease=_execute_lease())

        assert [call for call in mock_slog.error.call_args_list if call.args and call.args[0] == "run_pipeline_failed"] == []


class TestStructuralFramePath:
    """``_structural_frame_path`` must never emit an absolute or unbounded path."""

    @pytest.mark.parametrize(
        ("filename", "expected"),
        [
            # Package frames render from the package root.
            ("/opt/venv/lib/python3.12/site-packages/elspeth/web/execution/service.py", "elspeth/web/execution/service.py"),
            # A checkout directory sharing the package name must not widen the path.
            ("/opt/project/elspeth/src/elspeth/engine/coalesce_executor.py", "elspeth/engine/coalesce_executor.py"),
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

        rendered = _structural_frame_path("/opt/project/elspeth/.claude/worktrees/wt/tests/unit/web/test_x.py")
        assert not rendered.startswith("/")
        assert "/home/" not in rendered
        assert ".claude" not in rendered
        assert len(rendered.split("/")) <= _MAX_FRAME_PATH_PARTS


def _governed_approval_inputs(service: ExecutionServiceImpl) -> ApprovalGateInputs:
    snapshot = service._plugin_snapshot_for_user("author", operation="approval test")
    return ApprovalGateInputs(
        evidence=_build_web_plugin_policy_evidence(snapshot=snapshot, policy=service._web_plugin_policy),
        config_hash="a" * 64,
        canonical_version="sha256-rfc8785-v1",
        openrouter_catalog_sha256="b" * 64,
        runtime_val_manifest_sha256="c" * 64,
    )


# ── Cancel Mechanism ───────────────────────────────────────────────────


@pytest.mark.parametrize("refusal", ["missing_baseline", "incomplete_source", "compatibility_mismatch", "unsafe_effect"])
def test_durable_recovery_refusal_preserves_reason_without_terminalizing(
    service: ExecutionServiceImpl, mock_session_service: MagicMock, refusal: str
) -> None:
    from elspeth.engine.orchestrator.preflight import SinkEffectCapabilityError
    from elspeth.web.coordination.contracts import RecoveryRequiredReason
    from elspeth.web.execution.service import _RunRecoveryRequired
    from elspeth.web.sessions.protocol import SessionOperationMutationTransaction, SessionOperationRunMutations

    reason = RecoveryRequiredReason(refusal)
    transaction = MagicMock(spec=SessionOperationMutationTransaction)
    transaction.runs = MagicMock(spec=SessionOperationRunMutations)
    mock_session_service.session_operation_authority.mutate.side_effect = lambda _context, action: action(transaction)
    with (
        patch.object(
            service,
            "_restore_admitted_run",
            new=AsyncMock(
                spec=service._restore_admitted_run,
                side_effect=SinkEffectCapabilityError("unsafe recovery effect")
                if reason is RecoveryRequiredReason.UNSAFE_EFFECT
                else _RunRecoveryRequired(reason),
            ),
        ),
        patch.object(service, "_finalize_output_blobs") as finalize,
    ):
        run_id = uuid4()
        service._run_pipeline(
            str(run_id),
            _TEST_PIPELINE_YAML,
            threading.Event(),
            session_operation_lease=_execute_lease(),
            durable_admission=True,
        )
    transaction.runs.mark_recovery_required.assert_called_once_with(run_id=run_id, reason=reason)
    mock_session_service.assess_run_start_admission.assert_awaited_once()
    mock_session_service.issue_run_start_permit.assert_not_called()
    mock_session_service.update_run_status.assert_not_called()
    finalize.assert_not_called()


class TestP2aCleanupCatchNarrowing:
    """Preserve redaction and programmer-error visibility before transfer.

    A literal submit-no-return is a separate owned Unknown boundary below;
    it cannot authorize immediate status compensation.
    """

    @pytest.mark.asyncio
    async def test_setup_failure_cleanup_slog_uses_exc_class_not_str(
        self, service: ExecutionServiceImpl, mock_session_service: MagicMock, execution_fixture: ExecutionTestCustody
    ) -> None:
        """Actual blob-link failure reaches class-only pretransfer cleanup."""
        from sqlalchemy.exc import OperationalError

        session_id, run_id = uuid4(), uuid4()
        blob_ref = str(uuid4())
        canonical_path = f"/tmp/data/blobs/{session_id}/{blob_ref}_input.csv"
        mock_session_service.create_run.return_value = _run_record_stub(id=run_id)
        original = RuntimeError("blob storage unavailable")
        cleanup_original = OperationalError(
            "UPDATE runs ...",
            {"id": str(run_id), "error": "Setup failed: SuperSecretDSN://u:p@h/d"},
            Exception("lock wait timeout exceeded — __cause__ carries DSN"),
        )
        mock_session_service.update_run_status.side_effect = cleanup_original
        blob_service = _blob_service_stub()
        blob_service.get_blob.return_value = _ready_csv_blob_for_execution(
            blob_ref=blob_ref, session_id=session_id, storage_path=canonical_path
        )
        blob_service.link_blob_to_run.side_effect = original
        cast(Any, service)._blob_service = blob_service
        state = mock_session_service.get_current_state.return_value
        state.source = {
            "plugin": "csv",
            "on_success": "continue",
            "options": {"blob_ref": blob_ref, "path": canonical_path},
            "on_validation_failure": "quarantine",
        }
        observed = execution_fixture.observe_authority(session_id)
        with patch("elspeth.web.execution.service.slog") as mock_slog, pytest.raises(RuntimeError) as caught:
            await _execute(service, session_id=session_id, authority=observed, execution_fixture=execution_fixture)
        assert caught.value is original, "P2A_BLOB_PRIMARY_IDENTITY"
        blob_service.link_blob_to_run.assert_awaited_once()
        context = observed.calls[0][1]
        assert mock_session_service.update_run_status.await_count == 1, "P2A_CLEANUP_REACHED"
        update = mock_session_service.update_run_status.await_args
        assert update.args == (run_id,)
        assert update.kwargs["status"] == "failed"
        assert update.kwargs["session_operation_context"] is context
        assert cleanup_original.__traceback__ is not None
        slog_calls = [c for c in mock_slog.error.call_args_list if c[0] and c[0][0] == "run_cleanup_status_update_failed"]
        assert len(slog_calls) == 1, mock_slog.error.call_args_list
        kwargs = slog_calls[0][1]
        assert kwargs["cleanup_exc_class"] == "OperationalError"
        assert kwargs["original_exc_class"] == "RuntimeError"
        assert "cleanup_error" not in kwargs
        assert "original_error" not in kwargs
        assert str(run_id) not in service._shutdown_events
        assert _worker_lease is None, "P2A_UNUSED_ADOPTED_LEASE"

    @pytest.mark.asyncio
    async def test_setup_cleanup_narrow_catch_lets_runtimeerror_escape(
        self, service: ExecutionServiceImpl, mock_session_service: MagicMock, execution_fixture: ExecutionTestCustody
    ) -> None:
        """The actually awaited cleanup RuntimeError escapes the narrow catch."""
        session_id, run_id = uuid4(), uuid4()
        observed = execution_fixture.observe_authority(session_id)
        lease = await execution_fixture.acquire(
            service.execution_lease_release_registry,
            observed.authority,
            session_id=session_id,
            owner_instance_id="p2a-untransferred-handler",
            lease_seconds=30,
        )
        obligation = lease.execution_obligation
        assert obligation is not None
        original = RuntimeError("setup predecessor")
        cleanup_original = RuntimeError("dataclass contract violated inside update_run_status")
        assert cleanup_original is not original
        mock_session_service.update_run_status.side_effect = cleanup_original
        try:
            assert not obligation.completion_required and obligation.pipeline is None
            with pytest.raises(RuntimeError) as caught:
                await service._handle_pipeline_submission_failure(run_id, original, session_operation_lease=lease)
            assert caught.value is cleanup_original, "P2A_NARROW_CLEANUP_IDENTITY"
            mock_session_service.update_run_status.assert_awaited_once()
            update = mock_session_service.update_run_status.await_args
            assert update.args == (run_id,)
            assert update.kwargs["session_operation_context"] is lease.context
            assert update.kwargs["status"] == "failed"
            assert not obligation.completion_required
        except BaseException as primary:
            await close_execute_lease_before_transfer(lease, primary=primary)
            raise
        else:
            await close_execute_lease_before_transfer(lease)
        assert lease.closed and obligation.release_succeeded
        assert _worker_lease is None, "P2A_UNUSED_ADOPTED_LEASE"


def _p2a_original_leaves(original: BaseException) -> list[BaseException]:
    if isinstance(original, BaseExceptionGroup):
        return [leaf for child in original.exceptions for leaf in _p2a_original_leaves(child)]
    return [original]


class TestP2aSubmitNoReturnCustody:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("planned_cleanup", ["sql", "runtime"])
    async def test_literal_submit_original_survives_actual_private_join(
        self,
        service: ExecutionServiceImpl,
        mock_session_service: MagicMock,
        execution_fixture: ExecutionTestCustody,
        planned_cleanup: str,
    ) -> None:
        """Unknown submit has no immediate compensation or false COMPLETE."""
        from sqlalchemy.exc import OperationalError

        session_id = uuid4()
        registry = service.execution_lease_release_registry
        observed = execution_fixture.observe_authority(session_id)
        lease = await execution_fixture.acquire(
            registry, observed.authority, session_id=session_id, owner_instance_id="p2a-submit-owner", lease_seconds=30
        )
        obligation = lease.execution_obligation
        assert obligation is not None
        original = RuntimeError("pool shutdown")
        cleanup_original = (
            OperationalError("UPDATE runs ...", {}, Exception("private cleanup fault"))
            if planned_cleanup == "sql"
            else RuntimeError("dataclass contract violated inside update_run_status")
        )
        mock_session_service.update_run_status.side_effect = cleanup_original
        executor = service._executor
        assert isinstance(executor, concurrent.futures.ThreadPoolExecutor)
        entries = []
        authority_returns = []
        close_authority = service._close_execution_authority

        async def observe_authority_return(
            closing: SessionOperationLease, watcher: asyncio.Task[None] | None, error: BaseException | None
        ) -> None:
            await close_authority(closing, watcher, error)
            assert closing is lease
            actual_task = asyncio.current_task()
            assert actual_task is obligation.completion_task, "P2A_COMPLETION_PRODUCER_IDENTITY"
            assert obligation.release_succeeded, "P2A_RELEASE_BEFORE_COMPLETION"
            assert not obligation.completion_outcome_recorded and not obligation.completion_observed
            assert not obligation.retired and registry._pending[id(obligation)] is obligation, "P2A_COMPLETION_PENDING_AT_RELEASE_RETURN"
            authority_returns.append(actual_task)

        def submit_raises(invocation: Callable[[], Any]) -> Future[Any]:
            assert isinstance(invocation, partial)
            assert invocation.func.__self__ is service
            assert invocation.func.__func__ is ExecutionServiceImpl._run_pipeline
            assert invocation.keywords["session_operation_lease"] is lease
            assert obligation.completion_required, "P2A_COMPLETION_BEFORE_SUBMIT"
            assert obligation.context is obligation.acquire_submission.source_value
            assert obligation.acquire_submission.observed
            entries.append(invocation)
            raise original

        with (
            patch.object(executor, "submit", side_effect=submit_raises),
            patch.object(service, "_close_execution_authority", side_effect=observe_authority_return),
            patch("elspeth.web.execution.service.slog") as mock_slog,
        ):
            with pytest.raises(RuntimeError) as caught:
                await service.execute(session_id, session_operation_lease=lease)
            assert caught.value is original, "P2A_SUBMIT_ORIGINAL_IDENTITY"
            assert len(entries) == 1
            assert obligation.pipeline_submission_unknown is original, "P2A_UNKNOWN_ORIGINAL_IDENTITY"
            assert any(original is leaf for root in registry._failures for leaf in _p2a_original_leaves(root)), (
                "P2A_PREJOIN_RETAINED_ORIGINAL"
            )
            assert registry._pending[id(obligation)] is obligation
            assert not obligation.retired and not lease.closed, "P2A_NO_PREMATURE_RETIREMENT"
            assert obligation.completion_required and obligation.pipeline is None
            assert obligation.completion_task is None and obligation.release_submission is None
            assert not registry.executor_join_physically_observed
            mock_session_service.update_run_status.assert_not_awaited()
            assert mock_session_service.update_run_status.await_count == 0, "P2A_NO_PREMATURE_COMPENSATION"
            assert not any(c.args and c.args[0] == "run_cleanup_status_update_failed" for c in mock_slog.error.call_args_list)
            assert _worker_lease is None, "P2A_UNUSED_ADOPTED_LEASE"

            with pytest.raises(BaseException) as shutdown:
                await execution_fixture.shutdown_service(service)
            shutdown_leaves = _p2a_original_leaves(shutdown.value)
            assert len(shutdown_leaves) == 1 and shutdown_leaves[0] is original, "P2A_JOIN_RETAINS_ORIGINAL"

        assert registry.executor_join_physically_observed and registry.executor_join_succeeded
        assert service._executor is executor
        assert obligation.pipeline is None
        task = obligation.completion_task
        assert isinstance(task, asyncio.Task) and task.done(), "P2A_ACTUAL_COMPLETION_JOIN"
        assert len(authority_returns) == 1 and authority_returns[0] is task, "P2A_ACTUAL_AUTHORITY_RETURN"
        assert obligation.completion_outcome_recorded and obligation.completion_observed
        assert obligation.completion_original_error is None
        assert obligation.unknown_pipeline_cleanup_declared
        release = obligation.release_submission
        assert release is not None
        future, reservation = release.future, release.reservation
        assert isinstance(future, Future) and future.done(), "P2A_ACTUAL_RELEASE_FUTURE"
        assert reservation is not None and reservation.future is future
        assert reservation._registering_generation is obligation.generation
        assert reservation.released and release.callback_return_observed, "P2A_RELEASE_CALLBACK_COUNTER"
        witness = reservation.witness.snapshot()
        assert witness.callable_finished and witness.exited and not witness.impossible
        assert release.observed and release.original_error is None
        assert lease.closed and obligation.release_succeeded
        assert obligation.retired and id(obligation) not in registry._pending
        assert any(original is leaf for root in registry._failures for leaf in _p2a_original_leaves(root)), "P2A_RETAINED_REGISTRY_ORIGINAL"
        assert cleanup_original.__traceback__ is None
        mock_session_service.update_run_status.assert_not_awaited()
        with pytest.raises(BaseExceptionGroup) as refused:
            registry.assert_completed()
        refused_leaves = _p2a_original_leaves(refused.value)
        assert any(leaf is original for leaf in refused_leaves)
        assert any(type(leaf) is AuditIntegrityError for leaf in refused_leaves), "P2A_FAILED_COMPLETE_REFUSAL"

        roots_before = tuple(registry._failures)
        for copied in (
            RuntimeError("pool shutdown"),
            cleanup_original,
            RuntimeError("unrelated cleanup"),
            asyncio.CancelledError("unrelated"),
        ):
            try:
                execution_fixture.witness_cleanup_original(copied)
            except AssertionError:
                pass
            else:
                raise AssertionError("P2A_COPIED_WITNESS_REFUSED")
            assert not any(copied is item for item in execution_fixture.witnessed_cleanup_originals), "P2A_COPIED_WITNESS_REFUSED"
        execution_fixture.witness_cleanup_original(original)
        assert any(item is original for item in execution_fixture.witnessed_cleanup_originals)
        assert len(registry._failures) == len(roots_before)
        assert all(now is before for now, before in zip(registry._failures, roots_before, strict=True))
        with pytest.raises(BaseExceptionGroup):
            registry.assert_completed()


# ── Completion-Path Guard ─────────────────────────────────────────────


# ── Post-Completion Exception Recovery (elspeth-879f6de6bd) ───────────


# ── Liveness Registry ─────────────────────────────────────────────────


# ── Blob Ref Pre-Validation ───────────────────────────────────────────


class TestBlobRefPreValidation:
    """Malformed blob_ref must raise BEFORE create_run() to avoid
    orphaning a pending run that blocks future executions."""

    @pytest.mark.parametrize("blob_ref", [None, 42, "not-a-uuid", "AAAAAAAA-1111-4111-8111-111111111111"])
    def test_proof_resolver_rejects_malformed_present_binding(self, service: ExecutionServiceImpl, blob_ref: object) -> None:
        from dataclasses import replace

        from elspeth.web.execution.errors import MalformedBlobRefError

        state = _proof_gate_state(source_path=Path("/tmp/source.csv"), blob_id=None)
        source = state.sources["source"]
        state = replace(state, sources={"source": replace(source, options={**source.options, "blob_ref": blob_ref})})
        lease = _execute_lease()
        with pytest.raises(MalformedBlobRefError, match=r"sources\.source\.blob_ref"):
            service._authoritative_proof_blob_resolver(
                state,
                session_id=UUID(lease.context.fence.session_id),
                session_operation_context=lease.context,
            )

    @pytest.mark.asyncio
    async def test_malformed_blob_ref_raises_before_run_creation(
        self, service: ExecutionServiceImpl, mock_session_service: MagicMock, execution_fixture: ExecutionTestCustody
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
            await _execute(service, session_id=uuid4(), execution_fixture=execution_fixture)

        # The critical invariant: create_run() was never called,
        # so no stale pending run exists.
        mock_session_service.create_run.assert_not_called()

    @pytest.mark.asyncio
    async def test_valid_blob_ref_still_links_correctly(
        self, service: ExecutionServiceImpl, mock_session_service: MagicMock, execution_fixture: ExecutionTestCustody
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

        authority = execution_fixture.observe_authority(session_id)
        with patch.object(service, "_run_pipeline"):
            await _execute(service, session_id=session_id, authority=authority, execution_fixture=execution_fixture)

        blob_service.link_blob_to_run.assert_called_once_with(
            blob_id=UUID(blob_ref),
            run_id=run_id,
            direction="input",
            session_operation_context=authority.calls[0][1],
        )

    @pytest.mark.asyncio
    async def test_second_named_source_malformed_blob_ref_raises_before_run_creation(
        self, service: ExecutionServiceImpl, mock_session_service: MagicMock, execution_fixture: ExecutionTestCustody
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
            await _execute(service, session_id=session_id, execution_fixture=execution_fixture)

        mock_session_service.create_run.assert_not_called()
        blob_service.get_blob.assert_not_called()

    @pytest.mark.asyncio
    async def test_named_blob_sources_all_link_to_run(
        self, service: ExecutionServiceImpl, mock_session_service: MagicMock, execution_fixture: ExecutionTestCustody
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
        blob_service.get_blob.side_effect = lambda blob_id, *, session_operation_context: _ready_csv_blob_for_execution(
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
            await _execute(service, session_id=session_id, execution_fixture=execution_fixture)

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
        self, service: ExecutionServiceImpl, mock_session_service: MagicMock, execution_fixture: ExecutionTestCustody
    ) -> None:
        """The authoritative proof collapses cross-session custody to a generic rejection."""

        executing_session_id = uuid4()
        other_session_id = uuid4()
        blob_ref = str(uuid4())
        canonical_path = f"/tmp/data/blobs/{executing_session_id}/{blob_ref}_input.csv"

        blob_service = _blob_service_stub()
        # Blob belongs to other_session_id, not executing_session_id
        blob_service.get_blob.return_value = _ready_csv_blob_for_execution(
            blob_ref=blob_ref,
            session_id=other_session_id,
            storage_path=canonical_path,
        )
        cast(Any, service)._blob_service = blob_service

        state = mock_session_service.get_current_state.return_value
        state.source = {
            "plugin": "csv",
            "on_success": "continue",
            "options": {"blob_ref": blob_ref, "path": canonical_path},
            "on_validation_failure": "quarantine",
        }

        authority = execution_fixture.observe_authority(executing_session_id)
        with pytest.raises(PipelineValidationError) as exc_info:
            await _execute(service, session_id=executing_session_id, authority=authority, execution_fixture=execution_fixture)
        assert [error.error_code for error in exc_info.value.errors] == ["source_blob_unavailable"]
        assert [error.message for error in exc_info.value.errors] == [
            "The claimed source content could not be verified as ready for this request."
        ]
        assert str(other_session_id) not in str(exc_info.value)

        blob_service.get_blob.assert_awaited_once_with(UUID(blob_ref), session_operation_context=authority.calls[0][1])
        blob_service.read_blob_content_prefix_verified.assert_not_awaited()
        blob_service.link_blob_to_run.assert_not_called()
        # Critical: create_run was never called (rejected before run creation)
        mock_session_service.create_run.assert_not_called()

    @pytest.mark.asyncio
    async def test_second_named_source_cross_session_blob_ref_rejected(
        self, service: ExecutionServiceImpl, mock_session_service: MagicMock, execution_fixture: ExecutionTestCustody
    ) -> None:
        """Cross-session blob_ref on a non-first named source preserves IDOR collapse."""

        executing_session_id = uuid4()
        other_session_id = uuid4()
        orders_blob = str(uuid4())
        refunds_blob = str(uuid4())
        orders_path = f"/tmp/data/blobs/{executing_session_id}/{orders_blob}_orders.csv"
        refunds_path = f"/tmp/data/blobs/{executing_session_id}/{refunds_blob}_refunds.csv"

        blob_service = _blob_service_stub()
        blob_service.get_blob.side_effect = lambda blob_id, *, session_operation_context: _ready_csv_blob_for_execution(
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

        with pytest.raises(PipelineValidationError) as exc_info:
            await _execute(service, session_id=executing_session_id, execution_fixture=execution_fixture)
        assert [error.error_code for error in exc_info.value.errors] == ["source_blob_unavailable"]

        mock_session_service.create_run.assert_not_called()
        blob_service.link_blob_to_run.assert_not_called()

    @pytest.mark.asyncio
    async def test_same_session_blob_ref_accepted(
        self, service: ExecutionServiceImpl, mock_session_service: MagicMock, execution_fixture: ExecutionTestCustody
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
            run_id = await _execute(service, session_id=session_id, execution_fixture=execution_fixture)
        assert isinstance(run_id, UUID)


# ── Blob Source Path Read Guard (Tier 1) ─────────────────────────────


class TestBlobSourcePathReadGuard:
    """Authoritative preflight rejects noncanonical blob paths before creating a run."""

    @pytest.mark.asyncio
    async def test_diverging_stored_path_raises_structured_error(
        self, service: ExecutionServiceImpl, mock_session_service: MagicMock, execution_fixture: ExecutionTestCustody
    ) -> None:
        """A stale allowlisted path is rejected before a run is created."""

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

        with pytest.raises(PipelineValidationError) as exc_info:
            await _execute(service, session_id=session_id, execution_fixture=execution_fixture)
        assert [error.error_code for error in exc_info.value.errors] == ["source_blob_unavailable"]
        assert [error.message for error in exc_info.value.errors] == [
            "The claimed source content could not be verified as ready for this request."
        ]

        # Critical: create_run was never called — the session is not
        # poisoned with a pending run that the operator must clean up.
        mock_session_service.create_run.assert_not_called()
        # link_blob_to_run was never called either — guard fires before
        # any side effects.
        blob_service.link_blob_to_run.assert_not_called()

    @pytest.mark.asyncio
    async def test_missing_stored_path_raises_structured_error(
        self, service: ExecutionServiceImpl, mock_session_service: MagicMock, execution_fixture: ExecutionTestCustody
    ) -> None:
        """A blob_ref with no stored path fails authoritative proof."""

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

        with pytest.raises(PipelineValidationError) as exc_info:
            await _execute(service, session_id=session_id, execution_fixture=execution_fixture)
        assert [error.error_code for error in exc_info.value.errors] == ["source_blob_unavailable"]

    @pytest.mark.asyncio
    async def test_named_blob_source_path_mismatch_raises_structured_error(
        self, service: ExecutionServiceImpl, mock_session_service: MagicMock, execution_fixture: ExecutionTestCustody
    ) -> None:
        """Every named source blob_ref gets the same ownership/path guard."""

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

        with pytest.raises(PipelineValidationError) as exc_info:
            await _execute(service, session_id=session_id, execution_fixture=execution_fixture)
        assert [error.error_code for error in exc_info.value.errors] == ["source_blob_unavailable"]
        mock_session_service.create_run.assert_not_called()
        blob_service.link_blob_to_run.assert_not_called()

    @pytest.mark.asyncio
    async def test_second_named_blob_source_path_mismatch_raises_structured_error(
        self, service: ExecutionServiceImpl, mock_session_service: MagicMock, execution_fixture: ExecutionTestCustody
    ) -> None:
        """A non-first named source must also match the canonical blob path."""

        session_id = uuid4()
        orders_blob = str(uuid4())
        refunds_blob = str(uuid4())
        orders_path = f"/tmp/data/blobs/{session_id}/{orders_blob}_orders.csv"
        refunds_canonical_path = f"/tmp/data/blobs/{session_id}/{refunds_blob}_refunds.csv"
        refunds_diverging_path = f"/tmp/data/blobs/{session_id}/{refunds_blob}_OTHER.csv"

        blob_service = _blob_service_stub()
        blob_service.get_blob.side_effect = lambda blob_id, *, session_operation_context: _ready_csv_blob_for_execution(
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

        with pytest.raises(PipelineValidationError) as exc_info:
            await _execute(service, session_id=session_id, execution_fixture=execution_fixture)
        assert [error.error_code for error in exc_info.value.errors] == ["source_blob_unavailable"]
        mock_session_service.create_run.assert_not_called()
        blob_service.link_blob_to_run.assert_not_called()


# ── One Active Run (B6) ───────────────────────────────────────────────


# ── EventBus Bridge ───────────────────────────────────────────────────


# ── B10: _call_async() Bridge Tests ──────────────────────────────────


# ── W15: Running Status Failure Path ─────────────────────────────────


# ── IDOR Protection: verify_run_ownership ─────────────────────────────


class TestVerifyRunOwnership:
    """IDOR protection — verify_run_ownership checks user_id + auth_provider.

    Criticality 9/10: This is the gate between "attacker can watch other
    users' pipeline progress via WebSocket" and "access denied."
    """

    @pytest.fixture
    def idor_service(
        self, mock_loop: MagicMock, broadcaster: ProgressBroadcaster, execution_fixture: ExecutionTestCustody
    ) -> tuple[ExecutionServiceImpl, Any]:
        """ExecutionServiceImpl with controllable session service."""
        session_svc = create_autospec(SessionServiceProtocol, instance=True)
        settings = _WebSettingsStub()
        settings.auth_provider = "local"
        settings.landscape_url = "sqlite:///test.db"
        settings.payload_store_path = Path("/tmp/test")

        svc = execution_fixture.bind(
            ExecutionServiceImpl.for_trained_operator(
                loop=mock_loop,
                broadcaster=broadcaster,
                settings=settings,
                session_service=session_svc,
                yaml_generator=_YamlGeneratorStub(),
                telemetry=build_sessions_telemetry(),
                execution_lease_release_registry=execution_fixture.registry(execution_fixture.loop),
            )
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
        execution_fixture: ExecutionTestCustody,
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
            await _execute(service, session_id=uuid4(), execution_fixture=execution_fixture)

    @pytest.mark.asyncio
    async def test_sink_path_traversal_rejected(
        self,
        service: ExecutionServiceImpl,
        mock_session_service: MagicMock,
        mock_settings: MagicMock,
        execution_fixture: ExecutionTestCustody,
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
            await _execute(service, session_id=uuid4(), execution_fixture=execution_fixture)

    @pytest.mark.asyncio
    async def test_sink_path_under_outputs_accepted(
        self,
        service: ExecutionServiceImpl,
        mock_session_service: MagicMock,
        mock_settings: MagicMock,
        execution_fixture: ExecutionTestCustody,
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
            run_id = await _execute(service, session_id=session_id, execution_fixture=execution_fixture)
        assert isinstance(run_id, UUID)

    @pytest.mark.asyncio
    async def test_sink_path_in_other_session_blobs_rejected(
        self,
        service: ExecutionServiceImpl,
        mock_session_service: MagicMock,
        mock_settings: MagicMock,
        execution_fixture: ExecutionTestCustody,
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
            await _execute(service, session_id=uuid4(), execution_fixture=execution_fixture)

    @pytest.mark.asyncio
    async def test_sink_path_in_other_session_outputs_rejected(
        self,
        service: ExecutionServiceImpl,
        mock_session_service: MagicMock,
        mock_settings: MagicMock,
        execution_fixture: ExecutionTestCustody,
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
            await _execute(service, session_id=executing_session, execution_fixture=execution_fixture)

    @pytest.mark.asyncio
    async def test_sink_path_in_own_session_blobs_accepted(
        self,
        service: ExecutionServiceImpl,
        mock_session_service: MagicMock,
        mock_settings: MagicMock,
        execution_fixture: ExecutionTestCustody,
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
            run_id = await _execute(service, session_id=own_session, execution_fixture=execution_fixture)
        assert isinstance(run_id, UUID)

    @pytest.mark.asyncio
    async def test_sink_without_path_option_passes(
        self,
        service: ExecutionServiceImpl,
        mock_session_service: MagicMock,
        mock_settings: MagicMock,
        execution_fixture: ExecutionTestCustody,
    ) -> None:
        """Sink with no path/file options (e.g. database sink) passes check."""
        mock_settings.data_dir = "/tmp/elspeth_data"
        state = mock_session_service.get_current_state.return_value
        state.source = None
        state.outputs = [
            {
                "name": "db_sink",
                "plugin": "database",
                "options": {},
                "on_write_failure": "discard",
            }
        ]
        state.nodes = None
        state.edges = None

        with patch.object(service, "_run_pipeline"):
            run_id = await _execute(service, session_id=uuid4(), execution_fixture=execution_fixture)
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
    def _resolved_llm_reviews(*, node_id: str, options: Mapping[str, Any], model: str) -> list[dict[str, object]]:
        """Resolved reviews for ``options``, anchored as the live resolve path anchors them.

        ``options`` is the node's option mapping WITHOUT the requirements entry.
        A multi-query node's prompt review attests the whole prompt surface, so
        its anchor comes from :func:`prompt_review_anchor_hash_from_options`;
        a single-prompt node keeps the bare ``stable_hash(prompt_template)``.
        """
        prompt_template = options["prompt_template"]
        assert isinstance(prompt_template, str)
        surface_anchor = prompt_review_anchor_hash_from_options(options)
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
                "resolved_prompt_template_hash": surface_anchor if surface_anchor is not None else stable_hash(prompt_template),
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
        execution_fixture: ExecutionTestCustody,
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
            await _execute(service, session_id=uuid4(), execution_fixture=execution_fixture)

    @pytest.mark.asyncio
    async def test_transform_persist_directory_traversal_rejected(
        self,
        service: ExecutionServiceImpl,
        mock_session_service: MagicMock,
        mock_settings: MagicMock,
        execution_fixture: ExecutionTestCustody,
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
            await _execute(service, session_id=uuid4(), execution_fixture=execution_fixture)

    @pytest.mark.asyncio
    async def test_transform_persist_directory_under_outputs_accepted(
        self,
        service: ExecutionServiceImpl,
        mock_session_service: MagicMock,
        mock_settings: MagicMock,
        execution_fixture: ExecutionTestCustody,
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
            run_id = await _execute(service, session_id=session_id, execution_fixture=execution_fixture)
        assert isinstance(run_id, UUID)

    @pytest.mark.asyncio
    async def test_non_mapping_provider_config_is_skipped_without_disarming_the_scan(
        self,
        service: ExecutionServiceImpl,
        mock_session_service: MagicMock,
        mock_settings: MagicMock,
        execution_fixture: ExecutionTestCustody,
    ) -> None:
        """A non-Mapping ``provider_config`` is skipped, and the scan keeps going.

        Pins the ``continue`` arm of the nested path allowlist scan: a value
        that is not a Mapping cannot carry ``persist_directory`` at all, so it
        is outside this gate's subject set — but skipping it must not abandon
        the remaining nodes. The offending node is ordered SECOND so a
        ``break``/``return`` in place of ``continue`` turns this test red.
        """
        mock_settings.data_dir = "/tmp/elspeth_data"
        state = mock_session_service.get_current_state.return_value
        state.source = None
        state.outputs = None
        state.nodes = [
            {
                "id": "rag-malformed",
                "node_type": "transform",
                "plugin": "rag_retrieval",
                "input": "transform_in",
                "on_success": "results",
                "on_error": "discard",
                "options": {
                    "provider": "chroma",
                    "provider_config": "/etc/cron.d/backdoor",
                },
            },
            {
                "id": "rag-escaping",
                "node_type": "transform",
                "plugin": "rag_retrieval",
                "input": "transform_in",
                "on_success": "results",
                "on_error": "discard",
                "options": {
                    "provider": "chroma",
                    "provider_config": {"persist_directory": "/etc/cron.d/backdoor"},
                },
            },
        ]
        state.edges = None

        from elspeth.web.execution.errors import PathAllowlistViolationError

        with pytest.raises(PathAllowlistViolationError, match="rag-escaping"):
            await _execute(service, session_id=uuid4(), execution_fixture=execution_fixture)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "options",
        [
            pytest.param(
                {"endpoint": "https://tenant-b.search.windows.net", "index": "payroll", "use_managed_identity": True},
                id="raw-managed-identity-no-profile",
            ),
            pytest.param(
                {"profile": "policies", "index": "approved-documents", "endpoint": "https://evil.example.com"}, id="profile-plus-endpoint"
            ),
            pytest.param(
                {"profile": "policies", "index": "approved-documents", "use_managed_identity": True}, id="profile-plus-managed-identity"
            ),
            pytest.param({"profile": "policies", "index": "hr-records"}, id="index-outside-the-pin"),
        ],
    )
    @pytest.mark.parametrize("gate", ["real-gate", "gate-bypassed"])
    async def test_azure_ai_search_without_a_clean_profile_binding_rejected_before_run(
        self,
        service: ExecutionServiceImpl,
        mock_session_service: MagicMock,
        mock_settings: MagicMock,
        options: dict[str, Any],
        gate: str,
        execution_fixture: ExecutionTestCustody,
    ) -> None:
        """The execution-layer half of the guarantee that replaces the managed-identity refusal.

        With the real ``validate_pipeline`` gate the author gets a structured validation
        error. With the gate stubbed VALID (this module's autouse fixture, standing in
        for a caller that bypassed it) ``execute`` still refuses, because it lowers
        through the operator-profile policy itself. Either way no pipeline runs and no
        provider is ever constructed on the server's identity.
        """
        from elspeth.plugins.infrastructure.manager import get_shared_plugin_manager
        from elspeth.web.config import WebSettings
        from elspeth.web.plugin_policy.availability import build_plugin_snapshot
        from elspeth.web.plugin_policy.compiler import compile_web_plugin_policy
        from elspeth.web.plugin_policy.profiles import OperatorProfileRegistry, RuntimeWebPluginConfig

        class _NoSecrets:
            def has_server_ref(self, name: str) -> bool:
                return False

            def has_user_ref(self, principal: str, name: str) -> bool:
                return False

            def has_ref(self, principal: str, name: str) -> bool:
                return False

            def server_generation(self, name: str) -> str | None:
                return None

            def user_generation(self, principal: str, name: str) -> str | None:
                return None

        runtime = RuntimeWebPluginConfig.from_settings(
            WebSettings.model_validate(
                {
                    "composer_max_composition_turns": 4,
                    "composer_max_discovery_turns": 4,
                    "composer_timeout_seconds": 60,
                    "composer_rate_limit_per_minute": 20,
                    "shareable_link_signing_key": b"0123456789abcdef0123456789abcdef",
                    "plugin_allowlist": ["source:csv", "sink:csv", "transform:azure_ai_search"],
                    "azure_search_profiles": [
                        {
                            "alias": "policies",
                            "endpoint": "https://operator-private-marker.search.windows.net",
                            "auth": "managed_identity",
                            "indexes": ["approved-documents"],
                        }
                    ],
                }
            )
        )
        policy = compile_web_plugin_policy(registry=get_shared_plugin_manager(), settings=runtime)
        profiles = OperatorProfileRegistry(policy=policy, settings=runtime)
        snapshot = build_plugin_snapshot(
            policy=policy,
            catalog=create_catalog_service(),
            profiles=profiles,
            principal_scope="local:alice",
            secret_inventory=_NoSecrets(),
            generation_key=b"azure-search-execution-layer-proof",
        )
        service._plugin_snapshot_factory = lambda _user_id: snapshot
        service._operator_profile_registry = profiles

        mock_settings.data_dir = "/tmp/elspeth_data"
        state = mock_session_service.get_current_state.return_value
        state.source = None
        state.outputs = None
        state.nodes = [
            {
                "id": "rag",
                "node_type": "transform",
                "plugin": "azure_ai_search",
                "input": "transform_in",
                "on_success": "results",
                "on_error": "discard",
                "options": {"query_field": "question", "output_prefix": "policy", "schema": {"mode": "observed"}, **options},
            }
        ]
        state.edges = None

        with (
            patch.object(service, "_run_pipeline") as run_pipeline,
            patch("elspeth.plugins.transforms.azure.ai_search.AzureSearchProvider") as provider_class,
        ):
            if gate == "real-gate":
                with (
                    patch("elspeth.web.execution.validation.validate_pipeline", side_effect=_real_validate_pipeline),
                    pytest.raises(PipelineValidationError) as raised,
                ):
                    await _execute(service, session_id=uuid4(), user_id="alice", execution_fixture=execution_fixture)
                # The mocked state never yields loadable settings, so a bare
                # ``raises`` is satisfied by ANY node. Pin the refusal to the
                # operator-profile gate and to this node.
                assert [(error.error_code, error.component_id) for error in raised.value.errors] == [("plugin_options_invalid", "rag")]
                rendered = " ".join(error.message for error in raised.value.errors)
            else:
                with pytest.raises(RuntimeError, match="Plugin policy validation diverged") as diverged:
                    await _execute(service, session_id=uuid4(), user_id="alice", execution_fixture=execution_fixture)
                rendered = str(diverged.value)

        run_pipeline.assert_not_called()
        provider_class.assert_not_called()
        for private_value in ("tenant-b", "evil.example.com", "operator-private-marker"):
            assert private_value not in rendered

    @pytest.mark.asyncio
    async def test_sequential_multi_query_llm_default_retry_budget_rejected_before_run(
        self,
        service: ExecutionServiceImpl,
        mock_session_service: MagicMock,
        mock_settings: MagicMock,
        execution_fixture: ExecutionTestCustody,
    ) -> None:
        """Web execution must not run LLM configs that inherit the hour-long sequential retry default."""
        mock_settings.data_dir = "/tmp/elspeth_data"
        state = mock_session_service.get_current_state.return_value
        node_id = "llm_review"
        prompt_template = "Classify {{ text }}."
        model = "openai/gpt-4o-mini"
        state.source = None
        state.outputs = None
        options: dict[str, Any] = {
            "provider": "openrouter",
            "model": model,
            "prompt_template": prompt_template,
            "schema": {"mode": "observed"},
            "required_input_fields": [],
            "queries": [{"name": "classify", "input_fields": {"text": "body"}}],
        }
        options[INTERPRETATION_REQUIREMENTS_KEY] = self._resolved_llm_reviews(node_id=node_id, options=options, model=model)
        state.nodes = [
            {
                "id": node_id,
                "node_type": "transform",
                "plugin": "llm",
                "input": "transform_in",
                "on_success": "results",
                "on_error": "discard",
                "options": options,
            }
        ]
        state.edges = None

        with (
            patch.object(service, "_run_pipeline") as run_pipeline,
            pytest.raises(PipelineValidationError, match="sequential multi-query LLM"),
        ):
            await _execute(service, session_id=uuid4(), execution_fixture=execution_fixture)

        run_pipeline.assert_not_called()

    @pytest.mark.asyncio
    async def test_pooled_multi_query_llm_numeric_string_pool_size_reaches_run_submission(
        self,
        service: ExecutionServiceImpl,
        mock_session_service: MagicMock,
        mock_settings: MagicMock,
        execution_fixture: ExecutionTestCustody,
    ) -> None:
        """Exact integer strings accepted by LLMConfig must not be blocked by the retry policy."""
        mock_settings.data_dir = "/tmp/elspeth_data"
        state = mock_session_service.get_current_state.return_value
        node_id = "llm_review"
        prompt_template = "Classify {{ text }}."
        model = "openai/gpt-4o-mini"
        state.source = None
        state.outputs = None
        pooled_options: dict[str, Any] = {
            "provider": "openrouter",
            "model": model,
            "prompt_template": prompt_template,
            "schema": {"mode": "observed"},
            "required_input_fields": [],
            "queries": [{"name": "classify", "input_fields": {"text": "body"}}],
            "pool_size": "2.0",
        }
        pooled_options[INTERPRETATION_REQUIREMENTS_KEY] = self._resolved_llm_reviews(node_id=node_id, options=pooled_options, model=model)
        state.nodes = [
            {
                "id": node_id,
                "node_type": "transform",
                "plugin": "llm",
                "input": "transform_in",
                "on_success": "results",
                "on_error": "discard",
                "options": pooled_options,
            }
        ]
        state.edges = None

        with patch.object(service, "_run_pipeline"):
            run_id = await _execute(service, session_id=uuid4(), execution_fixture=execution_fixture)
        assert isinstance(run_id, UUID)

    @pytest.mark.asyncio
    async def test_non_rag_transform_without_provider_config_passes(
        self,
        service: ExecutionServiceImpl,
        mock_session_service: MagicMock,
        mock_settings: MagicMock,
        execution_fixture: ExecutionTestCustody,
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
            run_id = await _execute(service, session_id=uuid4(), execution_fixture=execution_fixture)
        assert isinstance(run_id, UUID)


# ── Transform Framing Restriction ─────────────────────────────────────


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
        execution_fixture: ExecutionTestCustody,
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
            await _execute(service, session_id=uuid4(), execution_fixture=execution_fixture)

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
        execution_fixture: ExecutionTestCustody,
    ) -> None:
        from elspeth.web.execution.errors import UnresolvedInterpretationPlaceholderError

        mock_settings.data_dir = "/tmp/elspeth_data"
        self._set_structured_pending_interpretation_state(mock_session_service)

        with patch.object(service, "_run_pipeline"), pytest.raises(UnresolvedInterpretationPlaceholderError) as excinfo:
            await _execute(service, session_id=uuid4(), execution_fixture=execution_fixture)

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
        execution_fixture: ExecutionTestCustody,
    ) -> None:
        """elspeth-5a94855935: a resolved invented_source whose content_hash
        drifted after review is rejected with the same contract as any other
        pending interpretation review (typed error -> HTTP 422), NOT a bare
        ValueError that the route layer mis-maps to a 404."""
        from elspeth.web.execution.errors import UnresolvedInterpretationPlaceholderError

        mock_settings.data_dir = "/tmp/elspeth_data"
        self._set_drifted_invented_source_state(mock_session_service)

        with patch.object(service, "_run_pipeline"), pytest.raises(UnresolvedInterpretationPlaceholderError) as excinfo:
            await _execute(service, session_id=uuid4(), execution_fixture=execution_fixture)

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
        execution_fixture: ExecutionTestCustody,
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
            await _execute(service, session_id=uuid4(), execution_fixture=execution_fixture)

        assert [(site.component_id, site.kind.value) for site in excinfo.value.sites] == [("source:orders", "invented_source")]
        mock_session_service.create_run.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_execute_emits_telemetry_per_unresolved_site(
        self,
        service: ExecutionServiceImpl,
        mock_session_service: MagicMock,
        mock_settings: MagicMock,
        execution_fixture: ExecutionTestCustody,
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
            await _execute(service, session_id=uuid4(), execution_fixture=execution_fixture)

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
        execution_fixture: ExecutionTestCustody,
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
                    "approved_prompt_artifact_hash": approved_prompt_artifact_hash_from_options({"prompt_template": prompt}),
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
            run_id = await _execute(service, session_id=session_id, execution_fixture=execution_fixture)

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
        execution_fixture: ExecutionTestCustody,
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
            run_id = await _execute(service, session_id=uuid4(), execution_fixture=execution_fixture)
        assert isinstance(run_id, UUID)

    @pytest.mark.asyncio
    async def test_relative_source_path_resolves_against_data_dir(
        self,
        service: ExecutionServiceImpl,
        mock_session_service: MagicMock,
        mock_settings: MagicMock,
        execution_fixture: ExecutionTestCustody,
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
            run_id = await _execute(service, session_id=session_id, execution_fixture=execution_fixture)
        assert isinstance(run_id, UUID)

    @pytest.mark.asyncio
    async def test_second_named_source_path_outside_allowed_dirs_raises(
        self,
        service: ExecutionServiceImpl,
        mock_session_service: MagicMock,
        mock_settings: MagicMock,
        execution_fixture: ExecutionTestCustody,
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
            await _execute(service, session_id=session_id, execution_fixture=execution_fixture)

    @pytest.mark.asyncio
    async def test_relative_traversal_still_blocked(
        self,
        service: ExecutionServiceImpl,
        mock_session_service: MagicMock,
        mock_settings: MagicMock,
        execution_fixture: ExecutionTestCustody,
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
            await _execute(service, session_id=uuid4(), execution_fixture=execution_fixture)


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
            service._run_pipeline(str(uuid4()), "source:\n  plugin: csv", threading.Event(), session_operation_lease=_execute_lease())

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
            service._run_pipeline(str(uuid4()), _TEST_PIPELINE_YAML, threading.Event(), session_operation_lease=_execute_lease())


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
        self,
        blob_service: BlobServiceProtocol,
        mock_settings: MagicMock,
        mock_session_service: MagicMock,
        execution_fixture: ExecutionTestCustody,
    ) -> ExecutionServiceImpl:
        svc = execution_fixture.bind(
            ExecutionServiceImpl.for_trained_operator(
                loop=MagicMock(spec=asyncio.AbstractEventLoop),
                broadcaster=MagicMock(spec=ProgressBroadcaster),
                settings=mock_settings,
                session_service=mock_session_service,
                yaml_generator=_YamlGeneratorStub(),
                telemetry=build_sessions_telemetry(),
                blob_service=blob_service,
                execution_lease_release_registry=execution_fixture.registry(execution_fixture.loop),
            )
        )
        call_async, loop = _make_strict_call_async()
        self._loops_to_close.append(loop)
        cast(Any, svc)._call_async = call_async
        return svc

    def test_suppresses_blob_not_found_error(
        self, mock_settings: MagicMock, mock_session_service: MagicMock, execution_fixture: ExecutionTestCustody
    ) -> None:
        from elspeth.web.blobs.protocol import BlobNotFoundError

        blob_service = _blob_service_stub()
        blob_service.finalize_run_output_blobs.side_effect = BlobNotFoundError("missing-blob")
        svc = self._make_service_with_blob(blob_service, mock_settings, mock_session_service, execution_fixture=execution_fixture)
        svc._finalize_output_blobs(str(uuid4()), success=True, session_operation_lease=_execute_lease())

    def test_propagates_runtime_error_from_blob_lifecycle(
        self, mock_settings: MagicMock, mock_session_service: MagicMock, execution_fixture: ExecutionTestCustody
    ) -> None:
        """RuntimeError is no longer suppressed — it's too broad and would
        catch Tier 1 anomaly signals.  Blob lifecycle errors should use
        BlobStateError or BlobNotFoundError instead.
        """
        blob_service = _blob_service_stub()
        blob_service.finalize_run_output_blobs.side_effect = RuntimeError("Cannot finalize — status is 'ready', expected 'pending'")
        svc = self._make_service_with_blob(blob_service, mock_settings, mock_session_service, execution_fixture=execution_fixture)
        with pytest.raises(RuntimeError, match="Cannot finalize"):
            svc._finalize_output_blobs(str(uuid4()), success=True, session_operation_lease=_execute_lease())

    def test_suppresses_blob_quota_exceeded_error(
        self, mock_settings: MagicMock, mock_session_service: MagicMock, execution_fixture: ExecutionTestCustody
    ) -> None:
        from elspeth.web.blobs.protocol import BlobQuotaExceededError

        blob_service = _blob_service_stub()
        blob_service.finalize_run_output_blobs.side_effect = BlobQuotaExceededError("sess-1", current_bytes=100, limit_bytes=50)
        svc = self._make_service_with_blob(blob_service, mock_settings, mock_session_service, execution_fixture=execution_fixture)
        svc._finalize_output_blobs(str(uuid4()), success=True, session_operation_lease=_execute_lease())

    def test_propagates_type_error(
        self, mock_settings: MagicMock, mock_session_service: MagicMock, execution_fixture: ExecutionTestCustody
    ) -> None:
        """Programmer bugs (TypeError, AttributeError, etc.) must still crash."""
        blob_service = _blob_service_stub()
        blob_service.finalize_run_output_blobs.side_effect = TypeError("unexpected keyword argument")
        svc = self._make_service_with_blob(blob_service, mock_settings, mock_session_service, execution_fixture=execution_fixture)
        with pytest.raises(TypeError, match="unexpected keyword argument"):
            svc._finalize_output_blobs(str(uuid4()), success=True, session_operation_lease=_execute_lease())

    def test_propagates_attribute_error(
        self, mock_settings: MagicMock, mock_session_service: MagicMock, execution_fixture: ExecutionTestCustody
    ) -> None:
        """AttributeError is a programmer bug — must crash."""
        blob_service = _blob_service_stub()
        blob_service.finalize_run_output_blobs.side_effect = AttributeError("'NoneType' object has no attribute 'id'")
        svc = self._make_service_with_blob(blob_service, mock_settings, mock_session_service, execution_fixture=execution_fixture)
        with pytest.raises(AttributeError):
            svc._finalize_output_blobs(str(uuid4()), success=True, session_operation_lease=_execute_lease())


# ── Terminal Ordering Invariant ───────────────────────────────────────


def _collect_terminal_types(mock_broadcaster: MagicMock) -> list[str]:
    """Extract terminal event types from a mock broadcaster's call log."""
    terminals = []
    for call in mock_broadcaster.broadcast.call_args_list:
        _, event = call[0]
        if event.event_type in ("completed", "failed", "cancelled"):
            terminals.append(event.event_type)
    return terminals


# ── Session Lock Cleanup ──────────────────────────────────────────────


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

        exc = RuntimeError("internal traceback details here /opt/project/elspeth/src")
        result = _sanitize_error_for_client(exc)
        assert "/opt/project" not in result
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


def test_structural_failure_message_names_group_count_without_row_claim() -> None:
    from elspeth.web.execution.service import _structural_failure_message

    msg = _structural_failure_message(rows_processed=1, collector_groups_failed=1)

    assert "collector_groups_failed=1" in msg
    assert "All rows either failed terminally" not in msg
    assert "failure accounting" in msg


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
            collector_groups_failed=1,
        )
        assert "rows_succeeded=7" in msg
        assert "rows_failed=3" in msg
        assert "rows_routed_failure=1" in msg
        assert "rows_quarantined=2" in msg
        assert "collector_groups_failed=1" in msg

    def test_structural_only_partial_failure_names_group_count(self) -> None:
        from elspeth.web.execution.service import _partial_completion_message

        msg = _partial_completion_message(
            rows_succeeded=1,
            rows_failed=0,
            rows_routed_failure=0,
            rows_quarantined=0,
            collector_groups_failed=1,
        )
        assert "collector_groups_failed=1" in msg
        assert "rows_failed=0" in msg

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
            coordination_token=leader_coordination_token(factory, run.run_id),
            plugin_name="test_source",
            node_type=NodeType.SOURCE,
            plugin_version="1.0",
            config={},
            schema_config=dynamic_schema,
            node_id="source_test",
            sequence=0,
        )
        factory.data_flow.register_node(
            coordination_token=leader_coordination_token(factory, run.run_id),
            plugin_name="web_scrape",
            node_type=NodeType.TRANSFORM,
            plugin_version="1.0",
            config={},
            schema_config=dynamic_schema,
            node_id=transform_id,
            sequence=1,
        )
        for index in range(rows):
            _row, token = factory.data_flow.create_row_with_token(
                coordination_token=leader_coordination_token(factory, run.run_id),
                token_id=f"canary_tok_{index}",
                source_node_id="source_test",
                row_index=index,
                data={"url": f"row-{index}"},
                source_row_index=index,
                ingest_sequence=index,
            )
            member = leader_coordination_token(factory, run.run_id).membership
            work_item = claim_test_work_item(factory, member_token=member, token_id=token.token_id, node_id=transform_id)
            factory.data_flow.record_transform_error(
                member_token=member,
                work_item=work_item,
                ref=TokenRef(token_id=token.token_id, run_id=run.run_id),
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
            # The row's terminal discard: the failure summary counts tokens
            # whose terminal outcome a transform error decided.
            factory.data_flow.record_token_outcome_leader(
                coordination_token=leader_coordination_token(factory, run.run_id),
                ref=TokenRef(token_id=token.token_id, run_id=run.run_id),
                outcome=TerminalOutcome.FAILURE,
                path=TerminalPath.QUARANTINED_AT_SOURCE,
                error_hash="a" * 16,
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
            service._run_pipeline(run_id, _TEST_PIPELINE_YAML, threading.Event(), session_operation_lease=_execute_lease())

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


# ---------------------------------------------------------------------------
# Collector arm of the fanout marker dispatch (elspeth-df8082552d, site b2).
#
# ``_fanout_marker_for_node`` had arms for transform, aggregation and gate and
# NONE for collector, so a collector never contributed a fanout marker — the
# same plugin marked on an aggregation went unmarked on a collector, and a
# collector that genuinely multiplies rows into an LLM node was invisible to
# the guard.
#
# The gap was previously assessed as masked, because a collector's mandatory
# ``scope_opener`` is a multi-row transform the trace marks anyway. That mask
# is not load-bearing here: these tests assert the COLLECTOR's own marker is
# present, not merely that some guard fires. (The mask itself is unsound for a
# separate reason — nothing enforces that an opener creates tokens; see
# elspeth-da3032d197.)
# ---------------------------------------------------------------------------


def _collector_marker_node(node_type: str, plugin: str = "batch_replicate") -> Any:
    """One node hosting ``plugin``, shaped for ``node_type``."""
    from elspeth.web.composer.state import NodeSpec

    kind_fields: dict[str, Any] = {}
    if node_type == "aggregation":
        kind_fields = {"trigger": {"count": 5}, "output_mode": "transform"}
    elif node_type == "collector":
        kind_fields = {"scope_name": "s", "scope_opener": "op", "scope_policy": "require_all"}
    return NodeSpec(
        id="c1",
        node_type=node_type,
        plugin=plugin,
        input="op_out",
        on_success="c1_out",
        on_error="errors",
        options={"copies": 3},
        condition=None,
        routes=None,
        fork_to=None,
        branches=None,
        policy=None,
        merge=None,
        **kind_fields,
    )


def _llm_inline_marker_pipeline_yaml(field_path: str, *, blob_id: UUID, sha256: str) -> str:
    """An ``llm`` transform whose ``field_path`` option is an inline_content marker (JSON is YAML)."""
    marker = {"blob_ref": str(blob_id), "mode": "inline_content", "sha256": sha256}
    options: dict[str, Any] = {"model": "test-model", "prompt_template": "Classify {{ row.text }}"}
    if field_path == "queries.q1.template":
        options["queries"] = {"q1": {"template": marker}}
    else:
        options[field_path] = marker
    config = {
        "source": {"plugin": "csv", "options": {"path": "input.csv"}},
        "transforms": [{"name": "classify", "plugin": "llm", "options": options}],
        "sinks": {"primary": {"plugin": "json", "options": {"path": "output.jsonl"}}},
    }
    return json.dumps(config)


@pytest.mark.usefixtures("mock_pipeline_config_assembly")
class TestInlineBlobPromptSurfaceModalityAdmission:
    """An LLM-authored blob must not become an executed LLM prompt or model by substitution.

    The prompt-template and model-choice reviews read those options as strings,
    so an inline_content marker there opens no review site. Run admission is
    the gate that holds for every authoring path: it refuses an LLM-authored
    blob in an ``llm`` transform's prompt surfaces before linking or reading
    it, as the ``blob_rows`` arm refuses LLM-authored rows.
    """

    @pytest.mark.parametrize(
        "modality",
        [
            pytest.param(CreationModality.LLM_GENERATED, id="llm_generated"),
            pytest.param(CreationModality.LLM_GENERATED_THEN_AMENDED, id="llm_generated_then_amended"),
        ],
    )
    @pytest.mark.parametrize("field_path", ["prompt_template", "system_prompt", "queries.q1.template", "model"])
    @patch("elspeth.web.execution.service.Orchestrator")
    @patch("elspeth.web.execution.service.build_validated_runtime_graph")
    @patch("elspeth.web.execution.service.load_settings_from_config_dict")
    @patch("elspeth.web.execution.service.open_landscape_db")
    @patch("elspeth.web.execution.service.FilesystemPayloadStore")
    def test_llm_authored_blob_in_llm_prompt_surface_is_refused_before_link(
        self,
        mock_payload_cls: MagicMock,
        mock_landscape_cls: MagicMock,
        mock_load: MagicMock,
        mock_runtime_graph: MagicMock,
        mock_orch_cls: MagicMock,
        service: ExecutionServiceImpl,
        mock_session_service: MagicMock,
        field_path: str,
        modality: CreationModality,
    ) -> None:
        del mock_landscape_cls, mock_runtime_graph
        content = b"You are a planner-written prompt."
        blob_id = uuid4()
        owner_session = uuid4()
        sha256 = hashlib.sha256(content).hexdigest()
        mock_session_service.get_run.return_value = _run_record_stub(status="running", session_id=owner_session)
        blob_service = _blob_service_stub()
        blob_service.get_blob.return_value = _blob_record_stub(
            blob_id=blob_id,
            session_id=owner_session,
            content_hash=sha256,
            size_bytes=len(content),
            creation_modality=modality,
        )
        blob_service.read_blob_content.return_value = content
        blob_service.finalize_run_output_blobs.return_value = BlobFinalizationResult(finalized=(), errors=())
        cast(Any, service)._blob_service = blob_service

        with pytest.raises(InlineBlobPromptSurfaceAdmissionError, match="LLM-authored") as caught:
            service._run_pipeline(
                str(uuid4()),
                _llm_inline_marker_pipeline_yaml(field_path, blob_id=blob_id, sha256=sha256),
                threading.Event(),
                session_operation_lease=_execute_lease(),
            )

        assert f"node:classify.options.{field_path}" in str(caught.value)
        blob_service.link_blob_to_run.assert_not_awaited()
        blob_service.read_blob_content.assert_not_awaited()
        mock_session_service.record_blob_inline_resolutions.assert_not_awaited()
        mock_load.assert_not_called()
        mock_orch_cls.assert_not_called()
        mock_payload_cls.return_value.store.assert_not_called()

    @pytest.mark.parametrize("field_path", ["prompt_template", "queries.q1.template", "model"])
    @patch("elspeth.web.execution.service.Orchestrator")
    @patch("elspeth.web.execution.service.build_validated_runtime_graph")
    @patch("elspeth.web.execution.service.load_settings_from_config_dict")
    @patch("elspeth.web.execution.service.open_landscape_db")
    @patch("elspeth.web.execution.service.FilesystemPayloadStore")
    def test_verbatim_blob_in_llm_prompt_surface_passes_modality_admission(
        self,
        mock_payload_cls: MagicMock,
        mock_landscape_cls: MagicMock,
        mock_load: MagicMock,
        mock_runtime_graph: MagicMock,
        mock_orch_cls: MagicMock,
        service: ExecutionServiceImpl,
        mock_session_service: MagicMock,
        field_path: str,
    ) -> None:
        """Control: user-verbatim content is linked, read and substituted as before.

        The audit write is made to fail so the run stops right after
        substitution; reaching it proves the modality gate admitted the ref.
        """
        del mock_payload_cls, mock_landscape_cls, mock_runtime_graph
        content = b"You are a user-written prompt."
        blob_id = uuid4()
        owner_session = uuid4()
        sha256 = hashlib.sha256(content).hexdigest()
        mock_session_service.get_run.return_value = _run_record_stub(status="running", session_id=owner_session)
        blob_service = _blob_service_stub()
        blob_service.get_blob.return_value = _blob_record_stub(
            blob_id=blob_id,
            session_id=owner_session,
            content_hash=sha256,
            size_bytes=len(content),
            creation_modality=CreationModality.VERBATIM,
        )
        blob_service.link_blob_to_run.return_value = None
        blob_service.read_blob_content.return_value = content
        blob_service.finalize_run_output_blobs.return_value = BlobFinalizationResult(finalized=(), errors=())
        cast(Any, service)._blob_service = blob_service
        mock_session_service.record_blob_inline_resolutions.side_effect = AuditIntegrityError("stop after substitution")

        with pytest.raises(AuditIntegrityError, match="stop after substitution"):
            service._run_pipeline(
                str(uuid4()),
                _llm_inline_marker_pipeline_yaml(field_path, blob_id=blob_id, sha256=sha256),
                threading.Event(),
                session_operation_lease=_execute_lease(),
            )

        blob_service.link_blob_to_run.assert_awaited_once()
        blob_service.read_blob_content.assert_awaited_once()
        resolutions = mock_session_service.record_blob_inline_resolutions.await_args.kwargs["resolutions"]
        assert [resolution.field_path for resolution in resolutions] == [f"node:classify.options.{field_path}"]
        mock_load.assert_not_called()
        mock_orch_cls.assert_not_called()


def test_per_call_quota_admission_persists_pending_under_transferred_lease(
    service: ExecutionServiceImpl, mock_session_service: MagicMock, real_loop: asyncio.AbstractEventLoop, monkeypatch: pytest.MonkeyPatch
) -> None:
    del real_loop
    monkeypatch.setattr(
        service, "_call_async", create_autospec(service._call_async, side_effect=AssertionError("run admission entered async bridge"))
    )
    run_uuid = uuid4()
    attempt = ProviderAttempt(attempt_id="provider-attempt", started_at=datetime.now(UTC))
    mock_session_service.begin_run_provider_attempt_sync.return_value = attempt
    assert service._admit_run_llm_call(run_uuid, _execute_lease()) == attempt.attempt_id
    mock_session_service.begin_run_provider_attempt_sync.assert_called_once_with(
        session_operation_context=_execute_lease().context, run_id=run_uuid
    )
    service._call_async.assert_not_called()


def test_per_call_quota_refusal_propagates_before_dispatch(
    service: ExecutionServiceImpl, mock_session_service: MagicMock, real_loop: asyncio.AbstractEventLoop, monkeypatch: pytest.MonkeyPatch
) -> None:
    del real_loop
    monkeypatch.setattr(
        service, "_call_async", create_autospec(service._call_async, side_effect=AssertionError("run admission entered async bridge"))
    )
    refusal = ChargeableAdmissionRefused(
        ChargeableAdmissionDecision(
            refusal_reason=AdmissionRefusalReason.TOKEN_ACCOUNTING_UNAVAILABLE,
            evidence=AdmissionPolicyEvidence(
                quota_disposition=QuotaDisposition.ACCOUNTING_UNAVAILABLE, identity_policy_id="policy", secret_wiring_hash="a" * 64
            ),
        )
    )
    mock_session_service.begin_run_provider_attempt_sync.side_effect = refusal
    with pytest.raises(ChargeableAdmissionRefused) as caught:
        service._admit_run_llm_call(uuid4(), _execute_lease())
    assert caught.value is refusal
    mock_session_service.settle_run_provider_attempt_sync.assert_not_called()
    service._call_async.assert_not_called()


def test_per_call_settlement_uses_exact_durable_call_identity(
    service: ExecutionServiceImpl, mock_session_service: MagicMock, real_loop: asyncio.AbstractEventLoop, monkeypatch: pytest.MonkeyPatch
) -> None:
    del real_loop
    monkeypatch.setattr(
        service, "_call_async", create_autospec(service._call_async, side_effect=AssertionError("run settlement entered async bridge"))
    )
    entry = TokenUsageEntry(
        model="model",
        prompt_tokens=3,
        completion_tokens=2,
        cached_prompt_tokens=None,
        reasoning_tokens=None,
        call_id="actual-call",
        recorded_at=datetime.now(UTC),
    )
    db = make_landscape_db()
    with patch("elspeth.web.execution.service._run_token_usage_entries", return_value=(entry,)):
        service._settle_run_llm_call(uuid4(), _execute_lease(), "pending-attempt", "actual-call", landscape_db=db, landscape_run_id="run")
        with pytest.raises(AuditIntegrityError, match="exactly one durable LLM call"):
            service._settle_run_llm_call(
                uuid4(), _execute_lease(), "pending-attempt", "unknown-call", landscape_db=db, landscape_run_id="run"
            )
    mock_session_service.settle_run_provider_attempt_sync.assert_called_once_with(
        session_operation_context=_execute_lease().context, attempt_id="pending-attempt", entry=entry
    )
    service._call_async.assert_not_called()
