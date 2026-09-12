"""Tests for the composer wire census: the MODEL wire has two loci.

A type-driven tool's model is the redaction manifest's ``argument_model``; a
declarative tool's model is the handler-side ``_*ArgumentsModel`` passed to
``_validate_mutation_arguments``. The census must see both, and every tool the
live registry ships must land in exactly one row.
"""

from scripts.cicd.composer_wire_census import census_model_wire


def test_type_driven_tools_take_their_model_from_the_manifest() -> None:
    rows = census_model_wire()
    assert rows["set_pipeline"].site == "manifest"
    assert "nodes" in rows["set_pipeline"].model_fields


def test_declarative_tools_take_their_model_from_the_handler() -> None:
    rows = census_model_wire()
    assert rows["upsert_node"].site.startswith("handler:")
    assert rows["upsert_node"].model_class == "_UpsertNodeArgumentsModel"


def test_every_registry_tool_has_a_row() -> None:
    from elspeth.web.composer.tools._dispatch import get_tool_definitions

    assert set(census_model_wire()) == {d["name"] for d in get_tool_definitions()}
