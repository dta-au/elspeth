# Environment Variables Reference

Reference for ELSPETH environment variables and `.env` configuration.

---

## Table of Contents

- [Automatic .env Loading](#automatic-env-loading)
- [Required Variables](#required-variables)
- [Optional Variables](#optional-variables)
- [LLM Provider Variables](#llm-provider-variables)
- [Web Deployment Variables](#web-deployment-variables)
- [Web LLM Configuration](#web-llm-configuration)
- [AWS Service Variables](#aws-service-variables)
- [Azure Service Variables](#azure-service-variables)
- [Telemetry Variables](#telemetry-variables)
- [Secret Field Detection](#secret-field-detection)
- [Example .env File](#example-env-file)
- [Security Best Practices](#security-best-practices)
- [Skipping .env Loading](#skipping-env-loading)
- [Docker and CI/CD](#docker-and-cicd)

---

## Automatic .env Loading

ELSPETH automatically loads environment variables from a `.env` file when you run any command. This eliminates the need to manually `source .env` before running pipelines.

**How it works:**

1. When any `elspeth` command runs, it looks for `.env` in the current directory
2. If not found, it searches parent directories
3. Variables from `.env` are loaded into the environment
4. Existing environment variables are **not** overwritten

---

## Required Variables

| Variable | Purpose | When Required |
|----------|---------|---------------|
| `ELSPETH_FINGERPRINT_KEY` | Secret fingerprinting | Config contains API keys or passwords |
| `ELSPETH_SIGNING_KEY` | Signed audit exports | `landscape.export.sign: true` |

### ELSPETH_FINGERPRINT_KEY

Used to HMAC-hash API keys and passwords before storing them in the audit trail. This ensures secrets are never stored in plain text while still allowing verification of which credentials were used.

Without this key, ELSPETH will refuse to run if your config contains API keys. This prevents accidental secret leakage to audit databases.

```bash
# Generate and set a secure key, then persist it in the deployment secret store
export ELSPETH_FINGERPRINT_KEY="$(python -c 'import secrets; print(secrets.token_hex(32))')"
```

### ELSPETH_SIGNING_KEY

Used to HMAC-sign exported audit records for integrity verification. Only required if you enable signed exports in your landscape configuration.

---

## Optional Variables

| Variable | Purpose | Default |
|----------|---------|---------|
| `ELSPETH_ALLOW_RAW_SECRETS` | Skip fingerprinting (development only) | `false` |
| `ELSPETH_KEYVAULT_ALLOWED_VAULT_URLS` | Pin `secrets.vault_url` to exact vaults | unset (approved-suffix check only) |
| `DATABASE_URL` | CLI/MCP audit database connection | `sqlite:///./data/audit.db` |
| `ELSPETH_WEB__REGISTRATION_MODE` | Local-auth registration mode: `open`, `email_verified`, or `closed` | `open` |
| `ELSPETH_WEB__PUBLIC_BASE_URL` | Public origin used to generate email-verification links on non-local hosts | unset |

### ELSPETH_ALLOW_RAW_SECRETS

**Development only.** When set to `true`, allows running pipelines without `ELSPETH_FINGERPRINT_KEY` even when configs contain secrets. Secrets will be stored in plain text in the audit trail.

**Never use in production.** This is intended only for local development and testing.

### ELSPETH_KEYVAULT_ALLOWED_VAULT_URLS

**Deployment-owned Key Vault allowlist.** A comma- or whitespace-separated list of exact Key Vault URLs. When set, a `secrets.vault_url` that is not an exact match is refused before any Key Vault call — closing the residual risk that a settings file points ELSPETH's Azure credentials at a real but foreign vault (e.g. one in another tenant).

Set this at the deployment/host level, **never** in pipeline YAML. When unset, `vault_url` is still restricted to approved Azure Key Vault host suffixes (`.vault.azure.net` and its sovereign-cloud variants). See the [Key Vault runbook](../runbooks/configure-keyvault-secrets.md) for details.

```bash
export ELSPETH_KEYVAULT_ALLOWED_VAULT_URLS="https://elspeth-prod-vault.vault.azure.net"
```

### ELSPETH_WEB__REGISTRATION_MODE

Controls local Composer registration. `open` creates active local users
immediately, `email_verified` creates pending users and writes one-use
verification links to `data/email-verifications.jsonl`, and `closed` disables
self-registration.

Non-local `email_verified` deployments must also set
`ELSPETH_WEB__PUBLIC_BASE_URL` to a public origin, for example
`https://elspeth.example.gov.au`. The value must be an origin only: no path,
query, or fragment.

---

## LLM Provider Variables

These keys serve CLI/batch pipelines directly. The web application reads the
same names for its Composer models and for server-scoped LLM profile
credentials — see [Web LLM Configuration](#web-llm-configuration).

### OpenRouter

| Variable | Purpose |
|----------|---------|
| `OPENROUTER_API_KEY` | API key for OpenRouter LLM service |

Used by the `llm` transform (provider: openrouter).

### Azure OpenAI

| Variable | Purpose |
|----------|---------|
| `AZURE_OPENAI_API_KEY` | API key for Azure OpenAI service |
| `AZURE_OPENAI_ENDPOINT` | Azure OpenAI resource endpoint URL |

Used by `llm` (provider: azure) transforms.

**Endpoint format:** `https://example-resource.openai.azure.com`

## Web Deployment Variables

These variables define the web process's deployment and persistence contract.
See the [deployment platform matrix](deployment-platforms.md) before selecting
a target.

| Variable | Allowed values / purpose |
| --- | --- |
| `ELSPETH_WEB__DEPLOYMENT_TARGET` | `default`, `docker-compose`, `linux-systemd`, `aws-ecs`, `azure-container-apps`, or `kubernetes`. The `azure-container-apps` value is reserved; no supported Container Apps bundle ships in this release. |
| `ELSPETH_WEB__DEPLOYMENT_STATE_MODE` | `auto`, `sqlite-single`, or `external-postgresql`. Production cloud targets require `external-postgresql`; native Linux can use `sqlite-single` on one host. |
| `ELSPETH_WEB__SESSION_DB_URL` | Session database URL. External mode requires PostgreSQL. |
| `ELSPETH_WEB__LANDSCAPE_URL` | Landscape database URL. Keep it distinct from the session database. |
| `ELSPETH_WEB__DATA_DIR` | Persistent application data directory. |
| `ELSPETH_WEB__PAYLOAD_STORE_PATH` | Persistent payload directory. Payload persistence is separate from database persistence. |
| `ELSPETH_WEB__SECRET_KEY` | Required session-token signing secret. Generate at least 32 random bytes and keep the value stable across restarts. |
| `ELSPETH_WEB__SHAREABLE_LINK_SIGNING_KEY` | Required base64-encoded shareable-link signing key. Generate with `openssl rand -base64 32` and keep the value stable across restarts. |
| `ELSPETH_WEB__COMPOSER_MAX_COMPOSITION_TURNS` | Required positive maximum for Composer build/revision turns. The standalone Docker example uses `15`. |
| `ELSPETH_WEB__COMPOSER_MAX_DISCOVERY_TURNS` | Required positive maximum for Composer discovery turns. The standalone Docker example uses `10`. |
| `ELSPETH_WEB__COMPOSER_TIMEOUT_SECONDS` | Required positive Composer timeout in seconds. The standalone Docker example uses `180.0`. |
| `ELSPETH_WEB__COMPOSER_RATE_LIMIT_PER_MINUTE` | Required positive per-user Composer request limit. The standalone Docker example uses `60`. |
| `WEB_CONCURRENCY` | Web worker count. Set exactly `1` for every supported deployment. |

For `aws-ecs`, both PostgreSQL URLs must include `sslmode=verify-full` and one
nonblank `sslrootcert` value naming the trusted CA bundle. Startup and
`elspeth doctor aws-ecs` reject omitted, plaintext, and non-verifying TLS modes
before opening a database connection.

The image includes both PostgreSQL clients, not the PostgreSQL server. Use
`postgresql+psycopg://` for psycopg v3 or `postgresql+psycopg2://` for
psycopg2. Initialize new external databases once with
`elspeth doctor deployment --init-schema`; ordinary web startup validates but
does not create schemas.

---

## Web LLM Configuration

The web application has **three independent LLM surfaces**. Configuring one
does not configure the others, and a deployment that sets only some of them
fails in the specific ways described below. All of these settings are read
once at startup: restart the web service after changing any of them.

| Surface | What it powers | Configured by |
| --- | --- | --- |
| Composer primary model | The Composer chat/tool loop that authors pipelines | `ELSPETH_WEB__COMPOSER_MODEL` |
| Composer advisor model | The mandatory independent reviewer inside the compose loop | `ELSPETH_WEB__COMPOSER_ADVISOR_MODEL` |
| Operator LLM profiles | The `llm` transform inside web-authored pipelines | `ELSPETH_WEB__LLM_PROFILES` |
| Tutorial profile | Which profile the first-run tutorial's `llm` node uses | `ELSPETH_WEB__TUTORIAL_LLM_PROFILE` |

### Composer models

`ELSPETH_WEB__COMPOSER_MODEL` and `ELSPETH_WEB__COMPOSER_ADVISOR_MODEL` are
LiteLLM model identifiers. Use a provider-prefixed form (`bedrock/...`,
`openrouter/...`); the provider is inferred from the prefix. Unprefixed
`gpt-*`, `o1*`, `o3*`, and `o4*` names infer OpenAI, and unprefixed `claude*`
names infer Anthropic; any other unprefixed name makes the Composer
unavailable with a provider-inference error.

The defaults (`gpt-5.5` primary, `anthropic/claude-sonnet-4-6` advisor) are
development conveniences. Production deployments should set both explicitly.

**The two models must differ.** The advisor is the independent reviewer of
the primary Composer's work, so the service refuses to start when both
resolve to the same canonical model id. Distinctness is checked on the final
path segment, so a provider prefix cannot mask a same-model pairing:
`bedrock/anthropic.claude-x` and `openrouter/anthropic/claude-x` count as the
same model.

Composer credentials come from the web process environment, keyed by the
inferred provider. Both the primary and the advisor contract must be
satisfied:

| Inferred provider | Required environment variable |
| --- | --- |
| `bedrock` | None. LiteLLM uses the ambient AWS credential chain (ECS task role, instance profile, `AWS_*` variables). Never inject static access keys, profiles, or endpoint overrides. |
| `openrouter` | `OPENROUTER_API_KEY` |
| `openai` | `OPENAI_API_KEY` |
| `anthropic` | `ANTHROPIC_API_KEY` |
| `azure` / `azure_ai` | `AZURE_API_KEY` |

A missing or empty required key does not stop the service: the Composer is
reported unavailable, with the missing variable named, through the sanitized
`GET /api/system/status` surface, and compose requests fail until the key is
provided.

At startup the service also sends one trivial probe request to each Composer
model (`ELSPETH_WEB__COMPOSER_BOOT_PROBE_ENABLED`, default `true`). A
provider *bad request* — for example a model that rejects the configured
`ELSPETH_WEB__COMPOSER_TEMPERATURE` or `ELSPETH_WEB__COMPOSER_SEED` — fails
startup, because that is a fixable operator configuration error. Transient
provider, auth, or network failures do not block boot; the Composer is
exercised again at first use.

### Operator LLM profiles (`ELSPETH_WEB__LLM_PROFILES`)

Web authors never choose a provider, model, endpoint, or credential for an
`llm` transform. The operator defines named **profiles**, and the web `llm`
node's public schema exposes exactly one selector: a required `profile`
field whose enum is the set of profile aliases usable by that signed-in
user. Every private binding (`provider`, `model`, `api_key`, `endpoint`,
`region_name`, `deployment_name`, ...) is rejected if authored directly, and
pipeline state and audit records store only the opaque alias.

`ELSPETH_WEB__LLM_PROFILES` is a JSON object mapping alias → profile
definition. Aliases are lowercase identifiers (letters/digits, words joined
with `-` or `_`).

| Field | Applies to | Notes |
| --- | --- | --- |
| `provider` | all | `bedrock`, `openrouter`, or `azure` |
| `model` | all | Bedrock: LiteLLM `bedrock/<model-id>` form. Azure: must equal `deployment_name`. |
| `credential_scope` | openrouter, azure | `server` or `user`. Required for these providers; forbidden for Bedrock. |
| `credential_ref` | openrouter, azure | Uppercase secret name (for example `OPENROUTER_API_KEY`). Required for these providers; forbidden for Bedrock. |
| `region_name` | bedrock | Optional; the ambient AWS region applies when omitted. Forbidden for OpenRouter and Azure. |
| `endpoint`, `deployment_name`, `api_version` | azure | `endpoint` (HTTPS, credential-free) and `deployment_name` are required for Azure; all three are forbidden for other providers. |
| `timeout_seconds` | openrouter | Only OpenRouter profiles accept an explicit timeout (default 60, max 300 seconds). |
| `max_tokens` | all | Optional completion-token cap. |

Credential rules:

- **Bedrock profiles are keyless.** They authenticate through the AWS default
  credential chain of the web process (the ECS task role on AWS). Setting
  `credential_scope` or `credential_ref` on a Bedrock profile is a startup
  error.
- **OpenRouter and Azure profiles are credentialed.** With
  `credential_scope: server`, the `credential_ref` name resolves as an
  environment variable of the web process; the name must appear in
  `ELSPETH_WEB__SERVER_SECRET_ALLOWLIST` (JSON array; default
  `OPENROUTER_API_KEY`, `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`,
  `AZURE_API_KEY`), the variable must be set and non-empty, and
  `ELSPETH_FINGERPRINT_KEY` must be set because secret use is fingerprinted
  into the audit trail. With `credential_scope: user`, the reference resolves
  through the signed-in user's own uploaded secret store instead.

A profile whose credential cannot be resolved for a given user is unusable
for that user. When a user has no usable profile at all, the `llm` transform
is hidden from that user's discovery and authoring surfaces entirely;
CSV/JSON/text authoring is unaffected.

Example — one keyless Bedrock profile and one server-credentialed OpenRouter
profile (collapse each value to a single line in an environment file):

```bash
ELSPETH_WEB__LLM_PROFILES='{
  "bedrock-haiku": {"provider": "bedrock", "model": "bedrock/anthropic.claude-3-haiku-20240307-v1:0", "region_name": "ap-southeast-2"},
  "sonnet": {"provider": "openrouter", "model": "anthropic/claude-sonnet-4.6", "credential_scope": "server", "credential_ref": "OPENROUTER_API_KEY"}
}'
ELSPETH_WEB__TUTORIAL_LLM_PROFILE=sonnet
```

Malformed values fail startup with sanitized errors: JSON fields report
"must be valid JSON object/array", and profile validation names the failing
field path without echoing the raw value.

### Tutorial profile and the launch contract

`ELSPETH_WEB__TUTORIAL_LLM_PROFILE` names the profile alias the first-run
tutorial's `llm` node must select.

- It must name a key of `ELSPETH_WEB__LLM_PROFILES`; the service refuses to
  start otherwise.
- When unset, the first-run tutorial is disabled — the launch path returns a
  typed HTTP 409 (`tutorial_profile_unavailable`) — without hiding ordinary
  CSV/JSON/text authoring.
- The named alias is listed first in the `profile` enum that authors see.

The tutorial pipeline has a fixed shape: one `csv` or `json` source →
`web_scrape`, `llm`, and `field_mapper` transforms → one `json` sink. The
authenticated launch path re-checks the complete contract immediately before
creating a run and returns a typed HTTP 409 naming the first failure:

- the tutorial profile is configured and usable by the launching user;
- `transform:web_scrape`, `transform:llm`, and `transform:field_mapper` are
  all installed and available;
- when the prompt-shield / content-safety control modes are `required`, the
  selected implementations must be authorized, available, and able to cover
  the tutorial pipeline.

**Configuring the tutorial profile is not sufficient by itself.** After any
change to the policy variables, verify the tutorial rows in
`GET /api/system/status` before signing off a deployment.

### Interplay with the plugin allowlist

`ELSPETH_WEB__PLUGIN_ALLOWLIST` (JSON array of kind-qualified ids such as
`"transform:passthrough"`) authorizes *optional* plugins on top of the
required web core, which is always authorized:

- sources: `source:csv`, `source:json`, `source:text`
- transforms: `transform:field_mapper`, `transform:llm`,
  `transform:web_scrape`
- sinks: `sink:csv`, `sink:json`, `sink:text`

Consequences:

- An empty or unset allowlist authorizes exactly the required core — it does
  **not** mean "allow every installed plugin". Any other installed plugin
  stays hidden from every web surface until listed.
- The tutorial transforms (`web_scrape`, `llm`, `field_mapper`) are part of
  the required core, so this release cannot lose them through the allowlist.
  Deployment templates should still list them explicitly next to the
  tutorial profile (as the AWS scenario module does) so the tutorial's
  dependency set stays visible in the deployment configuration.
- Listing a plugin that is not installed fails startup with a sanitized
  policy error.
- Authorization is not availability: `transform:llm` is always authorized,
  but it is *available* to a user only when at least one profile in
  `ELSPETH_WEB__LLM_PROFILES` is usable by that user. With no profiles
  configured, no user can author LLM nodes.

### Where each deployment sets these

**AWS ECS (Terraform scenario module).**
`deploy/aws-ecs/terraform/modules/scenario/locals.tf` renders the seven
protected plugin-policy variables (`ELSPETH_WEB__PLUGIN_ALLOWLIST`,
`ELSPETH_WEB__PLUGIN_PREFERENCES`, `ELSPETH_WEB__PLUGIN_CONTROL_MODES`,
`ELSPETH_WEB__LLM_PROFILES`, `ELSPETH_WEB__TUTORIAL_LLM_PROFILE`, and the
two Bedrock Guardrail variables) plus `ELSPETH_WEB__COMPOSER_MODEL` and
`ELSPETH_WEB__COMPOSER_ADVISOR_MODEL` into the web task definition. Its
`variables.tf` requires both Composer models to be `bedrock/...` ids and
requires them to differ. The scenario defines one keyless Bedrock profile
aliased `tutorial` (reusing the Composer model id) and points
`ELSPETH_WEB__TUTORIAL_LLM_PROFILE` at it; all Bedrock access flows through
the ECS task role, and no `OPENROUTER_API_KEY` secret is wired when both
Composer models are Bedrock. See the
[AWS ECS deployment runbook](../runbooks/aws-ecs-deployment.md) ("Web plugin
policy rollout") and the
[Bedrock model-selection runbook](../runbooks/aws-ecs-bedrock-opus-sonnet.md).

**Plain environment file (systemd / staging).** Set the same variables in
the service's environment file (the Linux runbook installs
`deploy/linux-systemd/elspeth-web.env.example` as
`/etc/elspeth/elspeth-web.env`), each JSON value on a single line, then
restart the web service. The example file carries the LLM variables as a
commented block — uncomment and set them. A typical OpenRouter-backed deployment
sets `ELSPETH_WEB__COMPOSER_MODEL` and `ELSPETH_WEB__COMPOSER_ADVISOR_MODEL`
(`openrouter/...` ids), `OPENROUTER_API_KEY`, `ELSPETH_WEB__LLM_PROFILES`
(a server-scoped OpenRouter profile referencing `OPENROUTER_API_KEY`), and
`ELSPETH_WEB__TUTORIAL_LLM_PROFILE` together, alongside
`ELSPETH_FINGERPRINT_KEY` so server-scoped credentials resolve. See the
[Ansible Ubuntu deployment runbook](../runbooks/ansible-ubuntu-deployment.md).

---

## AWS Service Variables

### Web control selection (prompt shield and content safety)

These four variables decide which safety controls the web surface offers, how
strictly it enforces them, and which operator-owned Guardrail each control
binds to. They are part of the same protected plugin-policy assignment as the
LLM variables above, and the process policy is frozen at startup — restart the
web service after changing any of them.

| Variable | Purpose |
|----------|---------|
| `ELSPETH_WEB__PLUGIN_PREFERENCES` | Ordered implementation choice per capability, e.g. `{"prompt_shield":["transform:aws_bedrock_prompt_shield"]}` |
| `ELSPETH_WEB__PLUGIN_CONTROL_MODES` | `required` or `recommend` per capability |
| `ELSPETH_WEB__BEDROCK_GUARDRAIL_PROFILES` | Operator-owned Guardrail bindings, each with `alias`, `plugin`, `guardrail_identifier`, `guardrail_version`, `region` |
| `ELSPETH_WEB__BEDROCK_GUARDRAIL_DEFAULT_PROFILES` | Which alias each control plugin uses by default |

`required` is the consequential setting. Under `required`, a web-authored
pipeline is rejected unless every LLM node is covered: the prompt shield must
dominate the node's input, and content safety must dominate **every** one of
its output streams. Authors cannot opt out, and the planner wires the controls
rather than recommending them. Under `recommend` the control is advisory and an
uncovered LLM node is allowed.

Guardrail profiles keep the same operator/author separation as LLM profiles.
Web authors select only an opaque `profile` alias, row `fields`, `schema`, and —
for content safety — `source`. The Guardrail identifier, immutable numeric
version, and region are operator-owned and lowered only for validation and
execution, so an author can never read or forge them. Credentials come from the
AWS default SDK chain; grant Bedrock permissions to the task role and never put
access keys or custom endpoints in profile or pipeline configuration.

See [configuration.md](configuration.md#environment-configuration) for a
complete worked example, and the plugin's YAML options under
[AWS Bedrock Guardrail shields](configuration.md#aws-bedrock-guardrail-shields).

### Amazon Textract

The `aws_textract_document_analysis` transform has **no environment
variables**. It is user-configurable per node (region, the row fields carrying
the S3 bucket and key, and the requested feature types), and it authenticates
through the ordinary AWS credential chain — the ECS task role in a container
deployment.

Two things gate it instead of an environment variable:

- **The allowlist.** It is not part of the required web core, so
  `ELSPETH_WEB__PLUGIN_ALLOWLIST` must include
  `"transform:aws_textract_document_analysis"` before any web surface offers it.
- **IAM.** The task role needs `textract:StartDocumentAnalysis` and
  `textract:GetDocumentAnalysis`. Without them the pipeline composes and
  validates cleanly and then fails at run time with `AccessDenied`, because
  authorization is not checked until the job is submitted.

See [AWS Textract document analysis](configuration.md#aws-textract-document-analysis)
for the plugin options, the output-field choices, and why extracted document
text must be treated as untrusted content.

### AWS credentials

AWS-backed plugins (`aws_s3` source and sink, the two Bedrock Guardrail
controls, Textract, and the `llm` transform with `provider: bedrock`) resolve
credentials through the standard AWS SDK chain rather than ELSPETH-specific
variables. In a container deployment that means the task role; locally it means
whatever `AWS_PROFILE` / `AWS_REGION` and the usual `AWS_*` variables resolve
to. Do not place access keys in plugin options.

---

## Azure Service Variables

### Azure Content Safety

| Variable | Purpose |
|----------|---------|
| `AZURE_CONTENT_SAFETY_KEY` | API key for Azure Content Safety |
| `AZURE_CONTENT_SAFETY_ENDPOINT` | Content Safety resource endpoint URL |

Used by the `azure_content_safety` transform plugin.

### Azure Prompt Shield

| Variable | Purpose |
|----------|---------|
| `AZURE_PROMPT_SHIELD_KEY` | API key for Azure Prompt Shield |
| `AZURE_PROMPT_SHIELD_ENDPOINT` | Prompt Shield resource endpoint URL |

Used by the `azure_prompt_shield` transform plugin.

### Azure Document Intelligence

| Variable | Purpose |
|----------|---------|
| `AZURE_DOCUMENT_INTELLIGENCE_KEY` | API key for Azure AI Document Intelligence |

Used by the `azure_document_intelligence` transform plugin when the plugin
option references `${AZURE_DOCUMENT_INTELLIGENCE_KEY}`. Configure the Document
Intelligence endpoint in the transform's `options.endpoint`; there is no
implicit endpoint environment variable.

### Azure Blob Storage

| Variable | Purpose |
|----------|---------|
| `AZURE_STORAGE_CONNECTION_STRING` | Connection string for Azure Blob Storage |

Used by `azure_blob` source and sink plugins.

---

## Telemetry Variables

### OpenTelemetry (OTLP)

| Variable | Purpose |
|----------|---------|
| `OTEL_ENDPOINT` | OTLP endpoint URL (non-sensitive; can be set in YAML instead) |
| `OTEL_TOKEN` | Auth token for OTLP exporter (sensitive) |

### Azure Application Insights

| Variable | Purpose |
|----------|---------|
| `APPLICATIONINSIGHTS_CONNECTION_STRING` | Application Insights connection string (sensitive) |

### Datadog

| Variable | Purpose |
|----------|---------|
| `DD_API_KEY` | Datadog API key for Datadog Agent deployment (not read by ELSPETH Datadog exporter options) |

---

## Secret Field Detection

ELSPETH automatically detects and fingerprints fields containing secrets based on naming patterns:

**Exact matches:**
- `api_key`
- `token`
- `password`
- `secret`
- `credential`

**Suffix patterns:**
- `*_secret`
- `*_key`
- `*_token`
- `*_password`
- `*_credential`

Fields matching these patterns in your configuration will be HMAC-hashed using `ELSPETH_FINGERPRINT_KEY` before being stored in the audit trail.

---

## Example .env File

Create a `.env` file in your project root:

```bash
# .env - ELSPETH environment configuration

# =====================================================================
# ELSPETH Security Settings
# =====================================================================

# Every value below is deliberately fake. Replace it from an approved secret
# store and keep this file untracked.

# Secret fingerprinting key (REQUIRED for production). Generate 32 random bytes
# and keep the resulting value stable for audit correlation.
ELSPETH_FINGERPRINT_KEY=fake_fingerprint_key_for_docs_only

# Signing key for audit exports (optional)
# Enables HMAC signatures on exported audit records
ELSPETH_SIGNING_KEY=fake_signing_key_for_docs_only

# =====================================================================
# LLM API Keys
# =====================================================================

# OpenRouter (for llm transform with provider: openrouter)
OPENROUTER_API_KEY=fake_openrouter_key_for_docs_only

# Azure OpenAI (for llm (provider: azure) transforms)
AZURE_OPENAI_API_KEY=fake_azure_openai_key_for_docs_only
AZURE_OPENAI_ENDPOINT=https://example-resource.openai.azure.com

# =====================================================================
# Azure Content Safety Services
# =====================================================================

# Azure Content Safety (for azure_content_safety transform)
AZURE_CONTENT_SAFETY_KEY=fake_content_safety_key_for_docs_only
AZURE_CONTENT_SAFETY_ENDPOINT=https://example-resource.cognitiveservices.azure.com

# Azure Prompt Shield (for azure_prompt_shield transform)
AZURE_PROMPT_SHIELD_KEY=fake_prompt_shield_key_for_docs_only
AZURE_PROMPT_SHIELD_ENDPOINT=https://example-resource.cognitiveservices.azure.com

# Azure Document Intelligence (for azure_document_intelligence transform)
AZURE_DOCUMENT_INTELLIGENCE_KEY=fake_document_intelligence_key_for_docs_only

# =====================================================================
# Azure Storage
# =====================================================================

# Azure Blob Storage (for azure_blob source/sink)
AZURE_STORAGE_CONNECTION_STRING=fake_azure_storage_connection_string_for_docs_only

# =====================================================================
# Web Composer Local Registration
# =====================================================================

# Set to closed for managed deployments, or email_verified when an operator or
# mailer will deliver links from data/email-verifications.jsonl.
ELSPETH_WEB__REGISTRATION_MODE=closed
# Required for non-local email_verified deployments.
# ELSPETH_WEB__PUBLIC_BASE_URL=https://elspeth.example.gov.au

# =====================================================================
# Telemetry (secrets only)
# =====================================================================

# OTLP auth token (optional; required if your OTLP endpoint enforces auth)
OTEL_TOKEN=fake_otel_token_for_docs_only

# Azure Application Insights connection string
APPLICATIONINSIGHTS_CONNECTION_STRING=fake_application_insights_connection_string_for_docs_only

# Datadog API key (optional if using local agent)
DD_API_KEY=fake_datadog_api_key_for_docs_only

# =====================================================================
# Development Settings (DO NOT USE IN PRODUCTION)
# =====================================================================

# Skip secret fingerprinting (development only!)
# ELSPETH_ALLOW_RAW_SECRETS=true
```

---

## Security Best Practices

### 1. Never commit .env to version control

Add to `.gitignore`:

```gitignore
.env
.env.local
.env.*.local
```

### 2. Use different keys per environment

```bash
# Production: generate once, store securely, and keep stable
export ELSPETH_FINGERPRINT_KEY="$(openssl rand -hex 32)"

# Development: generate a separate local-only value
export ELSPETH_FINGERPRINT_KEY="$(openssl rand -hex 32)"
```

### 3. Keep production keys stable

The fingerprint key affects how secrets appear in audit trails. Changing it mid-pipeline means you can't correlate which API key was used across runs.

### 4. Rotate API keys, not fingerprint keys

When you rotate an LLM provider API key, the new key gets a new fingerprint automatically. The `ELSPETH_FINGERPRINT_KEY` should remain stable to maintain audit consistency.

---

## Skipping .env Loading

In CI/CD or containerized environments where secrets are injected externally:

```bash
# Skip .env loading entirely
elspeth --no-dotenv run -s settings.yaml --execute
```

This is useful when:
- Secrets are injected via CI/CD environment variables
- Running in Kubernetes with secrets mounted
- Using Docker with `-e` flags

---

## Docker and CI/CD

When running ELSPETH in containers, pass environment variables directly:

```bash
docker run --rm \
  -e ELSPETH_FINGERPRINT_KEY="${ELSPETH_FINGERPRINT_KEY}" \
  -e OPENROUTER_API_KEY="${OPENROUTER_API_KEY}" \
  -e DATABASE_URL="sqlite:////app/data/audit.db" \
  -v elspeth-data:/app/data \
  -v $(pwd)/config:/app/config:ro \
  ghcr.io/johnm-dta/elspeth:${IMAGE_TAG:?set IMAGE_TAG to an exact published tag} \
  run --settings /app/config/pipeline.yaml --execute
```

For docker-compose:

```yaml
services:
  elspeth:
    image: ghcr.io/johnm-dta/elspeth:${IMAGE_TAG:?set an immutable sha-* or v* tag}
    environment:
      - ELSPETH_FINGERPRINT_KEY=${ELSPETH_FINGERPRINT_KEY}
      - OPENROUTER_API_KEY=${OPENROUTER_API_KEY}
      - DATABASE_URL=${DATABASE_URL:-sqlite:////app/data/audit.db}
    volumes:
      - elspeth-data:/app/data
```

See the [Docker Deployment Guide](../guides/docker.md) for complete container usage instructions.

---

## See Also

- [Configuration Reference](configuration.md) - Complete pipeline configuration options
- [Docker Deployment Guide](../guides/docker.md) - Container deployment
- [User Manual](../guides/user-manual.md) - Day-to-day CLI usage
