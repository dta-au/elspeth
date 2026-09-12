"""The scorecard joins real extraction authorities and exposes their limits."""

import sys
from dataclasses import replace
from types import MappingProxyType

import pytest
from scripts.cicd import composer_wire_census as census

from elspeth.contracts.freeze import deep_thaw
from elspeth.web.composer.tools.generation import GetPluginSchemaArgumentsModel


def _lost_name_read(arguments, state, context):
    validated = GetPluginSchemaArgumentsModel.model_validate(arguments)
    return validated.plugin_type


def test_live_scorecard_accounts_for_every_wire_and_empty_sets() -> None:
    report = census.census_scorecard()
    assert report["failures"] == []
    definitions = census.get_tool_definitions()
    assert report["tool_count"] == len(definitions)
    assert {row["tool"] for row in report["rows"]} == {definition["name"] for definition in definitions}
    empty = next(row for row in report["rows"] if row["tool"] == "list_blobs")
    assert empty["SHIPPED"]["root_keys"] == empty["ADMITTED"]["accepted_wire_names"] == []
    assert empty["MODEL"]["accepted_wire_names"] == []
    assert not empty["MODEL"]["unresolved"]
    assert empty["ADMITTED"]["mode"] == "closed_declarative"
    assert "not runtime" in " ".join(report["limitations"])
    assert "was not executed" in " ".join(report["limitations"])


@pytest.mark.parametrize("family", ["MODEL", "READ", "TAUGHT", "ADMITTED", "FRONTEND"])
@pytest.mark.parametrize("defect", ["missing", "duplicate", "wrong_identity"])
def test_join_refuses_missing_duplicate_or_wrong_family_endpoint(monkeypatch, family, defect) -> None:
    sources = {
        "MODEL": ("census_model_wire", census.census_model_wire),
        "READ": ("census_read_wire", census.census_read_wire),
        "TAUGHT": ("census_taught_wire", census.census_taught_wire),
        "ADMITTED": ("admitted_wire_rows", census.admitted_wire_rows),
        "FRONTEND": ("census_frontend_wire", census.census_frontend_wire),
    }
    name, original = sources[family]

    def altered(*args, **kwargs):
        rows = original(*args, **kwargs)
        if defect == "missing":
            del rows["list_blobs"]
        elif defect == "duplicate":
            rows["duplicate"] = rows["list_blobs"]
        else:
            rows["list_blobs"] = replace(rows["list_blobs"], tool="unregistered")
        return rows

    assert census.census_scorecard()["failures"] == []
    monkeypatch.setattr(census, name, altered)
    with pytest.raises(census.CensusError, match=family):
        census.census_scorecard()


def test_duplicate_shipped_definition_is_not_overwritten(monkeypatch) -> None:
    definitions = census.get_tool_definitions()
    definitions.append(definitions[0])
    monkeypatch.setattr(census, "get_tool_definitions", lambda: definitions)
    with pytest.raises(census.CensusError, match="duplicate tool"):
        census.census_scorecard()


def test_actual_closed_empty_policy_detects_new_advertised_key(monkeypatch) -> None:
    definitions = census.get_tool_definitions()
    next(item for item in definitions if item["name"] == "list_blobs")["parameters"]["properties"]["new_knob"] = {"type": "string"}
    monkeypatch.setattr(census, "get_tool_definitions", lambda: definitions)
    row = next(row for row in census.census_scorecard()["rows"] if row["tool"] == "list_blobs")
    assert row["differences"]["ADMITTED"] == ["new_knob"]
    assert row["ADMITTED"]["accepted_wire_names"] == []


def test_actual_handler_lost_read_is_not_blessed_by_model(monkeypatch) -> None:
    declarations = tuple(
        replace(item, handler=_lost_name_read, json_schema=deep_thaw(item.json_schema)) if item.name == "get_plugin_schema" else item
        for item in census._REGISTERED_TOOLS
    )
    monkeypatch.setattr(census, "_REGISTERED_TOOLS", declarations)
    row = next(row for row in census.census_scorecard()["rows"] if row["tool"] == "get_plugin_schema")
    assert row["differences"]["MODEL"] == []
    assert row["differences"]["READ"] == ["name"]
    assert row["READ"]["unresolved"] == []


def test_actual_teaching_loss_cannot_borrow_other_tool_prose(monkeypatch) -> None:
    definitions = census.get_tool_definitions()
    target = next(item for item in definitions if item["name"] == "get_blob_metadata")
    target["parameters"]["properties"]["blob_id"].pop("description", None)
    target["description"] = "Read metadata."
    next(item for item in definitions if item["name"] == "list_blobs")["description"] = "Other tool mentions `blob_id`."
    monkeypatch.setattr(census, "get_tool_definitions", lambda: definitions)
    monkeypatch.setattr(census, "build_system_prompt", lambda state: "Other tool uses `blob_id`.")
    row = next(row for row in census.census_scorecard()["rows"] if row["tool"] == "get_blob_metadata")
    assert row["differences"]["TAUGHT"] == ["blob_id"]


def test_actual_manifest_endpoint_removal_is_not_not_applicable(monkeypatch) -> None:
    monkeypatch.setattr(census, "MANIFEST", MappingProxyType({k: v for k, v in census.MANIFEST.items() if k != "list_blobs"}))
    with pytest.raises(census.CensusError, match=r"ADMITTED.*list_blobs"):
        census.census_scorecard()


def test_existing_schema_authority_survives_root_key_equality(monkeypatch) -> None:
    definitions = census.get_tool_definitions()
    schema = next(item["parameters"] for item in definitions if item["name"] == "upsert_node")
    schema["required"] = [*schema["required"], "description"]
    monkeypatch.setattr(census, "get_tool_definitions", lambda: definitions)
    row = next(row for row in census.census_scorecard()["rows"] if row["tool"] == "upsert_node")
    assert row["differences"]["MODEL"] == []
    assert row["MODEL"]["schema_failure"]
    assert any("MODEL schema" in problem for problem in row["problems"])


def test_cli_extraction_failure_is_nonzero_and_names_source(monkeypatch, capsys) -> None:
    from scripts.cicd import composer_frontend_wire as frontend

    monkeypatch.setattr(sys, "argv", ["census", "--scorecard"])
    monkeypatch.setattr(frontend, "FIXTURE_PATH", frontend.FIXTURE_PATH.with_name("missing-scorecard-fixture.json"))
    assert census.main() == 1
    assert "missing-scorecard-fixture.json" in capsys.readouterr().out
