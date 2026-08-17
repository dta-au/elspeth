from __future__ import annotations

import json
from pathlib import Path

import pytest
from evals.lib.battery_capture import (
    INSTRUMENT_KEYS,
    CaptureError,
    Instrument,
    assistant_turns,
    llm_calls,
    load_capture,
    parse_instrument,
    planner_attempts,
    tool_outcomes,
    tool_rows,
)

from tests.unit.evals.composer_battery import threadgen as tg

FIXTURES = Path(__file__).parent / "fixtures"


def test_ideal_run_parses_into_typed_rows() -> None:
    cap = load_capture(FIXTURES / "run_ideal")
    calls = llm_calls(cap)
    assert [c.status for c in calls] == ["success", "success", "success"]
    assert [c.tools_spec_hash is not None for c in calls] == [True, False, True]  # tool, advisor, tool
    turns = assistant_turns(cap)
    assert [len(t.tool_calls) for t in turns] == [3, 1]  # batched discovery, then set_pipeline
    assert turns[1].tool_calls[0].name == "set_pipeline"
    assert planner_attempts(cap) == []
    rows = tool_rows(cap)
    assert len(rows) == 4 and all(r.parent_assistant_id for r in rows)


def test_outcomes_use_the_durable_pair_not_the_stamp() -> None:
    cap = load_capture(FIXTURES / "run_ideal")
    out = tool_outcomes(cap)
    assert out["call_sp"] == "applied"  # composition_state_id set on the tool row
    assert out["call_d1"] == "completed"
    # mutate: strip the state id and give a rejecting content → rejected, regardless of the assistant stamp
    doc = json.loads((FIXTURES / "run_ideal/messages.json").read_text())
    for m in doc:
        if m["role"] == "tool" and m["tool_call_id"] == "call_sp":
            m["composition_state_id"] = None
            m["content"] = json.dumps({"success": False, "error": "rejected"})
        if m["role"] == "assistant":
            for tc in m.get("tool_calls") or []:
                if tc.get("id") == "call_sp":
                    tc["outcome"] = "applied"  # lying stamp
    cap2 = load_capture(FIXTURES / "run_ideal")
    cap2.messages = doc
    assert tool_outcomes(cap2)["call_sp"] == "rejected"


def test_envelope_cancelled_and_failed_statuses() -> None:
    cap = load_capture(FIXTURES / "run_ideal")
    doc = json.loads((FIXTURES / "run_ideal/messages.json").read_text())
    tool = next(m for m in doc if m["role"] == "tool" and m["tool_call_id"] == "call_sp")
    tool["composition_state_id"] = None
    tool["tool_calls"] = [{"_kind": "audit", "status": "cancelled", "version_before": 1, "version_after": 1}]
    cap.messages = doc
    assert tool_outcomes(cap)["call_sp"] == "cancelled"
    tool["tool_calls"] = [{"_kind": "audit", "status": "arg_error", "version_before": 1, "version_after": 1}]
    assert tool_outcomes(cap)["call_sp"] == "failed"
    tool["tool_calls"] = [{"_kind": "audit", "status": "ok", "version_before": 1, "version_after": 2}]
    assert tool_outcomes(cap)["call_sp"] == "applied"


def test_missing_messages_is_a_capture_error(tmp_path: Path) -> None:
    (tmp_path / "meta.json").write_text("{}")
    with pytest.raises(CaptureError):
        load_capture(tmp_path)


def test_instrument_contract_is_closed() -> None:
    assert set(INSTRUMENT_KEYS) == {"truncated", "read_integrity", "http_unrecovered", "auth_failed", "review_rounds_exhausted"}
    good = {"instrument": Instrument(truncated=True).to_dict()}
    assert parse_instrument(good) == Instrument(truncated=True)
    with pytest.raises(CaptureError, match="instrument"):
        parse_instrument({"instrument": {**Instrument().to_dict(), "http_error": "renamed key"}})  # unknown key
    with pytest.raises(CaptureError, match="instrument"):
        parse_instrument({"instrument": {"truncated": False}})  # missing keys
    with pytest.raises(CaptureError, match="instrument"):
        parse_instrument({})  # absent block


def test_tool_outcomes_agree_with_the_server_projection() -> None:
    """Characterization: the offline durable-pair projection must equal
    ``sessions/routes/_helpers.py:_tool_call_outcomes_by_call_id`` over the same rows,
    across applied / rejected / failed / cancelled / lying-stamp shapes."""
    from datetime import UTC, datetime
    from uuid import uuid4

    from elspeth.web.sessions.protocol import ChatMessageRecord
    from elspeth.web.sessions.routes._helpers import _tool_call_outcomes_by_call_id

    state_id = uuid4()
    rows = [
        tg.assistant_row(
            1,
            [
                tg.call("applied_pair", "set_pipeline", {}, stamp="rejected"),
                tg.call("rejected", "set_pipeline", {}),
                tg.call("failed", "set_pipeline", {}),
                tg.call("cancelled", "set_pipeline", {}),
                tg.call("completed", "get_plugin_schema", {}),
                tg.call("env_applied", "upsert_node", {}),
                tg.call("err_cancelled", "set_output", {}),
            ],
        ),
        tg.tool_row(2, "applied_pair", "as1", content={"success": True}, state_id=str(state_id)),
        tg.tool_row(3, "rejected", "as1", content={"success": False, "error": "no"}),
        tg.tool_row(
            4,
            "failed",
            "as1",
            content={"success": True},
            envelope={"_kind": "audit", "status": "arg_error", "version_before": 1, "version_after": 1},
        ),
        tg.tool_row(
            5,
            "cancelled",
            "as1",
            content={"success": True},
            envelope={"_kind": "audit", "status": "cancelled", "version_before": 1, "version_after": 1},
        ),
        tg.tool_row(6, "completed", "as1", content={"success": True, "schema": {}}),
        tg.tool_row(
            7,
            "env_applied",
            "as1",
            content={"success": True},
            envelope={"_kind": "audit", "status": "success", "version_before": 1, "version_after": 2},
        ),
        tg.tool_row(8, "err_cancelled", "as1", content={"error_class": "ToolCancelled", "_redaction_status": "cancelled"}),
    ]
    ours = tool_outcomes(tg.capture(rows, state=None, is_valid=None))
    records = [
        ChatMessageRecord(
            id=uuid4(),
            session_id=uuid4(),
            role=m["role"],
            content=m["content"],
            created_at=datetime(2026, 8, 17, tzinfo=UTC),
            writer_principal="compose_loop",
            sequence_no=m["sequence_no"],
            tool_calls=m["tool_calls"],
            composition_state_id=state_id if m.get("composition_state_id") else None,
            tool_call_id=m.get("tool_call_id"),
            parent_assistant_id=uuid4() if m.get("parent_assistant_id") else None,
        )
        for m in rows
    ]
    theirs = {k: v.outcome for k, v in _tool_call_outcomes_by_call_id(records, state_versions_by_id={str(state_id): 2}).items()}
    assert (
        ours
        == theirs
        == {
            "applied_pair": "applied",
            "rejected": "rejected",
            "failed": "failed",
            "cancelled": "cancelled",
            "completed": "completed",
            "env_applied": "applied",
            "err_cancelled": "cancelled",
        }
    )
