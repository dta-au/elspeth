"""Owned, immutable decisions at Web chargeable-work admission boundaries."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from elspeth.contracts.hashing import stable_hash


class ChargeableOperation(StrEnum):
    RUN = "run"
    COMPOSER = "composer"
    AUTO_TITLE = "auto_title"


class AdmissionRefusalReason(StrEnum):
    IDENTITY_DISABLED = "identity_disabled"
    IDENTITY_PENDING = "identity_pending"
    IDENTITY_MISSING = "identity_missing"
    QUOTA_POLICY_MISSING = "quota_policy_missing"
    TOKEN_ACCOUNTING_UNAVAILABLE = "token_accounting_unavailable"
    QUOTA_EXCEEDED = "quota_exceeded"
    POLICY_GENERATION_CHANGED = "policy_generation_changed"


class QuotaDisposition(StrEnum):
    NOT_ASSESSED = "not_assessed"
    NOT_CONFIGURED = "not_configured"
    POLICY_MISSING = "policy_missing"
    ACCOUNTING_UNAVAILABLE = "accounting_unavailable"
    WITHIN_CAP = "within_cap"
    EXCEEDED = "exceeded"


class AdmissionPolicyEvidence(BaseModel):
    """Sanitized decision facts; absence is never represented as measured zero."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    schema_version: Literal[2] = 2
    identity_policy_id: str | None = Field(default=None, min_length=1)
    container_policy_id: str | None = Field(default=None, min_length=1)
    quota_disposition: QuotaDisposition
    secret_wiring_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    dimension: Literal["tokens"] | None = None
    cap: int | None = Field(default=None, gt=0)
    ceiling: int | None = Field(default=None, gt=0)
    usage: int | None = Field(default=None, ge=0)

    @field_validator("schema_version", mode="before")
    @classmethod
    def _exact_version(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("Admission evidence version must be an exact integer")
        return value

    @model_validator(mode="after")
    def _assessed_policy_presence(self) -> AdmissionPolicyEvidence:
        measured = self.quota_disposition in {QuotaDisposition.WITHIN_CAP, QuotaDisposition.EXCEEDED}
        if measured:
            if self.dimension != "tokens" or self.usage is None or (self.cap is None and self.ceiling is None):
                raise ValueError("Measured quota evidence requires dimension, usage and a limit")
            if (self.cap is None) != (self.identity_policy_id is None) or (self.ceiling is None) != (self.container_policy_id is None):
                raise ValueError("Measured quota limits require matching policy IDs")
            limit = min(value for value in (self.cap, self.ceiling) if value is not None)
            if (self.usage >= limit) != (self.quota_disposition is QuotaDisposition.EXCEEDED):
                raise ValueError("Quota disposition contradicts measured usage and limit")
        elif any(value is not None for value in (self.dimension, self.cap, self.ceiling, self.usage)):
            raise ValueError("Unmeasured quota evidence cannot carry measured limits or usage")
        if self.quota_disposition is QuotaDisposition.ACCOUNTING_UNAVAILABLE and (
            self.identity_policy_id is None and self.container_policy_id is None
        ):
            raise ValueError("Accounting-unavailable evidence requires an assessed quota policy")
        return self

    @property
    def canonical_hash(self) -> str:
        return stable_hash(self.model_dump(mode="json"))


class ChargeableAdmissionDecision(BaseModel):
    """A closed refusal or a measured or explicitly unconfigured allowance."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    evidence: AdmissionPolicyEvidence
    refusal_reason: AdmissionRefusalReason | None

    @property
    def allowed(self) -> bool:
        return self.refusal_reason is None

    @model_validator(mode="after")
    def _consistent_decision(self) -> ChargeableAdmissionDecision:
        disposition = self.evidence.quota_disposition
        allowed_dispositions = {QuotaDisposition.NOT_CONFIGURED, QuotaDisposition.WITHIN_CAP}
        if self.refusal_reason is None and disposition not in allowed_dispositions:
            raise ValueError("Only unconfigured or within-cap token quotas admit chargeable work")
        if disposition in allowed_dispositions and self.refusal_reason is not None:
            raise ValueError("A quota allowance cannot carry a refusal")
        if self.refusal_reason is not None:
            expected_disposition = {
                AdmissionRefusalReason.IDENTITY_DISABLED: QuotaDisposition.NOT_ASSESSED,
                AdmissionRefusalReason.IDENTITY_PENDING: QuotaDisposition.NOT_ASSESSED,
                AdmissionRefusalReason.IDENTITY_MISSING: QuotaDisposition.NOT_ASSESSED,
                AdmissionRefusalReason.POLICY_GENERATION_CHANGED: QuotaDisposition.NOT_ASSESSED,
                AdmissionRefusalReason.QUOTA_POLICY_MISSING: QuotaDisposition.POLICY_MISSING,
                AdmissionRefusalReason.TOKEN_ACCOUNTING_UNAVAILABLE: QuotaDisposition.ACCOUNTING_UNAVAILABLE,
                AdmissionRefusalReason.QUOTA_EXCEEDED: QuotaDisposition.EXCEEDED,
            }[self.refusal_reason]
            if disposition is not expected_disposition:
                raise ValueError("Admission refusal contradicts quota assessment")
        if disposition in {QuotaDisposition.NOT_CONFIGURED, QuotaDisposition.NOT_ASSESSED} and (
            self.evidence.identity_policy_id is not None or self.evidence.container_policy_id is not None
        ):
            raise ValueError("Unassessed or unconfigured quotas cannot carry policy IDs")
        return self

    @property
    def canonical_hash(self) -> str:
        return stable_hash(self.model_dump(mode="json"))


class ChargeableAdmissionPolicy(BaseModel):
    """Boot-owned admission configuration, independent of mutable request data."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    identity_token_quota_configured: bool = False
    container_token_quota_configured: bool = False
    secret_wiring_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @property
    def token_quota_configured(self) -> bool:
        return self.identity_token_quota_configured or self.container_token_quota_configured


class ChargeableAdmissionRefused(RuntimeError):
    """A committed authority refusal, distinct from provider or quota exhaustion."""

    def __init__(self, decision: ChargeableAdmissionDecision) -> None:
        if decision.allowed:
            raise ValueError("An allowed decision is not a refusal")
        self.decision = decision
        assert decision.refusal_reason is not None
        super().__init__(f"Chargeable work admission refused: {decision.refusal_reason.value}")
