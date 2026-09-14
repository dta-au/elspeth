"""/validate turns resolved-review drift into a readiness blocker (finding #18).

``review_interpretations`` calls the strict materializer, whose drift guards
raise ``InterpretationReviewIntegrityError``. Before this change the error
escaped ``validate_pipeline`` uncaught (a bare 500 on /validate) while
/execute returned a 404; now validation reports a structured
``interpretation_review_drift`` failure on the interpretation-review check.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from elspeth.contracts.hashing import stable_hash
from elspeth.web.composer.state import CompositionState
from elspeth.web.execution.protocol import YamlGenerator
from elspeth.web.execution.schemas import CHECK_INTERPRETATION_REVIEW
from elspeth.web.interpretation_state import INTERPRETATION_REQUIREMENTS_KEY
from tests.unit.web.execution.test_validation import (
    _make_node,
    _make_settings,
    _make_state,
    validate_pipeline_for_trained_operator,
)

_PROMPT = "Rate {{ row.text }}"


def _drifted_prompt_review_state() -> CompositionState:
    return _make_state(
        nodes=(
            _make_node(
                plugin="llm",
                options={
                    "prompt_template": _PROMPT,
                    INTERPRETATION_REQUIREMENTS_KEY: [
                        {
                            "id": "prompt-template-review",
                            "kind": "llm_prompt_template",
                            "user_term": "rating prompt",
                            "status": "resolved",
                            "draft": _PROMPT,
                            "event_id": "event-1",
                            "accepted_value": _PROMPT,
                            "accepted_artifact_hash": None,
                            "resolved_prompt_template_hash": stable_hash("a prompt the user never saw"),
                        }
                    ],
                },
            ),
        )
    )


def test_resolved_prompt_review_drift_is_a_readiness_blocker_not_an_exception() -> None:
    mock_yaml_gen = MagicMock(spec=YamlGenerator)

    result = validate_pipeline_for_trained_operator(_drifted_prompt_review_state(), _make_settings(), mock_yaml_gen)

    assert result.is_valid is False
    expected_detail = "The approved llm_prompt_template review for transform 'test_node' no longer matches the current pipeline."
    failed = next(check for check in result.checks if check.name == CHECK_INTERPRETATION_REVIEW)
    assert failed.passed is False
    assert failed.detail == expected_detail
    assert failed.affected_nodes == ("test_node",)
    assert len(result.errors) == 1
    error = result.errors[0]
    assert error.error_code == "interpretation_review_drift"
    assert error.component_id == "test_node"
    assert error.component_type == "transform"
    assert error.message == expected_detail
    assert result.readiness.execution_ready is False
    assert [(b.code, b.component_id, b.component_type, b.detail) for b in result.readiness.blockers] == [
        ("interpretation_review_drift", "test_node", "transform", expected_detail)
    ]
    # Fixed copy only: the raw integrity message never reaches the result.
    assert "hash drifted" not in result.model_dump_json()
    mock_yaml_gen.generate_yaml.assert_not_called()
