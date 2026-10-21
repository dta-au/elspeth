"""The graph checksum reports the existing authored composition identity."""

from dataclasses import replace
from uuid import uuid4

import pytest
from pydantic import ValidationError

from elspeth.web.composer.pipeline_proposal import composition_content_hash
from elspeth.web.sessions.converters import state_from_record
from elspeth.web.sessions.routes._helpers import _state_response
from elspeth.web.sessions.schemas import CompositionStateResponse
from tests.unit.web.composer.test_state_response_contracts import state_cases
from tests.unit.web.sessions.test_route_error_records import _record


def test_state_response_exposes_authoritative_content_checksum() -> None:
    record = _record(None)
    payload = _state_response(record).model_dump(mode="json")

    assert record.metadata_ == state_from_record(record).to_dict()["metadata"]
    assert payload["composition_content_hash"] == composition_content_hash(state_from_record(record))


def test_null_metadata_remains_readable_without_a_checksum_but_not_runnable() -> None:
    record = replace(_record(None), metadata_=None)
    payload = _state_response(record).model_dump()
    assert payload["metadata"] is None
    assert payload["composition_content_hash"] is None
    assert CompositionStateResponse.model_validate(payload).composition_content_hash is None
    with pytest.raises(ValueError, match="None metadata_"):
        state_from_record(record)


@pytest.mark.parametrize("metadata", [{}, {"name": "Missing description"}])
def test_response_retains_corrupt_metadata_refusal(metadata: dict[str, str]) -> None:
    with pytest.raises((ValueError, KeyError)):
        _state_response(replace(_record(None), metadata_=metadata))


@pytest.mark.parametrize("value", ["", "a" * 63, "A" * 64, "g" * 64, "a" * 64 + "\n", 123])
def test_response_schema_refuses_malformed_checksum(value: object) -> None:
    payload = _state_response(_record(None)).model_dump()
    payload["composition_content_hash"] = value
    with pytest.raises(ValidationError):
        CompositionStateResponse.model_validate(payload)


def test_revision_and_validation_do_not_change_content_checksum() -> None:
    record = _record(None)
    revised = replace(record, id=uuid4(), session_id=uuid4(), version=record.version + 1, is_valid=True)

    assert (
        _state_response(record).model_dump()["composition_content_hash"]
        == _state_response(revised).model_dump()["composition_content_hash"]
    )


@pytest.mark.parametrize("metadata", [{"name": "Changed name"}, {"description": "Changed description"}])
def test_authored_metadata_changes_content_checksum(metadata: dict[str, str]) -> None:
    record = _record(None)
    assert record.metadata_ is not None
    changed = replace(record, metadata_={**record.metadata_, **metadata})

    assert (
        _state_response(record).model_dump()["composition_content_hash"]
        != _state_response(changed).model_dump()["composition_content_hash"]
    )


def test_checksum_binds_authored_paths_before_response_redaction() -> None:
    options = {"path": "internal-a.csv", "blob_ref": str(uuid4()), "schema": {"mode": "observed"}}
    source = {
        "plugin": "csv",
        "on_success": "output",
        "on_validation_failure": "discard",
        "options": options,
    }
    record = replace(_record(None), sources={"input": source})
    changed = replace(record, sources={"input": {**source, "options": {**options, "path": "internal-b.csv"}}})
    first, second = _state_response(record), _state_response(changed)

    assert first.sources == second.sources
    assert first.composition_content_hash == composition_content_hash(state_from_record(record))
    assert first.composition_content_hash != second.composition_content_hash


def test_option_key_order_is_canonical_but_source_order_remains_bound() -> None:
    source = {
        "plugin": "csv",
        "on_success": "output",
        "on_validation_failure": "discard",
        "options": {"path": "input.csv", "schema": {"mode": "observed"}},
    }
    reordered = {**source, "options": {"schema": {"mode": "observed"}, "path": "input.csv"}}
    record = replace(_record(None), sources={"first": source, "second": source})
    canonical = replace(record, sources={"first": reordered, "second": reordered})
    reversed_sources = replace(record, sources={"second": source, "first": source})

    assert _state_response(record).composition_content_hash == _state_response(canonical).composition_content_hash
    assert _state_response(record).composition_content_hash != _state_response(reversed_sources).composition_content_hash


@pytest.mark.parametrize("component", ["node", "edge", "output"])
def test_authored_component_edits_change_checksum(component: str) -> None:
    state, _ = state_cases()["full"]
    baseline = composition_content_hash(state)
    if component == "node":
        changed = replace(state, nodes=(replace(state.nodes[0], description="Changed step"), *state.nodes[1:]))
    elif component == "edge":
        changed = replace(state, edges=(replace(state.edges[0], label="Changed connection"),))
    else:
        changed = replace(state, outputs=(replace(state.outputs[0], options={"path": "changed.json"}),))
    assert composition_content_hash(changed) != baseline
