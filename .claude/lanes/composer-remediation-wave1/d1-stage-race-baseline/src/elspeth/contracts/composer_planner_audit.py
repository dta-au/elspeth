"""Value-free audit contract for one semantic composer planner response.

The LLM-call audit contract records the physical provider request.  This
contract records the semantic decision ELSPETH made after receiving a
response: what closed class of information it requested, whether a candidate
was accepted or rejected, and what the loop did next.  It deliberately carries
no prompts, tool arguments, candidate values, provider prose, or validator
messages.

Layer: L0 (contracts). Imports nothing above contracts.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol


class ComposerPlannerAttemptPhase(StrEnum):
    RESPONSE = "response"
    DISCOVERY = "discovery"
    CANDIDATE = "candidate"
    REPAIR = "repair"
    HATCH = "hatch"
    PROSE = "prose"


class ComposerPlannerAttemptOutcome(StrEnum):
    DISCOVERY_EXECUTED = "discovery_executed"
    CANDIDATE_REJECTED = "candidate_rejected"
    ARG_ERROR = "arg_error"
    DEFERRED_CLAIM = "deferred_claim"
    TRUNCATED = "truncated"
    PROSE_NUDGED = "prose_nudged"
    PROSE_REPLY = "prose_reply"
    GUARD_FIRED = "guard_fired"
    BUDGET_EXHAUSTED = "budget_exhausted"
    DECLINED = "declined"
    ACCEPTED = "accepted"
    MALFORMED_RESPONSE = "malformed_response"
    CANCELLED = "cancelled"
    INTERNAL_ERROR = "internal_error"


class ComposerPlannerAttemptLedTo(StrEnum):
    CONTINUE = "continue"
    REPAIR = "repair"
    HATCH = "hatch"
    TERMINAL = "terminal"
    DONE = "done"


class ComposerPlannerCode(StrEnum):
    CANDIDATE_CONSTRUCTION_ERROR = "CANDIDATE_CONSTRUCTION_ERROR"
    COMPLETION_TOKENS_EXCEEDED = "COMPLETION_TOKENS_EXCEEDED"
    COMPOSITION_EXHAUSTED = "COMPOSITION_EXHAUSTED"
    COST_CAP_EXCEEDED = "COST_CAP_EXCEEDED"
    COST_UNAVAILABLE = "COST_UNAVAILABLE"
    DECLINED = "DECLINED"
    DISCOVERY_CYCLE = "DISCOVERY_CYCLE"
    DISCOVERY_EXHAUSTED = "DISCOVERY_EXHAUSTED"
    DISCOVERY_NO_GAIN = "DISCOVERY_NO_GAIN"
    DISCOVERY_ONLY = "DISCOVERY_ONLY"
    MALFORMED_RESPONSE = "MALFORMED_RESPONSE"
    PROSE_REPLY = "PROSE_REPLY"
    PROVIDER_CALLS_EXHAUSTED = "PROVIDER_CALLS_EXHAUSTED"
    PROVIDER_ERROR = "PROVIDER_ERROR"
    REPAIR_BLIND_REPEAT = "REPAIR_BLIND_REPEAT"
    REPAIR_EXHAUSTED = "REPAIR_EXHAUSTED"
    REQUEST_BYTES_EXHAUSTED = "REQUEST_BYTES_EXHAUSTED"
    RESPONSE_TRUNCATED = "RESPONSE_TRUNCATED"
    TIMEOUT = "TIMEOUT"
    TOOL_CALLS_EXHAUSTED = "TOOL_CALLS_EXHAUSTED"
    VALIDATION_FAILED = "VALIDATION_FAILED"


class ComposerPlannerInformationClass(StrEnum):
    PIPELINE_CURRENT = "pipeline.current"
    PIPELINE_FULL = "pipeline.full"
    PIPELINE_SOURCE = "pipeline.source"
    PIPELINE_COMPONENT = "pipeline.component"
    CATALOG_SELECTION = "catalog.selection"
    CATALOG_DETAILS_SOURCE = "catalog.details.source"
    CATALOG_DETAILS_TRANSFORM = "catalog.details.transform"
    CATALOG_DETAILS_SINK = "catalog.details.sink"
    PLUGIN_SCHEMA = "plugin.schema"
    PLUGIN_ASSISTANCE = "plugin.assistance"
    MODEL_CATALOG = "model.catalog"
    BLOB_INDEX_SESSION = "blob.index.session"
    BLOB_INDEX_COMPOSER = "blob.index.composer"
    BLOB_METADATA = "blob.metadata"
    BLOB_INSPECTION = "blob.inspection"
    BLOB_CONTENT = "blob.content"
    SECRET_INDEX = "secret.index"
    SECRET_REFERENCE = "secret.reference"
    AUDIT_INFO = "audit.info"
    EXPRESSION_GRAMMAR = "expression.grammar"
    PIPELINE_PREVIEW = "pipeline.preview"
    PIPELINE_DIFF = "pipeline.diff"
    VALIDATION_CODE = "validation.code"


def _require_enum(value: object, enum_type: type[StrEnum], field_name: str) -> None:
    if type(value) is not enum_type:
        optional_suffix = " or None" if field_name == "planner_code" else ""
        raise TypeError(f"{field_name} must be {enum_type.__name__}{optional_suffix}")


def _require_tuple(value: object, item_type: type[object], field_name: str, *, unique: bool = False) -> None:
    if type(value) is not tuple:
        raise TypeError(f"{field_name} must be an exact tuple")
    if any(type(item) is not item_type for item in value):
        raise TypeError(f"{field_name} entries must be exact {item_type.__name__} values")
    if unique and len(set(value)) != len(value):
        raise ValueError(f"{field_name} entries must be unique")


def _safe_token(value: str) -> bool:
    return (
        bool(value)
        and len(value) <= 128
        and value[0] in "abcdefghijklmnopqrstuvwxyz"
        and all(character in "abcdefghijklmnopqrstuvwxyz0123456789_." for character in value)
    )


@dataclass(frozen=True, slots=True)
class ComposerPlannerAttempt:
    """One exact-once, value-free semantic response disposition."""

    ordinal: int
    planner_call_ordinal: int
    phase: ComposerPlannerAttemptPhase
    outcome: ComposerPlannerAttemptOutcome
    planner_code: ComposerPlannerCode | None
    selected_tools: tuple[str, ...]
    requested_information: tuple[ComposerPlannerInformationClass, ...]
    new_information: tuple[ComposerPlannerInformationClass, ...]
    rejection_codes: tuple[str, ...]
    candidate_shape_hash: str | None
    repeated_fingerprint: bool
    led_to: ComposerPlannerAttemptLedTo

    def __post_init__(self) -> None:
        if type(self.ordinal) is not int or self.ordinal < 1:
            raise ValueError("ordinal must be a positive exact integer")
        if type(self.planner_call_ordinal) is not int or self.planner_call_ordinal < 1:
            raise ValueError("planner_call_ordinal must be a positive exact integer")
        _require_enum(self.phase, ComposerPlannerAttemptPhase, "phase")
        _require_enum(self.outcome, ComposerPlannerAttemptOutcome, "outcome")
        if self.planner_code is not None:
            _require_enum(self.planner_code, ComposerPlannerCode, "planner_code")
        _require_tuple(self.selected_tools, str, "selected_tools")
        if any(not _safe_token(tool_name) for tool_name in self.selected_tools):
            raise ValueError("selected_tools entries must be bounded lowercase tool names")
        _require_tuple(
            self.requested_information,
            ComposerPlannerInformationClass,
            "requested_information",
        )
        _require_tuple(
            self.new_information,
            ComposerPlannerInformationClass,
            "new_information",
        )
        _require_tuple(self.rejection_codes, str, "rejection_codes", unique=True)
        if any(not _safe_token(code) for code in self.rejection_codes):
            raise ValueError("rejection_codes entries must be bounded lowercase codes")
        if self.candidate_shape_hash is not None and (
            type(self.candidate_shape_hash) is not str
            or len(self.candidate_shape_hash) != 64
            or any(character not in "0123456789abcdef" for character in self.candidate_shape_hash)
        ):
            raise ValueError("candidate_shape_hash must be 64 lowercase hexadecimal characters or None")
        if type(self.repeated_fingerprint) is not bool:
            raise TypeError("repeated_fingerprint must be an exact bool")
        _require_enum(self.led_to, ComposerPlannerAttemptLedTo, "led_to")

    def to_dict(self) -> dict[str, Any]:
        return {
            "ordinal": self.ordinal,
            "planner_call_ordinal": self.planner_call_ordinal,
            "phase": self.phase.value,
            "outcome": self.outcome.value,
            "planner_code": self.planner_code.value if self.planner_code is not None else None,
            "selected_tools": list(self.selected_tools),
            "requested_information": [information.value for information in self.requested_information],
            "new_information": [information.value for information in self.new_information],
            "rejection_codes": list(self.rejection_codes),
            "candidate_shape_hash": self.candidate_shape_hash,
            "repeated_fingerprint": self.repeated_fingerprint,
            "led_to": self.led_to.value,
        }


class ComposerPlannerAttemptRecorder(Protocol):
    """Append-only sink for semantic composer planner attempts."""

    def record_planner_attempt(self, attempt: ComposerPlannerAttempt) -> None:
        """Persist or buffer one semantic response disposition."""
        ...


__all__ = [
    "ComposerPlannerAttempt",
    "ComposerPlannerAttemptLedTo",
    "ComposerPlannerAttemptOutcome",
    "ComposerPlannerAttemptPhase",
    "ComposerPlannerAttemptRecorder",
    "ComposerPlannerCode",
    "ComposerPlannerInformationClass",
]
