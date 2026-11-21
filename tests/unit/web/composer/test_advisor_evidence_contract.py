"""An advisor finding may be answered with evidence when the graph is correct."""

from elspeth.web.composer.advisor_policy import ADVISOR_MUTATION_EXPECTATION_CLAUSE, ADVISOR_OUTPUT_CONTRACT_CLAUSE


def test_advisor_finding_can_be_answered_with_evidence() -> None:
    assert "or explain evidence that no edit is needed" in ADVISOR_OUTPUT_CONTRACT_CLAUSE
    assert "do not change correct content or make no-op edits" in ADVISOR_MUTATION_EXPECTATION_CLAUSE
    # The only channel to the next review is the reply the user reads, so the evidence must stand on its own.
    assert "state that evidence in a reply that stands on its own for the user" in ADVISOR_MUTATION_EXPECTATION_CLAUSE
    assert "for the next review" not in ADVISOR_MUTATION_EXPECTATION_CLAUSE
    assert "advisor block still prevents completion" in ADVISOR_MUTATION_EXPECTATION_CLAUSE
