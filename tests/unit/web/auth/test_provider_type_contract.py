"""Auth provider type contract checks.

The auth provider discriminator scopes identity-adjacent state across
sessions and user secrets.  It must stay a closed Literal contract after
configuration parsing; widening downstream signatures to ``str`` lets
typos type-check until a query silently returns no rows.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import get_args, get_type_hints

from elspeth.contracts.auth import AuthProviderType
from elspeth.web.config import WebSettings
from elspeth.web.secrets.service import ScopedSecretResolver, WebSecretService
from elspeth.web.secrets.user_store import UserSecretStore
from elspeth.web.sessions.protocol import SessionRecord, SessionServiceProtocol
from elspeth.web.sessions.service import SessionServiceImpl


def _annotation(member: Callable[..., object], parameter: str) -> object:
    signature = inspect.signature(member)
    return get_type_hints(member)[signature.parameters[parameter].name]


def test_auth_provider_type_is_closed_literal() -> None:
    assert get_args(AuthProviderType) == ("local", "oidc", "entra")


def test_web_settings_auth_provider_uses_shared_contract() -> None:
    assert get_type_hints(WebSettings)["auth_provider"] == AuthProviderType


def test_identity_scoped_records_use_shared_auth_provider_contract() -> None:
    assert get_type_hints(SessionRecord)["auth_provider_type"] == AuthProviderType


def test_secret_and_session_boundaries_do_not_widen_auth_provider_to_str() -> None:
    expected: list[tuple[str, Callable[..., object]]] = [
        ("UserSecretStore.has_secret", UserSecretStore.has_secret),
        ("UserSecretStore.has_secret_record", UserSecretStore.has_secret_record),
        ("UserSecretStore.get_secret", UserSecretStore.get_secret),
        ("UserSecretStore.set_secret", UserSecretStore.set_secret),
        ("UserSecretStore.delete_secret", UserSecretStore.delete_secret),
        ("UserSecretStore.list_secrets", UserSecretStore.list_secrets),
        ("WebSecretService.list_refs", WebSecretService.list_refs),
        ("WebSecretService.has_ref", WebSecretService.has_ref),
        ("WebSecretService.resolve", WebSecretService.resolve),
        ("WebSecretService.check_user_ref_resolvable", WebSecretService.check_user_ref_resolvable),
        ("WebSecretService.set_user_secret", WebSecretService.set_user_secret),
        ("WebSecretService.delete_user_secret", WebSecretService.delete_user_secret),
        ("ScopedSecretResolver.__init__", ScopedSecretResolver.__init__),
        ("SessionServiceProtocol.create_session", SessionServiceProtocol.create_session),
        ("SessionServiceProtocol.list_sessions", SessionServiceProtocol.list_sessions),
        ("SessionServiceImpl.create_session", SessionServiceImpl.create_session),
        ("SessionServiceImpl.list_sessions", SessionServiceImpl.list_sessions),
    ]
    # ``fork_session`` no longer takes a caller-supplied ``auth_provider_type``:
    # it now accepts a ``GuidedOperationFence`` and derives the provider
    # discriminator internally from the fenced parent session row (a stricter,
    # not-caller-supplied boundary). There is no ``str``-widening seam to guard
    # here anymore, so it drops out of the closed-Literal contract list.

    for label, member in expected:
        assert _annotation(member, "auth_provider_type") == AuthProviderType, (
            f"{label} widened auth_provider_type away from the closed Literal"
        )
