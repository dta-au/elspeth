"""Run-list and status routes expose a queued run until its worker starts."""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from elspeth.web.composer.state import CompositionState, OutputSpec, PipelineMetadata, SourceSpec
from elspeth.web.sessions.protocol import CompositionStateData
from tests.integration.web.conftest import _save_composition_state_with_compose_authority
from tests.integration.web.test_execute_terminal_write_held import TEST_CSV, _bounded, _run_with_a_held_write


async def _route_statuses(run: Any, session_id: str, run_id: str) -> tuple[str, str]:
    listed = await run.client.get(f"/api/sessions/{session_id}/runs", headers=run.headers)
    assert listed.status_code == 200, listed.text
    rows = [row for row in listed.json() if row["id"] == run_id]
    assert len(rows) == 1
    status = await run.client.get(f"/api/runs/{run_id}", headers=run.headers)
    assert status.status_code == 200, status.text
    return rows[0]["status"], status.json()["status"]


@pytest.mark.integration
@_bounded
async def test_queued_run_routes_advance_only_after_the_single_worker_starts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async with _run_with_a_held_write(tmp_path, monkeypatch) as first:
        created = await first.client.post("/api/sessions", headers=first.headers, json={"title": "next run"})
        assert created.status_code == 201, created.text
        session_id = created.json()["id"]
        blob_dir = tmp_path / "blobs" / session_id
        blob_dir.mkdir(parents=True)
        shutil.copy(TEST_CSV, blob_dir / "input.csv")
        state = CompositionState(
            source=SourceSpec(
                plugin="csv",
                on_success="primary",
                options={
                    "path": str(blob_dir / "input.csv"),
                    "schema": {"mode": "fixed", "fields": ["id: int", "name: str", "value: int"]},
                },
                on_validation_failure="discard",
            ),
            nodes=(),
            edges=(),
            outputs=(
                OutputSpec(
                    name="primary",
                    plugin="csv",
                    options={
                        "path": str(tmp_path / "outputs" / session_id / "result.csv"),
                        "schema": {"mode": "fixed", "fields": ["id: int", "name: str", "value: int"]},
                    },
                    on_write_failure="discard",
                ),
            ),
            metadata=PipelineMetadata(name="Next run", description="CSV passthrough"),
            version=1,
        ).to_dict()
        await _save_composition_state_with_compose_authority(
            first.app.state.session_service,
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
        started = await first.client.post(f"/api/sessions/{session_id}/execute", headers=first.headers)
        assert started.status_code == 202, started.text
        run_id = started.json()["run_id"]
        assert await _route_statuses(first, session_id, run_id) == ("pending", "pending")

        # The first worker is still held on its terminal write. Once released,
        # hold the second worker just after it writes running so both routes can
        # be observed before the short CSV pipeline completes.
        running_written = asyncio.Event()
        release_second = asyncio.Event()
        session_service = first.app.state.session_service
        original_update = session_service.update_run_status

        async def observe_worker_start(updated_run_id: UUID, status: str, *args: Any, **kwargs: Any) -> None:
            await original_update(updated_run_id, status, *args, **kwargs)
            if str(updated_run_id) == run_id and status == "running":
                running_written.set()
                await release_second.wait()

        monkeypatch.setattr(session_service, "update_run_status", observe_worker_start)
        try:
            first.release.set()
            await asyncio.wait_for(running_written.wait(), 30)
            assert await _route_statuses(first, session_id, run_id) == ("running", "running")
        finally:
            release_second.set()
