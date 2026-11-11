"""Regression tests for sweep findings #40, #80 and #103 (20261111-plugins-review).

#40: JSONSource structural quarantines must record the SAME row payload in
Landscape that they yield, so the quarantined emission links to its
validation error (and snapshot_for_resume does not crash).

#80: CSVSource decode errors must name the physical line that holds the bad
byte, and every valid row before it must be emitted, not the first line of the
~8 KB TextIOWrapper decode chunk that contains it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from elspeth.contracts.plugin_context import PluginContext
from elspeth.plugins.sources.csv_source import CSVSource
from elspeth.plugins.sources.json_source import JSONSource
from tests.fixtures.factories import make_source_context
from tests.fixtures.landscape import make_recorder_with_run


def _landscape_ctx() -> PluginContext:
    setup = make_recorder_with_run(source_plugin_name="json")
    return PluginContext(
        run_id=setup.run_id,
        coordination_token=setup.coordination_token,
        node_id=setup.source_node_id,
        config={},
        landscape=setup.factory.plugin_audit_writer(),
    )


@pytest.mark.parametrize(
    ("content", "data_key"),
    [
        pytest.param('[{"id": 1}]', "results", id="data_key_on_list_root"),
        pytest.param('{"wrong": [1]}', "results", id="data_key_missing"),
        pytest.param('{"results": {"id": 1}}', "results", id="data_key_extracts_non_list"),
        pytest.param('{"a": 1}', None, id="non_array_root"),
    ],
)
def test_json_structural_quarantine_links_to_recorded_validation_error(tmp_path: Path, content: str, data_key: str | None) -> None:
    path = tmp_path / "data.json"
    path.write_text(content)
    config: dict[str, object] = {
        "path": str(path),
        "format": "json",
        "on_validation_failure": "quarantine",
        "schema": {"mode": "observed"},
    }
    if data_key is not None:
        config["data_key"] = data_key
    ctx = _landscape_ctx()

    rows = list(JSONSource(config).load(ctx))

    assert len(rows) == 1
    quarantined = rows[0]
    assert quarantined.is_quarantined
    validation_error_id = ctx.pop_pending_quarantine_validation_error_id(quarantined.row)
    assert validation_error_id is not None, "quarantined structural row is not linked to its recorded validation error"
    assert validation_error_id.startswith("verr_")


def _csv_with_bad_byte(path: Path, *, data_rows: int, bad_line: int, encoding: str = "utf-8") -> None:
    """Write ``id,val`` plus ``data_rows`` rows, with an undecodable byte appended to physical line ``bad_line``."""
    lines = [line.encode(encoding) for line in ["id,val", *(f"{i},row{i}" for i in range(1, data_rows + 1))]]
    lines[bad_line - 1] += b"\xff"
    path.write_bytes(b"\n".join(lines) + b"\n")


@pytest.mark.parametrize(
    ("data_rows", "bad_line"),
    [
        pytest.param(3000, 2001, id="bad_byte_mid_file"),
        pytest.param(200, 151, id="bad_byte_in_first_decode_chunk"),
    ],
)
def test_csv_decode_error_points_at_the_line_holding_the_bad_byte(tmp_path: Path, data_rows: int, bad_line: int) -> None:
    path = tmp_path / "data.csv"
    _csv_with_bad_byte(path, data_rows=data_rows, bad_line=bad_line)
    source = CSVSource({"path": str(path), "on_validation_failure": "quarantine", "schema": {"mode": "observed"}})

    rows = list(source.load(make_source_context()))

    valid = [row for row in rows if not row.is_quarantined]
    quarantined = [row for row in rows if row.is_quarantined]
    # Physical line 1 is the header, so lines 2..bad_line-1 are the valid rows before the bad one.
    assert len(valid) == bad_line - 2
    assert [row.row["id"] for row in valid[-1:]] == [str(bad_line - 2)]
    assert len(quarantined) == 1
    bad = quarantined[0]
    assert bad.row["__line_number__"] == bad_line
    assert bad.row["__row_number__"] == bad_line - 1
    assert bad.source_row_index == bad_line - 2
    assert f"at line {bad_line}" in bad.quarantine_error


def test_csv_decode_error_line_is_exact_for_utf16(tmp_path: Path) -> None:
    path = tmp_path / "data.csv"
    text_lines = ["id,val", *(f"{i},row{i}" for i in range(1, 3001))]
    good = "\n".join(text_lines[:2000]).encode("utf-16") + "\n".encode("utf-16-le")
    # A lone low surrogate (DC00) is undecodable UTF-16 on physical line 2001.
    rest = "\n".join(text_lines[2000:]).encode("utf-16-le")
    path.write_bytes(good + b"\x00\xdc" + rest + "\n".encode("utf-16-le"))
    source = CSVSource({"path": str(path), "encoding": "utf-16", "on_validation_failure": "quarantine", "schema": {"mode": "observed"}})

    rows = list(source.load(make_source_context()))

    valid = [row for row in rows if not row.is_quarantined]
    quarantined = [row for row in rows if row.is_quarantined]
    assert len(valid) == 1999
    assert len(quarantined) == 1
    assert quarantined[0].row["__line_number__"] == 2001
    assert quarantined[0].source_row_index == 1999
