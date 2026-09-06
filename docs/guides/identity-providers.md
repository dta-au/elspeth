# Identity Providers

How to let your own staff sign in to an ELSPETH deployment with your
organisation's identity provider, instead of with local accounts.

## Overview

ELSPETH authenticates a browser in one of two ways. Local authentication keeps
usernames and password hashes in the deployment's own database. Single sign-on
delegates the question of "who is this person" to an identity provider you
already run, and ELSPETH accepts the answer.

Registering a client with an identity provider is a **deployment-time action**.
It is something you do in your own tenant, for your own environment. This
repository ships no client, no secret, and no tenant: nothing here is bound to
anyone else's directory, and nothing about building, testing, or running
ELSPETH waits on a provider.

Four provider profiles are implemented, plus local authentication:

| `auth_provider` | What it is |
| --------------- | ---------- |
| `local` | Usernames and password hashes in the deployment's own database. The default. |
| `oidc` | Any standards-compliant OpenID Connect provider, including AWS Cognito. |
| `entra` | Microsoft Entra ID. |
| `google` | Google Workspace, restricted to one hosted domain. |
| `vanguard` | VANguard, the Australian Government's identity exchange. |

Selecting a provider is a configuration change and a restart, never a rebuild.
The container image carries no credentials and the profile carries no
deployment facts.

---

## Choosing between local authentication and a provider

Use **local authentication** when:

- You are developing, evaluating, or running the tutorial.
- You are performing a cold install and need to sign in to a deployment that
  does not have a client registered yet. This is the normal first step: bring
  the deployment up on `local`, register the client, then switch.
- The deployment is a single-operator instance where a directory would be
  ceremony rather than control.

Local authentication is fully supported, not a degraded mode. It is also the
only mode in which `dev_admin_user` may be set: that setting names the one
local user who gets the in-app user-management surface, and it is refused
outright on any provider deployment.

Use a **provider** when:

- More than one person signs in, and you want joiners and leavers handled by
  your directory rather than by a second list.
- You need multi-factor authentication, conditional access, or session policy
  that your directory already enforces.
- You need sign-in events attributable to a corporate identity rather than to
  a local username.

You cannot mix the two. `auth_provider` names exactly one value. On a provider
deployment the local login route returns 404; on a local deployment the
single sign-on routes return 404.

---

## What ELSPETH requires of any provider

These four requirements are the same for all four profiles. If your provider
can satisfy them, ELSPETH can authenticate against it.

### A confidential client

ELSPETH must be registered as a **confidential client** — one that holds a
client secret — and not as a public, browser, or single-page-application
client.

This is not a formality. ELSPETH redeems the authorization code on the server:
the backend calls the token endpoint itself, authenticating with
`client_secret_basic`, and the browser never performs the exchange. That has
three consequences worth understanding before you register anything.

1. **An intercepted code is not enough to impersonate anyone.** The code
   travels back through the user's browser, which is the one part of the path
   you do not control. Redemption additionally requires the client secret,
   which lives only in the server's memory, and the PKCE verifier, which lives
   only in a sealed cookie belonging to that one login attempt. An attacker who
   captures a callback URL from a log, a proxy, or a shoulder-surfed address
   bar holds a code they cannot spend.
2. **Provider tokens never reach the browser.** The ID token is verified
   server-side and discarded. What the browser receives is a single-use handoff
   code delivered in the URL *fragment* — which browsers do not transmit, so it
   does not appear in load-balancer or web-server access logs — and it exchanges
   that for ELSPETH's own session token.
3. **A public client would leave the deployment unable to authenticate
   anyone.** `sso_client_secret` is required by every profile, so a client
   registered without a secret cannot be configured here at all.

### The authorization code flow, with PKCE

ELSPETH sends `response_type=code` with `code_challenge_method=S256` and
redeems with `grant_type=authorization_code`. Your provider must support the
authorization code flow and `client_secret_basic` client authentication at the
token endpoint.

It requests three scopes, the same for every profile: `openid`, `profile`,
`email`. ID tokens must be signed with `RS256`; the signature algorithm is
pinned, never read from the token header.

### The exact redirect URI

Register exactly one redirect URI, formed from your `public_base_url` plus the
fixed callback path:

```text
https://<your public base URL>/api/auth/sso/callback
```

Providers match redirect URIs by exact string comparison. Registering the site
root, or a trailing-slash variant, produces a refusal at the callback with
nothing in the browser to explain why. A trailing slash on `public_base_url`
itself is harmless — it is stripped when the URI is built — but the value
registered at the provider must match the URI above character for character.

### The implicit flow must not be enabled

ELSPETH never expects a token delivered through a redirect, and never
validates one. Leaving the implicit flow enabled on the client would allow the
provider to issue tokens straight into the browser at an address ELSPETH
publishes, turning a URL into a bearer credential for a flow the application
does not use. Enable the authorization code grant and nothing else.

---

## The four profiles

Each profile fixes the things that genuinely differ between providers: where
the issuer comes from, which origins its endpoints may be served from, what is
checked in the token beyond standard validation, and whether the profile calls
the userinfo endpoint.

| Profile | Issuer comes from | Extra required setting | Endpoints may be served from | Calls userinfo |
| ------- | ----------------- | ---------------------- | ---------------------------- | -------------- |
| `oidc` | `sso_issuer` | `sso_issuer` | The issuer's origin, plus any origin in the optional `sso_endpoint_origins` | No |
| `entra` | Derived: `https://login.microsoftonline.com/<entra_tenant_id>/v2.0` | `entra_tenant_id` | `https://login.microsoftonline.com` | No |
| `google` | Fixed: `https://accounts.google.com` | `google_hosted_domain` | Google's four published origins | No |
| `vanguard` | `sso_issuer` | `sso_issuer` | The issuer's origin only | Yes |

Additional per-profile behaviour:

| Profile | Extra token check | Signed-in username taken from |
| ------- | ----------------- | ----------------------------- |
| `oidc` | None beyond standard validation | `preferred_username`, else `cognito:username`, else `sub` |
| `entra` | The `tid` claim must equal `entra_tenant_id`; a missing `tid` is refused | `preferred_username`, else `sub` |
| `google` | The email must be verified, and the `hd` claim must equal `google_hosted_domain`; a missing `hd` is refused | `email`, else `sub` |
| `vanguard` | None beyond standard validation | `sub`. Display name is assembled from `given_name` and `family_name`, and `abn` is recorded as the organisation identifier |

Two of these deserve a note.

**Entra derives its issuer from the tenant**, which is why `sso_issuer` is
*forbidden* rather than optional on that profile: accepting both would let the
tenant check and the issuer check point at different directories.

**Google's `hd` check fails closed on absence.** Google emits `hd` for
Workspace accounts only, and it is not in Google's published `claims_supported`,
so a personal Gmail account produces a token with no `hd` at all. If a missing
claim were treated as "no restriction", every Google account in the world would
be a valid login. That is why `google_hosted_domain` is required and has no
default.

---

## Settings every non-local provider needs

Seven settings are required whichever profile you choose, plus that profile's
own extra setting from the table above.

| Setting | What it is for |
| ------- | -------------- |
| `sso_client_id` | The client identifier your provider issued. |
| `sso_client_secret` | That client's secret. Supplied by reference from a secret manager, never written down here. |
| `sso_transaction_secret` | Seals the short-lived login-transaction cookie carrying the PKCE verifier, state, and nonce. Independent of `secret_key`, so rotating one does not invalidate the other. Generate high-entropy random bytes. |
| `public_base_url` | The externally visible origin of this deployment. The redirect URI is built from it, so it must be the address users actually reach. An origin only — a path, query, or fragment is refused. |
| `compartment_id` | The deployment's compartment marking. See below. |
| `quota_default_tokens_per_day` | Daily token allowance written for each identity at activation. Must be greater than zero. |
| `quota_default_storage_bytes` | Storage allowance written for each identity at activation. Must be greater than zero. |

Two optional settings are worth knowing about from the start:

| Setting | What it is for |
| ------- | -------------- |
| `sso_admin_subjects` | Bootstrap only. A JSON array of provider subject identifiers seeded as the first `admin` at first login, and *only* while the deployment has no active human administrator. **Remove the list once that administrator is activated** — see the warning below. |
| `sso_endpoint_origins` | Generic OIDC only. Extra HTTPS origins the discovered endpoints may use, beyond the issuer's own origin. |

> **Remove `sso_admin_subjects` once the first administrator is activated.**
> The seed is gated on a *live* count of active human administrators,
> re-evaluated at every login — not on whether a bootstrap has ever happened.
> While at least one active human administrator exists the list does nothing.
> But if the deployment ever returns to zero — the sole administrator is
> disabled, deleted, or removed during offboarding — every subject still
> listed self-grants `admin` at their next login, bypassing the
> pending-by-default admission entirely. Someone who has since left the
> organisation is still on that list. Treat it as a one-shot seed you delete,
> not a standing configuration entry — `elspeth composer users bootstrap-admin`
> does the same job without leaving anything behind, and
> [Admitting the first person](#admitting-the-first-person) covers both paths
> and what neither of them recovers.

Every setting is supplied as an environment variable named `ELSPETH_WEB__`
followed by the setting name in upper case. Collection-valued settings
(`sso_endpoint_origins`, `sso_admin_subjects`) take a JSON array.

**The two secrets are the exception to how you supply them.**
`sso_client_secret` and `sso_transaction_secret` must arrive by reference from
a secret store — never as a literal in a task definition, a values file, a
compose file, or anything committed to a repository. See
[Keeping the client secret out of the repository](#keeping-the-client-secret-out-of-the-repository)
below for the worked AWS example.

A deployment also needs the settings every ELSPETH web service needs, such as
`secret_key`, `shareable_link_signing_key`, the database URLs, and the composer
limits. Those are covered in the
[Configuration Reference](../reference/configuration.md); this guide covers
only identity.

### The failure mode operators get wrong

**A partial identity configuration fails at settings load.** The container does
not start. It does not come up and report itself unready — it exits before the
application exists.

This surprises people, because most missing-dependency problems in a container
platform show up as a failing health check on a running task. Here the check
happens when settings are constructed, so what you see is a task that starts,
exits immediately, and restarts. The reason is in the exit log, not in a
readiness endpoint:

```text
auth_provider='oidc' requires: sso_issuer, compartment_id
```

The practical consequence: **apply all of a profile's settings in one
revision.** Adding them incrementally means every intermediate revision is a
deployment that cannot boot. There is a readiness check named `auth_mode` that
reports the same missing fields by name, and deployment procedures gate traffic
cutover on it, but a partially configured deployment never gets far enough to
answer it.

The same validation refuses a setting belonging to a different profile, by
name rather than ignoring it:

```text
auth_provider='entra' does not use: sso_issuer (configuring a setting for a different IdP is silently ignored otherwise)
```

An environment variable set to an empty string is treated as not configured,
not as a value. `ELSPETH_WEB__SSO_ISSUER=` in a task definition is refused at
load rather than producing a deployment that cannot complete a login.

---

## Registering the client with your provider

What follows is what to create in your provider's console, and which setting
takes which value. The steps are yours to perform in your own tenant.

### Generic OIDC, worked with AWS Cognito

The `oidc` profile targets any standards-compliant provider. AWS Cognito is
used here as the worked example because this repository ships Terraform that
registers exactly this client, in
`deploy/aws-ecs/terraform/modules/scenario/storage_identity.tf`.

Read that as a worked reference rather than something every deployment gets:
the pool, its domain and the client are each gated on
`deployment_mode == "upgrade"`, and a `first`-mode cold install creates none of
them and comes up on local authentication. Two of the three shipped scenario
roots are `first`. If you are deploying elsewhere, the steps below are yours to
perform in your own tenant.

Create a user pool, a pool domain, and an **app client with a secret**. For
Cognito that is `generate_secret = true`; in other providers it is usually the
difference between a "confidential"/"web application" client and a "public"/
"single-page application" client. Then:

- Allowed OAuth flow: authorization code grant only.
- Allowed scopes: `openid`, `profile`, `email`.
- Callback URL: `https://<your public base URL>/api/auth/sso/callback`, exactly.

Then set, **together with the seven common settings above** — the tables in
this section list only what is specific to each provider, and a deployment
given just these will not start:

| Setting | Value |
| ------- | ----- |
| `auth_provider` | `oidc` |
| `sso_issuer` | The pool issuer, `https://cognito-idp.<region>.amazonaws.com/<user-pool-id>`. It must match the `issuer` in the provider's discovery document exactly. |
| `sso_client_id` | The app client id. |
| `sso_client_secret` | The app client secret, by reference. |
| `sso_endpoint_origins` | `["https://<your-domain-prefix>.auth.<region>.amazoncognito.com"]` |

That last one is why the generic profile exists in this shape. Cognito serves
the authorization and token endpoints from the pool's **hosted domain**, which
is a different origin from the pool issuer. A strict same-origin rule would
refuse a correctly configured Cognito deployment, so this profile — and only
this profile — lets you name the additional origins. A provider whose endpoints
all sit on the issuer origin needs nothing here.

### Microsoft Entra ID

Register an application in your tenant. Under **Certificates & secrets**,
create a client secret; under **Authentication**, add a **Web** platform with
the redirect URI above, and leave both implicit-grant checkboxes clear. A
"Single-page application" platform registration is the wrong kind of client
here.

Then set, together with the seven common settings above:

| Setting | Value |
| ------- | ----- |
| `auth_provider` | `entra` |
| `entra_tenant_id` | Your directory (tenant) ID. |
| `sso_client_id` | The application (client) ID. |
| `sso_client_secret` | The client secret value, by reference. Note the expiry date you chose. |

Do not set `sso_issuer`. It is derived from the tenant, and configuring it is
refused. Every login is additionally checked to have been issued by that
tenant, so a multi-tenant registration cannot be used to sign in from a
directory you did not configure.

### Google Workspace

In a Google Cloud project, create an **OAuth client ID** of type **Web
application**, which is issued with a client secret. Add the redirect URI above
to *Authorised redirect URIs*. Configure the OAuth consent screen as internal
to your organisation.

Then set, together with the seven common settings above:

| Setting | Value |
| ------- | ----- |
| `auth_provider` | `google` |
| `google_hosted_domain` | Your Workspace domain, for example `example.gov.au`. |
| `sso_client_id` | The client ID, ending in `.apps.googleusercontent.com`. |
| `sso_client_secret` | The client secret, by reference. |

Do not set `sso_issuer`; it is fixed at `https://accounts.google.com` and
configuring it is refused. Sign-in additionally requires a verified email
address and a hosted domain matching your setting, so personal Google accounts
are refused even if they reach the consent screen.

### VANguard

VANguard is the Australian Government's identity exchange, used by government
entities to authenticate people to government services. There is no
self-service console: the client is **issued by the operating organisation** to
the agency deploying the service, and the issuer, client id, and client secret
come from that registration. Supply the issuer you were given.

Then set, together with the seven common settings above:

| Setting | Value |
| ------- | ----- |
| `auth_provider` | `vanguard` |
| `sso_issuer` | The issuer URL from your registration. |
| `sso_client_id` | The client identifier from your registration. |
| `sso_client_secret` | The client secret, by reference. |

VANguard is the only profile that calls the userinfo endpoint, because the
name parts and the ABN are not carried in the ID token. Its endpoints must all
be served from the issuer's own origin. The ABN is recorded against the
identity as its organisation identifier.

---

## Keeping the client secret out of the repository

**No client secret belongs in this repository, in a values file, or in a
rendered task definition.** A secret in a rendered deployment artifact is a
secret in every place that artifact is stored, diffed, and logged, which
usually includes a version control system and a deployment history you cannot
redact retrospectively.

Supply it **by reference**. Every container platform can inject a secret at
task start from a secret store, given only an identifier: the platform reads
the value, the process sees an environment variable, and the artifact contains
a pointer.

The AWS Terraform in this repository does exactly that, and is a fair worked
example. Cognito mints the secret at client creation; Terraform reads it as a
resource attribute straight into a Secrets Manager entry; the task definition
carries an ARN:

```json
{
  "name": "ELSPETH_WEB__SSO_CLIENT_SECRET",
  "valueFrom": "arn:aws:secretsmanager:<region>:<account>:secret:<name>:sso_client_secret::"
}
```

No human copies the secret, no console step is required, and the value appears
in no tracked file. The same treatment applies to `sso_transaction_secret`,
which is generated rather than issued.

Where your provider requires a human to copy a secret out of a console — Entra
and Google both do — paste it directly into your secret store, and reference it
from there. Record the expiry date somewhere you will see it before it passes.

One custody note that is easy to miss: when Terraform generates or reads a
secret, that value is stored in plaintext in Terraform **state**. The secret is
absent from the repository, but the state file now holds it and needs the same
protection as the secret store itself — a remote backend with encryption and
restricted access, never a state file committed or left on a workstation.

---

## Admitting the first person

**A working SSO deployment admits nobody until you do this.** It is the step
most likely to be missed, because nothing fails: the container starts, health
and readiness pass, the login page appears, and the provider authenticates
people correctly. They simply cannot get in.

The reason is that a first SSO login lands **pending** by design. The provider
established who someone is; it did not decide that this deployment admits them.
An administrator activates them. On a new deployment there is no administrator
to do it, and no amount of successful authentication creates one.

Two ways to make the first administrator. Both are refused once an active human
administrator exists, so neither is a way to escalate later.

**The operator command, and the one to prefer.** With access to the deployment's
sessions store, run:

```bash
elspeth composer users bootstrap-admin <provider> <subject> \
  --note "why this bootstrap is happening"
```

`<subject>` is the person's `sub` claim at your provider — the same value the
identity is keyed on, not their email address. The command creates or binds the
identity row, activates it, grants a deployment-wide `admin`, writes its quota
row, and records an audit row, all in one transaction. Prefer this: it leaves
nothing behind in your configuration.

**The seed list.** Set `sso_admin_subjects` to a JSON array containing that same
subject, deploy, and have that person log in. They are activated as `admin` on
that login. **Then remove the setting and redeploy** — see the warning under
[Settings every non-local provider needs](#settings-every-non-local-provider-needs)
for why leaving it in place is not safe.

Neither path recovers a deployment whose administrator row is still active but
whose administrator can no longer authenticate — a person who has lost their
account at the provider, say. Both are gated on there being *zero* active human
administrators. Keep more than one administrator activated once you can.

If your provider is a Cognito user pool created by this repository's Terraform,
note that the pool is created with administrator-creation only and ships with no
users at all. You create the first user yourself with `aws cognito-idp
admin-create-user`, that person signs in, and you then bootstrap them by one of
the two paths above. There is no local-login fallback on a deployment configured
for a provider: the local route is not served, and `dev_admin_user` is refused
outright on any non-local `auth_provider`.

---

## Quotas and the compartment marking

These two are required by every profile and neither is obvious, so they are
worth stating plainly.

### Quotas

`quota_default_tokens_per_day` and `quota_default_storage_bytes` are the
allowances written into a quota row whenever an identity is activated. They are
required rather than defaulted because a deployment authenticates people
against a **shared LLM credential that the deployment pays for**. Without a
quota row an activated identity would hold unbounded spend on it. There is no
sensible default for a number that depends on your provider contract, so the
deployment refuses to start rather than inventing one.

Both must be greater than zero.

Two further settings, `quota_container_tokens_per_day` and
`quota_container_storage_bytes`, are accepted and validated but **not yet
enforced**: no runtime path reads either in this release. Do not treat them as
a container-wide spend ceiling — setting them changes nothing today. The
per-identity defaults above are the control that exists.

### The compartment marking

`compartment_id` is the marking that identifies **this deployment** as a
compartment. It is required for every provider profile, and it is a label you
choose: a stable, meaningful identifier for the environment, such as its
scenario or environment name.

Its purpose is to make the same artifact appearing in two deployments
detectable later. **In this release the setting is validated and stored but
not yet consumed:** the `library_entries` table carries a non-null
`compartment_id` column and an index for it, and no runtime path writes or
reads one. The stamping of the marking into published library rows, exported
YAML, audit metadata and signed exports is the work this column is waiting
for. It is required now so that the value is fixed before anything starts
depending on it, and so no deployment has to be reconfigured when it does.

Pick a value that will still identify this deployment in a year, and do not
reuse one across environments that should not be confused with each other.
Marking is a recording control, never a preventive one: it makes movement
between compartments visible after the fact and cannot stop it.

---

## Break-glass endpoint overrides

Normal operation is **discovery**: ELSPETH fetches the provider's
`.well-known` discovery document, checks that its `issuer` matches the
configured issuer exactly, and takes the endpoints from it.

Four settings let you bypass discovery when a provider's document is wrong,
incomplete, or unreachable:

- `sso_authorization_endpoint`
- `sso_token_endpoint`
- `sso_jwks_uri`
- `sso_userinfo_endpoint`

You would use them for one reason: to keep a deployment working when the
provider's published metadata is at fault and you cannot get it fixed quickly.
They are a last resort, not a tuning knob.

Two rules apply, and there is a third thing you should understand about what
an override costs you.

**Available on every profile.** Each profile's own origin policy is what your
URLs are checked against: the issuer's origin for `oidc` and `vanguard`,
`https://login.microsoftonline.com` for `entra`, and Google's four published
origins for `google`. A deployment that derives its issuer rather than stating
it can still break the glass.

**All or none.** The first three must be set together. A partial override would silently mix operator-supplied
endpoints with discovered ones, leaving which origin policy applied to which
URL dependent on which variables happened to be set. Setting some and not
others is refused:

```text
sso endpoint overrides are all-or-none; missing: sso_jwks_uri, sso_token_endpoint
```

`sso_userinfo_endpoint` is not an override on its own — with no endpoints to
pair it with there is nothing for discovery to be bypassed *for* — so setting
it alone is refused too.

**Still origin-checked.** An override lets you name a *different URL* on an
origin the provider is expected to serve from. It is not a way to leave those
origins. Each URL is validated at settings load against the same origin policy
that profile applies to discovery, and an off-origin URL is refused:

```text
authorization_endpoint failed expected-origin check
```

For a generic OIDC deployment whose endpoints legitimately sit elsewhere, the
correct move is to declare that origin in `sso_endpoint_origins`, not to
override past the check.

**What you give up, and why these are not a tuning knob.** Setting the trio
does not merely skip a fetch. Discovery is where the provider's document is
checked to declare the issuer you configured — the `discovery document failed
the exact issuer check` refusal described above. With the overrides set, that
document is never fetched, so that check never runs, and the only remaining
tie between your configured issuer and the endpoints in use is the origin
policy. The pinned `sso_jwks_uri` is the sharper cost: signing keys are then
reached at an address fixed in your configuration rather than one the provider
publishes, so if the provider moves its JWKS endpoint, logins fail until you
change the setting by hand. Token validation itself is unaffected — issuer,
audience, expiry and nonce are still checked on every ID token.

Use the overrides to get through an outage or a broken document, and take them
back out afterwards. Leaving them configured permanently trades a fetch you
control for two guarantees you no longer have.

---

## What ELSPETH does not take from your directory

ELSPETH deliberately **does not read group or role claims** from the provider,
on any profile. If your directory emits `groups`, `roles`, or an equivalent,
those claims are ignored.

This is a deliberate boundary, not an unimplemented feature. A group name comes
from a directory this deployment does not administer, and it describes a fact
about your organisation: which team someone is in, which cost centre they
charge to, which mailing list they are on. What a person may do *here* — publish
to the library, approve a run, read the audit trail, administer other
identities — is a fact about this compartment. The two are not the same, and
treating one as the other means that anyone who can create a group in the
directory can grant themselves authority in ELSPETH.

So the provider answers exactly one question: **who is this person**. Authority
is granted separately, in this deployment, by an administrator here, and
recorded in `identity_roles` with a granting identity, a timestamp, an optional
expiry, and a note. The roles are a closed set: `admin`, `approver`, `reviewer`,
`user`, `curator`, `auditor`, `oversight`.

The practical consequence for a first-time deployment: **a first login lands
pending, not active.** Someone who authenticates successfully is refused access
until an administrator admits them. That is the design, and it is why
`sso_admin_subjects` exists — to seed the very first administrator, once, on a
deployment that has none.

Claims that *are* read are the standard ones: subject, issuer, audience,
expiry, nonce, and the profile claims backing username, display name, and
email — plus `tid` for Entra, `hd` for Google, and the VANguard name parts and
ABN from userinfo.

---

## Troubleshooting

### The task starts and immediately exits

These are settings-load refusals. The message names the setting.

| Message | Cause | Fix |
| ------- | ----- | --- |
| `auth_provider='<name>' requires: <settings>` | Not every required setting is configured. An empty string counts as absent. | Supply all of the profile's settings in one revision. |
| `auth_provider='<name>' does not use: <setting>` | A setting belonging to a different profile is configured. | Remove it. `sso_issuer` on `entra` or `google`, `entra_tenant_id` or `google_hosted_domain` on anything else, `sso_endpoint_origins` on anything but `oidc`. |
| `must not be blank (omit the field or set to a non-empty value)` | An environment variable is set to an empty string. | Unset it, or give it a value. |
| `Unknown ELSPETH_WEB__ setting: ELSPETH_WEB__OIDC_CLIENT_ID` | A deleted legacy setting is still exported. | See the upgrade note below. |
| `dev_admin_user requires auth_provider=local; IdP deployments must not carry it` | `dev_admin_user` survived the switch to a provider. | Remove it. |
| `Local auth does not use entra_tenant_id` | Provider settings left behind after switching back to local. | Remove it. |
| `public_base_url must be an origin without path, query, or fragment` | The value carries a path. | Use the bare origin. |
| `sso endpoint overrides are all-or-none; missing: <settings>` | A partial break-glass override. | Set all three, or none. |
| `authorization_endpoint failed expected-origin check` | A break-glass override points off the profile's permitted origins. | **First check the URL you supplied** — a typo is the common cause. Widening `sso_endpoint_origins` (generic `oidc` only) is correct only when the provider genuinely serves that endpoint from the other origin: every origin you add is one the backend may send the client secret to at the token endpoint, and may fetch signing keys from. |
| `Assertion failed`, with no message naming a setting | Break-glass endpoint overrides on `entra` or `google`, which cannot bypass discovery. | Remove the four `sso_*` endpoint settings. |

### Sign-in fails in the browser

| Symptom | Cause |
| ------- | ----- |
| `/api/auth/sso/start` returns 404, and no sign-in button appears | `auth_provider=local`. The single sign-on routes do not exist on a local deployment. |
| 503, `Single sign-on is not configured on this deployment` | A provider is selected but the runtime was not built. The deployment fails closed rather than half-way. |
| The provider refuses before ELSPETH is reached, citing the redirect URI | The registered URI is not exactly `https://<public_base_url>/api/auth/sso/callback`. |
| `discovery document failed the exact issuer check` | `sso_issuer` does not exactly match the `issuer` in the provider's discovery document. Trailing slashes matter. |
| `This account is awaiting approval` | Expected on a first login. The identity is pending until an administrator **activates** it, through the admin API. That is the design, not a fault. If this is the first person on a new deployment and there is no administrator yet to activate them, see [Admitting the first person](#admitting-the-first-person) — that is the step, and it is the one most often missed. |
| `This account has been disabled` | The person authenticated; an administrator disabled the identity here. |
| `This sign-in link has already been used or has expired` | The handoff code is single-use and short-lived. Start the login again. |
| `Entra ID token was issued by a different tenant`, or `is missing the required claim 'tid'` | The sign-in came from a directory other than `entra_tenant_id`. |
| `Google ID token is missing the required claim 'hd'` | A personal Google account, which carries no hosted domain. Only Workspace accounts in the configured domain can sign in. |
| `Google ID token is from a different hosted domain` | A Workspace account in a domain other than `google_hosted_domain`. |
| `Google ID token does not assert a verified email` | The account's email is not verified. |

### If you are upgrading

The legacy browser-client settings — the `oidc_*` family, the browser-origin
allowlist, and the Cognito access-token audience-claim mode — have been
removed. A deployment still exporting any of them refuses to boot on an unknown
setting. Delete them and configure the profile settings described above.
