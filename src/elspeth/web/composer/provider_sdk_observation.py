"""Process-local V4 SDK failure observations issued at the physical await.

The gateway's private scope surrounds only the actual SDK call. Its issued
object is bound by identity to original custody and attempt; neither a stored
hold nor a copied carrier supplies entry or failure authority.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from weakref import WeakKeyDictionary, ref

from httpx import TimeoutException, TransportError
from openai import APITimeoutError, OpenAIError

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.coordination.quota_authority import ProviderAttempt
from elspeth.web.sessions.continuation_sdk_terminal import SdkTerminalObservation

if TYPE_CHECKING:
    from elspeth.web.composer.provider_quota import ProviderCallCustody


@dataclass(frozen=True, slots=True, weakref_slot=True, eq=False)
class V4SdkFailureObservation:
    """An issued original SDK exception, not a serialized exception payload."""

    attempt: ProviderAttempt
    observation: SdkTerminalObservation
    original: BaseException = field(repr=False)


@dataclass(frozen=True, slots=True)
class _OriginalSource:
    custody: ref[ProviderCallCustody]
    attempt: ProviderAttempt
    observation: SdkTerminalObservation
    original_identity: int


_ISSUED: WeakKeyDictionary[V4SdkFailureObservation, _OriginalSource] = WeakKeyDictionary()


@contextmanager
def _observe_v4_sdk_await(custody: ProviderCallCustody) -> Iterator[None]:
    """Gateway-only scope, entered after mark_sdk_entered and before SDK await.

    No ordinary parser, recorder, SQL, retention or projection work belongs in
    this scope. An unrecognized exception propagates without an issued result.
    """
    from elspeth.web.composer.provider_quota import ProviderCallCustody

    if type(custody) is not ProviderCallCustody or not custody.requires_v4_original_result() or custody.has_v4_sdk_return():
        raise AuditIntegrityError("V4 SDK observation requires original entered custody")
    attempt = custody.attempt
    if type(attempt) is not ProviderAttempt or custody.v4_sdk_failure is not None:
        raise AuditIntegrityError("V4 SDK observation changed or repeated its original attempt")
    try:
        yield
    except (OpenAIError, TransportError, TimeoutError, asyncio.CancelledError) as original:
        if isinstance(original, (APITimeoutError, TimeoutException, TimeoutError)):
            category: SdkTerminalObservation = "provider_timeout"
        elif isinstance(original, asyncio.CancelledError):
            category = "no_observed_result"
        else:
            category = "provider_failure"
        if custody.attempt is not attempt or not custody.requires_v4_original_result():
            raise AuditIntegrityError("V4 SDK failure changed its original physical custody") from original
        observation = V4SdkFailureObservation(attempt, category, original)
        _ISSUED[observation] = _OriginalSource(ref(custody), attempt, category, id(original))
        custody.v4_sdk_failure = observation
        raise


def current_v4_sdk_failure(custody: ProviderCallCustody) -> V4SdkFailureObservation | None:
    """Read only a source-issued failure; absence never proves no SDK entry."""
    observation = custody.v4_sdk_failure
    if observation is not None:
        require_original_v4_sdk_failure(observation, custody=custody)
    return observation


def require_original_v4_sdk_failure(observation: V4SdkFailureObservation, *, custody: ProviderCallCustody) -> None:
    """Reject copied witnesses, foreign custody and changed original fields."""
    from elspeth.web.composer.provider_quota import ProviderCallCustody

    if type(observation) is not V4SdkFailureObservation or type(custody) is not ProviderCallCustody:
        raise AuditIntegrityError("V4 SDK failure requires its original issued observation")
    source = _ISSUED.get(observation)
    if (
        source is None
        or source.custody() is not custody
        or source.attempt is not observation.attempt
        or source.observation != observation.observation
        or source.original_identity != id(observation.original)
        or custody.attempt is not source.attempt
        or custody.v4_sdk_failure is not observation
        or not custody.requires_v4_original_result()
        or custody.has_v4_sdk_return()
    ):
        raise AuditIntegrityError("V4 SDK failure lost its original issued custody and attempt")


def forget_v4_sdk_failure(observation: V4SdkFailureObservation, *, custody: ProviderCallCustody) -> None:
    """Consume live observation only after the caller proves terminal commit."""
    require_original_v4_sdk_failure(observation, custody=custody)
    custody.v4_sdk_failure = None
    del _ISSUED[observation]


def failure_contains_original_sdk_observation(failure: BaseException, observation: V4SdkFailureObservation) -> bool:
    """Match exact original exception identity through bounded groups/causes."""
    pending = [failure]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if current is observation.original:
            return True
        if id(current) in seen:
            continue
        seen.add(id(current))
        if len(seen) > 128:
            raise AuditIntegrityError("SDK failure ancestry exceeds its bounded original source join")
        if isinstance(current, BaseExceptionGroup):
            pending.extend(current.exceptions)
        if current.__cause__ is not None:
            pending.append(current.__cause__)
    return False
