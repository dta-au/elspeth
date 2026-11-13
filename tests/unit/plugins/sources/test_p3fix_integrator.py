"""CSVSource refuses non-text codecs at config, like CSVSink (#26).

#80 (P2 json/csv lane) moved CSVSource from a text-mode ``open()`` to binary
reads decoded by ``codecs.getincrementaldecoder(encoding)``. Text-mode ``open()``
refused bytes-to-bytes and str-to-str codecs (base64, hex, zlib, rot13) with a
``LookupError`` before any row was read; the incremental decoder accepts them and
then crashes mid-load with ``binascii.Error`` (base64) or ``TypeError`` (rot13),
raw exceptions that are neither a row-level quarantine nor a config error.

``codecs.lookup()`` resolves those codecs, so the config validator let them
through. The fix applies the same text-encoding check CSVSinkConfig uses, so the
codec is refused before a source is ever built.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from elspeth.plugins.infrastructure.config_base import PluginConfigError
from elspeth.plugins.sources.csv_source import CSVSource


def _csv_source_config(tmp_path: Path, encoding: str) -> dict[str, object]:
    path = tmp_path / "source.csv"
    path.write_bytes(b"id\n1\n")
    return {"path": str(path), "schema": {"mode": "observed"}, "encoding": encoding, "on_validation_failure": "discard"}


@pytest.mark.parametrize("encoding", ["base64_codec", "hex", "zlib_codec", "rot_13"])
def test_csv_source_refuses_a_non_text_codec_at_config(tmp_path: Path, encoding: str) -> None:
    refusal: PluginConfigError | None = None
    try:
        CSVSource(_csv_source_config(tmp_path, encoding))
    except PluginConfigError as exc:
        refusal = exc

    assert refusal is not None, f"CSVSource accepted the non-text codec {encoding!r}; it must be refused at config"
    assert "not a text encoding" in str(refusal)


@pytest.mark.parametrize("encoding", ["utf-8", "utf-8-sig", "utf-16", "cp1252", "latin-1"])
def test_csv_source_still_accepts_text_encodings(tmp_path: Path, encoding: str) -> None:
    source = CSVSource(_csv_source_config(tmp_path, encoding))

    assert source._encoding == encoding
