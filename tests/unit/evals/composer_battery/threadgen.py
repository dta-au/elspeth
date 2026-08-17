"""Builders for synthetic battery captures.

Every helper returns plain dicts in the exact wire shape ``GET /messages``
serialises (see tests/unit/evals/composer_battery/fixtures/run_ideal), so
a scorer test reads like the thread it describes.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

from evals.lib.battery_capture import Capture, Instrument

COMPOSER = "openrouter/anthropic/claude-sonnet-5"
ADVISOR = "openrouter/anthropic/claude-opus-4-8"
TOOLS_HASH = "tsh"

_EPOCH = datetime(2026, 8, 17, tzinfo=UTC)


def ts(seconds: int) -> str:
    """Monotonic ISO timestamp ``seconds`` after the epoch (never wraps — seq 59→60 used to)."""
    return (_EPOCH + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")


def audit_row(
    seq: int,
    *,
    status: str = "success",
    tools: bool = True,
    model: str = COMPOSER,
    planner_ordinal: int | None = None,
    prompt_tokens: int | None = 100,
    completion_tokens: int | None = 10,
    cached: int | None = None,
    cost: float | None = 0.01,
    error_class: str | None = None,
) -> dict[str, Any]:
    call = {
        "model_requested": model,
        "model_returned": model.rsplit("/", 1)[-1],
        "status": status,
        "finish_reason": "tool_calls" if tools else "stop",
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": (prompt_tokens or 0) + (completion_tokens or 0),
        "cached_prompt_tokens": cached,
        "cache_creation_input_tokens": None,
        "cache_read_input_tokens": cached,
        "reasoning_tokens": None,
        "latency_ms": 1000,
        "provider_request_id": f"r{seq}",
        "messages_hash": f"mh{seq}",
        "tools_spec_hash": TOOLS_HASH if tools else None,
        "declared_tool_names": ["get_plugin_schema", "set_pipeline"] if tools else [],
        "started_at": ts(seq),
        "finished_at": ts(seq + 1),
        "error_class": error_class,
        "error_message": None,
        "temperature": None,
        "seed": None,
        "provider_cost": cost,
        "provider_cost_source": "response_usage.cost",
        "max_completion_tokens_requested": None,
        "planner_policy_hash": None,
        "planner_call_ordinal": planner_ordinal,
    }
    return {
        "id": f"a{seq}",
        "session_id": "s",
        "role": "audit",
        "content": "",
        "raw_content": None,
        "segments": [],
        "tool_calls": [{"_kind": "llm_call_audit", "call": call}],
        "created_at": ts(seq),
        "composition_state_id": None,
        "tool_call_id": None,
        "parent_assistant_id": None,
        "sequence_no": seq,
    }


def planner_attempt_row(
    seq: int,
    *,
    ordinal: int = 1,
    phase: str = "discovery",
    outcome: str = "accepted",
    planner_code: str | None = None,
    led_to: str = "done",
    new_information: tuple[str, ...] = (),
) -> dict[str, Any]:
    attempt = {
        "ordinal": ordinal,
        "planner_call_ordinal": ordinal,
        "phase": phase,
        "outcome": outcome,
        "planner_code": planner_code,
        "led_to": led_to,
        "selected_tools": [],
        "requested_information": [],
        "new_information": list(new_information),
        "rejection_codes": [],
        "candidate_shape_hash": None,
        "repeated_fingerprint": False,
    }
    return {
        "id": f"pa{seq}",
        "session_id": "s",
        "role": "audit",
        "content": "",
        "raw_content": None,
        "segments": [],
        "tool_calls": [{"_kind": "planner_attempt_audit", "attempt": attempt}],
        "created_at": ts(seq),
        "composition_state_id": None,
        "tool_call_id": None,
        "parent_assistant_id": None,
        "sequence_no": seq,
    }


def call(cid: str, name: str, args: dict[str, Any] | None = None, *, stamp: str | None = None) -> dict[str, Any]:
    return {
        "id": cid,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(args or {})},
        "outcome": stamp,
        "applied_state_version": None,
    }


def assistant_row(seq: int, calls: list[dict[str, Any]] | None = None, *, content: str = "") -> dict[str, Any]:
    return {
        "id": f"as{seq}",
        "session_id": "s",
        "role": "assistant",
        "content": content,
        "raw_content": None,
        "segments": [],
        "tool_calls": calls or None,
        "created_at": ts(seq),
        "composition_state_id": None,
        "tool_call_id": None,
        "parent_assistant_id": None,
        "sequence_no": seq,
    }


def tool_row(
    seq: int,
    cid: str,
    parent: str,
    *,
    content: dict[str, Any] | None = None,
    state_id: str | None = None,
    envelope: dict[str, Any] | None = None,
) -> dict[str, Any]:
    body = json.dumps(content if content is not None else {"success": True})
    return {
        "id": f"t{seq}",
        "session_id": "s",
        "role": "tool",
        "content": body,
        "raw_content": None,
        "segments": [],
        "tool_calls": [envelope] if envelope else None,
        "created_at": ts(seq),
        "composition_state_id": state_id,
        "tool_call_id": cid,
        "parent_assistant_id": parent,
        "sequence_no": seq,
    }


def user_row(seq: int, prompt: str = "prompt") -> dict[str, Any]:
    return {
        "id": f"u{seq}",
        "session_id": "s",
        "role": "user",
        "content": prompt,
        "raw_content": None,
        "segments": [],
        "tool_calls": None,
        "created_at": ts(seq),
        "composition_state_id": None,
        "tool_call_id": None,
        "parent_assistant_id": None,
        "sequence_no": seq,
    }


def meta(
    *,
    case: str = "fork_coalesce",
    repeat: int = 1,
    post_status: int = 200,
    detail: dict[str, Any] | None = None,
    terminal: dict[str, Any] | None = None,
    instrument: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "round": "t",
        "case": case,
        "repeat": repeat,
        "corpus_version": 0,
        "prompt_sha256": "x",
        "session_id": "s",
        "state_id": "state-2",
        "http": [{"step": "post_message", "status": post_status, "elapsed_ms": 1, "detail": detail}],
        "server_terminal": terminal or {"budget_exhausted": None, "reason": None, "source": "none"},
        "preferences": {"trust_mode": "auto_commit", "density_default": "high"},
        "instrument": instrument or Instrument().to_dict(),
        "identity": {
            "binding": {
                "substrate": "https://elspeth.foundryside.dev",
                "composer_model": COMPOSER,
                "advisor_model": ADVISOR,
                "model_returned": "claude-sonnet-5",
                "composer_timeout_seconds": 600.0,
                "budgets": {"composition_turns": 30, "discovery_turns": 10},
                "tools_spec_hash": TOOLS_HASH,
                "temperature": None,
                "seed": None,
            },
            "recorded": {
                "composer_skill_hash": None,
                "composer_skill_hash_source": "null",
                "local_skill_file_sha256": "skill-local",
                "env_file_sha256": "env-x",
                "first_call_messages_hash": "mh2",
                "server_version": None,
                "frontend_build": None,
            },
        },
    }


def capture(
    messages: list[dict[str, Any]],
    *,
    state: dict[str, Any] | None,
    is_valid: bool | None = True,
    reviews: list[dict[str, Any]] | None = None,
    meta_doc: dict[str, Any] | None = None,
) -> Capture:
    validate = (
        None
        if is_valid is None
        else {"is_valid": is_valid, "checks": [], "errors": [], "warnings": [], "readiness": "ready" if is_valid else "blocked"}
    )
    return Capture(
        messages=sorted(messages, key=lambda m: m["sequence_no"]),
        state=state,
        validate=validate,
        reviews=reviews or [],
        meta=meta_doc or meta(),
        run_dir=None,
    )


def ideal_thread(args: dict[str, Any], *, schema_calls: int = 3) -> list[dict[str, Any]]:
    """Exactly the floor: user -> audit(tool) -> assistant[N x get_plugin_schema] -> tool rows -> audit(tool) -> assistant[set_pipeline applied] -> tool row."""
    rows: list[dict[str, Any]] = [user_row(1), audit_row(2)]
    disc = [
        call(f"d{i}", "get_plugin_schema", {"plugin_type": "transform", "plugin_name": f"p{i}"}, stamp="completed")
        for i in range(schema_calls)
    ]
    rows.append(assistant_row(3, disc))
    seq = 4
    for i in range(schema_calls):
        rows.append(tool_row(seq, f"d{i}", "as3", content={"success": True, "schema": {}}))
        seq += 1
    rows.append(audit_row(seq))
    seq += 1
    sp_seq = seq
    rows.append(assistant_row(seq, [call("sp", "set_pipeline", args, stamp="applied")], content="Done."))
    seq += 1
    rows.append(tool_row(seq, "sp", f"as{sp_seq}", content={"success": True}, state_id="state-2"))
    return rows
