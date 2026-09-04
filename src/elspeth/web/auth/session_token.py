"""ELSPETH's own session token: minting, decoding, and the refresh chain.

This is the token ELSPETH issues to its own browser client after a login has
succeeded. It is NOT an IdP token and shares no code with one: an ID token is
evidence from a third party that must be verified against their keys, while
this is a statement we make to ourselves and verify with a key only we hold.
Conflating them is how a validator ends up trusting an attacker-chosen
algorithm, which is exactly the defect ``JWKSTokenValidator.decode_id_token``
was written to close.

WHY THIS IS ITS OWN MODULE
--------------------------
Minting used to live inside ``LocalAuthProvider``, where it could only ever
serve local logins. Every provider needs the same token with the same claims
and the same refresh bound, so it moves out whole rather than being copied
per provider -- a copied refresh bound is one that will eventually differ.

CLAIMS
------
``sub``       the ``identity_id``, never a username. A username is a display
              string an administrator can change; an identity_id is the
              ownership key that rows point at.
``username``  display only. Nothing authorises on it.
``provider``  which IdP issued the login this token descends from.
``iss``       always ``elspeth``.
``aud``       the deployment's public base URL, or ``elspeth-local``.
``jti``       a unique token id, so a future denylist has something to name.
``iat``/``exp`` issue and expiry.

THE THREE REFUSALS THAT MATTER
------------------------------
1. ``provider`` must equal the provider this deployment is configured for. A
   token minted while the deployment ran one IdP must not survive a switch to
   another: the subjects are unrelated namespaces, so the same ``sub`` can
   mean two different people.
2. ``iss`` and ``aud`` are verified, not merely present. Without ``aud`` two
   ELSPETH deployments sharing a secret would accept each other's tokens.
3. ``principal_is_active`` is consulted on BOTH authenticate and refresh.
   Refresh is the one that matters: it carries the original ``iat`` forward,
   so a token issued before a disable could otherwise be renewed for the
   whole refresh-chain window by someone whose access was already revoked.
"""

from __future__ import annotations

import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

import jwt
from jwt.exceptions import PyJWTError

from elspeth.contracts.auth import AuthProviderType
from elspeth.web.auth.models import AuthenticationError
from elspeth.web.validation import has_visible_content

_SIGNING_ALGORITHM: Final = "HS256"
_ISSUER: Final = "elspeth"
LOCAL_AUDIENCE: Final = "elspeth-local"
"""Audience for a deployment that has no public base URL configured."""

_JTI_BYTES: Final = 18

DEFAULT_TOKEN_EXPIRY_HOURS: Final = 24
DEFAULT_MAX_REFRESH_CHAIN_HOURS: Final = 168
"""Token lifetimes, unchanged from the values ``LocalAuthProvider`` carried.

They were constructor defaults that no caller ever overrode, so there is no
setting to read them from and this delivery does not invent one. Named here
because they are now shared by every provider: a copied security bound is one
that will eventually differ between providers.
"""


@dataclass(frozen=True, slots=True)
class SessionTokenClaims:
    """A decoded, fully verified session token.

    Constructing one of these is the ONLY way the rest of the application
    learns what a token said. Every field has been checked; there is no
    accessor that returns a raw claim.
    """

    identity_id: str
    username: str
    provider: AuthProviderType
    issued_at: int
    expires_at: int
    token_id: str


def _required_visible_claim(payload: dict[str, object], claim: str) -> str:
    """Read one required claim, treating absence as distinct from a bad value.

    Membership-then-index rather than ``.get()``: a missing claim and a claim
    present as ``None`` are different facts about a token, and ``.get()``
    collapses them into one. Both are refused here, but the distinction has to
    survive to the point of refusal for the refusal to mean anything. This is
    the same shape ``auth/audit.py`` uses for issued-token claims.
    """
    if claim not in payload:
        raise AuthenticationError("Invalid token")
    value = payload[claim]
    if type(value) is not str or not has_visible_content(value):
        raise AuthenticationError("Invalid token")
    return value


def _required_int_claim(payload: dict[str, object], claim: str) -> int:
    if claim not in payload:
        raise AuthenticationError("Invalid token")
    value = payload[claim]
    # ``bool`` is an ``int`` subclass in Python, and ``True`` would otherwise
    # read as the epoch second 1.
    if type(value) is not int:
        raise AuthenticationError("Invalid token")
    return value


class SessionTokenIssuer:
    """Mints and verifies this deployment's session tokens.

    One instance per process, built by the app factory from settings. It holds
    the signing key and the deployment's provider and audience, so no caller
    can mint a token for a different provider by passing an argument.
    """

    def __init__(
        self,
        *,
        signing_key: bytes,
        provider: AuthProviderType,
        audience: str,
        token_expiry_hours: int,
        max_refresh_chain_hours: int,
        principal_is_active: Callable[[str], bool],
    ) -> None:
        if type(signing_key) is not bytes or len(signing_key) < 32:
            raise ValueError("session token signing key must be at least 32 bytes")
        if not has_visible_content(audience):
            raise ValueError("session token audience must be a non-blank string")
        if token_expiry_hours <= 0:
            raise ValueError("token_expiry_hours must be positive")
        if max_refresh_chain_hours <= 0:
            raise ValueError("max_refresh_chain_hours must be positive")
        self._signing_key = signing_key
        self._provider = provider
        self._audience = audience
        self._token_expiry_hours = token_expiry_hours
        self._max_refresh_chain_hours = max_refresh_chain_hours
        self._principal_is_active = principal_is_active

    @property
    def provider(self) -> AuthProviderType:
        return self._provider

    @property
    def audience(self) -> str:
        return self._audience

    def mint(self, *, identity_id: str, username: str, issued_at: int | None = None) -> str:
        """Issue a token for an identity that the caller has already admitted.

        ``issued_at`` carries a refresh chain's ORIGINAL issue time forward so
        the chain ages. A fresh login passes ``None`` and starts a new chain.
        """
        if not has_visible_content(identity_id):
            raise AuthenticationError("Cannot mint a token without an identity")
        if not has_visible_content(username):
            raise AuthenticationError("Cannot mint a token without a username")
        now = int(time.time())
        payload = {
            "sub": identity_id,
            "username": username,
            "provider": self._provider,
            "iss": _ISSUER,
            "aud": self._audience,
            "jti": secrets.token_urlsafe(_JTI_BYTES),
            "iat": now if issued_at is None else issued_at,
            "exp": now + self._token_expiry_hours * 3600,
        }
        token: str = jwt.encode(payload, self._signing_key, algorithm=_SIGNING_ALGORITHM)
        return token

    def decode(self, token: str) -> SessionTokenClaims:
        """Verify a token's signature, envelope, and provider binding.

        Does NOT consult ``principal_is_active`` -- that read belongs to
        :meth:`authenticate`, so a caller that only needs to know what a token
        says (audit metadata, for instance) does not pay for a database round
        trip and does not accidentally treat "says X" as "may do X".
        """
        try:
            payload: dict[str, object] = jwt.decode(
                token,
                self._signing_key,
                algorithms=[_SIGNING_ALGORITHM],
                audience=self._audience,
                issuer=_ISSUER,
                options={"require": ["exp", "iat", "iss", "aud", "sub", "jti"]},
            )
        except PyJWTError as exc:
            raise AuthenticationError("Invalid token") from exc

        provider = _required_visible_claim(payload, "provider")
        if provider != self._provider:
            # Not merely a mismatch: subjects from two IdPs are unrelated
            # namespaces, so honouring this token could bind one person's
            # session to another person's identity.
            raise AuthenticationError("Token was issued for a different authentication provider")

        return SessionTokenClaims(
            identity_id=_required_visible_claim(payload, "sub"),
            username=_required_visible_claim(payload, "username"),
            provider=self._provider,
            issued_at=_required_int_claim(payload, "iat"),
            expires_at=_required_int_claim(payload, "exp"),
            token_id=_required_visible_claim(payload, "jti"),
        )

    def authenticate(self, token: str) -> SessionTokenClaims:
        """Verify a token AND confirm its identity may still act.

        The second half is what makes revocation latency one request rather
        than one token lifetime.
        """
        claims = self.decode(token)
        if not self._principal_is_active(claims.identity_id):
            raise AuthenticationError("Invalid token")
        return claims

    def refresh(self, token: str) -> str:
        """Issue a successor token, bounded by the original issue time.

        The chain bound is the only revocation-like mechanism a stateless
        bearer has: without it a stolen token is renewable forever. The
        identity check runs here too, because refresh is precisely the path a
        disabled person would use to outlive their disablement.
        """
        claims = self.authenticate(token)
        chain_age_hours = (int(time.time()) - claims.issued_at) / 3600
        if chain_age_hours > self._max_refresh_chain_hours:
            raise AuthenticationError("Token refresh chain expired — please re-authenticate")
        return self.mint(
            identity_id=claims.identity_id,
            username=claims.username,
            issued_at=claims.issued_at,
        )
