"""Integrity contracts for the shared canonical pipeline proposal envelope."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterator, Mapping
from dataclasses import fields, is_dataclass, replace
from decimal import Decimal
from types import MappingProxyType
from typing import Any, cast
from uuid import UUID

import numpy as np
import pytest

import elspeth.web.composer.pipeline_proposal as pipeline_proposal_module
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.freeze import FrozenJsonArray, deep_thaw, freeze_fields
from elspeth.core.canonical import canonical_json, stable_hash
from elspeth.web.composer.pipeline_proposal import (
    AbsentBase,
    PipelineProposal,
    PresentBase,
    composition_content_hash,
    pipeline_draft_hash,
)

_STATE_ID = UUID("00000000-0000-4000-8000-000000000001")


class _StrSubclass(str):
    pass


class _IntSubclass(int):
    pass


class _FloatSubclass(float):
    pass


class _ChangingMapping(Mapping[str, object]):
    """Return one safe snapshot, then expose a non-JSON replacement."""

    def __init__(self) -> None:
        self._items_calls = 0

    def __getitem__(self, key: str) -> object:
        if key != "value":
            raise KeyError(key)
        return "safe"

    def __iter__(self) -> Iterator[str]:
        return iter(("value",))

    def __len__(self) -> int:
        return 1

    def items(self) -> Any:
        self._items_calls += 1
        value: object = "safe" if self._items_calls == 1 else np.array([1, 2])
        return {"value": value}.items()


_INVALID_JSON_TREES: tuple[tuple[str, dict[object, object]], ...] = (
    ("decimal", {"value": Decimal("1.25")}),
    ("ndarray", {"value": np.array([1, 2])}),
    ("set", {"value": {1, 2}}),
    ("tuple", {"value": (1, 2)}),
    ("opaque", {"value": object()}),
    ("str-subclass", {"value": _StrSubclass("text")}),
    ("int-subclass", {"value": _IntSubclass(1)}),
    ("float-subclass", {"value": _FloatSubclass(1.25)}),
    ("str-key-subclass", {_StrSubclass("value"): "text"}),
    ("non-str-key", {1: "text"}),
    ("nan", {"value": float("nan")}),
    ("positive-infinity", {"value": float("inf")}),
    ("negative-infinity", {"value": float("-inf")}),
    ("outside-jcs-integer-domain", {"value": 2**53}),
)

_CANONICAL_JSON_COLLISIONS: tuple[
    tuple[str, dict[str, Any], dict[object, object]],
    ...,
] = (
    ("decimal-string-collision", {"value": "1.25"}, {"value": Decimal("1.25")}),
    ("ndarray-list-collision", {"value": [1, 2]}, {"value": np.array([1, 2])}),
    ("tuple-list-collision", {"value": [1, 2]}, {"value": (1, 2)}),
    ("str-subclass-collision", {"value": "text"}, {"value": _StrSubclass("text")}),
    ("int-subclass-collision", {"value": 1}, {"value": _IntSubclass(1)}),
    ("float-subclass-collision", {"value": 1.25}, {"value": _FloatSubclass(1.25)}),
    ("str-key-subclass-collision", {"value": "text"}, {_StrSubclass("value"): "text"}),
)


def _pipeline() -> dict[str, Any]:
    return {
        "source": {
            "plugin": "csv",
            "on_success": "rows",
            "options": {
                "credentials": {"secret_ref": "CSV_API_TOKEN"},
                "columns": ["name", "score"],
            },
        },
        "nodes": [
            {
                "id": "clean",
                "plugin": "normalize",
                "options": {"rules": [{"column": "name", "operation": "strip"}]},
            }
        ],
        "edges": [{"from": "rows", "to": "clean"}],
        "outputs": [
            {
                "sink_name": "clean",
                "plugin": "json",
                "options": {"destination": {"secret_ref": "OUTPUT_PATH"}},
            }
        ],
    }


def _create_proposal(
    *,
    pipeline: Mapping[str, Any] | None = None,
    base: AbsentBase | PresentBase | None = None,
    repair_count: int = 1,
    skill_hash: str = "c" * 64,
    supplied_draft_hash: str | None = None,
) -> PipelineProposal:
    return PipelineProposal.create(
        pipeline=_pipeline() if pipeline is None else pipeline,
        base=PresentBase(state_id=_STATE_ID, composition_content_hash="b" * 64) if base is None else base,
        repair_count=repair_count,
        skill_hash=skill_hash,
        supplied_draft_hash=supplied_draft_hash,
    )


def test_pipeline_proposal_is_recursively_immutable_and_detached() -> None:
    pipeline = _pipeline()
    proposal = _create_proposal(pipeline=pipeline)

    assert isinstance(proposal.pipeline, MappingProxyType)
    assert isinstance(proposal.pipeline["source"], MappingProxyType)
    assert isinstance(proposal.pipeline["source"]["options"]["columns"], tuple)
    assert isinstance(proposal.pipeline["nodes"][0]["options"]["rules"][0], MappingProxyType)

    with pytest.raises(TypeError):
        proposal.pipeline["nodes"] = ()  # type: ignore[index]
    with pytest.raises(TypeError):
        proposal.pipeline["source"]["options"]["new"] = "value"  # type: ignore[index]
    with pytest.raises(AttributeError):
        proposal.pipeline["nodes"].append({})

    pipeline["source"]["options"]["columns"].append("late_mutation")
    pipeline["nodes"][0]["options"]["rules"][0]["column"] = "changed"
    assert proposal.pipeline["source"]["options"]["columns"] == ("name", "score")
    assert proposal.pipeline["nodes"][0]["options"]["rules"][0]["column"] == "name"


def _nested_json_mapping(depth: int) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for _ in range(depth):
        value = {"child": value}
    return value


@pytest.mark.parametrize(
    "pipeline",
    [
        pytest.param(_nested_json_mapping(65), id="depth"),
        pytest.param({"values": [None] * 10_000}, id="nodes"),
        pytest.param({"values": ["x" * 61_700] * 17}, id="bytes"),
    ],
)
def test_create_rejects_over_budget_json_with_audit_integrity_error(pipeline: dict[str, Any]) -> None:
    with pytest.raises(AuditIntegrityError, match="pipeline"):
        _create_proposal(pipeline=pipeline)


@pytest.mark.parametrize(
    "pipeline",
    [
        pytest.param(_nested_json_mapping(63), id="depth"),
        pytest.param({"values": [None] * 9_984}, id="nodes"),
        pytest.param({"values": ["x" * 65_400] * 16}, id="bytes"),
    ],
)
def test_create_and_restore_accept_json_just_below_budgets(pipeline: dict[str, Any]) -> None:
    proposal = _create_proposal(pipeline=pipeline)

    assert PipelineProposal.from_dict(proposal.to_dict()) == proposal


@pytest.mark.parametrize(
    "pipeline",
    [
        pytest.param(_nested_json_mapping(65), id="depth"),
        pytest.param({"values": [None] * 10_000}, id="nodes"),
        pytest.param({"values": ["x" * 61_700] * 17}, id="bytes"),
    ],
)
def test_restore_rejects_over_budget_json_with_audit_integrity_error(pipeline: dict[str, Any]) -> None:
    payload = _create_proposal().to_dict()
    payload["pipeline"] = pipeline

    with pytest.raises(AuditIntegrityError, match="pipeline proposal payload"):
        PipelineProposal.from_dict(payload)


def test_pipeline_proposal_rejects_plain_tuple_inside_mapping_proxy() -> None:
    with pytest.raises(AuditIntegrityError, match="got tuple"):
        _create_proposal(pipeline=MappingProxyType({"value": (1, 2)}))


def test_secret_ref_markers_are_preserved_in_canonical_pipeline() -> None:
    pipeline = _pipeline()
    proposal = _create_proposal(pipeline=pipeline)

    assert deep_thaw(proposal.pipeline) == pipeline
    assert canonical_json(proposal.pipeline) == canonical_json(pipeline)
    assert proposal.pipeline["source"]["options"]["credentials"] == {"secret_ref": "CSV_API_TOKEN"}
    assert proposal.pipeline["outputs"][0]["options"]["destination"] == {"secret_ref": "OUTPUT_PATH"}


def test_draft_hash_has_explicit_v4_order_bound_envelope_golden_preimage() -> None:
    """The hash binds only the surviving proposal authority fields."""
    proposal = _create_proposal()
    expected_preimage = {
        "schema": "composer.pipeline-proposal-envelope.v4",
        "pipeline": _pipeline(),
        "base": {
            "kind": "present",
            "state_id": str(_STATE_ID),
            "composition_content_hash": "b" * 64,
        },
        "repair_count": 1,
        "skill_hash": "c" * 64,
    }
    expected_canonical_json = (
        '{"base":{"composition_content_hash":"' + "b" * 64 + '","kind":"present","state_id":"00000000-0000-4000-8000-000000000001"},'
        '"pipeline":{"edges":[{"from":"rows","to":"clean"}],'
        '"nodes":[{"id":"clean","options":{"rules":[{"column":"name","operation":"strip"}]},'
        '"plugin":"normalize"}],"outputs":[{"options":{"destination":{"secret_ref":"OUTPUT_PATH"}},'
        '"plugin":"json","sink_name":"clean"}],"source":{"on_success":"rows","options":{"columns":'
        '["name","score"],"credentials":{"secret_ref":"CSV_API_TOKEN"}},"plugin":"csv"}},"repair_count":1,'
        '"schema":"composer.pipeline-proposal-envelope.v4","skill_hash":"' + "c" * 64 + '"}'
    )

    assert canonical_json(expected_preimage) == expected_canonical_json
    assert proposal.draft_hash == stable_hash(expected_preimage)
    assert proposal.draft_hash == pipeline_draft_hash(
        pipeline=proposal.pipeline,
        base=proposal.base,
        repair_count=proposal.repair_count,
        skill_hash=proposal.skill_hash,
    )


@pytest.mark.parametrize(
    "change",
    [
        "pipeline",
        "base_kind",
        "base_state_id",
        "base_content_hash",
        "repair_count",
        "skill_hash",
    ],
)
def test_draft_hash_covers_every_authority_bearing_envelope_field(change: str) -> None:
    baseline = _create_proposal()
    kwargs: dict[str, Any] = {}
    if change == "pipeline":
        changed_pipeline = _pipeline()
        changed_pipeline["nodes"][0]["plugin"] = "filter"
        kwargs["pipeline"] = changed_pipeline
    elif change == "base_kind":
        kwargs["base"] = AbsentBase()
    elif change == "base_state_id":
        kwargs["base"] = PresentBase(
            state_id=UUID("00000000-0000-4000-8000-000000000002"),
            composition_content_hash="b" * 64,
        )
    elif change == "base_content_hash":
        kwargs["base"] = PresentBase(state_id=_STATE_ID, composition_content_hash="e" * 64)
    elif change == "repair_count":
        kwargs["repair_count"] = 2
    elif change == "skill_hash":
        kwargs["skill_hash"] = "e" * 64

    assert _create_proposal(**kwargs).draft_hash != baseline.draft_hash


def test_absent_and_present_bases_have_unambiguous_serialized_tags() -> None:
    absent = _create_proposal(base=AbsentBase()).to_dict()["base"]
    present = _create_proposal().to_dict()["base"]

    assert absent == {"kind": "absent"}
    assert present == {
        "kind": "present",
        "state_id": str(_STATE_ID),
        "composition_content_hash": "b" * 64,
    }


@pytest.mark.parametrize(
    "base_factory",
    [
        lambda: PresentBase(
            state_id="00000000-0000-4000-8000-000000000001",  # type: ignore[arg-type]
            composition_content_hash="b" * 64,
        ),
        lambda: PresentBase(state_id=_STATE_ID, composition_content_hash=""),
        lambda: PresentBase(state_id=_STATE_ID, composition_content_hash="B" * 64),
        lambda: PresentBase(state_id=_STATE_ID, composition_content_hash="not-a-hash"),
        lambda: AbsentBase(kind="present"),  # type: ignore[arg-type]
    ],
)
def test_base_rejects_invalid_uuid_tag_or_hash(base_factory: Callable[[], AbsentBase | PresentBase]) -> None:
    with pytest.raises(AuditIntegrityError):
        _create_proposal(base=base_factory())


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("repair_count", -1),
        ("repair_count", True),
        ("skill_hash", ""),
        ("skill_hash", "C" * 64),
        ("skill_hash", "c" * 63),
    ],
)
def test_proposal_rejects_negative_repair_or_malformed_nonempty_hashes(field: str, value: object) -> None:
    with pytest.raises(AuditIntegrityError):
        _create_proposal(**{field: value})


def test_construction_rejects_supplied_draft_mismatch() -> None:
    with pytest.raises(AuditIntegrityError, match="draft_hash mismatch"):
        _create_proposal(supplied_draft_hash="0" * 64)


def test_direct_construction_rejects_draft_hash_mismatch() -> None:
    with pytest.raises(AuditIntegrityError, match="draft_hash mismatch"):
        replace(_create_proposal(), draft_hash="0" * 64)


def test_strict_restore_round_trip_and_hash_reverification() -> None:
    proposal = _create_proposal()
    restored = PipelineProposal.from_dict(proposal.to_dict())
    assert restored == proposal

    bad_draft = proposal.to_dict()
    bad_draft["draft_hash"] = "0" * 64
    with pytest.raises(AuditIntegrityError, match="draft_hash mismatch"):
        PipelineProposal.from_dict(bad_draft)


@pytest.mark.parametrize("corrupt_value", [None, "", 42])
def test_restore_rejects_null_empty_or_wrong_type_required_hash_before_resealing(
    corrupt_value: object,
) -> None:
    payload = dict(_create_proposal().to_dict())
    payload["draft_hash"] = corrupt_value

    with pytest.raises(AuditIntegrityError, match="PipelineProposal draft_hash must be exactly 64 lowercase hexadecimal characters"):
        PipelineProposal.from_dict(payload)


def test_restore_rejects_missing_required_hash() -> None:
    payload = dict(_create_proposal().to_dict())
    del payload["draft_hash"]

    with pytest.raises(AuditIntegrityError, match="persisted fields are malformed"):
        PipelineProposal.from_dict(payload)


@pytest.mark.parametrize("mutation", ["extra_top_level", "missing_top_level", "unknown_base", "extra_base"])
def test_restore_rejects_noncanonical_envelope_shapes(mutation: str) -> None:
    payload = _create_proposal().to_dict()
    if mutation == "extra_top_level":
        payload["rationale"] = "model-authored text must not enter the envelope"
    elif mutation == "missing_top_level":
        del payload["skill_hash"]
    elif mutation == "unknown_base":
        payload["base"] = {"kind": "wildcard"}
    elif mutation == "extra_base":
        payload["base"]["version"] = 3

    with pytest.raises(AuditIntegrityError):
        PipelineProposal.from_dict(payload)


def test_envelope_defines_no_duplicate_topology_dataclasses_or_rationale() -> None:
    import elspeth.web.composer.pipeline_proposal as module

    dataclass_names = {
        name
        for name, value in vars(module).items()
        if isinstance(value, type) and value.__module__ == module.__name__ and is_dataclass(value)
    }
    assert dataclass_names == {"AbsentBase", "PresentBase", "PipelineProposal"}
    assert [field.name for field in fields(PipelineProposal)] == [
        "pipeline",
        "draft_hash",
        "base",
        "repair_count",
        "skill_hash",
    ]
    assert "rationale" not in PipelineProposal.__slots__
    assert "why" not in PipelineProposal.__slots__


def test_composition_content_hash_exactly_matches_the_ordered_sources_preimage() -> None:
    """The preimage is the content fields with ``sources`` as an ordered pair array.

    ``version`` and ``composer_meta`` stay excluded; the ``sources`` map is the
    Composer authority projection (``composer.ordered-sources.v1``).
    """

    class _State:
        # Real CompositionState instances always carry the memo slot; the
        # stand-in models that contract so the preimage pin exercises the
        # same uncached-compute path.
        _content_hash_memo: str | None = None

        def to_dict(self) -> dict[str, Any]:
            return {
                "version": 17,
                "sources": {"main": {"plugin": "csv"}},
                "nodes": [{"id": "clean"}],
                "edges": [{"from": "main", "to": "clean"}],
                "outputs": [{"sink_name": "clean"}],
                "metadata": {"name": "example"},
                "composer_meta": {"private_view_state": {"tab": "graph"}},
            }

    state = _State()
    state_d = state.to_dict()
    expected_hash = stable_hash(
        {
            "sources": {"schema": "composer.ordered-sources.v1", "items": [["main", {"plugin": "csv"}]]},
            "nodes": state_d["nodes"],
            "edges": state_d["edges"],
            "outputs": state_d["outputs"],
            "metadata": state_d["metadata"],
        }
    )
    assert composition_content_hash(state) == expected_hash


@pytest.mark.parametrize("pipeline", [[], {"nodes": float("nan")}])
def test_proposal_rejects_non_mapping_or_noncanonical_pipeline(pipeline: object) -> None:
    with pytest.raises(AuditIntegrityError):
        _create_proposal(pipeline=pipeline)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "invalid_tree",
    [tree for _, tree in _INVALID_JSON_TREES],
    ids=[case_id for case_id, _ in _INVALID_JSON_TREES],
)
def test_create_rejects_values_outside_strict_json_tree(invalid_tree: dict[object, object]) -> None:
    with pytest.raises(AuditIntegrityError, match="pipeline"):
        _create_proposal(pipeline=invalid_tree)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "invalid_tree",
    [tree for _, tree in _INVALID_JSON_TREES],
    ids=[case_id for case_id, _ in _INVALID_JSON_TREES],
)
def test_pipeline_draft_hash_rejects_values_outside_strict_json_tree(invalid_tree: dict[object, object]) -> None:
    with pytest.raises(AuditIntegrityError, match="pipeline"):
        pipeline_draft_hash(
            pipeline=invalid_tree,  # type: ignore[arg-type]
            base=AbsentBase(),
            repair_count=0,
            skill_hash="b" * 64,
        )


@pytest.mark.parametrize(
    ("valid_pipeline", "invalid_pipeline"),
    [(valid, invalid) for _, valid, invalid in _CANONICAL_JSON_COLLISIONS],
    ids=[case_id for case_id, _, _ in _CANONICAL_JSON_COLLISIONS],
)
def test_restore_rejects_non_json_values_even_when_they_collide_with_stored_hash(
    valid_pipeline: dict[str, Any],
    invalid_pipeline: dict[object, object],
) -> None:
    proposal = _create_proposal(pipeline=valid_pipeline)
    payload = proposal.to_dict()
    payload["pipeline"] = invalid_pipeline  # type: ignore[typeddict-item]

    with pytest.raises(AuditIntegrityError, match="pipeline"):
        PipelineProposal.from_dict(payload)


@pytest.mark.parametrize(
    ("valid_pipeline", "invalid_pipeline"),
    [(valid, invalid) for _, valid, invalid in _CANONICAL_JSON_COLLISIONS],
    ids=[case_id for case_id, _, _ in _CANONICAL_JSON_COLLISIONS],
)
def test_direct_construction_rejects_non_json_values_even_when_they_collide_with_draft_hash(
    valid_pipeline: dict[str, Any],
    invalid_pipeline: dict[object, object],
) -> None:
    proposal = _create_proposal(pipeline=valid_pipeline)

    with pytest.raises(AuditIntegrityError, match="pipeline"):
        PipelineProposal(
            pipeline=invalid_pipeline,  # type: ignore[arg-type]
            draft_hash=proposal.draft_hash,
            base=proposal.base,
            repair_count=proposal.repair_count,
            skill_hash=proposal.skill_hash,
        )


def test_create_hashes_and_seals_the_same_validated_mapping_snapshot() -> None:
    changing_pipeline = _ChangingMapping()

    proposal = _create_proposal(pipeline=changing_pipeline)
    baseline = _create_proposal(pipeline={"value": "safe"})

    assert proposal.draft_hash == baseline.draft_hash
    assert deep_thaw(proposal.pipeline) == {"value": "safe"}


class TestCompositionContentHashMemo:
    """``composition_content_hash`` memoizes on the frozen instance (elspeth-ac85b0ab0e review).

    Preflight identity keys rebuild the hash several times per composer turn;
    without the memo each rebuild re-serializes the whole state via
    ``to_dict``. The memo must never survive onto a modified copy — every
    mutation constructor re-runs ``__init__``, which resets the slot.
    """

    @staticmethod
    def _state():
        from elspeth.web.composer.state import (
            CompositionState,
            OutputSpec,
            PipelineMetadata,
            SourceSpec,
        )

        return CompositionState(
            source=SourceSpec(
                plugin="csv",
                on_success="rows",
                options={"path": "input.csv"},
                on_validation_failure="discard",
            ),
            nodes=(),
            edges=(),
            outputs=(
                OutputSpec(
                    name="main",
                    plugin="csv",
                    options={"path": "out.csv"},
                    on_write_failure="discard",
                ),
            ),
            metadata=PipelineMetadata(),
            version=1,
        )

    def test_repeat_calls_serialize_once(self, monkeypatch) -> None:
        from elspeth.web.composer.pipeline_proposal import composition_content_hash
        from elspeth.web.composer.state import CompositionState

        state = self._state()
        calls = 0
        real_to_dict = CompositionState.to_dict

        def counting_to_dict(instance):
            nonlocal calls
            calls += 1
            return real_to_dict(instance)

        monkeypatch.setattr(CompositionState, "to_dict", counting_to_dict)
        first = composition_content_hash(state)
        second = composition_content_hash(state)
        assert first == second
        assert calls == 1

    def test_mutated_copy_recomputes_and_differs(self) -> None:
        from elspeth.web.composer.pipeline_proposal import composition_content_hash

        state = self._state()
        baseline = composition_content_hash(state)
        renamed = state.with_metadata({"name": "renamed"})
        assert composition_content_hash(renamed) != baseline

    def test_version_only_copy_recomputes_to_the_same_content_hash(self) -> None:
        """Version is excluded from content identity; the memo must not leak
        a stale slot through ``replace`` either way."""
        from dataclasses import replace

        from elspeth.web.composer.pipeline_proposal import composition_content_hash

        state = self._state()
        baseline = composition_content_hash(state)
        bumped = replace(state, version=state.version + 1)
        assert bumped._content_hash_memo is None
        assert composition_content_hash(bumped) == baseline


# --- Fix 6: create verifies once and seals what it verified -----------------

# Values recorded from the pre-change implementation (``git show
# af393ba6:src/elspeth/web/composer/pipeline_proposal.py`` loaded under a
# temporary module name) for ``_pipeline()``. They pin that freezing and
# hashing once changed no output byte.
_HEAD_PRESENT_DRAFT_HASH = "88494d51a6166aa181d149445b8ac754d98852bb8878d16f52ddb22661c9bb5c"
_HEAD_ABSENT_DRAFT_HASH = "3dfb9df4d731928bdcb4d5be2c457b9077e94a10f498bcd68c435a6049b7c14f"
_HEAD_PIPELINE_CANONICAL_SHA256 = "9dd80072f3ed2699ace3bbed1e0b06db406c7bf5e33cd7c5ca9771a53c6ae9f8"


@pytest.mark.parametrize(
    ("base", "repair_count", "expected_draft_hash"),
    [
        pytest.param(PresentBase(state_id=_STATE_ID, composition_content_hash="b" * 64), 1, _HEAD_PRESENT_DRAFT_HASH, id="present"),
        pytest.param(AbsentBase(), 0, _HEAD_ABSENT_DRAFT_HASH, id="absent"),
    ],
)
def test_create_output_bytes_match_the_pre_carry_once_implementation(
    base: AbsentBase | PresentBase,
    repair_count: int,
    expected_draft_hash: str,
) -> None:
    proposal = _create_proposal(base=base, repair_count=repair_count)

    assert proposal.draft_hash == expected_draft_hash
    assert hashlib.sha256(canonical_json(proposal.pipeline).encode("utf-8")).hexdigest() == _HEAD_PIPELINE_CANONICAL_SHA256
    assert canonical_json(proposal.pipeline) == canonical_json(_pipeline())


def _count_calls(monkeypatch: pytest.MonkeyPatch, target: Callable[..., Any], name: str, counts: dict[str, int]) -> None:
    def counted(*args: Any, **kwargs: Any) -> Any:
        counts[name] += 1
        return target(*args, **kwargs)

    counts[name] = 0
    monkeypatch.setattr(pipeline_proposal_module, name, counted)


def _instrument(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    counts: dict[str, int] = {}
    _count_calls(
        monkeypatch, pipeline_proposal_module._validate_and_freeze_canonical_mapping, "_validate_and_freeze_canonical_mapping", counts
    )
    _count_calls(monkeypatch, pipeline_proposal_module._owned_pipeline_draft_hash, "_owned_pipeline_draft_hash", counts)
    _count_calls(monkeypatch, pipeline_proposal_module.pipeline_draft_hash, "pipeline_draft_hash", counts)
    _count_calls(monkeypatch, stable_hash, "stable_hash", counts)
    _count_calls(monkeypatch, freeze_fields, "freeze_fields", counts)
    return counts


def test_create_freezes_once_and_hashes_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """One ``create`` = one validate/freeze traversal and one draft hash.

    Negative control (measured on af393ba6 with the same wrappers): one
    ``create`` performed 4 ``_validate_and_freeze_canonical_mapping``
    traversals, 2 ``pipeline_draft_hash`` and 2 ``stable_hash`` calls, plus a
    ``freeze_fields`` rebuild; this assertion is red against that code.
    """
    counts = _instrument(monkeypatch)

    _create_proposal()

    assert counts == {
        "_validate_and_freeze_canonical_mapping": 1,
        "_owned_pipeline_draft_hash": 1,
        "pipeline_draft_hash": 0,
        "stable_hash": 1,
        "freeze_fields": 0,
    }


def test_direct_construction_still_runs_full_verification(monkeypatch: pytest.MonkeyPatch) -> None:
    """Positive control for the instrument, and the hand-built path's floor.

    A hand-built ``PipelineProposal(...)`` must still freeze, hash and compare
    in ``__post_init__``; if the wrappers did not intercept the module's
    calls, this would read zero and the create assertion above would be
    meaningless.
    """
    proposal = _create_proposal()
    counts = _instrument(monkeypatch)

    PipelineProposal(
        pipeline=_pipeline(),
        draft_hash=proposal.draft_hash,
        base=proposal.base,
        repair_count=proposal.repair_count,
        skill_hash=proposal.skill_hash,
    )

    assert counts == {
        "_validate_and_freeze_canonical_mapping": 1,
        "_owned_pipeline_draft_hash": 1,
        "pipeline_draft_hash": 0,
        "stable_hash": 1,
        "freeze_fields": 1,
    }


def test_create_seals_exactly_what_full_verification_accepts() -> None:
    proposal = _create_proposal()

    reverified = PipelineProposal(
        pipeline=proposal.pipeline,
        draft_hash=proposal.draft_hash,
        base=proposal.base,
        repair_count=proposal.repair_count,
        skill_hash=proposal.skill_hash,
    )

    assert reverified == proposal
    assert PipelineProposal.from_dict(proposal.to_dict()) == proposal


def test_hand_built_proposal_with_another_pipelines_valid_hash_is_refused() -> None:
    other = _create_proposal(pipeline={"value": "other"})

    with pytest.raises(AuditIntegrityError, match="draft_hash mismatch"):
        PipelineProposal(
            pipeline=_pipeline(),
            draft_hash=other.draft_hash,
            base=other.base,
            repair_count=other.repair_count,
            skill_hash=other.skill_hash,
        )


def test_from_dict_with_another_pipelines_valid_hash_is_refused() -> None:
    payload = _create_proposal().to_dict()
    payload["draft_hash"] = _create_proposal(pipeline={"value": "other"}).draft_hash

    with pytest.raises(AuditIntegrityError, match="draft_hash mismatch"):
        PipelineProposal.from_dict(payload)


def test_hand_built_proxy_is_not_trusted_as_already_frozen() -> None:
    """A ``MappingProxyType`` input is re-verified, not trusted by type."""
    backing = _pipeline()
    proposal = _create_proposal(pipeline=backing)
    backing["nodes"] = []

    with pytest.raises(AuditIntegrityError, match="draft_hash mismatch"):
        PipelineProposal(
            pipeline=MappingProxyType(backing),
            draft_hash=proposal.draft_hash,
            base=proposal.base,
            repair_count=proposal.repair_count,
            skill_hash=proposal.skill_hash,
        )


def _container_ids(value: object) -> set[int]:
    if type(value) in {dict, MappingProxyType}:
        mapping = cast(Mapping[str, object], value)
        return {id(value)}.union(*(_container_ids(child) for child in mapping.values()))
    if type(value) in {list, FrozenJsonArray}:
        sequence = cast(list[object], value)
        return {id(value)}.union(*(_container_ids(child) for child in sequence))
    return set()


@pytest.mark.parametrize("wrap", [pytest.param(dict, id="dict"), pytest.param(MappingProxyType, id="mapping-proxy")])
def test_create_owns_its_tree_after_the_caller_mutates_the_input(wrap: Callable[[dict[str, Any]], Mapping[str, Any]]) -> None:
    backing = _pipeline()
    proposal = _create_proposal(pipeline=wrap(backing))
    expected_canonical = canonical_json(_pipeline())

    assert not _container_ids(proposal.pipeline) & _container_ids(backing)

    backing["source"]["options"]["columns"].append("late_mutation")
    backing["nodes"][0]["options"]["rules"][0]["column"] = "changed"
    backing["edges"] = []
    backing["late"] = "key"

    assert canonical_json(proposal.pipeline) == expected_canonical
    assert proposal.draft_hash == _HEAD_PRESENT_DRAFT_HASH
    assert PipelineProposal.from_dict(proposal.to_dict()) == proposal
