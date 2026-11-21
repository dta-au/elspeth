"""Run admission refuses a file reference held where a local path belongs.

A composition state can hold a file reference (an ``inline_content`` blob
marker) at a sink ``path``, a source ``path`` or a ``persist_directory``. Run
admission confines those options to the session's directories before it
starts a run; a value that is not text must be refused there with the same
typed 422 the route returns for any other invalid pipeline, never reach
``Path(...)`` and surface as a 500.

Driven through the real ``/execute`` route with the in-process ASGI client
and a real app, session service and execution service.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from tests.fixtures.identities import wire_test_pipeline_user_authority
from tests.integration.web.conftest import _save_composition_state_with_compose_authority

INPUT_CSV = b"id,name,value\n1,ada,42\n2,grace,7\n"
FILE_TEXT = b"a file the composer could reference\n"

type StateEdit = Callable[[dict[str, Any], dict[str, str], str], None]


def _settings(work_dir: Path) -> Any:
    from elspeth.web.config import WebSettings

    for name in ("runs", "outputs", "blobs"):
        (work_dir / name).mkdir(exist_ok=True)
    payloads = work_dir / "payloads"
    payloads.mkdir(mode=0o700, exist_ok=True)
    return WebSettings(
        data_dir=work_dir,
        landscape_url=f"sqlite:///{work_dir}/runs/audit.db",
        payload_store_path=payloads,
        composer_max_composition_turns=15,
        composer_context_max_prompt_tokens=200_000,
        composer_max_discovery_turns=10,
        composer_timeout_seconds=85.0,
        composer_boot_probe_enabled=False,
        composer_rate_limit_per_minute=10,
        shareable_link_signing_key=b"\x00" * 32,
    )


def _base_state(*, csv_path: str, session_id: str) -> dict[str, Any]:
    from elspeth.web.composer.state import CompositionState, OutputSpec, PipelineMetadata, SourceSpec

    fields = ["id: int", "name: str", "value: int"]
    return CompositionState(
        source=SourceSpec(
            plugin="csv",
            on_success="primary",
            options={"path": csv_path, "schema": {"mode": "fixed", "fields": fields}},
            on_validation_failure="discard",
        ),
        nodes=(),
        edges=(),
        outputs=(
            OutputSpec(
                name="primary",
                plugin="text",
                options={"path": f"outputs/{session_id}/names.txt", "field": "name", "schema": {"mode": "fixed", "fields": fields}},
                on_write_failure="discard",
            ),
        ),
        metadata=PipelineMetadata(name="Path admission", description="One csv source to one text sink."),
        version=1,
    ).to_dict()


def _row_counts(app: Any) -> dict[str, int]:
    from elspeth.web.sessions.models import metadata as sessions_metadata

    with app.state.session_engine.connect() as conn:
        return {table.name: conn.execute(select(func.count()).select_from(table)).scalar_one() for table in sessions_metadata.sorted_tables}


def _output_files(work_dir: Path) -> list[str]:
    """Files a sink wrote under the outputs folder (a sink's own ``.lock`` file excluded)."""
    return sorted(
        str(path.relative_to(work_dir))
        for path in (work_dir / "outputs").rglob("*")
        if path.is_file() and not path.name.endswith(".elspeth.lock")
    )


async def _drive(work_dir: Path, edit: StateEdit | None) -> tuple[int, Any, dict[str, int], dict[str, int], list[str]]:
    """Save a state, POST /execute, and return status, body, row counts before/after and output files."""
    from asgi_lifespan import LifespanManager

    from elspeth.web.app import create_app
    from elspeth.web.sessions.protocol import CompositionStateData

    app = create_app(settings=_settings(work_dir))
    app.state.auth_provider.create_user("alice", "alicepass123", display_name="Alice")
    wire_test_pipeline_user_authority(app, identity_id="alice", engine=app.state.session_engine)

    async with (
        LifespanManager(app),
        AsyncClient(transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test") as client,
    ):
        login = await client.post("/api/auth/login", json={"username": "alice", "password": "alicepass123"})
        assert login.status_code == 200, login.text
        headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
        created = await client.post("/api/sessions", headers=headers, json={"title": "path admission"})
        assert created.status_code == 201, created.text
        session_id = created.json()["id"]

        uploaded_csv = await client.post(
            f"/api/sessions/{session_id}/blobs", headers=headers, files={"file": ("input.csv", INPUT_CSV, "text/csv")}
        )
        assert uploaded_csv.status_code == 201, uploaded_csv.text
        uploaded_file = await client.post(
            f"/api/sessions/{session_id}/blobs", headers=headers, files={"file": ("notes.txt", FILE_TEXT, "text/plain")}
        )
        assert uploaded_file.status_code == 201, uploaded_file.text
        marker = {"blob_ref": uploaded_file.json()["id"], "mode": "inline_content", "sha256": hashlib.sha256(FILE_TEXT).hexdigest()}

        from elspeth.web.sessions.models import blobs_table

        with app.state.session_engine.connect() as conn:
            csv_path = conn.execute(select(blobs_table.c.storage_path).where(blobs_table.c.id == uploaded_csv.json()["id"])).scalar_one()

        state = _base_state(csv_path=csv_path, session_id=session_id)
        if edit is not None:
            edit(state, marker, session_id)
        await _save_composition_state_with_compose_authority(
            app.state.session_service,
            UUID(session_id),
            CompositionStateData(
                sources=state["sources"],
                nodes=state["nodes"],
                edges=state["edges"],
                outputs=state["outputs"],
                metadata_=state["metadata"],
                is_valid=True,
                validation_errors=None,
            ),
            provenance="session_seed",
        )

        before = _row_counts(app)
        response = await client.post(f"/api/sessions/{session_id}/execute", headers=headers)
        body: Any = response.json() if response.headers.get("content-type", "").startswith("application/json") else response.text
        if response.status_code == 202:
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                run = await client.get(f"/api/runs/{body['run_id']}", headers=headers)
                assert run.status_code == 200, run.text
                if run.json()["status"] in ("completed", "failed", "cancelled"):
                    break
                await asyncio.sleep(0.2)
        after = _row_counts(app)
    return response.status_code, body, before, after, _output_files(work_dir)


def _sink(plugin: str, key: str, extra: dict[str, Any]) -> StateEdit:
    def edit(state: dict[str, Any], marker: dict[str, str], session_id: str) -> None:
        output = state["outputs"][0]
        output["plugin"] = plugin
        output["options"] = {**{k: v for k, v in output["options"].items() if k != "path"}, **extra, key: marker}

    return edit


def _source_path(state: dict[str, Any], marker: dict[str, str], session_id: str) -> None:
    state["sources"]["source"]["options"]["path"] = marker


def _node_persist_directory(state: dict[str, Any], marker: dict[str, str], session_id: str) -> None:
    state["nodes"] = [
        {
            "id": "lookup",
            "node_type": "transform",
            "plugin": "rag_retrieval",
            "input": "primary",
            "on_success": "looked_up",
            "on_error": "discard",
            "options": {
                "provider": "chroma",
                "provider_config": {"collection": "notes", "mode": "persistent", "persist_directory": marker},
            },
            "condition": None,
            "routes": None,
            "fork_to": None,
            "branches": None,
            "policy": None,
            "merge": None,
        }
    ]
    state["outputs"][0]["options"]["path"] = f"outputs/{session_id}/names.txt"


POSITIONS: dict[str, tuple[StateEdit, str, str, str]] = {
    # name: (edit, component_type, component_id, option)
    "text_sink_path": (_sink("text", "path", {"field": "name"}), "sink", "primary", "path"),
    "document_sink_path": (_sink("document", "path", {"field": "name", "mode": "write"}), "sink", "primary", "path"),
    "csv_sink_path": (_sink("csv", "path", {}), "sink", "primary", "path"),
    "json_sink_path": (_sink("json", "path", {}), "sink", "primary", "path"),
    "chroma_sink_persist_directory": (
        _sink(
            "chroma_sink",
            "persist_directory",
            {"collection": "notes", "mode": "persistent", "field_mapping": {"document_field": "name", "id_field": "id"}},
        ),
        "sink",
        "primary",
        "persist_directory",
    ),
    "csv_source_path": (_source_path, "source", "source", "path"),
    "transform_provider_config_persist_directory": (_node_persist_directory, "transform", "lookup", "persist_directory"),
}


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("position", sorted(POSITIONS))
async def test_a_file_reference_at_a_local_path_option_is_refused_at_run_admission(tmp_path: Path, position: str) -> None:
    edit, component_type, component_id, option = POSITIONS[position]

    status, body, before, after, outputs = await _drive(tmp_path, edit)

    assert status == 422, body
    assert body["detail"]["error_type"] == "pipeline_validation_failure"
    assert body["detail"]["kind"] == "pipeline_validation_failure"
    [error] = body["detail"]["errors"]
    assert (error["component_type"], error["component_id"], error["error_code"]) == (component_type, component_id, None)
    assert option in error["message"]
    assert "must be text, not a file reference" in error["message"]
    rendered = json.dumps(body)
    assert "mappingproxy" not in rendered
    assert "blob_ref" not in rendered
    assert "inline_content" not in rendered
    assert hashlib.sha256(FILE_TEXT).hexdigest() not in rendered
    assert after == before
    assert outputs == []


@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_same_graph_with_a_text_sink_path_still_starts(tmp_path: Path) -> None:
    status, body, before, after, outputs = await _drive(tmp_path, None)

    assert status == 202, body
    assert after["runs"] == before["runs"] + 1
    assert len(outputs) == 1
    assert outputs[0].endswith("names.txt")
