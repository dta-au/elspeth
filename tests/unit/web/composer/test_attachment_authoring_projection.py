"""Exact public attachment projection rebinds current owned metadata.

Plural freshness controls re-resolve metadata without asserting byte integrity.
Reviewed scalar fixtures use the actual fenced read and review path. No public
call receives private reuse authority or publishes a Composer proposal here.
"""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import replace
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import insert, update

from elspeth.contracts.credential_material import DEFAULT_CREDENTIAL_TRAVERSAL_POLICY
from elspeth.contracts.errors import FrameworkBugError
from elspeth.contracts.freeze import deep_thaw
from elspeth.plugins.sources.blob_rows import BlobRowsSourceConfig
from elspeth.web.composer.redaction import SetPipelineArgumentsModel
from elspeth.web.composer.state import CompositionState
from elspeth.web.composer.tools._common import _serialize_set_pipeline_arguments
from elspeth.web.composer.tools.state_responses import PIPELINE_STATE_RESPONSE_CONTRACT, AuthoringStateResponse
from elspeth.web.interpretation_state import SOURCE_AUTHORING_KEY
from elspeth.web.sessions.models import blobs_table
from tests.unit.web.composer import test_reviewed_plural_authority as custody
from tests.unit.web.composer import test_set_pipeline_attachment_roots as public

authority_harness = custody.authority_harness


def _seed_scalar(harness: custody._AuthorityHarness, *, source_name: str, reviewed: bool = False) -> CompositionState:
    blob_id = public._csv_blob(harness)
    if reviewed:
        # The received helper records genuine owned provenance/FK rows. The
        # scalar binder then performs its actual fenced read and review stamp.
        custody._mutate_later_record(harness, blob_id, "llm_generated")
    bound = public._public_call(
        harness,
        "set_source_from_blob",
        {
            "source_name": source_name,
            "blob_id": blob_id,
            "plugin": "csv",
            "on_success": "documents",
            "on_validation_failure": "discard",
            "options": {"schema": {"mode": "observed"}, "delimiter": ",", "encoding": "utf-8"},
        },
        custody._draft(),
    )
    assert bound.success, bound.to_dict()
    if not reviewed:
        assert bound.validation.is_valid, bound.to_dict()
    state, _record = custody._persist(harness, bound.updated_state, is_valid=bound.validation.is_valid)
    assert state.sources[source_name].options["blob_ref"] == blob_id
    assert (SOURCE_AUTHORING_KEY in state.sources[source_name].options) is reviewed
    return state


def _seed_plural(harness: custody._AuthorityHarness, *, source_name: str = "scans") -> custody._PersistedPlural:
    if source_name == "scans":
        return custody._seed_persisted_plural(harness)
    ids = public._images(harness)
    bound = custody._bind(harness, custody._draft(), list(ids), source_name=source_name)
    assert bound.success and bound.validation.is_valid, bound.to_dict()
    state, record = custody._persist(harness, bound.updated_state)
    return custody._PersistedPlural(state, record, ids)


def _projection(harness: custody._AuthorityHarness, state: CompositionState) -> dict[str, Any]:
    result = public._public_call(harness, "get_pipeline_state", {"component": "set_pipeline_arguments"}, state)
    assert result.success, result.to_dict()
    assert result.updated_state is state
    wire = deep_thaw(result.data)
    assert type(wire) is dict
    return wire


def _assert_exact_projection(state: CompositionState, wire: dict[str, Any]) -> None:
    default = set(state.sources) == {"source"}
    assert ("source" in wire) is default and ("sources" in wire) is not default
    roots = {"source": wire["source"]} if default else wire["sources"]
    assert tuple(roots) == tuple(state.sources)
    for source_name, source in state.sources.items():
        root = roots[source_name]
        assert (root["plugin"], root["on_success"], root["on_validation_failure"], root["description"]) == (
            source.plugin,
            source.on_success,
            source.on_validation_failure,
            source.description,
        )
        if source.plugin == "blob_rows":
            assert root["blob_ids"] == [entry["blob_id"] for entry in source.options["blobs"]]
            assert "blob_id" not in root
        else:
            assert root["blob_id"] == source.options["blob_ref"]
            assert "blob_ids" not in root
        assert not {"path", "blob_ref", "mode", "blobs", SOURCE_AUTHORING_KEY}.intersection(root["options"])
        assert root["options"]["schema"] == deep_thaw(source.options["schema"])
        if source.plugin == "csv":
            assert root["options"]["delimiter"] == ","
            assert root["options"]["encoding"] == "utf-8"
    rendered = json.dumps(wire, sort_keys=True)
    for source in state.sources.values():
        if "path" in source.options:
            assert source.options["path"] not in rendered
        if "blobs" in source.options:
            for entry in source.options["blobs"]:
                assert entry["payload_ref"] not in rendered
                assert entry["filename"] not in rendered
    assert [output["sink_name"] for output in wire["outputs"]] == [output.name for output in state.outputs]
    assert all("name" not in output for output in wire["outputs"])


def _strict_round_trip(wire: dict[str, Any]) -> None:
    before = deepcopy(wire)
    admitted = PIPELINE_STATE_RESPONSE_CONTRACT.admit(wire)
    assert type(admitted.value) is AuthoringStateResponse
    assert admitted.to_wire() == wire
    assert admitted.readmit(PIPELINE_STATE_RESPONSE_CONTRACT).to_wire() == wire
    assert PIPELINE_STATE_RESPONSE_CONTRACT.admit(admitted.value).to_wire() == wire
    assert SetPipelineArgumentsModel.model_validate(wire, strict=True).model_dump(exclude_unset=True) == wire
    assert wire == before


@pytest.mark.parametrize(
    "kind",
    ["default_scalar", "named_scalar", "default_reviewed_scalar", "named_reviewed_scalar", "default_plural", "named_plural", "mixed"],
)
def test_scalar_and_plural_authoring_projection_round_trip(authority_harness: custody._AuthorityHarness, kind: str) -> None:
    harness = authority_harness
    if kind.endswith("scalar"):
        state = _seed_scalar(harness, source_name="source" if kind.startswith("default") else "table", reviewed="reviewed" in kind)
    elif kind == "mixed":
        scalar = _seed_scalar(harness, source_name="table")
        bound = custody._bind(harness, scalar, list(public._images(harness)), source_name="scans")
        assert bound.success and bound.validation.is_valid, bound.to_dict()
        state, _record = custody._persist(harness, bound.updated_state)
    else:
        state = _seed_plural(harness, source_name="source" if kind == "default_plural" else "scans").state
    before = state.to_dict()

    wire = _projection(harness, state)

    _assert_exact_projection(state, wire)
    _strict_round_trip(wire)
    arguments_before = deepcopy(wire)
    reapplied = public._public_call(harness, "set_pipeline", wire, state)
    assert reapplied.success, reapplied.to_dict()
    if "reviewed" not in kind:
        assert reapplied.validation.is_valid, reapplied.to_dict()
    assert reapplied.updated_state.sources == state.sources
    assert (
        reapplied.updated_state.nodes,
        reapplied.updated_state.edges,
        reapplied.updated_state.outputs,
        reapplied.updated_state.metadata,
    ) == (state.nodes, state.edges, state.outputs, state.metadata)
    assert reapplied.updated_state.version == state.version + 1
    assert wire == arguments_before and state.to_dict() == before


def _raw_authoring(kind: str) -> dict[str, Any]:
    default = kind.startswith("default")
    name = "source" if default else "scans"
    ids = [str(UUID(int=1, version=4)), str(UUID(int=2, version=4))]
    root = public._root("blob_rows", blob_ids=ids) if kind.endswith("plural") else public._root("csv", blob_id=ids[0])
    return public._arguments({name: root}, default=default)


@pytest.mark.parametrize("kind", ["default_scalar", "named_scalar", "default_plural", "named_plural"])
def test_attachment_authoring_raw_and_owned_readmit(kind: str) -> None:
    raw = _raw_authoring(kind)
    before = deepcopy(raw)

    admitted = PIPELINE_STATE_RESPONSE_CONTRACT.admit(raw)

    _strict_round_trip(raw)
    assert type(admitted.value) is AuthoringStateResponse
    owned = admitted.value.source if kind.startswith("default") else admitted.value.sources["scans"]
    assert owned is not None
    if kind.endswith("plural"):
        # Access to the new carrier is deliberately after real raw admission,
        # so current source reaches its missing-shape refusal at runtime.
        assert type(owned.blob_ids) is tuple
        assert owned.blob_ids == tuple(str(UUID(int=index, version=4)) for index in (1, 2))
        assert owned.blob_id is None
    else:
        assert owned.blob_id == str(UUID(int=1, version=4))
    encoded = admitted.to_wire()
    assert type(encoded) is dict
    root = encoded["source"] if kind.startswith("default") else encoded["sources"]["scans"]
    if kind.endswith("plural"):
        root["blob_ids"].reverse()
    else:
        root["blob_id"] = str(UUID(int=999, version=4))
    assert admitted.to_wire() == before
    assert raw == before


@pytest.mark.parametrize("defect", ["ambiguous", "empty_ids", "mutable_ids", "wrong_member", "missing_failure_route"])
def test_owned_attachment_selector_corruption_refuses_after_raw_admission(defect: str) -> None:
    admitted = PIPELINE_STATE_RESPONSE_CONTRACT.admit(_raw_authoring("named_plural"))
    assert type(admitted.value) is AuthoringStateResponse
    owned = admitted.value
    source = owned.sources["scans"]
    # All new constructor fields occur after current raw admission; imports
    # and collection do not depend on an unpublished dataclass signature.
    if defect == "ambiguous":
        broken = replace(source, blob_id=str(UUID(int=3, version=4)))
    elif defect == "empty_ids":
        broken = replace(source, blob_ids=())
    elif defect == "mutable_ids":
        broken = replace(source, blob_ids=list(source.blob_ids))
    elif defect == "wrong_member":
        broken = replace(source, blob_ids=(source.blob_ids[0], 7))
    else:
        # Inspection permits this legacy SourceSpec value; exact authoring
        # must still pass the final strict SetPipelineArgumentsModel gate.
        broken = replace(source, source=replace(source.source, on_validation_failure=None))
    corrupted = replace(owned, sources={"scans": broken})

    with pytest.raises(FrameworkBugError):
        PIPELINE_STATE_RESPONSE_CONTRACT.admit(corrupted)
    assert admitted.readmit(PIPELINE_STATE_RESPONSE_CONTRACT).to_wire() == _raw_authoring("named_plural")


@pytest.mark.parametrize("kind", ["default_plural", "named_plural"])
@pytest.mark.parametrize("defect", ["ambiguous", "empty_ids", "wrong_container", "wrong_member", "unknown_key", "over_width"])
def test_raw_attachment_selector_corruption_refuses(kind: str, defect: str) -> None:
    raw = _raw_authoring(kind)
    root = raw["source"] if kind.startswith("default") else raw["sources"]["scans"]
    if defect == "ambiguous":
        root["blob_id"] = str(UUID(int=3, version=4))
    elif defect == "empty_ids":
        root["blob_ids"] = []
    elif defect == "wrong_container":
        root["blob_ids"] = "not-a-list"
    elif defect == "wrong_member":
        root["blob_ids"][1] = 7
    elif defect == "unknown_key":
        root["unknown_selector"] = None
    else:
        root["blob_ids"] = [str(UUID(int=index + 1, version=4)) for index in range(DEFAULT_CREDENTIAL_TRAVERSAL_POLICY.max_width + 1)]
    before = deepcopy(raw)
    with pytest.raises(FrameworkBugError):
        PIPELINE_STATE_RESPONSE_CONTRACT.admit(raw)
    assert raw == before


def _restore_blob(harness: custody._AuthorityHarness, blob_id: str, original: dict[str, Any]) -> None:
    present = custody._blob_row(harness, blob_id) is not None
    with harness.engine.begin() as conn:
        if not present:
            assert conn.execute(insert(blobs_table).values(**original)).rowcount == 1
        else:
            assert conn.execute(update(blobs_table).where(blobs_table.c.id == blob_id).values(**original)).rowcount == 1
    assert custody._blob_row(harness, blob_id) == original


def _assert_healthy_reapply(harness: custody._AuthorityHarness, seed: custody._PersistedPlural, wire: dict[str, Any]) -> None:
    healthy = public._public_call(harness, "set_pipeline", wire, seed.state)
    assert healthy.success and healthy.validation.is_valid, healthy.to_dict()
    assert healthy.updated_state.sources == seed.state.sources
    assert healthy.updated_state.version == seed.state.version + 1
    custody._assert_checkpoint_unchanged(harness, seed)


@pytest.mark.parametrize("source_name", ["source", "scans"])
@pytest.mark.parametrize("perturbation", ["payload_ref", "filename", "mime_type", "size_bytes"])
def test_public_plural_projection_resolves_current_records(
    authority_harness: custody._AuthorityHarness, source_name: str, perturbation: str
) -> None:
    harness = authority_harness
    seed = _seed_plural(harness, source_name=source_name)
    wire = _projection(harness, seed.state)
    _assert_exact_projection(seed.state, wire)
    before = deepcopy(wire)
    later = seed.blob_ids[1]
    original = custody._blob_row(harness, later)
    assert original is not None
    historical = deep_thaw(seed.state.sources[source_name].options["blobs"])
    try:
        custody._mutate_later_record(harness, later, perturbation)

        result = public._public_call(harness, "set_pipeline", wire, seed.state)

        assert result.success and result.validation.is_valid, result.to_dict()
        current = deep_thaw(result.updated_state.sources[source_name].options["blobs"])
        assert current == public._expected_entries(harness, list(seed.blob_ids))
        assert current[0] == historical[0] and current[1] != historical[1]
        assert {key for key in current[1] if current[1][key] != historical[1][key]} == {perturbation}
        assert result.updated_state.version == seed.state.version + 1
        assert deep_thaw(seed.state.sources[source_name].options["blobs"]) == historical
        assert wire == before
        custody._assert_checkpoint_unchanged(harness, seed)
    finally:
        _restore_blob(harness, later, original)
    _assert_healthy_reapply(harness, seed, wire)


@pytest.mark.parametrize("source_name", ["source", "scans"])
@pytest.mark.parametrize(
    "perturbation",
    [
        "foreign_session_same_user",
        "foreign_session_other_user",
        "not_ready_pending",
        "not_ready_error",
        "llm_generated",
        "disambiguated",
        "llm_generated_then_amended",
        "missing",
    ],
)
def test_public_plural_projection_refuses_unavailable_current_records(
    authority_harness: custody._AuthorityHarness, source_name: str, perturbation: str
) -> None:
    harness = authority_harness
    seed = _seed_plural(harness, source_name=source_name)
    wire = _projection(harness, seed.state)
    _assert_exact_projection(seed.state, wire)
    before = deepcopy(wire)
    later = seed.blob_ids[1]
    original = custody._blob_row(harness, later)
    assert original is not None
    try:
        custody._mutate_later_record(harness, later, perturbation)

        refused = public._public_call(harness, "set_pipeline", wire, seed.state)

        review_required = perturbation in {"llm_generated", "disambiguated", "llm_generated_then_amended"}
        public._assert_candidate_refusal(
            refused, seed.state, "source_blob_review_required" if review_required else "source_blob_unavailable", source_name
        )
        assert wire == before
        custody._assert_checkpoint_unchanged(harness, seed)
    finally:
        _restore_blob(harness, later, original)
    _assert_healthy_reapply(harness, seed, wire)


@pytest.mark.parametrize("position", [0, 1])
@pytest.mark.parametrize("defect", ["missing_id", "malformed_hash", "wrong_size", "unknown_field", "not_object"])
def test_unreconstructible_stored_member_refuses_projection(
    authority_harness: custody._AuthorityHarness, position: int, defect: str
) -> None:
    harness = authority_harness
    seed = _seed_plural(harness)
    options = deep_thaw(seed.state.sources["scans"].options)
    if defect == "missing_id":
        del options["blobs"][position]["blob_id"]
    elif defect == "malformed_hash":
        options["blobs"][position]["payload_ref"] = "invalid-hash"
    elif defect == "wrong_size":
        options["blobs"][position]["size_bytes"] = True
    elif defect == "unknown_field":
        options["blobs"][position]["storage_path"] = "not-public-custody"
    else:
        options["blobs"][position] = None
    source = replace(seed.state.sources["scans"], options=options)
    corrupted = replace(seed.state, sources={"scans": source})
    before = corrupted.to_dict()

    wire, error = _serialize_set_pipeline_arguments(corrupted)

    assert wire is None and error is not None
    assert corrupted.to_dict() == before
    custody._assert_checkpoint_unchanged(harness, seed)
    _assert_exact_projection(seed.state, _projection(harness, seed.state))


@pytest.mark.parametrize(
    "defect",
    [
        "not_list",
        "empty_list",
        "duplicate_id",
        "duplicate_hash",
        "scalar_id",
        "scalar_mode",
        "scalar_path",
        "scalar_authoring",
        "wrong_plugin",
    ],
)
def test_unreconstructible_stored_custody_refuses_projection(authority_harness: custody._AuthorityHarness, defect: str) -> None:
    harness = authority_harness
    seed = _seed_plural(harness)
    options = deep_thaw(seed.state.sources["scans"].options)
    plugin = "blob_rows"
    if defect == "not_list":
        options["blobs"] = {}
    elif defect == "empty_list":
        options["blobs"] = []
    elif defect == "duplicate_id":
        options["blobs"][1]["blob_id"] = options["blobs"][0]["blob_id"]
    elif defect == "duplicate_hash":
        options["blobs"][1]["payload_ref"] = options["blobs"][0]["payload_ref"]
    elif defect == "scalar_id":
        options["blob_ref"] = seed.blob_ids[0]
    elif defect == "scalar_mode":
        options["mode"] = "bind_source"
    elif defect == "scalar_path":
        row = custody._blob_row(harness, seed.blob_ids[0])
        assert row is not None
        options["path"] = row["storage_path"]
    elif defect == "scalar_authoring":
        options[SOURCE_AUTHORING_KEY] = {}
    else:
        plugin = "csv"
    corrupted = replace(seed.state, sources={"scans": replace(seed.state.sources["scans"], plugin=plugin, options=options)})
    before = corrupted.to_dict()

    wire, error = _serialize_set_pipeline_arguments(corrupted)

    assert wire is None and error is not None
    assert corrupted.to_dict() == before
    custody._assert_checkpoint_unchanged(harness, seed)
    _assert_exact_projection(seed.state, _projection(harness, seed.state))


def test_stored_width_refuses_projection_without_changing_credential_priority(authority_harness: custody._AuthorityHarness) -> None:
    harness = authority_harness
    seed = _seed_plural(harness)
    width = DEFAULT_CREDENTIAL_TRAVERSAL_POLICY.max_width
    entries = [
        {
            "blob_id": str(UUID(int=index + 1, version=4)),
            "payload_ref": hashlib.sha256(f"metadata {index}".encode()).hexdigest(),
            "filename": f"page-{index}.png",
            "mime_type": "image/png",
            "size_bytes": 1,
        }
        for index in range(width + 1)
    ]
    # This is a valid storage-only list; synthetic identities establish no
    # claim about current records or bytes, and no resolver is called here.
    storage = BlobRowsSourceConfig.model_validate(
        {"schema": {"mode": "observed"}, "on_validation_failure": "discard", "blobs": entries}, strict=True
    )
    assert len(storage.blobs) == width + 1
    options = {**deep_thaw(seed.state.sources["scans"].options), "blobs": entries}
    over_width = replace(seed.state, sources={"scans": replace(seed.state.sources["scans"], options=options)})
    before = over_width.to_dict()

    wire, error = _serialize_set_pipeline_arguments(over_width)

    assert wire is None and error is not None
    result = public._public_call(harness, "get_pipeline_state", {"component": "set_pipeline_arguments"}, over_width)
    assert result.success is False, result.to_dict()
    assert result.updated_state is over_width
    assert result.validation.errors[0].error_code == "credential_material_rejected"
    assert result.data is None
    assert over_width.to_dict() == before
    custody._assert_checkpoint_unchanged(harness, seed)
