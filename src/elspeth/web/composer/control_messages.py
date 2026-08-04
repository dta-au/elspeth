"""Durable provider-visible composer control-message contracts.

Control messages are stored as audit rows so they remain operator-generated
evidence rather than being misattributed to the human user.  The envelope
records the provider role needed to reconstruct the exact outbound message.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from typing import Any

from elspeth.contracts.errors import AuditIntegrityError

COMPOSER_CONTROL_MESSAGE_KIND = "composer_control_message"
COMPOSER_CONTROL_MESSAGE_SCHEMA = "composer.control-message.v1"
_ANTI_ANCHOR_ORIGIN = "anti_anchor"
_ADVISOR_SIGNOFF_WITHHELD_ORIGIN = "advisor_signoff_withheld"
# Closed origin vocabulary: every registered origin pins the exact provider
# role its replayed message carries. An origin outside this mapping (or a
# role that disagrees with it) fails replay closed — see
# ``replay_composer_control_message``.
_CONTROL_ORIGIN_PROVIDER_ROLES = {
    _ANTI_ANCHOR_ORIGIN: "user",
    _ADVISOR_SIGNOFF_WITHHELD_ORIGIN: "user",
}
_ANTI_ANCHOR_PROVIDER_ROLE = _CONTROL_ORIGIN_PROVIDER_ROLES[_ANTI_ANCHOR_ORIGIN]
_CONTROL_ENVELOPE_KEYS = frozenset({"_kind", "schema", "origin", "provider_role", "content_hash"})


def _content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _control_envelope(origin: str, content: str) -> dict[str, str]:
    if type(content) is not str or not content:
        raise ValueError(f"{origin} control content must be a non-empty string")
    return {
        "_kind": COMPOSER_CONTROL_MESSAGE_KIND,
        "schema": COMPOSER_CONTROL_MESSAGE_SCHEMA,
        "origin": origin,
        "provider_role": _CONTROL_ORIGIN_PROVIDER_ROLES[origin],
        "content_hash": _content_hash(content),
    }


def anti_anchor_control_envelope(content: str) -> dict[str, str]:
    """Return bounded provenance for one redacted anti-anchor hint."""

    return _control_envelope(_ANTI_ANCHOR_ORIGIN, content)


def advisor_signoff_withheld_control_envelope(content: str) -> dict[str, str]:
    """Return bounded provenance for one advisor-withheld disclosure.

    elspeth-2306940c70: the END advisor gate's terminal withhold publishes
    fixed backend copy with the model's prose withheld, so the turn replays
    into later model context as an empty assistant message. This envelope
    makes the non-completion durable and provider-visible: the disclosure
    replays as a user-role control message so the next turn's model cannot
    read the withheld turn as silent compliance.
    """

    return _control_envelope(_ADVISOR_SIGNOFF_WITHHELD_ORIGIN, content)


def replay_composer_control_message(
    *,
    stored_role: str,
    writer_principal: str,
    content: str,
    tool_calls: Sequence[Mapping[str, Any]] | None,
) -> dict[str, str] | None:
    """Decode a durable control row into its exact provider message.

    Non-control rows return ``None``.  A row that claims the control-message
    kind but violates the closed schema fails closed: silently dropping or
    reinterpreting it would make historical provider context unverifiable.
    """

    if not tool_calls:
        return None
    claimed = [item for item in tool_calls if item.get("_kind") == COMPOSER_CONTROL_MESSAGE_KIND]
    if not claimed:
        return None
    if len(tool_calls) != 1 or len(claimed) != 1:
        raise AuditIntegrityError("composer control audit row must contain exactly one control envelope")
    envelope = claimed[0]
    if frozenset(envelope) != _CONTROL_ENVELOPE_KEYS:
        raise AuditIntegrityError("composer control audit envelope fields do not match the closed schema")
    if stored_role != "audit" or writer_principal != "compose_loop":
        raise AuditIntegrityError("composer control message lacks system-origin audit attribution")
    if envelope["schema"] != COMPOSER_CONTROL_MESSAGE_SCHEMA:
        raise AuditIntegrityError("composer control audit envelope has an unsupported schema")
    expected_role = _CONTROL_ORIGIN_PROVIDER_ROLES.get(envelope["origin"])
    if expected_role is None or envelope["provider_role"] != expected_role:
        raise AuditIntegrityError("composer control audit envelope has an unsupported origin or provider role")
    if envelope["content_hash"] != _content_hash(content):
        raise AuditIntegrityError("composer control audit content hash does not match stored content")
    return {"role": expected_role, "content": content}
