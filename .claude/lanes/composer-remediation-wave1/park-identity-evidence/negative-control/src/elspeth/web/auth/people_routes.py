"""People directory -- GET /api/auth/admin/people.

ONE READ FACADE over two stores that answer different questions. ``auth.db``
holds local CREDENTIALS and is administered by local-auth administrators; the
``identities`` substrate holds ADMISSION and is administered by holders of a
live ``admin`` role. An administrator looking for a person should not have to
know which store the person's record lives in, so this module merges the two
for display. It merges nothing else: every write stays on the route that owns
it, behind that route's own guard.

Two capabilities, resolved independently, on every request
-----------------------------------------------------------
``local_accounts`` (a local-auth administrator or the configured dev admin)
and ``identity_admin`` (a live ``admin`` role) are resolved separately. A
configured dev admin need not hold an identity role, and external-auth
administrators cannot manage local credentials. Each source is QUERIED only
when its own capability is held. A caller holding neither sees 404.

Correlation
-----------
A local account and an identity are the same person exactly when the identity
is ``provider == "local"`` and ``subject == username``. Never by display name
or email: two people may share either. A retired identity's subject was
rewritten, so a recycled username correlates with its fresh identity only and
the retired row stays a separate, labelled record.

Paging a merged roster
----------------------
Local accounts with no identity ("access not set up") form a leading segment,
ordered by label; identities follow in the authority's own order. The page is
cut from that combined sequence, so ``offset`` means the same thing on every
page. Joining one page of identities against all local accounts would instead
repeat every unlinked account on every page.

A source that cannot be read is a 503 naming it, never an empty source: an
administrator shown "no matching people" must be able to trust it.

Passwords never pass through here.
"""

from __future__ import annotations

import sqlite3
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Query, Request, Response
from pydantic import Field
from sqlalchemy.exc import SQLAlchemyError

from elspeth.contracts.auth import AuthProviderType, IdentityAccessState, IdentityProviderType
from elspeth.web.async_workers import run_sync_in_worker
from elspeth.web.auth.admin_routes import can_manage_local_accounts
from elspeth.web.auth.identity_admin_routes import IdentityView, _authority, _hidden, _identity_view, _StrictModel, _uncacheable
from elspeth.web.auth.local import LocalAuthProvider, LocalUserAccount
from elspeth.web.auth.middleware import get_current_user
from elspeth.web.auth.models import UserIdentity
from elspeth.web.config import WebSettings
from elspeth.web.coordination.identity_authority import (
    DIRECTORY_LOOKUP_MAX,
    DIRECTORY_TEXT_MAX,
    IdentityDirectoryQuery,
    IdentitySummary,
    is_retired_identity,
)

PEOPLE_PAGE_MAX = 100
"""Upper bound on one page of the directory."""

LABEL_LOOKUP_MAX = 50
"""Upper bound on one by-id label lookup: the counterparts on one person's approver list."""

PeopleStatusFilter = Literal["all", "pending", "active", "disabled", "not_set_up"]
PeopleTypeFilter = Literal["people", "service", "all"]
PeopleSource = Literal["identities", "local_accounts"]


# ── Wire shapes ──────────────────────────────────────────────────────────


class PeopleCapabilities(_StrictModel):
    """What the CALLER may administer. UI advice: every route re-checks its own guard."""

    identity_admin: bool
    local_accounts: bool
    # The deployment's configured sign-in provider, so the panel can explain
    # how people here are identified without guessing from the caller's name.
    auth_provider: AuthProviderType
    # Whether the deployment runs the quota system (``WebSettings.quotas_enabled``).
    # Off, the panel shows usage and offers no cap form it knows would be refused.
    quotas_enabled: bool
    self_identity_id: str
    self_username: str


class LocalAccountView(_StrictModel):
    """A local sign-in account. Never a password, a hash or a token."""

    username: str
    display_name: str
    email: str | None
    email_verified: bool


class PersonActions(_StrictModel):
    """Advisory. A control shown because of these is still refused by the server if the caller lost the right."""

    manage_access: bool
    manage_credentials: bool
    set_up_access: bool
    is_self: bool


class IdentityPerson(_StrictModel):
    """A person the container has an identity row for, with their local account when the caller may see it."""

    record_type: Literal["identity"]
    key: str
    identity: IdentityView
    # The row ``retire_identity`` left behind when a local account was
    # deleted: history, not a person who can sign in.
    retired: bool
    # This person is the ONLY active human administrator, so the server will
    # refuse to disable, retire or demote them. Said per person so the
    # warning appears on the one person it is about. Advisory: R5 is decided
    # by the mutation, under its own lock.
    sole_active_admin: bool
    local_account: LocalAccountView | None
    actions: PersonActions


class LocalAccountPerson(_StrictModel):
    """A local account shown WITHOUT an identity.

    ``not_set_up`` is a fact: the caller may read identities and none is bound
    to this username. ``not_visible`` is the absence of one: the caller may
    not read identities, so nothing is claimed about this person's access.
    """

    record_type: Literal["local_account"]
    key: str
    local_account: LocalAccountView
    access: Literal["not_set_up", "not_visible"]
    actions: PersonActions


PersonRecord = Annotated[IdentityPerson | LocalAccountPerson, Field(discriminator="record_type")]


class PeopleListResponse(_StrictModel):
    people: list[PersonRecord]
    limit: int
    offset: int
    # No total: neither store is counted, and a badge that guessed would be
    # read as a fact. ``has_more`` is exact because one extra row is fetched.
    has_more: bool
    capabilities: PeopleCapabilities
    # Read in the same request as the list so the single-administrator
    # advisory and the roster agree. ``None`` when the caller may not know.
    active_human_admin_count: int | None


class PersonResponse(_StrictModel):
    person: PersonRecord
    capabilities: PeopleCapabilities


class PersonLabel(_StrictModel):
    """How to NAME an identity that is referenced from another person's record."""

    identity_id: str
    label: str
    # The username or subject that tells two people with one name apart. The
    # sign-in method is DATA beside it, not text inside it: the panel names
    # providers from one map, and a server-formatted "sam · oidc" was the one
    # place a raw provider token still reached an administrator.
    detail: str
    provider: IdentityProviderType
    kind: Literal["human", "service"]
    access_state: IdentityAccessState
    retired: bool


class PersonLabelsResponse(_StrictModel):
    labels: list[PersonLabel]


# ── Projections ──────────────────────────────────────────────────────────


def identity_key(identity_id: str) -> str:
    return f"identity:{identity_id}"


def local_key(username: str) -> str:
    return f"local:{username}"


def _local_view(account: LocalUserAccount) -> LocalAccountView:
    return LocalAccountView(
        username=account.user_id,
        display_name=account.display_name,
        email=account.email,
        email_verified=account.email_verified,
    )


def _identity_person(
    summary: IdentitySummary,
    *,
    account: LocalUserAccount | None,
    user: UserIdentity,
    active_admin_ids: frozenset[str],
) -> IdentityPerson:
    return IdentityPerson(
        record_type="identity",
        key=identity_key(summary.identity_id),
        identity=_identity_view(summary),
        retired=is_retired_identity(summary),
        sole_active_admin=active_admin_ids == {summary.identity_id},
        local_account=None if account is None else _local_view(account),
        actions=PersonActions(
            manage_access=True,
            manage_credentials=account is not None,
            set_up_access=False,
            is_self=summary.identity_id == user.user_id,
        ),
    )


def _local_person(account: LocalUserAccount, *, identities_visible: bool, user: UserIdentity) -> LocalAccountPerson:
    return LocalAccountPerson(
        record_type="local_account",
        key=local_key(account.user_id),
        local_account=_local_view(account),
        access="not_set_up" if identities_visible else "not_visible",
        actions=PersonActions(
            manage_access=False,
            manage_credentials=True,
            set_up_access=identities_visible,
            is_self=account.user_id == user.username,
        ),
    )


def _person_label(summary: IdentitySummary, *, account: LocalUserAccount | None) -> PersonLabel:
    """Named from the SAME projection the directory shows, so a label cannot reveal what a row withholds.

    An identity prepared ahead of its first sign-in has no profile yet, so the
    linked local account's name fills that gap -- for a caller who may read
    accounts, and NEVER for a never-admitted pending row. That row's profile
    is withheld on purpose (the projection blanks its ``username``), and a
    separately authorized source must not put back what it left out.
    """
    view = _identity_view(summary)
    shown_name = view.display_name.strip() if view.display_name is not None else ""
    if not shown_name and account is not None and view.username is not None:
        shown_name = account.display_name.strip()
    label = shown_name or view.username or view.subject
    secondary = view.username if (view.username is not None and view.username != label) else view.subject
    return PersonLabel(
        identity_id=view.identity_id,
        label=label,
        detail=secondary,
        provider=view.provider,
        kind=view.kind,
        access_state=view.access_state,
        retired=is_retired_identity(summary),
    )


def _account_label(account: LocalUserAccount) -> tuple[str, str]:
    shown = account.display_name.strip() or account.user_id
    return (shown.lower(), account.user_id)


def _account_matches(account: LocalUserAccount, text: str | None) -> bool:
    """Literal, case-insensitive containment: the same meaning the identity search gives a needle.

    ``lower``, not ``casefold``: the identity segment folds with the
    database's ``lower``, and one directory must not find "STRASSE" under
    "straße" in one segment and miss it in the other.
    """
    if text is None:
        return True
    needle = text.lower()
    return any(needle in field.lower() for field in (account.user_id, account.display_name, account.email or ""))


# ── Request plumbing ─────────────────────────────────────────────────────


def _source_unavailable(source: PeopleSource) -> HTTPException:
    return HTTPException(
        status_code=503,
        detail={"unavailable_source": source, "detail": f"The {source.replace('_', ' ')} store could not be read."},
    )


def _filter_needs(capability: Literal["identity_admin", "local_accounts"], status: PeopleStatusFilter) -> HTTPException:
    return HTTPException(
        status_code=422,
        detail={
            "refusal": "filter_requires_capability",
            "capability": capability,
            "detail": f"The '{status}' status filter needs the {capability} capability.",
        },
    )


async def _resolve(request: Request) -> tuple[UserIdentity, PeopleCapabilities]:
    """Authenticate, then resolve BOTH capabilities against live state. Nothing is cached."""
    user = await get_current_user(request)
    settings: WebSettings = request.app.state.settings
    try:
        identity_admin = await run_sync_in_worker(_authority(request).holds_active_role, identity_id=user.user_id, role="admin")
        local_accounts = await can_manage_local_accounts(request, user)
    except SQLAlchemyError as exc:
        raise _source_unavailable("identities") from exc
    return user, PeopleCapabilities(
        identity_admin=identity_admin,
        local_accounts=local_accounts,
        auth_provider=settings.auth_provider,
        quotas_enabled=settings.quotas_enabled,
        self_identity_id=user.user_id,
        self_username=user.username,
    )


async def _require_any(request: Request) -> tuple[UserIdentity, PeopleCapabilities]:
    user, capabilities = await _resolve(request)
    if not (capabilities.identity_admin or capabilities.local_accounts):
        raise _hidden()
    return user, capabilities


def _local_provider(request: Request) -> LocalAuthProvider:
    provider: LocalAuthProvider = request.app.state.auth_provider
    return provider


async def _all_local_accounts(request: Request) -> list[LocalUserAccount]:
    try:
        return await run_sync_in_worker(_local_provider(request).list_users)
    except sqlite3.Error as exc:
        raise _source_unavailable("local_accounts") from exc


async def _bound_local_identities(request: Request, usernames: list[str]) -> dict[str, IdentitySummary]:
    """username -> the identity live-bound to it. Chunked so the authority's bound holds for any store size."""
    bound: dict[str, IdentitySummary] = {}
    authority = _authority(request)
    try:
        for start in range(0, len(usernames), DIRECTORY_LOOKUP_MAX):
            chunk = usernames[start : start + DIRECTORY_LOOKUP_MAX]
            for summary in await run_sync_in_worker(authority.read_local_identity_summaries, usernames=chunk):
                bound[summary.subject] = summary
    except SQLAlchemyError as exc:
        raise _source_unavailable("identities") from exc
    return bound


def _linked_account(by_username: dict[str, LocalUserAccount], summary: IdentitySummary) -> LocalUserAccount | None:
    """The local account behind an identity, by EXACT correlation; ``None`` for every other identity.

    An identity-only caller passes an empty mapping, so nothing correlates and
    no credential fact is attached.
    """
    if summary.provider != "local" or is_retired_identity(summary) or summary.subject not in by_username:
        return None
    return by_username[summary.subject]


_KIND_OF_TYPE: dict[PeopleTypeFilter, Literal["human", "service"] | None] = {"people": "human", "service": "service", "all": None}


def create_people_router() -> APIRouter:
    """Create the people directory router; mounted for every provider."""
    router = APIRouter(prefix="/api/auth/admin/people", tags=["people"])

    @router.get("/capabilities", response_model=PeopleCapabilities)
    async def read_capabilities(request: Request, response: Response) -> PeopleCapabilities:
        """The caller's own capabilities, for any signed-in user: it discloses nothing about anyone else."""
        _user, capabilities = await _resolve(request)
        _uncacheable(response)
        return capabilities

    @router.get("/labels", response_model=PersonLabelsResponse)
    async def read_labels(
        request: Request,
        response: Response,
        identity_id: Annotated[list[str], Query(min_length=1, max_length=LABEL_LOOKUP_MAX)],
    ) -> PersonLabelsResponse:
        """Names for identities referenced from another record -- including people not on the current page."""
        _user, capabilities = await _require_any(request)
        if not capabilities.identity_admin:
            raise _hidden()
        wanted = sorted({value for value in identity_id if 1 <= len(value) <= 64})
        if len(wanted) != len(set(identity_id)):
            raise HTTPException(status_code=422, detail="identity_id values must be 1-64 characters")
        try:
            summaries = await run_sync_in_worker(_authority(request).read_identity_summaries, identity_ids=wanted)
        except SQLAlchemyError as exc:
            raise _source_unavailable("identities") from exc
        by_username = (
            {account.user_id: account for account in await _all_local_accounts(request)}
            if capabilities.local_accounts and any(summary.provider == "local" for summary in summaries)
            else {}
        )
        _uncacheable(response)
        return PersonLabelsResponse(labels=[_person_label(summary, account=_linked_account(by_username, summary)) for summary in summaries])

    @router.get("/identity/{identity_id}", response_model=PersonResponse)
    async def read_identity_person(request: Request, response: Response, identity_id: str) -> PersonResponse:
        """One person by identity, resolved directly rather than by rescanning pages."""
        user, capabilities = await _require_any(request)
        if not capabilities.identity_admin or not 1 <= len(identity_id) <= 64:
            raise _hidden()
        try:
            summary = await run_sync_in_worker(_authority(request).read_identity_summary, identity_id=identity_id)
            active_admin_ids = await run_sync_in_worker(_authority(request).active_human_admin_ids)
        except SQLAlchemyError as exc:
            raise _source_unavailable("identities") from exc
        if summary is None:
            raise _hidden()
        account: LocalUserAccount | None = None
        if capabilities.local_accounts and summary.provider == "local" and not is_retired_identity(summary):
            try:
                account = await run_sync_in_worker(_local_provider(request).read_user, summary.subject)
            except sqlite3.Error as exc:
                raise _source_unavailable("local_accounts") from exc
        _uncacheable(response)
        return PersonResponse(
            person=_identity_person(summary, account=account, user=user, active_admin_ids=active_admin_ids),
            capabilities=capabilities,
        )

    @router.get("/local/{username}", response_model=PersonResponse)
    async def read_local_person(request: Request, response: Response, username: str) -> PersonResponse:
        """One person by local username.

        Answers with the IDENTITY record once one is bound, so a panel that
        selected an "access not set up" account and then set access up is
        handed the person's permanent key on its next read.
        """
        user, capabilities = await _require_any(request)
        if not capabilities.local_accounts:
            raise _hidden()
        try:
            account = await run_sync_in_worker(_local_provider(request).read_user, username)
        except sqlite3.Error as exc:
            raise _source_unavailable("local_accounts") from exc
        if account is None:
            raise _hidden()
        person: IdentityPerson | LocalAccountPerson
        if capabilities.identity_admin:
            bound = await _bound_local_identities(request, [account.user_id])
            # No identity bound to the username is an ordinary answer here, not a missing key.
            summary = bound[account.user_id] if account.user_id in bound else None
            if summary is None:
                person = _local_person(account, identities_visible=True, user=user)
            else:
                try:
                    active_admin_ids = await run_sync_in_worker(_authority(request).active_human_admin_ids)
                except SQLAlchemyError as exc:
                    raise _source_unavailable("identities") from exc
                person = _identity_person(summary, account=account, user=user, active_admin_ids=active_admin_ids)
        else:
            person = _local_person(account, identities_visible=False, user=user)
        _uncacheable(response)
        return PersonResponse(person=person, capabilities=capabilities)

    @router.get("", response_model=PeopleListResponse)
    async def list_people(
        request: Request,
        response: Response,
        q: Annotated[str | None, Query(max_length=DIRECTORY_TEXT_MAX)] = None,
        status: Annotated[PeopleStatusFilter, Query()] = "all",
        provider: Annotated[IdentityProviderType | None, Query()] = None,
        person_type: Annotated[PeopleTypeFilter, Query(alias="type")] = "people",
        limit: Annotated[int, Query(ge=1, le=PEOPLE_PAGE_MAX)] = 50,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> PeopleListResponse:
        user, capabilities = await _require_any(request)
        text = q.strip() if q is not None and q.strip() else None
        if status == "not_set_up" and not (capabilities.local_accounts and capabilities.identity_admin):
            # "Not set up" is a statement about BOTH stores.
            raise _filter_needs("identity_admin" if capabilities.local_accounts else "local_accounts", status)
        if status in ("pending", "active", "disabled") and not capabilities.identity_admin:
            raise _filter_needs("identity_admin", status)

        accounts = await _all_local_accounts(request) if capabilities.local_accounts else []
        by_username = {account.user_id: account for account in accounts}
        bound = await _bound_local_identities(request, list(by_username)) if capabilities.identity_admin and accounts else {}

        # Segment A: local accounts shown without an identity.
        accounts_apply = status in ("all", "not_set_up") and provider in (None, "local") and person_type in ("people", "all")
        unlinked = sorted(
            (account for account in accounts if account.user_id not in bound and _account_matches(account, text)) if accounts_apply else (),
            key=_account_label,
        )
        wanted = limit + 1
        page: list[IdentityPerson | LocalAccountPerson] = [
            _local_person(account, identities_visible=capabilities.identity_admin, user=user)
            for account in unlinked[offset : offset + wanted]
        ]

        # Segment B: identities, continuing the same sequence.
        active_admins: int | None = None
        if capabilities.identity_admin:
            authority = _authority(request)
            try:
                # Read once, before the rows: the count the advisory shows and
                # the person each row names as sole administrator are one fact.
                active_admin_ids = await run_sync_in_worker(authority.active_human_admin_ids)
                active_admins = len(active_admin_ids)
                if status != "not_set_up" and len(page) < wanted:
                    # A person prepared ahead of their first sign-in is shown
                    # under their ACCOUNT's name and email, which the identity
                    # store does not hold. Name the matching linked accounts so
                    # the search finds the label it displays, before slicing.
                    linked_matches = (
                        tuple(sorted(username for username in bound if _account_matches(by_username[username], text)))
                        if text is not None
                        else ()
                    )
                    summaries = await run_sync_in_worker(
                        authority.search_identities,
                        query=IdentityDirectoryQuery(
                            text=text,
                            access_state=None if status == "all" else status,
                            provider=provider,
                            kind=_KIND_OF_TYPE[person_type],
                            linked_local_subjects=linked_matches,
                        ),
                        limit=wanted - len(page),
                        offset=max(0, offset - len(unlinked)),
                    )
                    page.extend(
                        _identity_person(
                            summary,
                            account=_linked_account(by_username, summary),
                            user=user,
                            active_admin_ids=active_admin_ids,
                        )
                        for summary in summaries
                    )
            except SQLAlchemyError as exc:
                raise _source_unavailable("identities") from exc

        _uncacheable(response)
        return PeopleListResponse(
            people=page[:limit],
            limit=limit,
            offset=offset,
            has_more=len(page) > limit,
            capabilities=capabilities,
            active_human_admin_count=active_admins,
        )

    return router
