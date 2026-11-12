"""Regression proofs for the P3 file-sink sweep findings (20261111-plugins-review)."""

from __future__ import annotations

import pytest

from elspeth.plugins.infrastructure.config_base import PluginConfigError
from elspeth.plugins.sinks.csv_sink import CSVSinkConfig


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
