"""An in-process OpenID Provider, real enough to be worth failing against.

Spec: docs/specs/2026-09-02-pluggable-sso-design.md §Testing — "In-process
fake IdP (discovery, JWKS, token, userinfo)".

WHY THIS IS NOT A MOCK
----------------------
The login path's whole job is to distrust what an IdP sends. A mock that
returns whatever the test asks for cannot exercise that: it agrees with the
code under test by construction, so the assertions pass whether or not the
verification exists. This serves the four documents a real provider serves,
signs its ID tokens with a real RSA key, and publishes the matching public
JWK — so a test that forges a token has to defeat a signature, and a test
that expects a rejection gets one for the reason it named.

The adversarial cases (algorithm confusion, list ``aud`` without ``azp``,
userinfo ``sub`` mismatch, nonce replay) are therefore expressed as
CONFIGURATION of a working provider rather than as hand-built payloads.
That distinction matters: a hand-built payload proves the parser rejects a
string, while a misconfigured provider proves the login refuses a real
counterparty behaving badly.

WHAT IT DELIBERATELY DOES NOT DO
--------------------------------
No network listener. The transport is an ``httpx.MockTransport`` the caller
mounts on the client under test, so nothing binds a port, nothing races
another test for one, and a request to an endpoint this provider does not
serve fails loudly instead of escaping to the internet.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import httpx
import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

_DEFAULT_ISSUER = "https://idp.example.gov.au"
_TOKEN_TTL_SECONDS = 300


@dataclass
class IssuedCode:
    """One authorization code the provider has handed out."""

    code: str
    nonce: str
    subject: str
    claims: dict[str, Any]
    redeemed: bool = False


@dataclass
class FakeIdP:
    """A working OpenID Provider whose misbehaviour is configurable.

    Every knob below names a REAL failure a production IdP (or something
    impersonating one) can present. Defaults are the well-behaved provider;
    a test opts into exactly one deviation so a refusal can only be
    attributed to that deviation.
    """

    issuer: str = _DEFAULT_ISSUER
    client_id: str = "elspeth-test-client"
    client_secret: str = "elspeth-test-secret"

    # --- deviations, all default to "behaves correctly" -----------------
    discovery_issuer_override: str | None = None
    """Serve a different ``issuer`` than configured — the mix-up attack."""

    jwks_key_type_override: str | None = None
    """Publish the JWK under a different ``kty`` (e.g. ``oct``)."""

    id_token_algorithm: str = "RS256"
    """Sign with something else — ``none``, ``HS256`` — for alg confusion."""

    audience_override: Any = None
    """Force ``aud``; a list without ``azp`` is the case the spec names."""

    omit_azp: bool = False
    include_nonce: bool = True
    userinfo_subject_override: str | None = None
    """Return a userinfo ``sub`` that disagrees with the ID token's."""

    userinfo_content_type: str = "application/json"
    userinfo_extra_claims: dict[str, Any] = field(default_factory=dict)
    token_response_override: dict[str, Any] | None = None

    # --- state ----------------------------------------------------------
    codes: dict[str, IssuedCode] = field(default_factory=dict)
    token_requests: list[httpx.Request] = field(default_factory=list)

    def __post_init__(self) -> None:
        # 2048 is the smallest size the JWK path treats as production-shaped;
        # generating per instance keeps tests independent, and a test that
        # wants a key mismatch makes a SECOND provider rather than mutating
        # this one's key underneath a token it already signed.
        self._private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self._kid = f"fake-idp-{uuid.uuid4().hex[:8]}"

    # ── the four documents ──────────────────────────────────────────────

    @property
    def discovery_url(self) -> str:
        return f"{self.issuer}/.well-known/openid-configuration"

    @property
    def jwks_uri(self) -> str:
        return f"{self.issuer}/.well-known/jwks.json"

    @property
    def authorization_endpoint(self) -> str:
        return f"{self.issuer}/oauth2/authorize"

    @property
    def token_endpoint(self) -> str:
        return f"{self.issuer}/oauth2/token"

    @property
    def userinfo_endpoint(self) -> str:
        return f"{self.issuer}/oauth2/userinfo"

    def discovery_document(self) -> dict[str, Any]:
        return {
            "issuer": self.discovery_issuer_override or self.issuer,
            "authorization_endpoint": self.authorization_endpoint,
            "token_endpoint": self.token_endpoint,
            "userinfo_endpoint": self.userinfo_endpoint,
            "jwks_uri": self.jwks_uri,
            "response_types_supported": ["code"],
            "subject_types_supported": ["public"],
            "id_token_signing_alg_values_supported": ["RS256"],
        }

    def jwks_document(self) -> dict[str, Any]:
        jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(self._private_key.public_key()))
        jwk["kid"] = self._kid
        jwk["use"] = "sig"
        jwk["alg"] = "RS256"
        if self.jwks_key_type_override is not None:
            jwk["kty"] = self.jwks_key_type_override
        return {"keys": [jwk]}

    # ── issuance ────────────────────────────────────────────────────────

    def authorize(self, *, nonce: str, subject: str = "person-0001", **claims: Any) -> str:
        """Hand out an authorization code, as the browser redirect would."""
        code = f"code-{uuid.uuid4().hex}"
        self.codes[code] = IssuedCode(code=code, nonce=nonce, subject=subject, claims=dict(claims))
        return code

    def mint_id_token(self, issued: IssuedCode, *, now: int | None = None) -> str:
        now = int(time.time()) if now is None else now
        audience: Any = self.audience_override if self.audience_override is not None else self.client_id
        payload: dict[str, Any] = {
            "iss": self.issuer,
            "sub": issued.subject,
            "aud": audience,
            "iat": now,
            "exp": now + _TOKEN_TTL_SECONDS,
            **issued.claims,
        }
        if self.include_nonce:
            payload["nonce"] = issued.nonce
        # ``azp`` disambiguates which client a multi-audience token is for.
        # Omitting it with a list audience is precisely the case the spec
        # requires the login path to refuse.
        if isinstance(audience, list) and not self.omit_azp:
            payload["azp"] = self.client_id

        if self.id_token_algorithm == "none":
            return jwt.encode(payload, key="", algorithm="none")
        if self.id_token_algorithm.startswith("HS"):
            return self._hmac_confusion_token(payload)
        return jwt.encode(payload, key=self._private_key, algorithm=self.id_token_algorithm, headers={"kid": self._kid})

    def _hmac_confusion_token(self, payload: dict[str, Any]) -> str:
        """Hand-build an HS256 token keyed on the PUBLIC key.

        Built byte by byte rather than through ``jwt.encode`` because PyJWT
        REFUSES this: it raises InvalidKeyError on an asymmetric key used as
        an HMAC secret, which is a defence in the library. An attacker has no
        such scruples, so a harness that cannot express the attack cannot
        prove ELSPETH's own defence — pinning ``algorithms`` to the profile's
        list rather than trusting the token header — actually holds.

        If PyJWT's refusal were the only thing stopping this, the attack would
        succeed against any client that pins algorithms less carefully.
        """
        secret = self.public_key_pem().encode("ascii")
        header = {"alg": self.id_token_algorithm, "typ": "JWT", "kid": self._kid}
        digest = {"HS256": hashlib.sha256, "HS384": hashlib.sha384, "HS512": hashlib.sha512}[self.id_token_algorithm]

        def b64(raw: bytes) -> bytes:
            return base64.urlsafe_b64encode(raw).rstrip(b"=")

        signing_input = b".".join(
            (
                b64(json.dumps(header, separators=(",", ":"), sort_keys=True).encode()),
                b64(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()),
            )
        )
        signature = hmac.new(secret, signing_input, digest).digest()
        return (signing_input + b"." + b64(signature)).decode("ascii")

    def public_key_pem(self) -> str:
        """The signing key's PUBLIC half, PEM-encoded.

        Public by definition — it is what the JWKS publishes — which is
        exactly why an algorithm-confusion attack can use it as an HMAC
        secret, and why pinning the algorithm list is the defence.
        """
        return (
            self._private_key.public_key()
            .public_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PublicFormat.SubjectPublicKeyInfo,
            )
            .decode("ascii")
        )

    # ── transport ───────────────────────────────────────────────────────

    def transport(self) -> httpx.MockTransport:
        """An ``httpx`` transport serving exactly this provider's endpoints.

        Anything else 404s rather than escaping: a login path that reaches
        for an endpoint nobody configured should fail in the test, not
        quietly succeed against the real internet.
        """
        return httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == self.discovery_url:
            return httpx.Response(200, json=self.discovery_document())
        if url == self.jwks_uri:
            return httpx.Response(200, json=self.jwks_document())
        if url == self.token_endpoint:
            return self._handle_token(request)
        if url == self.userinfo_endpoint:
            return self._handle_userinfo(request)
        return httpx.Response(404, json={"error": "not_found", "url": url})

    def _handle_token(self, request: httpx.Request) -> httpx.Response:
        self.token_requests.append(request)
        if self.token_response_override is not None:
            return httpx.Response(200, json=self.token_response_override)

        form = dict(httpx.QueryParams(request.content.decode()))
        code = form.get("code", "")
        issued = self.codes.get(code)
        if issued is None:
            return httpx.Response(400, json={"error": "invalid_grant"})
        if issued.redeemed:
            # Single-use, enforced by the PROVIDER. This is what makes a
            # replayed transaction cookie harmless without ELSPETH having to
            # be the only thing standing between an attacker and a session.
            return httpx.Response(400, json={"error": "invalid_grant", "error_description": "code already redeemed"})
        issued.redeemed = True

        return httpx.Response(
            200,
            json={
                "access_token": f"access-{uuid.uuid4().hex}",
                "token_type": "Bearer",
                "expires_in": _TOKEN_TTL_SECONDS,
                "id_token": self.mint_id_token(issued),
            },
        )

    def _handle_userinfo(self, request: httpx.Request) -> httpx.Response:
        # Serve the most recently redeemed identity: the login path calls
        # userinfo immediately after redeeming, so ordering is deterministic
        # without the test having to thread a handle through.
        redeemed = [c for c in self.codes.values() if c.redeemed]
        subject = redeemed[-1].subject if redeemed else "person-0001"
        body = {
            "sub": self.userinfo_subject_override or subject,
            **self.userinfo_extra_claims,
        }
        return httpx.Response(200, json=body, headers={"content-type": self.userinfo_content_type})
