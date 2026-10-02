"""Configured source proof must agree with runtime field-shape rejection."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest

from elspeth.contracts.contexts import SourceContext
from elspeth.contracts.freeze import deep_thaw
from elspeth.plugins.infrastructure.config_base import PluginConfigError
from elspeth.plugins.sources.csv_source import CSVSource
from elspeth.plugins.sources.json_source import JSONSource
from elspeth.web.composer.source_demand import sample_header_for_source
from elspeth.web.composer.state import CompositionState, PipelineMetadata, SourceSpec
from elspeth.web.composer.tools._common import _source_options_for_prevalidation
from elspeth.web.composer.tools.generation import (
    ResolvedProofBlob,
    _compute_proof_diagnostics_for_source,
    _DeclaredInputTypeProofBudget,
)


def _proof_and_runtime(
    tmp_path: Path,
    *,
    content: bytes,
    plugin: str,
    schema: dict[str, Any],
    options: dict[str, Any] | None = None,
    filename: str | None = None,
    prefix_only: bool = False,
    load_runtime: bool = True,
) -> tuple[list[Any], list[Any]]:
    path = tmp_path / (filename or ("source.csv" if plugin == "csv" else "source.json"))
    path.write_bytes(content)
    source_options = {"path": str(path), "schema": schema, **(options or {})}
    source = SourceSpec(plugin=plugin, on_success="output", options=source_options, on_validation_failure="discard")
    state = CompositionState(source=source, nodes=(), edges=(), outputs=(), metadata=PipelineMetadata(), version=1)
    digest = hashlib.sha256(content).hexdigest()
    blob = ResolvedProofBlob(
        metadata={
            "id": "synthetic",
            "status": "ready",
            "filename": path.name,
            "mime_type": "text/csv" if plugin == "csv" else "application/json",
            "content_hash": digest,
            "size_bytes": len(content),
            "storage_path": str(path),
        },
        verified_prefix=content[:8192] if prefix_only else content,
        verified_content_hash=digest,
        total_size_bytes=len(content),
    )
    diagnostics = _compute_proof_diagnostics_for_source(
        state,
        source_name="source",
        source=source,
        blob_id="synthetic",
        blob_resolver=lambda _: blob,
        declared_input_type_budget=_DeclaredInputTypeProofBudget(),
    )
    source_type = CSVSource if plugin == "csv" else JSONSource
    rows = []
    if load_runtime:
        instance = source_type({**deep_thaw(_source_options_for_prevalidation(source_options)), "on_validation_failure": "discard"})
        rows = list(instance.load(Mock(spec=SourceContext)))
    return diagnostics, rows


@pytest.mark.parametrize(
    ("content", "fields", "options", "blocked", "row_count"),
    [
        (b"a,b\n1,2\n", ["a: str", "b: str"], {}, False, 1),
        (b"x\n1\n", ["a: str", "b: str"], {}, True, 0),
        (b"a\n1\n", ["a: str", "b: str"], {}, True, 0),
        (b"a\n1\n", ["a: str", "b: str?"], {}, False, 1),
        (b"1,2\n", ["a: str", "b: str"], {"columns": ["a", "b"]}, False, 1),
        (b"1,2\n", ["a: str", "b: str"], {"columns": ["A", "b"], "field_mapping": {"A": "a"}}, False, 1),
        (b"External A\n1\n", ["a: str", "b: str"], {"field_mapping": {"external_a": "a"}}, True, 0),
    ],
    ids=["complete", "none", "partial", "optional", "explicit-columns", "mapped-columns", "partial-mapped"],
)
def test_csv_required_shape_proof_matches_runtime(
    tmp_path: Path, content: bytes, fields: list[str], options: dict[str, Any], blocked: bool, row_count: int
) -> None:
    diagnostics, rows = _proof_and_runtime(
        tmp_path, content=content, plugin="csv", schema={"mode": "flexible", "fields": fields}, options=options
    )
    assert len(rows) == row_count
    assert any(item["severity"] == "blocking" for item in diagnostics) is blocked


@pytest.mark.parametrize("records", [[{"id": 1}, {"id": 2, "extra": 3}], [{"id": 2, "extra": 3}, {"id": 1}]])
@pytest.mark.parametrize("format", ["json", "jsonl"])
def test_json_sparse_extras_warn_without_claiming_universal_loss(tmp_path: Path, records: list[Any], format: str) -> None:
    content = (json.dumps(records) if format == "json" else "\n".join(json.dumps(row) for row in records)).encode()
    diagnostics, rows = _proof_and_runtime(
        tmp_path, content=content, plugin="json", schema={"mode": "fixed", "fields": ["id: int"]}, options={"format": format}
    )
    assert [row.row["id"] for row in rows] == [1]
    assert not any(item["severity"] == "blocking" for item in diagnostics)
    assert any(item["severity"] == "warning" for item in diagnostics)


@pytest.mark.parametrize("format", ["json", "jsonl"])
def test_json_every_row_has_undeclared_keys_blocks_with_complete_evidence(tmp_path: Path, format: str) -> None:
    records = [{"id": 1, "extra_a": 2}, {"id": 2, "extra_b": 3}]
    content = (json.dumps(records) if format == "json" else "\n".join(json.dumps(row) for row in records)).encode()
    diagnostics, rows = _proof_and_runtime(
        tmp_path, content=content, plugin="json", schema={"mode": "fixed", "fields": ["id: int"]}, options={"format": format}
    )
    assert rows == []
    assert any(item["severity"] == "blocking" for item in diagnostics)


@pytest.mark.parametrize("metadata_first", [True, False])
def test_json_proof_selects_configured_data_key_independently_of_object_order(tmp_path: Path, metadata_first: bool) -> None:
    pairs = [("metadata", [{"version": 1}]), ("records", [{"External ID": 5}])]
    content = json.dumps(dict(pairs if metadata_first else reversed(pairs))).encode()
    diagnostics, rows = _proof_and_runtime(
        tmp_path,
        content=content,
        plugin="json",
        schema={"mode": "fixed", "fields": ["id: int"]},
        options={"data_key": "records", "field_mapping": {"external_id": "id"}},
    )
    assert [row.row for row in rows] == [{"id": 5}]
    assert not any(item["severity"] == "blocking" for item in diagnostics)


@pytest.mark.parametrize("format", ["json", "jsonl"])
def test_json_proof_applies_mapping_when_key_first_appears_in_a_later_row(tmp_path: Path, format: str) -> None:
    records = [{"a": 1}, {"External ID": 2}]
    content = (json.dumps(records) if format == "json" else "\n".join(json.dumps(row) for row in records)).encode()
    diagnostics, rows = _proof_and_runtime(
        tmp_path,
        content=content,
        plugin="json",
        schema={"mode": "fixed", "fields": ["a: int?", "id: int?"]},
        options={"format": format, "field_mapping": {"external_id": "id"}},
    )
    assert len(rows) == 2
    assert rows[1].row["id"] == 2
    assert not any(item["severity"] == "blocking" for item in diagnostics)


@pytest.mark.parametrize("format", ["json", "jsonl"])
def test_json_row_sampling_does_not_prove_whole_artifact_loss(tmp_path: Path, format: str) -> None:
    records = [{"id": index, "extra": 1} for index in range(100)] + [{"id": 100}]
    content = (json.dumps(records) if format == "json" else "\n".join(json.dumps(row) for row in records)).encode()
    diagnostics, rows = _proof_and_runtime(
        tmp_path, content=content, plugin="json", schema={"mode": "fixed", "fields": ["id: int"]}, options={"format": format}
    )
    assert [row.row for row in rows] == [{"id": 100}]
    assert not any(item["severity"] == "blocking" for item in diagnostics)


def test_csv_prefix_cut_in_header_does_not_prove_required_fields_absent(tmp_path: Path) -> None:
    content = b"a," + b"x" * 9000 + b"\n1,2\n"
    diagnostics, _rows = _proof_and_runtime(
        tmp_path, content=content, plugin="csv", schema={"mode": "flexible", "fields": ["a: str", "b: str"]}, prefix_only=True
    )
    assert not any(item["severity"] == "blocking" for item in diagnostics)


@pytest.mark.parametrize("encoding", ["utf-8-sig", "utf-16", "utf-16-le", "utf-16-be", "latin-1"])
def test_csv_configured_encoding_uses_actual_header(tmp_path: Path, encoding: str) -> None:
    diagnostics, rows = _proof_and_runtime(
        tmp_path,
        content="a\n1\n".encode(encoding),
        plugin="csv",
        schema={"mode": "flexible", "fields": ["a: str", "b: str"]},
        options={"encoding": encoding},
    )
    assert rows == []
    assert any(item["severity"] == "blocking" for item in diagnostics)


def test_csv_valid_header_still_proves_absence_when_data_sample_is_clipped(tmp_path: Path) -> None:
    diagnostics, rows = _proof_and_runtime(
        tmp_path,
        content=b'a\n"' + b"x" * 9000 + b'"\n',
        plugin="csv",
        schema={"mode": "flexible", "fields": ["a: str", "b: str"]},
        prefix_only=True,
    )
    assert rows == []
    assert any(item["severity"] == "blocking" for item in diagnostics)


@pytest.mark.parametrize("format", ["json", "jsonl"])
def test_json_byte_sampling_does_not_prove_whole_artifact_loss(tmp_path: Path, format: str) -> None:
    records = [{"id": 1, "extra": "x" * 9000}, {"id": 2}]
    content = (json.dumps(records) if format == "json" else "\n".join(json.dumps(row) for row in records)).encode()
    diagnostics, rows = _proof_and_runtime(
        tmp_path,
        content=content,
        plugin="json",
        schema={"mode": "fixed", "fields": ["id: int"]},
        options={"format": format},
        prefix_only=True,
    )
    assert [row.row for row in rows] == [{"id": 2}]
    assert not any(item["severity"] == "blocking" for item in diagnostics)


@pytest.mark.parametrize("prefix", [b"null\n", b"not-json\n", b'{"id":1,"extra":"\xff"}\n'])
def test_jsonl_invalid_records_do_not_become_extra_field_universals(tmp_path: Path, prefix: bytes) -> None:
    diagnostics, rows = _proof_and_runtime(
        tmp_path,
        content=prefix + b'{"id":2,"extra":3}\n',
        plugin="json",
        schema={"mode": "fixed", "fields": ["id: int"]},
        options={"format": "jsonl"},
    )
    assert rows == []
    assert not any(item["severity"] == "blocking" for item in diagnostics)


@pytest.mark.parametrize("format", ["json", "jsonl"])
def test_json_configured_encoding_and_mapping(tmp_path: Path, format: str) -> None:
    records = [{"External ID": 3, "comment": "café"}]
    content = (json.dumps(records, ensure_ascii=False) if format == "json" else json.dumps(records[0], ensure_ascii=False)).encode(
        "latin-1"
    )
    diagnostics, rows = _proof_and_runtime(
        tmp_path,
        content=content,
        plugin="json",
        schema={"mode": "fixed", "fields": ["id: int", "comment: str"]},
        options={"format": format, "encoding": "latin-1", "field_mapping": {"external_id": "id"}},
    )
    assert [row.row for row in rows] == [{"id": 3, "comment": "café"}]
    assert not any(item["severity"] == "blocking" for item in diagnostics)


@pytest.mark.parametrize(
    ("content", "options", "expected"),
    [
        (b"Case Study_\nvalue\n", {}, ("case_study",)),
        (b"Case Study_\nvalue\n", {"field_mapping": {"case_study": "study"}}, ("study",)),
        (b"preamble\nCase Study_\nvalue\n", {"skip_rows": 1}, ("case_study",)),
        (b"value\n", {"columns": ["study"]}, ("study",)),
        (b"a," + b"x" * 9000 + b"\n1,2\n", {}, None),
    ],
)
def test_review_sample_uses_certified_configured_csv_fields(
    tmp_path: Path, content: bytes, options: dict[str, Any], expected: tuple[str, ...] | None
) -> None:
    path = tmp_path / "sample.csv"
    path.write_bytes(content)
    source = SourceSpec(
        plugin="csv",
        on_success="output",
        on_validation_failure="discard",
        options={"path": str(path), "schema": {"mode": "observed"}, **options},
    )
    assert sample_header_for_source(source) == expected


def test_json_proof_consumes_uploaded_source_authoring_metadata(tmp_path: Path) -> None:
    diagnostics, rows = _proof_and_runtime(
        tmp_path,
        content=b'[{"External ID":3}]',
        plugin="json",
        schema={"mode": "fixed", "fields": ["id: int"]},
        options={"blob_ref": "synthetic", "source_authoring": {"origin": "uploaded"}, "field_mapping": {"external_id": "id"}},
    )
    assert [row.row for row in rows] == [{"id": 3}]
    assert not any(item["severity"] == "blocking" for item in diagnostics)


@pytest.mark.parametrize("records", [[{"Case Study": 1}, {"case_study": 2}], [{"case_study": 2}, {"Case Study": 1}]])
@pytest.mark.parametrize("format", ["json", "jsonl"])
def test_json_proof_preserves_cross_record_resolution_collisions(tmp_path: Path, records: list[Any], format: str) -> None:
    content = (json.dumps(records) if format == "json" else "\n".join(json.dumps(row) for row in records)).encode()
    diagnostics, rows = _proof_and_runtime(
        tmp_path,
        content=content,
        plugin="json",
        schema={"mode": "fixed", "fields": ["case_study: int"]},
        options={"format": format},
    )
    assert len(rows) == 1
    assert not any(item["severity"] == "blocking" for item in diagnostics)
    assert any("configured_json_field_resolution_failed" in item["message"] for item in diagnostics)


def test_json_configured_encoding_is_material_to_selected_fields(tmp_path: Path) -> None:
    diagnostics, rows = _proof_and_runtime(
        tmp_path,
        content='[{"café":3}]'.encode("latin-1"),
        plugin="json",
        schema={"mode": "fixed", "fields": ["id: int"]},
        options={"encoding": "latin-1", "field_mapping": {"café": "id"}},
    )
    assert [row.row for row in rows] == [{"id": 3}]
    assert not any(item["severity"] == "blocking" for item in diagnostics)


def test_csv_fixed_explicit_columns_proves_uniform_extra_fields(tmp_path: Path) -> None:
    diagnostics, rows = _proof_and_runtime(
        tmp_path,
        content=b"1,2\n",
        plugin="csv",
        schema={"mode": "fixed", "fields": ["a: str"]},
        options={"columns": ["a", "b"]},
    )
    assert rows == []
    assert any(item["severity"] == "blocking" for item in diagnostics)


@pytest.mark.parametrize("encoding", ["utf-16-le", "utf-16-be"])
def test_csv_configured_encoding_does_not_invent_header_mismatch(tmp_path: Path, encoding: str) -> None:
    diagnostics, rows = _proof_and_runtime(
        tmp_path,
        content="ab\nvalue\n".encode(encoding),
        plugin="csv",
        schema={"mode": "flexible", "fields": ["ab: str"]},
        options={"encoding": encoding},
    )
    assert [row.row for row in rows] == [{"ab": "value"}]
    assert not any(item["severity"] == "blocking" for item in diagnostics)


def test_csv_non_utf8_header_certifies_missing_required_field(tmp_path: Path) -> None:
    diagnostics, rows = _proof_and_runtime(
        tmp_path,
        content="café\nvalue\n".encode("latin-1"),
        plugin="csv",
        schema={"mode": "flexible", "fields": ["café: str", "b: str"]},
        options={"encoding": "latin-1"},
    )
    assert rows == []
    assert any(item["severity"] == "blocking" for item in diagnostics)


def test_csv_consumer_required_fields_do_not_invent_source_row_rejection(tmp_path: Path) -> None:
    diagnostics, rows = _proof_and_runtime(
        tmp_path,
        content=b"a\n1\n",
        plugin="csv",
        schema={"mode": "flexible", "fields": ["a: str", "b: str?"], "required_fields": ["b"]},
    )
    assert [row.row for row in rows] == [{"a": "1", "b": None}]
    assert not any(item["severity"] == "blocking" for item in diagnostics)


@pytest.mark.parametrize("format", ["json", "jsonl"])
def test_json_consumer_required_fields_do_not_invent_source_row_rejection(tmp_path: Path, format: str) -> None:
    diagnostics, rows = _proof_and_runtime(
        tmp_path,
        content=b'[{"a":1}]' if format == "json" else b'{"a":1}\n',
        plugin="json",
        schema={"mode": "fixed", "fields": ["a: int", "b: int?"], "required_fields": ["b"]},
        options={"format": format},
    )
    assert [row.row for row in rows] == [{"a": 1, "b": None}]
    assert not any(item["severity"] == "blocking" for item in diagnostics)


@pytest.mark.parametrize("encoding", ["utf-8", "base64_codec", "rot_13"])
def test_json_proof_quarantines_known_nontext_codec_decode_failure(tmp_path: Path, encoding: str) -> None:
    diagnostics, rows = _proof_and_runtime(
        tmp_path,
        content=b'[{"id":1}]',
        plugin="json",
        schema={"mode": "fixed", "fields": ["id: int"]},
        options={"encoding": encoding},
        load_runtime=encoding == "utf-8",
    )
    assert any(item["severity"] == "blocking" for item in diagnostics) is (encoding != "utf-8")
    if encoding == "utf-8":
        assert [row.row for row in rows] == [{"id": 1}]
    else:
        # The runtime constructor currently accepts registry-known nontext
        # codecs; file decoding refuses them. Proof must return repair feedback.
        runtime = JSONSource(
            {
                "path": str(tmp_path / "source.json"),
                "schema": {"mode": "observed"},
                "encoding": encoding,
                "on_validation_failure": "discard",
            }
        )
        with pytest.raises(LookupError):
            list(runtime.load(Mock(spec=SourceContext)))


@pytest.mark.parametrize("plugin", ["csv", "json"])
def test_source_proof_reports_unknown_codec_as_configuration_rejection(tmp_path: Path, plugin: str) -> None:
    diagnostics, _rows = _proof_and_runtime(
        tmp_path,
        content=b"id\n1\n" if plugin == "csv" else b'[{"id":1}]',
        plugin=plugin,
        schema={"mode": "fixed", "fields": ["id: int"]},
        options={"encoding": "definitely-not-a-codec"},
        load_runtime=False,
    )
    assert any(item["severity"] == "blocking" for item in diagnostics)
    runtime_type = CSVSource if plugin == "csv" else JSONSource
    with pytest.raises(PluginConfigError):
        runtime_type(
            {
                "path": str(tmp_path / ("source.csv" if plugin == "csv" else "source.json")),
                "schema": {"mode": "observed"},
                "encoding": "definitely-not-a-codec",
                "on_validation_failure": "discard",
            }
        )


@pytest.mark.parametrize("encoding", ["base64_codec", "rot_13"])
def test_csv_proof_already_reports_known_nontext_codec_failure(tmp_path: Path, encoding: str) -> None:
    diagnostics, _rows = _proof_and_runtime(
        tmp_path,
        content=b"id\n1\n",
        plugin="csv",
        schema={"mode": "fixed", "fields": ["id: int"]},
        options={"encoding": encoding},
        load_runtime=False,
    )
    assert any(item["severity"] == "blocking" for item in diagnostics)
    runtime = CSVSource(
        {"path": str(tmp_path / "source.csv"), "schema": {"mode": "observed"}, "encoding": encoding, "on_validation_failure": "discard"}
    )
    with pytest.raises(LookupError):
        list(runtime.load(Mock(spec=SourceContext)))
