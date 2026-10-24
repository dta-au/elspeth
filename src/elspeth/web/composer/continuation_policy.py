"""Immutable admitted timing policy retained by one Composer logical request.

These values retain timing allowances, never content clearance or live execution
custody. Current security and capability admission still run at every use.
"""

from __future__ import annotations

import math
from typing import Annotated, Final, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json, stable_hash
from elspeth.contracts.json_parser import parse_json_strict

PositiveSeconds = Annotated[float, Field(strict=True, gt=0, allow_inf_nan=False)]

# The persisted policy's version tag. A later shape is a NEW frozen model with a
# new tag; this v1 model is never edited once admitted operations retain it.
CONTINUATION_POLICY_SCHEMA_VERSION: Final = "composer.continuation.policy.v1"
_KNOWN_POLICY_SCHEMA_VERSIONS: Final = frozenset({CONTINUATION_POLICY_SCHEMA_VERSION})


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
    schema_version: Literal["composer.continuation.policy.v1"] = CONTINUATION_POLICY_SCHEMA_VERSION
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
    # The version is read from the persisted bytes, never defaulted: a payload
    # without its tag, or with an unknown one, is not this owned contract.
    if type(value) is not dict or "schema_version" not in value:
        raise AuditIntegrityError("continuation policy lost its schema version")
    version = value["schema_version"]
    if type(version) is not str or version not in _KNOWN_POLICY_SCHEMA_VERSIONS:
        raise AuditIntegrityError("continuation policy has an unknown schema version")
    try:
        policy = ContinuationAdmissionPolicy.model_validate_json(canonical_json(value))
    except ValueError as exc:
        raise AuditIntegrityError("continuation policy violates its complete owned contract") from exc
    if policy.policy_hash != expected_hash:
        raise AuditIntegrityError("continuation policy hash mismatch")
    return policy
