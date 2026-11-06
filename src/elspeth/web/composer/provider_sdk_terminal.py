"""Original SDK failure settlement and privately issued exceptional projections."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING
from weakref import WeakKeyDictionary

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.provider_sdk_observation import current_v4_sdk_failure, failure_contains_original_sdk_observation
from elspeth.web.sessions.continuation_sdk_tail import SdkTerminalFamily
from elspeth.web.sessions.continuation_sdk_terminal_result import SdkTerminalFailure, VerifiedSdkTerminal

if TYPE_CHECKING:
    from elspeth.web.composer.provider_quota import ProviderCallCustody, V4SdkTerminalCommitted


@dataclass(frozen=True, slots=True)
class _IssuedFailure:
    verified: VerifiedSdkTerminal
    family: SdkTerminalFamily
    original_identity: int
    control_identity: int
    finalization_identity: int
    cancellation_identities: tuple[int, ...]


_ISSUED: WeakKeyDictionary[SdkTerminalFailure, _IssuedFailure] = WeakKeyDictionary()


def _issue_sdk_terminal_failure(
    *, custody: ProviderCallCustody, original: BaseException, control: V4SdkTerminalCommitted
) -> SdkTerminalFailure:
    from elspeth.web.composer.provider_quota import V4SdkTerminalCommitted
    from elspeth.web.sessions.continuation_sdk_dispatch import sdk_terminal_family_for_custody

    if (
        type(control) is not V4SdkTerminalCommitted
        or custody.v4_sdk_terminal_result is not control.finalization
        or custody.v4_sdk_terminal_failure is not None
    ):
        raise AuditIntegrityError("SDK companion lost its original committed custody")
    finalized = control.finalization
    family = sdk_terminal_family_for_custody(custody)
    result = SdkTerminalFailure(finalized.verified, family, original, finalized.deferred_cancellations)
    _ISSUED[result] = _IssuedFailure(
        result.verified, family, id(original), id(control), id(finalized), tuple(id(item) for item in result.deferred_cancellations)
    )
    custody.v4_sdk_terminal_failure = result
    return result


def require_sdk_terminal_failure(result: SdkTerminalFailure) -> None:
    """Require the same privately issued exceptional result after custody unwinds."""
    from elspeth.web.composer.provider_quota import V4SdkTerminalCommitted

    if type(result) is not SdkTerminalFailure:
        raise AuditIntegrityError("SDK exceptional result requires its nominal issued carrier")
    original = _ISSUED.get(result)
    cause = result.original.__cause__
    if (
        original is None
        or result.verified is not original.verified
        or result.family is not original.family
        or id(result.original) != original.original_identity
        or type(cause) is not V4SdkTerminalCommitted
        or id(cause) != original.control_identity
        or id(cause.finalization) != original.finalization_identity
        or cause.finalization.verified is not result.verified
        or tuple(id(item) for item in result.deferred_cancellations) != original.cancellation_identities
    ):
        raise AuditIntegrityError("SDK exceptional result changed its privately issued original failure")


def capture_sdk_terminal_failure(failure: BaseException, *, custody: ProviderCallCustody) -> SdkTerminalFailure | None:
    """Owners may release only the exact companion observed by the SDK scope."""
    from elspeth.web.composer.provider_quota import V4SdkTerminalCommitted

    result = custody.v4_sdk_terminal_failure
    if result is None:
        return None
    cause = failure.__cause__
    if (
        result.original is not failure
        or type(cause) is not V4SdkTerminalCommitted
        or cause.finalization is not custody.v4_sdk_terminal_result
    ):
        raise AuditIntegrityError("SDK exceptional capture changed its original cause or custody")
    require_sdk_terminal_failure(result)
    return result


async def finalize_sdk_failure_exception(custody: ProviderCallCustody, failure: BaseException) -> None:
    """Close only the original observed SDK failure; preserve owned companions."""
    from elspeth.web.composer.provider_quota import V4SdkTerminalCommitted

    observed = current_v4_sdk_failure(custody)
    if observed is None or not custody.has_v4_sdk_finalizer() or not failure_contains_original_sdk_observation(failure, observed):
        return
    direct = failure is observed.original or (type(failure) is TimeoutError and failure.__cause__ is observed.original)
    finalized = await custody.finalize_v4_sdk_failure(observed, preserve_sdk_cancellation=isinstance(failure, asyncio.CancelledError))
    control = V4SdkTerminalCommitted(finalized)
    if not direct:
        _issue_sdk_terminal_failure(custody=custody, original=failure, control=control)
        raise failure from control
    raise control from failure


@asynccontextmanager
async def sdk_terminal_failure_scope(custody: ProviderCallCustody) -> AsyncIterator[None]:
    """Outside required provider audit: never classify audit/SQL faults as SDK failures."""
    try:
        yield
    except BaseException as failure:
        await finalize_sdk_failure_exception(custody, failure)
        raise
