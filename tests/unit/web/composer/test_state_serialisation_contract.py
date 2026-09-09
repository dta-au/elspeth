"""Serialisation contracts the owned-state authority guard depends on.

Two pins live here, both guarding the same seam from opposite directions.

``test_spec_from_dict_reads_exactly_its_dataclass_fields`` is what makes
``dataclasses.fields`` a sound proxy for "the keys a restore observes" in
``_reject_undeclared_owned_composition_state_fields``. If a ``from_dict`` ever
reads a key its dataclass does not declare, or stops reading one it does, that
helper starts accepting a silently-dropped field or rejecting an authored one.

``test_persisted_shape_content_hashes_are_pinned`` guards the far larger
blast radius the authority guard only samples. ``composition_content_hash``
hashes ``state.to_dict()`` AFTER ``from_dict`` has normalised, and those hashes
are stored — ``PresentBase.composition_content_hash`` binds a proposal to its
base state, and ``sessions/service.py`` re-derives and compares it on the fork
path. A new construction-time normalisation therefore silently invalidates
every stored binding for states whose authored shape it touches, with no
migration possible because the bytes are hash-bound. These pins make that
consequence visible in the same commit that introduces the normalisation
(elspeth-da00e1c1cb).
"""

from __future__ import annotations

import ast
import inspect
from dataclasses import fields as dataclass_fields
from pathlib import Path
from typing import Any

import pytest

from elspeth.web.composer.pipeline_proposal import composition_content_hash
from elspeth.web.composer.state import (
    CompositionState,
    EdgeSpec,
    NodeSpec,
    OutputSpec,
    PipelineMetadata,
    SourceSpec,
)

_OWNED_STATE_SPECS = (PipelineMetadata, SourceSpec, NodeSpec, EdgeSpec, OutputSpec)


_PAYLOAD_PARAMETER = "d"


def _reads_the_payload(node: ast.expr) -> bool:
    """Return whether ``node`` is the ``from_dict`` payload parameter itself."""
    return type(node) is ast.Name and node.id == _PAYLOAD_PARAMETER


def _from_dict_read_keys(class_def: ast.ClassDef) -> set[str]:
    """Return every payload key ``class_def.from_dict`` reads.

    Both idioms the specs use are collected: ``d["key"]`` subscripts and the
    ``"key" in d`` membership tests that guard optional fields.

    Each is anchored to the payload parameter. Collecting bare string
    subscripts would count a nested lookup such as ``d["options"]["path"]``, or
    any local-dict read, as a payload key — which would then have to be a
    declared field for this test to pass, silently loosening the guarantee it
    exists to carry.
    """
    from_dict = next(node for node in class_def.body if type(node) is ast.FunctionDef and node.name == "from_dict")
    keys: set[str] = set()
    for node in ast.walk(from_dict):
        if (
            type(node) is ast.Subscript
            and _reads_the_payload(node.value)
            and type(node.slice) is ast.Constant
            and type(node.slice.value) is str
        ):
            keys.add(node.slice.value)
        elif (
            type(node) is ast.Compare
            and [type(op) for op in node.ops] == [ast.In]
            and _reads_the_payload(node.comparators[0])
            and type(node.left) is ast.Constant
            and type(node.left.value) is str
        ):
            keys.add(node.left.value)
    return keys


@pytest.mark.parametrize("spec", _OWNED_STATE_SPECS, ids=lambda spec: spec.__name__)
def test_spec_from_dict_reads_exactly_its_dataclass_fields(spec: type) -> None:
    state_source = inspect.getsourcefile(NodeSpec)
    assert state_source is not None
    module = ast.parse(Path(state_source).read_text(encoding="utf-8"))
    class_def = next(node for node in module.body if type(node) is ast.ClassDef and node.name == spec.__name__)

    declared = {field.name for field in dataclass_fields(spec)}

    assert _from_dict_read_keys(class_def) == declared, (
        f"{spec.__name__}.from_dict no longer reads exactly its declared fields. "
        "_reject_undeclared_owned_composition_state_fields uses dataclasses.fields() as the set of "
        "keys a restore observes; a divergence makes it drop an authored field silently or reject a "
        "legitimate one."
    )


def _state_payload(nodes: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "version": 1,
        "sources": {
            "source": {
                "plugin": "csv",
                "on_success": "rows",
                "options": {"path": "cases.csv"},
                "on_validation_failure": "discard",
            }
        },
        "nodes": nodes,
        "edges": [],
        "outputs": [{"name": "out", "plugin": "json", "options": {}, "on_write_failure": "discard"}],
        "metadata": {"name": "Pinned", "description": "Serialisation contract"},
    }


def _structural_node(**overrides: Any) -> dict[str, Any]:
    node: dict[str, Any] = {
        "id": "join_rows",
        "node_type": "coalesce",
        "plugin": None,
        "input": "rows",
        "on_success": "out",
        "on_error": None,
        "options": {},
        "branches": ["left", "right"],
    }
    node.update(overrides)
    return node


def _collector_node(**overrides: Any) -> dict[str, Any]:
    node: dict[str, Any] = {
        "id": "page_stitcher",
        "node_type": "collector",
        "plugin": "batch_stats",
        "input": "pages",
        "on_success": "out",
        "on_error": None,
        "options": {},
        "scope_name": "document_pages",
        "scope_opener": "explode",
        "scope_policy": "require_all",
    }
    node.update(overrides)
    return node


@pytest.mark.parametrize(
    ("shape", "expected_hash"),
    [
        pytest.param(
            [],
            "ee343b52681824a7072806b58282abb520291b99185c62c6e65973d93d651123",
            id="no_structural_nodes",
        ),
        pytest.param(
            [_structural_node()],
            "13b7552baa393c7b14d01e2590df1c209383875fa7bda07e54872e3caf972849",
            id="coalesce_without_merge_or_policy",
        ),
        pytest.param(
            [_structural_node(id="union_rows", node_type="row_union")],
            "ab0074986f55de9f78f79946b460e32859b4fe76362e4cbd310b5838e70e7fa2",
            id="row_union_with_list_branches",
        ),
        pytest.param(
            [_collector_node()],
            "6cf8e9ffd32c44bb7ca2702865a1c89d5ae680469a06f47ed3f21d01821e2949",
            id="collector_with_scope_binding",
        ),
    ],
)
def test_persisted_shape_content_hashes_are_pinned(shape: list[dict[str, Any]], expected_hash: str) -> None:
    state = CompositionState.from_dict(_state_payload(shape))

    assert composition_content_hash(state) == expected_hash, (
        "A construction-time normalisation changed how an authored shape hashes. Stored "
        "PresentBase.composition_content_hash bindings are derived from this value and cannot be "
        "migrated (the proposal bytes are hash-bound), so every base binding for this shape becomes "
        "permanently unverifiable. Re-pin only after deciding what happens to already-persisted states."
    )


def test_collector_scope_fields_round_trip_and_omit_when_none() -> None:
    """Collector ``scope_*`` fields serialise omitted-when-None (2026-08-20 discipline).

    A pre-collector persisted node dict must stay byte-identical (no ``scope_*``
    keys appear on nodes that never carried them), and a collector's authored
    scope binding must survive ``from_dict(to_dict(state)) == state``.
    """
    payload = _state_payload([_collector_node(), _structural_node(id="join_rows", branches=["left", "right"])])
    state = CompositionState.from_dict(payload)

    serialized = state.to_dict()
    collector_dict = next(n for n in serialized["nodes"] if n["id"] == "page_stitcher")
    coalesce_dict = next(n for n in serialized["nodes"] if n["id"] == "join_rows")

    assert collector_dict["scope_name"] == "document_pages"
    assert collector_dict["scope_opener"] == "explode"
    assert collector_dict["scope_policy"] == "require_all"
    # The former scope_on_group_failure field is deleted (ADR-042): a
    # serialised collector must not resurrect it.
    assert "scope_on_group_failure" not in collector_dict
    # scope_policy is REQUIRED with no default: absent stays absent (None), so
    # a collector authored without it must serialise WITHOUT the key.
    sparse = CompositionState.from_dict(_state_payload([_collector_node(scope_policy=None)]))
    sparse_dict = next(n for n in sparse.to_dict()["nodes"] if n["id"] == "page_stitcher")
    assert "scope_policy" not in sparse_dict

    for key in ("scope_name", "scope_opener", "scope_policy"):
        assert key not in coalesce_dict

    assert CompositionState.from_dict(serialized) == state
