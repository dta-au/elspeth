"""Fork diagnostic admission preserves owned facts without rendering errors."""

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.sessions.routes.sessions import _bounded_fork_diagnostics, _fork_failure_diagnostic


class _UnrenderableFailure(RuntimeError):
    def __str__(self) -> str:
        raise AssertionError("raw exception must not be rendered")


def test_static_settlement_reason_is_preserved_but_unknown_text_is_not() -> None:
    safe = "Guided fork settlement parent is missing"
    assert _fork_failure_diagnostic(AuditIntegrityError(safe), phase="settlement") == safe
    error = AuditIntegrityError("database password=private")
    error.add_note("provider-secret")
    assert _fork_failure_diagnostic(error, phase="staging") == "ForkFailure[AuditIntegrityError]: phase=staging"
    assert _fork_failure_diagnostic(_UnrenderableFailure(), phase="settlement") == "ForkFailure[_UnrenderableFailure]: phase=settlement"


def test_diagnostic_bounds_retain_primary_reason_and_explicit_omission_count() -> None:
    notes = ["primary", *[f"cleanup-{index}" for index in range(40)]]
    bounded = _bounded_fork_diagnostics(notes)
    assert len(bounded) == 32
    assert bounded[:31] == tuple(notes[:31])
    assert bounded[-1] == "DiagnosticsOmitted: 10 additional notes"
    assert _bounded_fork_diagnostics(["x" * 600]) == ("x" * 497 + "... [truncated]",)
