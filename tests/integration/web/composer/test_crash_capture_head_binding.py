"""Crash captures may re-project only the durable checkpoint, never replace it.

A recovery persist (``convergence_persist``, ``plugin_crash_persist``,
``preflight_persist``) appends a ``composition_states`` row, and the session
head is the highest version. Each case drives the actual public route and the
real compose loop. Only ``litellm.acompletion`` is scripted, plus one named
fault per case:

- ``timeout``: the second provider call times out after P4 committed request
  2's ``tool_call`` checkpoint.
- ``plugin_crash``: the second tool in one batch crashes
  (``wire_required_controls``, as in test_freeform_required_controls); P4
  still commits the first tool's checkpoint.
- ``plugin_crash_unwind``: the same crash, but P4's transaction raises
  ``OperationalError`` (the session service's documented unwind arm), so
  nothing from the batch commits.
- ``preflight_crash``: ``preview_pipeline``'s runtime preflight raises after
  ``set_pipeline`` mutated the in-memory graph. That escapes before P4 starts.

Request 1 leaves an END-verified ``post_compose`` head. A crash capture is
saved only when its graph equals the durable head, which is the checkpoint
P4 committed and TurnEvidence verified. Otherwise no state row is written,
and the head stays on the verified checkpoint. The carried tool invocations
remain as the crash's audit evidence.
"""

from __future__ import annotations

import json

import httpx
import litellm
import pytest
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from elspeth.web.composer import tool_batch
from elspeth.web.composer.service import ComposerAvailability, ComposerServiceImpl
from elspeth.web.sessions.converters import state_from_record
from elspeth.web.sessions.models import chat_messages_table, composition_states_table
from tests.helpers.composer_common_sdk import CommonSDK, Function, Step, ToolCall
from tests.helpers.composer_operations import message_body, submit_and_settle
from tests.integration.web.composer.test_freeform_pipeline_planner import _routing_terminal_response
from tests.unit.web.sessions.test_routes import _install_required_route_prompt_shields, _make_app
from tests.unit.web.sessions.test_run_composer_turn import _TestSnapshotFactory

_FIRST_REQUEST = "Build the first pipeline."
_SECOND_REQUEST = "Change the routing threshold."
_SAVED_CONDITION = "row['amount'] > 500"


class _SecondRequestSDK(CommonSDK):
    """Request-2 replies with a distinct tool-call id space and optional batch siblings.

    The session refuses a provider tool-call id it already persisted, and
    CommonSDK numbers calls by ordinal, so request 2 prefixes its ids.
    """

    def __init__(self, *, siblings, **kwargs):
        super().__init__(**kwargs)
        self.siblings = siblings

    async def __call__(self, **kwargs):
        ordinal = len(self.requests)
        response = await super().__call__(**kwargs)
        for choice in response.choices:
            for name, arguments in self.siblings.get(ordinal, ()):
                choice.message.tool_calls.append(
                    ToolCall(f"sibling-{len(choice.message.tool_calls)}", Function(name, json.dumps(arguments)))
                )
            for call in choice.message.tool_calls:
                call.id = f"second-{call.id}"
        return response


def _states(engine, session_id):
    with engine.connect() as connection:
        return connection.execute(
            select(composition_states_table)
            .where(composition_states_table.c.session_id == session_id)
            .order_by(composition_states_table.c.version)
        ).all()


def _audit_mentions(engine, session_id, needle):
    with engine.connect() as connection:
        rows = connection.execute(select(chat_messages_table).where(chat_messages_table.c.session_id == session_id)).all()
    return [row for row in rows if needle in json.dumps([row.content, row.raw_content, row.tool_calls], default=str)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("case", "provenance", "error_type"),
    [
        ("timeout", "convergence_persist", "convergence"),
        ("plugin_crash", "plugin_crash_persist", "composer_plugin_error"),
        ("plugin_crash_unwind", "plugin_crash_persist", "composer_plugin_error"),
        ("preflight_crash", "preflight_persist", "composer_plugin_error"),
    ],
)
async def test_crash_capture_never_replaces_the_verified_head(tmp_path, monkeypatch, case, provenance, error_type):
    app, sessions = _make_app(tmp_path, quota_enabled=True)
    engine = app.state.session_engine
    _install_required_route_prompt_shields(app, monkeypatch)
    app.state.settings = app.state.settings.model_copy(update={"composer_model": "test/planner", "composer_boot_probe_enabled": False})
    monkeypatch.setattr(
        "elspeth.web.composer.service.compute_availability",
        lambda **kwargs: ComposerAvailability(available=True, provider="test", model=kwargs["model"]),
    )
    composer = ComposerServiceImpl.for_trained_operator(
        catalog=app.state.catalog_service, settings=app.state.settings, sessions_service=sessions, session_engine=engine
    )
    app.state.composer_service = composer
    app.state.interpretation_surfacing = composer._interpretation_surfacing
    app.state.plugin_snapshot_factory = _TestSnapshotFactory(app.state.plugin_snapshot_factory)
    session = await sessions.create_session("alice", "Crash capture head binding", "local")
    await sessions.update_composer_preferences(session.id, trust_mode="auto_commit", density_default="high", actor="test")
    session_id = str(session.id)
    (tmp_path / "blobs" / session_id).mkdir(parents=True, exist_ok=True)
    (tmp_path / "blobs" / session_id / "input.csv").write_text("amount,note\n600,synthetic\n100,ordinary\n", encoding="utf-8")
    (tmp_path / "outputs" / session_id).mkdir(parents=True, exist_ok=True)

    def pipeline(condition):
        call = _routing_terminal_response(tmp_path, session_id, condition=condition).choices[0].message.tool_calls[0]
        return json.loads(call.function.arguments)["pipeline"]

    first, second, third = pipeline("True"), pipeline(_SAVED_CONDITION), pipeline("row['amount'] > 700")

    real_wire = tool_batch.wire_required_controls
    wire_calls: list[None] = []
    crash_at: list[int] = []

    def wire_required_controls(*args, **kwargs):
        wire_calls.append(None)
        if crash_at and len(wire_calls) == crash_at[0]:
            raise RuntimeError("synthetic plugin crash")
        return real_wire(*args, **kwargs)

    monkeypatch.setattr(tool_batch, "wire_required_controls", wire_required_controls)
    real_reserve = sessions._reserve_sequence_range
    real_persist_turn = sessions.persist_compose_turn_async
    unwind_armed: list[bool] = []
    unwound: list[bool] = []

    def reserve_sequence_range(*args, **kwargs):
        if unwind_armed:
            raise OperationalError("INSERT", {}, Exception("synthetic commit-time failure"))
        return real_reserve(*args, **kwargs)

    async def persist_compose_turn_async(**kwargs):
        if case == "plugin_crash_unwind" and kwargs["plugin_crash_pending"]:
            unwind_armed.append(True)
        try:
            outcome = await real_persist_turn(**kwargs)
        finally:
            unwind_armed.clear()
        unwound.append(outcome.unwind_audit_failed)
        return outcome

    monkeypatch.setattr(sessions, "_reserve_sequence_range", reserve_sequence_range)
    monkeypatch.setattr(sessions, "persist_compose_turn_async", persist_compose_turn_async)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            script = CommonSDK(
                request=_FIRST_REQUEST,
                candidate="The draft was saved.",
                steps=(
                    Step("primary", tool_name="set_pipeline", arguments={"pipeline": first}),
                    Step("early"),
                    Step("primary", content="The draft was saved."),
                    Step("end"),
                ),
                saved=1,
            )
            script.bind(composer, monkeypatch)
            monkeypatch.setattr(litellm, "acompletion", script)
            settled = await submit_and_settle(client, app, path=f"/api/sessions/{session_id}/messages", body=message_body(_FIRST_REQUEST))
            assert settled.final.json()["status"] == "completed", settled.final.text
            script.assert_done()
            verified = await sessions.get_current_state(session.id)
            assert verified is not None
            assert [(row.provenance, row.id) for row in _states(engine, session_id)][-1] == ("post_compose", str(verified.id))

            siblings: dict[int, list[tuple[str, dict]]] = {}
            steps = [Step("primary", tool_name="set_pipeline", arguments={"pipeline": second})]
            if case == "timeout":
                steps.append(
                    Step("primary", failure=litellm.Timeout(message="synthetic timeout", model="test/planner", llm_provider="test"))
                )
            elif case == "preflight_crash":

                def crashing_preflight(*args, **kwargs):
                    raise RuntimeError("synthetic preflight crash")

                monkeypatch.setattr(composer._preflight, "runtime_preflight", crashing_preflight)
                siblings[0] = [("preview_pipeline", {})]
            else:
                crash_at.append(len(wire_calls) + 2)
                siblings[0] = [("set_pipeline", {"pipeline": third})]
            second_script = _SecondRequestSDK(siblings=siblings, request=_SECOND_REQUEST, candidate="unused", steps=tuple(steps))
            second_script.bind(composer, monkeypatch)
            monkeypatch.setattr(litellm, "acompletion", second_script)
            failed = await submit_and_settle(
                client, app, path=f"/api/sessions/{session_id}/messages", body=message_body(_SECOND_REQUEST, state_id=verified.id)
            )
            second_script.assert_done()
        final = failed.final.json()
        assert final["status"] == "failed", failed.final.text
        assert final["error"]["error_type"] == error_type, failed.final.text
        detail = final["error"]["body"]["detail"]
        assert unwound[-1] is (case == "plugin_crash_unwind")

        states = _states(engine, session_id)
        head = await sessions.get_current_state(session.id)
        assert head is not None and str(head.id) == states[-1].id
        if case in ("timeout", "plugin_crash"):
            # P4 committed and verified request 2's checkpoint. The capture is
            # its strict-lane re-projection: the identical graph, bound to it.
            assert [row.provenance for row in states] == ["tool_call", "post_compose", "tool_call", provenance]
            checkpoint, capture = states[-2], states[-1]
            assert capture.id == str(head.id)
            assert (capture.sources, capture.nodes, capture.edges, capture.outputs, capture.metadata_) == (
                checkpoint.sources,
                checkpoint.nodes,
                checkpoint.edges,
                checkpoint.outputs,
                checkpoint.metadata_,
            )
            assert detail["partial_state"]["id"] == str(head.id)
        else:
            # Nothing from the crashed batch committed. The capture is not
            # saved; the END-verified head of request 1 stays current.
            assert [row.provenance for row in states] == ["tool_call", "post_compose"]
            assert head.id == verified.id
            assert "partial_state" not in detail
            # The crash evidence survives as the drained tool invocations,
            # whose private authoring evidence retains the attempted graph.
            assert _audit_mentions(engine, session_id, "> 500"), "the uncommitted graph left no audit evidence"
        assert state_from_record(head).nodes[0].condition in ("True", _SAVED_CONDITION)
    finally:
        await app.state.composer_async_worker.stop()
        engine.dispose()
