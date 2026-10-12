"""Named reference tables work through the existing Composer blob protocol."""

import hashlib
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest

from elspeth.contracts.blobs import BlobRecord
from elspeth.contracts.freeze import deep_thaw
from elspeth.plugins.infrastructure.manager import get_shared_plugin_manager
from elspeth.web.catalog.service import CatalogServiceImpl
from elspeth.web.composer import yaml_generator
from elspeth.web.composer.tools import _prevalidate_plugin_options
from elspeth.web.composer.tools.sessions import build_set_pipeline_candidate
from elspeth.web.composer.tools.transforms import _execute_patch_node_options, _execute_upsert_node
from elspeth.web.execution.validation import validate_pipeline_for_trained_operator
from tests.unit.web.composer.test_authoring_reconciliation import _exact_arguments, _node, _state, _trained_context
from tests.unit.web.execution.test_validate_blob_inline import BLOB_ID, _ready_blob_record, _state_with_reference_join


def _uploaded_table_options() -> dict[str, Any]:
    return {
        "schema": {"mode": "observed"},
        "tables": {
            "products": {
                "reference_format": "csv",
                "reference_content": {"blob_ref": str(BLOB_ID), "mode": "inline_content", "sha256": "a" * 64},
            }
        },
        "lookups": [
            {"table": "products", "key_field": "product", "reference_key_name": "sku", "output": {"description": "ref['description']"}}
        ],
    }


@pytest.mark.parametrize("mutation", ["set_pipeline", "upsert_node", "patch_node_options"])
def test_uploaded_named_table_survives_authoring_and_full_state_roundtrip(mutation: str) -> None:
    options = _uploaded_table_options()
    node = _node(node_id="join", plugin="reference_join", options=options)
    previous = _state(nodes=(node,))
    context = _trained_context()
    arguments = deep_thaw(_exact_arguments(previous).data)
    if mutation == "set_pipeline":
        result = build_set_pipeline_candidate(arguments, replace(previous, nodes=()), context).result
    elif mutation == "upsert_node":
        result = _execute_upsert_node(arguments["nodes"][0], previous, context)
    else:
        result = _execute_patch_node_options({"node_id": "join", "patch": {"required_input_fields": ["product"]}}, previous, context)

    assert result.success, result.validation
    assert (
        result.updated_state.nodes[0].options["tables"]["products"]["reference_content"]
        == options["tables"]["products"]["reference_content"]
    )
    roundtrip = build_set_pipeline_candidate(deep_thaw(_exact_arguments(result.updated_state).data), result.updated_state, context).result
    assert roundtrip.success, roundtrip.validation


@pytest.mark.parametrize("invalid", ["format", "extra", "hash", "mode", "lookup", "emitted_default"])
def test_deferred_named_table_keeps_other_authoring_errors(invalid: str) -> None:
    options = _uploaded_table_options()
    table = options["tables"]["products"]
    if invalid == "format":
        table["reference_format"] = "invalid"
    elif invalid == "extra":
        table["unknown"] = table["reference_content"]
    elif invalid == "hash":
        table["reference_content"]["sha256"] = "bad"
    elif invalid == "mode":
        table["reference_content"]["mode"] = "bind_source"
    elif invalid == "lookup":
        options["lookups"][0]["key_field"] = 42
    else:
        options["lookups"][0]["on_miss"] = "default"
        options["lookups"][0]["default_values"] = {"description": "${REFERENCE_SECRET}"}

    error = _prevalidate_plugin_options("transform", "reference_join", options)
    assert error is not None
    if invalid == "emitted_default":
        assert "lookups[0].default_values must not contain environment-variable placeholders" in error


@pytest.mark.parametrize("deferred", [False, True], ids=["inline", "blob"])
@pytest.mark.parametrize("field", ["default_values", "output"])
@pytest.mark.parametrize("placeholder", [False, True], ids=["ordinary", "environment-placeholder"])
def test_nested_emitted_placeholder_parity_preserves_literal_reference_content(deferred: bool, field: str, placeholder: bool) -> None:
    options = _uploaded_table_options()
    if not deferred:
        options["tables"]["products"]["reference_content"] = "sku,description\nhat,${REFERENCE_SECRET}\n"
    lookup = options["lookups"][0]
    lookup["on_miss"] = "default"
    lookup["default_values"] = {"description": "ordinary fallback"}
    if placeholder:
        lookup[field] = {"description": "'${REFERENCE_SECRET}'"}
    error = _prevalidate_plugin_options("transform", "reference_join", options)
    if placeholder:
        assert error is not None
        assert f"lookups[0].{field} must not contain environment-variable placeholders" in error
    else:
        assert error is None


def test_named_uploaded_table_materializes_for_multiple_lookups(tmp_path: Path) -> None:
    session_id = uuid4()
    table = b"sku,description,price\nhats,A fine hat,2.00\n"
    digest = hashlib.sha256(table).hexdigest()
    state = _state_with_reference_join(tmp_path, session_id=session_id, reference_format="csv", content=table.decode())
    state = replace(
        state,
        nodes=(
            replace(
                state.nodes[0],
                options={
                    "schema": {"mode": "observed"},
                    "tables": {
                        "products": {
                            "reference_format": "csv",
                            "reference_content": {"blob_ref": str(BLOB_ID), "mode": "inline_content", "sha256": digest},
                        }
                    },
                    "lookups": [
                        {
                            "table": "products",
                            "key_field": "product",
                            "reference_key_name": "sku",
                            "output": {"description": "ref['description']"},
                        },
                        {"table": "products", "key_field": "product", "reference_key_name": "sku", "output": {"price": "ref['price']"}},
                    ],
                },
            ),
        ),
    )
    record = replace(
        _ready_blob_record(session_id=session_id, size_bytes=len(table)),
        filename="products.csv",
        mime_type="text/csv",
        content_hash=digest,
    )
    reads: list[UUID] = []

    def get_content(blob_id: UUID) -> tuple[BlobRecord, bytes]:
        reads.append(blob_id)
        return record, table

    result = validate_pipeline_for_trained_operator(
        state,
        SimpleNamespace(data_dir=tmp_path),
        yaml_generator,
        blob_get_metadata=lambda _blob_id: record,
        blob_get_content=get_content,
        session_id=str(session_id),
    )

    assert result.is_valid, result.errors
    assert reads == [BLOB_ID]


def test_catalog_exposes_typed_lookup_list_and_table_map() -> None:
    catalog = CatalogServiceImpl(get_shared_plugin_manager())
    schema = catalog.get_schema("transform", "reference_join").json_schema
    properties = schema["properties"]
    assert "tables" in properties
    assert "lookups" in properties
    # The complete public schema must describe the entry, not an arbitrary dict.
    lookup_schema = next(item for item in properties["lookups"]["anyOf"] if item.get("type") == "array")
    lookup_ref = lookup_schema["items"]["$ref"].rsplit("/", 1)[-1]
    assert set(schema["$defs"][lookup_ref]["required"]) >= {"table", "key_field", "reference_key_name", "output"}
