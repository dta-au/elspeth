"""Honesty-gate tests for the raising Tier-3 schema-projection boundaries.

``planner_authoring_aids._contract_discriminator`` / ``_contract_json_schema``
/ ``_contract_knob_schema`` each parse a plugin-declared JSON-Schema-shaped
fragment (the raw output of a plugin's ``ConfigModel.model_json_schema()``,
surfaced via ``PolicyCatalogView.get_schema``) and are decorated
``@trust_boundary``. The ``trust_boundary.tests`` elspeth-lints rule requires
a ``test_ref`` whose own body directly calls the decorated function through
its declared ``source_param`` inside a raising assertion — these tests are
that direct call, kept separate from the exemplar-validation suite in
``test_planner_authoring_aids.py`` so each stays a thin, focused unit test.
"""

from __future__ import annotations

import pytest

from elspeth.web.composer.planner_authoring_aids import (
    _contract_discriminator,
    _contract_json_schema,
    _contract_knob_schema,
    _SchemaContractProjectionUnsupported,
)


def test_contract_discriminator_rejects_unknown_key() -> None:
    with pytest.raises(_SchemaContractProjectionUnsupported):
        _contract_discriminator({"propertyName": "kind", "unexpected": True})


def test_contract_discriminator_accepts_known_shape() -> None:
    projected = _contract_discriminator({"propertyName": "kind", "mapping": {"a": "b"}})
    assert projected == {"propertyName": "kind", "mapping": {"a": "b"}}


def test_contract_json_schema_rejects_non_dict_non_bool() -> None:
    with pytest.raises(_SchemaContractProjectionUnsupported):
        _contract_json_schema("not-a-schema")


def test_contract_json_schema_accepts_bare_bool() -> None:
    assert _contract_json_schema(True) is True
    assert _contract_json_schema(False) is False


def test_contract_knob_schema_rejects_missing_fields_key() -> None:
    with pytest.raises(_SchemaContractProjectionUnsupported):
        _contract_knob_schema({"not_fields": []})


def test_contract_knob_schema_accepts_empty_fields() -> None:
    assert _contract_knob_schema({"fields": []}) == {"fields": []}
