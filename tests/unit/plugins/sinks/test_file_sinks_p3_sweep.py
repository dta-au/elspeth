"""Regression proofs for the P3 file-sink sweep findings (20261111-plugins-review)."""

from __future__ import annotations

import csv
from pathlib import Path

import pytest

from elspeth.plugins.infrastructure.config_base import PluginConfigError
from elspeth.plugins.sinks.csv_sink import CSVSink, CSVSinkConfig
from tests.fixtures.base_classes import inject_write_failure
from tests.unit.plugins.sinks.test_local_file_sink_effects import _prepare, _stage_path


def _csv_config_error(**options: object) -> str | None:
    """Return the config-validation message, or None when CSVSinkConfig admits the options."""
    try:
        CSVSinkConfig.from_dict({"path": "out.csv", "schema": {"mode": "observed"}, **options}, plugin_name="csv")
    except PluginConfigError as exc:
        return str(exc)
    return None


# --- Finding #25: delimiters csv.writer rejects must fail config validation ---


@pytest.mark.parametrize("delimiter", ['"', "\n", "\r"])
def test_csv_sink_config_rejects_delimiters_csv_writer_cannot_use(delimiter: str) -> None:
    error = _csv_config_error(delimiter=delimiter)
    assert error is not None, f"CSVSinkConfig admitted delimiter {delimiter!r}, which csv.writer rejects at publication"
    assert "delimiter" in error


@pytest.mark.parametrize("delimiter", [",", ";", "\t", "|"])
def test_csv_sink_config_keeps_accepting_usable_delimiters(delimiter: str) -> None:
    assert _csv_config_error(delimiter=delimiter) is None


# --- Finding #26: non-text codecs must fail config validation ---


@pytest.mark.parametrize("encoding", ["base64", "hex", "rot13", "zlib"])
def test_csv_sink_config_rejects_non_text_codecs(encoding: str) -> None:
    error = _csv_config_error(encoding=encoding)
    assert error is not None, f"CSVSinkConfig admitted non-text codec {encoding!r}, which crashes at publication"
    assert "encoding" in error


@pytest.mark.parametrize("encoding", ["utf-8", "utf-8-sig", "latin-1", "cp1252", "utf-16"])
def test_csv_sink_config_keeps_accepting_text_encodings(encoding: str) -> None:
    assert _csv_config_error(encoding=encoding) is None


# --- Finding #27: opt-in spreadsheet formula neutralisation for pipeline rows ---

_FORMULA_ROWS = [
    {"id": 1, "cell": '=HYPERLINK("http://evil","x")'},
    {"id": 2, "cell": "+1+1"},
    {"id": 3, "cell": "-2"},
    {"id": 4, "cell": "@SUM(A1)"},
    {"id": 5, "cell": "\tTAB"},
    {"id": 6, "cell": "plain"},
    {"id": -7, "cell": "a=b"},
]


def _written_csv_rows(tmp_path: Path, name: str, effect_id: str, **options: object) -> list[dict[str, str]]:
    sink = inject_write_failure(CSVSink({"path": str(tmp_path / name), "schema": {"mode": "observed"}, **options}))
    plan = _prepare(sink, effect_id=effect_id, rows=_FORMULA_ROWS)
    with _stage_path(plan).open(newline="", encoding="utf-8") as stream:
        return list(csv.DictReader(stream))


def test_csv_sink_neutralize_formulas_prefixes_formula_cells(tmp_path: Path) -> None:
    assert "neutralize_formulas" in CSVSinkConfig.model_fields, "CSVSinkConfig has no opt-in formula-neutralisation option"
    assert CSVSinkConfig.model_fields["neutralize_formulas"].default is False

    rows = _written_csv_rows(tmp_path, "neutral.csv", "f1" * 32, neutralize_formulas=True)

    assert [row["cell"] for row in rows] == [
        '\'=HYPERLINK("http://evil","x")',
        "'+1+1",
        "'-2",
        "'@SUM(A1)",
        "'\tTAB",
        "plain",
        "a=b",
    ]
    # Non-string values are numbers, not formula text: the negative int is untouched.
    assert rows[-1]["id"] == "-7"


def test_csv_sink_default_output_is_verbatim(tmp_path: Path) -> None:
    rows = _written_csv_rows(tmp_path, "verbatim.csv", "f2" * 32)

    assert [row["cell"] for row in rows] == [row["cell"] for row in _FORMULA_ROWS]
