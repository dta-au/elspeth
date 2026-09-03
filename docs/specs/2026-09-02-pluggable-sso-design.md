# Pluggable SSO and identity substrate — backend-for-frontend login for Entra, VANguard, Google, and generic OIDC

Date: 2026-09-02. Status: design, revision 2.7, implementation plan = tracker milestone elspeth-07cd19ba73.
Revision 2.2 applies the second review round (solution architect, systems thinker, security architect) on the operator's compartment model; items are marked **[rev2.2]**. The four operator decisions from that round (D14–D17) were ruled 2026-09-02 and applied as **[rev2.3]**. Revision 2.4 pins operator selection of the IdP profile by configuration alone, marked **[rev2.4]**. Revision 2.5 adds the per-person disk quota for uploaded blobs (D18), marked **[rev2.5]**. Revision 2.6 adds the approval and review mailbox, the round trip of request note and decision note between requester and approver, marked **[rev2.6]**. Revision 2.7 closes the four blocking defects a ten-seat panel review found on 2026-09-03 (D19 the provider discriminator, D20 the bootstrap admin, D21 the withdrawn VM in-place rebuild, and the epoch-freeze note), marked **[rev2.7]**.
Branch: `release/0.8.0`.
Revision 2 incorporates six independent reviews (security architecture,
solution design, reality check against the tree, systems risk, functional
needs, UX needs). Items marked **[rev2]** changed as a result. The raw reports
are session artefacts; the reconciled list lives in this document.

Sprint: this spec is the identity half of the "Identity and workflow
management" milestone in the tracker. Operator ruling (2026-09-02): build
90% of the final solution now and tweak on the fly, rather than a perfect
interim system that never gets permission to be replaced. So the workflow
half (approval, review attestation, shared library, per-day token quota,
per-person disk quota, the approver's audit view, delegated administration) is BUILT in the same sprint, and its tables
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
| D8 **[rev2]** | `relationship_type` | Closed CHECK + L0 Literal (`approver` only now, per D16), widened per delivery. Accepted on recommendation. |
| D9 **[rev2]** | Roles | `identity_roles` table ships now (`admin`, `curator`); `sso_admin_subjects` only seeds the first admin. Accepted on recommendation. |
| D10 **[rev2]** | Principal above identity | No `principals` table. The VANguard spike asks for a stable non-email subject; if none, detection columns plus a refusal (§Refusals R3). Identity merge is an unbuilt admin action. |
| D11 **[rev2.1]** | Operator facts, now ruled | Quota is **per person**, the aggregate of tokens used in the composer and tokens used in runs. Approval quorum is one (a count column is "for but not with"). The term is **flex teams**, not hybrid teams: anyone in the organisation can log on to any container (deployment) of that organisation, but permissions are federated within that container only. The system takes SSO accounts; a container administrator (a container-operations person, D14) grants `user`, `approver`, or `reviewer` permission and wires them into that container's org **tree**. The organisation console (§Terminology) is the later cross-container affordance and is explicitly not built now. |
| D12 **[rev2.1]** | Default access | **No access, even with SSO, until an administrator gives the tick of approval.** A first login creates an identity in `pending`; no session token is issued until an admin activates it. |
| D13 **[rev2.1]** | Workflow tables | Built "for but not with": basic columns now, fleshed out later, all in the same epoch pass. See §Workflow tables. |
| D14 **[rev2.3]** | Admin separation of duties | **(a).** `admin` is *container operations*, held by someone technical (CTO branch or similar), not a workload role. An identity holding `admin` may not hold `approver`, `reviewer`, `user`, or `curator` in the same container, and vice versa (R8). |
| D15 **[rev2.3]** | Quota numbers | `quota_default_tokens_per_day` is a **required container setting** (`WebSettings`, required unless `local`). Activation writes a `quota_policies` row with that number; an admin may override it per identity afterwards. `quota_container_tokens_per_day` is an optional ceiling. No applicable policy ⇒ refuse (only reachable through corruption, since every activated identity gets a row). |
| D16 **[rev2.3]** | Role and edge names | `approver` (was `manager`) and `reviewer`. Role `approver` may decide approvals and hold `approver` edges; the tree edge type is `approver` ("A is B's default approver"). Role `reviewer` may attest. "Manager" and "lead" appear nowhere in schema, API, or UI. |
| D17 **[rev2.3]** | IdP groups | Dropped. `groups_json` is removed; the Entra profile no longer extracts `groups`/`roles` and the group-overage check is gone with them; `UserProfile.groups` is always empty for SSO. |
| D18 **[rev2.5]** | Disk quota for uploaded blobs | Per person, a **level** not a rate: `SUM(blobs.size_bytes)` over the live blob rows of every session the identity owns (fork copies bytes, so each session's rows are real disk). `quota_default_storage_bytes` is a required container setting written into the identity's `quota_policies` row at activation, overridable per identity by an admin; `quota_container_storage_bytes` is an optional ceiling. Enforced at both upload routes under the existing per-session blob lock, before bytes are written; the existing `max_blob_storage_per_session_bytes` stays as the inner per-session bound. Over quota refuses and writes `quota_exceeded` with `dimension=storage`; accounting unavailable refuses (R13). |
| D19 **[rev2.7]** | `service` in the provider discriminator | Two L0 types. `AuthProviderType` keeps the five login values and governs settings, the registry, session tokens, `sessions`, `user_secrets` and both Landscape CHECKs. `IdentityProviderType = AuthProviderType | Literal["service"]` governs `identities.provider` alone, under its own named CHECK constant. Putting `service` in `AuthProviderType` fails the import-time parity assert and the app does not boot. **Applied on review recommendation; reversible until phase 1 lands.** |
| D20 **[rev2.7]** | Bootstrap admin | The seed and the operator CLI each write `access_state='active'`, an `admin` role, no workload role, and the audit pair in one transaction; the seed fires only while the container has zero active human admins. Activation accepts `role=none`, which is what an `admin` must be activated with under R8. The contradicting "lockout recovery is a config change" sentence is deleted. **Applied on review recommendation.** |
| D21 **[rev2.7]** | VM in-place rebuild | Withdrawn. Both deployment paths use the existing reset runbook, because the pre-1.0 gate this spec cites for ECS forbids in-place migration everywhere, and the promised byte-for-byte preservation of `user_secrets` is false across a key-derivation change. Re-admission of the known cohort moves inside the cutover window. **Applied on review recommendation; the fact it rests on is the runbook's own standing rule, not an assumption.** |

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
Adding an IdP is a deliberate edit to an L0 contract.

**`AuthProviderType` is the set of things that can authenticate a browser
[rev2.7, D19].** `service` is not one of them, so it is not a member: a
`service` identity holds an operator-issued credential and never completes an
OIDC walk, there is no `service` profile, and putting it in `AuthProviderType`
fails the parity assert above **at import**, which is a boot failure rather
than a test failure. Two L0 types, therefore:

- `AuthProviderType = Literal["local", "oidc", "entra", "vanguard", "google"]`
  — the login mechanism. Governs `WebSettings.auth_provider`, the profile
  registry, the session-token `provider` claim, `sessions` and `user_secrets`
  (a `service` identity never owns either), Landscape
  `ck_run_attributions_auth_provider_type` and `ck_auth_events_provider`.
- `IdentityProviderType = AuthProviderType | Literal["service"]` — how an
  identity row came to exist. Governs `identities.provider` alone, under its
  own named constant `_IDENTITY_PROVIDER_TYPE_CHECK`, distinct from
  `_AUTH_PROVIDER_TYPE_CHECK`.

The narrower type is a subset of the wider one, so the `sessions.user_id` →
`identities.identity_id` FK stays sound: every value `sessions` admits is a
value `identities` admits. The contract test pins **both** CHECK strings and
both Literals, and asserts the subset relation.

**Operator selection [rev2.4].** `WebSettings.auth_provider` is the only
selector: it names one registered profile, and every other `sso_*` field is
that profile's configuration for this container. The build carries no
credentials; the profile carries no deployment facts. The settings
validator rejects a value with no registered profile by naming the
registered profiles in the error, readiness reports the active profile
name and each missing `required_settings` field by name, and
`GET /api/auth/config` returns the active `provider`. Switching a container
from one IdP to another is a config change and a restart, never a build.

`EntraAuthProvider`
is deleted; its tenant check moves into the Entra profile and is re-declared
`@trust_boundary` there with its own `test_ref`. Group and role extraction,
including the group-overage fail-closed, is dropped (D17): IdP groups are
organisation facts, never compartment facts.

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
from `secret_key` with HKDF and distinct info strings for JWT signing and
user-secret encryption, so the SSO delivery does not widen the blast radius of
one 32-byte string. **Share-link signing is excluded [rev2.7]:
`shareable_link_signing_key` is its own Secrets Manager binding today, and
deriving it from `secret_key` would replace an independent secret with a
dependent one — the opposite of the goal.** Changing the user-secret key
derivation invalidates every stored secret, so it is bound to the epoch window
where both stores are recreated (§Two epochs) and is stated in the operator
notice; it must never ship in a release that keeps an existing store. Each provider supplies a
`principal_is_active(identity_id)` callback; for local that is the `auth.db`
row plus the identity row, for SSO the identity row.

`SsoAuthProvider` (one class, parameterised by profile) implements
`AuthProvider`: `authenticate` verifies the session token and confirms the
identity is enabled; `get_user_info` reads the identity row; `groups` is
always empty for SSO (D17). `POST /api/auth/token` is mounted for all providers and
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

1. `contracts/auth.py` `AuthProviderType` (hand-written) **and
   `IdentityProviderType = AuthProviderType | Literal["service"]`
   [rev2.7, D19]**.
2. `web/sessions/models.py` `_AUTH_PROVIDER_TYPE_CHECK`, used by BOTH
   `sessions` and `user_secrets` and staying at the five login values; **plus
   the new `_IDENTITY_PROVIDER_TYPE_CHECK` on `identities.provider` alone
   [rev2.7]**; `SESSION_SCHEMA_EPOCH` 49 → 50.
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
    above plus registry parity and **both** CHECK strings
    (`_AUTH_PROVIDER_TYPE_CHECK` and `_IDENTITY_PROVIDER_TYPE_CHECK`), both
    Literals, and the assertion that `get_args(AuthProviderType)` is a proper
    subset of `get_args(IdentityProviderType)` [rev2.7].

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
- **VM / SQLite:** the existing reset runbook
  ([docs/runbooks/staging-session-db-recreation.md](../runbooks/staging-session-db-recreation.md)),
  unchanged. **The in-place rebuild offered in rev2 is withdrawn [rev2.7,
  D21].** It contradicted the same pre-1.0 gate this section cites for ECS —
  the runbook's standing rule is "uninstall, archive/export when required,
  recreate, and reinstall; ELSPETH does not migrate either database in place" —
  and it promised to preserve `user_secrets` "byte-for-byte" across a delivery
  that changes the key those bytes are encrypted under (see §3), which is not
  preservation but silent corruption surfacing later as `InvalidToken`. Every
  deployment therefore lands on a fresh, empty store, and there is **one**
  cutover story to test rather than two. Users re-enter their secrets, as they
  do for every pre-1.0 epoch cut. Landscape on the VM follows the same
  archive-drop-recreate.
- **Both paths** export a `(provider, subject, pre_cutover_user_id,
  identity_id)` mapping as a named cutover artifact retained with the
  archive [rev2.2]: pre-cutover `WebPluginPolicyEvidence` hashes embed the
  old principal scope and are otherwise uninterpretable after the re-key.
- **Re-admission is part of the window, not the morning after [rev2.7, D21].**
  A recreated store has no `identities` rows, so without this step every
  returning user meets D12's `pending` wall and the only person who could
  clear it is the admin who is themselves locked out (C2 covers that half).
  Immediately after `--init-schema` the operator runs the bootstrap CLI for
  the first admin, then pre-provisions the known cohort from the mapping
  artifact by `(provider, subject)` — each row landing `active` with its
  chosen role, its `quota_policies` row written from the container defaults
  exactly as activation writes one (D15, D18), and the same audit pair.
  Anyone not in the artifact arrives `pending` and is activated normally.
  The operator notice says re-activation happened, not merely "log in again",
  and names what users must re-enter: their stored secrets.

**§Data model and §Workflow tables are living text; the epoch step is one-way
[rev2.7, C3].** Every revision of this spec since the plan was written has
moved a column, and tracker comments carrying those deltas have landed on
phase-4 steps that run *after* the epoch. Before implementing the epoch step,
re-read both sections at the spec's current revision and reconcile them
against the step's comments; the spec wins. The post-epoch relief valve at
§Workflow tables — "fleshed out later without a new epoch, by adding nullable
columns" — does **not** cover a NOT NULL column written at activation
(`quota_policies.storage_bytes`) or a rename (`approvals.note` →
`request_note`), both of which are table rewrites and must land in this
window or cost a second one with `rollback_permitted: false`.

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
| provider | text | CHECK `_IDENTITY_PROVIDER_TYPE_CHECK` = the `IdentityProviderType` values: the five IdP values (`local` included, D7) plus `service` [rev2.7, D19]. A `service` identity authenticates by an operator-issued credential, not OIDC; the mechanism is not built now. This is a **different** constant from the `_AUTH_PROVIDER_TYPE_CHECK` on `sessions`/`user_secrets`, which stays at the five login values — see §1. |
| kind | text | CHECK `('human','service')`, default `human` [rev2.2]. Service identities may hold only `admin` or `oversight`, never `approver`, `reviewer`, `user`, or `curator`, and may not approve, attest, or publish (CHECKs on the workflow tables). |
| subject | text | IdP `sub`, or local username |
| username | text | display only, non-blank; changes update the row and write an audit row |
| display_name | text null | |
| email | text null | |
| organisation_id | text null | VANguard ABN; null elsewhere |
| raw_claims_json | text null | bounded 16 KiB; declared profile keys plus `iss aud iat exp`; `groups`, `_claim_*`, `picture`, `at_hash`, `nonce` stripped; forensics only, never returned by any API. **Taken at activation, not at first sight [rev2.2]:** a `pending` row holds only provider, subject, and organisation_id, so the container does not accumulate profile PII of people who merely tried. Never-activated `pending` rows are purged by the retention sweep after a container-set window. |
| subject_email_at_first_seen | text null | D10 detection |
| rebound_at | datetime null | D10 detection: verified email changed under the same subject |
| first_seen_at | datetime | |
| last_login_at | datetime | |
| access_state | text | CHECK `('pending','active','disabled')`; default `pending` (D12). Local follows `registration_mode`: `open` activates on registration, otherwise `pending`. Read on **every request** in `get_current_user` [rev2.2], so revocation latency is one request, not the token lifetime. |
| pre_provisioned_at | datetime null | [rev2.2] An admin may create the row `active` by `(provider, subject)` before first login; first login binds instead of creating. This is how nine people are onboarded without each hitting a wall. |
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
| role | text | CHECK `('admin', 'approver', 'reviewer', 'user', 'curator', 'auditor', 'oversight')` [rev2.3]; L0 Literal `IdentityRole`. `user` = may author and run; `approver` = the functional/matrix lead: may decide approvals (role-based eligibility, see approvals) and hold `approver` edges; `reviewer` = may attest; `curator` = library gate; `admin` = container operations: identity, roles, and org-tree administration, held by someone technical, never a workload role (D14); `auditor` = read-only over the audit surfaces, no authoring, no run, all reads through `audit_access_log`; `oversight` = read plus quota-policy write, no activation, no role grant, no disable — the role the organisation console holds. Activation (D12) grants a role chosen from `user`, `approver`, `reviewer` or **`none`** [rev2.7, D20]; `admin` may never be combined with a workload role (R8), so activating an identity that holds `admin` requires `none` and the route refuses any other value. `none` is a request argument, not a stored role: it writes no `identity_roles` row. |
| expires_at | datetime null | [rev2.2] JIT grants. The console's role in a container is granted with an expiry by a *container* admin (the compartment owner reads the console in, not the reverse). |
| note | text null | [rev2.2] Reason for the grant; activation is the most consequential act in the model and must carry one. |
| scope | text null | reserved (library id, team id); null = deployment-wide |
| granted_by_identity_id | text FK | |
| granted_at | datetime | |
| revoked_at | datetime null | never deleted |

Partial unique on active `(identity_id, role, scope)` with both
`sqlite_where` and `postgresql_where` declared (dialect-symmetry contract).
**Bootstrap: the first admin activates themselves, once [rev2.7, D20].** A
role row alone is not access — D12 lands every first login in `pending` and R6
refuses a token to anything but `active` — so both bootstrap paths write the
whole state in **one audited transaction**: `access_state='active'`,
`activated_at`, `activated_by_identity_id = NULL` with actor `operator`, an
`admin` role row, **no workload role** (R8), and the `identity_activated` +
`role_granted` audit pair.

- `sso_admin_subjects` seeds a listed subject at first login **only while the
  container has zero active human admins**. Once one exists the list is inert,
  so it never becomes a standing grant and the compartment argument holds.
- The operator CLI does the same thing on demand, for recovery. It is the only
  path once an admin exists.

Lockout recovery is **not** a config edit [rev2.2]: adding a subject to a
container that already has an active admin does nothing. R5 counts only
*active human* identities with an unexpired, unrevoked `admin` role. Grant and
revoke are admin-only in this delivery; delegated administration is phase 4.

Pinned by an integration test: a fresh `--init-schema` store plus one listed
subject, walked through `start → callback → complete`, yields a session token
and a role list of exactly `admin` [rev2.7].

### `identity_relationships` (sessions store) [rev2]

| column | type | notes |
|--------|------|-------|
| relationship_id | text PK | |
| from_identity_id | text FK | |
| to_identity_id | text FK | |
| relationship_type | text | CHECK `('approver')`; L0 Literal `RelationshipType` (D8, D16) |
| asserted_by_identity_id | text FK | |
| asserted_at | datetime | |
| effective_from | datetime null | annotation only [rev2.2]: a partial-index predicate must be immutable, so "active" cannot consult the window and no check reads it. Leave cover is a second `approver` role grant, not an edge. |
| effective_until | datetime null | annotation only |
| revoked_at | datetime null | never deleted |
| revoked_by_identity_id | text null FK | |
| note | text null | |

CHECK `from_identity_id <> to_identity_id`. **Org tree (D11):** partial
unique on active `(to_identity_id, relationship_type)` so a person has at
most one active default approver, plus the partial unique on active
`(from, to, type)`; both with both dialect predicates declared. Cycles are
refused at write time by a bounded ancestor walk (route layer).
`from_identity_id` must hold an active `approver` role. Disabling an identity revokes, with the disabling
actor recorded, every active edge **incident to** it in either direction
[rev2.2], marks its open `approvals` as approver `revoked` and as requester
`revoked`, refuses any queued-not-started run it owns with an audit row,
and makes its `user_secrets` unresolvable while not `active`. The tree
carries one job: who oversees whom, for the approver's audit view. Approver
eligibility and leave cover are role questions, not tree questions.

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
  `library_recalled`, `quota_set`, `quota_exceeded`. `quota_set` and
  `quota_exceeded` carry `dimension` (`tokens` or `storage`), the cap, the
  ceiling in force, and the measured usage in `metadata_json` [rev2.5].
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
- `auth_events` gains an export path in the existing signed exporter **in
  this delivery** [rev2.2]: it is the only record of admission, role, and
  tree changes, and the record an accreditor asks for first. Every exported
  Landscape row that carries `identity_id` also carries the `(provider,
  subject, organisation_id, username)` snapshot at write time, so the export
  is self-describing without the sessions store. The per-run policy record
  (`run_web_plugin_policy`) also records the `quota_policies` row ids and the
  secret-wiring allowlist hash in force. Long-term retention remains a
  separate product question.

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
| add | `quota_default_tokens_per_day: int` | **required unless local** (D15); every activation writes a `quota_policies` row with this value, overridable per identity by an admin |
| add | `quota_container_tokens_per_day: int \| None` | optional container ceiling row (D15) |
| add | `quota_default_storage_bytes: int` | **required unless local** (D18); written into the same `quota_policies` row at activation |
| add | `quota_container_storage_bytes: int \| None` | optional container disk ceiling (D18) |
| keep | `max_upload_bytes`, `max_blob_storage_per_session_bytes` | per-file and per-session inner bounds; the identity quota is the outer bound |
| add | `compartment_id: str` | required unless local; the marking stamped into exports, library rows, and audit metadata |
| add | `identity_dormancy_days: int` | R9 window; default 90 |
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
- `GET identities` (paginated, bounded, filter by `access_state`, defaults
  to `pending` first, never returns `raw_claims_json`; for `pending` rows
  returns subject and organisation only [rev2.2]), `POST identities/{id}/
  activate` (grants `user`, `approver`, or `reviewer` and takes the profile snapshot in
  the same audited write, with a required `note`; the "tick of approval"),
  `POST identities` (pre-provision by `(provider, subject)`, [rev2.2]),
  `POST identities/{id}/disable` (refused for self and for the last active
  human admin), `POST identities/{id}/enable`.
- Every mutation accepts `on_behalf_of` and `console_request_id` only from
  a `service` identity and records them in `auth_events.metadata_json`
  (keys pinned in the L0 metadata contract now, because rows written
  without them are permanently anonymous) [rev2.2].
- The admin UI shows an advisory when the container has exactly one active
  human admin [rev2.2].
- A container admin can revoke a `service` identity's role in their own
  container, and R5 never protects a service identity; both pinned by
  tests, because container sovereignty is the property that makes the
  console pattern acceptable [rev2.2].
- `GET roles`, `POST roles`, `POST roles/{id}/revoke`.
- `GET relationships` (paginated), `POST relationships`,
  `POST relationships/{id}/revoke`.
- Every mutation writes its `auth_events` row before responding.

`POST identities/{id}/activate` takes `role ∈ {user, approver, reviewer,
none}` and refuses a workload role for an identity holding `admin` (R8)
[rev2.7, D20].

Recovery from total admin lockout is the operator CLI described under
§`identity_roles`, never a config edit [rev2.7 corrects rev2.2's contradiction:
this paragraph previously said the opposite of the paragraph that ruled it].

## Profiles

| profile | issuer | expected origins | extra checks | username | userinfo | notes |
|---------|--------|------------------|--------------|----------|----------|-------|
| oidc (Cognito) | `sso_issuer` | issuer + `sso_endpoint_origins` | none | `preferred_username` → `cognito:username` → `sub` | no | confidential app client with the ELSPETH callback URL; new client id (Cognito secrets are fixed at creation; operator to confirm) |
| entra | derived from tenant | `login.microsoftonline.com` | `tid` | `preferred_username` → `sub` | no | groups and roles are not collected (D17); the group-overage check is gone with them |
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
- **R2.** Refuse to run when no `approved` row matches the compiled binding
  tuple, or the matching row is `superseded` or `revoked`. **Delivered in
  this sprint** (phase 4), not future [rev2.2]; the "Send for approval"
  affordance must not exist before this refusal does.
- **R3.** Refuse, and record `rebound_at`, a login whose `(provider, subject)`
  resolves to an existing identity while the verified email differs from
  `subject_email_at_first_seen`, until an admin re-enables the identity.
- **R4.** Refuse to complete an admin mutation whose audit write failed.
- **R5.** Refuse to disable the caller's own identity or the last enabled admin.
- **R6.** Refuse to issue a session token (at `complete` and at refresh) for
  any identity whose `access_state` is not `active`; category
  `sso_access_pending` or `sso_identity_disabled`. The login page shows
  "awaiting approval" for pending. The `login` audit row is still written.
- **R7.** Refuse an `approver` edge whose `from` identity lacks an active
  `approver` role, and any edge that would create a cycle.
- **R8.** [rev2.3] Refuse any grant that would make one identity hold
  `admin` together with `approver`, `reviewer`, `user`, or `curator` in the
  same container (D14). Admin is container operations, not a workload role.
- **R9.** [rev2.2] Refuse a login for an identity dormant longer than the
  container's dormancy window: it drops to `pending` with
  `disable_reason='dormant'`, writes `identity_disabled`, and needs an admin
  to re-activate. This is the recycled-mailbox case R3 cannot see because
  the email did not change.
- **R10.** [rev2.2] Refuse a service identity's admin write that lacks
  `on_behalf_of` and `console_request_id`; refuse those keys from a human
  identity.
- **R11.** [rev2.2] Refuse to enable approval, attestation, and library
  enforcement when `provider=local` and `registration_mode=open`, because
  one human can then hold many identities and every ≠ CHECK is defeatable.
- **R12.** [rev2.2] Refuse to resolve a shareable review token for a
  requesting identity that is not `active`.
- **R13.** [rev2.5] Refuse an upload (multipart or inline) whose bytes
  would take the identity's live blob total over its `storage_bytes` or the
  container ceiling, before any byte reaches disk; write `quota_exceeded`
  with `dimension=storage`. Refuse when the accounting query fails. The
  response names the cap and the current usage so the user can delete
  blobs to recover; the existing per-session error keeps its shape.

## Frontend [rev2]

- `LoginPage.tsx`: the SSO button is a plain navigation to `sso_start_url`.
- Hash route `#/auth/callback`: reads `code` from the fragment, calls
  `history.replaceState` before any network call, posts to `complete`,
  stores the token where local login stores it. Error categories render the
  existing banner.
- `types/index.ts`: five-value union; `AuthConfig` loses the OIDC fields.
- Minimal admin UI: identities list with disable/enable, roles grant/revoke,
  relationships editor (the org chart), all behind the admin role. The
  identity row shows both quotas (tokens per day, storage bytes) with
  current usage, and one editor sets either [rev2.5].
- Upload surfaces render the storage refusal with cap and usage, and the
  blob list shows the identity's total so deletion is discoverable
  [rev2.5].
- **Mailbox [rev2.6]:** one surface, two folders, mounted for every
  active identity. *Inbox*: approvals awaiting my decision (addressed to
  me first, then any other open request I am eligible to decide, since
  eligibility is role-based) and review requests addressed to me, each
  opening the frozen read-only inspect view with the requester's note and
  approve/reject (or sign off/changes requested) plus a note field.
  *Sent*: my own requests with their state, the decider, the decision
  note, and when; opening one sets `decision_seen_at`. A badge on the
  navigation shows unread counts from one summary endpoint polled on the
  existing session-list cadence. Notification stops at the badge: there is
  no email or push transport in this sprint (a seam, see §Future seams).

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

- **Approval** is delivered in this sprint (§Workflow tables, R2). The
  execute route at `web/execution/routes.py` (after ownership, before
  `service.execute`) is the gate, surfaced as a distinct row in the
  readiness panel.
- **Shareable review tokens [rev2.2]:** 30-day bearer capabilities with no
  per-token revocation (only key rotation). They are compartment-consistent
  (the holder must be an active identity on the same deployment, R12) but
  the lifetime is a stated property, and the default should be shortened
  for SSO deployments.
- **Reviewer attestation:** a new event family per ADR-022 keyed on
  `identity_id` (who, when, artifact digest, verdict). The existing "Save
  for review" gesture captures no reviewer and must be renamed ("Share
  inspect link") before "Send for review" ships.
- **Shared library and personal lists:** a library entry is a named, frozen,
  content-addressed publication with deployment-wide read; the first
  shared-read surface, which must not weaken `verify_session_ownership`.
  Curator role already exists (D9). Personal lists are the existing
  per-identity session list.
- **Delegated administration:** an approver appointing a curator, or
  reading their own edges for an approver picker, is a scoped authorization check on
  top of `identity_roles` and `identity_relationships`; route-layer only.
- **Per-day token quota:** sum of LLM usage over `run_attributions` by
  `identity_id` per day; enforcement at execute. Subject scope is D11.
- **Disk quota:** `SUM(blobs.size_bytes)` per identity, enforced at upload
  (D18, R13).
- **Notification transport [rev2.6]:** the mailbox is in-app only. Email
  or chat delivery is a later adapter over the same summary query; the
  addresses exist on the identity row already.
- **Approver's audit view:** `identity_relationships` × `run_attributions`
  × `auth_events`, scoped to the caller's active `approver` edges.
- **Preview row trace:** elspeth-8310d6030c, independent.
- **`auth_events` export and retention:** separate product question.

## Workflow tables (sessions store, epoch 50) — "for but not with" [rev2.1, D13]

Basic columns only. Every table keys on `identity_id`. Every mutation writes
its `auth_events` row before responding. Fleshed out later without a new
epoch only by adding nullable columns; anything needing a CHECK change is a
deliberate epoch bump.

| table | columns | notes |
|-------|---------|-------|
| approvals | approval_id PK; session_id FK; state_id; binding_json (`config_hash`, `canonical_version`, `runtime_val_manifest_sha256`, `openrouter_catalog_sha256`, **`binding_generation_fingerprint`, `policy_hash`** [rev2.2]); requested_by_identity_id FK; approver_identity_id FK; requested_at; decided_at NULL; decision NULL CHECK `('approved','rejected','revoked','superseded')` [rev2.2]; required_count int default 1; request_note NULL [rev2.6]; decision_seen_at NULL [rev2.6] | One open request per `(session_id, state_id)`. **Mailbox [rev2.6]:** `request_note` is the requester's message to the approver ("please approve, it's for entirely legitimate business"); the approver's reply travels in `approval_decisions.note`. Both are bounded plain text (4 KiB), rendered as text, never as markup, and both are part of the audit record. `decision_seen_at` is set when the requester opens the decided request, so the badge can clear; it is a UI convenience, never a control. Author ≠ approver (CHECK). **Approver eligibility is role-based [rev2.2]:** any identity holding an active `approver` role in this container who is not the author may decide; the author's active `approver` edge only supplies the default suggestion in the picker. This is what gives the lead's own work an approver and gives leave cover without touching the tree. Any new `state_id` marks the open request `superseded`. Execute refuses (409, distinct `error_type`) unless an `approved` row matches the compiled binding (R2, **delivered in this sprint**, phase 4). `binding_generation_fingerprint` is included because `config_hash` records profile aliases, not the buckets or credentials they resolve to; without it an approval survives an operator repointing an alias. `snapshot_hash` is deliberately excluded (it embeds the principal scope and would never match across approver and author). Pinned by a test that fails if a new field enters `WebPluginPolicyEvidence` without a tuple decision. |
| approval_decisions | decision_id PK; approval_id FK; decided_by_identity_id FK; decided_at; decision CHECK `('approved','rejected')`; note NULL | [rev2.2] One row per deciding identity, so `required_count > 1` is a count over rows, not a schema change. Dual control: nullable `quota_policies.dual_control_above_tokens` and a per-container list of secret names / plugins whose wiring raises `required_count` to 2 (reserved, not enforced). |
| review_attestations | attestation_id PK; session_id FK; state_id; payload_digest; reviewer_identity_id FK; attested_at; verdict CHECK `('signed_off','changes_requested','withdrawn')` [rev2.2]; note NULL | Append-only. Reviewer ≠ author (CHECK); the reviewer must hold an active `reviewer` role [rev2.3]. **Named "reviewer attestations" everywhere — schema, API, UI [rev2.2].** It is a ledger, not a control: nothing refuses on it. The phrase "two-person rule" is reserved for something that refuses; a UI must never say "two-person rule satisfied" over an unenforced count. |
| library_entries | entry_id PK; published_from_session_id FK; payload_digest; compartment_id; title; version int; published_by_identity_id FK; curated_by_identity_id NULL FK; published_at; accepted_at NULL; rejected_at NULL; rejection_note NULL; deprecated_at NULL; recalled_at NULL; note NULL | Frozen, content-addressed. **A library entry is the public projection (`generate_public_yaml` shape), never a session reference, and it is config-only [rev2.2]:** publishing a pipeline that reads an uploaded blob is refused with a named `error_type` ("publish a profile-bound source instead"), because blob custody proves same-principal on fork and a cross-user fork of a blob-backed source cannot copy the blob without becoming an intra-container exfiltration path. Forking a library entry instantiates the projection into the forker's own staging session; `forked_from_session_id` points at that staging session and the entry's `payload_digest` carries provenance. Visible deployment-wide once `accepted_at` is set by a `curator`. Curator ≠ publisher (CHECK). Recall flags, never deletes. `library_published` audit rows carry `payload_digest` and `compartment_id` so the same artifact appearing in two containers is detectable later. |
| quota_policies | policy_id PK; identity_id NULL FK; tokens_per_day int; storage_bytes int [rev2.5, D18]; dual_control_above_tokens NULL int; set_by_identity_id FK; set_at; revoked_at NULL | Per person (D11) **plus the container ceiling row (`identity_id` NULL) shipped now [rev2.2]**, because activation otherwise grants unbounded spend on the container's shared LLM credential. Two partial uniques, both dialects: active per identity, and active `WHERE identity_id IS NULL` (NULLs are distinct for uniqueness in Postgres, so one predicate does not cover both). Activation writes the per-identity row from `quota_default_tokens_per_day` (D15), so no applicable policy is reachable only through corruption and refuses. Every `quota_set` / `quota_exceeded` event records the cap and the ceiling in force. **Storage [rev2.5]:** `storage_bytes` is a standing level, not a daily rate; usage is `SUM(blobs.size_bytes)` joined through `sessions.identity_id` over live rows (deleted blobs leave no row), evaluated inside the upload's existing session blob lock so two concurrent uploads cannot both pass. Blobs created by the system on the identity's behalf (inline custody, fork copies) count against the identity; the `system` exemption applies to tokens only. Admin set/revoke is one route for both dimensions. |
| token_usage_ledger | entry_id PK; identity_id NULL FK; source CHECK `('composer','run','auto_title','system')` [rev2.2]; session_id NULL FK; run_id NULL; model; prompt_tokens; completion_tokens; cached_prompt_tokens NULL; reasoning_tokens NULL; recorded_at | Operational accounting index, not audit truth (Landscape `calls` is). Composer writes one row per LLM call from `ComposerLLMCall.usage` (today persisted only inside JSON audit payloads, not queryable). Auto-titling (a paid background call per first message, which its own docstring flags as bypassing rate limits) writes `auto_title`; the boot probe writes `system` with `identity_id` NULL. Runs write one row per run at finalisation from the new `calls` token columns. Quota check = `SUM` over the ledger for the identity in the current UTC day, evaluated at execute and at composer turn start only; post-response spend lands in the next window. Over quota refuses and writes `quota_exceeded`. Accounting unavailable ⇒ refuse (fail closed); the `system` arm is exempt from the check. |

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
  the problem space, not corporate line management; the `approver` role and
  the `approver` edge mean "functional lead in this container" (D16: the
  words manager and lead appear nowhere in schema, API, or UI). The same
  person may hold an identity in several containers, each activated,
  role-assigned, and placed in the tree independently.
- **Guiding principle — compartments** (operator, 2026-09-02): containers
  work like compartments in the intelligence world. Membership, roles,
  oversight, and quota are decided inside each compartment; being in more
  compartments never grants anything in any of them. Any proposal for a
  shared organisation-wide identity, role, or usage table "for convenience"
  is a violation of this principle, not a simplification. The only
  organisation-wide fact is the SSO account itself.
- **What the system can and cannot promise [rev2.2].** Permissions never
  federate; that is enforced. Content carried by a person who is
  legitimately in two compartments cannot be prevented by any technical
  control (public YAML export, a downloaded output re-uploaded, a library
  entry re-authored elsewhere); the intelligence analogy handles this by
  *marking* material and *recording* its movement, not by pretending it
  cannot move. So: every container has an operator-set `compartment_id`
  (WebSettings) stamped into the public YAML metadata block, the shareable
  snapshot, every `library_entries` row, every `auth_events.metadata_json`,
  and every signed Landscape export; egress is already recorded
  (`export_yaml`); ingress is recorded as a Tier-1 event when a composition
  state is created from user-pasted text, carrying the sha256 of the text
  and any foreign marking it contains (recording, which the composer
  invariants permit; never authoring). Membership discipline, fewer people
  in fewer compartments, is the actual control.
- **Data plane [rev2.2].** Compartmentation is enforced over identity and
  everything keyed on it (sessions, user secrets, blobs, outputs, audit),
  and only *inferred* over data. Nothing stops an operator configuring the
  same bucket, database, or server-secret name in two containers, and each
  container's audit trail would then tell a clean, complete story of its
  own half. Non-overlap of operator profiles across containers is an
  operator obligation, not a system guarantee; `binding_generation_
  fingerprint` stays retrievable per container so a console can diff
  bindings across containers later. Within a container every activated
  `user` sees the same profiles, server secrets, and LLM credential: that is
  what a compartment means, and there are no need-to-know sub-tiers inside
  one. Activation is therefore a *data-access and spend* decision, not a
  login decision.
- **Organisation console (later, not now)**: the organisation-wide
  affordance is a console that manages and oversees each container
  centrally and applies oversight or organisation-wide policies. It works
  by reaching **into** each container, not by blending borders: every
  policy it applies is an ordinary audited write through that container's
  own admin API. **Its identity is not "admin everywhere" [rev2.2].** The
  console is a `service` identity holding the `oversight` role (read plus
  quota-policy write) granted with an expiry by each container's own admin;
  it never holds `admin` as a standing state; every write it makes carries
  `on_behalf_of` (the human SSO subject who drove it) and
  `console_request_id`; and a container admin can revoke it. Cross-container
  oversight reads are the console's to aggregate, container by container,
  and the evidentiary artefact is always the per-container signed export,
  never the console's view. The one cross-container primitive the console
  will need, "which SSO subjects hold active identities in more than one
  container", is a read over `(provider, subject)` across stores; there is
  no `principals` table (D10), so this is named here as a known tension for
  whoever builds it.
- **Consequence for the quota**: "per person" means per identity in this
  container. A person active in three containers has three independent
  daily quotas. An organisation-wide per-person ceiling would need
  cross-container aggregation, which is the later container-permissions
  territory and is not built now.

## Out of scope

The organisation console (central management, oversight, and
organisation-wide policy applied into containers; see §Terminology); deriving relationships from IdP data; review enforcement (attestation stays a ledger; approval enforcement R2 IS in scope); email or push notification of mailbox items;
cookie-based SPA sessions; storing IdP refresh tokens or `offline_access`;
multiple IdPs in one deployment; identity merge; RP-initiated IdP logout.

## External dependencies

A confidential VANguard client on the bridge's ABN-gated admin page with
ELSPETH's callback URL, plus a token pair for the spike. A confidential
Cognito app client. A Google Cloud OAuth client (live check only).
