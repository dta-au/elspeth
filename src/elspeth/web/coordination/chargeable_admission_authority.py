"""Principal and accounting admission inside an existing fenced transaction."""

from __future__ import annotations

from sqlalchemy import select

from elspeth.contracts.chargeable_admission import (
    AdmissionPolicyEvidence,
    AdmissionRefusalReason,
    ChargeableAdmissionDecision,
    ChargeableAdmissionPolicy,
    QuotaDisposition,
)
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.coordination.mutation_connection_registry import _resolve_mutation_connection
from elspeth.web.sessions.models import identities_table, quota_policies_table, sessions_table


class RepositoryChargeableAdmissionAuthority:
    """No incomplete ledger can grant permission to consume shared tokens."""

    @staticmethod
    def assess(connection_token: str, *, session_id: str, policy: ChargeableAdmissionPolicy) -> ChargeableAdmissionDecision:
        conn = _resolve_mutation_connection(connection_token)
        session = conn.execute(
            select(sessions_table.c.user_id, sessions_table.c.auth_provider_type).where(sessions_table.c.id == session_id)
        ).one()
        identity = conn.execute(
            select(identities_table.c.access_state, identities_table.c.provider)
            .where(identities_table.c.identity_id == session.user_id)
            .with_for_update()
        ).one_or_none()
        if identity is not None and identity.provider != session.auth_provider_type:
            raise AuditIntegrityError("Session owner identity provider custody mismatch")
        identity_refusal: AdmissionRefusalReason | None = None
        if identity is None:
            identity_refusal = AdmissionRefusalReason.IDENTITY_MISSING
        elif identity.access_state == "disabled":
            identity_refusal = AdmissionRefusalReason.IDENTITY_DISABLED
        elif identity.access_state == "pending":
            identity_refusal = AdmissionRefusalReason.IDENTITY_PENDING
        elif identity.access_state != "active":
            raise AuditIntegrityError("Stored identity has an unknown access state")
        if identity_refusal is not None:
            return ChargeableAdmissionDecision(
                refusal_reason=identity_refusal,
                evidence=AdmissionPolicyEvidence(
                    quota_disposition=QuotaDisposition.NOT_ASSESSED, secret_wiring_hash=policy.secret_wiring_hash
                ),
            )
        identity_policy = conn.execute(
            select(quota_policies_table.c.policy_id)
            .where(quota_policies_table.c.identity_id == session.user_id, quota_policies_table.c.revoked_at.is_(None))
            .with_for_update()
        ).one_or_none()
        container_policy = conn.execute(
            select(quota_policies_table.c.policy_id)
            .where(quota_policies_table.c.identity_id.is_(None), quota_policies_table.c.revoked_at.is_(None))
            .with_for_update()
        ).one_or_none()
        # Boot settings supply issuance defaults and required policy slots;
        # they do not disable an explicit operator-authored allowance row.
        if not policy.token_quota_configured and identity_policy is None and container_policy is None:
            return ChargeableAdmissionDecision(
                refusal_reason=None,
                evidence=AdmissionPolicyEvidence(
                    quota_disposition=QuotaDisposition.NOT_CONFIGURED, secret_wiring_hash=policy.secret_wiring_hash
                ),
            )
        missing = (policy.identity_token_quota_configured and identity_policy is None) or (
            policy.container_token_quota_configured and container_policy is None
        )
        return ChargeableAdmissionDecision(
            refusal_reason=AdmissionRefusalReason.QUOTA_POLICY_MISSING if missing else AdmissionRefusalReason.TOKEN_ACCOUNTING_UNAVAILABLE,
            evidence=AdmissionPolicyEvidence(
                identity_policy_id=identity_policy.policy_id if identity_policy is not None else None,
                container_policy_id=container_policy.policy_id if container_policy is not None else None,
                quota_disposition=QuotaDisposition.POLICY_MISSING if missing else QuotaDisposition.ACCOUNTING_UNAVAILABLE,
                secret_wiring_hash=policy.secret_wiring_hash,
            ),
        )
