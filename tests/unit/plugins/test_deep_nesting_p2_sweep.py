"""Deeply nested Tier-3 JSON is quarantined or routed, never a run crash.

Sweep 20261111-plugins-review, findings #18, #68, #112 and #138. Each parser
or walker below met external JSON nested far deeper than the interpreter's
recursion limit and let ``RecursionError`` (an engine-crash class, see
``engine/error_boundary.py``) escape instead of recording the input as a
value-level failure.

Every call that may raise goes through ``_outcome`` so that, on an unfixed
tree, the test fails at an assertion naming the escaped exception rather than
erroring out of the test body.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any
from unittest.mock import patch

from elspeth.contracts.plugin_context import PluginContext
from tests.fixtures.factories import make_operation_context

# Deep enough that CPython's C decoder raises RecursionError on Python 3.14.
UNPARSEABLY_DEEP = 100_000
# json.loads accepts this on 3.14, but Python-level walkers of the result
# (schema validation, hashing, freezing) exceed the recursion limit.
PARSEABLY_DEEP = 1_500


def _nested_list(depth: int) -> str:
    return "[" * depth + "]" * depth


def _outcome(call: Callable[[], Any]) -> Any:
    """Return the call's result, or the exception it raised."""
    try:
        return call()
    except Exception as exc:  # The tests assert on the type of what escaped.
        return exc


# ── #18: azure_blob JSON / JSONL parsing ────────────────────────────────────

_AZURE_PATCH_AUTH = "elspeth.plugins.infrastructure.azure_auth.AzureAuthConfig.create_blob_service_client"
_FAKE_CONN_STRING = (
    "DefaultEndpointsProtocol=https;AccountName=fake;AccountKey=ZmFrZQ==;EndpointSuffix=core.windows.net"  # secret-scan: allow-this-line
)


class _BlobDownload:
    def __init__(self, data: bytes) -> None:
        self._data = data
        self._offset = 0

    def read(self, size: int) -> bytes:
        chunk = self._data[self._offset : self._offset + size]
        self._offset += len(chunk)
        return chunk


class _BlobClient:
    def __init__(self, data: bytes) -> None:
        self._data = data

    def download_blob(self) -> _BlobDownload:
        return _BlobDownload(self._data)

    def close(self) -> None:
        return None


class _ContainerClient:
    def __init__(self, data: bytes) -> None:
        self._data = data

    def get_blob_client(self, _blob_path: str) -> _BlobClient:
        return _BlobClient(self._data)


class _ServiceClient:
    def __init__(self, data: bytes) -> None:
        self._data = data

    def get_container_client(self, _container: str) -> _ContainerClient:
        return _ContainerClient(self._data)


def _azure_rows(blob_format: str, blob_bytes: bytes, ctx: PluginContext) -> Any:
    from elspeth.plugins.sources.azure_blob_source import AzureBlobSource

    source = AzureBlobSource(
        {
            "connection_string": _FAKE_CONN_STRING,
            "container": "test-container",
            "blob_path": "data/input.json",
            "schema": {"mode": "observed"},
            "on_validation_failure": "quarantine",
            "format": blob_format,
        }
    )
    with patch(_AZURE_PATCH_AUTH, return_value=_ServiceClient(blob_bytes)):
        return _outcome(lambda: list(source.load(ctx)))


def test_azure_blob_json_array_too_deep_to_decode_is_quarantined_at_file_level() -> None:
    ctx = make_operation_context(plugin_name="azure_blob")
    blob = ('[{"id": 1, "x": ' + _nested_list(UNPARSEABLY_DEEP) + "}]").encode()

    rows = _azure_rows("json", blob, ctx)

    assert isinstance(rows, list), f"load() raised {type(rows).__name__} instead of quarantining the blob"
    assert len(rows) == 1
    assert rows[0].is_quarantined is True
    assert "nesting" in rows[0].quarantine_error


def test_azure_blob_json_array_with_a_deep_row_is_quarantined_not_a_crash() -> None:
    ctx = make_operation_context(plugin_name="azure_blob")
    blob = ('[{"id": 1, "x": ' + _nested_list(PARSEABLY_DEEP) + "}]").encode()

    rows = _azure_rows("json", blob, ctx)

    assert isinstance(rows, list), f"load() raised {type(rows).__name__} instead of quarantining the blob"
    assert [row.is_quarantined for row in rows] == [True]


def test_azure_blob_jsonl_deep_lines_are_quarantined_and_later_rows_survive() -> None:
    ctx = make_operation_context(plugin_name="azure_blob")
    lines = [
        '{"id": 1, "name": "before"}',
        '{"id": 2, "x": ' + _nested_list(UNPARSEABLY_DEEP) + "}",
        '{"id": 3, "x": ' + _nested_list(PARSEABLY_DEEP) + "}",
        '{"id": 4, "name": "after"}',
    ]
    blob = ("\n".join(lines) + "\n").encode()

    rows = _azure_rows("jsonl", blob, ctx)

    assert isinstance(rows, list), f"load() raised {type(rows).__name__} instead of quarantining the line"
    assert [row.is_quarantined for row in rows] == [False, True, True, False]
    assert rows[0].row == {"id": 1, "name": "before"}
    assert rows[3].row == {"id": 4, "name": "after"}
    assert all("nesting" in row.quarantine_error for row in rows[1:3])
    assert [row.row["__line_number__"] for row in rows[1:3]] == ["2", "3"]


def test_azure_blob_jsonl_moderate_nesting_still_loads() -> None:
    """Negative control: the bound must not refuse ordinary nested records."""
    ctx = make_operation_context(plugin_name="azure_blob")
    blob = (json.dumps({"id": 1, "x": {"y": [[1, 2], {"z": [3]}]}}) + "\n").encode()

    rows = _azure_rows("jsonl", blob, ctx)

    assert isinstance(rows, list)
    assert [row.is_quarantined for row in rows] == [False]
