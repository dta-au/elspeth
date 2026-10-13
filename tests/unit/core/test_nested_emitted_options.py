"""Nested emitted values must be guarded without treating selectors as output."""

import json
from pathlib import Path
from typing import Annotated, Any

import pytest
import yaml
from pydantic import BaseModel, Field, ValidationError

from elspeth.config_loading import _reject_sensitive_plugin_env_placeholders_before_expansion
from elspeth.contracts.emitted_option import EmittedToOutput, emitted_option_fields, emitted_option_values
from elspeth.plugins.infrastructure.config_base import PluginConfig


class NestedLookup(BaseModel):
    selector: str = "input"
    default_values: Annotated[dict[str, Any], EmittedToOutput("lookup defaults become row values")] = Field(default_factory=dict)


class NestedConfig(PluginConfig):
    lookups: list[NestedLookup] | None = None
    named: dict[str, NestedLookup] = Field(default_factory=dict)
    single: NestedLookup | None = Field(default=None, alias="one")
    reference_content: str = ""


class NestedPlugin:
    name = "nested_test"
    output_naming_config_keys: frozenset[str] = frozenset()

    @classmethod
    def get_config_model(cls) -> type[NestedConfig]:
        return NestedConfig


class NestedManager:
    def get_sources(self) -> list[type[NestedPlugin]]:
        return []

    def get_transforms(self) -> list[type[NestedPlugin]]:
        return [NestedPlugin]

    def get_sinks(self) -> list[type[NestedPlugin]]:
        return []


@pytest.fixture
def nested_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    from elspeth.plugins.infrastructure import manager

    monkeypatch.setattr(manager, "get_shared_plugin_manager", NestedManager)


@pytest.mark.parametrize(
    ("options", "path"),
    [
        ({"lookups": [{"default_values": {"result": "${NESTED_TEST_HOST}"}}]}, r"lookups\[0\]\.default_values"),
        ({"named": {"products": {"default_values": {"result": ["${NESTED_TEST_HOST}"]}}}}, r"named\['products'\]\.default_values"),
        ({"one": {"default_values": {"${NESTED_TEST_HOST}": "value"}}}, r"one\.default_values"),
    ],
)
def test_plugin_validation_rejects_nested_emitted_defaults(options: dict[str, Any], path: str) -> None:
    with pytest.raises(ValidationError, match=path):
        NestedConfig.model_validate(options)


@pytest.mark.parametrize(
    "options",
    [
        {"lookups": [{"default_values": {"result": "${NESTED_TEST_HOST}"}}]},
        {"named": {"products": {"default_values": {"result": "${NESTED_TEST_HOST:-fallback}"}}}},
        {"one": {"default_values": {"result": "${NESTED_TEST_HOST}"}}},
    ],
)
def test_loader_rejects_nested_defaults_before_expansion(nested_registry: None, options: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match="before env expansion"):
        _reject_sensitive_plugin_env_placeholders_before_expansion({"transforms": [{"plugin": "nested_test", "options": options}]})


def test_nested_guard_preserves_selectors_and_reference_content(nested_registry: None) -> None:
    options = {
        "lookups": [{"selector": "${NESTED_TEST_HOST}", "default_values": {"result": "ordinary fallback"}}],
        "reference_content": "key,value\nitem,${NESTED_TEST_HOST}\n",
    }
    config = NestedConfig.model_validate(options)
    assert config.lookups is not None
    assert config.lookups[0].selector == "${NESTED_TEST_HOST}"
    assert config.reference_content == options["reference_content"]
    _reject_sensitive_plugin_env_placeholders_before_expansion({"transforms": [{"plugin": "nested_test", "options": options}]})


def test_top_level_declaration_api_does_not_mark_nested_containers() -> None:
    assert emitted_option_fields(NestedConfig) == {}
    assert emitted_option_fields(NestedLookup) == {"default_values": "lookup defaults become row values"}


def test_reference_join_expression_literal_cannot_expand_host_value() -> None:
    with pytest.raises(ValueError, match="before env expansion"):
        _reject_sensitive_plugin_env_placeholders_before_expansion(
            {"transforms": [{"plugin": "reference_join", "options": {"output": {"result": "'${NESTED_TEST_HOST}'"}}}]}
        )


@pytest.mark.parametrize("options", [None, "invalid", {"lookups": "invalid"}, {"named": ["invalid"]}, {"lookups": [None, 3]}])
def test_malformed_nested_shapes_remain_for_config_validation(nested_registry: None, options: object) -> None:
    assert list(emitted_option_values(NestedConfig, options)) == []
    _reject_sensitive_plugin_env_placeholders_before_expansion({"transforms": [{"plugin": "nested_test", "options": options}]})


def test_nested_metadata_is_derived_for_each_model() -> None:
    class UnmarkedLookup(BaseModel):
        default_values: dict[str, str]

    class UnmarkedConfig(PluginConfig):
        lookups: list[UnmarkedLookup]

    options = {"lookups": [{"default_values": {"result": "${NESTED_TEST_HOST}"}}]}
    assert list(emitted_option_values(UnmarkedConfig, options)) == []
    assert list(emitted_option_values(NestedConfig, options)) == [
        ("lookups[0].default_values", {"result": "${NESTED_TEST_HOST}"}, "lookup defaults become row values")
    ]


@pytest.mark.parametrize("field", ["default_values", "output"])
@pytest.mark.parametrize("expand_env_vars", [False, True])
def test_real_lookup_loader_blocks_nested_host_expansion(monkeypatch: pytest.MonkeyPatch, field: str, expand_env_vars: bool) -> None:
    from elspeth.config_loading import load_settings_from_config_dict

    monkeypatch.setenv("NESTED_TEST_HOST", "host-value-must-not-reach-output")
    options = {
        "schema": {"mode": "observed"},
        "tables": {"products": {"reference_content": "key,value\nitem,name\n", "reference_format": "csv"}},
        "lookups": [
            {
                "table": "products",
                "key_field": "input",
                "reference_key_name": "key",
                "output": {"result": "ref['value']"},
                field: {"result": "'${NESTED_TEST_HOST}'"},
            }
        ],
    }
    with pytest.raises(ValueError, match=r"lookups\[0\].*before env expansion"):
        load_settings_from_config_dict({"transforms": [{"plugin": "reference_join", "options": options}]}, expand_env_vars=expand_env_vars)


def test_real_lookup_config_preserves_literal_table_and_selector_placeholders() -> None:
    from elspeth.plugins.transforms.reference_join import ReferenceJoinConfig

    options = {
        "schema": {"mode": "observed"},
        "tables": {"products": {"reference_content": "key,value\nitem,${NESTED_TEST_HOST}\n", "reference_format": "csv"}},
        "lookups": [
            {"table": "products", "key_field": "${INPUT_SELECTOR}", "reference_key_name": "key", "output": {"result": "ref['value']"}}
        ],
    }
    config = ReferenceJoinConfig.model_validate(options)
    assert config.lookups is not None
    assert config.lookups[0].key_field == "${INPUT_SELECTOR}"
    assert config.tables is not None
    assert config.tables["products"].reference_content == "key,value\nitem,${NESTED_TEST_HOST}\n"
    _reject_sensitive_plugin_env_placeholders_before_expansion(
        {"transforms": [{"plugin": "reference_join", "options": options}]}, expand_env_vars=False
    )


def _reference_pipeline(tmp_path: Path, *, named: bool, file_backed: bool = False) -> dict[str, Any]:
    (tmp_path / "input.csv").write_text("input\nitem\n", encoding="utf-8")
    literal = "key,value\nitem,${NESTED_TEST_HOST}\n"
    (tmp_path / "reference.csv").write_text(literal, encoding="utf-8")
    table = {"reference_format": "csv"}
    table["reference_file" if file_backed else "reference_content"] = "reference.csv" if file_backed else literal
    lookup = {"key_field": "input", "reference_key_name": "key", "output": {"result": "ref['value']"}}
    options = {"tables": {"products": table}, "lookups": [{"table": "products", **lookup}]} if named else {**table, **lookup}
    return {
        "sources": {
            "primary": {
                "plugin": "csv",
                "on_success": "rows",
                "options": {"path": str(tmp_path / "input.csv"), "schema": {"mode": "observed"}, "on_validation_failure": "discard"},
            }
        },
        "transforms": [
            {
                "name": "enrich",
                "plugin": "reference_join",
                "input": "rows",
                "on_success": "output",
                "on_error": "discard",
                "options": {"schema": {"mode": "observed"}, **options},
            }
        ],
        "sinks": {
            "output": {
                "plugin": "json",
                "on_write_failure": "discard",
                "options": {"path": str(tmp_path / "output.jsonl"), "format": "jsonl", "schema": {"mode": "observed"}},
            }
        },
        "landscape": {"url": f"sqlite:///{tmp_path / 'audit.db'}"},
        "payload_store": {"backend": "filesystem", "base_path": str(tmp_path / "payloads")},
    }


@pytest.mark.parametrize("named", [False, True])
def test_public_cli_rejects_inline_reference_host_expansion(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, named: bool) -> None:
    from typer.testing import CliRunner

    from elspeth.cli import app

    monkeypatch.setenv("NESTED_TEST_HOST", "host-value-must-not-reach-output")
    path = tmp_path / "settings.yaml"
    path.write_text(yaml.safe_dump(_reference_pipeline(tmp_path, named=named)), encoding="utf-8")
    result = CliRunner().invoke(app, ["run", "--settings", str(path), "--execute"])
    assert result.exit_code != 0
    assert "before env expansion" in result.output
    assert not (tmp_path / "output.jsonl").exists()


@pytest.mark.parametrize("named", [False, True])
def test_dict_loader_rejects_inline_reference_only_when_expanding(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, named: bool) -> None:
    from elspeth.config_loading import load_settings_from_config_dict

    monkeypatch.setenv("NESTED_TEST_HOST", "host-value-must-not-reach-output")
    raw = _reference_pipeline(tmp_path, named=named)
    with pytest.raises(ValueError, match=r"reference_content.*before env expansion"):
        load_settings_from_config_dict(raw, expand_env_vars=True)


@pytest.mark.parametrize("named", [False, True])
def test_dict_loader_preserves_inline_reference_without_expansion(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, named: bool) -> None:
    from elspeth.config_loading import load_settings_from_config_dict
    from elspeth.plugins.transforms.reference_join import ReferenceJoinConfig

    monkeypatch.setenv("NESTED_TEST_HOST", "host-value-must-not-reach-output")
    settings = load_settings_from_config_dict(_reference_pipeline(tmp_path, named=named))
    options = settings.transforms[0].options
    ReferenceJoinConfig.model_validate(options)
    table = options["tables"]["products"] if named else options
    assert table["reference_content"] == "key,value\nitem,${NESTED_TEST_HOST}\n"


@pytest.mark.parametrize("named", [False, True])
def test_file_loader_preserves_reference_literals_after_expansion(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, named: bool) -> None:
    from typer.testing import CliRunner

    from elspeth.cli import app
    from elspeth.config_loading import load_settings
    from elspeth.plugins.transforms.reference_join import ReferenceJoinConfig

    monkeypatch.setenv("NESTED_TEST_HOST", "host-value-must-not-reach-output")
    path = tmp_path / "settings.yaml"
    path.write_text(yaml.safe_dump(_reference_pipeline(tmp_path, named=named, file_backed=True)), encoding="utf-8")
    settings = load_settings(path)
    options = settings.transforms[0].options
    ReferenceJoinConfig.model_validate(options)
    table = options["tables"]["products"] if named else options
    assert table["reference_content"] == "key,value\nitem,${NESTED_TEST_HOST}\n"
    result = CliRunner().invoke(app, ["run", "--settings", str(path), "--execute"])
    assert result.exit_code == 0, result.output
    assert [json.loads(row) for row in (tmp_path / "output.jsonl").read_text().splitlines()] == [
        {"input": "item", "result": "${NESTED_TEST_HOST}"}
    ]


def test_preexpansion_only_metadata_preserves_validation_api_semantics() -> None:
    class ContentConfig(PluginConfig):
        text: Annotated[str, EmittedToOutput("content reaches rows", reject_at_validation=False)]

    class ParentConfig(PluginConfig):
        contents: dict[str, ContentConfig]

    value = {"text": "${NESTED_TEST_HOST}"}
    ContentConfig.model_validate(value)
    assert emitted_option_fields(ContentConfig) == {}
    assert list(emitted_option_values(ContentConfig, value)) == []
    assert emitted_option_fields(ContentConfig, for_env_expansion=True) == {"text": "content reaches rows"}
    assert list(emitted_option_values(ContentConfig, value, for_env_expansion=True)) == [
        ("text", "${NESTED_TEST_HOST}", "content reaches rows")
    ]
    parent = {"contents": {"first": value}}
    ParentConfig.model_validate(parent)
    assert list(emitted_option_values(ParentConfig, parent)) == []
    assert list(emitted_option_values(ParentConfig, parent, for_env_expansion=True)) == [
        ("contents['first'].text", "${NESTED_TEST_HOST}", "content reaches rows")
    ]
