"""``certain_union_collisions``: the build-time refusal of a union coalesce under
``union_collision_policy: fail`` whose every possible merge collides.

``fail`` is name-based, so a field every merge is certain to see from two
branches fails every row. The predicate may refuse ONLY what is certain — for
every arrival set the policy can merge — never a pipeline some row could merge.
"""

from __future__ import annotations

from typing import Literal

import pytest

from elspeth.core.dag.coalesce_merge import certain_union_collisions

_FORKED_PARENT = frozenset({"id", "price"})


def test_require_all_refuses_fields_guaranteed_by_two_branches() -> None:
    guarantees = {"path_a": _FORKED_PARENT | {"bonus"}, "path_b": _FORKED_PARENT | {"bonus"}}

    assert certain_union_collisions(guarantees, require_all=True, policy="require_all", quorum_count=None) == {
        "bonus": ("path_a", "path_b"),
        "id": ("path_a", "path_b"),
        "price": ("path_a", "path_b"),
    }


def test_require_all_with_disjoint_guarantees_is_not_certain() -> None:
    guarantees = {"path_a": frozenset({"a_score"}), "path_b": frozenset({"b_score"}), "path_c": frozenset()}

    assert certain_union_collisions(guarantees, require_all=True, policy="require_all", quorum_count=None) == {}


def test_branches_guaranteeing_nothing_are_never_refused() -> None:
    """An observed source forwards unknown fields: nothing is guaranteed, so a
    collision is only knowable per row and must stay a routed row fault."""
    guarantees = {"path_a": frozenset(), "path_b": frozenset()}

    assert certain_union_collisions(guarantees, require_all=True, policy="require_all", quorum_count=None) == {}


@pytest.mark.parametrize("policy", ["first", "best_effort"])
def test_policies_that_can_merge_a_single_branch_are_never_certain(policy: Literal["first", "best_effort"]) -> None:
    guarantees = {"path_a": _FORKED_PARENT, "path_b": _FORKED_PARENT}

    assert certain_union_collisions(guarantees, require_all=False, policy=policy, quorum_count=None) == {}


def test_quorum_refuses_when_every_quorum_subset_shares_a_guaranteed_field() -> None:
    guarantees = {"path_a": _FORKED_PARENT, "path_b": _FORKED_PARENT, "path_c": _FORKED_PARENT}

    assert certain_union_collisions(guarantees, require_all=False, policy="quorum", quorum_count=2) == {
        "id": ("path_a", "path_b", "path_c"),
        "price": ("path_a", "path_b", "path_c"),
    }


def test_quorum_with_one_collision_free_subset_is_not_certain() -> None:
    """path_c shares nothing with path_a, so the quorum {path_a, path_c} merges
    cleanly — a row CAN succeed, so the pipeline must build."""
    guarantees = {"path_a": frozenset({"x"}), "path_b": frozenset({"x"}), "path_c": frozenset({"y"})}

    assert certain_union_collisions(guarantees, require_all=False, policy="quorum", quorum_count=2) == {}


def test_quorum_without_a_count_is_a_config_validation_escape() -> None:
    with pytest.raises(RuntimeError, match="without a quorum_count"):
        certain_union_collisions(
            {"path_a": frozenset({"x"}), "path_b": frozenset({"x"})}, require_all=False, policy="quorum", quorum_count=None
        )
