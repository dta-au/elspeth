"""The construction-time schema cache is reused without being aliased out.

U-45 F3 stopped ``_to_summary`` regenerating a schema the cache already held.
That only stays safe while no caller can reach into the cache, so these pin
guarantee G6: per-view projection is unchanged, and a caller that mutates what
it was handed cannot rewrite a later listing or schema read.
"""

from __future__ import annotations

import json

import pytest

from elspeth.plugins.infrastructure.manager import PluginManager
from elspeth.web.catalog.service import CatalogServiceImpl


@pytest.fixture
def catalog() -> CatalogServiceImpl:
    """A fresh service per test: these mutate what a read handed back."""
    manager = PluginManager()
    manager.register_builtin_plugins()
    return CatalogServiceImpl(manager)


def test_a_mutated_schema_read_cannot_rewrite_the_next_one(catalog: CatalogServiceImpl) -> None:
    first = catalog.get_schema("transform", "llm")
    original_keys = sorted(first.json_schema)

    first.json_schema["_injected_by_a_caller"] = True
    first.json_schema.pop(original_keys[0])

    second = catalog.get_schema("transform", "llm")
    assert "_injected_by_a_caller" not in second.json_schema
    assert sorted(second.json_schema) == original_keys


def test_a_mutated_knob_schema_read_cannot_rewrite_the_next_one(catalog: CatalogServiceImpl) -> None:
    first = catalog.get_schema("transform", "llm")
    original_keys = sorted(first.knob_schema)

    first.knob_schema["_injected_by_a_caller"] = True

    assert sorted(catalog.get_schema("transform", "llm").knob_schema) == original_keys


def test_a_mutated_schema_read_cannot_rewrite_a_later_listing(catalog: CatalogServiceImpl) -> None:
    before = catalog.list_transforms()

    handed_out = catalog.get_schema("transform", "llm")
    handed_out.json_schema.clear()

    assert catalog.list_transforms() == before


def test_the_listing_still_projects_every_plugin_its_own_config_fields(catalog: CatalogServiceImpl) -> None:
    """Reuse must not collapse per-plugin projection onto one shared answer."""
    summaries = {summary.name: summary for summary in catalog.list_transforms()}
    assert len(summaries) > 1

    field_sets = {name: tuple(sorted(field.name for field in summary.config_fields)) for name, summary in summaries.items()}

    assert len(set(field_sets.values())) > 1, f"every transform projected identical config fields: {field_sets}"


def _mutable_default_field(catalog: CatalogServiceImpl) -> tuple[str, str]:
    for summary in catalog.list_transforms():
        for field in summary.config_fields:
            if isinstance(field.default, list) and field.default:
                return summary.name, field.name
    raise AssertionError("no transform exposes a non-empty list default, so this test measures nothing")


def test_a_mutated_listing_default_cannot_rewrite_the_cache(catalog: CatalogServiceImpl) -> None:
    """A config-field default is handed out by value, not by reference.

    Found by review: before the construction-time schema was reused, a default
    pointed into a freshly generated document and could not reach the cache.
    After the reuse it can, so the summary copies it.
    """
    name, field_name = _mutable_default_field(catalog)
    handed_out = next(f for s in catalog.list_transforms() if s.name == name for f in s.config_fields if f.name == field_name)
    expected = list(handed_out.default)

    handed_out.default.append("__injected_by_a_caller__")

    again = next(f for s in catalog.list_transforms() if s.name == name for f in s.config_fields if f.name == field_name)
    assert again.default == expected
    assert "__injected_by_a_caller__" not in json.dumps(catalog.get_schema("transform", name).json_schema)


def test_a_reassigned_secret_requirement_cannot_rewrite_the_cache(catalog: CatalogServiceImpl) -> None:
    """Nested requirement records are copied too, not just the schema dicts.

    Their candidates feed the plugin-availability check, so a caller editing
    them would change what later callers are told is available. ``candidates``
    is a tuple, so the vector is attribute reassignment rather than in-place
    mutation: ``_StrictResponse`` sets ``strict`` and ``extra="forbid"`` but not
    ``frozen``, so the shared record accepts a new value.
    """
    name = next(
        (s.name for s in catalog.list_transforms() if catalog.get_schema("transform", s.name).secret_requirements),
        None,
    )
    assert name is not None, "no transform declares a secret requirement, so this test measures nothing"
    requirement = catalog.get_schema("transform", name).secret_requirements[0]
    expected = requirement.candidates

    requirement.candidates = (*expected, "__injected_by_a_caller__")

    assert catalog.get_schema("transform", name).secret_requirements[0].candidates == expected
