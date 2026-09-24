"""Retained sink diversions replay their evidence without publishing either sink."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml
from typer.testing import CliRunner

from elspeth.cli import app
from elspeth.contracts import RunMode
from elspeth.contracts.sink_effects import SinkEffectRole
from elspeth.core.landscape import LandscapeDB
from elspeth.engine.executors.replay_sink_effect import VirtualReplaySinkEffect, verify_virtual_sink_members
from elspeth.plugins.sinks.csv_sink import CSVSink
from elspeth.plugins.sinks.database_sink import DatabaseSink
from elspeth.plugins.sinks.json_sink import JSONSink
from tests.fixtures.landscape import make_factory
from tests.integration.pipeline.test_database_effect_diversion import _provision_target, _sink_config
from tests.integration.pipeline.test_run_mode_end_to_end import _settings


@pytest.mark.parametrize("mode", ["replay", "verify"])
@pytest.mark.parametrize("destination", ["quarantine", "discard"])
def test_cli_retained_sink_diversion_preserves_dispositions_and_handoff(tmp_path: Path, mode: str, destination: str) -> None:
    settings = _settings(tmp_path)
    (tmp_path / "input.csv").write_text("value\nascii\ncafé\n", encoding="utf-8")
    primary_path = tmp_path / "output.csv"
    quarantine_path = tmp_path / "quarantine.jsonl"
    sinks = {
        "output": {
            "plugin": "csv",
            "on_write_failure": destination,
            "options": {"path": str(primary_path), "encoding": "ascii", "schema": {"mode": "observed"}},
        }
    }
    if destination == "quarantine":
        sinks["quarantine"] = {
            "plugin": "json",
            "on_write_failure": "discard",
            "options": {"path": str(quarantine_path), "format": "jsonl", "schema": {"mode": "observed"}},
        }
    settings["sinks"] = sinks
    settings_path = tmp_path / "settings.yaml"
    runner = CliRunner()
    settings_path.write_text(yaml.safe_dump(settings), encoding="utf-8")
    live = runner.invoke(app, ["--no-dotenv", "run", "--settings", str(settings_path), "--execute", "--format", "json"])
    expected_exit = 1 if destination == "discard" else 0
    assert live.exit_code == expected_exit, live.output
    live_run_id = json.loads(live.output.strip().splitlines()[-1])["run_id"]
    published = {primary_path: primary_path.read_bytes()}
    if destination == "quarantine":
        published[quarantine_path] = quarantine_path.read_bytes()
        assert json.loads(published[quarantine_path])["value"] == "café"

    settings["run_mode"] = mode
    settings["replay_from"] = live_run_id
    settings_path.write_text(yaml.safe_dump(settings), encoding="utf-8")
    with (
        patch.object(CSVSink, "prepare_effect", side_effect=AssertionError("non-live primary prepare")),
        patch.object(CSVSink, "commit_effect", side_effect=AssertionError("non-live primary publish")),
        patch.object(JSONSink, "prepare_effect", side_effect=AssertionError("non-live failsink prepare")),
        patch.object(JSONSink, "commit_effect", side_effect=AssertionError("non-live failsink publish")),
    ):
        non_live = runner.invoke(app, ["--no-dotenv", "run", "--settings", str(settings_path), "--execute", "--format", "json"])
    assert non_live.exit_code == expected_exit, non_live.output
    current_run_id = json.loads(non_live.output.strip().splitlines()[-1])["run_id"]
    assert {path: path.read_bytes() for path in published} == published
    with LandscapeDB.from_url(f"sqlite:///{tmp_path / 'landscape.db'}", create_tables=False) as db:
        factory = make_factory(db)
        verify_virtual_sink_members(factory, source_run_id=live_run_id, current_run_id=current_run_id, mode=RunMode(mode))
        source_members = factory.execution.sink_effects.get_members_for_run(live_run_id)
        replay_members = factory.execution.sink_effects.get_members_for_run(current_run_id)
        assert [member.prepared_disposition for member in replay_members].count("diverted") == 1
        assert sum(member.role is SinkEffectRole.FAILSINK for member in replay_members) == (destination == "quarantine")
        assert len(source_members) == len(replay_members)
        assert {(member.role, member.ingest_sequence, member.reason_hash) for member in source_members} == {
            (member.role, member.ingest_sequence, member.reason_hash) for member in replay_members
        }
        assert all(effect.publication_performed is False for effect in factory.execution.sink_effects.get_effects_for_run(current_run_id))

    # A virtual run is itself retained evidence; its diversion timestamp still
    # belongs to the original live run rather than either later run's start.
    settings["replay_from"] = current_run_id
    settings_path.write_text(yaml.safe_dump(settings), encoding="utf-8")
    with (
        patch.object(CSVSink, "prepare_effect", side_effect=AssertionError("chained primary prepare")),
        patch.object(JSONSink, "prepare_effect", side_effect=AssertionError("chained failsink prepare")),
    ):
        chained = runner.invoke(app, ["--no-dotenv", "run", "--settings", str(settings_path), "--execute", "--format", "json"])
    assert chained.exit_code == expected_exit, chained.output
    assert {path: path.read_bytes() for path in published} == published


@pytest.mark.parametrize("mode", ["replay", "verify"])
def test_cli_retained_database_commit_diversion_reconstructs_attribution(tmp_path: Path, mode: str) -> None:
    settings = _settings(tmp_path)
    source_path = tmp_path / "input.json"
    source_path.write_text('[{"id":1,"name":"one"},{"id":2,"name":"duplicate"}]', encoding="utf-8")
    settings["sources"] = {
        "primary": {
            "plugin": "json",
            "on_success": "source_out",
            "options": {"path": str(source_path), "on_validation_failure": "discard", "schema": {"mode": "observed"}},
        }
    }
    target_path = tmp_path / "target.db"
    target_url = f"sqlite:///{target_path}"
    _provision_target(target_url)
    settings["sinks"] = {"output": {"plugin": "database", "on_write_failure": "discard", "options": _sink_config(target_url)}}
    settings_path = tmp_path / "settings.yaml"
    runner = CliRunner()
    settings_path.write_text(yaml.safe_dump(settings), encoding="utf-8")
    live = runner.invoke(app, ["--no-dotenv", "run", "--settings", str(settings_path), "--execute", "--format", "json"])
    assert live.exit_code == 1, live.output
    live_run_id = json.loads(live.output.strip().splitlines()[-1])["run_id"]
    target_bytes = target_path.read_bytes()
    settings["run_mode"] = mode
    settings["replay_from"] = live_run_id
    settings_path.write_text(yaml.safe_dump(settings), encoding="utf-8")
    with (
        patch.object(DatabaseSink, "prepare_effect", side_effect=AssertionError("non-live SQL prepare")),
        patch.object(DatabaseSink, "commit_effect", side_effect=AssertionError("non-live SQL publish")),
    ):
        non_live = runner.invoke(app, ["--no-dotenv", "run", "--settings", str(settings_path), "--execute", "--format", "json"])
    assert non_live.exit_code == 1, non_live.output
    assert target_path.read_bytes() == target_bytes
    current_run_id = json.loads(non_live.output.strip().splitlines()[-1])["run_id"]
    with LandscapeDB.from_url(f"sqlite:///{tmp_path / 'landscape.db'}", create_tables=False) as db:
        factory = make_factory(db)
        source_members = factory.execution.sink_effects.get_members_for_run(live_run_id)
        replay_members = factory.execution.sink_effects.get_members_for_run(current_run_id)
        assert [(member.ingest_sequence, member.prepared_disposition) for member in source_members] == [
            (member.ingest_sequence, member.prepared_disposition) for member in replay_members
        ]
        sink_node_id = source_members[0].sink_node_id
        source = VirtualReplaySinkEffect(factory=factory, source_run_id=live_run_id, sink_node_id=sink_node_id)
        current = VirtualReplaySinkEffect(factory=factory, source_run_id=current_run_id, sink_node_id=sink_node_id)
        assert source._source_dispositions() == current._source_dispositions()
