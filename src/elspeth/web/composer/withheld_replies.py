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
from typing import Final, Literal

COMPOSER_WITHHELD_REPLY_KIND: Final = "composer_withheld_reply"
COMPOSER_WITHHELD_REPLY_SCHEMA: Final = "composer.withheld-reply.v1"

WithheldReplyOrigin = Literal[
    "repair_gate_superseded",
    "advisor_repair_tool_turn",
    "advisor_repair_terminal",
    "advisor_terminal_block",
]


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
