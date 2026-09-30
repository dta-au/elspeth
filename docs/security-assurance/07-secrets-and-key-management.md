# 07 — Secrets and key management

**Status:** product control baseline complete; deployment storage, custody and
rotation records open · **Reviewed against:** `release/0.8.1` @
`49c1845085d36811b120ef1c540048463e32aabc` (2026-09-30) ·
**Owner:** ELSPETH maintainer

Describes which secrets and keys ELSPETH uses, how they reach the
application, the boundaries of the controls that keep managed secret values
out of logs, audit records and model input, who may hold them, and what
rotating each one does. It expands flow F13 in
[02](02-data-flows-and-trust-boundaries.md#3-product-flows) and the
`secret_key` row of [01 § 7](01-system-overview-and-boundary.md#7-security-relevant-settings--deployment-record).

Facts in this document were checked against the tree at the commit above.
Where a secret is stored and who holds it depends on the deployment; those
items are marked **Deployment record** and completed in the controlled copy.

> **Sensitive when populated:** vault names, secret identifiers or ARNs, key
> versions and the names of people holding keys belong in the controlled
> copy, not in this public file.

Existing material:

- [guarantees.md § 12 Secret-reference handling](../release/guarantees.md#12-secret-reference-handling-rc-52)
- [Configure Key Vault secrets runbook](../runbooks/configure-keyvault-secrets.md)
- [Environment variables reference](../reference/environment-variables.md)
- [Judge signature handoff](../judge-signature-handoff.md) — operator-held
  signing key custody
- [Sharing pipelines guide](../guides/sharing-pipelines.md) — shareable-link
  key and its recovery procedure
- Source: `src/elspeth/core/security/`, `src/elspeth/web/secrets/`,
  `src/elspeth/web/key_derivation.py`

## 1. Secret inventory

Secrets are listed by type. The setting names are what the product reads;
the values, where they are stored and who holds them are deployment facts
(§ 1.6). Web settings are read from `ELSPETH_WEB__<FIELD>` environment
variables (`settings_from_env` in `src/elspeth/web/config.py`); an unknown
`ELSPETH_WEB__` name stops start-up [EV-010].

### 1.1 Web application keys

| Secret | Purpose | Setting / env var | Read by | Strength check at start-up |
|---|---|---|---|---|
| Web secret key | Root for four derived keys (§ 1.5): session-token signing, user-secret encryption, plugin-binding evidence tags, rate-limit subjects | `secret_key` / `ELSPETH_WEB__SECRET_KEY` | `src/elspeth/web/app.py` through `key_derivation.py` | Yes (§ 3) [EV-201] [EV-203] |
| SSO transaction secret | Seals the short-lived sign-in cookie holding the PKCE verifier, state and nonce (AES-256-GCM, bound to provider and redirect URI) | `sso_transaction_secret` / `ELSPETH_WEB__SSO_TRANSACTION_SECRET` | `src/elspeth/web/auth/sso.py` | Required for every SSO provider; until set, readiness names it and SSO sign-in is refused. Deployment risk decision: § 7.1 [EV-012] [EV-202] |
| SSO client secret | Authenticates ELSPETH to the identity provider as a confidential client | `sso_client_secret` / `ELSPETH_WEB__SSO_CLIENT_SECRET` | `src/elspeth/web/auth/sso.py` | Required for every SSO provider, as above [EV-012] |
| Shareable-link signing key | HMAC-SHA256 key for review-link tokens; deliberately independent of `secret_key` | `shareable_link_signing_key` / `ELSPETH_WEB__SHAREABLE_LINK_SIGNING_KEY` (base64) | `src/elspeth/web/shareable_reviews/signer.py` | Yes (§ 3); required — the service will not start without it [EV-117] [EV-203] |
| Operator metrics bearer token | Authorises scrapes of `/metrics`; tenant tokens are not accepted, and `/metrics` returns 404 when unset | `operator_metrics_bearer_token` / `ELSPETH_WEB__OPERATOR_METRICS_BEARER_TOKEN` | Web metrics route | Yes (§ 3) [EV-105] [EV-203] |
| Composer endpoint API key | Bearer credential for an operator-set OpenAI-compatible planner endpoint | `composer_endpoint_api_key` | Composer LLM client | Must be set together with `composer_endpoint_base_url`, so the client cannot fall back to an ambient provider key [EV-010] |
| Composer advisor endpoint API key | As above, for the advisor role | `composer_advisor_endpoint_api_key` | Composer advisor client | As above [EV-010] |

### 1.2 Data-store secrets

| Secret | Purpose | Setting / env var | Read by | Notes |
|---|---|---|---|---|
| Sessions database URL | Connection to the sessions database, including its credentials when PostgreSQL | `session_db_url` / `ELSPETH_WEB__SESSION_DB_URL` | Web application | The bundled AWS and Azure deployments deliver it from the secret store (§ 2.2) [EV-224] |
| Landscape database URL | Connection to the audit database | Web: `landscape_url`. CLI: `landscape.url` in settings | Web application, engine | A password in the URL is replaced by a fingerprint in the stored run configuration (§ 2.4) [EV-208] |
| Landscape SQLCipher passphrase | Encrypts a SQLite Landscape at rest | Web: `landscape_passphrase`. CLI: env var named by `landscape.encryption_key_env` (default `ELSPETH_AUDIT_KEY`) | `src/elspeth/core/landscape/database.py` | SQLite only; refused with a PostgreSQL URL. Passed to SQLCipher through a connection callback, so it is never part of the URL. Deployment risk decision: § 7.1 |
| Deployment database credentials | Schema-owner and runtime roles, and the administrative URL used to bootstrap them | AWS bundle: `ELSPETH_DB_ADMIN_URL`, `ELSPETH_DB_SCHEMA_PASSWORD`, `ELSPETH_DB_RUNTIME_PASSWORD`. Azure bundle: separate schema-owner and runtime URL secrets | Deployment bootstrap and the web task | Deployment-bundle facts; see § 2.2 [EV-224] |

### 1.3 Audit-integrity keys

| Secret | Purpose | Setting / env var | Read by | Notes |
|---|---|---|---|---|
| Fingerprint key | HMAC-SHA256 key for secret fingerprints recorded in place of values (§ 2.4) | `ELSPETH_FINGERPRINT_KEY` | `src/elspeth/contracts/security.py` | Required for Key Vault loading, for any web secret reference, and for any run whose configuration carries a recognised credential-named field. Deployment risk decision: § 7.1 [EV-207] [EV-208] |
| Audit export signing key | HMAC-SHA256 signature over exported audit records | Env var named by `landscape.export.signing_secret_ref`, with `signing_mode: hmac_sha256` and a `signer_key_id` | `src/elspeth/engine/orchestrator/export.py` | Unsigned export needs no key; signed export fails if the named variable is unset or blank [EV-226] [EV-316] |

### 1.4 Credentials for external services

| Secret | Purpose | How it is supplied | Notes |
|---|---|---|---|
| LLM provider keys | Planner, advisor and LLM transforms | CLI: `${VAR}` references in plugin options. Web: server secrets from the operator allowlist (default `OPENROUTER_API_KEY`, `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `AZURE_API_KEY`) or per-user secrets | Bedrock can use the task role, the Bedrock API key (`AWS_BEARER_TOKEN_BEDROCK`) or static IAM keys |
| Cloud data and service keys | S3, Azure Blob, Dataverse, Azure AI Search, Content Safety, Document Intelligence, Textract and similar | As above; web authors reach cloud targets through operator profiles ([02 § 4.2](02-data-flows-and-trust-boundaries.md#42-operator-profiles-for-web-authored-pipelines)) | Default server allowlist also names `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` and `AWS_SESSION_TOKEN`; the AWS default credential chain needs none of them |
| Sink database credentials | `database` sink connection | DSN in the sink's `url` option, which is the one non-heuristic field allowed to carry a secret reference | Password fingerprinted in the stored configuration [EV-208] [EV-215] |
| LLM gateway credentials | Inbound bearer and OAuth client for the optional gateway service | AWS bundle: `ELSPETH_LLM_GATEWAY_INBOUND_BEARER`, `ELSPETH_LLM_GATEWAY_OAUTH_CLIENT_ID`, `ELSPETH_LLM_GATEWAY_OAUTH_CLIENT_SECRET` | The gateway is out of scope unless deployed ([01 § 3](01-system-overview-and-boundary.md#3-product-components)) |
| Azure identity for Key Vault | Lets the CLI read Key Vault | `DefaultAzureCredential` (managed identity, workload identity, Azure CLI login or service-principal environment) | No ELSPETH setting; the vault itself is restricted (§ 2.1) |

### 1.5 Keys derived from the web secret key

`src/elspeth/web/key_derivation.py` derives one independent 32-byte key per
purpose with HKDF-SHA256, so disclosure of one derived key reveals nothing
about the others. Each `info` string carries a version suffix; a changed
derivation gets a new string.
[EV-201]

| Derived key | HKDF `info` | Used for |
|---|---|---|
| Session-token key | `elspeth-session-token-hs256-v1` | Signing and verifying HS256 session tokens (24-hour lifetime; refresh chain capped at 168 hours) |
| User-secret master key | `elspeth-user-secret-encryption-v1` | Master key for the per-user secret store (§ 2.3) |
| Plugin-binding key | `elspeth-plugin-binding-generation-v1` | HMAC tag on plugin-binding evidence recorded with each web run |
| Rate-limit key | `elspeth-rate-limit-subject-v1` | Keyed digests of rate-limit subjects, so stored buckets do not reveal who they belong to |
| SSO transaction key | `sso-transaction-v1` | Derived from `sso_transaction_secret`, **not** from `secret_key`, so each can be rotated alone |

`shareable_link_signing_key` is deliberately not derived: it is a separate
secret with its own lifecycle [EV-117].

### 1.6 Deployment record

Create a separate controlled-copy record for every long-lived secret or
credential the deployment uses. Do not combine values merely because they
share a provider or process: separate values may have different stores,
custodians, readers and rotation schedules.

| Field for each secret record | Value |
|---|---|
| Secret name or credential class | DEPLOYMENT-TODO: |
| Purpose and consuming process or job | DEPLOYMENT-TODO: |
| Secret store and exact identifier, path or ARN | DEPLOYMENT-TODO: controlled copy only |
| Current version or key identifier; creation date | DEPLOYMENT-TODO: controlled copy only |
| Generation or issue authority and approved generation method | DEPLOYMENT-TODO: |
| Delivery mechanism into the process or job | DEPLOYMENT-TODO: |
| Accountable owner and operational custodian | DEPLOYMENT-TODO: controlled copy only |
| Human and machine principals allowed to read it | DEPLOYMENT-TODO: controlled copy only |
| Principals allowed to create, change, rotate or delete it | DEPLOYMENT-TODO: controlled copy only |
| Backup, escrow or recovery arrangement, or explicit decision that none exists | DEPLOYMENT-TODO: controlled copy only |
| Scheduled rotation period, last rotation and next due date | DEPLOYMENT-TODO: |
| Compromise revocation procedure and dependent services that must restart or be reconfigured | DEPLOYMENT-TODO: |
| Retirement and destruction evidence | DEPLOYMENT-TODO: controlled copy only |

The controlled inventory must account separately for every applicable item
below. Repeat provider, cloud and CI rows once per distinct credential.

| Required inventory item | Applicable / secret-record reference |
|---|---|
| Web secret key | DEPLOYMENT-TODO: |
| SSO transaction secret | DEPLOYMENT-TODO: |
| SSO client secret | DEPLOYMENT-TODO: |
| Shareable-link signing key | DEPLOYMENT-TODO: |
| Operator metrics bearer token | DEPLOYMENT-TODO: |
| Composer endpoint API key | DEPLOYMENT-TODO: |
| Composer advisor endpoint API key | DEPLOYMENT-TODO: |
| Sessions database URL or credential | DEPLOYMENT-TODO: |
| Landscape database URL or credential | DEPLOYMENT-TODO: |
| Database administrative URL or credential | DEPLOYMENT-TODO: |
| Database schema-owner credential | DEPLOYMENT-TODO: |
| Database runtime credential | DEPLOYMENT-TODO: |
| Landscape SQLCipher passphrase | DEPLOYMENT-TODO: |
| Fingerprint key | DEPLOYMENT-TODO: |
| Audit export signing key | DEPLOYMENT-TODO: |
| Each LLM provider credential | DEPLOYMENT-TODO: repeat as needed |
| Each cloud data or service credential, including static IAM credentials if used | DEPLOYMENT-TODO: repeat as needed |
| Each sink database credential | DEPLOYMENT-TODO: repeat as needed |
| Each optional LLM-gateway credential | DEPLOYMENT-TODO: repeat as needed |
| Judge metadata HMAC key (§ 5.1) | DEPLOYMENT-TODO: |
| Each CI secret class referenced by an enabled workflow (§ 5.2) | DEPLOYMENT-TODO: repeat as needed |
| `user_secrets_enabled` decision and protection/backup of stored per-user secrets | DEPLOYMENT-TODO: |

Short-lived session JWTs, sealed SSO transaction cookies, one-time SSO
handoff codes, email-verification tokens, WebSocket tickets and shareable
review tokens are generated tokens rather than provisioned long-lived keys,
so they do not receive secret-store rows above. Record their issuer,
lifetime, storage, single-use or revocation behaviour through
[06 §§ 1.2–1.4 and 6](06-identity-and-access.md); the long-lived keys that
protect them remain in this inventory [EV-012] [EV-102] [EV-117].

Per-user secrets stored through the Web secret service are application data,
not operator-provisioned entries in this long-lived-key inventory. The
deployment still records whether `user_secrets_enabled` is allowed, how the
sessions database is backed up and protected, and the custody of the web
secret key from which their encryption keys derive.

## 2. How secrets reach the application

Managed configuration secrets are referred to by name and resolved at run
time ([guarantees § 12.1](../release/guarantees.md#121-secret-values-never-appear-in-configuration))
[EV-002].

### 2.1 Command-line pipelines

A pipeline's `secrets:` block (`SecretsConfig` in `src/elspeth/core/config.py`)
selects one source:

- **`env` (default).** Values come from the process environment. The CLI
  loads a `.env` file first unless `--no-dotenv` is given, and never
  overrides a variable that is already set. `${VAR}` references in plugin
  options are then expanded.
- **`keyvault`.** Each entry in `mapping` names an environment variable and
  the Azure Key Vault secret that fills it
  (`src/elspeth/core/security/config_secrets.py`). Controls:
  - `vault_url` must be a literal HTTPS URL with no `${VAR}`, no user
    information, no path, query or fragment, no port other than 443, no
    control characters, and a host ending in an approved Key Vault suffix
    (`.vault.azure.net` and its sovereign-cloud forms). This stops a
    settings file aiming ELSPETH's Azure credential at another host.
  - An optional deployment allowlist, `ELSPETH_KEYVAULT_ALLOWED_VAULT_URLS`,
    pins the exact vaults allowed, and is checked before any Key Vault call.
    It is set by the deployment, never in pipeline YAML.
  - The fingerprint key must be available, from the environment or from the
    same mapping, before any secret is fetched; it is fetched first.
  - All mapped secrets are fetched and fingerprinted before any is written
    to the environment, so a failure part-way leaves no partial state.
  - Successful lookups are cached for the life of the process; misses are
    not cached. [EV-205] [EV-206]

### 2.2 The web application

Web settings are read once at start-up from `ELSPETH_WEB__*` variables and
held in a frozen settings object; a change takes effect on restart. The
deployment bundles deliver the secret-bearing variables from the platform's
secret store rather than from plain values [EV-224]:

| Bundle | Mechanism | Secret-bearing variables bound (names) |
|---|---|---|
| AWS ECS (`deploy/aws-ecs/terraform/modules/scenario/ecs.tf`) | ECS task `secrets` with `valueFrom` (Secrets Manager) | `ELSPETH_WEB__SECRET_KEY`, `ELSPETH_WEB__SHAREABLE_LINK_SIGNING_KEY`, `ELSPETH_WEB__SSO_CLIENT_SECRET`, `ELSPETH_WEB__SSO_TRANSACTION_SECRET`, `ELSPETH_WEB__SESSION_DB_URL`, `ELSPETH_WEB__LANDSCAPE_URL`, `ELSPETH_WEB__COMPOSER_ENDPOINT_API_KEY`, `ELSPETH_WEB__COMPOSER_ADVISOR_ENDPOINT_API_KEY`, database bootstrap credentials and gateway credentials |
| Azure Container Apps (`deploy/azure-container-apps/workload.bicep`) | Container Apps secrets referencing versioned Key Vault secrets through a managed identity | `ELSPETH_WEB__SECRET_KEY`, `ELSPETH_WEB__SHAREABLE_LINK_SIGNING_KEY`, `ELSPETH_FINGERPRINT_KEY`, `ELSPETH_WEB__OPERATOR_METRICS_BEARER_TOKEN`, `ELSPETH_WEB__SESSION_DB_URL`, `ELSPETH_WEB__LANDSCAPE_URL`, composer endpoint keys |
| Docker Compose, Linux systemd | Environment file supplied by the operator | Example files: `deploy/compose/.env.example`, `deploy/linux-systemd/elspeth-web.env.example`. Real environment files under `deploy/` are ignored by git (`.gitignore`) |

#### 2.2.1 Deployment record — environment-file delivery

For each secret record in § 1.6, state the delivery mechanism. If an
environment file is used, complete these fields in the controlled copy:

| Environment-file decision | Value |
|---|---|
| Host or workload that reads the file | DEPLOYMENT-TODO: controlled copy only |
| File location and owner | DEPLOYMENT-TODO: controlled copy only |
| Read and write principals; filesystem permissions | DEPLOYMENT-TODO: controlled copy only |
| How the file is generated or updated without entering source control | DEPLOYMENT-TODO: |
| Restart and rollback procedure after a change | DEPLOYMENT-TODO: |
| Backup, disposal and compromise handling | DEPLOYMENT-TODO: |

### 2.3 Secrets used by web-authored pipelines

For managed secrets in recognised credential-bearing fields, a Web Composer
pipeline holds a reference marker, `{"secret_ref": "NAME"}` (optionally with
a scope), and the value is substituted only when the run starts
(`resolve_secret_refs` in `src/elspeth/core/secrets.py`). The secret service
(`src/elspeth/web/secrets/`) resolves references from two stores [EV-215]:

| Store | What it holds | Controls |
|---|---|---|
| Server secrets (`server_store.py`) | Environment variables the operator has listed in `server_secret_allowlist` | Only allowlisted names are readable; nothing else in the environment is exposed. Any `ELSPETH_*` name is refused in the allowlist at start-up and never resolved, so internal keys (web key, fingerprint key, audit passphrase) cannot be offered to users. A probe for a name that is not allowlisted cannot tell whether such a variable exists [EV-211] |
| User secrets (`user_store.py`) | Secrets a user enters for their own pipelines, in the sessions database | Encrypted with Fernet under a per-secret key derived by PBKDF2-HMAC-SHA256 (480,000 iterations, 16-byte random salt per write) from the user-secret master key (§ 1.5). Rows are scoped to the user and the sign-in provider. The operator can switch user secrets off entirely (`user_secrets_enabled = false`), leaving only operator-provisioned server secrets [EV-212] |

Further controls on this path:

- **Wiring is deny-by-default.** `secret_wiring_allowlist` lists each
  permitted `(secret, component type, plugin, option)` destination exactly.
  With no rules, no secret can be wired anywhere, and nothing the model
  writes can extend the list (`src/elspeth/web/secrets/wiring_policy.py`)
  [EV-214].
- **Placement is checked.** A marker may sit only in a credential-bearing
  field. `is_secret_field` in `src/elspeth/core/secrets.py` recognises a
  closed set of exact names and suffixes, and the database sink additionally
  recognises its whole-DSN `url` field
  (`src/elspeth/web/secrets/ref_policy.py`) [EV-215].
- **Literal credentials are refused.** A web-authored pipeline with a literal
  string in one of those recognised fields fails validation as
  `fabricated_secret` instead of being run [EV-215].
- **The model sees names only.** The composer's `list_secret_refs`,
  `validate_secret_ref` and `wire_secret_ref` tools return names, scopes and
  availability, never values (`src/elspeth/web/composer/tools/secrets.py`)
  [EV-213] [EV-215].
- **The HTTP API never returns a value.** Secret routes accept a value on
  create and return only the name and scope; response models forbid extra
  fields, so a value accidentally passed to one raises an error instead of
  being sent (`src/elspeth/web/secrets/schemas.py`) [EV-213].

These controls are not a general data-loss-prevention system. A value pasted
into ordinary user text or an unrecognised option name is outside the
credential-field policy and could enter composition state, model input or an
audit payload. Operators must provision credentials through the managed
secret path, authors must not paste values into prompts or ordinary fields,
and new credential-bearing plugin options must be added to the shared
recognition policy before use. The deployment records any accepted residual
risk in § 7.1 [EV-215].

### 2.4 What the audit trail records

For managed secret resolutions and recognised credential-bearing fields, the
audit trail records a fingerprint — an HMAC-SHA256 of the value under the
fingerprint key — in place of the value
([guarantees § 12.2](../release/guarantees.md#122-resolution-is-audited))
[EV-207] [EV-208] [EV-209] [EV-210] [EV-216].

| Where | What is recorded |
|---|---|
| `secret_resolutions` table (`src/elspeth/core/landscape/schema.py`) | For Key Vault loads: target variable, vault URL, secret name, time, latency and fingerprint. For web references: the reference name, its scope (`user` or `env`) and fingerprint. The fingerprint column only accepts a 64-character lower-case hex value [EV-209] [EV-216] |
| Stored run and node configuration | Each credential-named option is replaced by `<name>_fingerprint` (`_fingerprint_config_for_audit` and `sanitize_node_config_for_audit` in `src/elspeth/core/config.py`). Every free-form mapping in the settings model is covered, and a test discovers those mappings from the model rather than from a list (`TestAuditRedactionSectionCoverage`). A configuration that supplies both a secret and its `_fingerprint` field is refused, so a forged fingerprint cannot be injected [EV-208] |
| Database URLs | The password (in the URL, in query parameters or inside `odbc_connect`) is removed and fingerprinted; an unparsable URL that looks as if it carries credentials is refused [EV-208] |
| Outbound HTTP calls | Sensitive request headers (authorisation, API keys, tokens) are replaced by fingerprints (`src/elspeth/plugins/infrastructure/clients/fingerprinting.py`) [EV-210] |

If no fingerprint key is available, a run whose configuration contains a
credential-named value, or an authenticated HTTP call, is refused rather than
recorded without a fingerprint. The development-only override
`ELSPETH_ALLOW_RAW_SECRETS` relaxes this and must not be set in an assessed
deployment ([environment variables](../reference/environment-variables.md)).
The deployment records any use of the development override and any accepted
fingerprinting-boundary risk in § 7.1.
[EV-207] [EV-208]

Environment-sourced secrets in CLI pipelines are covered by the
configuration fingerprinting above; the `secret_resolutions` table itself is
written for Key Vault loads and web references. Values in unrecognised fields
are outside this guarantee; see § 2.3 and the deployment decision in § 7.1.
[EV-208] [EV-215]

### 2.5 When resolution fails

Resolution fails closed
([guarantees § 12.3](../release/guarantees.md#123-failed-resolution-fails-the-run))
[EV-205] [EV-213] [EV-217]:

- **CLI.** A missing Key Vault secret, an authentication failure, a request
  failure or a missing Azure SDK raises `SecretLoadError` and the run does
  not start. Only a "not found" response is treated as absence; every other
  Key Vault error propagates.
- **Web.** Every unresolved reference is collected and reported together
  (`SecretResolutionError`) and the run does not start. A missing fingerprint
  key returns HTTP 503 (`fingerprint_key_missing`); a stored user secret that
  can no longer be decrypted returns 409 (`secret_decryption_failed`) with
  advice to re-save it. Neither response contains a secret value.

## 3. Refusal of weak keys

Checked by `WebSettings` validators in `src/elspeth/web/config.py` when the
service starts. A refusal stops start-up and does not echo the rejected value
[EV-203] [EV-204].

| Key | Refused |
|---|---|
| `secret_key` | Blank; the shipped placeholder `change-me-in-production`; fewer than 32 bytes (UTF-8); a single repeated byte |
| `shareable_link_signing_key` | Missing (the field is required); a string that is not valid base64; fewer than 32 decoded bytes; a single repeated byte. String input is decoded as base64 explicitly, so a multi-byte character cannot pass the length floor with less entropy |
| `operator_metrics_bearer_token` | Shorter than 32 or longer than 512 characters; any whitespace, non-ASCII or non-printable character |
| `landscape_passphrase` | Blank; any value when the Landscape URL is not SQLite [EV-010] |
| Composer endpoint keys | A base URL without its key, or a key without its base URL [EV-010] |
| Server secret allowlist | Any `ELSPETH_*` name [EV-211] |

For `secret_key`, the placeholder, minimum-length and uniform-byte checks are
waived only for a test run bound to a loopback address — the host is
`127.0.0.1`, `localhost` or `::1` **and** the process is under pytest or has
`ELSPETH_ENV=test` (`_allow_insecure_test_keys`). For the shareable-link key,
only the uniform-byte placeholder check has that waiver; base64 decoding for
string input, the required field and the 32-byte minimum remain unconditional.
The app factory repeats the placeholder check for `secret_key` before
serving [EV-203].

Measured at `49c1845085d36811b120ef1c540048463e32aabc`: with a non-loopback host, the placeholder, a
short key and a 40-byte uniform key were each refused, and no refusal message
contained the key. Tests: `tests/unit/web/test_config.py`,
`tests/unit/web/test_config_shareable_link.py` [EV-204].

Keys not listed above are checked for presence only. The deployment records
its key-strength and generation controls in § 7.1.

## 4. Preventing disclosure

### 4.1 Settings, logs and errors

- **Masked types.** The SSO client and transaction secrets, the metrics
  token and both composer endpoint keys are `SecretStr`, and the
  shareable-link key is `SecretBytes`; their `repr()` shows a mask.
  The deployment records any residual exposure through non-masked settings
  or operator tooling in § 7.1 [EV-010].
- **No values in validation errors.** `WebSettings` sets
  `hide_input_in_errors=True`, and plugin-policy errors from environment
  loading name only the failing setting paths [EV-203].
- **Error handlers log the class, not the message.** The web handlers for
  secret, database and storage failures log the path, method, request id and
  exception class (`exc_class`), and return a fixed message
  (`src/elspeth/web/app.py`) [EV-217].
- **Passphrase kept out of URLs.** The SQLCipher passphrase is passed
  through a connection callback, never placed in the database URL.

### 4.2 Audit records and exports

- Fingerprints replace values in resolution records, stored configuration,
  database URLs and HTTP headers (§ 2.4) [EV-208] [EV-209] [EV-210].
- Contract-violation payloads written to the audit trail pass through a
  pattern-based scrubber first (`src/elspeth/contracts/secret_scrub.py`): it
  replaces the whole string on a match for common key formats (AWS, OpenAI,
  OpenRouter, GitHub, Google, Slack, JWT, PEM private keys, Azure storage
  keys and SAS signatures) and for credential-bearing connection strings and
  URLs. It is a last line of defence behind typed payloads [EV-218].
- The exporter serialises `secret_resolution` records with the resolution
  metadata and fingerprint (`tests/unit/core/landscape/test_exporter.py::TestSecretResolutionRecords::test_secret_resolution_fields`).
  Recognised credential fields in exported run and node configuration carry
  the already-fingerprinted stored configuration. This evidence does not
  establish redaction of arbitrary values in unrecognised fields; the policy
  boundary in § 2.3 still applies [EV-208] [EV-209].

### 4.3 What the composer model receives

- Managed secret values are represented by references in recognised
  credential fields (§ 2.3). The secret-service tools and HTTP response
  models return names, scopes and availability rather than resolved values,
  so values handled through that path are not sent to the planner or advisor.
  Authors must not paste secret material into ordinary composition text or
  unrecognised fields, because those inputs are outside this control
  [EV-213] [EV-215].
- Internal storage paths and blob locations are removed from state sent to
  the model by the redaction manifest (`src/elspeth/web/composer/redaction.py`).
  A committed snapshot holds a hash per manifest entry, and the
  [composer redaction gate](../../.github/workflows/composer-redaction-gate.yml)
  classifies any pull request that changes it as weakening or strengthening
  and requires the matching label, with a written rationale for a weakening
  ([policy guide](../guides/redaction-policy-changes.md)) [EV-014] [EV-220].

### 4.4 Source control

- **Pre-commit secret scanner.** `scripts/git-hooks/pre-commit-secret-scan.sh`,
  registered as the `secret-scan` hook in `.pre-commit-config.yaml`, scans
  every staged text file for credential-shaped strings matching a fixed
  pattern list: JWTs, OpenAI (`sk-` followed by an unbroken alphanumeric run)
  and Anthropic keys, Slack, AWS, Google and GitHub tokens, high-entropy bearer
  tokens, private-key blocks, connection strings with passwords, and quoted
  high-entropy values assigned to secret-like names. A match blocks the
  commit. The only per-line exception is the explicit marker
  `# secret-scan: allow-this-line`; project rules forbid bypassing the hook
  with `--no-verify` ([AGENTS.md](../../AGENTS.md#gotchas)) [EV-219].
- **Ignored environment files.** Real environment files and their backups
  under `deploy/` are excluded by `.gitignore`; only `*.example` files are
  tracked.

Server-side secret scanning and push protection are repository-hosting
controls and cannot be established from this tree. Before each release, the
ELSPETH maintainer verifies their live state in the hosting service and
records the date, repository, verifier, enabled features and any exception in
the repository administration record. That record is product operations
evidence, not a deployment/server fact and must not contain secret values.

## 5. Key custody and separation of duties

### 5.1 Judge metadata HMAC key

The `trust_tier.tier_model` lint allowlist seals each approved suppression
with an HMAC signature. Because an HMAC is symmetric, anyone holding the key
could forge an approval, so the key is held by the **operator only** — never
by CI, never by an AI agent
([custody rule \[O1\]](../judge-signature-handoff.md#the-custody-rule-o1),
[AGENTS.md](../../AGENTS.md#operator-signature-verification-tier-model-allowlist-signing))
[EV-221] [EV-222].

| Control | How it is enforced | Evidence |
|---|---|---|
| No workflow references the key | Test parses every workflow and fails on the key's name | `tests/unit/test_ci_workflow_xdist.py::test_no_workflow_references_the_operator_hmac_key` |
| CI never signs | Test fails if a signing verb appears in a `run:` step of the judge-gate workflow | `tests/unit/elspeth_lints/test_meta_ci_never_signs.py` |
| Agent tooling is key-free | The `elspeth-judge` MCP server refuses to start if the key is in its environment, and none of its tools signs; the judge's read-only tool server likewise refuses to start if the key, the judge override tokens or provider API keys are in its environment | `elspeth-lints/src/elspeth_lints/mcp/server.py`, `elspeth-lints/src/elspeth_lints/mcp/codex_judge_tools.py` |
| Agents stage, the operator signs | Agents stage an unsigned worklist; the operator's keyed `sign-bundle` / `rekey` step re-derives every binding from the tree and aborts on any staleness before writing | [Judge signature handoff](../judge-signature-handoff.md) |

CI does not verify these signatures, so a green CI run makes no claim about
them; verification is an operator step in a trusted context [EV-221].

### 5.2 CI secrets

Secret names referenced by `.github/workflows/*` at
`49c1845085d36811b120ef1c540048463e32aabc` (names only) [EV-223]:

| Workflow | Trigger | Secrets referenced | Use |
|---|---|---|---|
| `ci.yaml` | Push and pull request to protected branches | `OPENROUTER_API_KEY` | Integration tests that call a live model |
| `enforce-allowlist-judge-gates.yaml` | Push and pull request | `OPENROUTER_API_KEY` | Judge-quality policy check (no signing) |
| `build-push.yaml` | After CI on `main`, version tags, manual | `GITHUB_TOKEN`, `ACR_REGISTRY`, `AZURE_CLIENT_ID`, `AZURE_CLIENT_SECRET` | Push images to GHCR and Azure Container Registry; images are signed keyless with cosign through the workflow's OIDC token [EV-626] |
| `state-engine-live-provider.yml` | Manual only | `OPENROUTER_API_KEY`, `AZURE_API_KEY`, `AZURE_CLIENT_SECRET`, `AZURE_CONTENT_SAFETY_KEY`, `AZURE_DOCUMENT_INTELLIGENCE_KEY`, `AZURE_SEARCH_API_KEY`, `ELSPETH_STATE_ENGINE_AWS_POSTGRES_URL`, `GATEWAY_BEARER` | Live-provider tests; each job runs in a named GitHub environment |
| `codeql.yaml`, `composer-redaction-gate.yml`, `enforce-telemetry-backfill-trailer.yaml`, `mutation-testing.yaml`, `pages.yaml` | — | None | — |

No checked-in workflow references or injects the judge metadata HMAC key
(§ 5.1), a production web secret key, a share-link key, a fingerprint key or
a deployment database credential. The workflow files prove only which secret
names they reference; they do not prove which repository, organisation or
environment secrets exist in the hosting service. The live inventory and
event/fork exposure policy are therefore explicit Deployment record items
[EV-223].

### 5.3 Custody review procedure

Use this procedure for each scheduled custody review and after a material
change to a secret store, CI environment or deployment identity:

1. Enumerate human and machine principals that can read secret values,
   create or rotate versions, change access policy, administer CI secrets or
   assume a break-glass identity. Compare the live lists with every secret
   record in § 1.6.
2. Confirm least privilege at both the data plane and control plane. A
   principal that cannot read a value may still be able to grant itself read
   access through policy administration, so record and review both powers.
3. Re-confirm the separation decisions: image builders versus production-key
   holders; runtime versus schema-owner database access; and operator-only
   judge-key custody versus key-free CI and agent tooling.
4. Remove stale principals and narrow over-broad scopes. Rotate a secret when
   access cannot be shown to have remained within the approved population.
5. Exercise break-glass access without exposing values in the exercise
   record. Verify that use is logged, reviewed and followed by revocation or
   credential rotation as the deployment policy requires.
6. Record the reviewer, date, live source queried, changes, exceptions and
   evidence location in the controlled repository. Route suspected exposure
   to the compromise procedure in § 6.2.

### 5.4 Deployment record

| Item | Value |
|---|---|
| Who holds the judge metadata HMAC key, and where | DEPLOYMENT-TODO: |
| Confirmation that no repository or organisation Actions secret holds the judge key (the handoff notes that removing a workflow reference does not revoke a secret) | DEPLOYMENT-TODO: |
| Live inventory of repository, organisation and environment secrets; owning scope and enabled workflows for each | DEPLOYMENT-TODO: controlled copy only |
| Which workflow events may receive each CI secret, including fork and external-contributor policy | DEPLOYMENT-TODO: |
| Who can read production secrets in the secret store | DEPLOYMENT-TODO: |
| Who can change them | DEPLOYMENT-TODO: |
| Who can grant or change secret-store access, and how that authority is reviewed | DEPLOYMENT-TODO: |
| Separation between those who build the image and those who hold production keys | DEPLOYMENT-TODO: |
| CI secrets: owner, scope (repository or environment) and rotation | DEPLOYMENT-TODO: |
| Break-glass access to secrets | DEPLOYMENT-TODO: |
| Break-glass use logging, review and credential revocation after use | DEPLOYMENT-TODO: |

## 6. Rotation and compromise

### 6.1 What rotating each secret does

Web settings are read at start-up, so every web secret is rotated by
changing the value in the secret store and restarting (on Azure Container
Apps, a new secret version and a new revision —
[Container Apps runbook § Secret rotation](../runbooks/azure-container-apps-deployment.md#secret-rotation)).
The effect on existing state, measured from the code and pinned by
`tests/unit/web/test_key_derivation_wiring.py` [EV-202]:

| Secret rotated | Effect |
|---|---|
| `secret_key` | All four derived keys change at once. Every session token fails verification, so every user signs in again. Every stored user secret becomes undecryptable and must be re-entered (409 `secret_decryption_failed`). Rate-limit buckets start afresh. Plugin-binding evidence changes, so a web run queued before the change is refused and must be resubmitted |
| `sso_transaction_secret` | Only sign-ins in progress fail; session tokens and user secrets are unaffected |
| `sso_client_secret` | Rotate at the identity provider and in ELSPETH together; sign-in fails while they differ |
| `shareable_link_signing_key` | Every outstanding review link stops working; there is no overlap window. Links can be re-issued ([sharing guide § A signing key has been leaked](../guides/sharing-pipelines.md#a-signing-key-has-been-leaked)) [EV-117] |
| `operator_metrics_bearer_token` | Scrapers must be given the new token |
| Composer endpoint keys, provider keys | New calls use the new key after restart. A long-running CLI process keeps a Key Vault value it has already read until it exits |
| Fingerprint key | Fingerprints recorded before and after no longer match for the same value, so cross-run comparison of "same credential used" works only within one key period. Nothing already stored becomes unreadable |
| Audit export signing key | A fresh export can use the replacement key with a new `signer_key_id` when the rotation policy permits it. `multi_version` (default) permits the new signer identity; `single_export` refuses a different signer identity within the same export lineage. Existing delivered bundles are unchanged [EV-226] [EV-316] |
| Landscape SQLCipher passphrase | No in-product re-key. Deployment risk decision: § 7.1 |
| Database credentials | Change at the database and in the secret store; restart |

Audit-export key rotation changes future export snapshots only. Retain the
retired key and its public key ID under the deployment's access-controlled
historical-evidence policy for as long as an older signed bundle must be
preserved. The reviewed ELSPETH tree has internal bind/recovery/resume checks,
but it has no supported standalone verifier for a delivered bundle and no
command that re-signs an existing bundle [EV-316].

### 6.2 Generic rotation and compromise procedure

For a planned rotation:

1. Identify every consumer, delivery path and restart dependency from the
   secret record in § 1.6. Confirm rollback material and an authorised change
   window without copying the old value into the change record.
2. Generate or obtain the replacement from its authoritative issuer using
   the approved method. Give it a new version or key identifier where the
   protocol supports one.
3. Update the authoritative provider and secret store in the order required
   by that credential. For paired credentials such as an SSO client secret or
   database password, minimise the interval during which issuer and consumer
   disagree.
4. Start a new process, task or revision so start-up validation reads the new
   value. Do not treat an in-place store update as proof that a running
   process reloaded it.
5. Verify readiness and the affected path: sign-in for SSO keys, one
   authorised scrape for the metrics token, a secret-resolved test call for
   provider credentials, or database connectivity for database credentials.
   For an audit signing key, create a fresh export under the replacement
   `signer_key_id` where the rotation policy permits it and confirm the
   generated snapshot records that ID. This exercises generation; it is not
   independent verification of a delivered bundle [EV-226] [EV-316]. Check
   that logs and audit records contain identifiers or fingerprints rather
   than values.
6. Revoke or retire the old version after the new path is proven. Monitor the
   affected authentication, provider and audit signals for failures, then
   record the change, verifier, version identifiers and evidence in the
   controlled repository. For an audit export signing key, retire the old
   key from new signing but retain it under the historical-evidence policy
   while an older signed bundle is retained [EV-316].

For suspected compromise, start the deployment's
[incident response process](13-incident-response-and-continuity.md#1-purpose-scope-and-activation)
and preserve relevant logs before routine retention removes them. Disable or
revoke the exposed value at its authority, identify every consumer and the
exposure window, issue a replacement, redeploy and verify it using the steps
above, then review provider usage, authentication events and Landscape call
records for misuse. Apply the invalidation effects in § 6.1, notify affected
parties under the deployment's incident plan, and do not close the incident
until the old value is unusable and every dependent service is verified.

If an audit export signing key may be compromised, stop using it for new
exports and assess every bundle whose signing window may overlap the
exposure. Where the Landscape source records remain authoritative and the
rotation policy permits the new signer identity, generate a fresh export
under a replacement key and `signer_key_id`. Preserve the original bundle,
old key and incident findings under the historical-evidence policy; do not
present its exposed-key signature as independent integrity proof. ELSPETH has
no re-sign command and no supported standalone delivered-bundle verifier
[EV-226] [EV-316].

### 6.3 Deployment record — rotation schedule and compromise procedure

| Secret | Scheduled rotation | Procedure when suspected compromised | Who decides and who acts |
|---|---|---|---|
| Web secret key | DEPLOYMENT-TODO: | Rotate, restart, tell users to sign in and re-enter secrets, and review `auth_events` for the exposure window | DEPLOYMENT-TODO: |
| SSO client secret | DEPLOYMENT-TODO: | Revoke and replace at the IdP and ELSPETH together; verify a complete sign-in | DEPLOYMENT-TODO: |
| SSO transaction secret | DEPLOYMENT-TODO: | Replace and restart; sign-ins already in progress fail and must restart | DEPLOYMENT-TODO: |
| Shareable-link signing key | DEPLOYMENT-TODO: | Follow the [leaked-key procedure](../guides/sharing-pipelines.md#a-signing-key-has-been-leaked); re-issue required links | DEPLOYMENT-TODO: |
| Fingerprint key | DEPLOYMENT-TODO: | Replace and restart; document the new comparison period and investigate the old period using its original key under incident controls | DEPLOYMENT-TODO: |
| Audit export signing key | DEPLOYMENT-TODO: | Stop old-key signing; assess affected bundles; where source records and rotation policy permit, create a fresh export with the replacement key and `signer_key_id`; retain the old bundle and key under the historical-evidence policy. ELSPETH has no re-sign command or standalone delivered-bundle verifier [EV-226] [EV-316] | DEPLOYMENT-TODO: |
| Landscape passphrase | DEPLOYMENT-TODO: | Follow the datastore recovery decision; there is no in-product re-key | DEPLOYMENT-TODO: |
| Database credentials | DEPLOYMENT-TODO: | Revoke at the database, replace in the store, restart and verify both runtime and schema-owner paths separately | DEPLOYMENT-TODO: |
| Provider and cloud credentials | DEPLOYMENT-TODO: | Revoke at the provider, rotate, restart and review provider usage logs and Landscape call records | DEPLOYMENT-TODO: |
| Judge metadata HMAC key | DEPLOYMENT-TODO: | Run `elspeth-lints rekey` by the operator using the [handoff procedure](../judge-signature-handoff.md) | DEPLOYMENT-TODO: |
| Each CI secret | DEPLOYMENT-TODO: | Revoke at its issuer, replace at the narrowest environment/repository scope and inspect affected workflow runs | DEPLOYMENT-TODO: |

## 7. Controlled risk references

### 7.1 Deployment record

The public product document names the boundary; the controlled copy records
the deployment's decision and final risk-register identifier.

| Topic requiring a deployment decision | Controlled risk register reference or acceptance record |
|---|---|
| Strength and rotation policy for SSO secrets that receive presence checks only | DEPLOYMENT-TODO: |
| Landscape SQLCipher passphrase lifecycle and lack of in-product re-key | DEPLOYMENT-TODO: |
| Fingerprint-key custody and loss of comparison continuity on rotation | DEPLOYMENT-TODO: |
| Managed-field recognition boundary and author handling of arbitrary text or unrecognised fields | DEPLOYMENT-TODO: |
| Any assessed use of `ELSPETH_ALLOW_RAW_SECRETS` | DEPLOYMENT-TODO: |
| Exposure through non-masked settings, process environment or operator tooling | DEPLOYMENT-TODO: |
