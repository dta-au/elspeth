"""Cleanup review guidance describes structured identity and actual graph effects."""

from elspeth.web.composer.tools.generation import explain_validation_code
from elspeth.web.interpretation_state import RAW_HTML_CLEANUP_USER_TERM


def test_live_cleanup_guidance_leaves_draft_wording_to_the_model() -> None:
    guidance = explain_validation_code("interpretation_review_contract_unsatisfied")

    assert guidance is not None
    explanation, fix = guidance
    assert RAW_HTML_CLEANUP_USER_TERM in fix
    assert "Write the draft in your own words" in fix
    assert "Example review row:" in fix
    assert "paraphrased draft is NOT recognized" not in explanation
    assert "do not rephrase the draft" not in fix


def test_stored_malformed_cleanup_diagnostic_is_identified_as_historical() -> None:
    guidance = explain_validation_code("interpretation_review_draft_malformed")

    assert guidance is not None
    explanation, fix = guidance
    assert "Historical" in explanation
    assert "retired" in explanation
    assert "declared" in fix
    assert "copied verbatim without rephrasing" not in fix
