"""Immutable admitted timing policy retained by one Composer logical request.

These values retain timing allowances, never content clearance or live execution
custody. Current security and capability admission still run at every use.
"""

from __future__ import annotations

import math
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json, stable_hash
from elspeth.contracts.json_parser import parse_json_strict

PositiveSeconds = Annotated[float, Field(strict=True, gt=0, allow_inf_nan=False)]


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


class ContinuationAdmissionPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    segment: ContinuationSegmentPolicy
    actions: ContinuationActionBounds

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


def decode_continuation_policy(encoded: str, expected_hash: str) -> ContinuationAdmissionPolicy:
    value, error = parse_json_strict(encoded)
    if error is not None:
        raise AuditIntegrityError("continuation policy is not strict JSON")
    try:
        policy = ContinuationAdmissionPolicy.model_validate_json(canonical_json(value))
    except ValueError as exc:
        raise AuditIntegrityError("continuation policy violates its complete owned contract") from exc
    if policy.policy_hash != expected_hash:
        raise AuditIntegrityError("continuation policy hash mismatch")
    return policy
