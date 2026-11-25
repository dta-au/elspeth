"""Find a Tier-1 error carried in another exception's chain, and raise it as itself.

A coordination signal (a lost claim lease, a lost run membership or
leadership, an eviction) can be raised while a Tier-1 error is propagating,
leaving the Tier-1 error only in the signal's cause or context chain. The
callers here find that error and raise it in the signal's place, so it is
reported as the audit-integrity or framework failure it is.
"""

from __future__ import annotations

from typing import NoReturn

import elspeth.contracts.errors as contract_errors

__all__ = ["raise_keeping_own_links", "tier_1_error_in_chain"]


def tier_1_error_in_chain(error: BaseException) -> BaseException | None:
    """Return a Tier-1 error from ``error``'s cause and context chain, or None.

    Each exception is walked once, so a chain that loops back on itself
    still ends.
    """
    seen: set[int] = set()
    pending: list[BaseException] = [error]
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, contract_errors.TIER_1_ERRORS):
            return current
        pending.extend(linked for linked in (current.__cause__, current.__context__) if linked is not None)
    return None


def raise_keeping_own_links(error: BaseException) -> NoReturn:
    """Raise ``error`` with its own cause, context and suppression flag as they were.

    Raised while another exception is being handled, ``error`` would take
    that exception as its context. The links are restored as it propagates,
    so it leaves with its chain as it was first raised, and any exception
    raised later during its propagation takes it, chain and all, as context.
    """
    links = (error.__cause__, error.__context__, error.__suppress_context__)
    try:
        raise error
    finally:
        error.__cause__, error.__context__, error.__suppress_context__ = links
