"""Contract tests for the built-in catalogue reference-content test kit."""

from __future__ import annotations

import importlib
from dataclasses import FrozenInstanceError
from types import ModuleType
from typing import Any, Literal

import pytest

from elspeth.contracts import Determinism
from elspeth.plugins.infrastructure.base import BaseSink, BaseSource, BaseTransform
from elspeth.plugins.infrastructure.config_base import PluginConfig, PluginConfigError


class _ReferenceConfig(PluginConfig):
    path: str
    api_key: str | None = None


class _ReferenceSource(BaseSource):
    name = "reference_source"
    determinism = Determinism.IO_READ
    config_model = _ReferenceConfig


class _NoConfigSource(BaseSource):
    name = "null"
    determinism = Determinism.DETERMINISTIC
    config_model = None


class _ReferenceTransform(BaseTransform):
    name = "reference_transform"
    determinism = Determinism.DETERMINISTIC
    config_model = _ReferenceConfig
    usage_when_to_use = (
        "Use this for a local input file when a row-preserving validation step must produce typed records before downstream processing."
    )
    usage_when_not_to_use = (
        "Avoid this for network inputs because it performs no fetch; use an HTTP or object-storage plugin before this step."
    )
    example_use = "transform:\n  plugin: reference_transform\n  options:\n    path: data/orders.json"
    capability_tags = ("local-file", "row-validation")


class _ReferenceBatchTransform(BaseTransform):
    name = "reference_batch"
    determinism = Determinism.DETERMINISTIC
    config_model = _ReferenceConfig
    is_batch_aware = True


class _ReferenceSink(BaseSink):
    name = "reference_sink"
    determinism = Determinism.IO_WRITE
    config_model = _ReferenceConfig


def _testkit() -> ModuleType:
    return importlib.import_module("tests.fixtures.catalog_reference")


def _reference(
    testkit: ModuleType,
    kind: Literal["source", "transform", "sink"],
    plugin_cls: type[BaseSource] | type[BaseTransform] | type[BaseSink],
) -> Any:
    return testkit.BuiltinReference(kind=kind, plugin_cls=plugin_cls)


def _parse_example(
    testkit: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    *,
    kind: Literal["source", "transform", "sink"],
    plugin_cls: type[BaseSource] | type[BaseTransform] | type[BaseSink],
    example: str,
) -> None:
    monkeypatch.setattr(plugin_cls, "example_use", example)
    testkit.parse_and_validate_example(_reference(testkit, kind, plugin_cls))


def test_discover_builtin_references_returns_frozen_typed_records() -> None:
    testkit = _testkit()

    references = testkit.discover_builtin_references()

    assert references
    assert {reference.kind for reference in references} == {"source", "transform", "sink"}
    assert all(issubclass(reference.plugin_cls, (BaseSource, BaseTransform, BaseSink)) for reference in references)
    with pytest.raises(FrozenInstanceError):
        references[0].kind = "sink"


@pytest.mark.parametrize(
    ("field_name", "invalid_value"),
    [
        ("usage_when_to_use", None),
        ("usage_when_not_to_use", None),
        ("example_use", None),
        ("usage_when_to_use", ""),
        ("usage_when_not_to_use", ""),
        ("example_use", ""),
        ("usage_when_to_use", " \t\n"),
        ("usage_when_not_to_use", " \t\n"),
        ("example_use", " \t\n"),
        ("usage_when_to_use", "See the technical description."),
        ("usage_when_not_to_use", "See the technical description."),
        ("example_use", "See the technical description."),
    ],
)
def test_assert_reference_text_rejects_missing_blank_or_generic_prose(
    monkeypatch: pytest.MonkeyPatch,
    field_name: str,
    invalid_value: str | None,
) -> None:
    testkit = _testkit()
    monkeypatch.setattr(_ReferenceTransform, field_name, invalid_value)

    with pytest.raises(AssertionError):
        testkit.assert_reference_text(_ReferenceTransform)


@pytest.mark.parametrize(
    ("first_field", "second_field"),
    [
        ("usage_when_to_use", "usage_when_not_to_use"),
        ("usage_when_to_use", "example_use"),
        ("usage_when_not_to_use", "example_use"),
    ],
)
def test_assert_reference_text_rejects_duplicated_prose(
    monkeypatch: pytest.MonkeyPatch,
    first_field: str,
    second_field: str,
) -> None:
    testkit = _testkit()
    monkeypatch.setattr(
        _ReferenceTransform,
        second_field,
        getattr(_ReferenceTransform, first_field),
    )

    with pytest.raises(AssertionError):
        testkit.assert_reference_text(_ReferenceTransform)


def test_assert_reference_text_accepts_distinct_specific_prose() -> None:
    _testkit().assert_reference_text(_ReferenceTransform)


@pytest.mark.parametrize(
    "invalid_tags",
    [
        ["local-file", "row-validation"],
        ("local-file", "local-file"),
        ("Local-File", "row-validation"),
        ("", "row-validation"),
        ("general", "utility"),
        ("a" * 33, "row-validation"),
        ("only-one",),
        ("one", "two", "three", "four", "five", "six", "seven"),
    ],
)
def test_assert_reference_tags_rejects_invalid_tags(
    monkeypatch: pytest.MonkeyPatch,
    invalid_tags: object,
) -> None:
    testkit = _testkit()
    monkeypatch.setattr(_ReferenceTransform, "capability_tags", invalid_tags)

    with pytest.raises(AssertionError):
        testkit.assert_reference_tags(_ReferenceTransform)


def test_assert_reference_tags_accepts_two_to_six_unique_kebab_case_tags() -> None:
    _testkit().assert_reference_tags(_ReferenceTransform)


@pytest.mark.parametrize(
    "example",
    [
        ("sources:\n  input:\n    plugin: reference_source\n    options:\n      path: data/orders.json\n"),
        ("sources:\n  - plugin: reference_source\n    options:\n      path: data/orders.json\n"),
    ],
)
def test_parse_and_validate_example_accepts_source_under_sources(
    monkeypatch: pytest.MonkeyPatch,
    example: str,
) -> None:
    testkit = _testkit()

    _parse_example(
        testkit,
        monkeypatch,
        kind="source",
        plugin_cls=_ReferenceSource,
        example=example,
    )


def test_parse_and_validate_example_rejects_deleted_singular_source_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    testkit = _testkit()
    example = "source:\n  plugin: reference_source\n  options:\n    path: data/orders.json\n"

    with pytest.raises(AssertionError):
        _parse_example(
            testkit,
            monkeypatch,
            kind="source",
            plugin_cls=_ReferenceSource,
            example=example,
        )


def test_parse_and_validate_example_accepts_ordinary_transform_fragment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    testkit = _testkit()
    example = "transform:\n  plugin: reference_transform\n  options:\n    path: data/orders.json\n"

    _parse_example(
        testkit,
        monkeypatch,
        kind="transform",
        plugin_cls=_ReferenceTransform,
        example=example,
    )


@pytest.mark.parametrize(
    "example",
    [
        ("aggregations:\n  daily:\n    plugin: reference_batch\n    options:\n      path: data/orders.json\n"),
        ("aggregations:\n  - plugin: reference_batch\n    options:\n      path: data/orders.json\n"),
    ],
)
def test_parse_and_validate_example_accepts_batch_transform_under_aggregations(
    monkeypatch: pytest.MonkeyPatch,
    example: str,
) -> None:
    testkit = _testkit()

    _parse_example(
        testkit,
        monkeypatch,
        kind="transform",
        plugin_cls=_ReferenceBatchTransform,
        example=example,
    )


@pytest.mark.parametrize(
    "example",
    [
        ("sinks:\n  output:\n    plugin: reference_sink\n    options:\n      path: output/results.jsonl\n"),
        ("sinks:\n  - plugin: reference_sink\n    options:\n      path: output/results.jsonl\n"),
    ],
)
def test_parse_and_validate_example_accepts_sink_under_sinks(
    monkeypatch: pytest.MonkeyPatch,
    example: str,
) -> None:
    testkit = _testkit()

    _parse_example(
        testkit,
        monkeypatch,
        kind="sink",
        plugin_cls=_ReferenceSink,
        example=example,
    )


@pytest.mark.parametrize(
    "example",
    [
        "sources: []\n",
        (
            "sources:\n"
            "  - plugin: reference_source\n"
            "    options: {path: data/one.json}\n"
            "  - plugin: reference_source\n"
            "    options: {path: data/two.json}\n"
        ),
    ],
)
def test_parse_and_validate_example_requires_exactly_one_declaring_plugin(
    monkeypatch: pytest.MonkeyPatch,
    example: str,
) -> None:
    testkit = _testkit()

    with pytest.raises(AssertionError):
        _parse_example(
            testkit,
            monkeypatch,
            kind="source",
            plugin_cls=_ReferenceSource,
            example=example,
        )


def test_parse_and_validate_example_rejects_wrong_plugin_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    testkit = _testkit()
    example = "transform:\n  plugin: another_transform\n  options:\n    path: data/orders.json\n"

    with pytest.raises(AssertionError):
        _parse_example(
            testkit,
            monkeypatch,
            kind="transform",
            plugin_cls=_ReferenceTransform,
            example=example,
        )


def test_parse_and_validate_example_rejects_unknown_options_via_config_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    testkit = _testkit()
    example = "transform:\n  plugin: reference_transform\n  options:\n    path: data/orders.json\n    surprise: true\n"

    with pytest.raises(PluginConfigError, match="surprise"):
        _parse_example(
            testkit,
            monkeypatch,
            kind="transform",
            plugin_cls=_ReferenceTransform,
            example=example,
        )


def test_parse_and_validate_example_normalizes_allowed_secret_ref_for_config_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    testkit = _testkit()
    example = (
        "transform:\n"
        "  plugin: reference_transform\n"
        "  options:\n"
        "    path: data/orders.json\n"
        "    api_key:\n"
        "      secret_ref: EXAMPLE_API_KEY\n"
    )

    _parse_example(
        testkit,
        monkeypatch,
        kind="transform",
        plugin_cls=_ReferenceTransform,
        example=example,
    )
    assert "secret_ref: EXAMPLE_API_KEY" in example


def test_parse_and_validate_example_rejects_secret_ref_in_disallowed_field(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    testkit = _testkit()
    example = "transform:\n  plugin: reference_transform\n  options:\n    path:\n      secret_ref: INPUT_PATH\n"

    with pytest.raises(AssertionError, match="secret"):
        _parse_example(
            testkit,
            monkeypatch,
            kind="transform",
            plugin_cls=_ReferenceTransform,
            example=example,
        )


def test_parse_and_validate_example_rejects_literal_credential_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    testkit = _testkit()
    example = (
        "transform:\n  plugin: reference_transform\n  options:\n    path: data/orders.json\n    api_key: illustrative-literal-credential\n"
    )

    with pytest.raises(AssertionError, match="credential"):
        _parse_example(
            testkit,
            monkeypatch,
            kind="transform",
            plugin_cls=_ReferenceTransform,
            example=example,
        )


def test_parse_and_validate_example_accepts_quoted_null_without_options(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    testkit = _testkit()
    example = 'sources:\n  resume:\n    plugin: "null"\n'

    _parse_example(
        testkit,
        monkeypatch,
        kind="source",
        plugin_cls=_NoConfigSource,
        example=example,
    )
