"""Auth API routes -- /api/auth/login, /api/auth/register, /api/auth/token, /api/auth/config, /api/auth/me.

POST /login is only available when auth_provider is "local".
POST /register is only available when auth_provider is "local" and registration is open.
POST /verify-email consumes a local-auth email verification token.
POST /token re-issues a JWT from a valid existing token (local only).
GET /config returns auth configuration for frontend discovery (unauthenticated).
GET /me returns the full UserProfile for any auth provider.
"""

from __future__ import annotations

from typing import cast
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from elspeth.contracts.auth import AuthProviderType
from elspeth.contracts.trust_boundary import observation_boundary
from elspeth.core.landscape.auth_audit_repository import AUTH_AUDIT_PRINCIPAL_MAX_LENGTH
from elspeth.core.url_validation import validate_credential_safe_https_url
from elspeth.web.async_workers import run_sync_in_worker
from elspeth.web.auth.audit import AuthAuditWriter, classify_authentication_failure
from elspeth.web.auth.local import LocalAuthProvider, LocalAuthRegistrationConflict, bcrypt_password_bytes
from elspeth.web.auth.middleware import get_current_user
from elspeth.web.auth.models import AuthenticationError, AuthProviderUnavailable, UserIdentity
from elspeth.web.auth.protocol import AuthProvider, CredentialAuthProvider
from elspeth.web.auth.sso import (
    COOKIE_NAME,
    AdmittedIdentity,
    CallbackQuery,
    SsoLoginError,
    SsoRuntime,
    authorization_redirect,
    complete_login,
    cookie_attributes,
    failure_category,
    failure_location,
    login_callback,
)
from elspeth.web.config import WebSettings
from elspeth.web.middleware.rate_limit import check_auth_rate_limit
from elspeth.web.validation import has_visible_content


class _StrictResponse(BaseModel):
    """Tier 1 base for auth responses — no coercion, no extras.

    Auth responses carry identity material (user_id, groups, tokens) that
    the audit trail and authorization boundary both rely on.  Strict
    construction means a buggy provider adapter cannot smuggle unexpected
    claims into ``/me`` or ``/config``, and cannot silently add a
    ``refresh_token`` field to the bearer-only ``TokenResponse``.
    """

    model_config = ConfigDict(strict=True, extra="forbid")


class LoginRequest(BaseModel):
    """Request body for POST /api/auth/login."""

    # Reject oversized usernames at the boundary: the username is recorded as
    # the auth_events principal, so an unbounded value is an unauthenticated
    # audit-storage amplification vector. Bound matches the audit column.
    username: str = Field(max_length=AUTH_AUDIT_PRINCIPAL_MAX_LENGTH)
    password: str

    @field_validator("password")
    @classmethod
    def _password_must_fit_bcrypt(cls, v: str) -> str:
        bcrypt_password_bytes(v)
        return v


class RegisterRequest(BaseModel):
    """Request body for POST /api/auth/register."""

    username: str = Field(max_length=AUTH_AUDIT_PRINCIPAL_MAX_LENGTH)
    password: str
    display_name: str
    email: str | None = None

    @field_validator("password")
    @classmethod
    def _password_must_fit_bcrypt(cls, v: str) -> str:
        bcrypt_password_bytes(v)
        return v

    @field_validator("username", "password", "display_name")
    @classmethod
    def _must_not_be_blank(cls, v: str) -> str:
        if not has_visible_content(v):
            raise ValueError("must contain at least one visible character")
        return v

    @field_validator("email")
    @classmethod
    def _email_must_be_deliverable_when_present(cls, v: str | None) -> str | None:
        if v is None:
            return None
        trimmed = v.strip()
        if not has_visible_content(trimmed):
            raise ValueError("must contain at least one visible character")
        local, separator, domain = trimmed.partition("@")
        if separator != "@" or not local or not domain:
            raise ValueError("must be a valid email address")
        return trimmed


class VerifyEmailRequest(BaseModel):
    """Request body for POST /api/auth/verify-email."""

    token: str

    @field_validator("token")
    @classmethod
    def _token_must_not_be_blank(cls, v: str) -> str:
        if not has_visible_content(v):
            raise ValueError("must contain at least one visible character")
        return v


class TokenResponse(_StrictResponse):
    """Response for login and token refresh."""

    access_token: str
    token_type: str = "bearer"


class RegistrationPendingResponse(_StrictResponse):
    """Response for email-verified registration before token verification."""

    status: str = "verification_required"
    email: str


class UserProfileResponse(_StrictResponse):
    """Response for GET /api/auth/me."""

    user_id: str
    username: str
    display_name: str | None = None
    email: str | None = None
    groups: list[str] = []
    # True only for the local-auth user named by WebSettings.dev_admin_user;
    # the frontend uses it to reveal the dev user-management menu entry.
    dev_admin: bool = False


class AuthConfigResponse(_StrictResponse):
    """Response for GET /api/auth/config."""

    provider: AuthProviderType
    registration_mode: str
    oidc_issuer: str | None = None
    oidc_client_id: str | None = None
    authorization_endpoint: str | None = None
    token_endpoint: str | None = None
    # Where the SPA's "Sign in with SSO" button navigates. ``None`` whenever
    # the deployment is not wired for SSO — the same fact that makes the
    # three ``/sso/*`` routes refuse — so the button is hidden by the
    # condition that would make it fail, not by a separate guess.
    sso_start_url: str | None = None


class SsoCompleteRequest(BaseModel):
    """Request body for POST /api/auth/sso/complete: the handoff code from the fragment."""

    # A handoff code is 43 characters (token_urlsafe(32)). The bound is
    # generous but present: the code is hashed as-is, and an unauthenticated
    # caller must not be able to make the hash the expensive part.
    code: str = Field(min_length=1, max_length=128)

    model_config = ConfigDict(strict=True, extra="forbid")


def _mark_sensitive_auth_response_uncacheable(response: Response) -> None:
    """Bearer-token and live authority responses must not be retained by caches."""
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"


def _auth_audit_recorder(request: Request) -> AuthAuditWriter:
    return cast(AuthAuditWriter, request.app.state.auth_audit_recorder)


def _sso_runtime_if_wired(request: Request) -> SsoRuntime | None:
    """The SSO runtime, or ``None`` when this deployment is not wired for it.

    ONE attribute, ONE owned type. ``app.state.sso`` is set by the app
    factory when the provider is an IdP and every SSO setting resolved; the
    routes and ``/config`` read it here and nowhere else. Absent, or present
    but not an ``SsoRuntime``, means "not wired" — and that answer is the
    same whether the process is genuinely local-auth or is running a build in
    which the wiring has not landed yet. Nothing here reaches for a second
    attribute or guesses.
    """
    settings: WebSettings = request.app.state.settings
    if settings.auth_provider == "local":
        return None
    try:
        candidate = request.app.state.sso
    except AttributeError:
        return None
    if not isinstance(candidate, SsoRuntime):
        return None
    return candidate


async def _sso_runtime(request: Request, *, stage: str) -> SsoRuntime:
    """The SSO runtime for a ``/sso/*`` route, or the route's refusal.

    Local auth: 404, the route does not exist for this deployment — same as
    ``/login`` on an IdP deployment. An IdP provider with no runtime: 503
    ``provider_unavailable`` WITH an audit row, because a browser reached an
    SSO route on a deployment that says it does SSO and could not be served.
    That is exactly the unwired state a build carries before its wiring
    lands, and it fails closed rather than half-way.
    """
    settings: WebSettings = request.app.state.settings
    if settings.auth_provider == "local":
        raise HTTPException(status_code=404, detail="Not found")
    runtime = _sso_runtime_if_wired(request)
    if runtime is None:
        _auth_audit_recorder(request).record_auth_failure(
            request,
            provider=settings.auth_provider,
            failure_category="provider_unavailable",
            failure_stage=stage,
            user_id=None,
            username=None,
            exception_class=None,
        )
        raise HTTPException(status_code=503, detail="Single sign-on is not configured on this deployment")
    return runtime


def _single_query_value(request: Request, key: str) -> str | None:
    """Exactly one occurrence of ``key`` in the query string, else ``None``.

    The callback's query is written by whoever redirected the browser. A
    duplicated ``state`` or ``code`` is not a value with a tie-break rule; it
    is a request that does not match the one the IdP sends, and absence is
    what the walk refuses on.
    """
    values = request.query_params.getlist(key)
    if len(values) != 1:
        return None
    return values[0]


def _route_auth_failure(
    request: Request,
    *,
    detail: str,
    failure_category: str,
    failure_stage: str,
    user: UserIdentity | None = None,
    exc: AuthenticationError | None = None,
) -> HTTPException:
    """Record one route-owned auth failure before constructing its 401."""
    settings: WebSettings = request.app.state.settings
    _auth_audit_recorder(request).record_auth_failure(
        request,
        provider=settings.auth_provider,
        failure_category=failure_category,
        failure_stage=failure_stage,
        # ``user_id`` is the principal as the request named it; the
        # identity_id goes in its own column. Splitting them keeps one query
        # per question instead of a column that means two things.
        user_id=None if user is None else user.username,
        username=None if user is None else user.username,
        identity_id=None if user is None else user.user_id,
        exception_class=None if exc is None else type(exc).__name__,
    )
    return HTTPException(status_code=401, detail=detail)


@observation_boundary(
    tier=3,
    source="browser-supplied Origin request header",
    source_param="request",
    suppresses=("R1",),
    invariant=(
        "returns None on an absent, non-allowlisted, non-HTTPS-safe, or non-bare-origin "
        "Origin header; only a normalised, allowlisted, credential-safe origin is returned"
    ),
)
def _trusted_request_origin(settings: WebSettings, request: Request) -> str | None:
    origin = request.headers.get("origin")
    if origin is None:
        return None
    normalized_origin = origin.strip().rstrip("/")
    trusted_origins = {configured.strip().rstrip("/") for configured in settings.cors_origins}
    if normalized_origin not in trusted_origins:
        return None
    try:
        safe_origin = validate_credential_safe_https_url(
            normalized_origin,
            field_name="Origin",
            allow_http_loopback=True,
        ).rstrip("/")
    except ValueError:
        return None
    parsed = urlparse(safe_origin)
    if parsed.path not in {"", "/"} or parsed.params or parsed.query or parsed.fragment:
        return None
    return safe_origin


def _email_verification_origin(settings: WebSettings, request: Request) -> str:
    if settings.public_base_url is not None:
        return settings.public_base_url
    trusted_origin = _trusted_request_origin(settings, request)
    if trusted_origin is not None:
        return trusted_origin
    host = settings.host
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return f"http://{host}:{settings.port}"


def create_auth_router() -> APIRouter:
    """Create the auth router with /api/auth prefix."""
    router = APIRouter(prefix="/api/auth", tags=["auth"])

    @router.post("/login", response_model=TokenResponse)
    async def login(
        body: LoginRequest,
        request: Request,
        response: Response,
        _rate_limit: None = Depends(check_auth_rate_limit),
    ) -> TokenResponse:
        """Authenticate with username/password (local auth only).

        login() is synchronous (bcrypt is intentionally slow ~200ms),
        so it is offloaded to a thread to avoid blocking the event loop.

        No CSRF token required — this is a bearer-token-only API (no
        cookies). If cookie-based sessions are added later, CSRF
        protection must be revisited.
        """
        settings: WebSettings = request.app.state.settings
        if settings.auth_provider != "local":
            raise HTTPException(status_code=404, detail="Not found")

        provider: CredentialAuthProvider = request.app.state.auth_provider
        try:
            token = await provider.login(body.username, body.password)
        except AuthenticationError as exc:
            recorder = _auth_audit_recorder(request)
            recorder.record_login_failure(
                request,
                provider=settings.auth_provider,
                username=body.username,
                # Classified, not hardcoded. Login can now fail for reasons
                # that are not a bad credential — an identity awaiting
                # approval, or one that has been disabled — and recording
                # those as invalid_credentials would hide an approval queue
                # inside the trail that is supposed to surface brute force.
                failure_category=classify_authentication_failure(exc),
            )
            raise HTTPException(status_code=401, detail=exc.detail) from exc

        recorder = _auth_audit_recorder(request)
        recorder.record_login_success_and_token_issued(
            request,
            provider=settings.auth_provider,
            user_id=body.username,
            username=body.username,
            access_token=token,
        )
        _mark_sensitive_auth_response_uncacheable(response)
        return TokenResponse(access_token=token)

    @router.post(
        "/register",
        response_model=TokenResponse | RegistrationPendingResponse,
        status_code=200,
    )
    async def register(
        body: RegisterRequest,
        request: Request,
        response: Response,
        _rate_limit: None = Depends(check_auth_rate_limit),
    ) -> TokenResponse | RegistrationPendingResponse:
        """Register a new user account (local auth, open registration only).

        Creates the user with bcrypt-hashed password (offloaded to a thread),
        then auto-logs in and returns a JWT so the caller can proceed
        immediately without a separate login round-trip.
        """
        settings: WebSettings = request.app.state.settings
        if settings.auth_provider != "local":
            raise HTTPException(status_code=404, detail="Not found")

        if settings.registration_mode == "closed":
            raise HTTPException(status_code=404, detail="Not found")

        if settings.registration_mode == "email_verified" and body.email is None:
            raise HTTPException(
                status_code=422,
                detail="email is required when registration_mode=email_verified",
            )

        provider: LocalAuthProvider = request.app.state.auth_provider
        if settings.registration_mode == "email_verified":
            assert body.email is not None
            outbox_path = settings.data_dir / "email-verifications.jsonl"
            origin = _email_verification_origin(settings, request)
            try:
                await run_sync_in_worker(
                    provider.register_email_verified_user,
                    body.username,
                    body.password,
                    body.display_name,
                    email=body.email,
                    verification_origin=origin,
                    outbox_path=outbox_path,
                )
            except LocalAuthRegistrationConflict as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            except OSError as exc:
                raise HTTPException(status_code=500, detail="Email verification outbox could not be written") from exc
            response.status_code = 202
            return RegistrationPendingResponse(email=body.email)

        recorder = _auth_audit_recorder(request)

        def record_registration_token(access_token: str) -> None:
            recorder.record_token_issued(
                request,
                provider=settings.auth_provider,
                user_id=body.username,
                username=body.username,
                access_token=access_token,
                issuance_path="register",
            )

        try:
            token = await run_sync_in_worker(
                provider.register_open_user_with_audit,
                body.username,
                body.password,
                body.display_name,
                body.email,
                record_token_issued=record_registration_token,
            )
        except LocalAuthRegistrationConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        _mark_sensitive_auth_response_uncacheable(response)
        return TokenResponse(access_token=token)

    @router.post("/verify-email", response_model=TokenResponse)
    async def verify_email(
        body: VerifyEmailRequest,
        request: Request,
        response: Response,
        _rate_limit: None = Depends(check_auth_rate_limit),
    ) -> TokenResponse:
        """Verify a pending local-auth registration and issue a bearer token."""
        settings: WebSettings = request.app.state.settings
        if settings.auth_provider != "local":
            raise HTTPException(status_code=404, detail="Not found")

        provider: LocalAuthProvider = request.app.state.auth_provider
        recorder = _auth_audit_recorder(request)

        def record_verification_token(identity: UserIdentity, access_token: str) -> None:
            recorder.record_token_issued(
                request,
                provider=settings.auth_provider,
                user_id=identity.username,
                username=identity.username,
                access_token=access_token,
                issuance_path="email_verification",
            )

        try:
            token = await run_sync_in_worker(
                provider.verify_email_and_issue_token,
                body.token,
                record_token_issued=record_verification_token,
            )
        except AuthenticationError as exc:
            raise _route_auth_failure(
                request,
                detail=exc.detail,
                failure_category="invalid_token",
                failure_stage="verify_email",
                exc=exc,
            ) from exc
        _mark_sensitive_auth_response_uncacheable(response)
        return TokenResponse(access_token=token)

    @router.post("/token", response_model=TokenResponse)
    async def refresh_token(
        request: Request,
        response: Response,
    ) -> TokenResponse:
        """Re-issue a session token from a valid existing one (local auth only).

        Hands the provider the TOKEN. The chain bound used to be enforced
        against an ``iat`` read here from the middleware's unverified decode;
        the provider's issuer now reads it from its own verified decode, so
        the route no longer stands between a signature and a security bound.
        """
        settings: WebSettings = request.app.state.settings
        if settings.auth_provider != "local":
            raise HTTPException(status_code=404, detail="Not found")

        user = await get_current_user(request)
        token = request.state.auth_token

        provider: CredentialAuthProvider = request.app.state.auth_provider
        try:
            new_token = await provider.refresh(token)
        except AuthenticationError as exc:
            raise _route_auth_failure(
                request,
                detail=exc.detail,
                failure_category=classify_authentication_failure(exc),
                failure_stage="refresh",
                user=user,
                exc=exc,
            ) from exc
        recorder = _auth_audit_recorder(request)
        recorder.record_token_issued(
            request,
            provider=settings.auth_provider,
            user_id=user.username,
            username=user.username,
            access_token=new_token,
            issuance_path="refresh",
        )
        _mark_sensitive_auth_response_uncacheable(response)
        return TokenResponse(access_token=new_token)

    @router.get("/config", response_model=AuthConfigResponse)
    async def auth_config(request: Request, response: Response) -> AuthConfigResponse:
        """Return auth configuration for frontend discovery.

        This endpoint is unauthenticated -- the frontend needs it
        before any login flow.
        """
        settings: WebSettings = request.app.state.settings
        _mark_sensitive_auth_response_uncacheable(response)
        sso_runtime = _sso_runtime_if_wired(request)
        return AuthConfigResponse(
            provider=settings.auth_provider,
            registration_mode=settings.registration_mode,
            oidc_issuer=settings.oidc_issuer,
            oidc_client_id=settings.oidc_client_id,
            authorization_endpoint=request.app.state.oidc_authorization_endpoint,
            token_endpoint=request.app.state.oidc_token_endpoint,
            sso_start_url=None if sso_runtime is None else sso_runtime.client.start_url,
        )

    # ── the SSO walk ─────────────────────────────────────────────────────
    #
    # Three routes, one service (web/auth/sso.py). These are DARK until the
    # app factory sets ``app.state.sso`` (identity sprint step C-wiring):
    # until then every one of them refuses through ``_sso_runtime`` and
    # ``/config`` reports ``sso_start_url: null``. The bodies below do no
    # verification of their own — they read the request, hand it to the
    # service, and write the audit rows the service leaves to them.

    @router.get("/sso/start")
    async def sso_start(
        request: Request,
        _rate_limit: None = Depends(check_auth_rate_limit),
    ) -> RedirectResponse:
        """Begin a login: seal a transaction cookie and send the browser to the IdP.

        Accepts NO query parameters (spec §2 [rev2]): a return path here
        would be an open-redirect parameter on the one route guaranteed to
        be reachable unauthenticated. Anything supplied is ignored.
        """
        runtime = await _sso_runtime(request, stage="sso_start")
        client = runtime.client
        redirect = authorization_redirect(
            authorization_endpoint=client.endpoints.authorization_endpoint,
            client_id=client.client_id,
            redirect_uri=client.redirect_uri,
            scopes=client.scopes,
            transaction_secret=client.transaction_secret,
            provider=client.provider,
        )
        response = RedirectResponse(redirect.location, status_code=302)
        response.set_cookie(**cookie_attributes(redirect.cookie_value))
        _mark_sensitive_auth_response_uncacheable(response)
        return response

    @router.get("/sso/callback")
    async def sso_callback(
        request: Request,
        _rate_limit: None = Depends(check_auth_rate_limit),
    ) -> RedirectResponse:
        """The IdP sent the browser back. Verify everything, hand back a handoff.

        Every outcome is a redirect to the SPA's ``#/auth/callback`` route
        with either ``code`` or ``error`` in the FRAGMENT, and every outcome
        clears the transaction cookie — a cookie that survived a failed
        callback would be a state the next login compares against.
        """
        runtime = await _sso_runtime(request, stage="sso_callback")
        settings: WebSettings = request.app.state.settings
        client = runtime.client
        recorder = _auth_audit_recorder(request)
        query = CallbackQuery(
            code=_single_query_value(request, "code"),
            state=_single_query_value(request, "state"),
            error=_single_query_value(request, "error"),
        )
        cookie_value = request.cookies[COOKIE_NAME] if COOKIE_NAME in request.cookies else None

        def record_login(identity: AdmittedIdentity) -> None:
            recorder.record_login_success(
                request,
                provider=settings.auth_provider,
                user_id=identity.identity_id,
                username=identity.username,
                identity_id=identity.identity_id,
            )

        try:
            location = await login_callback(
                query,
                cookie_value,
                client=client,
                validator=runtime.validator,
                claim_checks=runtime.claim_checks,
                map_identity=runtime.map_identity,
                upsert_identity=runtime.upsert_identity,
                record_login=record_login,
                handoffs=runtime.handoffs,
                request_id=request.state.request_id,
                transport=runtime.transport,
            )
        except (SsoLoginError, AuthProviderUnavailable) as exc:
            category = failure_category(exc)
            recorder.record_auth_failure(
                request,
                provider=settings.auth_provider,
                failure_category=category,
                failure_stage="sso_callback",
                user_id=None,
                username=None,
                exception_class=type(exc).__name__,
            )
            location = failure_location(client.public_base_url, category)

        response = RedirectResponse(location, status_code=302)
        response.set_cookie(**cookie_attributes(None))
        _mark_sensitive_auth_response_uncacheable(response)
        return response

    @router.post("/sso/complete", response_model=TokenResponse)
    async def sso_complete(
        body: SsoCompleteRequest,
        request: Request,
        response: Response,
        _rate_limit: None = Depends(check_auth_rate_limit),
    ) -> TokenResponse:
        """Trade the handoff code for the session token. The only place one is minted."""
        runtime = await _sso_runtime(request, stage="sso_complete")
        settings: WebSettings = request.app.state.settings
        recorder = _auth_audit_recorder(request)

        def record_token_issued(identity: AdmittedIdentity, token: str, login_request_id: str) -> None:
            recorder.record_token_issued(
                request,
                provider=settings.auth_provider,
                user_id=identity.identity_id,
                username=identity.username,
                access_token=token,
                issuance_path="sso_complete",
                login_request_id=login_request_id,
            )

        try:
            session = complete_login(
                body.code,
                handoffs=runtime.handoffs,
                read_identity=runtime.read_identity,
                issuer=runtime.issuer,
                record_token_issued=record_token_issued,
            )
        except SsoLoginError as exc:
            recorder.record_auth_failure(
                request,
                provider=settings.auth_provider,
                failure_category=exc.category,
                failure_stage="sso_complete",
                user_id=None,
                username=None,
                exception_class=type(exc).__name__,
            )
            raise HTTPException(status_code=401, detail=exc.detail) from exc

        _mark_sensitive_auth_response_uncacheable(response)
        return TokenResponse(access_token=session.access_token)

    @router.get("/me", response_model=UserProfileResponse)
    async def me(
        request: Request,
        user: UserIdentity = Depends(get_current_user),  # noqa: B008
    ) -> UserProfileResponse:
        """Return the full profile of the authenticated user.

        The raw token was stashed on request.state.auth_token by
        get_current_user, so we don't re-parse the Authorization header.
        """
        # Note: authenticate() already decoded the JWT in get_current_user.
        # get_user_info() decodes it again to extract profile claims.
        # This is intentional — the middleware returns UserIdentity (minimal),
        # while /me needs the full UserProfile with groups/email/display_name.
        settings: WebSettings = request.app.state.settings
        token: str = request.state.auth_token
        auth_provider: AuthProvider = request.app.state.auth_provider

        try:
            profile = await auth_provider.get_user_info(token)
        except AuthProviderUnavailable as exc:
            recorder = _auth_audit_recorder(request)
            recorder.record_auth_failure(
                request,
                provider=settings.auth_provider,
                failure_category="provider_unavailable",
                failure_stage="profile_lookup",
                user_id=user.username,
                username=user.username,
                identity_id=user.user_id,
                exception_class=type(exc).__name__,
            )
            raise HTTPException(status_code=503, detail=exc.detail) from exc
        except AuthenticationError as exc:
            recorder = _auth_audit_recorder(request)
            recorder.record_auth_failure(
                request,
                provider=settings.auth_provider,
                failure_category=classify_authentication_failure(exc),
                failure_stage="profile_lookup",
                user_id=user.username,
                username=user.username,
                identity_id=user.user_id,
                exception_class=type(exc).__name__,
            )
            raise HTTPException(status_code=401, detail=exc.detail) from exc

        return UserProfileResponse(
            user_id=profile.user_id,
            username=profile.username,
            display_name=profile.display_name,
            email=profile.email,
            groups=list(profile.groups),
            # Username, not user_id: ``dev_admin_user`` names a local account,
            # while user_id is the identity_id. Must agree with the same
            # comparison in admin_routes._require_dev_admin, or the frontend
            # shows an admin surface the backend then 404s.
            dev_admin=(
                settings.auth_provider == "local" and settings.dev_admin_user is not None and profile.username == settings.dev_admin_user
            ),
        )

    return router
