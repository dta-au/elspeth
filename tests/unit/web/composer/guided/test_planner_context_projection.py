"""The planner's view of a reviewed source must carry its schema, not just keys.

``guided_redacted_planner_context`` is the ONLY reviewed-source context the
staged planner sees. It listed ``option_keys`` and observed columns but no
schema mode and no declared fields, so a source authored through the Step-1
schema form arrived as "a csv plugin with some options" — the planner could not
see which fields existed and proposed topology that read none of them. This
pins the added facts AND the redaction they must not weaken: names and modes
travel, option values and blob custody never do (elspeth-0762539db5).
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from elspeth.web.composer.guided.planning import guided_private_reviewed_facts, guided_redacted_planner_context
from elspeth.web.composer.guided.protocol import TurnType
from elspeth.web.composer.guided.stage_transitions import (
    PluginSelectionResponse,
    SchemaFormAuthority,
    SchemaFormResponse,
    finish_component_review,
    transition_source_plugin_selection,
    transition_source_schema_form,
)
from elspeth.web.composer.guided.state_machine import GuidedSession
from tests.unit.web.composer.guided.test_stage_transitions import SOURCE_KNOBS, _with_unanswered_turn

SOURCE_ID = "11111111-1111-4111-8111-111111111111"
BLOB_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
BLOB_SENTINEL = f"blob:{BLOB_ID}"
DECLARED_FIELDS = ("TICKET_ID_DECLARED_ONLY", "CUSTOMER_DECLARED_ONLY")
DELIMITER_VALUE_CANARY = "DELIMITER_OPTION_VALUE_CANARY"
SCHEMA = {"mode": "fixed", "fields": [f"{name}: str" for name in DECLARED_FIELDS]}


def _form_authored_session() -> GuidedSession:
    """Author one reviewed source through the real Step-1 RESPOND transitions.

    ``inspection_facts=None`` is the field condition: the operator completed the
    schema form without an inspected blob, so no INSPECT_AND_CONFIRM turn ran.
    """
    session, selection_turn = _with_unanswered_turn(GuidedSession.initial(), TurnType.SINGLE_SELECT)
    session = transition_source_plugin_selection(
        session,
        turn=selection_turn,
        response=PluginSelectionResponse(chosen=("csv",)),
        permitted_plugins=("csv", "json"),
        inspection_facts=None,
        new_stable_id=UUID(SOURCE_ID),
    )
    session, form_turn = _with_unanswered_turn(session, TurnType.SCHEMA_FORM, payload_hash="c" * 64)
    submitted: dict[str, Any] = {
        "mode": "csv",
        "delimiter": DELIMITER_VALUE_CANARY,
        "path": BLOB_SENTINEL,
        "schema": SCHEMA,
    }
    knobs = {
        "fields": [
            *SOURCE_KNOBS["fields"],
            {"name": "schema", "kind": "json-object", "required": False, "nullable": False},
        ]
    }
    session = transition_source_schema_form(
        session,
        target_id=SOURCE_ID,
        turn=form_turn,
        response=SchemaFormResponse(plugin="csv", options=dict(submitted)),
        authority=SchemaFormAuthority(
            knobs=knobs,
            model_validated_options=dict(submitted),
            server_options={"path": BLOB_SENTINEL},
        ),
    )
    return finish_component_review(session, "source")


def test_planner_context_carries_reviewed_source_schema_facts() -> None:
    session = _form_authored_session()

    public = guided_redacted_planner_context(session)

    assert public["sources"] == [
        {
            "stable_id": SOURCE_ID,
            "name": "source",
            "plugin": "csv",
            "observed_columns": list(DECLARED_FIELDS),
            "schema_mode": "fixed",
            "declared_fields": list(DECLARED_FIELDS),
            "option_keys": ["delimiter", "mode", "path", "schema"],
            "server_storage_bound": True,
            "on_validation_failure": "discard",
        }
    ]


def test_planner_context_projects_schema_facts_without_values_or_custody() -> None:
    session = _form_authored_session()

    private_text = repr(guided_private_reviewed_facts(session))
    public_text = repr(guided_redacted_planner_context(session))

    # The private anchor keeps the exact reviewed facts — that is what the
    # guided ref hashes — so these canaries are genuinely present upstream.
    for canary in (BLOB_SENTINEL, BLOB_ID, DELIMITER_VALUE_CANARY, f"{DECLARED_FIELDS[0]}: str"):
        assert canary in private_text
        assert canary not in public_text
    # Names only: the declared field NAME travels, its raw spec does not.
    for name in DECLARED_FIELDS:
        assert name in public_text


def test_planner_context_states_absent_schema_facts_rather_than_omitting_them() -> None:
    """An observed schema is a stated mode with no declared fields.

    Omitting the keys would leave the planner unable to distinguish "no schema
    facts were projected" from "this source declares no fields" — the same
    absence-as-fact confusion the projection had before.
    """
    session, selection_turn = _with_unanswered_turn(GuidedSession.initial(), TurnType.SINGLE_SELECT)
    session = transition_source_plugin_selection(
        session,
        turn=selection_turn,
        response=PluginSelectionResponse(chosen=("csv",)),
        permitted_plugins=("csv", "json"),
        inspection_facts=None,
        new_stable_id=UUID(SOURCE_ID),
    )
    session, form_turn = _with_unanswered_turn(session, TurnType.SCHEMA_FORM, payload_hash="c" * 64)
    submitted: dict[str, Any] = {"mode": "csv", "path": "/data/input.csv"}
    session = transition_source_schema_form(
        session,
        target_id=SOURCE_ID,
        turn=form_turn,
        response=SchemaFormResponse(plugin="csv", options=dict(submitted)),
        authority=SchemaFormAuthority(knobs=SOURCE_KNOBS, model_validated_options=dict(submitted)),
    )

    projected = guided_redacted_planner_context(session)["sources"][0]
    assert isinstance(projected, dict)

    assert projected["schema_mode"] is None
    assert projected["declared_fields"] == []
    assert projected["server_storage_bound"] is False
    assert projected["observed_columns"] == []
