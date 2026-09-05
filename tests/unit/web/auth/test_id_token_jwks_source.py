"""Where the validator's keys come from, once the endpoints are resolved.

Spec §Endpoint policy [rev2] says the generalised endpoint check is applied
"at startup and on every JWKS refresh". That holds only if a refresh fetches
the ``jwks_uri`` that WAS checked. A validator that re-discovers a key URL
under a rule of its own — the pre-sprint path, which re-read the discovery
document on every refresh and applied a same-origin rule that refuses
Google — has not been covered by the startup check at all; it has merely
been near it.

So these pin three things about a validator built with a resolved
``jwks_uri``: it fetches exactly that URL, it does so through the transport
it was given (so a test can stand a fake provider in front of it without
patching ``httpx``), and it never reads discovery. The redirect case is the
load-bearing one: a JWKS fetch that follows a redirect verifies signatures
against whoever the redirect names.
"""

from __future__ import annotations

import httpx
import pytest

from elspeth.web.auth.id_token import JWKSTokenValidator
from elspeth.web.auth.models import AuthProviderUnavailable
from tests.helpers.fake_idp import FakeIdP


@pytest.fixture
def idp() -> FakeIdP:
    return FakeIdP()


def _recording_transport(idp: FakeIdP, seen: list[str]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return idp.respond(request)

    return httpx.MockTransport(handler)


def _validator(idp: FakeIdP, transport: httpx.AsyncBaseTransport) -> JWKSTokenValidator:
    return JWKSTokenValidator(issuer=idp.issuer, audience=idp.client_id, algorithms=("RS256",), jwks_uri=idp.jwks_uri, transport=transport)


@pytest.mark.asyncio
async def test_a_resolved_jwks_uri_is_fetched_directly_and_discovery_is_never_read(idp: FakeIdP) -> None:
    seen: list[str] = []
    validator = _validator(idp, _recording_transport(idp, seen))

    jwks = await validator.ensure_jwks()

    assert seen == [idp.jwks_uri], "exactly one fetch, of exactly the resolved URL; discovery is not re-read"
    assert [entry.key_id for entry in jwks.entries] == [key["kid"] for key in idp.jwks_document()["keys"]]


@pytest.mark.asyncio
async def test_the_full_decode_with_refresh_accepts_the_well_behaved_provider(idp: FakeIdP) -> None:
    """Positive control for the refresh path. Without it, every refusal below is vacuous."""
    validator = _validator(idp, idp.transport())
    code = idp.authorize(nonce="n-1", subject="ada")
    token = idp.mint_id_token(idp.codes[code])

    claims = await validator.decode_id_token_with_refresh(token, audience=idp.client_id, nonce="n-1", client_id=idp.client_id)

    assert claims.subject == "ada"


@pytest.mark.asyncio
async def test_a_forced_refresh_of_an_unchanged_document_is_a_new_instance(idp: FakeIdP) -> None:
    """The refresh path tells "someone already replaced the cache" from "still the set I
    found insufficient" by IDENTITY, not by value: a re-fetch of the same document is a
    new ``JwkSet``, equal to the old one and observably not it."""
    validator = _validator(idp, idp.transport())
    first = await validator.ensure_jwks()

    second = await validator.ensure_jwks(refresh_if_unchanged=first)

    assert second == first, "same document, so equal as a value"
    assert second is not first, "but the refresh produced a new instance"
    third = await validator.ensure_jwks(refresh_if_unchanged=first)
    assert third is second, "a caller still holding the OLD set gets the replacement, without another fetch"


@pytest.mark.asyncio
async def test_a_redirect_at_the_jwks_uri_is_not_followed(idp: FakeIdP) -> None:
    """A followed redirect would verify signatures against whoever the redirect names."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if str(request.url) == idp.jwks_uri:
            return httpx.Response(302, headers={"location": "https://attacker.example.net/keys"})
        return httpx.Response(200, json={"keys": []})

    validator = _validator(idp, httpx.MockTransport(handler))

    with pytest.raises(AuthProviderUnavailable):
        await validator.ensure_jwks()

    assert seen == [idp.jwks_uri], "the redirect target was never requested"


@pytest.mark.asyncio
async def test_a_key_miss_refreshes_from_the_same_resolved_uri(idp: FakeIdP) -> None:
    """Key rotation: the refresh a key miss forces goes to the resolved URL, not to discovery."""
    rotated = FakeIdP(issuer=idp.issuer, client_id=idp.client_id)  # same issuer, a fresh key
    seen: list[str] = []
    documents = [idp.jwks_document(), rotated.jwks_document()]

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        assert str(request.url) == idp.jwks_uri
        return httpx.Response(200, json=documents.pop(0))

    validator = _validator(idp, httpx.MockTransport(handler))
    await validator.ensure_jwks()  # caches the OLD key set
    code = rotated.authorize(nonce="n-1", subject="ada")
    token = rotated.mint_id_token(rotated.codes[code])  # signed with the NEW key

    claims = await validator.decode_id_token_with_refresh(token, audience=idp.client_id, nonce="n-1", client_id=idp.client_id)

    assert claims.subject == "ada"
    assert seen == [idp.jwks_uri, idp.jwks_uri], "one initial fetch, one key-miss refresh, both to the resolved URL"
