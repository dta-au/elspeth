"""Frozen-input contract for the PREVALIDATION_REJECTED feedback seed.

Regression cover for a defect that shipped and was merged: the guard in
``_prevalidation_feedback_seed`` was converted from ``isinstance(..., Mapping)``
to ``type(...) is dict``. ``ToolResult`` is a frozen dataclass whose
``__post_init__`` runs ``freeze_fields(self, "data")``, so a mapping payload is
a ``mappingproxy`` by the time anything reads ``.data`` — and for a
``mappingproxy`` BOTH ``type(x) is dict`` and ``isinstance(x, dict)`` are
False. The exact-type form therefore made the mapping arm unreachable and sent
every rejection to the fallback, silently changing the payload the composer
model repairs from.

Every case here builds its input through the REAL producer (an actual
``ToolResult``), never a hand-built dict — a hand-built dict is not frozen and
gives a false all-clear.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest

from elspeth.web.composer.tool_batch import _prevalidation_feedback_seed
from elspeth.web.composer.tools._common import ToolResult
from tests.unit.web.composer._helpers import _empty_state


def _tool_result(data: Any) -> ToolResult:
    """Build a real ToolResult so __post_init__ freezes ``data`` for us."""
    state = _empty_state()
    return ToolResult(
        success=False,
        updated_state=state,
        validation=state.validate(),
        affected_nodes=(),
        data=data,
    )


def test_tool_result_freezes_mapping_data_so_exact_dict_tests_are_dead() -> None:
    """Pin the trap itself: the frozen payload is not a dict by either test."""
    result = _tool_result({"error_code": "plugin_not_installed"})

    assert isinstance(result.data, Mapping)
    assert not isinstance(result.data, dict)
    assert type(result.data) is not dict


def test_frozen_mapping_data_is_spread_into_the_feedback_payload() -> None:
    """Mapping arm: the candidate's own keys land at the top level."""
    result = _tool_result(
        {
            "error": "source plugin selection is unavailable",
            "error_code": "plugin_not_installed",
        }
    )

    feedback = dict(_prevalidation_feedback_seed(result.data))

    assert feedback == {
        "error": "source plugin selection is unavailable",
        "error_code": "plugin_not_installed",
    }
    assert "candidate_data" not in feedback


def test_the_callers_copy_is_mutable_and_leaves_the_frozen_source_alone() -> None:
    """run_tool_batch does dict(seed) then .update({...}); pin both halves."""
    result = _tool_result({"error_code": "plugin_not_installed"})

    feedback = dict(_prevalidation_feedback_seed(result.data))

    assert type(feedback) is dict
    feedback.update({"status": "PREVALIDATION_REJECTED"})
    assert feedback["status"] == "PREVALIDATION_REJECTED"
    # The frozen source is not mutated by the caller's copy.
    assert "status" not in result.data


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(["alpha", "beta"], id="sequence"),
        pytest.param(({"a": 1},), id="tuple-of-dicts"),
    ],
)
def test_non_mapping_data_falls_back_to_candidate_data(payload: Any) -> None:
    """Fallback arm: an admitted non-mapping shape is carried structurally, not raised.

    Scalars no longer reach here: ``ToolResult`` refuses them at construction
    (elspeth-e405ad7cd2 D2), see ``test_scalar_data_is_refused_at_construction``.
    """
    result = _tool_result(payload)

    feedback = dict(_prevalidation_feedback_seed(result.data))

    assert set(feedback) == {"candidate_data"}
    # Compared against the FROZEN value: deep_freeze rewrites a list to a
    # tuple subclass, so asserting against the pre-freeze literal would fail.
    assert feedback["candidate_data"] == result.data


@pytest.mark.parametrize("payload", [pytest.param("candidate rejected", id="string"), pytest.param(7, id="int")])
def test_scalar_data_is_refused_at_construction(payload: Any) -> None:
    """A scalar ``data`` is not a closed payload shape; the seed's fallback arm never sees one."""
    from elspeth.contracts.errors import AuditIntegrityError

    with pytest.raises(AuditIntegrityError, match=r"ToolResult\.data"):
        _tool_result(payload)


def test_absent_data_yields_an_empty_seed() -> None:
    """None arm: nothing to spread, and no candidate_data key invented."""
    result = _tool_result(None)

    assert result.data is None
    assert dict(_prevalidation_feedback_seed(result.data)) == {}
