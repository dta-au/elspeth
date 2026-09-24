"""Replay and verification preserve validation failures absent from source rows."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml
from click.testing import Result
from typer.testing import CliRunner

from elspeth.cli import app
from elspeth.core.landscape import LandscapeDB
from elspeth.core.landscape.factory import RecorderFactory
from elspeth.plugins.sinks.json_sink import JSONSink
from elspeth.plugins.sources.csv_source import CSVSource
from elspeth.plugins.sources.json_source import JSONSource
from elspeth.plugins.transforms.passthrough import PassThrough


def _settings(tmp_path: Path, source_type: type[CSVSource] | type[JSONSource]) -> dict[str, object]:
    return {
        "sources": {
            "primary": {
                "plugin": source_type.name,
                "on_success": "source_out",
                "options": {
                    "path": str(tmp_path / f"input.{source_type.name}"),
                    "on_validation_failure": "discard",
                    "schema": {"mode": "fixed", "fields": ["value: int"]},
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
                "options": {
                    "path": str(tmp_path / "output.json"),
                    "schema": {"mode": "observed"},
                },
            }
        },
        "landscape": {"url": f"sqlite:///{tmp_path / 'landscape.db'}"},
        "payload_store": {"base_path": str(tmp_path / "payloads")},
        "concurrency": {"max_workers": 1},
    }


def _write_input(tmp_path: Path, source_type: type[CSVSource] | type[JSONSource], values: list[int | str]) -> None:
    path = tmp_path / f"input.{source_type.name}"
    if source_type is CSVSource:
        with path.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(["value"])
            writer.writerows([[value] for value in values])
    else:
        path.write_text(json.dumps([{"value": value} for value in values]), encoding="utf-8")


def _invoke(tmp_path: Path, settings: dict[str, object]) -> Result:
    path = tmp_path / "settings.yaml"
    path.write_text(yaml.safe_dump(settings), encoding="utf-8")
    return CliRunner().invoke(app, ["--no-dotenv", "run", "--settings", str(path), "--execute", "--format", "json"])


def _run_id(result: Result) -> str:
    assert result.exit_code == 0, result.output
    return str(json.loads(result.output.strip().splitlines()[-1])["run_id"])


@pytest.mark.parametrize("source_type", [CSVSource, JSONSource], ids=["csv", "json"])
@pytest.mark.parametrize(
    "before,after",
    [
        ([7, "bad"], [7]),
        ([7], [7, "bad"]),
        ([7, "bad"], [7, "different"]),
        ([7, "bad", "bad"], [7, "bad"]),
        ([7, "bad"], [7, "bad", "bad"]),
    ],
    ids=["removed", "added", "changed", "duplicate-removed", "duplicate-added"],
)
def test_verify_rejects_changed_validation_discards_before_transform_start(
    tmp_path: Path,
    before: list[int | str],
    after: list[int | str],
    source_type: type[CSVSource] | type[JSONSource],
) -> None:
    settings = _settings(tmp_path, source_type)
    _write_input(tmp_path, source_type, before)
    source_run_id = _run_id(_invoke(tmp_path, settings))
    _write_input(tmp_path, source_type, after)
    settings.update(run_mode="verify", replay_from=source_run_id)
    with patch.object(PassThrough, "on_start", side_effect=AssertionError("transform started before verification")) as startup:
        result = _invoke(tmp_path, settings)
    assert result.exit_code == 2, result.output
    assert "verification_mismatch" in result.output
    assert "validation discards differ" in result.output
    startup.assert_not_called()


@pytest.mark.parametrize("source_type", [CSVSource, JSONSource], ids=["csv", "json"])
@pytest.mark.parametrize("values", [[7], [7, "bad"], [7, "bad", "bad"]], ids=["clean", "discard", "duplicates"])
def test_replay_and_verify_preserve_validation_discard_evidence(
    tmp_path: Path, values: list[int | str], source_type: type[CSVSource] | type[JSONSource]
) -> None:
    settings = _settings(tmp_path, source_type)
    _write_input(tmp_path, source_type, values)
    source_run_id = _run_id(_invoke(tmp_path, settings))
    artifact = (tmp_path / "output.json").read_bytes()
    settings.update(run_mode="replay", replay_from=source_run_id)
    with (
        patch.object(source_type, "on_start", side_effect=AssertionError("replay started source")),
        patch.object(source_type, "load", side_effect=AssertionError("replay loaded source")),
        patch.object(JSONSink, "commit_effect", side_effect=AssertionError("replay published sink")),
    ):
        replay_run_id = _run_id(_invoke(tmp_path, settings))
    settings["run_mode"] = "verify"
    verify_run_id = _run_id(_invoke(tmp_path, settings))
    with LandscapeDB.from_url(f"sqlite:///{tmp_path / 'landscape.db'}", create_tables=False) as db:
        read = RecorderFactory.read_only(db)
        original = read.data_flow.get_validation_errors_for_run(source_run_id)
        assert len(original) == len(values) - 1
        for run_id in (replay_run_id, verify_run_id):
            errors = read.data_flow.get_validation_errors_for_run(run_id)
            assert [(e.row_hash, e.row_data_json, e.error, e.schema_mode, e.destination) for e in errors] == [
                (e.row_hash, e.row_data_json, e.error, e.schema_mode, e.destination) for e in original
            ]
    assert (tmp_path / "output.json").read_bytes() == artifact
