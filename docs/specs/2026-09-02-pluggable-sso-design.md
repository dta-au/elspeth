# Pluggable SSO — backend-for-frontend login for Entra, VANguard, Google, and generic OIDC

Date: 2026-09-02. Status: design, pending implementation plan.
Branch: `release/0.8.0`.

## Problem

ELSPETH's web layer authenticates against one identity provider per
deployment, chosen by `WebSettings.auth_provider` from a closed set of
`local`, `oidc`, and `entra`. Adding VANguard (the Australian Government
SSO, reached through the DTA-owned SimpleSAMLphp OIDC bridge) and Google
exposed three limits of the current design:

1. **Pluggability stops at the class.** `EntraAuthProvider` is a 141-line
   wrapper over `JWKSTokenValidator`; Cognito is a mode flag on the same
   class. Every IdP is a new class plus edits to eleven closed-set sites
   (contract Literal, three database CHECK constraints, a frozenset,
   readiness, the app factory, the config route, the frontend union, and
   a contract test).
2. **The browser holds and presents IdP tokens.** `LoginPage.tsx` runs
   authorization-code + PKCE itself and stores the IdP's `access_token`
   as the Bearer for every ELSPETH request. That cannot work for Google,
   whose access tokens are opaque, and it means VANguard's profile
   claims (which the bridge only serves from `userinfo`) would need a
   provider call on every `/me`.
3. **Identity has no home.** SSO users exist only as claims inside a
   token. Nothing records who has logged in, with what profile, or how
   staff relate to one another.

The operator ruling (2026-09-02) was to pay this now rather than when an
auditor asks: per-IdP persisted discriminators, backend-owned code
exchange, an identity record, and a manually curated relationship record.

## Decisions

| # | Decision | Ruling |
|---|----------|--------|
| D1 | Persisted provider discriminator | Per-IdP values: `local`, `oidc`, `entra`, `vanguard`, `google`. Not a family + preset. |
| D2 | Who exchanges the authorization code | The backend, as a confidential client. Browser-side PKCE is deleted, not kept as a fallback. |
| D3 | Where an SSO profile lives | An `identities` table in the web state database. The session JWT stays minimal. |
| D4 | Staff relationships | Directed typed edges in an `identity_relationships` table, curated manually via admin routes. No derivation from IdP telemetry, no approval enforcement. |
| D5 | Delivery scope | Framework + all four profiles + Cognito confidential-client migration + old path deleted. VANguard and Cognito verified live; Google verified live when a client exists. |

## Architecture

Four units replace the three provider classes and the browser exchange.

### 1. IdP profile registry — `src/elspeth/web/auth/providers/`

One frozen dataclass per IdP. A profile declares:

- `name`: the `AuthProviderType` value it registers.
- `resolve_issuer(settings) -> str`: from `sso_issuer` (oidc, vanguard),
  derived from `entra_tenant_id` (entra), fixed (google).
- `scopes`: what to request. All four use `openid profile email`.
- `claim_checks(payload)`: fail-closed checks beyond standard validation
  (Entra `tid`; Google `email_verified` and `hd`; none for oidc and
  vanguard).
- `map_identity(id_claims, userinfo) -> IdentityClaims`: username,
  display name, email, groups, organisation id.
- `userinfo: bool`: whether the callback calls the userinfo endpoint.

The closed Literal `AuthProviderType` in `contracts/auth.py` is derived from
the registry's names; `tests/unit/web/auth/test_provider_type_contract.py`
continues to pin the tuple exactly, and gains an assertion that the three
CHECK strings, the run-lifecycle frozenset, and the frontend union carry the
same five values. `EntraAuthProvider` is deleted; its tenant check and
group/role extraction (including the group-overage fail-closed) move into
the Entra profile unchanged.

### 2. SSO login service — `src/elspeth/web/auth/sso.py`

The backend owns the whole authorization-code flow.

- **start**: generate PKCE verifier, `state`, and `nonce`; seal them in an
  encrypted, HttpOnly, `SameSite=Lax` cookie scoped to the callback path
  with a five-minute lifetime; redirect to the profile's authorization
  endpoint with `client_id`, S256 challenge, `state`, `nonce`, scopes, and
  the callback URL built from `public_base_url`. Endpoints come from
  discovery at startup exactly as today; the same-origin JWKS policy in
  `JWKSTokenValidator._validate_jwks_uri_policy` is unchanged.
- **callback**: read and clear the cookie; reject on missing cookie,
  `state` mismatch, or an IdP `error` parameter; redeem the code at the
  token endpoint with `client_secret_basic` under the existing httpx
  timeout; validate the ID token via `JWKSTokenValidator` with audience =
  client id and `nonce` = the cookie's; run the profile's claim checks;
  call userinfo with the access token when the profile requires it;
  discard the access and refresh tokens (ELSPETH never stores IdP
  tokens); upsert the identity; write an `auth_events` `login` row;
  mint a session token; store it against a random single-use handoff
  code (30-second lifetime, sessions store); redirect to the SPA callback
  page with only the handoff code in the query.
- **complete**: exchange the handoff code for the session token in a JSON
  body of the same shape local login returns. First use consumes it.

The cookie is stateless, so the flow works across ECS replicas without
shared transaction state. Failures redirect to the login page with an
opaque error category, never IdP text, and every failure writes an
`auth_failure` event with that category. Discovery or JWKS outage during
callback maps to the existing 503 `AuthProviderUnavailable` path.

### 3. Session token issuer — `src/elspeth/web/auth/session_token.py`

The HS256 minting (`sub`, `username`, `iat`, `exp`), verification, and the
bounded refresh chain currently inside `LocalAuthProvider` (`_issue_token`,
`_refresh_sync`, `authenticate`) are extracted into one shared unit. Local
login and SSO login both end here. `token_expiry_hours` and the refresh
chain bound apply to SSO sessions too.

An `SsoAuthProvider` (one class, parameterised by profile) implements the
existing `AuthProvider` protocol: `authenticate` verifies the session token
and confirms the identity record exists and is not disabled;
`get_user_info` reads the record.

### 4. Identity persistence — sessions store

Two new tables; see Data model.

### Deleted outright

- Browser-side PKCE in `LoginPage.tsx`, its transaction storage, and tests.
- The CSP `connect-src` token-origin exception in
  `_BrowserDocumentHeadersMiddleware`.
- `oidc_authorization_allowed_origins` and both validators in `auth/urls.py`
  that exist only for it.
- Cognito access-token mode (`oidc_audience_claim`,
  `_validate_cognito_access_claims`).
- `EntraAuthProvider`.
- `OIDCAuthProvider` as a bearer validator of IdP tokens.

## Data model

### Discriminator widening

`local | oidc | entra | vanguard | google` in:

- `contracts/auth.py` `AuthProviderType` (derived from the registry).
- `web/sessions/models.py` `_AUTH_PROVIDER_TYPE_CHECK`; `SESSION_SCHEMA_EPOCH`
  49 → 50.
- `core/landscape/schema.py` `ck_run_attributions_auth_provider_type` and
  `ck_auth_events_provider`; `core/landscape/database.py` constraint list
  updated to match.
- `core/landscape/run_lifecycle_repository.py` `_AUTH_PROVIDER_TYPES`.
- `web/frontend/src/types/index.ts` provider union.

Both stores are pre-release epoch designs with no migration framework: the
sessions store refuses to open on a mismatched epoch. Cutover for the
deployed sessions table is the in-place rebuild (SQLite cannot alter a
CHECK); Landscape follows its existing cutover-guide pattern.

### `identities` (sessions store)

| column | type | notes |
|--------|------|-------|
| identity_id | text PK | surrogate; the key relationships reference |
| provider | text | CHECK in the five values, never `local` |
| subject | text | IdP `sub` |
| username | text | non-blank |
| display_name | text null | |
| email | text null | |
| organisation_id | text null | VANguard ABN; null elsewhere |
| raw_claims_json | text | verified ID-token claims merged with userinfo, as received |
| first_seen_at | datetime | |
| last_login_at | datetime | |
| disabled_at | datetime null | set by admin; blocks `authenticate` |

Unique `(provider, subject)`. Local users are not rows here; they stay in
`auth.db`.

### `identity_relationships` (sessions store)

| column | type | notes |
|--------|------|-------|
| relationship_id | text PK | |
| from_identity_id | text FK | |
| to_identity_id | text FK | |
| relationship_type | text | short free string; `manager` is the first |
| asserted_by_identity_id | text FK | who created the link |
| asserted_at | datetime | |
| revoked_at | datetime null | rows are revoked, never deleted |
| note | text null | |

Unique on the active `(from, to, type)` triple. Nothing consumes these rows
yet; the table exists so that history starts now and a future approval
workflow references stable ELSPETH identities rather than raw IdP subjects.

### `auth_events`

No new columns. A login writes `event_type = login` with the profile
snapshot and `identity_id` in `metadata_json`. Failures write
`auth_failure` with the opaque category.

## Settings (`WebSettings`)

| change | field | notes |
|--------|-------|-------|
| add | `sso_client_id: str` | required unless local |
| add | `sso_client_secret: SecretStr` | required unless local; delivered like `operator_metrics_bearer_token` |
| add | `sso_issuer: str` | required for `oidc` and `vanguard`; forbidden for `entra` and `google` (derived/fixed) |
| add | `sso_transaction_secret: SecretStr` | encrypts the PKCE cookie; required unless local |
| add | `google_hosted_domain: str` | required for `google`; forbidden otherwise |
| add | `sso_admin_subjects: tuple[str, ...]` | IdP subjects granted the admin surface; forbidden for local (which keeps `dev_admin_user`) |
| keep | `entra_tenant_id` | required for `entra` only |
| keep | JWKS cache tuning | unchanged |
| keep | `token_expiry_hours`, refresh chain bound | now apply to SSO sessions |
| change | `public_base_url` | required whenever provider is not local (callback URL) |
| remove | `oidc_issuer`, `oidc_audience`, `oidc_client_id` | replaced by `sso_*`; audience is always the client id |
| remove | `oidc_audience_claim` | Cognito access-token mode is gone |
| remove | `oidc_authorization_endpoint`, `oidc_token_endpoint` | always from discovery |
| remove | `oidc_authorization_allowed_origins` | browser no longer contacts the IdP token endpoint |

Environment names keep the `ELSPETH_WEB__` prefix. `_validate_auth_fields`
enforces the per-provider required/forbidden matrix above. The
`elspeth web --auth` CLI option accepts the five values.

## Routes

`/api/auth/sso/` (not mounted when provider is `local`):

- `GET start` → 302 to the IdP.
- `GET callback` → 302 to the SPA callback page with `?code=<handoff>`.
- `POST complete` → `{access_token, token_type}`; consumes the handoff code.

`GET /api/auth/config` returns `provider`, `registration_mode`, and
`sso_start_url`. The OIDC fields are dropped from `AuthConfigResponse`.

`GET /api/auth/me` for SSO users reads the identity record.

`/api/auth/admin/` (existing gate, extended: for local the admin is
`dev_admin_user` as today; for SSO providers the admin is any identity whose
`subject` is listed in `sso_admin_subjects`. Same gate, one more membership
check, no new authorisation model):

- `GET identities`, `POST identities/{id}/disable`, `POST identities/{id}/enable`.
- `GET relationships`, `POST relationships`, `POST relationships/{id}/revoke`.

## Profiles

| profile | issuer | extra checks | username | userinfo | notes |
|---------|--------|--------------|----------|----------|-------|
| oidc (Cognito) | `sso_issuer` | none | `preferred_username` → `cognito:username` → `sub` | no | Cognito app client re-registered as confidential with the ELSPETH callback URL |
| entra | derived from tenant | `tid` = tenant; group-overage marker fails closed | `preferred_username` → `sub` | no | groups + `role:`-prefixed roles as today |
| vanguard | `sso_issuer` | none | `sub` (bridge sets `sub` = email); email = `sub` | yes | `given_name`, `family_name`, `abn` from userinfo; `abn` → `organisation_id`; display name built from the name parts |
| google | `https://accounts.google.com` | `email_verified` is true; `hd` = `google_hosted_domain` | `email` → `sub` | no | refuses to start without a hosted domain, so personal accounts can never log in by accident |

### VANguard facts measured 2026-09-02

Discovery document fetched from the bridge:

- issuer `https://d2www26g84civw.cloudfront.net/simplesaml/module.php/oidc`;
  `jwks_uri` on the same origin (passes the existing same-origin policy).
- `code_challenge_methods_supported` includes `S256`.
- `userinfo_endpoint` present; `id_token_signing_alg_values_supported` is `RS256`.
- `token_endpoint_auth_methods_supported` lists `client_secret_post`,
  `client_secret_basic`, `private_key_jwt`. `client_secret_basic` is used.
- No `claims_supported` published.

### VANguard facts still to measure (spike, before the profile is written)

Against a real confidential client and token pair:

- Which claims the ID token carries (`sub`, `email`, `nonce`, anything else).
- The exact userinfo response body (`given_name`, `family_name`, `abn`
  key names and types).
- Whether the token endpoint accepts `client_secret_basic` for that client.

Findings are recorded in this section as measured facts, not assumed.

## Frontend

- `LoginPage.tsx`: the SSO button becomes a plain navigation to
  `sso_start_url`. All PKCE transaction code and its tests are removed.
- New `/auth/callback` route: posts the handoff code to `complete`, stores
  the token where local login stores it, then navigates home. Errors from
  the query's opaque category render the existing banner.
- `types/index.ts`: provider union carries five values; `AuthConfig` loses
  the OIDC fields.

## Testing

- **Unit.** One fixture module per profile with a signed ID token and, for
  VANguard, a userinfo body. Every claim check has a positive and a
  fail-closed case. Registry parity test (Literal, three CHECK strings,
  frozenset, frontend union). Handoff code: single use and expiry. Cookie:
  tampered, expired, and state-mismatch transactions are rejected before
  any network call. Session-token issuer tests move with the code from
  `test_local_provider.py`.
- **Integration.** A fake IdP served in-process implementing discovery,
  JWKS, token, and userinfo, so the whole callback path runs without a real
  provider. Mutation checks on the guard lines, per project practice.
- **Live.** The ECS acceptance runbook's OIDC scenario is rewritten for the
  confidential Cognito client. VANguard is checked live once the client is
  registered. Google is checked live once a client exists.

## Rollout order

1. Schema (epoch 50, new tables, widened constraints) and the profile
   registry land with the old browser path intact and tests green.
2. Session token issuer extraction, SSO service, and routes.
3. Frontend switch and deletion of the old path in one commit, so there is
   never a moment with two live login designs.
4. Cognito client re-registered as confidential; ECS runbook and task
   definition updated; acceptance scenario re-run.
5. VANguard live check.

The VANguard spike (measure the token and userinfo shapes) happens before
step 2's profile is written.

## Out of scope

- Deriving relationships from IdP data (Entra `manager` via Graph, or any
  VANguard signal). VANguard emits none; Entra would need Graph permissions
  the app does not hold.
- Any approval or authorisation behaviour based on relationships.
- Cookie-based session auth for the SPA; the localStorage Bearer model is
  unchanged.
- Storing IdP refresh tokens or requesting `offline_access`.
- Multiple IdPs in one deployment.

## External dependencies

- A confidential VANguard client registered on the bridge's ABN-gated
  admin page with ELSPETH's callback URL, plus a token pair for the spike.
- A confidential Cognito app client for the ECS deployment.
- A Google Cloud OAuth client (for the live check only).
