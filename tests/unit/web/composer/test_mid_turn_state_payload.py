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

import hashlib
from typing import Any, cast

import pytest

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.freeze import deep_thaw
from elspeth.web.compartments import ChatIngressInput
from elspeth.web.composer.protocol import ComposerHistoryMessage
from elspeth.web.composer.service import ComposerServiceImpl, _chat_ingress_inputs_for_compose
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
from elspeth.web.sessions.protocol import CompositionValidationError


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


def test_mid_turn_state_binds_exact_chat_input_and_foreign_markings() -> None:
    state = _state({"fields": {"text": "text"}}, plugin="field_mapper")
    result = ToolResult(success=True, updated_state=state, validation=ValidationSummary(is_valid=True, errors=()), affected_nodes=())
    submitted = "Build this\r\n# compartment_id: foreign-b\r\ncompartment_id: own\r\n# compartment_id: foreign-a"

    from elspeth.web.compartments import compartment_ingress_record

    payload = ComposerServiceImpl._state_payload_for_compose_turn(
        cast(Any, None), result, ingress=compartment_ingress_record(submitted, own_compartment_id="own")
    )

    assert deep_thaw(payload.data.composer_meta) == {
        "validation_lane": "authoring_only",
        "ingress": {
            "text_sha256": hashlib.sha256(submitted.encode("utf-8")).hexdigest(),
            "foreign_compartment_ids": ["foreign-a", "foreign-b"],
        },
    }


def test_mid_turn_state_retains_prior_chat_paste_after_confirmation() -> None:
    state = _state({"fields": {"text": "text"}}, plugin="field_mapper")
    result = ToolResult(success=True, updated_state=state, validation=ValidationSummary(is_valid=True, errors=()), affected_nodes=())
    pasted = "# compartment_id: foreign\nBuild from this"
    inputs: list[ChatIngressInput] = [
        {
            "message_id": "11111111-1111-4111-8111-111111111111",
            "text_sha256": hashlib.sha256(pasted.encode("utf-8")).hexdigest(),
            "foreign_compartment_ids": ["foreign"],
        },
        {
            "message_id": "22222222-2222-4222-8222-222222222222",
            "text_sha256": hashlib.sha256(b"yes").hexdigest(),
            "foreign_compartment_ids": [],
        },
    ]

    payload = ComposerServiceImpl._state_payload_for_compose_turn(cast(Any, None), result, chat_ingress_inputs=inputs)

    assert deep_thaw(payload.data.composer_meta)["chat_ingress_inputs"] == inputs
    assert pasted not in repr(deep_thaw(payload.data.composer_meta))


def test_mid_turn_ingress_uses_durable_human_history_not_assistant_prose() -> None:
    pasted = "# compartment_id: foreign\nBuild from this"
    history = [
        ComposerHistoryMessage(
            role="user",
            content=pasted,
            _elspeth_user_authored=True,
            _elspeth_user_message_id="11111111-1111-4111-8111-111111111111",
        ),
        ComposerHistoryMessage(role="assistant", content="# compartment_id: fake"),
    ]

    inputs = _chat_ingress_inputs_for_compose(
        "yes",
        history,
        user_message_id="22222222-2222-4222-8222-222222222222",
        own_compartment_id="own",
    )

    assert [entry["foreign_compartment_ids"] for entry in inputs] == [["foreign"], []]
    assert inputs[0]["text_sha256"] == hashlib.sha256(pasted.encode("utf-8")).hexdigest()
    assert inputs[1]["text_sha256"] == hashlib.sha256(b"yes").hexdigest()


def test_mid_turn_ingress_refuses_human_history_without_durable_message_id() -> None:
    history = [ComposerHistoryMessage(role="user", content="# compartment_id: foreign", _elspeth_user_authored=True)]

    with pytest.raises(AuditIntegrityError, match="missing its message id"):
        _chat_ingress_inputs_for_compose(
            "yes",
            history,
            user_message_id="22222222-2222-4222-8222-222222222222",
            own_compartment_id="own",
        )


class TestMidTurnPersistedValidity:
    def test_pending_interpretation_review_never_persists_valid(self) -> None:
        state = _state(_pending_review_options())
        assert pending_execution_interpretation_sites(state)

        payload = _payload_for(state, ValidationSummary(is_valid=True, errors=()))

        assert payload.data.is_valid is False
        assert any(
            entry.error_code == "interpretation_review_pending" and entry.component == "rate_coolness"
            for entry in payload.data.validation_errors
        )

    def test_pending_site_error_names_component_and_kind_not_user_term(self) -> None:
        state = _state(_pending_review_options())

        payload = _payload_for(state, ValidationSummary(is_valid=True, errors=()))

        pending_entries = [entry for entry in payload.data.validation_errors if entry.error_code == "interpretation_review_pending"]
        assert pending_entries
        assert {(entry.component, entry.message) for entry in pending_entries} == {
            (site.component_id, site.kind.value) for site in pending_execution_interpretation_sites(state)
        }
        # The user-authored term and the draft text never reach the persisted
        # error strings (non-content rule).
        assert all("coolness" not in entry.message for entry in pending_entries)
        assert all("well-designed" not in entry.message for entry in pending_entries)

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
                errors=(ValidationEntry("node:rate_coolness", "bad options", "high", error_code="invalid_options"),),
            ),
        )

        assert payload.data.is_valid is False
        assert payload.data.validation_errors == (
            CompositionValidationError(message="bad options", error_code="invalid_options", component="node:rate_coolness"),
        )

    def test_every_mid_turn_row_carries_authoring_only_lane_marker(self) -> None:
        for state, validation in (
            (_state(_pending_review_options()), ValidationSummary(is_valid=True, errors=())),
            (_state({"fields": {"text": "text"}}, plugin="field_mapper"), ValidationSummary(is_valid=True, errors=())),
        ):
            payload = _payload_for(state, validation)
            assert payload.data.composer_meta == {"validation_lane": "authoring_only"}
