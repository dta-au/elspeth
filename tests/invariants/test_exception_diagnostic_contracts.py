"""Bounded discovery plus executable, field-specific diagnostic obligations.

Discovery covers direct constructor stores in three explicitly named modules,
not arbitrary Python dataflow. A new class or field must receive a reviewed
consumer or privacy case; a field read or populated fixture is not that proof.
"""

from typing import Any
from unittest.mock import patch

import pytest

from elspeth.composer_mcp import session
from elspeth.contracts import errors
from elspeth.contracts.audit_evidence import AuditEvidenceBase
from elspeth.core.checkpoint import recovery
from tests.fixtures.exception_diagnostic_cases import DIAGNOSTIC_CASES
from tests.helpers.exception_diagnostics import DiagnosticCase, check_case_partition, discover_exception_shapes


def test_every_scoped_exception_field_has_exactly_one_executable_disposition() -> None:
    shapes = discover_exception_shapes((errors, recovery, session))
    check_case_partition(shapes, DIAGNOSTIC_CASES)


@pytest.mark.parametrize(
    "case",
    DIAGNOSTIC_CASES,
    ids=lambda case: f"{case.exception.__name__}:{','.join(sorted(case.fields)) or 'inherited'}:{case.channel}",
)
def test_exception_diagnostic_contract(case: DiagnosticCase) -> None:
    case.exercise()


_MESSAGE_CASES = tuple(case for case in DIAGNOSTIC_CASES if case.channel == "message")
_SERIALIZER_CASES = tuple(
    case
    for case in DIAGNOSTIC_CASES
    if case.exception in (errors.ZeroEmissionSuccessContractViolation, errors.PassThroughContractViolation)
)


@pytest.mark.parametrize("case", _MESSAGE_CASES, ids=lambda case: f"{case.exception.__name__}:{','.join(sorted(case.fields))}")
@pytest.mark.parametrize("mutation", ("drop", "constant"))
def test_message_obligation_rejects_lost_distinction(case: DiagnosticCase, mutation: str) -> None:
    """Control every message oracle without editing shared production files."""
    observed: list[str] = []
    original = case.exception.__str__

    def capture(error: BaseException) -> str:
        message = original(error)
        observed.append(message)
        return message

    with patch.object(case.exception, "__str__", new=capture):
        case.exercise()
    assert observed, "positive control must reach the actual renderer"

    def mutated(error: BaseException) -> str:
        return "observation discarded" if mutation == "drop" else observed[0]

    with patch.object(case.exception, "__str__", new=mutated), pytest.raises(AssertionError, match="diagnostic fragment"):
        case.exercise()


@pytest.mark.parametrize("case", _SERIALIZER_CASES, ids=lambda case: f"{case.exception.__name__}:{','.join(sorted(case.fields))}")
@pytest.mark.parametrize("mutation", ("drop", "constant"))
def test_serializer_obligation_rejects_lost_distinction(case: DiagnosticCase, mutation: str) -> None:
    assert issubclass(case.exception, AuditEvidenceBase)
    original = case.exception.to_audit_dict
    observed: list[dict[str, Any]] = []

    def capture(error: AuditEvidenceBase) -> dict[str, Any]:
        payload = dict(original(error))
        observed.append(payload)
        return payload

    with patch.object(case.exception, "to_audit_dict", new=capture):
        case.exercise()
    assert observed, "positive control must reach the actual serializer"
    assert len(case.fields) == 1
    field = next(iter(case.fields))

    def mutated(error: AuditEvidenceBase) -> dict[str, Any]:
        payload = dict(original(error))
        if mutation == "drop":
            del payload[field]
        else:
            payload[field] = observed[0][field]
        return payload

    with patch.object(case.exception, "to_audit_dict", new=mutated), pytest.raises(AssertionError, match="diagnostic field"):
        case.exercise()


@pytest.mark.parametrize(
    "case, private_value",
    [
        (
            case,
            "private-row-alpha@example.invalid"
            if case.exception is errors.TypeMismatchViolation
            else "private-context-alpha@example.invalid",
        )
        for case in DIAGNOSTIC_CASES
        if case.channel == "private"
    ],
    ids=lambda value: value.exception.__name__ if isinstance(value, DiagnosticCase) else "leak",
)
def test_private_obligation_rejects_disclosure(case: DiagnosticCase, private_value: str) -> None:
    case.exercise()

    def leaking(error: BaseException) -> str:
        return private_value

    with patch.object(case.exception, "__str__", new=leaking), pytest.raises(AssertionError, match="private diagnostic leaked"):
        case.exercise()
