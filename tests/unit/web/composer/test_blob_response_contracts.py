"""Selected blob contracts retain legacy wire bytes and reject corrupt producers."""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from pydantic import BaseModel, ValidationError

from elspeth.contracts.errors import AuditIntegrityError, FrameworkBugError
from elspeth.contracts.freeze import deep_freeze, deep_thaw
from elspeth.web.composer.response_contracts import _AdmittedResponse
from elspeth.web.composer.tools import blobs
from elspeth.web.sessions.engine import create_session_engine
from tests.unit.web.composer.test_set_pipeline_candidate import _empty_state, _trained_context

_ID = "00000000-0000-4000-8000-000000000001"
_RECORD = {
    "id": _ID,
    "filename": "évidence.csv",
    "mime_type": "text/csv",
    "size_bytes": 8,
    "content_hash": "a" * 64,
    "status": "ready",
    "created_by": "assistant",
    "creation_modality": "verbatim",
}
_CONTENT = {
    "blob_id": _ID,
    "filename": "évidence.csv",
    "mime_type": "text/csv",
    "content": "\u03b1\ntext",
    "truncated": False,
    "size_bytes": 8,
    "created_by": "assistant",
    "creation_modality": "verbatim",
}
_METADATA = {key: _RECORD[key] for key in ("id", "filename", "mime_type", "size_bytes", "content_hash", "status")}
_INVENTORY = {key: _RECORD[key] for key in ("id", "filename", "mime_type", "size_bytes", "created_by", "creation_modality", "status")}
_INLINE = {"blob_id": _ID, "mime_type": "text/csv", "size_bytes": 8, "content_hash": "a" * 64, "filename": "évidence.csv"}
_CASES = [
    pytest.param(blobs._BLOB_CONTENT_RESPONSE, _CONTENT, id="content"),
    pytest.param(blobs._BLOB_METADATA_RESPONSE, _METADATA, id="metadata"),
    pytest.param(blobs._BLOB_INVENTORY_RESPONSE, [_INVENTORY], id="inventory"),
    pytest.param(blobs._COMPOSER_BLOBS_RESPONSE, {"blobs": [_INLINE]}, id="composer-list"),
]


@pytest.mark.parametrize(("contract", "payload"), _CASES)
@pytest.mark.parametrize("frozen", [False, True])
def test_selected_contract_preserves_root_key_order_and_json_bytes(contract: Any, payload: Any, frozen: bool) -> None:
    raw = deep_freeze(payload) if frozen else deepcopy(payload)
    admitted = contract.admit(raw)
    assert json.dumps(admitted.to_wire()) == json.dumps(payload)
    assert admitted.readmit(contract).to_wire() == payload
    assert admitted.to_wire() is not admitted.to_wire()


@pytest.mark.parametrize(("contract", "payload"), _CASES)
@pytest.mark.parametrize("defect", ["root", "extra", "missing", "scalar", "bool_size", "origin", "foreign_model"])
def test_malformed_producer_data_fails_with_fixed_safe_framework_error(contract: Any, payload: Any, defect: str) -> None:
    value = deepcopy(payload)
    record = value[0] if type(value) is list else value["blobs"][0] if "blobs" in value else value
    if defect == "root":
        value = "PRIVATE_REJECTED_VALUE"
    elif defect == "extra":
        record["PRIVATE_REJECTED_KEY"] = "PRIVATE_REJECTED_VALUE"
    elif defect == "missing":
        del record["filename"]
    elif defect == "scalar":
        record["filename"] = {"PRIVATE_REJECTED_KEY": "PRIVATE_REJECTED_VALUE"}
    elif defect == "bool_size":
        record["size_bytes"] = True
    elif defect == "origin":
        if "created_by" in record:
            record["created_by"] = "PRIVATE_REJECTED_VALUE"
        else:
            record["content_hash"] = 12
    else:

        class ForeignModel(BaseModel):
            filename: str = "PRIVATE_REJECTED_VALUE"

        value = ForeignModel()
    with pytest.raises(FrameworkBugError) as caught:
        contract.admit(value)
    assert str(caught.value) == "Blob discovery producer returned an invalid response"
    assert caught.value.__cause__ is None


@pytest.mark.parametrize(("contract", "payload"), _CASES)
@pytest.mark.parametrize("corruption", ["scalar", "extra", "missing"])
def test_cache_readmit_revalidates_mutated_exact_owned_value(contract: Any, payload: Any, corruption: str) -> None:
    admitted = contract.admit(deep_freeze(payload))
    assert isinstance(admitted, _AdmittedResponse)
    owned = admitted.value
    if contract is blobs._BLOB_INVENTORY_RESPONSE:
        record = owned[0]
    elif contract is blobs._COMPOSER_BLOBS_RESPONSE:
        record = owned.blobs[0]
    else:
        record = owned
    if corruption == "scalar":
        object.__setattr__(record, "size_bytes", True)
    elif corruption == "extra":
        object.__setattr__(record, "PRIVATE_REJECTED_KEY", "PRIVATE_REJECTED_VALUE")
    else:
        object.__delattr__(record, "filename")
    with pytest.raises(FrameworkBugError, match="Blob discovery producer returned an invalid response"):
        admitted.readmit(contract)


@pytest.mark.parametrize(("contract", "payload"), _CASES)
def test_admitted_closed_records_are_immutable(contract: Any, payload: Any) -> None:
    admitted = contract.admit(payload)
    assert isinstance(admitted, _AdmittedResponse)
    owned = admitted.value
    record = (
        owned[0] if contract is blobs._BLOB_INVENTORY_RESPONSE else owned.blobs[0] if contract is blobs._COMPOSER_BLOBS_RESPONSE else owned
    )
    with pytest.raises(ValidationError, match="frozen"):
        record.filename = "replacement"
    if contract is blobs._COMPOSER_BLOBS_RESPONSE:
        with pytest.raises(FrozenInstanceError):
            owned.blobs = ()


def test_descriptor_null_hash_rejects_while_metadata_null_remains_explicit() -> None:
    metadata = {**_METADATA, "content_hash": None, "status": "pending"}
    assert blobs._BLOB_METADATA_RESPONSE.admit(metadata).to_wire() == metadata
    with pytest.raises(FrameworkBugError):
        blobs._COMPOSER_BLOBS_RESPONSE.admit({"blobs": [{**_INLINE, "content_hash": None}]})


def test_descriptor_nominal_subclass_is_not_an_admission_fast_pass() -> None:
    class Impostor(blobs._BlobInlineResponse):
        private_field: str = "PRIVATE_REJECTED_VALUE"

    value = blobs._ComposerBlobsResponse(blobs=(Impostor.model_validate(_INLINE),))
    with pytest.raises(FrameworkBugError):
        blobs._COMPOSER_BLOBS_RESPONSE.admit(value)


def test_composer_wrapper_root_and_owned_tuple_are_closed() -> None:
    with pytest.raises(FrameworkBugError):
        blobs._COMPOSER_BLOBS_RESPONSE.admit({"blobs": [], "PRIVATE_REJECTED_KEY": "PRIVATE_REJECTED_VALUE"})
    admitted = blobs._COMPOSER_BLOBS_RESPONSE.admit({"blobs": [_INLINE]})
    assert isinstance(admitted, _AdmittedResponse)
    object.__setattr__(admitted.value, "blobs", list(admitted.value.blobs))
    with pytest.raises(FrameworkBugError):
        admitted.readmit(blobs._COMPOSER_BLOBS_RESPONSE)


def test_content_scalar_and_origin_vocabularies_are_closed() -> None:
    for key, value in (("truncated", 1), ("size_bytes", "8"), ("creation_modality", "PRIVATE_REJECTED_VALUE")):
        with pytest.raises(FrameworkBugError):
            blobs._BLOB_CONTENT_RESPONSE.admit({**_CONTENT, key: value})


@pytest.mark.parametrize(("contract", "payload"), _CASES)
def test_successful_absence_and_duck_serializers_are_not_admitted(contract: Any, payload: Any) -> None:
    class Mimic:
        def to_dict(self) -> Any:
            return payload

    for value in (None, Mimic()):
        with pytest.raises(FrameworkBugError):
            contract.admit(value)


@pytest.mark.parametrize("empty", [False, True])
def test_actual_list_producers_preserve_legacy_roots_and_order(empty: bool) -> None:
    context = _trained_context(
        data_dir=Path("/tmp/blob-response"), session_id="response", session_engine=create_session_engine("sqlite://")
    )
    inventory = [] if empty else [_INVENTORY, {**_INVENTORY, "id": "00000000-0000-4000-8000-000000000002", "created_by": "user"}]
    descriptors = [] if empty else [_INLINE]
    with patch.object(blobs, "_sync_list_blobs", autospec=True, return_value=inventory):
        result = blobs._handle_list_blobs({}, _empty_state(), context)
    contract = blobs._LIST_BLOBS_DECLARATION.response_contract
    assert contract is blobs._BLOB_INVENTORY_RESPONSE
    assert json.dumps(contract.admit(result.data).to_wire()) == json.dumps(inventory)
    with patch.object(blobs, "_sync_list_ready_blob_inline_descriptors", autospec=True, return_value=descriptors):
        result = blobs._handle_list_composer_blobs({}, _empty_state(), context)
    contract = blobs._LIST_COMPOSER_BLOBS_DECLARATION.response_contract
    assert contract is blobs._COMPOSER_BLOBS_RESPONSE
    assert json.dumps(contract.admit(result.data).to_wire()) == json.dumps({"blobs": descriptors})


@pytest.mark.parametrize("large", [False, True])
@pytest.mark.parametrize("created_by", ["user", "assistant", "pipeline"])
def test_actual_content_producer_preserves_truncation_and_origin_pair(large: bool, created_by: str) -> None:
    context = _trained_context(
        data_dir=Path("/tmp/blob-response"), session_id="response", session_engine=create_session_engine("sqlite://")
    )
    text = "x" * 50001 if large else "\u03b1\ntext"
    record = {**_RECORD, "created_by": created_by, "size_bytes": len(text.encode())}
    with patch.object(blobs, "_locked_read_ready_blob", autospec=True, return_value=(record, text.encode())):
        result = blobs._execute_get_blob_content({"blob_id": _ID}, _empty_state(), context)
    contract = blobs._GET_BLOB_CONTENT_DECLARATION.response_contract
    assert contract is blobs._BLOB_CONTENT_RESPONSE
    wire = contract.admit(result.data).to_wire()
    assert wire == {**_CONTENT, "content": text[:50000], "truncated": large, "created_by": created_by, "size_bytes": len(text.encode())}
    assert json.dumps(wire) == json.dumps(deep_thaw(result.data))


@pytest.mark.parametrize("pending", [False, True])
def test_actual_metadata_producer_preserves_explicit_null(pending: bool) -> None:
    context = _trained_context(session_id="response", session_engine=create_session_engine("sqlite://"))
    record = {**_RECORD, "content_hash": None, "status": "pending"} if pending else _RECORD
    with patch.object(blobs, "_sync_get_blob", autospec=True, return_value=record):
        result = blobs._handle_get_blob_metadata({"blob_id": _ID}, _empty_state(), context)
    contract = blobs._GET_BLOB_METADATA_DECLARATION.response_contract
    assert contract is blobs._BLOB_METADATA_RESPONSE
    assert json.dumps(contract.admit(result.data).to_wire()) == json.dumps(deep_thaw(result.data))


def test_ready_null_hash_producer_retains_integrity_exception() -> None:
    from unittest.mock import MagicMock

    from sqlalchemy import Engine

    engine = MagicMock(spec=Engine)
    connection = engine.connect.return_value.__enter__.return_value
    connection.execute.return_value.fetchall.return_value = [object()]
    with (
        patch.object(blobs, "_blob_row_to_tool_dict", autospec=True, return_value={**_RECORD, "content_hash": None}),
        pytest.raises(AuditIntegrityError, match="null content_hash"),
    ):
        blobs._sync_list_ready_blob_inline_descriptors(engine, "response")
