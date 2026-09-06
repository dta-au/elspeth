# Pluggable SSO and identity substrate — backend-for-frontend login for Entra, VANguard, Google, and generic OIDC

Date: 2026-09-02. Status: design, revision 2.11, implementation plan = tracker milestone elspeth-07cd19ba73.
Revision 2.2 applies the second review round (solution architect, systems thinker, security architect) on the operator's compartment model; items are marked **[rev2.2]**. The four operator decisions from that round (D14–D17) were ruled 2026-09-02 and applied as **[rev2.3]**. Revision 2.4 pins operator selection of the IdP profile by configuration alone, marked **[rev2.4]**. Revision 2.5 adds the per-person disk quota for uploaded blobs (D18), marked **[rev2.5]**. Revision 2.6 adds the approval and review mailbox, the round trip of request note and decision note between requester and approver, marked **[rev2.6]**. Revision 2.7 closes the four blocking defects a ten-seat panel review found on 2026-09-03 (D19 the provider discriminator, D20 the bootstrap admin, D21 the withdrawn VM in-place rebuild, and the epoch-freeze note), marked **[rev2.7]**. Revision 2.8 applies the verified remainder of that review — the surviving high and medium findings and rulings D24 to D34 — marked **[rev2.8]**; findings the verification pass refuted were not applied, and are listed with their refuting reason in the review record. Revision 2.9 corrects what implementation measured against the tree, marked **[rev2.9]**: the §Discriminator widening site inventory undercounted the `routes.py` local-only guards (four, not two) and misclassified them as sites needing a value edit. Revision 2.10 records five further things implementation measured, marked **[rev2.10]**: the third raw consumer of `secret_key`, what a pre-existing local user's first login does, the refresh chain's unverified-claim input, the conditional quota row, and the three places that compared a user id with a configured username. Revision 2.11 corrects a framing error, marked **[rev2.11]**: identity-provider client registrations were written as external dependencies owed to the project, when this is a public repository that stores no provider credential and a registration is a deployment-time input supplied by whoever deploys ELSPETH. A real client gates live verification and nothing else; §External dependencies is now §Deployment-time inputs, the AWS Terraform is recorded as creating the Cognito confidential client itself, and the VANguard spike is a live confirmation of a profile that is already written. **This revision narrows what it claims, and does not disturb D5.** It means the build and every test wait on nothing: steps 2 to 5 complete without any registration. It does not mean live verification left delivery scope. D5 stands as ruled — Cognito's client the repository's Terraform now mints itself, while VANguard's is issued by the operating organisation on its ABN-gated admin page (§Deployment-time inputs), so VANguard live verification remains a delivery obligation this project cannot discharge alone and must wait on that registration to close. A reader who takes "gates live verification and nothing else" as "nothing remains owed" has read it too broadly. Revision 2.12 corrects claims this document made about the tree that the tree does not support, marked **[rev2.12]**, found by sweeping the whole file for them rather than fixing them one at a time as they surfaced — three had surfaced separately on 2026-09-07 before the sweep was run, which is what made a sweep the right response. Two are defects rather than staleness and are now tracked: the `audit_access_log` `writer_principal` third value D27 required (elspeth-e6c2d254b2) and the four `calls` token columns rev2.1 specified (elspeth-255ae1a544) were both placed inside the delivery's one-way epoch window, neither rode it, and the window has cut — so each now costs the second `rollback_permitted: false` window this spec was structured to avoid, and `token_usage_ledger`'s `run` source arm has no data path in the meantime. The third is the epoch literals in §Discriminator widening, which have been removed rather than corrected: this spec states elsewhere that epoch numbers come from the runbook's compatibility record and never from here, and a document carrying that rule should not print the number. **This revision changes no ruling.** Every correction is to a statement of fact about what is built; where a requirement went unbuilt, the requirement stands and is now traceable to a ticket instead of reading as already satisfied.
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
| D19 **[rev2.7]** | `service` in the provider discriminator | Two L0 types. `AuthProviderType` keeps the five login values and governs settings, the registry, session tokens, `sessions`, `user_secrets` and both Landscape CHECKs. `IdentityProviderType = Literal[AuthProviderType, "service"]` (nested, so `get_args` flattens) governs `identities.provider` alone, under its own named CHECK constant. Putting `service` in `AuthProviderType` fails the import-time parity assert and the app does not boot. **Applied on review recommendation; reversible until phase 1 lands.** |
| D20 **[rev2.7]** | Bootstrap admin | The seed and the operator CLI each write `access_state='active'`, an `admin` role, no workload role, and the audit pair in one transaction; the seed fires only while the container has zero active human admins. Activation accepts `role=none`, which is what an `admin` must be activated with under R8. The contradicting "lockout recovery is a config change" sentence is deleted. **Applied on review recommendation.** |
| D21 **[rev2.7]** | VM in-place rebuild | Withdrawn. Both deployment paths use the existing reset runbook, because the pre-1.0 gate this spec cites for ECS forbids in-place migration everywhere, and the promised byte-for-byte preservation of `user_secrets` is false across a key-derivation change. Re-admission of the known cohort moves inside the cutover window. **Applied on review recommendation; the fact it rests on is the runbook's own standing rule, not an assumption.** |
| D24 **[rev2.8]** | Storage quota exactness | **Eventually consistent, not exact.** The per-session lock does not serialise two sessions of one identity, and making it exact needs an identity-scoped lock held across the fork copy loop, which would serialise every other upload by that identity for the duration of a fork. R13 instead enumerates all four byte-admitting sites, which is where the real hole was. Revisit only if measurement shows real overshoot. |
| D26 **[rev2.8]** | Record of a requested review | A sibling `review_requests` table. Widening `review_attestations` with a `requested` verdict contradicts its own append-only, non-null-reviewer, ledger-not-control design and would fill the audit view with requests nobody completed. |
| D27 **[rev2.8]** | Approver's read of another identity's session | Authorized per request over roles plus the live request row, minting no token, never through the shareable-review bearer capability. It writes an `audit_access_log` row under a new `writer_principal` value, which this ruling required be added in the delivery's epoch because adding it later costs the one-way window. **That value was never added and the epoch has since cut [rev2.12].** `web/sessions/models.py` still constrains the column to `('audit_grade_view', 'admin_tool')`; the window closed at sessions epoch 52 and the head is past it. D27's own cost argument therefore now applies to D27: implementing the approver read costs exactly the second window this ruling was written to avoid. Tracked as elspeth-e6c2d254b2 — the cheap remedy is to fold the value into whatever epoch bump happens next for another reason, which is why it is filed now rather than when the mailbox is built. |
| D28 **[rev2.8]** | Actor column on the container-ceiling quota row | Nullable FK plus a closed `set_by_actor` CHECK (`identity`, `config`, `operator`, `system`), mirroring `writer_principal`. A placeholder identity would put a fake row in the table R5 counts. |
| D29 **[rev2.8]** | Workflow-table FK deletion rule | `RESTRICT`, declared explicitly, plus `durable_history_exists` extended to count approvals, attestations and library entries so archive refuses instead of discarding them. `published_from_session_id` becomes a provenance column, not an FK. Declaring nothing was never neutral: it ships `RESTRICT` by accident. |
| D30 **[rev2.8]** | Acceptance criteria for the workflow half | Ship them with it. §Testing gains a workflow-governance subsection, R11 refuses at startup loudly rather than switching enforcement off silently, and the suite runs against a closed local deployment. |
| D31 **[rev2.8]** | Quota under local, and who writes the policy row | The exemption binds to R11's own predicate (`local` **and** open registration), not to `local` alone, and every path that makes an identity active writes the row. A blanket local exemption would uncap the shared credential in a configuration where governance is deliberately on. |
| D32 **[rev2.8]** | Does R3 disable the identity | Yes: `disabled`, `disable_reason='rebound'`, actor `system`, audit row. It honours R5 on the last active human admin rather than bricking the container, and it does **not** run the edge-revocation cascade, which is unrecoverable and fires most often on a marriage or a rename. |
| D34 **[rev2.8]** | Dormancy versus the last admin | R9 carries R5's last-admin exemption. Otherwise a single-admin container reaches zero active admins at day 91 by doing nothing, and the first-login-only seed cannot re-fire. |

**[rev2.11] D10's narration, not its ruling.** D10 says "the VANguard
**spike**". rev2.11 renamed that section to §VANguard live confirmation, so
the word now names nothing; read it as pointing there. Its conditional has
also moved: the profile is written and keys on `sub` either way
(`map_vanguard`), and the detection columns `subject_email_at_first_seen`
and `rebound_at` are on `identities`. What a live token pair still settles
is whether that `sub` is stable and non-email — the subject is an email
today — which is exactly why those columns exist. **The R3 refusal itself is
specified (§Refusals R3, D32) and is not yet implemented:** `rebound_at` is
only ever written `NULL`, `subject_email_at_first_seen` is written at first
sight and compared nowhere, and no `disable_reason='rebound'` exists in the
tree. D10's ruling — no `principals` table, identity merge an unbuilt admin
action — is unchanged and correct.

## Architecture

Five units replace the three provider classes and the browser exchange
[rev2.11: the count read "Four" and omitted §5].

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
- `IdentityProviderType = Literal[AuthProviderType, "service"]` — how an
  identity row came to exist. Governs `identities.provider` alone, under its
  own named constant `_IDENTITY_PROVIDER_TYPE_CHECK`, distinct from
  `_AUTH_PROVIDER_TYPE_CHECK`.

  **Write it nested, not as a union [rev2.7.1].** `AuthProviderType |
  Literal["service"]` is a `Union`, and `get_args` on it returns two nested
  `Literal` objects rather than six strings, so every membership check and
  every contract assertion over it silently reads the wrong shape. Nesting the
  alias inside `Literal[...]` flattens it: `get_args` returns the six strings,
  and mypy accepts assigning an `AuthProviderType` value to an
  `IdentityProviderType`. Measured on this project's interpreter (3.13.1).

The narrower type is a subset of the wider one, so the `sessions.user_id` →
`identities.identity_id` FK stays sound: every value `sessions` admits is a
value `identities` admits. The contract test pins **both** CHECK strings and
both Literals, and asserts
`set(get_args(IdentityProviderType)) == set(get_args(AuthProviderType)) | {"service"}`
— an equality over flat strings, which fails loudly if anyone rewrites the
alias as a union [rev2.7.1].

**Operator selection [rev2.4].** `WebSettings.auth_provider` is the only
selector: it names one registered profile, and every other `sso_*` field is
that profile's configuration for this container. The build carries no
credentials; the profile carries no deployment facts. The settings
validator rejects a value with no registered profile by naming the
registered profiles in the error, readiness reports the active profile
name, and `GET /api/auth/config` returns the active `provider`. Switching a
container from one IdP to another is a config change and a restart, never a
build.

**A partial identity configuration fails at settings load [rev2.11]. The
container does not start.** It does not come up and report itself unready:
`WebSettings` refuses the shape and names every missing `required_settings`
field, and for a registered profile it cannot wire, `create_app` raises and
names them again (§5). Earlier revisions of this paragraph said readiness
would name the missing fields. Readiness still holds that arm as a total
boundary, and its "no registered profile" arm is live, but no ordinary
deployment reaches it with an incomplete configuration — it exited before the
application existed. What an operator sees is a task that starts, exits
immediately, and restarts, with the reason in the exit log rather than a
readiness response; see
[docs/guides/identity-providers.md](../guides/identity-providers.md)
§The failure mode operators get wrong.

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
notice; it must never ship in a release that keeps an existing store.

**A THIRD raw consumer existed [rev2.10].** This section named two jobs;
implementation found `secret_key.encode("utf-8")` also feeding
`generation_key`, the HMAC that tags plugin-binding evidence
(`plugin_policy/availability.py`), at TWO construction sites — the app factory
and the AWS ECS acceptance harness. Spec silence was not a carve-out: unlike
`shareable_link_signing_key` it had no reasoned exemption, and leaving it raw
would have contradicted the stated goal. It is derived, and both sites move
together — the fingerprint is compared against a queued run's frozen copy in
`_require_current_binding_generation`, so one converted site alone would
refuse valid runs naming the wrong cause. It joins the user-secret key in the
epoch-window notice, because the fingerprint is persisted as run evidence in
the Landscape `run_web_plugin_policy` table and embedded in exports: bundles
written either side of the change carry different fingerprints for identical
policy state.

`config.py` keeps its raw read and must. `_enforce_secret_key_in_production`
weighs the operator's bytes for entropy, and HKDF output is uniformly
distributed by construction — validating a derived key would pass for every
input, including `"aaaa…"`. **Validation reads raw; consumption reads
derived.** Each provider supplies a
`principal_is_active(identity_id)` callback; for local that is the `auth.db`
row plus the identity row, for SSO the identity row.

**`refresh` takes the TOKEN, not decomposed claims [rev2.10].** The route
previously read `iat` from `request.state.auth_claims` — the middleware's
signature-UNVERIFIED decode — and passed it to `LocalAuthProvider.refresh` as
the chain bound. The signature was verified elsewhere so it was not
exploitable, but a security bound reading a value obtained without verifying a
signature is a shape that only stays safe by accident. The issuer now reads
`iat` from its own verified decode, and the route's whole claims-extraction
stage is deleted along with the `refresh_claims` audit failure category it
produced.

**The quota row is written only where a quota regime exists [rev2.10].**
D31 requires every activating path to write the per-identity row from
`quota_default_tokens_per_day` / `quota_default_storage_bytes`. Both are
OPTIONAL settings, and a container that configures neither has no quota regime
at all: there is no allowance to record, and inventing a number would impose a
limit the operator never chose. So activation writes the row when both
defaults are configured, and writes nothing when they are not. **Consequence
to close with the enforcement:** enabling quotas on a container that has
already admitted people needs a backfill, and phase 4's enforcement must
refuse on "a regime is configured and this identity has no row", never on
"no row".

**Three sites compared a user id with a configured username [rev2.10].**
Once `sub` became the identity_id, `UserIdentity.user_id` stopped being a
username, and every comparison against an operator-configured name broke.
Found and fixed: `admin_routes._require_dev_admin` (the whole dev-admin
surface would 404 for its own admin), `routes.py` `/me`'s `dev_admin` flag
(the frontend would show a surface the backend then refuses), and
`admin_routes.delete_user`'s self-delete guard (which would have stopped
firing, letting the admin delete their own credentials mid-session). All three
now compare `username`. Ownership comparisons needed no change: both sides
became identity_ids together.

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

### 5. Startup wiring — `src/elspeth/web/sso_wiring.py` [rev2.11]

The one place that knows the registry, the login walk, the identity
substrate and the settings, so the app factory binds one object
(`app.state.sso`) and the three `/sso/*` routes read that and nothing else.
Two phases, because the factory is synchronous and discovery is not:
`build_sso_wiring` runs in the factory and assembles everything needing no
network (token issuer, handoff store, admission and read callables, the
profile's claim checks), deciding by the same rule readiness reports on
whether the deployment is wired at all; `resolve_sso_runtime` runs in
lifespan and resolves the IdP endpoints — the operator's break-glass
override, else discovery under the profile's origin policy. Discovery
failing is a boot failure by choice: a deployment that cannot reach its IdP
cannot log anyone in, and saying so at startup beats saying it to every user
at the callback. `build_sso_wiring` returning `None` deliberately means two
different things — for `local` it is the ordinary answer, there being no IdP
to wire; for a registered profile it is a boot refusal, because with the
legacy bearer path deleted (§Deleted outright) there is no second way to
authenticate anyone. The invariant underneath is that a
partially configured profile must never produce a partially working login,
and it is the mechanism behind the boot failure described under §Operator
selection.

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
Zero rows means reject. Database clock, not replica clock. Expired rows are purged **lazily
[rev2.8]** — on consume and on the next login by the same identity — with a
`sso_handoffs` TTL of 15 minutes. There is no "existing retention sweep": that
phrase named nothing, and it was doing the work of bounding a table that grows
with every abandoned login. Lazy purge matches R9's own idiom and the
`_reap_stale_pending_registrations` precedent already in the tree, and costs no
background task and no fourth required setting. The purge writes no
`auth_events` row: the login attempt's own record already exists, and a
maintenance delete is not an authority mutation. `complete` is constant-time: hash,
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
   `IdentityProviderType = Literal[AuthProviderType, "service"]` (nested, not
   a union — see §1)
   [rev2.7, D19]**.
2. `web/sessions/models.py` `_AUTH_PROVIDER_TYPE_CHECK`, used by BOTH
   `sessions` and `user_secrets` and staying at the five login values; **plus
   the new `_IDENTITY_PROVIDER_TYPE_CHECK` on `identities.provider` alone
   [rev2.7]**; `SESSION_SCHEMA_EPOCH` bumps.
3. `core/landscape/schema.py` `ck_run_attributions_auth_provider_type`,
   `ck_auth_events_provider`; `SQLITE_SCHEMA_EPOCH` bumps.
   `core/landscape/database.py` needs no edit (it lists names only).
4. `core/landscape/run_lifecycle_repository.py` `_AUTH_PROVIDER_TYPES`.
5. `web/readiness.py::_check_auth_mode` — rewritten to iterate the active
   profile's `required_settings`, no per-provider branches.
6. `web/config.py::_validate_auth_fields` — per-provider required/forbidden
   matrix from the registry.
7. `cli.py:4007` `--auth` help text and value validation.
8. `web/auth/routes.py` local-only guards — **four of them, not the two this
   list claimed before [rev2.9]** (login, register, password change, refresh),
   plus the `== "local"` `dev_admin_user` affordance. **None needs a value
   edit:** every one compares `settings.auth_provider != "local"`, which stays
   correct as the Literal widens. The pin is therefore an invariant, not an
   edit — the contract test asserts the set of literals compared against
   `auth_provider` in that module is exactly `{"local"}`, which catches the
   real risk (a guard enumerating an IdP, which would expose the credential
   routes on the next provider added) without pinning a route count that
   ordinary work may legitimately change.
9. `web/frontend/src/types/index.ts` provider union.
10. `tests/unit/web/auth/test_provider_type_contract.py` pins all of the
    above plus registry parity and **both** CHECK strings
    (`_AUTH_PROVIDER_TYPE_CHECK` and `_IDENTITY_PROVIDER_TYPE_CHECK`), both
    Literals, and
    `set(get_args(IdentityProviderType)) == set(get_args(AuthProviderType)) | {"service"}`
    — equality over flat strings, which fails loudly if the alias is ever
    rewritten as a union [rev2.7.1].

**This list deliberately names no epoch number [rev2.12].** Each constant
carries its own numbered history comment recording which delivery took which
value — `SESSION_SCHEMA_EPOCH`'s block in `web/sessions/models.py` and
`SQLITE_SCHEMA_EPOCH`'s in `core/landscape/schema.py` — and the operator's
authority is the compatibility record in
[docs/runbooks/aws-ecs-deployment.md](../runbooks/aws-ecs-deployment.md).
A literal repeated here can only drift, and it did: until 2026-09-07 these
two entries read "49 → 50" and "36 → 37", while the sessions bump this
delivery actually took was **51 → 52** — the coordination substrate claimed
51 on the release line first and the sentinel is exact equality, so the
number this spec predicted was never available. Nobody re-derived it after
that landed. The Landscape half, 36 → 37, happened to stay true, which is
the more instructive half: a list where one number silently rots and its
neighbour silently survives gives a reader no way to tell which is which.

Three source comments and one test docstring repeat that same wrong 50
(`web/sessions/models.py` at the identity-substrate and workflow-governance
section headers, `core/landscape/schema.py`'s epoch-37 entry where it names
the paired sessions window, and
`tests/unit/web/sessions/test_identity_tables_schema.py`'s module docstring).
They are stale for the same reason and are tracked separately. Note what
their existence did while this spec also carried 50: a reader checking the
spec against the tree found agreement and concluded the spec was right. Two
records agreeing is not corroboration when one was copied from the other,
and the cross-check that should have caught this instead confirmed it.

### Two epochs, one window [rev2]

Landscape compares declared CHECK text against the reflected constraint
structurally, so the widened constraints trip its schema validator exactly as
the 2026-08-14 index change did. Both epochs bump in the same delivery and
are cut over in one service-stop window per
`docs/runbooks/staging-session-db-recreation.md`.

Cutover by deployment:

- **ECS (Postgres, both stores):** archive/export required evidence, drop,
  recreate, `--init-schema`, compatibility record updated with
  `rollback_permitted: false` and this delivery's two epochs — **copy those
  two numbers from the worked compatibility record in
  [docs/runbooks/aws-ecs-deployment.md](../runbooks/aws-ecs-deployment.md)
  (§Bound release/schema compatibility record), never from this spec
  [rev2.11].** That record is the artefact the acceptance gate re-reads and
  asserts against; epoch literals pinned here have drifted from
  `SESSION_SCHEMA_EPOCH` and `SQLITE_SCHEMA_EPOCH` before.
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
| username | text | display only, non-blank; changes update the row and write an audit row. **A `pending` row has no profile yet (`raw_claims_json` is taken at activation) and a pre-provisioned row has never logged in, so `username` is the IdP `subject` until a login supplies better [rev2.8].** That keeps the column NOT NULL and non-blank on every path, and it is what the admin sees in the pending queue. |
| display_name | text null | |
| email | text null | |
| organisation_id | text null | VANguard ABN; null elsewhere |
| raw_claims_json | text null | bounded 16 KiB; declared profile keys plus `iss aud iat exp`; `groups`, `_claim_*`, `picture`, `at_hash`, `nonce` stripped; forensics only, never returned by any API. **Taken at activation, not at first sight [rev2.2]:** a `pending` row holds only provider, subject, and organisation_id, so the container does not accumulate profile PII of people who merely tried. Never-activated `pending` rows are purged on the same lazy schedule [rev2.8], evaluated when an admin lists pending identities, after `identity_pending_retention_days` (default 90, optional setting). No background task. |
| subject_email_at_first_seen | text null | D10 detection |
| rebound_at | datetime null | D10 detection: verified email changed under the same subject |
| first_seen_at | datetime | row creation, including for a pre-provisioned row nobody has used [rev2.8] |
| last_login_at | datetime null | **nullable [rev2.8]:** a pre-provisioned or never-used identity has no login to stamp, and inventing one would falsify the dormancy window R9 measures |
| access_state | text | CHECK `('pending','active','disabled')`; default `pending` (D12). Local follows `registration_mode`: `open` activates on registration, otherwise `pending`. **A pre-existing local user's first login counts as their registration for this rule [rev2.10].** `auth.db` is never recreated by the reset runbook, so after the epoch pass every existing local account authenticates with valid credentials and no identity row — that path, not registration, is the normal one. Under `open` the deployment has already declared that anyone may admit themselves, so holding back the people who did so before this table existed while admitting every newcomer instantly would be incoherent; under any other mode they land `pending` and D21 re-admission is how the known cohort is cleared. **An existing row is never downgraded and never upgraded by a login:** a pre-provisioned `active` row survives a closed deployment, a `pending` row is not escapable by logging in again, and a `disabled` row stays disabled — re-authenticating is not an appeal. Read on **every request** in `get_current_user` [rev2.2], so revocation latency is one request, not the token lifetime. |
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
  container has zero active human admins**.
- The operator CLI (`elspeth composer users bootstrap-admin`) does the same
  thing on demand.

**Both are gated on the same live count, and it is a count, not a lifecycle
fact [rev2.11].** `bootstrap_admin` refuses when
`_active_human_admin_count(...) > 0`, re-evaluated inside the transaction
under the population lock; no durable "has ever been bootstrapped" flag
exists in the schema, and no column could answer that question. Two
consequences the earlier text had backwards:

- The seed **re-arms**. It is inert *while* an active human admin exists, not
  permanently. Should the container fall back to zero — the sole admin
  disabled, deleted, or offboarded — every subject still listed self-grants
  `admin` at their next login. A deployment must therefore DELETE the setting
  once the first administrator is activated; leaving it is a standing grant
  in all but name (elspeth-f4e69fe3bc).
- The CLI is **not** the path once an admin exists. It calls the same guarded
  method and is refused under the identical condition, so it recovers only
  the zero-admin case — the same case in which the seed re-arms. It is
  preferable to the seed because it leaves nothing behind in configuration,
  not because it reaches further. A lockout where the admin row is active but
  that person can no longer authenticate is recoverable by neither, and needs
  direct work against the sessions store.

An earlier revision said "once one exists the list is inert, so it never
becomes a standing grant", and that lockout recovery is "**not** a config
edit" [rev2.2]. The first is false whenever the count returns to zero; the
second is true only while an admin exists, which is precisely the state in
which nobody is locked out. R5 counts only *active human* identities with an
unexpired, unrevoked `admin` role. Grant and revoke are admin-only in this
delivery; delegated administration is phase 4.

The D20 walk is proved at UNIT level. An integration-level pin — a fresh
`--init-schema` store plus one listed subject, walked through
`start → callback → complete`, yielding a session token and a role list of
exactly `admin` — is **outstanding work, not a shipped test**
(elspeth-b9a109f9f0). rev2.7 wrote it in the present tense as "pinned by an
integration test"; no such test exists in the tree [rev2.11].

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
  `library_accepted`, `library_rejected`, `library_deprecated` [rev2.8],
  `library_recalled`, `quota_set`, `quota_exceeded`. `quota_set` and
  `quota_exceeded` carry `dimension` (`tokens` or `storage`), the cap, the
  ceiling in force, and the measured usage in `metadata_json` [rev2.5].
  The three library values close a real gap: curator acceptance is the act
  that makes an entry readable deployment-wide, and it had no event type,
  while `library_entries` already carries `rejected_at` and `deprecated_at`
  columns whose transitions nothing recorded. This CHECK is closed, so
  missing a value is a self-inflicted outage under R4, which refuses the
  mutation whose audit write failed — the list has to be right in this epoch.
- **Authorization denials need no new event type [rev2.8].** `auth_failure`
  exists, `failure_category` is an unconstrained `String(64)`, and
  `metadata_json` is free-form, so `{route, required_role}` under
  `failure_category='authz_denied'` is writable today with no schema change.
  Business-rule refusals (R5, R7, R8, R10) keep their own categories and are
  **not** filed as authorization denials: an authorized caller hitting a rule
  is not an escalation attempt, and conflating them poisons the audit view.
- `calls` gains nullable `prompt_tokens`, `completion_tokens`,
  `cached_prompt_tokens`, `reasoning_tokens` written from the provider's
  `TokenUsage` at call-record time **[rev2.1]**. Measured 2026-09-02: the
  `calls` table stores only request/response hashes and refs; LLM token
  counts live inside the response payload blob and are not queryable, and
  the MCP "LLM usage report" counts pipeline row-tokens, not LLM tokens.
  So "tokens used in runs" is NOT exposed today; these columns expose it.
  **None of the four was added, and the Landscape epoch has cut [rev2.12].**
  `core/landscape/schema.py`'s `calls_table` carries none of them, and the
  epoch-37 history entry records what actually rode that window —
  `auth_events.identity_id`, the `event_type` widening, the provider
  discriminator — with no `calls` change. The 2026-09-02 measurement above
  therefore still describes the tree exactly: run token counts remain
  unqueryable. Note what made this hard to see: `contracts/token_usage.py`
  defines all four field names on `TokenUsage`, so a tree-wide grep for them
  succeeds and reads as confirmation. The contract exists; nothing persists
  it. Tracked as elspeth-255ae1a544.
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
| oidc (Cognito) | `sso_issuer` | issuer + `sso_endpoint_origins` | none | `preferred_username` → `cognito:username` → `sub` | no | confidential app client with the ELSPETH callback URL; on AWS the repository's Terraform creates it in `upgrade` mode with `generate_secret = true` and passes the minted secret to the task by ARN [rev2.11] |
| entra | derived from tenant | `login.microsoftonline.com` | `tid` | `preferred_username` → `sub` | no | groups and roles are not collected (D17); the group-overage check is gone with them |
| vanguard | `sso_issuer` | same as issuer | none | `sub` (email today) | yes | `given_name`, `family_name`, `abn` → `organisation_id`; display name from name parts |
| google | `https://accounts.google.com` (the bare `accounts.google.com` form is rejected) | the four Google origins above | `email_verified` true; `hd` = `google_hosted_domain` (absent for non-Workspace accounts, fails closed) | `email` → `sub` | no | refuses to start without a hosted domain |

### VANguard facts measured 2026-09-02

Issuer `https://d2www26g84civw.cloudfront.net/simplesaml/module.php/oidc`;
`jwks_uri`, authorization, token, and userinfo endpoints all on the issuer
origin; `S256`; `RS256`; `token_endpoint_auth_methods_supported` =
`client_secret_post`, `client_secret_basic`, `private_key_jwt`; no
`claims_supported` published.

### VANguard live confirmation [rev2.11]

**[rev2.11: this read "spike (before the profile is written)". The profile is
written — it keys on `sub` and calls userinfo — so these are the assumptions it
already ships on, and a deployment holding a real confidential client and token
pair confirms them. It is not a gate on writing the profile.]** In this order:

1. Does the ID token carry any **stable, non-email subject**? (D10 hinges
   on it. If yes, the profile keys on it and `email` is a claim.)
2. Exact ID-token claim set (`nonce` presence, `aud` shape).
3. Exact userinfo body (`given_name`, `family_name`, `abn` key names/types).
4. Whether the JWKS entries carry `alg`.
5. Whether the token endpoint accepts `client_secret_basic` for that client.

A real token pair, once a deployment has one, can be added as a fixture
(redacted signature, pinned claims). It is an addition to the synthetic
fixtures, never a precondition for them [rev2.11].

### Google facts measured 2026-09-02 (by review)

Discovery: authorization `accounts.google.com/o/oauth2/v2/auth`, token
`oauth2.googleapis.com/token`, jwks `www.googleapis.com/oauth2/v3/certs`,
userinfo `openidconnect.googleapis.com/v1/userinfo`. `claims_supported`
does not list `hd`; it is emitted for Workspace accounts only. A live check
needs a client from whoever owns the Workspace domain, which is a deployment
errand; the profile and its tests do not wait for one [rev2.11].

## Refusals [rev2]

- **R1.** Refuse a self-edge in `identity_relationships`.
- **R2.** Refuse to run when no `approved` row matches the compiled binding
  tuple, or the matching row is `superseded` or `revoked`. **Delivered in
  this sprint** (phase 4), not future [rev2.2]; the "Send for approval"
  affordance must not exist before this refusal does.
- **R3.** Refuse, and record `rebound_at`, a login whose `(provider, subject)`
  resolves to an existing identity while the verified email differs from
  `subject_email_at_first_seen`, until an admin re-enables the identity.
  **The refusal also sets state [rev2.8, D32].** Refusing the login alone
  leaves outstanding tokens refreshing for the whole refresh chain, which is
  the window the refusal exists to close, so R3 sets
  `access_state='disabled'`, `disable_reason='rebound'`, actor `system`, and
  writes `identity_disabled`. Three bindings on that:
  - **It honours R5.** On the last active human admin, R3 refuses the login,
    writes the audit row, and leaves the identity `active` rather than
    bricking the container into C2's lockout. The audit row is the signal.
  - **It does not run the admin-disable cascade.** Edge revocation is
    unrecoverable — `identity_relationships` keeps `revoked_at` and re-enable
    restores nothing — and this fires most often on a marriage or a rename.
    The state change plus the audit row is the whole action.
  - **Re-enable updates `subject_email_at_first_seen`** to the new verified
    email, or R3 re-trips on the next login forever.
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
  **This is a route-layer refusal, not a CHECK [rev2.8].** It reads other
  rows of `identity_roles`, and no dialect can express a cross-row invariant
  in a CHECK; the same is true of the `identities.kind` restriction, which
  reads across tables. Both evaluate in the **same transaction** as the role
  insert, under `SELECT … FOR UPDATE` on the target `identities` row, at every
  writer: `POST roles`, `activate`, pre-provisioning when it takes a role, and
  **the D20 operator CLI**, which writes a role row and is therefore a real
  enforcement site. No trigger. Tested in both grant orders, plus a mutation
  case seeding a *revoked* workload row beside a live `admin` row, which must
  not refuse.
- **R9.** [rev2.2] Refuse a login for an identity dormant longer than the
  container's dormancy window: it drops to `pending` with
  `disable_reason='dormant'`, writes `identity_disabled`, and needs an admin
  to re-activate. This is the recycled-mailbox case R3 cannot see because
  the email did not change. **R9 carries R5's last-admin exemption [rev2.8,
  D34]:** it never re-pends the last active human admin. R5 guards only the
  disable *route*, so without this a single-admin container reaches zero
  active admins at day 91 by doing nothing, and the seed cannot re-fire
  because it is first-login-only. The dormant sole admin logs in, is not
  re-pended, and an `auth_events` row records the exemption.
- **R10.** [rev2.2] Refuse a service identity's admin write that lacks
  `on_behalf_of` and `console_request_id`; refuse those keys from a human
  identity.
- **R11.** [rev2.2] Refuse to enable approval, attestation, and library
  enforcement when `provider=local` and `registration_mode=open`, because
  one human can then hold many identities and every ≠ CHECK is defeatable.
  **R11 refuses at startup, loudly [rev2.8, D30].** It is a readiness failure
  naming both settings, not enforcement quietly switched off: a silent
  off-switch makes a good-faith test suite go green against nothing, and the
  shared route fixture at `tests/unit/web/conftest.py` sits in exactly this
  combination. The workflow-governance tests therefore configure a closed
  local deployment (`registration_mode` not `open`), which is a supported
  configuration and the one the tests must run in.
- **R12.** [rev2.2] Refuse to resolve a shareable review token for a
  requesting identity that is not `active`.
- **R13.** [rev2.5, restated rev2.8] Refuse **any path that would add a live
  `blobs` row for the identity** when the identity's live blob total would
  exceed its `storage_bytes` or the container ceiling, before any byte
  reaches disk; write `quota_exceeded` with `dimension=storage`. Refuse when
  the accounting query fails. The response names the cap and the current
  usage so the user can delete blobs to recover; the existing per-session
  error keeps its shape.

  **The four byte-admitting sites [rev2.8, D24].** "Upload" named one of
  four, so an identity at its cap could previously fork its own session
  repeatedly and never be refused. All four enforce:
  1. multipart upload;
  2. inline upload / inline custody;
  3. run-output finalize, which writes result blobs;
  4. `copy_blobs_for_fork`, which physically copies bytes.

  Site 4 refuses **before** the copy loop starts, so a refused fork leaves no
  half-populated child; the deliberate `missing_bytes == 0` idempotent-replay
  path stays exempt, because it admits no new bytes. Blobs in archived
  sessions count, and rows in `pending` or `error` state count: they occupy
  disk.

  **The bound is eventually consistent, not exact [rev2.8, D24].** The
  per-session lock is keyed on `session_id` alone, so two uploads into two
  sessions of one identity take non-conflicting locks and both may pass; the
  earlier claim that they "cannot both pass" was false on PostgreSQL, the
  production dialect. Making it exact needs an identity-scoped lock acquired
  before the session lock on every path, held across the fork copy loop,
  which would serialise every other upload by that identity for the duration
  of a fork. The overshoot is bounded by one concurrent admission per extra
  session and self-corrects on the next check, which is the same guarantee
  the token dimension already gives. Reconsider only if measurement shows a
  real overshoot.

- **R14.** [rev2.8] Refuse an LLM call — composer or run — that would take
  the identity over its `tokens_per_day` or the container ceiling; write
  `quota_exceeded` with `dimension=tokens`. Refuse when the accounting query
  fails, and refuse when no applicable policy row exists. The token dimension
  was previously specified only in a table cell; it is a numbered refusal
  because this project derives its mutation tests from numbered refusals, and
  a control with no number gets no adversarial test. Eventually consistent on
  the same terms as R13. The day boundary is UTC midnight, named in the test.

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
  **That view is authorized per request, and mints no token [rev2.8, D27].**
  The predicate follows from the role-based eligibility already ruled at
  rev2.2: the caller holds an active `approver` (or `reviewer`) role, the
  caller is not `requested_by_identity_id`, and a live `approvals` or
  `review_requests` row exists for that `(session_id, state_id)`. It is
  evaluated on every request over `identity_roles` and the request row,
  serving the same frozen projection. It must **not** reuse the existing
  shareable-review transport, which is a 30-day unrevocable bearer
  capability whose resolve route writes no audit row and never reads the
  caller identity it accepts — a capability, not an authorization. The read
  writes an `audit_access_log` row under a new `writer_principal` value: one
  identity reading another's work is the disclosure an auditor will ask
  about, and adding the value after this delivery's epoch cut costs exactly
  the window this spec is structured to take only once.
  **It was not added, and that cut has happened [rev2.12].** The column is
  still `('audit_grade_view', 'admin_tool')` in `web/sessions/models.py`,
  whose own comment calls those two "the entire universe of" the enum and
  requires a design review, a destructive session-DB recreation and a spec
  amendment to widen it. Everything else in that window rode it — the four
  identity tables and all five workflow governance tables shipped — so this
  is a single stranded item, not an unbuilt half. Read the paragraph above
  as the requirement it always was; do not read it as a description of the
  tree. Tracked as elspeth-e6c2d254b2.
  *Sent*: my own requests with their state, the decider, the decision
  note, and when; opening one sets `decision_seen_at`. A badge on the
  navigation shows unread counts from one summary endpoint. It needs its own
  timer: the "existing session-list cadence" is not a poll cadence at all —
  that loader is event-driven [rev2.8]. One timer for the badge, and no
  second one. Notification stops at the badge: there is
  no email or push transport in this sprint (a seam, see §Future seams).

## Testing [rev2]

- **Unit.** One fixture module per profile with a signed ID token. Every
  profile's fixtures are synthetic, VANguard's included [rev2.11: this said
  "and the real VANguard pair from the spike", which made a unit fixture wait
  on a live client; the tests were written without one]. Every claim check
  positive and
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

### Workflow governance [rev2.8, D30]

Everything above tests the login path. The workflow half ships enforcement —
R2 refuses a run, R13 and R14 refuse spend — and had no named test at all,
which is how a sprint ships tables that nothing exercises. These land before
phase 4 closes, not after.

- **Per refusal that actually refuses** (R2, R7, R8, R11, R13, R14, and R9,
  which is reachable now that `identity_dormancy_days` ships): a fire test
  that proves the refusal happens, **and** a mutation-derivation test that
  proves the guard derives from its authority rather than from a coincidence.
  This project's discipline keys off numbered refusals, which is why the token
  quota became R14 rather than staying a sentence in a table cell.
- **R2 through the real binding tuple.** One integration test that compiles an
  actual binding and drives the execute gate with it, not a hand-built tuple.
  A gate tested against a fixture proves the fixture.
- **The full round trip.** Request with a note, decide with a note, the
  requester sees the decision and the note, the badge clears. Plus the losing
  half of the concurrent-decide guard.
- **Both quota dimensions.** A storage refusal at each of R13's four
  byte-admitting sites, and a token refusal, each asserting the
  `quota_exceeded` row carries its `dimension`, cap, ceiling and usage. A
  day-boundary rollover case with the clock named explicitly (UTC midnight).
- **The separation rules as violations:** author = approver, curator =
  publisher, reviewer = author, and R8 in both grant orders including the
  revoked-workload-role case that must *not* refuse.
- **A seeded pre-existing cycle** in `identity_relationships`, since R7 guards
  the insert but says nothing about data that predates it.
- **Not tested, and why:** R10 has no reachable path because no
  service-credential mechanism ships this sprint; attestation has no refusal
  to mutate, by its own design as a ledger; and "flex teams" has no test
  because it is a property of two live deployments, not of code.
- **The suite runs against a closed local deployment.** R11 refuses
  enforcement under `local` plus open registration, and the shared route
  fixture sits in exactly that combination, so tests written the obvious way
  would pass against enforcement that was never on.

## Rollout order [rev2]

1. **Deployment inputs, not a project step [rev2.11]:** each deployment
   registers its own confidential client with its own provider and supplies
   the id, secret and issuer as configuration (§Deployment-time inputs). On
   AWS the repository's own Terraform creates the Cognito client and mints its
   secret, so there is nothing to land by hand. Steps 2 to 5 do not wait on
   any of this — the profiles and the login path are proved against the
   in-process fake IdP — and a real client gates only the live checks in
   steps 6 and 7. **[rev2.11: this read "Operator, first: register the
   confidential Cognito client …", which made a deployer's errand a
   precondition of the build and produced a ruling that had to be reversed.]**
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

## Future seams

**The heading used to say "recorded, not built" and several bullets below are
built this sprint [rev2.8].** Rather than move them to a fourth list that has
to stay in sync with the Decisions table, §Workflow tables and the milestone,
each in-sprint bullet now carries its ruling and its owning step inline. Read
the bullet, not the heading.

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
- **`auth_events` long-term retention:** a separate product question. The
  *export* half of this bullet was stale — it is owned in the plan already
  [rev2.8].

## Workflow tables (sessions store, this delivery's epoch) — "for but not with" [rev2.1, D13]

Basic columns only. Every table keys on `identity_id`. Every mutation writes
its `auth_events` row before responding. Fleshed out later without a new
epoch only by adding nullable columns; anything needing a CHECK change is a
deliberate epoch bump.

**Session foreign keys declare `ondelete` explicitly [rev2.8, D29].** Silence
is not neutral: SQLAlchemy emits `NO ACTION`, and with `PRAGMA foreign_keys=ON`
enforced at startup that ships the `RESTRICT` branch by accident. The rule is
`RESTRICT`, chosen deliberately, **plus** an extension of
`durable_history_exists` to count `approvals`, `review_attestations` and
`library_entries`. That predicate today counts only runs, composer completion
events and forks, so `archive_session` would physically delete a session whose
only history is an approval, silently discarding audit-bearing rows; with the
extension, archive refuses instead. `token_usage_ledger` is excluded from the
predicate — it is an accounting index, not audit truth, and its `session_id`
is nullable. `library_entries.published_from_session_id` becomes a
**provenance column, not an FK**, exactly as `forked_from_session_id` already
is, so a published entry outlives the staging session it came from.

**Actor columns that no identity fills [rev2.8, D28].** The container-ceiling
`quota_policies` row is derived from configuration and has no granting
identity, so `set_by_identity_id` is a **nullable** FK beside a closed
`set_by_actor` CHECK over `('identity', 'config', 'operator', 'system')`,
mirroring the `writer_principal` pattern already used in this codebase. A
non-null FK with an invented placeholder identity would put a fake row in the
table R5 counts.

| table | columns | notes |
|-------|---------|-------|
| approvals | approval_id PK; session_id FK; state_id; binding_json (`config_hash`, `canonical_version`, `runtime_val_manifest_sha256`, `openrouter_catalog_sha256`, **`binding_generation_fingerprint`, `policy_hash`** [rev2.2]); requested_by_identity_id FK; approver_identity_id FK; requested_at; decided_at NULL; decision NULL CHECK `('approved','rejected','revoked','superseded')` [rev2.2]; required_count int default 1; request_note NULL [rev2.6]; decision_seen_at NULL [rev2.6] | One open request per `(session_id, state_id)`. **Mailbox [rev2.6]:** `request_note` is the requester's message to the approver ("please approve, it's for entirely legitimate business"); the approver's reply travels in `approval_decisions.note`. Both are bounded plain text (4 KiB), rendered as text, never as markup, and both are part of the audit record. `decision_seen_at` is set when the requester opens the decided request, so the badge can clear; it is a UI convenience, never a control. **A negative decision must carry a note [rev2.8]:** the route refuses `decision='rejected'` with a blank or missing `note`, because the note is the requester's only channel for learning why, and an empty rejection turns the mailbox round trip into a dead end. Positive decisions and `withdrawn` leave it optional. Author ≠ approver (CHECK). **Approver eligibility is role-based [rev2.2]:** any identity holding an active `approver` role in this container who is not the author may decide; the author's active `approver` edge only supplies the default suggestion in the picker. This is what gives the lead's own work an approver and gives leave cover without touching the tree. Any new `state_id` marks the open request `superseded`. **Two eligible approvers may open the same request, so the decide route is a conditional write [rev2.8]:** it updates `WHERE decision IS NULL` — *not* `WHERE decided_at IS NULL`, because `superseded` and `revoked` set `decision` without being stated to stamp `decided_at`, and guarding on the timestamp would let a superseded request be overwritten to `approved` against a stale binding, defeating R2's own second clause. The loser gets one named `error_type` carrying the current state, rendered by the frontend; one type, not three, since the `superseded` and `revoked` arms are not "already decided by someone". The `approval_decisions` insert, the `approvals` update and the R4 audit write happen in **one transaction**, or the lost update simply reappears between the two tables. This implements quorum 1; `required_count > 1` stays reserved and unenforced. Execute refuses (409, distinct `error_type`) unless an `approved` row matches the compiled binding (R2, **delivered in this sprint**, phase 4). `binding_generation_fingerprint` is included because `config_hash` records profile aliases, not the buckets or credentials they resolve to; without it an approval survives an operator repointing an alias. `snapshot_hash` is deliberately excluded (it embeds the principal scope and would never match across approver and author). Pinned by a test that fails if a new field enters `WebPluginPolicyEvidence` without a tuple decision. |
| approval_decisions | decision_id PK; approval_id FK; decided_by_identity_id FK; decided_at; decision CHECK `('approved','rejected')`; note NULL | [rev2.2] One row per deciding identity, so `required_count > 1` is a count over rows, not a schema change. Dual control: nullable `quota_policies.dual_control_above_tokens` and a per-container list of secret names / plugins whose wiring raises `required_count` to 2 (reserved, not enforced). |
| review_attestations | attestation_id PK; session_id FK; state_id; payload_digest; reviewer_identity_id FK; attested_at; verdict CHECK `('signed_off','changes_requested','withdrawn')` [rev2.2]; note NULL | Append-only; `note` bounded at 4 KiB like the approval notes, and required non-blank when `verdict='changes_requested'` [rev2.8]. Reviewer ≠ author (CHECK); the reviewer must hold an active `reviewer` role [rev2.3]. **"Reviewer ≠ author" needs the author on the row to be a CHECK at all [rev2.8]:** add `author_identity_id`, denormalised as an **immutable snapshot taken at attestation time**, never a mirror of `sessions.identity_id`. A mirror would be a second source of truth that drifts when a session changes hands, the failure this codebase has already documented elsewhere. With the snapshot the rule is a single-row CHECK; without it, it is a cross-table invariant no dialect can express. **Named "reviewer attestations" everywhere — schema, API, UI [rev2.2].** It is a ledger, not a control: nothing refuses on it. The phrase "two-person rule" is reserved for something that refuses; a UI must never say "two-person rule satisfied" over an unenforced count. |
| review_requests **[rev2.8, D26]** | request_id PK; session_id FK; state_id; requested_by_identity_id FK; reviewer_identity_id NULL FK; requested_at; cancelled_at NULL; request_note NULL (4 KiB) | The rev2.6 mailbox promises an Inbox of "review requests addressed to me" and nothing recorded one: `review_attestations` is append-only with a non-null reviewer and exists only once a review has *happened*. A sibling table, rather than nullable columns and a `requested` verdict on the attestation ledger, because that would contradict the ledger's own append-only, "not a control" design and would fill the approver's audit view with requests nobody ever completed. `reviewer_identity_id` NULL means "any active `reviewer`", matching the role-based eligibility already ruled for approvals. Closed by an attestation on the same `(session_id, state_id)`, or by `cancelled_at`. The badge counts open rows addressed to the caller plus open unaddressed rows the caller is eligible for. |
| library_entries | entry_id PK; published_from_session_id provenance column (**not an FK** [rev2.8, D29]); payload_digest; compartment_id; title; version int; published_by_identity_id FK; curated_by_identity_id NULL FK; published_at; accepted_at NULL; rejected_at NULL; rejection_note NULL; deprecated_at NULL; recalled_at NULL; note NULL | Frozen, content-addressed. **A library entry is the public projection (`generate_public_yaml` shape), never a session reference, and it is config-only [rev2.2]:** publishing a pipeline that reads an uploaded blob is refused with a named `error_type` ("publish a profile-bound source instead"), because blob custody proves same-principal on fork and a cross-user fork of a blob-backed source cannot copy the blob without becoming an intra-container exfiltration path. Forking a library entry instantiates the projection into the forker's own staging session; `forked_from_session_id` points at that staging session and the entry's `payload_digest` carries provenance. Visible deployment-wide once `accepted_at` is set by a `curator`. Curator ≠ publisher (CHECK). Recall flags, never deletes. `rejection_note` is required non-blank on rejection and bounded at 4 KiB [rev2.8]. `library_published` audit rows carry `payload_digest` and `compartment_id` so the same artifact appearing in two containers is detectable later. |
| quota_policies | policy_id PK; identity_id NULL FK; tokens_per_day int; storage_bytes int [rev2.5, D18]; dual_control_above_tokens NULL int; set_by_identity_id FK; set_at; revoked_at NULL | Per person (D11) **plus the container ceiling row (`identity_id` NULL) shipped now [rev2.2]**, because activation otherwise grants unbounded spend on the container's shared LLM credential. Two partial uniques, both dialects: active per identity, and active `WHERE identity_id IS NULL` (NULLs are distinct for uniqueness in Postgres, so one predicate does not cover both). **Every path that makes an identity `active` writes the per-identity row** from `quota_default_tokens_per_day` and `quota_default_storage_bytes` [rev2.8, D31] — `POST activate`, local registration under `registration_mode=open`, pre-provisioning, the D20 bootstrap seed and operator CLI, and the D21 cutover re-admission. "At activation" was true of one path of six, and on the other five the identity's first run or upload refused with the audit record this spec defines as evidence of corruption. No applicable policy still refuses; it is now genuinely unreachable. Every `quota_set` / `quota_exceeded` event records the cap and the ceiling in force. **Storage [rev2.5]:** `storage_bytes` is a standing level, not a daily rate; usage is `SUM(blobs.size_bytes)` joined through `sessions.identity_id` over live rows (deleted blobs leave no row), evaluated at each of the four byte-admitting sites R13 enumerates. The bound is eventually consistent, not exact: the existing blob lock is keyed on `session_id` alone, so two sessions of one identity do not serialise against each other [rev2.8, D24]. Blobs created by the system on the identity's behalf (inline custody, fork copies) count against the identity; the `system` exemption applies to tokens only. Admin set/revoke is one route for both dimensions. |
| token_usage_ledger | entry_id PK; identity_id NULL FK; source CHECK `('composer','run','auto_title','system')` [rev2.2]; session_id NULL FK; run_id NULL; model; prompt_tokens; completion_tokens; cached_prompt_tokens NULL; reasoning_tokens NULL; recorded_at | Operational accounting index, not audit truth (Landscape `calls` is). Composer writes one row per LLM call from `ComposerLLMCall.usage` (today persisted only inside JSON audit payloads, not queryable). Auto-titling (a paid background call per first message, which its own docstring flags as bypassing rate limits) writes `auto_title`; the boot probe writes `system` with `identity_id` NULL. Runs write one row per run at finalisation from the new `calls` token columns — **a source that does not exist [rev2.12]**, so the `run` arm of this table's own `source` CHECK has no data path (elspeth-255ae1a544). The table shipped; that arm did not. Since the quota check below is a `SUM` over this ledger and the rule is "accounting unavailable ⇒ refuse", whoever wires the run arm decides whether run spend has been silently uncounted or is refusing — the answer is not settled here, and must not be assumed from this row. Quota check = `SUM` over the ledger for the identity in the current UTC day, evaluated at execute and at composer turn start only; post-response spend lands in the next window. Over quota refuses and writes `quota_exceeded`. Accounting unavailable ⇒ refuse (fail closed); the `system` arm is exempt from the check. |

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

## Deployment-time inputs [rev2.11]

Each of the four IdP profiles needs a client registration with its provider,
and each one is supplied by whoever deploys ELSPETH, for their own
environment. This repository is public and stores no provider credential, so a
registration is configuration a deployment brings — `sso_client_id`,
`sso_client_secret`, and the profile's issuer, tenant or hosted domain — not a
dependency the project waits on. No source file, and no unit or integration
test, needs one: every profile is proved against the in-process fake IdP
(`tests/helpers/fake_idp.py`), which signs with a real RSA key and serves
discovery, JWKS, token and the userinfo leg VANguard alone calls. The live
acceptance layer needs one by definition (§Testing, the **Live** bullet) —
`tests/e2e/aws-ecs-oidc.staging.spec.ts` is a checked-in test whose
`playwright.oidc.config.ts` refuses to start without `STAGING_BASE_URL`, and
it drives a deployed stack, not a fixture. And `auth_provider=local` needs no
registration at all. **[rev2.11 replaces
"External dependencies", which read these as work owed to the project before
it could proceed; that framing produced a ruling that had to be reversed, see
§Rollout order step 1.]**

The one thing a real client gates is **live verification against a running
provider**. Unit and integration coverage runs entirely against the in-process
fake IdP (§Testing) — it serves discovery, JWKS, token and userinfo and signs
with its own key — so every profile, claim check, origin policy and refusal is
provable with no account anywhere.

| profile | who registers the client |
|---------|--------------------------|
| oidc (Cognito) | the deployment's own Terraform. In `upgrade` deployment mode `aws_cognito_user_pool_client` carries `generate_secret = true`, so Cognito mints the secret, Terraform reads it as an attribute into Secrets Manager, and the task receives it by ARN reference. There is no console step and no secret in this repository. A `first`-mode cold install creates no client and runs on `local`. |
| entra | the tenant's own Entra administrator |
| vanguard | the operating organisation, on the bridge's ABN-gated admin page, carrying ELSPETH's callback URL |
| google | whoever owns the Workspace domain, as a Google Cloud OAuth client |

Whatever issues it, a registration must carry the redirect URI
`public_base_url` + `SSO_CALLBACK_PATH` (`/api/auth/sso/callback`), matched
exactly, and must be confidential: the backend redeems the code, so a public
client leaves the deployment unable to authenticate anyone (D2).
