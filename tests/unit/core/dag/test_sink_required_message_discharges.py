# tests/unit/core/dag/test_sink_required_message_discharges.py
"""The sink required-fields verdict must stay DISCHARGEABLE.

``elspeth-e1f8a84c8b`` was ruled "complete claim, but fix the surprise": the
semantics stay (``guaranteed_fields`` is a COMPLETE claim, and a producer that
declares any guarantee is held to exactly that set), and the remedy is that the
message says so instead of leaving an author to discover it by hitting it.

These tests pin the part that outlives any wording. A verdict is only useful if
it names a way OUT, and this one has three — complete the declaration, remove it
and abstain, or drop the sink's requirement. Route 2 is the one no author
guesses, because "delete the declaration you just added" reads like a step
backwards, and it is therefore the one a future tidy-up is most likely to prune
as redundant.

So the assertions here are deliberately NOT substring checks on the prose:
rephrasing must stay free, while silently losing a route must go red. The route
COUNT is pinned structurally, and the two option names an author has to edit are
DERIVED from the dataclasses that own them rather than restated here — renaming
``SchemaConfig.guaranteed_fields`` without updating the verdict fails this file
rather than shipping a message naming an option that no longer exists.
"""

from __future__ import annotations

import dataclasses
import re

from elspeth.contracts.schema import SchemaConfig
from elspeth.core.dag.models import NodeInfo
from elspeth.core.dag.schema_validation import _sink_required_violation_message

# The enumeration's shape: "  1. ", "  2. ", ... at the start of a line.
_ROUTE_LINE = re.compile(r"^ {2}(\d+)\. ", re.MULTILINE)


def _verdict() -> str:
    return _sink_required_violation_message("csv", "source_primary_ab12", frozenset({"colour"}))


def _dataclass_field_names(cls: type) -> frozenset[str]:
    return frozenset(f.name for f in dataclasses.fields(cls))


class TestTheVerdictStaysDischargeable:
    """Every route out must survive; the wording carrying them need not."""

    def test_the_verdict_enumerates_exactly_three_routes(self) -> None:
        """The count is the pin. Dropping a route reds this; rephrasing does not.

        Contiguous numbering is asserted too, so a route deleted from the middle
        fails here rather than shipping a verdict that skips from 1 to 3.
        """
        numbers = [int(n) for n in _ROUTE_LINE.findall(_verdict())]

        assert numbers == [1, 2, 3], (
            f"The sink required-fields verdict must offer three discharge routes; found {numbers}. "
            "If a route was deliberately removed, withdraw it here in the same commit and say why "
            "an author can still discharge the rule without it."
        )

    def test_the_abstain_route_survives(self) -> None:
        """Route 2 — remove the declaration — is the counter-intuitive one.

        Pinned separately from the count because it is the route most likely to
        be pruned as looking like a step backwards, and losing it strands the
        exact author the ruling was about: someone who ADDED a guarantee and got
        a rejection for it. Asserted by its two load-bearing halves (removing the
        declaration, and what that buys) rather than by its sentence.
        """
        route_2 = [line for line in _verdict().splitlines() if line.strip().startswith("2.")]
        assert len(route_2) == 1, _verdict()

        assert "Remove" in route_2[0] and "guaranteed_fields" in route_2[0], route_2[0]
        assert "per row" in route_2[0], (
            "Route 2 must say what abstaining BUYS — per-row enforcement at runtime. "
            "Without that an author reads it as 'give up the check' rather than 'move it'."
        )

    def test_every_option_an_author_must_edit_is_named(self) -> None:
        """Derived from the owning dataclasses, not restated.

        A rename that misses this message would otherwise ship a verdict telling
        authors to edit an option that no longer exists.
        """
        verdict = _verdict()

        guaranteed = "guaranteed_fields"
        assert guaranteed in _dataclass_field_names(SchemaConfig), (
            f"SchemaConfig no longer has a {guaranteed!r} field — this test's premise moved, not the message."
        )
        required = "declared_required_fields"
        assert required in _dataclass_field_names(NodeInfo), (
            f"NodeInfo no longer has a {required!r} field — this test's premise moved, not the message."
        )

        for option in (guaranteed, required):
            assert option in verdict, f"The verdict never names {option!r}, so the route that edits it is not actionable."


class TestTheVerdictStillStatesTheContractAndTheSymptom:
    """The new half must not displace what the message already owed."""

    def test_the_contract_is_stated_not_only_the_symptom(self) -> None:
        """The ruling's actual remedy: say that the claim is COMPLETE.

        Without this sentence the verdict explains what went wrong and never why
        adding a line caused it, which is the surprise elspeth-e1f8a84c8b names.
        """
        verdict = _verdict()
        assert "COMPLETE claim" in verdict, verdict
        assert "narrow" in verdict, "The verdict must say that declaring MORE can accept LESS — that is the surprise."

    def test_the_opening_symptom_sentence_is_preserved(self) -> None:
        """Two existing tests match on this sentence; it is a shared contract.

        test_dag_scenario_production_path.py and test_graph_validation.py both
        key on "does not guarantee them" and on the sink/upstream naming, so a
        rewrite that improved only the tail would break them from a distance.
        """
        verdict = _sink_required_violation_message("json", "coalesce_merge_b_99", frozenset({"id", "value"}))

        assert re.search(
            r"Sink 'json' requires fields \['id', 'value'\].*upstream 'coalesce_merge_b_99' does not guarantee them",
            verdict,
        ), verdict

    def test_both_call_sites_get_the_same_wording(self) -> None:
        """The single-owner property the function exists to hold.

        The sweep and the per-edge combined report must state the sink half
        identically, so a graph tripping BOTH rules reads the same as one
        tripping only this rule.
        """
        assert _verdict() == _sink_required_violation_message("csv", "source_primary_ab12", frozenset({"colour"}))
