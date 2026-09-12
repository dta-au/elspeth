"""Every live Composer tool has closed ADMITTED root argument names.

SHIPPED comes from the actual registry. ADMITTED comes from the manifest's
production closure policy or its argument model's accepted wire names. Empty
policies remain in the relation; missing/duplicate endpoints cannot disappear
through intersections or default values. Type-driven output is also exercised
through the real redactor, including existing default materialization.

This is root-name admission and emission evidence. MODEL proves actual handler
admission separately; READ, nested value redaction, TAUGHT and frontend projection
have their own authorities. No whole-schema or semantic consumption claim is
made here. Alias forms that the name-based sensitive walker cannot establish
are refused explicitly instead of being treated as Python-field equality.
"""

from __future__ import annotations

from copy import deepcopy
from types import MappingProxyType
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from pydantic import AliasChoices, AliasPath, BaseModel, ConfigDict, Field

from elspeth.web.composer import redaction
from elspeth.web.composer.redaction import (
    REDACTED_UNKNOWN_ARGUMENT_KEY,
    REDACTED_UNKNOWN_ARGUMENTS_FIELD,
    ToolRedaction,
    policy_closes_unknown_arguments,
    redact_tool_call_arguments,
)
from elspeth.web.composer.redaction_telemetry import NoopRedactionTelemetry
from elspeth.web.composer.tools._dispatch import get_tool_definitions

_TYPE_DRIVEN_INPUTS: dict[str, dict[str, Any]] = {
    "set_source": {
        "plugin": "csv",
        "on_success": "rows",
        "on_validation_failure": "discard",
        "options": {"nested": [None, False, 1]},
    },
    "create_blob": {"filename": "data.csv", "mime_type": "text/csv", "content": "x\n1\n"},
    "update_blob": {"blob_id": "blob", "content": "x\n2\n"},
    "set_source_from_blob": {"blob_id": "blob", "on_success": "rows"},
    "set_source_from_blobs": {"blob_ids": ["blob"], "on_success": "rows"},
    "set_pipeline": {"source": {"plugin": "csv", "on_success": "rows"}, "nodes": [], "edges": [], "outputs": []},
    "patch_source_options": {"source_name": "source", "patch": {"nested": [None, False, 1]}},
    "patch_node_options": {"node_id": "node", "patch": {"nested": [None, False, 1]}},
    "patch_output_options": {"sink_name": "output", "patch": {"nested": [None, False, 1]}},
    "get_blob_content": {"blob_id": "blob"},
    "request_interpretation_review": {"affected_node_id": "node", "kind": "vague_term", "user_term": "draft term"},
    "splice_transform": {
        "predecessor_id": "source",
        "successor_id": "output",
        "node": {"id": "node", "plugin": "passthrough", "options": {}},
    },
}


def test_emission_fixtures_cover_exact_live_type_driven_universe() -> None:
    assert _TYPE_DRIVEN_INPUTS.keys() == {name for name, entry in redaction.MANIFEST.items() if entry.argument_model is not None}


@pytest.mark.parametrize("mutation", ["missing", "extra"])
def test_emission_fixture_endpoint_drift_is_rejected(monkeypatch: pytest.MonkeyPatch, mutation: str) -> None:
    if mutation == "missing":
        monkeypatch.delitem(_TYPE_DRIVEN_INPUTS, "get_blob_content")
    else:
        monkeypatch.setitem(_TYPE_DRIVEN_INPUTS, "unregistered_tool", {})
    with pytest.raises(AssertionError):
        test_emission_fixtures_cover_exact_live_type_driven_universe()


def _shipped_argument_keys() -> dict[str, frozenset[str]]:
    """Read the owned registry grammar without skipping or overwriting rows."""
    shipped: dict[str, frozenset[str]] = {}
    for definition in get_tool_definitions():
        name = definition["name"]
        assert name not in shipped, f"duplicate shipped definition: {name}"
        shipped[name] = frozenset(definition["parameters"]["properties"])
    assert shipped, "empty tool registry"
    assert shipped.keys() == redaction.MANIFEST.keys(), (
        f"registry/manifest mismatch: missing policies={sorted(shipped.keys() - redaction.MANIFEST.keys())}; "
        f"missing definitions={sorted(redaction.MANIFEST.keys() - shipped.keys())}"
    )
    return shipped


def _model_argument_keys(tool: str, model: type[BaseModel]) -> frozenset[str]:
    """Measure accepted root names; diagnose unsupported aliases explicitly."""
    for name, field in model.model_fields.items():
        assert field.alias in (None, name), f"{tool}.{name}: unsupported input alias"
        assert field.validation_alias in (None, name), f"{tool}.{name}: unsupported validation alias"
        assert field.serialization_alias in (None, name), f"{tool}.{name}: unsupported serialization alias"
    assert model.model_config["extra"] == "forbid", f"{tool}: argument model must reject unknown root keys"
    schema = model.model_json_schema(mode="validation", by_alias=True)
    assert schema["type"] == "object", f"{tool}: unsupported argument root"
    accepted = frozenset(schema["properties"])
    assert accepted == frozenset(model.model_fields), f"{tool}: accepted schema/field names disagree"
    return accepted


def _admitted_argument_keys() -> dict[str, frozenset[str]]:
    """Keep every manifest entry, including closed policies with zero keys."""
    admitted: dict[str, frozenset[str]] = {}
    for name, entry in redaction.MANIFEST.items():
        model = entry.argument_model
        if model is not None:
            admitted[name] = _model_argument_keys(name, model)
        else:
            policy = entry.policy
            assert policy is not None
            assert policy_closes_unknown_arguments(policy), f"{name}: open argument policy"
            admitted[name] = frozenset(policy.known_argument_keys)
    return admitted


def _assert_admitted_wire() -> None:
    shipped = _shipped_argument_keys()
    admitted = _admitted_argument_keys()
    assert admitted.keys() == shipped.keys(), "ADMITTED relation omitted a registered tool"
    for name in shipped:
        assert shipped[name] == admitted[name], (
            f"{name}: SHIPPED/ADMITTED mismatch: "
            f"unadmitted={sorted(shipped[name] - admitted[name])}; unadvertised={sorted(admitted[name] - shipped[name])}"
        )


def _assert_emitted_argument_keys(tool: str, raw_arguments: dict[str, Any]) -> None:
    model = redaction.MANIFEST[tool].argument_model
    assert model is not None
    accepted = _model_argument_keys(tool, model)
    validated = model.model_validate(raw_arguments)
    result = redact_tool_call_arguments(tool, raw_arguments, telemetry=NoopRedactionTelemetry())
    # Sparse argument display preserves supplied keys without materializing defaults.
    supplied = frozenset(validated.model_dump(exclude_unset=True))
    assert supplied <= accepted
    assert frozenset(result) == supplied, f"{tool}: redacted emission lost or renamed a supplied root key"


def test_every_shipped_tool_has_complete_admitted_argument_names() -> None:
    _assert_admitted_wire()


def test_duplicate_shipped_definition_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    definitions = get_tool_definitions()
    definitions.append(definitions[0])
    monkeypatch.setitem(globals(), "get_tool_definitions", lambda: definitions)
    with pytest.raises(AssertionError, match="duplicate"):
        _shipped_argument_keys()


@pytest.mark.parametrize("missing", ["definition", "policy"])
def test_missing_relation_endpoint_is_rejected(monkeypatch: pytest.MonkeyPatch, missing: str) -> None:
    if missing == "definition":
        definitions = [definition for definition in get_tool_definitions() if definition["name"] != "list_blobs"]
        monkeypatch.setitem(globals(), "get_tool_definitions", lambda: definitions)
    else:
        monkeypatch.setattr(
            redaction, "MANIFEST", MappingProxyType({name: entry for name, entry in redaction.MANIFEST.items() if name != "list_blobs"})
        )
    with pytest.raises(AssertionError, match=r"registry/manifest mismatch.*list_blobs"):
        _assert_admitted_wire()


def test_closed_empty_policy_cannot_lose_an_advertised_key(monkeypatch: pytest.MonkeyPatch) -> None:
    definitions = get_tool_definitions()
    definition = next(item for item in definitions if item["name"] == "list_blobs")
    definition["parameters"]["properties"]["new_knob"] = {"type": "string"}
    monkeypatch.setitem(globals(), "get_tool_definitions", lambda: definitions)
    with pytest.raises(AssertionError, match=r"list_blobs.*new_knob"):
        _assert_admitted_wire()


def test_type_driven_model_cannot_lose_an_advertised_key(monkeypatch: pytest.MonkeyPatch) -> None:
    class EmptyArguments(BaseModel):
        model_config = ConfigDict(extra="forbid")

    replacement = ToolRedaction(argument_model=EmptyArguments)
    monkeypatch.setattr(redaction, "MANIFEST", MappingProxyType({**redaction.MANIFEST, "get_blob_content": replacement}))
    with pytest.raises(AssertionError, match=r"get_blob_content.*blob_id"):
        _assert_admitted_wire()


def test_type_driven_model_cannot_add_an_unadvertised_key(monkeypatch: pytest.MonkeyPatch) -> None:
    class ExtendedArguments(BaseModel):
        blob_id: str
        hidden: str = "default"
        model_config = ConfigDict(extra="forbid")

    replacement = ToolRedaction(argument_model=ExtendedArguments)
    monkeypatch.setattr(redaction, "MANIFEST", MappingProxyType({**redaction.MANIFEST, "get_blob_content": replacement}))
    with pytest.raises(AssertionError, match=r"get_blob_content.*hidden"):
        _assert_admitted_wire()


@pytest.mark.parametrize("alias", ["wire_id", AliasChoices("wire_id", "alternate"), AliasPath("wrapped", "wire_id")])
def test_unresolved_validation_alias_is_not_field_name_parity(alias: str | AliasChoices | AliasPath) -> None:
    class AliasedArguments(BaseModel):
        blob_id: str = Field(validation_alias=alias)
        model_config = ConfigDict(extra="forbid")

    raw = {"wrapped": {"wire_id": "blob"}} if isinstance(alias, AliasPath) else {"wire_id": "blob"}
    assert AliasedArguments.model_validate(raw).blob_id == "blob"
    with pytest.raises(AssertionError, match="unsupported validation alias"):
        _model_argument_keys("probe", AliasedArguments)


def test_input_alias_and_serialization_alias_are_distinct_relations() -> None:
    class InputAlias(BaseModel):
        blob_id: str = Field(alias="wire_id")
        model_config = ConfigDict(extra="forbid")

    class OutputAlias(BaseModel):
        blob_id: str = Field(serialization_alias="emitted_id")
        model_config = ConfigDict(extra="forbid", serialize_by_alias=True)

    assert InputAlias.model_validate({"wire_id": "blob"}).blob_id == "blob"
    assert OutputAlias.model_validate({"blob_id": "blob"}).model_dump() == {"emitted_id": "blob"}
    with pytest.raises(AssertionError, match="unsupported input alias"):
        _model_argument_keys("probe", InputAlias)
    with pytest.raises(AssertionError, match="unsupported serialization alias"):
        _model_argument_keys("probe", OutputAlias)


@pytest.mark.parametrize("tool", [name for name, entry in redaction.MANIFEST.items() if entry.argument_model is not None])
def test_type_driven_admitted_keys_reach_actual_redacted_output(tool: str) -> None:
    # Explicit JSON witnesses keep this root-name claim separate from the
    # sensitive-value property generators in the existing completeness suite.
    _assert_emitted_argument_keys(tool, deepcopy(_TYPE_DRIVEN_INPUTS[tool]))


def test_emitted_key_loss_is_detected(monkeypatch: pytest.MonkeyPatch) -> None:
    original = redact_tool_call_arguments

    def drop_blob_id(tool: str, arguments: dict[str, Any], *, telemetry: NoopRedactionTelemetry) -> dict[str, Any]:
        result = original(tool, arguments, telemetry=telemetry)
        del result["blob_id"]
        return result

    _assert_emitted_argument_keys("get_blob_content", {"blob_id": "blob"})
    monkeypatch.setitem(globals(), "redact_tool_call_arguments", drop_blob_id)
    with pytest.raises(AssertionError, match="emission lost or renamed"):
        _assert_emitted_argument_keys("get_blob_content", {"blob_id": "blob"})


def test_all_declarative_policies_close_unknown_arguments() -> None:
    for name, entry in redaction.MANIFEST.items():
        if entry.policy is not None:
            assert policy_closes_unknown_arguments(entry.policy), name


@pytest.mark.parametrize("tool", [name for name, entry in redaction.MANIFEST.items() if entry.policy is not None])
def test_actual_declarative_redactor_drops_unknown_name_and_value(tool: str) -> None:
    # This is redaction admission, not a claim that a handler accepts missing
    # required fields. Absent sensitive fields are the existing walker no-op.
    arguments = {"unadvertised_private_key": "unadvertised-private-value"}
    original = deepcopy(arguments)
    result = redact_tool_call_arguments(tool, arguments, telemetry=NoopRedactionTelemetry())
    assert result == {REDACTED_UNKNOWN_ARGUMENTS_FIELD: REDACTED_UNKNOWN_ARGUMENT_KEY}
    assert arguments == original


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("get_plugin_schema", {"plugin_type": "source", "name": "csv"}),
        ("get_plugin_assistance", {"plugin_type": "source", "plugin_name": "csv", "issue_code": None}),
        ("explain_validation_error", {"error_text": "quarantine_unknown_output"}),
        ("get_pipeline_state", {"component": "sources"}),
        ("list_models", {"provider": "example/", "limit": 1}),
    ],
)
def test_closed_discovery_preserves_supplied_values_and_omissions(tool: str, arguments: dict[str, Any]) -> None:
    schema = next(definition["parameters"] for definition in get_tool_definitions() if definition["name"] == tool)
    Draft202012Validator(schema).validate(arguments)
    assert redact_tool_call_arguments(tool, arguments, telemetry=NoopRedactionTelemetry()) == arguments
    assert redact_tool_call_arguments(tool, {}, telemetry=NoopRedactionTelemetry()) == {}


def test_closure_uses_allowlist_even_without_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    policy = redaction.MANIFEST["request_advisor_hint"].policy
    assert policy is not None and policy.known_argument_keys and not policy.redact_unknown_argument_keys
    assert policy_closes_unknown_arguments(policy)
    assert "request_advisor_hint" in _admitted_argument_keys()
    assert redact_tool_call_arguments("request_advisor_hint", {"unknown": "private"}, telemetry=NoopRedactionTelemetry()) == {
        REDACTED_UNKNOWN_ARGUMENTS_FIELD: REDACTED_UNKNOWN_ARGUMENT_KEY
    }
    monkeypatch.setitem(globals(), "policy_closes_unknown_arguments", lambda value: value.redact_unknown_argument_keys)
    with pytest.raises(AssertionError, match="open argument policy"):
        _admitted_argument_keys()
