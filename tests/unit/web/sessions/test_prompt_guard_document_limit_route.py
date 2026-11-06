"""PromptShield seam 7: one indivisible document unit at the Azure limit, through the real route.

Turn 1 sends a prompt sized so that, as turn-2 history, it becomes exactly one
guard document unit of ``PROMPT_GUARD_MAX_TEXT_CHARACTERS`` and then one more.
Turn 2 goes through the actual async ``/messages`` route, worker and
Continuation driver with Azure limits enforced (``required`` mode, mounted Azure
adapter; only its HTTP transport is replaced). Only ``litellm.acompletion``
stands in for the model.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

import httpx
import pytest
from sqlalchemy import select

from elspeth.contracts.freeze import deep_thaw
from elspeth.contracts.hashing import canonical_json
from elspeth.contracts.prompt_guard import PROMPT_GUARD_MAX_TEXT_CHARACTERS
from elspeth.web.async_workers import run_sync_in_worker
from elspeth.web.composer import continuation_semantic_advisor, prompt_guard
from elspeth.web.composer.continuation_codec import decode_continuation_snapshot
from elspeth.web.composer.continuation_policy import decode_continuation_policy
from elspeth.web.composer.continuation_progress import decode_continuation_frontier
from elspeth.web.composer.prompt_guard_clients import AzurePromptGuardClient
from elspeth.web.sessions.continuation import load_snapshot
from elspeth.web.sessions.models import (
    composer_continuation_checkpoints_table,
    composer_continuation_effects_table,
    composer_continuation_events_table,
    quota_provider_attempts_table,
    token_usage_ledger_table,
)
from tests.helpers import continuation_app as continuation_app_module
from tests.helpers.composer_operations import message_body
from tests.helpers.continuation_app import (
    ACTOR,
    ScriptedSDK,
    continuation_app,
    install_sdk,
    observe_original_driver_failures,
    semantic_primary_response,
    settle,
)

_LOCAL_DETAIL = "The authoring input is outside the supported prompt guard input envelope. The protected dispatch was not sent."
_MARKER = "SEAM7-LIMIT-UNIT:"
_SECOND_PROMPT = "Review the earlier request without changing anything."


def _history_unit(content: str) -> str:
    # The guard's rendering of a prior user message as an evidence document.
    return json.dumps({"content": content, "role": "user"}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _first_prompt(unit_length: int) -> str:
    overhead = len(_history_unit(_MARKER))
    # JSON escaping makes the *prior-history document* exact at the boundary
    # while the original live request stays small enough for mandatory Advisor
    # guard inspection on the seeding turn.
    escaped, remainder = divmod(unit_length - overhead, 2)
    content = _MARKER + "\\" * escaped + "u" * remainder
    assert len(_history_unit(content)) == unit_length
    assert len(content) < PROMPT_GUARD_MAX_TEXT_CHARACTERS
    return content


def _recovery_stop(request: dict[str, Any]) -> dict[str, Any]:
    retained = json.loads(request["messages"][-1]["content"])
    assert retained["end_candidate"]["kind"] == "recovery"
    frontier = decode_continuation_frontier(retained["frontier"])
    answer = semantic_primary_response(request)
    answer.update(
        decision="STOP_CONFIRMED",
        remaining_actionable_ids=[],
        prescribed_action=None,
        obligation_ids=[item.obligation_id for item in frontier.obligations],
        obligations=[
            {
                "obligation_id": item.obligation_id,
                "parent_id": item.parent_id,
                "description": item.description,
                "original_message_id": item.original_message_id,
                "start": item.start,
                "end": item.end,
                "status": "satisfied",
            }
            for item in frontier.obligations
        ],
        reason="The provider-authored handoff completes the original request.",
    )
    return answer


def _assert_documents_reproduce_complete_units(units: Sequence[str], documents: Sequence[str]) -> None:
    """Every document is the newline join of a contiguous run of COMPLETE units, in order."""
    remaining = list(units)
    for position, document in enumerate(documents):
        width = next((size for size in range(1, len(remaining) + 1) if "\n".join(remaining[:size]) == document), None)
        assert width is not None, f"document {position} ({len(document)} chars) does not reproduce complete units"
        del remaining[:width]
    assert remaining == [], f"{len(remaining)} units were not reproduced"


def _guard_checks(rows) -> list[dict[str, Any]]:
    return [
        deep_thaw(envelope["check"]) for row in rows for envelope in (row.tool_calls or ()) if envelope["_kind"] == "prompt_guard_audit"
    ]


def _recording_guard(monkeypatch: pytest.MonkeyPatch, guard_bodies: list[dict[str, Any]]) -> None:
    """Route the harness's Azure guard HTTP boundary through a recording clean transport."""

    def transport(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        guard_bodies.append(body)
        return httpx.Response(
            200,
            json={
                "userPromptAnalysis": {"attackDetected": False},
                "documentsAnalysis": [{"attackDetected": False} for _document in body["documents"]],
            },
        )

    def factory(configuration, *, http_transport):
        del http_transport
        return AzurePromptGuardClient(configuration, http_transport=httpx.MockTransport(transport))

    monkeypatch.setattr(continuation_app_module, "AzurePromptGuardClient", factory)


@pytest.mark.asyncio
async def test_indivisible_history_unit_at_limit_is_inspected_whole_and_one_more_is_refused(tmp_path, monkeypatch):
    pack_calls: list[tuple[tuple[str, ...], tuple[str, ...]]] = []
    guard_bodies: list[dict[str, Any]] = []
    sdk_calls: list[dict[str, Any]] = []
    guard_http_counts_at_sdk_entry: list[int] = []
    checks_at_sdk_entry: list[list[dict[str, Any]]] = []
    diagnostic_trace: list[dict[str, Any]] = []
    semantic_scalar_lengths: list[dict[str, int]] = []
    current_session: list[Any] = []
    service_ref: list[Any] = []
    handoff = (
        None,
        [
            {
                "id": "seam7-handoff",
                "type": "function",
                "function": {
                    "name": "handoff_to_user",
                    "arguments": canonical_json({"action": "park", "message": "Noted."}),
                },
            }
        ],
    )
    sdk = ScriptedSDK(
        [
            ("primary", handoff),
            ("semantic", _recovery_stop),
            ("primary", handoff),
            ("semantic", _recovery_stop),
            ("primary", handoff),
            ("semantic", _recovery_stop),
        ]
    )

    original_pack = prompt_guard._pack_documents
    original_semantic_parts = continuation_semantic_advisor.json_content_parts

    def observe_semantic_parts(serialized: str, **kwargs: Any) -> tuple[Any, ...]:
        parts = original_semantic_parts(serialized, **kwargs)
        document = json.loads(serialized)
        if type(document) is dict and "frontier" in document:
            lengths: dict[str, int] = {}

            def scalar_lengths(value: Any, path: str) -> None:
                if type(value) is str:
                    lengths[path] = len(value)
                elif type(value) is dict:
                    for key, child in value.items():
                        scalar_lengths(child, f"{path}.{key}")
                elif type(value) is list:
                    for index, child in enumerate(value):
                        scalar_lengths(child, f"{path}[{index}]")

            scalar_lengths(document, "retained")
            semantic_scalar_lengths.append(lengths)
        return parts

    # The diagnostic observes a known scalar path and excludes a known negative.
    observe_semantic_parts(canonical_json({"frontier": "probe"}), encoder=canonical_json)
    assert semantic_scalar_lengths == [{"retained.frontier": 5}]
    observe_semantic_parts(canonical_json({"other": "probe"}), encoder=canonical_json)
    assert semantic_scalar_lengths == [{"retained.frontier": 5}]
    semantic_scalar_lengths.clear()

    def observe_pack(units: Sequence[str]) -> tuple[str, ...]:
        packed: tuple[str, ...] = original_pack(units)
        pack_calls.append((tuple(units), packed))
        return packed

    def safe_unit_label(unit: str) -> tuple[str, int]:
        heading = unit.split("\n", 1)[0]
        if heading.startswith("Message ") and ", content part " in heading and heading.endswith(":"):
            return "message_part", len(heading) + 1
        return "other", 0

    assert safe_unit_label("Message 2, content part 10:\nprobe") == ("message_part", 28)
    assert safe_unit_label("probe") == ("other", 0)

    async def completion(**kwargs):
        checks_at_sdk_entry.append(_guard_checks(await service_ref[0].get_messages(current_session[0], limit=None)))
        guard_http_counts_at_sdk_entry.append(len(guard_bodies))
        sdk_calls.append(kwargs)
        return await sdk(**kwargs)

    # Only the guard's HTTP transport and the model SDK are replaced; the
    # packer and semantic JSON partitioner are observed through their originals.
    _recording_guard(monkeypatch, guard_bodies)
    monkeypatch.setattr(prompt_guard, "_pack_documents", observe_pack)
    monkeypatch.setattr(continuation_semantic_advisor, "json_content_parts", observe_semantic_parts)
    install_sdk(monkeypatch, completion)

    async with continuation_app(tmp_path, monkeypatch) as harness:
        app = harness.app
        service = app.state.session_service
        service_ref[:] = [service]

        async def submit(session_id, body):
            async with harness.client() as client:
                accepted = await client.post(f"/api/sessions/{session_id}/messages", json=body)
                assert accepted.status_code == 202, accepted.text
            routed = type(harness)(app=app, token=harness.token, session_id=session_id)
            return await settle(routed, operation_id=body["operation_id"])

        async def second_turn_after_history(unit_length):
            session = await service.create_session(user_id=ACTOR, title=f"Seam 7 unit of {unit_length}", auth_provider_type="local")
            current_session[:] = [session.id]
            first = _first_prompt(unit_length)
            assert len(first) <= PROMPT_GUARD_MAX_TEXT_CHARACTERS
            first_body = message_body(first)
            first_pack_start = len(pack_calls)
            with monkeypatch.context() as diagnostic_patch:
                observed_first = observe_original_driver_failures(diagnostic_patch, first_body["operation_id"], session_id=session.id)
                first_settled = await submit(session.id, first_body)
            diagnostic_trace.extend(observed_first)
            if first_settled.status != "completed":
                longest = sorted(
                    ((path, length) for record in semantic_scalar_lengths for path, length in record.items()),
                    key=lambda item: item[1],
                    reverse=True,
                )[:8]
                largest_units = sorted(
                    (
                        (call_index, len(unit), len(unit.encode("utf-8")), _MARKER in unit, *safe_unit_label(unit))
                        for call_index, (units, _packed) in enumerate(pack_calls[first_pack_start:], start=first_pack_start)
                        for unit in units
                    ),
                    key=lambda item: item[1],
                    reverse=True,
                )[:8]
                print(
                    "first-turn guard source sizes:",
                    json.dumps(
                        {
                            "first_status": first_settled.status,
                            "first_error": None if first_settled.error is None else first_settled.error.model_dump(mode="json"),
                            "semantic_scalars": longest,
                            "largest_guard_units": largest_units,
                            "sdk_roles": sdk.roles,
                            "original_driver_trace": diagnostic_trace[-12:],
                        },
                        sort_keys=True,
                    ),
                )
            assert first_settled.status == "completed"
            first_state = await service.get_current_state(session.id)
            before = await service.get_messages(session.id, limit=None)
            pack_calls.clear()
            guard_bodies.clear()
            sdk_calls.clear()
            guard_http_counts_at_sdk_entry.clear()
            checks_at_sdk_entry.clear()
            body = message_body(_SECOND_PROMPT, state_id=None if first_state is None else first_state.id)
            # The wrapper calls the original driver without changing deadlines or results.
            with monkeypatch.context() as diagnostic_patch:
                observed = observe_original_driver_failures(diagnostic_patch, body["operation_id"], session_id=session.id)
                settled = await submit(session.id, body)
            diagnostic_trace.extend(observed)
            # The first pack belongs to the initial PRIMARY. Over-limit input
            # must refuse there, before any reply-only classifier can run.
            assert pack_calls
            if unit_length == PROMPT_GUARD_MAX_TEXT_CHARACTERS + 1:
                assert len(pack_calls) == 1
            units, packed = pack_calls[0]
            marked = [unit for unit in units if _MARKER in unit]
            assert marked == [_history_unit(first)]
            assert len(marked[0]) == unit_length
            assert len(marked[0].encode("utf-8")) == unit_length
            return session, body, first, first_state, first_settled, before, settled, units, packed, marked[0]

        # One more character than the limit: the indivisible unit is refused, never sliced.
        session, body, first, first_state, refused_first_settled, before, settled, units, packed, unit = await second_turn_after_history(
            PROMPT_GUARD_MAX_TEXT_CHARACTERS + 1
        )
        # Byte-exact: the packed request still carries the whole unit, so the
        # size policy sees it and nothing is sent to Azure at all.
        _assert_documents_reproduce_complete_units(units, packed)
        assert unit in packed
        assert guard_bodies == []
        assert sdk_calls == []
        assert settled.status == "failed", settled.model_dump_json()
        assert settled.error is not None
        failure_detail = settled.model_dump_json()
        if settled.error.http_status != 422:
            # Read only bounded identities and dispositions after the public
            # terminal. Never log provider wire, user content or audit chunks.
            diagnostic: dict[str, Any] = {"snapshot": json.loads(failure_detail), "driver_trace": diagnostic_trace[-20:]}
            try:
                record = await run_sync_in_worker(
                    app.state.composer_async_operation_authority.get, session_id=session.id, operation_id=body["operation_id"]
                )
                diagnostic["record"] = (
                    None
                    if record is None
                    else {
                        "status": record.status,
                        "failure_code": record.failure_code,
                        "attempt": record.attempt,
                        "segment": record.current_segment_sequence,
                        "generation": record.ownership_generation,
                        "cursor": record.journal_cursor,
                        "settled_by": record.settled_by,
                    }
                )
                observed_rows = await service.get_messages(session.id, limit=None)
                diagnostic["guard_checks"] = [
                    (check["role"], check["disposition"], check["reason"])
                    for check in _guard_checks(observed_rows)
                    if record is not None and check["operation_id"] == record.session_operation_id
                ]
                with service._engine.connect() as connection:
                    try:
                        stored = load_snapshot(connection, session_id=str(session.id), operation_id=body["operation_id"])
                        stack = decode_continuation_snapshot(stored.payload_json)
                        policy = decode_continuation_policy(stack.admission_policy_json, stack.policy_hash)
                        diagnostic["policy"] = {"type": type(policy).__name__, "schema_version": policy.schema_version}
                    except Exception as error:
                        diagnostic["policy_readback_error_type"] = type(error).__name__
                    diagnostic["effects"] = [
                        (row.owner, row.disposition, row.effect_key)
                        for row in connection.execute(
                            select(
                                composer_continuation_effects_table.c.owner,
                                composer_continuation_effects_table.c.disposition,
                                composer_continuation_effects_table.c.effect_key,
                            ).where(composer_continuation_effects_table.c.operation_id == body["operation_id"])
                        )
                    ][:20]
                    diagnostic["events"] = [
                        row.tag
                        for row in connection.execute(
                            select(composer_continuation_events_table.c.tag).where(
                                composer_continuation_events_table.c.operation_id == body["operation_id"]
                            )
                        )
                    ][:20]
                    diagnostic["checkpoints"] = [
                        (row.kind, row.cursor)
                        for row in connection.execute(
                            select(composer_continuation_checkpoints_table.c.kind, composer_continuation_checkpoints_table.c.cursor).where(
                                composer_continuation_checkpoints_table.c.operation_id == body["operation_id"]
                            )
                        )
                    ][:20]
            except Exception as error:
                diagnostic["readback_error_type"] = type(error).__name__
            failure_detail = json.dumps(diagnostic, sort_keys=True, default=str)
        assert settled.error.http_status == 422, failure_detail
        public = settled.error.body
        assert public["detail"]["error_type"] == "prompt_guard_unsupported_input"
        assert public["detail"]["detail"] == _LOCAL_DETAIL

        record = await run_sync_in_worker(
            app.state.composer_async_operation_authority.get, session_id=session.id, operation_id=body["operation_id"]
        )
        assert record is not None and record.status == "failed"
        rows = await service.get_messages(session.id, limit=None)
        checks = _guard_checks(rows)
        refused = [check for check in checks if check["operation_id"] == record.session_operation_id]
        assert len(refused) == 1
        assert (refused[0]["disposition"], refused[0]["reason"]) == ("unavailable", "unsupported_input")
        assert refused[0]["operation_epoch"] == record.session_operation_epoch
        assert refused[0]["audit_id"] == public["detail"]["audit_id"]
        # Preceding effects are retained: turn 1's messages and state, and turn 2's user message.
        assert [row.id for row in rows][: len(before)] == [row.id for row in before]
        assert [row.content for row in rows if row.role == "user"] == [first, _SECOND_PROMPT]
        current = await service.get_current_state(session.id)
        assert (current is None and first_state is None) or (
            current is not None and first_state is not None and current.id == first_state.id
        )
        with service._engine.connect() as connection:
            attempts = connection.execute(
                select(quota_provider_attempts_table).where(quota_provider_attempts_table.c.session_id == str(session.id))
            ).all()
        assert len(attempts) == 2 and all(attempt.operation_id != record.session_operation_id for attempt in attempts)
        assert first not in json.dumps(public) + json.dumps(checks)

        # Exactly at the limit: the unit is inspected whole before PRIMARY SDK entry.
        (
            _session,
            _body,
            _first,
            at_limit_state,
            at_limit_first_settled,
            before,
            settled,
            units,
            packed,
            unit,
        ) = await second_turn_after_history(PROMPT_GUARD_MAX_TEXT_CHARACTERS)
        turn_one_checks = _guard_checks(before)
        _assert_documents_reproduce_complete_units(units, packed)
        assert unit in packed
        with service._engine.connect() as connection:
            quota_attempts = connection.execute(
                select(
                    quota_provider_attempts_table.c.session_id,
                    quota_provider_attempts_table.c.operation_id,
                    quota_provider_attempts_table.c.settled_at,
                    quota_provider_attempts_table.c.ledger_entry_id,
                )
            ).all()
            token_ledger = connection.execute(
                select(
                    token_usage_ledger_table.c.session_id,
                    token_usage_ledger_table.c.prompt_tokens,
                    token_usage_ledger_table.c.completion_tokens,
                )
            ).all()
        assert [request["model"] for request in sdk_calls] == ["test/model", "test/advisor"], json.dumps(
            {
                "first_statuses": [refused_first_settled.status, at_limit_first_settled.status],
                "status": settled.status,
                "error_status": None if settled.error is None else settled.error.http_status,
                "error_body": None if settled.error is None else settled.error.body,
                "driver_trace": diagnostic_trace[-20:],
                "quota_attempts": [tuple(row) for row in quota_attempts],
                "token_ledger": [tuple(row) for row in token_ledger],
                "guard_checks": [
                    (check["role"], check["disposition"], check["reason"])
                    for check in _guard_checks(await service.get_messages(_session.id, limit=None))
                ],
            },
            sort_keys=True,
            default=str,
        )
        assert guard_http_counts_at_sdk_entry
        first_primary_http = guard_bodies[: guard_http_counts_at_sdk_entry[0]]
        inspected = [document for body in first_primary_http for document in body["documents"]]
        _assert_documents_reproduce_complete_units(units, inspected)
        assert tuple(inspected) == packed
        assert all(body["userPrompt"] == _SECOND_PROMPT for body in first_primary_http)
        # Turn 2's own allow decision (one row beyond turn 1's) was durable before the SDK ran.
        assert checks_at_sdk_entry[0][: len(turn_one_checks)] == turn_one_checks
        assert len(checks_at_sdk_entry[0]) == len(turn_one_checks) + 1
        assert (checks_at_sdk_entry[0][-1]["disposition"], checks_at_sdk_entry[0][-1]["reason"]) == ("allow", "clean")

        # Integration completion point (U-40): both first turns and the at-limit
        # second turn complete with the model's provider-authored handoff, and turn 1 leaves
        # a checkpoint state behind.
        for label, completed in (
            ("over_limit_first", refused_first_settled),
            ("at_limit_first", at_limit_first_settled),
            ("at_limit_second", settled),
        ):
            assert completed.status == "completed", json.dumps(
                {"case": label, "settled": completed.model_dump(mode="json"), "driver_trace": diagnostic_trace[-40:]},
                sort_keys=True,
                default=str,
            )
            assert completed.result is not None and completed.result.message.content == "Noted."
        assert sdk.steps == []
        assert first_state is not None and at_limit_state is not None
