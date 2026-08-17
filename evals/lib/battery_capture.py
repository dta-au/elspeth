"""Typed view over a captured battery run directory (spec §4/§5).

Everything the scorer reads comes from these accessors, so a taxonomy
revision never re-parses raw JSON. ``tool_outcomes`` re-implements the
server's durable-pair projection (routes/_helpers.py
``_tool_call_outcomes_by_call_id``) so offline scoring never trusts a tool
NAME or the assistant stamp alone.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from elspeth.contracts.composer_audit import ComposerToolStatus


class CaptureError(RuntimeError):
    """A run directory is missing an artifact the scorer cannot do without (or carries one it cannot parse)."""


@dataclass(frozen=True)
class Instrument:
    """Driver-recorded instrument facts about one run — the battery-owned half of ``meta.json``."""

    truncated: bool = False
    read_integrity: str | None = None
    http_unrecovered: str | None = None
    auth_failed: bool = False
    review_rounds_exhausted: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


INSTRUMENT_KEYS: tuple[str, ...] = tuple(Instrument.__dataclass_fields__)


def parse_instrument(meta: Mapping[str, Any]) -> Instrument:
    """Strict parse: keys must be exactly INSTRUMENT_KEYS. A renamed/missing key is a CaptureError, never a clean run."""
    block = meta.get("instrument")
    if not isinstance(block, Mapping) or set(block) != set(INSTRUMENT_KEYS):
        raise CaptureError(
            f"meta.instrument must carry exactly {sorted(INSTRUMENT_KEYS)}; got {sorted(block) if isinstance(block, Mapping) else block!r}"
        )
    return Instrument(
        truncated=bool(block["truncated"]),
        read_integrity=block["read_integrity"],
        http_unrecovered=block["http_unrecovered"],
        auth_failed=bool(block["auth_failed"]),
        review_rounds_exhausted=bool(block["review_rounds_exhausted"]),
    )


@dataclass
class Capture:
    messages: list[dict[str, Any]]
    state: dict[str, Any] | None
    validate: dict[str, Any] | None
    reviews: list[dict[str, Any]]
    meta: dict[str, Any]
    run_dir: Path | None = None


@dataclass(frozen=True)
class LlmCall:
    sequence_no: int
    model_requested: str
    model_returned: str | None
    status: str
    tools_spec_hash: str | None
    planner_call_ordinal: int | None
    prompt_tokens: int | None
    completion_tokens: int | None
    cached_prompt_tokens: int | None
    provider_cost: float | None
    latency_ms: int
    started_at: str
    finished_at: str
    error_class: str | None


@dataclass(frozen=True)
class PlannerAttempt:
    sequence_no: int
    ordinal: int
    planner_call_ordinal: int | None
    phase: str
    outcome: str
    planner_code: str | None
    led_to: str
    selected_tools: tuple[str, ...]
    requested_information: tuple[str, ...]
    new_information: tuple[str, ...]
    rejection_codes: tuple[str, ...]
    repeated_fingerprint: bool


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]
    outcome: str | None


@dataclass(frozen=True)
class AssistantTurn:
    sequence_no: int
    message_id: str
    content: str
    raw_content: str | None
    tool_calls: tuple[ToolCall, ...]


@dataclass(frozen=True)
class ToolRow:
    sequence_no: int
    tool_call_id: str
    content: dict[str, Any] | None
    composition_state_id: str | None
    envelope: dict[str, Any] | None
    parent_assistant_id: str | None


def _read_json(path: Path, *, required: bool) -> Any:
    if not path.exists():
        if required:
            raise CaptureError(f"missing {path.name} in {path.parent}")
        return None
    try:
        return json.loads(path.read_text())
    except ValueError as exc:
        raise CaptureError(f"unparseable {path}: {exc}") from exc


def load_capture(run_dir: Path) -> Capture:
    run_dir = Path(run_dir)
    messages = _read_json(run_dir / "messages.json", required=True)
    meta = _read_json(run_dir / "meta.json", required=True)
    if not isinstance(messages, list) or not isinstance(meta, dict):
        raise CaptureError(f"{run_dir}: messages.json must be a list and meta.json an object")
    reviews = _read_json(run_dir / "reviews.json", required=False)
    return Capture(
        messages=sorted(messages, key=lambda m: (m.get("sequence_no") is None, m.get("sequence_no") or 0)),
        state=_read_json(run_dir / "state.json", required=False),
        validate=_read_json(run_dir / "validate.json", required=False),
        reviews=list(reviews) if isinstance(reviews, list) else [],
        meta=meta,
        run_dir=run_dir,
    )


def _seq(m: dict[str, Any]) -> int:
    return int(m.get("sequence_no") or 0)


def _audit_envelopes(capture: Capture, kind: str) -> list[tuple[int, dict[str, Any]]]:
    out: list[tuple[int, dict[str, Any]]] = []
    for m in capture.messages:
        if m.get("role") != "audit":
            continue
        for env in m.get("tool_calls") or []:
            if isinstance(env, dict) and env.get("_kind") == kind:
                out.append((_seq(m), env))
    return out


def llm_calls(capture: Capture) -> list[LlmCall]:
    calls: list[LlmCall] = []
    for seq, env in _audit_envelopes(capture, "llm_call_audit"):
        c = env.get("call") or {}
        calls.append(
            LlmCall(
                sequence_no=seq,
                model_requested=str(c.get("model_requested")),
                model_returned=c.get("model_returned"),
                status=str(c.get("status")),
                tools_spec_hash=c.get("tools_spec_hash"),
                planner_call_ordinal=c.get("planner_call_ordinal"),
                prompt_tokens=c.get("prompt_tokens"),
                completion_tokens=c.get("completion_tokens"),
                cached_prompt_tokens=c.get("cached_prompt_tokens"),
                provider_cost=c.get("provider_cost"),
                latency_ms=int(c.get("latency_ms") or 0),
                started_at=str(c.get("started_at")),
                finished_at=str(c.get("finished_at")),
                error_class=c.get("error_class"),
            )
        )
    return calls


def planner_attempts(capture: Capture) -> list[PlannerAttempt]:
    out: list[PlannerAttempt] = []
    for seq, env in _audit_envelopes(capture, "planner_attempt_audit"):
        a = env.get("attempt") or {}
        out.append(
            PlannerAttempt(
                sequence_no=seq,
                ordinal=int(a.get("ordinal") or 0),
                phase=str(a.get("phase")),
                outcome=str(a.get("outcome")),
                planner_call_ordinal=a.get("planner_call_ordinal"),
                planner_code=a.get("planner_code"),
                led_to=str(a.get("led_to")),
                selected_tools=tuple(str(t) for t in (a.get("selected_tools") or [])),
                requested_information=tuple(str(t) for t in (a.get("requested_information") or [])),
                new_information=tuple(str(t) for t in (a.get("new_information") or [])),
                rejection_codes=tuple(str(t) for t in (a.get("rejection_codes") or [])),
                repeated_fingerprint=bool(a.get("repeated_fingerprint")),
            )
        )
    return out


def assistant_turns(capture: Capture) -> list[AssistantTurn]:
    turns: list[AssistantTurn] = []
    for m in capture.messages:
        if m.get("role") != "assistant":
            continue
        calls: list[ToolCall] = []
        for tc in m.get("tool_calls") or []:
            if not isinstance(tc, dict) or "function" not in tc:
                continue
            fn = tc.get("function") or {}
            raw_args = fn.get("arguments")
            try:
                args = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args or {})
            except ValueError:
                args = {"_unparseable": True}
            calls.append(
                ToolCall(
                    id=str(tc.get("id")),
                    name=str(fn.get("name")),
                    arguments=args if isinstance(args, dict) else {},
                    outcome=tc.get("outcome"),
                )
            )
        turns.append(AssistantTurn(_seq(m), str(m.get("id")), str(m.get("content") or ""), m.get("raw_content"), tuple(calls)))
    return turns


def tool_rows(capture: Capture) -> list[ToolRow]:
    rows: list[ToolRow] = []
    for m in capture.messages:
        if m.get("role") != "tool" or not m.get("tool_call_id"):
            continue
        content: dict[str, Any] | None
        try:
            parsed = json.loads(m.get("content") or "")
            content = parsed if isinstance(parsed, dict) else None
        except ValueError:
            content = None
        env = None
        if m.get("tool_calls"):
            first = m["tool_calls"][0]
            env = first if isinstance(first, dict) else None
        rows.append(ToolRow(_seq(m), str(m["tool_call_id"]), content, m.get("composition_state_id"), env, m.get("parent_assistant_id")))
    return rows


_FAILED_STATUSES = frozenset({ComposerToolStatus.ARG_ERROR.value, ComposerToolStatus.PLUGIN_CRASH.value})
_CANCELLED = ComposerToolStatus.CANCELLED.value


def tool_outcomes(capture: Capture) -> dict[str, str]:
    """Durable-pair projection: applied | rejected | failed | cancelled | completed."""
    out: dict[str, str] = {}
    for row in tool_rows(capture):
        env = row.envelope
        if env is None and row.composition_state_id is not None:
            out[row.tool_call_id] = "applied"
            continue
        if env is not None:
            vb, va = env.get("version_before"), env.get("version_after")
            if isinstance(vb, int) and isinstance(va, int) and va > vb:
                out[row.tool_call_id] = "applied"
                continue
            status = env.get("status")
            if status == _CANCELLED:
                out[row.tool_call_id] = "cancelled"
                continue
            if status in _FAILED_STATUSES:
                out[row.tool_call_id] = "failed"
                continue
        content = row.content
        if isinstance(content, dict):
            if content.get("error_class"):
                out[row.tool_call_id] = "cancelled" if content.get("_redaction_status") == _CANCELLED else "failed"
                continue
            if content.get("success") is False:
                out[row.tool_call_id] = "rejected"
                continue
        out[row.tool_call_id] = "completed"
    return out


__all__ = [
    "INSTRUMENT_KEYS",
    "AssistantTurn",
    "Capture",
    "CaptureError",
    "Instrument",
    "LlmCall",
    "PlannerAttempt",
    "ToolCall",
    "ToolRow",
    "assistant_turns",
    "llm_calls",
    "load_capture",
    "parse_instrument",
    "planner_attempts",
    "tool_outcomes",
    "tool_rows",
]
