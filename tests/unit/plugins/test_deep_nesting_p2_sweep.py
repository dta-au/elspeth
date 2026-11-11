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

import pytest

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


# ── #68: blob_json_expand parse paths ───────────────────────────────────────


def _expand_field_text(parse_format: str, document: str) -> Any:
    from elspeth.plugins.transforms.blob_json_expand import BlobJSONExpand
    from elspeth.testing import make_pipeline_row
    from tests.fixtures.factories import make_context

    transform = BlobJSONExpand(
        {
            "schema": {"mode": "observed", "guaranteed_fields": ["content"]},
            "source": "field",
            "format": parse_format,
            "fields": ["document_id", "sections"],
        }
    )
    return _outcome(lambda: transform.process(make_pipeline_row({"content": document}), make_context()))


def _deep_record(depth: int) -> str:
    return '{"document_id": "d1", "sections": ' + _nested_list(depth) + "}"


_DEEP_DOCUMENTS = [
    pytest.param("json", "[" * UNPARSEABLY_DEEP, id="json-undecodable"),
    pytest.param("json", "[" + _deep_record(PARSEABLY_DEEP) + "]", id="json-deep-record"),
    pytest.param("jsonl", "[" * UNPARSEABLY_DEEP, id="jsonl-undecodable"),
    pytest.param("jsonl", '{"document_id": "d0", "sections": []}\n' + _deep_record(PARSEABLY_DEEP), id="jsonl-deep-record"),
]


@pytest.mark.parametrize(("parse_format", "document"), _DEEP_DOCUMENTS)
def test_blob_json_expand_routes_deep_json_to_on_error(parse_format: str, document: str) -> None:
    result = _expand_field_text(parse_format, document)

    assert not isinstance(result, Exception), f"process() raised {type(result).__name__} instead of returning an error result"
    assert result.status == "error"
    assert result.reason is not None
    assert result.reason["reason"] == "invalid_json"
    assert result.reason["error_type"] == "nesting_too_deep"
    assert result.retryable is False


@pytest.mark.parametrize("parse_format", ["json", "jsonl"])
def test_blob_json_expand_moderate_nesting_still_expands(parse_format: str) -> None:
    """Negative control: ordinary nested records are not refused by the bound."""
    record = _deep_record(5)
    document = "[" + record + "]" if parse_format == "json" else record

    result = _expand_field_text(parse_format, document)

    assert not isinstance(result, Exception)
    assert result.status == "success"


# ── #112: source-boundary non-canonical-number walker ───────────────────────


def _source_schema(mode: str) -> Any:
    from elspeth.contracts.schema import SchemaConfig
    from elspeth.plugins.infrastructure.schema_factory import create_schema_from_config

    config = {"mode": "observed"} if mode == "observed" else {"mode": "flexible", "fields": ["id: int"]}
    return create_schema_from_config(SchemaConfig.from_dict(config), "DeepRow", allow_coercion=True)


@pytest.mark.parametrize("mode", ["observed", "flexible"])
@pytest.mark.parametrize("depth", [100, PARSEABLY_DEEP])
def test_source_schema_rejects_deep_nesting_as_a_validation_error(mode: str, depth: int) -> None:
    from pydantic import ValidationError

    from elspeth.contracts.safe_validation_errors import safe_validation_error_text

    schema = _source_schema(mode)
    row = {"id": 1, "x": json.loads(_nested_list(depth))}

    outcome = _outcome(lambda: schema.model_validate(row))

    assert isinstance(outcome, ValidationError), f"model_validate returned/raised {type(outcome).__name__}, not a ValidationError"
    assert [error["type"] for error in outcome.errors()] == ["nesting_too_deep"]
    # The rendered, value-free reason names the rule rather than "custom".
    assert safe_validation_error_text(outcome, schema) == "1 validation error: <root>: [nesting_too_deep]"


def test_source_schema_still_admits_moderately_nested_rows() -> None:
    """Negative control: nesting within the bound still validates."""
    schema = _source_schema("observed")
    row = {"id": 1, "x": json.loads(_nested_list(10))}

    outcome = _outcome(lambda: schema.model_validate(row))

    assert not isinstance(outcome, Exception)
    assert outcome.to_row()["id"] == 1
