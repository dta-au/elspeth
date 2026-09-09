"""Persisted-shape pins for the mid-turn compose writer (elspeth-67c6fa691d).

Two writers share ``composition_states.is_valid``: the mid-turn compose
writer (``ComposerServiceImpl._state_payload_for_compose_turn``, Stage-1
authoring lane) and the strict turn-end writer
(``_composition_state_data_for_persist``). These tests pin the mid-turn
lane's persisted shape:

* a state carrying pending mandatory interpretation-review sites must NOT
  persist ``is_valid=True`` even when Stage-1 ``validate()`` passes;
* the pending sites are named in ``validation_errors`` (component id and
  kind only — never the user-authored term);
* every mid-turn row carries the ``validation_lane="authoring_only"``
  marker in ``composer_meta`` (documented at
  ``web/sessions/models.py::composition_states_table``).
"""

from __future__ import annotations

from typing import Any, cast

from elspeth.web.composer.service import ComposerServiceImpl
from elspeth.web.composer.state import (
    CompositionState,
    NodeSpec,
    PipelineMetadata,
    ValidationEntry,
    ValidationSummary,
)
from elspeth.web.composer.tools._common import ToolResult
from elspeth.web.interpretation_state import (
    INTERPRETATION_REQUIREMENTS_KEY,
    PROMPT_TEMPLATE_PARTS_KEY,
    pending_execution_interpretation_sites,
)


def _node(options: dict[str, object], *, plugin: str = "llm") -> NodeSpec:
    return NodeSpec(
        id="rate_coolness",
        node_type="transform",
        plugin=plugin,
        input="source",
        on_success="output",
        on_error="stop",
        options=options,
        condition=None,
        routes=None,
        fork_to=None,
        branches=None,
        policy=None,
        merge=None,
    )


def _state(options: dict[str, object], *, plugin: str = "llm") -> CompositionState:
    return CompositionState(
        source=None,
        nodes=(_node(options, plugin=plugin),),
        edges=(),
        outputs=(),
        metadata=PipelineMetadata(),
        version=2,
    )


def _pending_review_options() -> dict[str, object]:
    return {
        "prompt_template": "Rate pending interpretation: {{ row.text }}",
        PROMPT_TEMPLATE_PARTS_KEY: [
            {"kind": "text", "text": "Rate "},
            {"kind": "interpretation_ref", "requirement_id": "coolness"},
            {"kind": "text", "text": ": {{ row.text }}"},
        ],
        INTERPRETATION_REQUIREMENTS_KEY: [
            {
                "id": "coolness",
                "kind": "vague_term",
                "user_term": "coolness",
                "status": "pending",
                "draft": "well-designed and useful",
                "event_id": "event-1",
                "accepted_value": None,
                "accepted_artifact_hash": None,
                "resolved_prompt_template_hash": None,
            }
        ],
    }


def _payload_for(state: CompositionState, validation: ValidationSummary) -> Any:
    tool_result = ToolResult(
        success=True,
        updated_state=state,
        validation=validation,
        affected_nodes=("rate_coolness",),
    )
    # The method deletes ``self`` before use; invoke it unbound so the pin
    # does not need a fully wired service instance.
    return ComposerServiceImpl._state_payload_for_compose_turn(cast(Any, None), tool_result)


class TestMidTurnPersistedValidity:
    def test_pending_interpretation_review_never_persists_valid(self) -> None:
        state = _state(_pending_review_options())
        assert pending_execution_interpretation_sites(state)

        payload = _payload_for(state, ValidationSummary(is_valid=True, errors=()))

        assert payload.data.is_valid is False
        assert any(entry.startswith("interpretation_review_pending:rate_coolness:") for entry in payload.data.validation_errors)

    def test_pending_site_error_names_component_and_kind_not_user_term(self) -> None:
        state = _state(_pending_review_options())

        payload = _payload_for(state, ValidationSummary(is_valid=True, errors=()))

        pending_entries = [entry for entry in payload.data.validation_errors if entry.startswith("interpretation_review_pending:")]
        assert pending_entries
        assert all(entry.count(":") == 2 for entry in pending_entries)
        # The user-authored term and the draft text never reach the persisted
        # error strings (non-content rule).
        assert all("coolness" not in entry.split(":", 1)[1].replace("rate_coolness", "") for entry in pending_entries)
        assert all("well-designed" not in entry for entry in pending_entries)

    def test_clean_state_persists_stage1_verdict(self) -> None:
        state = _state({"fields": {"text": "text"}}, plugin="field_mapper")
        assert not pending_execution_interpretation_sites(state)

        payload = _payload_for(state, ValidationSummary(is_valid=True, errors=()))

        assert payload.data.is_valid is True
        assert payload.data.validation_errors == ()

    def test_stage1_failure_stays_invalid_and_keeps_messages(self) -> None:
        state = _state({"fields": {"text": "text"}}, plugin="field_mapper")

        payload = _payload_for(
            state,
            ValidationSummary(
                is_valid=False,
                errors=(ValidationEntry("node:rate_coolness", "bad options", "high"),),
            ),
        )

        assert payload.data.is_valid is False
        assert payload.data.validation_errors == ("bad options",)

    def test_every_mid_turn_row_carries_authoring_only_lane_marker(self) -> None:
        for state, validation in (
            (_state(_pending_review_options()), ValidationSummary(is_valid=True, errors=())),
            (_state({"fields": {"text": "text"}}, plugin="field_mapper"), ValidationSummary(is_valid=True, errors=())),
        ):
            payload = _payload_for(state, validation)
            assert payload.data.composer_meta == {"validation_lane": "authoring_only"}
