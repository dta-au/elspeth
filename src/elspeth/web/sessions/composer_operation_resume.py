"""``Last-Event-ID`` resume parsing for the durable Composer operation stream."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

COMPOSER_OPERATION_RESUME_TOKEN_INVALID: Final = "composer_operation_resume_token_invalid"
_MAX_REVISION: Final = 2**63 - 1


class ComposerOperationResumeTokenInvalid(ValueError):
    """``Last-Event-ID`` is not a single non-negative revision."""


def parse_last_event_id(values: Sequence[str]) -> int | None:
    """The revision a reconnecting client last accepted, or None.

    Resume is state transfer: the server always sends the current status
    first, so the value is informational (it is logged). A malformed value is
    still refused, so the header stays a closed contract.
    """
    if len(values) == 0:
        return None
    if len(values) != 1:
        raise ComposerOperationResumeTokenInvalid("Last-Event-ID must appear at most once")
    value = values[0].strip()
    if not (1 <= len(value) <= 19 and value.isascii() and value.isdigit()):
        raise ComposerOperationResumeTokenInvalid("Last-Event-ID must be a revision number")
    revision = int(value)
    if revision > _MAX_REVISION:
        raise ComposerOperationResumeTokenInvalid("Last-Event-ID is out of range")
    return revision
