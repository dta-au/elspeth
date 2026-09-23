"""A retained live CLI run can replay and verify without sink publication."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import yaml
from typer.testing import CliRunner

from elspeth.cli import app
from elspeth.plugins.sinks.json_sink import JSONSink
from elspeth.plugins.sources.csv_source import CSVSource
from elspeth.plugins.transforms.passthrough import PassThrough


def _settings(tmp_path: Path) -> dict[str, object]:
    source_path = tmp_path / "input.csv"
    source_path.write_text("value\n7\n8\n")
    sink_path = tmp_path / "output.json"
    return {
        "sources": {
            "primary": {
                "plugin": "csv",
                "on_success": "source_out",
                "options": {
                    "path": str(source_path),
                    "on_validation_failure": "discard",
                    "schema": {"mode": "observed"},
                },
            }
        },
        "transforms": [
            {
                "name": "copy",
                "plugin": "passthrough",
                "input": "source_out",
                "on_success": "output",
                "on_error": "discard",
                "options": {"schema": {"mode": "observed"}},
            }
        ],
        "sinks": {
            "output": {
                "plugin": "json",
                "on_write_failure": "discard",
                "options": {"path": str(sink_path), "schema": {"mode": "observed"}},
            }
        },
        "landscape": {"url": f"sqlite:///{tmp_path / 'landscape.db'}"},
        "concurrency": {"max_workers": 1},
        "payload_store": {"base_path": str(tmp_path / "payloads")},
    }


def test_cli_live_replay_verify_preserves_sink_artifact(tmp_path: Path) -> None:
    settings_path = tmp_path / "settings.yaml"
    sink_path = tmp_path / "output.json"
    settings = _settings(tmp_path)
    runner = CliRunner()

    def invoke() -> dict[str, object]:
        settings_path.write_text(yaml.safe_dump(settings))
        result = runner.invoke(app, ["--no-dotenv", "run", "--settings", str(settings_path), "--execute", "--format", "json"])
        assert result.exit_code == 0, result.output
        return json.loads(result.output.strip().splitlines()[-1])

    live = invoke()
    assert live["status"] == "completed"
    artifact = sink_path.read_bytes()

    settings["run_mode"] = "replay"
    settings["replay_from"] = live["run_id"]
    with (
        patch.object(CSVSource, "on_start", side_effect=AssertionError("replay started live source")),
        patch.object(CSVSource, "load", side_effect=AssertionError("replay read live source")),
        patch.object(JSONSink, "on_start", side_effect=AssertionError("replay started live sink")),
        patch.object(JSONSink, "write", side_effect=AssertionError("replay wrote sink")),
    ):
        replay = invoke()
    assert replay["status"] == "completed"
    assert sink_path.read_bytes() == artifact

    settings["run_mode"] = "verify"
    with (
        patch.object(JSONSink, "on_start", side_effect=AssertionError("verify started live sink")),
        patch.object(JSONSink, "write", side_effect=AssertionError("verify wrote sink")),
    ):
        verify = invoke()
    assert verify["status"] == "completed"
    assert sink_path.read_bytes() == artifact


def test_verify_rejects_late_source_drift_before_transform_start(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    sources = settings["sources"]
    assert isinstance(sources, dict)
    second_path = tmp_path / "second.csv"
    second_path.write_text("value\n9\n")
    sources["second"] = {
        "plugin": "csv",
        "on_success": "second_output",
        "options": {"path": str(second_path), "on_validation_failure": "discard", "schema": {"mode": "observed"}},
    }
    sinks = settings["sinks"]
    assert isinstance(sinks, dict)
    sinks["second_output"] = {
        "plugin": "json",
        "on_write_failure": "discard",
        "options": {"path": str(tmp_path / "second_output.json"), "schema": {"mode": "observed"}},
    }
    settings_path = tmp_path / "settings.yaml"
    settings_path.write_text(yaml.safe_dump(settings, sort_keys=False))
    runner = CliRunner()
    live = runner.invoke(app, ["--no-dotenv", "run", "--settings", str(settings_path), "--execute", "--format", "json"])
    assert live.exit_code == 0, live.output
    live_run_id = json.loads(live.output.strip().splitlines()[-1])["run_id"]

    second_path.write_text("value\n99\n")
    settings["run_mode"] = "verify"
    settings["replay_from"] = live_run_id
    settings_path.write_text(yaml.safe_dump(settings, sort_keys=False))
    with patch.object(PassThrough, "on_start", side_effect=AssertionError("transform started before complete source check")) as startup:
        result = runner.invoke(app, ["--no-dotenv", "run", "--settings", str(settings_path), "--execute", "--format", "json"])
    assert result.exit_code != 0, result.output
    assert "source" in result.output.lower()
    startup.assert_not_called()
