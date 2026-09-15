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
from elspeth.contracts.session_operation import SessionOperationContext
from elspeth.web.coordination.database_clock import database_now
from elspeth.web.coordination.mutation_connection_registry import _resolve_mutation_connection
from elspeth.web.coordination.quota_authority import RepositoryQuotaAuthority, utc_day_start
from elspeth.web.sessions.models import identities_table, sessions_table


class RepositoryChargeableAdmissionAuthority:
    """No incomplete ledger can grant permission to consume shared tokens."""

    @staticmethod
    def assess(
        connection_token: str,
        *,
        session_id: str,
        policy: ChargeableAdmissionPolicy,
        live_operation_context: SessionOperationContext | None = None,
    ) -> ChargeableAdmissionDecision:
        if live_operation_context is not None and live_operation_context.fence.session_id != session_id:
            raise AuditIntegrityError("Quota admission live operation belongs to another session")
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
        policies = RepositoryQuotaAuthority.active_policy(connection_token, identity_id=session.user_id)
        identity_policy = policies.identity
        container_policy = policies.container
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
        if missing:
            return ChargeableAdmissionDecision(
                refusal_reason=AdmissionRefusalReason.QUOTA_POLICY_MISSING,
                evidence=AdmissionPolicyEvidence(
                    identity_policy_id=identity_policy.policy_id if identity_policy is not None else None,
                    container_policy_id=container_policy.policy_id if container_policy is not None else None,
                    quota_disposition=QuotaDisposition.POLICY_MISSING,
                    secret_wiring_hash=policy.secret_wiring_hash,
                ),
            )
        usage = RepositoryQuotaAuthority.daily_token_total(
            connection_token,
            identity_id=session.user_id,
            day_start_utc=utc_day_start(database_now(conn)),
            live_operation_context=live_operation_context,
        )
        if usage is None:
            return ChargeableAdmissionDecision(
                refusal_reason=AdmissionRefusalReason.TOKEN_ACCOUNTING_UNAVAILABLE,
                evidence=AdmissionPolicyEvidence(
                    identity_policy_id=identity_policy.policy_id if identity_policy is not None else None,
                    container_policy_id=container_policy.policy_id if container_policy is not None else None,
                    quota_disposition=QuotaDisposition.ACCOUNTING_UNAVAILABLE,
                    secret_wiring_hash=policy.secret_wiring_hash,
                ),
            )
        cap = identity_policy.tokens_per_day if identity_policy is not None else None
        ceiling = container_policy.tokens_per_day if container_policy is not None else None
        limit = min(value for value in (cap, ceiling) if value is not None)
        exceeded = usage >= limit
        return ChargeableAdmissionDecision(
            refusal_reason=AdmissionRefusalReason.QUOTA_EXCEEDED if exceeded else None,
            evidence=AdmissionPolicyEvidence(
                identity_policy_id=identity_policy.policy_id if identity_policy is not None else None,
                container_policy_id=container_policy.policy_id if container_policy is not None else None,
                quota_disposition=QuotaDisposition.EXCEEDED if exceeded else QuotaDisposition.WITHIN_CAP,
                secret_wiring_hash=policy.secret_wiring_hash,
                dimension="tokens",
                cap=cap,
                ceiling=ceiling,
                usage=usage,
            ),
        )
