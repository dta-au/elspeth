"""Named reference tables work through the existing Composer blob protocol."""

import hashlib
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

from elspeth.contracts.blobs import BlobRecord
from elspeth.plugins.infrastructure.manager import get_shared_plugin_manager
from elspeth.web.catalog.service import CatalogServiceImpl
from elspeth.web.composer import yaml_generator
from elspeth.web.execution.validation import validate_pipeline_for_trained_operator
from tests.unit.web.execution.test_validate_blob_inline import BLOB_ID, _ready_blob_record, _state_with_reference_join


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
