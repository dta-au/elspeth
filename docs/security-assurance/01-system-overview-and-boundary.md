# 01 — System overview and boundary

**Status:** product baseline complete; Deployment record required ·
**Reviewed against:** `release/0.8.1` @
`49c1845085d36811b120ef1c540048463e32aabc` (2026-09-30) ·
**Owner:** ELSPETH maintainer

This document fixes the public product boundary inherited by the rest of the
assurance pack. It describes ELSPETH source, release artefacts and shipped
deployment options. It is not an assessment of a running service.

Sections headed **Deployment record** are forms for a controlled copy. An
installer completes those sections for the assessed installation without
adding hostnames, account identifiers, credentials, named role holders or
other deployment facts to this public baseline.

## 1. Product assurance scope

The target of this public baseline is the ELSPETH product/release surface:

- the source tree at the reviewed commit;
- official container and release artefacts built from that source; and
- deployment bundles shipped in `deploy/`.

Optional runtime components remain part of the documented product surface.
A named deployment includes them in its operational boundary only when it
enables them. Third-party identity, model, storage, database, email and
telemetry services are integrations rather than ELSPETH product components.
The deployment records below state where each integration sits in an assessed
system [EV-003], [EV-626], [EV-627].

### 1.1 Deployment record — assessed installation

| Item | Value |
|---|---|
| Product baseline release | `0.8.1` |
| Assessed or deployed product release | DEPLOYMENT-TODO: record the assessed release and reconcile it with the product baseline above |
| Deployed source commit | DEPLOYMENT-TODO: pin the deployed commit |
| Container or release artefact | DEPLOYMENT-TODO: record the immutable digest or release identifier and verification result |
| Hosting option and boundary | DEPLOYMENT-TODO: identify the selected bundle or operator-supplied platform |
| Enabled optional components | DEPLOYMENT-TODO: record Landscape MCP, Composer MCP, compatibility gateway and any other optional component |
| Assessment framework and scope | DEPLOYMENT-TODO: record the ISM release, classification level, Essential Eight target and other obligations |
| Commissioning body and system owner | DEPLOYMENT-TODO: |
| Assessor and assessment dates | DEPLOYMENT-TODO: |
| Agency use case | DEPLOYMENT-TODO: state who uses ELSPETH, for what work, and which decisions depend on its outputs |

## 2. System purpose

ELSPETH (Extensible Layered Secure Pipeline Engine for Transformation and
Handling) builds, validates, runs and audits data and large language model
(LLM) workflows whose outputs must be reviewed, explained and reproduced. A
pipeline is a directed graph of **sources → transforms, gates and barriers →
sinks**. The Landscape records row lineage, routing, external calls, effects
and outcomes so an output can be traced to the inputs, configuration and
external responses that produced it [EV-002], [EV-003], [EV-301], [EV-310].

ELSPETH has two primary authoring surfaces, and both target the same runtime:

1. version-controlled YAML, run through the `elspeth` command-line tool; and
2. the Web Composer, an authenticated application in which an LLM proposes
   pipeline changes and product validation and review gates admit or reject
   them.

Both paths use the same plugin contracts, graph validation, execution engine
and Landscape audit model
([Platform Architecture](../release/platform-architecture.md)) [EV-003].

## 3. Product components

| Component | Product function | Product scope | Named-deployment rule | Evidence |
|---|---|---|---|---|
| Pipeline engine | Builds and executes graphs; handles retries, checkpoints, resume and durable sink effects | In | Always present when pipelines run | [EV-002], [EV-003] |
| Command-line tool (`elspeth`) | Validates, runs, resumes, explains, purges and administers local/operator workflows | In | Host access is deployment-controlled | [EV-003] |
| Web application and frontend | FastAPI API plus the React/TypeScript single-page application | In | Present when the web service is deployed | [EV-003], [EV-010] |
| Web Composer | Planner/advisor tool loop, proposals, review and execution admission | In | Present with the web service | [EV-402], [EV-403], [EV-504] |
| Registered plugins | Source, transform and sink implementations | In | The deployment exposes only its enabled policy subset | [EV-009], [EV-013] |
| Landscape audit store | Audit, lineage, external-call and durable-effect records | In | Required for execution; storage location is deployment-controlled | [EV-301]–[EV-321] |
| Sessions store | Web identity, authoring, proposal, review, quota and run-coordination records | In | Required with the web service | [EV-306]–[EV-308] |
| Landscape analysis MCP server (`elspeth-mcp`) | Local, read-only analysis tools over a Landscape database | Optional product component | Inside the deployment boundary only when enabled | [EV-319], [EV-330] |
| Composer MCP server (`elspeth-composer`) | Local stdio pipeline-authoring tools with file-backed sessions and audit sidecars | Optional product component | Inside the deployment boundary only when enabled | [EV-019] |
| Container image | Multi-stage, non-root runtime image | In | Deployment pins and verifies an immutable image digest | [EV-627], [EV-628] |
| Deployment bundles | Docker Compose, AWS ECS, Azure Container Apps and Linux systemd definitions | In | The selected bundle becomes part of the assessed deployment | [EV-003], [EV-115], [EV-116] |
| LLM compatibility gateway | Separate service translating an organisation's model API to a supported Chat Completions subset | Optional companion component | Outside the core runtime boundary unless separately deployed and assessed | [EV-003] |
| Development tooling | CI, `elspeth-lints`, test and evaluation tooling | Development-lifecycle evidence; outside the runtime boundary | Not installed as a runtime component | [EV-604]–[EV-624] |

### 3.1 Registered plugins

The live product registry at the reviewed commit contains 9 sources, 39
transforms and 9 sinks. The web plugin policy further limits what web authors
may select, so a deployment exposes a subset [EV-009], [EV-013].

| Kind | Count | Registered plugins that can reach outside the process |
|---|---:|---|
| Sources | 9 | `aws_s3`, `azure_blob`, `dataverse`, `llm` |
| Transforms | 39 | `llm`, `blob_fetch`, `web_scrape`, `azure_ai_search`, `rag_retrieval`, `azure_content_safety`, `azure_prompt_shield`, `azure_document_intelligence`, `aws_bedrock_content_safety`, `aws_bedrock_prompt_shield`, `aws_textract_document_analysis`, `aws_textract_inline_analysis` |
| Sinks | 9 | `aws_s3`, `azure_blob`, `chroma_sink`, `database`, `dataverse` |

The exact enabled set and its operator profiles belong in the controlled
deployment record. [02](02-data-flows-and-trust-boundaries.md) describes the
corresponding boundary crossings.

### 3.2 Technology baseline

| Layer | Product baseline at the reviewed commit | Evidence |
|---|---|---|
| Language and runtime | Python 3.12+ from source; Python 3.13 in the official distroless container runtime | [EV-627] |
| Web | FastAPI served by uvicorn; React and TypeScript frontend built with Vite | [EV-003] |
| Data access | SQLAlchemy Core | [EV-003] |
| Databases | SQLite, with optional SQLCipher for the Landscape, for single-host use; PostgreSQL 16 for external-state deployment profiles | [EV-003], [EV-320] |
| Telemetry | OpenTelemetry pipeline exporters and an authenticated Prometheus operator endpoint; deployment profiles may constrain routing and granularity | [EV-322]–[EV-324] |

## 4. Product and deployment boundaries

### 4.1 Supported deployment targets

`deployment_target` selects product checks for these supported targets
[EV-010].

| Target | Product-supplied state pattern | Product boundary statement |
|---|---|---|
| `default` | Local SQLite and filesystem state under `data_dir` | Development and single-host evaluation profile |
| `docker-compose` | SQLite or the shipped PostgreSQL overlay | The operator supplies and configures browser TLS termination |
| `linux-systemd` | SQLite or PostgreSQL | The operator supplies the host, service account, proxy and network controls |
| `aws-ecs` | Aurora PostgreSQL, EFS and S3 in the shipped Terraform bundle | AWS managed services remain provider/deployer responsibilities [EV-115], [EV-320] |
| `azure-container-apps` | PostgreSQL, Azure Files and Key Vault in the shipped Bicep bundle | Azure managed services remain provider/deployer responsibilities [EV-116], [EV-224] |
| `kubernetes` | Operator-provided | ELSPETH ships no Kubernetes deployment bundle |

### 4.2 Deployment record — operational boundary

Record every external system used by the assessed installation and whether it
falls inside the system boundary.

| External system | Purpose | Inside / outside boundary | Owner |
|---|---|---|---|
| Identity provider | User sign-in | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: |
| LLM provider(s) | Composer planning/advice and LLM plugins | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: |
| PostgreSQL | Sessions and Landscape databases | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: |
| Object storage | Sources, sinks and retained payloads | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: |
| Secret store | Runtime secret resolution | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: |
| Telemetry backend | Operational metrics and traces | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: |
| Reverse proxy / load balancer | Browser TLS termination and routing | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: |
| Email delivery process/service | Delivery of local-account verification messages, if used | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: |
| MCP host and its model provider | Optional standalone Composer MCP client/LLM boundary | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: |

## 5. Data stores and data handled

The product creates or writes the following stores [EV-010], [EV-103],
[EV-212], [EV-301], [EV-306].

| Store | Product content | Location setting | Product boundary |
|---|---|---|---|
| Landscape audit database | Runs, rows, tokens, node states, external calls, routing, outcomes, authentication events and secret-resolution fingerprints | `landscape_url`; otherwise SQLite under `data_dir` | Authoritative audit record; optional SQLCipher passphrase for SQLite |
| Payload store | Retained row and call payloads referenced from the Landscape | `payload_store_path`; otherwise under `data_dir` | Default retention 90 days; purge removes eligible payloads while hashes remain [EV-325], [EV-326] |
| Sessions database | Composer sessions/messages/state, web run records, identities, local roles, SSO handoffs, quotas, preferences and encrypted user secrets | `session_db_url`; otherwise SQLite under `data_dir` | Separate from the Landscape; user secrets are encrypted and scoped [EV-212], [EV-306] |
| Local credential store (`auth.db`) | Local bcrypt password hashes, hashed verification tokens and durable verification-delivery intents | Under `data_dir` | Local authentication only; owner-only regular file [EV-103], [EV-018] |
| Email-verification delivery outbox | Email address, local user ID, delivery ID, one-time token and verification URL awaiting a deployment delivery process | `email-verifications.jsonl` under `data_dir` | Optional; owner-only file. ELSPETH does not deliver email [EV-018] |
| Managed blob directory | Files uploaded for pipeline use | Under `data_dir` | Session-scoped product storage |
| Composer MCP scratch store | Composition session JSON plus append-only tool-event sidecars | Operator-selected scratch directory | Optional local stdio component; host filesystem access controls the boundary [EV-019] |
| Pipeline outputs | Values written by configured sinks | Per sink | Outside ELSPETH after the sink effect completes |

### 5.1 Classification ceiling and banner

The operator may set `classification_banner` to `UNOFFICIAL`, `OFFICIAL`,
`OFFICIAL: Sensitive`, `PROTECTED` or `PROTECTED//CABINET`. The web application
returns the marking from `/api/system/status` and renders it in the signed-in
application shell. With no configured marking, it shows no banner [EV-016].

The banner communicates an approved ceiling; it does not inspect, classify or
block content. The deployer must set it no higher than the hosting, providers,
assessment and operating arrangements support. Users remain responsible for
keeping entered and uploaded content within that ceiling.

#### Deployment record — classification control

| Item | Value |
|---|---|
| Declared ceiling (`classification_banner`) | DEPLOYMENT-TODO: |
| Applicable PSPF, ISM and agency obligations | DEPLOYMENT-TODO: identify the requirements and local conditions of use |
| User communication | DEPLOYMENT-TODO: record induction, sign-in conditions and training |
| Spill reporting and response | DEPLOYMENT-TODO: record the reporting path and controlled incident procedure |

### 5.2 Deployment record — data classification and retention

| Data class | Product examples | Classification | Store / destination | Retention |
|---|---|---|---|---|
| Pipeline input rows | Source records and documents | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: |
| Pipeline outputs | Sink-bound results | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: |
| Uploaded files | Composer-managed input files | DEPLOYMENT-TODO: | Managed blob directory | DEPLOYMENT-TODO: |
| Composer transcripts and prompts | User messages, model calls, proposals and tool results | DEPLOYMENT-TODO: | Sessions database and approved LLM provider(s) | DEPLOYMENT-TODO: |
| Identity and session data | Provider, subject, username/display name, email when supplied, VANguard organisation ID, access state, local roles and session metadata | DEPLOYMENT-TODO: | Sessions database; local credentials/tokens in `auth.db` | DEPLOYMENT-TODO: |
| Audit records | Lineage, hashes, payload references, calls, decisions and effects | DEPLOYMENT-TODO: | Landscape and payload store | DEPLOYMENT-TODO: |

## 6. Users, roles and authentication

The provider registry contains local accounts, generic OpenID Connect (OIDC),
Microsoft Entra ID, Google Workspace and VANguard. A deployment enables one
`auth_provider`. Every successful sign-in ends with an ELSPETH-issued session
token. First-time SSO identities remain `pending` until an administrator
activates them, apart from the one-time first-administrator bootstrap rule
[EV-101]–[EV-103], [EV-107], [EV-111].

ELSPETH deliberately ignores identity-provider group and role claims. The
sessions database stores admitted identity attributes and ELSPETH's own closed
role vocabulary; directory groups grant no application authority [EV-101],
[EV-107].

### 6.1 Deployment record — user population

| User population | Approximate number | ELSPETH role(s) | Provider |
|---|---|---|---|
| Pipeline authors | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: |
| Reviewers / approvers | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: |
| Administrators | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: |
| Operators with host, CLI or MCP access | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: |

Identity roles, mutation guards and lifecycle controls are detailed in
[06](06-identity-and-access.md).

## 7. Security-relevant settings — Deployment record

The table records the stable product default or refusal condition now. A
controlled deployment copy records the selected value. `WebSettings` rejects
unknown fields and hides input values in validation errors [EV-010].

| Setting or group | Product default / contract | Security effect or refusal | Deployment value |
|---|---|---|---|
| `host`, `public_base_url` | `127.0.0.1`; none | Non-local email verification requires a public, non-loopback origin; links are not derived from the request `Host` header | DEPLOYMENT-TODO: |
| `deployment_target`, `deployment_state_mode`, `instance_id`, region/membership inputs | `default`; `auto`; a fresh instance ID is minted when omitted | Selects fail-closed deployment checks, store topology and process identity | DEPLOYMENT-TODO: |
| `data_dir`, `landscape_url`, `landscape_passphrase`, `payload_store_path`, `session_db_url` | `data`; URLs/paths derive from it when omitted | SQLite passphrase is refused for non-SQLite Landscape URLs; deployment checks enforce required store separation and transport [EV-010], [EV-320] | DEPLOYMENT-TODO: |
| `auth_provider`, `registration_mode`, `auth_rate_limit_per_minute` | `local`; `open`; 20/minute | Selects one provider. Open local registration admits reachable users; governed readiness refuses that combination. Excess auth failures are rate-limited [EV-103], [EV-106] | DEPLOYMENT-TODO: |
| SSO provider fields, endpoint origins, JWKS windows and `sso_admin_subjects` | Unset; profile-specific requirements | Startup/readiness refuses incomplete or contradictory profiles. Bootstrap subjects become inert after the first historical human-admin grant [EV-101], [EV-111] | DEPLOYMENT-TODO: |
| `secret_key`, `sso_transaction_secret`, `shareable_link_signing_key`, `operator_metrics_bearer_token` | Placeholder; unset; required with no default; unset | Non-local deployments refuse weak session keys; SSO and share tokens use purpose-separated keys; `/metrics` is 404 while its dedicated token is unset [EV-201]–[EV-204], [EV-117], [EV-323] | DEPLOYMENT-TODO: confirm source, custody and rotation plan |
| `compartment_id`, quota defaults, `identity_dormancy_days`, `identity_pending_retention_days`, `workflow_governance` | None; none; 90; 90; `off` | IdP and governed deployments require compartment/quota state; governance enables approval and separation-of-duty checks [EV-107]–[EV-113] | DEPLOYMENT-TODO: |
| `plugin_allowlist`, plugin preferences/control modes, provider profiles and bindings | No optional allowlist entries; prompt-shield and content-safety modes `recommend`; profiles empty | Policy fails closed; operator-profiled plugins remain unavailable without a valid profile. Web authors cannot set private provider bindings [EV-013], [EV-502], [EV-503] | DEPLOYMENT-TODO: |
| HTTP `allowed_origins`, `allowed_hosts`, headers and fetch limits | No exact-origin list; `public_only`; response 10 MiB; POST request 1 MiB; 30 s | Origin/address/DNS/redirect policy constrains public fetches; web-authored values cannot exceed 10 MiB request/response and 30 s [EV-011], [EV-020] | DEPLOYMENT-TODO: |
| Planner/advisor models, endpoints and same-model override | `gpt-5.5`; `anthropic/claude-sonnet-4-6`; endpoints unset; same model refused | Operator controls both destinations; endpoint and credential fields are paired; independent models are required unless explicitly overridden [EV-504] | DEPLOYMENT-TODO: |
| Composer turn/time/rate settings | Composition turns, discovery turns, timeout and per-user rate are required; no implicit values | Bounds must fit the declared transport idle ceiling; provider errors are hidden by default [EV-504] | DEPLOYMENT-TODO: |
| Planner/advisor resource budgets | Planner: 75 provider calls, 2 MiB request, 16,384 completion tokens, USD 5 cumulative cost, 2 repairs; advisor: 4 calls, 2 checkpoint passes, 4,000 prompt / 8,192 completion tokens, 60 s | Limits resource consumption and review loops; checkpoint and ordinary advisor budgets are separate [EV-504] | DEPLOYMENT-TODO: |
| `execution_rate_limit` | 60 calls/minute per unconfigured service | Operator-owned limit for external calls made by web-executed runs | DEPLOYMENT-TODO: |
| Pipeline and operator telemetry | Pipeline telemetry disabled, `lifecycle`, blocking backpressure; web operator mode `prometheus`, pipeline granularity `lifecycle` | `full` pipeline telemetry can export prompts, completions and HTTP bodies; AWS web mode permits only `lifecycle` or `rows` [EV-322], [EV-324] | DEPLOYMENT-TODO: |
| `classification_banner` | None | No banner is shown until the operator declares a supported ceiling; the banner does not classify content [EV-016] | DEPLOYMENT-TODO: |
| `payload_store_retention_days` | 90 | Informational retention period; the product does not schedule purge automatically [EV-325], [EV-326] | DEPLOYMENT-TODO: record purge schedule and legal hold |

## 8. Maturity and assessment constraints

ELSPETH is pre-release software maintained by a single developer. The project
[README](../../README.md) states that it requires use-case-specific validation,
is not ready for general production use, includes AI-generated code and remains
subject to major change.

An assessor should account for these product constraints:

- **Schema boundaries.** Crossing a sessions or Landscape schema boundary
  requires an archive/export decision and store recreation; the product does
  not provide general in-place migration or safe rollback over a newer store
  ([Platform Architecture § Operational Boundaries](../release/platform-architecture.md#operational-boundaries))
  [EV-003].
- **Support window.** Security fixes target the current release branch and
  `main` ([SECURITY.md](../../SECURITY.md)) [EV-001].
- **Assessment pinning.** A deployment assessment must name the exact source
  commit and immutable artefact digest and evaluate changes from the reviewed
  baseline [EV-625]–[EV-628].
- **Development assurance.** Automated gates, review records, test evidence
  and independent deployment testing carry particular weight for this
  development model [EV-604]–[EV-624].
