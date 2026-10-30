"""Composer-Operation-Version negotiation for the durable Composer routes.

The request header selects the lifecycle of a new admission only. For an
existing operation the stored row's ``lifecycle_version`` selects the
projection, and the header says which projections the client can read
(header 2 reads both lifecycles; header 1 or none reads only lifecycle 1).
A missing header means 1.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final, Literal

type ComposerOperationVersion = Literal[1, 2]

COMPOSER_OPERATION_VERSION_HEADER: Final = "Composer-Operation-Version"
COMPOSER_OPERATION_VERSION_UNSUPPORTED: Final = "composer_operation_version_unsupported"
COMPOSER_OPERATION_VERSION_REQUIRED: Final = "composer_operation_version_required"


class ComposerOperationVersionUnsupported(ValueError):
    """The request named a Composer-Operation-Version this server does not speak."""


def parse_composer_operation_version(values: Sequence[str]) -> ComposerOperationVersion:
    """Return the client's version from every occurrence of the header.

    Exactly one occurrence whose value, without surrounding whitespace, is
    ``1`` or ``2`` is accepted. No occurrence means 1. Anything else,
    including a repeated header or a list value, is refused.
    """
    if len(values) == 0:
        return 1
    if len(values) != 1:
        raise ComposerOperationVersionUnsupported("Composer-Operation-Version must appear at most once")
    match values[0].strip():
        case "1":
            return 1
        case "2":
            return 2
        case _:
            raise ComposerOperationVersionUnsupported("Composer-Operation-Version must be 1 or 2")


def can_read(client_version: ComposerOperationVersion, row_lifecycle: ComposerOperationVersion) -> bool:
    """A v2 reader reads both lifecycles; a v1 reader reads only lifecycle 1."""
    return row_lifecycle == 1 or client_version == 2


def version_header(version: ComposerOperationVersion) -> dict[str, str]:
    return {COMPOSER_OPERATION_VERSION_HEADER: str(version)}


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
