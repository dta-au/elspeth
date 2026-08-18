"""Tests for the applied-component echo on a successful mutation.

After a successful mutation the model knew THAT it worked and which errors
remained, but not what the server had stored where it transformed the input —
so ``get_pipeline_state`` was the only way to see a canonicalized route or a
reconciled sink-mirror edge, and that read is one of the four redundant
state-list calls the tutorial harness gates against (elspeth-f14aba9686).
``_mutation_result`` now carries the post-finalizer projection of the
components the mutation touched.

The echo is the exact ``set_pipeline`` arguments
``get_pipeline_state(component="set_pipeline_arguments")`` already serves to
this same surface, narrowed to what changed — a turn saved, not a new
disclosure surface.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from elspeth.web.catalog.policy_view import PolicyCatalogView
from elspeth.web.composer.state import CompositionState, PipelineMetadata
from elspeth.web.composer.tools import ToolResult, execute_tool
from elspeth.web.composer.tools import _common as tools_common
from elspeth.web.dependencies import create_catalog_service
from elspeth.web.plugin_policy.models import PluginAvailabilitySnapshot
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.models import chat_messages_table, sessions_table
from elspeth.web.sessions.schema import initialize_session_schema


def _empty_state() -> CompositionState:
    return CompositionState(
        source=None,
        nodes=(),
        edges=(),
        outputs=(),
        metadata=PipelineMetadata(),
        version=1,
    )


def _run(tool_name: str, arguments: dict[str, Any], state: CompositionState) -> ToolResult:
    """Dispatch one tool against the real plugin contracts."""
    catalog = create_catalog_service()
    snapshot = PluginAvailabilitySnapshot.for_trained_operator(catalog)
    return execute_tool(
        tool_name,
        arguments,
        state,
        PolicyCatalogView.for_trained_operator(catalog, snapshot),
        plugin_snapshot=snapshot,
    )


def _csv_source_arguments(path: Path, *, on_validation_failure: str = "discard") -> dict[str, Any]:
    return {
        "plugin": "csv",
        "on_success": "rows",
        "options": {"path": str(path), "schema": {"mode": "observed"}},
        "on_validation_failure": on_validation_failure,
    }


def _json_output_arguments(sink_name: str, path: Path) -> dict[str, Any]:
    return {
        "sink_name": sink_name,
        "plugin": "json",
        "options": {"path": str(path), "schema": {"mode": "observed"}},
    }


def _state_with_source(tmp_path: Path) -> CompositionState:
    result = _run("set_source", _csv_source_arguments(tmp_path / "in.csv"), _empty_state())
    assert result.success is True
    return result.updated_state


def test_successful_source_mutation_echoes_the_applied_component(tmp_path: Path) -> None:
    """A successful mutation reports what the server stored, not just that it stored it."""
    result = _run("set_source", _csv_source_arguments(tmp_path / "in.csv"), _empty_state())

    assert result.success is True
    echo = result.to_dict()["applied_component"]
    assert set(echo) == {"source"}
    assert echo["source"]["plugin"] == "csv"
    assert echo["source"]["on_success"] == "rows"
    # Scoped to what the mutation touched: no nodes/outputs/metadata ride along.
    assert "nodes" not in echo
    assert "metadata" not in echo


def test_echo_reports_the_post_finalizer_route_the_model_did_not_write(tmp_path: Path) -> None:
    """The echo shows a server-canonicalized field, not the authored bytes.

    ``canonicalize_source_validation_failure`` folds an unspecified route
    ("" — a sink name can never be empty, so it carries no routing intent)
    into the 'discard' default. That fold is exactly the class of server
    transform the model cannot see in a bare success envelope: it authored
    "", and what persisted is "discard".
    """
    result = _run("set_source", _csv_source_arguments(tmp_path / "in.csv", on_validation_failure=""), _empty_state())

    assert result.success is True
    assert result.updated_state.sources["source"].on_validation_failure == "discard"
    assert result.to_dict()["applied_component"]["source"]["on_validation_failure"] == "discard"


def test_echo_carries_the_server_retargeted_mirror_edge(tmp_path: Path) -> None:
    """The edge the tool moved server-side rides with its component.

    Scalar routes are the runtime authority and sink-targeting edges are their
    mirror, which every mutation reconciles itself (elspeth-67b44040ee): moving
    a node's ``on_success`` moves its drawn edge. That retarget is written by
    the server, appears nowhere in the tool arguments, and is invisible in a
    bare success envelope — so the echo carries the edges on the applied
    component's endpoints, not just the component.
    """
    state = _state_with_source(tmp_path)
    for sink_name in ("rows_out", "rows_alt"):
        state = _run("set_output", _json_output_arguments(sink_name, tmp_path / f"{sink_name}.json"), state).updated_state
    node = {
        "id": "clean",
        "node_type": "transform",
        "plugin": "field_mapper",
        "input": "rows",
        "on_success": "rows_out",
        "options": {"mapping": {"a": "b"}, "schema": {"mode": "observed"}},
    }
    state = _run("upsert_node", node, state).updated_state
    drawn = _run("upsert_edge", {"id": "e1", "from_node": "clean", "to_node": "rows_out", "edge_type": "on_success"}, state)
    assert drawn.success is True

    result = _run("upsert_node", {**node, "on_success": "rows_alt"}, drawn.updated_state)

    assert result.success is True
    echo = result.to_dict()["applied_component"]
    assert [node_payload["id"] for node_payload in echo["nodes"]] == ["clean"]
    # The model asked for on_success='rows_alt' and never mentioned edge e1.
    assert echo["edges"] == [
        {"id": "e1", "from_node": "clean", "to_node": "rows_alt", "edge_type": "on_success", "label": None},
    ]
    # Merged defaults are visible for the same reason: nothing authored on_error.
    assert echo["nodes"][0]["on_error"] == "discard"


def test_failed_mutation_carries_no_applied_component_echo(tmp_path: Path) -> None:
    """A rejection changed nothing, so there is no applied component to report."""
    result = _run(
        "set_source",
        {"plugin": "csv", "on_success": "rows", "options": {"bogus_option": True}, "on_validation_failure": "discard"},
        _empty_state(),
    )

    assert result.success is False
    assert result.applied_component is None
    assert "applied_component" not in result.to_dict()


def test_discovery_tool_carries_no_applied_component_echo(tmp_path: Path) -> None:
    """Discovery applies nothing; the read tools are untouched."""
    result = _run("get_pipeline_state", {}, _state_with_source(tmp_path))

    assert result.success is True
    assert "applied_component" not in result.to_dict()


def test_full_replacement_withholds_the_whole_document_echo(tmp_path: Path) -> None:
    """``set_pipeline`` echoes nothing: every component is 'the applied component'.

    The controller ruling scopes the echo to the applied COMPONENT, not whole
    state. On a full replacement those are the same thing, so the echo would be
    exactly the whole-document read this exists to avoid — and the model
    already holds those bytes verbatim in the call it just made.
    """
    result = _run(
        "set_pipeline",
        {
            "source": _csv_source_arguments(tmp_path / "in.csv"),
            "nodes": [],
            "edges": [],
            "outputs": [_json_output_arguments("rows", tmp_path / "o.json")],
            "metadata": {"name": "p"},
        },
        _empty_state(),
    )

    assert result.success is True
    assert result.applied_component is None
    assert "applied_component" not in result.to_dict()


def test_oversized_projection_omits_the_echo_whole(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Past the ceiling the echo is dropped entire, never truncated.

    Half a component reads as a complete one and would author a wrong repair,
    so there is no partial form. The same mutation under the shipped ceiling
    DOES carry the echo, so the omission is attributable to the bound rather
    than to the projection never having been built.
    """
    arguments = _csv_source_arguments(tmp_path / "in.csv")

    assert "applied_component" in _run("set_source", arguments, _empty_state()).to_dict()

    monkeypatch.setattr(tools_common, "_APPLIED_COMPONENT_ECHO_MAX_CANONICAL_BYTES", 8)
    result = _run("set_source", arguments, _empty_state())

    assert result.success is True
    assert "applied_component" not in result.to_dict()


@pytest.fixture
def blob_env(tmp_path: Path) -> dict[str, Any]:
    """Minimal session + blob storage for the inline-ref tool."""
    engine = create_session_engine("sqlite:///:memory:")
    initialize_session_schema(engine)
    session_id = "session-echo-blob"
    now = datetime.now(UTC)
    with engine.begin() as conn:
        conn.execute(
            sessions_table.insert().values(
                id=session_id,
                user_id="test-user",
                auth_provider_type="local",
                title="Echo Blob Test",
                created_at=now,
                updated_at=now,
            )
        )
        conn.execute(
            chat_messages_table.insert().values(
                id="user-message-1",
                session_id=session_id,
                role="user",
                content="Use this exact content.",
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
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "blobs").mkdir()
    return {"engine": engine, "session_id": session_id, "data_dir": str(data_dir)}


def _collision_state(tmp_path: Path) -> CompositionState:
    """A named source and a node that share the name 'clean'.

    Both spellings are legal — sources and nodes live in separate namespaces —
    so the affected-component vocabulary must keep them apart: a source is
    reported as ``source:<name>``, a node as its bare id.
    """
    state = _run(
        "set_source",
        {**_csv_source_arguments(tmp_path / "in.csv"), "source_name": "clean"},
        _empty_state(),
    ).updated_state
    state = _run("set_output", _json_output_arguments("rows_out", tmp_path / "o.json"), state).updated_state
    return _run(
        "upsert_node",
        {
            "id": "clean",
            "node_type": "transform",
            "plugin": "field_mapper",
            "input": "rows",
            "on_success": "rows_out",
            "options": {"mapping": {"a": "b"}, "schema": {"mode": "observed"}},
        },
        state,
    ).updated_state


def test_inline_ref_on_a_named_source_echoes_the_source_not_a_same_named_node(
    tmp_path: Path,
    blob_env: dict[str, Any],
) -> None:
    """``wire_blob_inline_ref`` reports the source it wired, in the shared spelling.

    Its affected-component helper stripped the ``source:`` prefix that every
    other source-mutating site keeps, so the echo resolved the bare name — which
    is a NODE id in the affected vocabulary. With a same-named node present the
    tool echoed that node (a component the call never touched); with no
    collision it silently echoed nothing. The collision is the discriminating
    fixture: only the prefixed spelling can tell the two apart.
    """
    catalog = create_catalog_service()
    snapshot = PluginAvailabilitySnapshot.for_trained_operator(catalog)
    content = "a,b\n1,2\n"
    blob = execute_tool(
        "create_blob",
        {"filename": "rows.csv", "mime_type": "text/csv", "content": content},
        _empty_state(),
        PolicyCatalogView.for_trained_operator(catalog, snapshot),
        plugin_snapshot=snapshot,
        data_dir=blob_env["data_dir"],
        session_engine=blob_env["engine"],
        session_id=blob_env["session_id"],
        user_message_id="user-message-1",
        user_message_content=f"Use this exact content:\n{content}",
    )
    assert blob.success is True

    result = execute_tool(
        "wire_blob_inline_ref",
        {"field_path": "source:clean.options.path", "blob_id": blob.data["blob_id"]},
        _collision_state(tmp_path),
        PolicyCatalogView.for_trained_operator(catalog, snapshot),
        plugin_snapshot=snapshot,
        data_dir=blob_env["data_dir"],
        session_engine=blob_env["engine"],
        session_id=blob_env["session_id"],
    )

    assert result.success is True
    assert result.affected_nodes == ("source:clean",)
    echo = result.to_dict()["applied_component"]
    assert set(echo) == {"sources"}
    assert set(echo["sources"]) == {"clean"}
    assert "nodes" not in echo
