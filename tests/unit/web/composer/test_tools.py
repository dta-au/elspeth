"""Tests for composition tools — discovery delegation and mutation + validation."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any, ClassVar, cast
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.pool import StaticPool

from elspeth.contracts.freeze import deep_thaw
from elspeth.contracts.hashing import stable_hash
from elspeth.contracts.session_operation import SessionOperationContext, SessionOperationKind
from elspeth.web.catalog.policy_view import PolicyCatalogView
from elspeth.web.catalog.protocol import CatalogService
from elspeth.web.catalog.schemas import (
    ConfigFieldSummary,
    PluginSchemaInfo,
    PluginSummary,
)
from elspeth.web.composer.state import (
    CompositionState,
    NodeSpec,
    OutputSpec,
    PipelineMetadata,
    SourceSpec,
    ValidationEntry,
    ValidationSummary,
)
from elspeth.web.composer.tools import (
    ToolContext,
    ToolResult,
    _compute_validation_delta,
    _failure_result,
    _inject_prior_validation,
    _prevalidate_plugin_options,
    get_tool_definitions,
)
from elspeth.web.composer.tools import (
    execute_tool as _execute_tool,
)
from elspeth.web.composer.tools._common import rejected_component_prefix
from elspeth.web.config import WebSettings
from elspeth.web.coordination.sqlite_authority import SQLiteLocalSessionOperationAuthority
from elspeth.web.credential_guard import CredentialMaterialRefused
from elspeth.web.dependencies import create_catalog_service
from elspeth.web.execution.schemas import (
    ValidationCheck,
    ValidationError,
    ValidationReadiness,
    ValidationReadinessBlocker,
    ValidationResult,
)
from elspeth.web.interpretation_state import (
    INTERPRETATION_REQUIREMENTS_KEY,
    approved_prompt_artifact_hash_from_options,
)
from elspeth.web.plugin_policy.models import (
    PluginAvailability,
    PluginAvailabilitySnapshot,
    PluginId,
    PluginUnavailableReason,
    WebPluginPolicy,
)
from elspeth.web.plugin_policy.profiles import OperatorProfileRegistry, RuntimeWebPluginConfig
from elspeth.web.provider_config_policy import AWS_S3_ENDPOINT_URL_POLICY_ERROR
from elspeth.web.secrets.wiring_policy import (
    SecretWiringComponentType,
    SecretWiringPolicy,
    SecretWiringRule,
)
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.models import blobs_table, chat_messages_table, sessions_table
from elspeth.web.sessions.schema import initialize_session_schema
from tests.fixtures.identities import ensure_test_identity
from tests.helpers.session_fences import fenced_operation_context


def _attached_notes(exc: BaseException) -> tuple[str, ...]:
    """Notes ``BaseException.add_note`` attached to ``exc``; empty when none were.

    ``add_note`` stores ``__notes__`` in the exception's instance dictionary and
    CPython creates it lazily, so "no note was attached" is genuinely an absent
    key rather than an empty value. Reading the instance dict states that
    directly — this is dict inspection, not an attribute probe, and it is used
    only where the assertion is that NO note exists. Where a note is REQUIRED,
    read ``exc.__notes__`` directly so a missing one raises.
    """
    return tuple(vars(exc).get("__notes__", ()))


# Stub SHA-256 hex digest for test fixtures.  Must satisfy the
# ``ck_blobs_ready_hash`` invariant — exactly 64 lowercase hex
# characters — even when the surrounding test does not actually verify
# the hash.  Using a structurally valid placeholder keeps the fixtures
# from accidentally exercising the malformed-hash bypass path the
# database CHECK was added to close.
_STUB_SHA256 = "a" * 64
_STUB_SHA256_ALT = "b" * 64
EXPECTED_REDACTED_BLOB_SOURCE_PATH = "<redacted-blob-source-path>"


class _CallRecord:
    def __init__(self, args: tuple[object, ...], kwargs: dict[str, object]) -> None:
        self.args = args
        self.kwargs = kwargs


class _SyncCallRecorder:
    def __init__(self, return_value: object = None) -> None:
        self.return_value = return_value
        self.side_effect: Callable[..., object] | BaseException | None = None
        self.call_args: _CallRecord | None = None
        self.call_args_list: list[_CallRecord] = []

    def __call__(self, *args: object, **kwargs: object) -> object:
        record = _CallRecord(args, kwargs)
        self.call_args = record
        self.call_args_list.append(record)
        if isinstance(self.side_effect, BaseException):
            raise self.side_effect
        if self.side_effect is not None:
            return self.side_effect(*args, **kwargs)
        return self.return_value

    def assert_called_once_with(self, *args: object, **kwargs: object) -> None:
        assert len(self.call_args_list) == 1
        record = self.call_args_list[0]
        assert record.args == args
        assert record.kwargs == kwargs

    def assert_called_once(self) -> None:
        assert len(self.call_args_list) == 1


class _SecretServiceDouble:
    def __init__(self) -> None:
        self.list_refs = _SyncCallRecorder([])
        self.has_ref = _SyncCallRecorder(False)


@pytest.fixture
def passing_runtime_preflight() -> _SyncCallRecorder:
    """A wired Stage-2 runtime preflight that passes.

    ``preview_pipeline`` fails closed when no preflight callback is wired
    (an un-run stage may not ride the success side), so a test that wants
    a preview to publish ``preview_is_valid: True`` must wire one. Deliberately
    NOT autouse: a blanket always-passing stub would hide exactly the
    un-run-stage state this guard exists to expose.
    """
    return _SyncCallRecorder(
        ValidationResult(
            is_valid=True,
            checks=[ValidationCheck(name="settings_load", passed=True, detail="Settings loaded.", affected_nodes=(), outcome_code=None)],
            errors=[],
            readiness=ValidationReadiness(authoring_valid=True, execution_ready=True, completion_ready=True, blockers=[]),
        )
    )


def _empty_state() -> CompositionState:
    return CompositionState(
        source=None,
        nodes=(),
        edges=(),
        outputs=(),
        metadata=PipelineMetadata(),
        version=1,
    )


_AWS_S3_ENDPOINT_SENTINEL = "https://composer-canary.attacker.invalid/private"
_VALID_AWS_S3_OPTIONS: Mapping[str, Any] = {
    "bucket": "test-bucket",
    "key": "test-key",
    "schema": {"mode": "observed"},
}
_VALID_AWS_S3_PROFILE_OPTIONS: Mapping[str, Any] = {
    "profile": "demo-input",
    "key": "records/input.csv",
    "format": "csv",
    "schema": {"mode": "observed"},
}


def _aws_s3_source_state() -> CompositionState:
    return CompositionState(
        source=SourceSpec(
            plugin="aws_s3",
            on_success="main",
            options={},
            on_validation_failure="discard",
        ),
        nodes=(),
        edges=(),
        outputs=(),
        metadata=PipelineMetadata(),
        version=7,
    )


def _aws_s3_output_state() -> CompositionState:
    return CompositionState(
        source=None,
        nodes=(),
        edges=(),
        outputs=(
            OutputSpec(
                name="main",
                plugin="aws_s3",
                options={},
                on_write_failure="discard",
            ),
        ),
        metadata=PipelineMetadata(),
        version=7,
    )


def _assert_aws_s3_endpoint_url_rejected(
    result: ToolResult,
    original_state: CompositionState,
    *,
    forbidden_value: str = _AWS_S3_ENDPOINT_SENTINEL,
    rejected_component: str | None = None,
) -> None:
    assert result.success is False
    assert result.updated_state is original_state
    assert result.updated_state.version == original_state.version
    assert result.data is None
    # A set_pipeline component rejection names its subject both structurally
    # and as the minted message prefix; an incremental tool's rejection is
    # about the component it was called with and carries neither.
    expected_prefix = "" if rejected_component is None else rejected_component_prefix(rejected_component)
    assert result.validation.errors[0].message == f"{expected_prefix}{AWS_S3_ENDPOINT_URL_POLICY_ERROR}"
    assert result.validation.errors[0].rejected_component == rejected_component
    assert forbidden_value not in repr(result.data)
    assert forbidden_value not in repr(result.validation)


def _default_source(state: CompositionState) -> SourceSpec | None:
    return state.sources.get("source")


def _pipeline_state_default_source(data: Mapping[str, Any]) -> Mapping[str, Any] | None:
    sources = data["sources"]
    if not isinstance(sources, Mapping):
        raise AssertionError("get_pipeline_state data['sources'] must be a mapping")
    source = sources.get("source")
    if source is not None and not isinstance(source, Mapping):
        raise AssertionError("get_pipeline_state data['sources']['source'] must be a mapping")
    return source


def _mock_catalog() -> MagicMock:
    """Mock CatalogService with real PluginSummary/PluginSchemaInfo instances.

    AC #16: Tests must use real PluginSummary and PluginSchemaInfo instances,
    not plain dicts. Mock return types must match the CatalogService protocol.
    """
    catalog = MagicMock(spec=CatalogService)
    catalog.list_sources.return_value = [
        PluginSummary(
            name="csv",
            description="CSV file source",
            plugin_type="source",
            config_fields=[
                ConfigFieldSummary(name="path", type="string", required=True, description="File path", default=None),
            ],
        ),
        PluginSummary(
            name="text",
            description="Text line source",
            plugin_type="source",
            config_fields=[],
        ),
        PluginSummary(
            name="json",
            description="JSON file source",
            plugin_type="source",
            config_fields=[],
        ),
        PluginSummary(name="aws_s3", description="S3 source", plugin_type="source", config_fields=[]),
    ]
    catalog.list_transforms.return_value = [
        PluginSummary(
            name="passthrough",
            description="Uppercase transform",
            plugin_type="transform",
            config_fields=[],
        ),
        PluginSummary(name="field_mapper", description="Field mapper", plugin_type="transform", config_fields=[]),
        PluginSummary(name="llm", description="LLM transform", plugin_type="transform", config_fields=[]),
        PluginSummary(name="web_scrape", description="Web scrape", plugin_type="transform", config_fields=[]),
        PluginSummary(name="blob_fetch", description="Blob fetch", plugin_type="transform", config_fields=[]),
        PluginSummary(name="batch_replicate", description="Batch replicate", plugin_type="transform", config_fields=[]),
        PluginSummary(name="batch_stats", description="Batch stats", plugin_type="transform", config_fields=[]),
        PluginSummary(
            name="batch_distribution_profile",
            description="Batch distribution profile",
            plugin_type="transform",
            config_fields=[],
        ),
        PluginSummary(name="line_explode", description="Line explode", plugin_type="transform", config_fields=[]),
        PluginSummary(name="rag_retrieval", description="RAG retrieval", plugin_type="transform", config_fields=[]),
        PluginSummary(name="value_transform", description="Value transform", plugin_type="transform", config_fields=[]),
        PluginSummary(
            name="azure_prompt_shield",
            description="Prompt injection shield",
            plugin_type="transform",
            config_fields=[],
        ),
    ]
    catalog.list_sinks.return_value = [
        PluginSummary(
            name="csv",
            description="CSV file sink",
            plugin_type="sink",
            config_fields=[],
        ),
        PluginSummary(name="json", description="JSON sink", plugin_type="sink", config_fields=[]),
        PluginSummary(name="text", description="Text sink", plugin_type="sink", config_fields=[]),
        PluginSummary(name="aws_s3", description="S3 sink", plugin_type="sink", config_fields=[]),
        PluginSummary(name="database", description="Database sink", plugin_type="sink", config_fields=[]),
    ]
    catalog.get_schema.return_value = PluginSchemaInfo(
        name="csv",
        plugin_type="source",
        description="CSV file source",
        json_schema={"title": "CsvSourceConfig", "properties": {"path": {"type": "string"}}},
        knob_schema={"fields": []},
    )
    return catalog


def _wiring_policy(
    secret: str,
    component_type: str,
    plugin: str,
    option_key: str,
) -> SecretWiringPolicy:
    """Server-authored allowlist authorizing exactly one wiring (elspeth-f3c1aafd25)."""
    return SecretWiringPolicy(
        rules=(
            SecretWiringRule(
                secret=secret,
                component_type=cast("SecretWiringComponentType", component_type),
                plugin=plugin,
                option_key=option_key,
            ),
        )
    )


def execute_tool(
    tool_name: str,
    arguments: dict[str, Any],
    state: CompositionState,
    catalog: CatalogService,
    **kwargs: Any,
) -> ToolResult:
    """Invoke the strict dispatcher through an explicit test trust boundary."""
    if kwargs.get("data_dir") is not None and "session_id" not in kwargs:
        kwargs["session_id"] = "test-session"
    supplied_snapshot = kwargs.pop("plugin_snapshot", None)
    if isinstance(catalog, PolicyCatalogView):
        if not isinstance(supplied_snapshot, PluginAvailabilitySnapshot):
            raise AssertionError("policy catalog tests must supply their exact snapshot")
        policy_catalog = catalog
        snapshot = supplied_snapshot
    else:
        if supplied_snapshot is not None:
            raise AssertionError("a snapshot requires its matching policy catalog")
        snapshot = PluginAvailabilitySnapshot.for_trained_operator(catalog)
        policy_catalog = PolicyCatalogView.for_trained_operator(catalog, snapshot)
    return _execute_tool(
        tool_name,
        arguments,
        state,
        policy_catalog,
        plugin_snapshot=snapshot,
        **kwargs,
    )


def _trained_tool_context(catalog: CatalogService | None = None, **kwargs: Any) -> Any:
    full_catalog = catalog or _mock_catalog()
    snapshot = PluginAvailabilitySnapshot.for_trained_operator(full_catalog)
    return ToolContext(
        catalog=PolicyCatalogView.for_trained_operator(full_catalog, snapshot),
        plugin_snapshot=snapshot,
        **kwargs,
    )


def _web_authored_tool_context(catalog: CatalogService | None = None, **kwargs: Any) -> Any:
    """Non-trained (web-authored) ToolContext, for handlers invoked directly
    (bypassing the ``execute_tool`` test shim's trained-operator default)."""
    full_catalog = catalog or _mock_catalog()
    view, snapshot = _web_authored_policy_pair(full_catalog)
    return ToolContext(
        catalog=view,
        plugin_snapshot=snapshot,
        **kwargs,
    )


def _web_authored_policy_pair(
    catalog: CatalogService,
) -> tuple[PolicyCatalogView, PluginAvailabilitySnapshot]:
    """Non-trained (web-authored) snapshot with full catalog availability.

    Unlike ``_restricted_policy_pair`` (which additionally hides one
    plugin), this mirrors the plugin_snapshot shape a real web/LLM
    composer session carries: ``PluginSnapshotAuthority.RESTRICTED`` so
    ``is_trained_operator`` is False, but nothing removed from
    ``available`` — isolating the web-authored-policy gate under test
    from the separate plugin-visibility gate.
    """
    unrestricted = PluginAvailabilitySnapshot.for_trained_operator(catalog)
    snapshot = PluginAvailabilitySnapshot.create(
        policy_hash="web-authored-test-policy",
        principal_scope="local:test-user",
        available=unrestricted.available,
        unavailable=(),
        selected=unrestricted.selected,
        usable_profile_aliases=(),
        selected_profile_aliases=(),
        binding_generation_fingerprint="web-authored-test-generation",
    )
    return PolicyCatalogView(catalog, snapshot, MagicMock(spec=OperatorProfileRegistry)), snapshot


def _profiled_s3_policy_pair() -> tuple[PolicyCatalogView, PluginAvailabilitySnapshot]:
    catalog = create_catalog_service()
    source_id = PluginId("source", "aws_s3")
    settings = WebSettings.model_validate(
        {
            "composer_max_composition_turns": 4,
            "composer_max_discovery_turns": 4,
            "composer_timeout_seconds": 60,
            "composer_rate_limit_per_minute": 20,
            "shareable_link_signing_key": b"0123456789abcdef0123456789abcdef",
            "deployment_aws_region": "ap-southeast-1",
            "aws_s3_source_profiles": ({"alias": "demo-input", "bucket": "elspeth-demo-input", "prefix": "incoming"},),
        }
    )
    runtime = RuntimeWebPluginConfig.from_settings(settings)
    policy = WebPluginPolicy.create(
        required=frozenset({source_id}),
        configured_optional=frozenset(),
        preferences=(),
        control_modes=(),
        plugin_code_identities=((source_id, "1.0.0", "profiled-s3-test"),),
    )
    profiles = OperatorProfileRegistry(policy=policy, settings=runtime)
    trained = PluginAvailabilitySnapshot.for_trained_operator(catalog)
    snapshot = PluginAvailabilitySnapshot.create(
        policy_hash="profiled-s3-test-policy",
        principal_scope="local:test-user",
        available=trained.available,
        unavailable=(),
        selected=trained.selected,
        usable_profile_aliases=((source_id, ("demo-input",)),),
        selected_profile_aliases=((source_id, "demo-input"),),
        binding_generation_fingerprint="profiled-s3-test-generation",
    )
    return PolicyCatalogView(catalog, snapshot, profiles), snapshot


def _restricted_policy_pair(
    catalog: CatalogService,
    plugin_id: PluginId,
    reason: PluginUnavailableReason = PluginUnavailableReason.NOT_AUTHORIZED,
) -> tuple[PolicyCatalogView, PluginAvailabilitySnapshot]:
    unrestricted = PluginAvailabilitySnapshot.for_trained_operator(catalog)
    snapshot = PluginAvailabilitySnapshot.create(
        policy_hash="restricted-test-policy",
        principal_scope="local:test-user",
        available=unrestricted.available - {plugin_id},
        unavailable=(PluginAvailability(plugin_id, reason),),
        selected=unrestricted.selected,
        usable_profile_aliases=(),
        selected_profile_aliases=(),
        binding_generation_fingerprint="restricted-test-generation",
    )
    return PolicyCatalogView(catalog, snapshot, MagicMock(spec=OperatorProfileRegistry)), snapshot


def test_direct_upsert_cannot_name_hidden_registered_plugin() -> None:
    """A direct dispatcher call cannot bypass the request policy view."""
    catalog = _mock_catalog()
    policy_catalog, snapshot = _restricted_policy_pair(catalog, PluginId("transform", "azure_prompt_shield"))

    result = execute_tool(
        "upsert_node",
        {
            "id": "shield",
            "node_type": "transform",
            "plugin": "azure_prompt_shield",
            "input": "main",
            "on_success": "clean",
            "options": {"schema": {"mode": "observed"}},
        },
        _empty_state(),
        policy_catalog,
        plugin_snapshot=snapshot,
    )

    assert result.success is False
    assert result.validation.errors[0].error_code == "plugin_not_enabled"
    assert result.updated_state.version == 1


@pytest.mark.parametrize(
    ("tool_name", "arguments", "plugin_id"),
    [
        (
            "set_source",
            {"plugin": "aws_s3", "on_success": "main", "options": {}, "on_validation_failure": "discard"},
            PluginId("source", "aws_s3"),
        ),
        (
            "upsert_node",
            {
                "id": "aggregate",
                "node_type": "aggregation",
                "plugin": "batch_stats",
                "input": "rows",
                "on_success": "main",
                "options": {},
                "trigger": {"count": 10},
            },
            PluginId("transform", "batch_stats"),
        ),
        (
            "set_output",
            {"sink_name": "main", "plugin": "database", "options": {}, "on_write_failure": "discard"},
            PluginId("sink", "database"),
        ),
        (
            "set_pipeline",
            {
                "source": {
                    "plugin": "aws_s3",
                    "on_success": "main",
                    "options": {},
                    "on_validation_failure": "discard",
                },
                "nodes": [],
                "edges": [],
                "outputs": [],
            },
            PluginId("source", "aws_s3"),
        ),
    ],
)
def test_canonical_mutations_reject_hidden_registered_plugins(
    tool_name: str,
    arguments: dict[str, Any],
    plugin_id: PluginId,
) -> None:
    catalog = _mock_catalog()
    view, snapshot = _restricted_policy_pair(catalog, plugin_id)

    result = execute_tool(tool_name, arguments, _empty_state(), view, plugin_snapshot=snapshot)

    assert result.success is False
    assert result.validation.errors[0].error_code == "plugin_not_enabled"
    catalog.post_call_hints.assert_not_called()


def test_historical_hidden_component_can_be_removed_or_replaced() -> None:
    catalog = _mock_catalog()
    hidden_id = PluginId("transform", "azure_prompt_shield")
    view, snapshot = _restricted_policy_pair(catalog, hidden_id)
    state = CompositionState(
        source=None,
        nodes=(
            NodeSpec(
                id="shield",
                node_type="transform",
                plugin="azure_prompt_shield",
                input="rows",
                on_success="main",
                on_error="discard",
                options={},
                condition=None,
                routes=None,
                fork_to=None,
                branches=None,
                policy=None,
                merge=None,
            ),
        ),
        edges=(),
        outputs=(),
        metadata=PipelineMetadata(),
        version=1,
    )

    removed = execute_tool("remove_node", {"id": "shield"}, state, view, plugin_snapshot=snapshot)
    replaced = execute_tool(
        "upsert_node",
        {
            "id": "shield",
            "node_type": "transform",
            "plugin": "passthrough",
            "input": "rows",
            "on_success": "main",
            "on_error": "discard",
            "options": {"schema": {"mode": "observed"}},
        },
        state,
        view,
        plugin_snapshot=snapshot,
    )

    assert removed.success is True
    assert removed.updated_state.nodes == ()
    assert replaced.success is True
    assert replaced.updated_state.nodes[0].plugin == "passthrough"


def _session_engine_with_session() -> tuple[Any, str]:
    """Create a session DB with one session row for blob-tool tests."""
    from datetime import UTC, datetime

    engine = create_session_engine(
        "sqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    initialize_session_schema(engine)
    with engine.begin() as conn:
        ensure_test_identity(conn, identity_id="test-user")
    session_id = str(uuid4())
    now = datetime.now(UTC)
    with engine.begin() as conn:
        conn.execute(
            sessions_table.insert().values(
                id=session_id,
                user_id="test-user",
                auth_provider_type="local",
                title="Test Session",
                created_at=now,
                updated_at=now,
            )
        )
    return engine, session_id


def _insert_user_message(engine: Any, session_id: str, content: str) -> str:
    """Persist a user chat message for verbatim blob provenance tests."""
    from datetime import UTC, datetime

    user_message_id = str(uuid4())
    with engine.begin() as conn:
        latest = conn.execute(
            select(chat_messages_table.c.sequence_no)
            .where(chat_messages_table.c.session_id == session_id)
            .order_by(chat_messages_table.c.sequence_no.desc())
        ).first()
        sequence_no = 1 if latest is None else latest.sequence_no + 1
        conn.execute(
            chat_messages_table.insert().values(
                id=user_message_id,
                session_id=session_id,
                role="user",
                content=content,
                raw_content=None,
                tool_calls=None,
                tool_call_id=None,
                sequence_no=sequence_no,
                writer_principal="route_user_message",
                created_at=datetime.now(UTC),
                composition_state_id=None,
                parent_assistant_id=None,
            )
        )
    return user_message_id


def _verbatim_blob_context(engine: Any, session_id: str, content: str) -> dict[str, str]:
    """Return execute_tool kwargs proving ``content`` came from a user message."""
    user_message_content = f"Use this exact content:\n{content}"
    user_message_id = _insert_user_message(engine, session_id, user_message_content)
    return {
        "user_message_id": user_message_id,
        "user_message_content": user_message_content,
    }


@contextmanager
def _blob_operation(
    engine: Any,
    session_id: str,
    *,
    kind: SessionOperationKind = SessionOperationKind.COMPOSE,
) -> Iterator[tuple[SQLiteLocalSessionOperationAuthority, SessionOperationContext]]:
    """Hold genuine operation authority around one explicit test dispatch."""
    authority = SQLiteLocalSessionOperationAuthority(engine)
    with fenced_operation_context(engine, session_id, operation_kind=kind) as context:
        yield authority, context


def _insert_session_fork_operation(engine: Any, session_id: str, *, status: str) -> str:
    """Persist one valid session-fork operation in the requested lifecycle state."""
    from datetime import UTC, datetime, timedelta

    from elspeth.web.sessions.models import session_operation_receipts_table

    operation_id = str(uuid4())
    now = datetime.now(UTC)
    values: dict[str, Any] = {
        "session_id": session_id,
        "operation_id": operation_id,
        "kind": "session_fork",
        "status": status,
        "request_hash": "f" * 64,
        "attempt": 1,
        "created_at": now,
        "updated_at": now,
    }
    if status == "in_progress":
        values.update(lease_token="fork-lease", lease_expires_at=now + timedelta(hours=1))
    elif status == "failed":
        values.update(settled_at=now, failure_code="operation_failed")
    elif status == "completed":
        target_session_id = str(uuid4())
        with engine.begin() as conn:
            conn.execute(
                sessions_table.insert().values(
                    id=target_session_id,
                    user_id="test-user",
                    auth_provider_type="local",
                    title="Fork child",
                    forked_from_session_id=session_id,
                    created_at=now,
                    updated_at=now,
                )
            )
        values.update(
            settled_at=now,
            result_session_id=target_session_id,
            response_hash="e" * 64,
        )
    else:
        raise AssertionError(f"unsupported test fork status {status!r}")
    with engine.begin() as conn:
        conn.execute(session_operation_receipts_table.insert().values(**values))
    return operation_id


def test_execute_create_blob_honors_configured_session_quota(tmp_path: Path) -> None:
    engine, session_id = _session_engine_with_session()
    blob_provenance = _verbatim_blob_context(engine, session_id, "exceeds")
    with _blob_operation(engine, session_id) as (blob_authority, blob_operation_context):
        result = execute_tool(
            "create_blob",
            {"filename": "too-large.txt", "mime_type": "text/plain", "content": "exceeds"},
            _empty_state(),
            _mock_catalog(),
            data_dir=str(tmp_path),
            session_engine=engine,
            session_id=session_id,
            session_operation_authority=blob_authority,
            session_operation_context=blob_operation_context,
            max_blob_storage_per_session_bytes=3,
            **blob_provenance,
        )

    assert result.success is False
    assert "3 byte limit" in result.validation.errors[0].message
    assert list((tmp_path / "blobs" / session_id).glob("*")) == []


class TestToolResult:
    def test_frozen(self) -> None:
        state = _empty_state()
        from elspeth.web.composer.state import ValidationSummary

        result = ToolResult(
            success=True,
            updated_state=state,
            validation=ValidationSummary(is_valid=False, errors=(ValidationEntry("test", "err", "high"),)),
            affected_nodes=("n1", "n2"),
        )
        with pytest.raises(AttributeError):
            result.success = False  # type: ignore[misc]

    def test_affected_nodes_frozen(self) -> None:
        state = _empty_state()
        from elspeth.web.composer.state import ValidationSummary

        result = ToolResult(
            success=True,
            updated_state=state,
            validation=ValidationSummary(is_valid=True, errors=()),
            affected_nodes=("n1",),
        )
        assert isinstance(result.affected_nodes, tuple)

    def test_to_dict_includes_warnings_and_suggestions(self) -> None:
        state = _empty_state()
        from elspeth.web.composer.state import ValidationSummary

        result = ToolResult(
            success=True,
            updated_state=state,
            validation=ValidationSummary(
                is_valid=True,
                errors=(),
                warnings=(ValidationEntry("test", "warn1", "medium"), ValidationEntry("test", "warn2", "medium")),
                suggestions=(ValidationEntry("test", "sug1", "low"),),
            ),
            affected_nodes=(),
        )
        d = result.to_dict()
        assert d["validation"]["warnings"] == [
            {"component": "test", "message": "warn1", "severity": "medium"},
            {"component": "test", "message": "warn2", "severity": "medium"},
        ]
        assert d["validation"]["suggestions"] == [
            {"component": "test", "message": "sug1", "severity": "low"},
        ]

    def test_to_dict_empty_warnings_and_suggestions(self) -> None:
        state = _empty_state()
        from elspeth.web.composer.state import ValidationSummary

        result = ToolResult(
            success=True,
            updated_state=state,
            validation=ValidationSummary(is_valid=True, errors=()),
            affected_nodes=(),
        )
        d = result.to_dict()
        assert d["validation"]["warnings"] == []
        assert d["validation"]["suggestions"] == []


class TestFailureResult:
    """``_failure_result`` must lead validation.errors with the rejection reason.

    The composer LLM converges via ``validation.errors`` ordering — see
    composer session 58d7ede3 (2026-05-08) where the LLM read stale
    state-snapshot errors first and burned a full round retrying the
    same call shape with only a cosmetic change. Locking the leading
    entry here makes that regression invisible to refactors.
    """

    def test_prepends_rejection_reason_to_errors(self) -> None:
        state = _empty_state()  # has neither source nor sinks
        result = _failure_result(state, "boom: missing path")

        assert result.success is False
        assert result.validation.is_valid is False
        first = result.validation.errors[0]
        assert first.component == "rejected_mutation"
        assert first.message == "boom: missing path"
        assert first.severity == "high"
        # State-level errors still present, just no longer leading.
        components = [e.component for e in result.validation.errors[1:]]
        assert "source" in components
        assert "pipeline" in components

    @pytest.mark.parametrize("code", [None, "profile_unavailable"])
    def test_rejection_details_have_one_serialized_authority(self, code: str | None) -> None:
        result = _failure_result(_empty_state(), "rejection text", error_code=code)
        wire = result.to_dict()
        assert result.data is None
        assert "data" not in wire
        entry = wire["validation"]["errors"][0]
        assert entry["component"] == "rejected_mutation"
        assert entry["message"] == "rejection text"
        if code is None:
            assert "error_code" not in entry
        else:
            assert entry["error_code"] == code

    def test_preserves_warnings_and_semantic_contracts(self) -> None:
        """Non-error fields on the input ValidationSummary survive prepending."""
        # state.validate() on an empty state yields no warnings/suggestions,
        # so this test asserts the ValidationSummary fields are reachable
        # and unchanged in shape after _failure_result wraps them.
        state = _empty_state()
        result = _failure_result(state, "x")
        assert result.validation.warnings == ()
        assert result.validation.suggestions == ()
        assert result.validation.semantic_contracts == ()
        assert result.validation.edge_contracts == ()


class TestToolResultSemanticContracts:
    """ToolResult.to_dict() must surface semantic_contracts.

    Every mutation tool (upsert_node, set_source, patch_*) returns a
    ToolResult; validation produced by state.validate() now carries
    semantic_contracts. Without exposing them in to_dict(), MCP clients
    only see the legacy errors/warnings fields and miss the structured
    plugin-declared contract records.
    """

    def test_tool_result_to_dict_includes_semantic_contracts(self) -> None:
        from tests.unit.web.composer.test_semantic_validator import _wardline_state

        state = _wardline_state(text_separator=" ")
        validation = state.validate()
        tr = ToolResult(
            success=True,
            updated_state=state,
            validation=validation,
            affected_nodes=(),
        )
        payload = tr.to_dict()
        assert "semantic_contracts" in payload["validation"]
        assert len(payload["validation"]["semantic_contracts"]) == 1
        contract = payload["validation"]["semantic_contracts"][0]
        assert contract["outcome"] == "conflict"
        assert contract["consumer_plugin"] == "line_explode"
        assert contract["producer_plugin"] == "web_scrape"
        assert contract["from_id"] == "scrape"
        assert contract["to_id"] == "explode"
        assert contract["requirement_code"] == "line_explode.source_field.line_framed_text"

    def test_tool_result_to_dict_emits_empty_list_for_no_contracts(self) -> None:
        """Surface parity: empty list when no contracts, not omitted."""
        state = _empty_state()
        from elspeth.web.composer.state import ValidationSummary

        result = ToolResult(
            success=True,
            updated_state=state,
            validation=ValidationSummary(is_valid=True, errors=()),
            affected_nodes=(),
        )
        d = result.to_dict()
        assert "semantic_contracts" in d["validation"]
        assert d["validation"]["semantic_contracts"] == []


class TestPreviewPipelineSemanticContracts:
    """A preview's envelope ``validation`` carries semantic_contracts; ``data`` does not twin them (R4)."""

    def test_envelope_validation_includes_semantic_contracts(self) -> None:
        from elspeth.web.composer.tools import _execute_preview_pipeline
        from tests.unit.web.composer.test_semantic_validator import _wardline_state

        state = _wardline_state(text_separator=" ")
        result = _execute_preview_pipeline({}, state, _trained_tool_context())
        contracts = result.to_dict()["validation"]["semantic_contracts"]
        assert len(contracts) == 1
        assert contracts[0]["outcome"] == "conflict"
        assert contracts[0]["consumer_plugin"] == "line_explode"
        assert "semantic_contracts" not in result.data


class TestToolResultValidation:
    def test_mutation_includes_validation(self) -> None:
        """Every mutation tool result includes validation summary."""
        state = _empty_state()
        catalog = _mock_catalog()
        result = execute_tool(
            "set_source",
            {
                "plugin": "csv",
                "on_success": "t1",
                "options": {"path": "/data/in.csv", "schema": {"mode": "observed"}},
                "on_validation_failure": "quarantine",
            },
            state,
            catalog,
        )
        assert result.validation is not None
        # Source is set but no sinks — should have validation error
        assert not result.validation.is_valid
        assert any("No sinks" in e.message for e in result.validation.errors)


class TestComputeValidationDelta:
    """Tests for _compute_validation_delta identity semantics."""

    def test_same_message_different_component_not_collapsed(self) -> None:
        """Two entries with identical message but different component are distinct."""
        before = ValidationSummary(
            is_valid=False,
            errors=(ValidationEntry("node:a", "Configuration incomplete.", "high"),),
        )
        after = ValidationSummary(
            is_valid=False,
            errors=(
                ValidationEntry("node:a", "Configuration incomplete.", "high"),
                ValidationEntry("node:b", "Configuration incomplete.", "high"),
            ),
        )
        delta = _compute_validation_delta(before, after)
        # node:b is genuinely new — must appear in new_errors
        assert len(delta["new_errors"]) == 1
        assert delta["new_errors"][0]["component"] == "node:b"
        assert delta["resolved_errors"] == []

    def test_same_component_same_message_not_duplicated(self) -> None:
        """Identical (component, message) across before/after is not new."""
        entry = ValidationEntry("source", "No source configured.", "high")
        before = ValidationSummary(is_valid=False, errors=(entry,))
        after = ValidationSummary(is_valid=False, errors=(entry,))
        delta = _compute_validation_delta(before, after)
        assert delta["new_errors"] == []
        assert delta["resolved_errors"] == []

    def test_resolved_entry_uses_component_identity(self) -> None:
        """An entry resolved for one component doesn't mask another."""
        before = ValidationSummary(
            is_valid=False,
            errors=(
                ValidationEntry("node:a", "Missing field.", "high"),
                ValidationEntry("node:b", "Missing field.", "high"),
            ),
        )
        after = ValidationSummary(
            is_valid=False,
            errors=(ValidationEntry("node:b", "Missing field.", "high"),),
        )
        delta = _compute_validation_delta(before, after)
        assert len(delta["resolved_errors"]) == 1
        assert delta["resolved_errors"][0]["component"] == "node:a"
        assert delta["new_errors"] == []

    def test_warning_delta_uses_component_identity(self) -> None:
        """Warnings also use (component, message) identity."""
        before = ValidationSummary(
            is_valid=True,
            errors=(),
            warnings=(ValidationEntry("output:main", "No path configured.", "medium"),),
        )
        after = ValidationSummary(
            is_valid=True,
            errors=(),
            warnings=(
                ValidationEntry("output:main", "No path configured.", "medium"),
                ValidationEntry("output:backup", "No path configured.", "medium"),
            ),
        )
        delta = _compute_validation_delta(before, after)
        assert len(delta["new_warnings"]) == 1
        assert delta["new_warnings"][0]["component"] == "output:backup"

    def test_both_empty_yields_empty_delta(self) -> None:
        """Two empty validation states produce an all-empty delta."""
        before = ValidationSummary(is_valid=True, errors=(), warnings=())
        after = ValidationSummary(is_valid=True, errors=(), warnings=())
        delta = _compute_validation_delta(before, after)
        assert delta == {
            "new_errors": [],
            "resolved_errors": [],
            "new_warnings": [],
            "resolved_warnings": [],
        }

    def test_empty_before_makes_all_after_new(self) -> None:
        """When before is empty, every entry in after is new."""
        before = ValidationSummary(is_valid=True, errors=(), warnings=())
        after = ValidationSummary(
            is_valid=False,
            errors=(
                ValidationEntry("node:x", "Bad config.", "high"),
                ValidationEntry("source", "Missing field.", "high"),
            ),
            warnings=(ValidationEntry("output:main", "Slow path.", "medium"),),
        )
        delta = _compute_validation_delta(before, after)
        assert len(delta["new_errors"]) == 2
        assert len(delta["new_warnings"]) == 1
        assert delta["resolved_errors"] == []
        assert delta["resolved_warnings"] == []

    def test_empty_after_makes_all_before_resolved(self) -> None:
        """When after is empty, every entry in before is resolved."""
        before = ValidationSummary(
            is_valid=False,
            errors=(ValidationEntry("node:a", "Missing plugin.", "high"),),
            warnings=(ValidationEntry("output:main", "No path.", "medium"),),
        )
        after = ValidationSummary(is_valid=True, errors=(), warnings=())
        delta = _compute_validation_delta(before, after)
        assert len(delta["resolved_errors"]) == 1
        assert delta["resolved_errors"][0]["component"] == "node:a"
        assert len(delta["resolved_warnings"]) == 1
        assert delta["resolved_warnings"][0]["component"] == "output:main"
        assert delta["new_errors"] == []
        assert delta["new_warnings"] == []

    def test_mixed_errors_and_warnings_independent(self) -> None:
        """Error and warning deltas are computed independently."""
        shared_entry = ValidationEntry("node:a", "Problem.", "high")
        before = ValidationSummary(
            is_valid=False,
            errors=(shared_entry,),
            warnings=(ValidationEntry("source", "Old warning.", "medium"),),
        )
        after = ValidationSummary(
            is_valid=False,
            errors=(shared_entry,),
            warnings=(ValidationEntry("source", "New warning.", "medium"),),
        )
        delta = _compute_validation_delta(before, after)
        # Error unchanged — no new, no resolved
        assert delta["new_errors"] == []
        assert delta["resolved_errors"] == []
        # Warning changed — old resolved, new appeared
        assert len(delta["new_warnings"]) == 1
        assert delta["new_warnings"][0]["message"] == "New warning."
        assert len(delta["resolved_warnings"]) == 1
        assert delta["resolved_warnings"][0]["message"] == "Old warning."

    def test_serialized_entries_include_severity(self) -> None:
        """Delta entries are serialized via to_dict() and include severity."""
        before = ValidationSummary(is_valid=True, errors=(), warnings=())
        after = ValidationSummary(
            is_valid=False,
            errors=(ValidationEntry("node:a", "Broken.", "high"),),
        )
        delta = _compute_validation_delta(before, after)
        entry = delta["new_errors"][0]
        assert entry == {"component": "node:a", "message": "Broken.", "severity": "high"}


class TestInjectPriorValidation:
    """Tests for _inject_prior_validation — attaches pre-mutation validation."""

    def _make_result(
        self,
        *,
        success: bool,
        prior: ValidationSummary | None = None,
    ) -> ToolResult:
        state = _empty_state()
        return ToolResult(
            success=success,
            updated_state=state,
            validation=ValidationSummary(is_valid=True, errors=()),
            affected_nodes=(),
            prior_validation=prior,
        )

    def test_injects_prior_on_success(self) -> None:
        """Successful mutation without prior_validation gets it injected."""
        prior = ValidationSummary(is_valid=False, errors=(ValidationEntry("source", "No source.", "high"),))
        result = self._make_result(success=True)
        assert result.prior_validation is None

        injected = _inject_prior_validation(result, prior)
        assert injected.prior_validation is prior
        assert injected.success is True

    def test_skips_injection_on_failure(self) -> None:
        """Failed mutation results are returned unchanged."""
        prior = ValidationSummary(is_valid=True, errors=())
        result = self._make_result(success=False)

        injected = _inject_prior_validation(result, prior)
        assert injected.prior_validation is None
        assert injected is result  # identity — unchanged

    def test_skips_injection_when_already_set(self) -> None:
        """Results that already carry prior_validation are not overwritten."""
        original_prior = ValidationSummary(
            is_valid=False,
            errors=(ValidationEntry("node:a", "Handler set this.", "high"),),
        )
        new_prior = ValidationSummary(is_valid=True, errors=())
        result = self._make_result(success=True, prior=original_prior)

        injected = _inject_prior_validation(result, new_prior)
        assert injected.prior_validation is original_prior  # not overwritten
        assert injected is result  # identity — unchanged

    def test_to_dict_includes_delta_when_prior_set(self) -> None:
        """ToolResult.to_dict() includes validation_delta when prior_validation present."""
        prior = ValidationSummary(
            is_valid=False,
            errors=(ValidationEntry("source", "No source.", "high"),),
        )
        state = _empty_state()
        result = ToolResult(
            success=True,
            updated_state=state,
            validation=ValidationSummary(is_valid=True, errors=()),
            affected_nodes=(),
            prior_validation=prior,
        )
        d = result.to_dict()
        assert "validation_delta" in d
        assert len(d["validation_delta"]["resolved_errors"]) == 1
        assert d["validation_delta"]["new_errors"] == []

    def test_to_dict_omits_delta_when_no_prior(self) -> None:
        """ToolResult.to_dict() excludes validation_delta when no prior_validation."""
        result = self._make_result(success=True)
        d = result.to_dict()
        assert "validation_delta" not in d


class TestExecuteToolPriorValidation:
    """Integration: execute_tool populates prior_validation for mutation tools."""

    def test_mutation_tool_gets_prior_validation(self) -> None:
        """set_source (a mutation tool) should populate prior_validation."""
        state = _empty_state()
        catalog = _mock_catalog()
        result = execute_tool(
            "set_source",
            {
                "plugin": "csv",
                "on_success": "t1",
                "options": {"path": "/data/in.csv", "schema": {"mode": "observed"}},
                "on_validation_failure": "quarantine",
            },
            state,
            catalog,
        )
        assert result.success is True
        assert result.prior_validation is not None
        # Prior should reflect the original empty state's validation
        d = result.to_dict()
        assert "validation_delta" in d

    def test_discovery_tool_has_no_prior_validation(self) -> None:
        """list_sources (a discovery tool) should NOT have prior_validation."""
        state = _empty_state()
        catalog = _mock_catalog()
        result = execute_tool("list_sources", {}, state, catalog)
        assert result.success is True
        assert result.prior_validation is None
        d = result.to_dict()
        assert "validation_delta" not in d

    def test_threaded_prior_used_for_mutation(self) -> None:
        """When prior_validation is threaded, execute_tool uses it as-is."""
        state = _empty_state()
        catalog = _mock_catalog()
        # Pre-compute validation for the empty state
        threaded = state.validate()
        result = execute_tool(
            "set_source",
            {
                "plugin": "csv",
                "on_success": "t1",
                "options": {"path": "/data/in.csv", "schema": {"mode": "observed"}},
                "on_validation_failure": "quarantine",
            },
            state,
            catalog,
            prior_validation=threaded,
        )
        assert result.success is True
        # The threaded validation should be used as the prior — identity check
        assert result.prior_validation is threaded

    def test_threaded_prior_produces_correct_delta(self) -> None:
        """Threaded validation produces the same delta as fresh computation."""
        state = _empty_state()
        catalog = _mock_catalog()
        source_args = {
            "plugin": "csv",
            "on_success": "t1",
            "options": {"path": "/data/in.csv", "schema": {"mode": "observed"}},
            "on_validation_failure": "quarantine",
        }
        # Without threading (fresh computation)
        result_fresh = execute_tool("set_source", source_args, state, catalog)
        # With threading
        threaded = state.validate()
        result_threaded = execute_tool(
            "set_source",
            source_args,
            state,
            catalog,
            prior_validation=threaded,
        )
        # Deltas should be identical
        delta_fresh = result_fresh.to_dict()["validation_delta"]
        delta_threaded = result_threaded.to_dict()["validation_delta"]
        assert delta_fresh == delta_threaded

    def test_chained_threading_across_mutations(self) -> None:
        """Validation chains correctly across sequential mutations."""
        state = _empty_state()
        catalog = _mock_catalog()

        # First mutation — no prior to thread
        r1 = execute_tool(
            "set_source",
            {
                "plugin": "csv",
                "on_success": "main",
                "options": {"path": "/data/in.csv", "schema": {"mode": "observed"}},
                "on_validation_failure": "quarantine",
            },
            state,
            catalog,
        )
        assert r1.success is True

        # Second mutation — thread r1's validation as prior
        r2 = execute_tool(
            "set_output",
            {
                "sink_name": "main",
                "plugin": "csv",
                "options": {"path": "/data/outputs/test-session/out.csv", "schema": {"mode": "observed"}},
                "on_write_failure": "discard",
            },
            r1.updated_state,
            catalog,
            prior_validation=r1.validation,
        )
        assert r2.success is True
        # r1.validation becomes r2's prior — identity check
        assert r2.prior_validation is r1.validation
        # The delta should reflect changes from r1's state to r2's state
        assert "validation_delta" in r2.to_dict()


# ---------------------------------------------------------------------------
# get_pipeline_state functional tests
# ---------------------------------------------------------------------------


class TestGetPipelineState:
    """Functional tests for get_pipeline_state — exercises all three modes
    (full state, component-specific, not-found) plus deep_thaw and redaction.
    """

    def _build_populated_state(self) -> CompositionState:
        """Build a state with source, node, output, and edge via tool calls."""
        state = _empty_state()
        catalog = _mock_catalog()

        r1 = execute_tool(
            "set_source",
            {
                "plugin": "csv",
                "on_success": "t1",
                "options": {"path": "/data/blobs/test-session/in.csv", "schema": {"mode": "observed"}},
                "on_validation_failure": "quarantine",
            },
            state,
            catalog,
        )
        assert r1.success is True

        r2 = execute_tool(
            "upsert_node",
            {
                "id": "t1",
                "node_type": "transform",
                "plugin": "passthrough",
                "input": "source",
                "on_success": "out",
                "options": {"schema": {"mode": "observed"}},
            },
            r1.updated_state,
            catalog,
        )
        assert r2.success is True

        r3 = execute_tool(
            "set_output",
            {
                "sink_name": "out",
                "plugin": "csv",
                "options": {"path": "/data/outputs/test-session/result.csv", "schema": {"mode": "observed"}},
                "on_write_failure": "discard",
            },
            r2.updated_state,
            catalog,
        )
        assert r3.success is True

        r4 = execute_tool(
            "upsert_edge",
            {"id": "e1", "from_node": "source", "to_node": "t1", "edge_type": "on_success"},
            r3.updated_state,
            catalog,
        )
        assert r4.success is True

        return r4.updated_state

    def test_full_state_returns_all_components(self) -> None:
        """No component arg returns source, nodes, outputs, edges, metadata."""
        state = self._build_populated_state()
        catalog = _mock_catalog()

        result = execute_tool("get_pipeline_state", {}, state, catalog)
        assert result.success is True

        # Use to_dict() for structural checks — result.data is frozen by ToolResult.__post_init__
        data = result.to_dict()["data"]
        assert _pipeline_state_default_source(data) is not None
        assert _pipeline_state_default_source(data)["plugin"] == "csv"
        assert len(data["nodes"]) == 1
        assert data["nodes"][0]["id"] == "t1"
        assert len(data["outputs"]) == 1
        assert data["outputs"][0]["sink_name"] == "out"
        assert len(data["edges"]) == 1
        assert data["edges"][0]["id"] == "e1"
        assert "metadata" in data
        # The envelope's own ``version`` is the only carrier; the payload no
        # longer twins it (elspeth-e405ad7cd2, systems ledger #39).
        assert "version" not in data

    def test_full_state_alias_full_returns_all_components(self) -> None:
        """component='full' is accepted as an explicit full-state alias."""
        state = self._build_populated_state()
        catalog = _mock_catalog()

        result = execute_tool("get_pipeline_state", {"component": "full"}, state, catalog)
        assert result.success is True
        data = result.to_dict()["data"]
        assert data["inspection"]["resolved_component"] == "full"
        assert "full" in data["inspection"]["accepted_full_state_aliases"]
        assert _pipeline_state_default_source(data) is not None
        assert data["nodes"][0]["id"] == "t1"
        assert data["outputs"][0]["sink_name"] == "out"
        assert data["edges"][0]["id"] == "e1"

    def test_full_state_alias_empty_string_returns_all_components(self) -> None:
        """component='' is accepted as an explicit full-state alias."""
        state = self._build_populated_state()
        catalog = _mock_catalog()

        result = execute_tool("get_pipeline_state", {"component": ""}, state, catalog)
        assert result.success is True
        data = result.to_dict()["data"]
        assert data["inspection"]["requested_component"] == ""
        assert data["inspection"]["resolved_component"] == "full"
        assert data["nodes"][0]["id"] == "t1"
        assert data["outputs"][0]["sink_name"] == "out"

    def test_full_state_options_are_plain_dicts(self) -> None:
        """to_dict() deep_thaw converts frozen containers to plain dicts for JSON serialization."""
        state = self._build_populated_state()
        catalog = _mock_catalog()

        result = execute_tool("get_pipeline_state", {}, state, catalog)
        assert result.success is True

        # to_dict() runs deep_thaw on result.data — options must be plain dicts
        data = result.to_dict()["data"]
        source_opts = _pipeline_state_default_source(data)["options"]
        assert isinstance(source_opts, dict)
        assert isinstance(source_opts.get("schema"), dict)

        node_opts = data["nodes"][0]["options"]
        assert isinstance(node_opts, dict)

    def test_component_source(self) -> None:
        """component='source' returns only the source component."""
        state = self._build_populated_state()
        catalog = _mock_catalog()

        result = execute_tool("get_pipeline_state", {"component": "source"}, state, catalog)
        assert result.success is True
        data = result.to_dict()["data"]
        assert "sources" in data
        assert _pipeline_state_default_source(data)["plugin"] == "csv"
        # Should not contain nodes/outputs/edges
        assert "nodes" not in data
        assert "outputs" not in data

    def test_component_source_when_none(self) -> None:
        """component='source' with no source set returns null source."""
        state = _empty_state()
        catalog = _mock_catalog()

        result = execute_tool("get_pipeline_state", {"component": "source"}, state, catalog)
        assert result.success is True
        data = result.to_dict()["data"]
        assert _pipeline_state_default_source(data) is None

    def test_component_node_by_id(self) -> None:
        """component=<node_id> returns that node's details."""
        state = self._build_populated_state()
        catalog = _mock_catalog()

        result = execute_tool("get_pipeline_state", {"component": "t1"}, state, catalog)
        assert result.success is True
        data = result.to_dict()["data"]
        assert "node" in data
        assert data["node"]["id"] == "t1"
        assert data["node"]["plugin"] == "passthrough"
        assert isinstance(data["node"]["options"], dict)

    def test_full_state_alias_does_not_shadow_real_node_id(self) -> None:
        """A real node id named 'full' remains addressable as a component."""
        state = _empty_state().with_node(
            NodeSpec(
                id="full",
                node_type="transform",
                plugin="passthrough",
                input="source",
                on_success="out",
                on_error=None,
                options={"schema": {"mode": "observed"}},
                condition=None,
                routes=None,
                fork_to=None,
                branches=None,
                policy=None,
                merge=None,
            )
        )
        catalog = _mock_catalog()

        result = execute_tool("get_pipeline_state", {"component": "full"}, state, catalog)
        assert result.success is True
        data = result.to_dict()["data"]
        assert "node" in data
        assert data["node"]["id"] == "full"
        assert "nodes" not in data

    def test_component_output_by_name(self) -> None:
        """component=<output_name> returns that output's details."""
        state = self._build_populated_state()
        catalog = _mock_catalog()

        result = execute_tool("get_pipeline_state", {"component": "out"}, state, catalog)
        assert result.success is True
        data = result.to_dict()["data"]
        assert "output" in data
        assert data["output"]["sink_name"] == "out"
        assert data["output"]["plugin"] == "csv"

    def test_component_not_found(self) -> None:
        """component=<nonexistent> returns failure."""
        state = self._build_populated_state()
        catalog = _mock_catalog()

        result = execute_tool("get_pipeline_state", {"component": "nonexistent"}, state, catalog)
        assert result.success is False

    def test_empty_state_full_returns_nulls(self) -> None:
        """Full state on empty pipeline returns null source and empty lists."""
        state = _empty_state()
        catalog = _mock_catalog()

        result = execute_tool("get_pipeline_state", {}, state, catalog)
        assert result.success is True
        data = result.to_dict()["data"]
        assert _pipeline_state_default_source(data) is None
        assert data["nodes"] == []
        assert data["outputs"] == []
        assert data["edges"] == []

    def test_no_prior_validation(self) -> None:
        """get_pipeline_state is a discovery tool — no prior_validation."""
        state = self._build_populated_state()
        catalog = _mock_catalog()

        result = execute_tool("get_pipeline_state", {}, state, catalog)
        assert result.prior_validation is None

    def test_blob_ref_source_path_redacted(self) -> None:
        """When source has blob_ref, internal storage path is redacted (B4)."""
        source = SourceSpec(
            plugin="csv",
            on_success="t1",
            options={"path": "/internal/blobs/abc123.csv", "blob_ref": "abc123", "schema": {"mode": "observed"}},
            on_validation_failure="quarantine",
        )
        state = CompositionState(
            source=source,
            nodes=(),
            edges=(),
            outputs=(),
            metadata=PipelineMetadata(),
            version=1,
        )
        catalog = _mock_catalog()

        result = execute_tool("get_pipeline_state", {}, state, catalog)
        assert result.success is True
        data = result.to_dict()["data"]
        # path key remains visible so the LLM can tell the source is configured,
        # but the internal storage value itself is not exposed.
        assert _pipeline_state_default_source(data)["options"]["path"] == EXPECTED_REDACTED_BLOB_SOURCE_PATH
        assert _pipeline_state_default_source(data)["options"]["blob_ref"] == "abc123"
        assert "/internal/blobs/abc123.csv" not in str(data)


# ---------------------------------------------------------------------------
# Blob tool tests — composer-level security boundaries
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Blob active-run protection (Finding 2: 73a1aa6cef)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Blob update quota enforcement (Finding 5: 527546bedb)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Secret tool tests — composer-level secret reference wiring
# ---------------------------------------------------------------------------


class TestSecretTools:
    """Secret reference composition tools: discovery, validation, and wiring.

    Security contracts tested:
    - Secret tools fail without secret_service (no ambient access)
    - list_secret_refs never returns plaintext values
    - validate_secret_ref returns availability status
    - wire_secret_ref sets a secret_ref marker in source options
    """

    def _mock_secret_service(self) -> _SecretServiceDouble:
        from elspeth.contracts.secrets import SecretInventoryItem

        svc = _SecretServiceDouble()
        svc.list_refs.return_value = [
            SecretInventoryItem(name="OPENROUTER_API_KEY", scope="user", available=True),
            SecretInventoryItem(name="DB_PASSWORD", scope="server", available=True),
        ]
        svc.has_ref.return_value = True
        return svc

    def test_list_secret_refs_without_service_returns_failure(self) -> None:
        """Secret tools with no secret_service must fail — no ambient access."""
        state = _empty_state()
        catalog = _mock_catalog()
        result = execute_tool("list_secret_refs", {}, state, catalog)
        assert result.success is False

    def test_list_secret_refs_returns_inventory(self) -> None:
        """list_secret_refs returns inventory items without values."""
        state = _empty_state()
        catalog = _mock_catalog()
        svc = self._mock_secret_service()
        result = execute_tool(
            "list_secret_refs",
            {},
            state,
            catalog,
            secret_service=svc,
            user_id="test-user",
        )
        assert result.success is True
        assert len(result.data) == 2
        # Ensure no value field leaked
        for item in result.data:
            assert "value" not in item

    def test_list_secret_refs_returns_unavailability_reason(self) -> None:
        """Unavailable secret refs expose structural reasons, never values."""
        from elspeth.contracts.secrets import SecretInventoryItem

        state = _empty_state()
        catalog = _mock_catalog()
        svc = _SecretServiceDouble()
        svc.list_refs.return_value = [
            SecretInventoryItem(
                name="OPENROUTER_API_KEY",
                scope="server",
                available=False,
                source_kind="env",
                reason="fingerprint_resolver_not_configured",
            )
        ]

        result = execute_tool(
            "list_secret_refs",
            {},
            state,
            catalog,
            secret_service=svc,
            user_id="test-user",
        )

        assert result.success is True
        assert len(result.data) == 1
        assert dict(result.data[0]) == {
            "name": "OPENROUTER_API_KEY",
            "scope": "server",
            "available": False,
            "source_kind": "env",
            "reason": "fingerprint_resolver_not_configured",
        }
        assert "value" not in repr(result.to_dict())

    def test_validate_secret_ref_returns_availability(self) -> None:
        """validate_secret_ref returns name and availability status."""
        state = _empty_state()
        catalog = _mock_catalog()
        svc = self._mock_secret_service()
        result = execute_tool(
            "validate_secret_ref",
            {"name": "OPENROUTER_API_KEY"},
            state,
            catalog,
            secret_service=svc,
            user_id="test-user",
        )
        assert result.success is True
        assert result.data["name"] == "OPENROUTER_API_KEY"
        assert result.data["available"] is True

    def test_validate_secret_ref_returns_unavailability_reason(self) -> None:
        """Known but unavailable secret refs carry the inventory reason."""
        from elspeth.contracts.secrets import SecretInventoryItem

        state = _empty_state()
        catalog = _mock_catalog()
        svc = _SecretServiceDouble()
        svc.has_ref.return_value = False
        svc.list_refs.return_value = [
            SecretInventoryItem(
                name="OPENROUTER_API_KEY",
                scope="server",
                available=False,
                source_kind="env",
                reason="fingerprint_resolver_not_configured",
            )
        ]

        result = execute_tool(
            "validate_secret_ref",
            {"name": "OPENROUTER_API_KEY"},
            state,
            catalog,
            secret_service=svc,
            user_id="test-user",
        )

        assert result.success is True
        assert dict(result.data) == {
            "name": "OPENROUTER_API_KEY",
            "available": False,
            "scope": "server",
            "source_kind": "env",
            "reason": "fingerprint_resolver_not_configured",
        }

    def test_validate_secret_ref_uses_bounded_resolvability_check_for_inventory_hits(self) -> None:
        """validate_secret_ref must not trust cheap inventory availability."""
        from elspeth.contracts.secrets import SecretInventoryItem

        state = _empty_state()
        catalog = _mock_catalog()
        svc = _SecretServiceDouble()
        svc.has_ref.return_value = False
        svc.list_refs.return_value = [
            SecretInventoryItem(
                name="OPENROUTER_API_KEY",
                scope="user",
                available=True,
                source_kind="user_store",
            )
        ]

        result = execute_tool(
            "validate_secret_ref",
            {"name": "OPENROUTER_API_KEY"},
            state,
            catalog,
            secret_service=svc,
            user_id="test-user",
        )

        svc.has_ref.assert_called_once_with("test-user", "OPENROUTER_API_KEY")
        assert result.success is True
        assert dict(result.data) == {
            "name": "OPENROUTER_API_KEY",
            "available": False,
            "scope": "user",
            "source_kind": "user_store",
            "reason": "value_decryption_failed",
        }

    def test_validate_secret_ref_without_service_returns_failure(self) -> None:
        state = _empty_state()
        catalog = _mock_catalog()
        result = execute_tool(
            "validate_secret_ref",
            {"name": "OPENROUTER_API_KEY"},
            state,
            catalog,
        )
        assert result.success is False

    def test_wire_secret_ref_sets_marker_in_source_options(self) -> None:
        """wire_secret_ref patches source options with a secret_ref marker."""
        catalog = _mock_catalog()
        svc = self._mock_secret_service()
        # First set a source
        state = _empty_state()
        r1 = execute_tool(
            "set_source",
            {
                "plugin": "csv",
                "on_success": "t1",
                "options": {"path": "/data/in.csv", "schema": {"mode": "observed"}},
                "on_validation_failure": "quarantine",
            },
            state,
            catalog,
        )
        assert r1.success is True
        # Now wire a secret into the source
        r2 = execute_tool(
            "wire_secret_ref",
            {
                "name": "OPENROUTER_API_KEY",
                "target": "source",
                "option_key": "api_key",
            },
            r1.updated_state,
            catalog,
            secret_service=svc,
            user_id="test-user",
            secret_wiring_policy=_wiring_policy("OPENROUTER_API_KEY", "source", "csv", "api_key"),
        )
        assert r2.success is True
        assert _default_source(r2.updated_state) is not None

        opts = deep_thaw(_default_source(r2.updated_state).options)
        assert opts["api_key"] == {"secret_ref": "OPENROUTER_API_KEY"}
        # Original options preserved
        assert opts["path"] == "/data/in.csv"

    def test_wire_secret_ref_rejects_source_non_credential_field(self) -> None:
        """wire_secret_ref must enforce the same placement policy as set_source."""
        catalog = _mock_catalog()
        svc = self._mock_secret_service()
        state = _empty_state()
        r1 = execute_tool(
            "set_source",
            {
                "plugin": "csv",
                "on_success": "t1",
                "options": {"path": "/data/in.csv", "schema": {"mode": "observed"}},
                "on_validation_failure": "quarantine",
            },
            state,
            catalog,
        )
        assert r1.success is True

        result = execute_tool(
            "wire_secret_ref",
            {
                "name": "OPENROUTER_API_KEY",
                "target": "source",
                "option_key": "path",
            },
            r1.updated_state,
            catalog,
            secret_service=svc,
            user_id="test-user",
            # Authorize the wiring so the placement policy is the gate under test.
            secret_wiring_policy=_wiring_policy("OPENROUTER_API_KEY", "source", "csv", "path"),
        )

        assert result.success is False
        assert result.updated_state is r1.updated_state
        assert "csv" in result.validation.errors[0].message
        assert "path" in result.validation.errors[0].message
        assert "OPENROUTER_API_KEY" in result.validation.errors[0].message
        assert "only credential-bearing fields" in result.validation.errors[0].message

    def test_wire_secret_ref_rejects_node_non_credential_field(self) -> None:
        """wire_secret_ref must enforce placement policy for node options."""
        catalog = _mock_catalog()
        svc = self._mock_secret_service()
        state = _empty_state()
        r1 = execute_tool(
            "upsert_node",
            {
                "id": "classify",
                "node_type": "transform",
                "plugin": "llm",
                "input": "source_out",
                "on_success": "main",
                "on_error": "discard",
                "options": _llm_options_with_api_key({"secret_ref": "OPENROUTER_API_KEY"}),
            },
            state,
            catalog,
        )
        assert r1.success is True

        result = execute_tool(
            "wire_secret_ref",
            {
                "name": "OPENROUTER_API_KEY",
                "target": "node",
                "target_id": "classify",
                "option_key": "template",
            },
            r1.updated_state,
            catalog,
            secret_service=svc,
            user_id="test-user",
            # Authorize the wiring so the placement policy is the gate under test.
            secret_wiring_policy=_wiring_policy("OPENROUTER_API_KEY", "transform", "llm", "template"),
        )

        assert result.success is False
        assert result.updated_state is r1.updated_state
        assert "llm" in result.validation.errors[0].message
        assert "template" in result.validation.errors[0].message
        assert "OPENROUTER_API_KEY" in result.validation.errors[0].message
        assert "only credential-bearing fields" in result.validation.errors[0].message

    def test_wire_secret_ref_rejects_output_non_credential_field(self) -> None:
        """wire_secret_ref must enforce placement policy for output options."""
        catalog = _mock_catalog()
        svc = self._mock_secret_service()
        state = _empty_state()
        r1 = execute_tool(
            "set_output",
            {
                "sink_name": "main",
                "plugin": "csv",
                "options": {"path": "/data/out.csv", "schema": {"mode": "observed"}},
                "on_write_failure": "discard",
            },
            state,
            catalog,
        )
        assert r1.success is True

        result = execute_tool(
            "wire_secret_ref",
            {
                "name": "OPENROUTER_API_KEY",
                "target": "output",
                "target_id": "main",
                "option_key": "path",
            },
            r1.updated_state,
            catalog,
            secret_service=svc,
            user_id="test-user",
            # Authorize the wiring so the placement policy is the gate under test.
            secret_wiring_policy=_wiring_policy("OPENROUTER_API_KEY", "sink", "csv", "path"),
        )

        assert result.success is False
        assert result.updated_state is r1.updated_state
        assert "csv" in result.validation.errors[0].message
        assert "path" in result.validation.errors[0].message
        assert "OPENROUTER_API_KEY" in result.validation.errors[0].message
        assert "only credential-bearing fields" in result.validation.errors[0].message

    def test_wire_secret_ref_without_service_returns_failure(self) -> None:
        state = _empty_state()
        catalog = _mock_catalog()
        result = execute_tool(
            "wire_secret_ref",
            {
                "name": "OPENROUTER_API_KEY",
                "target": "source",
                "option_key": "api_key",
            },
            state,
            catalog,
        )
        assert result.success is False

    def test_wire_secret_ref_nonexistent_ref_fails(self) -> None:
        """wire_secret_ref fails if the secret ref doesn't exist."""
        catalog = _mock_catalog()
        svc = self._mock_secret_service()
        svc.has_ref.return_value = False
        state = _empty_state()
        r1 = execute_tool(
            "set_source",
            {
                "plugin": "csv",
                "on_success": "t1",
                "options": {"schema": {"mode": "observed"}},
                "on_validation_failure": "quarantine",
            },
            state,
            catalog,
        )
        r2 = execute_tool(
            "wire_secret_ref",
            {
                "name": "NONEXISTENT",
                "target": "source",
                "option_key": "api_key",
            },
            r1.updated_state,
            catalog,
            secret_service=svc,
            user_id="test-user",
        )
        assert r2.success is False

    def test_wire_secret_ref_gets_prior_validation(self) -> None:
        """Secret mutation tools must populate prior_validation for validation delta."""
        catalog = _mock_catalog()
        svc = self._mock_secret_service()
        state = _empty_state()
        r1 = execute_tool(
            "set_source",
            {
                "plugin": "csv",
                "on_success": "t1",
                "options": {"path": "/data/in.csv", "schema": {"mode": "observed"}},
                "on_validation_failure": "quarantine",
            },
            state,
            catalog,
        )
        assert r1.success is True
        r2 = execute_tool(
            "wire_secret_ref",
            {
                "name": "OPENROUTER_API_KEY",
                "target": "source",
                "option_key": "api_key",
            },
            r1.updated_state,
            catalog,
            secret_service=svc,
            user_id="test-user",
            secret_wiring_policy=_wiring_policy("OPENROUTER_API_KEY", "source", "csv", "api_key"),
        )
        assert r2.success is True
        assert r2.prior_validation is not None
        d = r2.to_dict()
        assert "validation_delta" in d

    def _build_state_with_source(self, catalog: Any) -> CompositionState:
        """Helper: build state with a source for secret tool tests."""
        r = execute_tool(
            "set_source",
            {
                "plugin": "csv",
                "on_success": "t1",
                "options": {"path": "/data/in.csv", "schema": {"mode": "observed"}},
                "on_validation_failure": "quarantine",
            },
            _empty_state(),
            catalog,
        )
        assert r.success is True
        return r.updated_state

    def test_wire_secret_ref_threaded_prior_used(self) -> None:
        """Threaded prior_validation is reused by secret mutation tools (identity check).

        execute_tool dispatches secret mutations through a separate branch from
        standard mutations. Both branches must honour the prior_validation kwarg.
        """
        catalog = _mock_catalog()
        svc = self._mock_secret_service()
        state = self._build_state_with_source(catalog)
        threaded = state.validate()
        result = execute_tool(
            "wire_secret_ref",
            {
                "name": "OPENROUTER_API_KEY",
                "target": "source",
                "option_key": "api_key",
            },
            state,
            catalog,
            secret_service=svc,
            user_id="test-user",
            prior_validation=threaded,
            secret_wiring_policy=_wiring_policy("OPENROUTER_API_KEY", "source", "csv", "api_key"),
        )
        assert result.success is True
        assert result.prior_validation is threaded

    def test_wire_secret_ref_threaded_prior_produces_correct_delta(self) -> None:
        """Threaded and fresh prior_validation produce identical deltas for secret tools."""
        catalog = _mock_catalog()
        svc = self._mock_secret_service()
        state = self._build_state_with_source(catalog)
        secret_args = {
            "name": "OPENROUTER_API_KEY",
            "target": "source",
            "option_key": "api_key",
        }
        wiring_policy = _wiring_policy("OPENROUTER_API_KEY", "source", "csv", "api_key")
        # Fresh (no threading)
        result_fresh = execute_tool(
            "wire_secret_ref",
            secret_args,
            state,
            catalog,
            secret_service=svc,
            user_id="test-user",
            secret_wiring_policy=wiring_policy,
        )
        # Threaded
        threaded = state.validate()
        result_threaded = execute_tool(
            "wire_secret_ref",
            secret_args,
            state,
            catalog,
            secret_service=svc,
            user_id="test-user",
            prior_validation=threaded,
            secret_wiring_policy=wiring_policy,
        )
        delta_fresh = result_fresh.to_dict()["validation_delta"]
        delta_threaded = result_threaded.to_dict()["validation_delta"]
        assert delta_fresh == delta_threaded


class TestSecretWiringAuthorization:
    """wire_secret_ref is deny-by-default (elspeth-f3c1aafd25).

    Adjudicated policy: deny every wiring unless a server-authored
    destination allowlist authorizes the exact
    (secret, component_type, plugin, option_key) tuple. LLM tool arguments
    are never approval; an absent policy is a denial, not an allow.
    """

    def _svc(self) -> _SecretServiceDouble:
        svc = _SecretServiceDouble()
        svc.has_ref.return_value = True
        return svc

    def _state_with_source(self, catalog: Any) -> CompositionState:
        r = execute_tool(
            "set_source",
            {
                "plugin": "csv",
                "on_success": "t1",
                "options": {"path": "/data/in.csv", "schema": {"mode": "observed"}},
                "on_validation_failure": "quarantine",
            },
            _empty_state(),
            catalog,
        )
        assert r.success is True
        return r.updated_state

    def test_wire_without_policy_denies_and_leaves_state_unchanged(self) -> None:
        """No secret_wiring_policy in context = deny-by-default, never an allow."""
        catalog = _mock_catalog()
        state = self._state_with_source(catalog)
        result = execute_tool(
            "wire_secret_ref",
            {"name": "OPENROUTER_API_KEY", "target": "source", "option_key": "api_key"},
            state,
            catalog,
            secret_service=self._svc(),
            user_id="test-user",
        )
        assert result.success is False
        assert result.updated_state is state
        assert "OPENROUTER_API_KEY" in result.validation.errors[0].message
        assert "secret_wiring_allowlist" in result.validation.errors[0].message
        opts = deep_thaw(_default_source(result.updated_state).options)
        assert "api_key" not in opts

    def test_wire_with_empty_policy_denies(self) -> None:
        catalog = _mock_catalog()
        state = self._state_with_source(catalog)
        result = execute_tool(
            "wire_secret_ref",
            {"name": "OPENROUTER_API_KEY", "target": "source", "option_key": "api_key"},
            state,
            catalog,
            secret_service=self._svc(),
            user_id="test-user",
            secret_wiring_policy=SecretWiringPolicy(rules=()),
        )
        assert result.success is False
        assert result.updated_state is state

    @pytest.mark.parametrize(
        ("rule_secret", "rule_component", "rule_plugin", "rule_option"),
        [
            # Each axis off by one from the requested (OPENROUTER_API_KEY,
            # source, csv, api_key) wiring — every near-miss denies.
            ("OPENAI_API_KEY", "source", "csv", "api_key"),
            ("OPENROUTER_API_KEY", "sink", "csv", "api_key"),
            ("OPENROUTER_API_KEY", "source", "json", "api_key"),
            ("OPENROUTER_API_KEY", "source", "csv", "token"),
        ],
    )
    def test_wire_with_near_miss_rule_denies(
        self,
        rule_secret: str,
        rule_component: str,
        rule_plugin: str,
        rule_option: str,
    ) -> None:
        catalog = _mock_catalog()
        state = self._state_with_source(catalog)
        result = execute_tool(
            "wire_secret_ref",
            {"name": "OPENROUTER_API_KEY", "target": "source", "option_key": "api_key"},
            state,
            catalog,
            secret_service=self._svc(),
            user_id="test-user",
            secret_wiring_policy=_wiring_policy(rule_secret, rule_component, rule_plugin, rule_option),
        )
        assert result.success is False
        assert result.updated_state is state

    def test_wire_node_target_authorized_by_transform_rule(self) -> None:
        """Node targets (transform AND aggregation) authorize under 'transform'."""
        catalog = _mock_catalog()
        state = self._state_with_source(catalog)
        r1 = execute_tool(
            "upsert_node",
            {
                "id": "classify",
                "node_type": "transform",
                "plugin": "llm",
                "input": "t1",
                "on_success": "main",
                "on_error": "discard",
                "options": _llm_options_with_api_key({"secret_ref": "OPENROUTER_API_KEY"}),
            },
            state,
            catalog,
        )
        assert r1.success is True
        result = execute_tool(
            "wire_secret_ref",
            {"name": "AZURE_API_KEY", "target": "node", "target_id": "classify", "option_key": "api_key"},
            r1.updated_state,
            catalog,
            secret_service=self._svc(),
            user_id="test-user",
            secret_wiring_policy=_wiring_policy("AZURE_API_KEY", "transform", "llm", "api_key"),
        )
        assert result.success is True
        node = next(n for n in result.updated_state.nodes if n.id == "classify")
        assert deep_thaw(node.options)["api_key"] == {"secret_ref": "AZURE_API_KEY"}

    def test_wire_output_target_authorized_by_sink_rule(self) -> None:
        catalog = _mock_catalog()
        r1 = execute_tool(
            "set_output",
            {
                "sink_name": "main",
                "plugin": "csv",
                "options": {"path": "/data/out.csv", "schema": {"mode": "observed"}},
                "on_write_failure": "discard",
            },
            _empty_state(),
            catalog,
        )
        assert r1.success is True
        result = execute_tool(
            "wire_secret_ref",
            {"name": "SINK_API_KEY", "target": "output", "target_id": "main", "option_key": "api_key"},
            r1.updated_state,
            catalog,
            secret_service=self._svc(),
            user_id="test-user",
            secret_wiring_policy=_wiring_policy("SINK_API_KEY", "sink", "csv", "api_key"),
        )
        assert result.success is True
        output = next(o for o in result.updated_state.outputs if o.name == "main")
        assert deep_thaw(output.options)["api_key"] == {"secret_ref": "SINK_API_KEY"}

    def test_authorization_runs_before_marker_write_not_after(self) -> None:
        """A denied wiring never reaches placement/endpoint policy — the
        authorization boundary is outermost after existence."""
        catalog = _mock_catalog()
        state = self._state_with_source(catalog)
        result = execute_tool(
            "wire_secret_ref",
            {"name": "OPENROUTER_API_KEY", "target": "source", "option_key": "path"},
            state,
            catalog,
            secret_service=self._svc(),
            user_id="test-user",
        )
        assert result.success is False
        # The denial is the authorization denial, not the placement denial.
        assert "secret_wiring_allowlist" in result.validation.errors[0].message
        assert "only credential-bearing fields" not in result.validation.errors[0].message


class TestSecretToolsArgumentValidation:
    """Tier-3 shape contract for secret-ref dispatch.

    Pairs with ``TestSecretTools`` (semantic / state-mutation paths).  These
    tests exercise the central JSON Schema dispatcher guard. Malformed LLM
    calls (wrong type, extra field, unknown ``target`` enum) must fail before
    handler dispatch without echoing attacker-controlled values.
    """

    def _svc(self) -> _SecretServiceDouble:
        from elspeth.contracts.secrets import SecretInventoryItem

        svc = _SecretServiceDouble()
        svc.list_refs.return_value = [
            SecretInventoryItem(name="OPENROUTER_API_KEY", scope="user", available=True),
        ]
        svc.has_ref.return_value = True
        return svc

    def test_validate_secret_ref_wrong_type_for_name(self) -> None:
        state = _empty_state()
        catalog = _mock_catalog()
        result = execute_tool(
            "validate_secret_ref",
            {"name": 42},
            state,
            catalog,
            secret_service=self._svc(),
            user_id="test-user",
            tool_arguments_hash="0" * 64,
            validate_arguments=True,
        )
        assert result.success is False
        assert "Invalid arguments for tool 'validate_secret_ref'" in result.validation.errors[0].message
        assert "type" in result.validation.errors[0].message
        assert "42" not in result.validation.errors[0].message

    def test_validate_secret_ref_rejects_extra_field(self) -> None:
        state = _empty_state()
        catalog = _mock_catalog()
        result = execute_tool(
            "validate_secret_ref",
            {"name": "OPENROUTER_API_KEY", "bogus": "value"},
            state,
            catalog,
            secret_service=self._svc(),
            user_id="test-user",
            tool_arguments_hash="0" * 64,
            validate_arguments=True,
        )
        assert result.success is False
        assert "Invalid arguments for tool 'validate_secret_ref'" in result.validation.errors[0].message
        assert "unsupported" in result.validation.errors[0].message
        assert "OPENROUTER_API_KEY" not in result.validation.errors[0].message
        assert "value" not in result.validation.errors[0].message

    def test_wire_secret_ref_wrong_type_for_name(self) -> None:
        state = _empty_state()
        catalog = _mock_catalog()
        result = execute_tool(
            "wire_secret_ref",
            {"name": 42, "target": "source", "option_key": "api_key"},
            state,
            catalog,
            secret_service=self._svc(),
            user_id="test-user",
            tool_arguments_hash="0" * 64,
            validate_arguments=True,
        )
        assert result.success is False
        assert "Invalid arguments for tool 'wire_secret_ref'" in result.validation.errors[0].message
        assert "type" in result.validation.errors[0].message
        assert "42" not in result.validation.errors[0].message

    def test_wire_secret_ref_unknown_target_enum(self) -> None:
        state = _empty_state()
        catalog = _mock_catalog()
        result = execute_tool(
            "wire_secret_ref",
            {"name": "OPENROUTER_API_KEY", "target": "global", "option_key": "api_key"},
            state,
            catalog,
            secret_service=self._svc(),
            user_id="test-user",
            tool_arguments_hash="0" * 64,
            validate_arguments=True,
        )
        assert result.success is False
        assert "Invalid arguments for tool 'wire_secret_ref'" in result.validation.errors[0].message
        assert "declared values" in result.validation.errors[0].message
        assert "global" not in result.validation.errors[0].message

    def test_wire_secret_ref_rejects_extra_field(self) -> None:
        state = _empty_state()
        catalog = _mock_catalog()
        result = execute_tool(
            "wire_secret_ref",
            {
                "name": "OPENROUTER_API_KEY",
                "target": "source",
                "option_key": "api_key",
                "bogus": "value",
            },
            state,
            catalog,
            secret_service=self._svc(),
            user_id="test-user",
            tool_arguments_hash="0" * 64,
            validate_arguments=True,
        )
        assert result.success is False
        assert "Invalid arguments for tool 'wire_secret_ref'" in result.validation.errors[0].message
        assert "unsupported" in result.validation.errors[0].message
        assert "OPENROUTER_API_KEY" not in result.validation.errors[0].message
        assert "value" not in result.validation.errors[0].message


# ---------------------------------------------------------------------------
# Merge-patch helper tests
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# patch_source_options tool tests
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# patch_node_options tool tests
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# patch_output_options tool tests
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Patch output path security (Finding 1: 3554012f39)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# set_pipeline tool tests
# ---------------------------------------------------------------------------


def _valid_pipeline_args() -> dict[str, Any]:
    """Return a minimal valid set_pipeline args dict."""
    return {
        "source": {
            "plugin": "csv",
            "on_success": "source_out",
            "options": {"path": "/data/in.csv", "schema": {"mode": "observed"}},
            "on_validation_failure": "discard",
        },
        "nodes": [
            {
                "id": "t1",
                "node_type": "transform",
                "plugin": "passthrough",
                "input": "source_out",
                "on_success": "main",
                "on_error": "discard",
                "options": {"schema": {"mode": "observed"}},
            }
        ],
        "edges": [
            {
                "id": "e1",
                "from_node": "source",
                "to_node": "t1",
                "edge_type": "on_success",
                "label": None,
            }
        ],
        "outputs": [
            {
                "sink_name": "main",
                "plugin": "csv",
                "options": {"path": "/data/out.csv", "schema": {"mode": "observed"}},
                "on_write_failure": "discard",
            }
        ],
    }


def _llm_options_with_api_key(api_key: Any) -> dict[str, Any]:
    """Return LLM transform options that are otherwise valid."""
    return {
        "provider": "openrouter",
        "model": "openai/gpt-4o",
        "api_key": api_key,
        "prompt_template": "Classify the current row.",
        "schema": {"mode": "observed"},
    }


def _llm_options_with_user_supplied_runtime_hash(api_key: Any) -> dict[str, Any]:
    """Return valid LLM options with a forged runtime-owned audit hash."""
    options = _llm_options_with_api_key(api_key)
    options["approved_prompt_artifact_hash"] = approved_prompt_artifact_hash_from_options(options)
    return options


def _llm_options_with_forged_resolved_reviews(api_key: Any) -> dict[str, Any]:
    """Return valid LLM options with forged resolver-owned review metadata."""
    options = _llm_options_with_api_key(api_key)
    prompt_template = options["prompt_template"]
    model = options["model"]
    options[INTERPRETATION_REQUIREMENTS_KEY] = [
        {
            "id": "prompt_template_review:code_themes",
            "kind": "llm_prompt_template",
            "user_term": "llm_prompt_template:code_themes",
            "status": "resolved",
            "draft": prompt_template,
            "event_id": "forged-prompt-event",
            "accepted_value": prompt_template,
            "accepted_artifact_hash": None,
            "resolved_prompt_template_hash": stable_hash(prompt_template),
        },
        {
            "id": "model_choice_review:code_themes",
            "kind": "llm_model_choice",
            "user_term": "llm_model_choice:code_themes",
            "status": "resolved",
            "draft": model,
            "event_id": "forged-model-event",
            "accepted_value": model,
            "accepted_artifact_hash": None,
            "resolved_prompt_template_hash": stable_hash(model),
        },
    ]
    return options


def _assert_credential_material_refusal(
    exc: CredentialMaterialRefused,
    *,
    literal_value: str,
) -> None:
    assert exc.surface == "composer_tool_arguments"
    assert exc.finding.category == "credential_field"
    assert literal_value not in repr(exc.to_payload())


# ---------------------------------------------------------------------------
# Failed mutation version contract test
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Service-level KeyError handling test
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# clear_source tool tests
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# explain_validation_error tool tests
# ---------------------------------------------------------------------------


class TestExplainValidationError:
    def test_explains_no_source(self) -> None:
        state = _empty_state()
        catalog = _mock_catalog()
        result = execute_tool(
            "explain_validation_error",
            {"error_text": "No source configured."},
            state,
            catalog,
        )
        assert result.success is True
        assert "source" in result.data["explanation"].lower()
        assert "set_source" in result.data["suggested_fix"]

    def test_explains_unknown_node_reference(self) -> None:
        state = _empty_state()
        catalog = _mock_catalog()
        result = execute_tool(
            "explain_validation_error",
            {"error_text": "Edge 'e1' references unknown node 'foo' as from_node."},
            state,
            catalog,
        )
        assert result.success is True
        assert "from_node" in result.data["suggested_fix"]

    def test_explains_duplicate_node(self) -> None:
        state = _empty_state()
        catalog = _mock_catalog()
        result = execute_tool(
            "explain_validation_error",
            {"error_text": "Duplicate node ID: 'transform_1'."},
            state,
            catalog,
        )
        assert result.success is True
        assert "unique" in result.data["explanation"].lower()

    def test_unknown_error_teaches_closed_codes(self) -> None:
        """Unmatched text returns the closed-code catalogue, not a shrug.

        A planner can call the tool with
        exactly ``{"error_text": "ValidationError"}`` — the error_class from
        repair feedback, not a message or code — and got a generic non-answer
        mid-repair. The fallback now teaches usage: name the closed codes and
        say to pass the exact ``error_code`` string.

        The catalogue rides ONLY inside ``suggested_fix``: a ``known_codes``
        array (117 entries) exceeded the 64-wide persisted-projection cap and
        collapsed the whole audit row to a ``response_projection_limit`` stub
        (elspeth-3e28029d2f), so the array is gone — pinned absent here.
        """
        state = _empty_state()
        catalog = _mock_catalog()
        for junk in ("ValidationError", "node:merge_branches ValidationError validation_error"):
            result = execute_tool(
                "explain_validation_error",
                {"error_text": junk},
                state,
                catalog,
            )
            assert result.success is True
            assert "does not match" in result.data["explanation"]
            assert "error_code" in result.data["suggested_fix"]
            # The catalogue itself rides along so the model can route next turn.
            assert "unknown_node_type" in result.data["suggested_fix"]
            assert "coalesce_missing_branches" in result.data["suggested_fix"]
            assert "known_codes" not in result.data

    def test_fuzzy_routes_closed_code_embedded_in_noise(self) -> None:
        """A closed code buried in noise (any case) resolves to its guidance."""
        state = _empty_state()
        catalog = _mock_catalog()
        result = execute_tool(
            "explain_validation_error",
            {"error_text": "node:merge_branches COALESCE_MISSING_BRANCHES"},
            state,
            catalog,
        )
        assert result.success is True
        assert result.data["error_code"] == "coalesce_missing_branches"
        assert "branches" in result.data["suggested_fix"] and "policy=" in result.data["suggested_fix"]

    def test_explains_path_violation(self) -> None:
        state = _empty_state()
        catalog = _mock_catalog()
        result = execute_tool(
            "explain_validation_error",
            {"error_text": "Path violation (S2): '/etc/passwd' is outside the allowed directories."},
            state,
            catalog,
        )
        assert result.success is True
        assert "allowed directories" in result.data["explanation"]

    def test_explains_unreachable_node(self) -> None:
        state = _empty_state()
        catalog = _mock_catalog()
        result = execute_tool(
            "explain_validation_error",
            {"error_text": "Node 't1' input 'foo' is not reachable from any edge or the source on_success."},
            state,
            catalog,
        )
        assert result.success is True
        assert "on_success" in result.data["suggested_fix"].lower()

    def test_explains_schema_contract_violation(self) -> None:
        state = _empty_state()
        catalog = _mock_catalog()
        result = execute_tool(
            "explain_validation_error",
            {"error_text": ("Schema contract violation: 'source' -> 'add_world'. Consumer requires ['text']; producer guarantees [].")},
            state,
            catalog,
        )
        assert result.success is True
        assert "upstream" in result.data["explanation"].lower()
        assert "preview_pipeline" in result.data["suggested_fix"]
        assert "patch_source_options" in result.data["suggested_fix"]
        assert "patch_node_options" in result.data["suggested_fix"]

    def test_explains_sink_schema_contract_violation(self) -> None:
        state = _empty_state()
        catalog = _mock_catalog()
        result = execute_tool(
            "explain_validation_error",
            {
                "error_text": (
                    "Schema contract violation: 't1' -> 'output:main'. "
                    "Sink 'main' requires fields: [text]. "
                    "Producer (value_transform) guarantees: []. "
                    "Missing fields: [text]."
                )
            },
            state,
            catalog,
        )
        assert result.success is True
        assert "sink" in result.data["explanation"].lower()
        assert "preview_pipeline" in result.data["suggested_fix"]
        assert "patch_output_options" in result.data["suggested_fix"]
        assert "patch_source_options" in result.data["suggested_fix"]
        assert "patch_node_options" in result.data["suggested_fix"]

    # ---------------------------------------------------------------------
    # "Expected ..." hint surfacing — observation elspeth-obs-eb4509376c.
    #
    # The validator catches schema-spec mistakes and produces strings
    # like ``"... Expected single-key dict like {'field_name': 'type'} ..."``.
    # The handler used to throw away that hint entirely. These tests lock
    # in that the hint is echoed verbatim into ``suggested_fix``.
    # ---------------------------------------------------------------------

    def test_surfaces_expected_hint_when_pattern_matches(self) -> None:
        state = _empty_state()
        catalog = _mock_catalog()
        # The "Invalid options for source 'csv'" prefix matches an
        # existing pattern; the inner "Expected ..." span is the actionable
        # hint that the catalogue's static fix doesn't carry.
        error_text = (
            "Invalid options for source 'csv': Field spec at index 0 is a "
            "dict with 2 keys. Expected single-key dict like "
            "{'field_name': 'type'} or a string like 'field_name: type'."
        )
        result = execute_tool(
            "explain_validation_error",
            {"error_text": error_text},
            state,
            catalog,
        )
        assert result.success is True
        # Catalogue fix (from the matched pattern) is preserved.
        assert "patch_source_options" in result.data["suggested_fix"]
        # And the validator hint is appended verbatim.
        assert ("Expected single-key dict like {'field_name': 'type'} or a string like 'field_name: type'.") in result.data["suggested_fix"]

    def test_surfaces_expected_hint_when_no_pattern_matches(self) -> None:
        state = _empty_state()
        catalog = _mock_catalog()
        error_text = (
            "Field spec at index 0 is a dict with 2 keys. Expected "
            "single-key dict like {'field_name': 'type'} or a string "
            "like 'field_name: type'."
        )
        result = execute_tool(
            "explain_validation_error",
            {"error_text": error_text},
            state,
            catalog,
        )
        assert result.success is True
        # Falls through to the closed-code fallback but still surfaces the hint.
        assert "does not match" in result.data["explanation"]
        assert ("Expected single-key dict like {'field_name': 'type'} or a string like 'field_name: type'.") in result.data["suggested_fix"]

    def test_no_hint_appended_when_expected_substring_absent(self) -> None:
        state = _empty_state()
        catalog = _mock_catalog()
        result = execute_tool(
            "explain_validation_error",
            {"error_text": "No source configured."},
            state,
            catalog,
        )
        assert result.success is True
        # Without "Expected " the catalogue fix should be returned
        # unchanged — no synthetic hint, no trailing whitespace artifacts.
        assert result.data["suggested_fix"] == ("Use set_source to configure a source plugin (e.g. csv, json, dataverse).")

    def test_expected_hint_stops_at_sentence_boundary(self) -> None:
        state = _empty_state()
        catalog = _mock_catalog()
        error_text = "Some preamble. Expected an integer. Got a string. Other noise."
        result = execute_tool(
            "explain_validation_error",
            {"error_text": error_text},
            state,
            catalog,
        )
        assert result.success is True
        # The first sentence after "Expected " is what we surface.
        # Trailing "Got a string. Other noise." should not be included.
        assert "Expected an integer." in result.data["suggested_fix"]
        assert "Got a string" not in result.data["suggested_fix"]
        assert "Other noise" not in result.data["suggested_fix"]


class TestExplainValidationCode:
    """``explain_validation_code`` resolves a bare closed code to its guidance.

    The one-shot planner's repair feedback strips validation messages to the
    closed ``error_code`` (redaction boundary), so the node-shape codes must
    resolve to the SAME catalogue guidance ``explain_validation_error`` returns
    for the full message. First-match ordering over message-oriented regexes is
    load-bearing here — these cases prove each code lands on its intended entry
    and no earlier pattern eagerly mis-matches.
    """

    def test_unknown_node_type_teaches_fork_via_gate(self) -> None:
        from elspeth.web.composer.tools.generation import explain_validation_code

        resolved = explain_validation_code("unknown_node_type")
        assert resolved is not None
        explanation, fix = resolved
        assert "no 'fork' node_type" in explanation
        assert "GATE" in fix and "fork_to" in fix

    def test_coalesce_on_success_must_be_sink_names_sink(self) -> None:
        from elspeth.web.composer.tools.generation import explain_validation_code

        resolved = explain_validation_code("coalesce_on_success_must_be_sink")
        assert resolved is not None
        explanation, fix = resolved
        assert "sink" in explanation.lower()
        assert "sink" in fix.lower()

    def test_coalesce_policy_invalid_names_the_closed_vocabulary(self) -> None:
        """The surviving coalesce-policy code carries the whole repair.

        ``coalesce_missing_policy`` was retired with its emission
        (elspeth-deb2f5ed93) — an unset policy is the runtime's default, not a
        defect — so this entry is now the only route from a policy rejection to
        the engine's four legal values.
        """
        from elspeth.web.composer.tools.generation import explain_validation_code

        resolved = explain_validation_code("coalesce_policy_invalid")
        assert resolved is not None
        _explanation, fix = resolved
        for policy in ("require_all", "quorum", "best_effort", "first"):
            assert policy in fix
        assert "merge" in fix

    def test_coalesce_config_invalid_explains_that_options_are_not_runtime_configuration(self) -> None:
        from elspeth.web.composer.tools.generation import explain_validation_code

        resolved = explain_validation_code("coalesce_config_invalid")
        assert resolved is not None
        explanation, fix = resolved
        assert "options" in explanation
        assert "options" in fix
        assert "branches" in fix and "policy" in fix and "merge" in fix

    def test_gate_on_error_unknown_sink_teaches_node_level_policy(self) -> None:
        from elspeth.web.composer.tools.generation import explain_validation_code

        resolved = explain_validation_code("gate_on_error_unknown_sink")
        assert resolved is not None
        explanation, fix = resolved
        assert "gate" in explanation.lower()
        assert "upsert_node" in fix
        assert "discard" in fix

    def test_pipeline_decision_unregistered_lists_registered_kinds(self) -> None:
        from elspeth.web.composer.tools.generation import explain_validation_code

        resolved = explain_validation_code("pipeline_decision_unregistered")
        assert resolved is not None
        _explanation, fix = resolved
        # Naming the registered vocabulary is the whole point — an invented
        # decision term can never be reviewed, so the fix must enumerate the
        # kinds the planner may actually use.
        assert "drop_raw_html_fields" in fix
        assert "web_scrape_http_identity" in fix
        assert "prompt_injection_shield_recommendation" in fix

    def test_interpretation_requirements_invalid_teaches_full_row_shape(self) -> None:
        from elspeth.web.composer.tools.generation import explain_validation_code

        resolved = explain_validation_code("interpretation_requirements_invalid")
        assert resolved is not None
        explanation, fix = resolved
        # The guidance teaches the planner an authorable row shape after a rejection.
        assert "user_term" in fix
        assert "kind" in fix and "draft" in fix
        # Escape valve is load-bearing: the shield review is advisory, so
        # omitting the block entirely converges where a malformed row loops.
        assert "omit" in fix.lower()
        assert "user_term" in explanation or "malformed" in explanation.lower()

    def test_closed_code_catalogue_is_fully_explainable(self) -> None:
        """Every advertised closed code must resolve to catalogue guidance.

        ``_CLOSED_VALIDATION_ERROR_CODES`` is what the explain tool's fallback
        advertises and what its fuzzy route scans for — a code listed there
        that ``explain_validation_code`` cannot resolve would route the model
        to a dead end.
        """
        from elspeth.web.composer.tools.generation import (
            _CLOSED_VALIDATION_ERROR_CODES,
            explain_validation_code,
        )

        assert _CLOSED_VALIDATION_ERROR_CODES
        for code in _CLOSED_VALIDATION_ERROR_CODES:
            assert explain_validation_code(code) is not None, code

    def test_unmatched_code_returns_none(self) -> None:
        from elspeth.web.composer.tools.generation import explain_validation_code

        # The generic fallback code and policy-denial codes carry no catalogue
        # entry — callers must attach nothing rather than a misleading generic.
        assert explain_validation_code("validation_error") is None
        assert explain_validation_code("plugin_policy_denied") is None
        assert explain_validation_code("") is None


# ---------------------------------------------------------------------------
# list_models tool tests
# ---------------------------------------------------------------------------


class TestListModels:
    def test_mixed_catalog_provider_and_limit_select_exact_models(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from elspeth.web.composer.tools import generation

        matching = [f"example/model{i:02}" for i in range(52)]
        models = ["other/foreign", "bare-model", *matching, "example-other/foreign"]
        monkeypatch.setattr(generation, "read_litellm_model_list", lambda: models)

        default = execute_tool("list_models", {"provider": "example/"}, _empty_state(), _mock_catalog())
        limited = execute_tool("list_models", {"provider": "example/", "limit": 2}, _empty_state(), _mock_catalog())
        complete = execute_tool("list_models", {"provider": "example/", "limit": 52}, _empty_state(), _mock_catalog())

        assert default.success is True
        assert deep_thaw(default.data) == {"models": matching[:50], "count": 52, "truncated": True}
        assert limited.success is True
        assert deep_thaw(limited.data) == {"models": matching[:2], "count": 52, "truncated": True}
        assert complete.success is True
        assert deep_thaw(complete.data) == {"models": matching, "count": 52, "truncated": False}

    def test_mixed_catalog_omitted_and_empty_provider_take_distinct_branches(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from elspeth.web.composer.tools import generation

        models = ["example/one", "bare-one", "other/two", "bare-two", "openrouter/stale"]
        monkeypatch.setattr(generation, "read_litellm_model_list", lambda: models)
        monkeypatch.setattr(generation, "get_catalog_values", lambda name: frozenset({"live/one", "live/two"}))

        summary = execute_tool("list_models", {}, _empty_state(), _mock_catalog())
        unprefixed = execute_tool("list_models", {"provider": ""}, _empty_state(), _mock_catalog())

        assert summary.success is True
        assert deep_thaw(summary.data["providers"]) == {"example": 1, "": 2, "other": 1, "openrouter": 2}
        assert summary.data["total_models"] == 6
        assert "models" not in summary.data
        assert unprefixed.success is True
        assert deep_thaw(unprefixed.data) == {"models": ["bare-one", "bare-two"], "count": 2, "truncated": False}

    def test_list_models_returns_provider_summary(self) -> None:
        state = _empty_state()
        catalog = _mock_catalog()
        result = execute_tool("list_models", {}, state, catalog)
        assert result.success is True
        # Without provider filter, returns provider-grouped summary
        assert "providers" in result.data
        assert "total_models" in result.data

    def test_list_models_with_provider_filter(self) -> None:
        state = _empty_state()
        catalog = _mock_catalog()
        # Even if litellm returns an empty list, the filter shouldn't crash
        result = execute_tool(
            "list_models",
            {"provider": "openrouter/"},
            state,
            catalog,
        )
        assert result.success is True
        assert isinstance(result.data["models"], (list, tuple))

    def test_list_models_empty_string_provider_filters_unprefixed(self) -> None:
        """Empty string from provider summary round-trips as a filter for unprefixed models."""
        state = _empty_state()
        catalog = _mock_catalog()
        result = execute_tool(
            "list_models",
            {"provider": ""},
            state,
            catalog,
        )
        assert result.success is True
        # Should enter the filter path, not the summary path
        assert "models" in result.data
        assert "providers" not in result.data

    def test_list_models_summary_uses_empty_string_for_unprefixed(self) -> None:
        """Provider summary uses empty string (not display-only label) for unprefixed models."""
        state = _empty_state()
        catalog = _mock_catalog()
        result = execute_tool("list_models", {}, state, catalog)
        assert result.success is True
        providers = result.data.get("providers", {})
        # Must not contain the non-round-trippable "(no provider)" label
        assert "(no provider)" not in providers

    def test_list_models_openrouter_reads_live_catalog(self) -> None:
        """``provider="openrouter/"`` returns the live-catalog slice, not the bundled litellm slice.

        Closes the loop between composer discovery and validator preflight:
        the validator (``CatalogValueSource`` on ``OpenRouterLLMProviderConfig``)
        rejects model identifiers absent from ``get_catalog_values(MODEL_CATALOG_OPENROUTER)``,
        so the discovery tool MUST advertise the exact same set. A
        regression here re-opens the
        ``ValueSourceValidationError`` class of tutorial-run failure.
        """
        from elspeth.contracts.value_source import get_catalog_values
        from elspeth.plugins.transforms.llm.model_catalog import MODEL_CATALOG_OPENROUTER

        state = _empty_state()
        catalog = _mock_catalog()
        live = get_catalog_values(MODEL_CATALOG_OPENROUTER)
        if not live:
            pytest.skip("OpenRouter catalog empty; cannot verify wiring")
        result = execute_tool(
            "list_models",
            {"provider": "openrouter/", "limit": 1000},
            state,
            catalog,
        )
        assert result.success is True
        returned = set(result.data["models"])
        # Sample-correctness: every advertised model exists in the live catalog.
        assert returned <= live, (
            "list_models advertised openrouter slugs absent from the live catalog "
            f"(strays={returned - live!r}) — composer discovery and validator preflight drifted apart"
        )

    def test_list_models_openrouter_count_reflects_live_catalog(self) -> None:
        """The unfiltered summary advertises the live-catalog count for openrouter.

        Prevents the regression where the summary's ``providers["openrouter"]``
        count came from the stale bundled litellm list while a follow-up
        ``provider="openrouter/"`` filter returned the live (smaller) set.
        """
        from elspeth.contracts.value_source import get_catalog_values
        from elspeth.plugins.transforms.llm.model_catalog import MODEL_CATALOG_OPENROUTER

        state = _empty_state()
        catalog = _mock_catalog()
        live = get_catalog_values(MODEL_CATALOG_OPENROUTER)
        if not live:
            pytest.skip("OpenRouter catalog empty; cannot verify wiring")
        result = execute_tool("list_models", {}, state, catalog)
        assert result.success is True
        providers = result.data["providers"]
        assert providers.get("openrouter") == len(live)


# ---------------------------------------------------------------------------
# get_audit_info tool tests
# ---------------------------------------------------------------------------


class TestGetAuditInfo:
    """``get_audit_info`` returns CONSTANT facts about the Landscape audit.

    The audit backend is operator-managed (``WebSettings.get_landscape_url()``)
    and intentionally not composer-controllable (security fix S1, see
    ``web/composer/yaml_generator.py:179``). This tool exists so the LLM can
    answer "is audit on, can I configure it?" without inventing a backend
    type and without leaking operator-internal config (URL, encryption key)
    into chat. These tests pin down both behaviours.
    """

    def test_returns_enabled_true_and_not_modifiable(self) -> None:
        state = _empty_state()
        catalog = _mock_catalog()
        result = execute_tool("get_audit_info", {}, state, catalog)
        assert result.success is True
        assert result.data["enabled"] is True
        assert result.data["composer_modifiable"] is False

    def test_summary_fields_present_and_non_empty(self) -> None:
        """The model paraphrases ``summary``; an empty string here would let it invent text."""
        state = _empty_state()
        catalog = _mock_catalog()
        result = execute_tool("get_audit_info", {}, state, catalog)
        assert isinstance(result.data["summary"], str) and result.data["summary"].strip()
        assert isinstance(result.data["audit_export_summary"], str) and result.data["audit_export_summary"].strip()

    def test_payload_never_leaks_operator_internal_fields(self) -> None:
        """Regression guard: never expose URL/DSN/path/encryption-key fields.

        If a future edit adds any of these as a top-level key OR embeds them
        into one of the existing string fields, this test fails. The audit
        URL is operator-internal — surfacing it to the LLM would defeat the
        purpose of S1 (the LLM might echo it back to the user, log it to
        chat history, or use it to construct a sink shape).
        """
        state = _empty_state()
        catalog = _mock_catalog()
        result = execute_tool("get_audit_info", {}, state, catalog)
        forbidden_keys = {
            "url",
            "dsn",
            "path",
            "database_url",
            "landscape_url",
            "encryption_key",
            "encryption_key_env",
            "passphrase",
            "backend",
        }
        assert forbidden_keys.isdisjoint(result.data.keys()), (
            f"get_audit_info leaked operator-internal keys into payload: {forbidden_keys & result.data.keys()}"
        )

        # Also string-scan the values: a future change might inline a path
        # into the summary text. Catch obvious DSN / sqlite path patterns.
        joined_text = " ".join(v for v in result.data.values() if isinstance(v, str)).lower()
        for forbidden_substring in ("sqlite:///", "postgresql://", "postgres://", ".db", "/audit", "audit.db"):
            assert forbidden_substring not in joined_text, (
                f"get_audit_info summary text contains forbidden DSN/path substring "
                f"{forbidden_substring!r} — operator-internal config must not be "
                f"echoed into the LLM context (security fix S1)."
            )

    def test_state_is_unchanged(self) -> None:
        """Discovery tool MUST NOT mutate state — updated_state is the same instance."""
        state = _empty_state()
        catalog = _mock_catalog()
        result = execute_tool("get_audit_info", {}, state, catalog)
        assert result.updated_state is state

    def test_is_registered_as_cacheable_discovery(self) -> None:
        """Result is constant per compose call — should be cacheable."""
        from elspeth.web.composer.tools import (
            is_cacheable_discovery_tool,
            is_discovery_tool,
        )

        assert is_discovery_tool("get_audit_info")
        assert is_cacheable_discovery_tool("get_audit_info")

    def test_appears_in_tool_definitions(self) -> None:
        """The tool must be advertised in the LLM-visible function list."""
        from elspeth.web.composer.tools import get_tool_definitions

        names = {defn["name"] for defn in get_tool_definitions()}
        assert "get_audit_info" in names


# ---------------------------------------------------------------------------
# get_plugin_assistance tool tests
# ---------------------------------------------------------------------------


class TestGetPluginAssistance:
    def test_returns_structured_payload_for_known_issue_code(self) -> None:
        state = _empty_state()
        catalog = _mock_catalog()
        result = execute_tool(
            "get_plugin_assistance",
            {
                "plugin_type": "transform",
                "plugin_name": "web_scrape",
                "issue_code": "web_scrape.content.compact_text",
            },
            state,
            catalog,
        )
        assert result.success is True
        # ToolResult.to_dict deep-thaws ``data`` for LLM consumption; tests
        # exercise the wire shape rather than the frozen in-memory form.
        payload = result.to_dict()["data"]
        assert payload["plugin_type"] == "transform"
        assert payload["plugin_name"] == "web_scrape"
        assert payload["issue_code"] == "web_scrape.content.compact_text"
        assert "summary" in payload
        assert isinstance(payload["summary"], str)
        assert payload["summary"]
        assert isinstance(payload["suggested_fixes"], list)
        assert payload["suggested_fixes"]
        assert isinstance(payload["examples"], list)
        # web_scrape declares two PluginAssistanceExample entries for this code.
        assert len(payload["examples"]) == 2
        for example in payload["examples"]:
            assert isinstance(example["title"], str)
            # before/after are dicts when present (post-thaw).
            assert example["before"] is None or isinstance(example["before"], dict)
            assert example["after"] is None or isinstance(example["after"], dict)
        assert isinstance(payload["composer_hints"], list)

    def test_line_explode_assistance_returns_structured_payload(self) -> None:
        state = _empty_state()
        catalog = _mock_catalog()
        result = execute_tool(
            "get_plugin_assistance",
            {
                "plugin_type": "transform",
                "plugin_name": "line_explode",
                "issue_code": "line_explode.source_field.line_framed_text",
            },
            state,
            catalog,
        )
        assert result.success is True
        payload = result.data
        assert payload["plugin_name"] == "line_explode"
        assert payload["issue_code"] == "line_explode.source_field.line_framed_text"
        assert payload["summary"]
        assert payload["suggested_fixes"]

    def test_batch_distribution_profile_assistance_points_categorical_counts_to_top_k(self) -> None:
        state = _empty_state()
        catalog = _mock_catalog()
        result = execute_tool(
            "get_plugin_assistance",
            {
                "plugin_type": "transform",
                "plugin_name": "batch_distribution_profile",
                "issue_code": "batch_distribution_profile.value_field.numeric",
            },
            state,
            catalog,
        )
        assert result.success is True
        payload = result.data
        assert payload["plugin_name"] == "batch_distribution_profile"
        assert payload["issue_code"] == "batch_distribution_profile.value_field.numeric"
        assert "numeric" in payload["summary"]
        assert any("batch_top_k" in fix for fix in payload["suggested_fixes"])

    def test_unknown_issue_code_returns_explicit_no_assistance(self) -> None:
        """Plugin returns None for unknown issue codes -> tool returns success
        with explicit summary=None and empty suggestion list so the agent sees
        that nothing was published rather than a hard failure."""
        state = _empty_state()
        catalog = _mock_catalog()
        result = execute_tool(
            "get_plugin_assistance",
            {
                "plugin_type": "transform",
                "plugin_name": "web_scrape",
                "issue_code": "web_scrape.unrecognized.code",
            },
            state,
            catalog,
        )
        assert result.success is True
        payload = result.to_dict()["data"]
        assert payload["plugin_name"] == "web_scrape"
        assert payload["issue_code"] == "web_scrape.unrecognized.code"
        assert payload["summary"] is None
        assert payload["suggested_fixes"] == []
        assert payload["examples"] == []
        assert payload["composer_hints"] == []

    def test_unknown_plugin_name_returns_failure(self) -> None:
        state = _empty_state()
        catalog = _mock_catalog()
        result = execute_tool(
            "get_plugin_assistance",
            {
                "plugin_type": "transform",
                "plugin_name": "no_such_plugin_xyz",
                "issue_code": "anything",
            },
            state,
            catalog,
        )
        assert result.success is False
        assert result.validation.errors[0].error_code == "plugin_not_installed"

    def test_invalid_plugin_type_returns_failure(self) -> None:
        """plugin_type is validated up-front; mistyped value surfaces as failure."""
        state = _empty_state()
        catalog = _mock_catalog()
        from elspeth.web.composer.protocol import ToolArgumentError

        with pytest.raises(ToolArgumentError):
            execute_tool(
                "get_plugin_assistance",
                {
                    "plugin_type": "transformer",  # typo
                    "plugin_name": "web_scrape",
                },
                state,
                catalog,
            )

    def test_dispatches_to_source_family(self) -> None:
        """plugin_type='source' looks up the plugin via get_source_by_name."""
        state = _empty_state()
        catalog = _mock_catalog()
        result = execute_tool(
            "get_plugin_assistance",
            {
                "plugin_type": "source",
                "plugin_name": "csv",
            },
            state,
            catalog,
        )
        assert result.success is True
        payload = result.to_dict()["data"]
        assert payload["plugin_type"] == "source"
        assert payload["plugin_name"] == "csv"

    def test_csv_discovery_explains_generated_source_review_handoff(self) -> None:
        """CSV guidance should make source-level invented-source review mechanics explicit."""
        state = _empty_state()
        catalog = _mock_catalog()
        result = execute_tool(
            "get_plugin_assistance",
            {
                "plugin_type": "source",
                "plugin_name": "csv",
            },
            state,
            catalog,
        )
        assert result.success is True
        payload = result.to_dict()["data"]
        hints = " ".join(payload["composer_hints"])

        assert "columns tells CSVSource how to parse headerless rows" in hints
        assert "downstream DAG validation still needs a field contract" in hints
        assert "must not author schema.guaranteed_fields" in hints
        assert "source_data_contract" in hints
        assert "schema.guaranteed_fields" in hints
        assert "CSV source options do not have url_field" in hints
        assert "set url_field on the web_scrape node" in hints
        assert "If you authored CSV rows or chose source values" in hints
        assert "blob-backed source" in hints
        assert "stage invented_source on source.options.interpretation_requirements" in hints
        assert "request_interpretation_review" in hints
        assert "affected_node_id='source'" in hints
        assert "omit llm_draft" in hints
        assert "server uses the exact staged source draft" in hints
        assert "source is not a transform node" in hints
        assert "do not search nodes[] for source" in hints

    def test_dispatches_to_sink_family(self) -> None:
        """plugin_type='sink' looks up the plugin via get_sink_by_name."""
        state = _empty_state()
        catalog = _mock_catalog()
        result = execute_tool(
            "get_plugin_assistance",
            {
                "plugin_type": "sink",
                "plugin_name": "json",
            },
            state,
            catalog,
        )
        assert result.success is True
        payload = result.to_dict()["data"]
        assert payload["plugin_type"] == "sink"
        assert payload["plugin_name"] == "json"

    def test_json_sink_discovery_explains_that_sink_names_do_not_clean_rows(self) -> None:
        """JSON sink guidance should point row cleanup back to field_mapper."""
        state = _empty_state()
        catalog = _mock_catalog()
        result = execute_tool(
            "get_plugin_assistance",
            {
                "plugin_type": "sink",
                "plugin_name": "json",
            },
            state,
            catalog,
        )
        assert result.success is True
        payload = result.to_dict()["data"]
        hints = " ".join(payload["composer_hints"])

        assert "JSON sink writes the row it receives" in hints
        assert "schema, format, sink name, and output name do not drop fields" in hints
        assert "Use field_mapper before the sink" in hints
        assert "web_scrape results saved without raw page bodies" in hints
        assert "field_mapper(select_only=true)" in hints
        assert "a sink named cleanup is not a cleanup transform" in hints

    def test_omitting_issue_code_returns_discovery_payload(self) -> None:
        """Discovery-time mode: ``issue_code`` may be omitted entirely."""
        state = _empty_state()
        catalog = _mock_catalog()
        result = execute_tool(
            "get_plugin_assistance",
            {
                "plugin_type": "transform",
                "plugin_name": "web_scrape",
            },
            state,
            catalog,
        )
        assert result.success is True
        payload = result.to_dict()["data"]
        assert payload["issue_code"] is None
        hints = " ".join(payload["composer_hints"])
        assert "transform" in hints
        assert "not a source" in hints
        assert "url_field" in hints
        assert "Unknown source plugin: web_scrape" in hints
        assert "surface prompt-injection shielding as an important recommendation" in hints
        assert "Recommendation is not permission to add a node" in hints
        assert "do not add passthrough, placeholder, no-op, or renamed utility nodes" in hints
        assert "do not substitute azure_content_safety" in hints
        assert "do not insert it automatically" in hints
        assert "route through azure_content_safety first" not in hints

    def test_web_scrape_discovery_explains_pass_through_url_contract(self) -> None:
        """web_scrape should guide downstream nodes to use guaranteed URL fields."""
        state = _empty_state()
        catalog = _mock_catalog()
        result = execute_tool(
            "get_plugin_assistance",
            {
                "plugin_type": "transform",
                "plugin_name": "web_scrape",
            },
            state,
            catalog,
        )
        assert result.success is True
        payload = result.to_dict()["data"]
        hints = " ".join(payload["composer_hints"])

        assert "passes through upstream row fields" in hints
        assert "fetch_url_final" in hints
        assert "Do not make downstream LLM templates require a URL field" in hints
        assert "unless the upstream source schema or web_scrape schema guarantees that field" in hints
        assert "do not patch web_scrape guaranteed_fields by guess" in hints
        assert "schema is required" in hints
        assert "For raw HTML, set format to raw" in hints
        assert "not html" in hints
        assert "If the user-facing output should exclude raw scraped content" in hints
        assert "route the final path through field_mapper with select_only: true" in hints
        assert "a sink name or output name is not cleanup" in hints
        assert "A validator-valid direct route from web_scrape or an LLM to the sink is still incomplete" in hints
        assert "If scraped public internet content flows into an LLM" in hints
        assert "azure_prompt_shield" in hints
        assert "only when discovery lists it" in hints
        assert "prompt_injection_shield_recommendation" in hints

    def test_field_mapper_discovery_explains_cleanup_whitelist_semantics(self) -> None:
        """field_mapper hints should make utility-cleanup behavior explicit."""
        state = _empty_state()
        catalog = _mock_catalog()
        result = execute_tool(
            "get_plugin_assistance",
            {
                "plugin_type": "transform",
                "plugin_name": "field_mapper",
            },
            state,
            catalog,
        )
        assert result.success is True
        payload = result.to_dict()["data"]
        assert payload["issue_code"] is None
        hints = " ".join(payload["composer_hints"])

        assert "Use select_only: true" in hints
        assert "whitelist exactly the saved output fields" in hints
        assert "only utility transform that actually removes raw fields" in hints
        assert "source -> web_scrape -> llm -> field_mapper(cleanup) -> sink" in hints
        assert "A JSON sink named cleanup is not a cleanup transform" in hints
        assert "A validator-valid direct route from web_scrape or an LLM to the sink is still incomplete" in hints
        assert "A cleanup stream name is not a cleanup node" in hints
        assert "do not offer to repair it later" in hints
        assert "set the upstream LLM or scraper on_success to the cleanup mapper" in hints
        assert "set the cleanup mapper on_success to the sink" in hints
        assert "If an LLM routes directly to a JSON sink whose name sounds like cleanup" in hints
        assert "the LLM passes through raw scrape fields until this field_mapper whitelists them" in hints
        assert "route the mapper directly to the existing sink" in hints
        assert "do not remove the cleanup mapper or output" in hints
        assert "before raw scraped fields exist cannot satisfy scraped-content cleanup" in hints
        assert "preserve requested enrichment, extraction, scoring, or LLM response fields" in hints
        assert "stage a pipeline_decision interpretation requirement" in hints
        assert "request its review after mutation succeeds" in hints
        assert "naming a sink or output" in hints
        assert "does not clean data" in hints
        assert "If the user already asked to remove, drop, exclude, or avoid saving raw scrape fields" in hints
        assert "that request is the authorization and requirement to add the cleanup field_mapper" in hints
        assert "do not ask whether to add cleanup later" in hints
        assert "use user_term 'drop_raw_html_fields'" in hints
        assert "not permission to omit the cleanup node" in hints

    def test_llm_discovery_recommends_prompt_shield_for_internet_content(self) -> None:
        """LLM plugin hints recommend prompt shield without silently changing topology."""
        state = _empty_state()
        catalog = _mock_catalog()
        result = execute_tool(
            "get_plugin_assistance",
            {
                "plugin_type": "transform",
                "plugin_name": "llm",
            },
            state,
            catalog,
        )
        assert result.success is True
        payload = result.to_dict()["data"]
        assert payload["issue_code"] is None
        hints = " ".join(payload["composer_hints"])
        # run-2 G3 (decided): the advisory covers llms consuming externally-
        # fetched remote content — not "every unshielded llm"; the old hint
        # contradicted the skill and the aids' prompt_shield rule.
        assert "externally-fetched remote content" in hints
        assert "always advisory (never blocking)" in hints
        assert "azure_prompt_shield" in hints
        assert "only when discovery lists it" in hints
        assert "whenever no authorized shield is upstream (State B/C)" in hints
        assert "recommendation is not permission to add a node" in hints
        assert "do not add passthrough, placeholder, no-op, or renamed utility nodes" in hints
        assert "backend automatically stages and surfaces llm_prompt_template" in hints
        assert "do not author or request that review" in hints
        assert "llm_prompt_template" in hints
        assert "scoring scale" in hints
        assert "Measurable adjectives are not exempt" in hints
        assert "over 6 ft" in hints
        assert "top quartile" in hints
        assert "Prompt-template review is not enough" in hints
        assert "put the vague_term requirement in interpretation_requirements before set_pipeline" in hints
        assert "When repairing or upserting an LLM node, repeat the review preflight" in hints
        assert (
            "carry forward existing pending LLM interpretation requirements and add missing vague_term or prompt shield requirements"
            in hints
        )
        assert "If the prompt asks the model to return a score, rating, rank, class, or pass/fail result" in hints
        assert "that output shape is authored judgement semantics when you chose the scale" in hints
        assert "stable user_term preserving the user's criterion phrase" in hints
        assert "not the whole task phrase" in hints
        assert "use the adjective or noun phrase that names the criterion" in hints
        assert "For how <adjective> phrasing, use the adjective itself as user_term" in hints
        assert "only an llm_prompt_template review is incomplete" in hints
        assert "Do not stop by saying the rubric is part of the reviewed prompt" in hints
        assert "required_input_fields names upstream columns" in hints
        assert "schema.fields may declare output types (ADR-050)" in hints
        assert "Never require this node's generated fields as upstream inputs" in hints
        assert "pass-through guarantees come from upstream" in hints
        assert "good_colour_pair_answer / approximate_hex_answer downstream" in hints
        assert "Single-query LLM output is written to response_field" in hints
        assert "Prompt wording alone does not create separate JSON fields" in hints
        assert "Configure single-query output_fields" in hints
        assert "no downstream parser is needed" in hints
        assert "preserve response_field through cleanup" in hints
        assert "preserves upstream row fields while adding response_field" in hints
        assert "does not remove raw scrape fields" in hints
        assert "put a field_mapper cleanup node between the LLM and the sink" in hints
        assert "rate how cool they are" not in hints
        assert "vague_term" in hints
        assert "good, bad" not in hints
        assert "Subjective user terms" not in hints
        assert "do not insert it automatically" in hints
        assert "prompt_injection_shield_recommendation" in hints
        assert "LLM-node reviews stack" in hints
        assert "Interpretation reviews are not transform stages" in hints
        assert "Do not create passthrough, review, recommendation, or placeholder nodes" in hints
        assert "route through azure_content_safety first" not in hints

    def test_llm_post_call_hints_do_not_keyword_scan_subjective_terms(self) -> None:
        """Plugin hints must not decide arbitrary/vague terms by backend word list."""
        from elspeth.plugins.transforms.llm.transform import LLMTransform

        hints = LLMTransform.get_post_call_hints(
            tool_name="upsert_node",
            config_snapshot={
                "prompt_template": "Rate how cool this public web page is on a 1-10 scale.",
                "response_field": "cool_assessment",
            },
        )

        assert hints == ()


# ---------------------------------------------------------------------------
# preview_pipeline tool tests
# ---------------------------------------------------------------------------


def _stage1_valid_preview_state() -> CompositionState:
    """A csv -> csv pipeline that Stage 1 accepts and Stage 3 leaves inert.

    Both proof-stage inputs are non-blob paths, so the only stage that can
    move the published verdict is the runtime preflight.
    """
    return (
        _empty_state()
        .with_source(
            SourceSpec(
                plugin="csv",
                on_success="main",
                options={"path": "/data/blobs/test-session/input.csv", "schema": {"mode": "observed"}},
                on_validation_failure="discard",
            )
        )
        .with_output(
            OutputSpec(
                name="main",
                plugin="csv",
                options={"path": "/data/outputs/test-session/out.csv", "schema": {"mode": "observed"}},
                on_write_failure="discard",
            )
        )
    )


class TestPreviewPipeline:
    def test_preview_empty_pipeline(self) -> None:
        state = _empty_state()
        catalog = _mock_catalog()
        result = execute_tool("preview_pipeline", {}, state, catalog)
        assert result.success is True
        assert result.data["preview_is_valid"] is False
        assert _pipeline_state_default_source(result.data) is None
        assert result.data["node_count"] == 0

    def test_preview_valid_pipeline(self) -> None:
        state = _empty_state()
        catalog = _mock_catalog()
        # Build a minimal valid pipeline
        r1 = execute_tool(
            "set_source",
            {
                "plugin": "csv",
                "on_success": "t1",
                "options": {"path": "/data/in.csv", "schema": {"mode": "observed"}},
                "on_validation_failure": "quarantine",
            },
            state,
            catalog,
        )
        r2 = execute_tool(
            "upsert_node",
            {
                "id": "t1",
                "node_type": "transform",
                "plugin": "passthrough",
                "input": "t1",
                "on_success": "main",
                "options": {"schema": {"mode": "observed"}},
            },
            r1.updated_state,
            catalog,
        )
        r3 = execute_tool(
            "set_output",
            {
                "sink_name": "main",
                "plugin": "csv",
                "options": {"path": "/data/outputs/test-session/output.csv", "schema": {"mode": "observed"}},
            },
            r2.updated_state,
            catalog,
        )
        result = execute_tool("preview_pipeline", {}, r3.updated_state, catalog)
        assert result.success is True
        assert _pipeline_state_default_source(result.data)["plugin"] == "csv"
        assert _pipeline_state_default_source(result.data)["has_schema_config"] is True
        assert result.data["node_count"] == 1
        assert result.data["output_count"] == 1

    def test_preview_pipeline_includes_edge_contracts(self, passing_runtime_preflight: _SyncCallRecorder) -> None:
        """preview_pipeline includes raw edge contract evidence from validation."""
        state = _empty_state()
        catalog = _mock_catalog()
        r1 = execute_tool(
            "set_source",
            {
                "plugin": "csv",
                "on_success": "t1",
                "options": {"path": "/data/in.csv", "schema": {"mode": "fixed", "fields": ["text: str"]}},
                "on_validation_failure": "discard",
            },
            state,
            catalog,
        )
        r2 = execute_tool(
            "upsert_node",
            {
                "id": "t1",
                "node_type": "transform",
                "plugin": "value_transform",
                "input": "t1",
                "on_success": "main",
                "on_error": "discard",
                "options": {
                    "required_input_fields": ["text"],
                    "operations": [{"target": "out", "expression": "row['text']"}],
                    "schema": {"mode": "observed"},
                },
            },
            r1.updated_state,
            catalog,
        )
        r3 = execute_tool(
            "set_output",
            {
                "sink_name": "main",
                "plugin": "csv",
                "options": {"path": "outputs/out.csv", "schema": {"mode": "observed"}},
                "on_write_failure": "discard",
            },
            r2.updated_state,
            catalog,
        )

        result = execute_tool("preview_pipeline", {}, r3.updated_state, catalog, runtime_preflight=passing_runtime_preflight)

        assert result.success is True
        assert "edge_contracts" in result.data
        contracts = result.data["edge_contracts"]
        assert len(contracts) >= 1
        source_to_t1 = next(c for c in contracts if c["to"] == "t1")
        assert source_to_t1["from"] == "source"
        assert "text" in source_to_t1["producer_guarantees"]
        assert "text" in source_to_t1["consumer_requires"]
        assert source_to_t1["satisfied"] is True
        assert result.data["preview_is_valid"] is True

    def test_reserved_connection_names_covers_every_implicit_self_publisher(self) -> None:
        """A repair branch may not be minted onto a name an implicit publisher owns.

        ``_reserved_connection_names`` read ``node.on_success`` directly, so a
        node publishing under its OWN id contributed nothing. The hole only
        opens for a DANGLING one — a consumed publisher's id arrives via its
        consumer's ``input``, and a queue's id is always its own ``input`` —
        which is exactly why it survived: every ordinary pipeline hides it.

        Enumerated FROM ``_IMPLICIT_SELF_PUBLISHING_NODE_TYPES`` so a fourth
        node kind cannot skip this site, and so the partial fix (restating the
        set as coalesce+aggregation) fails here.
        """
        from elspeth.web.composer._producer_resolver import _IMPLICIT_SELF_PUBLISHING_NODE_TYPES, published_success_connection
        from elspeth.web.composer.tools._common import _reserved_connection_names

        for kind in sorted(_IMPLICIT_SELF_PUBLISHING_NODE_TYPES):
            publisher = NodeSpec(
                id="publisher",
                node_type=kind,
                plugin="batch_stats" if kind == "aggregation" else None,
                # A queue's input IS its own id; the others consume upstream.
                input="publisher" if kind == "queue" else "feed_out",
                on_success=None,
                on_error="main" if kind == "aggregation" else None,
                options={"schema": {"mode": "observed"}} if kind == "aggregation" else {},
                condition=None,
                routes=None,
                fork_to=None,
                branches={"only": "feed_out"} if kind == "coalesce" else None,
                policy="all" if kind == "coalesce" else None,
                merge="row_union" if kind == "coalesce" else None,
                **({"trigger": {"count": 2}} if kind == "aggregation" else {}),
            )
            assert published_success_connection(publisher) == "publisher", kind
            state = (
                _empty_state()
                .with_source(
                    SourceSpec(
                        plugin="csv",
                        on_success="rows",
                        options={"path": "/data/in.csv", "schema": {"mode": "observed"}},
                        on_validation_failure="discard",
                    )
                )
                .with_node(
                    NodeSpec(
                        id="feeder",
                        node_type="transform",
                        plugin="passthrough",
                        input="rows",
                        on_success="feed_out",
                        on_error="main",
                        options={"schema": {"mode": "observed"}},
                        condition=None,
                        routes=None,
                        fork_to=None,
                        branches=None,
                        policy=None,
                        merge=None,
                    )
                )
                .with_node(publisher)
                .with_output(OutputSpec(name="main", plugin="json", options={"schema": {"mode": "observed"}}, on_write_failure="discard"))
            )
            assert "publisher" in _reserved_connection_names(state), (
                f"A dangling '{kind}' publishing under its own id is not a reserved connection name, so a "
                "repair branch can be minted onto it. Applying that repair introduces a fresh "
                "duplicate_connection_producer — the planner's own repair breaks the pipeline. "
                "Ask published_success_connection; do not restate the set here."
            )

    def test_duplicate_consumer_repair_does_not_mint_a_branch_onto_a_publisher_id(self) -> None:
        """The consequence, end to end: the repair must not create a new error.

        Measured before the fix on this exact shape — the generated branch
        name collided with the dangling aggregation's id and applying the
        suggestion produced:

            Duplicate producer for connection 'shared_to_t1':
            aggregation 'shared_to_t1' and gate 'fork_shared' fork 'shared_to_t1'

        One error in, a different error out, with the planner having done
        exactly what it was told.
        """
        from elspeth.web.composer.tools._common import _graph_repair_suggestions

        def _passthrough(node_id: str, input_label: str, on_success: str) -> NodeSpec:
            return NodeSpec(
                id=node_id,
                node_type="transform",
                plugin="passthrough",
                input=input_label,
                on_success=on_success,
                on_error="main",
                options={"schema": {"mode": "observed"}},
                condition=None,
                routes=None,
                fork_to=None,
                branches=None,
                policy=None,
                merge=None,
            )

        state = (
            _empty_state()
            .with_source(
                SourceSpec(
                    plugin="csv",
                    on_success="shared",
                    options={"path": "/data/in.csv", "schema": {"mode": "observed"}},
                    on_validation_failure="discard",
                )
            )
            .with_node(_passthrough("t1", "shared", "main"))
            .with_node(_passthrough("t2", "shared", "main"))
            .with_node(_passthrough("feeder", "shared", "agg_in"))
            .with_node(
                NodeSpec(
                    # id deliberately equals the branch name the repair generates
                    id="shared_to_t1",
                    node_type="aggregation",
                    plugin="batch_stats",
                    input="agg_in",
                    on_success=None,
                    on_error="main",
                    options={"schema": {"mode": "observed"}},
                    condition=None,
                    routes=None,
                    fork_to=None,
                    branches=None,
                    policy=None,
                    merge=None,
                    trigger={"count": 2},
                )
            )
            .with_output(OutputSpec(name="main", plugin="json", options={"schema": {"mode": "observed"}}, on_write_failure="discard"))
        )

        validation = state.validate()
        assert any(error.error_code == "duplicate_connection_consumer" for error in validation.errors)

        generated_branches: list[str] = []
        for suggestion in _graph_repair_suggestions(state, validation):
            for tool_call in suggestion["tool_sequence"]:
                branches = tool_call.get("arguments", {}).get("fork_to")
                if branches:
                    generated_branches.extend(branches)

        assert generated_branches, "the duplicate-consumer repair produced no fork branches to check"
        assert "shared_to_t1" not in generated_branches, (
            "The repair minted a branch named 'shared_to_t1', which is already the implicit connection id of "
            f"the dangling aggregation of that name. Generated: {generated_branches}. Applying this repair "
            "creates a duplicate_connection_producer the pipeline did not have."
        )

    def test_preview_pipeline_suggests_fork_gate_for_duplicate_consumers(self) -> None:
        """Duplicate consumers get a copyable fork-gate repair skeleton."""
        state = (
            _empty_state()
            .with_source(
                SourceSpec(
                    plugin="csv",
                    on_success="classified_rows",
                    options={"path": "/data/in.csv", "schema": {"mode": "fixed", "fields": ["text: str"]}},
                    on_validation_failure="quarantine",
                )
            )
            .with_node(
                NodeSpec(
                    id="fraud_filter",
                    node_type="transform",
                    plugin="value_transform",
                    input="classified_rows",
                    on_success="fraud_rows",
                    on_error="discard",
                    options={
                        "operations": [{"target": "fraud_flag", "expression": "row['text']"}],
                        "schema": {"mode": "observed"},
                    },
                    condition=None,
                    routes=None,
                    fork_to=None,
                    branches=None,
                    policy=None,
                    merge=None,
                )
            )
            .with_node(
                NodeSpec(
                    id="regular_filter",
                    node_type="transform",
                    plugin="value_transform",
                    input="classified_rows",
                    on_success="regular_rows",
                    on_error="discard",
                    options={
                        "operations": [{"target": "regular_flag", "expression": "row['text']"}],
                        "schema": {"mode": "observed"},
                    },
                    condition=None,
                    routes=None,
                    fork_to=None,
                    branches=None,
                    policy=None,
                    merge=None,
                )
            )
            .with_output(OutputSpec(name="fraud_rows", plugin="csv", options={"path": "outputs/fraud.csv"}, on_write_failure="discard"))
            .with_output(OutputSpec(name="regular_rows", plugin="csv", options={"path": "outputs/regular.csv"}, on_write_failure="discard"))
        )

        result = execute_tool("preview_pipeline", {}, state, _mock_catalog())
        payload = result.to_dict()["validation"]

        assert result.success is True
        assert any("Duplicate consumer for connection 'classified_rows'" in err["message"] for err in payload["errors"])
        repairs = payload["graph_repair_suggestions"]
        assert len(repairs) == 1
        repair = repairs[0]
        assert repair["code"] == "duplicate_consumer_connection"
        assert repair["connection"] == "classified_rows"
        assert repair["strategy"] == "insert_fork_gate"
        assert repair["tool_sequence"][0]["arguments"]["input"] == "classified_rows_to_fraud_filter"
        assert repair["tool_sequence"][1]["arguments"]["input"] == "classified_rows_to_regular_filter"
        assert repair["tool_sequence"][2] == {
            "tool": "upsert_node",
            "arguments": {
                "id": "fork_classified_rows",
                "node_type": "gate",
                "plugin": None,
                "input": "classified_rows",
                "on_success": None,
                "on_error": None,
                "options": {},
                "condition": "True",
                "routes": {"true": "fork", "false": "fork"},
                "fork_to": ["classified_rows_to_fraud_filter", "classified_rows_to_regular_filter"],
                "branches": None,
                "policy": None,
                "merge": None,
                "trigger": None,
                "output_mode": None,
                "expected_output_count": None,
            },
        }
        assert repair["tool_sequence"][-1] == {"tool": "preview_pipeline", "arguments": {}}

        fixed_state = state
        for step in repair["tool_sequence"][:-1]:
            step_result = execute_tool(step["tool"], step["arguments"], fixed_state, _mock_catalog())
            fixed_state = step_result.updated_state
        fixed_validation = execute_tool("preview_pipeline", {}, fixed_state, _mock_catalog()).to_dict()["validation"]

        assert not any("Duplicate consumer for connection 'classified_rows'" in err["message"] for err in fixed_validation["errors"])

    @pytest.mark.parametrize("duplicate_branch", ["control", "treatment"])
    def test_duplicate_consumer_repair_patches_row_union_branch_and_validates(self, duplicate_branch: str) -> None:
        """Every suggested mutation succeeds and repairs the real branch slot.

        The deliberately minimal fork skeleton does not synthesize an inner
        correlated barrier. It therefore leaves the pre-existing independent
        fork-destination/topology issue visible for the planner's next repair
        turn instead of silently changing downstream semantics.
        """
        branch_connections = {
            "control": "control_done",
            "treatment": "treatment_done",
        }
        duplicate_connection = branch_connections[duplicate_branch]
        state = (
            _empty_state()
            .with_source(
                SourceSpec(
                    plugin="csv",
                    on_success="rows",
                    options={"path": "/data/in.csv", "schema": {"mode": "observed"}},
                    on_validation_failure="discard",
                )
            )
            .with_node(
                NodeSpec(
                    id="fan_out",
                    node_type="gate",
                    plugin=None,
                    input="rows",
                    on_success=None,
                    on_error=None,
                    options={},
                    condition="True",
                    routes={"true": "fork", "false": "fork"},
                    fork_to=("control", "treatment"),
                    branches=None,
                    policy=None,
                    merge=None,
                )
            )
            .with_node(
                NodeSpec(
                    id="control_path",
                    node_type="transform",
                    plugin="passthrough",
                    input="control",
                    on_success="control_done",
                    on_error="discard",
                    options={"schema": {"mode": "observed"}},
                    condition=None,
                    routes=None,
                    fork_to=None,
                    branches=None,
                    policy=None,
                    merge=None,
                )
            )
            .with_node(
                NodeSpec(
                    id="treatment_path",
                    node_type="transform",
                    plugin="passthrough",
                    input="treatment",
                    on_success="treatment_done",
                    on_error="discard",
                    options={"schema": {"mode": "observed"}},
                    condition=None,
                    routes=None,
                    fork_to=None,
                    branches=None,
                    policy=None,
                    merge=None,
                )
            )
            .with_node(
                NodeSpec(
                    id="variant_union",
                    node_type="row_union",
                    plugin=None,
                    input="control_done",
                    on_success="unioned_rows",
                    on_error=None,
                    options={},
                    condition=None,
                    routes=None,
                    fork_to=None,
                    branches=branch_connections,
                    policy=None,
                    merge=None,
                )
            )
            .with_node(
                NodeSpec(
                    id="duplicate_reader",
                    node_type="transform",
                    plugin="passthrough",
                    input=duplicate_connection,
                    on_success="duplicate_rows",
                    on_error="discard",
                    options={"schema": {"mode": "observed"}},
                    condition=None,
                    routes=None,
                    fork_to=None,
                    branches=None,
                    policy=None,
                    merge=None,
                )
            )
            .with_node(
                NodeSpec(
                    id="consume_union",
                    node_type="transform",
                    plugin="passthrough",
                    input="unioned_rows",
                    on_success="main",
                    on_error="discard",
                    options={"schema": {"mode": "observed"}},
                    condition=None,
                    routes=None,
                    fork_to=None,
                    branches=None,
                    policy=None,
                    merge=None,
                )
            )
            .with_output(
                OutputSpec(
                    name="main",
                    plugin="json",
                    options={"path": "outputs/main.json", "schema": {"mode": "observed"}},
                    on_write_failure="discard",
                )
            )
            .with_output(
                OutputSpec(
                    name="duplicate_rows",
                    plugin="json",
                    options={"path": "outputs/duplicate.json", "schema": {"mode": "observed"}},
                    on_write_failure="discard",
                )
            )
        )

        preview = execute_tool("preview_pipeline", {}, state, _mock_catalog())
        repair = preview.to_dict()["validation"]["graph_repair_suggestions"][0]
        union_step = next(
            step for step in repair["tool_sequence"] if step["tool"] == "upsert_node" and step["arguments"]["id"] == "variant_union"
        )
        repaired_branches = union_step["arguments"]["branches"]
        repaired_connection = repaired_branches[duplicate_branch]

        assert repaired_connection != duplicate_connection
        assert union_step["arguments"]["input"] == (repaired_connection if duplicate_branch == "control" else "control_done")

        repaired_state = state
        for step in repair["tool_sequence"][:-1]:
            step_result = execute_tool(step["tool"], dict(step["arguments"]), repaired_state, _mock_catalog())
            assert step_result.success is True, step_result.to_dict()
            repaired_state = step_result.updated_state

        repaired_preview = execute_tool("preview_pipeline", {}, repaired_state, _mock_catalog())
        remaining_codes = {entry.error_code for entry in repaired_preview.validation.errors}
        assert "duplicate_connection_consumer" not in remaining_codes
        assert "row_union_input_mismatch" not in remaining_codes
        assert "fork_branch_no_destination" in remaining_codes

    def test_duplicate_consumer_repair_excludes_row_union_identity_branch(self) -> None:
        """Identity barrier branches are direct COPY edges, not connection consumers."""

        def _consumer(node_id: str) -> NodeSpec:
            return NodeSpec(
                id=node_id,
                node_type="transform",
                plugin="passthrough",
                input="control",
                on_success=f"{node_id}_out",
                on_error="discard",
                options={"schema": {"mode": "observed"}},
                condition=None,
                routes=None,
                fork_to=None,
                branches=None,
                policy=None,
                merge=None,
            )

        state = (
            _empty_state()
            .with_node(_consumer("ordinary_a"))
            .with_node(_consumer("ordinary_b"))
            .with_node(
                NodeSpec(
                    id="variant_union",
                    node_type="row_union",
                    plugin=None,
                    input="control",
                    on_success="unioned_rows",
                    on_error=None,
                    options={},
                    condition=None,
                    routes=None,
                    fork_to=None,
                    branches={"control": "control", "treatment": "treatment"},
                    policy=None,
                    merge=None,
                )
            )
        )

        preview_validation = execute_tool("preview_pipeline", {}, state, _mock_catalog()).to_dict()["validation"]
        duplicate_error = next(entry for entry in preview_validation["errors"] if entry["error_code"] == "duplicate_connection_consumer")
        assert "ordinary_a" in duplicate_error["message"]
        assert "ordinary_b" in duplicate_error["message"]
        assert "variant_union" not in duplicate_error["message"]

        repair = next(entry for entry in preview_validation["graph_repair_suggestions"] if entry["connection"] == "control")
        assert [consumer["id"] for consumer in repair["affected_consumers"]] == [
            "ordinary_a",
            "ordinary_b",
        ]
        assert [step["arguments"]["id"] for step in repair["tool_sequence"] if step["tool"] == "upsert_node"] == [
            "ordinary_a",
            "ordinary_b",
            "fork_control",
        ]

    def test_duplicate_consumer_repair_patches_one_node_once_for_two_branch_bindings(self) -> None:
        """Two aliases of one row_union sharing a connection collapse to one upsert.

        A repair skeleton rebuilt from the original node per binding emits two
        ``upsert_node`` calls for the same id, and the second reverts the first
        — applying the suggested sequence leaves the duplicate in place. The
        patches must accumulate onto one payload per node id while the fork
        gate still publishes one distinct branch per binding.
        """
        state = (
            _empty_state()
            .with_source(
                SourceSpec(
                    plugin="csv",
                    on_success="rows",
                    options={"path": "/data/in.csv", "schema": {"mode": "observed"}},
                    on_validation_failure="discard",
                )
            )
            .with_node(
                NodeSpec(
                    id="fan_out",
                    node_type="gate",
                    plugin=None,
                    input="rows",
                    on_success=None,
                    on_error=None,
                    options={},
                    condition="True",
                    routes={"true": "fork", "false": "fork"},
                    fork_to=("control", "treatment"),
                    branches=None,
                    policy=None,
                    merge=None,
                )
            )
            .with_node(
                NodeSpec(
                    id="control_path",
                    node_type="transform",
                    plugin="passthrough",
                    input="control",
                    on_success="control_done",
                    on_error="discard",
                    options={"schema": {"mode": "observed"}},
                    condition=None,
                    routes=None,
                    fork_to=None,
                    branches=None,
                    policy=None,
                    merge=None,
                )
            )
            .with_node(
                NodeSpec(
                    id="treatment_path",
                    node_type="transform",
                    plugin="passthrough",
                    input="treatment",
                    on_success="treatment_done",
                    on_error="discard",
                    options={"schema": {"mode": "observed"}},
                    condition=None,
                    routes=None,
                    fork_to=None,
                    branches=None,
                    policy=None,
                    merge=None,
                )
            )
            # Inner barrier: its release connection is downstream of BOTH fork
            # branches, so the outer row_union can bind both aliases to it.
            .with_node(
                NodeSpec(
                    id="inner_union",
                    node_type="row_union",
                    plugin=None,
                    input="control_done",
                    on_success="union_out",
                    on_error=None,
                    options={},
                    condition=None,
                    routes=None,
                    fork_to=None,
                    branches={"control": "control_done", "treatment": "treatment_done"},
                    policy=None,
                    merge=None,
                )
            )
            .with_node(
                NodeSpec(
                    id="outer_union",
                    node_type="row_union",
                    plugin=None,
                    input="union_out",
                    on_success="union_out2",
                    on_error=None,
                    options={},
                    condition=None,
                    routes=None,
                    fork_to=None,
                    # BOTH aliases claim the one connection: one node, two bindings.
                    branches={"control": "union_out", "treatment": "union_out"},
                    policy=None,
                    merge=None,
                )
            )
            .with_node(
                NodeSpec(
                    id="consume_union",
                    node_type="transform",
                    plugin="passthrough",
                    input="union_out2",
                    on_success="main",
                    on_error="discard",
                    options={"schema": {"mode": "observed"}},
                    condition=None,
                    routes=None,
                    fork_to=None,
                    branches=None,
                    policy=None,
                    merge=None,
                )
            )
            .with_output(
                OutputSpec(
                    name="main",
                    plugin="json",
                    options={"path": "outputs/main.json", "schema": {"mode": "observed"}},
                    on_write_failure="discard",
                )
            )
        )

        preview_validation = execute_tool("preview_pipeline", {}, state, _mock_catalog()).to_dict()["validation"]
        assert "duplicate_connection_consumer" in {entry["error_code"] for entry in preview_validation["errors"]}
        repair = next(entry for entry in preview_validation["graph_repair_suggestions"] if entry["connection"] == "union_out")

        upsert_ids = [step["arguments"]["id"] for step in repair["tool_sequence"] if step["tool"] == "upsert_node"]
        assert len(upsert_ids) == len(set(upsert_ids)), repair["tool_sequence"]
        assert upsert_ids.count("outer_union") == 1

        union_step = next(step for step in repair["tool_sequence"] if step["arguments"].get("id") == "outer_union")
        patched_branches = union_step["arguments"]["branches"]
        assert patched_branches["control"] != patched_branches["treatment"]
        assert "union_out" not in patched_branches.values()
        assert union_step["arguments"]["input"] == patched_branches["control"]

        gate_step = repair["tool_sequence"][-2]
        assert gate_step["arguments"]["node_type"] == "gate"
        assert sorted(gate_step["arguments"]["fork_to"]) == sorted(patched_branches.values())
        assert repair["tool_sequence"][-1] == {"tool": "preview_pipeline", "arguments": {}}

        repaired_state = state
        for step in repair["tool_sequence"][:-1]:
            step_result = execute_tool(step["tool"], dict(step["arguments"]), repaired_state, _mock_catalog())
            assert step_result.success is True, step_result.to_dict()
            repaired_state = step_result.updated_state

        repaired_preview = execute_tool("preview_pipeline", {}, repaired_state, _mock_catalog())
        remaining_codes = {entry.error_code for entry in repaired_preview.validation.errors}
        assert "duplicate_connection_consumer" not in remaining_codes
        assert "row_union_input_mismatch" not in remaining_codes

    def test_preview_source_with_schema_config_field_name(self) -> None:
        state = _empty_state().with_source(
            SourceSpec(
                plugin="csv",
                on_success="t1",
                options={"path": "/data/in.csv", "schema_config": {"mode": "observed"}},
                on_validation_failure="quarantine",
            )
        )
        catalog = _mock_catalog()

        result = execute_tool("preview_pipeline", {}, state, catalog)

        assert result.success is True
        assert _pipeline_state_default_source(result.data)["plugin"] == "csv"
        assert _pipeline_state_default_source(result.data)["has_schema_config"] is True

    def test_preview_pipeline_surfaces_runtime_preflight_failure(self) -> None:
        state = _stage1_valid_preview_state()
        catalog = _mock_catalog()
        runtime_preflight = _SyncCallRecorder(
            ValidationResult(
                is_valid=False,
                checks=[
                    ValidationCheck(
                        name="settings_load",
                        passed=False,
                        detail="Forbidden name: 'end_of_source'",
                        affected_nodes=(),
                        outcome_code=None,
                    )
                ],
                errors=[
                    ValidationError(
                        component_id="agg1",
                        component_type="aggregation",
                        message="Forbidden name: 'end_of_source'",
                        suggestion="Omit trigger for end-of-source-only aggregation.",
                        error_code=None,
                    )
                ],
                readiness=ValidationReadiness(authoring_valid=False, execution_ready=False, completion_ready=False, blockers=[]),
            )
        )

        result = execute_tool(
            "preview_pipeline",
            {},
            state,
            catalog,
            data_dir="/data",
            runtime_preflight=runtime_preflight,
        )

        assert result.success is True
        assert result.runtime_preflight is not None
        assert result.validation.is_valid is True
        assert result.runtime_preflight.is_valid is False
        assert result.data["preview_is_valid"] is False
        assert result.runtime_preflight.errors[0].message == "Forbidden name: 'end_of_source'"
        # The preflight rides ONLY on the envelope field; no nested twin.
        assert "runtime_preflight" not in result.data
        runtime_preflight.assert_called_once_with(state)

    def test_preview_pipeline_without_runtime_preflight_keeps_the_authoring_verdict_on_the_envelope(self) -> None:
        state = _stage1_valid_preview_state()

        result = execute_tool(
            "preview_pipeline",
            {},
            state,
            _mock_catalog(),
            data_dir="/data",
            runtime_preflight=None,
        )

        assert result.success is True
        assert result.runtime_preflight is None
        assert result.validation.is_valid is True
        assert "runtime_preflight" not in result.data
        assert result.data["preview_is_valid"] is False

    def test_preview_pipeline_absent_runtime_preflight_fails_closed(self) -> None:
        """An un-run Stage 2 may not ride the success side of the conjunct.

        Mutation caught: restoring the silent-drop conjunct
        (``if runtime_result is not None: is_valid = is_valid and ...``)
        republishes a bare ``true`` for a pipeline nothing ran the runtime
        checks against, and the marker disappears.
        """
        state = _stage1_valid_preview_state()

        result = execute_tool(
            "preview_pipeline",
            {},
            state,
            _mock_catalog(),
            data_dir="/data",
            runtime_preflight=None,
        )

        assert result.success is True
        assert result.validation.is_valid is True
        assert result.data["preview_is_valid"] is False
        assert "runtime_preflight" not in result.data

        markers = [entry for entry in result.data["preview_errors"] if entry.get("error_code") == "runtime_preflight_not_run"]
        assert len(markers) == 1, result.data["preview_errors"]
        assert markers[0]["severity"] == "high"
        assert "did not run" in markers[0]["message"]

        # The authoring check's own report stays true to itself: the marker
        # is a preview-stage entry under ``data``, never an entry on the
        # envelope's ``validation``.
        authoring_codes = [entry.error_code for entry in result.validation.errors]
        assert "runtime_preflight_not_run" not in authoring_codes

    def test_preview_pipeline_runtime_conjunct_three_directions(
        self,
        passing_runtime_preflight: _SyncCallRecorder,
    ) -> None:
        """Wired-pass, wired-fail, and absent must stay three distinct verdicts.

        Mutation caught: any future 'unification' that folds the absent case
        into either wired semantics — e.g.
        ``is_valid and (runtime_result is None or runtime_result.is_valid)``
        (absent collapses into pass) or dropping the marker while keeping
        ``is_valid=False`` (absent collapses into wired-fail, indistinguishable
        from a real runtime rejection) — breaks at least one arm loudly.
        """
        state = _stage1_valid_preview_state()
        failing_preflight = _SyncCallRecorder(
            ValidationResult(
                is_valid=False,
                checks=[
                    ValidationCheck(
                        name="path_allowlist",
                        passed=False,
                        detail="Path traversal blocked: sink 'main'",
                        affected_nodes=(),
                        outcome_code=None,
                    )
                ],
                errors=[
                    ValidationError(
                        component_id="main",
                        component_type="sink",
                        message="Path traversal blocked: sink 'main'",
                        suggestion="Write inside the configured data directory.",
                        error_code=None,
                    )
                ],
                readiness=ValidationReadiness(authoring_valid=True, execution_ready=False, completion_ready=False, blockers=[]),
            )
        )

        def preview(runtime_preflight: _SyncCallRecorder | None) -> dict[str, Any]:
            result = execute_tool(
                "preview_pipeline",
                {},
                state,
                _mock_catalog(),
                data_dir="/data",
                runtime_preflight=runtime_preflight,
            )
            assert result.success is True
            return result.to_dict()

        wired_pass = preview(passing_runtime_preflight)
        wired_fail = preview(failing_preflight)
        absent = preview(None)

        def marker_codes(envelope: dict[str, Any]) -> list[str]:
            return [
                entry.get("error_code")
                for entry in envelope["data"]["preview_errors"]
                if entry.get("error_code") == "runtime_preflight_not_run"
            ]

        # The preflight is an envelope field, never a nested twin under data.
        assert all("runtime_preflight" not in envelope["data"] for envelope in (wired_pass, wired_fail, absent))

        assert wired_pass["data"]["preview_is_valid"] is True
        assert wired_pass["runtime_preflight"]["is_valid"] is True
        assert marker_codes(wired_pass) == []

        assert wired_fail["data"]["preview_is_valid"] is False
        assert wired_fail["runtime_preflight"]["is_valid"] is False
        assert marker_codes(wired_fail) == []

        assert absent["data"]["preview_is_valid"] is False
        assert "runtime_preflight" not in absent
        assert marker_codes(absent) == ["runtime_preflight_not_run"]

    def test_preview_data_carries_exactly_the_preview_stage_facts(
        self,
        passing_runtime_preflight: _SyncCallRecorder,
    ) -> None:
        """``data`` is the preview stage's own facts and the overview — nothing the envelope already carries.

        Mutation caught: re-hoisting the authoring verdict under ``data``
        (``is_valid`` / ``errors`` / ``warnings`` / ``suggestions`` /
        ``semantic_contracts`` / ``graph_repair_suggestions``, or a nested
        ``authoring_validation`` copy) puts byte-twins of the envelope's own
        ``validation`` back on the wire under a second name, and revives the
        ``is_valid`` homonym (the 3-stage conjunct wearing the authoring
        verdict's key). The key set is exact so an addition is a deliberate
        teaching change, not a drift (elspeth-e405ad7cd2 R4).
        """
        state = _stage1_valid_preview_state()

        result = execute_tool(
            "preview_pipeline",
            {},
            state,
            _mock_catalog(),
            data_dir="/data",
            runtime_preflight=passing_runtime_preflight,
        )
        assert result.success is True
        assert frozenset(result.data) == _PREVIEW_DATA_KEYS
        assert "is_valid" not in result.data
        assert "authoring_validation" not in result.data
        assert "errors" not in result.data

        with_structural = execute_tool(
            "preview_pipeline",
            {},
            state,
            _mock_catalog(),
            data_dir="/data",
            runtime_preflight=passing_runtime_preflight,
            structural_preflight=_SyncCallRecorder(_tolerant_result(valid=True)),
        )
        assert with_structural.success is True
        assert frozenset(with_structural.data) == _PREVIEW_DATA_KEYS | {"structural_preview"}

    def test_preview_errors_is_only_the_preview_stage(
        self,
        passing_runtime_preflight: _SyncCallRecorder,
    ) -> None:
        """``preview_errors`` carries entries the preview stage itself mints, never authoring errors.

        Mutation caught: seeding ``preview_errors`` from the authoring
        errors (the pre-R4 ``summary_errors = authoring errors + marker``
        shape) makes every authoring error appear twice on the wire —
        once under ``validation.errors`` and once under ``data`` — and
        hides which entries the envelope's ``validation`` does NOT know.
        """
        # An empty pipeline is authoring-invalid: at least one error rides on
        # the envelope's own validation, and none of them may leak into data.
        state = _empty_state()

        absent = execute_tool("preview_pipeline", {}, state, _mock_catalog(), data_dir="/data", runtime_preflight=None)
        assert absent.success is True
        assert absent.validation.is_valid is False
        assert len(absent.validation.errors) >= 1
        assert [entry["error_code"] for entry in absent.data["preview_errors"]] == ["runtime_preflight_not_run"]
        assert absent.data["preview_errors"][0]["severity"] == "high"
        assert "did not run" in absent.data["preview_errors"][0]["message"]
        authoring_codes = {entry.error_code for entry in absent.validation.errors}
        assert "runtime_preflight_not_run" not in authoring_codes

        wired = execute_tool(
            "preview_pipeline",
            {},
            state,
            _mock_catalog(),
            data_dir="/data",
            runtime_preflight=passing_runtime_preflight,
        )
        assert wired.success is True
        assert wired.validation.is_valid is False
        assert list(wired.data["preview_errors"]) == []

    def test_preview_is_valid_keeps_the_authoring_leg_of_the_conjunct(
        self,
        passing_runtime_preflight: _SyncCallRecorder,
    ) -> None:
        """Authoring INVALID with the runtime stage wired-and-passing and no blocking proof still yields ``preview_is_valid`` false.

        Mutation caught: ``preview_is_valid = True`` in place of
        ``validation.is_valid`` (dropping the authoring leg of the three-stage
        conjunct). Every other authoring-invalid pin runs with the runtime
        callback absent, where the un-run branch forces the conjunct false
        regardless of the authoring verdict — so only this three-leg state
        (authoring the sole mover) can see the leg go missing
        (elspeth-e405ad7cd2 R4-fix1).
        """
        state = _empty_state()

        result = execute_tool(
            "preview_pipeline",
            {},
            state,
            _mock_catalog(),
            data_dir="/data",
            runtime_preflight=passing_runtime_preflight,
        )
        assert result.success is True
        # The two other legs are green: the runtime verdict rode through and
        # nothing in the source proof blocks.
        assert result.runtime_preflight is not None
        assert result.runtime_preflight.is_valid is True
        assert [d for d in result.data["proof_diagnostics"] if d["severity"] == "blocking"] == []
        assert list(result.data["preview_errors"]) == []
        # Authoring is the only red leg, and it alone must fail the conjunct.
        assert result.validation.is_valid is False
        assert result.data["preview_is_valid"] is False


_PREVIEW_DATA_KEYS = frozenset(
    {
        "preview_is_valid",
        "preview_errors",
        "edge_contracts",
        "proof_diagnostics",
        "sources",
        "node_count",
        "output_count",
        "nodes",
        "outputs",
    }
)
"""The exact ``preview_pipeline`` ``data`` key set when a runtime check is wired (``structural_preview`` joins when its callback is)."""


def _handoff_strict_preflight() -> _SyncCallRecorder:
    """A strict Stage-2 verdict in the pending-review handoff shape."""
    return _SyncCallRecorder(
        ValidationResult(
            is_valid=False,
            checks=[
                ValidationCheck(
                    name="interpretation_review",
                    passed=False,
                    detail="Interpretation review pending.",
                    affected_nodes=(),
                    outcome_code=None,
                )
            ],
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
                        component_id="summarize",
                        component_type="transform",
                        detail="1 pending interpretation review.",
                    )
                ],
            ),
        )
    )


def _tolerant_result(*, valid: bool) -> ValidationResult:
    if valid:
        return ValidationResult(
            is_valid=True,
            checks=[ValidationCheck(name="graph_structure", passed=True, detail="Graph OK.", affected_nodes=(), outcome_code=None)],
            errors=[],
            readiness=ValidationReadiness(authoring_valid=True, execution_ready=True, completion_ready=True, blockers=[]),
        )
    return ValidationResult(
        is_valid=False,
        checks=[
            ValidationCheck(
                name="plugin_instantiation",
                passed=False,
                detail="llm node 'summarize' rejected its configuration.",
                affected_nodes=("summarize",),
                outcome_code=None,
            ),
            # A downstream stage skipped after the failure above: derived
            # noise the projection must drop.
            ValidationCheck(
                name="graph_structure",
                passed=False,
                detail="Skipped after failure.",
                affected_nodes=(),
                outcome_code="validation.skipped_after_failure",
            ),
        ],
        errors=[
            ValidationError(
                component_id="edge:summarize->main",
                component_type="edge",
                message="consumer requires ['colour'], producer guarantees (none)",
                suggestion="Declare the field on the producer.",
                error_code=None,
            )
        ],
        readiness=ValidationReadiness(authoring_valid=True, execution_ready=False, completion_ready=False, blockers=[]),
    )


def _pending_prompt_template_llm_node() -> NodeSpec:
    """An llm node whose prompt parts carry a PENDING interpretation ref.

    ``materialize_state_for_authoring`` must substitute placeholder text for
    the pending ref, so a state carrying this node materializes to a
    DIFFERENT object — the ``masking_applied=True`` arm.
    """
    return NodeSpec(
        id="summarize",
        node_type="transform",
        plugin="llm",
        input="summarize",
        on_success="main",
        on_error="discard",
        options={
            "profile": "default",
            "required_input_fields": ["colour"],
            "prompt_template_parts": [
                {"kind": "text", "text": "Rate {{ row.colour }} as "},
                {"kind": "interpretation_ref", "requirement_id": "req-1"},
            ],
            "interpretation_requirements": [
                {
                    "id": "req-1",
                    "kind": "vague_term",
                    "user_term": "cool",
                    "status": "pending",
                    "draft": "a 1-10 coolness scale",
                }
            ],
            "schema": {"mode": "observed"},
        },
        condition=None,
        routes=None,
        fork_to=None,
        branches=None,
        policy=None,
        merge=None,
    )


class TestPreviewPipelineStructuralPreview:
    """The additive ``data["structural_preview"]`` block (elspeth-229e9e8195).

    Four handler branches: no callback → block absent; tolerant green →
    block with empty findings; tolerant red with ``masking_applied=False``
    → "equivalent" framing; tolerant red with ``masking_applied=True`` →
    "provisional" report-don't-blindly-repair framing. ``is_valid`` stays
    the strict conjunct in every branch — the block never joins it.
    """

    def _preview(
        self,
        state: CompositionState,
        *,
        structural_preflight: _SyncCallRecorder | None,
    ) -> dict[str, Any]:
        result = execute_tool(
            "preview_pipeline",
            {},
            state,
            _mock_catalog(),
            data_dir="/data",
            runtime_preflight=_handoff_strict_preflight(),
            structural_preflight=structural_preflight,
        )
        assert result.success is True
        return result.data

    def test_no_structural_callback_leaves_block_absent(self) -> None:
        data = self._preview(_stage1_valid_preview_state(), structural_preflight=None)
        assert "structural_preview" not in data
        assert data["preview_is_valid"] is False  # strict handoff verdict governs

    def test_tolerant_green_yields_block_with_empty_findings(self) -> None:
        data = self._preview(
            _stage1_valid_preview_state(),
            structural_preflight=_SyncCallRecorder(_tolerant_result(valid=True)),
        )
        block = data["structural_preview"]
        assert block["is_valid"] is True
        assert list(block["failing_checks"]) == []
        assert list(block["errors"]) == []
        # No prompt-bearing node: masking was a no-op, findings are exact.
        assert block["masking_applied"] is False
        assert block["confidence"] == "equivalent"
        # The strict conjunct is untouched by a green tolerant pass.
        assert data["preview_is_valid"] is False

    def test_tolerant_red_without_masking_is_framed_equivalent(self) -> None:
        data = self._preview(
            _stage1_valid_preview_state(),
            structural_preflight=_SyncCallRecorder(_tolerant_result(valid=False)),
        )
        block = data["structural_preview"]
        assert block["is_valid"] is False
        assert block["masking_applied"] is False
        assert block["confidence"] == "equivalent"
        assert [check["name"] for check in block["failing_checks"]] == ["plugin_instantiation"]
        assert block["errors"][0]["message"] == "consumer requires ['colour'], producer guarantees (none)"
        # The skipped-after-failure stamp is derived noise and must be dropped.
        assert all(check["outcome_code"] != "validation.skipped_after_failure" for check in block["failing_checks"])
        assert data["preview_is_valid"] is False

    def test_tolerant_red_with_masking_is_framed_provisional(self) -> None:
        """The ticket's false-positive shape: a pending prompt-template ref.

        ``materialize_state_for_authoring`` substitutes placeholder text for
        the pending interpretation ref, so a tolerant red at
        ``plugin_instantiation`` may be an artifact of the placeholder — the
        block must say so and instruct report-not-repair.
        """
        from elspeth.web.interpretation_state import materialize_state_for_authoring

        state = _stage1_valid_preview_state().with_node(_pending_prompt_template_llm_node())
        # Premise check: this state genuinely materializes to a new object.
        assert materialize_state_for_authoring(state) is not state

        data = self._preview(state, structural_preflight=_SyncCallRecorder(_tolerant_result(valid=False)))
        block = data["structural_preview"]
        assert block["is_valid"] is False
        assert block["masking_applied"] is True
        assert block["confidence"] == "provisional"
        assert "placeholder" in block["note"]
        assert "Report" in block["note"]
        assert [check["name"] for check in block["failing_checks"]] == ["plugin_instantiation"]
        assert data["preview_is_valid"] is False

    def test_masking_noop_state_is_framed_equivalent_with_prompt_node(self) -> None:
        """A resolved-parts prompt renders identically → identical state object."""
        from dataclasses import replace as dc_replace

        from elspeth.web.interpretation_state import materialize_state_for_authoring

        node = _pending_prompt_template_llm_node()
        resolved_options = dict(node.options)
        resolved_options["interpretation_requirements"] = [
            {
                "id": "req-1",
                "kind": "vague_term",
                "user_term": "cool",
                "status": "resolved",
                "accepted_value": "a 1-10 coolness scale",
            }
        ]
        resolved_options["prompt_template"] = "Rate {{ row.colour }} as a 1-10 coolness scale"
        resolved_node = dc_replace(node, options=resolved_options)
        state = _stage1_valid_preview_state().with_node(resolved_node)
        assert materialize_state_for_authoring(state) is state

        data = self._preview(state, structural_preflight=_SyncCallRecorder(_tolerant_result(valid=False)))
        assert data["structural_preview"]["masking_applied"] is False
        assert data["structural_preview"]["confidence"] == "equivalent"


class TestPrevalidatePluginOptions:
    """Direct unit tests for _prevalidate_plugin_options.

    Covers the 6 code paths identified in the architecture review:
    structured rejection (unknown plugin), success, config error with prefix stripping,
    injected_fields merge, MappingProxyType deep-thaw, and ValueError surfacing.
    Also covers the absence-is-evidence contract: missing required fields (like
    path) must produce validation errors, not be papered over by placeholders.
    """

    def test_valid_options_returns_none(self) -> None:
        """Valid config returns None (no pre-validation error)."""
        result = _prevalidate_plugin_options(
            "transform",
            "passthrough",
            {"schema": {"mode": "observed"}},
        )
        assert result is None

    def test_invalid_options_returns_error_string(self) -> None:
        """Missing required field returns a descriptive error string."""
        result = _prevalidate_plugin_options(
            "transform",
            "passthrough",
            {},  # missing required 'schema'
        )
        assert result is not None
        assert result.startswith("Invalid options for transform 'passthrough':")

    def test_unknown_transform_plugin_returns_actionable_error(self) -> None:
        """Unregistered transform name surfaces as an actionable error string."""
        result = _prevalidate_plugin_options(
            "transform",
            "this_plugin_does_not_exist",
            {"some": "options"},
        )
        assert result is not None
        assert "this_plugin_does_not_exist" in result
        assert "transform" in result.lower()
        assert "list_transforms" in result

    def test_unknown_source_plugin_returns_actionable_error(self) -> None:
        """Unregistered source name surfaces as an actionable error string."""
        result = _prevalidate_plugin_options(
            "source",
            "no_such_source_plugin",
            {"path": "/data/blobs/test-session/in.csv"},
        )
        assert result is not None
        assert "no_such_source_plugin" in result
        assert "source" in result.lower()
        assert "list_sources" in result

    def test_unknown_sink_plugin_returns_actionable_error(self) -> None:
        """Unregistered sink name surfaces as an actionable error string."""
        result = _prevalidate_plugin_options(
            "sink",
            "no_such_sink_plugin",
            {"path": "/data/outputs/test-session/out.csv"},
        )
        assert result is not None
        assert "no_such_sink_plugin" in result
        assert "sink" in result.lower()
        assert "list_sinks" in result

    def test_injected_fields_satisfy_required_options(self) -> None:
        """Injected fields are merged in for validation only — not stored in state."""
        # csv source requires on_validation_failure + path (injected) plus schema (in options)
        result = _prevalidate_plugin_options(
            "source",
            "csv",
            {"schema": {"mode": "observed"}},
            injected_fields={"on_validation_failure": "discard", "path": "/tmp/test.csv"},
        )
        assert result is None

    def test_frozen_mappingproxy_options_are_thawed(self) -> None:
        """MappingProxyType options from CompositionState are deep-thawed before Pydantic sees them."""
        from types import MappingProxyType

        frozen_options = MappingProxyType({"schema": MappingProxyType({"mode": "observed"})})
        result = _prevalidate_plugin_options(
            "transform",
            "passthrough",
            frozen_options,  # type: ignore[arg-type]
        )
        assert result is None

    def test_config_class_prefix_stripped_from_error(self) -> None:
        """'Invalid configuration for XConfig:' prefix is stripped so the LLM sees only the problem."""
        result = _prevalidate_plugin_options(
            "transform",
            "passthrough",
            {},  # missing required 'schema'
        )
        assert result is not None
        # Internal class name should not appear — LLM gets the validation detail only
        assert "Invalid configuration for PassThroughConfig" not in result
        assert result.startswith("Invalid options for transform 'passthrough':")

    def test_llm_unknown_provider_surfaced_not_swallowed(self) -> None:
        """ValueError from get_config_model (unknown LLM provider) becomes an error, not silent None."""
        result = _prevalidate_plugin_options(
            "transform",
            "llm",
            {"provider": "nonexistent_provider", "schema": {"mode": "observed"}},
        )
        assert result is not None
        assert result.startswith("Invalid options for transform 'llm':")
        assert "nonexistent_provider" in result

    def test_llm_valid_provider_missing_required_fields_surfaces_errors(self) -> None:
        """Valid provider with missing required fields reports them — not silent None.

        Verifies Phase 2 of LLM dispatch: after provider resolution succeeds,
        the provider-specific Pydantic model validates required fields. Without
        this test, a regression that returns the base LLMConfig (which lacks
        deployment_name/endpoint) instead of AzureOpenAIConfig would be invisible.
        """
        result = _prevalidate_plugin_options(
            "transform",
            "llm",
            {"provider": "azure", "schema": {"mode": "observed"}},
        )
        assert result is not None
        assert result.startswith("Invalid options for transform 'llm':")
        # Azure-specific required fields must be reported
        assert "deployment_name" in result
        assert "endpoint" in result
        assert "api_key" in result

    def test_llm_openrouter_missing_required_fields_surfaces_errors(self) -> None:
        """OpenRouter with missing required fields reports provider-specific missing fields."""
        result = _prevalidate_plugin_options(
            "transform",
            "llm",
            {"provider": "openrouter", "schema": {"mode": "observed"}},
        )
        assert result is not None
        assert result.startswith("Invalid options for transform 'llm':")
        # OpenRouter-specific required fields — model is required (no deployment_name fallback)
        assert "model" in result
        assert "api_key" in result

    def test_llm_secret_ref_does_not_hide_prompt_field_declaration_error(self) -> None:
        """A deferred credential must not suppress unrelated model validation."""
        result = _prevalidate_plugin_options(
            "transform",
            "llm",
            {
                "provider": "openrouter",
                "api_key": {"secret_ref": "OPENROUTER_API_KEY"},
                "model": "openai/gpt-4o",
                "prompt_template": "Summarise {{ row.text }}",
                "schema": {"mode": "observed"},
            },
        )

        assert result is not None
        assert "required_input_fields is not declared" in result
        assert "api_key" not in result

    def test_llm_openrouter_invalid_model_surfaces_structural_hint_without_raw_value(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Composer prevalidation rejects unknown OpenRouter models without echoing the value.

        Catalog membership is now enforced via the value-source walker
        (check_config_value_sources), so patch the catalog at the walker's lookup
        site rather than the (removed) construction-time validator's.
        """
        monkeypatch.setattr(
            "elspeth.engine.orchestrator.preflight.get_catalog_values",
            lambda catalog_id: frozenset({"openai/gpt-4o"}),
        )

        result = _prevalidate_plugin_options(
            "transform",
            "llm",
            {
                "provider": "openrouter",
                "api_key": "sk-test-key",
                "model": "anthropic/claude-3-opus",
                "prompt_template": "Analyze: {{ row.text }}",
                "schema": {"mode": "observed"},
                "required_input_fields": [],
            },
        )
        assert result is not None
        assert result == (
            "Invalid options for transform 'llm': configured value is not in catalog "
            "'openrouter'; pick a valid value via the list_models composer tool"
        )
        assert "anthropic/claude-3-opus" not in result

    def test_llm_secret_ref_still_runs_value_source_validation(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Placeholder validation must continue into catalog-backed checks."""
        monkeypatch.setattr(
            "elspeth.engine.orchestrator.preflight.get_catalog_values",
            lambda catalog_id: frozenset({"openai/gpt-4o"}),
        )

        result = _prevalidate_plugin_options(
            "transform",
            "llm",
            {
                "provider": "openrouter",
                "api_key": {"secret_ref": "OPENROUTER_API_KEY"},
                "model": "anthropic/claude-3-opus",
                "prompt_template": "Analyze: {{ row.text }}",
                "schema": {"mode": "observed"},
                "required_input_fields": [],
            },
        )

        assert result == (
            "Invalid options for transform 'llm': configured value is not in catalog "
            "'openrouter'; pick a valid value via the list_models composer tool"
        )
        assert "anthropic/claude-3-opus" not in result

    def test_unreachable_plugin_type_raises_assertion(self) -> None:
        """Passing an invalid plugin_type triggers the unreachable-branch assertion (not silent bypass)."""
        with pytest.raises(AssertionError, match="unexpected plugin_type"):
            _prevalidate_plugin_options(
                "unknown_kind",  # type: ignore[arg-type]
                "csv",
                {},
            )

    def test_absent_source_path_returns_error_not_none(self) -> None:
        """Absence of path is evidence of a missing required field — not a gap to fill with a placeholder.

        Pre-validates csv source options without path. The function must return a descriptive
        error, not None. This guards against regression where callers inject a fake placeholder
        path to suppress the error (violating the data manifesto's 'absence is evidence' rule).
        """
        result = _prevalidate_plugin_options(
            "source",
            "csv",
            {"schema": {"mode": "observed"}},
            injected_fields={"on_validation_failure": "quarantine"},
            # path deliberately absent — caller did not provide it
        )
        assert result is not None
        assert "path" in result

    def test_absent_sink_path_returns_error_not_none(self) -> None:
        """Absence of path for a sink plugin is a validation error, not a placeholder opportunity.

        Pre-validates csv sink options without path. The function must return a descriptive
        error, not None. Regression guard for the symmetric case on sinks.
        """
        result = _prevalidate_plugin_options(
            "sink",
            "csv",
            {},
            # path deliberately absent, no injected_fields
        )
        assert result is not None
        assert "path" in result

    def test_null_source_no_config_model_returns_none(self) -> None:
        """NullSource is registered as None in the source registry — no config validation needed.

        Exercises the ``config_cls is None`` branch in
        ``_prevalidate_plugin_options``. This is distinct from
        UnknownPluginTypeError: 'null' is a known, valid plugin that explicitly has no config
        class (it is a resume-only source with no options).
        """
        result = _prevalidate_plugin_options(
            "source",
            "null",
            {},
        )
        assert result is None

    def test_batch_stats_valid_options(self) -> None:
        """Aggregation plugin (batch_stats) with valid options passes pre-validation.

        batch_stats is an aggregation plugin dispatched as plugin_type="transform".
        This exercises the aggregation-as-transform path in _prevalidate_plugin_options.
        """
        result = _prevalidate_plugin_options(
            "transform",
            "batch_stats",
            {
                "schema": {"mode": "observed"},
                "value_field": "amount",
            },
        )
        assert result is None

    def test_batch_stats_missing_value_field(self) -> None:
        """Aggregation plugin with missing required field returns error."""
        result = _prevalidate_plugin_options(
            "transform",
            "batch_stats",
            {
                "schema": {"mode": "observed"},
                # value_field deliberately absent
            },
        )
        assert result is not None
        assert result.startswith("Invalid options for transform 'batch_stats':")
        assert "value_field" in result

    def test_batch_stats_empty_value_field_rejected(self) -> None:
        """batch_stats rejects empty string value_field via field_validator."""
        result = _prevalidate_plugin_options(
            "transform",
            "batch_stats",
            {
                "schema": {"mode": "observed"},
                "value_field": "",
            },
        )
        assert result is not None
        assert "value_field" in result

    def test_upsert_node_aggregation_type_validates_as_transform(self) -> None:
        """upsert_node with node_type='aggregation' routes through _prevalidate_transform.

        Regression guard: the upsert_node guard checks
        ``node_type in ("transform", "aggregation")``. If someone narrowed this
        to ``node_type == "transform"``, aggregation nodes would bypass
        pre-validation. This test exercises the aggregation path end-to-end.
        """
        state = _empty_state()
        catalog = _mock_catalog()
        # Add batch_stats to the mock catalog's transform list
        catalog.list_transforms.return_value = [
            *catalog.list_transforms.return_value,
            PluginSummary(
                name="batch_stats",
                description="Batch statistics aggregation",
                plugin_type="transform",
                config_fields=[],
            ),
        ]
        # Missing value_field should be caught by pre-validation
        result = execute_tool(
            "upsert_node",
            {
                "id": "agg1",
                "node_type": "aggregation",
                "plugin": "batch_stats",
                "input": "source",
                "on_success": "out",
                "options": {"schema": {"mode": "observed"}},
                # value_field deliberately absent
            },
            state,
            catalog,
        )
        assert result.success is False

    def test_upsert_node_aggregation_valid_options_succeeds(self) -> None:
        """upsert_node with node_type='aggregation' and valid options succeeds."""
        state = _empty_state()
        catalog = _mock_catalog()
        catalog.list_transforms.return_value = [
            *catalog.list_transforms.return_value,
            PluginSummary(
                name="batch_stats",
                description="Batch statistics aggregation",
                plugin_type="transform",
                config_fields=[],
            ),
        ]
        result = execute_tool(
            "upsert_node",
            {
                "id": "agg1",
                "node_type": "aggregation",
                "plugin": "batch_stats",
                "input": "source",
                "on_success": "out",
                "options": {
                    "schema": {"mode": "observed"},
                    "value_field": "amount",
                },
            },
            state,
            catalog,
        )
        assert result.success is True
        node = result.updated_state.nodes[0]
        assert node.id == "agg1"
        assert node.node_type == "aggregation"
        assert node.plugin == "batch_stats"
        assert deep_thaw(node.options) == {"schema": {"mode": "observed"}, "value_field": "amount"}

    def test_patch_node_options_collector_routes_through_prevalidation(self) -> None:
        """patch_node_options with node_type='collector' pre-validates the patch.

        Regression guard (2026-08-26 systems review): the patch guard checked
        ``node_type in ("transform", "aggregation")``, so patching a collector's
        options skipped plugin prevalidation entirely — invalid options were
        accepted and failed only later at lowering/preflight. Collectors are
        plugin-bearing (batch-transform contract) and take the same path.
        """
        state = CompositionState.from_dict(
            {
                "version": 1,
                "sources": {
                    "source": {
                        "plugin": "csv",
                        "on_success": "pages",
                        "options": {"path": "cases.csv"},
                        "on_validation_failure": "discard",
                    }
                },
                "nodes": [
                    {
                        "id": "page_stitcher",
                        "node_type": "collector",
                        "plugin": "batch_stats",
                        "input": "pages",
                        "on_success": "out",
                        "on_error": None,
                        "options": {"schema": {"mode": "observed"}, "value_field": "amount"},
                        "scope_name": "document_pages",
                        "scope_opener": "explode",
                        "scope_policy": "require_all",
                    }
                ],
                "edges": [],
                "outputs": [{"name": "out", "plugin": "json", "options": {}, "on_write_failure": "discard"}],
                "metadata": {"name": "Collector patch", "description": ""},
            }
        )
        catalog = _mock_catalog()
        catalog.list_transforms.return_value = [
            *catalog.list_transforms.return_value,
            PluginSummary(
                name="batch_stats",
                description="Batch statistics aggregation",
                plugin_type="transform",
                config_fields=[],
            ),
        ]

        # value_field must be a string; prevalidation must refuse the patch.
        result = execute_tool(
            "patch_node_options",
            {"node_id": "page_stitcher", "patch": {"value_field": 123}},
            state,
            catalog,
        )
        assert result.success is False

        # A valid patch still lands.
        result = execute_tool(
            "patch_node_options",
            {"node_id": "page_stitcher", "patch": {"value_field": "page_size_bytes"}},
            state,
            catalog,
        )
        assert result.success is True

    def test_upsert_node_batch_stats_group_by_keeps_expected_count_open(self) -> None:
        """Grouped rollups may emit one row per group, so cardinality stays unset."""
        state = _empty_state()
        catalog = _mock_catalog()
        catalog.list_transforms.return_value = [
            *catalog.list_transforms.return_value,
            PluginSummary(
                name="batch_stats",
                description="Batch statistics aggregation",
                plugin_type="transform",
                config_fields=[],
            ),
        ]
        result = execute_tool(
            "upsert_node",
            {
                "id": "agg1",
                "node_type": "aggregation",
                "plugin": "batch_stats",
                "input": "source",
                "on_success": "out",
                "options": {
                    "schema": {"mode": "observed"},
                    "value_field": "amount",
                    "group_by": "customer_tier",
                },
            },
            state,
            catalog,
        )
        assert result.success is True
        node = result.updated_state.nodes[0]
        assert node.expected_output_count is None

    def test_upsert_node_aggregation_rejects_required_input_fields(self) -> None:
        """ADR-013 declared input fields are not valid for batch-aware aggregation nodes."""
        state = _empty_state()
        catalog = _mock_catalog()
        catalog.list_transforms.return_value = [
            *catalog.list_transforms.return_value,
            PluginSummary(
                name="batch_stats",
                description="Batch statistics aggregation",
                plugin_type="transform",
                config_fields=[],
            ),
        ]

        result = execute_tool(
            "upsert_node",
            {
                "id": "agg1",
                "node_type": "aggregation",
                "plugin": "batch_stats",
                "input": "source",
                "on_success": "out",
                "options": {
                    "schema": {"mode": "observed"},
                    "value_field": "amount",
                    "required_input_fields": ["amount"],
                },
            },
            state,
            catalog,
        )

        assert result.success is False
        assert result.data is None
        messages = result.validation.errors[0].message
        assert "required_input_fields" in messages
        assert "batch-aware" in messages

    def test_upsert_node_rejects_batch_replicate_as_plain_transform(self) -> None:
        """Batch-only fan-out plugins must use the aggregation path, not row-mode transforms."""
        state = _empty_state()
        catalog = _mock_catalog()
        catalog.list_transforms.return_value = [
            *catalog.list_transforms.return_value,
            PluginSummary(
                name="batch_replicate",
                description="Batch replicate aggregation",
                plugin_type="transform",
                config_fields=[],
            ),
        ]

        result = execute_tool(
            "upsert_node",
            {
                "id": "replicate",
                "node_type": "transform",
                "plugin": "batch_replicate",
                "input": "source",
                "on_success": "out",
                "on_error": "discard",
                "options": {
                    "schema": {"mode": "observed"},
                    "replications": [
                        {"source_field": "region", "output_field": "region_copy"},
                    ],
                },
            },
            state,
            catalog,
        )

        assert result.success is False
        assert result.data is None
        messages = result.validation.errors[0].message
        assert "batch_replicate" in messages
        assert "aggregation" in messages
        assert "output_mode: transform" in messages

    def test_secret_ref_field_passes_prevalidation(self) -> None:
        """Options with secret_ref markers pass prevalidation.

        A secret-ref'd field is provisioned (the user called wire_secret_ref),
        just deferred to execution time. Prevalidation must not reject it.
        """
        result = _prevalidate_plugin_options(
            "transform",
            "azure_content_safety",
            {
                "api_key": {"secret_ref": "AZURE_API_KEY"},
                "endpoint": "https://test.cognitiveservices.azure.com",
                "schema": {"mode": "observed"},
                "fields": "text",
                "thresholds": {"hate": 2, "violence": 2, "sexual": 2, "self_harm": 2},
            },
        )
        assert result is None

    def test_secret_ref_field_non_secret_errors_still_reported(self) -> None:
        """Secret-ref'd fields are forgiven but other errors are still reported.

        api_key has a secret_ref (valid), but fields and thresholds are missing
        (real errors). The error message must mention the missing fields but
        NOT api_key.
        """
        result = _prevalidate_plugin_options(
            "transform",
            "azure_content_safety",
            {
                "api_key": {"secret_ref": "AZURE_API_KEY"},
                "endpoint": "https://test.cognitiveservices.azure.com",
                "schema": {"mode": "observed"},
                # fields and thresholds deliberately absent
            },
        )
        assert result is not None
        assert "fields" in result
        assert "thresholds" in result
        assert "api_key" not in result

    def test_secret_ref_in_source_passes_prevalidation(self) -> None:
        """Source options with a secret_ref marker pass prevalidation."""
        result = _prevalidate_plugin_options(
            "transform",
            "llm",
            {
                "provider": "openrouter",
                "model": "openai/gpt-4o",
                "api_key": {"secret_ref": "OPENROUTER_API_KEY"},
                "prompt_template": "Classify: {{ row.text }}",
                "required_input_fields": ["text"],
                "schema": {"mode": "observed"},
            },
        )
        assert result is None

    def test_malformed_secret_ref_marker_still_reports_field_error(self) -> None:
        """Only syntactically valid secret_ref markers are stripped.

        A non-string secret_ref value is malformed and must remain in the
        options payload so the plugin config model reports the field error
        during composer-time validation.
        """
        result = _prevalidate_plugin_options(
            "transform",
            "azure_content_safety",
            {
                "api_key": {"secret_ref": 123},
                "endpoint": "https://test.cognitiveservices.azure.com",
                "schema": {"mode": "observed"},
                "fields": "text",
                "thresholds": {"hate": 2, "violence": 2, "sexual": 2, "self_harm": 2},
            },
        )
        assert result is not None
        assert "api_key" in result


# ---------------------------------------------------------------------------
# create_blob Tier-3 type guard (elspeth-7a26880c65, Task 2)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# update_blob Tier-3 type guard (elspeth-7a26880c65, Task 3)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# set_source_from_blob Tier-3 type guard
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# get_blob_content — Tier-1 guards (bug_002: composer tool bypassed the
# lifecycle / integrity / decode guards enforced by
# BlobServiceImpl.read_blob_content).  Any path that returns blob bytes
# to the LLM must refuse partial/failed blobs, detect corruption or
# tampering via hash verification, and not crash the tool dispatcher on
# non-UTF-8 bytes that the MIME allowlist happens to admit.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# get_blob_content — blob origin on the read-back path (elspeth-47eba5cced).
#
# Live session 891b7b1e: the planner fabricated rows at the user's request,
# wrote them to a blob, read them back, and narrated its own invention to the
# user as "the system auto-generated placeholder content".  The read-back
# result carried no origin facts at all, so a self-authored blob was
# structurally indistinguishable from a discovered one.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# update_blob — active-run guard (bug_004: composer could mutate blob bytes
# while an ExecutionService run was actively consuming them).  Mirrors the
# delete_blob two-check pattern: blob_run_links lookup + composition_states
# source scan for the pre-link window.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# update_blob — atomic write-order (bug_004: the file write happened BEFORE
# the DB transaction began, creating a window in which a pipeline reader
# would see new bytes against the stale DB hash even with a correct
# active-run guard).  The fix writes to a sibling tempfile and swaps in
# the new content with os.replace only after the guard + quota + UPDATE
# have all succeeded.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# inspect_source mirrors get_blob_content's lifecycle/integrity/decode
# guards, but returns SourceInspectionFacts as a structured dict rather than
# raw bytes — so the LLM can reason about headers and types without seeing
# row content.
# ---------------------------------------------------------------------------


class TestInspectSourceTool:
    """``inspect_source`` returns bounded structural facts about a blob."""

    @pytest.fixture(autouse=True)
    def _setup(self, tmp_path: Path):
        from datetime import UTC, datetime
        from uuid import uuid4

        from sqlalchemy.pool import StaticPool

        from elspeth.web.blobs.service import content_hash as _content_hash
        from elspeth.web.sessions.engine import create_session_engine
        from elspeth.web.sessions.models import blobs_table, sessions_table
        from elspeth.web.sessions.schema import initialize_session_schema

        self.engine = create_session_engine(
            "sqlite:///:memory:",
            poolclass=StaticPool,
            connect_args={"check_same_thread": False},
        )
        initialize_session_schema(self.engine)
        with self.engine.begin() as conn:
            ensure_test_identity(conn, identity_id="test-user")

        self.data_dir = tmp_path
        self.session_id = str(uuid4())
        self.blob_id = str(uuid4())
        now = datetime.now(UTC)

        storage_dir = tmp_path / "blobs" / self.session_id
        storage_dir.mkdir(parents=True)
        self.storage_path = storage_dir / f"{self.blob_id}_orders.csv"
        self.content_bytes = b"order_id,customer,price\nO-1,Alice,49.95\nO-2,Bob,150.00\n"
        self.content_hash_hex = _content_hash(self.content_bytes)
        self.storage_path.write_bytes(self.content_bytes)

        with self.engine.begin() as conn:
            conn.execute(
                sessions_table.insert().values(
                    id=self.session_id,
                    user_id="test-user",
                    auth_provider_type="local",
                    title="Test",
                    created_at=now,
                    updated_at=now,
                )
            )
            conn.execute(
                blobs_table.insert().values(
                    id=self.blob_id,
                    session_id=self.session_id,
                    filename="orders.csv",
                    mime_type="text/csv",
                    size_bytes=len(self.content_bytes),
                    content_hash=self.content_hash_hex,
                    storage_path=str(self.storage_path),
                    created_at=now,
                    created_by="user",
                    source_description=None,
                    status="ready",
                )
            )

    def _set_status(self, status: str) -> None:
        from elspeth.web.sessions.models import blobs_table

        with self.engine.begin() as conn:
            conn.execute(blobs_table.update().where(blobs_table.c.id == self.blob_id).values(status=status))

    def test_returns_csv_facts_for_ready_blob(self) -> None:
        state = _empty_state()
        catalog = _mock_catalog()
        with _blob_operation(self.engine, self.session_id) as (blob_authority, blob_operation_context):
            result = execute_tool(
                "inspect_source",
                {"blob_id": self.blob_id},
                state,
                catalog,
                data_dir=str(self.data_dir),
                session_engine=self.engine,
                session_id=self.session_id,
                session_operation_authority=blob_authority,
                session_operation_context=blob_operation_context,
            )
        assert result.success is True
        data = result.data
        assert data["source_kind"] == "csv"
        # Data is deep-frozen by ToolResult.__post_init__: lists become tuples,
        # dicts become MappingProxyType. Compare against the frozen forms.
        assert tuple(data["observed_headers"]) == ("order_id", "customer", "price")
        assert dict(data["inferred_types"]) == {
            "order_id": "str",
            "customer": "str",
            "price": "float",
        }
        assert data["sample_row_count"] == 2
        assert data["redacted_identity"]["filename"] == "orders.csv"
        assert data["redacted_identity"]["mime_type"] == "text/csv"
        # Identity must NOT include storage_path
        assert "storage_path" not in data["redacted_identity"]

    def test_returns_url_candidates_when_present(self) -> None:
        from datetime import UTC, datetime
        from uuid import uuid4

        from elspeth.web.blobs.service import content_hash as _content_hash
        from elspeth.web.sessions.models import blobs_table

        # Add a second blob with URL content
        url_blob_id = str(uuid4())
        url_path = self.storage_path.parent / f"{url_blob_id}_urls.txt"
        url_bytes = b"https://example.com/api\n"
        url_path.write_bytes(url_bytes)
        with self.engine.begin() as conn:
            conn.execute(
                blobs_table.insert().values(
                    id=url_blob_id,
                    session_id=self.session_id,
                    filename="urls.txt",
                    mime_type="text/plain",
                    size_bytes=len(url_bytes),
                    content_hash=_content_hash(url_bytes),
                    storage_path=str(url_path),
                    created_at=datetime.now(UTC),
                    created_by="user",
                    source_description=None,
                    status="ready",
                )
            )

        with _blob_operation(self.engine, self.session_id) as (blob_authority, blob_operation_context):
            result = execute_tool(
                "inspect_source",
                {"blob_id": url_blob_id},
                _empty_state(),
                _mock_catalog(),
                data_dir=str(self.data_dir),
                session_engine=self.engine,
                session_id=self.session_id,
                session_operation_authority=blob_authority,
                session_operation_context=blob_operation_context,
            )
        assert result.success is True
        assert result.data["source_kind"] == "text"
        # url_candidates are redacted to scheme + host (+ port): userinfo and path
        # are dropped because they can carry credentials / reset tokens / PII.
        assert tuple(result.data["url_candidates"]) == ("https://example.com",)
        assert any("compatible HTTP fetch transform" in w for w in result.data["warnings"])
        assert all("web_scrape" not in w for w in result.data["warnings"])

    def test_pending_blob_refused(self) -> None:
        self._set_status("pending")
        with _blob_operation(self.engine, self.session_id) as (blob_authority, blob_operation_context):
            result = execute_tool(
                "inspect_source",
                {"blob_id": self.blob_id},
                _empty_state(),
                _mock_catalog(),
                data_dir=str(self.data_dir),
                session_engine=self.engine,
                session_id=self.session_id,
                session_operation_authority=blob_authority,
                session_operation_context=blob_operation_context,
            )
        assert result.success is False
        # Failure messages live in result.validation.errors[0].message (set by _failure_result).
        assert "pending" in result.validation.errors[0].message.lower()

    def test_missing_blob_returns_failure(self) -> None:
        from uuid import uuid4

        with _blob_operation(self.engine, self.session_id) as (blob_authority, blob_operation_context):
            result = execute_tool(
                "inspect_source",
                {"blob_id": str(uuid4())},
                _empty_state(),
                _mock_catalog(),
                data_dir=str(self.data_dir),
                session_engine=self.engine,
                session_id=self.session_id,
                session_operation_authority=blob_authority,
                session_operation_context=blob_operation_context,
            )
        assert result.success is False
        assert "not found" in result.validation.errors[0].message.lower()

    def test_without_session_context_returns_failure(self) -> None:
        result = execute_tool(
            "inspect_source",
            {"blob_id": self.blob_id},
            _empty_state(),
            _mock_catalog(),
        )
        assert result.success is False

    def test_missing_blob_id_raises_tool_argument_error(self) -> None:
        from pydantic import ValidationError as PydanticValidationError

        from elspeth.web.composer.protocol import ToolArgumentError

        with (
            pytest.raises(
                ToolArgumentError,
                # A missing field is not a JSON-type fault, so the type
                # guidance is not appended (only *_type/json_* causes get it).
                match=(r"^'inspect_source arguments' must be object conforming to InspectSourceArgumentsModel, got ValidationError$"),
            ) as exc_info,
            _blob_operation(self.engine, self.session_id) as (blob_authority, blob_operation_context),
        ):
            execute_tool(
                "inspect_source",
                {},
                _empty_state(),
                _mock_catalog(),
                data_dir=str(self.data_dir),
                session_engine=self.engine,
                session_id=self.session_id,
                session_operation_authority=blob_authority,
                session_operation_context=blob_operation_context,
            )
        assert isinstance(exc_info.value.__cause__, PydanticValidationError)

    def test_inspect_source_does_not_use_unbounded_read_bytes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from elspeth.web.blobs.service import content_hash as _content_hash
        from elspeth.web.sessions.models import blobs_table

        large_content = b"order_id,customer,price\n" + (b"O-1,Alice,49.95\n" * 2048)
        self.storage_path.write_bytes(large_content)
        with self.engine.begin() as conn:
            conn.execute(
                blobs_table.update()
                .where(blobs_table.c.id == self.blob_id)
                .values(size_bytes=len(large_content), content_hash=_content_hash(large_content))
            )

        original_read_bytes = Path.read_bytes

        def _forbid_storage_read_bytes(path: Path) -> bytes:
            if path == self.storage_path:
                raise AssertionError("inspect_source must not slurp the full blob with Path.read_bytes")
            return original_read_bytes(path)

        monkeypatch.setattr(Path, "read_bytes", _forbid_storage_read_bytes)

        with _blob_operation(self.engine, self.session_id) as (blob_authority, blob_operation_context):
            result = execute_tool(
                "inspect_source",
                {"blob_id": self.blob_id},
                _empty_state(),
                _mock_catalog(),
                data_dir=str(self.data_dir),
                session_engine=self.engine,
                session_id=self.session_id,
                session_operation_authority=blob_authority,
                session_operation_context=blob_operation_context,
            )

        assert result.success is True
        assert result.data["byte_range_inspected"][1] <= 8 * 1024

    def test_hash_mismatch_raises_blob_integrity_error(self) -> None:
        """Tier-1 invariant — corrupted blob must escalate, not return facts."""
        from elspeth.web.blobs.protocol import BlobIntegrityError

        # Tamper with the on-disk bytes so SHA-256 mismatches the stored hash.
        self.storage_path.write_bytes(b"tampered,content\n9,9\n")
        with (
            pytest.raises(BlobIntegrityError),
            _blob_operation(self.engine, self.session_id) as (blob_authority, blob_operation_context),
        ):
            execute_tool(
                "inspect_source",
                {"blob_id": self.blob_id},
                _empty_state(),
                _mock_catalog(),
                data_dir=str(self.data_dir),
                session_engine=self.engine,
                session_id=self.session_id,
                session_operation_authority=blob_authority,
                session_operation_context=blob_operation_context,
            )

    def test_non_uuid_blob_id_rejected_before_lookup(self) -> None:
        """LLM placeholder identifiers must fail at the blob-id boundary."""
        with _blob_operation(self.engine, self.session_id) as (blob_authority, blob_operation_context):
            result = execute_tool(
                "inspect_source",
                {"blob_id": "__missing__"},
                _empty_state(),
                _mock_catalog(),
                data_dir=str(self.data_dir),
                session_engine=self.engine,
                session_id=self.session_id,
                session_operation_authority=blob_authority,
                session_operation_context=blob_operation_context,
            )

        assert result.success is False
        assert "not a valid UUID" in result.validation.errors[0].message
        assert "upload" in result.validation.errors[0].message
        assert "list_blobs" in result.validation.errors[0].message
        assert "not found" not in result.validation.errors[0].message.lower()


# ---------------------------------------------------------------------------
# preview_pipeline proof step. proof_diagnostics surfaces blocking issues that
# depend on observed input shape: fixed CSV schema omitting observed columns,
# text source containing a URL with no web_scrape downstream, missing or
# unreadable blob storage. is_valid is forced to False when any proof
# diagnostic is blocking, even if authoring/runtime checks pass.
# ---------------------------------------------------------------------------


class TestPreviewProofStep:
    """preview_pipeline must surface input-shape diagnostics when session context is supplied."""

    @pytest.fixture(autouse=True)
    def _setup(self, tmp_path: Path):
        from datetime import UTC, datetime
        from uuid import uuid4

        from sqlalchemy.pool import StaticPool

        from elspeth.web.blobs.service import content_hash as _content_hash
        from elspeth.web.sessions.engine import create_session_engine
        from elspeth.web.sessions.models import blobs_table, sessions_table
        from elspeth.web.sessions.schema import initialize_session_schema

        self.engine = create_session_engine(
            "sqlite:///:memory:",
            poolclass=StaticPool,
            connect_args={"check_same_thread": False},
        )
        initialize_session_schema(self.engine)
        with self.engine.begin() as conn:
            ensure_test_identity(conn, identity_id="test-user")

        self.data_dir = tmp_path
        self.session_id = str(uuid4())
        self.csv_blob_id = str(uuid4())
        self.url_blob_id = str(uuid4())
        now = datetime.now(UTC)

        # CSV blob with three observed columns
        storage_dir = tmp_path / "blobs" / self.session_id
        storage_dir.mkdir(parents=True)
        self.csv_storage_path = storage_dir / f"{self.csv_blob_id}_orders.csv"
        csv_content = b"order_id,customer,price\nO-1,Alice,49.95\nO-2,Bob,150.00\n"
        self.csv_storage_path.write_bytes(csv_content)

        # Text blob with a single URL
        self.url_storage_path = storage_dir / f"{self.url_blob_id}_url.txt"
        url_content = b"https://example.com/data.json\n"
        self.url_storage_path.write_bytes(url_content)

        with self.engine.begin() as conn:
            conn.execute(
                sessions_table.insert().values(
                    id=self.session_id,
                    user_id="test-user",
                    auth_provider_type="local",
                    title="Test",
                    created_at=now,
                    updated_at=now,
                )
            )
            conn.execute(
                blobs_table.insert().values(
                    id=self.csv_blob_id,
                    session_id=self.session_id,
                    filename="orders.csv",
                    mime_type="text/csv",
                    size_bytes=len(csv_content),
                    content_hash=_content_hash(csv_content),
                    storage_path=str(self.csv_storage_path),
                    created_at=now,
                    created_by="user",
                    source_description=None,
                    status="ready",
                )
            )
            conn.execute(
                blobs_table.insert().values(
                    id=self.url_blob_id,
                    session_id=self.session_id,
                    filename="url.txt",
                    mime_type="text/plain",
                    size_bytes=len(url_content),
                    content_hash=_content_hash(url_content),
                    storage_path=str(self.url_storage_path),
                    created_at=now,
                    created_by="user",
                    source_description=None,
                    status="ready",
                )
            )

    def _state_with_csv_source(
        self,
        *,
        session_operation_authority: SQLiteLocalSessionOperationAuthority,
        session_operation_context: SessionOperationContext,
        schema_mode: str = "fixed",
        fields: tuple[object, ...] = (),
        on_validation_failure: str = "discard",
        columns: tuple[str, ...] | None = None,
    ):
        """Build a state with a CSV blob source via the composer tool API."""
        schema: dict[str, object] = {"mode": schema_mode}
        if fields:
            schema["fields"] = list(fields)

        state = _empty_state()
        catalog = _mock_catalog()
        # Wire source via set_source_from_blob — this is the canonical way to
        # produce a state with source.options.blob_ref set.
        source_options: dict[str, object] = {"schema": schema}
        if columns is not None:
            source_options["columns"] = list(columns)
        result = execute_tool(
            "set_source_from_blob",
            {
                "blob_id": self.csv_blob_id,
                "on_success": "rows",
                "on_validation_failure": on_validation_failure,
                "options": source_options,
            },
            state,
            catalog,
            data_dir=str(self.data_dir),
            session_operation_authority=session_operation_authority,
            session_operation_context=session_operation_context,
            session_engine=self.engine,
            session_id=self.session_id,
        )
        assert result.success, result.data
        state = result.updated_state

        result = execute_tool(
            "set_output",
            {
                "sink_name": "out",
                "plugin": "json",
                "options": {
                    "path": "outputs/out.json",
                    "schema": {"mode": "observed"},
                    "mode": "write",
                    "collision_policy": "auto_increment",
                },
                "on_write_failure": "discard",
            },
            state,
            catalog,
        )
        assert result.success, result.data
        return result.updated_state

    def _state_with_text_url_source(
        self,
        *,
        session_operation_authority: SQLiteLocalSessionOperationAuthority,
        session_operation_context: SessionOperationContext,
        fetch_plugin: str | None,
    ):
        """Build a state with a text URL blob source and optional HTTP fetcher."""
        state = _empty_state()
        catalog = _mock_catalog()

        result = execute_tool(
            "set_source_from_blob",
            {
                "blob_id": self.url_blob_id,
                "on_success": "url_rows" if fetch_plugin is not None else "content",
                "on_validation_failure": "discard",
                "options": {
                    "column": "url",
                    "schema": {"mode": "fixed", "fields": ["url: str"]},
                },
            },
            state,
            catalog,
            data_dir=str(self.data_dir),
            session_operation_authority=session_operation_authority,
            session_operation_context=session_operation_context,
            session_engine=self.engine,
            session_id=self.session_id,
        )
        assert result.success, result.data
        state = result.updated_state

        if fetch_plugin is not None:
            options = (
                {
                    "url_field": "url",
                    "schema": {"mode": "fixed", "fields": ["url: str"]},
                    "content_field": "content",
                    "fingerprint_field": "content_fingerprint",
                    "format": "text",
                    "text_separator": "\n",
                    "http": {
                        "abuse_contact": "test@example.com",
                        "scraping_reason": "test",
                        "allowed_hosts": "public_only",
                    },
                }
                if fetch_plugin == "web_scrape"
                else {
                    "url_field": "url",
                    "schema": {"mode": "fixed", "fields": ["url: str"]},
                    "http": {
                        "abuse_contact": "test@example.com",
                        "fetch_reason": "test",
                        "allowed_hosts": "public_only",
                    },
                }
            )
            result = execute_tool(
                "upsert_node",
                {
                    "id": "fetch",
                    "node_type": "transform",
                    "plugin": fetch_plugin,
                    "input": "url_rows",
                    "on_success": "content",
                    "on_error": "discard",
                    "options": options,
                },
                state,
                catalog,
            )
            assert result.success, result.data
            state = result.updated_state

        result = execute_tool(
            "set_output",
            {
                "sink_name": "out",
                "plugin": "json",
                "options": {
                    "path": "outputs/out.json",
                    "schema": {"mode": "observed"},
                    "mode": "write",
                    "collision_policy": "auto_increment",
                },
                "on_write_failure": "discard",
            },
            state,
            catalog,
        )
        assert result.success, result.data
        return result.updated_state

    # -- Empty / no-op cases ------------------------------------------------

    def test_proof_empty_when_no_blob_source(self) -> None:
        """Path-based source has no blob to inspect — diagnostics empty."""
        # Build a state via set_pipeline using a path-based source (no blob_ref)
        with _blob_operation(self.engine, self.session_id) as (blob_authority, blob_operation_context):
            args = _valid_pipeline_args()
            args["source"]["on_validation_failure"] = "discard"
            result = execute_tool("set_pipeline", args, _empty_state(), _mock_catalog())
            assert result.success, result.data

            result = execute_tool(
                "preview_pipeline",
                {},
                result.updated_state,
                _mock_catalog(),
                data_dir=str(self.data_dir),
                session_operation_authority=blob_authority,
                session_operation_context=blob_operation_context,
                session_engine=self.engine,
                session_id=self.session_id,
            )
            assert result.success is True
            assert result.data["proof_diagnostics"] == ()

    def test_proof_empty_when_no_session_context(self) -> None:
        """Blob source with no session_engine — proof step degrades to empty."""
        with _blob_operation(self.engine, self.session_id) as (blob_authority, blob_operation_context):
            state = self._state_with_csv_source(
                session_operation_authority=blob_authority, session_operation_context=blob_operation_context, schema_mode="observed"
            )
            result = execute_tool("preview_pipeline", {}, state, _mock_catalog())
            assert result.success is True
            assert result.data["proof_diagnostics"] == ()

    def test_proof_inspects_named_sources_beyond_compatibility_source(self) -> None:
        """A non-first named source must not bypass proof diagnostics."""
        with _blob_operation(self.engine, self.session_id) as (blob_authority, blob_operation_context):
            state = self._state_with_csv_source(
                session_operation_authority=blob_authority, session_operation_context=blob_operation_context, schema_mode="observed"
            ).with_named_source(
                "url_source",
                SourceSpec(
                    plugin="text",
                    on_success="content",
                    options={
                        "blob_ref": self.url_blob_id,
                        "column": "url",
                        "schema": {"mode": "fixed", "fields": ["url: str"]},
                    },
                    on_validation_failure="discard",
                ),
            )

            result = execute_tool(
                "preview_pipeline",
                {},
                state,
                _mock_catalog(),
                data_dir=str(self.data_dir),
                session_operation_authority=blob_authority,
                session_operation_context=blob_operation_context,
                session_engine=self.engine,
                session_id=self.session_id,
            )

            diagnostics = result.data["proof_diagnostics"]
            matching = [d for d in diagnostics if d["code"] == "text_source_url_without_web_scrape"]
            assert matching
            assert matching[0]["evidence_locator"]["source_name"] == "url_source"

    def test_unrelated_branch_fetcher_does_not_suppress_text_url_blocker(self) -> None:
        """Fetcher membership is necessary but must be downstream of this source."""
        with _blob_operation(self.engine, self.session_id) as (blob_authority, blob_operation_context):
            state = self._state_with_csv_source(
                session_operation_authority=blob_authority, session_operation_context=blob_operation_context, schema_mode="observed"
            ).with_named_source(
                "url_source",
                SourceSpec(
                    plugin="text",
                    on_success="url_content",
                    options={
                        "blob_ref": self.url_blob_id,
                        "column": "url",
                        "schema": {"mode": "fixed", "fields": ["url: str"]},
                    },
                    on_validation_failure="discard",
                ),
            )
            state = state.with_node(
                NodeSpec(
                    id="unrelated_fetch",
                    node_type="transform",
                    plugin="web_scrape",
                    input="rows",
                    on_success="unrelated_content",
                    on_error="discard",
                    options={},
                    condition=None,
                    routes=None,
                    fork_to=None,
                    branches=None,
                    policy=None,
                    merge=None,
                )
            )

            result = execute_tool(
                "preview_pipeline",
                {},
                state,
                _mock_catalog(),
                data_dir=str(self.data_dir),
                session_operation_authority=blob_authority,
                session_operation_context=blob_operation_context,
                session_engine=self.engine,
                session_id=self.session_id,
            )

            matching = [
                diagnostic for diagnostic in result.data["proof_diagnostics"] if diagnostic["code"] == "text_source_url_without_web_scrape"
            ]
            assert matching
            assert matching[0]["evidence_locator"]["source_name"] == "url_source"

    def test_same_blob_sources_share_one_metadata_and_verified_content_read(self) -> None:
        """Composer preview evaluates each alias without re-reading its blob."""
        from elspeth.web.composer.tools import generation as generation_module

        with _blob_operation(self.engine, self.session_id) as (blob_authority, blob_operation_context):
            base = self._state_with_csv_source(
                session_operation_authority=blob_authority, session_operation_context=blob_operation_context, schema_mode="observed"
            )
            source = base.sources["source"]
            gate = NodeSpec(
                id="orders_gate",
                node_type="gate",
                plugin=None,
                input="order_rows",
                on_success=None,
                on_error=None,
                options={},
                condition="row['price'] > 100",
                routes={"true": "high_value", "false": "standard"},
                fork_to=None,
                branches=None,
                policy=None,
                merge=None,
            )
            state = replace(
                base,
                sources={
                    "orders": replace(source, on_success="order_rows"),
                    "refunds": replace(source, on_success="refund_rows"),
                },
                nodes=(
                    gate,
                    replace(gate, id="refunds_gate", input="refund_rows"),
                ),
                outputs=(
                    OutputSpec(
                        name="high_value",
                        plugin="json",
                        options={
                            "path": "outputs/high.json",
                            "schema": {"mode": "observed"},
                            "mode": "write",
                            "collision_policy": "auto_increment",
                        },
                        on_write_failure="discard",
                    ),
                    OutputSpec(
                        name="standard",
                        plugin="json",
                        options={
                            "path": "outputs/standard.json",
                            "schema": {"mode": "observed"},
                            "mode": "write",
                            "collision_policy": "auto_increment",
                        },
                        on_write_failure="discard",
                    ),
                ),
            )
            original_locked_read = generation_module._locked_read_ready_blob
            original_read_bytes = Path.read_bytes
            metadata_reads: list[str] = []
            content_reads: list[Path] = []

            def counted_locked_read(
                engine: Any,
                session_id: str,
                blob_id: str,
                *,
                data_dir: str,
                session_operation_authority: SQLiteLocalSessionOperationAuthority,
                session_operation_context: SessionOperationContext,
            ) -> Any:
                metadata_reads.append(blob_id)
                return original_locked_read(
                    engine,
                    session_id,
                    blob_id,
                    data_dir=data_dir,
                    session_operation_authority=session_operation_authority,
                    session_operation_context=session_operation_context,
                )

            def counted_read_bytes(path: Path) -> bytes:
                content_reads.append(path)
                return original_read_bytes(path)

            with (
                patch.object(generation_module, "_locked_read_ready_blob", side_effect=counted_locked_read),
                patch.object(Path, "read_bytes", counted_read_bytes),
            ):
                result = execute_tool(
                    "preview_pipeline",
                    {},
                    state,
                    _mock_catalog(),
                    data_dir=str(self.data_dir),
                    session_operation_authority=blob_authority,
                    session_operation_context=blob_operation_context,
                    session_engine=self.engine,
                    session_id=self.session_id,
                )

            assert result.validation.is_valid is True
            matching = [
                diagnostic
                for diagnostic in result.data["proof_diagnostics"]
                if diagnostic["code"] == "gate_expression_type_mismatch_against_source_schema"
            ]
            assert [
                (
                    diagnostic["evidence_locator"]["source_name"],
                    diagnostic["evidence_locator"]["node_id"],
                )
                for diagnostic in matching
            ] == [("orders", "orders_gate"), ("refunds", "refunds_gate")]
            assert metadata_reads == [self.csv_blob_id]
            assert content_reads == [self.csv_storage_path]

    @pytest.mark.parametrize("status", ["pending", "error"])
    def test_non_ready_blob_metadata_abstains_before_any_path_or_integrity_access(
        self,
        status: str,
    ) -> None:
        """Lifecycle status is checked before touching concurrent blob bytes."""
        from sqlalchemy import update

        from elspeth.web.composer.tools import generation as generation_module

        with _blob_operation(self.engine, self.session_id) as (blob_authority, blob_operation_context):
            state = self._state_with_csv_source(
                session_operation_authority=blob_authority, session_operation_context=blob_operation_context, schema_mode="observed"
            )
        with self.engine.begin() as conn:
            conn.execute(update(blobs_table).where(blobs_table.c.id == self.csv_blob_id).values(status=status, content_hash=None))

        with (
            _blob_operation(self.engine, self.session_id) as (blob_authority, blob_operation_context),
            patch.object(
                Path,
                "exists",
                side_effect=AssertionError("non-ready proof blob must not access its backing path"),
            ) as exists,
            patch.object(
                Path,
                "read_bytes",
                side_effect=AssertionError("non-ready proof blob must not read bytes"),
            ) as read_bytes,
            patch.object(
                generation_module,
                "_verify_blob_content_integrity",
                side_effect=AssertionError("non-ready proof blob must not verify content"),
            ) as verify,
        ):
            diagnostics = generation_module.compute_proof_diagnostics(
                state,
                data_dir=str(self.data_dir),
                session_operation_authority=blob_authority,
                session_operation_context=blob_operation_context,
                session_engine=self.engine,
                session_id=self.session_id,
            )

        assert diagnostics == []
        exists.assert_not_called()
        read_bytes.assert_not_called()
        verify.assert_not_called()

    # -- csv_fixed_schema_omits_observed_columns ----------------------------

    def test_fixed_csv_omits_columns_with_discard_blocks(self) -> None:
        with _blob_operation(self.engine, self.session_id) as (blob_authority, blob_operation_context):
            state = self._state_with_csv_source(
                session_operation_authority=blob_authority,
                session_operation_context=blob_operation_context,
                schema_mode="fixed",
                fields=("order_id: str",),
                on_validation_failure="discard",
            )
            result = execute_tool(
                "preview_pipeline",
                {},
                state,
                _mock_catalog(),
                data_dir=str(self.data_dir),
                session_operation_authority=blob_authority,
                session_operation_context=blob_operation_context,
                session_engine=self.engine,
                session_id=self.session_id,
            )
            diagnostics = result.data["proof_diagnostics"]
            codes = [d["code"] for d in diagnostics]
            assert "csv_fixed_schema_omits_observed_columns" in codes
            blocking = [d for d in diagnostics if d["severity"] == "blocking"]
            assert blocking, "expected a blocking diagnostic for omitted observed columns"
            # is_valid is forced False by the blocking proof diagnostic.
            assert result.data["preview_is_valid"] is False

    def test_fixed_csv_with_all_columns_does_not_block(self) -> None:
        with _blob_operation(self.engine, self.session_id) as (blob_authority, blob_operation_context):
            state = self._state_with_csv_source(
                session_operation_authority=blob_authority,
                session_operation_context=blob_operation_context,
                schema_mode="fixed",
                fields=("order_id: str", "customer: str", "price: float"),
                on_validation_failure="discard",
            )
            result = execute_tool(
                "preview_pipeline",
                {},
                state,
                _mock_catalog(),
                data_dir=str(self.data_dir),
                session_operation_authority=blob_authority,
                session_operation_context=blob_operation_context,
                session_engine=self.engine,
                session_id=self.session_id,
            )
            codes = [d["code"] for d in result.data["proof_diagnostics"]]
            assert "csv_fixed_schema_omits_observed_columns" not in codes

    def test_fixed_csv_with_structured_fields_does_not_crash_or_block(self) -> None:
        with _blob_operation(self.engine, self.session_id) as (blob_authority, blob_operation_context):
            state = self._state_with_csv_source(
                session_operation_authority=blob_authority,
                session_operation_context=blob_operation_context,
                schema_mode="fixed",
                fields=(
                    {"name": "order_id", "field_type": "str"},
                    {"name": "customer", "field_type": "str"},
                    {"name": "price", "field_type": "float"},
                ),
                on_validation_failure="discard",
            )
            result = execute_tool(
                "preview_pipeline",
                {},
                state,
                _mock_catalog(),
                data_dir=str(self.data_dir),
                session_operation_authority=blob_authority,
                session_operation_context=blob_operation_context,
                session_engine=self.engine,
                session_id=self.session_id,
            )
            assert result.success is True
            codes = [d["code"] for d in result.data["proof_diagnostics"]]
        assert "csv_fixed_schema_omits_observed_columns" not in codes

    def test_flexible_csv_does_not_block(self) -> None:
        """Flexible mode accepts extra columns by design."""
        with _blob_operation(self.engine, self.session_id) as (authority, context):
            state = self._state_with_csv_source(
                session_operation_authority=authority, session_operation_context=context, schema_mode="flexible", fields=("order_id: str",)
            )
            result = execute_tool(
                "preview_pipeline",
                {},
                state,
                _mock_catalog(),
                session_engine=self.engine,
                session_id=self.session_id,
                data_dir=str(self.data_dir),
                session_operation_authority=authority,
                session_operation_context=context,
            )
            codes = [d["code"] for d in result.data["proof_diagnostics"]]
            assert "csv_fixed_schema_omits_observed_columns" not in codes

    def test_fixed_csv_with_routed_failures_does_not_block(self) -> None:
        """on_validation_failure routes to a sink → not silent discard, not blocking."""
        with _blob_operation(self.engine, self.session_id) as (authority, context):
            state = self._state_with_csv_source(
                session_operation_authority=authority,
                session_operation_context=context,
                schema_mode="fixed",
                fields=("order_id: str",),
                on_validation_failure="quarantine_sink",
            )
            result = execute_tool(
                "preview_pipeline",
                {},
                state,
                _mock_catalog(),
                session_engine=self.engine,
                session_id=self.session_id,
                data_dir=str(self.data_dir),
                session_operation_authority=authority,
                session_operation_context=context,
            )
            codes = [d["code"] for d in result.data["proof_diagnostics"]]
            assert "csv_fixed_schema_omits_observed_columns" not in codes

    # -- text_source_url_without_web_scrape ---------------------------------

    def test_text_url_without_web_scrape_blocks(self) -> None:
        with _blob_operation(self.engine, self.session_id) as (authority, context):
            state = self._state_with_text_url_source(
                session_operation_authority=authority, session_operation_context=context, fetch_plugin=None
            )
            result = execute_tool(
                "preview_pipeline",
                {},
                state,
                _mock_catalog(),
                session_engine=self.engine,
                session_id=self.session_id,
                data_dir=str(self.data_dir),
                session_operation_authority=authority,
                session_operation_context=context,
            )
            diagnostics = result.data["proof_diagnostics"]
            codes = [d["code"] for d in diagnostics]
            assert "text_source_url_without_web_scrape" in codes
            blocking = [d for d in diagnostics if d["severity"] == "blocking"]
            assert blocking
            assert result.data["preview_is_valid"] is False

    def test_text_url_with_web_scrape_does_not_block(self) -> None:
        with _blob_operation(self.engine, self.session_id) as (authority, context):
            state = self._state_with_text_url_source(
                session_operation_authority=authority, session_operation_context=context, fetch_plugin="web_scrape"
            )
            result = execute_tool(
                "preview_pipeline",
                {},
                state,
                _mock_catalog(),
                session_engine=self.engine,
                session_id=self.session_id,
                data_dir=str(self.data_dir),
                session_operation_authority=authority,
                session_operation_context=context,
            )
            codes = [d["code"] for d in result.data["proof_diagnostics"]]
            assert "text_source_url_without_web_scrape" not in codes

    def test_text_url_with_blob_fetch_does_not_block(self) -> None:
        with _blob_operation(self.engine, self.session_id) as (authority, context):
            state = self._state_with_text_url_source(
                session_operation_authority=authority, session_operation_context=context, fetch_plugin="blob_fetch"
            )
            result = execute_tool(
                "preview_pipeline",
                {},
                state,
                _mock_catalog(),
                session_engine=self.engine,
                session_id=self.session_id,
                data_dir=str(self.data_dir),
                session_operation_authority=authority,
                session_operation_context=context,
            )
            codes = [d["code"] for d in result.data["proof_diagnostics"]]
            assert "text_source_url_without_web_scrape" not in codes

    # -- inspection warnings surfaced as info -------------------------------

    def test_inspection_warnings_surfaced_as_info(self) -> None:
        """The text source's web_scrape warning is mirrored in proof_diagnostics as info."""
        with _blob_operation(self.engine, self.session_id) as (authority, context):
            state = self._state_with_text_url_source(
                session_operation_authority=authority, session_operation_context=context, fetch_plugin="web_scrape"
            )
            result = execute_tool(
                "preview_pipeline",
                {},
                state,
                _mock_catalog(),
                session_engine=self.engine,
                session_id=self.session_id,
                data_dir=str(self.data_dir),
                session_operation_authority=authority,
                session_operation_context=context,
            )
            diagnostics = result.data["proof_diagnostics"]
            info = [d for d in diagnostics if d["severity"] == "info"]
            # web_scrape warning from inspection should be mirrored — as info, not blocking
            assert any(d["code"] == "source_inspection_warning" for d in info)

    # -- csv_duplicate_headers (promoted to blocking) -----------------------
    # Duplicate CSV headers cause silent column collapse in csv.DictReader
    # (last-write-wins) and similar libraries, fabricating a single column
    # from multiple source columns. That is a Tier-1 audit-integrity
    # violation and must force the repair loop, not pass through as
    # advisory info.

    def _replace_csv_blob_with_duplicate_headers(self, *, headerless: bool = False) -> str:
        """Overwrite the seeded CSV blob's bytes + content_hash so it has
        duplicate headers. Must update content_hash to match the new bytes
        or the proof step's BlobIntegrityError check will fire instead.
        """
        from sqlalchemy import update

        from elspeth.web.blobs.service import content_hash as _content_hash
        from elspeth.web.sessions.models import blobs_table

        sentinel = "ELSPETH_DUPLICATE_HEADER_SENTINEL_7F3A"
        if headerless:
            new_bytes = (f"O-1,{sentinel},{sentinel},49.95\nO-2,Bob,Jones,150.00\n").encode()
        else:
            new_bytes = (f"order_id,{sentinel},{sentinel},price\nO-1,Alice,Smith,49.95\nO-2,Bob,Jones,150.00\n").encode()
        self.csv_storage_path.write_bytes(new_bytes)
        with self.engine.begin() as conn:
            conn.execute(
                update(blobs_table)
                .where(blobs_table.c.id == self.csv_blob_id)
                .values(
                    size_bytes=len(new_bytes),
                    content_hash=_content_hash(new_bytes),
                )
            )
        return sentinel

    def test_csv_duplicate_headers_blocks(self) -> None:
        """Duplicate CSV headers must surface as a blocking proof diagnostic."""
        sentinel = self._replace_csv_blob_with_duplicate_headers()
        with _blob_operation(self.engine, self.session_id) as (authority, context):
            state = self._state_with_csv_source(
                session_operation_authority=authority, session_operation_context=context, schema_mode="observed"
            )
            result = execute_tool(
                "preview_pipeline",
                {},
                state,
                _mock_catalog(),
                session_engine=self.engine,
                session_id=self.session_id,
                data_dir=str(self.data_dir),
                session_operation_authority=authority,
                session_operation_context=context,
            )
            diagnostics = result.data["proof_diagnostics"]
            codes = [d["code"] for d in diagnostics]
            assert "csv_duplicate_headers" in codes, diagnostics
            # Severity is blocking, not info.
            dup = next(d for d in diagnostics if d["code"] == "csv_duplicate_headers")
            assert dup["severity"] == "blocking", dup
            # Must carry an actionable suggested_repair string (not None) so the
            # forced-repair loop has a concrete remedy to relay to the LLM.
            assert isinstance(dup["suggested_repair"], str) and dup["suggested_repair"], dup
            model_visible_payload = json.dumps(deep_thaw(diagnostics), sort_keys=True)
            assert sentinel not in model_visible_payload
            assert "1 duplicate header value class(es)" in dup["message"]
            assert "2 duplicate column position(s)" in dup["message"]
            assert dup["evidence_locator"]["observed_header_count"] == 4
            assert dup["evidence_locator"]["duplicate_header_class_count"] == 1
            assert dup["evidence_locator"]["duplicate_header_column_count"] == 2
            assert tuple(dup["evidence_locator"]["duplicate_header_positions"]) == (2, 3)
            assert dup["evidence_locator"]["header_values_redacted"] is True
            repair = dup["suggested_repair"]
            assert "field_mapping" not in repair
            assert "on_validation_failure" not in repair
            assert "quarantine" not in repair
            assert "correct" in repair
            assert "re-upload" in repair
            assert "headerless" in repair
            assert "explicit unique `columns`" in repair
            # is_valid is forced False by the blocking proof diagnostic.
            assert result.data["preview_is_valid"] is False

    def test_explicit_unique_columns_clear_duplicate_warning_for_headerless_input(self) -> None:
        self._replace_csv_blob_with_duplicate_headers(headerless=True)
        with _blob_operation(self.engine, self.session_id) as (authority, context):
            state = self._state_with_csv_source(
                session_operation_authority=authority,
                session_operation_context=context,
                schema_mode="observed",
                columns=("order_id", "given_name", "family_name", "price"),
            )

            result = execute_tool(
                "preview_pipeline",
                {},
                state,
                _mock_catalog(),
                session_engine=self.engine,
                session_id=self.session_id,
                data_dir=str(self.data_dir),
                session_operation_authority=authority,
                session_operation_context=context,
            )

            codes = [d["code"] for d in result.data["proof_diagnostics"]]
            assert "csv_duplicate_headers" not in codes

    def test_preview_redacts_regressed_duplicate_warning_text(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Generation must not trust even a regressed source warning string."""
        import elspeth.web.composer.tools.generation as generation_module
        from elspeth.web.composer.source_inspection import SourceInspectionFacts

        raw_warning_sentinel = "RAW_GENERATION_SENTINEL_91D2"
        raw_facts = SourceInspectionFacts(
            source_kind="csv",
            redacted_identity={"filename": "orders.csv", "mime_type": "text/csv", "byte_size": "1"},
            byte_range_inspected=(0, 1),
            sample_row_count=1,
            observed_headers=("order_id", "repeated", "repeated", "price"),
            inferred_types=None,
            url_candidates=(),
            warnings=(f"csv_duplicate_headers: {raw_warning_sentinel}",),
        )
        monkeypatch.setattr(generation_module, "inspect_csv_source_content", lambda **_kwargs: raw_facts)
        with _blob_operation(self.engine, self.session_id) as (authority, context):
            state = self._state_with_csv_source(
                session_operation_authority=authority, session_operation_context=context, schema_mode="observed"
            )

            result = execute_tool(
                "preview_pipeline",
                {},
                state,
                _mock_catalog(),
                session_engine=self.engine,
                session_id=self.session_id,
                data_dir=str(self.data_dir),
                session_operation_authority=authority,
                session_operation_context=context,
            )

            diagnostics = result.data["proof_diagnostics"]
            serialized = json.dumps(deep_thaw(diagnostics), sort_keys=True)
            duplicate = next(d for d in diagnostics if d["code"] == "csv_duplicate_headers")
            assert raw_warning_sentinel not in serialized
            assert duplicate["severity"] == "blocking"
            assert duplicate["evidence_locator"]["duplicate_header_class_count"] == 1
            assert duplicate["evidence_locator"]["duplicate_header_column_count"] == 2
            assert tuple(duplicate["evidence_locator"]["duplicate_header_positions"]) == (2, 3)
            assert duplicate["evidence_locator"]["header_values_redacted"] is True
            assert "re-upload" in duplicate["suggested_repair"]

    def test_csv_without_duplicate_headers_does_not_block(self) -> None:
        """Clean headers must not produce a csv_duplicate_headers diagnostic."""
        with _blob_operation(self.engine, self.session_id) as (authority, context):
            state = self._state_with_csv_source(
                session_operation_authority=authority, session_operation_context=context, schema_mode="observed"
            )
            result = execute_tool(
                "preview_pipeline",
                {},
                state,
                _mock_catalog(),
                session_engine=self.engine,
                session_id=self.session_id,
                data_dir=str(self.data_dir),
                session_operation_authority=authority,
                session_operation_context=context,
            )
            codes = [d["code"] for d in result.data["proof_diagnostics"]]
            assert "csv_duplicate_headers" not in codes

    def test_observed_csv_numeric_gate_type_mismatch_blocks(self) -> None:
        """Observed CSV leaves values stringy; numeric gate predicates must
        fail preview before runtime hits ExpressionEvaluationError.
        """
        with _blob_operation(self.engine, self.session_id) as (authority, context):
            state = self._state_with_csv_source(
                session_operation_authority=authority, session_operation_context=context, schema_mode="observed"
            )
            result = execute_tool(
                "upsert_node",
                {
                    "id": "price_gate",
                    "node_type": "gate",
                    "plugin": None,
                    "input": "rows",
                    "condition": "row['price'] >= 100",
                    "routes": {"true": "out", "false": "out"},
                    "options": {},
                },
                state,
                _mock_catalog(),
            )
            assert result.success, result.data

            result = execute_tool(
                "preview_pipeline",
                {},
                result.updated_state,
                _mock_catalog(),
                session_engine=self.engine,
                session_id=self.session_id,
                data_dir=str(self.data_dir),
                session_operation_authority=authority,
                session_operation_context=context,
            )

            diagnostics = result.data["proof_diagnostics"]
            mismatch = [d for d in diagnostics if d["code"] == "gate_expression_type_mismatch_against_source_schema"]
            assert mismatch, diagnostics
            assert mismatch[0]["severity"] == "blocking"
            assert mismatch[0]["evidence_locator"]["node_id"] == "price_gate"
            assert mismatch[0]["evidence_locator"]["field"] == "price"
            assert result.data["preview_is_valid"] is False

    def test_observed_csv_string_amplification_gate_blocks_without_evaluating(self, monkeypatch) -> None:
        """A Mult over an observed (string-typed) field must be diagnosed
        BEFORE evaluation, never evaluated at preview (THREAT-001).

        ``row['price'] * 100000`` on observed CSV is str-repetition: sampling
        would allocate the amplified string per sampled row inside the web
        process. The amplification diagnostic replaces evaluation entirely.
        """
        from elspeth.contracts import PipelineRow
        from elspeth.core.expression_parser import ExpressionParser

        with _blob_operation(self.engine, self.session_id) as (authority, context):
            state = self._state_with_csv_source(
                session_operation_authority=authority, session_operation_context=context, schema_mode="observed"
            )
            result = execute_tool(
                "upsert_node",
                {
                    "id": "amp_gate",
                    "node_type": "gate",
                    "plugin": None,
                    "input": "rows",
                    "condition": "row['price'] * 100000",
                    "routes": {"true": "out", "false": "out"},
                    "options": {},
                },
                state,
                _mock_catalog(),
            )
            assert result.success, result.data

            evaluated_expressions: list[str] = []
            original_evaluate = ExpressionParser.evaluate

            def counting_evaluate(self: ExpressionParser, context: dict[str, Any] | PipelineRow) -> Any:
                evaluated_expressions.append(repr(self))
                return original_evaluate(self, context)

            monkeypatch.setattr(ExpressionParser, "evaluate", counting_evaluate)

            result = execute_tool(
                "preview_pipeline",
                {},
                result.updated_state,
                _mock_catalog(),
                session_engine=self.engine,
                session_id=self.session_id,
                data_dir=str(self.data_dir),
                session_operation_authority=authority,
                session_operation_context=context,
            )

            diagnostics = result.data["proof_diagnostics"]
            amplification = [d for d in diagnostics if d["code"] == "gate_expression_unbounded_string_amplification"]
            assert amplification, diagnostics
            assert amplification[0]["severity"] == "blocking"
            assert amplification[0]["evidence_locator"]["node_id"] == "amp_gate"
            assert result.data["preview_is_valid"] is False
            # The mechanism, not just the symptom: the amplifying condition was
            # never evaluated against sampled rows.
            assert not any("100000" in expr for expr in evaluated_expressions), evaluated_expressions
            # And the new branch did not misfile the finding as a type mismatch.
            mismatch = [d for d in diagnostics if d["code"] == "gate_expression_type_mismatch_against_source_schema"]
            assert not mismatch, mismatch

    def test_observed_csv_memory_error_yields_resource_code(self, monkeypatch) -> None:
        """MemoryError during sampled-row evaluation gets its own resource
        diagnostic, not the schema-typing repair (belt-and-braces arm)."""
        from elspeth.contracts import PipelineRow
        from elspeth.core.expression_parser import ExpressionParser

        with _blob_operation(self.engine, self.session_id) as (authority, context):
            state = self._state_with_csv_source(
                session_operation_authority=authority, session_operation_context=context, schema_mode="observed"
            )
            result = execute_tool(
                "upsert_node",
                {
                    "id": "oom_gate",
                    "node_type": "gate",
                    "plugin": None,
                    "input": "rows",
                    "condition": "row['price'] >= 100",
                    "routes": {"true": "out", "false": "out"},
                    "options": {},
                },
                state,
                _mock_catalog(),
            )
            assert result.success, result.data

            def exploding_evaluate(self: ExpressionParser, context: dict[str, Any] | PipelineRow) -> Any:
                raise MemoryError("simulated allocation failure")

            monkeypatch.setattr(ExpressionParser, "evaluate", exploding_evaluate)

            result = execute_tool(
                "preview_pipeline",
                {},
                result.updated_state,
                _mock_catalog(),
                session_engine=self.engine,
                session_id=self.session_id,
                data_dir=str(self.data_dir),
                session_operation_authority=authority,
                session_operation_context=context,
            )

            diagnostics = result.data["proof_diagnostics"]
            resource = [d for d in diagnostics if d["code"] == "gate_expression_preview_memory_exhaustion"]
            assert resource, diagnostics
            assert resource[0]["severity"] == "blocking"
            assert resource[0]["evidence_locator"]["node_id"] == "oom_gate"
            # A resource event must not receive the schema-typing repair.
            mismatch = [d for d in diagnostics if d["code"] == "gate_expression_type_mismatch_against_source_schema"]
            assert not mismatch, mismatch

    def test_observed_csv_batch_stats_string_value_field_blocks_through_transform(self) -> None:
        """Observed CSV strings must not reach numeric batch_stats at runtime.

        This mirrors the hard-mode p2_t2_edge failure: a string-ish field
        survives an observed-schema value_transform and batch_stats rejects it
        only when the aggregation executes.
        """
        from sqlalchemy import update

        from elspeth.web.blobs.service import content_hash as _content_hash
        from elspeth.web.sessions.models import blobs_table

        csv_content = b"respondent_id,community,financial_barrier\nR-1,Community-A,yes\nR-2,Community-B,no\n"
        self.csv_storage_path.write_bytes(csv_content)
        with self.engine.begin() as conn:
            conn.execute(
                update(blobs_table)
                .where(blobs_table.c.id == self.csv_blob_id)
                .values(
                    size_bytes=len(csv_content),
                    content_hash=_content_hash(csv_content),
                )
            )

        with _blob_operation(self.engine, self.session_id) as (authority, context):
            state = (
                self._state_with_csv_source(
                    session_operation_authority=authority, session_operation_context=context, schema_mode="observed"
                )
                .with_node(
                    NodeSpec(
                        id="classify",
                        node_type="transform",
                        plugin="value_transform",
                        input="rows",
                        on_success="classified_rows",
                        on_error="discard",
                        options={
                            "schema": {"mode": "observed"},
                            "operations": [{"target": "financial_only", "expression": "row['financial_barrier']"}],
                        },
                        condition=None,
                        routes=None,
                        fork_to=None,
                        branches=None,
                        policy=None,
                        merge=None,
                    )
                )
                .with_node(
                    NodeSpec(
                        id="summarize",
                        node_type="aggregation",
                        plugin="batch_stats",
                        input="classified_rows",
                        on_success="out",
                        on_error="discard",
                        options={
                            "schema": {"mode": "observed"},
                            "value_field": "financial_barrier",
                            "group_by": "community",
                            "compute_mean": True,
                        },
                        condition=None,
                        routes=None,
                        fork_to=None,
                        branches=None,
                        policy=None,
                        merge=None,
                    )
                )
            )

            result = execute_tool(
                "preview_pipeline",
                {},
                state,
                _mock_catalog(),
                session_engine=self.engine,
                session_id=self.session_id,
                data_dir=str(self.data_dir),
                session_operation_authority=authority,
                session_operation_context=context,
            )

            diagnostics = result.data["proof_diagnostics"]
            mismatch = [d for d in diagnostics if d["code"] == "aggregation_numeric_value_field_type_mismatch_against_source_schema"]
            assert mismatch, diagnostics
            assert mismatch[0]["severity"] == "blocking"
            assert mismatch[0]["evidence_locator"]["node_id"] == "summarize"
            assert mismatch[0]["evidence_locator"]["field"] == "financial_barrier"
            assert mismatch[0]["evidence_locator"]["observed_type"] == "str"
            assert result.data["preview_is_valid"] is False

    def test_observed_named_csv_batch_stats_string_value_field_blocks_through_transform(self) -> None:
        """Named CSV sources must participate in source-field proof walk-back."""
        from sqlalchemy import update

        from elspeth.web.blobs.service import content_hash as _content_hash
        from elspeth.web.sessions.models import blobs_table

        csv_content = b"respondent_id,community,financial_barrier\nR-1,Community-A,yes\nR-2,Community-B,no\n"
        self.csv_storage_path.write_bytes(csv_content)
        with self.engine.begin() as conn:
            conn.execute(
                update(blobs_table)
                .where(blobs_table.c.id == self.csv_blob_id)
                .values(
                    size_bytes=len(csv_content),
                    content_hash=_content_hash(csv_content),
                )
            )

        with _blob_operation(self.engine, self.session_id) as (authority, context):
            base_state = self._state_with_csv_source(
                session_operation_authority=authority, session_operation_context=context, schema_mode="observed"
            )
            named_csv_source = SourceSpec(
                plugin="csv",
                on_success="survey_rows",
                options={
                    "blob_ref": self.csv_blob_id,
                    "schema": {"mode": "observed"},
                },
                on_validation_failure="discard",
            )
            state = (
                base_state.without_source()
                .with_named_source("survey_csv", named_csv_source)
                .with_node(
                    NodeSpec(
                        id="classify",
                        node_type="transform",
                        plugin="value_transform",
                        input="survey_rows",
                        on_success="classified_rows",
                        on_error="discard",
                        options={
                            "schema": {"mode": "observed"},
                            "operations": [{"target": "financial_only", "expression": "row['financial_barrier']"}],
                        },
                        condition=None,
                        routes=None,
                        fork_to=None,
                        branches=None,
                        policy=None,
                        merge=None,
                    )
                )
                .with_node(
                    NodeSpec(
                        id="summarize",
                        node_type="aggregation",
                        plugin="batch_stats",
                        input="classified_rows",
                        on_success="out",
                        on_error="discard",
                        options={
                            "schema": {"mode": "observed"},
                            "value_field": "financial_barrier",
                            "group_by": "community",
                            "compute_mean": True,
                        },
                        condition=None,
                        routes=None,
                        fork_to=None,
                        branches=None,
                        policy=None,
                        merge=None,
                    )
                )
            )

            result = execute_tool(
                "preview_pipeline",
                {},
                state,
                _mock_catalog(),
                session_engine=self.engine,
                session_id=self.session_id,
                data_dir=str(self.data_dir),
                session_operation_authority=authority,
                session_operation_context=context,
            )

            diagnostics = result.data["proof_diagnostics"]
            mismatch = [d for d in diagnostics if d["code"] == "aggregation_numeric_value_field_type_mismatch_against_source_schema"]
            assert mismatch, diagnostics
            assert mismatch[0]["severity"] == "blocking"
            assert mismatch[0]["evidence_locator"]["node_id"] == "summarize"
            assert mismatch[0]["evidence_locator"]["field"] == "financial_barrier"
            assert mismatch[0]["evidence_locator"]["observed_type"] == "str"
            assert result.data["preview_is_valid"] is False

    def test_observed_csv_numeric_aggregation_does_not_block_after_field_overwrite(self) -> None:
        """The proof step abstains once an upstream transform overwrites the field."""
        from sqlalchemy import update

        from elspeth.web.blobs.service import content_hash as _content_hash
        from elspeth.web.sessions.models import blobs_table

        csv_content = b"respondent_id,community,financial_barrier\nR-1,Community-A,yes\nR-2,Community-B,no\n"
        self.csv_storage_path.write_bytes(csv_content)
        with self.engine.begin() as conn:
            conn.execute(
                update(blobs_table)
                .where(blobs_table.c.id == self.csv_blob_id)
                .values(
                    size_bytes=len(csv_content),
                    content_hash=_content_hash(csv_content),
                )
            )

        with _blob_operation(self.engine, self.session_id) as (authority, context):
            state = (
                self._state_with_csv_source(
                    session_operation_authority=authority, session_operation_context=context, schema_mode="observed"
                )
                .with_node(
                    NodeSpec(
                        id="coerce_flag",
                        node_type="transform",
                        plugin="value_transform",
                        input="rows",
                        on_success="coerced_rows",
                        on_error="discard",
                        options={
                            "schema": {"mode": "observed"},
                            "operations": [{"target": "financial_barrier", "expression": "1"}],
                        },
                        condition=None,
                        routes=None,
                        fork_to=None,
                        branches=None,
                        policy=None,
                        merge=None,
                    )
                )
                .with_node(
                    NodeSpec(
                        id="summarize",
                        node_type="aggregation",
                        plugin="batch_stats",
                        input="coerced_rows",
                        on_success="out",
                        on_error="discard",
                        options={
                            "schema": {"mode": "observed"},
                            "value_field": "financial_barrier",
                            "group_by": "community",
                            "compute_mean": True,
                        },
                        condition=None,
                        routes=None,
                        fork_to=None,
                        branches=None,
                        policy=None,
                        merge=None,
                    )
                )
            )

            result = execute_tool(
                "preview_pipeline",
                {},
                state,
                _mock_catalog(),
                session_engine=self.engine,
                session_id=self.session_id,
                data_dir=str(self.data_dir),
                session_operation_authority=authority,
                session_operation_context=context,
            )

            codes = [d["code"] for d in result.data["proof_diagnostics"]]
            assert "aggregation_numeric_value_field_type_mismatch_against_source_schema" not in codes

    # -- the same numeric proof for a COLLECTOR-hosted batch plugin (elspeth-1016a47e8f) --

    def _batch_barrier_behind_expand_opener(
        self,
        *,
        session_operation_authority: SQLiteLocalSessionOperationAuthority,
        session_operation_context: SessionOperationContext,
        barrier_node_type: str,
        plugin: str = "batch_stats",
        options: dict[str, Any] | None = None,
    ):
        """csv(observed) -> pdf_rasterize (scope opener) -> batch barrier -> out.

        The two barrier kinds differ in EXACTLY ONE field, `node_type` (a
        collector additionally carries the scope binding it cannot legally omit).
        Both compositions validate clean, so any difference in the emitted
        diagnostics is caused by the barrier kind and nothing else.
        """
        barrier_options: dict[str, Any] = (
            options
            if options is not None
            else {
                "schema": {"mode": "observed"},
                "value_field": "price",
                "compute_mean": True,
            }
        )
        scope_binding = (
            {"scope_name": "doc_pages", "scope_opener": "explode", "scope_policy": "require_all"}
            if barrier_node_type == "collector"
            else {}
        )
        return (
            self._state_with_csv_source(
                session_operation_authority=session_operation_authority,
                session_operation_context=session_operation_context,
                schema_mode="observed",
            )
            .with_node(
                NodeSpec(
                    id="explode",
                    node_type="transform",
                    plugin="pdf_rasterize",
                    input="rows",
                    on_success="pages",
                    on_error="discard",
                    options={"schema": {"mode": "observed"}},
                    condition=None,
                    routes=None,
                    fork_to=None,
                    branches=None,
                    policy=None,
                    merge=None,
                )
            )
            .with_node(
                NodeSpec(
                    id="summarize",
                    node_type=barrier_node_type,  # type: ignore[arg-type]
                    plugin=plugin,
                    input="pages",
                    on_success="out",
                    on_error="discard",
                    options=barrier_options,
                    condition=None,
                    routes=None,
                    fork_to=None,
                    branches=None,
                    policy=None,
                    merge=None,
                    **scope_binding,
                )
            )
        )

    def _proof_codes(
        self,
        authority: SQLiteLocalSessionOperationAuthority,
        context: SessionOperationContext,
        state: CompositionState,
    ) -> list[str]:
        result = execute_tool(
            "preview_pipeline",
            {},
            state,
            _mock_catalog(),
            session_engine=self.engine,
            session_id=self.session_id,
            data_dir=str(self.data_dir),
            session_operation_authority=authority,
            session_operation_context=context,
        )
        return result.data["proof_diagnostics"]

    @pytest.mark.parametrize("barrier_node_type", ["aggregation", "collector"])
    def test_observed_csv_numeric_value_field_blocks_for_every_batch_barrier_kind(self, barrier_node_type: str) -> None:
        """The numeric proof must not vanish when the plugin is hosted as a collector.

        Asserting the CODE is load-bearing, not stylistic: `is_valid` is False
        in BOTH arms of this harness anyway (`runtime_preflight_not_run` is a
        preview artefact), so a test asserting `is_valid` would pass against the
        defect. Before the fix the collector arm emitted ZERO diagnostics while
        the aggregation arm emitted this one — a blocking export diagnostic
        silently lost to a `node_type` gate (archived issue elspeth-1016a47e8f).
        """
        with _blob_operation(self.engine, self.session_id) as (authority, context):
            diagnostics = self._proof_codes(
                authority,
                context,
                self._batch_barrier_behind_expand_opener(
                    session_operation_authority=authority, session_operation_context=context, barrier_node_type=barrier_node_type
                ),
            )

            mismatch = [d for d in diagnostics if d["code"] == "aggregation_numeric_value_field_type_mismatch_against_source_schema"]
            assert mismatch, [d["code"] for d in diagnostics]
            assert mismatch[0]["severity"] == "blocking"
            assert mismatch[0]["evidence_locator"]["node_id"] == "summarize"
            assert mismatch[0]["evidence_locator"]["field"] == "price"
            assert mismatch[0]["evidence_locator"]["observed_type"] == "str"
            # The detector records the REAL node kind so the preflight can label the
            # blocker without guessing it back out of the (historically named) code.
            assert mismatch[0]["evidence_locator"]["node_type"] == barrier_node_type
            # Kind-accurate prose: a collector must not be told to fix a node it does
            # not have, and only a collector is told about its scope.
            assert mismatch[0]["message"].startswith(f"{barrier_node_type.capitalize()} 'summarize'")
            assert f"upstream of the {barrier_node_type}" in mismatch[0]["suggested_repair"]
            assert ("EXPAND scope" in mismatch[0]["suggested_repair"]) is (barrier_node_type == "collector")

    def test_collector_hosting_a_non_numeric_batch_plugin_emits_no_numeric_proof(self) -> None:
        """NEGATIVE control: the test above must not pass merely because a
        collector produces diagnostics at all. `batch_top_k` is batch-aware and
        collector-hostable but is NOT a numeric value_field plugin — it is the
        very plugin this diagnostic's repair text recommends instead — so it
        must stay silent on the identical topology.
        """
        with _blob_operation(self.engine, self.session_id) as (authority, context):
            diagnostics = self._proof_codes(
                authority,
                context,
                self._batch_barrier_behind_expand_opener(
                    session_operation_authority=authority,
                    session_operation_context=context,
                    barrier_node_type="collector",
                    plugin="batch_top_k",
                    options={"schema": {"mode": "observed"}, "field": "customer"},
                ),
            )

            assert "aggregation_numeric_value_field_type_mismatch_against_source_schema" not in [d["code"] for d in diagnostics]

    def test_batch_rank_numeric_value_field_blocks_behind_observed_csv(self) -> None:
        """batch_rank ranks a numeric value_field: an observed CSV string there fails every batch, so the proof fires."""
        with _blob_operation(self.engine, self.session_id) as (authority, context):
            diagnostics = self._proof_codes(
                authority,
                context,
                self._batch_barrier_behind_expand_opener(
                    session_operation_authority=authority,
                    session_operation_context=context,
                    barrier_node_type="aggregation",
                    plugin="batch_rank",
                    options={"schema": {"mode": "observed"}, "value_field": "price"},
                ),
            )

            mismatch = [d for d in diagnostics if d["code"] == "aggregation_numeric_value_field_type_mismatch_against_source_schema"]
            assert mismatch, [d["code"] for d in diagnostics]
            assert mismatch[0]["evidence_locator"]["node_id"] == "summarize"
            assert mismatch[0]["evidence_locator"]["field"] == "price"

    # -- declared-input-type mismatch against observed CSV (elspeth-e6e552ce34) --

    @staticmethod
    def _plain_transform(
        node_id: str,
        *,
        plugin: str,
        input_connection: str,
        on_success: str,
        options: dict[str, object],
    ) -> NodeSpec:
        return NodeSpec(
            id=node_id,
            node_type="transform",
            plugin=plugin,
            input=input_connection,
            on_success=on_success,
            on_error="discard",
            options=options,
            condition=None,
            routes=None,
            fork_to=None,
            branches=None,
            policy=None,
            merge=None,
        )

    def test_observed_csv_typed_transform_input_declaration_blocks(self) -> None:
        """A concrete non-str input declaration on a directly-fed transform blocks."""
        with _blob_operation(self.engine, self.session_id) as (authority, context):
            state = self._state_with_csv_source(
                session_operation_authority=authority, session_operation_context=context, schema_mode="observed"
            ).with_node(
                self._plain_transform(
                    "tidy",
                    plugin="passthrough",
                    input_connection="rows",
                    on_success="out",
                    options={"schema": {"mode": "flexible", "fields": ["price: float", "customer: str"]}},
                )
            )

            result = execute_tool(
                "preview_pipeline",
                {},
                state,
                _mock_catalog(),
                session_engine=self.engine,
                session_id=self.session_id,
                data_dir=str(self.data_dir),
                session_operation_authority=authority,
                session_operation_context=context,
            )

            diagnostics = result.data["proof_diagnostics"]
            mismatch = [d for d in diagnostics if d["code"] == "declared_input_type_mismatch_against_source_schema"]
            assert mismatch, diagnostics
            assert len(mismatch) == 1
            assert mismatch[0]["severity"] == "blocking"
            assert mismatch[0]["evidence_locator"]["node_id"] == "tidy"
            assert mismatch[0]["evidence_locator"]["field"] == "price"
            assert mismatch[0]["evidence_locator"]["declared_type"] == "float"
            assert mismatch[0]["evidence_locator"]["observed_type"] == "str"
            assert result.data["preview_is_valid"] is False

    def test_observed_csv_declared_input_proof_is_request_bounded(self) -> None:
        """Wide schemas stop at a request-wide work/result bound and fail closed."""
        from sqlalchemy import update

        from elspeth.web.blobs.service import content_hash as _content_hash
        from elspeth.web.sessions.models import blobs_table

        field_names = tuple(f"field_{index}" for index in range(300))
        csv_content = (",".join(field_names) + "\n" + ",".join("1" for _ in field_names) + "\n").encode()
        self.csv_storage_path.write_bytes(csv_content)
        with self.engine.begin() as conn:
            conn.execute(
                update(blobs_table)
                .where(blobs_table.c.id == self.csv_blob_id)
                .values(size_bytes=len(csv_content), content_hash=_content_hash(csv_content))
            )

        with _blob_operation(self.engine, self.session_id) as (blob_authority, blob_operation_context):
            state = self._state_with_csv_source(
                session_operation_authority=blob_authority, session_operation_context=blob_operation_context, schema_mode="observed"
            ).with_node(
                self._plain_transform(
                    "wide_consumer",
                    plugin="passthrough",
                    input_connection="rows",
                    on_success="out",
                    options={"schema": {"mode": "flexible", "fields": [f"{name}: int" for name in field_names]}},
                )
            )

            result = execute_tool(
                "preview_pipeline",
                {},
                state,
                _mock_catalog(),
                data_dir=str(self.data_dir),
                session_operation_authority=blob_authority,
                session_operation_context=blob_operation_context,
                session_engine=self.engine,
                session_id=self.session_id,
            )

        diagnostics = result.data["proof_diagnostics"]
        mismatches = [item for item in diagnostics if item["code"] == "declared_input_type_mismatch_against_source_schema"]
        budget_failures = [item for item in diagnostics if item["code"] == "source_inspection_failed"]
        assert len(mismatches) == 16
        assert len(budget_failures) == 1
        assert budget_failures[0]["evidence_locator"]["max_declared_input_type_checks"] == 256
        assert budget_failures[0]["evidence_locator"]["max_declared_input_type_diagnostics"] == 16
        assert result.data["preview_is_valid"] is False

        from elspeth.web.composer.discovery_response import admit_discovery_result

        admitted = admit_discovery_result("preview_pipeline", result).to_dict()
        admitted_budget_failures = [item for item in admitted["data"]["proof_diagnostics"] if item["code"] == "source_inspection_failed"]
        assert admitted_budget_failures[0]["evidence_locator"] == budget_failures[0]["evidence_locator"]

    def test_observed_csv_str_input_declaration_does_not_block(self) -> None:
        """str/any declarations match what an observed CSV actually delivers."""
        with _blob_operation(self.engine, self.session_id) as (authority, context):
            state = self._state_with_csv_source(
                session_operation_authority=authority, session_operation_context=context, schema_mode="observed"
            ).with_node(
                self._plain_transform(
                    "tidy",
                    plugin="passthrough",
                    input_connection="rows",
                    on_success="out",
                    options={"schema": {"mode": "flexible", "fields": ["price: str", "customer: any"]}},
                )
            )

            result = execute_tool(
                "preview_pipeline",
                {},
                state,
                _mock_catalog(),
                session_engine=self.engine,
                session_id=self.session_id,
                data_dir=str(self.data_dir),
                session_operation_authority=authority,
                session_operation_context=context,
            )

            codes = [d["code"] for d in result.data["proof_diagnostics"]]
            assert "declared_input_type_mismatch_against_source_schema" not in codes

    def test_observed_csv_fork_llm_coalesce_typed_declaration_blocks(self) -> None:
        """The elspeth-e6e552ce34 incident shape: fork → llm branches → coalesce →
        field_mapper declaring `id: int` over an observed CSV whose values are str.

        preview_pipeline passed twice on this exact topology in the live
        incident (session 94bdae4f, run cc2279a3) and every row quarantined at
        the field_mapper's input preflight. The walk must reach through the
        pass-through llm nodes and the union coalesce.
        """
        from sqlalchemy import update

        from elspeth.web.blobs.service import content_hash as _content_hash
        from elspeth.web.sessions.models import blobs_table

        csv_content = b"id,question\n1,What causes the seasons on Earth?\n2,Why is the sky blue?\n"
        self.csv_storage_path.write_bytes(csv_content)
        with self.engine.begin() as conn:
            conn.execute(
                update(blobs_table)
                .where(blobs_table.c.id == self.csv_blob_id)
                .values(
                    size_bytes=len(csv_content),
                    content_hash=_content_hash(csv_content),
                )
            )

        def _branch_llm(node_id: str, branch: str, response_field: str) -> NodeSpec:
            options = _llm_options_with_api_key({"secret_ref": "OPENROUTER_API_KEY"})
            options["response_field"] = response_field
            return self._plain_transform(
                node_id,
                plugin="llm",
                input_connection=branch,
                on_success=f"answered_{branch}",
                options=options,
            )

        with _blob_operation(self.engine, self.session_id) as (authority, context):
            state = (
                self._state_with_csv_source(
                    session_operation_authority=authority, session_operation_context=context, schema_mode="observed"
                )
                .with_node(
                    NodeSpec(
                        id="fan_out",
                        node_type="gate",
                        plugin=None,
                        input="rows",
                        on_success=None,
                        on_error=None,
                        options={},
                        condition="True",
                        routes={"true": "fork", "false": "fork"},
                        fork_to=("branch_a", "branch_b"),
                        branches=None,
                        policy=None,
                        merge=None,
                    )
                )
                .with_node(_branch_llm("llm_a", "branch_a", "answer_a"))
                .with_node(_branch_llm("llm_b", "branch_b", "answer_b"))
                .with_node(
                    NodeSpec(
                        id="merge_branches",
                        node_type="coalesce",
                        plugin=None,
                        input="answered_branch_a",
                        on_success="merged_rows",
                        on_error=None,
                        options={},
                        condition=None,
                        routes=None,
                        fork_to=None,
                        branches={"branch_a": "answered_branch_a", "branch_b": "answered_branch_b"},
                        policy="require_all",
                        merge="union",
                    )
                )
                .with_node(
                    self._plain_transform(
                        "tidy_columns",
                        plugin="field_mapper",
                        input_connection="merged_rows",
                        on_success="out",
                        options={
                            "schema": {
                                "mode": "flexible",
                                "fields": ["id: int", "question: str", "answer_a: str", "answer_b: str"],
                                "guaranteed_fields": ["id", "question", "answer_a", "answer_b"],
                            },
                            "mapping": {
                                "id": "id",
                                "question": "question",
                                "answer_a": "answer_a",
                                "answer_b": "answer_b",
                            },
                            "select_only": True,
                        },
                    )
                )
            )

            result = execute_tool(
                "preview_pipeline",
                {},
                state,
                _mock_catalog(),
                session_engine=self.engine,
                session_id=self.session_id,
                data_dir=str(self.data_dir),
                session_operation_authority=authority,
                session_operation_context=context,
            )

            diagnostics = result.data["proof_diagnostics"]
            mismatch = [d for d in diagnostics if d["code"] == "declared_input_type_mismatch_against_source_schema"]
            assert mismatch, diagnostics
            assert len(mismatch) == 1, mismatch
            assert mismatch[0]["evidence_locator"]["node_id"] == "tidy_columns"
            assert mismatch[0]["evidence_locator"]["field"] == "id"
            assert mismatch[0]["evidence_locator"]["declared_type"] == "int"
            assert mismatch[0]["evidence_locator"]["inferred_sample_type"] == "int"
            assert result.data["preview_is_valid"] is False

    def test_observed_csv_typed_sink_schema_blocks(self) -> None:
        """The same contradiction one node further on: a fixed sink schema
        declaring `order_id: int` is armed even when every transform is clean."""
        with _blob_operation(self.engine, self.session_id) as (authority, context):
            state = (
                self._state_with_csv_source(
                    session_operation_authority=authority, session_operation_context=context, schema_mode="observed"
                )
                .with_node(
                    self._plain_transform(
                        "forward",
                        plugin="passthrough",
                        input_connection="rows",
                        on_success="typed_out",
                        options={"schema": {"mode": "observed"}},
                    )
                )
                .with_output(
                    OutputSpec(
                        name="typed_out",
                        plugin="json",
                        options={
                            "path": "outputs/typed.json",
                            "schema": {"mode": "fixed", "fields": ["order_id: int", "customer: str", "price: str"]},
                            "mode": "write",
                            "collision_policy": "auto_increment",
                        },
                        on_write_failure="discard",
                    )
                )
            )

            result = execute_tool(
                "preview_pipeline",
                {},
                state,
                _mock_catalog(),
                session_engine=self.engine,
                session_id=self.session_id,
                data_dir=str(self.data_dir),
                session_operation_authority=authority,
                session_operation_context=context,
            )

            diagnostics = result.data["proof_diagnostics"]
            mismatch = [d for d in diagnostics if d["code"] == "declared_input_type_mismatch_against_source_schema"]
            assert mismatch, diagnostics
            assert len(mismatch) == 1, mismatch
            assert mismatch[0]["evidence_locator"]["output_name"] == "typed_out"
            assert mismatch[0]["evidence_locator"]["field"] == "order_id"
            assert mismatch[0]["evidence_locator"]["declared_type"] == "int"
            assert result.data["preview_is_valid"] is False

    def test_malformed_node_schema_block_abstains_without_crashing(self) -> None:
        """A node whose schema block cannot be parsed is an abstention, not a
        diagnostic: contract-config validation owns reporting it, and the proof
        step must not double-report or escape as a ValueError through
        preview_pipeline (the tool dispatcher only catches ToolArgumentError)."""
        with _blob_operation(self.engine, self.session_id) as (authority, context):
            state = self._state_with_csv_source(
                session_operation_authority=authority, session_operation_context=context, schema_mode="observed"
            ).with_node(
                self._plain_transform(
                    "tidy",
                    plugin="passthrough",
                    input_connection="rows",
                    on_success="out",
                    options={"schema": {"mode": "flexible", "fields": ["price: not_a_real_type"]}},
                )
            )

            result = execute_tool(
                "preview_pipeline",
                {},
                state,
                _mock_catalog(),
                session_engine=self.engine,
                session_id=self.session_id,
                data_dir=str(self.data_dir),
                session_operation_authority=authority,
                session_operation_context=context,
            )

            codes = [d["code"] for d in result.data["proof_diagnostics"]]
            assert "declared_input_type_mismatch_against_source_schema" not in codes

    def test_unparseable_schema_abstention_is_carried_structurally(self) -> None:
        """The abstention is a distinct fact from "declares nothing typed" —
        collapsing both into an empty tuple made the detector's own abstention
        invisible to its tests (lint R6 swallow, review finding)."""
        from elspeth.web.composer.tools.generation import _DeclaredConcreteFields

        unparseable = _DeclaredConcreteFields(fields=(), schema_unparseable=True)
        nothing_declared = _DeclaredConcreteFields(fields=(), schema_unparseable=False)

        assert unparseable != nothing_declared
        assert unparseable.abstains and nothing_declared.abstains

    def test_declared_source_schema_types_do_not_trip_the_observed_arm(self) -> None:
        """A source that declares its types coerces at ingestion — no diagnostic."""
        with _blob_operation(self.engine, self.session_id) as (authority, context):
            state = self._state_with_csv_source(
                session_operation_authority=authority,
                session_operation_context=context,
                schema_mode="flexible",
                fields=("order_id: str", "customer: str", "price: float"),
            ).with_node(
                self._plain_transform(
                    "tidy",
                    plugin="passthrough",
                    input_connection="rows",
                    on_success="out",
                    options={"schema": {"mode": "flexible", "fields": ["price: float"]}},
                )
            )

            result = execute_tool(
                "preview_pipeline",
                {},
                state,
                _mock_catalog(),
                session_engine=self.engine,
                session_id=self.session_id,
                data_dir=str(self.data_dir),
                session_operation_authority=authority,
                session_operation_context=context,
            )

            codes = [d["code"] for d in result.data["proof_diagnostics"]]
            assert "declared_input_type_mismatch_against_source_schema" not in codes

    def test_csv_duplicate_headers_registered_as_blocking_code(self) -> None:
        """Registry membership ripples — the constructor would crash if the
        emission site used an unregistered code, so this test pins the
        canonical-vocabulary contract independently of emission."""
        from elspeth.web.composer.tools import _BLOCKING_DIAGNOSTIC_CODES

        assert "csv_duplicate_headers" in _BLOCKING_DIAGNOSTIC_CODES
        assert "csv_source_field_resolution_error" in _BLOCKING_DIAGNOSTIC_CODES
        assert "declared_input_type_mismatch_against_source_schema" in _BLOCKING_DIAGNOSTIC_CODES

    # -- missing/unreadable blob --------------------------------------------

    def test_missing_storage_file_blocks(self) -> None:
        self.csv_storage_path.unlink()
        with _blob_operation(self.engine, self.session_id) as (authority, context):
            state = self._state_with_csv_source(
                session_operation_authority=authority, session_operation_context=context, schema_mode="observed"
            )
            result = execute_tool(
                "preview_pipeline",
                {},
                state,
                _mock_catalog(),
                session_engine=self.engine,
                session_id=self.session_id,
                data_dir=str(self.data_dir),
                session_operation_authority=authority,
                session_operation_context=context,
            )
            codes = [d["code"] for d in result.data["proof_diagnostics"]]
            assert "source_inspection_failed" in codes
            assert result.data["preview_is_valid"] is False

    def test_blocking_proof_overrides_authoring_validation(self, passing_runtime_preflight: _SyncCallRecorder) -> None:
        """Authoring is valid but a blocking proof diagnostic still flips ``preview_is_valid``.

        The runtime check is held green so the proof stage is the only stage
        that can move the verdict: with authoring valid on the envelope and
        the preflight passing, a false ``preview_is_valid`` can only be the
        blocking diagnostic (pin 3 of elspeth-e405ad7cd2 R4).
        """
        with _blob_operation(self.engine, self.session_id) as (authority, context):
            state = self._state_with_csv_source(
                session_operation_authority=authority,
                session_operation_context=context,
                schema_mode="fixed",
                fields=("order_id: str",),
                on_validation_failure="discard",
            )
            # Route the source's ``rows`` connection into the sink so the
            # authoring check is genuinely green; the helper's ``out`` sink leaves
            # ``rows`` unconsumed, which would make authoring — not the proof
            # stage — the reason for the verdict below.
            removed = execute_tool("remove_output", {"sink_name": "out"}, state, _mock_catalog())
            assert removed.success, removed.data
            routed = execute_tool(
                "set_output",
                {
                    "sink_name": "rows",
                    "plugin": "json",
                    "options": {
                        "path": "outputs/out.json",
                        "schema": {"mode": "observed"},
                        "mode": "write",
                        "collision_policy": "auto_increment",
                    },
                    "on_write_failure": "discard",
                },
                removed.updated_state,
                _mock_catalog(),
            )
            assert routed.success, routed.data
            state = routed.updated_state
            result = execute_tool(
                "preview_pipeline",
                {},
                state,
                _mock_catalog(),
                session_engine=self.engine,
                session_id=self.session_id,
                data_dir=str(self.data_dir),
                session_operation_authority=authority,
                session_operation_context=context,
                runtime_preflight=passing_runtime_preflight,
            )
            assert result.validation.is_valid is True
            assert result.runtime_preflight is not None
            assert result.runtime_preflight.is_valid is True
            assert any(d["severity"] == "blocking" for d in result.data["proof_diagnostics"])
            assert result.data["preview_is_valid"] is False
            assert list(result.data["preview_errors"]) == []

    # -- Tier-3 persisted-option boundaries: malformed source.options ---------
    # ``source.options`` is composer/operator-authored config re-read from
    # persisted session state — Tier-3 origin. A drifted / hand-edited / stale
    # store can carry a malformed value that the helpers re-validate on read,
    # raising ``ValueError``. ``compute_proof_diagnostics`` is called UNWRAPPED
    # from the preview tool and the dispatcher only catches ``ToolArgumentError``,
    # so an unhandled ``ValueError`` would crash the tool. These pin the boundary
    # at the ``compute_proof_diagnostics`` level: each malformed option must yield
    # a blocking diagnostic and NO exception may escape. The tampered options are
    # injected via ``dataclasses.replace`` to model persisted state that drifted
    # after the set_source_from_blob write-time validation that originally
    # produced it (the "validated once is not validated forever" T3 read-back).

    def _state_with_tampered_source_options(
        self,
        authority: SQLiteLocalSessionOperationAuthority,
        context: SessionOperationContext,
        options: dict[str, object],
    ):
        """Build a valid CSV blob state, then overwrite source.options.

        Models persisted external-origin options that drifted between the
        write-time validation and this read.
        """
        import dataclasses

        state = self._state_with_csv_source(
            session_operation_authority=authority, session_operation_context=context, schema_mode="observed"
        )
        source = _default_source(state)
        assert source is not None
        tampered = dataclasses.replace(source, options=options)
        return state.with_source(tampered)

    def test_proof_malformed_schema_block_yields_blocking_diagnostic(self) -> None:
        """A malformed ``schema`` block (get_raw_schema_config raises) blocks,

        does not crash the tool.
        """
        from elspeth.web.composer.tools import compute_proof_diagnostics

        # schema.mode is not a recognised mode → get_raw_schema_config raises
        # ValueError. blob_ref must survive so the proof step inspects the blob.
        with _blob_operation(self.engine, self.session_id) as (authority, context):
            state = self._state_with_tampered_source_options(
                authority, context, {"blob_ref": self.csv_blob_id, "schema": {"mode": "not_a_real_mode"}}
            )
            diagnostics = compute_proof_diagnostics(
                state,
                session_engine=self.engine,
                session_id=self.session_id,
                data_dir=str(self.data_dir),
                session_operation_authority=authority,
                session_operation_context=context,
            )
            blocking = [d for d in diagnostics if d["severity"] == "blocking"]
            assert any(d["code"] == "csv_source_field_resolution_error" for d in blocking), diagnostics
            # The schema-block builder's repair text must point at the schema knob,
            # not at header/field_mapping resolution.
            schema_diag = next(d for d in blocking if d["code"] == "csv_source_field_resolution_error")
            assert "schema" in schema_diag["suggested_repair"]
            assert "schema.mode" in schema_diag["suggested_repair"]

    def test_proof_malformed_columns_option_yields_blocking_diagnostic(self) -> None:
        """A malformed ``columns`` option (inspect wrap raises) blocks, no crash."""
        from elspeth.web.composer.tools import compute_proof_diagnostics

        # columns must be a sequence of str; an int member raises ValueError
        # inside _csv_source_columns, surfaced by the inspect-call wrap.
        with _blob_operation(self.engine, self.session_id) as (authority, context):
            state = self._state_with_tampered_source_options(
                authority, context, {"blob_ref": self.csv_blob_id, "columns": ["order_id", 123]}
            )
            diagnostics = compute_proof_diagnostics(
                state,
                session_engine=self.engine,
                session_id=self.session_id,
                data_dir=str(self.data_dir),
                session_operation_authority=authority,
                session_operation_context=context,
            )
            blocking = [d for d in diagnostics if d["severity"] == "blocking"]
            assert any(d["code"] == "csv_source_field_resolution_error" for d in blocking), diagnostics

    def test_proof_malformed_delimiter_option_yields_blocking_diagnostic(self) -> None:
        """A malformed ``delimiter`` option (inspect wrap raises) blocks, no crash."""
        from elspeth.web.composer.tools import compute_proof_diagnostics

        # delimiter must be a single character; a multi-char string raises
        # ValueError inside _csv_source_delimiter, surfaced by the inspect wrap.
        with _blob_operation(self.engine, self.session_id) as (authority, context):
            state = self._state_with_tampered_source_options(authority, context, {"blob_ref": self.csv_blob_id, "delimiter": "||"})
            diagnostics = compute_proof_diagnostics(
                state,
                session_engine=self.engine,
                session_id=self.session_id,
                data_dir=str(self.data_dir),
                session_operation_authority=authority,
                session_operation_context=context,
            )
            blocking = [d for d in diagnostics if d["severity"] == "blocking"]
            assert any(d["code"] == "csv_source_field_resolution_error" for d in blocking), diagnostics

    # -- proof step integrity verification -----------------------------------
    # The proof step reads blob bytes through the same Tier-1 invariants as
    # _execute_get_blob_content and _execute_inspect_source: NULL stored
    # content_hash escalates via AuditIntegrityError; SHA-256 mismatch
    # escalates via BlobIntegrityError. Without these the audit trail would
    # accept LLM repair turns driven by unverified bytes.

    def test_proof_raises_blob_integrity_error_on_hash_mismatch(self) -> None:
        """Tampering with on-disk bytes after upload must raise, not soft-fail.

        The proof step reads the blob's bytes; if SHA-256 of the bytes
        does not match the stored content_hash, that's a Tier-1 anomaly
        (filesystem corruption, tampering, or write-path bug) and must
        ESCALATE — not silently let downstream LLM repair turns act on
        garbage.
        """
        from elspeth.web.blobs.protocol import BlobIntegrityError

        with _blob_operation(self.engine, self.session_id) as (authority, context):
            state = self._state_with_csv_source(
                session_operation_authority=authority, session_operation_context=context, schema_mode="observed"
            )
            # Tamper with the on-disk bytes after the row was inserted.
            self.csv_storage_path.write_bytes(b"tampered,data\nX,Y\n")
            with pytest.raises(BlobIntegrityError):
                execute_tool(
                    "preview_pipeline",
                    {},
                    state,
                    _mock_catalog(),
                    session_engine=self.engine,
                    session_id=self.session_id,
                    data_dir=str(self.data_dir),
                    session_operation_authority=authority,
                    session_operation_context=context,
                )

    def test_proof_raises_audit_integrity_error_on_null_content_hash(self) -> None:
        """A ``ready`` blob with NULL content_hash is a DB-integrity anomaly.

        Enforced at write time by the ``ck_blobs_ready_hash`` CHECK
        constraint. If the proof step ever observes NULL here, the
        constraint was bypassed (or the database is corrupt). Must
        ESCALATE via AuditIntegrityError; cannot soft-degrade to "no
        diagnostics" because that would let an unverified blob drive
        repair turns.
        """
        from sqlalchemy import update

        from elspeth.contracts.errors import AuditIntegrityError
        from elspeth.web.sessions.models import blobs_table

        # Bypass the CHECK constraint by suspending it; sqlite respects
        # ``PRAGMA defer_foreign_keys`` but not check toggles inline,
        # so we drop and recreate without the constraint for the test
        # row. Simpler: directly patch via raw SQL with a workaround
        # — but the cleanest test path is to set status='ready' and
        # NULL the hash explicitly via UPDATE; sqlite will reject the
        # CHECK if defined. Use a raw SQL UPDATE that violates the
        # check guard if present, else falls through; the row's
        # presence on read with NULL hash is what we need.
        with _blob_operation(self.engine, self.session_id) as (authority, context):
            state = self._state_with_csv_source(
                session_operation_authority=authority, session_operation_context=context, schema_mode="observed"
            )
            with self.engine.begin() as conn:
                # Disable the row-level check by toggling pragma; sqlite
                # 3.x respects this for the connection. Then null the hash.
                conn.exec_driver_sql("PRAGMA ignore_check_constraints = ON")
                conn.execute(update(blobs_table).where(blobs_table.c.id == self.csv_blob_id).values(content_hash=None))
                conn.exec_driver_sql("PRAGMA ignore_check_constraints = OFF")

            with pytest.raises(AuditIntegrityError, match="NULL content_hash"):
                execute_tool(
                    "preview_pipeline",
                    {},
                    state,
                    _mock_catalog(),
                    session_engine=self.engine,
                    session_id=self.session_id,
                    data_dir=str(self.data_dir),
                    session_operation_authority=authority,
                    session_operation_context=context,
                )


class TestFieldPreservationWalk:
    """Direct tests of the proof step's field-preservation walk arms.

    Adversarial cases for the fail-closed detector (the walk feeds a BLOCKING
    diagnostic, so a wrong positive wedges a valid pipeline): each arm is
    exercised in both the preserve and the abstain direction, against the
    real plugin registry — no catalog or blob custody needed.
    """

    @staticmethod
    def _observed_csv_state():
        return _empty_state().with_source(
            SourceSpec(
                plugin="csv",
                on_success="rows",
                options={"path": "/data/in.csv", "schema": {"mode": "observed"}},
                on_validation_failure="discard",
            )
        )

    @staticmethod
    def _node(node_id: str, **overrides: Any) -> NodeSpec:
        base: dict[str, Any] = {
            "id": node_id,
            "node_type": "transform",
            "plugin": "passthrough",
            "input": "rows",
            "on_success": "done",
            "on_error": "discard",
            "options": {"schema": {"mode": "observed"}},
            "condition": None,
            "routes": None,
            "fork_to": None,
            "branches": None,
            "policy": None,
            "merge": None,
        }
        base.update(overrides)
        return NodeSpec(**base)

    def _walk(self, state, connection: str, field: str) -> bool:
        from elspeth.web.composer.tools.generation import _source_field_reaches_connection_without_type_change

        return _source_field_reaches_connection_without_type_change(
            state,
            connection,
            source_name="source",
            field_name=field,
        )

    def test_type_coerce_declared_fields_abstain(self) -> None:
        """type_coerce re-declares every input field in its OUTPUT config, so
        membership abstention covers both the conversion target (the value
        provably changed type) and the untouched declared field. The second
        abstention is deliberate conservatism, not a defect: the walk is
        shared with the gate-sampling arm, which needs VALUE preservation —
        a declared field may have been rewritten to a different value of the
        same type, so mere same-type declarations cannot clear it."""
        state = self._observed_csv_state().with_node(
            self._node(
                "coerce",
                plugin="type_coerce",
                options={
                    "schema": {"mode": "flexible", "fields": ["price: str", "customer: str"]},
                    "conversions": [{"field": "price", "to": "float"}],
                },
            )
        )
        assert self._walk(state, "done", "price") is False
        assert self._walk(state, "done", "customer") is False

    def test_llm_pass_through_preserves_undeclared_fields(self) -> None:
        """An llm declares only its ADDED fields (response_field and its
        siblings) in its output config, so a source field recurses through —
        the arm that closed the elspeth-e6e552ce34 walk gap."""
        options = _llm_options_with_api_key({"secret_ref": "OPENROUTER_API_KEY"})
        options["response_field"] = "answer_a"
        state = self._observed_csv_state().with_node(self._node("branch_llm", plugin="llm", options=options))
        assert self._walk(state, "done", "price") is True
        assert self._walk(state, "done", "answer_a") is False

    def test_observed_mode_type_coerce_abstains_entirely(self) -> None:
        """A pass-through transform that declares NO output fields has opted out
        of the declaration discipline — silence is not type-preservation
        (the observed-mode type_coerce trap, elspeth-85e8afa2f5)."""
        state = self._observed_csv_state().with_node(
            self._node(
                "coerce",
                plugin="type_coerce",
                options={
                    "schema": {"mode": "observed"},
                    "conversions": [{"field": "price", "to": "float"}],
                },
            )
        )
        assert self._walk(state, "done", "price") is False
        assert self._walk(state, "done", "customer") is False

    def _fork_coalesce_state(self, *, merge: str, branch_b_plugin: str = "passthrough", branch_b_options: dict[str, Any] | None = None):
        return (
            self._observed_csv_state()
            .with_node(
                self._node(
                    "fan_out",
                    node_type="gate",
                    plugin=None,
                    on_success=None,
                    on_error=None,
                    options={},
                    condition="True",
                    routes={"true": "fork", "false": "fork"},
                    fork_to=("branch_a", "branch_b"),
                )
            )
            .with_node(self._node("path_a", input="branch_a", on_success="done_a"))
            .with_node(
                self._node(
                    "path_b",
                    plugin=branch_b_plugin,
                    input="branch_b",
                    on_success="done_b",
                    options=branch_b_options or {"schema": {"mode": "observed"}},
                )
            )
            .with_node(
                self._node(
                    "merge_it",
                    node_type="coalesce",
                    plugin=None,
                    input="done_a",
                    on_success="merged",
                    on_error=None,
                    options={},
                    branches={"branch_a": "done_a", "branch_b": "done_b"},
                    policy="require_all",
                    merge=merge,
                )
            )
        )

    def test_union_coalesce_preserves_when_all_branches_preserve(self) -> None:
        state = self._fork_coalesce_state(merge="union")
        assert self._walk(state, "merged", "price") is True

    def test_nested_coalesce_abstains(self) -> None:
        """A nested merge rewrites the top level to branch names — no top-level
        source field survives it."""
        state = self._fork_coalesce_state(merge="nested")
        assert self._walk(state, "merged", "price") is False

    def test_union_coalesce_abstains_when_one_branch_overwrites(self) -> None:
        """Unanimity: one branch overwriting the field collapses the vote."""
        state = self._fork_coalesce_state(
            merge="union",
            branch_b_plugin="value_transform",
            branch_b_options={
                "schema": {"mode": "observed"},
                "operations": [{"target": "price", "expression": "1"}],
            },
        )
        assert self._walk(state, "merged", "price") is False

    def test_shared_queue_predecessors_are_evaluated_once(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Layered queue fan-in remains linear in the authored graph size."""
        from elspeth.web.composer._producer_resolver import ProducerResolver

        state = self._observed_csv_state().with_named_source(
            "other",
            SourceSpec(
                plugin="csv",
                on_success="queue_0_a",
                options={"path": "/data/other.csv", "schema": {"mode": "observed"}},
                on_validation_failure="discard",
            ),
        )
        state = state.with_node(self._node("queue_0_a", node_type="queue", plugin=None, input="queue_0_a", on_success=None))
        state = state.with_node(self._node("queue_0_b", node_type="queue", plugin=None, input="queue_0_b", on_success=None))
        depth = 8
        for layer in range(1, depth + 1):
            previous = layer - 1
            destinations = (f"queue_{layer}_a", f"queue_{layer}_b")
            for branch in ("a", "b"):
                state = state.with_node(
                    self._node(
                        f"gate_{layer}_{branch}",
                        node_type="gate",
                        plugin=None,
                        input=f"queue_{previous}_{branch}",
                        on_success=None,
                        condition="True",
                        routes={"true": destinations[0], "false": destinations[1]},
                        options={},
                    )
                )
            for destination in destinations:
                state = state.with_node(self._node(destination, node_type="queue", plugin=None, input=destination, on_success=None))

        calls = 0
        original = ProducerResolver.queue_predecessors

        def counting_queue_predecessors(resolver: ProducerResolver, queue_id: str):
            nonlocal calls
            calls += 1
            return original(resolver, queue_id)

        monkeypatch.setattr(ProducerResolver, "queue_predecessors", counting_queue_predecessors)

        assert self._walk(state, f"queue_{depth}_a", "price") is False
        assert calls <= (2 * depth) + 1


class TestBlockingDiagnosticRegistry:
    """``_blocking_diagnostic`` enforces the canonical-codes invariant.

    The skill markdown that drives the composer LLM cites these codes by
    name; if a contributor adds a new blocker without registering the code
    in ``_BLOCKING_DIAGNOSTIC_CODES``, the LLM's repair vocabulary drifts
    silently. The constructor's runtime assertion turns that drift into a
    crash at the construction site.
    """

    def test_unregistered_code_raises_at_construction(self) -> None:
        from elspeth.web.composer.tools import _blocking_diagnostic

        with pytest.raises(AssertionError, match="not registered in _BLOCKING_DIAGNOSTIC_CODES"):
            _blocking_diagnostic(
                code="this_code_was_never_registered",
                message="msg",
                suggested_repair="repair",
                evidence_locator={},
            )

    def test_registered_codes_construct_successfully(self) -> None:
        from elspeth.web.composer.tools import _BLOCKING_DIAGNOSTIC_CODES, _blocking_diagnostic

        for code in _BLOCKING_DIAGNOSTIC_CODES:
            d = _blocking_diagnostic(
                code=code,
                message="msg",
                suggested_repair="repair",
                evidence_locator={"source": "blob", "blob_id": "abc"},
            )
            # Construction sets severity blocking and preserves the inputs.
            assert d["code"] == code
            assert d["severity"] == "blocking"
            assert d["message"] == "msg"
            assert d["suggested_repair"] == "repair"
            assert d["evidence_locator"] == {"source": "blob", "blob_id": "abc"}


class TestToolContextSecretServiceTyping:
    """Type-contract tests for ``ToolContext.secret_service``.

    Prior to elspeth-d017c958e9 the field was ``Any | None``, which
    silently accepted any object — including the wrong production
    surface (``WebSecretService``, which requires ``auth_provider_type``)
    or a misnamed kwarg. The field now annotates ``WebSecretResolver``
    (L0 protocol from ``elspeth.contracts.secrets``), and production
    wiring passes ``ScopedSecretResolver``.

    These tests pin the structural contract so the dispatch boundary
    stays typed.
    """

    def test_scoped_secret_resolver_satisfies_protocol(self) -> None:
        """Production ``ScopedSecretResolver`` must satisfy the protocol."""
        from elspeth.contracts.secrets import WebSecretResolver
        from elspeth.web.secrets.service import ScopedSecretResolver

        # ScopedSecretResolver only depends on its inner service for
        # delegation; structural-typing checks need only the methods,
        # not a live DB.
        class _ServiceStub:
            def list_refs(self, user_id: str, *, auth_provider_type: str) -> list[Any]:
                return []

            def has_ref(self, user_id: str, name: str, *, auth_provider_type: str) -> bool:
                return False

            def resolve(self, user_id: str, name: str, *, auth_provider_type: str) -> None:
                return None

        resolver = ScopedSecretResolver(_ServiceStub(), auth_provider_type="local")  # type: ignore[arg-type]
        assert isinstance(resolver, WebSecretResolver)

    def test_tool_context_accepts_protocol_implementor(self) -> None:
        """ToolContext.secret_service must accept any WebSecretResolver."""

        class _ResolverStub:
            def list_refs(self, user_id: str) -> list[Any]:
                return []

            def has_ref(self, user_id: str, name: str) -> bool:
                return False

            def resolve(self, user_id: str, name: str) -> None:
                return None

        ctx = _trained_tool_context(secret_service=_ResolverStub())
        # Field is reachable, typed, and forwards the structural surface.
        assert ctx.secret_service is not None
        assert ctx.secret_service.has_ref("u1", "missing") is False
        assert ctx.secret_service.list_refs("u1") == []

    def test_tool_context_secret_service_defaults_to_none(self) -> None:
        """Non-secret-aware callers (legacy direct tests) construct with None."""
        ctx = _trained_tool_context()
        assert ctx.secret_service is None


# ---------------------------------------------------------------------------
# Queue fan-in exposure through the mutation tools (elspeth-a5b86149d4 /
# elspeth-6421ffa028). The canonical structural queue NodeSpec is
# id == input, node_type="queue", plugin=None, on_success/on_error None,
# description-only options, every other field None. The single intrinsic
# guard is state.queue_node_contract_error; upsert_node must accept a
# canonical queue and reject every intrinsic malformation atomically.
# ---------------------------------------------------------------------------

_QUEUE_UPSERT_ARGS: dict[str, Any] = {
    "id": "inbound",
    "node_type": "queue",
    "plugin": None,
    "input": "inbound",
    "on_success": None,
    "on_error": None,
    "options": {"description": "Orders and refunds interleave here"},
}


def _upsert_node_definition() -> Mapping[str, Any]:
    definition = next(d for d in get_tool_definitions() if d["name"] == "upsert_node")
    assert isinstance(definition, Mapping)
    return definition


class TestUpsertNodeQueue:
    def test_closed_node_type_enum_includes_queue(self) -> None:
        """The upsert_node declaration's closed node_type enum advertises queue."""
        enum = _upsert_node_definition()["parameters"]["properties"]["node_type"]["enum"]
        assert "queue" in enum
        # The four pre-existing node types remain advertised.
        for existing in ("transform", "gate", "aggregation", "coalesce"):
            assert existing in enum

    def test_declaration_documents_queue_contract(self) -> None:
        """The tool description states the id == input / plugin-omitted / description-only contract."""
        description = _upsert_node_definition()["description"]
        assert "queue" in description.lower()
        assert "id == input" in description

    def test_upsert_canonical_queue_succeeds_and_persists(self) -> None:
        result = execute_tool("upsert_node", dict(_QUEUE_UPSERT_ARGS), _empty_state(), _mock_catalog())
        assert result.success is True
        assert result.updated_state.version == 2
        queue = next(n for n in result.updated_state.nodes if n.id == "inbound")
        assert queue.node_type == "queue"
        assert queue.input == "inbound"
        assert queue.plugin is None
        assert queue.on_success is None
        assert queue.on_error is None
        assert dict(queue.options) == {"description": "Orders and refunds interleave here"}
        # Every routing/shape field a canonical queue must leave unset, written
        # out one per line: the explicit list is the contract, and an added
        # NodeSpec field shows up here as a missing assertion rather than
        # silently passing a name-driven loop.
        assert queue.condition is None
        assert queue.routes is None
        assert queue.fork_to is None
        assert queue.branches is None
        assert queue.policy is None
        assert queue.merge is None
        assert queue.trigger is None
        assert queue.output_mode is None
        assert queue.expected_output_count is None

    def test_orphan_queue_persists_even_though_validation_reports_incomplete(self) -> None:
        """Incremental authoring: an orphan canonical queue is a mutation SUCCESS.

        Pipeline completeness (missing producers / downstream) is validation
        telemetry, not a mutation rejection.
        """
        result = execute_tool("upsert_node", dict(_QUEUE_UPSERT_ARGS), _empty_state(), _mock_catalog())
        assert result.success is True
        assert any(n.id == "inbound" for n in result.updated_state.nodes)
        # The queue has no producer and no downstream consumer yet, so the
        # validation summary is not valid — but the mutation still landed.
        assert result.validation.is_valid is False

    @pytest.mark.parametrize(
        "override",
        [
            {"input": "elsewhere"},
            {"plugin": "passthrough"},
            {"on_success": "downstream"},
            {"on_error": "errors"},
            {"condition": "row['x'] > 0"},
            {"routes": {"true": "a", "false": "b"}},
            {"fork_to": ["a", "b"]},
            {"branches": ["a", "b"]},
            {"policy": "all"},
            {"merge": "first"},
            {"trigger": {"count": 5}},
            {"output_mode": "passthrough"},
            {"expected_output_count": 3},
            {"options": {"description": "ok", "priority": "high"}},
            {"options": {"description": 123}},
        ],
    )
    def test_intrinsically_malformed_queue_is_rejected_atomically(self, override: dict[str, Any]) -> None:
        state = _empty_state()
        args = {**_QUEUE_UPSERT_ARGS, **override}
        result = execute_tool("upsert_node", args, state, _mock_catalog())
        assert result.success is False
        # Atomic: the exact prior state/version is untouched, no partial node.
        assert result.updated_state.version == state.version
        assert all(n.id != "inbound" for n in result.updated_state.nodes)


_ROW_UNION_UPSERT_ARGS: dict[str, Any] = {
    "id": "variant_union",
    "node_type": "row_union",
    "plugin": None,
    "input": "control_done",
    "on_success": "unioned_rows",
    "on_error": None,
    "options": {},
    "branches": {
        "control": "control_done",
        "treatment": "treatment_done",
    },
    "timeout_seconds": 45.0,
}


def _row_union_node_spec() -> NodeSpec:
    return NodeSpec(
        id="variant_union",
        node_type="row_union",
        plugin=None,
        input="control_done",
        on_success="unioned_rows",
        on_error=None,
        options={},
        condition=None,
        routes=None,
        fork_to=None,
        branches={
            "control": "control_done",
            "treatment": "treatment_done",
        },
        policy=None,
        merge=None,
        timeout_seconds=45.0,
    )


class TestUpsertNodeRowUnion:
    def test_generic_tool_schema_advertises_row_union_and_structural_timeout(self) -> None:
        definitions = get_tool_definitions()
        names = {definition["name"] for definition in definitions}
        assert "upsert_row_union" not in names

        upsert = _upsert_node_definition()
        assert "row_union" in upsert["parameters"]["properties"]["node_type"]["enum"]
        timeout = upsert["parameters"]["properties"]["timeout_seconds"]
        assert timeout["type"] == ["number", "null"]
        assert timeout["exclusiveMinimum"] == 0
        assert "finite" in timeout["description"]

    def test_upsert_persists_canonical_row_union_and_timeout(self) -> None:
        result = execute_tool("upsert_node", dict(_ROW_UNION_UPSERT_ARGS), _empty_state(), _mock_catalog())

        assert result.success is True, result.to_dict()
        union = next(node for node in result.updated_state.nodes if node.id == "variant_union")
        assert union.node_type == "row_union"
        assert union.plugin is None
        assert union.input == "control_done"
        assert dict(union.branches or {}) == {
            "control": "control_done",
            "treatment": "treatment_done",
        }
        assert union.on_success == "unioned_rows"
        assert union.timeout_seconds == 45.0

        inspected = execute_tool(
            "get_pipeline_state",
            {"component": "variant_union"},
            result.updated_state,
            _mock_catalog(),
        )
        assert inspected.success is True
        assert inspected.data["node"]["timeout_seconds"] == 45.0

    @pytest.mark.parametrize("invalid_timeout", [True, "30", 0, -1, float("nan"), float("inf")])
    def test_timeout_rejects_invalid_tier_3_values_without_mutation(self, invalid_timeout: object) -> None:
        from elspeth.web.composer.protocol import ToolArgumentError

        state = _empty_state()

        with pytest.raises(ToolArgumentError):
            execute_tool(
                "upsert_node",
                {**_ROW_UNION_UPSERT_ARGS, "timeout_seconds": invalid_timeout},
                state,
                _mock_catalog(),
            )

        assert state.version == 1
        assert state.nodes == ()

    @pytest.mark.parametrize("timeout_seconds", [30, 30.5])
    def test_timeout_accepts_actual_int_and_float_values(self, timeout_seconds: int | float) -> None:
        result = execute_tool(
            "upsert_node",
            {**_ROW_UNION_UPSERT_ARGS, "timeout_seconds": timeout_seconds},
            _empty_state(),
            _mock_catalog(),
        )

        assert result.success is True, result.to_dict()
        assert result.updated_state.nodes[0].timeout_seconds == float(timeout_seconds)

    @pytest.mark.parametrize(
        "arguments",
        [
            {
                "id": "transform_node",
                "node_type": "transform",
                "plugin": "passthrough",
                "input": "rows",
                "on_success": "transformed",
                "on_error": "discard",
                "options": {"schema": {"mode": "observed"}},
                "timeout_seconds": 30,
            },
            {
                "id": "gate_node",
                "node_type": "gate",
                "plugin": None,
                "input": "rows",
                "condition": "True",
                "routes": {"true": "discard", "false": "discard"},
                "timeout_seconds": 30,
            },
            {
                "id": "aggregation_node",
                "node_type": "aggregation",
                "plugin": None,
                "input": "rows",
                "on_success": "aggregated",
                "on_error": "discard",
                "timeout_seconds": 30,
            },
            {
                "id": "queue_node",
                "node_type": "queue",
                "plugin": None,
                "input": "queue_node",
                "options": {},
                "timeout_seconds": 30,
            },
        ],
        ids=["transform", "gate", "aggregation", "queue"],
    )
    def test_timeout_rejects_non_barrier_node_types_atomically(self, arguments: dict[str, Any]) -> None:
        state = _empty_state()

        result = execute_tool("upsert_node", arguments, state, _mock_catalog())

        assert result.success is False
        assert result.updated_state is state
        assert result.updated_state.version == state.version
        assert "timeout_seconds" in result.validation.errors[0].message

    def test_patch_node_options_cannot_add_options_to_row_union(self) -> None:
        state = _empty_state().with_node(_row_union_node_spec())

        result = execute_tool(
            "patch_node_options",
            {"node_id": "variant_union", "patch": {"schema": {"mode": "observed"}}},
            state,
            _mock_catalog(),
        )

        assert result.success is False
        assert result.updated_state is state
        assert result.updated_state.version == state.version
        assert result.updated_state.nodes[0].options == {}

    @pytest.mark.parametrize("edge_type", ["on_success", "on_error"])
    def test_upsert_edge_cannot_route_row_union_directly_to_sink(self, edge_type: str) -> None:
        state = (
            _empty_state()
            .with_node(_row_union_node_spec())
            .with_output(
                OutputSpec(
                    name="results",
                    plugin="json",
                    options={"path": "outputs/results.json", "schema": {"mode": "observed"}},
                    on_write_failure="discard",
                )
            )
        )

        result = execute_tool(
            "upsert_edge",
            {
                "id": f"variant_union_{edge_type}",
                "from_node": "variant_union",
                "to_node": "results",
                "edge_type": edge_type,
            },
            state,
            _mock_catalog(),
        )

        assert result.success is False
        assert result.updated_state is state
        assert result.updated_state.version == state.version
        union = result.updated_state.nodes[0]
        assert union.on_success == "unioned_rows"
        assert union.on_error is None

    def test_set_output_cannot_turn_row_union_processing_connection_into_sink(self) -> None:
        state = _empty_state().with_node(_row_union_node_spec())

        result = execute_tool(
            "set_output",
            {
                "sink_name": "unioned_rows",
                "plugin": "json",
                "options": {"path": "outputs/results.json", "schema": {"mode": "observed"}},
                "on_write_failure": "discard",
            },
            state,
            _mock_catalog(),
        )

        assert result.success is False
        assert result.updated_state is state
        assert result.updated_state.version == state.version
        assert result.updated_state.outputs == ()

    @pytest.mark.parametrize(
        "override",
        [
            {"plugin": "passthrough"},
            {"input": "not_the_first_branch"},
            {"on_success": None},
            {"on_error": "errors"},
            {"options": {"schema": {"mode": "observed"}}},
            {"branches": {"only": "control_done"}},
            {"policy": "require_all"},
            {"merge": "union"},
        ],
    )
    def test_intrinsically_malformed_row_union_is_rejected_atomically(self, override: dict[str, Any]) -> None:
        state = _empty_state()
        result = execute_tool(
            "upsert_node",
            {**_ROW_UNION_UPSERT_ARGS, **override},
            state,
            _mock_catalog(),
        )

        assert result.success is False
        assert result.updated_state is state
        assert result.updated_state.version == state.version


class TestRowUnionTopologyCodesDoNotBlockUnrelatedMutations:
    """row_union step-8 topology findings are telemetry, not mutation gates.

    ``_post_mutation_invariant_error`` selects only intrinsic node-shape and
    namespace invariants. While the two step-8 topology checks shared the
    intrinsic ``row_union_branch_invalid`` code, completing the topology from
    an *unrelated* node made that node's own mutation roll back with an error
    naming the mis-wired row_union.
    """

    @staticmethod
    def _transform(node_id: str, input_connection: str, on_success: str) -> NodeSpec:
        return NodeSpec(
            id=node_id,
            node_type="transform",
            plugin="passthrough",
            input=input_connection,
            on_success=on_success,
            on_error="discard",
            options={"schema": {"mode": "observed"}},
            condition=None,
            routes=None,
            fork_to=None,
            branches=None,
            policy=None,
            merge=None,
        )

    def _mis_wired_state(self) -> CompositionState:
        """A swapped-branch row_union whose topology is not yet complete.

        ``treatment`` is deliberately absent, so ``treatment_done`` has no
        producer and only the non-blocking reachability code fires.
        """
        return (
            _empty_state()
            .with_source(
                SourceSpec(
                    plugin="csv",
                    on_success="fork_in",
                    options={"path": "/data/in.csv", "schema": {"mode": "observed"}},
                    on_validation_failure="discard",
                )
            )
            .with_node(
                NodeSpec(
                    id="fork_rows",
                    node_type="gate",
                    plugin=None,
                    input="fork_in",
                    on_success=None,
                    on_error=None,
                    options={},
                    condition="True",
                    routes={"true": "fork", "false": "fork"},
                    fork_to=("control_branch", "treatment_branch"),
                    branches=None,
                    policy=None,
                    merge=None,
                )
            )
            .with_node(self._transform("control", "control_branch", "control_done"))
            .with_node(
                NodeSpec(
                    id="variant_union",
                    node_type="row_union",
                    plugin=None,
                    input="treatment_done",
                    on_success="union_out",
                    on_error=None,
                    options={},
                    condition=None,
                    routes=None,
                    fork_to=None,
                    # Swapped: each alias claims the other branch's connection.
                    branches={
                        "control_branch": "treatment_done",
                        "treatment_branch": "control_done",
                    },
                    policy=None,
                    merge=None,
                )
            )
            .with_node(self._transform("after_union", "union_out", "main"))
            .with_output(
                OutputSpec(
                    name="main",
                    plugin="json",
                    options={"path": "outputs/main.json", "schema": {"mode": "observed"}},
                    on_write_failure="discard",
                )
            )
        )

    def test_topology_codes_are_not_mutation_blocking_invariants(self) -> None:
        from elspeth.web.composer.tools._common import (
            _MUTATION_BLOCKING_INVARIANT_CODES,
            _ROW_UNION_INTRINSIC_ERROR_CODES,
        )

        topology_codes = {
            "row_union_branch_not_downstream",
            "row_union_downstream_group_invalid",
            "row_union_schema_incompatible",
        }
        assert not topology_codes & _MUTATION_BLOCKING_INVARIANT_CODES
        assert not topology_codes & _ROW_UNION_INTRINSIC_ERROR_CODES
        # The intrinsic node-shape family must still gate mutations.
        assert {
            "row_union_config_invalid",
            "row_union_branches_invalid",
            "row_union_branch_invalid",
            "row_union_input_mismatch",
            "row_union_on_success_invalid",
            "row_union_timeout_invalid",
        } <= _MUTATION_BLOCKING_INVARIANT_CODES

    def test_completing_topology_from_an_unrelated_node_is_not_rejected(self) -> None:
        state = self._mis_wired_state()

        preview = execute_tool("preview_pipeline", {}, state, _mock_catalog())
        assert "row_union_branch_unreachable" in {entry.error_code for entry in preview.validation.errors}

        result = execute_tool(
            "upsert_node",
            {
                "id": "treatment",
                "node_type": "transform",
                "plugin": "passthrough",
                "input": "treatment_branch",
                "on_success": "treatment_done",
                "on_error": "discard",
                "options": {"schema": {"mode": "observed"}},
                "condition": None,
                "routes": None,
                "fork_to": None,
                "branches": None,
                "policy": None,
                "merge": None,
                "trigger": None,
                "output_mode": None,
                "expected_output_count": None,
            },
            state,
            _mock_catalog(),
        )

        assert result.success is True, result.to_dict()
        assert any(node.id == "treatment" for node in result.updated_state.nodes)
        assert not any(entry.component == "rejected_mutation" for entry in result.validation.errors)

        # The mis-wiring now surfaces as non-blocking validation telemetry
        # against the row_union itself, under a topology-specific code.
        codes = {entry.error_code for entry in result.validation.errors}
        assert "row_union_branch_not_downstream" in codes
        assert "row_union_branch_invalid" not in codes

    def test_mis_wired_row_union_still_persists_while_topology_is_incomplete(self) -> None:
        state = self._mis_wired_state()

        result = execute_tool("preview_pipeline", {}, state, _mock_catalog())

        assert result.success is True
        assert any(node.id == "variant_union" for node in result.updated_state.nodes)


class TestStructuralBarrierTimeoutBoundary:
    @staticmethod
    def _coalesce_arguments(timeout_seconds: object) -> dict[str, Any]:
        return {
            "id": "joined",
            "node_type": "coalesce",
            "plugin": None,
            "input": "control_done",
            "branches": {
                "control": "control_done",
                "treatment": "treatment_done",
            },
            "policy": "require_all",
            "merge": "union",
            "timeout_seconds": timeout_seconds,
        }

    @pytest.mark.parametrize("tool_name", ["upsert_node", "set_pipeline"])
    def test_coalesce_options_are_rejected_before_mutation(self, tool_name: str) -> None:
        """No authoring mutation may persist options that YAML lowering erases."""
        state = _empty_state()
        node = self._coalesce_arguments(None)
        node["options"] = {"schema": {"mode": "observed"}}
        arguments = node
        if tool_name == "set_pipeline":
            arguments = _valid_pipeline_args()
            arguments["nodes"] = [node]

        result = execute_tool(tool_name, arguments, state, _mock_catalog())

        assert result.success is False
        assert result.updated_state is state
        assert result.data is None
        assert result.validation.errors[0].error_code == "coalesce_config_invalid"
        assert "options" in result.validation.errors[0].message
        assert state.nodes == ()

    @pytest.mark.parametrize("invalid_timeout", [0, -1, float("nan"), float("inf")])
    @pytest.mark.parametrize("tool_name", ["upsert_node", "set_pipeline"])
    def test_invalid_coalesce_timeout_is_rejected_before_mutation(
        self,
        tool_name: str,
        invalid_timeout: float,
    ) -> None:
        from elspeth.web.composer.protocol import ToolArgumentError

        state = _empty_state()
        node = self._coalesce_arguments(invalid_timeout)
        arguments = node
        if tool_name == "set_pipeline":
            arguments = _valid_pipeline_args()
            arguments["nodes"] = [node]

        with pytest.raises(ToolArgumentError):
            execute_tool(tool_name, arguments, state, _mock_catalog())

        assert state.version == 1
        assert state.nodes == ()


class TestQueueBoundaryFieldEvidenceAbstains:
    """A queue exposes observed/unknown schema — field/numeric evidence must
    abstain at that boundary and MUST NOT synthesise a union of the
    predecessors' schemas (elspeth-a5b86149d4)."""

    @staticmethod
    def _typed_amount_source(on_success: str) -> SourceSpec:
        # A fixed-schema source that DOES declare `amount: int` — a merged
        # union across two of these WOULD prove the value_field numeric.
        return SourceSpec(
            plugin="csv",
            on_success=on_success,
            options={"path": "/data/x.csv", "schema": {"mode": "fixed", "fields": ["amount: int"]}},
            on_validation_failure="discard",
        )

    @staticmethod
    def _distribution_profile(input_stream: str) -> NodeSpec:
        return NodeSpec(
            id="profile_amounts",
            node_type="aggregation",
            plugin="batch_distribution_profile",
            input=input_stream,
            on_success="main",
            on_error="discard",
            options={"schema": {"mode": "observed"}, "value_field": "amount"},
            condition=None,
            routes=None,
            fork_to=None,
            branches=None,
            policy=None,
            merge=None,
        )

    @staticmethod
    def _main_sink() -> OutputSpec:
        return OutputSpec(
            name="main",
            plugin="json",
            options={"path": "/data/outputs/test-session/main.jsonl", "format": "jsonl", "schema": {"mode": "observed"}},
            on_write_failure="discard",
        )

    def _queue_node(self) -> NodeSpec:
        return NodeSpec(
            id="inbound",
            node_type="queue",
            plugin=None,
            input="inbound",
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

    def test_direct_typed_source_proves_numeric_value_field(self) -> None:
        """Control: a direct typed source proves `amount` is numeric — no abstention warning."""
        state = CompositionState(
            sources={"orders": self._typed_amount_source("straight")},
            nodes=(self._distribution_profile("straight"),),
            edges=(),
            outputs=(self._main_sink(),),
            metadata=PipelineMetadata(),
            version=1,
        )
        result = state.validate()
        assert not any("does not declare the field type" in entry.message for entry in result.warnings), (
            "a direct typed producer must PROVE the value_field, not abstain"
        )

    def test_queue_boundary_abstains_and_does_not_union_predecessor_schemas(self) -> None:
        """Two typed sources through a queue: the numeric proof abstains, not unions."""
        state = CompositionState(
            sources={
                "orders": self._typed_amount_source("inbound"),
                "refunds": self._typed_amount_source("inbound"),
            },
            nodes=(self._queue_node(), self._distribution_profile("inbound")),
            edges=(),
            outputs=(self._main_sink(),),
            metadata=PipelineMetadata(),
            version=1,
        )
        result = state.validate()
        # Both predecessors declare `amount: int`; a union WOULD have proven
        # numeric. The queue's observed boundary must abstain instead, so the
        # numeric-only advisory still fires against the observed schema.
        assert any(
            entry.component == "node:profile_amounts"
            and "batch_distribution_profile.value_field.numeric" in entry.message
            and "does not declare the field type" in entry.message
            for entry in result.warnings
        )


class TestExplainStructuralNodeShapeCodes:
    """The closed structural error codes must be explainable — they are the
    only repair signal the planner feedback carries (messages are stripped),
    so explain_validation_error(code) is how the model learns the mechanics."""

    def test_explains_unknown_node_type_teaches_fork_vocabulary(self) -> None:
        state = _empty_state()
        catalog = _mock_catalog()
        result = execute_tool(
            "explain_validation_error",
            {"error_text": "unknown_node_type"},
            state,
            catalog,
        )
        assert result.success is True
        fix = result.data["suggested_fix"]
        assert "fork_to" in fix
        assert "coalesce" in fix
        assert "change ONLY the invalid node" in fix

    def test_explains_coalesce_on_success_must_be_sink(self) -> None:
        state = _empty_state()
        catalog = _mock_catalog()
        result = execute_tool(
            "explain_validation_error",
            {"error_text": "coalesce_on_success_must_be_sink"},
            state,
            catalog,
        )
        assert result.success is True
        assert "sink" in result.data["suggested_fix"]

    def test_explains_coalesce_policy_invalid(self) -> None:
        state = _empty_state()
        catalog = _mock_catalog()
        result = execute_tool(
            "explain_validation_error",
            {"error_text": "coalesce_policy_invalid"},
            state,
            catalog,
        )
        assert result.success is True
        assert "require_all" in result.data["suggested_fix"]

    def test_explains_transform_missing_on_success(self) -> None:
        state = _empty_state()
        catalog = _mock_catalog()
        result = execute_tool(
            "explain_validation_error",
            {"error_text": "transform_missing_on_success"},
            state,
            catalog,
        )
        assert result.success is True
        assert "on_success" in result.data["suggested_fix"]


class TestStructuralNodeTypeProbedAsPlugin:
    """gate/coalesce/row_union/queue are built-in node_types, not plugins. A model that
    probes the plugin registry for them must be taught the wiring, not told
    "not installed" — live regression: the composer honestly declined an A/B
    request on the false premise that coalesce "is not installed in this
    deployment" (session 9afca49d)."""

    def _trained_pair(self) -> tuple[Any, Any]:
        from elspeth.web.dependencies import create_catalog_service

        full = create_catalog_service()
        snapshot = PluginAvailabilitySnapshot.for_trained_operator(full)
        return PolicyCatalogView.for_trained_operator(full, snapshot), snapshot

    @pytest.mark.parametrize(
        ("name", "expected_fragments"),
        [
            ("coalesce", ("node_type", "branches", "policy", "queries")),
            ("gate", ("node_type", "fork_to", "routes", "on_error", "discard", "sink", "node-level")),
            (
                "row_union",
                ("node_type", "branches", "input", "on_success", "require_all", "N-to-N", "timeout_seconds"),
            ),
            ("queue", ("node_type", "fan-in")),
        ],
    )
    def test_get_plugin_schema_teaches_structural_node_types(
        self,
        name: str,
        expected_fragments: tuple[str, ...],
    ) -> None:
        policy_catalog, snapshot = self._trained_pair()

        result = execute_tool(
            "get_plugin_schema",
            {"plugin_type": "transform", "name": name},
            _empty_state(),
            policy_catalog,
            plugin_snapshot=snapshot,
        )

        assert result.success is False
        message = result.validation.errors[0].message
        assert "not a plugin" in message
        for fragment in expected_fragments:
            assert fragment in message, f"teaching message for {name!r} must mention {fragment!r}: {message}"

    @pytest.mark.parametrize("name", [[], {}])
    def test_get_plugin_schema_rejects_unhashable_name(self, name: object) -> None:
        policy_catalog, snapshot = self._trained_pair()

        from elspeth.web.composer.protocol import ToolArgumentError

        with pytest.raises(ToolArgumentError):
            execute_tool(
                "get_plugin_schema",
                {"plugin_type": "transform", "name": name},
                _empty_state(),
                policy_catalog,
                plugin_snapshot=snapshot,
            )


class TestExplainGateRouteLabels:
    def test_explains_gate_route_labels_mismatch(self) -> None:
        state = _empty_state()
        catalog = _mock_catalog()
        result = execute_tool(
            "explain_validation_error",
            {"error_text": "gate_route_labels_mismatch"},
            state,
            catalog,
        )
        assert result.success is True
        fix = result.data["suggested_fix"]
        assert '"true"' in fix and '"false"' in fix
        assert "fork" in fix


class TestExplainDanglingDestinations:
    def test_explains_transform_on_success_dangling(self) -> None:
        state = _empty_state()
        catalog = _mock_catalog()
        result = execute_tool(
            "explain_validation_error",
            {"error_text": "transform_on_success_dangling"},
            state,
            catalog,
        )
        assert result.success is True
        fix = result.data["suggested_fix"]
        assert "sink_name" in fix
        assert "input" in fix

    def test_explains_gate_on_error_unknown_sink_as_node_level_policy(self) -> None:
        result = execute_tool(
            "explain_validation_error",
            {"error_text": "gate_on_error_unknown_sink"},
            _empty_state(),
            _mock_catalog(),
        )

        assert result.success is True
        assert "gate" in result.data["explanation"].lower()
        assert "upsert_node" in result.data["suggested_fix"]
        assert "discard" in result.data["suggested_fix"]


class TestStepDescriptionAuthoring:
    """The optional per-step ``description`` lands on the committed spec from
    every incremental authoring tool and from set_pipeline, and an option-only
    patch preserves it (elspeth-051eadb901)."""

    def test_upsert_node_carries_description(self) -> None:
        result = execute_tool(
            "upsert_node",
            {
                "id": "t1",
                "node_type": "transform",
                "plugin": "passthrough",
                "input": "source_out",
                "on_success": "main",
                "options": {"schema": {"mode": "observed"}},
                "description": "Pass rows through unchanged.",
            },
            _empty_state(),
            _mock_catalog(),
        )
        assert result.success is True
        assert result.updated_state.nodes[0].description == "Pass rows through unchanged."

    def test_set_source_and_set_output_carry_descriptions(self) -> None:
        catalog = _mock_catalog()
        sourced = execute_tool(
            "set_source",
            {
                "plugin": "csv",
                "on_success": "rows",
                "options": {"path": "/data/in.csv", "schema": {"mode": "observed"}},
                "on_validation_failure": "discard",
                "description": "Read the input CSV.",
            },
            _empty_state(),
            catalog,
        )
        assert sourced.success is True
        assert sourced.updated_state.sources["source"].description == "Read the input CSV."

        sunk = execute_tool(
            "set_output",
            {
                "sink_name": "rows",
                "plugin": "json",
                "options": {"path": "/data/out.json", "schema": {"mode": "observed"}},
                "on_write_failure": "discard",
                "description": "Write results to JSON.",
            },
            sourced.updated_state,
            catalog,
        )
        assert sunk.success is True
        assert sunk.updated_state.outputs[0].description == "Write results to JSON."

    def test_set_pipeline_carries_descriptions_on_every_step_kind(self) -> None:
        result = execute_tool(
            "set_pipeline",
            {
                "source": {
                    "plugin": "csv",
                    "on_success": "rows",
                    "options": {"path": "/data/in.csv", "schema": {"mode": "observed"}},
                    "on_validation_failure": "discard",
                    "description": "Read the input CSV.",
                },
                "nodes": [
                    {
                        "id": "t1",
                        "node_type": "transform",
                        "plugin": "passthrough",
                        "input": "rows",
                        "on_success": "main",
                        "options": {"schema": {"mode": "observed"}},
                        "description": "Pass rows through unchanged.",
                    }
                ],
                "edges": [],
                "outputs": [
                    {
                        "sink_name": "main",
                        "plugin": "json",
                        "options": {"path": "/data/out.json", "schema": {"mode": "observed"}},
                        "on_write_failure": "discard",
                        "description": "Write results to JSON.",
                    }
                ],
            },
            _empty_state(),
            _mock_catalog(),
        )
        assert result.success is True
        assert result.updated_state.sources["source"].description == "Read the input CSV."
        assert result.updated_state.nodes[0].description == "Pass rows through unchanged."
        assert result.updated_state.outputs[0].description == "Write results to JSON."

    def test_patch_node_options_preserves_description(self) -> None:
        catalog = _mock_catalog()
        created = execute_tool(
            "upsert_node",
            {
                "id": "t1",
                "node_type": "transform",
                "plugin": "passthrough",
                "input": "source_out",
                "on_success": "main",
                "options": {"schema": {"mode": "observed"}},
                "description": "Pass rows through unchanged.",
            },
            _empty_state(),
            catalog,
        )
        assert created.success is True
        patched = execute_tool(
            "patch_node_options",
            {"node_id": "t1", "patch": {"required_input_fields": ["new"]}},
            created.updated_state,
            catalog,
        )
        assert patched.success is True
        assert patched.updated_state.nodes[0].description == "Pass rows through unchanged."


class TestNormalizeEchoedInterpretationRequirements:
    """Direct guard-mechanics pins for the echo normalizer (elspeth-c67fbbbd83).

    The end-to-end echo behaviour is pinned in test_promote_set_pipeline /
    test_promote_set_source_from_blob; these kill the mutations the e2e
    surface cannot see (the auto-wired disclosure row's exemption, and the
    exact-match hash arms in isolation).
    """

    _STORED_ROW: ClassVar[dict[str, Any]] = {
        "id": "source_review:inline_source_data",
        "kind": "invented_source",
        "user_term": "inline_source_data",
        "status": "pending",
        "draft": "a,b\n1,2\n",
        "event_id": None,
        "accepted_value": None,
        "accepted_artifact_hash": None,
        "resolved_prompt_template_hash": None,
    }

    def test_full_row_echo_reduces_to_the_author_shell(self) -> None:
        from elspeth.web.composer.tools._common import _normalize_echoed_interpretation_requirements

        options, normalized = _normalize_echoed_interpretation_requirements(
            {"interpretation_requirements": [dict(self._STORED_ROW)]},
            stored_options={"interpretation_requirements": [dict(self._STORED_ROW)]},
        )
        assert normalized is True
        assert options["interpretation_requirements"] == [
            {"kind": "invented_source", "user_term": "inline_source_data", "draft": "a,b\n1,2\n"}
        ]

    def test_context_projection_echo_reduces_to_the_author_shell(self) -> None:
        from elspeth.web.composer.tools._common import _normalize_echoed_interpretation_requirements

        projection = {field: self._STORED_ROW[field] for field in ("id", "kind", "user_term", "draft", "status")}
        options, normalized = _normalize_echoed_interpretation_requirements(
            {"interpretation_requirements": [projection]},
            stored_options={"interpretation_requirements": [dict(self._STORED_ROW)]},
        )
        assert normalized is True
        assert options["interpretation_requirements"] == [
            {"kind": "invented_source", "user_term": "inline_source_data", "draft": "a,b\n1,2\n"}
        ]

    @pytest.mark.parametrize("field, value", [("status", "resolved"), ("draft", "tampered"), ("id", "other:id")])
    def test_any_single_field_difference_is_left_for_the_gate(self, field: str, value: str) -> None:
        from elspeth.web.composer.tools._common import _normalize_echoed_interpretation_requirements

        tampered = {**self._STORED_ROW, field: value}
        options, normalized = _normalize_echoed_interpretation_requirements(
            {"interpretation_requirements": [tampered]},
            stored_options={"interpretation_requirements": [dict(self._STORED_ROW)]},
        )
        assert normalized is False
        assert options["interpretation_requirements"] == [tampered]

    def test_without_stored_options_nothing_matches(self) -> None:
        from elspeth.web.composer.tools._common import _normalize_echoed_interpretation_requirements

        _options, normalized = _normalize_echoed_interpretation_requirements(
            {"interpretation_requirements": [dict(self._STORED_ROW)]},
            stored_options=None,
        )
        assert normalized is False

    def test_auto_wired_disclosure_row_is_never_normalized(self) -> None:
        """Only the required-control finalizer may stage that user_term; the
        echo path must not synthesize an author shell for it."""
        from elspeth.web.composer.tools._common import _normalize_echoed_interpretation_requirements
        from elspeth.web.interpretation_state import REQUIRED_CONTROL_AUTO_WIRED_USER_TERM

        stored = {**self._STORED_ROW, "user_term": REQUIRED_CONTROL_AUTO_WIRED_USER_TERM, "id": "auto:control"}
        options, normalized = _normalize_echoed_interpretation_requirements(
            {"interpretation_requirements": [dict(stored)]},
            stored_options={"interpretation_requirements": [dict(stored)]},
        )
        assert normalized is False
        assert options["interpretation_requirements"] == [stored]
