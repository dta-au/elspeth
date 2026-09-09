"""ToolResult admits only closed payload shapes on its four formerly-loose fields (ADR-032).

``data``, ``plugin_schemas``, ``validation_guidance`` and ``applied_component`` were
``Any`` / ``Mapping[str, Any]``; the model receives them verbatim, so their shape
is part of the wire contract. The type now says what they are and the
constructor refuses, nominally (exact container types, no ``isinstance`` on
subclasses), anything else — the gate in ``test_tool_result_envelope_gate.py``
is the backstop, this is the close (elspeth-e405ad7cd2, D2).
"""

from __future__ import annotations

from types import MappingProxyType

import pytest
from pydantic import BaseModel

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.tools._common import ToolResult
from tests.unit.web.composer._helpers import _empty_state


class _Model(BaseModel):
    x: int = 1


class _DictLookalike(dict):
    """A dict subclass: structurally a mapping, nominally not the closed shape."""


def _result(*, success: bool = True, **fields: object) -> ToolResult:
    state = _empty_state()
    return ToolResult(success=success, updated_state=state, validation=state.validate(), affected_nodes=(), **fields)


@pytest.mark.parametrize(
    "data",
    [{"a": 1}, MappingProxyType({"a": 1}), [{"a": 1}], ({"a": 1},), _Model(), None, ["x", "y"]],
    ids=["dict", "proxy", "list", "tuple", "model", "none", "list-of-str"],
)
def test_admits_every_closed_data_shape(data: object) -> None:
    _result(data=data)


@pytest.mark.parametrize(
    "data",
    [_DictLookalike(a=1), "text", 1, 1.0, {1, 2}, object(), frozenset()],
    ids=["dict-subclass", "str", "int", "float", "set", "object", "frozenset"],
)
def test_refuses_an_open_data_shape(data: object) -> None:
    with pytest.raises(AuditIntegrityError, match=r"ToolResult\.data"):
        _result(data=data)


def test_refuses_validation_guidance_without_a_codes_mapping() -> None:
    with pytest.raises(AuditIntegrityError, match="validation_guidance"):
        _result(success=False, validation_guidance={"explain_tool": "x"})
    with pytest.raises(AuditIntegrityError, match="validation_guidance"):
        _result(success=False, validation_guidance={"codes": ["not", "a", "mapping"]})


def test_admits_validation_guidance_with_codes() -> None:
    result = _result(success=False, validation_guidance={"codes": {"c": {"explanation": "e", "suggested_fix": "f"}}})
    assert result.validation_guidance is not None
    assert "c" in result.validation_guidance["codes"]


def test_refuses_applied_component_with_a_key_outside_the_registry() -> None:
    with pytest.raises(AuditIntegrityError, match="applied_component"):
        _result(applied_component={"gates": []})


def test_refuses_applied_component_that_is_not_a_mapping() -> None:
    with pytest.raises(AuditIntegrityError, match="applied_component"):
        _result(applied_component=[("nodes", [])])


def test_admits_applied_component_with_registry_keys_only() -> None:
    result = _result(applied_component={"nodes": [], "edges": []})
    assert result.applied_component is not None
    assert set(result.applied_component) == {"nodes", "edges"}


def test_refuses_plugin_schemas_whose_values_are_not_mappings() -> None:
    with pytest.raises(AuditIntegrityError, match="plugin_schemas"):
        _result(success=False, plugin_schemas={"transform/x": "not a schema"})


def test_admission_runs_before_freezing_so_a_refusal_never_leaves_a_half_frozen_result() -> None:
    """The refusal is raised from ``__post_init__`` before ``freeze_fields``; a caller that catches it holds nothing."""
    with pytest.raises(AuditIntegrityError):
        _result(data=1)
