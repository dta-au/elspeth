"""Purpose-separated key derivation from the single web ``secret_key``.

One 32-byte operator secret feeds three unrelated cryptographic jobs: signing
session tokens, encrypting user secrets at rest, and tagging plugin-binding
evidence. Handing the same raw string to all three means a weakness or a
disclosure in any one of them is a weakness in all three, and the SSO delivery
adds traffic to the first of those without changing that.

HKDF-SHA256 with a distinct ``info`` string per job fixes that: each consumer
gets an independent 32-byte key, and recovering one tells an attacker nothing
about the others. This is the whole point of a KDF's ``info`` parameter --
domain separation -- and it costs one function call at startup.

WHAT IS DELIBERATELY NOT DERIVED
--------------------------------
``shareable_link_signing_key`` keeps its own Secrets Manager binding. Deriving
it from ``secret_key`` would replace an *independent* secret with a *dependent*
one, which is the opposite of what this module is for. Independence beats
derivation whenever the operator is already willing to manage a second secret.

EPOCH BINDING
-------------
Two of the three derivations change a value that is compared against something
already at rest, so both are bound to the epoch window where the stores are
recreated (see the operator notice and ``docs/runbooks``):

* user-secret encryption -- every stored secret was encrypted under the raw
  key and cannot be decrypted under the derived one;
* plugin-binding generation -- the fingerprint is persisted as run evidence in
  the Landscape ``run_web_plugin_policy`` table and embedded in exports, so a
  bundle written before the change and one written after carry different
  fingerprints for identical policy state.

Neither may ship in a release that keeps an existing store.
"""

from __future__ import annotations

import base64
from typing import Final

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

_DERIVED_KEY_BYTES: Final = 32

# Version suffixes are load-bearing: changing a derivation is a new info
# string, never an edit to an existing one, so an old and a new key can never
# collide on the same label.
_SESSION_TOKEN_INFO: Final = b"elspeth-session-token-hs256-v1"
_USER_SECRET_INFO: Final = b"elspeth-user-secret-encryption-v1"
_BINDING_GENERATION_INFO: Final = b"elspeth-plugin-binding-generation-v1"


def _derive(secret_key: str, *, info: bytes) -> bytes:
    """Derive one 32-byte purpose key from the web ``secret_key``.

    ``salt=None`` is HKDF's documented default of an all-zero salt. A random
    salt would have to be stored and read back on every boot, and the security
    it buys over a fixed one requires a low-entropy input -- which
    ``_enforce_secret_key_in_production`` already refuses.
    """
    if type(secret_key) is not str:
        raise TypeError("secret_key must be a string")
    if not secret_key:
        raise ValueError("secret_key must not be empty")
    return HKDF(
        algorithm=hashes.SHA256(),
        length=_DERIVED_KEY_BYTES,
        salt=None,
        info=info,
    ).derive(secret_key.encode("utf-8"))


def derive_session_token_key(secret_key: str) -> bytes:
    """Return the HMAC key that signs and verifies session tokens."""
    return _derive(secret_key, info=_SESSION_TOKEN_INFO)


def derive_user_secret_master_key(secret_key: str) -> str:
    """Return the master key :class:`UserSecretStore` stretches per secret.

    Returned as text, not bytes, because the store's own KDF takes a string
    and re-encodes it: urlsafe base64 of the 32 derived bytes is an injective
    encoding that survives that round trip without an ambiguous ``bytes`` /
    ``str`` boundary in the middle of a decryption path.
    """
    derived = _derive(secret_key, info=_USER_SECRET_INFO)
    return base64.urlsafe_b64encode(derived).decode("ascii")


def derive_binding_generation_key(secret_key: str) -> bytes:
    """Return the HMAC key that tags plugin-binding evidence.

    Every construction site must move together. The fingerprint is compared
    against a queued run's frozen copy, so two live call sites deriving
    differently would refuse valid runs with a binding-rotation error that
    names the wrong cause.
    """
    return _derive(secret_key, info=_BINDING_GENERATION_INFO)
