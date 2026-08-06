"""Unit tests for the interrupted-run resume guidance emitters.

elspeth-1f5b83cd28 / observation elspeth-obs-d8958eb450: after a graceful
shutdown the CLI must suggest ``elspeth resume ... --execute`` only when the
shared source-lifecycle gate and the resume baseline say the resume can
succeed; otherwise it prints the refuse reason. Guidance must never raise —
the interrupted exit contract (exit 3) survives any check failure.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import Connection

from elspeth.cli import _emit_interrupted_resume_guidance, _emit_interrupted_resume_guidance_from_url
from elspeth.contracts import Determinism, NodeType, RunStatus
from elspeth.core.checkpoint import CheckpointManager
from elspeth.core.dag import ExecutionGraph
from elspeth.core.landscape.database import LandscapeDB
from elspeth.core.landscape.schema import nodes_table, run_sources_table, runs_table
from tests.fixtures.landscape import make_landscape_db
from tests.helpers.checkpoint import checkpoint_draft


@pytest.fixture
def db() -> LandscapeDB:
    return make_landscape_db()


def _insert_interrupted_run(conn: Connection, run_id: str, *, lifecycle_state: str) -> None:
    conn.execute(
        runs_table.insert().values(
            run_id=run_id,
            started_at=datetime.now(UTC),
            config_hash="cfg",
            settings_json="{}",
            canonical_version="sha256-rfc8785-v1",
            status=RunStatus.INTERRUPTED,
            openrouter_catalog_sha256="0" * 64,
            openrouter_catalog_source="bundled",
        )
    )
    for node_id, node_type in (("source-node", NodeType.SOURCE), ("checkpoint-node", NodeType.TRANSFORM)):
        conn.execute(
            nodes_table.insert().values(
                node_id=node_id,
                run_id=run_id,
                plugin_name="test",
                node_type=node_type,
                plugin_version="1.0.0",
                determinism=Determinism.DETERMINISTIC,
                config_hash="node_cfg",
                config_json="{}",
                registered_at=datetime.now(UTC),
            )
        )
    conn.execute(
        run_sources_table.insert().values(
            run_id=run_id,
            source_node_id="source-node",
            source_name="primary",
            plugin_name="test_source",
            lifecycle_state=lifecycle_state,
            config_hash="src_cfg",
            schema_json="{}",
            schema_contract_json=None,
            schema_contract_hash=None,
            field_resolution_json=None,
            recorded_at=datetime.now(UTC),
        )
    )


def _create_checkpoint(db: LandscapeDB, run_id: str) -> None:
    graph = ExecutionGraph()
    graph.add_node("checkpoint-node", node_type=NodeType.TRANSFORM, plugin_name="test", config={})
    CheckpointManager(db).create_checkpoint(draft=checkpoint_draft(run_id=run_id, sequence_number=1, graph=graph))


def test_incomplete_source_prints_refusal_not_execute_suggestion(db: LandscapeDB, capsys: pytest.CaptureFixture[str]) -> None:
    run_id = "run-guidance-interrupted"
    with db.write_connection() as conn:
        _insert_interrupted_run(conn, run_id, lifecycle_state="interrupted")
    _create_checkpoint(db, run_id)

    _emit_interrupted_resume_guidance(db, run_id)

    out = capsys.readouterr().out
    assert "This run cannot be resumed:" in out
    assert "primary=interrupted" in out
    assert "--execute" not in out


def test_complete_source_with_baseline_suggests_execute(db: LandscapeDB, capsys: pytest.CaptureFixture[str]) -> None:
    run_id = "run-guidance-resumable"
    with db.write_connection() as conn:
        _insert_interrupted_run(conn, run_id, lifecycle_state="exhausted")
    _create_checkpoint(db, run_id)

    _emit_interrupted_resume_guidance(db, run_id)

    assert f"Resume with: elspeth resume {run_id} --execute" in capsys.readouterr().out


def test_missing_baseline_prints_refusal(db: LandscapeDB, capsys: pytest.CaptureFixture[str]) -> None:
    run_id = "run-guidance-no-baseline"
    with db.write_connection() as conn:
        _insert_interrupted_run(conn, run_id, lifecycle_state="exhausted")

    _emit_interrupted_resume_guidance(db, run_id)

    out = capsys.readouterr().out
    assert "no resume baseline exists" in out
    assert "--execute" not in out


def test_gate_failure_degrades_to_probe_suggestion(
    db: LandscapeDB, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    import elspeth.core.checkpoint.recovery as recovery_module

    def _boom(_db: LandscapeDB, _run_id: str) -> None:
        raise RuntimeError("gate exploded")

    monkeypatch.setattr(recovery_module, "check_source_lifecycle_resumable", _boom)

    _emit_interrupted_resume_guidance(db, "run-guidance-degraded")

    out = capsys.readouterr().out
    assert "Resumability check failed" in out
    assert "gate exploded" in out
    assert "probe with: elspeth resume run-guidance-degraded" in out
    assert "--execute" not in out


def test_from_url_wrapper_degrades_on_unopenable_db(capsys: pytest.CaptureFixture[str]) -> None:
    _emit_interrupted_resume_guidance_from_url("not-a-valid-url", None, "run-guidance-bad-url")

    out = capsys.readouterr().out
    assert "Resumability check failed" in out
    assert "probe with: elspeth resume run-guidance-bad-url" in out
    assert "--execute" not in out
