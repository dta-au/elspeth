"""Explicit operator quota rows remain authoritative without issuance defaults."""

import pytest
from pydantic import SecretBytes
from sqlalchemy import select, update
from typer.testing import CliRunner

from elspeth.cli import app
from elspeth.contracts.chargeable_admission import AdmissionRefusalReason, ChargeableAdmissionPolicy, ChargeableOperation, QuotaDisposition
from elspeth.web.config import WebSettings
from elspeth.web.coordination.contracts import SessionOperationKind
from elspeth.web.coordination.sqlite_authority import SQLiteLocalSessionOperationAuthority
from elspeth.web.secrets.wiring_policy import EMPTY_SECRET_WIRING_POLICY
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.models import identities_table, quota_policies_table


@pytest.mark.parametrize("explicit_quota", [False, True])
@pytest.mark.parametrize("storage_defaults", [False, True])
def test_cli_quota_policy_is_not_disabled_by_absent_issuance_defaults(tmp_path, explicit_quota, storage_defaults):
    arguments = [
        "--no-dotenv",
        "composer",
        "users",
        "bootstrap-admin",
        "local",
        "operator",
        "--note",
        "quota admission regression",
        "--data-dir",
        str(tmp_path),
    ]
    if explicit_quota:
        arguments.extend(["--quota-tokens-per-day", "1000", "--quota-storage-bytes", "1000000"])
    result = CliRunner().invoke(app, arguments)
    assert result.exit_code == 0, result.output
    settings = WebSettings(
        data_dir=tmp_path,
        composer_max_composition_turns=15,
        composer_max_discovery_turns=10,
        composer_timeout_seconds=85.0,
        composer_rate_limit_per_minute=10,
        shareable_link_signing_key=SecretBytes(b"\x00" * 32),
        quota_default_tokens_per_day=None,
        quota_container_tokens_per_day=None,
        quota_default_storage_bytes=1000000 if storage_defaults else None,
        quota_container_storage_bytes=10000000 if storage_defaults else None,
    )
    policy = ChargeableAdmissionPolicy(
        identity_token_quota_configured=settings.quota_default_tokens_per_day is not None,
        container_token_quota_configured=settings.quota_container_tokens_per_day is not None,
        secret_wiring_hash=EMPTY_SECRET_WIRING_POLICY.canonical_hash,
    )
    engine = create_session_engine(f"sqlite:///{tmp_path / 'sessions.db'}")
    try:
        with engine.connect() as conn:
            identity_id = conn.execute(select(identities_table.c.identity_id)).scalar_one()
            quota_id = conn.execute(select(quota_policies_table.c.policy_id)).scalar_one_or_none()
        assert (quota_id is not None) is explicit_quota
        authority = SQLiteLocalSessionOperationAuthority(engine)
        session = authority.create_session_with_initial_fence(
            user_id=identity_id,
            title="quota",
            auth_provider_type="local",
            owner_instance_id="owner",
            lease_seconds=30,
        )
        context = authority.acquire(
            session_id=session.id,
            operation_kind=SessionOperationKind.COMPOSE,
            owner_instance_id="owner",
            lease_seconds=30,
        )
        decision = authority.mutate(
            context, lambda tx: tx.session.assess_chargeable_operation(policy=policy, operation=ChargeableOperation.COMPOSER)
        )
        if explicit_quota:
            assert decision.refusal_reason is AdmissionRefusalReason.TOKEN_ACCOUNTING_UNAVAILABLE
            assert decision.evidence.identity_policy_id == quota_id
            assert decision.evidence.quota_disposition is QuotaDisposition.ACCOUNTING_UNAVAILABLE
            # A revoked policy no longer applies: absence remains an explicit,
            # measured allowance, not a permanently enabled global switch.
            from datetime import UTC, datetime

            with engine.begin() as conn:
                conn.execute(update(quota_policies_table).values(revoked_at=datetime.now(UTC)))
            decision = authority.mutate(
                context, lambda tx: tx.session.assess_chargeable_operation(policy=policy, operation=ChargeableOperation.COMPOSER)
            )
        assert decision.allowed
        assert decision.evidence.identity_policy_id is None
        assert decision.evidence.container_policy_id is None
        assert decision.evidence.quota_disposition is QuotaDisposition.NOT_CONFIGURED
        authority.release(context)
    finally:
        engine.dispose()
