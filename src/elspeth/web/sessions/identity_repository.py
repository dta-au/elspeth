"""Record types and row parsers of the ``identities`` substrate.

``identities`` is CURRENT STATE, not history: the record of what happened at
each login lives in the Landscape ``auth_events`` trail. Nothing here may be
read as an audit log.

Every read and write of the substrate is a method of
:class:`elspeth.web.coordination.identity_authority.RepositoryIdentityAuthority`
(``ensure_identity`` resolves ``(provider, subject)`` to the one row that
identifies a person, creating it on first sight; ``read_identity`` is the
authorisation read behind every authenticate and refresh; ``retire_identity``
frees a deleted local credential's binding). This module holds what those
methods return and how a stored row is parsed into it -- it takes no engine
and opens no connection, so a caller holding only this module cannot reach
the tables.

ADMISSION IS NOT AUTHENTICATION
-------------------------------
A row here says a person has been seen. Whether they may do anything is
``access_state``, and D12 puts every first sight behind an administrator's
approval by default. The one documented relaxation is a local deployment whose
``registration_mode`` is ``open``: it has already declared that anyone may
admit themselves, so gating the people who did so before this table existed
would refuse the cohort while admitting every newcomer instantly. That
decision is made by the CALLER and arrives as the authority's ``activate``;
neither module reads settings.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Final, cast, get_args

from sqlalchemy import Row

from elspeth.contracts.auth import IdentityProviderType
from elspeth.web.sessions.models import identities_table


@dataclass(frozen=True, slots=True)
class IdentityRecord:
    """The authorisation-relevant state of one identity row."""

    identity_id: str
    provider: IdentityProviderType
    subject: str
    username: str
    access_state: str

    @property
    def is_active(self) -> bool:
        return self.access_state == "active"


@dataclass(frozen=True, slots=True)
class EnsureIdentityOutcome:
    """What ``RepositoryIdentityAuthority.ensure_identity`` did, so the caller can audit it.

    ``activated_now`` is the caller's cue to write the ``identity_activated``
    and ``quota_set`` audit pair. It is true only on the transition, never on
    a login by an already-active identity, so a returning user does not
    manufacture an activation event on every visit.
    """

    record: IdentityRecord
    created: bool
    activated_now: bool
    # Whether a quota_policies row was actually written. The container
    # defaults are independently optional, so an activation can legitimately
    # write none -- and an audit event claiming an allowance that no row
    # records would tell an auditor the opposite of the truth.
    quota_written: bool


# Write the ``identity_activated`` + ``quota_set`` pair for a first admission.
# Takes ``quota_written`` because the two facts must agree: a ``quota_set``
# event for an activation that wrote no policy row asserts an allowance that
# does not exist, which is worse than recording no allowance at all.
RecordAdmission = Callable[[str, str, bool], None]


class IdentityRowCorruptionError(RuntimeError):
    """A stored identity row does not satisfy its own declared vocabulary.

    Raised rather than coerced. The column carries a CHECK constraint, so
    reaching this means the constraint was dropped, the row was written by
    something that bypassed it, or the store is not the store we think it is.
    Every one of those is a reason to refuse the request, not to guess.
    """


_PROVIDER_VALUES: Final = frozenset(get_args(IdentityProviderType))
_ACCESS_STATES: Final = frozenset({"pending", "active", "disabled"})


def _parsed_provider(value: object, *, identity_id: str) -> IdentityProviderType:
    """Narrow a stored provider string to the closed contract type.

    Validated rather than cast. The CHECK constraint and this function derive
    from the SAME ``IdentityProviderType``, so widening the contract widens
    both together and a value admitted by one is admitted by the other.
    """
    if type(value) is not str or value not in _PROVIDER_VALUES:
        raise IdentityRowCorruptionError(f"identity {identity_id} has provider {value!r}, which is not a known provider")
    return cast("IdentityProviderType", value)


def _parsed_access_state(value: object, *, identity_id: str) -> str:
    if type(value) is not str or value not in _ACCESS_STATES:
        raise IdentityRowCorruptionError(f"identity {identity_id} has access_state {value!r}, which is not a known state")
    return value


def _parsed_text(value: object, *, identity_id: str, column: str) -> str:
    if type(value) is not str:
        raise IdentityRowCorruptionError(f"identity {identity_id} has a non-text {column}")
    return value


def _row_to_record(row: Row[Any]) -> IdentityRecord:
    identity_id = row.identity_id
    if type(identity_id) is not str:
        raise IdentityRowCorruptionError("an identity row has a non-text identity_id")
    return IdentityRecord(
        identity_id=identity_id,
        provider=_parsed_provider(row.provider, identity_id=identity_id),
        subject=_parsed_text(row.subject, identity_id=identity_id, column="subject"),
        username=_parsed_text(row.username, identity_id=identity_id, column="username"),
        access_state=_parsed_access_state(row.access_state, identity_id=identity_id),
    )


_IDENTITY_COLUMNS = (
    identities_table.c.identity_id,
    identities_table.c.provider,
    identities_table.c.subject,
    identities_table.c.username,
    identities_table.c.access_state,
)
