"""Shared fixtures for auth provider tests.

Provides RSA keypair generation, JWKS response building, and JWT
signing for both OIDC and Entra test modules.
"""

from __future__ import annotations

import json
import pathlib

import jwt as pyjwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey, RSAPublicKey

from elspeth.web.auth.local import LocalAuthProvider
from elspeth.web.auth.models import IdentityClaims
from elspeth.web.auth.session_token import (
    DEFAULT_MAX_REFRESH_CHAIN_HOURS,
    DEFAULT_TOKEN_EXPIRY_HOURS,
    LOCAL_AUDIENCE,
    SessionTokenIssuer,
)
from elspeth.web.coordination.identity_authority import IdentityRetired, RepositoryIdentityAuthority, local_identity_retirer
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.identity_repository import EnsureIdentityOutcome, ensure_identity, local_identity_retirer, read_identity
from elspeth.web.sessions.schema import initialize_session_schema


@pytest.fixture
def rsa_keypair() -> tuple[RSAPrivateKey, RSAPublicKey]:
    """Generate an RSA key pair for signing test JWTs."""
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_key = private_key.public_key()
    return private_key, public_key


def build_rsa_jwk(public_key: RSAPublicKey, *, alg: str | None = "RS256") -> dict[str, object]:
    """Build a JWKS response dict from the test RSA public key."""
    # Use PyJWT's RSAAlgorithm to export the public key as a JWK dict
    jwk_json = pyjwt.algorithms.RSAAlgorithm.to_jwk(public_key)
    key_dict = json.loads(jwk_json)
    key_dict["kid"] = "test-key-1"
    key_dict["use"] = "sig"
    if alg is None:
        key_dict.pop("alg", None)
    else:
        key_dict["alg"] = alg
    return {"keys": [key_dict]}


@pytest.fixture
def jwks_response(rsa_keypair):
    """Build a JWKS response dict from the test RSA public key."""
    _, public_key = rsa_keypair
    return build_rsa_jwk(public_key)


def make_rsa_token(private_key, claims: dict[str, object], *, algorithm: str = "RS256") -> str:
    """Sign a JWT with an RSA private key using the requested algorithm.

    Not a fixture — a plain helper function imported explicitly by
    test modules that need it.
    """
    priv_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return pyjwt.encode(
        claims,
        priv_pem.decode(),
        algorithm=algorithm,
        headers={"kid": "test-key-1"},
    )


def make_rs256_token(private_key, claims: dict[str, object]) -> str:
    """Backward-compatible helper for the common RS256 case."""
    return make_rsa_token(private_key, claims, algorithm="RS256")


def build_local_auth_provider(
    db_path,
    *,
    registration_open: bool = True,
    signing_key: bytes = b"local-provider-test-signing-key!",
    audience: str = LOCAL_AUDIENCE,
    token_expiry_hours: int = DEFAULT_TOKEN_EXPIRY_HOURS,
    max_refresh_chain_hours: int = DEFAULT_MAX_REFRESH_CHAIN_HOURS,
    session_engine=None,
    quota_tokens_per_day: int | None = None,
    quota_storage_bytes: int | None = None,
) -> LocalAuthProvider:
    """Build a LocalAuthProvider wired to a real in-memory identity substrate.

    A local provider now needs THREE things: a credential store (``auth.db``),
    an identity substrate to resolve ``sub``, and a token issuer. Tests build
    them here rather than each constructing its own, so the wiring cannot
    drift between test modules -- and so a change to the provider's
    collaborators is one edit rather than fifty-eight.

    ``registration_open=True`` is the default because it matches the shipped
    ``registration_mode`` default: a first login is admitted immediately.
    Pass ``False`` to exercise the D12 pending wall.

    The engine is REAL, not a stub. The behaviours these tests depend on --
    a repeat login finding its own identity, an activation writing a quota
    row -- are arbitrated by constraints, and a stub arbitrates nothing.

    FILE-BACKED, not ``:memory:``. The session engine pools per THREAD
    (SingletonThreadPool), and admission runs inside ``run_sync_in_worker``,
    so an in-memory database would hand the worker thread its own empty copy
    and every login would fail with "no such table: identities".
    """
    if session_engine is not None:
        engine = session_engine
    else:
        sessions_db = pathlib.Path(db_path).parent / "identity-substrate.db"
        engine = create_session_engine(f"sqlite:///{sessions_db}")
        initialize_session_schema(engine)

    # The substrate is reached only through its authority, as in app.py.
    authority = RepositoryIdentityAuthority(engine)

    def _principal_is_active(identity_id: str) -> bool:
        record = authority.read_identity(identity_id=identity_id)
        return record is not None and record.is_active

    def _record_nothing(_identity_id: str, _username: str, _quota_written: bool) -> None:
        # These tests exercise the provider, not the Landscape admission
        # pair; app.py binds the real recorder. Explicit, because the
        # authority refuses to guess that a caller audits nothing.
        return None

    def _record_no_retirement(_outcome: IdentityRetired) -> None:
        # Same decision as ``_record_nothing``: the provider tests do not
        # exercise the Landscape retirement row that app.py records.
        return None

    def _admit_identity(claims: IdentityClaims) -> EnsureIdentityOutcome:
        return authority.ensure_identity(
            claims=claims,
            activate=registration_open,
            quota_tokens_per_day=quota_tokens_per_day,
            quota_storage_bytes=quota_storage_bytes,
            record_admission=_record_nothing,
        )

    return LocalAuthProvider(
        db_path=db_path,
        token_issuer=SessionTokenIssuer(
            signing_key=signing_key,
            provider="local",
            audience=audience,
            token_expiry_hours=token_expiry_hours,
            max_refresh_chain_hours=max_refresh_chain_hours,
            principal_is_active=_principal_is_active,
        ),
        admit_identity=_admit_identity,
        retire_identity=local_identity_retirer(engine),
    )
