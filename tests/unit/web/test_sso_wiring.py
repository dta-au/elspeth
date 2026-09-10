"""The wired / unwired decision, and what a wired deployment binds.

``build_sso_wiring`` returning ``None`` is the ONE fact both the app factory
and readiness (missing fields named) read. For ``local`` it is the ordinary
answer; for a registered IdP profile it is a boot refusal, since step E
deleted the legacy bearer path and left no half-working alternative. These
tests pin that the decision derives from the profile registry's required
settings and nothing else, and that a wired deployment's runtime is built
from the profile rather than from a per-provider branch.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import SecretBytes

from elspeth.web.auth.claims import IdTokenClaims
from elspeth.web.auth.sso import SsoRuntime
from elspeth.web.config import WebSettings
from elspeth.web.coordination.identity_authority import RepositoryIdentityAuthority
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.schema import initialize_session_schema
from elspeth.web.sso_wiring import build_sso_wiring, resolve_sso_runtime, sso_missing_settings
from tests.helpers.fake_idp import FakeIdP

_COMPOSER: dict[str, Any] = {
    "composer_max_composition_turns": 15,
    "composer_max_discovery_turns": 10,
    "composer_timeout_seconds": 85.0,
    "composer_rate_limit_per_minute": 10,
    "shareable_link_signing_key": SecretBytes(b"\x00" * 32),
    "operator_metrics_bearer_token": "operator-metrics-token-for-tests-0001",
    "secret_key": "dev-secret",
}


def _oidc_wired(tmp_path: Path, idp: FakeIdP, **overrides: Any) -> WebSettings:
    base: dict[str, Any] = {
        "data_dir": tmp_path,
        "auth_provider": "oidc",
        "sso_issuer": idp.issuer,
        "sso_client_id": idp.client_id,
        "sso_client_secret": idp.client_secret,
        "sso_transaction_secret": "t" * 40,
        "public_base_url": "https://elspeth.example.gov.au",
        "compartment_id": "example-compartment",
        "quota_default_tokens_per_day": 100_000,
        "quota_default_storage_bytes": 1_000_000,
        **_COMPOSER,
    }
    base.update(overrides)
    return WebSettings(**base)


def _oidc_unconfigured(tmp_path: Path) -> WebSettings:
    """An ``oidc`` settings object whose required settings are all empty.

    ``WebSettings`` refuses this shape at construction -- ``_validate_auth_fields``
    raises ``auth_provider='oidc' requires: ...`` before such an object exists --
    so it is built by blanking a wired one through ``model_copy(update=...)``,
    which skips validators. That is not a shortcut around the model: it is the
    only way the object reaches ``sso_missing_settings`` in production too. The
    ``else`` arm in ``create_app`` that reports the missing names calls itself
    "the total boundary for a mocked or corrupted settings object" for exactly
    this reason, and readiness reads the same matrix.
    """
    return _oidc_wired(tmp_path, FakeIdP()).model_copy(
        update={
            "sso_client_id": None,
            "sso_client_secret": None,
            "sso_transaction_secret": None,
            "public_base_url": None,
            "compartment_id": None,
            "quota_default_tokens_per_day": None,
            "quota_default_storage_bytes": None,
            "sso_issuer": None,
        }
    )


@pytest.fixture
def substrate(tmp_path: Path):
    engine = create_session_engine(f"sqlite:///{tmp_path / 'sessions.db'}")
    initialize_session_schema(engine)
    return engine, RepositoryIdentityAuthority(engine)


def test_missing_settings_are_the_profiles_required_settings_not_configured(tmp_path: Path) -> None:
    assert sso_missing_settings(_oidc_unconfigured(tmp_path)) == (
        "sso_client_id",
        "sso_client_secret",
        "sso_transaction_secret",
        "public_base_url",
        "compartment_id",
        "quota_default_tokens_per_day",
        "quota_default_storage_bytes",
        "sso_issuer",
    )
    assert sso_missing_settings(_oidc_wired(tmp_path, FakeIdP())) == ()


def test_local_and_unwired_deployments_build_nothing(tmp_path: Path, substrate) -> None:
    engine, authority = substrate
    local = WebSettings(data_dir=tmp_path, auth_provider="local", **_COMPOSER)
    assert build_sso_wiring(local, session_engine=engine, identity_authority=authority, resolved_state_mode="sqlite-single") is None
    assert (
        build_sso_wiring(
            _oidc_unconfigured(tmp_path), session_engine=engine, identity_authority=authority, resolved_state_mode="sqlite-single"
        )
        is None
    )


@pytest.mark.asyncio
async def test_a_wired_deployment_resolves_its_runtime_by_discovery_under_the_profiles_policy(tmp_path: Path, substrate) -> None:
    engine, authority = substrate
    idp = FakeIdP()
    wiring = build_sso_wiring(
        _oidc_wired(tmp_path, idp), session_engine=engine, identity_authority=authority, resolved_state_mode="sqlite-single"
    )
    assert wiring is not None
    assert wiring.token_issuer.audience == "https://elspeth.example.gov.au"
    assert wiring.token_issuer.provider == "oidc"

    runtime = await resolve_sso_runtime(wiring, _oidc_wired(tmp_path, idp), transport=idp.transport())
    assert isinstance(runtime, SsoRuntime)
    assert runtime.client.endpoints.authorization_endpoint == idp.authorization_endpoint
    assert runtime.client.endpoints.jwks_uri == idp.jwks_uri
    assert runtime.client.redirect_uri == "https://elspeth.example.gov.au/api/auth/sso/callback"
    assert runtime.client.start_url == "https://elspeth.example.gov.au/api/auth/sso/start"
    assert runtime.client.userinfo is False
    assert runtime.validator._algorithms == ("RS256",)


@pytest.mark.asyncio
async def test_a_wired_deployment_takes_the_break_glass_override_without_discovery(tmp_path: Path, substrate) -> None:
    engine, authority = substrate
    idp = FakeIdP()
    settings = _oidc_wired(
        tmp_path,
        idp,
        sso_authorization_endpoint=f"{idp.issuer}/authorize",
        sso_token_endpoint=f"{idp.issuer}/token",
        sso_jwks_uri=f"{idp.issuer}/keys",
    )
    wiring = build_sso_wiring(settings, session_engine=engine, identity_authority=authority, resolved_state_mode="sqlite-single")
    assert wiring is not None

    def refuse(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"no request expected under the override, got {request.url}")

    runtime = await resolve_sso_runtime(wiring, settings, transport=httpx.MockTransport(refuse))
    assert runtime.client.endpoints.token_endpoint == f"{idp.issuer}/token"
    assert runtime.client.endpoints.userinfo_endpoint is None


@pytest.mark.asyncio
async def test_a_wired_deployments_first_login_lands_pending(tmp_path: Path, substrate) -> None:
    """D12: the IdP verified who the person is, not whether this container admits them."""
    engine, authority = substrate
    idp = FakeIdP()
    wiring = build_sso_wiring(
        _oidc_wired(tmp_path, idp), session_engine=engine, identity_authority=authority, resolved_state_mode="sqlite-single"
    )
    assert wiring is not None
    claims = wiring.map_identity(
        IdTokenClaims(issuer=idp.issuer, subject="ada", audience=idp.client_id, issued_at=1_700_000_000, expires_at=1_700_000_300), None
    )
    admitted = wiring.upsert_identity(claims)
    assert admitted.access_state == "pending"
    assert wiring.read_identity(admitted.identity_id) is not None
    assert wiring.token_issuer.mint(identity_id=admitted.identity_id, username=admitted.username)


# ── D20 bootstrap seed ───────────────────────────────────────────────────


def _seeded_settings(tmp_path: Path, idp: FakeIdP, *subjects: str) -> WebSettings:
    # The web app creates data_dir/runs at boot; the seed's audit rows go to
    # the Landscape there, so the fixture stands in for that boot step.
    (tmp_path / "runs").mkdir(exist_ok=True)
    return _oidc_wired(tmp_path, idp, sso_admin_subjects=subjects)


def _claims_for(wiring, idp: FakeIdP, subject: str):
    return wiring.map_identity(
        IdTokenClaims(issuer=idp.issuer, subject=subject, audience=idp.client_id, issued_at=1_700_000_000, expires_at=1_700_000_300),
        None,
    )


def _auth_event_rows(settings: WebSettings):
    from sqlalchemy import select

    from elspeth.core.landscape.database import LandscapeDB
    from elspeth.core.landscape.schema import auth_events_table

    with LandscapeDB.from_url(settings.get_landscape_url()) as db, db.read_only_connection() as conn:
        return conn.execute(select(auth_events_table).order_by(auth_events_table.c.occurred_at)).fetchall()


def test_a_listed_subject_becomes_the_first_admin_at_first_login_with_the_audit_pair(tmp_path: Path, substrate) -> None:
    """``sso_admin_subjects`` seeds a listed subject ONLY while the container has zero active human admins (spec D20)."""
    import json

    engine, authority = substrate
    idp = FakeIdP()
    settings = _seeded_settings(tmp_path, idp, "ada")
    wiring = build_sso_wiring(settings, session_engine=engine, identity_authority=authority, resolved_state_mode="sqlite-single")
    assert wiring is not None

    admitted = wiring.upsert_identity(_claims_for(wiring, idp, "ada"))

    assert admitted.access_state == "active"
    assert [grant.role for grant in authority.active_roles(identity_id=admitted.identity_id)] == ["admin"]
    assert authority.count_active_human_admins() == 1
    rows = _auth_event_rows(settings)
    assert [row.event_type for row in rows] == ["identity_activated", "role_granted", "quota_set"]
    assert {row.identity_id for row in rows} == {admitted.identity_id}
    assert all(row.request_id is None and row.client_host is None for row in rows), "no request to read in the login worker"
    activated = json.loads(rows[0].metadata_json)
    assert activated["actor"] == "operator" and activated["cause"] == "bootstrap"
    assert activated["on_behalf_of"] is None and activated["console_request_id"] is None
    assert json.loads(rows[1].metadata_json)["role"] == "admin"
    quota = json.loads(rows[2].metadata_json)
    assert (quota["tokens_per_day"], quota["storage_bytes"]) == (100_000, 1_000_000)

    # A repeat login by the bootstrapped admin is an ordinary login: still active, no second activation.
    again = wiring.upsert_identity(_claims_for(wiring, idp, "ada"))
    assert again.identity_id == admitted.identity_id and again.access_state == "active"
    assert len(_auth_event_rows(settings)) == 3


def test_the_seed_list_is_inert_once_an_active_human_admin_exists(tmp_path: Path, substrate) -> None:
    """A second listed subject lands pending like anyone else: the list never becomes a standing grant."""
    engine, authority = substrate
    idp = FakeIdP()
    settings = _seeded_settings(tmp_path, idp, "ada", "bob")
    wiring = build_sso_wiring(settings, session_engine=engine, identity_authority=authority, resolved_state_mode="sqlite-single")
    assert wiring is not None

    ada = wiring.upsert_identity(_claims_for(wiring, idp, "ada"))
    bob = wiring.upsert_identity(_claims_for(wiring, idp, "bob"))

    assert ada.access_state == "active"
    assert bob.access_state == "pending"
    assert authority.active_roles(identity_id=bob.identity_id) == ()
    assert authority.count_active_human_admins() == 1
    assert [row.event_type for row in _auth_event_rows(settings)] == ["identity_activated", "role_granted", "quota_set"]


def test_an_unlisted_first_login_lands_pending_even_with_no_admin(tmp_path: Path, substrate) -> None:
    engine, authority = substrate
    idp = FakeIdP()
    settings = _seeded_settings(tmp_path, idp, "ada")
    wiring = build_sso_wiring(settings, session_engine=engine, identity_authority=authority, resolved_state_mode="sqlite-single")
    assert wiring is not None

    carol = wiring.upsert_identity(_claims_for(wiring, idp, "carol"))

    assert carol.access_state == "pending"
    assert authority.count_active_human_admins() == 0
    assert not (tmp_path / "runs" / "audit.db").exists() or _auth_event_rows(settings) == []


@pytest.mark.asyncio
async def test_the_spec_pin_a_fresh_store_and_one_listed_subject_walk_to_a_token_with_exactly_admin(tmp_path: Path, substrate) -> None:
    """Spec §identity_roles [rev2.7]: fresh ``--init-schema`` store + one listed subject, start → callback → complete → token, roles == admin."""
    from urllib.parse import parse_qs, urlsplit

    import jwt as pyjwt

    from elspeth.web.auth.sso import CallbackQuery, authorization_redirect, complete_login, login_callback

    engine, authority = substrate
    idp = FakeIdP()
    settings = _seeded_settings(tmp_path, idp, "ada")
    wiring = build_sso_wiring(settings, session_engine=engine, identity_authority=authority, resolved_state_mode="sqlite-single")
    assert wiring is not None
    runtime = await resolve_sso_runtime(wiring, settings, transport=idp.transport())
    client = runtime.client

    # start
    redirect = authorization_redirect(
        authorization_endpoint=client.endpoints.authorization_endpoint,
        client_id=client.client_id,
        redirect_uri=client.redirect_uri,
        scopes=("openid",),
        transaction_secret=client.transaction_secret,
        provider=client.provider,
    )
    sent = parse_qs(urlsplit(redirect.location).query)
    # callback
    code = idp.authorize(nonce=sent["nonce"][0], subject="ada")
    seen: list[str] = []
    location = await login_callback(
        CallbackQuery(code=code, state=sent["state"][0], error=None),
        redirect.cookie_value,
        client=client,
        validator=runtime.validator,
        claim_checks=runtime.claim_checks,
        map_identity=runtime.map_identity,
        upsert_identity=runtime.upsert_identity,
        record_login=lambda identity: seen.append(identity.identity_id),
        handoffs=runtime.handoffs,
        request_id="walk-1",
        transport=runtime.transport,
    )
    # The code rides in the fragment as ``#/auth/callback?code=...`` (one SPA route, one parser).
    handoff = parse_qs(urlsplit("x://x/" + urlsplit(location).fragment).query)["code"][0]
    # complete
    session = complete_login(
        handoff,
        handoffs=runtime.handoffs,
        read_identity=runtime.read_identity,
        issuer=runtime.issuer,
        record_token_issued=lambda identity, token, login_request_id: None,
    )

    identity_id = pyjwt.decode(session.access_token, options={"verify_signature": False})["sub"]
    assert seen == [identity_id]
    assert [grant.role for grant in authority.active_roles(identity_id=identity_id)] == ["admin"]
    assert authority.count_active_human_admins() == 1
    assert [row.event_type for row in _auth_event_rows(settings)] == ["identity_activated", "role_granted", "quota_set"]


# ── R3: the rebound refusal reaches the login path ───────────────────────


def _identity_claims(subject: str, email: str):
    from elspeth.web.auth.models import IdentityClaims

    return IdentityClaims(provider="oidc", subject=subject, username=subject, display_name=None, email=email)


def test_a_rebound_login_is_refused_at_the_wiring_and_writes_the_system_disable(tmp_path: Path, substrate) -> None:
    """R3's seam: the authority writes the state, the wiring turns it into a refusal.

    ``_upsert_identity`` is the ONLY place this can be raised -- the authority
    cannot import the login errors -- so a rebound that never became an
    ``SsoIdentityRebound`` would be a silently admitted login.
    """
    from elspeth.web.auth.sso import SsoIdentityRebound

    engine, authority = substrate
    idp = FakeIdP()
    settings = _oidc_wired(tmp_path, idp)
    (tmp_path / "runs").mkdir(exist_ok=True)
    wiring = build_sso_wiring(settings, session_engine=engine, identity_authority=authority, resolved_state_mode="sqlite-single")
    assert wiring is not None

    admitted = wiring.upsert_identity(_identity_claims("ada", "ada@old.example"))
    assert admitted.access_state == "pending"

    with pytest.raises(SsoIdentityRebound) as refused:
        wiring.upsert_identity(_identity_claims("ada", "ada@new.example"))

    # The category is what the browser is sent and what the auth_failure row
    # files the refusal under.
    assert refused.value.category == "sso_identity_rebound"
    # ...and the detail names neither address.
    assert "@" not in refused.value.detail

    disabled = [row for row in _auth_event_rows(settings) if row.event_type == "identity_disabled"]
    assert len(disabled) == 1
    assert disabled[0].identity_id == admitted.identity_id
    assert disabled[0].outcome == "success"
    # No request columns: the disable is the authority's act, not the
    # request's. The refused login writes its own row with those.
    assert disabled[0].request_id is None


def _backdate_login(engine, identity_id: str, *, days: int) -> None:
    """Move ``last_login_at`` into the past; the database clock cannot be moved from here."""
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import update

    from elspeth.web.sessions.models import identities_table

    with engine.begin() as conn:
        conn.execute(
            update(identities_table)
            .where(identities_table.c.identity_id == identity_id)
            .values(
                last_login_at=datetime.now(UTC) - timedelta(days=days),
                activated_at=datetime.now(UTC) - timedelta(days=days),
            )
        )


def test_a_dormant_login_is_re_pended_at_the_wiring_and_writes_the_system_disable(tmp_path: Path, substrate) -> None:
    """R9's seam: the container's window reaches the authority, and the re-pend is audited.

    ``identity_dormancy_days`` was a validated-only setting until this landed;
    the assertion that matters is that the wiring PASSES it, because an
    authority that never receives it cannot enforce R9 no matter what it does
    with the value.
    """
    from sqlalchemy import update

    from elspeth.web.sessions.models import identities_table

    engine, authority = substrate
    idp = FakeIdP()
    settings = _oidc_wired(tmp_path, idp, identity_dormancy_days=30)
    (tmp_path / "runs").mkdir(exist_ok=True)
    wiring = build_sso_wiring(settings, session_engine=engine, identity_authority=authority, resolved_state_mode="sqlite-single")
    assert wiring is not None

    admitted = wiring.upsert_identity(_identity_claims("ada", "ada@example.com"))
    # R9 acts on ACTIVE rows; an SSO first login lands pending (D12), so this
    # stands in for the administrator's activation.
    with engine.begin() as conn:
        conn.execute(update(identities_table).where(identities_table.c.identity_id == admitted.identity_id).values(access_state="active"))
    _backdate_login(engine, admitted.identity_id, days=31)

    bound = wiring.upsert_identity(_identity_claims("ada", "ada@example.com"))

    # The state gate refuses the login from here: the row is pending, so
    # ``admit`` raises SsoAccessPending. R9 needs no refusal of its own.
    assert bound.access_state == "pending"
    disabled = [row for row in _auth_event_rows(settings) if row.event_type == "identity_disabled"]
    assert len(disabled) == 1
    assert disabled[0].outcome == "success"
    assert disabled[0].identity_id == admitted.identity_id
    metadata = json.loads(disabled[0].metadata_json)
    assert (metadata["cause"], metadata["state"], metadata["dormancy_days"]) == ("dormant", "pending", 30)


def test_the_last_admins_dormancy_is_exempted_and_the_exemption_is_the_only_evidence(tmp_path: Path, substrate, monkeypatch) -> None:
    """D34's audit must succeed before the login erases its dormancy evidence."""
    from elspeth.web.auth.audit import AuthAuditRecorder

    engine, authority = substrate
    idp = FakeIdP()
    settings = _oidc_wired(tmp_path, idp, identity_dormancy_days=30, sso_admin_subjects=["root"])
    (tmp_path / "runs").mkdir(exist_ok=True)
    wiring = build_sso_wiring(settings, session_engine=engine, identity_authority=authority, resolved_state_mode="sqlite-single")
    assert wiring is not None

    seeded = wiring.upsert_identity(_identity_claims("root", "root@example.com"))
    assert seeded.access_state == "active"
    assert authority.count_active_human_admins() == 1
    _backdate_login(engine, seeded.identity_id, days=400)
    previous_login = authority.read_identity_summary(identity_id=seeded.identity_id).last_login_at

    def refuse_audit(*args, **kwargs):
        raise RuntimeError("exemption audit unavailable")

    with monkeypatch.context() as patch:
        patch.setattr(AuthAuditRecorder, "record_identity_dormancy_exempted", refuse_audit)
        with pytest.raises(RuntimeError, match="exemption audit unavailable"):
            wiring.upsert_identity(_identity_claims("root", "root@example.com"))
    assert authority.read_identity_summary(identity_id=seeded.identity_id).last_login_at == previous_login

    bound = wiring.upsert_identity(_identity_claims("root", "root@example.com"))

    # NOT re-pended: the login proceeds and the container keeps its admin.
    assert bound.access_state == "active"
    assert authority.count_active_human_admins() == 1
    exempted = [row for row in _auth_event_rows(settings) if row.failure_category == "dormancy_last_admin_exempt"]
    assert len(exempted) == 1
    assert (exempted[0].event_type, exempted[0].outcome) == ("identity_disabled", "failure")
    assert exempted[0].identity_id == seeded.identity_id
    # No re-pend row: a success-outcome dormancy row would assert a state
    # change that did not happen.
    assert [row.outcome for row in _auth_event_rows(settings) if row.event_type == "identity_disabled"] == ["failure"]
