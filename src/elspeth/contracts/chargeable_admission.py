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
    APPROVAL_REQUIRED = "approval_required"
    APPROVAL_BINDING_MISMATCH = "approval_binding_mismatch"


class QuotaDisposition(StrEnum):
    NOT_ASSESSED = "not_assessed"
    NOT_CONFIGURED = "not_configured"
    POLICY_MISSING = "policy_missing"
    ACCOUNTING_UNAVAILABLE = "accounting_unavailable"
    WITHIN_CAP = "within_cap"
    EXCEEDED = "exceeded"


class ApprovalDisposition(StrEnum):
    """The closed R2 verdict, evaluated after chargeable policy allows work."""

    NOT_ASSESSED = "not_assessed"
    MATCHED = "matched"
    REQUIRED = "required"
    BINDING_MISMATCH = "binding_mismatch"


class AdmissionPolicyEvidence(BaseModel):
    """Sanitized decision facts; absence is never represented as measured zero."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    schema_version: Literal[2, 3] = 2
    identity_policy_id: str | None = Field(default=None, min_length=1)
    container_policy_id: str | None = Field(default=None, min_length=1)
    quota_disposition: QuotaDisposition
    secret_wiring_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    dimension: Literal["tokens"] | None = None
    cap: int | None = Field(default=None, gt=0)
    ceiling: int | None = Field(default=None, gt=0)
    usage: int | None = Field(default=None, ge=0)
    approval_disposition: ApprovalDisposition | None = None
    approval_binding_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @field_validator("schema_version", mode="before")
    @classmethod
    def _exact_version(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("Admission evidence version must be an exact integer")
        return value

    @model_validator(mode="after")
    def _assessed_policy_presence(self) -> AdmissionPolicyEvidence:
        if self.schema_version == 2 and (self.approval_disposition is not None or self.approval_binding_hash is not None):
            raise ValueError("Version 2 admission evidence cannot carry approval evidence")
        if self.schema_version == 3:
            if self.approval_disposition is None:
                raise ValueError("Version 3 admission evidence requires an approval verdict")
            assessed = self.approval_disposition is not ApprovalDisposition.NOT_ASSESSED
            if assessed != (self.approval_binding_hash is not None):
                raise ValueError("Assessed approval evidence requires the compiled binding hash")
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
        payload = self.model_dump(mode="json")
        if self.schema_version == 2:
            # Version 2 evidence predates R2. Preserve its stored hash on replay.
            payload.pop("approval_disposition")
            payload.pop("approval_binding_hash")
        return stable_hash(payload)


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
        approval_reasons = {
            AdmissionRefusalReason.APPROVAL_REQUIRED: ApprovalDisposition.REQUIRED,
            AdmissionRefusalReason.APPROVAL_BINDING_MISMATCH: ApprovalDisposition.BINDING_MISMATCH,
        }
        generation_changed_after_assessment = (
            self.refusal_reason is AdmissionRefusalReason.POLICY_GENERATION_CHANGED
            and self.evidence.schema_version == 3
            and disposition in allowed_dispositions
            and self.evidence.approval_disposition is ApprovalDisposition.MATCHED
        )
        if self.refusal_reason in approval_reasons:
            if disposition not in allowed_dispositions or self.evidence.approval_disposition is not approval_reasons[self.refusal_reason]:
                raise ValueError("Approval refusal requires an allowed quota and matching approval verdict")
        elif self.refusal_reason is None:
            if disposition not in allowed_dispositions:
                raise ValueError("Only unconfigured or within-cap token quotas admit chargeable work")
            if self.evidence.schema_version == 3 and self.evidence.approval_disposition is not ApprovalDisposition.MATCHED:
                raise ValueError("Governed allowance requires a matching approval")
        elif disposition in allowed_dispositions and not generation_changed_after_assessment:
            raise ValueError("A quota allowance cannot carry a non-approval refusal")
        if self.refusal_reason is not None:
            quota_refusal_dispositions = {
                AdmissionRefusalReason.IDENTITY_DISABLED: QuotaDisposition.NOT_ASSESSED,
                AdmissionRefusalReason.IDENTITY_PENDING: QuotaDisposition.NOT_ASSESSED,
                AdmissionRefusalReason.IDENTITY_MISSING: QuotaDisposition.NOT_ASSESSED,
                AdmissionRefusalReason.POLICY_GENERATION_CHANGED: QuotaDisposition.NOT_ASSESSED,
                AdmissionRefusalReason.QUOTA_POLICY_MISSING: QuotaDisposition.POLICY_MISSING,
                AdmissionRefusalReason.TOKEN_ACCOUNTING_UNAVAILABLE: QuotaDisposition.ACCOUNTING_UNAVAILABLE,
                AdmissionRefusalReason.QUOTA_EXCEEDED: QuotaDisposition.EXCEEDED,
            }
            # Approval refusals preserve the measured quota result. Every
            # other refusal has one mandatory quota disposition; a newly
            # added reason must be classified here rather than disappearing.
            expected_disposition = None if self.refusal_reason in approval_reasons else quota_refusal_dispositions[self.refusal_reason]
            if expected_disposition is not None and disposition is not expected_disposition and not generation_changed_after_assessment:
                raise ValueError("Admission refusal contradicts quota assessment")
            if (
                expected_disposition is not None
                and self.evidence.schema_version == 3
                and (self.evidence.approval_disposition is not ApprovalDisposition.NOT_ASSESSED)
                and not generation_changed_after_assessment
            ):
                raise ValueError("Approval must be unassessed after a chargeable refusal")
        if disposition in {QuotaDisposition.NOT_CONFIGURED, QuotaDisposition.NOT_ASSESSED} and (
            self.evidence.identity_policy_id is not None or self.evidence.container_policy_id is not None
        ):
            raise ValueError("Unassessed or unconfigured quotas cannot carry policy IDs")
        return self

    @property
    def canonical_hash(self) -> str:
        payload = self.model_dump(mode="json")
        if self.evidence.schema_version == 2:
            # Existing persisted version 2 decisions were hashed without R2.
            payload["evidence"].pop("approval_disposition")
            payload["evidence"].pop("approval_binding_hash")
        return stable_hash(payload)


class ChargeableAdmissionPolicy(BaseModel):
    """Boot-owned admission configuration, independent of mutable request data."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    identity_token_quota_configured: bool = False
    container_token_quota_configured: bool = False
    workflow_governance_on: bool = False
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
