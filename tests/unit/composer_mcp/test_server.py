"""Tests for composer MCP server — tool registration and dispatch."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import yaml

from elspeth.composer_mcp.server import _build_tool_defs, _dispatch_tool, create_server
from elspeth.composer_mcp.session import SessionCheckout, SessionManager
from elspeth.contracts.composer_audit import ComposerToolRecorder
from elspeth.web.catalog.protocol import CatalogService
from elspeth.web.catalog.schemas import PluginSummary
from elspeth.web.composer.state import (
    CompositionState,
    NodeSpec,
    OutputSpec,
    PipelineMetadata,
    SourceSpec,
)
from elspeth.web.interpretation_state import SOURCE_AUTHORING_KEY


def _empty_state() -> CompositionState:
    return CompositionState(
        source=None,
        nodes=(),
        edges=(),
        outputs=(),
        metadata=PipelineMetadata(),
        version=1,
    )


def _invalid_contract_state() -> CompositionState:
    return CompositionState(
        source=SourceSpec(
            plugin="csv",
            on_success="t1",
            options={"path": "/data/blobs/input.csv", "schema": {"mode": "observed"}},
            on_validation_failure="quarantine",
        ),
        nodes=(
            NodeSpec(
                id="t1",
                node_type="transform",
                plugin="value_transform",
                input="t1",
                on_success="main",
                on_error="discard",
                options={
                    "required_input_fields": ["text"],
                    "operations": [{"target": "out", "expression": "row['text']"}],
                    "schema": {"mode": "observed"},
                },
                condition=None,
                routes=None,
                fork_to=None,
                branches=None,
                policy=None,
                merge=None,
            ),
        ),
        edges=(),
        outputs=(
            OutputSpec(
                name="main",
                plugin="csv",
                options={"path": "outputs/out.csv", "schema": {"mode": "observed"}, "mode": "write", "collision_policy": "auto_increment"},
                on_write_failure="discard",
            ),
        ),
        metadata=PipelineMetadata(),
        version=1,
    )


def _valid_state_with_no_edge_contracts() -> CompositionState:
    return CompositionState(
        source=SourceSpec(
            plugin="csv",
            on_success="main",
            options={"path": "/data/in.csv", "schema": {"mode": "observed"}},
            on_validation_failure="quarantine",
        ),
        nodes=(),
        edges=(),
        outputs=(
            OutputSpec(
                name="main",
                plugin="csv",
                options={"path": "outputs/out.csv", "schema": {"mode": "observed"}, "mode": "write", "collision_policy": "auto_increment"},
                on_write_failure="discard",
            ),
        ),
        metadata=PipelineMetadata(),
        version=1,
    )


def _connection_valid_field_mapper_state_without_edges() -> CompositionState:
    return CompositionState(
        source=SourceSpec(
            plugin="text",
            on_success="mapper_in",
            options={"path": "/data/in.txt", "column": "text", "schema": {"mode": "observed"}},
            on_validation_failure="quarantine",
        ),
        nodes=(
            NodeSpec(
                id="map_body",
                node_type="transform",
                plugin="field_mapper",
                input="mapper_in",
                on_success="main",
                on_error="discard",
                options={
                    "schema": {"mode": "observed", "guaranteed_fields": ["text"], "required_fields": ["text"]},
                    "mapping": {"text": "body"},
                },
                condition=None,
                routes=None,
                fork_to=None,
                branches=None,
                policy=None,
                merge=None,
            ),
        ),
        edges=(),
        outputs=(
            OutputSpec(
                name="main",
                plugin="csv",
                options={
                    "path": "outputs/out.csv",
                    "schema": {"mode": "observed", "required_fields": ["body"]},
                    "mode": "write",
                    "collision_policy": "auto_increment",
                },
                on_write_failure="discard",
            ),
        ),
        metadata=PipelineMetadata(),
        version=1,
    )


def _mock_catalog() -> CatalogService:
    catalog = MagicMock(spec=CatalogService)
    catalog.list_sources.return_value = [
        PluginSummary(name="csv", description="CSV source", plugin_type="source", config_fields=[]),
    ]
    catalog.list_transforms.return_value = [
        PluginSummary(name="value_transform", description="Value transform", plugin_type="transform", config_fields=[]),
    ]
    catalog.list_sinks.return_value = [
        PluginSummary(name="csv", description="CSV sink", plugin_type="sink", config_fields=[]),
        PluginSummary(name="json", description="JSON sink", plugin_type="sink", config_fields=[]),
    ]
    return catalog


def _call_handler(handlers: dict[object, object], name: str, arguments: dict[str, object]) -> object:
    from mcp.types import CallToolRequest, CallToolRequestParams

    request = CallToolRequest(
        method="tools/call",
        params=CallToolRequestParams(name=name, arguments=arguments),
    )
    return handlers[CallToolRequest](request)  # type: ignore[index,operator]


def _session_authority(scratch_dir: Path) -> tuple[SessionManager, list[SessionCheckout | None]]:
    return SessionManager(scratch_dir), [None]


def _dispatch_session_once(
    tool_name: str,
    arguments: dict[str, Any],
    state: CompositionState,
    scratch_dir: Path,
) -> dict[str, Any]:
    session_manager, session_checkout_ref = _session_authority(scratch_dir)
    return _dispatch_tool(
        tool_name,
        arguments,
        state,
        _mock_catalog(),
        scratch_dir,
        session_manager=session_manager,
        session_checkout_ref=session_checkout_ref,
    )


class TestBuildToolDefs:
    """Tests for _build_tool_defs() tool registration."""

    def test_returns_more_than_20_tools(self) -> None:
        tools = _build_tool_defs()
        assert len(tools) > 20

    def test_tool_count_matches_registry(self) -> None:
        """Tool count must equal composer subset + session tools."""
        from elspeth.composer_mcp.server import _COMPOSER_TOOL_NAMES, _SESSION_TOOL_DEFS

        tools = _build_tool_defs()
        expected = len(_COMPOSER_TOOL_NAMES) + len(_SESSION_TOOL_DEFS)
        assert len(tools) == expected, f"Expected {expected}, got {len(tools)}"

    def test_all_tools_have_name_and_description(self) -> None:
        for tool in _build_tool_defs():
            assert "name" in tool, f"Tool missing 'name': {tool}"
            assert "description" in tool, f"Tool {tool['name']} missing 'description'"
            assert tool["name"], "Tool name must be non-empty"
            assert tool["description"], f"Tool {tool['name']} has empty description"

    def test_discovery_tools_present(self) -> None:
        names = {t["name"] for t in _build_tool_defs()}
        for expected in ("list_sources", "list_transforms", "list_sinks", "get_plugin_schema", "get_expression_grammar"):
            assert expected in names, f"Discovery tool '{expected}' missing"

    def test_mutation_tools_present(self) -> None:
        names = {t["name"] for t in _build_tool_defs()}
        for expected in ("set_source", "upsert_node", "upsert_edge", "set_output", "set_pipeline"):
            assert expected in names, f"Mutation tool '{expected}' missing"

    def test_session_tools_present(self) -> None:
        names = {t["name"] for t in _build_tool_defs()}
        for expected in ("new_session", "save_session", "load_session", "list_sessions", "generate_yaml", "delete_session"):
            assert expected in names, f"Session tool '{expected}' missing"

    def test_get_plugin_assistance_tool_registered(self) -> None:
        names = {t["name"] for t in _build_tool_defs()}
        assert "get_plugin_assistance" in names

    def test_blob_and_secret_tools_excluded(self) -> None:
        names = {t["name"] for t in _build_tool_defs()}
        for excluded in (
            "list_blobs",
            "set_source_from_blob",
            "get_blob_metadata",
            "list_secret_refs",
            "validate_secret_ref",
            "wire_secret_ref",
        ):
            assert excluded not in names, f"Blob/secret tool '{excluded}' should be excluded"


class TestDispatchTool:
    @pytest.mark.asyncio
    async def test_live_session_manager_token_authorizes_save_after_mutation(self, scratch_dir: Path) -> None:
        recorder = MagicMock(spec_set=ComposerToolRecorder)
        server = create_server(_mock_catalog(), scratch_dir, recorder=recorder)
        created = await _call_handler(server.request_handlers, "new_session", {"name": "CAS"})  # type: ignore[misc]
        created_payload = json.loads(created.root.content[0].text)
        session_id = created_payload["data"]["session_id"]
        assert "token" not in created_payload["data"]

        mutated = await _call_handler(  # type: ignore[misc]
            server.request_handlers,
            "set_metadata",
            {
                "patch": {"name": "CAS updated"},
            },
        )
        assert json.loads(mutated.root.content[0].text)["success"] is True

        saved = await _call_handler(server.request_handlers, "save_session", {"session_id": session_id})  # type: ignore[misc]
        saved_payload = json.loads(saved.root.content[0].text)
        assert saved_payload["success"] is True
        assert "token" not in saved_payload["data"]

        mutated_again = await _call_handler(  # type: ignore[misc]
            server.request_handlers,
            "set_metadata",
            {
                "patch": {"description": "second mutation"},
            },
        )
        assert json.loads(mutated_again.root.content[0].text)["success"] is True

        saved_again = await _call_handler(server.request_handlers, "save_session", {"session_id": session_id})  # type: ignore[misc]
        saved_again_payload = json.loads(saved_again.root.content[0].text)
        assert saved_again_payload["success"] is True
        assert "token" not in saved_again_payload["data"]

        for call in recorder.record.call_args_list:
            invocation = call.args[0]
            if invocation.result_canonical is not None:
                assert "token" not in json.loads(invocation.result_canonical).get("data", {})

        durable = SessionManager(scratch_dir).load(session_id)
        assert durable.metadata.name == "CAS updated"
        assert durable.metadata.description == "second mutation"
        assert durable.version == 3

    @pytest.mark.asyncio
    async def test_live_save_to_non_active_session_conflicts_and_preserves_sessions(self, scratch_dir: Path) -> None:
        server = create_server(_mock_catalog(), scratch_dir)
        created_a = await _call_handler(server.request_handlers, "new_session", {"name": "A"})  # type: ignore[misc]
        session_a = json.loads(created_a.root.content[0].text)["data"]["session_id"]
        created_b = await _call_handler(server.request_handlers, "new_session", {"name": "B"})  # type: ignore[misc]
        session_b = json.loads(created_b.root.content[0].text)["data"]["session_id"]
        path_a = scratch_dir / f"{session_a}.json"
        path_b = scratch_dir / f"{session_b}.json"
        original_a = path_a.read_bytes()
        original_b = path_b.read_bytes()

        await _call_handler(  # type: ignore[misc]
            server.request_handlers,
            "set_source",
            {
                "plugin": "csv",
                "on_success": "source_out",
                "options": {"path": "/data/blobs/input.csv", "schema": {"mode": "observed"}},
                "on_validation_failure": "discard",
            },
        )

        rejected = await _call_handler(server.request_handlers, "save_session", {"session_id": session_a})  # type: ignore[misc]

        assert rejected.root.isError is True
        assert path_a.read_bytes() == original_a
        assert path_b.read_bytes() == original_b

    @pytest.mark.asyncio
    async def test_delete_clears_active_checkout_authority(self, scratch_dir: Path) -> None:
        server = create_server(_mock_catalog(), scratch_dir)
        created = await _call_handler(server.request_handlers, "new_session", {"name": "deleted"})  # type: ignore[misc]
        created_payload = json.loads(created.root.content[0].text)
        session_id = created_payload["data"]["session_id"]
        original = CompositionState.from_dict(created_payload["state"])

        deleted = await _call_handler(server.request_handlers, "delete_session", {"session_id": session_id})  # type: ignore[misc]
        assert json.loads(deleted.root.content[0].text)["success"] is True

        # Recreate the same durable bytes externally. A retained pre-delete
        # checkout token would now compare equal and incorrectly regain write
        # authority; the live server must have cleared it after the tombstone.
        path = SessionManager(scratch_dir).save(session_id, original)
        recreated = path.read_bytes()

        await _call_handler(  # type: ignore[misc]
            server.request_handlers,
            "set_source",
            {
                "plugin": "csv",
                "on_success": "source_out",
                "options": {"path": "/data/blobs/input.csv", "schema": {"mode": "observed"}},
                "on_validation_failure": "discard",
            },
        )
        rejected = await _call_handler(server.request_handlers, "save_session", {"session_id": session_id})  # type: ignore[misc]

        assert rejected.root.isError is True
        assert path.read_bytes() == recreated

    @pytest.mark.asyncio
    async def test_delete_clears_active_authority_when_tombstone_recording_raises(
        self,
        scratch_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from elspeth.composer_mcp.audit import JsonlEventRecorder, events_sidecar_path
        from elspeth.contracts.composer_audit import ComposerToolInvocation

        original_record = JsonlEventRecorder.record
        delete_attempts = 0

        def record_then_fail_delete(
            recorder: JsonlEventRecorder,
            invocation: ComposerToolInvocation,
        ) -> None:
            nonlocal delete_attempts
            original_record(recorder, invocation)
            if invocation.tool_name == "delete_session":
                delete_attempts += 1
                raise RuntimeError("delete tombstone recorder failure")

        monkeypatch.setattr(JsonlEventRecorder, "record", record_then_fail_delete)
        server = create_server(_mock_catalog(), scratch_dir)
        created = await _call_handler(server.request_handlers, "new_session", {"name": "deleted"})  # type: ignore[misc]
        created_payload = json.loads(created.root.content[0].text)
        session_id = created_payload["data"]["session_id"]
        path = scratch_dir / f"{session_id}.json"
        original_bytes = path.read_bytes()
        sidecar = events_sidecar_path(scratch_dir, session_id)

        delete_error = await _call_handler(server.request_handlers, "delete_session", {"session_id": session_id})  # type: ignore[misc]

        assert delete_error.root.isError is True
        assert delete_attempts == 1
        assert not path.exists()
        tombstone_audit = sidecar.read_bytes()

        # Recreate the deleted session with byte-identical durable state. The
        # failed audit must not retain either its scope or its old CAS evidence.
        path.write_bytes(original_bytes)
        await _call_handler(  # type: ignore[misc]
            server.request_handlers,
            "set_source",
            {
                "plugin": "csv",
                "on_success": "source_out",
                "options": {"path": "/data/blobs/input.csv", "schema": {"mode": "observed"}},
                "on_validation_failure": "discard",
            },
        )
        save_error = await _call_handler(server.request_handlers, "save_session", {"session_id": session_id})  # type: ignore[misc]

        assert save_error.root.isError is True
        assert path.read_bytes() == original_bytes
        assert sidecar.read_bytes() == tombstone_audit

    def test_dispatch_constructs_explicit_trained_operator_policy(self, scratch_dir: Path) -> None:
        from elspeth.web.catalog.policy_view import PolicyCatalogView
        from elspeth.web.plugin_policy.models import PluginAvailabilitySnapshot

        catalog = _mock_catalog()
        real_snapshot_builder = PluginAvailabilitySnapshot.for_trained_operator
        real_view_builder = PolicyCatalogView.for_trained_operator
        with (
            patch.object(
                PluginAvailabilitySnapshot,
                "for_trained_operator",
                side_effect=real_snapshot_builder,
            ) as snapshot_builder,
            patch.object(PolicyCatalogView, "for_trained_operator", side_effect=real_view_builder) as view_builder,
        ):
            result = _dispatch_tool("list_sources", {}, _empty_state(), catalog, scratch_dir)

        assert result["success"] is True
        snapshot_builder.assert_called_once_with(catalog)
        view_builder.assert_called_once()

    """Tests for _dispatch_tool() dispatch logic."""

    @pytest.fixture()
    def scratch_dir(self, tmp_path: Path) -> Path:
        d = tmp_path / "scratch"
        d.mkdir()
        return d

    def test_list_sources_returns_success(self, scratch_dir: Path) -> None:
        result = _dispatch_tool(
            "list_sources",
            {},
            _empty_state(),
            _mock_catalog(),
            scratch_dir,
        )
        assert result["success"] is True

    def test_set_source_path_without_session_identity_fails_closed(self, scratch_dir: Path) -> None:
        # set_source is promoted to a type-driven manifest entry
        # (SetSourceArgumentsModel) with extra="forbid" — the LLM-supplied
        # argument set MUST include all four required fields.  Prior to
        # promotion this test omitted on_validation_failure and relied on
        # the handler defaulting it via .get(); that path is gone.
        result = _dispatch_tool(
            "set_source",
            {
                "plugin": "csv",
                "on_success": "node_1",
                "options": {"path": "/data/blobs/input.csv", "schema": {"mode": "observed"}},
                "on_validation_failure": "discard",
            },
            _empty_state(),
            _mock_catalog(),
            scratch_dir,
        )
        assert result["success"] is False
        assert "Path violation (S2)" in result["data"]["error"]
        assert "data_dir" in result["data"]["error"]

    def test_set_output_requires_explicit_collision_policy(self, scratch_dir: Path) -> None:
        result = _dispatch_tool(
            "set_output",
            {
                "sink_name": "main",
                "plugin": "csv",
                "options": {"path": "outputs/out.csv", "schema": {"mode": "observed"}},
                "on_write_failure": "discard",
            },
            _empty_state(),
            _mock_catalog(),
            scratch_dir,
        )

        assert result["success"] is False
        assert "collision_policy" in result["error"]
        assert result["state"] == _empty_state().to_dict()

    def test_set_output_accepts_explicit_collision_policy(self, scratch_dir: Path) -> None:
        result = _dispatch_tool(
            "set_output",
            {
                "sink_name": "main",
                "plugin": "csv",
                "options": {
                    "path": "outputs/out.csv",
                    "schema": {"mode": "observed"},
                    "mode": "write",
                    "collision_policy": "auto_increment",
                },
                "on_write_failure": "discard",
            },
            _empty_state(),
            _mock_catalog(),
            scratch_dir,
        )

        assert result["success"] is True
        assert result["state"]["outputs"][0]["options"]["collision_policy"] == "auto_increment"

    def test_patch_output_options_cannot_remove_collision_policy(self, scratch_dir: Path) -> None:
        state = CompositionState(
            source=None,
            nodes=(),
            edges=(),
            outputs=(
                OutputSpec(
                    name="main",
                    plugin="csv",
                    options={
                        "path": "outputs/out.csv",
                        "schema": {"mode": "observed"},
                        "mode": "write",
                        "collision_policy": "auto_increment",
                    },
                    on_write_failure="discard",
                ),
            ),
            metadata=PipelineMetadata(),
            version=1,
        )

        result = _dispatch_tool(
            "patch_output_options",
            {"sink_name": "main", "patch": {"collision_policy": None}},
            state,
            _mock_catalog(),
            scratch_dir,
        )

        assert result["success"] is False
        assert "collision_policy" in result["error"]
        assert result["state"] == state.to_dict()

    def test_set_pipeline_requires_explicit_output_collision_policy(self, scratch_dir: Path) -> None:
        result = _dispatch_tool(
            "set_pipeline",
            {
                "source": {
                    "plugin": "csv",
                    "on_success": "main",
                    "options": {"path": "/data/blobs/input.csv", "schema": {"mode": "observed"}},
                    "on_validation_failure": "discard",
                },
                "nodes": [],
                "edges": [],
                "outputs": [
                    {
                        "sink_name": "main",
                        "plugin": "json",
                        "options": {"path": "outputs/out.json", "schema": {"mode": "observed"}},
                        "on_write_failure": "discard",
                    }
                ],
            },
            _empty_state(),
            _mock_catalog(),
            scratch_dir,
        )

        assert result["success"] is False
        assert "Output 'main'" in result["error"]
        assert "collision_policy" in result["error"]

    def test_new_session_returns_session_id(self, scratch_dir: Path) -> None:
        session_manager, session_checkout_ref = _session_authority(scratch_dir)
        result = _dispatch_tool(
            "new_session",
            {},
            _empty_state(),
            _mock_catalog(),
            scratch_dir,
            session_manager=session_manager,
            session_checkout_ref=session_checkout_ref,
        )
        assert result["success"] is True
        assert "session_id" in result["data"]

    def test_save_and_load_round_trip(self, scratch_dir: Path) -> None:
        session_manager, session_checkout_ref = _session_authority(scratch_dir)
        # Create a session first
        new_result = _dispatch_tool(
            "new_session",
            {},
            _empty_state(),
            _mock_catalog(),
            scratch_dir,
            session_manager=session_manager,
            session_checkout_ref=session_checkout_ref,
        )
        session_id = new_result["data"]["session_id"]

        # Modify state with a path-free composer tool. Raw local source paths
        # are intentionally unavailable to this unscoped direct dispatcher.
        modified = _dispatch_tool(
            "set_metadata",
            {"patch": {"name": "Round Trip"}},
            _empty_state(),
            _mock_catalog(),
            scratch_dir,
        )
        modified_state = CompositionState.from_dict(modified["state"])

        # Save the modified state
        save_result = _dispatch_tool(
            "save_session",
            {"session_id": session_id},
            modified_state,
            _mock_catalog(),
            scratch_dir,
            session_manager=session_manager,
            session_checkout_ref=session_checkout_ref,
        )
        assert save_result["success"] is True

        # Load it back
        load_result = _dispatch_tool(
            "load_session",
            {"session_id": session_id},
            _empty_state(),
            _mock_catalog(),
            scratch_dir,
            session_manager=session_manager,
            session_checkout_ref=session_checkout_ref,
        )
        assert load_result["success"] is True
        assert load_result["state"]["metadata"]["name"] == "Round Trip"

    def test_delete_missing_session_before_scratch_exists_returns_not_found(self, tmp_path: Path) -> None:
        scratch_dir = tmp_path / "scratch"
        session_id = "0" * 12
        session_manager, session_checkout_ref = _session_authority(scratch_dir)

        result = _dispatch_tool(
            "delete_session",
            {"session_id": session_id},
            _empty_state(),
            _mock_catalog(),
            scratch_dir,
            session_manager=session_manager,
            session_checkout_ref=session_checkout_ref,
        )

        assert result["success"] is False
        assert result["error"] == f"Session not found: {session_id}"
        assert result["state"] == _empty_state().to_dict()

    def test_generate_yaml_returns_string_for_valid_state(self, scratch_dir: Path) -> None:
        result = _dispatch_session_once(
            "generate_yaml",
            {},
            _valid_state_with_no_edge_contracts(),
            scratch_dir,
        )
        assert result["success"] is True
        assert isinstance(result["data"], str)

    def test_local_trained_operator_generate_yaml_keeps_raw_profile_behavior_without_registry(
        self,
        scratch_dir: Path,
    ) -> None:
        state = CompositionState(
            source=SourceSpec(
                plugin="csv",
                on_success="llm_in",
                options={"path": "/data/in.csv", "schema": {"mode": "observed"}},
                on_validation_failure="discard",
            ),
            nodes=(
                NodeSpec(
                    id="summarize",
                    node_type="transform",
                    plugin="llm",
                    input="llm_in",
                    on_success="main",
                    on_error="discard",
                    options={
                        "profile": "operator-owned-alias",
                        "prompt_template": "Summarise {{ row }}",
                        "schema": {"mode": "observed"},
                    },
                    condition=None,
                    routes=None,
                    fork_to=None,
                    branches=None,
                    policy=None,
                    merge=None,
                ),
            ),
            edges=(),
            outputs=(
                OutputSpec(
                    name="main",
                    plugin="json",
                    options={
                        "path": "outputs/out.jsonl",
                        "schema": {"mode": "observed"},
                        "mode": "write",
                        "collision_policy": "auto_increment",
                    },
                    on_write_failure="discard",
                ),
            ),
            metadata=PipelineMetadata(),
            version=1,
        )

        result = _dispatch_session_once("generate_yaml", {}, state, scratch_dir)

        assert result["success"] is True
        assert yaml.safe_load(result["data"])["transforms"][0]["options"]["profile"] == "operator-owned-alias"

    def test_generate_yaml_strips_blob_bound_source_storage_path(self, scratch_dir: Path) -> None:
        storage_path = "/data/blobs/session/98b1357d_input.csv"
        state = CompositionState(
            source=SourceSpec(
                plugin="csv",
                on_success="main",
                options={
                    "path": storage_path,
                    "blob_ref": "98b1357d-5aab-4fb3-85b4-5ad643912e84",
                    "mode": "bind_source",
                    "schema": {"mode": "observed"},
                },
                on_validation_failure="quarantine",
            ),
            nodes=(),
            edges=(),
            outputs=(
                OutputSpec(
                    name="main",
                    plugin="csv",
                    options={
                        "path": "outputs/out.csv",
                        "schema": {"mode": "observed"},
                        "mode": "write",
                        "collision_policy": "auto_increment",
                    },
                    on_write_failure="discard",
                ),
            ),
            metadata=PipelineMetadata(),
            version=1,
        )

        result = _dispatch_session_once(
            "generate_yaml",
            {},
            state,
            scratch_dir,
        )

        assert result["success"] is True
        assert storage_path not in result["data"]
        options = yaml.safe_load(result["data"])["sources"]["source"]["options"]
        assert "path" not in options
        assert "blob_ref" not in options
        assert "mode" not in options
        assert options["schema"] == {"mode": "observed"}

    def test_generate_yaml_projects_adjacent_state_through_public_boundary(self, scratch_dir: Path) -> None:
        """The MCP result's adjacent state must be as public as its YAML data."""
        private_values = {
            "/private/blob-backed.csv",
            "/private/explicit-null.csv",
            "/private/path-only.csv",
            "/private/nested.csv",
            "/private/source-index",
            "/private/vector-index",
            "/private/node-input.csv",
            "/private/blob-output.csv",
            "/private/null-output.csv",
            "/private/output.csv",
            "98b1357d-5aab-4fb3-85b4-5ad643912e84",
            "20b944e3-fd46-434f-b9a2-4fb508db30f0",
            "30b944e3-fd46-434f-b9a2-4fb508db30f0",
        }
        state = CompositionState(
            sources={
                "blob_backed": SourceSpec(
                    plugin="csv",
                    on_success="blob_out",
                    options={
                        "path": "/private/blob-backed.csv",
                        "blob_ref": "98b1357d-5aab-4fb3-85b4-5ad643912e84",
                        "mode": "bind_source",
                        "schema": {"mode": "observed"},
                    },
                    on_validation_failure="discard",
                ),
                "explicit_null": SourceSpec(
                    plugin="csv",
                    on_success="null_out",
                    options={
                        "path": "/private/explicit-null.csv",
                        "blob_ref": None,
                        "schema": {"mode": "observed"},
                    },
                    on_validation_failure="discard",
                ),
                "path_only": SourceSpec(
                    plugin="csv",
                    on_success="pass_in",
                    options={
                        "file": "/private/path-only.csv",
                        SOURCE_AUTHORING_KEY: {
                            "path": "/private/nested.csv",
                            "blob_ref": "20b944e3-fd46-434f-b9a2-4fb508db30f0",
                        },
                        "custody": {
                            "persist_directory": "/private/source-index",
                            "blob_id": "30b944e3-fd46-434f-b9a2-4fb508db30f0",
                        },
                        "schema": {"mode": "observed"},
                    },
                    on_validation_failure="discard",
                ),
            },
            nodes=(
                NodeSpec(
                    id="pass",
                    node_type="transform",
                    plugin="llm",
                    input="pass_in",
                    on_success="main",
                    on_error="discard",
                    options={
                        "profile": "operator-owned-alias",
                        "prompt_template": "{{ lookup.path }} {{ lookup.file }} {{ lookup.mode }}",
                        "lookup": {
                            "path": "north",
                            "file": "case.txt",
                            "mode": "bind_source",
                            "safe": "kept",
                        },
                        "provider_config": {
                            "persist_directory": "/private/vector-index",
                            "nested": {"path": "/private/node-input.csv"},
                        },
                    },
                    condition=None,
                    routes=None,
                    fork_to=None,
                    branches=None,
                    policy=None,
                    merge=None,
                ),
            ),
            edges=(),
            outputs=(
                OutputSpec(
                    name="blob_out",
                    plugin="csv",
                    options={
                        "path": "/private/blob-output.csv",
                        "mode": "write",
                        "collision_policy": "auto_increment",
                    },
                    on_write_failure="discard",
                ),
                OutputSpec(
                    name="null_out",
                    plugin="csv",
                    options={
                        "path": "/private/null-output.csv",
                        "mode": "write",
                        "collision_policy": "auto_increment",
                    },
                    on_write_failure="discard",
                ),
                OutputSpec(
                    name="main",
                    plugin="csv",
                    options={
                        "path": "/private/output.csv",
                        "schema": {"mode": "observed"},
                        "mode": "write",
                        "collision_policy": "auto_increment",
                    },
                    on_write_failure="discard",
                ),
            ),
            metadata=PipelineMetadata(),
            version=1,
        )

        result = _dispatch_session_once("generate_yaml", {}, state, scratch_dir)
        serialized = yaml.safe_dump(result)

        assert result["success"] is True, result
        assert all(value not in serialized for value in private_values)
        assert "blob_ref" not in serialized
        assert "blob_id" not in serialized
        assert SOURCE_AUTHORING_KEY not in serialized
        assert set(result["state"]["sources"]) == {"blob_backed", "explicit_null", "path_only"}
        assert all(source["plugin"] == "csv" for source in result["state"]["sources"].values())
        expected_lookup = {
            "path": "north",
            "file": "case.txt",
            "mode": "bind_source",
            "safe": "kept",
        }
        assert result["state"]["nodes"][0]["options"]["lookup"] == expected_lookup
        assert yaml.safe_load(result["data"])["transforms"][0]["options"]["lookup"] == expected_lookup

    def test_generate_yaml_rejects_state_missing_file_sink_collision_policy(self, scratch_dir: Path) -> None:
        state = CompositionState(
            source=SourceSpec(
                plugin="csv",
                on_success="main",
                options={"path": "/data/in.csv", "schema": {"mode": "observed"}},
                on_validation_failure="discard",
            ),
            nodes=(),
            edges=(),
            outputs=(
                OutputSpec(
                    name="main",
                    plugin="json",
                    options={"path": "outputs/out.json", "schema": {"mode": "observed"}},
                    on_write_failure="discard",
                ),
            ),
            metadata=PipelineMetadata(),
            version=1,
        )

        result = _dispatch_session_once(
            "generate_yaml",
            {},
            state,
            scratch_dir,
        )

        assert result["success"] is False
        assert "collision_policy" in result["error"]

    def test_generate_yaml_rejects_invalid_contract_state(self, scratch_dir: Path) -> None:
        result = _dispatch_session_once(
            "generate_yaml",
            {},
            _invalid_contract_state(),
            scratch_dir,
        )

        assert result["success"] is False
        assert "invalid" in result["error"].lower()
        assert result["validation"]["is_valid"] is False
        assert len(result["validation"]["errors"]) >= 1
        assert result["validation"]["edge_contracts"] == [
            {
                "from": "source",
                "to": "t1",
                "producer_guarantees": [],
                "consumer_requires": ["text"],
                "missing_fields": ["text"],
                "satisfied": False,
            }
        ]

    def test_generate_yaml_allows_valid_state_with_no_edge_contracts(self, scratch_dir: Path) -> None:
        result = _dispatch_session_once(
            "generate_yaml",
            {},
            _valid_state_with_no_edge_contracts(),
            scratch_dir,
        )

        assert result["success"] is True
        assert isinstance(result["data"], str)

    def test_generate_yaml_allows_connection_valid_state_without_ui_edges(self, scratch_dir: Path) -> None:
        result = _dispatch_session_once(
            "generate_yaml",
            {},
            _connection_valid_field_mapper_state_without_edges(),
            scratch_dir,
        )

        assert result["success"] is True
        assert "field_mapper" in result["data"]
        assert "body" in result["data"]

    def test_unknown_tool_returns_failure(self, scratch_dir: Path) -> None:
        result = _dispatch_tool(
            "nonexistent_tool",
            {},
            _empty_state(),
            _mock_catalog(),
            scratch_dir,
        )
        assert result["success"] is False


class TestValidationToDictSemanticContracts:
    """Validation payload must surface semantic_contracts for MCP clients.

    Without this, only the legacy edge_contracts field reaches MCP and
    the new plugin-declared semantic layer is invisible to agent
    consumers — which makes the /validate response asymmetric across
    HTTP and MCP surfaces.
    """

    def test_semantic_contracts_in_payload(self) -> None:
        from elspeth.composer_mcp.server import _validation_to_dict
        from tests.unit.web.composer.test_semantic_validator import _wardline_state

        state = _wardline_state(text_separator=" ")
        validation = state.validate()
        payload = _validation_to_dict(validation)

        assert "semantic_contracts" in payload
        assert isinstance(payload["semantic_contracts"], list)
        assert len(payload["semantic_contracts"]) == 1
        contract = payload["semantic_contracts"][0]
        assert contract["from_id"] == "scrape"
        assert contract["to_id"] == "explode"
        assert contract["producer_field"] == "content"
        assert contract["consumer_field"] == "content"
        assert contract["outcome"] == "conflict"
        assert contract["consumer_plugin"] == "line_explode"
        assert contract["producer_plugin"] == "web_scrape"
        assert contract["requirement_code"] == "line_explode.source_field.line_framed_text"

    def test_empty_semantic_contracts_emits_empty_list_not_absent(self) -> None:
        """Surface parity: pre-semantic short-circuits emit [], not absent.

        /validate's pre-semantic short-circuit returns serialize the
        Pydantic default of [] rather than omitting the field; MCP must
        match that surface so clients can treat 'absent' as a clear
        signal of an older server version, not as 'maybe satisfied'.
        """
        from elspeth.composer_mcp.server import _validation_to_dict

        state = _empty_state()
        validation = state.validate()
        payload = _validation_to_dict(validation)

        assert "semantic_contracts" in payload
        assert payload["semantic_contracts"] == []


@pytest.mark.asyncio
async def test_mcp_preview_runtime_preflight_joins_shared_session_inflight() -> None:
    from elspeth.composer_mcp.server import _mcp_preview_runtime_preflight
    from elspeth.web.execution.runtime_preflight import RuntimePreflightCoordinator
    from elspeth.web.execution.schemas import ValidationReadiness, ValidationResult

    coordinator = RuntimePreflightCoordinator()
    state = _valid_state_with_no_edge_contracts()
    calls = 0
    started = asyncio.Event()
    release = asyncio.Event()
    expected = ValidationResult(
        is_valid=True,
        checks=[],
        errors=[],
        readiness=ValidationReadiness(authoring_valid=True, execution_ready=True, completion_ready=True, blockers=[]),
    )

    async def run_preflight(candidate: CompositionState) -> ValidationResult:
        nonlocal calls
        assert candidate is state
        calls += 1
        started.set()
        await release.wait()
        return expected

    first_task = asyncio.create_task(
        _mcp_preview_runtime_preflight(
            state,
            coordinator=coordinator,
            session_scope="session:shared-web-session",
            settings_hash="settings-hash",
            timeout_seconds=1.0,
            run_preflight=run_preflight,
        )
    )
    await started.wait()
    second_task = asyncio.create_task(
        _mcp_preview_runtime_preflight(
            state,
            coordinator=coordinator,
            session_scope="session:shared-web-session",
            settings_hash="settings-hash",
            timeout_seconds=1.0,
            run_preflight=run_preflight,
        )
    )

    await asyncio.sleep(0)
    release.set()
    first, second = await asyncio.gather(first_task, second_task)

    assert first is expected
    assert second is expected
    assert calls == 1


# ---------------------------------------------------------------------------
# Queue fan-in over the composer MCP surface (elspeth-a5b86149d4 /
# elspeth-6421ffa028). tools/list must advertise queue in the closed
# upsert_node node_type enum, and the generic call_tool dispatch must
# persist and retrieve a queue node with no storage migration.
# ---------------------------------------------------------------------------

_QUEUE_UPSERT_ARGS: dict[str, object] = {
    "id": "inbound",
    "node_type": "queue",
    "plugin": None,
    "input": "inbound",
    "on_success": None,
    "on_error": None,
    "options": {"description": "Orders and refunds interleave here"},
}


class TestQueueExposure:
    @pytest.fixture()
    def scratch_dir(self, tmp_path: Path) -> Path:
        d = tmp_path / "scratch"
        d.mkdir()
        return d

    def test_tools_list_advertises_queue_node_type(self) -> None:
        upsert = next(t for t in _build_tool_defs() if t["name"] == "upsert_node")
        enum = upsert["parameters"]["properties"]["node_type"]["enum"]
        assert "queue" in enum

    def test_call_tool_persists_and_retrieves_queue_node(self, scratch_dir: Path) -> None:
        result = _dispatch_tool(
            "upsert_node",
            dict(_QUEUE_UPSERT_ARGS),
            _empty_state(),
            _mock_catalog(),
            scratch_dir,
        )
        assert result["success"] is True, result.get("error")
        nodes = result["state"]["nodes"]
        queue = next(n for n in nodes if n["id"] == "inbound")
        assert queue["node_type"] == "queue"
        assert queue["input"] == "inbound"
        assert queue["plugin"] is None
        assert queue["options"] == {"description": "Orders and refunds interleave here"}

    def test_call_tool_rejects_malformed_queue_atomically(self, scratch_dir: Path) -> None:
        state = _empty_state()
        result = _dispatch_tool(
            "upsert_node",
            {**_QUEUE_UPSERT_ARGS, "input": "elsewhere"},
            state,
            _mock_catalog(),
            scratch_dir,
        )
        assert result["success"] is False
        assert result["state"] == state.to_dict()
