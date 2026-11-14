"""P5 io-validation sweep: one authority for codecs, CSV delimiters and text decoding.

Maintainer ruling 2026-11-14: a defect fixed in one plugin is fixed in every
sibling. CSVSink validated its delimiter by building a csv.writer and its codec
by probing ``"".encode(v)``; every other plugin with a CSV ``delimiter`` or a
text ``encoding`` must refuse the same values at config load, through ONE shared
validator, instead of crashing on the first row or at publication.
"""

from __future__ import annotations

import importlib.util
import typing
from collections.abc import Iterator
from typing import Any

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
