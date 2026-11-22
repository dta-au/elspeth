"""A capped file listing says it is capped.

``list_blobs`` and ``list_composer_blobs`` show at most the 50 newest files.
A session holds more than that as soon as prompts live in files and every
revision is a new file. When the cap hides files the model-visible result must
say how many, and that an unlisted file still works by its id; and the planner
must not treat such a listing as the whole inventory. These tests drive the
real dispatcher over real session rows.

The planner keys on a marker, the fixed lead-in ``Incomplete list: ``, and
never on the sentence after it.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import event

import elspeth.web.composer.pipeline_planner as planner
from elspeth.contracts.freeze import deep_thaw
from elspeth.web.composer import tool_batch
from elspeth.web.composer.discovery_response import admit_discovery_result, serialize_admitted_discovery_result
from elspeth.web.composer.redaction import redact_tool_call_response
from elspeth.web.composer.redaction_telemetry import NoopRedactionTelemetry
from elspeth.web.composer.tools import _CACHEABLE_DISCOVERY_TOOL_NAMES, blobs
from elspeth.web.composer.tools._common import ToolResult
from elspeth.web.sessions.models import blobs_table, sessions_table
from tests.unit.web.composer.test_tools import (
    _STUB_SHA256,
    _empty_state,
    _mock_catalog,
    _session_engine_with_session,
    execute_tool,
)

_ID_KEY = {"list_blobs": "id", "list_composer_blobs": "blob_id"}
_KIND = {"list_blobs": "files", "list_composer_blobs": "ready files"}
_INFORMATION_KEY = {"list_blobs": "blob.index.session", "list_composer_blobs": "blob.index.composer"}
_TOOLS = tuple(_ID_KEY)
_EPOCH = datetime(2026, 11, 1, tzinfo=UTC)


def _add_session(engine: Any) -> str:
    session_id = str(uuid4())
    with engine.begin() as conn:
        conn.execute(
            sessions_table.insert().values(
                id=session_id,
                user_id="test-user",
                auth_provider_type="local",
                title="Other Session",
                created_at=_EPOCH,
                updated_at=_EPOCH,
            )
        )
    return session_id


def _seed(
    engine: Any,
    session_id: str,
    count: int,
    *,
    status: str = "ready",
    first_second: int = 0,
    same_timestamp: bool = False,
    ids: list[str] | None = None,
) -> list[str]:
    """Insert ``count`` rows, each one second newer than the last; return ids oldest first."""
    inserted = [str(uuid4()) for _ in range(count)] if ids is None else ids
    assert len(inserted) == count
    with engine.begin() as conn:
        for index, blob_id in enumerate(inserted):
            conn.execute(
                blobs_table.insert().values(
                    id=blob_id,
                    session_id=session_id,
                    filename=f"prompt-{first_second + index:04d}.txt",
                    mime_type="text/plain",
                    size_bytes=0 if status == "pending" else 12,
                    content_hash=_STUB_SHA256 if status == "ready" else None,
                    storage_path=f"/unused/{blob_id}",
                    created_at=_EPOCH + timedelta(seconds=first_second + (0 if same_timestamp else index)),
                    created_by="pipeline" if status == "pending" else "assistant",
                    source_description=None,
                    status=status,
                )
            )
    return inserted


def _call(tool: str, engine: Any, session_id: str) -> ToolResult:
    result = execute_tool(tool, {}, _empty_state(), _mock_catalog(), session_engine=engine, session_id=session_id)
    assert result.success is True
    return result


def _listed_ids(tool: str, result: ToolResult) -> list[str]:
    """Read the ids out of each tool's existing, unchanged ``data`` shape."""
    data = deep_thaw(result.data)
    if tool == "list_blobs":
        assert type(data) is list
        entries = data
    else:
        assert type(data) is dict and set(data) == {"blobs"}
        entries = data["blobs"]
    return [entry[_ID_KEY[tool]] for entry in entries]


def _freeform_visible(tool: str, result: ToolResult) -> dict[str, Any]:
    """The JSON the compose loop sends the model as this tool's result."""
    payload = json.loads(serialize_admitted_discovery_result(admit_discovery_result(tool, result)))
    assert type(payload) is dict
    return payload


def _planner_visible(tool: str, result: ToolResult) -> dict[str, Any]:
    """The JSON the planner sends the model as this tool's result."""
    payload = json.loads(
        planner._serialize_provider_discovery_result(call=_planner_call(tool), result=admit_discovery_result(tool, result))
    )
    assert type(payload) is dict
    return payload


_VISIBLE = {"freeform": _freeform_visible, "planner": _planner_visible}


def _hints(visible: dict[str, Any]) -> list[str]:
    hints = visible.get("post_call_hints", [])
    assert type(hints) is list
    return hints


def _planner_call(tool: str) -> Any:
    return planner._ParsedToolCall(call_id="c", name=tool, arguments={}, raw_arguments="{}")


def _closed_keys(tool: str, result: ToolResult) -> tuple[str, ...]:
    """The inventory keys the planner marks supplied for this listing result."""
    return planner._resolved_discovery_information_keys(_planner_call(tool), admit_discovery_result(tool, result))


@pytest.mark.parametrize("path", sorted(_VISIBLE))
@pytest.mark.parametrize("tool", _TOOLS)
@pytest.mark.parametrize(("count", "omitted"), [(51, 1), (120, 70)])
def test_capped_listing_tells_the_model_how_many_files_it_left_out(tool: str, count: int, omitted: int, path: str) -> None:
    engine, session_id = _session_engine_with_session()
    seeded = _seed(engine, session_id, count)

    result = _call(tool, engine, session_id)
    hints = _hints(_VISIBLE[path](tool, result))

    assert _listed_ids(tool, result) == list(reversed(seeded))[:50]
    assert hints, f"{tool} listed 50 of {count} files and told the model nothing about the other {omitted} ({path} path)"
    assert len(hints) == 1
    kind = _KIND[tool]
    assert hints[0].startswith(
        f"Incomplete list: the 50 newest of {count} {kind} in this session are shown; {omitted} older {kind} are not listed. "
    )
    assert "A file that is not listed still exists and still works by its id." in hints[0]
    assert "No tool finds a file by name." in hints[0]


@pytest.mark.parametrize("path", sorted(_VISIBLE))
@pytest.mark.parametrize("tool", _TOOLS)
@pytest.mark.parametrize("count", [0, 1, 49, 50])
def test_complete_listing_carries_no_hint(tool: str, count: int, path: str) -> None:
    engine, session_id = _session_engine_with_session()
    seeded = _seed(engine, session_id, count)

    result = _call(tool, engine, session_id)

    assert _listed_ids(tool, result) == list(reversed(seeded))
    assert _hints(_VISIBLE[path](tool, result)) == []


@pytest.mark.parametrize("tool", _TOOLS)
def test_hint_is_written_from_the_count_alone(tool: str) -> None:
    """No file name, id or other stored text reaches the hint: it is a function of the count and the tool."""
    engine, session_id = _session_engine_with_session()
    seeded = _seed(engine, session_id, 51)

    hints = _hints(_freeform_visible(tool, _call(tool, engine, session_id)))

    assert hints == [blobs._blob_list_incomplete_hint(total=51, ready_only=tool == "list_composer_blobs")]
    assert "prompt-" not in hints[0]
    assert not [blob_id for blob_id in seeded if blob_id in hints[0]]


@pytest.mark.parametrize("tool", _TOOLS)
def test_page_and_count_come_from_one_statement(tool: str) -> None:
    """The number in the hint is counted by the statement that read the page, so the two cannot disagree."""
    engine, session_id = _session_engine_with_session()
    _seed(engine, session_id, 60)
    statements: list[str] = []

    def record(conn: Any, cursor: Any, statement: str, parameters: Any, context: Any, executemany: bool) -> None:
        if "blobs" in statement.lower():
            statements.append(statement.lower())

    event.listen(engine, "after_cursor_execute", record)
    try:
        result = _call(tool, engine, session_id)
    finally:
        event.remove(engine, "after_cursor_execute", record)

    assert len(statements) == 1, statements
    assert "count(" in statements[0] and "limit" in statements[0]
    assert len(_hints(_freeform_visible(tool, result))) == 1


@pytest.mark.parametrize("tool", _TOOLS)
def test_another_sessions_files_do_not_make_a_complete_listing_look_capped(tool: str) -> None:
    engine, session_id = _session_engine_with_session()
    _seed(engine, session_id, 50)
    _seed(engine, _add_session(engine), 30, first_second=1000)

    assert _hints(_freeform_visible(tool, _call(tool, engine, session_id))) == []


def test_ready_listing_counts_ready_files_only() -> None:
    """Pending files are not listed by ``list_composer_blobs`` and must not be counted as hidden."""
    engine, session_id = _session_engine_with_session()
    _seed(engine, session_id, 50)
    _seed(engine, session_id, 30, status="pending", first_second=1000)

    ready = _call("list_composer_blobs", engine, session_id)
    everything = _hints(_freeform_visible("list_blobs", _call("list_blobs", engine, session_id)))

    assert len(_listed_ids("list_composer_blobs", ready)) == 50
    assert _hints(_freeform_visible("list_composer_blobs", ready)) == []
    assert len(everything) == 1
    assert everything[0].startswith("Incomplete list: the 50 newest of 80 files in this session are shown; 30 older files are not listed. ")


def test_ready_listing_over_the_cap_reports_ready_files_hidden() -> None:
    engine, session_id = _session_engine_with_session()
    _seed(engine, session_id, 60)
    _seed(engine, session_id, 30, status="pending", first_second=1000)

    hints = _hints(_freeform_visible("list_composer_blobs", _call("list_composer_blobs", engine, session_id)))

    assert len(hints) == 1
    assert hints[0].startswith(
        "Incomplete list: the 50 newest of 60 ready files in this session are shown; 10 older ready files are not listed. "
    )


@pytest.mark.parametrize("tool", _TOOLS)
def test_files_created_in_the_same_instant_are_capped_in_one_fixed_order(tool: str) -> None:
    """Sixty files share one ``created_at``: the cap falls inside the tie.

    Ordering by timestamp alone leaves the order of tied rows to the database,
    so which ten files are hidden would not be fixed. The ids are inserted in
    descending order, the reverse of the tie-break, so storage order cannot
    produce the expected answer by accident.
    """
    engine, session_id = _session_engine_with_session()
    tied = sorted((str(UUID(int=number)) for number in range(1, 61)), reverse=True)
    _seed(engine, session_id, 60, same_timestamp=True, ids=tied)
    newer = _seed(engine, session_id, 1, first_second=10)

    listed = _listed_ids(tool, _call(tool, engine, session_id))

    assert listed == [*newer, *sorted(tied)[:49]]


@pytest.mark.parametrize("tool", _TOOLS)
def test_both_tools_stay_zero_argument_and_teach_the_cap(tool: str) -> None:
    from elspeth.web.composer.tools._dispatch import get_tool_definitions

    definition = next(item for item in get_tool_definitions() if item["name"] == tool)

    assert definition["parameters"]["properties"] == {}
    description = definition["description"]
    assert "at most the 50 newest" in description
    assert "a `post_call_hints` entry starting `Incomplete list:` says how many are not listed" in description
    assert "An unlisted file still works by its id; no tool finds a file by name." in description


@pytest.mark.parametrize("tool", _TOOLS)
def test_neither_listing_is_served_from_the_discovery_cache(tool: str) -> None:
    """The discovery cache rebuilds a result from its data alone and drops ``post_call_hints``.

    A cached listing would therefore reach the model capped and unmarked. If a
    listing tool is ever made cacheable, the cache must carry the hint first.
    """
    assert tool not in _CACHEABLE_DISCOVERY_TOOL_NAMES


@pytest.mark.parametrize("tool", _TOOLS)
@pytest.mark.parametrize(("count", "closes"), [(50, True), (51, False), (120, False)])
def test_planner_closes_the_inventory_gap_only_on_a_complete_listing(tool: str, count: int, closes: bool) -> None:
    """A capped listing must leave the planner's file-inventory key pending.

    Closing it makes the planner announce that every information gap is closed
    while files it never saw still exist.
    """
    engine, session_id = _session_engine_with_session()
    _seed(engine, session_id, count)

    assert _closed_keys(tool, _call(tool, engine, session_id)) == ((_INFORMATION_KEY[tool],) if closes else ())


@pytest.mark.parametrize("tool", _TOOLS)
def test_planner_keys_on_the_marker_and_not_on_the_sentence(tool: str) -> None:
    """The gap decision follows the fixed lead-in alone.

    Any wording after the marker keeps the gap open; a result without the
    marker closes it, whether it has no hint at all or a different hint.
    """
    engine, session_id = _session_engine_with_session()
    _seed(engine, session_id, 51)
    capped = _call(tool, engine, session_id)
    key = (_INFORMATION_KEY[tool],)

    assert _closed_keys(tool, capped) == ()
    assert _closed_keys(tool, replace(capped, post_call_hints=(f"{blobs._BLOB_LIST_INCOMPLETE_PREFIX}reworded entirely.",))) == ()
    assert _closed_keys(tool, replace(capped, post_call_hints=("Unrelated note.", f"{blobs._BLOB_LIST_INCOMPLETE_PREFIX}x"))) == ()
    assert _closed_keys(tool, replace(capped, post_call_hints=())) == key
    assert (
        _closed_keys(tool, replace(capped, post_call_hints=("The pipeline is unchanged; no proposal was submitted for approval.",))) == key
    )
    assert _closed_keys(tool, replace(capped, post_call_hints=("This is an incomplete list: 1 older file is not listed.",))) == key


@pytest.mark.parametrize("tool", _TOOLS)
def test_capped_listing_persists_the_same_redacted_audit_row_as_before(tool: str) -> None:
    """The hint adds no new persisted surface: the redacted row is the one a hint-free result produces."""
    engine, session_id = _session_engine_with_session()
    _seed(engine, session_id, 51)
    visible = _freeform_visible(tool, _call(tool, engine, session_id))
    without_hint = {key: value for key, value in visible.items() if key != "post_call_hints"}

    redacted = redact_tool_call_response(tool, visible, telemetry=NoopRedactionTelemetry())

    assert len(_hints(visible)) == 1
    assert redacted == redact_tool_call_response(tool, without_hint, telemetry=NoopRedactionTelemetry())
    assert "Incomplete list" not in json.dumps(redacted)


def test_skill_wordings_that_identify_a_hint_match_what_the_producers_emit() -> None:
    """The skill says two ``post_call_hints`` entries are recognised by their wording.

    Each wording is pinned here to the string its producer really emits, so an
    edit to either producer string turns the skill's claim red. Both producer
    strings are obtained by running the producer, not by copying the literal.

    Negative control, checked by hand: changing ``_BLOB_LIST_INCOMPLETE_PREFIX``
    in ``tools/blobs.py``, or the no-change sentence in
    ``tool_batch._unapplied_admission``, makes this test fail.
    """
    skill = " ".join((Path(tool_batch.__file__).parent / "skills" / "pipeline_composer.md").read_text(encoding="utf-8").split())
    engine, session_id = _session_engine_with_session()
    state = _empty_state()
    base = _call("list_blobs", engine, session_id)

    incomplete = blobs._blob_list_incomplete_hint(total=51, ready_only=False)
    no_change = tool_batch._unapplied_admission(base, "no_change", state=state, validation=state.validate(), runtime_preflight=None)

    assert skill.count("`Incomplete list:`") == 2
    assert blobs._BLOB_LIST_INCOMPLETE_PREFIX.rstrip() == "Incomplete list:"
    assert incomplete.startswith(blobs._BLOB_LIST_INCOMPLETE_PREFIX)
    assert "one that says the pipeline is unchanged means the call was a no-op" in skill
    assert len(no_change.result.post_call_hints) == 1
    assert "pipeline is unchanged" in no_change.result.post_call_hints[0].lower()


async def _run_planner_listing(
    tool: str, files: int, tmp_path: Path, tool_context: Any, *, budget: int, recorder: Any = None
) -> tuple[Any, str | None]:
    """Drive the real planner loop: it lists files on every turn, with the inventory key declared pending.

    Returns the scripted provider, whose ``requests`` are what the model was
    sent, and the planner error code that ended the loop.
    """
    from unittest.mock import patch

    from elspeth.web.coordination.sqlite_authority import SQLiteLocalSessionOperationAuthority
    from tests.helpers.session_fences import fenced_operation_context
    from tests.unit.web.composer.test_pipeline_planner import (
        PLANNER_DISCOVERY_TOOL_NAMES,
        _custody,
        _plan,
        _response,
        _ScriptedCompletion,
        _session_context,
    )

    engine, origin = await _session_context()
    try:
        _seed(engine, origin.session_id, files)
        with fenced_operation_context(engine, origin.session_id) as context:
            completion = _ScriptedCompletion(*(_response((tool, {})) for _ in range(budget + 2)))
            custody = replace(
                _custody(tmp_path),
                session_engine=engine,
                session_operation_context=context,
                session_operation_authority=SQLiteLocalSessionOperationAuthority(engine),
            )
            policy = planner.PlannerDiscoveryPolicy(
                manifest=planner.PlannerInformationManifest(supplied=frozenset(), unresolved=frozenset({_INFORMATION_KEY[tool]})),
                discovery_tool_names=PLANNER_DISCOVERY_TOOL_NAMES,
                unresolved_classes=(),
            )
            code: str | None = None
            with patch.object(planner.PlannerDiscoveryPolicy, "initial", return_value=policy):
                try:
                    await _plan(
                        tmp_path=tmp_path,
                        tool_context=tool_context,
                        completion=completion,
                        originating_message=origin,
                        custody_config=custody,
                        information_aware=True,
                        model_overrides={"max_discovery_turns": budget},
                        recorder=recorder,
                    )
                except planner.PipelinePlannerError as error:
                    code = error.code
        return completion, code
    finally:
        engine.dispose()


def _tool_results(request: dict[str, Any]) -> list[dict[str, Any]]:
    return [json.loads(message["content"]) for message in request["messages"] if message["role"] == "tool"]


def _notices(request: dict[str, Any]) -> int:
    return sum(message.get("content") == planner._ALL_INFORMATION_GAPS_CLOSED_NOTICE for message in request["messages"])


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", _TOOLS)
async def test_planner_loop_never_announces_gaps_closed_after_a_capped_listing(tool: str, tmp_path: Path, tool_context: Any) -> None:
    """Through the real planner loop: the model sees the hint, and the gaps-closed notice does not follow it.

    The open key means the planner may list again. That repeat is bounded by
    ``max_discovery_turns`` (setting ``composer_max_discovery_turns``): the loop
    ends with ``DISCOVERY_EXHAUSTED`` after exactly that many listing turns.
    """
    budget = 3

    completion, code = await _run_planner_listing(tool, 51, tmp_path, tool_context, budget=budget)

    assert code == "DISCOVERY_EXHAUSTED"
    assert len(completion.requests) == budget + 1
    results = _tool_results(completion.requests[-1])
    assert len(results) == budget
    kind = _KIND[tool]
    for result in results:
        assert result["success"] is True
        hints = _hints(result)
        assert len(hints) == 1, f"{tool} result reached the planner model without the incomplete-list hint"
        assert hints[0].startswith(f"Incomplete list: the 50 newest of 51 {kind} in this session are shown; 1 older {kind} ")
    assert [_notices(request) for request in completion.requests] == [0] * (budget + 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", _TOOLS)
async def test_planner_loop_announces_gaps_closed_after_a_complete_listing(tool: str, tmp_path: Path, tool_context: Any) -> None:
    """The other direction through the same loop: 50 files is the whole inventory, so the notice follows."""
    completion, _ = await _run_planner_listing(tool, 50, tmp_path, tool_context, budget=1)

    results = _tool_results(completion.requests[1])
    assert len(results) == 1 and results[0]["success"] is True
    assert "post_call_hints" not in results[0]
    assert _notices(completion.requests[1]) == 1


async def _discovery_attempts(tool: str, files: int, tmp_path: Path, tool_context: Any, *, budget: int) -> list[Any]:
    from elspeth.contracts.composer_planner_audit import ComposerPlannerAttemptOutcome
    from elspeth.web.composer.audit import BufferingRecorder

    recorder = BufferingRecorder()
    await _run_planner_listing(tool, files, tmp_path, tool_context, budget=budget, recorder=recorder)
    return [attempt for attempt in recorder.planner_attempts if attempt.outcome is ComposerPlannerAttemptOutcome.DISCOVERY_EXECUTED]


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", _TOOLS)
async def test_capped_listing_records_no_new_information_on_the_attempt(tool: str, tmp_path: Path, tool_context: Any) -> None:
    """The attempt record says what was learned. A capped listing resolved nothing, first time or repeated."""
    budget = 3

    attempts = await _discovery_attempts(tool, 51, tmp_path, tool_context, budget=budget)

    assert len(attempts) == budget
    assert all(len(attempt.requested_information) == 1 for attempt in attempts)
    assert [attempt.new_information for attempt in attempts] == [()] * budget


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", _TOOLS)
async def test_complete_listing_records_the_file_index_as_new_information(tool: str, tmp_path: Path, tool_context: Any) -> None:
    """The other direction: the whole inventory was read, so the attempt records it as learned."""
    attempts = await _discovery_attempts(tool, 50, tmp_path, tool_context, budget=1)

    assert len(attempts) == 1
    assert len(attempts[0].requested_information) == 1
    assert attempts[0].new_information == attempts[0].requested_information


def test_planner_custody_rule_names_the_marker_the_producer_emits() -> None:
    """The planner's source-custody rule quotes the marker; a renamed prefix must turn this red."""
    from elspeth.web.composer.planner_authoring_aids import _SOURCE_CUSTODY_RULES

    quoted = f"`{blobs._BLOB_LIST_INCOMPLETE_PREFIX.rstrip()}`"

    assert sum(rule.count(quoted) for rule in _SOURCE_CUSTODY_RULES) == 1
    assert sum(rule.count("Incomplete list") for rule in _SOURCE_CUSTODY_RULES) == 1
