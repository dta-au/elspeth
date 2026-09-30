# 06 — Identity and access

**Status:** product control baseline complete; deployment records open ·
**Reviewed against:** `release/0.8.1` @ `49c1845085d36811b120ef1c540048463e32aabc`
(2026-09-30) · **Owner:** ELSPETH maintainer

Describes how users are identified, authenticated and authorised, and how
their access is granted, reviewed and removed. It covers the web application.
The command-line tool and the MCP servers do not authenticate their users;
access to them is controlled by the host they run on (§ 5.3).

Facts about the product were checked against the tree at the commit above,
by reading the code and, where stated, by running it. Sections marked
**Deployment record** are completed in the controlled copy for each
deployment, not in this public file.

Existing material:

- [guarantees.md § 11 Authentication and identity](../release/guarantees.md#11-authentication-and-identity-rc-52)
  and [§ 13 Multi-user session](../release/guarantees.md#13-multi-user-session-rc-52)
- [Platform Architecture § Security-Relevant Design Choices](../release/platform-architecture.md#security-relevant-design-choices)
- [Identity workflow cutover runbook](../runbooks/identity-workflow-cutover.md)
- Source: `src/elspeth/web/auth/`, `src/elspeth/web/coordination/identity_authority.py`

The sign-in and API flows themselves are F1–F4 and F15 in
[02 § 3](02-data-flows-and-trust-boundaries.md#3-product-flows); this document
describes the identity and access controls on those flows.

## 1. Authentication

### 1.1 Providers

A deployment enables exactly one provider (`auth_provider`). The supported
set, measured with `registered_provider_names()` from
`elspeth.web.auth.providers`, is `entra`, `google`, `local`, `oidc` and
`vanguard`. The registry checks at import that it matches the
`AuthProviderType` contract, so a provider cannot be half-added
(`src/elspeth/web/auth/providers/__init__.py`) [EV-101].

| Provider | Supported | What ELSPETH checks beyond standard ID-token validation (§ 1.2) | Enabled in this deployment | MFA enforced by | Notes |
|---|---|---|---|---|---|
| Local accounts (`local`) | Yes | Password against a bcrypt hash in `auth.db` (§ 1.3) | See § 1.7 | See § 1.7 | No IdP; see § 1.3 |
| OpenID Connect (`oidc`, incl. AWS Cognito) | Yes | None beyond § 1.2. Issuer set by the operator (`sso_issuer`); endpoints must be on the issuer's origin or on origins the operator lists (`sso_endpoint_origins`, needed for Cognito's hosted domain) | See § 1.7 | See § 1.7 | |
| Microsoft Entra ID (`entra`) | Yes | The `tid` claim must be present and equal `entra_tenant_id`. Issuer is derived from the tenant; endpoints must be on `https://login.microsoftonline.com` | See § 1.7 | See § 1.7 | |
| Google Workspace (`google`) | Yes | `email_verified` must be true; the signed `hd` claim must be present and equal `google_hosted_domain`, so personal Google accounts are refused | See § 1.7 | See § 1.7 | Exactly one hosted domain |
| VANguard (`vanguard`) | Yes | Userinfo is called, and its `sub` must equal the ID token's `sub`. Endpoints must be on the issuer's origin | See § 1.7 | See § 1.7 | ABN is recorded as `organisation_id` |

Every IdP deployment must also set `sso_client_id`, `sso_client_secret`,
`sso_transaction_secret`, `public_base_url`, `compartment_id` and both
per-identity quota defaults, and must not set another provider's settings.
Readiness (`/api/ready`) reports any missing setting by name, and the
sign-in routes refuse while the profile is incomplete
(`src/elspeth/web/sso_wiring.py`, `src/elspeth/web/readiness.py`).

**IdP groups and roles are not read.** ELSPETH takes a closed set of claims
from the ID token (`src/elspeth/web/auth/claims.py`) and ignores group and
role claims. Authority inside ELSPETH comes only from its own role records
(§ 2), so a directory group grants nothing here.

### 1.2 SSO sign-in

ELSPETH is a confidential backend client using the authorisation-code flow
with PKCE (S256). The browser never receives an IdP token; the flow and its
transport controls are summarised in
[02 F2](02-data-flows-and-trust-boundaries.md#3-product-flows) [EV-012]. The identity
controls are:

- **ID-token verification** (`src/elspeth/web/auth/id_token.py`): signature
  checked against the IdP's published keys, with the algorithm pinned per
  profile (RS256 for all four) and never read from the token header; `exp`,
  `iat`, `iss`, `sub` and `aud` required; audience must be this client; 60
  seconds of clock leeway; the nonce must match the one sealed in the login
  transaction; when `aud` lists several parties, `azp` must name this client.
  A matched JSON Web Key (JWK) must declare an RSA or EC key type; symmetric
  (`oct`) and other key types are refused before their key material is used.
- **Login transaction:** state, nonce and PKCE verifier are sealed with
  AES-GCM in a `__Host-` cookie that is `Secure`, `HttpOnly`,
  `SameSite=Lax` and valid for 300 seconds. Its key is derived from
  `sso_transaction_secret`, which is separate from `secret_key`.
- **Handoff:** the callback returns a one-time code in the URL fragment. The
  code has 256 bits of entropy, only its SHA-256 hash is stored, it expires
  after 900 seconds, and single use is enforced in the database.
- **Admission:** a token is issued only to an identity in the `active`
  state (§ 2.1). A first SSO sign-in creates a `pending` identity and is
  refused until an administrator activates it (§ 4).

#### JWKS retrieval and cache authority

All SSO profiles use the same JSON Web Key Set (JWKS) validator
(`JWKSTokenValidator` in `src/elspeth/web/auth/id_token.py`) [EV-119]. The
resolved JWKS URI has already passed the provider endpoint-origin policy,
and each refresh fetches that exact URI with redirects disabled. A fetched
document is shape-checked before it can replace the cache.

The cache balances signing-key rotation, identity-provider availability and
the risk of granting stale keys too much authority:

- `jwks_cache_ttl_seconds` (default 3,600) is the normal refresh interval.
- An unknown token key ID may force one refresh, but a cooldown prevents
  repeated unknown IDs from amplifying traffic to the identity provider.
- `jwks_failure_retry_seconds` (default 300, minimum 10) throttles refresh
  attempts after network or document-validation failures. At cold start,
  requests fail closed during that window. With a previously validated
  cache, concurrent requests may use the stale cache without queueing behind
  a failed refresh.
- `jwks_max_stale_seconds` (default 86,400) is the absolute authority limit,
  measured from the last successful, fully validated fetch. Failed retries
  do not renew it. Once it expires, sign-in fails closed until a valid JWKS
  is fetched.

The deployment records all three values and the availability/security reason
for any change from the defaults in § 1.7. Lifecycle and failure behaviour
are exercised by `tests/unit/web/auth/test_jwks_cache_lifecycle.py`.

### 1.3 Local accounts

Local credentials live in `auth.db` under `data_dir`, separate from
identities and roles, which live in the sessions database
([02 § 6](02-data-flows-and-trust-boundaries.md#6-trust-changes-inside-the-product-boundary))
(`src/elspeth/web/auth/local.py`) [EV-103].

- Passwords are hashed with bcrypt at cost 12 (the library default,
  measured: `bcrypt.gensalt()` gives `$2b$12$`). Passwords over bcrypt's
  72-byte limit are refused rather than truncated.
- An unknown username is checked against a dummy hash, so a wrong username
  and a wrong password take the same time and return the same message.
- `auth.db` is created owner-only (`0600`) and is refused at open if it is
  not a regular, single-link, owner-only file owned by the service user.
- Passwords set by an administrator are generated by the server (24 URL-safe
  characters, about 144 bits) and shown once.

Registration is controlled by `registration_mode` (default `open`):

| Mode | `POST /api/auth/register` | Admission of the new identity |
|---|---|---|
| `open` | Accepted; a token is returned at once | Active at first sign-in. Anyone who can reach the site can register ([01 § 7](01-system-overview-and-boundary.md#7-security-relevant-settings--deployment-record)) |
| `email_verified` | Requires an email address. A one-use verification token (24-hour lifetime, stored as a SHA-256 hash) is written to an owner-only outbox file, `email-verifications.jsonl` under `data_dir`; delivering the email is the deployment's job | `pending` until an administrator activates it |
| `closed` | Returns 404 | Accounts are created by an operator (`elspeth composer users add`) or a local-account administrator (§ 3.3), and are `pending` until an administrator activates them |

Outside a local test host, `email_verified` requires a publicly reachable
`public_base_url`, so emailed links never come from a request's `Host`
header.

The deployment records its treatment of local-account password and MFA risk
in § 7.1.

### 1.4 Session tokens

Every sign-in, whichever provider, ends with ELSPETH issuing its own session
token (`src/elspeth/web/auth/session_token.py`) [EV-102].

| Property | Value at `49c1845085d36811b120ef1c540048463e32aabc` |
|---|---|
| Format and algorithm | JWT, HS256 |
| Signing key | Derived from `secret_key` with HKDF-SHA256 under a purpose-specific label (`src/elspeth/web/key_derivation.py`). Placeholder, undersized (under 32 bytes) and single-repeated-byte keys are refused outside local test hosts ([01 § 7](01-system-overview-and-boundary.md#7-security-relevant-settings--deployment-record)) |
| Claims | `sub` (the identity ID, never the username), `username` (display only), `provider`, `iss` = `elspeth`, `aud` (the deployment's `public_base_url`, or `elspeth-local`), `jti`, `iat`, `exp` |
| Lifetime | 24 hours. A fixed constant, not a setting |
| Refresh | `POST /api/auth/token`, local accounts only. A refreshed token keeps the original `iat`, and refresh is refused once that is more than 168 hours old. SSO users sign in again when the token expires |
| Checked on every request | Signature, required claims, issuer and audience; `provider` must equal the deployment's provider, so a token survives neither a change of IdP nor use against another deployment; the identity must still be `active`. For local accounts, a verified credential row must also still exist |
| Transport | `Authorization: Bearer` header. No session cookie, so the API has no cross-site request forgery surface. The single-page application keeps the token in browser local storage |
| Sign-out | `POST /api/auth/logout` records a `logout` event; the client discards the token |

Because the `active` check runs on every request against the identity
store, disabling an identity in ELSPETH takes effect on its next request,
not at token expiry. The deployment records its treatment of session
termination and idle-timeout risk in § 7.1.

### 1.5 Sign-in rate limiting

The sign-in, registration, email-verification and SSO routes, and the audit
write for failed bearer-token checks, share a per-client limit:
`auth_rate_limit_per_minute`, default 20, over a sliding 60-second window
(`src/elspeth/web/middleware/rate_limit.py`). With PostgreSQL state, all
replicas draw on one shared budget. An over-limit sign-in, registration or
SSO-route request is rejected with 429 before its handler runs and therefore
writes no `auth_events` row. A failed bearer token is always refused; when
its failure-audit write is over the shared limit, the middleware suppresses
that audit row and increments `auth_failure_audit.suppressed_total` instead
(`src/elspeth/web/auth/middleware.py`).

The client is identified by the address the server sees
(`request.client.host`). Behind a reverse proxy, that is the client only if
the server trusts the proxy's forwarded headers — a Deployment record item
(§ 1.7). The deployment records any residual rate-limiting risk in § 7.1.

### 1.6 Recording authentication events

Authentication and authority events are written to the `auth_events` table
in the Landscape (`src/elspeth/core/landscape/schema.py`) [EV-106]. The
event vocabulary is closed — 23 types, in `AuthAuditEventType`
(`src/elspeth/contracts/auth.py`), backed by a database CHECK constraint —
so an unknown event type fails the write.

| Event | Written when |
|---|---|
| `login` (success or failure) | Local sign-in; SSO callback |
| `token_issued` | Any token issued, with how (`login`, `register`, `email_verification`, `refresh`, SSO complete) and its issue and expiry times |
| `auth_failure` | Any refused authentication, with a failure category, the stage, and the exception class — never the external error text |
| `logout` | Sign-out |
| Admission and authority events | § 3.5 |

Each row records the provider, identity, username, request ID, client
address and user agent. Failure categories separate a bad credential
(`invalid_credentials`) from an identity that authenticated but is not
admitted (`access_pending`, `identity_disabled`, `sso_access_pending`,
`sso_identity_disabled`, `sso_identity_rebound`), so a queue of people
awaiting approval does not hide in the same count as guessed passwords. SSO
refusals carry one of a closed set of categories (`sso_state_mismatch`,
`sso_id_token_invalid`, `sso_claim_check_failed`, `sso_handoff_invalid` and
others, `src/elspeth/web/auth/sso.py`).

How these records are retained, monitored and reviewed is in
[08](08-logging-audit-and-monitoring.md).

### 1.7 Deployment record

| Item | Value |
|---|---|
| Provider in use (`auth_provider`) and IdP tenant / issuer | DEPLOYMENT-TODO: |
| Public application origin (`public_base_url`) | DEPLOYMENT-TODO: |
| OAuth/OIDC client ID (`sso_client_id`) and the redirect URI registered at the IdP | DEPLOYMENT-TODO: |
| MFA: where it is enforced (IdP conditional access, Cognito MFA setting, or none for local) and for which users | DEPLOYMENT-TODO: |
| IdP policies that apply (conditional access, device compliance, session length at the IdP) | DEPLOYMENT-TODO: |
| Additional permitted endpoint origins (`sso_endpoint_origins`), with the reason each differs from the issuer origin | DEPLOYMENT-TODO: |
| Break-glass endpoint overrides (`sso_authorization_endpoint`, `sso_token_endpoint`, `sso_userinfo_endpoint`, `sso_jwks_uri`), or confirmation that discovery supplies all endpoints | DEPLOYMENT-TODO: |
| JWKS cache policy (`jwks_cache_ttl_seconds`, `jwks_failure_retry_seconds`, `jwks_max_stale_seconds`) and reason for any non-default value | DEPLOYMENT-TODO: |
| Browser origins allowed by `cors_origins` | DEPLOYMENT-TODO: |
| `registration_mode` (local only) | DEPLOYMENT-TODO: |
| Local-account password policy and how it is enforced | DEPLOYMENT-TODO: |
| Who can reach the sign-in page (network restriction, if any) | DEPLOYMENT-TODO: |
| Reverse proxy forwarded-header trust, so that client addresses in the rate limiter and `auth_events` are real client addresses | DEPLOYMENT-TODO: |
| `auth_rate_limit_per_minute` | DEPLOYMENT-TODO: |
| Custody of `secret_key` and `sso_transaction_secret`; rotation — see [07](07-secrets-and-key-management.md) | DEPLOYMENT-TODO: |

## 2. Authorisation

### 2.1 Access states

Authentication proves who someone is; admission decides whether they may use
this deployment; roles decide what they may do. Admission is the
`access_state` of the person's identity record
(`IdentityAccessState` in `src/elspeth/contracts/auth.py`):

| State | Meaning | Token issued? |
|---|---|---|
| `pending` | First seen, not yet approved; or put back after dormancy (§ 4) | No |
| `active` | Approved by an administrator (or admitted by open registration) | Yes |
| `disabled` | Disabled by an administrator, by a credential deletion, or automatically on a subject rebound (§ 4) | No |

An unrecognised state is refused, and a missing identity record is never
treated as a grant.

### 2.2 Roles

Roles are records in the sessions database (`identity_roles`), granted and
revoked by administrators, never deleted, and optionally time-limited
(`expires_at`). The vocabulary is closed — seven roles in `IdentityRole`,
backed by a CHECK constraint [EV-107].

| Role | Intended purpose | Enforced at `49c1845085d36811b120ef1c540048463e32aabc` |
|---|---|---|
| `admin` | Deployment operations: identities, roles, approver relationships, quotas | Yes — every identity-administration and quota route (§ 3.1) |
| `approver` | Decides approval requests; may appoint a curator for someone they directly oversee | Yes, when workflow governance is on (§ 2.5) |
| `reviewer` | Attests review requests | Yes, when workflow governance is on |
| `curator` | Accepts, rejects, deprecates and recalls library entries | Yes, when workflow governance is on |
| `user` | Author and run pipelines; publish to and fork from the library | Library publishing and forking check it when workflow governance is on. Web authoring and running are admitted by `active` identity state and do not require this role; record the deployment's risk decision in § 7.1 [EV-104] |
| `auditor` | Reserved for future read-only audit authority | Non-authorising at this commit: recording the role grants no additional route access [EV-104] |
| `oversight` | Reserved for future oversight authority | Non-authorising at this commit: recording the role grants no additional route access; quota changes require `admin` [EV-104] |

Rules the identity authority applies to every grant
(`src/elspeth/web/coordination/identity_authority.py`):

- `admin` is never combined with `user`, `approver`, `reviewer` or
  `curator`, in either order, so an administrator cannot also author,
  approve, attest or curate.
- An identity of kind `service` may hold only `admin` or `oversight`.
- Activation grants at most one of `user`, `approver` or `reviewer`, or no
  role.

Guarantees § 11.4 records that ELSPETH does not decide which people should
hold which roles; that is the deploying organisation's policy, recorded in
§ 2.7.

### 2.3 Enforcement on each request

Every protected route calls the same dependency (`get_current_user`,
`src/elspeth/web/auth/middleware.py`), which applies the checks in § 1.4.
Role checks read the role store on each request; they are not cached in the
token. Identity, role, relationship and quota mutations re-check the actor's
authority inside the same sessions-database transaction as the change. Local
credential administration is a separate two-store path: it performs a live
per-request capability check before changing `auth.db`, and account deletion
uses the fencing and compensation controls in § 3.3. A caller without the
required capability gets `404 Not found`, so the existence of administrative
routes is not confirmed to them.

Unauthenticated calls to protected API routes are refused with 401 [EV-105].
The deliberately public authentication, configuration and status endpoints
include the sign-in routes
(`/api/auth/login`, `/register`, `/verify-email`, `/sso/start`,
`/sso/callback`, `/sso/complete`), `/api/auth/config`, `/api/health`,
`/api/ready` and `/api/system/status`
([02 § 3](02-data-flows-and-trust-boundaries.md#3-product-flows), F17). Any accepted
exception to the authenticated-route policy is recorded in the controlled
risk register through § 7.1. The run-progress WebSocket is authenticated with single-use tickets
([02 F4](02-data-flows-and-trust-boundaries.md#3-product-flows)). `/metrics` needs
the separate operator bearer token (02 F14).

### 2.4 Session isolation

A Composer session belongs to the identity that created it. Session-scoped
routes check that the session exists, is not archived, belongs to the caller,
and was created under the current provider, and answer `404 Session not
found` in every failing case, so one user cannot probe for another's
session IDs (`src/elspeth/web/sessions/ownership.py`,
guarantees § 13.1) [EV-109].

Two routes let one person see another's work, both read-only:

- **Shareable review links** (§ 6).
- **Governed inspection:** with workflow governance on, an approver or
  reviewer with an open request may inspect the requested composition, and
  each inspection writes an access-log record
  (`src/elspeth/web/sessions/routes/workflow/inspect.py`).

### 2.5 Workflow governance

`workflow_governance` is `off` by default. When `on` [EV-110]:

- **Runs need approval.** A run starts only if an approval exists for the
  exact composition state, bound to its configuration hash, runtime
  manifest, plugin-policy hash and related fingerprints; a changed pipeline
  needs a new approval (`src/elspeth/web/coordination/run_start_permit_authority.py`,
  `approval_authority.py`).
- **Author is not approver.** The decider must be a different, `active`,
  human identity holding a live `approver` role; the author deciding their
  own request is refused (`ApprovalAuthorIsApprover`). Both participants are
  locked and re-read inside the decision's transaction, using database time.
- **Requester is not reviewer.** An attestation needs a live `reviewer` role
  and a reviewer other than the requester (`review_authority.py`).
- **Publisher is not curator.** Publishing to the library needs a live
  `user` role; accepting, rejecting, deprecating or recalling an entry needs
  a live `curator` role, and the entry's publisher may not curate it
  (`library_authority.py`).
- Every request, decision, attestation and library change writes an
  `auth_events` row (§ 3.5).

Governance is only as strong as the rule "one person, one identity". Local
accounts with `registration_mode=open` break that rule, because anyone can
register more accounts. With `workflow_governance=on`, `auth_provider=local`
and `registration_mode=open`, readiness reports `auth_mode` as not ready,
naming the combination, and `/api/ready` answers 503; governance also
requires `compartment_id` (`_workflow_governance_refusal` in
`src/elspeth/web/readiness.py`). With governance off, the approval, review
and library routes answer `409 workflow_governance_off`.

### 2.6 Compartments

`compartment_id` names the deployment's compartment. It is stamped on
exported pipeline YAML, library entries and audit metadata, and text a user
pastes into the Composer is checked for other compartments' markings, which
are recorded with the input's hash (`src/elspeth/web/compartments.py`). This
lets the same artefact be traced across deployments. It is a marking, not an
access boundary: all users of one deployment are in the same compartment.
Separate compartments are separate deployments.

### 2.7 Deployment record

| Item | Value |
|---|---|
| `workflow_governance` | DEPLOYMENT-TODO: |
| `compartment_id` | DEPLOYMENT-TODO: |
| Who approves role assignments, and on what basis | DEPLOYMENT-TODO: |
| Role holders — number of identities per role, and named `admin` holders | DEPLOYMENT-TODO: record in the controlled copy only |
| Approver relationships (who oversees whom) and how they are maintained | DEPLOYMENT-TODO: |
| Separation-of-duties rules the agency applies beyond those in § 2.2 and § 2.5 | DEPLOYMENT-TODO: |

## 3. Administrative access

### 3.1 What an administrator can do

| Capability | Route | Who |
|---|---|---|
| List identities; pre-provision, activate, enable, disable | `/api/auth/admin/identities…` | Live `admin` role |
| Grant and revoke roles | `/api/auth/admin/roles…` | Live `admin` role; an `approver` may grant only `curator`, only to someone they directly oversee, only with governance on |
| Assert and revoke approver relationships | `/api/auth/admin/relationships…` | Live `admin` role |
| Directory of people (merged view of identities and local accounts) | `/api/auth/admin/people…` | Either capability below; each source is shown only to its own capability |
| Set and revoke per-identity quotas | `/api/workflow/quota/identities/…` | Live `admin` role |
| Create local accounts, reset passwords, delete accounts | `/api/auth/admin/users…` | Local authentication only: an `active` human `admin`, or the configured `dev_admin_user` (§ 3.3) |

Sources: `src/elspeth/web/auth/identity_admin_routes.py`, `people_routes.py`,
`quota_routes.py`, `admin_routes.py` [EV-108]. The browser interface for
these is the **People & access** panel.

### 3.2 Creating the first administrator

A new deployment has no administrator. There are two ways to make one, both
writing the identity, its `active` state, a deployment-wide `admin` role and
its quota row in one audited transaction (`identity_activated` with cause
`bootstrap`) [EV-111]:

- **Configured seed (SSO):** a subject listed in `sso_admin_subjects`
  activates itself as `admin` at its first sign-in, but only while no human
  `admin` grant has ever existed in the deployment, including revoked and
  expired grants. After that the list is inert and never acts as a standing
  grant (`src/elspeth/web/sso_wiring.py`). Two replicas racing are
  serialised by a database lock.
- **Operator recovery:** `elspeth composer users bootstrap-admin PROVIDER
  SUBJECT --note "…"`, run by an operator with access to the stores. It is
  refused while any active human administrator exists, and the note is
  required and recorded (`src/elspeth/cli.py`).

Further administrators are granted the `admin` role by an existing one. The
last active human administrator cannot be disabled, have the role revoked,
or have their local account deleted (rule R5); the refusal happens before
anything changes.

### 3.3 Local credential administration

With local authentication, credentials are administered separately from
identities. Active human administrators can create accounts (with a
server-generated password shown once), reset passwords and delete accounts.
The optional `dev_admin_user` setting names one local username that has the
same credential capability without holding a role. It is refused at start-up
unless `auth_provider=local` (`src/elspeth/web/config.py`,
`src/elspeth/web/auth/admin_routes.py`) [EV-112]. Deleting an account needs a
reason, and an administrator cannot delete their own account. Creating a
credential does not admit anyone: the new identity follows the registration
mode's admission rule (§ 1.3).

Every successful account-deletion route call is recorded in the application's
operational log (`dev_admin_user_deleted`). When the username has a bound
identity, deletion also retires that identity and writes
`identity_disabled` with cause `credential_deleted` to `auth_events`. A
credential may exist for a username that has never signed in; in that case
there is no identity to retire and therefore no `auth_events` row. Credential
creation and password reset are operationally logged. The deployment records
any residual local-administration risk in § 7.1.

Deletion cannot be one database transaction because credentials and
identities use separate stores. The identity authority checks last-admin
protection before it calls the credential deletion. The local store snapshots
the salted password hash, re-reads it in the credential-deletion transaction
and refuses if it changed, which prevents deletion of a newly reset or
re-created credential. Credential deletion then precedes identity retirement;
a retry can complete an interrupted retirement idempotently without deleting
a later credential.

### 3.4 Recording administrative actions

Every change to identities, roles, relationships and quotas writes its
`auth_events` row (`identity_activated`, `identity_disabled`,
`identity_enabled`, `role_granted`, `role_revoked`,
`relationship_asserted`, `relationship_revoked`, `quota_set`) inside the
same transaction as the change: if the audit write fails, the change does
not commit. Activation needs a note and disabling needs a reason; both are
recorded. Each row names the acting identity and carries the
`on_behalf_of` and `console_request_id` keys, which are empty when a human
administrator acts for themselves (`src/elspeth/web/auth/audit.py`,
`src/elspeth/contracts/auth.py`).

### 3.5 Deployment record

| Item | Value |
|---|---|
| Named administrators and why each needs the role | DEPLOYMENT-TODO: controlled copy only |
| `sso_admin_subjects` at first deployment; confirmation the seed has been consumed | DEPLOYMENT-TODO: |
| `dev_admin_user` — must be unset outside development | DEPLOYMENT-TODO: |
| Who may run operator commands (`bootstrap-admin`, `users add`, `users remove`) and from where | DEPLOYMENT-TODO: |
| Break-glass procedure if every administrator is locked out | DEPLOYMENT-TODO: |
| How administrative events in `auth_events` are reviewed, and how often | DEPLOYMENT-TODO: see [08](08-logging-audit-and-monitoring.md) |

## 4. Account lifecycle

| Stage | How | Evidence left |
|---|---|---|
| First sign-in (SSO) | Creates a `pending` identity keyed on provider and subject | `login` and `auth_failure` (`sso_access_pending`) |
| First sign-in (local) | Creates the identity; `active` only under `open` registration | `login`, and `identity_activated` when admitted |
| Pre-provisioning | An administrator creates an `active` identity by provider and subject before first sign-in, with a role and a note; the first sign-in binds to it | `identity_activated` (cause `pre_provision`) |
| Activation | An administrator approves a `pending` identity, choosing a role and giving a note. The raw profile claims snapshot is stored only once an identity is active, not for people who merely tried to sign in | `identity_activated`, plus `role_granted` and `quota_set` where written |
| Disable | An administrator disables with a reason; takes effect on the next request (§ 1.4). Revokes the person's approver relationships | `identity_disabled` |
| Re-enable | An administrator re-enables with a note | `identity_enabled` |
| Subject rebound | If a signed-in SSO subject's verified email differs from the one first seen, the identity is disabled and the sign-in refused, in case the IdP has reissued the subject to someone else | `identity_disabled` (cause `rebound`), `auth_failure` (`sso_identity_rebound`) |
| Dormancy | At sign-in, an `active` identity whose previous sign-in is older than `identity_dormancy_days` (default 90) is returned to `pending` and must be re-approved. Applies to local and SSO identities; the last active human administrator is exempt | `identity_disabled` (cause `dormant`); for the exempt administrator, the same event type with outcome `failure` |
| Removal (local) | `elspeth composer users remove USERNAME --reason "…"` or the administrator delete route. The credential is deleted. If a bound identity exists, it is retired: disabled, and its subject rewritten so a later account with the same username gets a new identity rather than inheriting the old one | Operational deletion log in every successful route case; `identity_disabled` (cause `credential_deleted`, with the reason) when a bound identity is retired |
| Removal (SSO) | Disable in ELSPETH, and remove or disable the person at the IdP | `identity_disabled` |

Activated and retired identity records are not deleted. They anchor the
person's audit history: sessions, runs and audit events refer to them
(`src/elspeth/web/coordination/identity_authority.py`) [EV-113] [EV-114].
The repository also contains `purge_stale_pending_identities`, which can
delete never-activated `pending` identities after a retention period. No
runtime path calls that surface in this release;
`identity_pending_retention_days` is validated configuration only. Record and
reassess the retention behaviour before wiring the purge into a deployment.

**Offboarding note.** Disabling a person at the IdP stops new sign-ins. A
session token already issued remains valid until it expires (at most 24
hours) unless the identity is also disabled in ELSPETH, which takes effect
on the next request. Offboarding procedures should therefore do both.

**Access review.** The identity list (`GET /api/auth/admin/identities`),
role list (`GET /api/auth/admin/roles`, optionally including revoked grants)
and the People & access panel show each identity's state, roles, last
sign-in and who activated or disabled it. `auth_events` holds the history.

Use this generic procedure for each scheduled access review:

1. Export or view all identities, including `pending` and `disabled`, all
   current and historical role grants, approver relationships, per-identity
   quotas and last sign-in times.
2. Reconcile every active identity with the deployment's authoritative
   personnel or directory record. Confirm the business need for every live
   role, with separate attention to `admin`, approver, reviewer and curator
   separation.
3. Resolve stale pending identities, expired temporary grants, dormant
   identities and people whose employment or duties changed. Apply changes
   through the administrative routes so the authority and audit checks run.
4. Verify removals from both ELSPETH and the IdP. For local users, delete the
   credential with a reason; for SSO users, disable the identity in ELSPETH
   as well as at the IdP so an existing ELSPETH token stops on its next use.
5. Record the review date, reviewer, population examined, changes made,
   unresolved exceptions and supporting `auth_events` query or export in the
   controlled repository. Send suspected compromise to
   [13 § 1](13-incident-response-and-continuity.md#1-purpose-scope-and-activation).

For a joiner, require an approved request, choose the least-privileged role,
pre-provision or approve the pending identity, and verify the first sign-in.
For a mover, re-perform the role and relationship decision rather than adding
new access to old access. For a leaver, disable ELSPETH access and the IdP
account, revoke relationships and roles, remove local credentials where
applicable, and verify that the next authenticated request is refused. The
deployment record below supplies the organisation's approvers, service
targets, review cadence and evidence location.

### 4.1 Deployment record

| Item | Value |
|---|---|
| Joiner process: who requests access, who approves, pre-provisioning or first-sign-in approval | DEPLOYMENT-TODO: |
| Mover process: how role changes are requested and approved | DEPLOYMENT-TODO: |
| Leaver process: disable in ELSPETH and at the IdP; target time | DEPLOYMENT-TODO: |
| `identity_dormancy_days` | DEPLOYMENT-TODO: |
| Access review cadence, reviewer, and where the record of each review is kept | DEPLOYMENT-TODO: |
| Link between ELSPETH identities and the agency's HR or directory records | DEPLOYMENT-TODO: |

## 5. Machine and service identities

### 5.1 Inside the application

The identity model has a `service` kind for a future organisation console
acting on a person's behalf, restricted to `admin` or `oversight`. Service
credentials are not implemented at this commit: pre-provisioning a `service`
identity is refused (`ServiceIdentityProvisioningUnavailable`), and no
non-browser credential can call the web API. Every API caller is a person
who signed in through the configured provider [EV-104].

### 5.2 Identities the application runs as

The deployment bundles give the running service its own cloud identities,
separate from any user. As shipped at
`49c1845085d36811b120ef1c540048463e32aabc` [EV-115] [EV-116]:

| Bundle | Identity | Scope in the bundle |
|---|---|---|
| AWS ECS | Task role | Read, write and delete objects under this deployment's S3 prefix and list its acceptance bucket; analyse documents with Textract; mount and write through the deployment's EFS access point; open ECS Exec SSM message channels; write the operator CloudWatch log group and X-Ray traces; read CloudWatch metric data and X-Ray traces. When Bedrock is the LLM back end, invoke only the configured Bedrock models and apply/get the two deployment guardrails. Trust is limited to ECS tasks in the same account and region; a permissions boundary is applied |
| AWS ECS | Task execution role | Pull the image; read this deployment's Secrets Manager secrets at task start |
| AWS ECS | Database roles | A schema-owner role and a separate runtime role. The runtime role can connect and read and write rows but cannot create objects. Neither is a superuser or can create databases or roles (`database_bootstrap.tf`) |
| Azure Container Apps | Runtime user-assigned managed identity | Pull from the registry (`AcrPull`); `Storage Blob Data Contributor` on one container; `Key Vault Secrets User` for runtime secrets |
| Azure Container Apps | Schema-owner managed identity | `AcrPull`; `Key Vault Secrets User` on the application/runtime vault and on the separate schema-owner vault; used by the schema job |

Docker Compose and systemd deployments run under whatever OS account and
database credentials the operator provides.

### 5.3 Operators

The `elspeth` command-line tool and the MCP servers (`elspeth-mcp`,
`elspeth-composer`) do not authenticate their users. Anyone who can run them
with access to the data directory, databases and secrets has that access.
Operator access is therefore controlled by the host, container platform and
database permissions, and is recorded below.

### 5.4 Deployment record

| Identity | Type (IAM role, managed identity, DB account, OS account) | Used by | Permissions granted | Credential storage and rotation | Owner |
|---|---|---|---|---|---|
| DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: |

Also record: who holds operator shell or console access to the running
service and its databases, and how that access is granted and reviewed.

## 6. Shareable review links

A shareable review link lets the owner of a composition show a frozen,
read-only copy of it to another person
([ADR-022](../architecture/adr/022-shareable-reviews.md),
`src/elspeth/web/shareable_reviews/`) [EV-117].

- **Issue.** Only the session owner can mark a composition ready for review
  or fetch its link (session ownership check, § 2.4). Marking is refused
  unless the composition passes validation and has no readiness errors. An
  audit record is written before the snapshot is stored, and the snapshot is
  content-addressed in the payload store.
- **Token.** HMAC-SHA256 over a canonical-JSON envelope: version, session,
  state, creation and expiry times, a random nonce, the snapshot's digest and
  the creator's identity. The key is `shareable_link_signing_key`, separate
  from `secret_key`. The service does not start without it; it must be at
  least 32 bytes and, outside local test hosts, not a single repeated byte
  (`src/elspeth/web/config.py`). Signatures are compared in constant time;
  unknown versions and expired tokens are refused, and every failure returns
  the same generic 401.
- **Scope.** One snapshot of one composition state, read-only. The token is
  a capability, not a credential: the recipient must also be signed in, and
  the route returns 401 without a session token (measured, [EV-105]). Any
  `active` identity in the deployment who has the link can open it.
  Tampering with the stored snapshot is detected because it is read back by
  its digest.
- **Expiry.** `shareable_link_lifetime_seconds`, default 30 days. A link
  also stops working when the snapshot is removed under payload retention
  (default 90 days), with a 404 asking for a fresh link.
- **Revocation.** Rotating `shareable_link_signing_key` invalidates every
  outstanding link at once. The deployment records its treatment of link
  lifetime and distribution risk in § 7.1.
- **Rate limiting.** Issue uses the per-user write limit; opening a link
  uses the stricter per-user limit.

### 6.1 Deployment record

| Item | Value |
|---|---|
| Whether shareable links are used | DEPLOYMENT-TODO: |
| `shareable_link_lifetime_seconds` | DEPLOYMENT-TODO: |
| Custody and rotation of `shareable_link_signing_key` — see [07](07-secrets-and-key-management.md) | DEPLOYMENT-TODO: |
| Guidance to users on who links may be sent to | DEPLOYMENT-TODO: |

## 7. Controlled risk references

### 7.1 Deployment record

The public product document names the control boundary; the controlled copy
records the deployment's decision and final risk-register identifier. Do not
put sensitive finding detail in this repository.

| Topic requiring a deployment decision | Controlled risk register reference or acceptance record |
|---|---|
| Local-account password and MFA policy | DEPLOYMENT-TODO: |
| Session termination, bearer-token storage and idle timeout | DEPLOYMENT-TODO: |
| Authentication rate limiting and reverse-proxy client-address trust | DEPLOYMENT-TODO: |
| Any approved unauthenticated-route exception | DEPLOYMENT-TODO: |
| Active-identity authoring/running without a `user` role | DEPLOYMENT-TODO: |
| Local credential-administration and two-store deletion residual risk | DEPLOYMENT-TODO: |
| Shareable-link lifetime, distribution and all-links-at-once revocation | DEPLOYMENT-TODO: |

## 8. Current evidence map

The claim-side citations in this document resolve to these current entries in
[16](16-evidence-index.md). Each entry records its own revision and evidence
source; this table does not create a separate deployment test result.

| ID | Item |
|---|---|
| EV-012 | SSO sign-in flow (existing entry) |
| EV-101 | IdP profile registry and per-provider claim checks |
| EV-102 | Session token issuer |
| EV-103 | Local accounts: hashing, `auth.db` protection, registration modes |
| EV-105 | Protected-route authentication tests and public-endpoint inventory |
| EV-106 | `auth_events` schema and event vocabulary |
| EV-107 | Role vocabulary and grant rules |
| EV-108 | Administrative routes and their guards |
| EV-109 | Session ownership check |
| EV-110 | Workflow governance authorities and readiness refusal |
| EV-111 | First-administrator bootstrap |
| EV-112 | Local credential administration and `dev_admin_user` |
| EV-113 | Account lifecycle: rebound, dormancy, retirement, last-administrator protection |
| EV-114 | `elspeth composer users` commands |
| EV-115 | AWS ECS task roles and database roles |
| EV-116 | Azure Container Apps managed identities |
| EV-117 | Shareable review links |
| EV-119 | JWKS retrieval, permitted key types, cache lifecycle, retry throttle and maximum stale authority |

Whether these controls are sufficient for the deployment is assessed in
[04](04-threat-model.md); open limitations are tracked in
[15](15-risk-register.md).
