"""Immutable admitted timing policy retained by one Composer logical request.

These values retain timing allowances, never content clearance or live execution
custody. Current security and capability admission still run at every use.
"""

from __future__ import annotations

import math
from decimal import Decimal
from typing import Annotated, Final, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import _CANONICAL_SAFE_INTEGER_MAX, canonical_json, stable_hash
from elspeth.contracts.json_parser import parse_json_strict

PositiveSeconds = Annotated[float, Field(strict=True, gt=0, allow_inf_nan=False)]

# The only executable retained policy contract; older tags are refused.
CONTINUATION_POLICY_SCHEMA_VERSION_V4: Final = "composer.continuation.policy.v4"


class ContinuationActionPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    preparation: PositiveSeconds
    guard: PositiveSeconds | None
    quota: PositiveSeconds | None
    physical: PositiveSeconds
    retention: PositiveSeconds

    @field_validator("preparation", "guard", "quota", "physical", "retention", mode="before")
    @classmethod
    def _numeric(cls, value: object) -> object:
        if type(value) is bool:
            raise ValueError("stage bound cannot be boolean")
        return value

    @property
    def total(self) -> float:
        return self.preparation + (self.guard or 0.0) + (self.quota or 0.0) + self.physical + self.retention

    @model_validator(mode="after")
    def _finite_total(self) -> Self:
        if not math.isfinite(self.total):
            raise ValueError("total action allowance must be finite")
        return self


class ContinuationSegmentPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    nominal: PositiveSeconds
    watermark: PositiveSeconds
    quantum: PositiveSeconds
    reserve: PositiveSeconds

    @model_validator(mode="after")
    def _watermark(self) -> Self:
        if self.watermark >= self.nominal:
            raise ValueError("watermark must be smaller than nominal segment")
        return self


class ContinuationActionBounds(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    primary: ContinuationActionPolicy
    advisor: ContinuationActionPolicy
    title: ContinuationActionPolicy
    read: ContinuationActionPolicy
    session_sql: ContinuationActionPolicy
    blob: ContinuationActionPolicy
    external: ContinuationActionPolicy


class ContinuationAdmittedCostPolicy(BaseModel):
    """Frozen cost limits captured from the existing operator settings."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    max_provider_calls: Annotated[int, Field(strict=True, ge=1)]
    max_request_bytes: Annotated[int, Field(strict=True, ge=1)]
    max_completion_tokens: Annotated[int, Field(strict=True, ge=1)]
    max_cumulative_provider_cost: Decimal
    repair_budget: Annotated[int, Field(strict=True, ge=0)]

    @model_validator(mode="after")
    def _finite_cost(self) -> Self:
        if not self.max_cumulative_provider_cost.is_finite() or self.max_cumulative_provider_cost < 0:
            raise ValueError("provider cost limit requires finite nonnegative Decimal")
        return self


class ContinuationAdmissionPolicyV4(BaseModel):
    """Current whole-operation allowance and paid-provider overflow disposition.

    The disposition only permits a terminal incomplete-evidence receipt after
    an actual provider call. It does not authorize dropping tool results or
    weakening the complete-success contract.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["composer.continuation.policy.v4"] = CONTINUATION_POLICY_SCHEMA_VERSION_V4
    segment: ContinuationSegmentPolicy
    actions: ContinuationActionBounds
    provider_cost: ContinuationAdmittedCostPolicy
    max_retained_payload_bytes: Annotated[int, Field(strict=True, ge=1, le=_CANONICAL_SAFE_INTEGER_MAX)]
    paid_provider_overflow_disposition: Literal["terminal_paid_overflow"]

    @model_validator(mode="after")
    def _finite_segment_sizing(self) -> Self:
        for action in (
            self.actions.primary,
            self.actions.advisor,
            self.actions.title,
            self.actions.read,
            self.actions.session_sql,
            self.actions.blob,
            self.actions.external,
        ):
            if not math.isfinite(action.total + self.segment.reserve + self.segment.watermark):
                raise ValueError("complete segment sizing allowance must be finite")
        return self

    @property
    def policy_hash(self) -> str:
        return stable_hash(self.model_dump(mode="json"))


def decode_continuation_policy(encoded: str, expected_hash: str) -> ContinuationAdmissionPolicyV4:
    value, error = parse_json_strict(encoded)
    if error is not None:
        raise AuditIntegrityError("continuation policy is not strict JSON")
    if type(value) is not dict or "schema_version" not in value:
        raise AuditIntegrityError("continuation policy lost its schema version")
    version = value["schema_version"]
    if type(version) is not str or version != CONTINUATION_POLICY_SCHEMA_VERSION_V4:
        raise AuditIntegrityError("continuation policy has an unsupported schema version")
    try:
        policy = ContinuationAdmissionPolicyV4.model_validate_json(canonical_json(value))
    except ValueError as exc:
        raise AuditIntegrityError("continuation policy violates its complete owned contract") from exc
    if policy.policy_hash != expected_hash:
        raise AuditIntegrityError("continuation policy hash mismatch")
    return policy
