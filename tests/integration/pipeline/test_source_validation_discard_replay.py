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


def _write_input(tmp_path: Path, source_type: type[CSVSource] | type[JSONSource], values: list[int | str | float]) -> None:
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
@pytest.mark.parametrize(
    "values",
    [[7], [7, "bad"], [7, "bad", "bad"], [7, 1e17]],
    # 1e17 under `value: int` is an int beyond ±(2**53-1): discarded, and its
    # recorded raw row holds the double, which canonical JSON writes as
    # 100000000000000000 — replay and verify must read it back as that double
    # (review-codexfix-handoffs-r1 F1).
    ids=["clean", "discard", "duplicates", "unsafe-integral-double"],
)
def test_replay_and_verify_preserve_validation_discard_evidence(
    tmp_path: Path, values: list[int | str | float], source_type: type[CSVSource] | type[JSONSource]
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


def test_verify_restores_a_quarantined_non_object_double_beyond_2_53_exactly(tmp_path: Path) -> None:
    """A quarantined bare JSON number is re-read from canonical text as the recorded double.

    A JSONL line holding a number, not an object, is quarantined as
    ``{"_raw": value}``; replay compares that payload with the validation
    record's ``row_data_json``. RFC 8785 writes 2**60 as the padded shortest
    form ``1152921504606847000``, which ``json.loads`` reads as an int that is
    NOT equal to the double 2**60 — verify then refused the run as
    "quarantine payload disagrees with validation evidence"
    (review-codexfix-handoffs-r1 F1).
    """
    (tmp_path / "input.jsonl").write_text('{"value": 7}\n1.152921504606847e18\n', encoding="utf-8")
    settings: dict[str, object] = {
        "sources": {
            "primary": {
                "plugin": "json",
                "on_success": "output",
                "options": {
                    "path": str(tmp_path / "input.jsonl"),
                    "format": "jsonl",
                    "on_validation_failure": "quarantine",
                    "schema": {"mode": "observed"},
                },
            }
        },
        "sinks": {
            name: {
                "plugin": "json",
                "on_write_failure": "discard",
                "options": {"path": str(tmp_path / f"{name}.jsonl"), "format": "jsonl", "schema": {"mode": "observed"}},
            }
            for name in ("output", "quarantine")
        },
        "landscape": {"url": f"sqlite:///{tmp_path / 'landscape.db'}"},
        "payload_store": {"base_path": str(tmp_path / "payloads")},
        "concurrency": {"max_workers": 1},
    }
    source = _invoke(tmp_path, settings)
    assert source.exit_code == 1, source.output  # completed with failures: one row quarantined
    source_run_id = str(json.loads(source.output.strip().splitlines()[-1])["run_id"])
    assert [json.loads(line) for line in (tmp_path / "quarantine.jsonl").read_text().splitlines()] == [{"_raw": float(2**60)}]

    settings.update(run_mode="verify", replay_from=source_run_id)
    verify = _invoke(tmp_path, settings)

    assert verify.exit_code == 1, verify.output
    assert "disagrees with validation evidence" not in verify.output
    assert json.loads(verify.output.strip().splitlines()[-1])["status"] == "completed_with_failures"
