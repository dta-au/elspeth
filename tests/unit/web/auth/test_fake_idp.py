"""The fake IdP's own tests.

A harness nobody has tested is worse than none: every login test downstream
inherits its defects, and a harness that silently serves the WRONG thing turns
a passing suite into evidence for a claim nobody checked.

Two properties matter and they pull in opposite directions:

* the well-behaved provider must be genuinely well-behaved — a real signature
  over a real JWK, an issuer that matches, a nonce that round-trips — or a
  login test that passes proves only that the harness agreed with itself;
* each deviation must produce exactly the malformation it names and nothing
  else, or an adversarial test refuses for a reason the test did not intend
  and its assertion passes for the wrong cause.

So these tests verify the harness against the REAL verification code
(``JWKSTokenValidator.decode_id_token``) wherever it exists, rather than
re-implementing a second opinion here.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time

import httpx
import jwt
import pytest

from elspeth.web.auth.id_token import JWKSTokenValidator
from elspeth.web.auth.models import AuthenticationError
from tests.helpers.fake_idp import FakeIdP


@pytest.fixture
def idp() -> FakeIdP:
    return FakeIdP()


def _client(provider: FakeIdP) -> httpx.Client:
    return httpx.Client(transport=provider.transport())


# --------------------------------------------------------------------------
# The well-behaved provider. If these fail, nothing downstream means anything.
# --------------------------------------------------------------------------


def test_discovery_declares_the_configured_issuer(idp: FakeIdP) -> None:
    with _client(idp) as client:
        document = client.get(idp.discovery_url).json()
    assert document["issuer"] == idp.issuer
    assert document["jwks_uri"] == idp.jwks_uri


def test_discovery_serves_every_endpoint_the_login_needs(idp: FakeIdP) -> None:
    """Missing one would fail startup discovery for a reason unrelated to the test."""
    with _client(idp) as client:
        document = client.get(idp.discovery_url).json()
    for key in ("authorization_endpoint", "token_endpoint", "userinfo_endpoint", "jwks_uri"):
        assert document[key].startswith(idp.issuer), key


def test_the_published_jwk_actually_verifies_the_signature(idp: FakeIdP) -> None:
    """The property that makes forgery tests meaningful.

    If the JWK did not match the signing key, every token would be rejected
    and every refusal test would pass without exercising anything.
    """
    code = idp.authorize(nonce="n-1", subject="ada")
    token = idp.mint_id_token(idp.codes[code])

    with _client(idp) as client:
        jwks = client.get(idp.jwks_uri).json()
    key = jwt.PyJWK.from_dict(jwks["keys"][0]).key

    decoded = jwt.decode(token, key=key, algorithms=["RS256"], audience=idp.client_id, issuer=idp.issuer)
    assert decoded["sub"] == "ada"
    assert decoded["nonce"] == "n-1"


def test_the_kid_in_the_header_matches_the_published_key(idp: FakeIdP) -> None:
    """Key lookup is by kid; a mismatch would exercise the refresh path instead."""
    code = idp.authorize(nonce="n-1")
    header = jwt.get_unverified_header(idp.mint_id_token(idp.codes[code]))
    with _client(idp) as client:
        assert header["kid"] == client.get(idp.jwks_uri).json()["keys"][0]["kid"]


def test_a_code_redeems_once_and_then_is_refused(idp: FakeIdP) -> None:
    """Single use is enforced by the PROVIDER, as a real IdP does.

    This is what makes a replayed transaction cookie harmless without ELSPETH
    being the only thing standing between an attacker and a session.
    """
    code = idp.authorize(nonce="n-1")
    with _client(idp) as client:
        first = client.post(idp.token_endpoint, content=f"code={code}")
        second = client.post(idp.token_endpoint, content=f"code={code}")

    assert first.status_code == 200
    assert first.json()["token_type"] == "Bearer"
    assert second.status_code == 400
    assert second.json()["error"] == "invalid_grant"


def test_an_unknown_code_is_refused(idp: FakeIdP) -> None:
    with _client(idp) as client:
        assert client.post(idp.token_endpoint, content="code=never-issued").status_code == 400


def test_userinfo_reports_the_subject_that_was_redeemed(idp: FakeIdP) -> None:
    code = idp.authorize(nonce="n-1", subject="ada")
    with _client(idp) as client:
        client.post(idp.token_endpoint, content=f"code={code}")
        body = client.get(idp.userinfo_endpoint).json()
    assert body["sub"] == "ada"


def test_an_unserved_endpoint_404s_rather_than_escaping(idp: FakeIdP) -> None:
    """A login reaching for an unconfigured endpoint must fail in the test."""
    with _client(idp) as client:
        assert client.get(f"{idp.issuer}/oauth2/somewhere-else").status_code == 404


# --------------------------------------------------------------------------
# Each deviation produces exactly the malformation it names.
# --------------------------------------------------------------------------


def test_issuer_override_changes_only_discovery(idp: FakeIdP) -> None:
    """The mix-up shape: discovery disagrees with configuration."""
    idp.discovery_issuer_override = "https://attacker.example.com"
    with _client(idp) as client:
        assert client.get(idp.discovery_url).json()["issuer"] == "https://attacker.example.com"
    # The TOKEN still claims the real issuer — otherwise the test would be
    # refused at the token's iss check rather than at the discovery check.
    code = idp.authorize(nonce="n-1")
    payload = jwt.decode(idp.mint_id_token(idp.codes[code]), options={"verify_signature": False})
    assert payload["iss"] == idp.issuer


def test_key_type_override_changes_only_kty(idp: FakeIdP) -> None:
    idp.jwks_key_type_override = "oct"
    with _client(idp) as client:
        jwk = client.get(idp.jwks_uri).json()["keys"][0]
    assert jwk["kty"] == "oct"
    assert jwk["kid"] and jwk["alg"] == "RS256"


def test_none_algorithm_produces_an_unsigned_token(idp: FakeIdP) -> None:
    idp.id_token_algorithm = "none"
    code = idp.authorize(nonce="n-1")
    token = idp.mint_id_token(idp.codes[code])
    assert jwt.get_unverified_header(token)["alg"] == "none"
    assert token.rsplit(".", 1)[1] == "", "an alg=none token must carry an empty signature"


def test_hs256_confusion_signs_with_the_public_key_as_the_secret(idp: FakeIdP) -> None:
    """The real attack shape: the JWKS is public, so it is available as a key.

    The HMAC is recomputed HERE rather than via ``jwt.decode``, for the same
    reason the harness hand-builds it: PyJWT refuses an asymmetric key as an
    HMAC secret (InvalidKeyError), which is a defence in the library. Going
    through PyJWT would therefore test PyJWT's guard, not ELSPETH's — and
    ELSPETH's guard is the one that has to hold, because a client that pins
    algorithms less carefully gets no help from that refusal.
    """
    idp.id_token_algorithm = "HS256"
    code = idp.authorize(nonce="n-1")
    token = idp.mint_id_token(idp.codes[code])

    assert jwt.get_unverified_header(token)["alg"] == "HS256"

    signing_input, _, encoded_signature = token.rpartition(".")
    expected = hmac.new(idp.public_key_pem().encode("ascii"), signing_input.encode("ascii"), hashlib.sha256).digest()
    actual = base64.urlsafe_b64decode(encoded_signature + "=" * (-len(encoded_signature) % 4))
    assert hmac.compare_digest(expected, actual), "the forged token must be a VALID HS256 JWS over the public key"

    payload = json.loads(base64.urlsafe_b64decode(signing_input.split(".")[1] + "=="))
    assert payload["sub"] and payload["iss"] == idp.issuer


def test_list_audience_without_azp_is_exactly_that(idp: FakeIdP) -> None:
    idp.audience_override = [idp.client_id, "some-other-client"]
    idp.omit_azp = True
    code = idp.authorize(nonce="n-1")
    payload = jwt.decode(idp.mint_id_token(idp.codes[code]), options={"verify_signature": False})

    assert isinstance(payload["aud"], list)
    assert "azp" not in payload


def test_list_audience_carries_azp_by_default(idp: FakeIdP) -> None:
    """The positive control: a list aud alone must not be the deviation."""
    idp.audience_override = [idp.client_id, "some-other-client"]
    code = idp.authorize(nonce="n-1")
    payload = jwt.decode(idp.mint_id_token(idp.codes[code]), options={"verify_signature": False})
    assert payload["azp"] == idp.client_id


def test_userinfo_subject_override_disagrees_with_the_token(idp: FakeIdP) -> None:
    idp.userinfo_subject_override = "someone-else"
    code = idp.authorize(nonce="n-1", subject="ada")
    with _client(idp) as client:
        client.post(idp.token_endpoint, content=f"code={code}")
        body = client.get(idp.userinfo_endpoint).json()
    token_sub = jwt.decode(idp.mint_id_token(idp.codes[code]), options={"verify_signature": False})["sub"]

    assert body["sub"] == "someone-else"
    assert token_sub == "ada"


def test_omitting_the_nonce_omits_only_the_nonce(idp: FakeIdP) -> None:
    idp.include_nonce = False
    code = idp.authorize(nonce="n-1")
    payload = jwt.decode(idp.mint_id_token(idp.codes[code]), options={"verify_signature": False})
    assert "nonce" not in payload
    assert payload["sub"] and payload["aud"] == idp.client_id


def test_two_providers_have_independent_keys(idp: FakeIdP) -> None:
    """A key-mismatch test makes a SECOND provider rather than mutating one."""
    other = FakeIdP()
    with _client(idp) as a, _client(other) as b:
        assert a.get(idp.jwks_uri).json()["keys"][0]["n"] != b.get(other.jwks_uri).json()["keys"][0]["n"]


def test_an_expired_token_can_be_minted_deterministically(idp: FakeIdP) -> None:
    code = idp.authorize(nonce="n-1")
    token = idp.mint_id_token(idp.codes[code], now=int(time.time()) - 3600)
    payload = jwt.decode(token, options={"verify_signature": False})
    assert payload["exp"] < int(time.time())


def test_token_requests_are_recorded_for_assertion(idp: FakeIdP) -> None:
    """The login must send client_secret_basic; tests need the request to check."""
    code = idp.authorize(nonce="n-1")
    with _client(idp) as client:
        client.post(idp.token_endpoint, content=f"code={code}", headers={"authorization": "Basic abc"})
    assert len(idp.token_requests) == 1
    assert idp.token_requests[0].headers["authorization"] == "Basic abc"


def test_the_jwk_is_json_serialisable_as_served(idp: FakeIdP) -> None:
    """It crosses a JSON boundary in every real use; prove it survives one."""
    assert json.loads(json.dumps(idp.jwks_document()))["keys"][0]["kty"] == "RSA"


class TestHarnessAgainstTheRealValidator:
    """The harness's contract, checked against production verification.

    Everything above proves the fake IdP serves what it claims. This proves
    the claim is USEFUL: the real ``JWKSTokenValidator.decode_id_token``
    accepts the well-behaved provider and refuses each deviation, each for
    the reason that deviation names.

    Without the positive control, "everything is refused" would be equally
    consistent with a harness that emits garbage. Without the per-deviation
    refusals, the harness could be serving one malformation while a test
    believes it is exercising another.
    """

    @staticmethod
    def _decode(idp: FakeIdP, *, nonce: str = "n-1", subject: str = "ada"):
        validator = JWKSTokenValidator(issuer=idp.issuer, audience=idp.client_id, algorithms=("RS256",))
        code = idp.authorize(nonce=nonce, subject=subject)
        token = idp.mint_id_token(idp.codes[code])
        return validator.decode_id_token(
            token,
            JWKSTokenValidator._validate_jwks_document(idp.jwks_document()),
            audience=idp.client_id,
            nonce=nonce,
            client_id=idp.client_id,
        )

    def test_the_well_behaved_provider_is_accepted(self, idp: FakeIdP) -> None:
        """THE POSITIVE CONTROL. If this fails, every refusal below is vacuous."""
        claims = self._decode(idp)
        assert claims.subject == "ada"
        assert claims.issuer == idp.issuer

    def test_an_unsigned_token_is_refused(self, idp: FakeIdP) -> None:
        idp.id_token_algorithm = "none"
        with pytest.raises(AuthenticationError, match="Invalid token"):
            self._decode(idp)

    def test_algorithm_confusion_is_refused(self, idp: FakeIdP) -> None:
        """The header says HS256; the profile's pinned list says RS256 only."""
        idp.id_token_algorithm = "HS256"
        with pytest.raises(AuthenticationError, match="Invalid token"):
            self._decode(idp)

    def test_a_non_asymmetric_jwk_is_refused_before_use(self, idp: FakeIdP) -> None:
        """The fake publishes ONE key, so an ``oct`` entry leaves the document with no usable
        key and the JWKS boundary refuses it whole; the per-key type gate behind a usable
        sibling is pinned in ``test_id_token_decode``."""
        idp.jwks_key_type_override = "oct"
        with pytest.raises(AuthenticationError, match="unusable key entries"):
            self._decode(idp)

    def test_a_token_without_a_nonce_is_refused(self, idp: FakeIdP) -> None:
        idp.include_nonce = False
        with pytest.raises(AuthenticationError, match="Invalid token"):
            self._decode(idp)

    def test_a_list_audience_without_azp_is_refused(self, idp: FakeIdP) -> None:
        idp.audience_override = [idp.client_id, "some-other-client"]
        idp.omit_azp = True
        with pytest.raises(AuthenticationError, match="authorized party"):
            self._decode(idp)

    def test_a_list_audience_with_azp_is_accepted(self, idp: FakeIdP) -> None:
        """The other side of the boundary: a list aud alone is legitimate."""
        idp.audience_override = [idp.client_id, "some-other-client"]
        assert self._decode(idp).subject == "ada"

    def test_a_replayed_nonce_from_another_login_is_refused(self, idp: FakeIdP) -> None:
        """The nonce binds the token to THIS browser's login attempt."""
        validator = JWKSTokenValidator(issuer=idp.issuer, audience=idp.client_id, algorithms=("RS256",))
        code = idp.authorize(nonce="nonce-from-a-different-login", subject="ada")
        token = idp.mint_id_token(idp.codes[code])
        with pytest.raises(AuthenticationError, match="Invalid token"):
            validator.decode_id_token(
                token,
                JWKSTokenValidator._validate_jwks_document(idp.jwks_document()),
                audience=idp.client_id,
                nonce="the-nonce-this-browser-actually-sent",
                client_id=idp.client_id,
            )

    def test_a_token_signed_by_a_different_provider_is_refused(self, idp: FakeIdP) -> None:
        """Two providers, independent keys — the JWKS must not verify a stranger."""
        other = FakeIdP(issuer=idp.issuer, client_id=idp.client_id)
        validator = JWKSTokenValidator(issuer=idp.issuer, audience=idp.client_id, algorithms=("RS256",))
        code = other.authorize(nonce="n-1", subject="ada")
        token = other.mint_id_token(other.codes[code])
        with pytest.raises(AuthenticationError, match="Invalid token"):
            validator.decode_id_token(
                token,
                JWKSTokenValidator._validate_jwks_document(idp.jwks_document()),  # OUR provider's keys, THEIR token
                audience=idp.client_id,
                nonce="n-1",
                client_id=idp.client_id,
            )
