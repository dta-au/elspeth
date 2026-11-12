"""Regression test: JSONSource must quarantine over-deep external JSON, not crash.

Follow-up to sweep findings #18/#68/#112/#138 (20261111-plugins-review), which
bounded nesting before decode in azure_blob, blob_json_expand, the schema
walker and LLM validation. JSONSource decoded JSONL lines and JSON array files
with no bound: on Python 3.14 the C decoder accepts ~1500-deep documents that
the Python-level audit walkers then cannot traverse, so the run crashed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from elspeth.plugins.sources.json_source import JSONSource
from tests.unit.plugins.test_json_csv_p2_sweep import _landscape_ctx

_DEPTH = 1500
_DEEP = '{"a": ' * _DEPTH + "1" + "}" * _DEPTH


def _load(path: Path, fmt: str) -> list:
    config = {"path": str(path), "format": fmt, "on_validation_failure": "quarantine", "schema": {"mode": "observed"}}
    try:
        return list(JSONSource(config).load(_landscape_ctx()))
    except RecursionError as exc:
        pytest.fail(f"over-deep {fmt} input crashed the source with RecursionError instead of quarantining: {exc}")


def test_jsonl_over_deep_line_is_quarantined_and_later_lines_still_load(tmp_path: Path) -> None:
    path = tmp_path / "data.jsonl"
    path.write_text('{"id": 1}\n{"x": ' + _DEEP + '}\n{"id": 2}\n')

    rows = _load(path, "jsonl")

    assert [r.is_quarantined for r in rows] == [False, True, False]
    assert "nesting" in rows[1].quarantine_error
    assert rows[1].row["__line_number__"] == 2
    assert rows[2].row == {"id": 2}


def test_json_array_over_deep_file_is_quarantined(tmp_path: Path) -> None:
    path = tmp_path / "data.json"
    path.write_text('[{"id": 1}, {"x": ' + _DEEP + "}]")

    rows = _load(path, "json")

    assert len(rows) == 1
    assert rows[0].is_quarantined
    assert "nesting" in rows[0].quarantine_error


def test_nesting_at_the_bound_still_loads(tmp_path: Path) -> None:
    # The row object is level 1, so 63 further levels sit exactly at the 64 bound.
    inner = '{"a": ' * 63 + "1" + "}" * 63
    path = tmp_path / "data.jsonl"
    path.write_text('{"x": ' + inner + "}\n")

    rows = _load(path, "jsonl")

    assert [r.is_quarantined for r in rows] == [False]
