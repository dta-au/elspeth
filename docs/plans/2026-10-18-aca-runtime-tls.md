# 0.8.8 — ACA enhancements

Attribute this work to 0.8.8 (ACA enhancements). Integrate the remaining runtime
database TLS admission and doctor changes into the existing Composer 0.8.7
workstream's combined candidate, now targeting 0.8.9. First-install instructions
and their test-harness repairs already landed in main through PR #321.
Qualify the combined candidate before merging. Cloud publication, deployment
and paid service acceptance remain separate.

## Ownership and release integration

| Stream | Current ownership |
| --- | --- |
| Composer 0.8.7 workstream | Sole integrator of the combined candidate; Composer repairs, shared guard admission, Azure adapter and selective bespoke-detector retirement; AWS/Bedrock and GCP adapters remain on its master list |
| ACA, attributed to 0.8.8 | Remaining TLS admission, doctor evidence, affected tests and operating documentation; supply reviewed changes to the existing integrator |
| Engine 0.8.9 workstream | Engine fixes under its existing separate controller; actual accepted source must be integrated and qualified before claiming it delivered |

The combined candidate targets 0.8.9; the Composer workstream retains its 0.8.7
name and ACA attribution remains 0.8.8. A target version does not establish
integration, qualification or release. The existing Composer controller owns
final application, conflict resolution, release metadata and changelog
consistency. Preserve the ACA branch and evidence until its changes are
verified in the destination. Do not create a competing integration writer.

Verify exact accepted refs and source custody before integration. Reconcile the
TLS runbook edits with the integrator's required-guard configuration repair;
do not replace either change with an older file. Run one frozen full default
and serial PostgreSQL qualification pair on the combined candidate under the
existing allocator. Occupied runners do not block independent source review,
documentation repair or integration preparation.

## Implemented components and remaining work

The TLS implementation at `bdeaa6d2fc3d4fae708780b72d5adffd9a247230` has
independent source review, affected unit/startup/documentation tests, static
checks, compiled Bicep, frontend build and a terminal real-PostgreSQL
positive/negative selection. Those are bounded local receipts, not full
qualification or live Azure acceptance.

The ACA branch also contains the first-install owner's reviewed source
`c4addcd05762a1876d2fda64fe1da034a0aa98ce`: transport-ceiling export, explicit
Azure deployment pricing identities, fresh-store operating guidance and offline
instruction rehearsal, plus reviewed timing controls in tests and the Playwright
startup harness. Those changes are already in main at
`31a6cc6adaf338783eda2617d1e279360522880d` through PR #321; do not import them
again as new work. Their predecessor receipts do not qualify the combined
candidate with the remaining TLS changes and the other workstreams.

Remaining work:

1. Complete independent review of the exact combined batch and resolve findings.
2. Run frozen full default and serial PostgreSQL qualification when the existing
   allocator grants actual safe capacity. An occupied slot blocks those runs,
   not independent review, documentation or integration preparation.
3. Complete the existing controller's required authoring acceptance before
   remote push, keeping private acceptance inputs out of repository material.
   Complete exact-head required CI and protected landing through the existing
   release process, then verify the resulting main state.

No historical grant reserves test capacity. A new allocation names its owner
and releases at terminal driver plus children, or a concrete preflight refusal.
Do not interrupt valid CI or launch competing broad suites. Preserve Composer
priority at the next natural boundary. Timing questions never relax priorities,
verification or required fixes.

## Outcome and reviewed baseline gap

ACA startup and `doctor deployment` must reject database URLs that do not
require authenticated PostgreSQL TLS. Doctor must report TLS evidence for each
initial preflight schema-inspection connection for Sessions and Landscape.
This proves those doctor connections. Initialization uses separate connections
with the same validated URLs; the checks do not observe every future web
connection or establish deployed acceptance.

At reviewed base `a0336e9ab61f80ab631c9159f818224589ffba5a`,
`src/elspeth/web/deployment_contract.py` applied authenticated TLS URL checks
only to AWS ECS, and `src/elspeth/web/doctor.py` requested TLS inspection only
for AWS checks. The ACA runbook already required verified TLS but warned that
doctor did not measure it. The candidate closes those bounded gaps.

## Contract and compatibility

- ACA requires exactly one explicit `sslmode=verify-full` and one nonblank
  `sslrootcert`, either `system` or an absolute CA file path. Validation does
  not read or print the path. Keep the existing driver allowlist.
- Inspect raw decoded query pairs with blank values retained before SQLAlchemy
  normalization. Reject repeated TLS parameters, including blank duplicates and
  encoded keys, and reject the legacy `requiressl` alias in any position.
  Reject disagreement between URL parsers over decoded credentials; preserve
  correctly percent-escaped credentials. Fragment syntax must not hide query
  parameters from the validator.
- If `gssencmode` is explicit, accept only one `disable` value. Omission remains
  supported for documented ACA TCP/TLS deployment; do not claim arbitrary
  Kerberos transport behavior or create a global environment policy.
- Retain AWS's immutable RDS-root requirement and other targets' contracts.
- Reuse `postgres_tls_check` on each database's initial doctor schema-inspection
  connection. Failed prerequisites produce explicit failed TLS checks without
  connecting. Missing/failed TLS evidence prevents inspection/initialization.
  Preserve stale-store refusal and fresh-schema-only initialization.
- Initialization's final schema-verification connection is not independently
  observed by these checks; its URL retains the authenticated TLS policy.
  Preserve AWS's existing generic/dedicated doctor-wrapper behavior.
- Previously accepted insecure ACA URLs now fail startup/preflight with redacted
  corrective diagnostics. This deliberately enforces the existing ACA operating
  requirement. There is no data-format, epoch, migration or reset change.

PostgreSQL documents certificate/hostname verification and system roots in
[libpq connection parameters](https://www.postgresql.org/docs/16/libpq-connect.html).
Microsoft documents verified TLS and explicit CA files for Flexible Server in
[TLS connection setup](https://learn.microsoft.com/en-us/azure/postgresql/security/security-tls-how-to-connect).

## Falsification and qualification

Contract tests cover both database fields and supported drivers, valid roots,
missing/blank/repeated parameters, weaker modes, aliases, parser disagreement,
correctly escaped credentials, encoded query keys and diagnostic redaction.
Retain positive AWS and neutral-target controls. Doctor tests prove checks use
the initial inspection connection, prerequisite failures do not connect and
TLS failures prevent initialization. The actual startup entry point rejects
insecure ACA settings before database work.

Real PostgreSQL tests retain the provider-neutral disposable certificate
fixture. Prove success, untrusted CA refusal and reachable hostname-mismatch
refusal without mutation. DNS failure is not proof of hostname verification.
Run PostgreSQL serially, with no conflicting suite, using explicit testcontainer
selection. Mock and unit tests do not prove the driver enforces TLS.

Bind both worktree Python roots and verify import provenance. Use the canonical
frozen gate for Ruff, mypy, contracts, keyless lints, default tests and serial
PostgreSQL. Read terminal stage exit codes and frozen status. Compare keyless
trust-tier findings to baseline; do not claim operator signature clearance,
suppress failures or hand-edit signed metadata. Run affected documentation and
whole-tree checks appropriate to the final paths. Repeat checks only when
intervening changes or unresolved concerns justify them.

Runbook and ACA skill text must describe measured doctor connections and their
limits. Protected authoring also needs the shared owner's required guard
configuration before readiness. Only its explicit guard-scoped development
exception may permit absent guard configuration; configured block, error or
unavailability still refuses. Neither raw-secret nor signing-tool settings
silently grant guard-bypass authority. The shared owner controls the exact
setting and enforcement contract; ACA must document the accepted configuration
when integrating it, without inventing a competing default or implementation.

## Fresh installation and commissioning

The operator selected a separate fresh ACA installation preserving current
staging. It creates new accounts, sessions, Library and audit records; it does
not transfer existing records or encrypted secrets. A preserving transition
requires a separately reviewed design and compatibility decision.

Plan commissioning one or two days after completion of the whole repository
scope, including provider adapters assigned to the end of .7. This is a
scheduling assumption, dependent on a qualified image and operator readiness,
not deployment authorization. Do not change resources, access, secrets,
services or stores as part of this repository repair.

Distinguish implemented, locally qualified, CI-qualified, landed and
live-accepted states. Local and CI receipts do not establish live Azure
identity, network, NFS, ingress streaming, multi-replica recovery, telemetry or
restore behavior. Engineering-day estimates are not calibrated agentic elapsed
time, and capacity waits must remain visible rather than hidden in a date.
