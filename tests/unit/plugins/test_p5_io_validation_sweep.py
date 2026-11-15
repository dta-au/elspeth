"""P5 io-validation sweep: one authority for codecs, CSV delimiters and text decoding.

Maintainer ruling 2026-11-14: a defect fixed in one plugin is fixed in every
sibling. CSVSink validated its delimiter by building a csv.writer and its codec
by probing ``"".encode(v)``; every other plugin with a CSV ``delimiter`` or a
text ``encoding`` must refuse the same values at config load, through ONE shared
validator, instead of crashing on the first row or at publication.
"""

from __future__ import annotations

import csv
import importlib.util
import io
import typing
from collections.abc import Iterator
from typing import Any, Literal

import pytest
from pydantic import BaseModel, ValidationError, create_model
from pydantic.fields import FieldInfo

from elspeth.plugins.infrastructure import config_base
from elspeth.plugins.infrastructure.discovery import discover_all_plugins

_OBSERVED: dict[str, Any] = {"mode": "observed"}
_NON_TEXT_CODECS = ("base64", "rot13", "hex", "zlib")
_BAD_CSV_DELIMITERS = ('"', "\n", "\r", ",,", "")

# blob_text_expand's ``delimiter`` is a free-form split separator for
# ``str.split``, not a CSV dialect delimiter: multi-character values are its
# documented contract. It is the only ``delimiter`` field that is not CSV.
_NOT_CSV_DELIMITER_FIELDS = frozenset({("BlobTextExpandConfig", "delimiter")})


# ---------------------------------------------------------------------------
# The shared authority exists.
# ---------------------------------------------------------------------------


def test_shared_text_encoding_and_delimiter_authority_exists() -> None:
    namespace = vars(config_base)
    assert "validate_text_encoding" in namespace
    assert "validate_csv_delimiter" in namespace
    assert "TextEncoding" in namespace
    assert "CSVDelimiter" in namespace
    assert importlib.util.find_spec("elspeth.plugins.infrastructure.text_decoding") is not None


# ---------------------------------------------------------------------------
# Whole-tree: every plugin config field named ``encoding`` / ``delimiter``
# (nested option models included) carries the shared validation in its TYPE,
# so it cannot drift from the runtime consumer.
# ---------------------------------------------------------------------------


def _nested_models(annotation: object) -> Iterator[type[BaseModel]]:
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        yield annotation
        return
    for arg in typing.get_args(annotation):
        yield from _nested_models(arg)


def _walk_config_fields(model: type[BaseModel], seen: set[type[BaseModel]]) -> Iterator[tuple[type[BaseModel], str, Any]]:
    if model in seen:
        return
    seen.add(model)
    for name, info in model.model_fields.items():
        yield model, name, info
        for nested in _nested_models(info.annotation):
            yield from _walk_config_fields(nested, seen)


def _plugin_config_fields() -> list[tuple[str, str, Any]]:
    found: list[tuple[str, str, Any]] = []
    seen: set[type[BaseModel]] = set()
    for classes in discover_all_plugins().values():
        for plugin_cls in classes:
            config_model = plugin_cls.get_config_model()
            if config_model is None:
                continue
            for model, name, info in _walk_config_fields(config_model, seen):
                if name in {"encoding", "delimiter"}:
                    found.append((model.__name__, name, info))
    return found


class _FieldProbe:
    """Validate one value against a field's TYPE (annotation + its metadata) alone.

    Field-level ``@field_validator`` methods are deliberately NOT carried: the
    check lives in the shared annotated type or it does not count.
    """

    def __init__(self, info: FieldInfo) -> None:
        self._model = create_model("FieldProbe", value=(info.annotation, FieldInfo.merge_field_infos(info, default=...)))

    def validate_python(self, value: object) -> object:
        return self._model.model_validate({"value": value}).model_dump()["value"]


def _field_type_adapter(info: FieldInfo) -> _FieldProbe:
    return _FieldProbe(info)


_FIELDS = _plugin_config_fields()


def test_tree_walk_finds_the_known_encoding_and_delimiter_sites() -> None:
    """Positive control: the walker reaches nested option models."""
    found = {(model, name) for model, name, _ in _FIELDS}
    assert ("CSVSourceConfig", "delimiter") in found
    assert ("TextSourceConfig", "encoding") in found
    assert ("CSVOptions", "delimiter") in found
    assert ("JSONOptions", "encoding") in found
    assert ("CSVWriteOptions", "delimiter") in found
    assert ("BlobJSONExpandConfig", "encoding") in found
    assert ("BlobTextExpandConfig", "delimiter") in found


@pytest.mark.parametrize(
    ("model_name", "field_name", "info"), [pytest.param(*f, id=f"{f[0]}.{f[1]}") for f in _FIELDS if f[1] == "encoding"]
)
def test_every_encoding_field_type_refuses_non_text_codecs(model_name: str, field_name: str, info: Any) -> None:
    adapter = _field_type_adapter(info)
    assert adapter.validate_python("utf-8") == "utf-8"
    for codec in _NON_TEXT_CODECS:
        with pytest.raises(ValidationError):
            adapter.validate_python(codec)
    with pytest.raises(ValidationError):
        adapter.validate_python("definitely-not-a-codec")


@pytest.mark.parametrize(
    ("model_name", "field_name", "info"),
    [pytest.param(*f, id=f"{f[0]}.{f[1]}") for f in _FIELDS if f[1] == "delimiter" and (f[0], f[1]) not in _NOT_CSV_DELIMITER_FIELDS],
)
def test_every_csv_delimiter_field_type_refuses_what_the_csv_module_refuses(model_name: str, field_name: str, info: Any) -> None:
    adapter = _field_type_adapter(info)
    assert adapter.validate_python("|") == "|"
    for delimiter in _BAD_CSV_DELIMITERS:
        with pytest.raises(ValidationError):
            adapter.validate_python(delimiter)


# ---------------------------------------------------------------------------
# Behavioural: the named sites refuse at config load, with one message.
# ---------------------------------------------------------------------------


def _config_cases() -> list[Any]:
    from elspeth.plugins.sinks import aws_s3_sink, azure_blob_sink
    from elspeth.plugins.sinks.csv_sink import CSVSinkConfig
    from elspeth.plugins.sinks.json_sink import JSONSinkConfig
    from elspeth.plugins.sources import aws_s3_source, azure_blob_source
    from elspeth.plugins.sources.csv_source import CSVSourceConfig
    from elspeth.plugins.sources.json_source import JSONSourceConfig
    from elspeth.plugins.sources.text_source import TextSourceConfig
    from elspeth.plugins.transforms.blob_csv_expand import BlobCSVExpandConfig
    from elspeth.plugins.transforms.blob_json_expand import BlobJSONExpandConfig
    from elspeth.plugins.transforms.blob_text_expand import BlobTextExpandConfig

    source = {"path": "in.dat", "schema": _OBSERVED, "on_validation_failure": "discard"}
    return [
        pytest.param(CSVSourceConfig, source, id="csv_source"),
        pytest.param(TextSourceConfig, {**source, "column": "line"}, id="text_source"),
        pytest.param(JSONSourceConfig, source, id="json_source"),
        pytest.param(azure_blob_source.CSVOptions, {}, id="azure_blob_source.csv"),
        pytest.param(azure_blob_source.JSONOptions, {}, id="azure_blob_source.json"),
        pytest.param(aws_s3_source.CSVOptions, {}, id="aws_s3_source.csv"),
        pytest.param(aws_s3_source.JSONOptions, {}, id="aws_s3_source.json"),
        pytest.param(azure_blob_sink.CSVWriteOptions, {}, id="azure_blob_sink.csv"),
        pytest.param(aws_s3_sink.CSVWriteOptions, {}, id="aws_s3_sink.csv"),
        pytest.param(CSVSinkConfig, {"path": "out.csv", "schema": _OBSERVED}, id="csv_sink"),
        pytest.param(JSONSinkConfig, {"path": "out.json", "schema": _OBSERVED}, id="json_sink"),
        pytest.param(BlobCSVExpandConfig, {"schema": _OBSERVED, "blob_ref_field": "blob_ref"}, id="blob_csv_expand"),
        pytest.param(BlobJSONExpandConfig, {"schema": _OBSERVED, "blob_ref_field": "blob_ref", "fields": ["a"]}, id="blob_json_expand"),
        pytest.param(BlobTextExpandConfig, {"schema": _OBSERVED, "blob_ref_field": "blob_ref"}, id="blob_text_expand"),
    ]


@pytest.mark.parametrize(("model", "baseline"), _config_cases())
@pytest.mark.parametrize("codec", ["base64", "rot13"])
def test_non_text_codec_is_refused_at_config_load(model: type[BaseModel], baseline: dict[str, Any], codec: str) -> None:
    assert model.model_validate(baseline) is not None  # control: the baseline is valid
    with pytest.raises(ValidationError) as refusal:
        model.model_validate({**baseline, "encoding": codec})
    assert "not a text encoding" in str(refusal.value)


def _delimiter_cases() -> list[Any]:
    from elspeth.plugins.sinks import aws_s3_sink, azure_blob_sink
    from elspeth.plugins.sources import aws_s3_source, azure_blob_source
    from elspeth.plugins.sources.csv_source import CSVSourceConfig

    source = {"path": "in.csv", "schema": _OBSERVED, "on_validation_failure": "discard"}
    return [
        pytest.param(CSVSourceConfig, source, id="csv_source"),
        pytest.param(azure_blob_source.CSVOptions, {}, id="azure_blob_source"),
        pytest.param(aws_s3_source.CSVOptions, {}, id="aws_s3_source"),
        pytest.param(azure_blob_sink.CSVWriteOptions, {}, id="azure_blob_sink"),
        pytest.param(aws_s3_sink.CSVWriteOptions, {}, id="aws_s3_sink"),
    ]


@pytest.mark.parametrize(("model", "baseline"), _delimiter_cases())
@pytest.mark.parametrize("delimiter", ['"', "\n", "\r"])
def test_csv_module_refused_delimiter_is_refused_at_config_load(model: type[BaseModel], baseline: dict[str, Any], delimiter: str) -> None:
    assert model.model_validate({**baseline, "delimiter": ";"}) is not None  # control
    with pytest.raises(ValidationError) as refusal:
        model.model_validate({**baseline, "delimiter": delimiter})
    assert "not usable by the csv module" in str(refusal.value)


def test_file_inject_csv_delimiter_uses_the_shared_check() -> None:
    from elspeth.plugins.transforms.file_inject import parse_injected_content

    with pytest.raises(ValueError, match="not usable by the csv module"):
        parse_injected_content("a\n1\n", fmt="csv", data_key=None, delimiter='"')


# ---------------------------------------------------------------------------
# F5/F6: one decoding authority. Every reader byte-escapes undecodable code
# units on their own line (UTF-16/32 included) and decodes UTF-8 as utf-8-sig.
# ---------------------------------------------------------------------------

_BOM = b"\xef\xbb\xbf"


def _utf16_with_bad_unit(lines: list[str], bad_index: int) -> bytes:
    """UTF-16-LE with BOM; a lone high surrogate (D800) prefixes line ``bad_index``."""
    out = b"\xff\xfe"
    for index, line in enumerate(lines):
        if index == bad_index:
            out += b"\x00\xd8"
        out += (line + "\n").encode("utf-16-le")
    return out


def _shape(rows: list[Any]) -> list[tuple[bool, int]]:
    return [(row.is_quarantined, row.source_row_index) for row in rows]


def _text_source(path: Any, **overrides: Any) -> Any:
    from elspeth.plugins.sources.text_source import TextSource

    config: dict[str, Any] = {"path": str(path), "column": "line", "schema": _OBSERVED, "on_validation_failure": "quarantine"}
    config.update(overrides)
    return TextSource(config)


def test_text_source_utf16_bad_code_unit_keeps_every_other_line(tmp_path: Any) -> None:
    """lane-06 suspected TextSource shared json/csv's UTF-16 chunk loss; it must not."""
    from unittest.mock import MagicMock

    lines = [f"line{i}" for i in range(3000)]  # several 8 KiB read-ahead chunks
    path = tmp_path / "in.txt"
    path.write_bytes(_utf16_with_bad_unit(lines, 1500))

    rows = list(_text_source(path, encoding="utf-16").load(MagicMock()))

    valid = [row.row["line"] for row in rows if not row.is_quarantined]
    assert valid == lines[:1500] + lines[1501:]
    quarantined = [row for row in rows if row.is_quarantined]
    assert len(quarantined) == 1
    assert quarantined[0].row["__line_number__"] == 1501
    assert quarantined[0].source_row_index == 1500


def test_text_source_strips_a_leading_utf8_bom_only(tmp_path: Any) -> None:
    from unittest.mock import MagicMock

    path = tmp_path / "bom.txt"
    path.write_bytes(_BOM + "hello\n\ufeffworld\n".encode())

    rows = list(_text_source(path).load(MagicMock()))

    assert [row.row["line"] for row in rows] == ["hello", "\ufeffworld"]


def _s3(payload: bytes, **overrides: Any) -> list[Any]:
    from tests.unit.plugins.sources.test_aws_s3_source import _source_for

    source, _, ctx = _source_for(payload, **overrides)
    return list(source.load(ctx))


@pytest.mark.parametrize("terminator", [b"\n", b"\r", b"\r\n"], ids=["lf", "cr", "crlf"])
@pytest.mark.parametrize("record_chars", [32, 33], ids=["at-limit", "over-limit"])
def test_s3_jsonl_record_limit_excludes_final_terminator(terminator: bytes, record_chars: int) -> None:
    record = b'{"id":"' + b"x" * (record_chars - 9) + b'"}'
    assert len(record) == record_chars
    rows = _s3(record + terminator + b'{"id":"ok"}' + terminator, format="jsonl", max_record_chars=32)

    assert _shape(rows) == [(record_chars > 32, 0), (False, 1)]
    if record_chars == 32:
        assert rows[0].row == {"id": "x" * 23}
    assert rows[1].row == {"id": "ok"}


def test_s3_jsonl_utf16_oversized_line_with_distant_bad_unit_keeps_next_record() -> None:
    long_line = ('{"id":"' + "x" * 5_000 + '"}').encode("utf-16-le")
    payload = b"\xff\xfe" + long_line + b"\x00\xd8" + '\n{"id":2}\n'.encode("utf-16-le")

    try:
        rows = _s3(payload, format="jsonl", json_options={"encoding": "utf-16"}, max_record_chars=32)
    except UnicodeDecodeError as exc:
        pytest.fail(f"run aborted while skipping the oversized line: {exc}")

    assert _shape(rows) == [(True, 0), (False, 1)]
    assert rows[1].row == {"id": 2}


def test_s3_jsonl_utf16_drain_at_limit_64_quarantines_a_line_with_a_bad_unit_after_5000_chars() -> None:
    """Third release review reproduction: the drain loop reaches the bad unit, the run continues."""
    long_line = ('{"id":"' + "y" * 5_000).encode("utf-16-le") + b"\x00\xd8" + '"}'.encode("utf-16-le")
    payload = b"\xff\xfe" + long_line + '\n{"id":2}\n'.encode("utf-16-le")

    try:
        rows = _s3(payload, format="jsonl", json_options={"encoding": "utf-16"}, max_record_chars=64)
    except UnicodeDecodeError as exc:
        pytest.fail(f"run aborted while draining the oversized line: {exc}")

    assert _shape(rows) == [(True, 0), (False, 1)]
    assert rows[1].row == {"id": 2}


@pytest.mark.parametrize(
    ("limit", "expected"),
    [(7, [(False, 0), (False, 1)]), (6, [(True, 0), (False, 1)])],
    ids=["exactly-at-limit", "oversized-drained"],
)
def test_s3_jsonl_crlf_is_one_terminator_across_bounded_reads_and_the_drain(limit: int, expected: list[tuple[bool, int]]) -> None:
    """Third release review reproduction: '{"x":1}' is 7 chars; the next record keeps index 1 either way."""
    rows = _s3(b'{"x":1}\r\n{}\r\n', format="jsonl", max_record_chars=limit)

    assert _shape(rows) == expected
    assert rows[1].row == {}


@pytest.mark.parametrize("embedded", [b"\n", b"\r", b"\r\n"], ids=["lf", "cr", "crlf"])
@pytest.mark.parametrize("record_chars", [32, 33], ids=["at-limit", "over-limit"])
def test_s3_csv_embedded_newline_counts_but_final_terminator_does_not(embedded: bytes, record_chars: int) -> None:
    record = b'"' + b"x" * (record_chars - len(embedded) - 3) + embedded + b'y"'
    assert len(record) == record_chars
    rows = _s3(record + b"\r\n", format="csv", csv_options={"has_header": False}, columns=["v"], max_record_chars=32)

    assert _shape(rows) == [(record_chars > 32, 0)]
    if record_chars == 32:
        assert rows[0].row == {"v": "x" * (record_chars - len(embedded) - 3) + embedded.decode() + "y"}


@pytest.mark.parametrize("terminators", [(b"\r",) * 3, (b"\r", b"\n", b"\r\n")], ids=["cr-only", "mixed"])
def test_s3_csv_consecutive_exact_limit_records_reset_terminator(terminators: tuple[bytes, ...]) -> None:
    values = ["a" * 32, "b" * 32, "c" * 32]
    payload = b"".join(value.encode() + ending for value, ending in zip(values, terminators, strict=True))
    rows = _s3(payload, format="csv", csv_options={"has_header": False}, columns=["v"], max_record_chars=32)

    assert _shape(rows) == [(False, 0), (False, 1), (False, 2)]
    assert [row.row["v"] for row in rows] == values


@pytest.mark.parametrize("value", ["", None], ids=["empty-string", "none-converted-to-empty"])
def test_s3_csv_single_empty_field_limit_matches_source(value: str | None) -> None:
    from elspeth.plugins.sinks.aws_s3_sink import CSVWriteOptions, S3RecordSizeLimitError, _serialize_rows_to_spool

    with pytest.raises(S3RecordSizeLimitError):
        _serialize_rows_to_spool(
            [{"v": value}],
            format="csv",
            csv_options=CSVWriteOptions(include_header=False),
            fieldnames=["v"],
            max_object_bytes=1024,
            max_record_chars=1,
        )
    serialized = _serialize_rows_to_spool(
        [{"v": value}],
        format="csv",
        csv_options=CSVWriteOptions(include_header=False),
        fieldnames=["v"],
        max_object_bytes=1024,
        max_record_chars=2,
    )
    try:
        payload = serialized.body.read()
    finally:
        serialized.close()
    assert payload == b'""\r\n'
    rows = _s3(payload, format="csv", csv_options={"has_header": False}, columns=["v"], max_record_chars=2)
    assert _shape(rows) == [(False, 0)]
    assert rows[0].row == {"v": ""}


@pytest.mark.parametrize("values", [[""], ["", ""], ["a,b"], ['a"b'], ["a\rb"], ["a\nb"], ["a\r\nb"], ["a", "b"]])
def test_s3_csv_record_measurement_matches_writer(values: list[str]) -> None:
    from elspeth.plugins.sinks.aws_s3_sink import _csv_record_chars

    buffer = io.StringIO(newline="")
    csv.writer(buffer, delimiter=",", lineterminator="\r\n").writerow(values)
    assert _csv_record_chars(values, ",") == len(buffer.getvalue()) - 2


@pytest.mark.parametrize("format", ["csv", "jsonl"])
def test_s3_sink_record_at_limit_round_trips_through_source(format: Literal["csv", "jsonl"]) -> None:
    from elspeth.plugins.sinks.aws_s3_sink import CSVWriteOptions, _serialize_rows_to_spool

    value = "x" * (32 if format == "csv" else 24)  # JSONL adds {"v":""}: eight characters.
    serialized = _serialize_rows_to_spool(
        [{"v": value}],
        format=format,
        csv_options=CSVWriteOptions(include_header=False),
        fieldnames=["v"],
        max_object_bytes=1024,
        max_record_chars=32,
    )
    try:
        payload = serialized.body.read()
    finally:
        serialized.close()
    assert len(payload.rstrip(b"\r\n")) == 32

    options: dict[str, Any] = {"format": format, "max_record_chars": 32}
    if format == "csv":
        options["csv_options"] = {"has_header": False}
        options["columns"] = ["v"]
    rows = _s3(payload, **options)
    assert _shape(rows) == [(False, 0)]
    assert rows[0].row == {"v": value}


@pytest.mark.parametrize("oversized_chars", [20, 41], ids=["cr-at-first-bounded-read", "cr-at-skip-chunk"])
def test_s3_jsonl_crlf_split_by_a_bounded_read_is_one_terminator(oversized_chars: int) -> None:
    oversized = b'{"id":"' + b"x" * (oversized_chars - 9) + b'"}'
    assert len(oversized) == oversized_chars
    payload = b'{"id":"1"}\r\n' + oversized + b'\r\n{"id":"3"}\r\n{"id":"4"}\r\n'

    rows = _s3(payload, format="jsonl", max_record_chars=20)

    assert _shape(rows) == [(False, 0), (oversized_chars > 20, 1), (False, 2), (False, 3)]
    assert rows[0].row == {"id": "1"}
    if oversized_chars == 20:
        assert rows[1].row == {"id": "x" * 11}
    else:
        assert rows[1].quarantine_error == "JSONL record exceeds configured character limit"
    assert [rows[2].row, rows[3].row] == [{"id": "3"}, {"id": "4"}]


def test_s3_jsonl_utf16_bad_code_unit_quarantines_only_its_line() -> None:
    payload = _utf16_with_bad_unit(['{"id":1}', '{"id":2}', '{"id":3}'], 1)

    rows = _s3(payload, format="jsonl", json_options={"encoding": "utf-16"})

    assert _shape(rows) == [(False, 0), (True, 1), (False, 2)]
    assert [rows[0].row, rows[2].row] == [{"id": 1}, {"id": 3}]
    assert rows[1].row["__line_number__"] == 2


def test_s3_jsonl_first_line_crlf_split_at_third_skip_chunk_keeps_next_row_index() -> None:
    """Review reproduction: a 41-char first line at a 20-char limit ends its third chunk on CR.

    The LF that the next read returns completes that terminator; the valid second line
    must keep source_row_index 1.
    """
    oversized = b'{"id":"' + b"x" * 32 + b'"}'
    assert len(oversized) == 41
    payload = oversized + b'\r\n{"id":2}\r\n'

    rows = _s3(payload, format="jsonl", max_record_chars=20)

    assert _shape(rows) == [(True, 0), (False, 1)]
    assert rows[1].row == {"id": 2}


@pytest.mark.parametrize("tail", ["bad-unit-then-next-line", "bad-unit-past-decoder-buffer", "odd-byte-at-eof"])
def test_s3_jsonl_utf16_oversized_line_with_undecodable_remainder_is_quarantined(tail: str) -> None:
    """Skipping the rest of an oversized line must not raise on bytes the codec cannot decode.

    The undecodable bytes sit after the record limit, so only skip_rest_of_line reads
    them. Raising there escaped the oversized-record handler and aborted the run. The
    past-decoder-buffer case puts the bad unit beyond the wrapper's first ~8 KiB read.
    """
    filler = 10_000 if tail == "bad-unit-past-decoder-buffer" else 40
    oversized = ('{"id":"' + "x" * filler + '"}').encode("utf-16-le")
    head = b"\xff\xfe" + '{"id":1}\n'.encode("utf-16-le")
    if tail in ("bad-unit-then-next-line", "bad-unit-past-decoder-buffer"):
        payload = head + oversized + b"\x00\xd8" + '\n{"id":3}\n'.encode("utf-16-le")
        expected = [(False, 0), (True, 1), (False, 2)]
    else:
        payload = head + oversized + b"\x41"
        expected = [(False, 0), (True, 1)]

    try:
        rows = _s3(payload, format="jsonl", json_options={"encoding": "utf-16"}, max_record_chars=20)
    except UnicodeDecodeError as exc:
        pytest.fail(f"run aborted while skipping the oversized line instead of quarantining it: {exc}")

    assert _shape(rows) == expected
    assert rows[0].row == {"id": 1}
    if tail != "odd-byte-at-eof":
        assert rows[2].row == {"id": 3}


@pytest.mark.parametrize(("fmt", "body"), [("jsonl", b'{"id":1}\n{"id":2}\n'), ("json", b'[{"id":1},{"id":2}]')])
def test_s3_json_strips_a_leading_utf8_bom(fmt: str, body: bytes) -> None:
    rows = _s3(_BOM + body, format=fmt)

    assert _shape(rows) == [(False, 0), (False, 1)]
    assert [row.row for row in rows] == [{"id": 1}, {"id": 2}]


def _azure(payload: bytes, **overrides: Any) -> list[Any]:
    from unittest.mock import patch

    from tests.fixtures.factories import make_operation_context
    from tests.unit.plugins.sources.test_azure_blob_source import PATCH_AUTH, _base_config, _fake_blob_service, _make_source

    source = _make_source(_base_config(**overrides))
    with patch(PATCH_AUTH, return_value=_fake_blob_service(payload)):
        return list(source.load(make_operation_context(plugin_name="azure_blob")))


def test_azure_jsonl_utf16_bad_code_unit_quarantines_only_its_line() -> None:
    payload = _utf16_with_bad_unit(['{"id":1}', '{"id":2}', '{"id":3}'], 1)

    rows = _azure(payload, format="jsonl", json_options={"encoding": "utf-16"})

    assert _shape(rows) == [(False, 0), (True, 1), (False, 2)]
    assert [rows[0].row, rows[2].row] == [{"id": 1}, {"id": 3}]
    assert rows[1].row["__line_number__"] == 2


def test_azure_csv_utf16_bad_code_unit_quarantines_only_its_record() -> None:
    payload = _utf16_with_bad_unit(["id,name", "1,a", "2,b", "3,c"], 2)

    rows = _azure(payload, format="csv", csv_options={"encoding": "utf-16"})

    assert _shape(rows) == [(False, 0), (True, 1), (False, 2)]
    assert [rows[0].row, rows[2].row] == [{"id": "1", "name": "a"}, {"id": "3", "name": "c"}]
    assert rows[1].row["__line_number__"] == "3"


@pytest.mark.parametrize(("fmt", "body"), [("jsonl", b'{"id":1}\n{"id":2}\n'), ("json", b'[{"id":1},{"id":2}]')])
def test_azure_json_strips_a_leading_utf8_bom(fmt: str, body: bytes) -> None:
    rows = _azure(_BOM + body, format=fmt)

    assert _shape(rows) == [(False, 0), (False, 1)]
    assert [row.row for row in rows] == [{"id": 1}, {"id": 2}]


def test_blob_json_expand_accepts_a_utf8_bom_prefixed_document() -> None:
    import json

    from tests.unit.plugins.transforms.test_blob_json_expand import _run_blob

    result = _run_blob(_BOM + json.dumps([{"document_id": "d1", "title": "T", "sections": []}]).encode())

    assert result.status == "success"
    assert result.rows is not None
    assert [row.to_dict()["document_id"] for row in result.rows] == ["d1"]


def test_blob_text_expand_strips_a_leading_utf8_bom() -> None:
    from tests.unit.plugins.transforms.test_blob_text_expand import _run

    _, result = _run(_BOM + b"alpha\nbeta\n")

    assert result.rows is not None
    assert [row.to_dict()["line"] for row in result.rows] == ["alpha", "beta"]


def test_blob_csv_expand_strips_a_leading_utf8_bom() -> None:
    from tests.fixtures.factories import make_context, make_pipeline_row
    from tests.unit.plugins.transforms.test_blob_csv_expand import _build_transform, _hash, _PayloadStoreFake

    body = _BOM + b"id,name\n1,alice\n"
    transform = _build_transform(field_mapping={"id": "record_id"})
    transform._payload_store = _PayloadStoreFake({_hash(body): body})

    result = transform.process(make_pipeline_row({"url": "https://example.test/a.csv", "blob_ref": _hash(body)}), make_context())

    assert result.status == "success"
    assert result.rows is not None
    assert result.rows[0].to_dict()["record_id"] == "1"
