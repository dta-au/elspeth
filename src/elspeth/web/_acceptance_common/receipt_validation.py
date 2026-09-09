"""Bounded receipt admission shared by every acceptance provider.

Moved from ``_aws_ecs_acceptance/receipt_contracts.py`` (the document
traversal, the numeric/timestamp readers, the connection-budget validator) and
``_aws_ecs_acceptance/contracts.py`` (the digest and timestamp helpers and the
identity patterns). Two pieces are generalised rather than moved:

- :func:`validate_connection_budget_receipt` takes the receipt ``schema_id``
  the provider binds (``elspeth.rds-connection-budget.v3`` on ECS); everything
  else about the budget receipt is identical across providers.
- :func:`validate_exec_receipt_schema` takes an :class:`ExecReceiptDescriptor`
  naming the provider's subject field (``task_arn_sha256`` on ECS,
  ``replica_binding_sha256`` on Container Apps), its closed check-kind set and
  the per-kind detail validators. The envelope shape, the version-1 ``ok``
  contract, the candidate-sha and scenario-id bounds are one implementation.

The provider packages keep their own thin bindings with their own
``@trust_boundary`` tests; the boundaries here carry the shared tests under
``tests/unit/web/acceptance_common/``.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from elspeth.contracts.freeze import freeze_fields
from elspeth.contracts.trust_boundary import trust_boundary

from .errors import AcceptanceCheckError

MAX_EXEC_RECEIPT_CHARS = 16 * 1024
MAX_EXEC_STREAM_BYTES = 2 * 1024 * 1024
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
_GIT_SHA_PATTERN = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_SCENARIO_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _utc_timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise AcceptanceCheckError("timestamp")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_utc_z_timestamp(value: str) -> datetime:
    """Parse one strict UTC timestamp ending in ``Z``."""

    if type(value) is not str or not value.endswith("Z"):
        raise ValueError("timestamp must be a UTC Z timestamp")
    try:
        parsed = datetime.fromisoformat(f"{value[:-1]}+00:00")
    except ValueError:
        raise ValueError("timestamp must be a UTC Z timestamp") from None
    if parsed.tzinfo != UTC:
        raise ValueError("timestamp must be a UTC Z timestamp")
    return parsed


_FORBIDDEN_RECEIPT_KEYS = frozenset(
    {
        "password",
        "credential",
        "credentials",
        "secret",
        "token",
        "access_token",
        "refresh_token",
        "command",
        "environment",
        "provider_response",
        "raw_response",
        "raw_output",
        "message",
        "exception_text",
        "headers",
        "cookies",
        "username",
    }
)


@trust_boundary(
    tier=3,
    source="one node of an untrusted receipt document read back from the acceptance receipt store",
    source_param="value",
    suppresses=("R1", "R5"),
    invariant=(
        "raises AcceptanceCheckError('receipt_store_schema') before use on any container over its size limit, any key "
        "that is not a bounded safe identifier or that names forbidden or raw content, any control-character or "
        "oversized string, and any non-JSON or non-finite scalar; the same error also rejects a node that exhausts "
        "the caller's whole-document node budget or exceeds the depth limit"
    ),
    test_ref=("tests/unit/web/aws_ecs_acceptance/test_receipt_contracts.py::test_receipt_value_visit_rejects_forbidden_key"),
    test_fingerprint="a173e99eda87d962aa77fa64ae6e9de976ffbfc7d3e3678c282f697aaa95ded4",
)
def _visit_receipt_value(value: object, *, depth: int, remaining: int) -> int:
    """Admit one receipt node and return the node budget left after it.

    The budget is threaded, not reset: every recursive call consumes from and
    returns the caller's remaining count, so the 4096 cap stays a whole-document
    node cap rather than a per-branch one.
    """

    remaining -= 1
    if remaining < 0 or depth > 8:
        raise AcceptanceCheckError("receipt_store_schema")
    if isinstance(value, dict):
        if len(value) > 256:
            raise AcceptanceCheckError("receipt_store_schema")
        for key, child in value.items():
            if (
                type(key) is not str
                or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", key) is None
                or key.lower() in _FORBIDDEN_RECEIPT_KEYS
                or key.lower().endswith("_raw")
            ):
                raise AcceptanceCheckError("receipt_store_schema")
            remaining = _visit_receipt_value(child, depth=depth + 1, remaining=remaining)
    elif isinstance(value, list):
        if len(value) > 1024:
            raise AcceptanceCheckError("receipt_store_schema")
        for child in value:
            remaining = _visit_receipt_value(child, depth=depth + 1, remaining=remaining)
    elif isinstance(value, str):
        if len(value) > 16 * 1024 or any(ord(character) < 32 or ord(character) == 127 for character in value):
            raise AcceptanceCheckError("receipt_store_schema")
    elif (value is not None and not isinstance(value, (bool, int, float))) or (isinstance(value, float) and not math.isfinite(value)):
        raise AcceptanceCheckError("receipt_store_schema")
    return remaining


@trust_boundary(
    tier=3,
    source="a whole receipt document read back from the acceptance receipt store or the orphan-sweep inventory",
    source_param="payload",
    suppresses=("R1", "R5"),
    invariant=(
        "raises AcceptanceCheckError('receipt_store_schema') before use when the payload is not a dict, or when any "
        "node beneath it fails the bounded-document admission the traversal enforces"
    ),
    test_ref=("tests/unit/web/aws_ecs_acceptance/test_receipt_contracts.py::test_bounded_receipt_document_rejects_non_dict_payload"),
    test_fingerprint="5687a5689ddad1424f50bcdbcc173c5f46a4cfcc2be82ae1dba3b8b7ff39c470",
)
def _validate_bounded_receipt_document(payload: object) -> dict[str, object]:
    if not isinstance(payload, dict):
        raise AcceptanceCheckError("receipt_store_schema")
    _visit_receipt_value(payload, depth=0, remaining=4096)
    return payload


@trust_boundary(
    tier=3,
    source="one numeric field of an untrusted connection-budget receipt read back from the acceptance receipt store",
    source_param="value",
    suppresses=("R1", "R5"),
    invariant=(
        "raises AcceptanceCheckError('receipt_store_schema') before use on a bool, a non-real value, a non-finite "
        "float, or a negative number; returns an owned float otherwise"
    ),
    test_ref=("tests/unit/web/aws_ecs_acceptance/test_receipt_contracts.py::test_receipt_number_rejects_bool_as_a_number"),
    test_fingerprint="72ff8815d1351ec4322f0a795e2a9716a86f9fd42e370c407d36deb103231434",
)
def _receipt_number(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)) or float(value) < 0:
        raise AcceptanceCheckError("receipt_store_schema")
    return float(value)


def _receipt_nonnegative_integer(value: object) -> int:
    if type(value) is not int or value < 0:
        raise AcceptanceCheckError("receipt_store_schema")
    return value


def _receipt_timestamp(value: object) -> datetime:
    if type(value) is not str:
        raise AcceptanceCheckError("receipt_store_schema")
    try:
        return _parse_utc_z_timestamp(value)
    except ValueError:
        raise AcceptanceCheckError("receipt_store_schema") from None


_CONNECTION_BUDGET_FIELDS = frozenset(
    {
        "schema",
        "acceptance_run_id_sha256",
        "cluster_id_sha256",
        "window_start",
        "window_end",
        "period_seconds",
        "expected_points",
        "points",
        "high_water",
        "max_connections",
        "approved_budget",
        "safety_margin",
        "ok",
    }
)


@trust_boundary(
    tier=3,
    source=(
        "an untrusted database connection-budget receipt: the verify-connection-budget details decoded from a "
        "receipt sentinel, or the stored receipt document, on any provider"
    ),
    source_param="payload",
    suppresses=("R1", "R5"),
    invariant=(
        "raises AcceptanceCheckError('receipt_store_schema' or 'receipt_store_binding') before use unless the payload "
        "is a dict with exactly the budget fields under the caller's schema id, sha256 run and cluster identities "
        "matching the expected bindings, a minute-aligned ten-point window whose observed timestamps are exactly the "
        "expected ones, and a high-water reading that is the maximum observed count and within the approved budget "
        "and safety margin"
    ),
    test_ref=("tests/unit/web/acceptance_common/test_receipt_validation.py::test_connection_budget_receipt_rejects_short_point_series"),
    test_fingerprint="7e7d67e2e9a8c123357f08bbeeab7607729146c07bdc8d8b986c39be39769439",
)
def validate_connection_budget_receipt(
    payload: object,
    *,
    schema_id: str,
    expected_acceptance_run_id_sha256: str | None = None,
    expected_cluster_id_sha256: str | None = None,
) -> dict[str, object]:
    if not isinstance(payload, dict) or set(payload) != _CONNECTION_BUDGET_FIELDS:
        raise AcceptanceCheckError("receipt_store_schema")
    if (
        payload["schema"] != schema_id
        or type(payload["acceptance_run_id_sha256"]) is not str
        or _SHA256_PATTERN.fullmatch(payload["acceptance_run_id_sha256"]) is None
        or type(payload["cluster_id_sha256"]) is not str
        or _SHA256_PATTERN.fullmatch(payload["cluster_id_sha256"]) is None
    ):
        raise AcceptanceCheckError("receipt_store_schema")
    if (expected_acceptance_run_id_sha256 is not None and payload["acceptance_run_id_sha256"] != expected_acceptance_run_id_sha256) or (
        expected_cluster_id_sha256 is not None and payload["cluster_id_sha256"] != expected_cluster_id_sha256
    ):
        raise AcceptanceCheckError("receipt_store_binding")
    points = payload["points"]
    window_start = _receipt_timestamp(payload["window_start"])
    window_end = _receipt_timestamp(payload["window_end"])
    if (
        payload["period_seconds"] != 60
        or payload["expected_points"] != 10
        or window_end - window_start != timedelta(minutes=10)
        or window_start.second != 0
        or window_start.microsecond != 0
    ):
        raise AcceptanceCheckError("receipt_store_schema")
    expected_timestamps = [window_start + timedelta(minutes=offset) for offset in range(10)]
    if not isinstance(points, list) or len(points) != len(expected_timestamps):
        raise AcceptanceCheckError("receipt_store_schema")
    counts: list[float] = []
    observed_timestamps: list[datetime] = []
    for point in points:
        if not isinstance(point, dict) or set(point) != {"timestamp", "count"}:
            raise AcceptanceCheckError("receipt_store_schema")
        observed_timestamps.append(_receipt_timestamp(point["timestamp"]))
        counts.append(_receipt_number(point["count"]))
    if observed_timestamps != expected_timestamps or len(set(observed_timestamps)) != len(observed_timestamps):
        raise AcceptanceCheckError("receipt_store_schema")
    high_water = _receipt_number(payload["high_water"])
    maximum = _receipt_nonnegative_integer(payload["max_connections"])
    budget = _receipt_nonnegative_integer(payload["approved_budget"])
    margin = _receipt_nonnegative_integer(payload["safety_margin"])
    if (
        payload["ok"] is not True
        or high_water != max(counts)
        or high_water > budget
        or budget > maximum - margin
        or maximum - high_water < margin
    ):
        raise AcceptanceCheckError("receipt_store_schema")
    return payload


DetailValidator = Callable[[Mapping[str, object]], object]
"""Per-check detail validator: raises ``AcceptanceCheckError`` on rejection."""


@dataclass(frozen=True)
class ExecReceiptDescriptor:
    """What one provider's exec-receipt envelope binds and admits.

    ``subject_field`` names the sha256 that ties the receipt to the platform
    subject that produced it (an ECS task ARN, a Container Apps replica
    binding). ``detail_validators`` is the closed check-kind registry: its keys
    are the only ``check`` values the envelope admits, and each value rejects
    a ``details`` dict outside that kind's closed shape.
    """

    provider: str
    subject_field: str
    detail_validators: Mapping[str, DetailValidator]

    def __post_init__(self) -> None:
        if self.provider not in {"aws", "azure"}:
            raise ValueError("provider must be aws or azure")
        if re.fullmatch(r"[a-z][a-z0-9_]{0,63}_sha256", self.subject_field) is None:
            raise ValueError("subject_field must be a snake_case identifier ending in _sha256")
        if not self.detail_validators:
            raise ValueError("detail_validators must name at least one check kind")
        if any(re.fullmatch(r"[a-z][a-z0-9-]{0,63}", check) is None for check in self.detail_validators):
            raise ValueError("check kinds must be bounded kebab-case identifiers")
        # The registry is the closed check-kind set: freeze it so a provider
        # cannot grow it after construction.
        freeze_fields(self, "detail_validators")

    @property
    def check_kinds(self) -> frozenset[str]:
        return frozenset(self.detail_validators)

    @property
    def envelope_fields(self) -> frozenset[str]:
        return frozenset({"version", "check", "ok", "candidate_sha", self.subject_field, "scenario_id", "details"})


@trust_boundary(
    tier=3,
    source=(
        "one closed exec receipt envelope on any provider: the base64 payload decoded from the platform's exec "
        "output, or a stored receipt document read back from the receipt store"
    ),
    source_param="payload",
    suppresses=("R1", "R5"),
    invariant=(
        "raises AcceptanceCheckError('exec_receipt_schema' or 'exec_receipt') before use on any payload that is not a "
        "dict with exactly the descriptor's envelope fields, a version-1 ok envelope, a check name in the descriptor's "
        "closed registry, a git candidate sha, a sha256 subject binding, a well-formed scenario id, and a details "
        "dict the registered per-check validator accepts"
    ),
    test_ref=("tests/unit/web/acceptance_common/test_receipt_validation.py::test_exec_receipt_schema_rejects_non_dict_and_open_envelopes"),
    test_fingerprint="e0bd4c02ec1ca85eca51988ec381dabfd83f31225a43b6224f963256ad233d74",
)
def validate_exec_receipt_schema(payload: object, *, descriptor: ExecReceiptDescriptor) -> dict[str, object]:
    if not isinstance(payload, dict) or set(payload) != descriptor.envelope_fields:
        raise AcceptanceCheckError("exec_receipt_schema")
    if payload["version"] != 1 or type(payload["version"]) is not int or payload["ok"] is not True:
        raise AcceptanceCheckError("exec_receipt")
    check = payload["check"]
    candidate_sha = payload["candidate_sha"]
    subject_sha256 = payload[descriptor.subject_field]
    scenario_id = payload["scenario_id"]
    details = payload["details"]
    if type(check) is not str or check not in descriptor.detail_validators:
        raise AcceptanceCheckError("exec_receipt_schema")
    if type(candidate_sha) is not str or _GIT_SHA_PATTERN.fullmatch(candidate_sha) is None:
        raise AcceptanceCheckError("exec_receipt_schema")
    if type(subject_sha256) is not str or _SHA256_PATTERN.fullmatch(subject_sha256) is None:
        raise AcceptanceCheckError("exec_receipt_schema")
    if type(scenario_id) is not str or _SCENARIO_ID_PATTERN.fullmatch(scenario_id) is None:
        raise AcceptanceCheckError("exec_receipt_schema")
    if not isinstance(details, dict):
        raise AcceptanceCheckError("exec_receipt_schema")
    descriptor.detail_validators[check](details)
    return payload
