"""Every registered plugin exposes its declarations to the field-name spelling rule.

Operator ruling 2026-09-25 (elspeth-5887fb7928): a DECLARATION names a field as
rows carry it, and a header spelling is refused — at build where the upstream
proves it (``validate_declared_field_spellings``), per row otherwise (the
transform, batch and sink preflights). All of those read one surface per plugin:
``declared_read_fields`` (transforms, batch transforms, sinks) and
``declared_created_fields`` (transforms). A declaration that stays off it is
silently exempt from the rule — the inert-declaration defect the rule exists to
close — so these sweeps construct every registered plugin from the live
registry and require each declaration surface to land there.

The sink sweep covers the four sinks no local run can reach (aws_s3,
azure_blob, chroma_sink, dataverse): they share the schema-model construction
the runnable sinks do, and this is the proof their declarations are seen.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any, cast

import pytest

from elspeth.contracts.field_spelling import DeclaredSpellings
from elspeth.plugins.infrastructure.base import BaseTransform
from elspeth.plugins.infrastructure.manager import get_shared_plugin_manager
from elspeth.plugins.infrastructure.preflight import plugin_preflight_mode
from elspeth.web.composer.state import _probe_sink_declarations
from tests.unit.web.composer.test_sink_required_fields_parity import _minimal_sink_configs

# 'id' and 'body' keep chroma_sink constructible (its id/document fields must be declared).
_HEADER_SCHEMA: dict[str, Any] = {"mode": "flexible", "fields": ["Name: int?", "id: str", "body: str"]}
# Sinks whose custom ``headers`` mapping is keyed by row field names.
_CUSTOM_HEADER_SINKS = frozenset({"aws_s3", "azure_blob", "csv", "json"})


def _registered_sink_names() -> list[str]:
    return sorted(sink_cls.name for sink_cls in get_shared_plugin_manager().get_sinks())


def _registered_transforms() -> list[type[BaseTransform]]:
    # Every registered transform subclasses BaseTransform (the harness convention
    # of tests/invariants/test_pass_through_invariants.py); the cast documents it.
    registered = cast("list[type[BaseTransform]]", get_shared_plugin_manager().get_transforms())
    return sorted(registered, key=lambda cls: cls.name)


@pytest.mark.parametrize("sink_name", _registered_sink_names())
def test_every_sink_exposes_its_schema_declaration(sink_name: str, tmp_path: Path) -> None:
    """A header-spelled schema field is on the sink's read surface — and the composer reads the same set."""
    options = copy.deepcopy(_minimal_sink_configs(str(tmp_path))[sink_name])
    options["schema"] = _HEADER_SCHEMA

    with plugin_preflight_mode(True):
        sink = get_shared_plugin_manager().create_sink(sink_name, options)
    try:
        reads = sink.declared_read_fields
    finally:
        sink.close()

    assert "Name" in reads, f"{sink_name!r} declares 'Name: int?' but its declared_read_fields {sorted(reads)} omit it"
    assert _probe_sink_declarations(sink_name, options).reads == reads
    [spelling] = [
        s for s in DeclaredSpellings.of(reads=reads, creates=()).in_row(row_keys={"name"}, forwarded_keys=()) if s.literal == "Name"
    ]
    assert spelling.canonical == "name"


@pytest.mark.parametrize("sink_name", sorted(_CUSTOM_HEADER_SINKS))
def test_a_custom_headers_key_is_a_read_declaration(sink_name: str, tmp_path: Path) -> None:
    """``apply_display_headers`` matches custom keys to row keys as written; a header-spelled key crashed the write."""
    options = copy.deepcopy(_minimal_sink_configs(str(tmp_path))[sink_name])
    options["headers"] = {"id": "Ident", "Name": "Full"}

    with plugin_preflight_mode(True):
        sink = get_shared_plugin_manager().create_sink(sink_name, options)
    try:
        assert {"id", "Name"} <= sink.declared_read_fields
    finally:
        sink.close()


def test_the_custom_header_roster_is_the_registry_s(tmp_path: Path) -> None:
    """Positive control for the roster above: exactly these sinks accept a custom ``headers`` mapping."""
    accepting = set()
    for sink_name in _registered_sink_names():
        options = copy.deepcopy(_minimal_sink_configs(str(tmp_path))[sink_name])
        options["headers"] = {"id": "Ident"}
        try:
            with plugin_preflight_mode(True):
                sink = get_shared_plugin_manager().create_sink(sink_name, options)
        except ValueError:
            # A sink without a headers option refuses the unknown key.
            continue
        sink.close()
        accepting.add(sink_name)
    assert accepting == _CUSTOM_HEADER_SINKS


def test_dataverse_field_mapping_keys_are_read_declarations(tmp_path: Path) -> None:
    options = copy.deepcopy(_minimal_sink_configs(str(tmp_path))["dataverse"])
    options["field_mapping"] = {"name": "name", "Account": "accountnumber"}

    with plugin_preflight_mode(True):
        sink = get_shared_plugin_manager().create_sink("dataverse", options)
    try:
        assert {"name", "Account"} <= sink.declared_read_fields
    finally:
        sink.close()


@pytest.mark.parametrize("transform_cls", _registered_transforms(), ids=lambda cls: cls.name)
def test_every_transform_reads_what_it_declares(transform_cls: type[BaseTransform]) -> None:
    """Every declared-input and required name is a read; an authored schema field is a read or a creation."""
    config = copy.deepcopy(transform_cls.probe_config())
    transform = transform_cls(config)

    reads = transform.declared_read_fields
    assert transform.declared_input_fields <= reads
    assert transform.schema_required_input_fields() <= reads
    schema_config = transform._schema_config
    assert schema_config is not None
    authored = frozenset(field.name for field in schema_config.fields or ())
    assert authored <= reads | transform.declared_created_fields


def test_type_coerce_conversion_fields_are_declared_inputs() -> None:
    """S7(b): the conversion field is a declaration, projected onto declared_input_fields (Q4 amendment)."""
    from elspeth.plugins.transforms.type_coerce import TypeCoerce

    transform = TypeCoerce({"schema": {"mode": "observed"}, "conversions": [{"field": "Price", "to": "int"}]})

    assert "Price" in transform.declared_input_fields
    assert "Price" in transform.declared_read_fields


def test_value_transform_targets_are_created_names() -> None:
    """S7(c): every operation target is on the create surface, whatever its schema declares."""
    from elspeth.plugins.transforms.value_transform import ValueTransform

    transform = ValueTransform(
        {"schema": {"mode": "flexible", "fields": ["Name: int?"]}, "operations": [{"target": "Name", "expression": "row['name']"}]}
    )

    assert "Name" in transform.declared_created_fields


def test_field_mapper_targets_are_created_and_sources_are_lookups() -> None:
    """Mapping SOURCES are row lookups (either spelling); non-identity TARGETS are created names."""
    from elspeth.plugins.transforms.field_mapper import FieldMapper

    transform = FieldMapper({"schema": {"mode": "observed"}, "mapping": {"Name": "given", "id": "Ident"}})

    assert {"given", "Ident"} <= transform.declared_created_fields
    assert "Name" not in transform.declared_read_fields
