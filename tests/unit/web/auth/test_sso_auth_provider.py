"""SsoAuthProvider: the bearer authority for an SSO deployment.

It authenticates ELSPETH session tokens through the issuer that minted them,
and nothing else -- an IdP token, a token for another deployment, or a token
whose identity may no longer act is refused.
"""

from __future__ import annotations

import pytest

from elspeth.web.auth.models import AuthenticationError
from elspeth.web.auth.session_token import SessionTokenIssuer
from elspeth.web.auth.sso import SsoAuthProvider
from elspeth.web.sessions.identity_repository import IdentityRecord

SIGNING_KEY = b"sso-auth-provider-test-signing-key!!"
AUDIENCE = "https://elspeth.example.gov.au"


class _Identities:
    """A tiny substrate double: which identity ids exist, and whether each may act."""

    def __init__(self) -> None:
        self.rows: dict[str, IdentityRecord] = {}

    def add(self, identity_id: str, *, access_state: str = "active") -> None:
        self.rows[identity_id] = IdentityRecord(
            identity_id=identity_id,
            provider="vanguard",
            subject="ada@example.gov.au",
            username="ada@example.gov.au",
            access_state=access_state,
        )

    def read(self, identity_id: str) -> IdentityRecord | None:
        return self.rows.get(identity_id)

    def is_active(self, identity_id: str) -> bool:
        record = self.read(identity_id)
        return record is not None and record.is_active


@pytest.fixture
def identities() -> _Identities:
    return _Identities()


def _issuer(identities: _Identities, *, audience: str = AUDIENCE) -> SessionTokenIssuer:
    return SessionTokenIssuer(
        signing_key=SIGNING_KEY,
        provider="vanguard",
        audience=audience,
        token_expiry_hours=24,
        max_refresh_chain_hours=168,
        principal_is_active=identities.is_active,
    )


@pytest.mark.asyncio
async def test_the_issuers_own_token_for_an_active_identity_authenticates(identities: _Identities) -> None:
    identities.add("id-ada")
    issuer = _issuer(identities)
    provider = SsoAuthProvider(issuer=issuer, read_identity=identities.read)
    token = issuer.mint(identity_id="id-ada", username="ada@example.gov.au")

    identity = await provider.authenticate(token)
    assert identity.user_id == "id-ada"
    assert identity.username == "ada@example.gov.au"

    profile = await provider.get_user_info(token)
    assert profile.user_id == "id-ada"
    assert profile.username == "ada@example.gov.au"


@pytest.mark.asyncio
@pytest.mark.parametrize("access_state", ["pending", "disabled"])
async def test_an_identity_that_may_not_act_is_refused_on_the_next_request(identities: _Identities, access_state: str) -> None:
    """A disable is felt at the next request, not at the end of the token's life."""
    identities.add("id-ada")
    issuer = _issuer(identities)
    provider = SsoAuthProvider(issuer=issuer, read_identity=identities.read)
    token = issuer.mint(identity_id="id-ada", username="ada@example.gov.au")
    assert (await provider.authenticate(token)).user_id == "id-ada"

    identities.add("id-ada", access_state=access_state)
    with pytest.raises(AuthenticationError):
        await provider.authenticate(token)
    with pytest.raises(AuthenticationError):
        await provider.get_user_info(token)


@pytest.mark.asyncio
async def test_a_token_for_another_deployment_is_refused(identities: _Identities) -> None:
    identities.add("id-ada")
    provider = SsoAuthProvider(issuer=_issuer(identities), read_identity=identities.read)
    foreign = _issuer(identities, audience="https://staging.example.gov.au").mint(identity_id="id-ada", username="ada")
    with pytest.raises(AuthenticationError):
        await provider.authenticate(foreign)


@pytest.mark.asyncio
async def test_a_row_that_vanished_between_the_two_reads_is_a_refusal_not_a_blank_profile(identities: _Identities) -> None:
    identities.add("id-ada")
    issuer = _issuer(identities)
    token = issuer.mint(identity_id="id-ada", username="ada@example.gov.au")
    provider = SsoAuthProvider(issuer=issuer, read_identity=lambda _identity_id: None)
    with pytest.raises(AuthenticationError):
        await provider.get_user_info(token)
