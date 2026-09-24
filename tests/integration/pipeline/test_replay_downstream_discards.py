"""Downstream discards keep their valid source rows during replay admission."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml
from typer.testing import CliRunner

from elspeth.cli import app
from elspeth.core.landscape import LandscapeDB
from elspeth.core.landscape.factory import RecorderFactory
from elspeth.plugins.sinks.json_sink import JSONSink
from elspeth.plugins.sources.json_source import JSONSource
from tests.integration.pipeline.test_run_mode_end_to_end import _settings


@pytest.mark.parametrize("aggregate", [False, True], ids=["transform", "aggregation"])
def test_downstream_discards_replay_and_verify_without_source_quarantine(tmp_path: Path, aggregate: bool) -> None:
    settings = _settings(tmp_path)
    source_path = tmp_path / "input.json"
    source_path.write_text('[{"value":7},{"value":"bad"}]', encoding="utf-8")
    settings["sources"] = {
        "primary": {
            "plugin": "json",
            "on_success": "source_out",
            "options": {
                "path": str(source_path),
                "on_validation_failure": "discard",
                "schema": {"mode": "fixed", "fields": ["value: any"]},
            },
        }
    }
    node = {
        "name": "process_values",
        "plugin": "batch_stats" if aggregate else "type_coerce",
        "input": "source_out",
        "on_success": "output",
        "on_error": "discard",
        "options": {
            "schema": {"mode": "observed"},
            **({"value_field": "value"} if aggregate else {"conversions": [{"field": "value", "to": "int"}]}),
        },
    }
    if aggregate:
        node.update(trigger={"count": 1}, output_mode="transform")
        settings["transforms"] = []
        settings["aggregations"] = [node]
    else:
        settings["transforms"] = [node]
    settings_path = tmp_path / "settings.yaml"
    runner = CliRunner()

    def invoke() -> dict[str, object]:
        settings_path.write_text(yaml.safe_dump(settings), encoding="utf-8")
        result = runner.invoke(app, ["--no-dotenv", "run", "--settings", str(settings_path), "--execute", "--format", "json"])
        assert result.exit_code == 1, result.output
        return json.loads(result.output.strip().splitlines()[-1])

    live = invoke()
    assert live["status"] == "completed_with_failures"
    artifact = (tmp_path / "output.json").read_bytes()
    settings["replay_from"] = live["run_id"]
    for mode in ("replay", "verify"):
        settings["run_mode"] = mode
        with patch.object(JSONSink, "commit_effect", side_effect=AssertionError("non-live sink publication")):
            if mode == "replay":
                with patch.object(JSONSource, "load", side_effect=AssertionError("replay loaded source")):
                    replayed = invoke()
            else:
                replayed = invoke()
        assert replayed["status"] == live["status"]
        assert (tmp_path / "output.json").read_bytes() == artifact
        with LandscapeDB.from_url(f"sqlite:///{tmp_path / 'landscape.db'}", create_tables=False) as db:
            read = RecorderFactory.read_only(db)
            assert read.data_flow.get_validation_errors_for_run(str(replayed["run_id"])) == []
