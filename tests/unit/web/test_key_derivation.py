"""Purpose-separated key derivation from the web ``secret_key``.

The property under test is INDEPENDENCE: three consumers of one operator
secret must receive three keys, none of which can be computed from another
without the master. A test that only checked "returns 32 bytes" would pass
against an implementation that returned the same key three times, which is
the exact defect this module exists to prevent.
"""

from __future__ import annotations

import base64

import pytest

from elspeth.web.key_derivation import (
    derive_binding_generation_key,
    derive_session_token_key,
    derive_user_secret_master_key,
)

_MASTER = "an-operator-secret-of-entirely-adequate-length-0123456789"


def test_each_purpose_gets_a_different_key_from_one_master() -> None:
    """The point of the module: no two purposes share key material."""
    session = derive_session_token_key(_MASTER)
    generation = derive_binding_generation_key(_MASTER)
    user_secret = base64.urlsafe_b64decode(derive_user_secret_master_key(_MASTER))

    assert len({session, generation, user_secret}) == 3


def test_no_derived_key_is_the_master_itself() -> None:
    """A pass-through implementation must not satisfy this suite."""
    raw = _MASTER.encode("utf-8")
    assert derive_session_token_key(_MASTER) != raw
    assert derive_binding_generation_key(_MASTER) != raw
    assert derive_user_secret_master_key(_MASTER) != _MASTER


def test_derivation_is_deterministic_across_calls() -> None:
    """Boots must agree, or every token from the previous boot is invalid."""
    assert derive_session_token_key(_MASTER) == derive_session_token_key(_MASTER)
    assert derive_binding_generation_key(_MASTER) == derive_binding_generation_key(_MASTER)
    assert derive_user_secret_master_key(_MASTER) == derive_user_secret_master_key(_MASTER)


def test_a_different_master_gives_a_different_key_everywhere() -> None:
    other = _MASTER + "-rotated"
    assert derive_session_token_key(_MASTER) != derive_session_token_key(other)
    assert derive_binding_generation_key(_MASTER) != derive_binding_generation_key(other)
    assert derive_user_secret_master_key(_MASTER) != derive_user_secret_master_key(other)


def test_derived_keys_are_full_width() -> None:
    """A short key would silently weaken HS256 and the evidence HMAC."""
    assert len(derive_session_token_key(_MASTER)) == 32
    assert len(derive_binding_generation_key(_MASTER)) == 32
    assert len(base64.urlsafe_b64decode(derive_user_secret_master_key(_MASTER))) == 32


def test_the_user_secret_key_survives_the_text_round_trip() -> None:
    """It is handed to a KDF that re-encodes it, so it must be ASCII-safe."""
    derived = derive_user_secret_master_key(_MASTER)
    assert derived.isascii()
    assert derived.encode("utf-8").decode("utf-8") == derived


@pytest.mark.parametrize(
    "derive",
    [derive_session_token_key, derive_user_secret_master_key, derive_binding_generation_key],
)
def test_an_empty_master_is_refused(derive) -> None:
    """Deriving from nothing would produce a stable, publicly computable key."""
    with pytest.raises(ValueError, match="must not be empty"):
        derive("")


@pytest.mark.parametrize(
    "derive",
    [derive_session_token_key, derive_user_secret_master_key, derive_binding_generation_key],
)
def test_a_non_string_master_is_refused(derive) -> None:
    with pytest.raises(TypeError, match="must be a string"):
        derive(b"already-bytes")  # type: ignore[arg-type]
