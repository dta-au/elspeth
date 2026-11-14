"""Configured-root file authoring through the shipped MCP construction.

These tests intentionally use real catalog admission and registered SDK
handlers. Pytest supplies executor recovery for the isolated preview controls;
they do not qualify the standalone process lifecycle.
"""

from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
import yaml
from mcp.server import Server
from mcp.types import CallToolRequest, CallToolRequestParams, CallToolResult, TextContent

from elspeth.composer_mcp.server import _build_stdio_server, _StdioValidationSettings, create_server
from elspeth.composer_mcp.session import SessionManager
from elspeth.contracts.composer_paths import McpConfiguredRootAuthority
from elspeth.web.catalog.policy_view import PolicyCatalogView
from elspeth.web.composer import yaml_generator
from elspeth.web.composer.pipeline_proposal import composition_content_hash
from elspeth.web.composer.state import CompositionState
from elspeth.web.composer.tools._common import ToolContext
from elspeth.web.dependencies import create_catalog_service
from elspeth.web.execution import validation as execution_validation
from elspeth.web.execution._validation_materialization import materialize_validation_yaml
from elspeth.web.execution._validation_model import PhaseReport
from elspeth.web.execution.runtime_preflight import RuntimePreflightCoordinator
from elspeth.web.execution.schemas import ValidationReadiness, ValidationResult
from elspeth.web.execution.validation import _compiled_identity_settings, validate_pipeline
from elspeth.web.plugin_policy.models import PluginAvailabilitySnapshot
from tests.unit.web.composer.test_splice_transform_tool import _state as _linear_state
from tests.unit.web.execution.test_validation import (
    _fake_pipeline_config,
    _fake_settings,
    _FakeRuntimeBundle,
    _make_output,
    _make_state,
    _runtime_graph_mock,
    _s3_source_profile_policy_context,
)
from tests.unit.web.execution.test_validation_materialization import _interpretation


def _source(path: object = "input.csv") -> dict[str, Any]:
    return {
        "plugin": "csv",
        "on_success": "rows",
        "options": {"path": path, "schema": {"mode": "observed"}},
        "on_validation_failure": "discard",
    }


def _output(path: object = "outputs/report.csv") -> dict[str, Any]:
    return {
        "sink_name": "result",
        "plugin": "csv",
        "options": {
            "path": path,
            "schema": {"mode": "observed"},
            "mode": "write",
            "collision_policy": "auto_increment",
        },
        "on_write_failure": "discard",
    }


def _provider_options(path: object) -> dict[str, Any]:
    return {
        "provider": "chroma",
        "provider_config": {"collection": "docs", "persist_directory": path},
        "schema": {"mode": "flexible", "fields": ["text: str"]},
        "output_prefix": "rag_",
        "query_field": "text",
    }


def _provider_node(path: object) -> dict[str, Any]:
    return {
        "id": "after",
        "node_type": "transform",
        "plugin": "rag_retrieval",
        "input": "middle",
        "on_success": "result",
        "on_error": "discard",
        "options": _provider_options(path),
    }


def _seed_state() -> CompositionState:
    payload = _linear_state().to_dict()
    payload["sources"]["source"]["options"]["path"] = "input.csv"
    payload["nodes"][1].update(_provider_node("indexes/docs"))
    payload["outputs"][0]["options"] = _output()["options"]
    return CompositionState.from_dict(payload)


# Literal expected diagnostics keep a refusal for an unrelated plugin/options
# error from satisfying a path-security test. The bulk cases exercise every
# existing source branch plus the provider and output gates independently.
_WRITERS = [
    pytest.param("set_source", "source_path_policy_invalid", "source", id="set-source"),
    pytest.param("patch_source_options", "source_path_policy_invalid", "source", id="patch-source"),
    pytest.param("set_output", "sink_path_policy_invalid", "output:result", id="set-output"),
    pytest.param("patch_output_options", "sink_path_policy_invalid", "output:result", id="patch-output"),
    pytest.param("upsert_node", "provider_config_path_policy_invalid", "node:after", id="upsert-provider"),
    pytest.param("patch_node_options", "provider_config_path_policy_invalid", "node:after", id="patch-provider"),
    pytest.param("splice_transform", "provider_config_path_policy_invalid", "node:inserted", id="splice-provider"),
    pytest.param("bulk-source", "source_path_policy_invalid", "source", id="bulk-singular-source"),
    pytest.param("bulk-sources", "source_path_policy_invalid", "source:orders", id="bulk-plural-source"),
    pytest.param("bulk-output", "sink_path_policy_invalid", "output:result", id="bulk-output"),
    pytest.param("bulk-provider", "provider_config_path_policy_invalid", "node:after", id="bulk-provider"),
]


def _mutation(writer: str, path: object) -> tuple[str, dict[str, Any]]:
    if writer == "set_source":
        return writer, _source(path)
    if writer == "patch_source_options":
        return writer, {"patch": {"path": path}}
    if writer == "set_output":
        return writer, _output(path)
    if writer == "patch_output_options":
        return writer, {"sink_name": "result", "patch": {"path": path}}
    if writer == "upsert_node":
        return writer, _provider_node(path)
    if writer == "patch_node_options":
        return writer, {"node_id": "after", "patch": {"provider_config": {"collection": "docs", "persist_directory": path}}}
    if writer == "splice_transform":
        return writer, {
            "predecessor_id": "before",
            "successor_id": "after",
            "node": {
                "id": "inserted",
                "plugin": "rag_retrieval",
                "on_error": "discard",
                "options": {**_provider_options(path), "output_prefix": "lookup_"},
            },
        }
    args = {
        "source": _source(path)
        if writer == "bulk-source"
        else {"plugin": "null", "on_success": "rows", "options": {"schema": {"mode": "observed"}}, "on_validation_failure": "discard"},
        "nodes": [_provider_node(path)] if writer == "bulk-provider" else [],
        "edges": [],
        "outputs": [_output(path)] if writer == "bulk-output" else [],
    }
    if writer == "bulk-provider":
        args["nodes"][0]["input"] = "rows"
    if writer == "bulk-sources":
        args.pop("source")
        args["sources"] = {
            "control": {
                "plugin": "null",
                "on_success": "control_rows",
                "options": {"schema": {"mode": "observed"}},
                "on_validation_failure": "discard",
            },
            "orders": _source(path),
        }
    return "set_pipeline", args


@dataclass
class _LiveMcp:
    server: Server
    scratch: Path
    session_id: str

    async def raw_call(self, name: str, arguments: dict[str, Any]) -> CallToolResult:
        request = CallToolRequest(method="tools/call", params=CallToolRequestParams(name=name, arguments=arguments))
        response = await self.server.request_handlers[CallToolRequest](request)
        assert isinstance(response.root, CallToolResult)
        return response.root

    async def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        response = await self.raw_call(name, arguments)
        assert not response.isError, response
        content = response.content[0]
        assert isinstance(content, TextContent)
        return json.loads(content.text)

    @property
    def private_state(self) -> CompositionState:
        return SessionManager(self.scratch).load(self.session_id)

    @property
    def saved_bytes(self) -> bytes:
        return (self.scratch / f"{self.session_id}.json").read_bytes()

    @property
    def last_audit(self) -> dict[str, Any]:
        lines = (self.scratch / f"{self.session_id}.events.jsonl").read_text().splitlines()
        return json.loads(lines[-1])


async def _live_mcp(tmp_path: Path, root: Path, *, configured: bool = True, state: CompositionState | None = None) -> _LiveMcp:
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    manager = SessionManager(scratch)
    session_id, _ = manager.new_session(name="Path admission")
    manager.save_if_current(session_id, state if state is not None else _seed_state(), expected_token=None)
    catalog = create_catalog_service()
    server = (
        _build_stdio_server(catalog, scratch, root)
        if configured
        else create_server(catalog, scratch, runtime_preflight=None, runtime_preflight_settings_hash=None)
    )
    live = _LiveMcp(server, scratch, session_id)
    assert (await live.call("load_session", {"session_id": session_id}))["success"] is True
    return live


async def _assert_refused_unchanged(
    live: _LiveMcp,
    writer: str,
    path: object,
    code: str,
    component: str,
    *,
    mutation: tuple[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    before = live.private_state
    saved = live.saved_bytes
    public_before = await live.call("get_pipeline_state", {})
    tool, arguments = _mutation(writer, path) if mutation is None else mutation
    result = await live.call(tool, arguments)
    assert live.last_audit["status"] == "success"
    assert live.last_audit["version_before"] == before.version
    assert live.last_audit["version_after"] == before.version
    assert result["success"] is False, result
    assert result["version"] == before.version
    assert result["affected_nodes"] == []
    errors = result["validation"]["errors"]
    assert (errors[0]["error_code"], errors[0]["rejected_component"]) == (code, component), errors
    public_after = await live.call("get_pipeline_state", {})
    assert public_after["state"] == public_before["state"]
    assert live.saved_bytes == saved
    # Saving the live state detects private mutations hidden by response
    # redaction; an idempotent save also preserves the exact durable bytes.
    assert (await live.call("save_session", {"session_id": live.session_id}))["success"] is True
    after = live.private_state
    assert after.to_dict() == before.to_dict()
    assert composition_content_hash(after) == composition_content_hash(before)
    assert live.saved_bytes == saved
    return result


@pytest.mark.asyncio
@pytest.mark.parametrize(("writer", "code", "component"), _WRITERS)
@pytest.mark.parametrize("spelling", ["relative", "absolute"])
async def test_configured_root_admits_file_authoring(tmp_path: Path, writer: str, code: str, component: str, spelling: str) -> None:
    """Removing root authority from any writer must break its positive control."""
    root = tmp_path / "data"
    root.mkdir()
    (root / "admitted.csv").write_text("text\nhello\n")
    live = await _live_mcp(tmp_path, root)
    path = "admitted.csv" if spelling == "relative" else str(root / "admitted.csv")
    tool, arguments = _mutation(writer, path)
    result = await live.call(tool, arguments)
    assert result["success"] is True, result
    assert (await live.call("save_session", {"session_id": live.session_id}))["success"] is True
    assert live.private_state.version > _seed_state().version


@pytest.mark.asyncio
@pytest.mark.parametrize(("writer", "code", "component"), _WRITERS)
@pytest.mark.parametrize("escape", ["absolute", "traversal", "symlink"])
async def test_each_writer_refuses_escape_atomically(tmp_path: Path, writer: str, code: str, component: str, escape: str) -> None:
    """Skipping any gate must expose both the exact reason and state mutation."""
    root = tmp_path / "data"
    root.mkdir()
    outside = tmp_path / "private"
    outside.mkdir()
    (root / "link").symlink_to(outside, target_is_directory=True)
    (root / "outputs").mkdir()
    (root / "outputs" / "link").symlink_to(outside, target_is_directory=True)
    path = {"absolute": str(outside / "data.csv"), "traversal": "../private/data.csv", "symlink": "link/data.csv"}[escape]
    live = await _live_mcp(tmp_path, root)
    if writer == "bulk-output" and escape == "traversal":
        # Existing spelling canonicalization intentionally rejects '..'
        # before the containment gate and keeps its option-error channel.
        await _assert_refused_unchanged(live, writer, path, "plugin_options_invalid", component)
        return
    result = await _assert_refused_unchanged(live, writer, path, code, component)
    message = result["validation"]["errors"][0]["message"]
    assert "configured" in message.lower() and "root" in message.lower()
    assert "blob" not in message.lower()


@pytest.mark.asyncio
@pytest.mark.parametrize(("writer", "code", "component"), _WRITERS)
async def test_embedded_mcp_without_root_refuses_every_local_writer(tmp_path: Path, writer: str, code: str, component: str) -> None:
    live = await _live_mcp(tmp_path, tmp_path / "data", configured=False)
    await _assert_refused_unchanged(live, writer, "input.csv", code, component)


@pytest.mark.asyncio
async def test_embedded_mcp_without_root_keeps_nonfile_source_authoring(tmp_path: Path) -> None:
    live = await _live_mcp(tmp_path, tmp_path / "data", configured=False)
    result = await live.call(
        "set_source",
        {
            "plugin": "null",
            "on_success": "rows",
            "options": {"schema": {"mode": "observed"}},
            "on_validation_failure": "discard",
        },
    )
    assert result["success"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize(("writer", "code", "component"), _WRITERS)
@pytest.mark.parametrize("value", [42, ["input.csv"], None, "", "bad\x00path"])
async def test_malformed_path_is_an_intentional_tool_refusal(tmp_path: Path, writer: str, code: str, component: str, value: object) -> None:
    """Enabling root checks must not expose raw Path TypeError/ValueError."""
    live = await _live_mcp(tmp_path, tmp_path / "not-created")
    tool, arguments = _mutation(writer, value)
    response = await live.raw_call(tool, arguments)
    content = response.content[0]
    assert isinstance(content, TextContent)
    assert "Tool error: TypeError" not in content.text
    assert "Tool error: ValueError" not in content.text
    if writer in {"upsert_node", "patch_node_options", "splice_transform", "bulk-provider"} and value is None:
        assert not response.isError
        assert json.loads(content.text)["success"] is True
    elif response.isError:
        assert content.text.startswith("Tool error: ")
        assert live.last_audit["status"] == "arg_error"
        assert live.last_audit["error_class"] == "ToolArgumentError"
    else:
        assert json.loads(content.text)["success"] is False
        assert live.last_audit["status"] == "success"


@pytest.mark.asyncio
async def test_missing_contained_files_and_root_are_not_missing_authority(tmp_path: Path) -> None:
    root = tmp_path / "not-created"
    live = await _live_mcp(tmp_path, root)
    assert (await live.call("set_source", _source("missing.csv")))["success"] is True
    assert (await live.call("set_output", _output("reports/new.csv")))["success"] is True
    assert not root.exists()


@pytest.mark.asyncio
async def test_source_encoding_patch_validates_and_retains_the_admitted_path(tmp_path: Path) -> None:
    live = await _live_mcp(tmp_path, tmp_path / "data")
    assert (await live.call("set_source", _source()))["success"] is True
    assert (await live.call("patch_source_options", {"patch": {"encoding": "utf-8-sig"}}))["success"] is True
    await live.call("save_session", {"session_id": live.session_id})
    assert live.private_state.sources["source"].options["path"] == "input.csv"
    assert live.private_state.sources["source"].options["encoding"] == "utf-8-sig"


@pytest.mark.asyncio
@pytest.mark.parametrize("writer", ["patch_source_options", "patch_output_options", "patch_node_options"])
async def test_nonpath_patch_cannot_reauthorize_a_legacy_escape(tmp_path: Path, writer: str) -> None:
    payload = _seed_state().to_dict()
    code, component, arguments = "source_path_policy_invalid", "source", {"patch": {"encoding": "utf-8"}}
    if writer == "patch_source_options":
        payload["sources"]["source"]["options"]["path"] = "/legacy-private/input.csv"
    elif writer == "patch_output_options":
        payload["outputs"][0]["options"]["path"] = "/legacy-private/output.csv"
        code, component = "sink_path_policy_invalid", "output:result"
        arguments = {"sink_name": "result", "patch": {"encoding": "utf-8"}}
    else:
        payload["nodes"][1]["options"]["provider_config"]["persist_directory"] = "/legacy-private/index"
        code, component = "provider_config_path_policy_invalid", "node:after"
        arguments = {"node_id": "after", "patch": {"output_prefix": "retrieved_"}}
    live = await _live_mcp(tmp_path, tmp_path / "data", state=CompositionState.from_dict(payload))
    before, saved = live.private_state.to_dict(), live.saved_bytes
    result = await live.call(writer, arguments)
    assert result["success"] is False
    assert (result["validation"]["errors"][0]["error_code"], result["validation"]["errors"][0]["rejected_component"]) == (code, component)
    assert result["affected_nodes"] == []
    await live.call("save_session", {"session_id": live.session_id})
    assert live.private_state.to_dict() == before
    assert live.saved_bytes == saved


@pytest.mark.asyncio
async def test_public_export_omits_private_paths_without_replacing_live_state(tmp_path: Path) -> None:
    payload = _seed_state().to_dict()
    payload["sources"]["source"]["options"]["path"] = str(tmp_path / "PRIVATE-SOURCE.csv")
    payload["outputs"][0]["options"]["path"] = str(tmp_path / "PRIVATE-OUTPUT.csv")
    payload["nodes"][1]["options"]["provider_config"]["persist_directory"] = str(tmp_path / "PRIVATE-PROVIDER")
    live = await _live_mcp(tmp_path, tmp_path, state=CompositionState.from_dict(payload))
    before = deepcopy(live.private_state.to_dict())
    exported = await live.call("generate_yaml", {})
    assert exported["success"] is True, exported
    public_text = json.dumps(exported)
    for canary in ("PRIVATE-SOURCE", "PRIVATE-OUTPUT", "PRIVATE-PROVIDER"):
        assert canary not in public_text
    await live.call("save_session", {"session_id": live.session_id})
    assert live.private_state.to_dict() == before


def _preview_state(path: str = "input.csv") -> CompositionState:
    payload = _seed_state().to_dict()
    payload["sources"]["source"].update(_source(path))
    payload["sources"]["source"]["on_success"] = "result"
    payload["nodes"] = []
    payload["edges"] = []
    return CompositionState.from_dict(payload)


@pytest.mark.asyncio
async def test_in_root_file_preview_reaches_actual_plugin_and_graph_checks(tmp_path: Path) -> None:
    root = tmp_path / "data"
    root.mkdir()
    (root / "input.csv").write_text("text\nhello\n")
    live = await _live_mcp(tmp_path, root, state=_preview_state())
    assert (await live.call("set_source", {**_source(), "on_success": "result"}))["success"] is True
    preview = await live.call("preview_pipeline", {})
    preflight = preview["runtime_preflight"]
    assert preflight["is_valid"] is True, preflight
    assert {"path_allowlist", "plugin_instantiation", "graph_structure", "schema_compatibility"} <= {
        check["name"] for check in preflight["checks"] if check["passed"]
    }
    assert preview["data"]["preview_is_valid"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("component", ["source", "output", "provider"])
async def test_loaded_legacy_escape_is_preserved_and_refused_by_preview(tmp_path: Path, component: str) -> None:
    root = tmp_path / "data"
    root.mkdir()
    payload = _preview_state().to_dict()
    escaped = str(tmp_path / "PRIVATE-LEGACY-PATH")
    if component == "source":
        payload["sources"]["source"]["options"]["path"] = escaped
    elif component == "output":
        payload["outputs"][0]["options"]["path"] = escaped
    else:
        payload = _seed_state().to_dict()
        payload["nodes"][1]["options"]["provider_config"]["persist_directory"] = escaped
    live = await _live_mcp(tmp_path, root, state=CompositionState.from_dict(payload))
    before, saved = live.private_state.to_dict(), live.saved_bytes
    preview = await live.call("preview_pipeline", {})
    preflight = preview["runtime_preflight"]
    assert preflight["is_valid"] is False
    assert "path_allowlist" in {check["name"] for check in preflight["checks"] if not check["passed"]}
    assert preview["data"]["preview_is_valid"] is False
    await live.call("save_session", {"session_id": live.session_id})
    assert live.private_state.to_dict() == before
    assert live.saved_bytes == saved


_SOURCE_SINK_PATH_KEYS = [
    pytest.param("source", "path", id="source-path"),
    pytest.param("source", "file", id="source-file"),
    pytest.param("output", "path", id="sink-path"),
    pytest.param("output", "file", id="sink-file"),
    pytest.param("output", "persist_directory", id="sink-persist-directory"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("component", "key"), _SOURCE_SINK_PATH_KEYS)
@pytest.mark.parametrize("escape_link", [False, True])
async def test_loaded_legacy_null_path_stops_before_materialization_and_plugin_setup(
    tmp_path: Path, component: str, key: str, escape_link: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A present null must fail the path gate before any scalar-to-path rewrite."""
    root = tmp_path / "data"
    root.mkdir()
    (root / "input.csv").write_text("text\ninside\n")
    outside = tmp_path / "outside.csv"
    outside.write_text("text\noutside\n")
    if escape_link:
        (root / "None").symlink_to(outside)
    payload = _preview_state().to_dict()
    options = payload["sources"]["source"]["options"] if component == "source" else payload["outputs"][0]["options"]
    options[key] = None
    live = await _live_mcp(tmp_path, root, state=CompositionState.from_dict(payload))
    before, saved = live.private_state, live.saved_bytes
    before_public = await live.call("get_pipeline_state", {})
    outside_bytes = outside.read_bytes()
    reached: list[dict[str, Any]] = []
    materialize = execution_validation.materialize_validation_yaml
    instantiate = execution_validation.instantiate_runtime_plugins

    def observe_materialization(*args: Any, **kwargs: Any) -> Any:
        result = materialize(*args, **kwargs)
        reached.append({"phase": "materialization", "yaml": result.artifact.pipeline_yaml if isinstance(result, PhaseReport) else None})
        return result

    def observe_plugin_setup(*args: Any, **kwargs: Any) -> Any:
        reached.append({"phase": "plugin_setup"})
        return instantiate(*args, **kwargs)

    monkeypatch.setattr(execution_validation, "materialize_validation_yaml", observe_materialization)
    monkeypatch.setattr(execution_validation, "instantiate_runtime_plugins", observe_plugin_setup)
    preview = await live.call("preview_pipeline", {})
    preflight = preview["runtime_preflight"]
    (tmp_path / "observed-null-preview.json").write_text(
        json.dumps(
            {
                "component": component,
                "key": key,
                "escape_link": escape_link,
                "root": str(root),
                "outside": str(outside),
                "reached": reached,
                "preflight": preflight,
                "outside_bytes_unchanged": outside.read_bytes() == outside_bytes,
            },
            indent=2,
        )
    )
    path_checks = [check for check in preflight["checks"] if check["name"] == "path_allowlist"]
    assert len(path_checks) == 1
    assert path_checks[0]["passed"] is False, {"preflight": preflight, "reached": reached}
    assert preflight["is_valid"] is False
    assert preview["data"]["preview_is_valid"] is False
    assert reached == []
    assert outside.read_bytes() == outside_bytes
    assert live.saved_bytes == saved
    after_public = await live.call("get_pipeline_state", {})
    assert after_public["state"] == before_public["state"]
    assert (await live.call("save_session", {"session_id": live.session_id}))["success"] is True
    after = live.private_state
    assert after.to_dict() == before.to_dict()
    assert after.version == before.version
    assert composition_content_hash(after) == composition_content_hash(before)
    assert live.saved_bytes == saved


@pytest.mark.parametrize(("component", "key"), _SOURCE_SINK_PATH_KEYS)
def test_null_path_cannot_be_materialized_into_execution_or_identity_yaml(tmp_path: Path, component: str, key: str) -> None:
    """Calling either materializer directly must not invent a pathname from null."""
    root = tmp_path / "data"
    root.mkdir()
    (root / "None").symlink_to(tmp_path / "outside.csv")
    payload = _preview_state().to_dict()
    options = payload["sources"]["source"]["options"] if component == "source" else payload["outputs"][0]["options"]
    options[key] = None
    state = CompositionState.from_dict(payload)
    authority = McpConfiguredRootAuthority(root)
    with pytest.raises(ValueError, match="nonempty string path"):
        materialize_validation_yaml(
            _interpretation(state),
            yaml_generator=yaml_generator,
            data_dir=root,
            session_id=None,
            blob_get_metadata=None,
            load_yaml=yaml.safe_load,
            mcp_root_authority=authority,
        )
    with pytest.raises(ValueError, match="nonempty string path"):
        _compiled_identity_settings(state, yaml_generator=yaml_generator, data_dir=root, session_id=None, mcp_root_authority=authority)


def test_missing_source_sink_keys_and_nullable_provider_survive_both_materializers(tmp_path: Path) -> None:
    """Absent optional keys stay absent; only nested provider null is retained."""
    payload = _seed_state().to_dict()
    payload["sources"]["source"]["options"].pop("path")
    payload["outputs"][0]["options"].pop("path")
    payload["nodes"][1]["options"]["provider_config"]["persist_directory"] = None
    state = CompositionState.from_dict(payload)
    authority = McpConfiguredRootAuthority(tmp_path)
    materialized = materialize_validation_yaml(
        _interpretation(state),
        yaml_generator=yaml_generator,
        data_dir=tmp_path,
        session_id=None,
        blob_get_metadata=None,
        load_yaml=yaml.safe_load,
        mcp_root_authority=authority,
    )
    assert isinstance(materialized, PhaseReport)
    execution = yaml.safe_load(materialized.artifact.pipeline_yaml)
    identity = _compiled_identity_settings(
        state,
        yaml_generator=yaml_generator,
        data_dir=tmp_path,
        session_id=None,
        mcp_root_authority=authority,
    ).audit_safe_settings()
    for document in (execution, identity):
        assert not {"path", "file"}.intersection(document["sources"]["source"]["options"])
        assert not {"path", "file", "persist_directory"}.intersection(document["sinks"]["result"]["options"])
        assert document["transforms"][1]["options"]["provider_config"]["persist_directory"] is None


@pytest.mark.asyncio
async def test_canonical_root_and_contained_leaf_symlinks_allow_authoring(tmp_path: Path) -> None:
    actual = tmp_path / "actual-data"
    actual.mkdir()
    (actual / "input.csv").write_text("text\nhello\n")
    (actual / "alias.csv").symlink_to(actual / "input.csv")
    root = tmp_path / "configured-data"
    root.symlink_to(actual, target_is_directory=True)
    live = await _live_mcp(tmp_path, root)
    assert (await live.call("set_source", _source("alias.csv")))["success"] is True


@pytest.mark.asyncio
async def test_preview_rechecks_a_symlink_changed_since_admission(tmp_path: Path) -> None:
    root = tmp_path / "data"
    root.mkdir()
    target = root / "input.csv"
    target.write_text("text\nhello\n")
    link = root / "alias.csv"
    link.symlink_to(target)
    live = await _live_mcp(tmp_path, root, state=_preview_state())
    assert (await live.call("set_source", {**_source("alias.csv"), "on_success": "result"}))["success"] is True
    # Admission was valid; a different later resolution must not inherit it.
    link.unlink()
    link.symlink_to(tmp_path / "private.csv")
    preview = await live.call("preview_pipeline", {})
    assert "path_allowlist" in {check["name"] for check in preview["runtime_preflight"]["checks"] if not check["passed"]}


@pytest.mark.asyncio
@pytest.mark.parametrize("component", ["source", "output", "provider"])
@pytest.mark.parametrize("value", ["", 42, ["input.csv"], "bad\x00path"])
async def test_malformed_legacy_path_preview_has_route_accurate_refusal(tmp_path: Path, component: str, value: object) -> None:
    """Legacy lexical errors must refuse before Path and direct repair to MCP authority."""
    payload = _seed_state().to_dict()
    if component == "source":
        payload["sources"]["source"]["options"]["path"] = value
    elif component == "output":
        payload["outputs"][0]["options"]["path"] = value
    else:
        payload["nodes"][1]["options"]["provider_config"]["persist_directory"] = value
    live = await _live_mcp(tmp_path, tmp_path / "data", state=CompositionState.from_dict(payload))
    preview = await live.call("preview_pipeline", {})
    result = preview["runtime_preflight"]
    assert result["is_valid"] is False
    assert "path_allowlist" in {check["name"] for check in result["checks"] if not check["passed"]}
    assert "configured data root" in result["errors"][0]["suggestion"]


@pytest.mark.asyncio
async def test_bulk_sink_spelling_is_canonicalized_without_an_mcp_session_segment(tmp_path: Path) -> None:
    live = await _live_mcp(tmp_path, tmp_path / "data")
    tool, arguments = _mutation("bulk-output", "report.csv")
    assert (await live.call(tool, arguments))["success"] is True
    await live.call("save_session", {"session_id": live.session_id})
    assert live.private_state.outputs[0].options["path"] == "outputs/report.csv"


@pytest.mark.asyncio
@pytest.mark.parametrize("source_form", ["source", "sources"])
@pytest.mark.parametrize("late_component", ["output", "provider"])
async def test_bulk_good_file_source_and_late_bad_path_are_atomic(tmp_path: Path, source_form: str, late_component: str) -> None:
    """Neither source form may replace live state before a later component refuses."""
    root = tmp_path / "data"
    root.mkdir()
    (root / "input.csv").write_text("text\nhello\n")
    live = await _live_mcp(tmp_path, root)
    arguments: dict[str, Any] = {"nodes": [], "edges": [], "outputs": [_output()]}
    if source_form == "source":
        arguments["source"] = _source()
    else:
        arguments["sources"] = {"orders": _source()}
    if late_component == "output":
        arguments["outputs"].append({**_output(str(tmp_path / "outside.csv")), "sink_name": "late"})
        code, component = "sink_path_policy_invalid", "output:late"
    else:
        arguments["nodes"] = [
            {
                **_provider_node("indexes/good"),
                "id": "early",
                "input": "rows",
                "on_success": "next",
                "options": {**_provider_options("indexes/good"), "output_prefix": "early_"},
            },
            {**_provider_node(str(tmp_path / "outside-index")), "input": "next"},
        ]
        code, component = "provider_config_path_policy_invalid", "node:after"
    await _assert_refused_unchanged(live, "bulk-source", "unused", code, component, mutation=("set_pipeline", arguments))


@pytest.mark.parametrize("provider_path", ["indexes/docs", None])
def test_mcp_execution_and_compiled_identity_resolve_the_same_private_paths(tmp_path: Path, provider_path: str | None) -> None:
    """Dropping authority at either materializer must expose a path disagreement."""
    payload = _seed_state().to_dict()
    payload["nodes"][1]["options"]["provider_config"]["persist_directory"] = provider_path
    state = CompositionState.from_dict(payload)
    authority = McpConfiguredRootAuthority(tmp_path / "data")
    generator = yaml_generator
    report = materialize_validation_yaml(
        _interpretation(state),
        yaml_generator=generator,
        data_dir=authority.root,
        session_id=None,
        blob_get_metadata=None,
        load_yaml=yaml.safe_load,
        mcp_root_authority=authority,
    )
    assert isinstance(report, PhaseReport)
    execution = yaml.safe_load(report.artifact.pipeline_yaml)
    identity = _compiled_identity_settings(
        state,
        yaml_generator=generator,
        data_dir=authority.root,
        session_id=None,
        mcp_root_authority=authority,
    ).audit_safe_settings()
    for document in (execution, identity):
        assert document["sources"]["source"]["options"]["path"] == str(authority.root / "input.csv")
        assert document["sinks"]["result"]["options"]["path"] == str(authority.root / "outputs/report.csv")
        assert document["transforms"][1]["options"]["provider_config"]["persist_directory"] == (
            str(authority.root / "indexes/docs") if provider_path is not None else None
        )


def test_profiled_validation_passes_authority_to_real_compiled_identity(tmp_path: Path) -> None:
    """The profiled caller must keep the same authority as executable materialization."""
    profiles, snapshot = _s3_source_profile_policy_context()
    authority = McpConfiguredRootAuthority(tmp_path)
    state = _make_state(
        source_plugin="aws_s3",
        source_options={
            "profile": "demo-input",
            "key": "records/input.csv",
            "format": "csv",
            "schema": {"mode": "observed"},
        },
        outputs=(_make_output(options={"path": "outputs/report.csv", "schema": {"mode": "observed"}}),),
    )
    identities: list[dict[str, Any]] = []

    def record_identity(*args: Any, **kwargs: Any) -> Any:
        assert kwargs["mcp_root_authority"] is authority
        document = _compiled_identity_settings(*args, **kwargs)
        identities.append(document.audit_safe_settings())
        return document

    with (
        patch("elspeth.web.execution.validation._compiled_identity_settings", record_identity),
        patch("elspeth.web.execution.validation.load_settings_from_yaml_string", return_value=_fake_settings()),
        patch("elspeth.web.execution.validation.instantiate_runtime_plugins", return_value=_FakeRuntimeBundle()),
        patch("elspeth.web.execution.preflight.bind_profiled_s3_source_audit_identities"),
        patch("elspeth.web.execution.validation.build_runtime_graph", return_value=_runtime_graph_mock()),
        patch("elspeth.web.execution.validation.assemble_and_validate_pipeline_config", return_value=_fake_pipeline_config()),
    ):
        result = validate_pipeline(
            state,
            _StdioValidationSettings(data_dir=tmp_path),
            yaml_generator,
            plugin_snapshot=snapshot,
            profile_registry=profiles,
            catalog=create_catalog_service(),
            mcp_root_authority=authority,
        )
    assert result.is_valid is True, result
    assert len(identities) == 1
    assert identities[0]["sinks"]["primary"]["options"]["path"] == str(tmp_path / "outputs/report.csv")


def test_mcp_root_is_nominal_and_cannot_mix_with_web_custody(tmp_path: Path) -> None:
    catalog = create_catalog_service()
    snapshot = PluginAvailabilitySnapshot.for_trained_operator(catalog)
    view = PolicyCatalogView.for_trained_operator(catalog, snapshot)
    invalid: Any = tmp_path
    with pytest.raises(TypeError, match="McpConfiguredRootAuthority"):
        ToolContext(catalog=view, plugin_snapshot=snapshot, mcp_root_authority=invalid)
    for custody in ({"session_id": "web-session"}, {"data_dir": str(tmp_path)}):
        with pytest.raises(ValueError, match="Web session custody"):
            ToolContext(catalog=view, plugin_snapshot=snapshot, mcp_root_authority=McpConfiguredRootAuthority(tmp_path), **custody)
    invalid_root: Any = str(tmp_path)
    with pytest.raises(TypeError, match="must be a Path"):
        McpConfiguredRootAuthority(invalid_root)
    leaf = tmp_path / "file"
    leaf.write_text("not a directory")
    with pytest.raises(ValueError, match="must be a directory"):
        McpConfiguredRootAuthority(leaf)


@pytest.mark.parametrize("case", ["wrong-type", "web-session", "different-root"])
def test_empty_validation_cannot_hide_an_invalid_owned_path_context(tmp_path: Path, case: str) -> None:
    """Owned configuration errors must precede even the empty-composition shortcut."""
    catalog = create_catalog_service()
    authority: Any = McpConfiguredRootAuthority(tmp_path)
    data_dir = tmp_path / "other" if case == "different-root" else tmp_path
    session_id = "web-session" if case == "web-session" else None
    if case == "wrong-type":
        authority = tmp_path
    payload = _preview_state().to_dict()
    payload["sources"], payload["outputs"] = {}, []
    with pytest.raises(TypeError if case == "wrong-type" else ValueError):
        validate_pipeline(
            CompositionState.from_dict(payload),
            _StdioValidationSettings(data_dir=data_dir),
            yaml_generator,
            plugin_snapshot=PluginAvailabilitySnapshot.for_trained_operator(catalog),
            profile_registry=None,
            catalog=catalog,
            session_id=session_id,
            mcp_root_authority=authority,
        )


@pytest.mark.asyncio
async def test_shared_coordinator_distinguishes_authority_mode_and_canonical_root(tmp_path: Path) -> None:
    """Equal caller settings hashes cannot let one filesystem mode borrow another's verdict."""
    coordinator = RuntimePreflightCoordinator()
    release = asyncio.Event()
    started: list[str] = []
    servers = []
    first_root = tmp_path / "a"
    first_root.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(first_root, target_is_directory=True)
    roots = [first_root, alias, tmp_path / "b", None]
    for index, root in enumerate(roots):

        async def preflight(state: CompositionState, label: str = str(index)) -> ValidationResult:
            started.append(label)
            await release.wait()
            return ValidationResult(
                is_valid=True,
                checks=[],
                errors=[],
                warnings=[],
                readiness=ValidationReadiness(authoring_valid=True, execution_ready=True, completion_ready=True, blockers=[]),
            )

        scratch = tmp_path / str(index)
        scratch.mkdir()
        servers.append(
            _LiveMcp(
                create_server(
                    create_catalog_service(),
                    scratch,
                    runtime_preflight=preflight,
                    runtime_preflight_settings_hash="same-settings",
                    runtime_preflight_coordinator=coordinator,
                    session_scope_provider=lambda: "same-scope",
                    mcp_root_authority=McpConfiguredRootAuthority(root) if root is not None else None,
                ),
                scratch,
                "unused",
            )
        )
    calls = [asyncio.create_task(live.call("preview_pipeline", {})) for live in servers]
    try:
        for _ in range(100):
            if len(started) >= 3:
                break
            await asyncio.sleep(0.001)
        assert len(started) == 3
    finally:
        release.set()
        await asyncio.gather(*calls)
