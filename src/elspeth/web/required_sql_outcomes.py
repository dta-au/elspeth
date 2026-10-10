"""Known physical SQL outcomes handed off before deferred cancellation escapes."""

from __future__ import annotations

from asyncio import CancelledError
from dataclasses import dataclass


def _validate_cancellations(cancellations: tuple[CancelledError, ...]) -> None:
    if type(cancellations) is not tuple:
        raise TypeError("Deferred cancellations must be an exact tuple")
    for index, cancellation in enumerate(cancellations):
        if not isinstance(cancellation, CancelledError):
            raise TypeError("Deferred cancellation must retain its original object")
        if any(cancellation is earlier for earlier in cancellations[:index]):
            raise ValueError("Deferred cancellations must be unique by identity")


@dataclass(frozen=True, slots=True)
class RequiredSQLReturned[T]:
    value: T
    deferred_cancellations: tuple[CancelledError, ...]

    def __post_init__(self) -> None:
        _validate_cancellations(self.deferred_cancellations)


@dataclass(frozen=True, slots=True)
class RequiredSQLRaised:
    error: BaseException
    deferred_cancellations: tuple[CancelledError, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.error, BaseException):
            raise TypeError("Required SQL failure must retain its original exception")
        _validate_cancellations(self.deferred_cancellations)


type RequiredSQLFinishOnce[T] = RequiredSQLReturned[T] | RequiredSQLRaised
