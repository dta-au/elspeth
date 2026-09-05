"""Shared receipt admission: the descriptor-generalised exec envelope and the schema-parametrised budget validator.

The moved validators keep their ECS-owned tests (``test_receipt_contracts.py``,
unedited). The two functions that were generalised rather than moved carry
their raising-shape tests here; the ``@trust_boundary`` decorators in
``receipt_validation.py`` name these functions as ``test_ref``.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

import pytest

from elspeth.web._acceptance_common.errors import AcceptanceCheckError
from elspeth.web._acceptance_common.receipt_validation import (
    ExecReceiptDescriptor,
    validate_connection_budget_receipt,
    validate_exec_receipt_schema,
)

_SCHEMA = "elspeth.postgres-flexible-connection-budget.v1"


def _budget_details(schema: str = _SCHEMA) -> dict[str, object]:
    return {
        "schema": schema,
        "acceptance_run_id_sha256": "b" * 64,
        "cluster_id_sha256": "a" * 64,
        "window_start": "2026-07-14T01:00:00Z",
        "window_end": "2026-07-14T01:10:00Z",
        "period_seconds": 60,
        "expected_points": 10,
        "points": [{"timestamp": f"2026-07-14T01:{minute:02d}:00Z", "count": 8.0} for minute in range(10)],
        "high_water": 8.0,
        "max_connections": 100,
        "approved_budget": 20,
        "safety_margin": 10,
        "ok": True,
    }


def _accept_any(_details: Mapping[str, object]) -> None:
    return None


def _descriptor(subject_field: str = "replica_binding_sha256") -> ExecReceiptDescriptor:
    return ExecReceiptDescriptor(
        provider="azure",
        subject_field=subject_field,
        detail_validators=MappingProxyType({"verify-doctor-job": _accept_any, "verify-connection-budget": _accept_any}),
    )


def _envelope(descriptor: ExecReceiptDescriptor, **overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "version": 1,
        "check": "verify-doctor-job",
        "ok": True,
        "candidate_sha": "c" * 40,
        descriptor.subject_field: "d" * 64,
        "scenario_id": "A",
        "details": {},
    }
    payload.update(overrides)
    return payload


def test_connection_budget_receipt_rejects_short_point_series() -> None:
    details = _budget_details()
    details["points"] = details["points"][:9]  # type: ignore[index]
    with pytest.raises(AcceptanceCheckError, match="receipt_store_schema"):
        validate_connection_budget_receipt(details, schema_id=_SCHEMA)


def test_connection_budget_receipt_binds_the_callers_schema_id() -> None:
    assert validate_connection_budget_receipt(_budget_details(), schema_id=_SCHEMA) is not None
    with pytest.raises(AcceptanceCheckError, match="receipt_store_schema"):
        validate_connection_budget_receipt(_budget_details("elspeth.rds-connection-budget.v3"), schema_id=_SCHEMA)


@pytest.mark.parametrize(
    ("field", "value"),
    [("high_water", True), ("max_connections", 100.0), ("approved_budget", "20"), ("ok", 1)],
)
def test_connection_budget_receipt_rejects_type_confusion(field: str, value: object) -> None:
    details = _budget_details()
    details[field] = value
    with pytest.raises(AcceptanceCheckError, match="receipt_store_schema"):
        validate_connection_budget_receipt(details, schema_id=_SCHEMA)


def test_connection_budget_receipt_binds_run_and_cluster_identities() -> None:
    with pytest.raises(AcceptanceCheckError, match="receipt_store_binding"):
        validate_connection_budget_receipt(_budget_details(), schema_id=_SCHEMA, expected_acceptance_run_id_sha256="f" * 64)
    with pytest.raises(AcceptanceCheckError, match="receipt_store_binding"):
        validate_connection_budget_receipt(_budget_details(), schema_id=_SCHEMA, expected_cluster_id_sha256="f" * 64)


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(None, id="none"),
        pytest.param([], id="list"),
        pytest.param("ELSPETH_ACCEPTANCE_RECEIPT_V1:", id="string"),
        pytest.param({}, id="empty-dict"),
        pytest.param({**_envelope(_descriptor()), "extra": 1}, id="open-envelope"),
        pytest.param({key: value for key, value in _envelope(_descriptor()).items() if key != "details"}, id="missing-details"),
        pytest.param(_envelope(_descriptor("task_arn_sha256")), id="wrong-providers-subject-field"),
    ],
)
def test_exec_receipt_schema_rejects_non_dict_and_open_envelopes(payload: object) -> None:
    with pytest.raises(AcceptanceCheckError, match="exec_receipt_schema"):
        validate_exec_receipt_schema(payload, descriptor=_descriptor())


def test_exec_receipt_schema_accepts_the_descriptors_envelope_and_dispatches_the_kind() -> None:
    seen: list[Mapping[str, object]] = []

    def record(details: Mapping[str, object]) -> None:
        seen.append(details)

    descriptor = ExecReceiptDescriptor(
        provider="azure", subject_field="replica_binding_sha256", detail_validators=MappingProxyType({"verify-doctor-job": record})
    )
    payload = _envelope(descriptor, details={"job": "ok"})
    assert validate_exec_receipt_schema(payload, descriptor=descriptor) is payload
    assert seen == [{"job": "ok"}]


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"version": 2}, "exec_receipt"),
        ({"version": True}, "exec_receipt"),
        ({"ok": False}, "exec_receipt"),
        ({"check": "verify-s3"}, "exec_receipt_schema"),
        ({"check": 7}, "exec_receipt_schema"),
        ({"candidate_sha": "not-a-sha"}, "exec_receipt_schema"),
        ({"replica_binding_sha256": "d" * 63}, "exec_receipt_schema"),
        ({"scenario_id": "-bad"}, "exec_receipt_schema"),
        ({"details": []}, "exec_receipt_schema"),
    ],
)
def test_exec_receipt_schema_rejects_each_field_out_of_shape(overrides: dict[str, object], expected: str) -> None:
    with pytest.raises(AcceptanceCheckError, match=expected):
        validate_exec_receipt_schema(_envelope(_descriptor(), **overrides), descriptor=_descriptor())


def test_exec_receipt_detail_validator_rejection_propagates() -> None:
    def refuse(_details: Mapping[str, object]) -> None:
        raise AcceptanceCheckError("exec_receipt_schema")

    descriptor = ExecReceiptDescriptor(
        provider="aws", subject_field="task_arn_sha256", detail_validators=MappingProxyType({"verify-s3": refuse})
    )
    with pytest.raises(AcceptanceCheckError, match="exec_receipt_schema"):
        validate_exec_receipt_schema(_envelope(descriptor, check="verify-s3"), descriptor=descriptor)


class TestExecReceiptDescriptor:
    def test_check_kinds_are_the_registry_keys_and_envelope_names_the_subject(self) -> None:
        descriptor = _descriptor()
        assert descriptor.check_kinds == {"verify-doctor-job", "verify-connection-budget"}
        assert descriptor.envelope_fields == {"version", "check", "ok", "candidate_sha", "replica_binding_sha256", "scenario_id", "details"}

    @pytest.mark.parametrize(
        "kwargs",
        [
            pytest.param({"provider": "gcp"}, id="unknown-provider"),
            pytest.param({"subject_field": "task_arn"}, id="subject-not-a-sha256"),
            pytest.param({"subject_field": "Task-ARN_sha256"}, id="subject-not-snake-case"),
            pytest.param({"detail_validators": MappingProxyType({})}, id="no-kinds"),
            pytest.param({"detail_validators": MappingProxyType({"Verify S3": _accept_any})}, id="kind-not-kebab"),
        ],
    )
    def test_descriptor_is_closed_at_construction(self, kwargs: dict[str, object]) -> None:
        base: dict[str, object] = {
            "provider": "azure",
            "subject_field": "replica_binding_sha256",
            "detail_validators": MappingProxyType({"verify-doctor-job": _accept_any}),
        }
        base.update(kwargs)
        with pytest.raises(ValueError):
            ExecReceiptDescriptor(**base)  # type: ignore[arg-type]
