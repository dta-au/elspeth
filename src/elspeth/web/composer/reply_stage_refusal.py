"""Durable record of a reply stage the context budget refused.

After review cards are staged the compose loop makes one optional provider
call for a reply. When that request cannot be fitted to the context budget
the turn's work stands, no provider call is made and the reply is absent.
There is no call record for a call that was never made, so without this row
the stored turn could not be told apart from one whose reply stage ran out of
time.

The record is an ``audit`` row carrying this envelope, like a withheld reply:
every ``audit`` row is excluded from the chat view and from prompt history.
It holds sizes, the configured ceilings and closed-vocabulary names only;
nothing in it is derived from user or model text.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal, TypedDict, get_args

COMPOSER_REPLY_STAGE_REFUSAL_KIND: Final = "composer_reply_stage_refusal"
COMPOSER_REPLY_STAGE_REFUSAL_SCHEMA: Final = "composer.reply-stage-refusal.v1"
# The closed-set reason shared with the refusals that fail a turn.
REPLY_STAGE_REFUSAL_REASON: Final = "context_budget_exceeded"

ContextBudgetCeiling = Literal["tokens", "bytes"]
# The components a compose-loop request is divided into when a refusal names its largest.
ComposeLoopContextBudgetComponent = Literal["system", "catalog", "tools", "state", "current_message", "latest_tool_round", "other"]


class ReplyStageRefusalEnvelope(TypedDict):
    """The stored ``tool_calls`` envelope of a reply-stage refusal's audit row."""

    _kind: str
    schema: str
    reason: str
    ceiling: ContextBudgetCeiling
    estimated_tokens: int
    request_wire_bytes: int
    max_prompt_tokens: int
    max_request_bytes: int
    largest_component: ComposeLoopContextBudgetComponent


@dataclass(frozen=True, slots=True)
class ReplyStageRefusal:
    """What the context budget measured when it refused a reply-stage request."""

    ceiling: ContextBudgetCeiling
    estimated_tokens: int
    request_wire_bytes: int
    max_prompt_tokens: int
    max_request_bytes: int
    largest_component: ComposeLoopContextBudgetComponent

    def __post_init__(self) -> None:
        if self.ceiling not in get_args(ContextBudgetCeiling):
            raise ValueError("reply stage refusal ceiling is outside its closed vocabulary")
        if self.largest_component not in get_args(ComposeLoopContextBudgetComponent):
            raise ValueError("reply stage refusal component is outside its closed vocabulary")
        for name, value in (
            ("estimated_tokens", self.estimated_tokens),
            ("request_wire_bytes", self.request_wire_bytes),
            ("max_prompt_tokens", self.max_prompt_tokens),
            ("max_request_bytes", self.max_request_bytes),
        ):
            if type(value) is not int or value < 0:
                raise ValueError(f"reply stage refusal {name} must be a non-negative exact integer")

    def envelope(self) -> ReplyStageRefusalEnvelope:
        """The ``tool_calls`` envelope of the audit row that records this refusal."""
        return {
            "_kind": COMPOSER_REPLY_STAGE_REFUSAL_KIND,
            "schema": COMPOSER_REPLY_STAGE_REFUSAL_SCHEMA,
            "reason": REPLY_STAGE_REFUSAL_REASON,
            "ceiling": self.ceiling,
            "estimated_tokens": self.estimated_tokens,
            "request_wire_bytes": self.request_wire_bytes,
            "max_prompt_tokens": self.max_prompt_tokens,
            "max_request_bytes": self.max_request_bytes,
            "largest_component": self.largest_component,
        }
