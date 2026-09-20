"""The identity directory reads behind the People & access panel.

Search is where a redaction rule can leak without any field being returned:
a needle that MATCHES a withheld field tells the searcher what the field
holds. So the cases here are mostly about what a search must not find.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import update

from elspeth.web.auth.models import IdentityClaims
from elspeth.web.coordination.approval_lifecycle_authority import RepositoryApprovalLifecycleAuthority
from elspeth.web.coordination.identity_authority import (
    DIRECTORY_LOOKUP_MAX,
    DIRECTORY_TEXT_MAX,
    IdentityDirectoryQuery,
    RepositoryIdentityAuthority,
    is_retired_identity,
)
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.models import identities_table
from elspeth.web.sessions.schema import initialize_session_schema


@pytest.fixture
def engine():
    eng = create_session_engine("sqlite:///:memory:")
    initialize_session_schema(eng)
    return eng


@pytest.fixture
def authority(engine) -> RepositoryIdentityAuthority:
    return RepositoryIdentityAuthority(engine, lifecycle_effect=RepositoryApprovalLifecycleAuthority().apply)


def _noop(*_args: Any) -> None:
    return None


def _login(
    authority: RepositoryIdentityAuthority,
    subject: str,
    *,
    activate: bool,
    provider: Any = "local",
    display_name: str | None = None,
    email: str | None = None,
    organisation_id: str | None = None,
) -> str:
    return authority.ensure_identity(
        claims=IdentityClaims(
            provider=provider,
            subject=subject,
            username=subject,
            display_name=display_name,
            email=email,
            organisation_id=organisation_id,
        ),
        activate=activate,
        quota_tokens_per_day=None,
        quota_storage_bytes=None,
        identity_dormancy_days=90,
        record_admission=_noop,
        record_rebound=_noop,
        record_dormant=_noop,
    ).record.identity_id


def _query(**overrides: Any) -> IdentityDirectoryQuery:
    values: dict[str, Any] = {"text": None, "access_state": None, "provider": None, "kind": None}
    values.update(overrides)
    return IdentityDirectoryQuery(**values)


def _subjects(authority: RepositoryIdentityAuthority, **overrides: Any) -> list[str]:
    return [row.subject for row in authority.search_identities(query=_query(**overrides), limit=50, offset=0)]


def test_search_matches_an_admitted_profile_on_every_shown_field(authority) -> None:
    _login(authority, "jdoe", activate=True, display_name="Jane Doe", email="jane@corp.example", organisation_id="org-7")

    assert _subjects(authority, text="JANE D") == ["jdoe"]
    assert _subjects(authority, text="corp.example") == ["jdoe"]
    assert _subjects(authority, text="jdo") == ["jdoe"]
    assert _subjects(authority, text="org-7") == ["jdoe"]
    assert _subjects(authority, text="nobody") == []


def test_search_cannot_find_a_never_admitted_pending_row_by_a_withheld_field(authority) -> None:
    """The projection blanks these fields; a match on them would disclose them anyway."""
    _login(authority, "sub-9931", activate=False, display_name="Sam Lee", email="sam@secret.example", organisation_id="org-2")

    assert _subjects(authority, text="Sam Lee") == []
    assert _subjects(authority, text="secret.example") == []
    # What the queue shows of the row still finds it.
    assert _subjects(authority, text="sub-99") == ["sub-9931"]
    assert _subjects(authority, text="org-2") == ["sub-9931"]


def test_search_finds_a_re_pended_row_by_its_profile(engine, authority) -> None:
    """A dormancy re-pend has an ``activated_at``: the container already holds that profile legitimately."""
    identity_id = _login(authority, "rtn", activate=True, display_name="Rita Returning", email="rita@corp.example")
    with engine.begin() as conn:
        conn.execute(update(identities_table).where(identities_table.c.identity_id == identity_id).values(access_state="pending"))

    assert _subjects(authority, text="Returning", access_state="pending") == ["rtn"]


def test_search_text_is_literal_not_a_pattern(authority) -> None:
    _login(authority, "a_b", activate=True, display_name="Percent %100")
    _login(authority, "axb", activate=True, display_name="Plain")

    assert _subjects(authority, text="a_b") == ["a_b"]
    assert _subjects(authority, text="%") == ["a_b"]
    assert _subjects(authority, text="\\") == []


def test_search_filters_compose_and_order_is_stable_across_pages(engine, authority) -> None:
    for name in ("Zed", "amy", "Bob", "amy"):
        _login(authority, f"{name.lower()}-{len(_subjects(authority))}", activate=True, display_name=name)
    _login(authority, "oidc-person", activate=True, provider="oidc", display_name="Carl")
    with engine.begin() as conn:
        conn.execute(
            identities_table.insert().values(
                identity_id="console-service",
                provider="service",
                kind="service",
                subject="console-service",
                username="console-service",
                first_seen_at=datetime.now(UTC),
                access_state="active",
                activated_at=datetime.now(UTC),
            )
        )

    everyone = authority.search_identities(query=_query(), limit=50, offset=0)
    labels = [(row.display_name or row.username).lower() for row in everyone]
    assert labels == sorted(labels)
    # Two people share a name: their order is fixed by identity_id, so paging cannot duplicate or skip one.
    paged = [
        row.identity_id for offset in range(len(everyone)) for row in authority.search_identities(query=_query(), limit=1, offset=offset)
    ]
    assert paged == [row.identity_id for row in everyone]

    assert _subjects(authority, provider="oidc") == ["oidc-person"]
    assert _subjects(authority, kind="service") == ["console-service"]
    assert "console-service" not in _subjects(authority, kind="human")
    assert _subjects(authority, access_state="disabled") == []


def test_local_correlation_is_exact_and_skips_a_retired_key(authority) -> None:
    old_id = _login(authority, "pat", activate=True)
    _login(authority, "pat", activate=True, provider="oidc")
    authority.retire_identity(
        provider="local",
        subject="pat",
        reason="local credential deleted",
        record=_noop,
        protect_last_admin=True,
        delete_credential=lambda: None,
    )
    new_id = _login(authority, "pat", activate=True)

    bound = authority.read_local_identity_summaries(usernames=["pat", "ghost"])
    assert [row.identity_id for row in bound] == [new_id]

    summaries = {row.identity_id: row for row in authority.read_identity_summaries(identity_ids=[old_id, new_id, "absent"])}
    assert set(summaries) == {old_id, new_id}
    assert is_retired_identity(summaries[old_id]) is True
    assert is_retired_identity(summaries[new_id]) is False


def test_directory_reads_are_bounded(authority) -> None:
    with pytest.raises(ValueError, match="at most"):
        _query(text="x" * (DIRECTORY_TEXT_MAX + 1))
    with pytest.raises(ValueError, match="at most"):
        authority.read_identity_summaries(identity_ids=["i"] * (DIRECTORY_LOOKUP_MAX + 1))
    with pytest.raises(ValueError, match="at most"):
        authority.read_local_identity_summaries(usernames=["u"] * (DIRECTORY_LOOKUP_MAX + 1))
    with pytest.raises(TypeError, match="IdentityDirectoryQuery"):
        impostor: Any = object()
        authority.search_identities(query=impostor, limit=10, offset=0)
    assert authority.read_identity_summaries(identity_ids=[]) == ()
