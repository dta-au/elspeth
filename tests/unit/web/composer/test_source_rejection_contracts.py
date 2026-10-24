"""Source refusals retain the saved graph and respect metadata/read custody."""

import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from unittest.mock import create_autospec
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from elspeth.contracts.composer_audit import ComposerToolStatus
from elspeth.contracts.enums import CreationModality
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.freeze import deep_thaw
from elspeth.web.blobs.service import BlobServiceImpl
from elspeth.web.catalog.policy_view import PolicyCatalogView
from elspeth.web.composer.audit import BufferingRecorder, ComposerToolFailure
from elspeth.web.composer.discovery_cache import serialize_tool_result
from elspeth.web.composer.pipeline_planner import _allowlisted_candidate_feedback
from elspeth.web.composer.protocol import ComposerPluginCrashError
from elspeth.web.composer.provider_gateway import _admit_composer_llm_completion
from elspeth.web.composer.redaction import redact_tool_call_response
from elspeth.web.composer.redaction_telemetry import NoopRedactionTelemetry
from elspeth.web.composer.service import ComposerAvailability
from elspeth.web.composer.state import CompositionState, OutputSpec, ValidationSummary
from elspeth.web.composer.tools import execute_tool, sources
from elspeth.web.composer.tools._common import ToolContext, ToolResult, _validate_source_path, normalize_tool_result_validation
from elspeth.web.composer.tools._dispatch import finalize_tool_result
from elspeth.web.composer.tools.blobs import _persist_prepared_blob_create, _prepare_blob_create, _sync_get_blob
from elspeth.web.composer.tools.generation import explain_validation_code
from elspeth.web.coordination.sqlite_authority import SQLiteLocalSessionOperationAuthority
from elspeth.web.dependencies import create_catalog_service
from elspeth.web.plugin_policy.models import PluginAvailabilitySnapshot, PluginId, PluginUnavailableReason
from elspeth.web.plugin_policy.profiles import OperatorProfileRegistry
from elspeth.web.sessions.models import blobs_table, sessions_table
from tests.helpers.session_fences import fenced_operation_context
from tests.unit.web.blobs.test_service import _seed_pending_blob
from tests.unit.web.composer import test_set_source_from_blobs as plural_blob_tests
from tests.unit.web.composer._helpers import _composer_service_with_session, _persisted_tool_responses
from tests.unit.web.composer.test_compose_loop_audit_wiring import _make_llm_response, _make_settings
from tests.unit.web.composer.test_promote_set_source_from_blob import _session_engine_with_user_message
from tests.unit.web.composer.test_semantic_validator import _wardline_state
from tests.unit.web.composer.test_set_source_from_blobs import _JPEG, _PNG, _create_ready_blob
from tests.unit.web.composer.test_tools import (
    _VALID_AWS_S3_PROFILE_OPTIONS,
    _empty_state,
    _profiled_s3_policy_pair,
    _restricted_policy_pair,
)

Surface = Literal["handler", "dispatch"]
harness = plural_blob_tests.harness


def _context(**kwargs: Any) -> ToolContext:
    catalog = create_catalog_service()
    snapshot = PluginAvailabilitySnapshot.for_trained_operator(catalog)
    return ToolContext(catalog=PolicyCatalogView.for_trained_operator(catalog, snapshot), plugin_snapshot=snapshot, **kwargs)


def _saved_state(source_name: str = "source") -> CompositionState:
    state = _wardline_state(text_separator=" ", scrape_format="text")
    # A valid, unreferenced output produces a real standing warning while
    # preserving the Wardline graph's actual errors and contract findings.
    unused_output = OutputSpec(
        name="main",
        plugin="json",
        on_write_failure="discard",
        options={
            "path": "outputs/test-session/main.json",
            "schema": {"mode": "observed"},
            "mode": "write",
            "collision_policy": "auto_increment",
        },
    )
    return replace(state, sources={source_name: state.sources["source"]}, outputs=(*state.outputs, unused_output), version=17)


def _options() -> dict[str, Any]:
    return {"path": "data/url.csv", "schema": {"mode": "observed"}}


def _invoke(surface: Surface, tool: str, args: dict[str, Any], state: CompositionState, context: ToolContext) -> ToolResult:
    if surface == "handler":
        handler = {
            "set_source": sources._execute_set_source,
            "set_source_from_blob": sources._execute_set_source_from_blob,
            "set_source_from_blobs": sources._execute_set_source_from_blobs,
            "patch_source_options": sources._handle_patch_source_options,
            "clear_source": sources._execute_clear_source,
        }[tool]
        return handler(args, state, context)
    return execute_tool(
        tool,
        args,
        state,
        context.catalog,
        plugin_snapshot=context.plugin_snapshot,
        data_dir=context.data_dir,
        session_id=context.session_id,
        session_engine=context.session_engine,
    )


def _assert_refusal(result: ToolResult, state: CompositionState, code: str, subject: str | None, standing: ValidationSummary) -> None:
    assert not result.success
    assert result.updated_state is state and result.updated_state.version == 17
    assert result.affected_nodes == () and result.prior_validation is None
    assert "validation_delta" not in result.to_dict()
    leading = result.validation.errors[0]
    assert (leading.component, leading.error_code, leading.rejected_component) == ("rejected_mutation", code, subject)
    assert result.validation.errors[1:] == standing.errors
    assert result.validation.warnings == standing.warnings
    assert result.validation.suggestions == standing.suggestions
    assert result.validation.edge_contracts == standing.edge_contracts
    assert result.validation.semantic_contracts == standing.semantic_contracts


@pytest.mark.parametrize("surface", ["handler", "dispatch"])
@pytest.mark.parametrize("source_name", ["source", "orders"])
@pytest.mark.parametrize("tool", ["set_source", "set_source_from_blob", "set_source_from_blobs", "patch_source_options", "clear_source"])
def test_source_refusals_keep_saved_findings_and_precise_subject(surface: Surface, source_name: str, tool: str) -> None:
    state = _saved_state(source_name)
    before = state.to_dict()
    context = _context()
    subject = "source" if source_name == "source" else "source:orders"
    args: dict[str, Any] = {"source_name": source_name}
    code = "failure_route_required"
    if tool == "set_source":
        args.update(plugin="csv", options=_options(), on_success="scrape_in", on_validation_failure=None)
    elif tool == "set_source_from_blob":
        args.update(blob_id=str(uuid4()), on_success="scrape_in", on_validation_failure=None)
    elif tool == "set_source_from_blobs":
        args.update(blob_ids=[str(uuid4())], on_success="scrape_in", on_validation_failure=None)
    else:
        args["source_name"] = "absent"
        subject, code = "source:absent", "source_not_found"
        if tool == "patch_source_options":
            args["patch"] = {"encoding": "utf-8"}
    standing = state.validate() if surface == "handler" else context.catalog.validate_composition_state(state).validation
    assert standing.errors and standing.warnings and standing.edge_contracts and standing.semantic_contracts
    result = _invoke(surface, tool, args, state, context)
    _assert_refusal(result, state, code, subject, standing)
    other = replace(context.plugin_snapshot, snapshot_hash="packet1c-other-policy-snapshot")
    view = PolicyCatalogView.for_trained_operator(create_catalog_service(), other)
    result = normalize_tool_result_validation(result, view)
    _assert_refusal(result, state, code, subject, view.validate_composition_state(state).validation)
    assert state.to_dict() == before
    if surface == "dispatch":
        assert result.validation_guidance is not None and code in result.validation_guidance["codes"]


@pytest.mark.parametrize("surface", ["handler", "dispatch"])
@pytest.mark.parametrize("tool", ["set_source", "patch_source_options"])
def test_source_path_is_safe_in_response_history_redaction_and_planner(tmp_path: Path, surface: Surface, tool: str) -> None:
    private_root = tmp_path / "NON-AUTHORED-SOURCE-ROOT-CANARY"
    path = "/outside-session/authored.csv"
    raw = _validate_source_path({"path": path}, str(private_root), session_id="test-session")
    assert raw is not None and str(private_root) in raw
    context = _context(data_dir=str(private_root), session_id="test-session")
    state = _saved_state("orders")
    args = {"source_name": "orders", "patch": {"path": path}}
    if tool == "set_source":
        args = {
            "source_name": "orders",
            "plugin": "csv",
            "options": {**_options(), "path": path},
            "on_success": "scrape_in",
            "on_validation_failure": "discard",
        }
    result = _invoke(surface, tool, args, state, context)
    entry = result.validation.errors[0]
    assert not result.success and result.updated_state is state
    assert (entry.error_code, entry.rejected_component, entry.plugin_identity) == ("source_path_policy_invalid", "source:orders", None)
    assert entry.message == (
        "Source file path options (path or file) must use this session's permitted source locations. "
        "Use the session's blob binding or an authorized source path."
    )
    assert result.plugin_schemas is None
    feedback = _allowlisted_candidate_feedback(result)
    projected = feedback["validation"]["errors"][0]
    assert projected["component"] == "source:orders" and "detail" not in projected
    assert projected["suggested_fix"]
    redacted = redact_tool_call_response(tool, result.to_dict(), telemetry=NoopRedactionTelemetry())
    assert redacted["validation"]["errors"][0]["error_code"] == "source_path_policy_invalid"
    for payload in (repr(result.to_dict()), serialize_tool_result(result), repr(feedback), repr(redacted)):
        assert "NON-AUTHORED-SOURCE-ROOT-CANARY" not in payload


@pytest.mark.parametrize("surface", ["handler", "dispatch"])
@pytest.mark.parametrize("tool", ["set_source", "patch_source_options", "set_source_from_blobs"])
@pytest.mark.parametrize("reason", [PluginUnavailableReason.NOT_AUTHORIZED, PluginUnavailableReason.LOCAL_REQUIREMENT_MISSING])
def test_source_policy_is_owned_and_does_not_read_unadmitted_schema(
    harness, monkeypatch: pytest.MonkeyPatch, surface: Surface, tool: str, reason: PluginUnavailableReason
) -> None:
    engine, _, session_id = harness
    blob_id = _create_ready_blob(harness, content=_PNG, filename="page.png", mime_type="image/png")
    catalog = create_catalog_service()
    plugin = "blob_rows" if tool == "set_source_from_blobs" else "csv"
    view, snapshot = _restricted_policy_pair(catalog, PluginId("source", plugin), reason)
    context = ToolContext(catalog=view, plugin_snapshot=snapshot, session_engine=engine, session_id=session_id)
    state = _saved_state()
    args = {"patch": {"encoding": "utf-8"}}
    if tool == "set_source":
        args = {"plugin": "csv", "options": _options(), "on_success": "scrape_in", "on_validation_failure": "discard"}
    elif tool == "set_source_from_blobs":
        args = {"blob_ids": [blob_id], "on_success": "scrape_in", "on_validation_failure": "discard"}
    original = PolicyCatalogView.get_schema
    calls = []

    def schema(self, kind, name):
        if kind == "source" and name == plugin:
            calls.append((kind, name))
            raise AssertionError("unadmitted schema was read")
        return original(self, kind, name)

    monkeypatch.setattr(PolicyCatalogView, "get_schema", schema)
    result = _invoke(surface, tool, args, state, context)
    assert not result.success and result.updated_state is state
    entry = result.validation.errors[0]
    assert (entry.error_code, entry.rejected_component, entry.plugin_identity) == (reason.value, "source", None)
    assert not calls and result.plugin_schemas is None


@pytest.mark.parametrize("tool", ["set_source", "patch_source_options", "set_source_from_blobs"])
@pytest.mark.parametrize("error_type", [ValueError, KeyError])
def test_source_admitted_schema_fault_propagates_from_actual_owner(
    harness, monkeypatch: pytest.MonkeyPatch, tool: str, error_type: type[Exception]
) -> None:
    engine, _, session_id = harness
    blob_id = _create_ready_blob(harness, content=_PNG, filename="page.png", mime_type="image/png")
    context = _context(session_engine=engine, session_id=session_id)
    state = _saved_state()
    plugin = "blob_rows" if tool == "set_source_from_blobs" else "csv"
    assert PluginId("source", plugin) in context.plugin_snapshot.available
    args = {"patch": {"encoding": "utf-8"}}
    if tool == "set_source":
        args = {"plugin": "csv", "options": _options(), "on_success": "scrape_in", "on_validation_failure": "discard"}
    elif tool == "set_source_from_blobs":
        args = {"blob_ids": [blob_id], "on_success": "scrape_in", "on_validation_failure": "discard"}
    original = PolicyCatalogView.get_schema
    calls = []
    fault = error_type("OWNED-SCHEMA-FAULT-CANARY")

    def schema(self, kind, name):
        if kind == "source" and name == plugin:
            calls.append((kind, name))
            raise fault
        return original(self, kind, name)

    monkeypatch.setattr(PolicyCatalogView, "get_schema", schema)
    with pytest.raises(error_type) as caught:
        _invoke("handler", tool, args, state, context)
    assert caught.value is fault and calls == [("source", plugin)]
    assert state.version == 17


@pytest.mark.parametrize("surface", ["handler", "dispatch"])
@pytest.mark.parametrize("tool", ["set_source", "patch_source_options"])
def test_source_prevalidation_retains_exact_tuple_code_identity_and_authored_detail(surface: Surface, tool: str) -> None:
    context, state = _context(), _saved_state("orders")
    invalid = {"schema": {"mode": "AUTHORED-SOURCE-OPTION-CANARY"}}
    args = {"source_name": "orders", "patch": invalid}
    if tool == "set_source":
        args = {
            "source_name": "orders",
            "plugin": "csv",
            "options": {**_options(), **invalid},
            "on_success": "scrape_in",
            "on_validation_failure": "discard",
        }
    result = _invoke(surface, tool, args, state, context)
    entry = result.validation.errors[0]
    assert (entry.error_code, entry.rejected_component, entry.plugin_identity) == (
        "plugin_options_invalid",
        "source:orders",
        ("source", "csv"),
    )
    assert "AUTHORED-SOURCE-OPTION-CANARY" in entry.message
    feedback = _allowlisted_candidate_feedback(result)
    assert "AUTHORED-SOURCE-OPTION-CANARY" in feedback["validation"]["errors"][0]["detail"]
    if surface == "dispatch":
        assert result.to_dict()["plugin_schemas"] == {"source/csv": context.catalog.get_schema("source", "csv").model_dump(mode="json")}


@pytest.mark.parametrize("surface", ["handler", "dispatch"])
@pytest.mark.parametrize("tool", ["set_source", "patch_source_options"])
@pytest.mark.parametrize(
    ("profile_case", "lowering_error"),
    [
        pytest.param("valid-alias", None, id="valid-alias"),
        pytest.param("invalid-alias", None, id="invalid-alias"),
        pytest.param("profile-outage", ValueError, id="profile-outage-value"),
        pytest.param("profile-outage", RuntimeError, id="profile-outage-runtime"),
    ],
)
def test_source_profile_prevalidation_keeps_distinct_owned_tuple_code(
    monkeypatch: pytest.MonkeyPatch,
    surface: Surface,
    tool: str,
    profile_case: str,
    lowering_error: type[Exception] | None,
) -> None:
    view, snapshot = _profiled_s3_policy_pair()
    context = ToolContext(catalog=view, plugin_snapshot=snapshot)
    assert PluginId("source", "aws_s3") in snapshot.available
    state = _saved_state("orders")
    state = replace(
        state, sources={"orders": replace(state.sources["orders"], plugin="aws_s3", options=dict(_VALID_AWS_S3_PROFILE_OPTIONS))}
    )
    alias = "unavailable-authored-alias" if profile_case == "invalid-alias" else "demo-input"
    args = {"source_name": "orders", "patch": {"profile": alias}}
    if tool == "set_source":
        args = {
            "source_name": "orders",
            "plugin": "aws_s3",
            "options": {**_VALID_AWS_S3_PROFILE_OPTIONS, "profile": alias},
            "on_success": "scrape_in",
            "on_validation_failure": "discard",
        }
    before = state.to_dict()
    lowering_calls = []
    if lowering_error is not None:
        original = OperatorProfileRegistry.lower_options

        def lower(registry, plugin_id, *, alias, safe_options):
            if plugin_id == PluginId("source", "aws_s3"):
                assert alias == "demo-input"
                lowering_calls.append((plugin_id, alias, deep_thaw(safe_options)))
                raise lowering_error("PRIVATE-PROFILE-LOWERING-CANARY")
            return original(registry, plugin_id, alias=alias, safe_options=safe_options)

        # Inject only an outage of the real configured resolver after valid
        # plugin, alias and public-schema admission. No snapshot contradiction.
        monkeypatch.setattr(OperatorProfileRegistry, "lower_options", lower)
    result = _invoke(surface, tool, args, state, context)
    assert state.to_dict() == before
    if profile_case == "valid-alias":
        assert result.success
        if tool == "patch_source_options":
            assert result.updated_state is state
            assert result.updated_state.version == 17
            assert result.affected_nodes == () and result.applied_component is None
        else:
            assert result.updated_state.version == 18
        assert deep_thaw(result.updated_state.sources["orders"].options) == dict(_VALID_AWS_S3_PROFILE_OPTIONS)
        return
    if profile_case == "profile-outage":
        assert lowering_calls
        assert all(call[:2] == (PluginId("source", "aws_s3"), "demo-input") for call in lowering_calls)
        expected_options = {key: value for key, value in _VALID_AWS_S3_PROFILE_OPTIONS.items() if key != "profile"}
        expected_options["on_validation_failure"] = "discard" if tool == "set_source" else state.sources["orders"].on_validation_failure
        assert any(call[2] == expected_options for call in lowering_calls)
    assert not result.success and result.updated_state is state
    entry = result.validation.errors[0]
    assert (entry.error_code, entry.rejected_component, entry.plugin_identity) == (
        "profile_unavailable" if profile_case == "profile-outage" else "plugin_options_invalid",
        "source:orders",
        ("source", "aws_s3"),
    )
    if surface == "handler":
        assert result.plugin_schemas is None
    else:
        assert result.to_dict()["plugin_schemas"] == {"source/aws_s3": view.get_schema("source", "aws_s3").model_dump(mode="json")}
        assert "elspeth-demo-input" not in repr(result.plugin_schemas)
    assert "PRIVATE-PROFILE-LOWERING-CANARY" not in repr(result.to_dict())
    assert "PRIVATE-PROFILE-LOWERING-CANARY" not in serialize_tool_result(result)
    assert "PRIVATE-PROFILE-LOWERING-CANARY" not in repr(_allowlisted_candidate_feedback(result))


@pytest.mark.parametrize("tool", ["set_source", "patch_source_options"])
def test_source_missing_plugin_keeps_actual_not_installed_reason(tool: str) -> None:
    context, state = _context(), _saved_state()
    plugin = "not_installed_contract_source"
    assert context.catalog.unavailable_reason(PluginId("source", plugin)) is PluginUnavailableReason.NOT_INSTALLED
    state = replace(state, sources={"source": replace(state.sources["source"], plugin=plugin)})
    args = {"patch": {"encoding": "utf-8"}}
    if tool == "set_source":
        args = {"plugin": plugin, "options": _options(), "on_success": "scrape_in", "on_validation_failure": "discard"}
    result = _invoke("handler", tool, args, state, context)
    entry = result.validation.errors[0]
    assert (entry.error_code, entry.rejected_component, entry.plugin_identity) == ("plugin_not_installed", "source", None)
    assert not result.success and result.updated_state is state and result.plugin_schemas is None


@pytest.mark.parametrize("finalized", [False, True])
@pytest.mark.parametrize("tool", ["set_source", "patch_source_options"])
def test_source_credential_refusal_retains_repair_without_literal(finalized: bool, tool: str) -> None:
    # The public dispatch credential guard independently refuses this input
    # before the handler. Exercise the handler's defense and its real finalizer.
    literal = "literal-source-key-for-contract-test"
    state = _saved_state("orders")
    args = {"source_name": "orders", "patch": {"api_key": literal}}
    if tool == "set_source":
        args = {
            "source_name": "orders",
            "plugin": "csv",
            "options": {**_options(), "api_key": literal},
            "on_success": "scrape_in",
            "on_validation_failure": "discard",
        }
    context = _context()
    result = _invoke("handler", tool, args, state, context)
    if finalized:
        result = finalize_tool_result(
            result,
            tool_name=tool,
            catalog=context.catalog,
            context=context,
            prior_validation=context.catalog.validate_composition_state(state).validation,
        )
    assert not result.success and result.updated_state is state
    entry = result.validation.errors[0]
    assert (entry.error_code, entry.rejected_component) == ("credential_literal_forbidden", "source:orders")
    assert result.data is not None and set(result.data) == {"credential_fields", "components", "repair"}
    assert result.data["credential_fields"] == ("source:orders:api_key",)
    assert result.data["components"] == ({"component_id": "source:orders", "component_type": "source", "fields": ("api_key",)},)
    assert result.data["repair"]["inline_form"]["example_options"] == {"api_key": {"secret_ref": "<NAME>"}}
    assert result.data["repair"]["post_hoc_form"]["tool_sequence"] == ("list_secret_refs", "validate_secret_ref", "wire_secret_ref")
    assert literal not in repr(result.to_dict()) and literal not in serialize_tool_result(result)
    assert literal not in repr(_allowlisted_candidate_feedback(result))


@pytest.mark.parametrize("tool", ["set_source", "patch_source_options"])
def test_source_reconciliation_failure_has_no_false_source_subject(monkeypatch: pytest.MonkeyPatch, tool: str) -> None:
    state, context = _saved_state(), _context()
    args = {"patch": {"encoding": "utf-8"}}
    if tool == "set_source":
        args = {"plugin": "csv", "options": _options(), "on_success": "scrape_in", "on_validation_failure": "discard"}
    calls = []

    def reconcile(saved, proposed):
        assert saved is state and proposed.version == 18
        calls.append(proposed)
        raise ValueError("cross-component review mismatch")

    monkeypatch.setattr(sources, "reconcile_authoritative_reviews", reconcile)
    result = _invoke("handler", tool, args, state, context)
    assert calls and result.updated_state is state
    assert (result.validation.errors[0].error_code, result.validation.errors[0].rejected_component) == (
        "review_reconciliation_failed",
        None,
    )


def _forbid_content_reads(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args, **kwargs):
        raise AssertionError("plural binding read source content")

    monkeypatch.setattr(sources, "_locked_read_ready_blob", forbidden)
    monkeypatch.setattr(sources, "inspect_blob_content", forbidden)
    monkeypatch.setattr(Path, "read_bytes", forbidden)
    monkeypatch.setattr(Path, "open", forbidden)


@pytest.mark.parametrize("surface", ["handler", "dispatch"])
def test_plural_metadata_order_is_authoritative_without_content_reads(harness, monkeypatch: pytest.MonkeyPatch, surface: Surface) -> None:
    engine, _, session_id = harness
    png = _create_ready_blob(harness, content=_PNG, filename="one.png", mime_type="image/png")
    jpg = _create_ready_blob(harness, content=_JPEG, filename="two.jpg", mime_type="image/jpeg")
    records = [_sync_get_blob(engine, blob_id, session_id) for blob_id in (jpg, png)]
    context, state = _context(session_engine=engine, session_id=session_id), _empty_state()
    _forbid_content_reads(monkeypatch)
    result = _invoke(
        surface,
        "set_source_from_blobs",
        {"blob_ids": [jpg, png], "source_name": "documents", "on_success": "docs", "on_validation_failure": "discard"},
        state,
        context,
    )
    assert result.success and result.updated_state.version == 2 and result.affected_nodes == ("source:documents",)
    assert state.sources == {}
    expected = [
        {
            "blob_id": r["id"],
            "payload_ref": r["content_hash"],
            "filename": r["filename"],
            "mime_type": r["mime_type"],
            "size_bytes": r["size_bytes"],
        }
        for r in records
    ]
    assert deep_thaw(result.updated_state.sources["documents"].options["blobs"]) == expected
    assert [row["blob_id"] for row in result.data["source_blobs"]] == [jpg, png]
    assert "storage_path" not in repr(result.data)


@pytest.mark.parametrize("case", ["missing", "foreign", "pending-null", "duplicate-id", "duplicate-content", "invalid-id"])
def test_plural_refuses_whole_list_without_reads_or_partial_mutation(harness, monkeypatch: pytest.MonkeyPatch, case: str) -> None:
    engine, blob_service, session_id = harness
    good = _create_ready_blob(harness, content=_PNG, filename="one.png", mime_type="image/png")
    ids, code = [good, str(uuid4())], "source_blob_unavailable"
    context = _context(session_engine=engine, session_id=session_id)
    if case == "foreign":
        other_session = str(uuid4())
        with engine.begin() as conn:
            conn.execute(
                sessions_table.insert().values(
                    id=other_session,
                    user_id="alice",
                    auth_provider_type="local",
                    title="other owned session",
                    created_at=datetime.now(UTC),
                    updated_at=datetime.now(UTC),
                )
            )
        context = replace(context, session_id=other_session)
        ids = [good]
    elif case == "pending-null":
        pending = _seed_pending_blob(engine, blob_service, UUID(session_id), "pending.csv")
        assert pending.content_hash is None
        ids = [good, str(pending.id)]
    elif case == "duplicate-id":
        ids, code = [good, good.upper()], "source_blob_duplicate"
    elif case == "duplicate-content":
        duplicate = _create_ready_blob(harness, content=_PNG, filename="copy.png", mime_type="image/png")
        ids, code = [good, duplicate], "source_blob_duplicate"
    elif case == "invalid-id":
        ids, code = [good, "not-a-uuid"], "source_blob_identity_invalid"
    state = _saved_state("documents")
    before = state.to_dict()
    _forbid_content_reads(monkeypatch)
    result = _invoke(
        "dispatch",
        "set_source_from_blobs",
        {"blob_ids": ids, "source_name": "documents", "on_success": "docs", "on_validation_failure": "discard"},
        state,
        context,
    )
    assert state.to_dict() == before
    _assert_refusal(result, state, code, "source:documents", context.catalog.validate_composition_state(state).validation)
    assert result.data is None and result.plugin_schemas is None
    if code == "source_blob_duplicate":
        assert result.validation_guidance is not None and code in result.validation_guidance["codes"]
        assert "distinct intended input" in result.validation_guidance["codes"][code]["suggested_fix"]
        feedback = _allowlisted_candidate_feedback(result)["validation"]["errors"][0]
        assert feedback["error_code"] == code and feedback["component"] == "source:documents"
        assert feedback["suggested_fix"] and "detail" not in feedback
    if case in {"missing", "foreign"}:
        assert result.validation.errors[0].message.endswith("not found.")
        assert "one.png" not in result.validation.errors[0].message


@pytest.mark.parametrize("corruption", ["ready-null", "invalid-mime"])
def test_plural_corrupt_owned_rows_raise_at_the_actual_integrity_boundary(
    harness, monkeypatch: pytest.MonkeyPatch, corruption: str
) -> None:
    engine, _, session_id = harness
    blob_id = _create_ready_blob(harness, content=_PNG, filename="page.png", mime_type="image/png")
    if corruption == "ready-null":
        # First prove the ordinary database boundary rejects the impossible row.
        with pytest.raises(IntegrityError), engine.begin() as conn:
            conn.execute(update(blobs_table).where(blobs_table.c.id == blob_id).values(content_hash=None))
        # Deliberate corruption fixture, scoped to this owned in-memory database.
        # Re-enable CHECKs immediately; production still reads the real row.
        with engine.begin() as conn:
            conn.exec_driver_sql("PRAGMA ignore_check_constraints=ON")
            try:
                conn.execute(update(blobs_table).where(blobs_table.c.id == blob_id).values(content_hash=None))
            finally:
                conn.exec_driver_sql("PRAGMA ignore_check_constraints=OFF")
        match = "Ready blob .* has no canonical content hash"
    else:
        with engine.begin() as conn:
            conn.execute(update(blobs_table).where(blobs_table.c.id == blob_id).values(mime_type="application/x-parquet"))
        match = r"blobs\.mime_type is 'application/x-parquet'"
    state, context = _saved_state(), _context(session_engine=engine, session_id=session_id)
    before = state.to_dict()
    _forbid_content_reads(monkeypatch)
    with pytest.raises(AuditIntegrityError, match=match):
        _invoke(
            "dispatch",
            "set_source_from_blobs",
            {"blob_ids": [blob_id], "on_success": "docs", "on_validation_failure": "discard"},
            state,
            context,
        )
    assert state.to_dict() == before


@pytest.mark.parametrize(
    "modality", [CreationModality.LLM_GENERATED, CreationModality.DISAMBIGUATED, CreationModality.LLM_GENERATED_THEN_AMENDED]
)
def test_plural_all_authored_modalities_require_review_from_real_persisted_provenance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, modality: CreationModality
) -> None:
    engine, session_id, message_id = _session_engine_with_user_message("Generate the intended source data.")
    prepared = _prepare_blob_create(
        filename="generated.csv",
        mime_type="text/csv",
        content="name,score\nada,42\n",
        data_dir=str(tmp_path),
        session_id=session_id,
        creation_modality=modality,
        created_from_message_id=message_id,
        creating_model_identifier="offline/model",
        creating_model_version="offline-v1",
        creating_provider="offline",
        creating_composer_skill_hash="a" * 64,
        creating_arguments_hash="b" * 64,
    )
    with fenced_operation_context(engine, session_id) as operation:
        assert (
            _persist_prepared_blob_create(
                prepared,
                session_engine=engine,
                session_id=session_id,
                session_operation_context=operation,
                session_operation_authority=SQLiteLocalSessionOperationAuthority(engine),
            )
            is None
        )
    with engine.connect() as conn:
        row = conn.execute(select(blobs_table).where(blobs_table.c.id == prepared.blob_id)).mappings().one()
    assert row["creation_modality"] == modality.value and row["created_from_message_id"] == message_id
    assert row["creating_model_identifier"] == "offline/model" and row["content_hash"] == prepared.content_hash
    state, context = _saved_state("orders"), _context(session_engine=engine, session_id=session_id, data_dir=str(tmp_path))
    _forbid_content_reads(monkeypatch)
    result = _invoke(
        "dispatch",
        "set_source_from_blobs",
        {"blob_ids": [prepared.blob_id], "source_name": "orders", "on_success": "docs", "on_validation_failure": "discard"},
        state,
        context,
    )
    _assert_refusal(
        result, state, "source_blob_review_required", "source:orders", context.catalog.validate_composition_state(state).validation
    )
    assert "not representable" in result.validation.errors[0].message
    assert explain_validation_code("source_blob_review_required") is not None


@pytest.mark.parametrize("surface", ["handler", "dispatch"])
@pytest.mark.parametrize("patch", [{"blob_ref": str(uuid4())}, {"path": "data/different.csv"}])
def test_source_binding_lock_keeps_real_singular_binding_and_review_custody(harness, surface: Surface, patch: dict[str, Any]) -> None:
    engine, _, session_id = harness
    blob_id = _create_ready_blob(harness, content=b"name,score\nada,42\n", filename="input.csv", mime_type="text/csv")
    context = _context(session_engine=engine, session_id=session_id)
    bound = _invoke(
        "dispatch",
        "set_source_from_blob",
        {"blob_id": blob_id, "on_success": "scrape_in", "on_validation_failure": "discard", "options": {"schema": {"mode": "observed"}}},
        _empty_state(),
        context,
    )
    assert bound.success
    state = _saved_state("orders")
    state = replace(state, sources={"orders": bound.updated_state.sources["source"]})
    before = state.to_dict()
    result = _invoke(surface, "patch_source_options", {"source_name": "orders", "patch": patch}, state, context)
    standing = state.validate() if surface == "handler" else context.catalog.validate_composition_state(state).validation
    _assert_refusal(result, state, "source_binding_locked", "source:orders", standing)
    assert state.to_dict() == before and state.sources["orders"].options["blob_ref"] == blob_id
    assert "clear_source first" not in result.validation.errors[0].message
    assert explain_validation_code("source_binding_locked") is not None


@pytest.mark.parametrize("surface", ["handler", "dispatch"])
def test_source_successes_preserve_literal_options_failure_choice_and_update_semantics(surface: Surface) -> None:
    context, original = _context(), _empty_state()
    options = {**_options(), "encoding": "utf-8", "delimiter": ";"}
    created = _invoke(
        surface,
        "set_source",
        {"plugin": "csv", "options": options, "on_success": "out", "on_validation_failure": "discard"},
        original,
        context,
    )
    assert created.success and original.sources == {}
    replaced = _invoke(surface, "set_source", {"plugin": "csv", "options": options, "on_success": "out"}, created.updated_state, context)
    assert replaced.success and replaced.updated_state.sources["source"].on_validation_failure == "discard"
    patched = _invoke(surface, "patch_source_options", {"patch": {"encoding": "utf-8-sig"}}, replaced.updated_state, context)
    assert patched.success
    assert patched.updated_state.sources["source"].options["delimiter"] == ";"
    assert patched.updated_state.sources["source"].on_validation_failure == "discard"
    cleared = _invoke(surface, "clear_source", {}, patched.updated_state, context)
    assert cleared.success and cleared.updated_state.sources == {}
    assert [r.updated_state.version for r in (created, replaced, patched, cleared)] == [2, 3, 4, 5]
    assert all(r.affected_nodes == ("source",) for r in (created, replaced, patched, cleared))


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["schema-value", "schema-key", "ready-null", "invalid-mime"])
async def test_source_outer_fault_keeps_distinct_integrity_and_completed_outcome_accounting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    def available(*, model: str, **kwargs: Any) -> ComposerAvailability:
        return ComposerAvailability(available=True, model=model, provider="offline")

    monkeypatch.setattr("elspeth.web.composer.service.compute_availability", available)
    catalog = create_catalog_service()
    service, session_id = _composer_service_with_session(catalog=catalog, settings=_make_settings(data_dir=tmp_path))
    engine = service._require_sessions_service()._engine
    state = _saved_state()
    if fault.startswith("schema-"):
        args = {"plugin": "csv", "options": _options(), "on_success": "scrape_in", "on_validation_failure": "discard"}
        tool = "set_source"
        original = PolicyCatalogView.get_schema
        original_source = sources._execute_set_source
        owned_error = ValueError("owned source schema fault") if fault == "schema-value" else KeyError("owned source schema fault")
        in_source_handler = False
        schema_calls = []

        def schema(self, kind, name):
            if in_source_handler and (kind, name) == ("source", "csv"):
                schema_calls.append((kind, name))
                raise owned_error
            return original(self, kind, name)

        def source_handler(arguments, saved, context):
            nonlocal in_source_handler
            assert saved.version == 18 and saved.metadata.name == "Earlier completed edit"
            in_source_handler = True
            try:
                return original_source(arguments, saved, context)
            finally:
                in_source_handler = False

        monkeypatch.setattr(PolicyCatalogView, "get_schema", schema)
        # The registered _handle_set_source calls this module-owned function
        # dynamically; keep the immutable dispatch registry and wrapper intact.
        monkeypatch.setattr(sources, "_execute_set_source", source_handler)
    else:
        service_blobs = BlobServiceImpl(engine, tmp_path)
        with fenced_operation_context(engine, session_id) as operation:
            record = await service_blobs.create_blob(
                session_id=UUID(session_id), filename="input.png", content=_PNG, mime_type="image/png", session_operation_context=operation
            )
        with engine.begin() as conn:
            if fault == "ready-null":
                conn.exec_driver_sql("PRAGMA ignore_check_constraints=ON")
                try:
                    conn.execute(update(blobs_table).where(blobs_table.c.id == str(record.id)).values(content_hash=None))
                finally:
                    conn.exec_driver_sql("PRAGMA ignore_check_constraints=OFF")
            else:
                conn.execute(update(blobs_table).where(blobs_table.c.id == str(record.id)).values(mime_type="application/x-parquet"))
        args = {"blob_ids": [str(record.id)], "on_success": "docs", "on_validation_failure": "discard"}
        tool = "set_source_from_blobs"
    response = _make_llm_response(
        tool_calls=[
            {"id": "source_prior", "name": "set_metadata", "arguments": {"patch": {"name": "Earlier completed edit"}}},
            {"id": "source_fault", "name": tool, "arguments": args},
        ]
    )
    llm = create_autospec(service._provider_gateway._call_llm, return_value=_admit_composer_llm_completion(response))
    monkeypatch.setattr(service._provider_gateway, "_call_llm", llm)
    recorder = BufferingRecorder()
    monkeypatch.setattr("elspeth.web.composer.service.BufferingRecorder", lambda: recorder)
    expected = ComposerPluginCrashError if fault.startswith("schema-") else ComposerToolFailure
    with pytest.raises(expected) as caught:
        await service._run_one_turn_for_test(session_id=session_id, initial_state=state)
    assert llm.await_count == 1
    invocations = recorder.invocations
    assert [r.tool_call_id for r in invocations] == ["source_prior", "source_fault"]
    assert [r.status for r in invocations] == [ComposerToolStatus.SUCCESS, ComposerToolStatus.PLUGIN_CRASH]
    assert invocations[0].version_after == 18
    assert invocations[1].version_after is None
    assert state.version == 17 and state.metadata.name == "wardline"
    if isinstance(caught.value, ComposerPluginCrashError):
        assert schema_calls == [("source", "csv")]
        assert caught.value.original_exc is owned_error
        assert caught.value.partial_state is not None
        assert caught.value.partial_state.version == 18 and caught.value.partial_state.metadata.name == "Earlier completed edit"
        assert caught.value.partial_state.sources == state.sources
        assert caught.value.failed_turn is not None and caught.value.failed_turn.tool_calls_attempted == 2
        rows = _persisted_tool_responses(service, session_id)
        assert [r["tool_call_id"] for r in rows] == ["source_prior", "source_fault"]
        assert json.loads(rows[0]["result_canonical"])["success"] is True
        assert json.loads(rows[1]["result_canonical"])["error_class"] == type(owned_error).__name__
    else:
        assert type(caught.value) is ComposerToolFailure
        assert type(caught.value.original_exception) is AuditIntegrityError
        assert caught.value.__cause__ is caught.value.original_exception
        assert caught.value.tool_invocations == invocations
        detail = str(caught.value.original_exception)
        if fault == "ready-null":
            assert "has no canonical content hash" in detail
        else:
            assert "blobs.mime_type is 'application/x-parquet'" in detail
        assert invocations[1].error_class == "AuditIntegrityError"
        # Batch integrity faults retain their class as the causal exception;
        # the service's existing audit-only carrier owns both invocations.
        assert not isinstance(caught.value, ComposerPluginCrashError)
