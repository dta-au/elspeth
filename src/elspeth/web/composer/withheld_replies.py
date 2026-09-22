"""Durable record of composer model replies that were not published.

Several compose-loop exits decline to publish the model's own words: a repair
gate supersedes a reply the model believed was final, and the advisor-repair
cohort replaces prose written after internal advisor findings entered the
model's context. ``ComposerLLMCall`` stores request hashes and token counts but
no response text, so without this row an unpublished reply exists nowhere.

The record is an ``audit`` row carrying this envelope. It is deliberately NOT a
``composer_control_message``: ``replay_composer_control_message`` decodes
control rows back into provider context, and a withheld reply must never
re-enter it — advisor-cohort prose may quote or rebut findings the user never
saw. Every ``audit`` row is excluded from the chat view and from prompt
history unless that decoder claims it, so a distinct kind keeps the text
recoverable without rendering or replaying it.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Final, Literal

COMPOSER_WITHHELD_REPLY_KIND: Final = "composer_withheld_reply"
COMPOSER_WITHHELD_REPLY_SCHEMA: Final = "composer.withheld-reply.v1"

WithheldReplyOrigin = Literal[
    "repair_gate_superseded",
    "advisor_repair_tool_turn",
    "advisor_repair_terminal",
    "advisor_terminal_block",
    "compose_deadline_expired",
    "planner_prose_unadmitted",
]


@dataclass(frozen=True, slots=True)
class WithheldReply:
    """One unpublished reply staged by a producer that holds no session.

    The pipeline planner runs without a session write context, so it stages
    the words on the request's ``BufferingRecorder`` and the caller that does
    hold one persists them inside the planner audit cohort.

    The guided lane does not use this: its audit cohort is hash-only by design
    (``ComposerChatTurn.assistant_message_hash``), so a guided planning
    request's staged replies are never persisted.
    """

    origin: WithheldReplyOrigin
    content: str

    def __post_init__(self) -> None:
        if type(self.content) is not str or not self.content.strip():
            raise ValueError(f"{self.origin} withheld reply content must be a non-blank string")


def withheld_reply_envelope(origin: WithheldReplyOrigin, content: str) -> dict[str, str]:
    """Return bounded provenance for one unpublished model reply."""

    if type(content) is not str or not content:
        raise ValueError(f"{origin} withheld reply content must be a non-empty string")
    return {
        "_kind": COMPOSER_WITHHELD_REPLY_KIND,
        "schema": COMPOSER_WITHHELD_REPLY_SCHEMA,
        "origin": origin,
        "content_hash": hashlib.sha256(content.encode("utf-8")).hexdigest(),
    }
