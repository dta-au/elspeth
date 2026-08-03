"""End-to-end admission proof for observed CSV numeric gate predicates."""

from __future__ import annotations

from pathlib import Path
from uuid import UUID

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient

from elspeth.web.app import create_app
from elspeth.web.composer.state import (
    CompositionState,
    NodeSpec,
    OutputSpec,
    PipelineMetadata,
    SourceSpec,
)
from elspeth.web.config import WebSettings
from elspeth.web.sessions.protocol import CompositionStateData


def _settings(tmp_path: Path) -> WebSettings:
    (tmp_path / "runs").mkdir()
    (tmp_path / "payloads").mkdir(mode=0o700)
    return WebSettings(
        data_dir=tmp_path,
        landscape_url=f"sqlite:///{tmp_path}/runs/audit.db",
        payload_store_path=tmp_path / "payloads",
        composer_max_composition_turns=15,
        composer_max_discovery_turns=10,
        composer_timeout_seconds=85.0,
        composer_rate_limit_per_minute=10,
        secret_key="x" * 32,
        shareable_link_signing_key=b"\x00" * 32,
        plugin_allowlist=("transform:passthrough",),
    )


def _state(
    *,
    blob_id: UUID,
    source_path: str,
    output_dir: Path,
    topology: str,
) -> CompositionState:
    source_output = "inbound" if topology == "queue" else "rows"
    gate_input = "preserved_rows" if topology == "passthrough" else source_output
    nodes: tuple[NodeSpec, ...] = ()
    if topology == "passthrough":
        nodes = (
            NodeSpec(
                id="retain_fields",
                node_type="transform",
                plugin="passthrough",
                input="rows",
                on_success="preserved_rows",
                on_error="discard",
                options={"schema": {"mode": "observed"}},
                condition=None,
                routes=None,
                fork_to=None,
                branches=None,
                policy=None,
                merge=None,
            ),
        )
    elif topology == "queue":
        nodes = (
            NodeSpec(
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
            ),
        )
    nodes += (
        NodeSpec(
            id="amount_gate",
            node_type="gate",
            plugin=None,
            input=gate_input,
            on_success=None,
            on_error=None,
            options={},
            condition="row['amount'] > 500",
            routes={"true": "high_value", "false": "standard"},
            fork_to=None,
            branches=None,
            policy=None,
            merge=None,
        ),
    )
    return CompositionState(
        sources={
            "source": SourceSpec(
                plugin="csv",
                on_success=source_output,
                options={
                    "path": source_path,
                    "blob_ref": str(blob_id),
                    "schema": {"mode": "observed"},
                },
                on_validation_failure="discard",
            )
        },
        nodes=nodes,
        edges=(),
        outputs=(
            OutputSpec(
                name="high_value",
                plugin="csv",
                options={
                    "path": str(output_dir / "high.csv"),
                    "schema": {"mode": "observed"},
                },
                on_write_failure="discard",
            ),
            OutputSpec(
                name="standard",
                plugin="csv",
                options={
                    "path": str(output_dir / "standard.csv"),
                    "schema": {"mode": "observed"},
                },
                on_write_failure="discard",
            ),
        ),
        metadata=PipelineMetadata(name="Observed CSV gate admission proof"),
        version=1,
    )


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("topology", ["direct", "passthrough", "queue"])
async def test_validate_and_execute_block_observed_numeric_gate_before_run_creation(
    tmp_path: Path,
    topology: str,
) -> None:
    """Real validate and execute surfaces must agree and create no run."""
    app = create_app(settings=_settings(tmp_path))
    app.state.auth_provider.create_user("proof-user", "proof-pass-123", display_name="Proof User")

    async with LifespanManager(app), AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        login = await client.post(
            "/api/auth/login",
            json={"username": "proof-user", "password": "proof-pass-123"},
        )
        assert login.status_code == 200, login.text
        headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
        created = await client.post(
            "/api/sessions",
            headers=headers,
            json={"title": "Observed gate proof"},
        )
        assert created.status_code == 201, created.text
        session_id = UUID(created.json()["id"])
        blob = await app.state.blob_service.create_blob(
            session_id,
            "amounts.csv",
            b"amount\n250.00\n750.00\n",
            "text/csv",
            created_by="user",
        )
        output_dir = tmp_path / "outputs" / str(session_id)
        state = _state(
            blob_id=blob.id,
            source_path=blob.storage_path,
            output_dir=output_dir,
            topology=topology,
        )
        state_dict = state.to_dict()
        await app.state.session_service.save_composition_state(
            session_id,
            CompositionStateData(
                sources=state_dict["sources"],
                nodes=state_dict["nodes"],
                edges=state_dict["edges"],
                outputs=state_dict["outputs"],
                metadata_=state_dict["metadata"],
                is_valid=True,
                validation_errors=None,
            ),
            provenance="session_seed",
        )

        validation_response = await client.post(
            f"/api/sessions/{session_id}/validate",
            headers=headers,
        )
        execution_response = await client.post(
            f"/api/sessions/{session_id}/execute",
            headers=headers,
        )
        runs = await app.state.session_service.list_runs_for_session(session_id)

    validation = validation_response.json()
    observed = (
        validation_response.status_code,
        validation["is_valid"],
        validation["checks"][24]["name"],
        validation["checks"][24]["passed"],
        execution_response.status_code,
        len(runs),
    )
    assert observed == (200, False, "proof_diagnostics", False, 422, 0), validation
    assert validation["errors"][0]["error_code"] == "gate_expression_type_mismatch_against_source_schema"
    assert execution_response.json()["detail"]["errors"][0]["error_code"] == ("gate_expression_type_mismatch_against_source_schema")
