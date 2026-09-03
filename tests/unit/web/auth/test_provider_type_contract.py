"""Auth provider type contract checks.

The auth provider discriminator scopes identity-adjacent state across
sessions and user secrets.  It must stay a closed Literal contract after
configuration parsing; widening downstream signatures to ``str`` lets
typos type-check until a query silently returns no rows.

The discriminator is restated in ten places across three languages -- an L0
Literal, two SQL CHECK constraints in each of two stores, a write-side
frozenset, a TypeScript union, a CLI help string, and the profile registry.
Only the Literal is authoritative.  Every test below derives its expectation
from ``get_args`` rather than repeating the values, so a site that falls
behind a widening fails here instead of at a customer's first login.
"""

from __future__ import annotations

import inspect
import re
import secrets
from collections.abc import Callable
from pathlib import Path
from typing import get_args, get_type_hints

from pydantic import SecretStr
from sqlalchemy import CheckConstraint, Table

from elspeth.contracts.auth import (
    AuthProviderType,
    IdentityProviderType,
    IdentityRole,
    RelationshipType,
)
from elspeth.core.landscape.run_lifecycle_repository import _AUTH_PROVIDER_TYPES
from elspeth.core.landscape.schema import auth_events_table, run_attributions_table
from elspeth.web.auth.providers import PROFILE_REGISTRY, registered_provider_names
from elspeth.web.config import WebSettings, configured_auth_settings
from elspeth.web.secrets.service import ScopedSecretResolver, WebSecretService
from elspeth.web.secrets.user_store import UserSecretStore
from elspeth.web.sessions.models import (
    _AUTH_PROVIDER_TYPE_CHECK,
    _IDENTITY_PROVIDER_TYPE_CHECK,
    _IDENTITY_ROLE_CHECK,
    _RELATIONSHIP_TYPE_CHECK,
    identities_table,
    identity_relationships_table,
    identity_roles_table,
    sessions_table,
    user_secrets_table,
)
from elspeth.web.sessions.protocol import SessionRecord, SessionServiceProtocol
from elspeth.web.sessions.service import SessionServiceImpl

REPO_ROOT = Path(__file__).resolve().parents[4]

LOGIN_PROVIDERS = get_args(AuthProviderType)


def _local_settings(**overrides: object) -> WebSettings:
    """A minimal VALID WebSettings -- the real model, not a stand-in.

    Building the real thing matters here: these tests check that a name
    resolves against ``WebSettings``, and a stub would answer for itself.
    """
    return WebSettings(
        secret_key=secrets.token_hex(40),
        shareable_link_signing_key=secrets.token_hex(40),
        composer_max_composition_turns=10,
        composer_max_discovery_turns=5,
        composer_timeout_seconds=120.0,
        composer_rate_limit_per_minute=10,
        **overrides,
    )


def _annotation(member: Callable[..., object], parameter: str) -> object:
    signature = inspect.signature(member)
    return get_type_hints(member)[signature.parameters[parameter].name]


def _sql_in_list(values: tuple[str, ...]) -> str:
    """The SQL ``IN`` list a CHECK constraint must carry for ``values``."""
    return ", ".join(f"'{value}'" for value in values)


def _check_constraint_text(table: Table, name: str) -> str:
    for constraint in table.constraints:
        if isinstance(constraint, CheckConstraint) and constraint.name == name:
            return str(constraint.sqltext)
    raise AssertionError(f"{table.name} declares no CHECK constraint named {name!r}")


def test_auth_provider_type_is_closed_literal() -> None:
    assert get_args(AuthProviderType) == ("local", "oidc", "entra", "vanguard", "google")


def test_identity_provider_type_is_the_login_set_plus_service() -> None:
    """``IdentityProviderType`` must stay a flat Literal, never a union.

    ``AuthProviderType | Literal["service"]`` type-checks and reads correctly
    to a human, but ``get_args`` on a union returns the two operand
    ``Literal`` objects rather than the six strings, so every membership
    check and every assertion over it silently inspects the wrong shape.
    Nesting the alias inside ``Literal[...]`` flattens it.  This equality is
    over flat strings and therefore fails loudly if anyone rewrites it.
    """
    assert set(get_args(IdentityProviderType)) == set(get_args(AuthProviderType)) | {"service"}
    assert all(isinstance(value, str) for value in get_args(IdentityProviderType))


def test_service_is_not_a_login_mechanism() -> None:
    """A service identity holds an operator credential and never logs in.

    Keeping ``service`` out of ``AuthProviderType`` is what makes the
    narrower type a subset of the wider one, which is what keeps the
    ``sessions.user_id`` -> ``identities.identity_id`` foreign key sound.
    """
    assert "service" not in get_args(AuthProviderType)
    assert set(get_args(AuthProviderType)) < set(get_args(IdentityProviderType))


def test_identity_role_and_relationship_type_are_closed_literals() -> None:
    assert get_args(IdentityRole) == (
        "admin",
        "approver",
        "reviewer",
        "user",
        "curator",
        "auditor",
        "oversight",
    )
    # ``none`` is an activation request argument, not a stored role: it
    # writes no ``identity_roles`` row, so it must never become a member.
    assert "none" not in get_args(IdentityRole)
    assert get_args(RelationshipType) == ("approver",)


def test_profile_registry_matches_the_login_contract() -> None:
    """Every login provider but ``local`` has a profile, and no others.

    The registry re-asserts this at import, so a mismatch is a boot failure
    rather than a test failure; this test states the same contract where a
    reader looking for it will find it.
    """
    assert frozenset(PROFILE_REGISTRY) | {"local"} == frozenset(get_args(AuthProviderType))
    assert "local" not in PROFILE_REGISTRY
    assert registered_provider_names() == tuple(sorted(get_args(AuthProviderType)))


def test_every_setting_a_profile_names_actually_exists() -> None:
    """A profile naming a setting that does not exist fails silently forever.

    ``required_settings`` is resolved by name. If a profile names a typo,
    ``auth_setting_values`` raises ``KeyError`` at validation time -- but only
    for a deployment that selects that provider, which may be nobody until it
    is somebody. Pinned here so the typo cannot leave the branch.
    """
    named: set[str] = set()
    for profile in PROFILE_REGISTRY.values():
        named |= set(profile.required_settings)
        named |= profile.forbidden_settings

    unknown_to_settings = sorted(named - set(WebSettings.model_fields))
    assert not unknown_to_settings, f"profiles name settings WebSettings does not define: {unknown_to_settings}"

    settings = _local_settings()
    unresolvable = sorted(named - set(configured_auth_settings(settings)))
    assert not unresolvable, f"auth_setting_values cannot resolve: {unresolvable}"


def test_the_settings_reader_carries_nothing_it_is_not_asked_for() -> None:
    """The reader and the registry must be the same set, both ways.

    A name in the reader that no profile wants is dead weight that reads as
    supported; a name a profile wants that the reader lacks is a KeyError in
    production. Equality catches both.
    """
    named: set[str] = set()
    for profile in PROFILE_REGISTRY.values():
        named |= set(profile.required_settings)
        named |= profile.forbidden_settings
    assert set(configured_auth_settings(_local_settings())) == named


def test_a_blank_setting_does_not_count_as_configured() -> None:
    """`ELSPETH_WEB__SSO_ISSUER=` is how a task definition ships a hole.

    A blank satisfies every ``is not None`` check on the way to a deployment
    that cannot complete a login. The field validators reject blanks at parse
    time; this pins the readiness-side verdict too, because readiness stays
    total against state it did not parse.
    """
    settings = _local_settings()
    assert configured_auth_settings(settings)["sso_issuer"] is False

    blank = _local_settings(sso_endpoint_origins=())
    assert configured_auth_settings(blank)["sso_endpoint_origins"] is False

    configured = _local_settings(
        sso_client_secret=SecretStr("s3cret"),
        sso_endpoint_origins=("https://origin.example",),
        quota_default_tokens_per_day=100_000,
    )
    values = configured_auth_settings(configured)
    assert values["sso_client_secret"] is True
    assert values["sso_endpoint_origins"] is True
    assert values["quota_default_tokens_per_day"] is True


def test_a_whitespace_only_secret_is_not_a_secret() -> None:
    """A SecretStr hides its value, including from a truthiness check."""
    assert configured_auth_settings(_local_settings(sso_client_secret=SecretStr("   ")))["sso_client_secret"] is False


def test_sessions_and_user_secrets_share_one_widened_check() -> None:
    expected = f"auth_provider_type IN ({_sql_in_list(LOGIN_PROVIDERS)})"
    assert expected == _AUTH_PROVIDER_TYPE_CHECK
    assert _check_constraint_text(sessions_table, "ck_sessions_auth_provider_type") == expected
    assert _check_constraint_text(user_secrets_table, "ck_user_secrets_auth_provider_type") == expected


def test_identities_admits_the_wider_set_than_sessions_does() -> None:
    """The two discriminators are different constants, and must stay so.

    ``identities`` admits ``service``; ``sessions`` and ``user_secrets`` do
    not, because a service principal never completes a browser login and so
    can own neither. Collapsing these onto one constant would either admit a
    session for a principal that cannot log in, or refuse an identity row for
    a principal the model requires.
    """
    identity_providers = get_args(IdentityProviderType)
    assert f"provider IN ({_sql_in_list(identity_providers)})" == _IDENTITY_PROVIDER_TYPE_CHECK
    assert _check_constraint_text(identities_table, "ck_identities_provider") == _IDENTITY_PROVIDER_TYPE_CHECK
    assert _IDENTITY_PROVIDER_TYPE_CHECK != _AUTH_PROVIDER_TYPE_CHECK
    assert "'service'" in _IDENTITY_PROVIDER_TYPE_CHECK
    assert "'service'" not in _AUTH_PROVIDER_TYPE_CHECK


def test_stored_role_and_edge_checks_derive_from_their_literals() -> None:
    assert f"role IN ({_sql_in_list(get_args(IdentityRole))})" == _IDENTITY_ROLE_CHECK
    assert _check_constraint_text(identity_roles_table, "ck_identity_roles_role") == _IDENTITY_ROLE_CHECK
    assert f"relationship_type IN ({_sql_in_list(get_args(RelationshipType))})" == _RELATIONSHIP_TYPE_CHECK
    assert _check_constraint_text(identity_relationships_table, "ck_identity_relationships_type") == _RELATIONSHIP_TYPE_CHECK


def test_landscape_checks_admit_exactly_the_login_providers() -> None:
    assert _check_constraint_text(auth_events_table, "ck_auth_events_provider") == (f"provider IN ({_sql_in_list(LOGIN_PROVIDERS)})")
    assert _check_constraint_text(run_attributions_table, "ck_run_attributions_auth_provider_type") == (
        f"auth_provider_type IN ({_sql_in_list(LOGIN_PROVIDERS)})"
    )


def test_run_attribution_guard_derives_from_the_contract() -> None:
    assert frozenset(get_args(AuthProviderType)) == _AUTH_PROVIDER_TYPES


def test_frontend_provider_union_mirrors_the_contract() -> None:
    """The TypeScript union has no compiler link to the Python Literal.

    ``GET /api/auth/config`` returns ``provider`` verbatim, so a union that
    lags the Literal makes the frontend fail to narrow a real response.
    """
    source = (REPO_ROOT / "src/elspeth/web/frontend/src/types/index.ts").read_text(encoding="utf-8")
    expected = "  provider: " + " | ".join(f'"{value}"' for value in LOGIN_PROVIDERS) + ";"
    assert expected in source, f"AuthConfig.provider must read exactly:\n{expected}"


def test_cli_auth_help_lists_every_selectable_provider() -> None:
    """``--auth`` names what an operator may pass, and refuses the rest."""
    source = (REPO_ROOT / "src/elspeth/cli.py").read_text(encoding="utf-8")
    expected = 'help="Auth provider: ' + ", ".join(LOGIN_PROVIDERS) + '"'
    assert expected in source, f"the --auth option help must read exactly:\n{expected}"


def test_local_only_routes_guard_on_local_not_on_an_enumeration() -> None:
    """The credential routes must exclude every IdP, present and future.

    ``settings.auth_provider != "local"`` stays correct as the Literal
    widens; an enumeration of IdP values would silently expose the local
    password and refresh routes on the next provider added.
    """
    source = (REPO_ROOT / "src/elspeth/web/auth/routes.py").read_text(encoding="utf-8")

    # Four credential-only routes guard this way (login, register, password
    # change, refresh). The count is not pinned -- adding or removing a
    # credential route is ordinary work -- but every guard must be a
    # comparison against ``local`` and nothing else.
    assert source.count('settings.auth_provider != "local"') >= 1
    compared_literals = set(re.findall(r'settings\.auth_provider\s*[!=]=\s*"([a-z]+)"', source))
    assert compared_literals == {"local"}, (
        f"routes.py compares auth_provider against {sorted(compared_literals)}; "
        "guarding on an IdP value exposes the credential routes on the next provider added"
    )


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
