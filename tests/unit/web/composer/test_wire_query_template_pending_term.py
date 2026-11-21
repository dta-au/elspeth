"""A prompt file wired onto one query's template, on a node whose node-level prompt has term reviews.

Vague-term review state belongs to the node-level ``prompt_template`` only: a
slot is an ``interpretation_ref`` entry in ``prompt_template_parts``, and a
query reaches it by inheriting the node-level prompt. ``vague_term_wiring_count``
therefore reads every PENDING term on the node as unresolvable once any query
has a template of its own, and the node-authoring doors refuse to write that
shape (``vague_term_query_override``).

``wire_blob_inline_ref`` used to be the one door without that check: it wrote
the marker, left a pending review nothing could resolve, and from then on
``patch_node_options``, ``upsert_node`` and ``splice_transform`` refused edits to
every node in the pipeline. The wire now refuses instead, writes nothing, and
names its repairs: have the user approve the pending term (always open: the
term gets its card at the end of the turn as usual) and wire again, or, only
when the file is meant for every query, bind it to the node-level prompt.

The refusal's sentences are guidance text and are asserted by their stable
parts only (code, ids, field paths, "Nothing was changed."). The older doors'
text is asserted verbatim: this change must not alter it.

Sessions stranded before the fix still hold that state. The tests named
``test_recovery_of_an_already_stranded_session_by_...`` and
``test_an_already_stranded_session_...`` build it by hand (the wire can no longer
produce it) and are the proof that such a session can be repaired. They are not
dead code: do not delete them because the state is "unreachable".

Every starting state is admitted by the real ``set_pipeline`` door, every
mutation goes through the real tool dispatcher with the real plugin catalog,
and the card and proposal tests run on the real session service and route.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
import structlog
from sqlalchemy import MetaData, select
from sqlalchemy.engine import Engine
from sqlalchemy.pool import StaticPool

from elspeth.contracts.composer_interpretation import InterpretationChoice, InterpretationKind, InterpretationSurfaceOrigin
from elspeth.contracts.freeze import deep_thaw
from elspeth.contracts.hashing import stable_hash
from elspeth.web.blobs.service import BlobServiceImpl
from elspeth.web.catalog.policy_view import PolicyCatalogView
from elspeth.web.composer import yaml_generator
from elspeth.web.composer.interpretation_surfacing import (
    surface_pending_interpretation_reviews_for_state,
    unsurfaceable_pending_interpretation_review_sites,
)
from elspeth.web.composer.protocol import ToolArgumentError
from elspeth.web.composer.state import CompositionState, NodeSpec
from elspeth.web.composer.tools import ToolResult, build_set_pipeline_candidate
from elspeth.web.composer.tools import blobs as blob_tools
from elspeth.web.composer.tools import execute_tool as _execute_tool
from elspeth.web.composer.tools.sessions import _assert_affected_component, _handle_request_interpretation_review
from elspeth.web.coordination.sqlite_authority import SQLiteLocalSessionOperationAuthority
from elspeth.web.dependencies import create_catalog_service
from elspeth.web.interpretation_state import (
    INTERPRETATION_REQUIREMENTS_KEY,
    PROMPT_TEMPLATE_PARTS_KEY,
    VAGUE_TERM_QUERY_OVERRIDE_PREFIX,
    InterpretationReviewPending,
    materialize_state_for_execution,
    reconcile_authoritative_reviews,
    unwired_vague_term_error,
    vague_term_wiring_count,
)
from elspeth.web.plugin_policy.models import PluginAvailabilitySnapshot
from elspeth.web.sessions.converters import state_from_record
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.models import chat_messages_table, composition_proposals_table
from elspeth.web.sessions.protocol import CompositionStateData, CompositionStateRecord
from elspeth.web.sessions.schema import initialize_session_schema
from elspeth.web.sessions.telemetry import build_sessions_telemetry
from tests.helpers.session_fences import fenced_operation_context
from tests.unit.web._sync_asgi_client import SyncASGITestClient
from tests.unit.web.composer.test_set_pipeline_candidate import _empty_state, _structured_llm_args, _trained_context
from tests.unit.web.composer.test_set_pipeline_review_finalization import _wire_pending_tone_review
from tests.unit.web.execution.test_validation import _make_settings, validate_pipeline_for_trained_operator
from tests.unit.web.sessions.session_test_authority import FencedSessionServiceHarness
from tests.unit.web.sessions.test_blob_proposal_accept import _create_test_blob, _create_test_proposal, _make_app
from tests.unit.web.sessions.test_interpretation_events_service import (
    _compose_operation,
    _insert_session,
    _save_composition_state,
)

_NODE = "classify"
_OTHER = "copy"
_TERM = "tone"
_TERM_ROW = f"{_TERM}:{_NODE}"
_DRAFT = "Use a concise factual tone."
_SECOND_TERM = "style"
_SECOND_TERM_ROW = f"{_SECOND_TERM}:{_NODE}"
_QUERY_OVERRIDE_CODE = "vague_term_query_override"
_INHERITING_QUERIES: dict[str, Any] = {name: {"input_fields": {"text": "text"}} for name in ("first", "second")}
_OTHER_NODE_PATCH: dict[str, Any] = {"node_id": _OTHER, "patch": {"schema": {"mode": "observed"}}}

# The exact text the node-authoring doors (patch_node_options, upsert_node, splice_transform, set_pipeline)
# return for a pending term beside a query template, copied from a run on ce7d94c1a, before this change.
# Those doors are untouched and their text must stay byte-identical.
_DOOR_REFUSAL = (
    "Pending vague_term review is bypassed by a query template on node 'classify': requirement 'tone:classify' "
    "(user term 'tone'). Query templates for 'first' bypass the node-level template. A node-level reviewed slot is "
    "rejected at staging when any query has its own template. Place the reviewed term on a node where all queries "
    "use the node-level template."
)
_NOTHING_CHANGED = "Nothing was changed."


def _assert_wire_refusal(result: ToolResult, *, node_id: str, option_path: str, terms: Mapping[str, str]) -> str:
    """The stable facts of the wire's refusal: the code, and every id the planner has to send back.

    The sentences around them are guidance text and may be reworded; these parts may not.
    """
    assert result.success is False
    assert [error.error_code for error in result.validation.errors] == [_QUERY_OVERRIDE_CODE]
    message = result.validation.errors[0].message
    assert f"Binding a file to {option_path} " in message
    assert _NOTHING_CHANGED in message
    assert _QUERY_OVERRIDE_CODE in message
    assert f"node {node_id!r}" in message
    for requirement_id, user_term in terms.items():
        assert f"requirement {requirement_id!r}" in message
        assert repr(user_term) in message
    # Approving the pending review comes first; the node-level path is named with what it costs.
    assert message.index("(a)") < message.index("(b)")
    assert "wire_blob_inline_ref again with the same field_path" in message
    assert "EVERY query" in message
    assert f"node:{node_id}.options.prompt_template" in message
    assert "retires its term reviews" in message
    return message


# --------------------------------------------------------------------------- #
# State builders: every starting state is admitted by the real set_pipeline door
# --------------------------------------------------------------------------- #


def _arguments(tmp_path: Path, *, list_form: bool = False, term: bool = True, second_term: bool = False) -> dict[str, Any]:
    """csv -> llm ``classify`` (two queries, both inheriting the node prompt) -> passthrough ``copy`` -> json."""
    arguments = _structured_llm_args(tmp_path)
    node = arguments["nodes"][0]
    node["on_success"] = "classified"
    if term:
        _wire_pending_tone_review(node)
    if second_term:
        options = node["options"]
        options["prompt_template"] += " Style: Plain."
        options[PROMPT_TEMPLATE_PARTS_KEY] += [
            {"kind": "text", "text": " Style: "},
            {"kind": "interpretation_ref", "requirement_id": _SECOND_TERM_ROW},
        ]
        options[INTERPRETATION_REQUIREMENTS_KEY].append({"kind": "vague_term", "user_term": _SECOND_TERM, "draft": "Plain."})
    node["options"]["queries"] = (
        [{"name": name, **query} for name, query in _INHERITING_QUERIES.items()] if list_form else dict(_INHERITING_QUERIES)
    )
    arguments["nodes"].append(
        {
            "id": _OTHER,
            "node_type": "transform",
            "plugin": "passthrough",
            "input": "classified",
            "on_success": "main",
            "on_error": "discard",
            "options": {"schema": {"mode": "observed"}},
        }
    )
    return arguments


def _admitted_state(tmp_path: Path, *, list_form: bool = False, term: bool = True, second_term: bool = False) -> CompositionState:
    candidate = build_set_pipeline_candidate(
        _arguments(tmp_path, list_form=list_form, term=term, second_term=second_term),
        _empty_state(),
        _trained_context(data_dir=tmp_path),
    )
    assert candidate.acceptable, candidate.result.to_dict()
    return candidate.result.updated_state


def _node(state: CompositionState, node_id: str = _NODE) -> NodeSpec:
    return next(node for node in state.nodes if node.id == node_id)


def _options(state: CompositionState, node_id: str = _NODE) -> dict[str, Any]:
    return deep_thaw(_node(state, node_id).options)


def _term_rows(state: CompositionState) -> list[tuple[str, str]]:
    return [(row["id"], row["status"]) for row in _options(state)[INTERPRETATION_REQUIREMENTS_KEY] if row["kind"] == "vague_term"]


def _with_accepted_term(state: CompositionState) -> CompositionState:
    """The same node after the user accepted the term card: the row is resolved and the prompt renders its value."""
    options = _options(state)
    text = next(part["text"] for part in options[PROMPT_TEMPLATE_PARTS_KEY] if part["kind"] == "text")
    rows = []
    for row in options[INTERPRETATION_REQUIREMENTS_KEY]:
        if row["kind"] == "vague_term":
            row = {
                **row,
                "status": "resolved",
                "event_id": "evt-tone",
                "accepted_value": _DRAFT,
                "accepted_artifact_hash": None,
                "resolved_prompt_template_hash": stable_hash(_DRAFT),
            }
        rows.append(row)
    options[INTERPRETATION_REQUIREMENTS_KEY] = rows
    options["prompt_template"] = text + _DRAFT
    return replace(state, nodes=tuple(replace(node, options=options) if node.id == _NODE else node for node in state.nodes))


def _query_template(state: CompositionState, *, list_form: bool = False, query: str = "first") -> Any:
    queries = _options(state)["queries"]
    entry = next(item for item in queries if item["name"] == query) if list_form else queries[query]
    return entry["template"] if "template" in entry else None


def _option_path(*, list_form: bool = False) -> str:
    return f"queries.{'0' if list_form else 'first'}.template"


def _query_path(*, list_form: bool = False) -> str:
    return f"node:{_NODE}.options.{_option_path(list_form=list_form)}"


def _vague_sites(state: CompositionState) -> list[tuple[str, str]]:
    materialized = materialize_state_for_execution(state)
    if not isinstance(materialized, InterpretationReviewPending):
        return []
    return [(site.component_id, site.user_term) for site in materialized.sites if site.kind is InterpretationKind.VAGUE_TERM]


def _state_data(state: CompositionState) -> CompositionStateData:
    document = state.to_dict()
    return CompositionStateData(
        sources=document["sources"],
        nodes=document["nodes"],
        edges=document["edges"],
        outputs=document["outputs"],
        metadata_=document["metadata"],
        is_valid=True,
    )


# --------------------------------------------------------------------------- #
# Session environment and tool calls
# --------------------------------------------------------------------------- #


@pytest.fixture()
def env(tmp_path: Path) -> Iterator[dict[str, Any]]:
    """One session database shared by the tool handlers and the session service."""
    engine = create_session_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    initialize_session_schema(engine)
    session_id = str(uuid4())
    with engine.begin() as conn:
        _insert_session(conn, session_id)
        conn.execute(
            chat_messages_table.insert().values(
                id="user-message-1",
                session_id=session_id,
                role="user",
                content="Use this file as the prompt.",
                raw_content=None,
                tool_calls=None,
                tool_call_id=None,
                sequence_no=1,
                writer_principal="route_user_message",
                created_at=datetime.now(UTC),
                composition_state_id=None,
                parent_assistant_id=None,
            )
        )
    (tmp_path / "blobs").mkdir(exist_ok=True)
    yield {"engine": engine, "session_id": session_id, "data_dir": str(tmp_path)}


def _call(env: dict[str, Any], tool_name: str, arguments: dict[str, Any], state: CompositionState) -> ToolResult:
    """Run one composer tool through the strict dispatcher with the real plugin catalog."""
    catalog = create_catalog_service()
    snapshot = PluginAvailabilitySnapshot.for_trained_operator(catalog)
    with fenced_operation_context(env["engine"], env["session_id"]) as context:
        return _execute_tool(
            tool_name,
            arguments,
            state,
            PolicyCatalogView.for_trained_operator(catalog, snapshot),
            plugin_snapshot=snapshot,
            data_dir=env["data_dir"],
            session_engine=env["engine"],
            session_id=env["session_id"],
            session_operation_context=context,
            session_operation_authority=SQLiteLocalSessionOperationAuthority(env["engine"]),
            user_message_id="user-message-1",
            user_message_content="Use this exact content:\nAnswer about {{ row.text }}",
        )


def _prompt_file(env: dict[str, Any]) -> dict[str, Any]:
    result = _call(
        env,
        "create_blob",
        {"filename": "prompt.txt", "mime_type": "text/plain", "content": "Answer about {{ row.text }}"},
        _empty_state(),
    )
    assert result.success, result.to_dict()
    return dict(result.data)


def _marker(blob: Mapping[str, Any]) -> dict[str, Any]:
    return {"blob_ref": blob["blob_id"], "mode": "inline_content", "sha256": blob["content_hash"]}


def _wire(env: dict[str, Any], state: CompositionState, field_path: str, blob: Mapping[str, Any]) -> ToolResult:
    return _call(env, "wire_blob_inline_ref", {"field_path": field_path, "blob_id": blob["blob_id"]}, state)


def _hand_stranded(healthy: CompositionState, blob: Mapping[str, Any]) -> CompositionState:
    """The state the wire stored before it had the check, built from the same three steps it ran.

    Marker at the query position, the node's prompt review re-staged, reviews
    reconciled. The wire handler now refuses this write, so a session that
    holds the state today got it before the fix; the recovery tests start here.
    """
    marked = blob_tools._apply_inline_blob_marker(healthy, _query_path(), _marker(blob))
    restaged = replace(
        marked,
        nodes=tuple(
            replace(
                node,
                options=blob_tools._options_with_default_prompt_template_review(
                    node_id=node.id, plugin=node.plugin, options=node.options, existing_options=_node(healthy).options
                ),
            )
            if node.id == _NODE
            else node
            for node in marked.nodes
        ),
    )
    stranded = reconcile_authoritative_reviews(healthy, restaged)
    # The premise every recovery test relies on: the term is pending and nothing can resolve it.
    assert _query_template(stranded) == _marker(blob)
    assert _term_rows(stranded) == [(_TERM_ROW, "pending")]
    assert vague_term_wiring_count(_options(stranded), user_term=_TERM) == 0
    error = unwired_vague_term_error(stranded)
    assert error is not None
    assert error.startswith(f"{VAGUE_TERM_QUERY_OVERRIDE_PREFIX} on node {_NODE!r}: requirement {_TERM_ROW!r}")
    return stranded


def _stranded_state(env: dict[str, Any], tmp_path: Path) -> CompositionState:
    return _hand_stranded(_admitted_state(tmp_path), _prompt_file(env))


def _codes(result: ToolResult) -> list[str | None]:
    return [error.error_code for error in result.validation.errors]


def _stored_rows(engine: Engine) -> dict[str, list[tuple[Any, ...]]]:
    """Every row of every table in the session database."""
    metadata = MetaData()
    metadata.reflect(bind=engine)
    with engine.connect() as connection:
        return {
            name: sorted((tuple(row) for row in connection.execute(select(table))), key=repr) for name, table in metadata.tables.items()
        }


def _stored_files(data_dir: str) -> list[tuple[str, bytes]]:
    root = Path(data_dir) / "blobs"
    return sorted((str(path.relative_to(root)), path.read_bytes()) for path in root.rglob("*") if path.is_file())


# --------------------------------------------------------------------------- #
# The fix: the wire refuses, names both repairs, and writes nothing
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("list_form", [False, True])
def test_query_file_wire_over_a_pending_node_term_is_refused_with_both_repairs(
    env: dict[str, Any], tmp_path: Path, list_form: bool
) -> None:
    state = _admitted_state(tmp_path, list_form=list_form)
    blob = _prompt_file(env)
    assert vague_term_wiring_count(_options(state), user_term=_TERM) == 1

    result = _wire(env, state, _query_path(list_form=list_form), blob)

    _assert_wire_refusal(result, node_id=_NODE, option_path=_option_path(list_form=list_form), terms={_TERM_ROW: _TERM})
    assert result.updated_state is state
    # The state handed back is the one the caller had: the term is still wired and its card can still be raised.
    assert _query_template(result.updated_state, list_form=list_form) is None
    assert vague_term_wiring_count(_options(result.updated_state), user_term=_TERM) == 1
    assert unwired_vague_term_error(result.updated_state) is None
    assert unsurfaceable_pending_interpretation_review_sites(result.updated_state) == ()


def test_a_refused_query_file_wire_names_every_pending_term(env: dict[str, Any], tmp_path: Path) -> None:
    state = _admitted_state(tmp_path, second_term=True)
    assert _term_rows(state) == [(_TERM_ROW, "pending"), (_SECOND_TERM_ROW, "pending")]

    result = _wire(env, state, _query_path(), _prompt_file(env))

    _assert_wire_refusal(result, node_id=_NODE, option_path=_option_path(), terms={_TERM_ROW: _TERM, _SECOND_TERM_ROW: _SECOND_TERM})
    assert result.updated_state is state


def test_a_refused_query_file_wire_keeps_a_long_term_and_a_long_query_name_whole(env: dict[str, Any], tmp_path: Path) -> None:
    """The planner sends the field path and the requirement id back, so neither is shortened.

    The other doors echo both unshortened too (``unwired_vague_term_error``, ``vague_term_query_override_error``).
    """
    long_term = "tone-" + "t" * 595
    long_query = "query_" + "q" * 194
    arguments = _arguments(tmp_path, term=False)
    options = arguments["nodes"][0]["options"]
    text = options["prompt_template"] + " "
    options["prompt_template"] = text + _DRAFT
    options[PROMPT_TEMPLATE_PARTS_KEY] = [
        {"kind": "text", "text": text},
        {"kind": "interpretation_ref", "requirement_id": f"{long_term}:{_NODE}"},
    ]
    options[INTERPRETATION_REQUIREMENTS_KEY] = [{"kind": "vague_term", "user_term": long_term, "draft": _DRAFT}]
    options["queries"] = {long_query: {"input_fields": {"text": "text"}}, "second": {"input_fields": {"text": "text"}}}
    candidate = build_set_pipeline_candidate(arguments, _empty_state(), _trained_context(data_dir=tmp_path))
    assert candidate.acceptable, candidate.result.to_dict()
    state = candidate.result.updated_state
    assert _term_rows(state) == [(f"{long_term}:{_NODE}", "pending")]

    result = _wire(env, state, f"node:{_NODE}.options.queries.{long_query}.template", _prompt_file(env))

    _assert_wire_refusal(result, node_id=_NODE, option_path=f"queries.{long_query}.template", terms={f"{long_term}:{_NODE}": long_term})
    assert result.updated_state is state


def test_the_refusal_text_pasted_into_explain_validation_error_finds_the_code(env: dict[str, Any], tmp_path: Path) -> None:
    """The message carries the code literal, so a model that pastes the message (not the code) still gets the guidance."""
    state = _admitted_state(tmp_path)
    refused = _wire(env, state, _query_path(), _prompt_file(env))
    message = _assert_wire_refusal(refused, node_id=_NODE, option_path=_option_path(), terms={_TERM_ROW: _TERM})

    explained = _call(env, "explain_validation_error", {"error_text": message}, state)

    assert explained.success, explained.to_dict()
    assert explained.data["error_code"] == _QUERY_OVERRIDE_CODE
    assert "A refusal from wire_blob_inline_ref lists its own repairs" in explained.data["suggested_fix"]


@pytest.mark.asyncio
async def test_after_a_refused_query_file_wire_the_models_own_review_request_for_the_term_is_staged(
    env: dict[str, Any], tmp_path: Path
) -> None:
    """The model can raise the card itself in the same turn: ``request_interpretation_review`` accepts the term."""
    service = FencedSessionServiceHarness(env["engine"], telemetry=build_sessions_telemetry(), log=structlog.get_logger("test"))
    session_id = UUID(env["session_id"])
    healthy = _admitted_state(tmp_path)
    refused = _wire(env, healthy, _query_path(), _prompt_file(env))
    assert refused.success is False
    saved: CompositionStateRecord = await _save_composition_state(
        service, session_id, _state_data(refused.updated_state), provenance="tool_call"
    )

    result = await _handle_request_interpretation_review(
        arguments={"affected_node_id": _NODE, "kind": "vague_term", "user_term": _TERM, "llm_draft": _DRAFT},
        state=refused.updated_state,
        session_id=session_id,
        composition_state_id=saved.id,
        tool_call_id="call_tone",
        now=datetime.now(UTC),
        per_term_cap=3,
        per_session_day_cap=10,
        model_identifier="test-model",
        model_version="test-model-v1",
        provider="test-provider",
        composer_skill_hash="a" * 64,
        create_pending_interpretation_event=service.create_pending_interpretation_event,
        list_interpretation_events=service.list_interpretation_events,
    )

    assert result.success, result.to_dict()
    assert result.data["_kind"] == "interpretation_review_pending"
    assert result.affected_nodes == (_NODE,)
    assert result.updated_state is refused.updated_state
    cards = await service.list_interpretation_events(session_id)
    assert [(card.affected_node_id, card.user_term, card.choice) for card in cards] == [(_NODE, _TERM, InterpretationChoice.PENDING)]
    assert str(cards[0].id) == result.data["event_id"]


def test_a_refused_query_file_wire_writes_nothing(env: dict[str, Any], tmp_path: Path) -> None:
    """No marker, no blob change, no review event, no new state: the database and the blob files are byte-identical."""
    state = _admitted_state(tmp_path)
    rows_before_blob = _stored_rows(env["engine"])
    files_before_blob = _stored_files(env["data_dir"])
    blob = _prompt_file(env)
    rows_before = _stored_rows(env["engine"])
    files_before = _stored_files(env["data_dir"])
    # The instrument sees a write when there is one: creating the file changed both.
    assert rows_before != rows_before_blob
    assert files_before != files_before_blob
    assert rows_before["interpretation_events"] == []
    version_before = state.version
    options_before = _options(state)

    result = _wire(env, state, _query_path(), blob)

    assert result.success is False
    assert result.updated_state is state
    assert result.updated_state.version == version_before
    assert _options(result.updated_state) == options_before
    assert _query_template(result.updated_state) is None
    assert result.data is None
    rows_after = _stored_rows(env["engine"])
    # The only rows a call may touch are the operation fence's own bookkeeping (acquire and release).
    fence_tables = {name for name in rows_after if "fence" in name or "operation" in name}
    assert {name for name in rows_after if rows_after[name] != rows_before[name]} <= fence_tables
    assert rows_after["blobs"] == rows_before["blobs"]
    assert rows_after["interpretation_events"] == []
    assert rows_after["composition_states"] == rows_before["composition_states"]
    assert _stored_files(env["data_dir"]) == files_before


# --------------------------------------------------------------------------- #
# Controls: wires the fix must leave alone
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("list_form", [False, True])
def test_query_file_wire_keeps_an_accepted_node_term_working(env: dict[str, Any], tmp_path: Path, list_form: bool) -> None:
    state = _with_accepted_term(_admitted_state(tmp_path, list_form=list_form))
    blob = _prompt_file(env)
    before = _options(state)

    result = _wire(env, state, _query_path(list_form=list_form), blob)

    assert result.success is True, result.to_dict()
    wired = result.updated_state
    after = _options(wired)
    assert _query_template(wired, list_form=list_form) == _marker(blob)
    assert _term_rows(wired) == [(_TERM_ROW, "resolved")]
    accepted = next(row for row in after[INTERPRETATION_REQUIREMENTS_KEY] if row["kind"] == "vague_term")
    assert accepted["accepted_value"] == _DRAFT
    assert after[PROMPT_TEMPLATE_PARTS_KEY] == before[PROMPT_TEMPLATE_PARTS_KEY]
    assert after["prompt_template"] == before["prompt_template"]
    assert "superseded_review_ids" not in result.data
    assert unwired_vague_term_error(wired) is None
    # Run waits only for the node's prompt card (it now covers the file), not for any term.
    assert _vague_sites(wired) == []
    materialized = materialize_state_for_execution(wired)
    assert isinstance(materialized, InterpretationReviewPending)
    assert (InterpretationKind.LLM_PROMPT_TEMPLATE, _NODE) in [(site.kind, site.component_id) for site in materialized.sites]
    # The rest of the pipeline stays editable.
    edit = _call(env, "patch_node_options", _OTHER_NODE_PATCH, wired)
    assert edit.success is True, edit.to_dict()


@pytest.mark.parametrize("list_form", [False, True])
def test_query_file_wire_on_a_node_without_terms_succeeds(env: dict[str, Any], tmp_path: Path, list_form: bool) -> None:
    state = _admitted_state(tmp_path, list_form=list_form, term=False)
    blob = _prompt_file(env)

    result = _wire(env, state, _query_path(list_form=list_form), blob)

    assert result.success is True, result.to_dict()
    wired = result.updated_state
    assert _query_template(wired, list_form=list_form) == _marker(blob)
    assert unwired_vague_term_error(wired) is None
    assert _vague_sites(wired) == []
    edit = _call(env, "patch_node_options", _OTHER_NODE_PATCH, wired)
    assert edit.success is True, edit.to_dict()


def test_node_prompt_file_wire_over_a_pending_term_still_replaces_the_prompt(env: dict[str, Any], tmp_path: Path) -> None:
    """Replace means replace: a pending term never blocks a file on the node-level prompt itself."""
    state = _admitted_state(tmp_path)
    blob = _prompt_file(env)

    result = _wire(env, state, f"node:{_NODE}.options.prompt_template", blob)

    assert result.success is True, result.to_dict()
    options = _options(result.updated_state)
    assert options["prompt_template"] == _marker(blob)
    assert PROMPT_TEMPLATE_PARTS_KEY not in options
    assert _term_rows(result.updated_state) == []
    assert list(result.data["superseded_review_ids"]) == [_TERM_ROW]


def test_system_prompt_file_wire_over_a_pending_term_succeeds_and_keeps_it_resolvable(env: dict[str, Any], tmp_path: Path) -> None:
    state = _admitted_state(tmp_path)
    blob = _prompt_file(env)
    before = _options(state)

    result = _wire(env, state, f"node:{_NODE}.options.system_prompt", blob)

    assert result.success is True, result.to_dict()
    after = _options(result.updated_state)
    assert after["system_prompt"] == _marker(blob)
    assert after[PROMPT_TEMPLATE_PARTS_KEY] == before[PROMPT_TEMPLATE_PARTS_KEY]
    assert _term_rows(result.updated_state) == [(_TERM_ROW, "pending")]
    assert vague_term_wiring_count(after, user_term=_TERM) == 1
    assert unwired_vague_term_error(result.updated_state) is None
    assert "superseded_review_ids" not in result.data


def test_the_same_marker_written_by_patch_node_options_is_refused(env: dict[str, Any], tmp_path: Path) -> None:
    """The door the wire now agrees with."""
    state = _admitted_state(tmp_path)
    blob = _prompt_file(env)

    result = _call(
        env,
        "patch_node_options",
        {
            "node_id": _NODE,
            "patch": {"queries": {**_INHERITING_QUERIES, "first": {"input_fields": {"text": "text"}, "template": _marker(blob)}}},
        },
        state,
    )

    assert result.success is False
    assert result.updated_state is state
    assert _codes(result) == [_QUERY_OVERRIDE_CODE]
    assert result.validation.errors[0].message == _DOOR_REFUSAL


# --------------------------------------------------------------------------- #
# Explicit-approve mode: accepting a proposed wire replays the same handler
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_accepting_a_proposed_query_file_wire_is_refused_and_leaves_the_head_alone(tmp_path: Path) -> None:
    app, service = _make_app(tmp_path)
    client = SyncASGITestClient(app)
    session = await service.create_session("alice", "Query file", "local")
    healthy = _admitted_state(tmp_path)
    base = await _save_composition_state(service, session.id, _state_data(healthy), provenance="tool_call")
    user_message = await service.add_message(
        session.id, "user", "Use my file as the prompt for the first query.", writer_principal="route_user_message"
    )
    blob = await _create_test_blob(
        service, BlobServiceImpl(service._engine, tmp_path), session.id, filename="prompt.txt", content=b"Answer about {{ row.text }}"
    )
    arguments = {"field_path": _query_path(), "blob_id": str(blob.id)}
    proposal = await _create_test_proposal(
        service,
        session_id=session.id,
        tool_call_id="call_wire",
        tool_name="wire_blob_inline_ref",
        summary="Use the uploaded file as the first query's prompt.",
        rationale="Requested by the user.",
        affects=(_NODE,),
        arguments_json=arguments,
        arguments_redacted_json=arguments,
        base_state_id=base.id,
        actor="composer-web:user:alice",
        user_message_id=user_message.id,
        composer_model_identifier="test-model",
        composer_model_version="test-model-v1",
        composer_provider="test-provider",
        composer_skill_hash="a" * 64,
        tool_arguments_hash=stable_hash(arguments),
    )

    response = client.post(f"/api/sessions/{session.id}/proposals/{proposal.id}/accept")

    assert response.status_code == 422, response.text
    detail = response.json()["detail"]
    assert detail["error_type"] == "proposal_validation_failed"
    assert detail["tool_name"] == "wire_blob_inline_ref"
    refusals = [entry["message"] for entry in detail["validation_errors"] if _NOTHING_CHANGED in entry["message"]]
    assert len(refusals) == 1
    assert f"Binding a file to {_option_path()} " in refusals[0]
    assert f"requirement {_TERM_ROW!r}" in refusals[0]
    with service._engine.connect() as connection:
        stored = connection.execute(select(composition_proposals_table)).one()
    assert stored.status == "rejected"
    head = await service.get_current_state(session.id)
    assert head is not None
    assert head.id == base.id
    assert _query_template(state_from_record(head)) is None
    assert unwired_vague_term_error(state_from_record(head)) is None


# --------------------------------------------------------------------------- #
# Sessions stranded before the fix: what they see
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("door", ["patch_node_options", "upsert_node", "splice_transform"])
def test_an_already_stranded_session_refuses_edits_to_an_unrelated_node(env: dict[str, Any], tmp_path: Path, door: str) -> None:
    """One stranded node refuses node edits anywhere in the pipeline, and the refusal names repairs that work."""
    healthy = _admitted_state(tmp_path)
    other = _node(healthy, _OTHER)
    by_door: dict[str, dict[str, Any]] = {
        "patch_node_options": _OTHER_NODE_PATCH,
        "upsert_node": {
            "id": _OTHER,
            "node_type": "transform",
            "plugin": "passthrough",
            "input": other.input,
            "on_success": other.on_success,
            "on_error": other.on_error,
            "options": {"schema": {"mode": "observed"}},
        },
        # A new passthrough between ``classify`` and ``copy``: it adds a node and leaves the llm node's options alone.
        "splice_transform": {
            "predecessor_id": _NODE,
            "successor_id": _OTHER,
            "node": {"id": "inserted", "plugin": "passthrough", "options": {"schema": {"mode": "observed"}}, "on_error": "discard"},
        },
    }
    arguments = by_door[door]
    # Control: the same edit is accepted on the healthy pipeline.
    control = _call(env, door, arguments, healthy)
    assert control.success is True, control.to_dict()

    stranded = _hand_stranded(healthy, _prompt_file(env))
    result = _call(env, door, arguments, stranded)

    assert result.success is False
    assert result.updated_state is stranded
    assert _codes(result) == [_QUERY_OVERRIDE_CODE]
    assert result.validation.errors[0].message == _DOOR_REFUSAL


def test_an_already_stranded_session_blocks_run_on_the_term(env: dict[str, Any], tmp_path: Path) -> None:
    stranded = _stranded_state(env, tmp_path)

    assert _vague_sites(stranded) == [(_NODE, _TERM)]
    validation = validate_pipeline_for_trained_operator(stranded, _make_settings(str(tmp_path)), yaml_generator)
    assert validation.is_valid is False
    assert "interpretation_review_pending" in [entry.error_code for entry in validation.errors]
    assert _NODE in [entry.component_id for entry in validation.errors if entry.error_code == "interpretation_review_pending"]


def test_an_already_stranded_session_still_accepts_a_file_on_another_prompt_position(env: dict[str, Any], tmp_path: Path) -> None:
    """The wire refuses only what it would strand itself: a node stranded earlier is not a reason to refuse.

    Refusing the second query file would tell the caller to have the user
    approve a review that may have no card. The state is no worse for it: the
    same repairs apply.
    """
    stranded = _stranded_state(env, tmp_path)
    blob = _prompt_file(env)

    second_query = _wire(env, stranded, f"node:{_NODE}.options.queries.second.template", blob)
    system_prompt = _wire(env, stranded, f"node:{_NODE}.options.system_prompt", blob)

    assert second_query.success is True, second_query.to_dict()
    assert _query_template(second_query.updated_state, query="second") == _marker(blob)
    assert system_prompt.success is True, system_prompt.to_dict()
    assert _options(system_prompt.updated_state)["system_prompt"] == _marker(blob)


def test_set_pipeline_restating_a_stranded_node_is_refused(env: dict[str, Any], tmp_path: Path) -> None:
    stranded = _stranded_state(env, tmp_path)
    arguments = _session_arguments(env, tmp_path)
    arguments["nodes"][0]["options"]["queries"] = _options(stranded)["queries"]

    result = _call(env, "set_pipeline", arguments, stranded)

    assert result.success is False
    assert result.updated_state is stranded
    assert _DOOR_REFUSAL in [error.message for error in result.validation.errors if error.error_code == _QUERY_OVERRIDE_CODE]


@pytest.mark.asyncio
async def test_a_refused_query_file_wire_leaves_the_term_to_get_its_card_and_succeeds_once_it_is_approved(
    env: dict[str, Any], tmp_path: Path
) -> None:
    """Term staged and file wire attempted in one turn: repair (a) of the refusal is always open.

    The refused wire hands back the state it was given, so the turn ends on the
    healthy head; the end-of-turn surfacer raises the term's card as for any
    other pending term; the user approves it; the same wire then succeeds.
    """
    service = FencedSessionServiceHarness(env["engine"], telemetry=build_sessions_telemetry(), log=structlog.get_logger("test"))
    session_id = UUID(env["session_id"])
    healthy = _admitted_state(tmp_path)
    blob = _prompt_file(env)

    refused = _wire(env, healthy, _query_path(), blob)
    _assert_wire_refusal(refused, node_id=_NODE, option_path=_option_path(), terms={_TERM_ROW: _TERM})
    assert refused.updated_state is healthy
    assert unsurfaceable_pending_interpretation_review_sites(refused.updated_state) == ()

    # The turn settles on the state the refusal handed back; no card exists yet.
    saved: CompositionStateRecord = await _save_composition_state(
        service, session_id, _state_data(refused.updated_state), provenance="tool_call"
    )
    assert await service.list_interpretation_events(session_id) == []
    async with _compose_operation(service, session_id) as context:
        await surface_pending_interpretation_reviews_for_state(
            state_from_record(saved),
            sessions_service=service,
            session_id=str(session_id),
            session_operation_context=context,
            current_state_id=str(saved.id),
            surface_origin=InterpretationSurfaceOrigin.COMPOSER_LLM,
            model_identifier="test-model",
            model_version="test-model-v1",
            provider="test-provider",
            composer_skill_hash="a" * 64,
        )
    cards = [event for event in await service.list_interpretation_events(session_id) if event.kind is InterpretationKind.VAGUE_TERM]
    assert [(card.affected_node_id, card.user_term, card.choice) for card in cards] == [(_NODE, _TERM, InterpretationChoice.PENDING)]

    resolved, new_state = await service.resolve_interpretation_event(
        session_id=session_id,
        event_id=cards[0].id,
        choice=InterpretationChoice.ACCEPTED_AS_DRAFTED,
        amended_value=None,
        actor="user:alice",
        runtime_model_identifier=None,
        runtime_model_version=None,
    )
    assert resolved.choice is InterpretationChoice.ACCEPTED_AS_DRAFTED
    approved = state_from_record(new_state)
    assert _term_rows(approved) == [(_TERM_ROW, "resolved")]

    wired = _wire(env, approved, _query_path(), blob)

    assert wired.success, wired.to_dict()
    assert _query_template(wired.updated_state) == _marker(blob)
    assert _term_rows(wired.updated_state) == [(_TERM_ROW, "resolved")]
    assert unwired_vague_term_error(wired.updated_state) is None
    assert _vague_sites(wired.updated_state) == []


@pytest.mark.asyncio
async def test_an_already_stranded_session_with_no_card_gives_the_user_nothing_to_approve(env: dict[str, Any], tmp_path: Path) -> None:
    """Term staged and file wired in one turn, before any card was raised.

    The user has no card for the term and none can be made: the surfacer skips
    the site and the review-request tool refuses it. What is left is the agent:
    each ``test_recovery_of_an_already_stranded_session_by_agent_...`` repair
    below works from exactly this state.
    """
    service = FencedSessionServiceHarness(env["engine"], telemetry=build_sessions_telemetry(), log=structlog.get_logger("test"))
    session_id = UUID(env["session_id"])
    stranded = _stranded_state(env, tmp_path)
    await _save_composition_state(service, session_id, _state_data(stranded), provenance="tool_call")

    assert await service.list_interpretation_events(session_id) == []
    unsurfaceable = unsurfaceable_pending_interpretation_review_sites(stranded)
    assert [(site.component_id, site.user_term) for site in unsurfaceable if site.kind is InterpretationKind.VAGUE_TERM] == [(_NODE, _TERM)]
    with pytest.raises(ToolArgumentError, match="pending vague_term requirement with no resolvable prompt wiring"):
        _assert_affected_component(stranded, _NODE, InterpretationKind.VAGUE_TERM, _TERM)


# --------------------------------------------------------------------------- #
# Recovery of a session stranded before the fix. Keep these: see the module docstring.
# --------------------------------------------------------------------------- #


def _session_arguments(env: dict[str, Any], tmp_path: Path) -> dict[str, Any]:
    """The admitted pipeline restated for the tool session (the source path is session-scoped)."""
    arguments = _arguments(tmp_path)
    arguments["source"]["options"]["path"] = str(tmp_path / "blobs" / env["session_id"] / "input.csv")
    return arguments


def test_recovery_of_an_already_stranded_session_by_agent_restating_queries_without_the_file(env: dict[str, Any], tmp_path: Path) -> None:
    """Agent-reachable (patch_node_options). It undoes the file binding.

    ``patch_node_options`` replaces a top-level option whole, so the patch has
    to restate every query: one naming only ``first`` leaves ``second`` out
    and is refused (``queries_patch_omits_existing``), which is why no refusal
    text suggests it.
    """
    stranded = _stranded_state(env, tmp_path)

    partial = _call(env, "patch_node_options", {"node_id": _NODE, "patch": {"queries": {"first": {"template": None}}}}, stranded)
    assert partial.success is False
    assert partial.updated_state is stranded
    assert _codes(partial) == ["queries_patch_omits_existing"]
    assert "leaves out 'second'" in partial.validation.errors[0].message

    result = _call(env, "patch_node_options", {"node_id": _NODE, "patch": {"queries": _INHERITING_QUERIES}}, stranded)

    assert result.success is True, result.to_dict()
    repaired = result.updated_state
    assert _query_template(repaired) is None
    assert sorted(_options(repaired)["queries"]) == ["first", "second"]
    assert _term_rows(repaired) == [(_TERM_ROW, "pending")]
    assert vague_term_wiring_count(_options(repaired), user_term=_TERM) == 1
    assert unwired_vague_term_error(repaired) is None
    edit = _call(env, "patch_node_options", _OTHER_NODE_PATCH, repaired)
    assert edit.success is True, edit.to_dict()


def test_recovery_of_an_already_stranded_session_by_agent_removing_the_term_and_its_slot(env: dict[str, Any], tmp_path: Path) -> None:
    """Agent-reachable (patch_node_options). It keeps the file and drops the term for the queries that still inherit."""
    stranded = _stranded_state(env, tmp_path)
    text = next(part["text"] for part in _options(stranded)[PROMPT_TEMPLATE_PARTS_KEY] if part["kind"] == "text")

    result = _call(
        env,
        "patch_node_options",
        {
            "node_id": _NODE,
            "patch": {
                INTERPRETATION_REQUIREMENTS_KEY: [],
                PROMPT_TEMPLATE_PARTS_KEY: [{"kind": "text", "text": text}],
            },
        },
        stranded,
    )

    assert result.success is True, result.to_dict()
    repaired = result.updated_state
    assert _term_rows(repaired) == []
    assert _query_template(repaired) is not None
    assert unwired_vague_term_error(repaired) is None
    assert _vague_sites(repaired) == []


def test_recovery_of_an_already_stranded_session_by_agent_binding_the_file_to_the_node_prompt(env: dict[str, Any], tmp_path: Path) -> None:
    """Agent-reachable (wire_blob_inline_ref on prompt_template): the replacement retires the term and names it."""
    stranded = _stranded_state(env, tmp_path)
    blob = _prompt_file(env)

    result = _wire(env, stranded, f"node:{_NODE}.options.prompt_template", blob)

    assert result.success is True, result.to_dict()
    repaired = result.updated_state
    options = _options(repaired)
    assert options["prompt_template"] == _marker(blob)
    assert PROMPT_TEMPLATE_PARTS_KEY not in options
    assert _term_rows(repaired) == []
    assert list(result.data["superseded_review_ids"]) == [_TERM_ROW]
    assert unwired_vague_term_error(repaired) is None
    assert _vague_sites(repaired) == []


def test_recovery_of_an_already_stranded_session_by_agent_replacing_the_pipeline(env: dict[str, Any], tmp_path: Path) -> None:
    """Agent-reachable (set_pipeline). A full replacement without the query file is accepted; it undoes the binding."""
    stranded = _stranded_state(env, tmp_path)

    result = _call(env, "set_pipeline", _session_arguments(env, tmp_path), stranded)

    assert result.success is True, result.to_dict()
    repaired = result.updated_state
    assert _query_template(repaired) is None
    assert _term_rows(repaired) == [(_TERM_ROW, "pending")]
    assert vague_term_wiring_count(_options(repaired), user_term=_TERM) == 1
    assert unwired_vague_term_error(repaired) is None


@pytest.mark.asyncio
async def test_recovery_of_an_already_stranded_session_by_user_approving_a_card_raised_before_the_file(
    env: dict[str, Any], tmp_path: Path
) -> None:
    """User-only (the review card in the UI): a card that predates the query file is still pending and still approvable."""
    service = FencedSessionServiceHarness(env["engine"], telemetry=build_sessions_telemetry(), log=structlog.get_logger("test"))
    session_id = UUID(env["session_id"])
    healthy = _admitted_state(tmp_path)
    saved: CompositionStateRecord = await _save_composition_state(service, session_id, _state_data(healthy), provenance="tool_call")
    async with _compose_operation(service, session_id) as context:
        event = await service.create_pending_interpretation_event(
            session_id=session_id,
            composition_state_id=saved.id,
            affected_node_id=_NODE,
            tool_call_id="call_tone",
            user_term=_TERM,
            kind=InterpretationKind.VAGUE_TERM,
            llm_draft=_DRAFT,
            model_identifier="test-model",
            model_version="test-model-v1",
            provider="test-provider",
            composer_skill_hash="a" * 64,
            session_operation_context=context,
        )
    assert event.choice is InterpretationChoice.PENDING

    # The stranded state becomes the next head, as the unfixed wire left it.
    stranded = _hand_stranded(state_from_record(saved), _prompt_file(env))
    await _save_composition_state(service, session_id, _state_data(stranded), provenance="tool_call")

    # The card that predates the file is not retired by the new head: it is still pending.
    events = await service.list_interpretation_events(session_id)
    assert [item.choice for item in events if item.id == event.id] == [InterpretationChoice.PENDING]

    # The user accepts it. The resolver never looks at the queries, so it lands.
    resolved, new_state = await service.resolve_interpretation_event(
        session_id=session_id,
        event_id=event.id,
        choice=InterpretationChoice.ACCEPTED_AS_DRAFTED,
        amended_value=None,
        actor="user:alice",
        runtime_model_identifier=None,
        runtime_model_version=None,
    )
    assert resolved.choice is InterpretationChoice.ACCEPTED_AS_DRAFTED
    repaired = state_from_record(new_state)
    assert _term_rows(repaired) == [(_TERM_ROW, "resolved")]
    assert _options(repaired)["prompt_template"].endswith(_DRAFT)
    assert _query_template(repaired) is not None
    assert unwired_vague_term_error(repaired) is None
    assert _vague_sites(repaired) == []
