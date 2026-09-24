"""CLI regressions for source declaration order, row contracts, and PDF refusals."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml
from sqlalchemy import select
from typer.testing import CliRunner

from elspeth.cli import app
from elspeth.core.landscape import LandscapeDB
from elspeth.core.landscape.schema import nodes_table, rows_table
from elspeth.core.payload_store import FilesystemPayloadStore
from elspeth.plugins.infrastructure.rasterize.renderer import PoolRenderer
from elspeth.plugins.sinks.json_sink import JSONSink
from elspeth.plugins.sources.csv_source import CSVSource
from elspeth.plugins.sources.json_source import JSONSource
from tests.fixtures.pdf_documents import MALFORMED_PDF, minimal_pdf
from tests.integration.pipeline.test_run_mode_end_to_end import _settings


def _invoke(settings: dict[str, object], tmp_path: Path, expected_exit: int = 0) -> dict[str, object]:
    settings_path = tmp_path / "settings.yaml"
    settings_path.write_text(yaml.safe_dump(settings, sort_keys=False), encoding="utf-8")
    result = CliRunner().invoke(app, ["--no-dotenv", "run", "--settings", str(settings_path), "--execute", "--format", "json"])
    assert result.exit_code == expected_exit, result.output
    return json.loads(result.output.strip().splitlines()[-1])


def test_multiple_sources_preserve_declaration_order_for_replay_and_verify(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    second_path = tmp_path / "second.csv"
    second_path.write_text("value\n9\n", encoding="utf-8")
    settings["sources"] = {
        name: {
            "plugin": "csv",
            "on_success": sink,
            "options": {"path": str(path), "on_validation_failure": "discard", "schema": {"mode": "observed"}},
        }
        for name, sink, path in (("z_first", "output", tmp_path / "input.csv"), ("a_second", "second_output", second_path))
    }
    settings["transforms"] = []
    settings["sinks"] = {
        name: {
            "plugin": "json",
            "on_write_failure": "discard",
            "options": {"path": str(tmp_path / f"{name}.json"), "schema": {"mode": "observed"}},
        }
        for name in ("output", "second_output")
    }
    live = _invoke(settings, tmp_path)
    with LandscapeDB.from_url(f"sqlite:///{tmp_path / 'landscape.db'}", create_tables=False) as db, db.engine.connect() as connection:
        sources = connection.execute(
            select(nodes_table.c.config_json, nodes_table.c.sequence_in_pipeline)
            .where(nodes_table.c.run_id == live["run_id"], nodes_table.c.node_type == "source")
            .order_by(nodes_table.c.sequence_in_pipeline)
        ).all()
    assert [(json.loads(config)["source_name"], sequence) for config, sequence in sources] == [("z_first", 0), ("a_second", 1)]
    artifacts = {name: (tmp_path / f"{name}.json").read_bytes() for name in ("output", "second_output")}
    settings["replay_from"] = live["run_id"]
    for mode in ("replay", "verify"):
        settings["run_mode"] = mode
        with patch.object(JSONSink, "commit_effect", side_effect=AssertionError("non-live sink publication")):
            if mode == "replay":
                with patch.object(CSVSource, "load", side_effect=AssertionError("replay loaded source")):
                    result = _invoke(settings, tmp_path)
            else:
                result = _invoke(settings, tmp_path)
        assert result["status"] == "completed"
        assert {name: (tmp_path / f"{name}.json").read_bytes() for name in artifacts} == artifacts

    sources_config = settings["sources"]
    assert isinstance(sources_config, dict)
    settings["sources"] = dict(reversed(tuple(sources_config.items())))
    settings_path = tmp_path / "settings.yaml"
    settings_path.write_text(yaml.safe_dump(settings, sort_keys=False), encoding="utf-8")
    reordered = CliRunner().invoke(app, ["--no-dotenv", "run", "--settings", str(settings_path), "--execute", "--format", "json"])
    assert reordered.exit_code != 0, reordered.output
    assert "source order differs from audited run" in reordered.output


@pytest.mark.parametrize("source_format", ["json", "jsonl"])
def test_sparse_source_row_contracts_survive_replay_and_verify(tmp_path: Path, source_format: str) -> None:
    settings = _settings(tmp_path)
    source_path = tmp_path / f"input.{source_format}"
    source_rows = [{"value": 7}, {"value": 8, "extra": "optional"}]
    source_path.write_text(
        json.dumps(source_rows) if source_format == "json" else "\n".join(json.dumps(row) for row in source_rows), encoding="utf-8"
    )
    settings["sources"] = {
        "primary": {
            "plugin": "json",
            "on_success": "source_out",
            "options": {"path": str(source_path), "on_validation_failure": "discard", "schema": {"mode": "observed"}},
        }
    }
    live = _invoke(settings, tmp_path)
    with LandscapeDB.from_url(f"sqlite:///{tmp_path / 'landscape.db'}", create_tables=False) as db, db.engine.connect() as connection:
        contracts = (
            connection.execute(
                select(rows_table.c.source_contract_json)
                .where(rows_table.c.run_id == live["run_id"])
                .order_by(rows_table.c.source_row_index)
            )
            .scalars()
            .all()
        )
    assert len(contracts) == 2 and contracts[0] != contracts[1]
    artifact = (tmp_path / "output.json").read_bytes()
    settings["replay_from"] = live["run_id"]
    for mode in ("replay", "verify"):
        settings["run_mode"] = mode
        with patch.object(JSONSink, "commit_effect", side_effect=AssertionError("non-live sink publication")):
            if mode == "replay":
                with patch.object(JSONSource, "load", side_effect=AssertionError("replay loaded source")):
                    result = _invoke(settings, tmp_path)
            else:
                result = _invoke(settings, tmp_path)
        assert result["status"] == "completed"
        assert (tmp_path / "output.json").read_bytes() == artifact


def test_mixed_pdf_success_and_refusal_replay_and_verify(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    store = FilesystemPayloadStore(tmp_path / "payloads")
    good_ref = store.store(minimal_pdf())
    refused_ref = store.store(MALFORMED_PDF)
    (tmp_path / "input.csv").write_text(f"blob_ref\n{good_ref}\n{refused_ref}\n", encoding="utf-8")
    settings["transforms"] = [
        {
            "name": "render",
            "plugin": "pdf_rasterize",
            "input": "source_out",
            "on_success": "output",
            "on_error": "errors",
            "options": {"schema": {"mode": "observed"}},
        }
    ]
    settings["sinks"] = {
        name: {
            "plugin": "json",
            "on_write_failure": "discard",
            "options": {"path": str(tmp_path / f"{name}.json"), "schema": {"mode": "observed"}},
        }
        for name in ("output", "errors")
    }
    live = _invoke(settings, tmp_path, expected_exit=1)
    assert live["status"] == "completed_with_failures"
    assert len(json.loads((tmp_path / "output.json").read_text())) == 1
    assert len(json.loads((tmp_path / "errors.json").read_text())) == 1
    artifacts = {name: (tmp_path / f"{name}.json").read_bytes() for name in ("output", "errors")}
    settings["replay_from"] = live["run_id"]
    for mode in ("replay", "verify"):
        settings["run_mode"] = mode
        with patch.object(JSONSink, "commit_effect", side_effect=AssertionError("non-live sink publication")):
            if mode == "replay":
                with patch.object(PoolRenderer, "render", side_effect=AssertionError("replay launched PDF worker")):
                    result = _invoke(settings, tmp_path, expected_exit=1)
            else:
                result = _invoke(settings, tmp_path, expected_exit=1)
        assert result["status"] == "completed_with_failures"
        assert {name: (tmp_path / f"{name}.json").read_bytes() for name in artifacts} == artifacts
