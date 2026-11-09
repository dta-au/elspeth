"""Closed failure vocabulary shared by every acceptance facade.

Moved verbatim from ``_aws_ecs_acceptance/contracts.py``. The error codes and
step names are static and never derived from runtime data; the step
contextvar records the coarse phase that was executing when a failure
surfaced so the operator-facing envelope can name it.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from contextvars import ContextVar

# Closed error-code vocabulary for the operator-facing failure envelope.
# Every non-check acceptance failure projects onto exactly one of these
# static codes; unknown exceptions map to ``acceptance_internal``.  Codes
# never carry provider or server response content.
ACCEPTANCE_ERROR_CODES = frozenset(
    {
        "state_file_unwritable",
        "state_file_unreadable",
        "state_file_untrusted",
        "state_file_too_large",
        "state_file_invalid",
        "ca_unreadable",
        "connection_failed",
        "request_timeout",
        "unexpected_http_status",
        "response_too_large",
        "response_shape_invalid",
        "cross_origin_response",
        "input_invalid",
        "operator_telemetry_failed",
        "acceptance_internal",
    }
)

# Closed step vocabulary: the coarse acceptance phase that was executing
# when a failure surfaced.  Step names are static and never derived from
# runtime data.
ACCEPTANCE_STEPS = frozenset(
    {
        "env_validate",
        "client_setup",
        "register",
        "login",
        "capture_fetch",
        "verify_fetch",
        "state_persist",
        "state_load",
    }
)

_ACCEPTANCE_STEP: ContextVar[str | None] = ContextVar("elspeth_acceptance_step", default=None)


@contextlib.contextmanager
def acceptance_step(name: str) -> Iterator[None]:
    """Tag the executing acceptance step for the failure envelope.

    On success the previous step is restored; on failure the innermost
    step deliberately stays set so the top-level envelope can report it.
    """

    if name not in ACCEPTANCE_STEPS:
        raise ValueError(f"unknown acceptance step: {name}")
    token = _ACCEPTANCE_STEP.set(name)
    yield
    _ACCEPTANCE_STEP.reset(token)


def current_acceptance_step() -> str | None:
    """Return the innermost recorded acceptance step, if any."""

    return _ACCEPTANCE_STEP.get()


def reset_acceptance_step() -> None:
    """Clear any step left set by a previously failed invocation."""

    _ACCEPTANCE_STEP.set(None)


def _closed_error_code(error_code: object) -> str:
    return error_code if type(error_code) is str and error_code in ACCEPTANCE_ERROR_CODES else "acceptance_internal"


class AcceptanceInputError(RuntimeError):
    """Static failure raised before an acceptance request is sent."""

    error_code = "input_invalid"
    status: int | None = None


class AcceptanceHttpError(RuntimeError):
    """Static HTTP failure that never includes a response or exception body."""

    def __init__(self, message: str, *, error_code: str = "connection_failed", status: int | None = None) -> None:
        super().__init__(message)
        self.error_code = _closed_error_code(error_code)
        self.status = status


class AcceptanceStateError(RuntimeError):
    """Static protected-state failure that never includes file content."""

    def __init__(self, message: str, *, error_code: str = "state_file_invalid") -> None:
        super().__init__(message)
        self.error_code = _closed_error_code(error_code)
        self.status: int | None = None


class AcceptanceCheckError(RuntimeError):
    """A static named acceptance check failure safe for operator output."""

    def __init__(
        self,
        check: str,
        *,
        missing: tuple[str, ...] | None = None,
        cause_class: str | None = None,
        cause_fields: tuple[str, ...] | None = None,
    ) -> None:
        super().__init__(f"acceptance check failed: {check}")
        self.check = check
        self.missing = missing
        self.cause_class = cause_class
        self.cause_fields = cause_fields
