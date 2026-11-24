"""Canonical row values across the CLI sink boundary."""

from __future__ import annotations

import csv
import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml
from sqlalchemy import Column, Integer, MetaData, Table, Text, create_engine
from typer.testing import CliRunner

from elspeth.cli import app
from elspeth.contracts import Determinism, SourceRow
from elspeth.contracts.schema_contract import FieldContract, SchemaContract
from elspeth.plugins.infrastructure.discovery import create_dynamic_hookimpl
from elspeth.plugins.infrastructure.manager import PluginManager, scoped_plugin_manager
from elspeth.plugins.sinks.database_sink import database_effect_ledger_table
from elspeth.plugins.sources.json_source import JSONSource


class BytesFileSource(JSONSource):
    """Test plugin: read a local input file and emit its bytes as a row field."""

    name = "x1_bytes_file"
    determinism = Determinism.IO_READ

    def load(self, ctx: Any) -> Iterator[SourceRow]:
        del ctx
        contract = SchemaContract(
            mode="OBSERVED",
            fields=(
                FieldContract(normalized_name="id", original_name="id", python_type=int, required=False, source="inferred"),
                FieldContract(normalized_name="payload", original_name="payload", python_type=object, required=False, source="inferred"),
            ),
            locked=True,
        )
        self.set_schema_contract(contract)
        yield SourceRow.valid({"id": 1, "payload": self._path.read_bytes()}, contract=contract, source_row_index=0)


@pytest.fixture
def bytes_plugin() -> Iterator[None]:
    manager = PluginManager()
    manager.register_builtin_plugins()
    manager.register(create_dynamic_hookimpl([BytesFileSource], "elspeth_get_source"))
    with scoped_plugin_manager(manager):
        yield


def _query(path: Path, sql: str) -> list[tuple[Any, ...]]:
    con = sqlite3.connect(path)
    try:
        return list(con.execute(sql))
    finally:
        con.close()


def _facts(audit: Path) -> dict[str, Any]:
    if not audit.exists():
        return {"audit_missing": True}
    return {
        "runs": _query(audit, "select run_id, status from runs"),
        "tokens": _query(audit, "select token_id from tokens"),
        "outcomes": _query(audit, "select token_id, outcome, path, completed, sink_name from token_outcomes"),
        "work": _query(audit, "select token_id, status, pending_sink_name from token_work_items"),
        "sink_states": _query(
            audit,
            "select n.plugin_name, ns.status, ns.error_json from node_states ns join nodes n on n.node_id=ns.node_id and n.run_id=ns.run_id where n.node_type='sink'",
        ),
        "effects": _query(audit, "select effect_id, state from sink_effects"),
        "members": _query(audit, "select effect_id, token_id, member_state from sink_effect_members"),
    }


def _target(sink: str, target: Path) -> Any:
    if sink == "database":
        return _query(target, "select id, payload from output_rows")
    if not target.exists():
        return None
    if sink == "json":
        return [json.loads(line) for line in target.read_text().splitlines() if line]
    with target.open(newline="") as stream:
        return list(csv.DictReader(stream))


@pytest.mark.parametrize("sink", ["csv", "json", "database"])
@pytest.mark.parametrize("trigger", ["control", "nested_key", "bytes_shape", "bytes"])
def test_canonical_row_reaches_sink(tmp_path: Path, sink: str, trigger: str, bytes_plugin: None) -> None:
    input_path = tmp_path / ("input.bin" if trigger == "bytes" else "input.jsonl")
    if trigger == "bytes":
        input_path.write_bytes(b"x1-bytes")
        source = {
            "plugin": "x1_bytes_file",
            "on_success": "out",
            "options": {"path": str(input_path), "on_validation_failure": "discard", "schema": {"mode": "observed"}},
        }
    else:
        payload: Any = (
            {"__elspeth_canonical_type__": "x", "v": 2}
            if trigger == "nested_key"
            else {"__elspeth_canonical_type__": "bytes", "base64": "eDEtYnl0ZXM="}
            if trigger == "bytes_shape"
            else "ordinary"
        )
        input_path.write_text(json.dumps({"id": 1, "payload": payload}) + "\n")
        source = {
            "plugin": "json",
            "on_success": "out",
            "options": {"path": str(input_path), "format": "jsonl", "on_validation_failure": "discard", "schema": {"mode": "observed"}},
        }
    target = tmp_path / ("target.db" if sink == "database" else f"out.{'csv' if sink == 'csv' else 'jsonl'}")
    if sink == "database":
        engine = create_engine(f"sqlite:///{target}")
        metadata = MetaData()
        Table("output_rows", metadata, Column("id", Integer), Column("payload", Text))
        database_effect_ledger_table(metadata, "_elspeth_sink_effects")
        metadata.create_all(engine)
        engine.dispose()
        options = {
            "url": f"sqlite:///{target}",
            "table": "output_rows",
            "if_exists": "append",
            "schema": {"mode": "fixed", "fields": ["id: int", "payload: any"]},
            "effect_ledger": {"table": "_elspeth_sink_effects", "schema_version": 1, "permissions": ["select", "insert"]},
        }
    else:
        options = {"path": str(target), "schema": {"mode": "observed"}}
        if sink == "json":
            options["format"] = "jsonl"
    settings = {
        "sources": {"src": source},
        "sinks": {"out": {"plugin": sink, "on_write_failure": "discard", "options": options}},
        "landscape": {"url": f"sqlite:///{tmp_path / 'audit.db'}"},
        "payload_store": {"backend": "filesystem", "base_path": str(tmp_path / "payloads")},
    }
    settings_path = tmp_path / "settings.yaml"
    settings_path.write_text(yaml.safe_dump(settings, sort_keys=False))
    result = CliRunner().invoke(app, ["run", "--settings", str(settings_path), "--execute"])
    facts = _facts(tmp_path / "audit.db")
    landed = _target(sink, target)
    assert "audit_missing" not in facts, result.output
    assert len(facts["runs"]) == 1, facts
    assert result.exit_code == 0, result.output
    assert landed, facts
    assert facts["runs"][0][1] == "completed", facts
    assert len(facts["outcomes"]) == 1 and facts["outcomes"][0][1:4] == ("success", "default_flow", 1), facts
    assert [item[1] for item in facts["work"]] == ["terminal"], facts
    assert [item[1] for item in facts["sink_states"]] == ["completed"], facts
    assert len(facts["effects"]) == len(facts["members"]) == 1, facts
    expected = "eDEtYnl0ZXM=" if trigger == "bytes" else payload
    if sink == "database":
        assert landed == [(1, expected if isinstance(expected, str) else json.dumps(expected, separators=(",", ":")))], landed
    elif sink == "json":
        assert landed == [{"id": 1, "payload": expected}], landed
    else:
        assert landed == [{"id": "1", "payload": expected if isinstance(expected, str) else str(expected)}], landed


def test_reserved_mapping_reaches_quarantine_sink(tmp_path: Path) -> None:
    payload = {"__elspeth_canonical_type__": "x", "v": 2}
    input_path = tmp_path / "input.jsonl"
    input_path.write_text(json.dumps({"id": "invalid", "payload": payload}) + "\n")
    quarantine = tmp_path / "quarantine.jsonl"
    settings = {
        "sources": {
            "src": {
                "plugin": "json",
                "on_success": "out",
                "options": {
                    "path": str(input_path),
                    "format": "jsonl",
                    "on_validation_failure": "quarantine",
                    "schema": {"mode": "fixed", "fields": ["id: int", "payload: any"]},
                },
            }
        },
        "sinks": {
            name: {
                "plugin": "json",
                "on_write_failure": "discard",
                "options": {"path": str(tmp_path / f"{name}.jsonl"), "format": "jsonl", "schema": {"mode": "observed"}},
            }
            for name in ("out", "quarantine")
        },
        "landscape": {"url": f"sqlite:///{tmp_path / 'audit.db'}"},
        "payload_store": {"backend": "filesystem", "base_path": str(tmp_path / "payloads")},
    }
    settings_path = tmp_path / "settings.yaml"
    settings_path.write_text(yaml.safe_dump(settings, sort_keys=False))

    result = CliRunner().invoke(app, ["run", "--settings", str(settings_path), "--execute"])

    assert result.exit_code == 1, result.output
    rows = [json.loads(line) for line in quarantine.read_text().splitlines()]
    assert len(rows) == 1
    assert rows[0]["payload"] == payload
