# 04 — Threat model

**Status:** product threat model and method complete; deployment decisions and
risk acceptance open · **Reviewed against:**
`release/0.8.1` @ `49c1845085d36811b120ef1c540048463e32aabc`
(2026-09-30) · **Owner:** ELSPETH maintainer

Identifies what is worth protecting, who controls each input, where an
attacker can act, which controls stand in the way, and how severe a failure
of each control would be. Residual risks feed the risk register
([15](15-risk-register.md)), which is held in the controlled copy.

It builds on [02](02-data-flows-and-trust-boundaries.md) (flows and trust
tiers) and describes controls detailed in
[06](06-identity-and-access.md) (identity),
[07](07-secrets-and-key-management.md) (secrets) and
[08](08-logging-audit-and-monitoring.md) (audit). Each supporting document
records its own review commit. For this review, the implementation claims and
the source delta since their latest evidence were checked at the commit in the
header; § 4.6 reflects the current web-fetch address policy and § 4.11 the
current supply-chain gates.

**Method.** The model is produced and maintained as follows:

1. Take components, entry points, data flows and trust crossings from
   [01](01-system-overview-and-boundary.md) and
   [02 § 3](02-data-flows-and-trust-boundaries.md#3-product-flows). Keep runtime,
   deployment, build and local-tooling surfaces distinct.
2. Classify each input by who controls it (§ 3), and record the assumptions
   that change the attacker's authority. The same path can have a different
   deployment risk where all users are trusted operators and where users are
   separated tenants.
3. Apply all six STRIDE categories to every surface in § 4. Record the
   resulting abuse path as a threat, or record why the category has no
   separate product-relevant path (§ 5.1).
4. For each threat, identify the affected asset and security objective,
   current controls, control status, and the product-level consequence if the
   path succeeds. Section 6 calibrates that consequence; it is not a risk
   rating and does not include deployment-specific likelihood.
5. Give each residual threat an opaque risk reference. Give a deployment
   threat a reference that is activated when the deployment decision leaves
   the threat applicable. The controlled register combines likelihood,
   consequence, control effectiveness and treatment (§ 7 and
   [15](15-risk-register.md)).

Threat IDs and risk references are stable: never renumber or reuse them.
Retire an obsolete item with its disposition and successor reference rather
than deleting it. More than one threat may share a risk reference only when
the controlled register records one causal scenario, treatment and acceptance
decision for them. `R-001` intentionally groups T-004, T-022 and T-029 on
that basis. Public text describes threats and controls but keeps the cause,
exposure conditions, likelihood, treatment and acceptance for an open risk in
the controlled register.

## 1. Overview

ELSPETH is a Python / FastAPI and React application for composing,
validating, running and auditing data pipelines. Users sign in, upload
source data, work with an LLM-backed Composer that proposes pipeline
changes, store secret references, run pipelines through plugins, and inspect
runs, outputs, audit records and shareable review links. The same repository
holds the command-line runtime, the plugins, the Landscape audit store and
local MCP tools for querying audit data.

The web application is multi-user. Every user authenticates through one
configured provider ([06 § 1](06-identity-and-access.md#1-authentication)),
is admitted by an administrator or by the registration mode, and works in
sessions they own. Beyond ownership, ELSPETH has seven roles and optional
workflow governance (approval, review and curation with separation of
duties, [06 § 2](06-identity-and-access.md#2-authorisation)). Governance is
off by default, so whether users are treated as trusted operators or as
separated tenants is a deployment decision (§ 7), and it changes the
severity of several threats below.

## 2. Assets

| Asset | Why it matters | C | I | A |
|---|---|---|---|---|
| Audit trail (Landscape) and session audit | Evidence for every output and decision | High | **Critical** | Medium |
| Pipeline input data, uploaded files and outputs | May hold personal or classified information | **High** | High | Medium |
| Session tokens and identities | Grant access as a user or administrator | **High** | High | Medium |
| User and server secrets; process environment | Credentials for providers, data stores and services | **Critical** | High | Low |
| Signing and derivation keys (`secret_key`, share-link key, SSO transaction secret, export signing key, fingerprint key) | Forging tokens, links or exports; decrypting user secrets | **Critical** | **Critical** | Low |
| Composition state and proposals | Define what will run and send data where | Medium | **High** | Medium |
| Operator configuration (web settings, plugin policy, profiles, allowlists) | Sets every boundary in this document | Medium | **Critical** | Medium |
| Release artefacts and container images | Everything above runs from them | Low | **Critical** | Medium |

C, I and A are confidentiality, integrity and availability.

## 3. Who controls each input

### 3.1 Attacker-controlled (unauthenticated or authenticated user)

- Unauthenticated HTTP requests to public routes (sign-in, registration,
  SSO callback, `/api/auth/config`, `/api/health`, `/api/ready`,
  `/api/system/status`, share-link resolution).
- Sign-in and registration bodies; SSO callback parameters; bearer tokens
  presented to the API; WebSocket tickets.
- Authenticated REST bodies, uploaded files, chat messages to the Composer,
  and references a user can supply (session, state, blob, run and secret
  identifiers).
- Content that reaches the model or the engine from outside: source rows,
  uploaded documents, fetched web pages, and model output itself (tool
  calls and text).
- MCP tool arguments, if an MCP server is connected to an untrusted agent.

### 3.2 Provider-controlled (external, may fail or be malicious)

Identity-provider discovery, JWKS, token and userinfo responses; LLM
responses; Key Vault; cloud data and document services; websites; databases
reached by source or sink plugins. All are Tier 3
([02 § 2](02-data-flows-and-trust-boundaries.md#2-trust-tiers)).

### 3.3 Operator-controlled

Web settings and environment variables, including `secret_key`, the
share-link key, the identity-provider configuration, `registration_mode`,
`workflow_governance`, the server-secret and secret-wiring allowlists, the
web plugin policy and operator profiles, per-session directory allowlists,
data directories, provider keys, database URLs, telemetry settings, and
whether `/metrics` or an MCP server is exposed. Operator-authored YAML run
through the CLI is not a sandbox: it can deliberately read files, call
networks, use credentials and send data to sinks.

### 3.4 Developer-controlled

Plugin code shipped in the package, frontend build output, tests, fixtures,
CI workflows and scripts, examples and generated documentation. A weakness
that needs developer control is of low production severity unless a
deployment loads third-party plugins or untrusted build artefacts; the
controls on this channel are in [09](09-secure-development-lifecycle.md) and
[10](10-vulnerability-and-supply-chain.md).

### 3.5 Assumptions

- TLS termination, reverse-proxy configuration and network egress rules
  belong to the deployment ([03](03-shared-responsibility-matrix.md)).
- Plugins are trusted code; their configuration is user-controlled when a
  pipeline is written in the Web Composer.
- The identity provider authenticates people correctly and enforces MFA
  where required; ELSPETH does not read IdP group or role claims
  ([06 § 1.1](06-identity-and-access.md#11-providers)).
- Operators hold the keys in § 2 and do not expose them to users.
- If every web user is a trusted operator, pipeline misuse is mainly
  operational risk. If users are separated tenants, the boundaries between
  what a user configures and what the runtime does become high-value
  security boundaries.

## 4. Attack surfaces and controls

Each subsection lists the attacker's goal, the controls in place at
`49c1845085d36811b120ef1c540048463e32aabc`, and where residual risk is
recorded. Evidence IDs refer to
[16](16-evidence-index.md).

### 4.1 Authentication and sessions

**Goal:** sign in as someone else, keep access after it is withdrawn, or
forge a token.

Controls:

- Five providers behind one registry; ELSPETH is a confidential OIDC client
  with PKCE, the browser never receives IdP tokens, and a one-time handoff
  code in the URL fragment is exchanged for an ELSPETH session token
  ([06 § 1.2](06-identity-and-access.md#12-sso-sign-in)) [EV-012] [EV-101].
- ID-token algorithms are fixed per provider profile and never read from the
  token header; `exp`, `iat`, `iss`, `sub` and `aud` are required.
- Session tokens are HS256 under a key derived from `secret_key`; weak keys
  are refused at start-up; tokens are bound to provider and audience; the
  identity's `active` state is re-checked on every request, so disabling a
  user takes effect on their next request
  ([06 § 1.4](06-identity-and-access.md#14-session-tokens)) [EV-102].
- The run-progress WebSocket accepts only a single-use, short-lived ticket
  bound to the run and user, minted after an ownership check; a session token
  in the WebSocket URL is refused (`src/elspeth/web/execution/routes.py`)
  [EV-401].
- Sign-in, registration and SSO routes are rate-limited per client, and
  failed attempts are audited in `auth_events`
  ([06 § 1.5](06-identity-and-access.md#15-sign-in-rate-limiting)) [EV-106].
- The API uses bearer tokens, not cookies, so it has no cross-site request
  forgery surface. Session tokens have a bounded lifetime and refresh-chain
  lifetime; the identity's active state remains authoritative on every
  request [EV-102].

Risk references: see controlled risk register `R-001` and `R-002`; activate
`R-003` when the deployment decision makes it applicable.

### 4.2 Authorisation and cross-user access

**Goal:** read or change another user's sessions, runs, files, secrets or
outputs, or exercise a role not held.

Controls:

- Session-scoped routes require the session's owner, the same provider and a
  non-archived session, and answer a uniform 404 on every failure
  (`src/elspeth/web/sessions/ownership.py`)
  ([06 § 2.4](06-identity-and-access.md#24-session-isolation)) [EV-109].
- State, run and blob identifiers resolve only inside their own session;
  blob inputs are pinned to the owning session and content hash when a run
  is admitted.
- Seven roles, re-read per request and re-checked inside each mutating
  transaction; a caller without the role gets 404
  ([06 § 2.2](06-identity-and-access.md#22-roles)) [EV-107] [EV-108].
- With workflow governance on: a run needs an approval bound to the exact
  frozen composition; the author cannot approve, the requester cannot
  review, the publisher cannot curate; cross-user inspection is limited to
  an approver or reviewer on an open request and is logged first
  ([06 § 2.5](06-identity-and-access.md#25-workflow-governance)) [EV-110].
- Secrets are scoped to the user and provider; server secrets are limited to
  an operator allowlist; an operator can disable user secrets entirely
  ([07 § 2.3](07-secrets-and-key-management.md#23-secrets-used-by-web-authored-pipelines))
  [EV-211] [EV-212] [EV-213].
- Shareable review links are signed, expiring capabilities that still
  require an authenticated user, and give read-only views
  ([06 § 6](06-identity-and-access.md#6-shareable-review-links)) [EV-117].

Risk reference: activate controlled risk register `R-004` when the deployment
decision makes it applicable. T-007 and T-008 are controlled product threats.

### 4.3 Composer and the LLM boundary

**Goal:** use prompt injection (from a user, a source row, an uploaded
document or a web page) to make the Composer build an unsafe or costly
pipeline, misuse an allowed connector, or disclose data; or exploit the
parsing of model output.

Model output is Tier 3. Controls:

- **Strict tool contracts.** Every tool call's arguments are validated on
  the server against a closed schema (no unknown keys) before any handler
  runs. This holds for every provider and regardless of the
  `composer_strict_tools` setting, which only controls whether a strict
  schema is also sent to the provider for reliability. An unknown tool name
  is refused. Rejections are reported without echoing values, and a
  rejected call's raw arguments are redacted in the audit record
  (`src/elspeth/web/composer/tools/_dispatch.py`) [EV-402].
- **Immutable, versioned state.** Composition state and every node spec are
  deeply frozen; each edit produces a new versioned state
  (`src/elspeth/web/composer/state.py`).
- **Separated proposals and fenced commits.** Committed history is stored
  apart from proposals. Accepting a proposal requires that it is still
  pending, that the session head equals the proposal's base, and that the
  caller holds the session's exclusive operation fence and write lock, so a
  stale or duplicate proposal cannot be applied
  (`src/elspeth/web/sessions/mutation_capabilities.py`,
  `proposal_authority.py`) [EV-403].
- **Review binds to what will run.** Accepting a whole-pipeline proposal
  requires its draft hash; approvals bind the effective prompts, source
  order and branch order, so a change after review invalidates it.
- **Trust mode.** By default (`trust_mode = auto_commit`, a per-session
  preference) planner changes are applied to the working composition without
  a separate approval of each change; `explicit_approve` requires the user to
  approve every change. In both modes a pipeline runs only when a user starts
  it, after full validation, and — with governance on — after an approver's
  approval.
- **No server-authored structure.** The server validates, rejects or redacts
  model output; it never synthesises pipeline structure in place of the
  planner (composer invariants,
  [AGENTS.md](../../AGENTS.md#composer-invariants-non-negotiable)).
- **Bounded authority over connectors.** Web-authored pipelines may use only
  plugins in the operator's policy; cloud targets, LLM endpoints and search
  services are reached only through operator profiles whose endpoints and
  credentials the author cannot set; LLM nodes cannot set a base URL
  ([02 § 4.2](02-data-flows-and-trust-boundaries.md#42-operator-profiles-for-web-authored-pipelines))
  [EV-013] [EV-503].
- **Data minimisation.** A prompt or query template can read only the
  fields its node declares; the redaction manifest strips internal paths and
  sensitive arguments before state reaches the model; secret values never
  enter composition state [EV-014] [EV-220] [EV-508].
- **Required controls.** The operator's policy can require prompt-shield or
  content-safety plugins in every web-authored pipeline [EV-502] [EV-507].
- **Bounded cost.** Per-composition limits on provider calls, request size,
  completion tokens and cost
  ([02 F6](02-data-flows-and-trust-boundaries.md#3-product-flows)) [EV-504].

Prompt injection remains in scope: the controls above bound what injected
instructions can make the Composer do, not whether a model follows them.
Model-specific risks are assessed in [11](11-ai-llm-risk-assessment.md).

Risk references: see controlled risk register `R-005`, `R-006` and `R-007`.

### 4.4 Pipeline execution, files and blobs

**Goal:** read or write files outside what the user may reach, or run code
through templates.

Controls:

- Per-session source and sink directory allowlists, enforced at validation,
  again at execution and at the Composer route; the checked path is the path
  opened (relative paths are rewritten under `data_dir`).
- File-backed template options (`template_file`, `lookup_file`,
  `system_prompt_file`) are refused in web-authored configurations.
- Templates render in a sandboxed Jinja environment inside separate worker
  processes with per-render limits (2 s CPU, 5 s wall time, 256 MiB extra
  memory, 4 MiB output) and limits on template size and structure
  (`src/elspeth/core/templates.py`) [EV-404].
- Uploads: closed MIME allowlist with content sniffing, filename
  sanitisation, per-session quota, content hashing; downloads are served as
  attachments; internal storage paths never appear in responses
  ([02 F5](02-data-flows-and-trust-boundaries.md#3-product-flows)).
- Replay and verify runs may use only payloads and network evidence the
  source run recorded, and revalidate archived network pins under the
  current address policy.

T-016 and T-017 are controlled product threats; this surface has no separate
residual or deployment risk reference in the current model.

### 4.5 Secrets and the process environment

**Goal:** make a pipeline written in the Web Composer disclose a server-side
secret or environment variable.

Controls:

- The web application never expands `${VAR}` in web-authored content: every
  web path that loads settings does so with environment expansion off.
- At run start, the execution envelope refuses any `${...}` that is not the
  exact name of a secret in the user's inventory, and refuses `:-` defaults
  (`src/elspeth/web/execution/envelope.py`) [EV-405].
- `${VAR}` is refused in plugin options whose value reaches output, derived
  from plugin declarations.
- Server secrets come only from the operator allowlist and never include
  `ELSPETH_*` internal names; wiring a secret into an option is denied
  unless the operator's wiring allowlist names that exact destination; the
  user must acknowledge the exact wiring set immediately before launch; the
  model sees secret names, never values
  ([07 § 2.3](07-secrets-and-key-management.md#23-secrets-used-by-web-authored-pipelines))
  [EV-211] [EV-214] [EV-215].
- The audit trail records fingerprints, not values
  ([07 § 2.4](07-secrets-and-key-management.md#24-what-the-audit-trail-records))
  [EV-207] [EV-208] [EV-209].

For CLI pipelines, `${VAR}` expansion is intended: the operator authors the
YAML and owns its environment.

Risk reference: see controlled risk register `R-008`.

### 4.6 Network egress and server-side request forgery

**Goal:** make ELSPETH send requests to internal services or cloud
instance-metadata endpoints, or send data to a host the operator did not
intend.

Controls ([02 § 4.1](02-data-flows-and-trust-boundaries.md#41-public-http-fetching)) [EV-011]:

- HTTP and HTTPS only; credentials in URLs refused.
- Hostnames resolved once and every address checked; the connection is
  pinned to the checked address (DNS-rebinding defence).
- A fixed list of loopback, private, carrier-grade NAT, unique-local,
  reserved/special-use and IPv4-embedding IPv6 ranges is blocked by default;
  link-local, cloud-metadata and platform-service address forms are blocked
  regardless of operator policy
  ([02 § 4.1](02-data-flows-and-trust-boundaries.md#41-public-http-fetching)).
- Web-authored pipelines cannot widen the address policy (`allow_private` or
  CIDR lists are refused for any fetching plugin).
- Optional exact-origin allowlist (`http.allowed_origins`), checked before
  DNS, on every redirect and in replay; links discovered in fetched pages are
  followed only under it.
- Redirects are limited and each hop is re-validated; POST redirects are not
  followed; request headers come from a closed allowlist, and a request that
  carries configured headers is refused if a redirect would take it to
  another origin.
- Request and response bodies are bounded for web-authored pipelines
  (10 MiB each, 30 s timeout), including multipart requests, which accept
  text or stored-blob parts only; response size is checked after
  decompression.
- TLS verification is always on.

Operator settings that widen the policy (`allow_private`, CIDR lists) move
the resulting decision into the deployment record. Risk references: see
controlled risk register `R-009` and `R-010`.

### 4.7 Frontend rendering

**Goal:** inject script into the single-page application, which would give
access to the user's token (§ 4.1).

Controls:

- React escapes content by default; one Markdown renderer is used, without
  raw HTML; Mermaid diagrams render at their strict security level and their
  SVG passes through DOMPurify; external links carry
  `noopener noreferrer`; no `dangerouslySetInnerHTML` or `eval` in the
  frontend source [EV-406].
- HTML responses carry a Content-Security-Policy with `script-src 'self'`
  and `frame-ancestors 'none'`, plus `X-Frame-Options: DENY` and
  `Referrer-Policy: no-referrer`
  ([02 F3](02-data-flows-and-trust-boundaries.md#3-product-flows)).
- Frontend dependencies are locked by `package-lock.json`, and known
  advisories in the sanitiser, diagram and build libraries have been patched.

Risk reference: see controlled risk register `R-001`.

### 4.8 Audit store, MCP and local tooling

**Goal:** alter or delete audit evidence, or read all audit data through a
tooling surface.

Controls: audit-first writes, closed vocabularies and hash checks, write
fencing between replicas, append-only transcripts and sealed exports;
exports are HMAC-signed when that signing mode is configured, while the
product default is unsigned
([08 § 2](08-logging-audit-and-monitoring.md#2-integrity)) [EV-315] [EV-316]. The Landscape
MCP server runs locally over stdio, opens the database read-only, accepts a
single `SELECT` or `WITH` statement inside a read-only transaction, and caps
rows [EV-407]. The Composer MCP server validates session identifiers
wherever it builds a file path. An MCP server exposed to an untrusted agent
or network can still disclose every audit record it can read, including
personal information; exposing one is an operator decision (§ 7).

Risk references: see controlled risk register `R-011`; activate `R-012` when
the deployment decision makes it applicable.

### 4.9 Information disclosure and observability

`/api/auth/config`, `/api/health`, `/api/ready` and `/api/system/status` are
unauthenticated by design
([02 F17](02-data-flows-and-trust-boundaries.md#3-product-flows)) [EV-105].
`/api/system/status` returns the deployment identity, model names, frontend
build and classification banner; readiness returns redacted check results.
`/metrics` needs an operator bearer token and refuses per-user labels
[EV-323] [EV-328].
Failure reasons recorded in the audit trail and shown to users are
value-free: they do not carry row values, and fetch refusals carry only a
closed refusal kind, not the URL or address. Exposure of the unauthenticated
endpoints beyond the users who need them is a deployment decision (§ 7).

Risk reference: see controlled risk register `R-013`.

### 4.10 Resource exhaustion

Controls: template CPU, memory, time and output limits in bounded workers
(§ 4.4) [EV-404]; web-authored HTTP request, response and timeout ceilings
(§ 4.6); graph nesting limits (`src/elspeth/core/config.py`,
`src/elspeth/core/dag/bound_regions.py`); per-composition planner and advisor
limits, bounded retries and a required per-user Composer rate limit [EV-504];
and a declared 10 MiB API request-body ceiling
(`src/elspeth/web/app.py`) [EV-408].

Risk references: see controlled risk register `R-007` and `R-014`.

### 4.11 Supply chain and build

Controls: GitHub Actions pinned to commit SHAs; container base images pinned
by digest; CodeQL with the `security-extended` suite; dependency auditing
and Dependabot; images signed with cosign; the judge-metadata signing key
held by the operator only. Detail in [09](09-secure-development-lifecycle.md)
and [10](10-vulnerability-and-supply-chain.md) [EV-605] [EV-612] [EV-616]
[EV-626] [EV-627].

Risk reference: see controlled risk register `R-015`.

### 4.12 Deployment hardening

Controls: non-root, distroless runtime image; a non-local bind without a
configured secret key fails at start-up; the `elspeth web` command uses the
environment's configured provider unless one is given explicitly; deployment
profiles fail closed on missing or unsafe settings (database TLS pinning on
AWS, private directories, distinct databases); readiness reports unsafe
governance combinations [EV-110] [EV-320] [EV-328] [EV-409] [EV-627]
[EV-711] [EV-712].

Risk reference: activate controlled risk register `R-016` when the deployment
decision makes it applicable.

## 5. Threat register

Status meanings:

- **Controlled:** current product controls address the modelled path. This is
  a control assessment, not a claim that exploitation is impossible.
- **Residual:** current controls reduce the path, but the deployment must
  assess and treat the referenced record in the controlled risk register.
- **Deployment:** applicability and control effectiveness depend on § 7. If
  the selected deployment leaves the path applicable, import its risk
  reference before authorisation.

STRIDE abbreviations are **S**poofing, **T**ampering,
**R**epudiation, **I**nformation disclosure, **D**enial of service and
**E**levation of privilege. Severity is the product-level consequence if the
path succeeds; likelihood and the deployment's risk rating belong in the
controlled register.

| ID | STRIDE | Threat | Surface | Affected asset / objective | Current controls | Status | Severity and basis | Risk ref |
|---|---|---|---|---|---|---|---|---|
| T-001 | S, E | Forged session token or cross-context token substitution | 4.1 | Identities and session tokens (C, I) | Derived HS256 key, weak-key refusal, required claims and provider/audience binding | Controlled | Critical — successful forgery bypasses authentication | — |
| T-002 | S, E | ID-token substitution or algorithm confusion | 4.1 | Identities and session tokens (C, I) | Pinned algorithms, nonce, audience and provider-specific claims | Controlled | Critical — successful substitution bypasses authentication | — |
| T-003 | S, E | Continued access after an identity is disabled | 4.1 | Identities and authorisation (I) | Per-request `active` check | Controlled | High — the user retains authenticated access | — |
| T-004 | I, S | Token theft from the browser | 4.1, 4.7 | Session tokens (C) | CSP, sanitised rendering, no raw HTML, bounded token lifetime | Residual | High — a bearer credential permits impersonation | `R-001` |
| T-005 | S | Credential stuffing against local accounts | 4.1 | Identities (C, I) | Rate limit, bcrypt, uniform failure and audit | Residual | High — success compromises an account | `R-002` |
| T-006 | S, E | Self-registration by unintended users | 4.1 | Identities and deployment boundary (I) | `registration_mode` and admission states | Deployment | Medium — impact depends on admission and tenancy decisions | `R-003` if applicable |
| T-007 | I, E | Reading another user's session, run, blob or secret | 4.2 | User data and secrets (C) | Ownership checks, uniform 404 and scoped identifiers | Controlled | Critical — the path crosses a user confidentiality boundary | — |
| T-008 | S, E | Acting with a role not held | 4.2 | Authorisation and configuration (I) | Live role checks in the request and mutation transaction | Controlled | Critical — privileged actions may affect system-wide controls | — |
| T-009 | T, R, E | Author approving their own run | 4.2 | Composition and approval records (I) | Governance separation of duties | Deployment | High — approval provenance and workflow integrity fail | `R-004` if applicable |
| T-010 | T, I, E | Prompt injection causing unsafe but authorised egress | 4.3 | Pipeline data and composition (C, I) | Operator profiles, plugin policy, required controls and declared-field templates | Residual | Medium — authorised connectors can disclose data to an unintended public destination | `R-005` |
| T-011 | T, E | Malformed or invented tool arguments reaching handlers | 4.3 | Composition and configuration (I) | Server-side closed-schema validation | Controlled | High — unvalidated arguments could cross the planner/server authority boundary | — |
| T-012 | T | Stale or duplicate proposal applied | 4.3 | Composition state (I) | Proposal authority, head check and operation fence | Controlled | High — the committed composition differs from the user's current state | — |
| T-013 | T, R | Change after review taking effect | 4.3 | Composition and approval records (I) | Draft-hash, prompt and branch-order binding | Controlled | High — execution would no longer match the reviewed object | — |
| T-014 | T, E | Model-authored change applied without human review | 4.3 | Composition state (I) | Trust mode; explicit user start and full run validation | Residual | High — model output can change execution intent | `R-006` |
| T-015 | D | Provider-cost or execution consumption exceeds operational tolerance | 4.3, 4.10 | Provider budget and service availability (A) | Planner/advisor limits, quotas where configured and rate limits | Residual | Medium — cost or capacity can be exhausted without compromising another security objective | `R-007` |
| T-016 | I, T, E | File read or write outside permitted directories | 4.4 | Host files and pipeline data (C, I) | Per-session allowlists, checked-path rewriting and file-option refusal | Controlled | Critical — arbitrary host file access crosses the runtime boundary | — |
| T-017 | E, D | Code execution or exhaustion through templates | 4.4, 4.10 | Runtime and service capacity (I, A) | Sandboxed Jinja in bounded worker processes | Controlled | Critical — arbitrary code execution compromises the runtime | — |
| T-018 | I, E | Web-authored environment reference discloses a process secret | 4.5 | Secrets and process environment (C) | No web expansion and execution-envelope refusal | Controlled | Critical — process credentials may provide external or administrative access | — |
| T-019 | I | Secret disclosed through output, audit or the model | 4.5 | User and server secrets (C) | Wiring allowlist, launch acknowledgement, fingerprints and names-only model exposure | Residual | Critical — a credential value may grant access outside ELSPETH | `R-008` |
| T-020 | I, E | Request forgery to internal services or metadata endpoints | 4.6 | Network trust boundary and credentials (C, I) | Address policy, DNS pinning and always-blocked ranges | Residual | Critical — internal control-plane access can expose credentials or privileged services | `R-009` |
| T-021 | I | Data sent to an unintended public host | 4.6 | Pipeline data (C) | Exact-origin allowlist and header origin binding | Residual | Medium — data leaves the intended destination boundary | `R-010` |
| T-022 | T, I, E | Script injection in the frontend | 4.7 | Session tokens and user-visible state (C, I) | Escaping, sanitiser and CSP | Residual | High — script execution can act with the user's browser authority | `R-001` |
| T-023 | T, R | Tampering with audit evidence | 4.8 | Audit trail (I) | Schema checks, mutation fencing, append-only transcripts, sealed exports, optional signing and database access control | Residual | Critical — evidence may no longer prove what ran or occurred | `R-011` |
| T-024 | I, E | Bulk audit disclosure through an exposed MCP server | 4.8 | Audit trail and pipeline data (C) | Local stdio, read-only transactions and row caps; exposure is a deployment decision | Deployment | High — audit records may contain personal or sensitive operational data | `R-012` if applicable |
| T-025 | I | Reconnaissance through unauthenticated endpoints | 4.9 | Deployment metadata (C) | Redacted readiness and bearer-protected metrics | Residual | Medium — disclosed metadata can assist later targeting | `R-013` |
| T-026 | D | Resource exhaustion affects all users | 4.10 | Web and execution service (A) | Bounded workers, body/time ceilings, graph limits and rate limits | Residual | High — shared service availability can be lost | `R-014` |
| T-027 | S, T, R, D, E | Compromised dependency, action or image | 4.11 | Release artefacts and runtime (I, A) | SHA and digest pinning, scanning, provenance and signing | Residual | Critical — a compromised artefact executes with product authority | `R-015` |
| T-028 | S, T, I, D, E | Unsafe deployment configuration undermines a product control | 4.12 | All deployment assets (C, I, A) | Fail-closed profiles, start-up checks and readiness | Deployment | Critical — the affected control may be a primary trust boundary | `R-016` if applicable |
| T-029 | S, E | Reuse of a valid stolen session token | 4.1 | Identities and session tokens (C, I) | Bounded token/refresh lifetime and per-request identity-state check | Residual | High — the bearer can impersonate the identity until the credential loses authority | `R-001` |

### 5.1 STRIDE coverage by surface

Each cell either names the threat that covers the category or explains why no
separate abuse path is recorded. A threat may cover more than one category;
the register above gives the primary consequence and controls.

| Surface | S | T | R | I | D | E |
|---|---|---|---|---|---|---|
| 4.1 Authentication and sessions | T-001–T-003, T-005, T-006, T-029 | T-001, T-002 (token integrity) | Authentication events provide attribution; no separate path beyond T-029 | T-004 | T-026 covers shared web capacity | T-001–T-003, T-006, T-029 |
| 4.2 Authorisation and cross-user access | T-008 | T-007, T-009 | T-009 | T-007 | T-026 covers shared web capacity | T-007–T-009 |
| 4.3 Composer and LLM | Identity is established at 4.1; no separate planner identity | T-010–T-014 | T-013 covers review attribution | T-010 | T-015 | T-010, T-011, T-014 |
| 4.4 Pipeline, files and blobs | Identity and ownership are handled at 4.1–4.2 | T-016, T-017 | T-023 covers audit-record repudiation | T-016 | T-017, T-026 | T-016, T-017 |
| 4.5 Secrets and environment | Identity and secret ownership are handled at 4.1–4.2 | T-018, T-019 | Fingerprinted audit records address attribution; no separate path | T-018, T-019 | No distinct secret-use availability path; T-026 covers shared capacity | T-018, T-019 |
| 4.6 Network egress | Destination authority is covered by T-020 | T-020, T-021 | External-call audit covers attribution; no separate path | T-020, T-021 | T-026 covers shared capacity | T-020 |
| 4.7 Frontend rendering | T-004 | T-022 | Rendering creates no durable security claim; no separate path | T-004, T-022 | T-026 covers shared web capacity | T-022 |
| 4.8 Audit, MCP and tooling | T-024 covers misplaced tool authority | T-023 | T-023 | T-024 | T-026 covers shared service capacity | T-024 |
| 4.9 Information and observability | Identity-bearing surfaces are covered at 4.1 | Endpoints are read-only; no separate tampering path | Endpoints make no user-attributed decision; no separate path | T-025 | T-026 covers shared web capacity | Endpoints grant no additional authority; no separate path |
| 4.10 Resource exhaustion | Not applicable to the availability-only surface | Not applicable to the availability-only surface | Not applicable to the availability-only surface | Not applicable to the availability-only surface | T-015, T-026 | Not applicable to the availability-only surface |
| 4.11 Supply chain and build | T-027 (artefact identity) | T-027 | T-027 (provenance) | Disclosure is a consequence of T-027, not a separate supply-chain path | T-027 | T-027 |
| 4.12 Deployment hardening | T-028 | T-028 | Deployment records provide attribution; T-028 covers loss of that control | T-028 | T-028 | T-028 |

## 6. Severity calibration

The threat register uses this scale for the product-level consequence of a
successful abuse path. The deployment applies its approved risk matrix in the
controlled register: it reassesses consequence for the actual data and
classification, assesses likelihood, evaluates existing controls, and derives
inherent and residual ratings. Adjust threats that require a separated,
untrusted tenant when every user is a trusted operator; record the basis rather
than changing the stable threat or risk identifiers.

**Critical:** unauthenticated remote code execution; authentication bypass
or forged session or ID tokens; cross-user read or write of secrets, files,
run outputs or sessions; disclosure of environment secrets from web-authored
content; request forgery to cloud metadata or internal control planes;
arbitrary file read or write outside allowlists on production hosts;
compromise of signing keys allowing persistent token or link forgery.

**High:** stored or reflected script injection that steals bearer tokens;
path traversal within user data directories; identity-token validation
flaws that need a compromised or attacker-controlled IdP configuration;
plaintext secrets in audit records, logs or HTTP errors; the MCP query
server exposed to untrusted users with access to personal information;
authenticated resource exhaustion that reliably blocks all users despite
configured limits.

**Medium:** cross-user existence or metadata oracles without content
disclosure; rate-limit bypass enabling credential stuffing without direct
authentication bypass; prompt injection causing unsafe but authorised data
egress; request forgery limited to attacker-chosen public hosts; unbounded
payloads causing single-user denial of service; unauthenticated status
leaks useful for reconnaissance.

**Low:** self-inflicted script injection with no token exposure; verbose
validation errors revealing non-sensitive topology; CLI path or environment
expansion where only the operator controls the YAML; build, test or
developer-tool issues not reachable in production; denial of service that
needs local filesystem access or trusted administrator configuration.

## 7. Deployment record — complete in the controlled copy

Every `DEPLOYMENT-TODO:` below is intentionally unpopulated in this public
product repository. Complete the fields for the assessed installation, then
import every applicable `Residual` and `Deployment` risk reference into the
controlled register and reconcile them as required by
[15](15-risk-register.md).

| Decision | Options | Value |
|---|---|---|
| Assessed release | Commit and immutable container digest | DEPLOYMENT-TODO: |
| Tenancy model | Trusted operators only / separated users on one deployment | DEPLOYMENT-TODO: |
| Data classification and highest consequence represented in § 6 | Agency classification and impact assessment | DEPLOYMENT-TODO: |
| `workflow_governance` | `off` / `on` | DEPLOYMENT-TODO: |
| Default Composer trust mode | `auto_commit` / `explicit_approve` | DEPLOYMENT-TODO: |
| `registration_mode` (local accounts) | `open` / `email_verified` / `closed` | DEPLOYMENT-TODO: |
| Identity lifecycle and session invalidation requirement | IdP, disablement, logout, password reset and maximum tolerated credential lifetime | DEPLOYMENT-TODO: |
| Reverse-proxy client identity boundary | Trusted proxy chain and authoritative client-address source | DEPLOYMENT-TODO: |
| User secrets | enabled / server-only | DEPLOYMENT-TODO: |
| Secret wiring allowlist entries that send a secret to a network destination, and their origin pins | — | DEPLOYMENT-TODO: |
| `http.allowed_origins` required for fetching plugins in the web policy | yes / no | DEPLOYMENT-TODO: |
| Network ingress, egress and TLS controls inherited from the platform | Control and evidence references | DEPLOYMENT-TODO: |
| Who can reach `/api/system/status`, `/api/ready` and `/metrics` | — | DEPLOYMENT-TODO: |
| MCP servers deployed, and to whom they are exposed | — | DEPLOYMENT-TODO: |
| Audit export signing mode | `unsigned` / `hmac_sha256` | DEPLOYMENT-TODO: |
| Export signer and key custody | Role, key identifier, storage and rotation authority | DEPLOYMENT-TODO: |
| Signed-export verification procedure and result | Procedure/evidence reference and last verification | DEPLOYMENT-TODO: |
| Release-image signature/provenance verification | Digest, verification procedure and result | DEPLOYMENT-TODO: |
| Telemetry granularity and back end ([08 § 3.2](08-logging-audit-and-monitoring.md#32-pipeline-telemetry)) | — | DEPLOYMENT-TODO: |
| Applicable deployment threats and imported risk IDs | Reconcile every `Deployment` row in § 5 | DEPLOYMENT-TODO: |
| Residual and deployment-risk reconciliation result | Counts and controlled-register reference | DEPLOYMENT-TODO: |
| Residual risks accepted, accepting authority, scope and expiry | See [15](15-risk-register.md) | DEPLOYMENT-TODO: |
