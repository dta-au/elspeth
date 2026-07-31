"""Reference-content contract for every built-in source plugin."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast

import pytest

from elspeth.core.config import load_bounded_pipeline_yaml
from tests.fixtures.catalog_reference import (
    BuiltinReference,
    assert_reference_tags,
    assert_reference_text,
    discover_builtin_references,
    parse_and_validate_example,
)

EXPECTED_SOURCE_NAMES = {"aws_s3", "azure_blob", "csv", "dataverse", "json", "null", "text"}
SOURCE_REFERENCES = tuple(reference for reference in discover_builtin_references() if reference.kind == "source")
SOURCES_BY_NAME = {reference.plugin_cls.name: reference for reference in SOURCE_REFERENCES}


def _declaring_node(reference: BuiltinReference) -> Mapping[str, Any]:
    parsed = load_bounded_pipeline_yaml(reference.plugin_cls.example_use)
    sources = cast(Mapping[str, object], parsed["sources"])
    assert len(sources) == 1
    return cast(Mapping[str, Any], next(iter(sources.values())))


def test_source_catalogue_discovers_every_and_only_builtin_source() -> None:
    assert set(SOURCES_BY_NAME) == EXPECTED_SOURCE_NAMES


@pytest.mark.parametrize("reference", SOURCE_REFERENCES, ids=lambda reference: reference.plugin_cls.name)
def test_source_catalogue_reference_content_is_specific_and_valid(reference: BuiltinReference) -> None:
    assert_reference_text(reference.plugin_cls)
    assert_reference_tags(reference.plugin_cls)
    parse_and_validate_example(reference)


@pytest.mark.parametrize("reference", SOURCE_REFERENCES, ids=lambda reference: reference.plugin_cls.name)
def test_source_example_plugin_identity_matches_its_declaration(reference: BuiltinReference) -> None:
    assert _declaring_node(reference)["plugin"] == reference.plugin_cls.name


def test_null_is_the_only_internal_source_and_quotes_its_yaml_plugin_name() -> None:
    internal_sources = {reference.plugin_cls.name for reference in SOURCE_REFERENCES if "internal" in reference.plugin_cls.capability_tags}
    assert internal_sources == {"null"}
    assert 'plugin: "null"' in SOURCES_BY_NAME["null"].plugin_cls.example_use


def test_aws_s3_warns_that_ordinary_web_composer_cannot_use_it() -> None:
    aws_s3 = SOURCES_BY_NAME["aws_s3"].plugin_cls
    guidance = f"{aws_s3.usage_when_to_use} {aws_s3.usage_when_not_to_use}".casefold()
    assert "ordinary web composer cannot use" in guidance


def test_dataverse_warns_against_webhooks_and_change_streams() -> None:
    when_not_to_use = SOURCES_BY_NAME["dataverse"].plugin_cls.usage_when_not_to_use.casefold()
    assert "webhooks" in when_not_to_use
    assert "change streams" in when_not_to_use


def test_csv_example_uses_the_current_plural_source_shape_and_complete_routing() -> None:
    parsed = load_bounded_pipeline_yaml(SOURCES_BY_NAME["csv"].plugin_cls.example_use)
    assert set(parsed) == {"sources"}
    source = cast(Mapping[str, Any], cast(Mapping[str, object], parsed["sources"])["primary"])
    assert source["plugin"] == "csv"
    assert source["on_success"] == "output"
    options = cast(Mapping[str, Any], source["options"])
    assert options["schema"] == {"mode": "observed"}
    assert options["on_validation_failure"] == "discard"


@pytest.mark.parametrize("source_name", sorted(EXPECTED_SOURCE_NAMES - {"null"}))
def test_non_null_source_examples_have_an_explicit_validation_failure_policy(source_name: str) -> None:
    source = _declaring_node(SOURCES_BY_NAME[source_name])
    options = cast(Mapping[str, Any], source["options"])
    assert options["on_validation_failure"] in {"discard"}
