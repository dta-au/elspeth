"""Coalesce ``branches`` normalisation is discriminated by ABC, not exact type.

``_execute_upsert_node`` and ``_execute_upsert_queue_node`` reify the validated
``branches`` argument into ``NodeSpec.branches``
(``CoalesceBranches = tuple[str, ...] | Mapping[str, str]``) with::

    dict(validated.branches) if isinstance(validated.branches, Mapping) else tuple(validated.branches)

The ``isinstance`` arm is load-bearing and must NOT be narrowed to the house
exact-type idiom ``type(validated.branches) is dict``. The else-branch is
``tuple(...)``, which on a mapping yields a tuple of its KEYS — so under an
exact-type test any branches carrier that is a ``Mapping`` but not exactly
``dict`` would turn a NAMED coalesce branch map into a POSITIONAL branch
tuple. That is a composer state corruption the author never sees: the
mutation succeeds, the node persists, and the branch names are gone.

What this module does and does not pin, stated plainly, because the
distinction is the whole argument:

* It PINS the precondition the current code rests on — pydantic reconstructs
  the field, so what reaches the discriminator is always an exact ``dict`` or
  an exact ``list``. That is a property of the annotation
  ``branches: list[str] | dict[str, str] | None``, not of the call sites. A
  future annotation change that admits a read-only ``Mapping`` through
  un-reconstructed fails here rather than in a persisted pipeline.
* It does NOT turn red merely because someone swaps in
  ``type(validated.branches) is dict``. Measured: with the reconstruction
  guarantee holding, both forms are behaviourally identical today, so no
  end-to-end test can distinguish them. That is exactly why the guarantee is
  pinned directly and why ``test_the_two_discriminator_forms_diverge_on_a_read_only_mapping``
  demonstrates the divergence on the expression itself.

The third fact, which lives outside pytest: mypy narrows the negative arm of
``isinstance(b, Mapping)`` to ``list[str]`` but leaves the negative arm of
``type(b) is dict`` as the un-narrowed union, so the exact-type form also
forfeits the static proof that ``tuple(...)`` is handed a list.

Both call sites carry a tier-model R5 rationale for the same reason
(``docs/agents/sweeps/tier-burndown/B45.rationales.json``); this module is the
executable half of that argument.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Any

import pytest

from elspeth.web.composer.state import NodeSpec, queue_node_contract_error
from elspeth.web.composer.tools.transforms import _UpsertNodeArgumentsModel
from tests.unit.web.composer.test_tools import (
    _empty_state,
    _mock_catalog,
    execute_tool,
)

_BRANCH_MAP: dict[str, str] = {"a": "branch_a_done", "b": "branch_b_done"}


class _PlainDictSubclass(dict[str, str]):
    """A ``dict`` subclass with no overridden behaviour.

    Exactly ``dict`` is False for it, ``isinstance(_, Mapping)`` is True — the
    minimal witness that the two discriminator forms are not interchangeable.
    """


def _coalesce_arguments(branches: Any) -> dict[str, Any]:
    return {
        "id": "joined",
        "node_type": "coalesce",
        "plugin": None,
        "input": "branch_a_done",
        "branches": branches,
        "policy": "require_all",
        "merge": "union",
    }


class TestValidatedBranchesAreReconstructed:
    """The guarantee the discriminator rests on: pydantic rebuilds the value."""

    @pytest.mark.parametrize(
        "carrier",
        [
            pytest.param(dict(_BRANCH_MAP), id="plain-dict"),
            pytest.param(MappingProxyType(dict(_BRANCH_MAP)), id="mappingproxy"),
            pytest.param(_PlainDictSubclass(_BRANCH_MAP), id="dict-subclass"),
        ],
    )
    def test_every_mapping_carrier_validates_to_exactly_dict(self, carrier: Any) -> None:
        validated = _UpsertNodeArgumentsModel.model_validate(_coalesce_arguments(carrier))

        assert type(validated.branches) is dict
        assert validated.branches == _BRANCH_MAP

    @pytest.mark.parametrize(
        "carrier",
        [
            pytest.param(["branch_a_done", "branch_b_done"], id="list"),
            pytest.param(("branch_a_done", "branch_b_done"), id="tuple"),
        ],
    )
    def test_every_sequence_carrier_validates_to_exactly_list(self, carrier: Any) -> None:
        validated = _UpsertNodeArgumentsModel.model_validate(_coalesce_arguments(carrier))

        assert type(validated.branches) is list
        assert validated.branches == ["branch_a_done", "branch_b_done"]

    def test_empty_mapping_and_empty_sequence_keep_their_arms(self) -> None:
        """Smart-union resolution on empties still separates the two arms."""
        assert type(_UpsertNodeArgumentsModel.model_validate(_coalesce_arguments({})).branches) is dict
        assert type(_UpsertNodeArgumentsModel.model_validate(_coalesce_arguments([])).branches) is list


class TestDiscriminatorFormsAreNotInterchangeable:
    """Why the ``isinstance`` arm cannot be narrowed to the exact-type idiom."""

    @pytest.mark.parametrize(
        "carrier",
        [
            pytest.param(MappingProxyType(dict(_BRANCH_MAP)), id="mappingproxy"),
            pytest.param(_PlainDictSubclass(_BRANCH_MAP), id="dict-subclass"),
        ],
    )
    def test_the_two_discriminator_forms_diverge_on_a_read_only_mapping(self, carrier: Any) -> None:
        """On a Mapping that is not exactly ``dict`` the forms disagree, and the
        exact-type form loses the branch NAMES rather than raising."""
        named_branches: object = _BRANCH_MAP
        positional_branch_keys: object = ("a", "b")
        by_abc: object = dict(carrier) if isinstance(carrier, Mapping) else tuple(carrier)
        by_exact_type: object = dict(carrier) if type(carrier) is dict else tuple(carrier)

        assert by_abc == named_branches
        assert by_exact_type == positional_branch_keys
        assert by_abc != by_exact_type, (
            "if these ever agree the divergence has been designed out and this module's argument for keeping isinstance needs re-deriving"
        )


class TestBranchesSurviveTheUpsertAsNamedBranches:
    """End-to-end: a mapping carrier must never land as a tuple of keys."""

    @pytest.mark.parametrize(
        "carrier",
        [
            pytest.param(dict(_BRANCH_MAP), id="plain-dict"),
            pytest.param(MappingProxyType(dict(_BRANCH_MAP)), id="mappingproxy"),
            pytest.param(_PlainDictSubclass(_BRANCH_MAP), id="dict-subclass"),
        ],
    )
    def test_mapping_branches_persist_as_a_mapping_not_a_tuple_of_keys(self, carrier: Any) -> None:
        result = execute_tool("upsert_node", _coalesce_arguments(carrier), _empty_state(), _mock_catalog())

        assert result.success is True
        node = next(candidate for candidate in result.updated_state.nodes if candidate.id == "joined")
        assert isinstance(node.branches, Mapping), (
            "a Mapping branches carrier must reify as a NAMED branch map; a tuple here means the "
            "discriminator fell through to tuple(...) and silently dropped the branch names"
        )
        assert dict(node.branches) == _BRANCH_MAP

    def test_sequence_branches_persist_as_a_tuple(self) -> None:
        result = execute_tool(
            "upsert_node",
            _coalesce_arguments(["branch_a_done", "branch_b_done"]),
            _empty_state(),
            _mock_catalog(),
        )

        assert result.success is True
        node = next(candidate for candidate in result.updated_state.nodes if candidate.id == "joined")
        assert node.branches == ("branch_a_done", "branch_b_done")


class TestQueueNodeSeesTheNormalisedBranches:
    """``_execute_upsert_queue_node`` normalises BEFORE the intrinsic guard.

    The queue arm reifies ``branches`` into the ``NodeSpec`` it hands to
    ``queue_node_contract_error``, which reports ``branches`` as a forbidden
    field. So the same discriminator runs on the queue path too, and its
    output is what the rejection message is derived from — the site is
    reachable, not dead code behind the guard.
    """

    def test_queue_node_carrying_mapping_branches_is_rejected_by_the_contract(self) -> None:
        arguments = _coalesce_arguments(MappingProxyType(dict(_BRANCH_MAP)))
        arguments["node_type"] = "queue"
        arguments["plugin"] = "queue_plugin"

        result = execute_tool("upsert_node", arguments, _empty_state(), _mock_catalog())

        assert result.success is False
        assert result.updated_state.nodes == ()

    def test_contract_guard_reads_a_mapping_for_a_mapping_carrier(self) -> None:
        """The guard's input is the reified value, so the arm matters here too."""
        validated = _UpsertNodeArgumentsModel.model_validate(_coalesce_arguments(MappingProxyType(dict(_BRANCH_MAP))))
        assert validated.branches is not None
        branches = dict(validated.branches) if isinstance(validated.branches, Mapping) else tuple(validated.branches)
        node = NodeSpec(
            id="q",
            node_type="queue",
            plugin="queue_plugin",
            input="q",
            on_success=None,
            on_error=None,
            options={},
            condition=None,
            routes=None,
            fork_to=None,
            branches=branches,
            policy=None,
            merge=None,
            trigger=None,
        )

        assert isinstance(node.branches, Mapping)
        assert queue_node_contract_error(node) is not None
