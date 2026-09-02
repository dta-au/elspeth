# Pluggable SSO and identity substrate — backend-for-frontend login for Entra, VANguard, Google, and generic OIDC

Date: 2026-09-02. Status: design, revision 2.1, implementation plan = tracker milestone elspeth-07cd19ba73.
Branch: `release/0.8.0`.
Revision 2 incorporates six independent reviews (security architecture,
solution design, reality check against the tree, systems risk, functional
needs, UX needs). Items marked **[rev2]** changed as a result. The raw reports
are session artefacts; the reconciled list lives in this document.

Sprint: this spec is the identity half of the "Identity and workflow
management" milestone in the tracker. Operator ruling (2026-09-02): build
90% of the final solution now and tweak on the fly, rather than a perfect
interim system that never gets permission to be replaced. So the workflow
half (approval, review attestation, shared library, per-day quota, manager
view, delegated administration) is BUILT in the same sprint, and its tables
ride the same epoch pass (phase 1 step 0 fixes their shapes as a
§Workflow tables addendum to this document before the epoch lands). The
seams in §Future seams are the starting point for that addendum.

## Problem

ELSPETH's web layer authenticates against one identity provider per
deployment, chosen by `WebSettings.auth_provider` from a closed set of
`local`, `oidc`, and `entra`. Adding VANguard (the Australian Government SSO,
reached through the DTA-owned SimpleSAMLphp OIDC bridge) and Google exposed
three limits:

1. **Pluggability stops at the class.** `EntraAuthProvider` is a 141-line
   wrapper over `JWKSTokenValidator`; Cognito is a mode flag on the same
   class. Every IdP is a new class plus edits to a dozen closed-set sites.
2. **The browser holds and presents IdP tokens.** `LoginPage.tsx` runs
   authorization-code + PKCE itself and stores the IdP's `access_token` as
   the Bearer for every ELSPETH request. That cannot work for Google, whose
   access tokens are opaque, and VANguard's profile claims (served only from
   `userinfo`) would need a provider call on every `/me`.
3. **Identity has no home.** SSO users exist only as claims inside a token.
   Nothing records who has logged in, with what profile, what roles they
   hold, or how staff relate to one another.

Operator ruling (2026-09-02): pay this now rather than when an auditor asks.
The tech-debt-free window is weeks, not months.

## Decisions

| # | Decision | Ruling |
|---|----------|--------|
| D1 | Persisted provider discriminator | Per-IdP values: `local`, `oidc`, `entra`, `vanguard`, `google`. |
| D2 | Who exchanges the authorization code | The backend, as a confidential client. Browser-side PKCE is deleted, not kept as a fallback. Cognito re-registers as confidential in this delivery. |
| D3 | Where an SSO profile lives | `identities` table in the web state (sessions) store. Session JWT stays minimal. |
| D4 | Staff relationships | Directed typed edges in `identity_relationships`, curated manually via admin routes. No derivation from IdP data, no approval enforcement in this delivery. |
| D5 | Delivery scope | Framework + all four profiles + Cognito migration + old path deleted. VANguard and Cognito verified live; Google verified live when a client exists. |
| D6 **[rev2]** | Session subject and ownership key | `sub` = `identity_id`. `sessions`, `user_secrets`, `user_preferences`, and Landscape `run_attributions` are keyed on `identity_id`. Accepted on reviewer recommendation; reversible until phase 1 lands. |
| D7 **[rev2]** | Local users | Get an `identities` row on first login (`provider='local'`, `subject=username`). `auth.db` becomes credentials only. Accepted on recommendation. |
| D8 **[rev2]** | `relationship_type` | Closed CHECK + L0 Literal (`manager` only now), widened per delivery. Accepted on recommendation. |
| D9 **[rev2]** | Roles | `identity_roles` table ships now (`admin`, `curator`); `sso_admin_subjects` only seeds the first admin. Accepted on recommendation. |
| D10 **[rev2]** | Principal above identity | No `principals` table. The VANguard spike asks for a stable non-email subject; if none, detection columns plus a refusal (§Refusals R3). Identity merge is an unbuilt admin action. |
| D11 **[rev2.1]** | Operator facts, now ruled | Quota is **per person**, the aggregate of tokens used in the composer and tokens used in runs. Approval quorum is one (a count column is "for but not with"). The term is **flex teams**, not hybrid teams: anyone in the organisation can log on to any container (deployment) of that organisation, but permissions are federated within that container only. The system takes SSO accounts; a container administrator grants `user` or `manager` permission and wires them into that container's org **tree**. A cross-container permissions manager is a possible later feature and is explicitly not built now. |
| D12 **[rev2.1]** | Default access | **No access, even with SSO, until an administrator gives the tick of approval.** A first login creates an identity in `pending`; no session token is issued until an admin activates it. |
| D13 **[rev2.1]** | Workflow tables | Built "for but not with": basic columns now, fleshed out later, all in the same epoch pass. See §Workflow tables. |

## Architecture

Four units replace the three provider classes and the browser exchange.

### 1. IdP profile registry — `src/elspeth/web/auth/providers/`

One frozen dataclass per IdP. A profile declares:

- `name`: its `AuthProviderType` value.
- `resolve_issuer(settings)`: from `sso_issuer` (oidc, vanguard), derived from
  `entra_tenant_id` (entra), fixed `https://accounts.google.com` (google).
- `expected_origins(settings)` **[rev2]**: the exact origin each discovered
  endpoint (authorization, token, userinfo, jwks) must have. Google fixed:
  `accounts.google.com`, `oauth2.googleapis.com`,
  `openidconnect.googleapis.com`, `www.googleapis.com` (measured
  2026-09-02). Entra derived from the tenant. VANguard same-origin as
  issuer. Generic OIDC: issuer origin plus the operator's
  `sso_endpoint_origins` (Cognito's hosted domain differs from the pool
  issuer).
- `scopes`: `openid profile email` for all four.
- `id_token_algorithms`: `("RS256",)` for all four.
- `claim_checks(payload)`: fail-closed checks beyond standard validation
  (Entra `tid`; Google `email_verified` true and `hd` equals
  `google_hosted_domain`; none for oidc and vanguard).
- `map_identity(id_claims, userinfo) -> IdentityClaims`.
- `userinfo: bool`.
- `required_settings`: the field names readiness checks non-blank.

**[rev2]** `AuthProviderType` in `contracts/auth.py` stays a hand-written L0
Literal (contracts import nothing above them, and a Literal cannot be
computed). The registry asserts parity at import:
`frozenset(profile names) | {"local"} == frozenset(get_args(AuthProviderType))`.
Adding an IdP is a deliberate edit to an L0 contract. `EntraAuthProvider`
is deleted; its tenant check and group/role extraction (including the
group-overage fail-closed) move into the Entra profile and are re-declared
`@trust_boundary` there with their own `test_ref`.

### 2. SSO login service — `src/elspeth/web/auth/sso.py`

- **start** (`GET /api/auth/sso/start`, no query parameters accepted
  **[rev2]**): generate PKCE verifier, `state`, `nonce`; seal them in the
  transaction cookie (§Transaction cookie); redirect to the profile's
  authorization endpoint with `client_id`, S256 challenge, `state`, `nonce`,
  scopes, and the callback URL built from `public_base_url`.
- **callback** (`GET /api/auth/sso/callback`): read and clear the cookie;
  reject on missing/invalid cookie, `state` mismatch, or an IdP `error`
  parameter; redeem the code at the token endpoint with `client_secret_basic`
  (httpx, redirects disabled, existing timeouts); parse the token response at
  a Tier-3 boundary (`token_type` Bearer case-insensitive, `id_token`
  non-empty, size bound); validate the ID token (§ID-token validation); run
  the profile's claim checks; call userinfo when the profile requires it
  (§Userinfo); discard the access and refresh tokens (ELSPETH never stores
  IdP tokens); upsert the identity; write `auth_events` `login`; write a
  handoff row (§Handoff); redirect to
  `{public_base_url}/#/auth/callback?code=<handoff>` — the code travels in
  the **fragment** so it never reaches the ALB or uvicorn **[rev2]**.
- **complete** (`POST /api/auth/sso/complete`): consume the handoff code
  atomically, re-check `disabled_at`, **mint the session token here**
  **[rev2]**, write `auth_events` `token_issued` joined to the login row by
  `request_id`, return `{access_token, token_type}`.

All three routes carry `check_auth_rate_limit`; failures are recorded through
`_record_auth_failure_after_rate_limit` **[rev2]**. Failure redirects carry
only a category from the closed set in §Failure categories. Discovery or
JWKS outage maps to the existing 503 `AuthProviderUnavailable` path.

**Endpoint policy [rev2].** The SSRF checks in
`auth/urls.py::validate_oidc_browser_endpoints` (HTTPS, no credentials,
canonical host, literal-IP block, dot-segment and encoding rejection,
parser-equivalence) are kept and generalised into one function applied to
all four discovered endpoints at startup and on every JWKS refresh, with
"same origin as issuer or in the operator allowlist" replaced by "matches
the profile's `expected_origins`". Discovery `issuer` must equal the
configured issuer exactly. Only the browser-origin allowlist field and its
normaliser (`validate_oidc_browser_origins`) are deleted. **Explicit
endpoint override stays as break-glass** (`sso_authorization_endpoint`,
`sso_token_endpoint`, `sso_userinfo_endpoint`, `sso_jwks_uri`, all-or-none,
still subject to the origin policy), because startup discovery is a hard IdP
dependency at the moment rollback is forbidden.

### 3. Session token issuer — `src/elspeth/web/auth/session_token.py`

HS256 minting, decoding, and the bounded refresh chain extracted from
`LocalAuthProvider`. **[rev2]** Claims: `sub` = `identity_id`, `username`
(display only), `provider`, `iss="elspeth"`, `aud=<public_base_url or
"elspeth-local">`, `jti`, `iat`, `exp`. `authenticate` rejects any token
whose `provider` differs from `settings.auth_provider`. Keys are derived
from `secret_key` with HKDF and distinct info strings for JWT signing,
user-secret encryption, and share-link signing, so the SSO delivery does not
widen the blast radius of one 32-byte string. Each provider supplies a
`principal_is_active(identity_id)` callback; for local that is the `auth.db`
row plus the identity row, for SSO the identity row.

`SsoAuthProvider` (one class, parameterised by profile) implements
`AuthProvider`: `authenticate` verifies the session token and confirms the
identity is enabled; `get_user_info` reads the identity row (including
`groups_json`). `POST /api/auth/token` is mounted for all providers and
re-checks `disabled_at` and `provider` **[rev2]**. `POST /api/auth/logout`
writes an `auth_events` `logout` row; the client discards the token
(server-side revocation is a future `jti` denylist) **[rev2]**.

**Disable reach [rev2].** Enforced at `authenticate`, `refresh`, `complete`,
and WebSocket-ticket issue. Open WebSockets and running pipelines are not
torn down: a run is an audited unit of work and its attribution is already
recorded; killing it mid-flight would leave a partial audit record. Pending
handoff rows for the identity are consumed on disable.

**BCP note [rev2].** This is the browser-app BCP's token-mediating backend
minus its cookie session: the SPA keeps a localStorage bearer by operator
choice, so an XSS in the SPA still yields a bearer. The cookie-session
variant is a recorded future option.

### 4. Identity persistence — sessions store

See §Data model.

### ID-token validation [rev2]

A dedicated ID-token decode path on `JWKSTokenValidator` (the Cognito
access-token branch and `oidc_audience_claim` are deleted, leaving one
path): algorithms pinned to the profile's list; any JWK whose `kty` is not
RSA or EC rejected before use; required claims `exp iat iss sub aud nonce`;
`nonce` compared with `hmac.compare_digest` against the cookie's; bounded
leeway 60 s; when `aud` is a list, `azp` must equal the client id. `kid`
match and key-miss refresh unchanged. Open ticket elspeth-e8a9973c37
(header-driven `alg`) is closed by this path; elspeth-8a9b311198 (401/503
ordering) now applies to four IdPs and is cross-referenced, not fixed here.

### Userinfo [rev2]

Response must be 200 with `application/json`, body ≤ 64 KiB, parsed at a
`@trust_boundary(tier=3)` function that requires userinfo `sub` to equal the
ID-token `sub` (compare_digest), reads only the profile's declared keys,
asserts each is a visible string, and constructs `IdentityClaims`. Anything
else fails the login with `sso_userinfo_invalid`.

### Transaction cookie [rev2]

`__Host-elspeth_sso_txn`; `Path=/`; `Secure` unconditionally (uvicorn runs
without proxy headers, so the request scheme is not trustworthy); `HttpOnly`;
`SameSite=Lax`; `Max-Age=300`. AES-256-GCM with a 96-bit random nonce; key =
HKDF(`sso_transaction_secret`, info `sso-transaction-v1`); plaintext
`{v:1, state, nonce, verifier, iat}`; AAD = `provider|redirect_uri|v`.
Callback rejects if `now - iat > 300 s` or `iat` is more than 30 s in the
future. Cleared on every callback outcome. `start` and `callback` responses
are `Cache-Control: no-store`. The cookie is stateless by design: replay of a
captured cookie plus callback URL is stopped by the IdP's single-use code;
ELSPETH enforces single use at the handoff. Two tabs each calling `start`
overwrite one cookie; the first callback fails `sso_state_mismatch` and the
banner says to try again. The API's bearer-only CSRF posture is unchanged;
the comment at `routes.py:242-244` is updated to say why.

### Handoff [rev2]

Table `sso_handoffs(code_hash PK, identity_id FK, issued_at, expires_at,
consumed_at NULL, request_id)`. The code is `secrets.token_urlsafe(32)`;
only `sha256(code)` is stored; the session token is never at rest. Consume
is one statement:
`UPDATE sso_handoffs SET consumed_at = <db now> WHERE code_hash = ? AND
consumed_at IS NULL AND expires_at > <db now> RETURNING identity_id`.
Zero rows means reject. Database clock, not replica clock. Expired rows are
purged by the existing retention sweep. `complete` is constant-time: hash,
then conditional update, never select-then-compare.

### Failure categories [rev2]

Closed set, each an explicit exception class, never a `detail` prefix:
`sso_cookie_missing`, `sso_cookie_invalid`, `sso_state_mismatch`,
`sso_idp_error` (IdP `error` mapped onto `{access_denied, other}`;
`error_description` never stored), `sso_token_exchange_failed`,
`sso_id_token_invalid`, `sso_claim_check_failed`, `sso_userinfo_invalid`,
`sso_identity_disabled`, `sso_access_pending`, `sso_handoff_invalid`,
`provider_unavailable`.

### Deleted outright [rev2]

- Browser-side PKCE in `LoginPage.tsx`, its transaction storage and tests
  (`LoginPage.test.tsx`), and the PKCE parts of `api/client.auth.test.ts`.
- `_BrowserDocumentHeadersMiddleware`'s per-request `connect-src` origin
  computation and `oidc_browser_endpoint_origin` (the browser no longer calls
  the token endpoint; the CSP collapses to the static prefix).
- `oidc_authorization_allowed_origins` and `validate_oidc_browser_origins`.
- Cognito access-token mode: `oidc_audience_claim`,
  `_validate_cognito_access_claims` and its inline trust-boundary entry.
- `EntraAuthProvider`; `OIDCAuthProvider` as a bearer validator of IdP tokens.
- `AuthConfigResponse` fields `oidc_issuer`, `oidc_client_id`,
  `authorization_endpoint`, `token_endpoint`.
- 24 tracked `frontend/dist/assets/index-*.js` bundles containing PKCE code;
  `dist/` is rebuilt and superseded bundles removed.
- A whole-tree AST assertion pins that nothing under `src/elspeth/web`
  imports a deleted symbol.

## Data model

### Discriminator widening — exact worklist [rev2]

`local | oidc | entra | vanguard | google` in:

1. `contracts/auth.py` `AuthProviderType` (hand-written).
2. `web/sessions/models.py` `_AUTH_PROVIDER_TYPE_CHECK`, used by BOTH
   `sessions` and `user_secrets`; `SESSION_SCHEMA_EPOCH` 49 → 50.
3. `core/landscape/schema.py` `ck_run_attributions_auth_provider_type`,
   `ck_auth_events_provider`; `SQLITE_SCHEMA_EPOCH` 36 → 37.
   `core/landscape/database.py` needs no edit (it lists names only).
4. `core/landscape/run_lifecycle_repository.py` `_AUTH_PROVIDER_TYPES`.
5. `web/readiness.py::_check_auth_mode` — rewritten to iterate the active
   profile's `required_settings`, no per-provider branches.
6. `web/config.py::_validate_auth_fields` — per-provider required/forbidden
   matrix from the registry.
7. `cli.py:4007` `--auth` help text and value validation.
8. `web/auth/routes.py` provider guards at lines 246 and 405.
9. `web/frontend/src/types/index.ts` provider union.
10. `tests/unit/web/auth/test_provider_type_contract.py` pins all of the
    above plus registry parity and the CHECK strings.

### Two epochs, one window [rev2]

Landscape compares declared CHECK text against the reflected constraint
structurally, so the widened constraints trip its schema validator exactly as
the 2026-08-14 index change did. Both epochs bump in the same delivery and
are cut over in one service-stop window per
`docs/runbooks/staging-session-db-recreation.md`.

Cutover by deployment:

- **ECS (Postgres, both stores):** archive/export required evidence, drop,
  recreate, `--init-schema`, compatibility record updated with
  `session_epoch: 50`, `landscape_epoch: 37`, `rollback_permitted: false`.
  In-place changes are forbidden by the pre-1.0 gate. All live SSO sessions
  are invalidated at this deploy; users log in again. This is stated in the
  operator notice for the window.
- **VM / SQLite:** the existing reset runbook, or a documented in-place
  rebuild that covers `sessions` and `user_secrets` (encrypted values and
  salts preserved byte-for-byte), backfills `identity_id` from
  `(provider, subject)` for every existing row, and restamps the
  `schema_identity` row. Landscape on the VM follows the same
  archive-drop-recreate.

Epoch fan-out checklist (all pinned by tests): `CHANGELOG.md`;
`tests/unit/website/test_release_site_contract.py`;
`docs/guides/sharing-pipelines.md` + `test_release_version_surfaces.py`;
`docs/runbooks/staging-session-db-recreation.md` + its policy test;
`web/_aws_ecs_acceptance/receipt_contracts.py` + `test_receipt_contracts.py`
+ `test_cleanup_control_service.py`; `docs/runbooks/aws-ecs-deployment.md`
compatibility-record example.

### `identities` (sessions store) [rev2]

| column | type | notes |
|--------|------|-------|
| identity_id | text PK | surrogate; the key every ownership and future workflow table references |
| provider | text | CHECK in the five values, `local` included (D7) |
| subject | text | IdP `sub`, or local username |
| username | text | display only, non-blank; changes update the row and write an audit row |
| display_name | text null | |
| email | text null | |
| organisation_id | text null | VANguard ABN; null elsewhere |
| groups_json | text | IdP groups/roles as a JSON list, refreshed per login; feeds `/me` |
| raw_claims_json | text | bounded 16 KiB; declared profile keys plus `iss aud iat exp`; `groups`, `_claim_*`, `picture`, `at_hash`, `nonce` stripped; forensics only, never returned by any API |
| subject_email_at_first_seen | text null | D10 detection |
| rebound_at | datetime null | D10 detection: verified email changed under the same subject |
| first_seen_at | datetime | |
| last_login_at | datetime | |
| access_state | text | CHECK `('pending','active','disabled')`; default `pending` (D12). Local follows `registration_mode`: `open` activates on registration, otherwise `pending`. |
| activated_at | datetime null | |
| activated_by_identity_id | text null FK | |
| disabled_at | datetime null | |
| disabled_by_identity_id | text null FK | |
| disable_reason | text null | |

Unique `(provider, subject)`. `identities` is **current state**; the
per-login profile snapshot in `auth_events.metadata_json` is the history of
record.

### `identity_roles` (sessions store) [rev2, D9]

| column | type | notes |
|--------|------|-------|
| role_id | text PK | |
| identity_id | text FK | |
| role | text | CHECK `('admin', 'manager', 'user', 'curator')`; L0 Literal `IdentityRole`. `user` = may author and run; `manager` = the functional/matrix lead: may approve for the people they lead in this container and hold `manager` edges; `curator` = library gate; `admin` = identity/roles/org-tree administration. Activation (D12) grants `user` unless the admin picks `manager`. |
| scope | text null | reserved (library id, team id); null = deployment-wide |
| granted_by_identity_id | text FK | |
| granted_at | datetime | |
| revoked_at | datetime null | never deleted |

Partial unique on active `(identity_id, role, scope)` with both
`sqlite_where` and `postgresql_where` declared (dialect-symmetry contract).
`sso_admin_subjects` seeds an `admin` row for a listed subject at first login
and is otherwise inert. Grant and revoke are admin-only in this delivery; a
manager appointing a curator is phase-5 design (delegated authorization).

### `identity_relationships` (sessions store) [rev2]

| column | type | notes |
|--------|------|-------|
| relationship_id | text PK | |
| from_identity_id | text FK | |
| to_identity_id | text FK | |
| relationship_type | text | CHECK `('manager')`; L0 Literal `RelationshipType` (D8) |
| asserted_by_identity_id | text FK | |
| asserted_at | datetime | |
| effective_from | datetime null | delegation / leave cover |
| effective_until | datetime null | |
| revoked_at | datetime null | never deleted |
| revoked_by_identity_id | text null FK | |
| note | text null | |

CHECK `from_identity_id <> to_identity_id`. **Org tree (D11):** partial
unique on active `(to_identity_id, relationship_type)` so a person has at
most one active manager, plus the partial unique on active `(from, to, type)`;
both with both dialect predicates declared. Cycles are refused at write time
by a bounded ancestor walk (route layer). `from_identity_id` must hold an
active `manager` role. Disabling an identity surfaces, and by default revokes
with the disabling actor recorded, every active edge terminating on it.

### `sso_handoffs` (sessions store) [rev2]

As in §Handoff.

### Ownership re-key [rev2, D6]

`sessions.user_id`, `user_secrets.user_id`, `user_preferences.user_id` carry
`identity_id` with a foreign key to `identities`. Landscape
`run_attributions.initiated_by_user_id` carries `identity_id` (no cross-store
FK). `auth_provider_type` columns stay for the audit reader. The contract
test keeps pinning that none of these boundaries widens to `str`.

### `auth_events` (Landscape) [rev2]

- `ck_auth_events_event_type` widened: `login`, `token_issued`,
  `auth_failure`, `logout`, `identity_activated`, `identity_disabled`,
  `identity_enabled`, `role_granted`, `role_revoked`,
  `relationship_asserted`, `relationship_revoked`, `approval_requested`,
  `approval_decided`, `review_attested`, `library_published`,
  `library_recalled`, `quota_set`, `quota_exceeded`.
- `calls` gains nullable `prompt_tokens`, `completion_tokens`,
  `cached_prompt_tokens`, `reasoning_tokens` written from the provider's
  `TokenUsage` at call-record time **[rev2.1]**. Measured 2026-09-02: the
  `calls` table stores only request/response hashes and refs; LLM token
  counts live inside the response payload blob and are not queryable, and
  the MCP "LLM usage report" counts pipeline row-tokens, not LLM tokens.
  So "tokens used in runs" is NOT exposed today; these columns expose it.
- New nullable indexed column `identity_id`.
- `login` is written at callback, `token_issued` at complete, joined by
  `request_id`. `metadata_json` for `login` is `{identity_id, provider,
  username, request_id}` plus the bounded profile snapshot.
- Every admin mutation writes its row synchronously, crash-on-failure, before
  the response (ADR-022 D2 ordering).
- `auth_events` has no export path today (the exporter is run-scoped). That
  retention question is a named follow-up, not this delivery.

## Settings (`WebSettings`) [rev2]

| change | field | notes |
|--------|-------|-------|
| add | `sso_client_id: str` | required unless local |
| add | `sso_client_secret: SecretStr` | required unless local; non-blank visible ASCII; never in `/config`, audit, logs, or error bodies |
| add | `sso_issuer: str` | required for `oidc`, `vanguard`; forbidden for `entra`, `google` |
| add | `sso_endpoint_origins: tuple[str, ...]` | generic `oidc` only; exact HTTPS origins the discovered endpoints may use beyond the issuer origin (Cognito hosted domain) |
| add | `sso_authorization_endpoint`, `sso_token_endpoint`, `sso_userinfo_endpoint`, `sso_jwks_uri` | break-glass, all-or-none, origin policy still applies |
| add | `sso_transaction_secret: SecretStr` | required unless local; `secret_key` strength validators apply |
| add | `google_hosted_domain: str` | required for `google`; forbidden otherwise |
| add | `sso_admin_subjects: tuple[str, ...]` | bootstrap only; seeds the first `admin` role row |
| keep | `entra_tenant_id` | required for `entra` only |
| keep | JWKS cache tuning; `token_expiry_hours`; refresh chain bound | the latter two now apply to SSO sessions via the mounted refresh route |
| change | `public_base_url` | required whenever provider is not local |
| remove | `oidc_issuer`, `oidc_audience`, `oidc_client_id`, `oidc_audience_claim`, `oidc_authorization_endpoint`, `oidc_token_endpoint`, `oidc_authorization_allowed_origins` | see Deleted |

Rotation: rolling task-definition revision; in-flight logins during the
window fail `sso_cookie_invalid` and retry cleanly. The ECS task definition
sets `sso_client_id`, `sso_client_secret` (Secrets Manager `valueFrom`),
`sso_issuer`, `sso_endpoint_origins`, `sso_transaction_secret`,
`public_base_url`. Rate limiting is per-replica-global in ECS until
`proxy_headers` / `forwarded_allow_ips` (VPC CIDR) are enabled in the launch;
enabling them is part of phase 4.

## Routes [rev2]

`/api/auth/sso/` (not mounted when provider is `local`): `GET start`,
`GET callback`, `POST complete`. `POST /api/auth/token` and
`POST /api/auth/logout` for all providers. `GET /api/auth/config` returns
`provider`, `registration_mode`, `sso_start_url`. `GET /api/auth/me` reads
the identity row for every provider.

`/api/auth/admin/` is a **new authorization path** for SSO deployments (the
existing dev-admin gate is local-only and structlog-only by design):

- Caller must hold an active `admin` role row (or be `dev_admin_user` under
  local). Membership is checked per request, never cached.
- `GET identities` (paginated, bounded, filter by `access_state`, never
  returns `raw_claims_json`), `POST identities/{id}/activate` (grants `user`
  or `manager` in the same audited write; the "tick of approval"),
  `POST identities/{id}/disable` (refused for self and for the last enabled
  admin), `POST identities/{id}/enable`.
- `GET roles`, `POST roles`, `POST roles/{id}/revoke`.
- `GET relationships` (paginated), `POST relationships`,
  `POST relationships/{id}/revoke`.
- Every mutation writes its `auth_events` row before responding.

Recovery from total admin lockout is a config change to
`sso_admin_subjects` plus restart.

## Profiles

| profile | issuer | expected origins | extra checks | username | userinfo | notes |
|---------|--------|------------------|--------------|----------|----------|-------|
| oidc (Cognito) | `sso_issuer` | issuer + `sso_endpoint_origins` | none | `preferred_username` → `cognito:username` → `sub` | no | confidential app client with the ELSPETH callback URL; new client id (Cognito secrets are fixed at creation; operator to confirm) |
| entra | derived from tenant | `login.microsoftonline.com` | `tid`; group-overage fails closed | `preferred_username` → `sub` | no | groups + `role:` roles into `groups_json` |
| vanguard | `sso_issuer` | same as issuer | none | `sub` (email today) | yes | `given_name`, `family_name`, `abn` → `organisation_id`; display name from name parts |
| google | `https://accounts.google.com` (the bare `accounts.google.com` form is rejected) | the four Google origins above | `email_verified` true; `hd` = `google_hosted_domain` (absent for non-Workspace accounts, fails closed) | `email` → `sub` | no | refuses to start without a hosted domain |

### VANguard facts measured 2026-09-02

Issuer `https://d2www26g84civw.cloudfront.net/simplesaml/module.php/oidc`;
`jwks_uri`, authorization, token, and userinfo endpoints all on the issuer
origin; `S256`; `RS256`; `token_endpoint_auth_methods_supported` =
`client_secret_post`, `client_secret_basic`, `private_key_jwt`; no
`claims_supported` published.

### VANguard spike (before the profile is written) [rev2]

Against a real confidential client and token pair, in this order:

1. Does the ID token carry any **stable, non-email subject**? (D10 hinges
   on it. If yes, the profile keys on it and `email` is a claim.)
2. Exact ID-token claim set (`nonce` presence, `aud` shape).
3. Exact userinfo body (`given_name`, `family_name`, `abn` key names/types).
4. Whether the JWKS entries carry `alg`.
5. Whether the token endpoint accepts `client_secret_basic` for that client.

The real token pair becomes a test fixture (redacted signature, pinned
claims).

### Google facts measured 2026-09-02 (by review)

Discovery: authorization `accounts.google.com/o/oauth2/v2/auth`, token
`oauth2.googleapis.com/token`, jwks `www.googleapis.com/oauth2/v3/certs`,
userinfo `openidconnect.googleapis.com/v1/userinfo`. `claims_supported`
does not list `hd`; it is emitted for Workspace accounts only. Live check
waits for a registered client.

## Refusals [rev2]

- **R1.** Refuse a self-edge in `identity_relationships`.
- **R2.** (Future) refuse to run when the approved binding differs from the
  compiled binding; see §Future seams for the binding tuple.
- **R3.** Refuse, and record `rebound_at`, a login whose `(provider, subject)`
  resolves to an existing identity while the verified email differs from
  `subject_email_at_first_seen`, until an admin re-enables the identity.
- **R4.** Refuse to complete an admin mutation whose audit write failed.
- **R5.** Refuse to disable the caller's own identity or the last enabled admin.
- **R6.** Refuse to issue a session token (at `complete` and at refresh) for
  any identity whose `access_state` is not `active`; category
  `sso_access_pending` or `sso_identity_disabled`. The login page shows
  "awaiting approval" for pending. The `login` audit row is still written.
- **R7.** Refuse a `manager` edge whose `from` identity lacks an active
  `manager` role, and any edge that would create a cycle.

## Frontend [rev2]

- `LoginPage.tsx`: the SSO button is a plain navigation to `sso_start_url`.
- Hash route `#/auth/callback`: reads `code` from the fragment, calls
  `history.replaceState` before any network call, posts to `complete`,
  stores the token where local login stores it. Error categories render the
  existing banner.
- `types/index.ts`: five-value union; `AuthConfig` loses the OIDC fields.
- Minimal admin UI: identities list with disable/enable, roles grant/revoke,
  relationships editor (the org chart), all behind the admin role.

## Testing [rev2]

- **Unit.** One fixture module per profile with a signed ID token (and the
  real VANguard pair from the spike). Every claim check positive and
  fail-closed. Registry parity. Endpoint-origin policy per profile including
  Google's cross-origin document and Cognito's hosted domain
  (`test_urls.py` carried forward). Cookie: tampered, expired, future-skewed,
  state-mismatch. Handoff: single use, expiry, disabled-between-callback-and-
  complete. Session token: provider mismatch rejected, HKDF key separation.
  Adversarial: header `alg` confusion (`HS256`, `none`, `ES256` against an
  RSA key), list `aud` without `azp`, userinfo `sub` mismatch, `start` with
  query parameters, hostile `Host` header not affecting `redirect_uri`.
- **Integration.** In-process fake IdP (discovery, JWKS, token, userinfo).
  Two concurrent `complete` calls on one code against Postgres. Mutation
  checks on guard lines.
- **Test inventory affected.** `test_oidc_provider.py` (delete; Cognito
  cases move to the profile), `test_entra_provider.py` (delete; cases move),
  `test_local_provider.py` (split), `test_urls.py` (rewrite),
  `test_provider_type_contract.py` (extend), `test_admin_routes.py`,
  `test_routes.py`, `test_config.py` (134 oidc/entra references),
  `test_app.py`, `test_web_command.py`, `tests/unit/web/secrets/
  test_user_store.py`, `LoginPage.test.tsx` (delete), `client.auth.test.ts`,
  `tests/e2e/harness/oidc-evidence*.ts`, `oidc-redacting-reporter*.ts`,
  `aws-ecs-oidc.staging.spec.ts`, `playwright.oidc.config.ts`,
  `tsconfig.oidc.json`.
- **Live.** ECS runbook §Authentication rewritten: `prepare_scenario_b_oidc`
  asserts `hasClientSecret == true`; the Playwright evidence flow drives
  start → IdP → callback → fragment code → complete and captures the ELSPETH
  token, not the IdP's; the compatibility-record example and its two pinned
  tests carry the new epochs. VANguard live once the client exists. Google
  live once a client exists.

## Rollout order [rev2]

1. **Operator, first:** register the confidential Cognito client with the
   ELSPETH callback URL and land its id and secret in the task definition;
   register the VANguard confidential client; run the VANguard spike.
2. Contracts and schema: both epochs, new tables, re-key, widened CHECKs,
   registry with parity test, readiness rewrite, fan-out checklist. Old
   browser path still intact; tests green.
3. Session token issuer extraction, profiles, SSO service, routes, admin
   path. Stage a judge bundle for the moved trust boundaries (`local.py`
   `ast_path` drift is certain; Entra overage check re-declared).
4. Frontend switch, deletion of the old path, `dist/` rebuild, AST
   no-deleted-imports assertion — one commit.
5. Runbook and Playwright harness rewrite; task definition; proxy headers.
6. Cutover: one service-stop window, both stores recreated on ECS,
   compatibility record countersigned, session-invalidation notice, Cognito
   live check.
7. VANguard live check; Google when a client exists.

## Future seams (recorded, not built)

- **Approval binding target:** `(config_hash, canonical_version,
  sha256(runtime_val_manifest_json), openrouter_catalog_sha256)`. An
  approval record will carry this tuple; the execute route at
  `web/execution/routes.py` (after ownership, before `service.execute`) is
  the gate, surfaced as a distinct row in the readiness panel.
- **Reviewer attestation:** a new event family per ADR-022 keyed on
  `identity_id` (who, when, artifact digest, verdict). The existing "Save
  for review" gesture captures no reviewer and must be renamed ("Share
  inspect link") before "Send for review" ships.
- **Shared library and personal lists:** a library entry is a named, frozen,
  content-addressed publication with deployment-wide read; the first
  shared-read surface, which must not weaken `verify_session_ownership`.
  Curator role already exists (D9). Personal lists are the existing
  per-identity session list.
- **Delegated administration:** a manager appointing a curator, or reading
  their own edges for an approver picker, is a scoped authorization check on
  top of `identity_roles` and `identity_relationships`; route-layer only.
- **Per-day token quota:** sum of LLM usage over `run_attributions` by
  `identity_id` per day; enforcement at execute. Subject scope is D11.
- **Manager audit view:** `identity_relationships` × `run_attributions` ×
  `auth_events`, needs the `identity_id` column landed here.
- **Preview row trace:** elspeth-8310d6030c, independent.
- **`auth_events` export and retention:** separate product question.

## Workflow tables (sessions store, epoch 50) — "for but not with" [rev2.1, D13]

Basic columns only. Every table keys on `identity_id`. Every mutation writes
its `auth_events` row before responding. Fleshed out later without a new
epoch only by adding nullable columns; anything needing a CHECK change is a
deliberate epoch bump.

| table | columns | notes |
|-------|---------|-------|
| approvals | approval_id PK; session_id FK; state_id; binding_json (`config_hash`, `canonical_version`, `runtime_val_manifest_sha256`, `openrouter_catalog_sha256`); requested_by_identity_id FK; approver_identity_id FK; requested_at; decided_at NULL; decision NULL CHECK `('approved','rejected','revoked')`; required_count int default 1; note NULL | One open request per `(session_id, state_id)`. Author ≠ approver (CHECK). Default approver = the author's active manager edge. Any new `state_id` supersedes the request. Execute refuses (409, distinct `error_type`) unless an `approved` row matches the compiled binding (R2). |
| review_attestations | attestation_id PK; session_id FK; state_id; payload_digest; reviewer_identity_id FK; attested_at; verdict CHECK `('signed_off','changes_requested')`; note NULL | Append-only. Two rows with distinct reviewers on one `payload_digest` = the two-person rule. Reviewer ≠ author (CHECK). |
| library_entries | entry_id PK; published_from_session_id FK; payload_digest; title; version int; published_by_identity_id FK; curated_by_identity_id NULL FK; published_at; accepted_at NULL; deprecated_at NULL; recalled_at NULL; note NULL | Frozen, content-addressed. Visible deployment-wide once `accepted_at` is set by a `curator`. Curator ≠ publisher (CHECK). Forks keep `forked_from_session_id`; recall flags, never deletes. |
| quota_policies | policy_id PK; identity_id FK; tokens_per_day int; set_by_identity_id FK; set_at; revoked_at NULL | Per person (D11). At most one active per identity (partial unique, both dialects). No deployment-wide row now; a NULL `identity_id` ceiling is the obvious later addition. |
| token_usage_ledger | entry_id PK; identity_id FK; source CHECK `('composer','run')`; session_id NULL FK; run_id NULL; model; prompt_tokens; completion_tokens; cached_prompt_tokens NULL; reasoning_tokens NULL; recorded_at | Operational accounting index, not audit truth (Landscape `calls` is). Composer writes one row per LLM call from `ComposerLLMCall.usage` (today persisted only inside JSON audit payloads, not queryable). Runs write one row per run at finalisation from the new `calls` token columns. Quota check = `SUM` over the ledger for the identity in the current UTC day, evaluated at execute and at composer turn start; over quota refuses and writes `quota_exceeded`. Accounting unavailable ⇒ refuse (fail closed). |

## Terminology

- **Container**: one ELSPETH deployment with its own sessions store and
  Landscape store. Identities, roles, and the org tree are per container.
- **Flex team**: any SSO account in the organisation can log on to any of
  the organisation's containers; what it may do is decided only by that
  container's activation, roles, and org tree. Nothing federates
  permissions across containers.
- **Nominal use case** (operator, 2026-09-02): not three people in one
  team. Around nine people cherry-picked from across the organisation who
  work on one problem space, overseen by a **matrix or functional lead**.
  The container's org tree therefore records functional oversight within
  the problem space, not corporate line management; the `manager` role and
  the `manager` edge mean "functional lead in this container". The same
  person may hold an identity in several containers, each activated,
  role-assigned, and placed in the tree independently.
- **Guiding principle — compartments** (operator, 2026-09-02): containers
  work like compartments in the intelligence world. Membership, roles,
  oversight, and quota are decided inside each compartment; being in more
  compartments never grants anything in any of them, and nothing leaks
  between them. Any proposal for a shared organisation-wide identity, role,
  or usage table "for convenience" is a violation of this principle, not a
  simplification. The only organisation-wide fact is the SSO account
  itself.
- **Organisation console (later, not now)**: the organisation-wide
  affordance is a console that manages and oversees each container
  centrally and applies oversight or organisation-wide policies. It works
  by reaching **into** each container, not by blending borders: the console
  acts as an identity holding the `admin` role in every container it
  oversees, and every policy it applies is an ordinary audited write
  through that container's own admin API (a quota policy row, an activation,
  a role grant), with the console's identity recorded as the actor. This is
  why every policy in this design is data set via an audited admin write
  and why no schema is reserved for the console: the container admin API
  *is* its interface. Cross-container reads for oversight are the console's
  problem to aggregate, container by container.
- **Consequence for the quota**: "per person" means per identity in this
  container. A person active in three containers has three independent
  daily quotas. An organisation-wide per-person ceiling would need
  cross-container aggregation, which is the later container-permissions
  territory and is not built now.

## Out of scope

The organisation console (central management, oversight, and
organisation-wide policy applied into containers; see §Terminology); deriving relationships from IdP data; approval or review enforcement;
cookie-based SPA sessions; storing IdP refresh tokens or `offline_access`;
multiple IdPs in one deployment; identity merge; RP-initiated IdP logout.

## External dependencies

A confidential VANguard client on the bridge's ABN-gated admin page with
ELSPETH's callback URL, plus a token pair for the spike. A confidential
Cognito app client. A Google Cloud OAuth client (live check only).
