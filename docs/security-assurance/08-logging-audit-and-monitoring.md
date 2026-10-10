# 08 — Logging, audit and monitoring

**Status:** generic product records, controls and response guidance complete;
deployment records remain open ·
**Product source reviewed against:** `release/0.8.1` @
`487ac85a377f135e012bb206e3769cd65f6fadb8` (2026-10-01) · **Owner:** DTA Cloud
Engineering

Describes what ELSPETH records, how the record is protected from tampering,
how long it is kept, and how security events are detected.

ELSPETH treats its audit trail as a product characteristic
([ADR-046](../architecture/adr/046-audit-grade-is-a-product-characteristic.md)),
which is a strong position for the ISM's event-logging controls.

Facts in this document were checked against the tree at the commit above,
by reading the code and by introspecting the live schema models. Sections
that depend on a particular deployment are marked **Deployment record**;
fill those in the controlled copy, not in this public file.

Existing material:

- [guarantees.md § 1 Audit guarantees](../release/guarantees.md#1-audit-guarantees)
  [EV-002]
- [Landscape System](../architecture/landscape.md) and
  [Landscape Entry Points](../architecture/landscape-entry-points.md).
  The inventory figures in `landscape.md` are dated; § 1.2 below is the
  measured inventory at the reviewed commit.
- [ADR-047 Landscape database clock authority](../architecture/adr/047-landscape-database-clock-authority.md)
  and [ADR-048 Required coordination token for Landscape mutations](../architecture/adr/048-required-coordination-token-for-landscape-mutations.md)
- [Audit Tier-1 violation runbook](../runbooks/audit-tier1-violation.md) and
  [Database maintenance runbook](../runbooks/database-maintenance.md)
- [Telemetry guide](../guides/telemetry.md) and
  [Landscape MCP analysis server](../guides/landscape-mcp-analysis.md)
- [guarantees.md § 16 AWS ECS operator telemetry](../release/guarantees.md#16-aws-ecs-operator-telemetry-071)

## 1. What is recorded

ELSPETH keeps four kinds of record. Only the first two are evidence.

| Channel | Store | Role | Written |
|---|---|---|---|
| **Landscape** | Landscape audit database (`landscape_url`) | Authoritative audit record of pipeline runs, authentication and administrative events, and secret resolution | Synchronously, before the operation it records is treated as done; a failed write fails the operation (§ 3.1) |
| **Session audit** | Sessions database (`session_db_url`) | Composer transcripts, proposals, review and approval decisions, session operations, audit-grade access log | Synchronously, in the same transaction as the change it records |
| **Telemetry** | Configured exporters and the web `/metrics` endpoint | Operational visibility: dashboards and alerts | After the audit write; delivery failure follows the configured policy (§ 3) |
| **Operational logs** | Process stdout/stderr (structlog; JSON with `--json-logs`) | Diagnostics, and the channel of last resort when audit or telemetry is itself failing | As events occur |

The stores themselves are described in
[01 § 5](01-system-overview-and-boundary.md#5-data-stores-and-data-handled)
and the audit-write flow in
[02 F12 and F14](02-data-flows-and-trust-boundaries.md#3-product-flows).

### 1.1 Event classes

| Event class | Recorded in | Fields | Example query / evidence |
|---|---|---|---|
| Pipeline run, row lineage, outcomes | Landscape: `runs`, `run_sources`, `nodes`, `edges`, `rows`, `tokens`, `token_parents`, `token_outcomes`, `node_states`, `routing_events` (§ 1.2) | Run configuration hash and settings, source row hash and payload reference, per-step input and output hashes, routing reason, two-axis terminal outcome (lifecycle and path) | `elspeth explain --run <RUN_ID> --row <ROW_ID> --database <DB>`; MCP `explain_token` (§ 6) |
| External calls (LLM, HTTP) | Landscape: `calls`, `operations`, `call_verifications` | Call type, status, request and response hashes and payload references, latency, token counts, error, exactly one parent (node state or operation) | MCP `get_calls`, `get_llm_usage_report`; [guarantees § 4.1](../release/guarantees.md#41-call-recording) |
| Authentication success / failure | Landscape: `auth_events` | Event type, outcome, provider, principal, failure category, request id, client host, user agent, metadata (§ 1.3) | `SELECT event_type, outcome, failure_category, occurred_at FROM auth_events WHERE username = :u ORDER BY occurred_at` |
| Session mutation | Sessions database: `composition_states`, `chat_messages`, `session_operation_receipts` and `_events`, `proposal_events` (§ 1.4) | Session owner, writer principal, version and provenance, request and result hashes | [Tier-1 runbook triage queries](../runbooks/audit-tier1-violation.md#triage-queries) |
| Administrative actions | Landscape: `auth_events` (identity, role, relationship, quota, pending-identity purge and workflow-governance events) | Actor identity, target identity, role and scope, note, `on_behalf_of`, `console_request_id`; purge summary fields and exact deleted identity IDs | `SELECT * FROM auth_events WHERE event_type IN ('role_granted','role_revoked','identity_disabled','pending_identities_purged')` |
| Secret resolution (fingerprint only) | Landscape: `secret_resolutions` | Run, source, vault URL, secret name, keyed fingerprint (64 lower-case hex), latency; never the value (§ 1.5) | MCP `query` on `secret_resolutions` by `run_id` |
| Application errors | Pipeline: Landscape `validation_errors`, `transform_errors`, `node_states.error_json`. Web: structured log events keyed by request id | Error class and reason; for web, the `X-Request-ID` every response carries | [Tier-1 runbook § Correlating a reported error](../runbooks/audit-tier1-violation.md#correlating-a-reported-error-to-its-server-log) |

### 1.2 Landscape inventory

Measured at `352430f4f659704d50efffc5dfca967ae9b73ebb` from the live SQLAlchemy metadata
(`elspeth.core.landscape.schema.metadata`): **49 tables**, schema epoch
**49**, 150 CHECK constraints and 107 foreign keys (65 of them composite,
so a record cannot bind to evidence from another run) [EV-301].

| Purpose | Tables |
|---|---|
| Run metadata and admission | `runs`, `run_attributions`, `run_sources`, `preflight_results`, `run_web_plugin_policy`, `run_start_admissions`, `elspeth_schema_identity` |
| Security events | `auth_events`, `secret_resolutions` |
| Static graph | `nodes`, `edges` |
| Data flow and lineage | `rows`, `tokens`, `token_parents`, `token_outcomes`, `token_lineage_frames`, `group_records`, `group_losses` |
| Execution and external calls | `node_states`, `operations`, `calls`, `call_verifications`, `routing_events`, `artifacts` |
| Batching and aggregation | `batches`, `batch_members`, `batch_outputs`, `aggregation_results`, `aggregation_result_members`, `aggregation_result_outputs`, `collector_group_failures` |
| Errors | `validation_errors`, `transform_errors` |
| Scheduling, coordination and recovery | `token_work_items`, `scheduler_events`, `run_coordination`, `run_coordination_events`, `run_workers`, `checkpoints` |
| Durable output effects | `sink_effect_streams`, `sink_effects`, `sink_effect_members`, `sink_effect_attempts`, `sink_effect_export_snapshots`, `coalesce_effects`, `coalesce_effect_members` |
| Audit export | `audit_export_snapshots`, `audit_export_snapshot_chunks` |
| Sidecar journal | `sidecar_journal_outbox` |

Reproduce every published count from the repository root. The final two lines
control the classifier used for the 64-hex subset: one known matching CHECK
must be accepted and one unrelated CHECK must be rejected.

```bash
PYTHONPATH=src .venv/bin/python - <<'PY'
from sqlalchemy import CheckConstraint, ForeignKeyConstraint
from sqlalchemy.dialects import sqlite

from elspeth.core.landscape.schema import SQLITE_SCHEMA_EPOCH, metadata

checks = [
    constraint
    for table in metadata.tables.values()
    for constraint in table.constraints
    if isinstance(constraint, CheckConstraint)
]
foreign_keys = [
    constraint
    for table in metadata.tables.values()
    for constraint in table.constraints
    if isinstance(constraint, ForeignKeyConstraint)
]


def is_lower_hex_64(constraint: CheckConstraint) -> bool:
    compiled = constraint.sqltext.compile(dialect=sqlite.dialect())
    expression = "".join(str(compiled).lower().split())
    return (
        "length(" in expression
        and "=64" in expression
        and "notglob'*[^0-9a-f]*'" in expression
    )


checks_by_name = {constraint.name: constraint for constraint in checks}
print("schema_epoch", SQLITE_SCHEMA_EPOCH)
print("tables", len(metadata.tables))
print("check_constraints", len(checks))
print("foreign_keys", len(foreign_keys))
print(
    "composite_foreign_keys",
    sum(len(constraint.elements) > 1 for constraint in foreign_keys),
)
print("hex64_checks", sum(is_lower_hex_64(constraint) for constraint in checks))
print(
    "positive_control_ck_runs_config_hash_hex",
    is_lower_hex_64(checks_by_name["ck_runs_config_hash_hex"]),
)
print(
    "negative_control_ck_runs_mode",
    is_lower_hex_64(checks_by_name["ck_runs_mode"]),
)
PY
```

Expected output at the reviewed commit:

```text
schema_epoch 49
tables 49
check_constraints 150
foreign_keys 107
composite_foreign_keys 65
hex64_checks 57
positive_control_ck_runs_config_hash_hex True
negative_control_ck_runs_mode False
```

The output of every sink write is itself recorded: the effect's identity and
plan are persisted before any I/O, and each attempt and its result are
recorded (`sink_effect_*`). Leadership refusals by the coordination fence are
recorded in `run_coordination_events` (§ 2.5).

### 1.3 Authentication and administrative events

`auth_events` is a closed vocabulary. The `AuthAuditEventType` literal in
`src/elspeth/contracts/auth.py` and the database CHECK constraint
`ck_auth_events_event_type` both list the same **24 event types**; a write
with any other value is refused by the database, so an unlisted event cannot
be recorded by mistake [EV-106] [EV-302].

| Group | Event types |
|---|---|
| Authentication | `login`, `token_issued`, `auth_failure`, `logout` |
| Admission and authority | `identity_activated`, `identity_disabled`, `identity_enabled`, `role_granted`, `role_revoked`, `relationship_asserted`, `relationship_revoked` |
| Workflow governance | `approval_requested`, `approval_decided`, `review_requested`, `review_request_cancelled`, `review_attested`, `library_published`, `library_accepted`, `library_rejected`, `library_deprecated`, `library_recalled` |
| Quotas | `quota_set`, `quota_exceeded` |
| Identity lifecycle maintenance | `pending_identities_purged` |

Each row carries `outcome` (`success` or `failure`), `provider` (one of the
five sign-in providers), the principal (`user_id`, `username` and the durable
`identity_id`), `request_id`, `client_host`, `user_agent` and canonical-JSON
`metadata`. A failure must carry a `failure_category` and a success must not
(`src/elspeth/core/landscape/auth_audit_repository.py`). Every row's metadata
is stamped with the deployment's `compartment_id` (when one is configured),
and callers cannot override it (`src/elspeth/web/auth/audit.py`).

How the product records them at the reviewed commit [EV-303] [EV-304]
[EV-305]:

- **Sign-in.** A successful local sign-in writes `login` and `token_issued`
  in one Landscape transaction before the token is returned; if that write
  fails, the request fails and no token is delivered
  (`src/elspeth/web/auth/routes.py`, `login`). SSO completion records
  `token_issued` as part of completing the one-time handoff.
- **Failures are classified, not free text.** `classify_authentication_failure`
  maps each refusal to a category — for example `invalid_credentials`,
  `access_pending`, `identity_disabled`, `email_unverified`,
  `tenant_claim_invalid`, `claims_invalid`, `invalid_token`,
  `provider_unavailable` — so an approval queue is not mistaken for a
  password-guessing attempt, and external data from the identity provider is
  not stored in the category ([guarantees § 11.3](../release/guarantees.md#113-authentication-failure-is-recorded)).
- **Bearer-token failures on protected API routes** (missing, malformed or
  invalid token) are recorded as `auth_failure` by `get_current_user`. Those
  writes are subject to the per-client authentication rate limit so that an
  unauthenticated caller cannot force unbounded database writes; a bearer-
  failure write skipped over the limit increments
  `auth_failure_audit.suppressed_total` by failure category
  (`src/elspeth/web/auth/middleware.py`). This counter does not cover every
  authentication rate-limit rejection: `check_auth_rate_limit` also rejects
  over-limit local sign-in, registration and SSO requests before their route
  handler runs, without writing an `auth_events` row or incrementing the
  suppression counter (`src/elspeth/web/auth/routes.py`).
- **Administrative mutations** (activation, disable, enable, role and
  relationship changes, quota changes and pending-identity purge)
  synchronously write the Landscape
  audit row before the Sessions transaction commits. If the Landscape write
  fails, the exception rolls back the Sessions mutation. The two databases
  use separate engines and transactions; ELSPETH does not claim a distributed
  transaction, so a later Sessions commit failure can leave an audit event for
  a mutation that did not commit
  (`src/elspeth/web/coordination/identity_authority.py`,
  `src/elspeth/web/auth/audit.py`). Pending purge submits one bounded summary
  containing the batch ID, configured retention, deleted count, exact deleted
  IDs and `has_more`, plus one event naming each exact `DELETE … RETURNING`
  identity, as a single Landscape batch before Sessions commit [EV-812]. Each
  event carries the acting identity,
  and `on_behalf_of` and `console_request_id` so that a change made through a
  service identity names the person who asked.
- A disabled identity is kept, not deleted, so it keeps anchoring its audit
  history.

Record deployment-specific residual-risk acceptance in
[15 — Risk register](15-risk-register.md).

Detail on roles and account lifecycle is in
[06](06-identity-and-access.md).

### 1.4 Session, composer and workflow audit

The sessions database (session schema epoch 72 in the 0.8.3 candidate)
holds the authoring record ([guarantees § 13.3](../release/guarantees.md#133-session-mutation-is-audited),
[§ 14.2](../release/guarantees.md#142-composer-transcript-is-preserved))
[EV-306].

| Record | Tables | Protection |
|---|---|---|
| Composer transcript, including tool calls and results | `chat_messages` (role, content, raw content, tool calls, writer principal, sequence), `message_ingress_receipts` | Database triggers: message content cannot be updated; rows cannot be deleted while their session exists; ingress receipts cannot be updated or deleted |
| Pipeline state history | `composition_states` (versioned, with provenance and the state it was derived from) | New versions are appended; a failed composer operation leaves the committed state unchanged |
| Proposals and decisions | `composition_proposals`, `proposal_events` (`proposal.created`, `proposal.accepted`, `proposal.rejected`, `trust_mode.changed`, `auto_commit.revoked`), `composition_rejection_events`, `interpretation_events` | Resolved interpretation events cannot be updated or deleted (trigger) |
| Completion events | `composer_completion_events` (`mark_ready_for_review`, `export_yaml`) | Cannot be updated or deleted (trigger) |
| Session operations (fork, revert) | `session_operation_receipts`, `session_operation_receipt_events` (`claimed`, `renewed`, `taken_over`, `completed`, `failed`) | Receipt events cannot be updated or deleted; a settled receipt is immutable (triggers) |
| Review and approval | `review_requests`, `review_attestations`, `approvals`, `approval_decisions`, `library_entries` | Decisions also written to `auth_events` (§ 1.3) |
| Usage | `token_usage_ledger`, `quota_provider_attempts` | — |
| Web run records | `runs`, `run_events`, `run_execution_inputs`, `run_start_permits` | Linked to the Landscape run by `landscape_run_id` |

The eleven triggers are created on both SQLite and PostgreSQL (measured by
creating a scratch SQLite sessions database and listing its triggers; the
PostgreSQL forms are defined beside them in `src/elspeth/web/sessions/models.py`).
When a user deletes a session that carries durable history — a run, a
completion event, a fork, an approval, review or library record, or usage
records — the session is soft-archived: it is hidden, and its transcript
and records are kept (`decide_and_soft_archive` in
`src/elspeth/web/coordination/repository.py`). A session with none of these
is deleted [EV-307].

A failure to write a transcript row during a compose turn fails the turn
(HTTP 500, `audit_integrity_error`) and increments the counters listed in
the [Tier-1 runbook](../runbooks/audit-tier1-violation.md#signals).

Record deployment-specific residual-risk acceptance in
[15 — Risk register](15-risk-register.md).

### 1.5 Secret resolution

Every secret resolved for a run is recorded in `secret_resolutions` with a
keyed HMAC fingerprint of the value, never the value itself
([guarantees § 12.2](../release/guarantees.md#122-resolution-is-audited)).
The database refuses a fingerprint that is not 64 lower-case hexadecimal
characters (`ck_secret_resolutions_fingerprint_hex`). Loading secrets from
Azure Key Vault is refused before any vault call unless the fingerprint key
(`ELSPETH_FINGERPRINT_KEY`) is available, so secrets cannot be fetched
without their resolution being recordable
(`src/elspeth/core/security/config_secrets.py`) [EV-309]. Key handling is in
[07](07-secrets-and-key-management.md).

### 1.6 External calls

Each LLM, HTTP, SQL or file call made by a plugin is recorded in `calls`
before its result is used: request and response hashes (64-hex, enforced by
CHECK constraints), references to the stored payloads, status, latency and
token counts ([guarantees § 4.1](../release/guarantees.md#41-call-recording)).
A call has exactly one parent — a node state or a source/sink operation —
enforced by `calls_has_parent`. In `replay` and `verify` run modes,
`call_verifications` records whether each live response matched the recorded
one [EV-310].

### 1.7 Operational logs

Logs are written with structlog to stdout/stderr, as JSON when
`--json-logs` is set (`src/elspeth/core/logging.py`); the container platform
collects them. The logging policy
(`.claude/skills/logging-telemetry-policy/SKILL.md`) limits the logger to
transitory debugging and to reporting failures of the audit or telemetry
systems themselves. Row-level decisions, call results and outcomes are not
logged, because the Landscape already holds them.

Web error handling is designed so logs do not become a second copy of user
data:

- every response carries `X-Request-ID`, and error bodies repeat it, so a
  user-quoted error can be matched to its log event;
- the terminal-error log events (`http_error_envelope`,
  `http_validation_error_envelope`, `http_audit_integrity_error`) carry the
  request id and status, and deliberately withhold envelope fields and
  request input;
- database errors are reported through a credential-scrubbing descriptor
  (`src/elspeth/core/landscape/database.py`) [EV-311].

#### Deployment record — log collection, retention and access

| Item | Value |
|---|---|
| Log destination (CloudWatch Logs, Log Analytics, journald, SIEM) | DEPLOYMENT-TODO: |
| Retention period | DEPLOYMENT-TODO: |
| Who can read logs | DEPLOYMENT-TODO: |
| Forwarding to agency SIEM | DEPLOYMENT-TODO: |

### 1.8 Reads of the audit record

Audit-grade reads of a composer transcript — `GET
/api/sessions/{id}/messages` with `include_tool_rows`, `include_llm_audit`,
`include_raw_content` or `include_rejection_reasons` — and workflow inspect
views are written to `audit_access_log` (requesting principal, path, allowed
query arguments, IP address, writer principal) **before** rows are returned.
If that write fails, the endpoint returns HTTP 500
(`audit_access_log_write_failed`) and no transcript rows
([Tier-1 runbook](../runbooks/audit-tier1-violation.md#immediate-response))
[EV-308].

The shipped writers store the exact string exposed as
`request.client.host`; they do not truncate or hash it. Physical session
deletion cascades the row, while durable-history soft archive retains it and
there is no independent expiry or purge. The reverse-proxy trust, necessity
and literal-address retention decision are recorded in
[12 § 4.2](12-privacy-impact-assessment.md#42-deployment-record--audit-access-network-address-policy)
[EV-813].

Record deployment-specific residual-risk acceptance in
[15 — Risk register](15-risk-register.md).

## 2. Integrity

### 2.1 Canonical JSON and hashes

Hashes are SHA-256 over RFC 8785 canonical JSON, versioned as
`sha256-rfc8785-v1` (`src/elspeth/contracts/hashing.py`,
`src/elspeth/core/canonical.py`;
[guarantees § 1.3](../release/guarantees.md#13-hash-integrity)):

- the same data always gives the same hash;
- NaN and Infinity are rejected, not converted;
- integers outside ±(2^53 − 1) are rejected, and a source quarantines a row
  that carries one;
- stored canonical JSON reads back to the value that was hashed.

Payloads (source rows, call requests and responses, routing reasons) are
stored content-addressed by their SHA-256 hash. The payload store verifies
the hash on every read and raises an integrity error on mismatch
(`src/elspeth/core/payload_store.py`), so an altered payload file is
detected when it is next used by `explain`, replay or resume [EV-312]
[EV-313].

### 2.2 Tier-1: crash on inconsistency

Audit data is Tier 1 ([02 § 2](02-data-flows-and-trust-boundaries.md#2-trust-tiers)).
The product refuses to write, or to present, an inconsistent record rather
than repair it:

- **Schema constraints.** 150 CHECK constraints close enumerations (run mode,
  event types, outcomes, providers), including 57 whose compiled SQLite
  expression contains the 64-character lower-case hexadecimal predicate
  (including nullable forms), and bind related fields (for example a call's
  single parent, or a replay run naming a different source run). Composite
  foreign keys keep lineage, routing and quarantine evidence inside its own
  run [EV-301].
- **Read validation.** Rows are validated as they are loaded into model
  objects (`src/elspeth/core/landscape/model_loaders.py`); a corrupt or
  cross-run row raises `AuditIntegrityError` [EV-314].
- **Fail loudly at the edge.** The web application maps `AuditIntegrityError`
  to HTTP 500 `audit_integrity_error` with no partial data in the body
  (`src/elspeth/web/app.py`); the engine stops the run.
- **No fabricated identity.** The row write path refuses a row without its
  source-supplied index and ingest sequence
  ([Landscape § Per-row source identity](../architecture/landscape.md#per-row-source-identity-adr-025)).

### 2.3 Sealed and signed exports

- **Sealed snapshots.** An audit export is first sealed into
  `audit_export_snapshots` and `audit_export_snapshot_chunks`. Database
  triggers (SQLite and PostgreSQL) validate each chunk's content hash and
  predecessor chain on insert, refuse chunks for a sealed snapshot, and
  refuse any UPDATE or DELETE of a sealed snapshot or chunk
  (`src/elspeth/core/landscape/schema.py`) [EV-315].
- **Signed exports.** With `landscape.export.signing_mode: hmac_sha256`, each
  exported record carries an HMAC-SHA256 signature, the records are chained,
  and a final manifest records the record count, chunk and snapshot hashes,
  the final chain hash, the signer key id and the manifest signature
  (`src/elspeth/contracts/audit_export.py`;
  [configuration reference § Export Settings](../reference/configuration.md#export-settings)).
  The key is resolved from a named environment variable and is never
  persisted. An enabled export must state `signing_mode` explicitly;
  `authentication_policy: required` refuses an unsigned configuration before
  the run starts [EV-316].

**Verification support.** The product's internal snapshot bind, recovery and
resume paths re-read the registered bytes and verify the complete snapshot
graph: canonical record framing, record and byte counts, content hashes,
chunk predecessor seals, snapshot and manifest hashes, authentication-event
coverage, the record-signature chain, every record HMAC, and the final
manifest HMAC. These paths require the configured signing-key bytes for an
HMAC-signed snapshot and fail closed if the verifier is absent or any check
fails (`_verify_snapshot_graph` in
`src/elspeth/core/landscape/execution/audit_export_snapshots.py`).

`elspeth audit-export verify PATH` is the supported, database-independent
verification path for a delivered JSON file or portable CSV directory. It
re-derives the canonical record stream, record signatures and chain, chunk and
snapshot graph, content hashes and final manifest. A portable CSV directory
also carries the authenticated canonical stream; the verifier regenerates
every deterministic CSV projection and refuses changed, missing, renamed,
additional, non-regular or case-colliding entries. The physical container must
match the authenticated `export_format` [EV-316].

Verification first captures every delivered regular file exactly once through
a bounded, no-follow, nonblocking descriptor into verifier-owned temporary
storage. All parsing, re-derivation and cross-file comparison then use that
private snapshot. The success result's `artifact_digest` identifies the exact
captured JSON bytes or the canonical CSV directory bundle (relative names,
hashes, sizes and schema). Mixed-file captures fail the authenticated manifest
and projection checks. Verification does not freeze the source pathname after
capture; any later consumer must preserve custody of the source or re-run
verification and match the digest immediately before use.

Verification requires HMAC authentication by default. Operators map the exact
historical `signer_key_id` to an environment-variable reference with
`--key-ref`; there is no current-key fallback and the key value is not accepted
on the command line. A deliberately unsigned export is refused unless the
operator supplies `--allow-unsigned`, and that successful result is labelled
`authenticated: false`. HMAC proves shared-secret authentication rather than
public-key non-repudiation. The deployment still decides who may access exports
and verification keys and must retain each historical key for at least the
corresponding export-retention period; record those choices in § 4.4.

### 2.4 Clock authority

Custody, liveness, expiry and lease decisions read the Landscape database's
clock, not the clock of whichever process asks
([ADR-047](../architecture/adr/047-landscape-database-clock-authority.md);
`read_landscape_decision_time` in
`src/elspeth/core/landscape/database_clock.py`), so no replica's own clock
enters those decisions and no caller can supply the time. The gate
`tests/unit/core/landscape/test_database_clock_authority.py` checks clock
provenance [EV-317].

Event timestamps (for example `auth_events.occurred_at`) are taken in UTC
from the application host.

#### Deployment record — time synchronisation

Time source and synchronisation for application and database hosts (ISM
event-log time accuracy): DEPLOYMENT-TODO:

### 2.5 Write authority with more than one writer

When more than one replica or worker writes to one Landscape, every mutation
API takes a required, keyword-only authority token of one exact owned type:
`CoordinationToken` (run leader) for run-scoped writes and
`WorkerMembershipToken` for member-, item- and claim-scoped writes
(`src/elspeth/contracts/coordination.py`;
[ADR-048 and its 2026-09-07 amendment](../architecture/adr/048-required-coordination-token-for-landscape-mutations.md)).
Each fenced transaction first checks the token against the database; a
deposed leader's write is refused and the refusal is recorded in
`run_coordination_events`. The gate
`tests/unit/architecture/test_web_landscape_mutation_fencing.py` exercises the
fence [EV-318].

### 2.6 What is detected and what is not

| Change | Detected? | How |
|---|---|---|
| A payload file is altered or replaced | Yes, on next read | Content hash verified on read (§ 2.1) |
| An audit row is written with an invalid value, broken hash format or cross-run reference | Refused at write | CHECK constraints and composite foreign keys (§ 2.2) |
| A structurally inconsistent row is read | Yes, on read | Tier-1 read validation raises `AuditIntegrityError` |
| A sealed audit-export snapshot is updated or deleted through SQL | Refused | Database triggers (§ 2.3) |
| A signed export file is altered after export | Yes, by a holder of the signing key | Record HMACs, record chain, signed manifest (§ 2.3) |
| Transcript content is updated, or a transcript row deleted, through SQL | Refused | Sessions-database triggers (§ 1.4) |
| A stale replica writes after losing leadership | Refused and recorded | Coordination fence (§ 2.5) |

These controls operate through the application and the database schema.
They are not a substitute for access control on the databases: a principal
with owner rights on a database can alter rows, stored hashes and triggers
together. Protection against privileged database users rests on the
deployment's database access control, separation of duties and
database-level audit logging.

Record deployment-specific residual-risk acceptance in
[15 — Risk register](15-risk-register.md).

### 2.7 Database access control and encryption

Product controls at the reviewed commit:

- The Landscape and sessions databases are separate stores; for PostgreSQL,
  startup checks that the two URLs are distinct
  ([02 § 6](02-data-flows-and-trust-boundaries.md#6-trust-changes-inside-the-product-boundary)).
- An SQLite Landscape can be encrypted with SQLCipher (`landscape_passphrase`).
- In the `aws-ecs` profile, startup refuses a PostgreSQL URL that does not
  use `sslmode=verify-full` with the pinned RDS trust root
  (`src/elspeth/web/deployment_contract.py`).
- Readiness refuses symlinked or group/world-writable data, payload and blob
  directories (§ 5.1).
- Read-only consumers open the Landscape read-only: SQLite through a
  `mode=ro` URI with `PRAGMA query_only=ON`, PostgreSQL with `SET
  TRANSACTION READ ONLY` (`src/elspeth/core/landscape/database.py`).

Read-only access is evidenced by [EV-319]; the AWS ECS database TLS contract
is evidenced by [EV-320].

#### Deployment record — database access and protection

| Item | Value |
|---|---|
| Database accounts and their rights (application, migration/owner, read-only analysis, DBA) | DEPLOYMENT-TODO: |
| Whether the application account can alter schema or triggers | DEPLOYMENT-TODO: |
| Database-level audit logging (for example PostgreSQL `pgaudit`, cloud database audit logs) | DEPLOYMENT-TODO: |
| Encryption at rest (database, payload store, backups) | DEPLOYMENT-TODO: |
| TLS to the database for non-AWS targets | DEPLOYMENT-TODO: |
| Host or volume access to SQLite files and the payload store | DEPLOYMENT-TODO: |

## 3. Audit versus telemetry

### 3.1 Audit primacy

The Landscape is the legal record; telemetry is operational visibility. At
every emission point the audit write happens first, synchronously; telemetry
fires only after it succeeds and is dispatched asynchronously. Logging is
used only when the audit or telemetry system itself is failing
([guarantees § 16.1 and § 16.3](../release/guarantees.md#161-audit-authority-is-unchanged)).
Consequences:

- a failed audit write fails the operation;
- a telemetry failure cannot roll back audit records that already committed;
- a telemetry record is never evidence of lineage or of a successful audit
  write;
- telemetry delivery failure can still fail the run under the configured
  policy described in § 3.2;
- investigations go back to the Landscape.

`TelemetryManager` documents and implements
emission after Landscape recording (`src/elspeth/telemetry/manager.py`);
tests include `test_telemetry_emitted_after_landscape_recording`
(`tests/unit/plugins/clients/test_llm_telemetry.py`), the composer
audit-failure primacy tests (`tests/unit/web/composer/test_audit_failure_primacy.py`)
and the sign-in audit-failure tests (`tests/unit/web/auth/test_routes.py`,
`tests/unit/web/auth/test_middleware.py`) [EV-321].

### 3.2 Pipeline telemetry

Pipelines can stream telemetry to four exporters: **OTLP**, **Azure
Monitor**, **Datadog** and **console** (`src/elspeth/telemetry/exporters/`;
[Telemetry guide](../guides/telemetry.md)). Defaults (`TelemetrySettings`):
disabled, granularity `lifecycle`, backpressure `block`,
`fail_on_total_exporter_failure: true` and
`max_consecutive_failures: 10` [EV-322].

| Granularity | Events | Data content |
|---|---|---|
| `lifecycle` | Run start and finish, phase changes, run and source spans | Identifiers and counts |
| `rows` | Adds row, transform, gate and token events | Identifiers and status; content hashes are removed before any exporter sees them |
| `full` | Adds external-call events | The event contract can carry LLM and HTTP request and response payloads; the selected exporter determines which fields cross its boundary |

At `full`, the exporters project an external-call event as follows:

| Exporter | Projection of request and response content |
|---|---|
| Console | Serialises the complete event with `event.to_dict()` to process output, including payload fields |
| Datadog | Serialises the complete event with `event.to_dict()` into span tags, including payload fields |
| Azure Monitor | Uses the generic event serializer; payload dictionaries become JSON span attributes |
| OTLP | Uses a closed allowlist. It exports bounded identifiers, status, latency and request/response hashes, but excludes payloads, provider strings, URLs, paths, credentials and exception text |

Console, Datadog and Azure Monitor can therefore disclose prompts,
completions and HTTP bodies at `full`; OTLP deliberately does not. Approve
`full` against the actual exporter and destination selected for the
deployment ([02 § 5](02-data-flows-and-trust-boundaries.md#5-data-leaving-the-product-boundary)).

An individual exporter failure is isolated by its circuit breaker and counted
in manager health (`events_dropped`, `queue_drops`, `exporter_failures` and
`consecutive_total_failures`). If every exporter fails for 10 consecutive
events under the generic default, `TelemetryManager` stores a
`TelemetryExporterError` and raises it at flush; an otherwise clean run then
fails. If `fail_on_total_exporter_failure: false`, the manager logs a critical
event, disables telemetry after the configured threshold and lets the run
continue. The AWS ECS overlay sets that option to `false`, so its task-local
operator telemetry is best effort
([Telemetry guide § Failure Handling](../guides/telemetry.md#failure-handling)).

### 3.3 Web operator metrics

The web application exposes Prometheus metrics at `/metrics`
(`src/elspeth/web/app.py`) [EV-323]:

- it returns 404 unless `operator_metrics_bearer_token` is set (32–512
  visible ASCII characters);
- the bearer token is compared in constant time; a wrong or missing token
  gets 401;
- responses are marked no-store;
- if the scrape would expose a `run_id`, `session_id` or `user_id` label, the
  endpoint returns 503 and logs `prometheus_scrape_forbidden_tenant_labels`
  instead of serving it.

Source code and OpenTelemetry APIs use dotted instrument names. Prometheus
replaces dots with underscores in `/metrics` and adds `_total` to counters
that do not already have that suffix. For example:

| OpenTelemetry instrument | Prometheus `/metrics` series |
|---|---|
| `composer.audit.tool_row_tier1_violation_total` | `composer_audit_tool_row_tier1_violation_total` |
| `auth_failure_audit.suppressed_total` | `auth_failure_audit_suppressed_total` |
| `run.failure` | `run_failure_total` |
| `external_call.failure` | `external_call_failure_total` |

### 3.4 AWS ECS mode

In the `aws-ecs` web profile, uploaded-pipeline telemetry routing is
replaced by one OTLP exporter to a fixed task-local receiver
(`127.0.0.1:4317`); only `lifecycle` and `rows` granularity are accepted;
the CloudWatch Agent sidecar, not ELSPETH, delivers metrics and traces to AWS
using the ECS task role; and
metric dimensions are restricted to bounded operator-controlled values, with
user, session, run, row, token, content, URL, exception, request, account
and task identities excluded (`src/elspeth/web/operator_telemetry.py`;
[guarantees § 16.2](../release/guarantees.md#162-aws-telemetry-routing-is-operator-owned))
[EV-324].
Collector health is visible as `operator.telemetry.export_failures`,
`operator.telemetry.queue_drops`, `operator.telemetry.collector_unavailable`
and `operator.telemetry.last_success_age_seconds` internally, and with dots
normalised to underscores in `/metrics`. The `run.failure`,
`external_call.failure` and other operator pipeline instruments are wired only
when `operator_telemetry: aws-otlp`; Prometheus-only mode does not create them.

## 4. Retention and archive

### 4.1 Retention by store

Product retention behaviour at the reviewed commit is evidenced by [EV-325].

#### Deployment record — retention periods and schedules

| Store | Product behaviour at the reviewed commit | Deployment retention |
|---|---|---|
| Landscape rows (metadata, hashes, lineage, security events) | Kept indefinitely. No product command deletes audit rows; the runbook forbids hand deletion and sets out a controlled retention procedure ([Database maintenance § Audit Database Retention](../runbooks/database-maintenance.md#audit-database-retention)) | DEPLOYMENT-TODO: |
| Payload store (row and call payloads) | Kept until an operator runs `elspeth purge`. Default retention period 90 days (`PayloadStoreSettings.retention_days`); the web setting `payload_store_retention_days` is shown on the audit-readiness panel. The product does not purge on a schedule | DEPLOYMENT-TODO: purge schedule |
| Sessions database (transcripts, proposals, decisions) | Kept. A user's session delete is a soft archive when the session has durable history, otherwise a deletion (§ 1.4) | DEPLOYMENT-TODO: |
| Sealed export snapshots | Immutable in the Landscape; content-store `retention_days` never authorises deletion of a referenced snapshot | DEPLOYMENT-TODO: |
| Operational logs and telemetry | Held by the platform, not ELSPETH (§ 1.7) | DEPLOYMENT-TODO: |

Record deployment-specific residual-risk acceptance in
[15 — Risk register](15-risk-register.md).

### 4.2 Payload purge

`elspeth purge` (`src/elspeth/cli.py`, `src/elspeth/core/retention/purge.py`)
deletes payload-store blobs for runs that completed more than the retention
period ago. It:

- considers only runs in a purge-eligible terminal state with a completion
  time before the cutoff, and protects running and interrupted runs;
- keeps any payload still referenced by a run inside the retention period
  (payloads are content-addressed and can be shared between runs);
- runs as a dry run with `--dry-run` and asks for confirmation unless `--yes`
  is given;
- supports the filesystem payload backend.

What it keeps: all Landscape rows, including every hash and payload
reference. After a purge, `explain` reports "payload no longer available"
and the affected runs' reproducibility grade is lowered
([guarantees § 1.4](../release/guarantees.md#14-payload-retention))
[EV-326].

### 4.3 Audit export and export resume

A pipeline can export its run's audit record to a sink at the end of the run
(`landscape.export`; [configuration reference § Export Settings](../reference/configuration.md#export-settings)).
At the reviewed commit the export carries 30 record types — run, node, edge, row,
token, token parent, token outcome, node state, routing event, operation,
call, call verification, artifact, batch and batch member, sink effect and
its streams, members and attempts, validation and transform errors, scheduler
events, group records and losses, collector group failures, secret
resolutions, web plugin policy and export configuration — plus the final
manifest. Authentication events are included only with `auth_events:
deployment_snapshot`. Every export is marked with the deployment compartment.

Publication uses the same durable effect protocol as other sinks, so a crash
or transient failure leaves the export recoverable. `elspeth export-resume
<RUN_ID>` shows eligibility by default and, with `--execute`, reuses the
sealed snapshot (never re-derives it) and publishes exactly once.

The record-type inventory and resume command are evidenced by [EV-327].

Record deployment-specific residual-risk acceptance in
[15 — Risk register](15-risk-register.md).

### 4.4 Backups, schema changes and legal holds

- Backups: [Backup and recovery runbook](../runbooks/backup-and-recovery.md)
  and [13](13-incident-response-and-continuity.md).
- Schema changes: crossing a Landscape or session schema epoch needs an
  archive or export decision and a new database; older stores are not
  migrated in place ([01 § 8](01-system-overview-and-boundary.md#8-maturity-and-assessment-constraints)).
  Keep the release that wrote an archived database with the archive.

#### Deployment record — retention authority, archive and export custody

| Item | Value |
|---|---|
| Records retention authority and periods (for example the *Archives Act 1983* and the agency's records authority) | DEPLOYMENT-TODO: |
| Legal-hold procedure (suspend `elspeth purge`, preserve databases and payload store) | DEPLOYMENT-TODO: |
| Archive location for retired Landscape and sessions databases | DEPLOYMENT-TODO: |
| Export sink and storage location | DEPLOYMENT-TODO: |
| Export signing mode and signer key identifier | DEPLOYMENT-TODO: |
| Export signing-key custodian and authorised verification roles | DEPLOYMENT-TODO: |

## 5. Monitoring and alerting

### 5.1 Signals the product provides

The names below are the names an operator can query. Internal OpenTelemetry
names are included in parentheses where they differ [EV-328] [EV-329].

| Signal | Where and availability | What it shows |
|---|---|---|
| Liveness | `GET /api/health` (unauthenticated) | The process can answer HTTP |
| Readiness | `GET /api/ready` (unauthenticated; 503 when not ready); `readiness_check_not_ready` log event | Nine redacted checks: `auth_mode`, `session_db`, `session_schema`, `landscape_db`, `landscape_schema`, `data_dir`, `payload_store`, `blob_dir`, `instance_membership` |
| Deployment checks | `elspeth health [--check-web]`, `elspeth doctor deployment`, `elspeth doctor aws-ecs` | Configuration and connectivity for deployment scripts and container health checks |
| Tier-1 audit counters | Prometheus: `composer_audit_tool_row_tier1_violation_total`, `composer_audit_tool_row_integrity_violation_total`, `composer_audit_audit_access_log_write_failed_total`, `composer_audit_tool_row_persist_failed_during_unwind_total`, `composer_blob_inline_audit_row_tier1_violation_total` | Audit writes or invariants failed. The two critical tool-row counters have a product target of zero ([Tier-1 runbook](../runbooks/audit-tier1-violation.md#signals)) |
| Audit reads | Prometheus: `composer_audit_audit_grade_view_total` | Successful audit-grade transcript reads |
| Authentication | Landscape `auth_events`; Prometheus `auth_failure_audit_suppressed_total` and `composer_share_token_verify_failure_total` | Classified authentication failures, bearer-failure writes suppressed by the anti-amplification limit, and rejected share tokens. Pre-handler rate-limit rejection on sign-in, registration and SSO has no `auth_events` row and does not increment the suppression counter |
| Pipeline failures | Landscape and MCP `get_error_analysis`, `diagnose`, `get_recent_activity` on every deployment; Prometheus `run_failure_total` and `external_call_failure_total` only in AWS ECS operator mode | Audited run and external-call failures. Generic Prometheus-only mode does not create the two counters |
| Pipeline telemetry health | `TelemetryManager.health_metrics`; `TelemetryExporterError` under the default total-failure policy | Attempted, delivered, failed, dropped and pending events; queue loss; individual and consecutive total exporter failures |
| AWS ECS collector health | Prometheus `operator_telemetry_export_failures`, `operator_telemetry_queue_drops`, `operator_telemetry_collector_unavailable`, `operator_telemetry_last_success_age_seconds` | Task-local OTLP delivery health; absent outside AWS ECS operator mode |
| Metrics scrape refusal | HTTP 503 and structured logs `prometheus_scrape_failed` or `prometheus_scrape_forbidden_tenant_labels` | Collection failed, or the generated exposition contained a forbidden tenant-identity label |
| Web errors | Structured logs `http_audit_integrity_error`, `http_error_envelope`, `http_validation_error_envelope`, keyed by request id | A request failed at an application or audit-integrity boundary without logging request content |

The product emits these signals; it does not ship deployable alert rules,
dashboards, a monitoring backend or alert routing.

### 5.2 Generic interpretation and first response

| Observation | Product meaning | First response and reusable guidance |
|---|---|---|
| Either critical tool-row audit counter increases | Work may have occurred without the transcript evidence required to prove it, or an audit invariant rejected the row | Stop retrying the affected compose turn, preserve the Sessions database and follow the [Tier-1 runbook](../runbooks/audit-tier1-violation.md) |
| `composer_audit_audit_access_log_write_failed_total` increases | An audit-grade read failed closed; the endpoint must have returned no transcript rows | Confirm the response contained no protected rows, preserve the database and follow the [Tier-1 runbook](../runbooks/audit-tier1-violation.md#immediate-response) |
| `http_audit_integrity_error` | A Tier-1 read-side check refused inconsistent evidence | Correlate by `X-Request-ID`, stop the affected operation and use the [Tier-1 runbook correlation procedure](../runbooks/audit-tier1-violation.md#correlating-a-reported-error-to-its-server-log) |
| `/api/ready` returns 503 or `readiness_check_not_ready` appears | At least one named startup or dependency check is not ready | Identify the named check, preserve its bounded detail, and begin the [incident-response runbook](../runbooks/incident-response.md); use the applicable deployment runbook for platform recovery |
| `prometheus_scrape_failed` | A collector raised while `/metrics` generated its response | Inspect the logged exception class and bounded detail, isolate the failing collector, and restore collection before trusting silence in dashboards |
| `prometheus_scrape_forbidden_tenant_labels` | The scrape contained `run_id`, `session_id` or `user_id`; the endpoint withheld the entire body | Treat this as a telemetry data-boundary incident, identify the instrument that added the label, and keep the scrape disabled until the label is removed |
| `TelemetryExporterError`, repeated `ALL telemetry exporters failing`, or an AWS collector-health signal | Telemetry delivery is unavailable or losing events; the Landscape remains authoritative for completed audit writes | Check endpoint reachability, credentials and exporter service status using [Telemetry guide — All Exporters Failing](../guides/telemetry.md#all-exporters-failing). Determine from configuration whether flush must fail the run or disable telemetry |
| Authentication failures or `auth_failure_audit_suppressed_total` increase | Authentication is being refused; suppression means the per-client write cap skipped bearer-failure audit writes. Endpoint rate-limit rejection is a separate signal and leaves no `auth_events` row | Query `auth_events` by category, principal and request id, then correlate endpoint 429 responses separately; distinguish access-state, token, provider, rate-limit and credential failures before responding ([06](06-identity-and-access.md)) |
| Administrative event | An identity, role, relationship, quota, pending-identity purge or workflow-governance mutation was attempted | Review actor, target, outcome, `on_behalf_of` and `console_request_id`; for purge, reconcile the bounded summary, exact per-identity rows and `has_more`; investigate an unrecognised event through the [incident-response runbook](../runbooks/incident-response.md) |
| Audited run or external-call failure | A run ended uncleanly or an external call failed | Diagnose from Landscape or the read-only MCP tools, then follow the relevant pipeline section of the [incident-response runbook](../runbooks/incident-response.md) |
| Database audit reports a direct audit-table write, trigger change or schema change | A privileged database action may have bypassed application controls | Preserve database and platform audit evidence; follow [Database maintenance](../runbooks/database-maintenance.md) and the [incident-response runbook](../runbooks/incident-response.md) |

### 5.3 Deployment record — monitoring, thresholds and alert routing

Complete this table in the controlled deployment copy. The signal names and
generic response above remain product facts; monitoring backends, query
syntax, alert thresholds, severity mappings, recipients, response targets and
deployed rule identifiers are installation decisions.

| Signal class | Monitoring backend and query | Threshold and window | Severity | Recipient | Response target | Deployed rule / local runbook |
|---|---|---|---|---|---|---|
| Tier-1 audit counters | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO:; generic [Tier-1 runbook](../runbooks/audit-tier1-violation.md) |
| Audit-access-log write failures | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO:; generic [Tier-1 runbook](../runbooks/audit-tier1-violation.md) |
| Readiness failures | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO:; generic [incident response](../runbooks/incident-response.md) |
| Metrics scrape failure or forbidden labels | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO:; generic § 5.2 |
| Authentication failures and suppressed audit writes | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO:; generic § 5.2 |
| Administrative-event review | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO:; generic § 5.2 |
| Run and external-call failures, if AWS ECS operator metrics are enabled | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO:; generic [incident response](../runbooks/incident-response.md) |
| Total exporter failure, telemetry loss or silent log pipeline | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO:; generic [telemetry guidance](../guides/telemetry.md#all-exporters-failing) |
| Database-level audit event | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO:; generic [database maintenance](../runbooks/database-maintenance.md) |

SIEM or monitoring platform, and the mechanism by which `auth_events` reach
it (scheduled query, export or read-only replica): DEPLOYMENT-TODO:

## 6. Explaining outputs and reviewing the record

| Tool | Access | Use |
|---|---|---|
| `elspeth explain --run <RUN_ID> [--row <ROW_ID> \| --token <TOKEN_ID>] --database <DB>` | Operator with the database | Full lineage for a row or token: source row, transforms, hashes, calls, routing and terminal outcome; interactive, text (`--no-tui`) or JSON (`--json`) output ([guarantees § 1.1](../release/guarantees.md#11-complete-lineage)) |
| `elspeth-mcp` Landscape analysis server | Local MCP client over stdio, with database access | 33 read-only tools (lineage, calls, errors, outcomes, performance, contracts, diagnostics). Its SQL tool accepts a single `SELECT` or `WITH … SELECT` and runs on a read-only connection (§ 2.7) ([guide](../guides/landscape-mcp-analysis.md)) [EV-330] |
| Web run inspection | Signed-in session owner | Run status, results, outputs and diagnostics (`/api/runs/{run_id}` and sub-routes) |
| Web audit-readiness panel | Signed-in session owner | Audit readiness of the current pipeline and its explanation (`/api/sessions/{id}/audit-readiness`) |
| Audit-grade transcript view | Signed-in session owner; each read logged (§ 1.8) | Tool calls, raw model output and rejection reasons behind a pipeline |
| Shareable review link | Signed-in recipient holding the link | Read-only inspect view of a frozen snapshot ([ADR-022](../architecture/adr/022-shareable-reviews.md); [02 F15](02-data-flows-and-trust-boundaries.md#3-product-flows)) |

### 6.1 Deployment record — production audit access

Who may use `explain` and `elspeth-mcp` against the production databases, and
through which read-only account: DEPLOYMENT-TODO:

## 7. Controlled risk references

### 7.1 Deployment record

The controlled copy records whether each logging and audit topic applies and,
when it does, the final risk-register identifier. An acceptance decision is
recorded on that risk; it does not replace the risk record. A `Not applicable`
disposition requires dated evidence. The reconciliation in
[15 § 6](15-risk-register.md#6-deployment-record--source-reconciliation)
counts these rows as the authoritative Document 08 source population.

| Topic requiring a deployment decision | Applicability disposition | Controlled `R-nnn` | Decision evidence |
|---|---|---|---|
| Authentication and administrative audit gaps, including suppressed writes | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: |
| Session and Composer audit durability, audit-access logging and literal client-address retention | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: |
| Production access to audit records, `explain` and Landscape analysis MCP | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: |
| Privileged database alteration, direct write authority and database encryption | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: |
| Retention, payload purge, soft-archived managed blobs, backups and legal holds | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: |
| Export signing, resume, verifier independence and evidence custody | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: |
