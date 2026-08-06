"""Reference-content contract for every built-in sink plugin."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from pathlib import Path
from typing import Any, cast

import pytest

from elspeth.contracts.sink_effects import ResolvedSinkEffectMode, SinkEffectExecutionPurpose
from elspeth.core.config import load_bounded_pipeline_yaml
from elspeth.plugins.infrastructure.config_base import PluginConfigError
from elspeth.plugins.infrastructure.preflight import plugin_preflight_mode
from tests.fixtures.catalog_reference import (
    BuiltinReference,
    assert_reference_tags,
    assert_reference_text,
    discover_builtin_references,
    parse_and_validate_example,
)

EXPECTED_SINK_TAGS = {
    "aws_s3": ("aws", "s3", "cloud", "object-storage"),
    "azure_blob": ("azure", "blob", "cloud", "object-storage"),
    "chroma_sink": ("chroma", "vector-store", "embedding", "rag"),
    "csv": ("csv", "file", "batch", "tabular"),
    "database": ("database", "sql", "tabular", "exactly-once"),
    "dataverse": ("dataverse", "odata", "crm", "upsert"),
    "document": ("document", "file", "multiline", "single-value"),
    "json": ("json", "jsonl", "file", "structured"),
    "text": ("text", "file", "line-oriented", "single-field"),
}
EXPECTED_SINK_NAMES = set(EXPECTED_SINK_TAGS)
SINK_REFERENCES = tuple(reference for reference in discover_builtin_references() if reference.kind == "sink")
SINKS_BY_NAME = {reference.plugin_cls.name: reference for reference in SINK_REFERENCES}


def _declaring_node(reference: BuiltinReference) -> Mapping[str, Any]:
    parsed = load_bounded_pipeline_yaml(reference.plugin_cls.example_use)
    sinks = cast(Mapping[str, object], parsed["sinks"])
    assert len(sinks) == 1
    return cast(Mapping[str, Any], next(iter(sinks.values())))


def _options(reference: BuiltinReference) -> Mapping[str, Any]:
    return cast(Mapping[str, Any], _declaring_node(reference)["options"])


def _assert_relative_output_path(value: object) -> None:
    assert isinstance(value, str)
    path = Path(value)
    assert not path.is_absolute()
    assert ".." not in path.parts
    assert path.parts[0] == "outputs"


def _assert_plugin_specific_example_contract(reference: BuiltinReference) -> None:
    name = reference.plugin_cls.name
    options = _options(reference)

    if name == "aws_s3":
        assert options["format"] == "csv"
        assert options["overwrite"] is False
        assert "run_id" in cast(str, options["key"])
        assert "endpoint_url" not in options
    elif name == "azure_blob":
        assert options["use_managed_identity"] is True
        assert options["format"] == "jsonl"
        assert options["overwrite"] is False
        assert "run_id" in cast(str, options["blob_path"])
        assert not {"connection_string", "sas_token", "client_secret"} & options.keys()
    elif name == "chroma_sink":
        assert options["mode"] == "persistent"
        assert options["on_duplicate"] == "overwrite"
        _assert_relative_output_path(options["persist_directory"])
        field_mapping = cast(Mapping[str, Any], options["field_mapping"])
        assert field_mapping["id_field"] == "document_id"
        assert field_mapping["document_field"] == "body"
        assert field_mapping["metadata_fields"] == ["category"]
    elif name == "csv":
        assert options["collision_policy"] == "auto_increment"
        assert cast(Mapping[str, Any], options["schema"])["mode"] == "observed"
        _assert_relative_output_path(options["path"])
    elif name == "database":
        assert options["if_exists"] == "append"
        ledger = cast(Mapping[str, Any], options["effect_ledger"])
        assert ledger["table"] == "_elspeth_sink_effects"
        assert ledger["schema_version"] == 1
        assert set(cast(list[str], ledger["permissions"])) == {"insert", "select"}
        assert options["url"] == {"secret_ref": "PROVISIONED_SQLITE_URL"}
    elif name == "dataverse":
        assert options["auth"] == {"method": "managed_identity"}
        assert options["entity"] == "contacts"
        assert options["mode"] == "upsert"
        assert options["alternate_key"] == "emailaddress1"
        assert options["alternate_key"] in cast(Mapping[str, str], options["field_mapping"]).values()

        config_schema = reference.plugin_cls.config_model.model_json_schema()
        assert "EntitySetName" in config_schema["properties"]["entity"]["description"]
        lookup_schema = config_schema["$defs"]["LookupConfig"]
        assert "EntitySetName" in lookup_schema["properties"]["target_entity"]["description"]

        unsafe_options = deepcopy(dict(options))
        unsafe_options["field_mapping"]["account_id"] = "ignored_column"
        unsafe_options["lookups"] = {
            "account_id": {
                "target_entity": "accounts(record)/systemusers",
                "target_field": "parentcustomerid",
            }
        }
        with pytest.raises(PluginConfigError, match="ASCII identifier"):
            reference.plugin_cls.config_model.from_dict(unsafe_options, plugin_name="dataverse")
    elif name == "document":
        assert options["field"] == "announcement_text"
        assert options["collision_policy"] == "auto_increment"
        schema = cast(Mapping[str, Any], options["schema"])
        assert schema["mode"] == "fixed"
        assert schema["fields"] == ["announcement_text: str"]
        # The example must not advertise an append/resume knob this sink
        # deliberately does not offer.
        assert "mode" not in options
        _assert_relative_output_path(options["path"])
    elif name == "json":
        assert options["format"] == "jsonl"
        assert options["mode"] == "write"
        assert options["collision_policy"] == "auto_increment"
        assert cast(Mapping[str, Any], options["schema"])["mode"] == "observed"
        _assert_relative_output_path(options["path"])
    elif name == "text":
        assert options["field"] == "line_text"
        schema = cast(Mapping[str, Any], options["schema"])
        assert schema["mode"] == "fixed"
        assert schema["fields"] == ["line_text: str"]
        _assert_relative_output_path(options["path"])
    else:  # pragma: no cover - exact-name inventory assertion owns this guard
        raise AssertionError(f"unexpected built-in sink {name!r}")


def _assert_constructor_is_side_effect_free(reference: BuiltinReference, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    options = dict(_options(reference))
    if reference.plugin_cls.name == "database":
        assert options["url"] == {"secret_ref": "PROVISIONED_SQLITE_URL"}
        options["url"] = f"sqlite:///{tmp_path / 'catalogue.sqlite3'}"
    before = tuple(tmp_path.rglob("*"))
    with plugin_preflight_mode(True):
        sink = reference.plugin_cls(options)
    try:
        assert tuple(tmp_path.rglob("*")) == before
        if reference.plugin_cls.name == "database":
            mode = reference.plugin_cls._resolve_sink_effect_mode(
                options,
                purpose=SinkEffectExecutionPurpose.FRESH,
            )
            assert mode == ResolvedSinkEffectMode("append")
    finally:
        sink.close()
    assert tuple(tmp_path.rglob("*")) == before


def test_sink_catalogue_discovers_every_and_only_builtin_sink() -> None:
    assert set(SINKS_BY_NAME) == EXPECTED_SINK_NAMES


@pytest.mark.parametrize("reference", SINK_REFERENCES, ids=lambda reference: reference.plugin_cls.name)
def test_sink_catalogue_reference_content_is_specific_valid_and_truthful(
    reference: BuiltinReference,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert_reference_text(reference.plugin_cls)
    assert_reference_tags(reference.plugin_cls)
    assert reference.plugin_cls.capability_tags == EXPECTED_SINK_TAGS[reference.plugin_cls.name]
    parse_and_validate_example(reference)
    assert _declaring_node(reference)["plugin"] == reference.plugin_cls.name
    _assert_plugin_specific_example_contract(reference)
    _assert_constructor_is_side_effect_free(reference, tmp_path, monkeypatch)
