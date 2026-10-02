"""/validate reports resolved pipeline-decision SEMANTICS drift as a blocker (finding #18).

``_validate_pipeline_decision_review`` re-runs the decision's semantic check
on a RESOLVED row before comparing the artifact hash. When the node no longer
implements the approved decision (here a raw-HTML cleanup whose
``select_only`` was switched off after approval) that check refused with a
bare ``ValueError``. The hash-drift arm beside it already raised the typed
``InterpretationReviewIntegrityError``, so this shape still escaped
``validate_pipeline`` as an exception (a 500 on /validate, a 404 on /execute)
instead of the ``interpretation_review_drift`` blocker.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from elspeth.web.composer.state import CompositionState
from elspeth.web.execution.protocol import YamlGenerator
from elspeth.web.execution.schemas import CHECK_INTERPRETATION_REVIEW
from elspeth.web.interpretation_state import pipeline_decision_artifact_hash
from tests.unit.web.execution.test_validation import (
    _make_node,
    _make_settings,
    _make_state,
    validate_pipeline_for_trained_operator,
)
from tests.unit.web.test_interpretation_state import _pipeline_decision_options

_CLEANUP_USER_TERM = "drop_raw_html_fields"


def _cleanup_state(*, select_only: bool) -> CompositionState:
    reviewed = _make_node(plugin="field_mapper", options=_pipeline_decision_options(status="resolved"))
    accepted_hash = pipeline_decision_artifact_hash(reviewed, (reviewed,), user_term=_CLEANUP_USER_TERM)
    options = _pipeline_decision_options(status="resolved", artifact_hash=accepted_hash)
    options["select_only"] = select_only
    return _make_state(nodes=(_make_node(plugin="field_mapper", options=options),))


def test_approved_cleanup_decision_passes_the_interpretation_review_check() -> None:
    """Control: the same fixture without the drift clears the interpretation check."""
    yaml_generator = MagicMock(spec=YamlGenerator)
    yaml_generator.generate_yaml.return_value = "sources: {}\nsinks: {}\n"

    result = validate_pipeline_for_trained_operator(_cleanup_state(select_only=True), _make_settings(), yaml_generator)

    check = next(check for check in result.checks if check.name == CHECK_INTERPRETATION_REVIEW)
    assert check.passed is True
    assert all(error.error_code != "interpretation_review_drift" for error in result.errors)


def test_resolved_pipeline_decision_semantics_drift_is_a_readiness_blocker_not_an_exception() -> None:
    mock_yaml_gen = MagicMock(spec=YamlGenerator)

    result = validate_pipeline_for_trained_operator(_cleanup_state(select_only=False), _make_settings(), mock_yaml_gen)

    assert result.is_valid is False
    expected_detail = "The approved pipeline_decision review for transform 'test_node' no longer matches the current pipeline."
    failed = next(check for check in result.checks if check.name == CHECK_INTERPRETATION_REVIEW)
    assert failed.passed is False
    assert failed.detail == expected_detail
    assert failed.affected_nodes == ("test_node",)
    assert [(error.error_code, error.component_id, error.component_type, error.message) for error in result.errors] == [
        ("interpretation_review_drift", "test_node", "transform", expected_detail)
    ]
    assert result.readiness.execution_ready is False
    assert [(b.code, b.component_id, b.component_type, b.detail) for b in result.readiness.blockers] == [
        ("interpretation_review_drift", "test_node", "transform", expected_detail)
    ]
    # Fixed copy only: the raw semantic-check message never reaches the result.
    assert "select_only" not in result.model_dump_json()
    mock_yaml_gen.generate_yaml.assert_not_called()
