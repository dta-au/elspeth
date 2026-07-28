# AWS RDS Immutable Trust-Root Design

Date: 2026-07-28
Status: approved for implementation; production release blocker
Branch context: `codex/aws-rds-trust-root-hardening`
Tracker: `elspeth-ca436b5f1b`, blocking `elspeth-671a17d5c0`

## Purpose

Make the ELSPETH AWS ECS/PostgreSQL release path reproducible, fail closed, and
eligible for production release qualification.

The current AWS task definitions fetch the RDS global CA bundle from the public
internet at task startup. The web and doctor tasks persist that download in
application-writable EFS, while the database bootstrap task downloads another
copy into `/tmp`. Neither path pins or verifies the downloaded bytes. This
makes database admission depend on runtime network availability and lets the
application identity create the trust root used to authenticate its database.

That is a production release blocker. Acceptance is the collection of evidence
that the exact artifact is fit for production; an acceptance environment does
not relax the production contract. The existing image at OCI digest
`sha256:c5e65357b7470cf1a702eeb084e865f0f5e0e43ab9741b76e872fa7568029700`
is an acceptance-attempt artifact and must not be promoted to the final public
release-candidate tag.

## Scope

This change covers the trust-root and filesystem contract for every
ELSPETH-owned container in the AWS ECS Terraform package:

- the web service;
- schema-initialization and runtime doctor tasks;
- the database bootstrap task;
- payload and local-auth qualification tasks; and
- candidate and rollback task definitions.

It also covers:

- the canonical container image and its Docker build context;
- Aurora PostgreSQL instance CA selection;
- session, Landscape, schema-owner, and bootstrap connection URLs;
- AWS ECS deployment-contract and doctor validation;
- offline CI and container verification; and
- source-free live AWS release qualification and teardown.

The two-database topology is unchanged. Session state and Landscape audit state
remain separate logical PostgreSQL databases on one Aurora cluster; this design
does not require separate RDS clusters or instances.

## Non-Goals

- Replacing AWS Private CA, the RDS-managed server certificate, or PostgreSQL's
  certificate validation.
- Supporting AWS partitions or regions in which the selected RDS CA identifier
  is unavailable. The 0.7.2 AWS package remains qualified for the commercial
  AWS partition.
- Adding a runtime fallback to the operating-system CA store, an init-container
  download, EFS, S3, Secrets Manager, or the public RDS trust-store endpoint.
- Treating a mutable tag, a successful test-environment run, or a previously
  built image as production release evidence.
- Changing the separation or ownership model of the session and Landscape
  databases.

## Security and Release Invariants

The implementation must maintain all of these invariants:

1. No ELSPETH AWS container performs network I/O to obtain database trust
   material during task startup, doctor execution, schema initialization, or
   normal service operation.
2. The reviewed AWS commercial-region `global-bundle.pem` is a versioned
   release input. Its exact SHA-256 is committed and checked offline.
3. The runtime bundle is root-owned, mode `0444`, and located at
   `/etc/elspeth/rds/global-bundle.pem` in the immutable image.
4. Every AWS ECS PostgreSQL URL uses `sslmode=verify-full` and the exact
   immutable `sslrootcert` path. A merely nonblank or alternate path is not
   accepted.
5. The Aurora instance explicitly selects `rds-ca-rsa2048-g1`.
6. ELSPETH containers run with a read-only root filesystem. Writable EFS and
   task-local scratch mounts do not cover `/etc/elspeth` and cannot replace or
   modify the trust root.
7. Missing, changed, malformed, or unusable trust material fails before a
   PostgreSQL connection is admitted. There is no fallback.
8. Candidate and rollback images are subject to the same contract. A legacy
   image without the immutable bundle is ineligible for this package rather
   than being rescued by a runtime download.
9. Only a newly built image digest that passes the complete release
   qualification may receive the final public 0.7.2 RC tag.

## Trust-Root Supply Chain

### Maintainer update

The AWS commercial-region bundle is retrieved only as an explicit maintainer
update from:

`https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem`

The update is reviewed like source code and commits:

- `deploy/aws-ecs/trust/rds-global-bundle.pem`;
- `deploy/aws-ecs/trust/rds-global-bundle.pem.sha256`; and
- provenance and rotation instructions in
  `deploy/aws-ecs/trust/README.md`.

The checksum file uses the conventional form:

```text
<64 lowercase hexadecimal SHA-256 characters>  rds-global-bundle.pem
```

The implementation commit records the actual digest; no build or runtime step
downloads bytes to fill it. The review procedure verifies the source URL,
retrieval date, exact byte digest, successful parsing of every PEM certificate,
and the expected RDS CA identity through the AWS RDS certificate catalogue.

The repository-wide `*.pem` Docker exclusion remains in place. A single narrow
`.dockerignore` exception admits only this reviewed public bundle; private keys
and arbitrary PEM files remain excluded.

### Image construction

The Docker builder copies the reviewed bundle and checksum into its prepared
runtime root. The final image contains:

```text
/etc/elspeth/rds/global-bundle.pem
/etc/elspeth/rds/global-bundle.pem.sha256
```

Both files and their parent directories are owned by root. Directories are
mode `0755`; files are mode `0444`. The image exposes OCI labels for the bundle
SHA-256 and RDS CA identifier so release tooling can compare the image metadata
with the source manifest without starting the application.

The application package contains the canonical path, expected digest, and CA
identifier as constants in one AWS RDS trust module. Tests require those
constants, the source checksum manifest, the OCI labels, and the bytes copied
into the image to agree.

## Terraform and Runtime Contract

### Aurora CA selection

`aws_rds_cluster_instance.database` sets:

```hcl
ca_cert_identifier = "rds-ca-rsa2048-g1"
```

The live release qualification must confirm that the provisioned instance's
AWS API `CACertificateIdentifier` is exactly `rds-ca-rsa2048-g1`. The global
bundle remains appropriate because it contains the commercial-region roots
needed to validate RDS chains while the explicit instance setting prevents an
account or provider default from silently choosing a different CA generation.

### PostgreSQL URLs

All runtime, schema-owner, and bootstrap URLs use this query contract:

```text
sslmode=verify-full&sslrootcert=/etc/elspeth/rds/global-bundle.pem
```

For example:

```text
postgresql+psycopg://ROLE:PASSWORD@RDS_ENDPOINT:5432/elspeth_landscape?sslmode=verify-full&sslrootcert=/etc/elspeth/rds/global-bundle.pem
```

The session URL differs only by role as applicable and database name, normally
`elspeth_session`. Passwords and complete URLs remain Secrets Manager values;
they are never written to logs or qualification evidence.

The AWS deployment contract rejects:

- any `sslmode` other than the single value `verify-full`;
- a missing, blank, repeated, or alternate `sslrootcert`;
- a URL whose trust-root path is not the canonical image path; and
- query shapes that SQLAlchemy cannot represent as one unambiguous value.

### Startup and bootstrap

The ECS identity wrapper retains only task-metadata discovery and CLI argument
normalization. Its RDS bundle download, temporary-file replacement, and EFS
write are deleted.

The database bootstrap script imports the shared trust verifier from the
candidate image and validates the image bundle before opening the admin
connection. Its `urllib.request` import, public trust-store request, and
`/tmp/rds-global-bundle.pem` write are deleted. Bootstrap uses the same
canonical path as every other PostgreSQL connection.

No task has a network fallback if validation fails.

## Read-Only Filesystem and Writable State

Every ELSPETH container definition sets `readonlyRootFilesystem = true`.
The CloudWatch Agent sidecar retains its own independently justified
filesystem contract; it does not receive database URLs or trust-root authority.

The existing EFS mount remains writable at `/var/lib/elspeth` for application
data, payloads, outputs, and local-auth state. It no longer contains the RDS CA
bundle.

Where a container demonstrably needs temporary files, the task definition
provides a task-lifetime scratch volume mounted at `/tmp` and sets `TMPDIR` to
that mount. The mount is never EFS-backed, never contains trust material, and
does not overlap `/etc/elspeth`. Container smoke testing must prove both that
the non-root ELSPETH identity can use the scratch directory and that the
application succeeds with the root filesystem read-only. A scratch mount is
added only to task definitions that the test demonstrates need it.

## Admission and Doctor Behaviour

The shared AWS RDS trust verifier runs before database inspection or schema
initialization. It:

1. accepts only the canonical absolute path, opens it with
   `O_RDONLY | O_NOFOLLOW`, and validates the opened descriptor with `fstat`
   so a path swap cannot substitute another file;
2. requires the opened descriptor to identify a regular file;
3. computes SHA-256 from those opened bytes and compares it with the committed
   expected digest using a constant-time comparison;
4. splits and parses every certificate with `cryptography.x509`;
5. rejects trailing data, malformed blocks, an empty bundle, and certificates
   that are not CA certificates; and
6. returns redacted metadata only: path, expected digest, actual digest,
   certificate count, and pass/fail reason.

`elspeth doctor aws-ecs --json` exposes a dedicated trust-root check. The
schema-initializing doctor performs the same check before any DDL. Web startup
uses the same verifier before constructing persistent PostgreSQL-backed
services, so bypassing doctor does not bypass admission.

Failures identify the violated contract and tell the operator to deploy an
eligible image. They never print URLs, credentials, certificate bodies, or a
fallback command that downloads the bundle inside a running task.

## Error Handling

- Missing bundle or checksum disagreement: doctor, bootstrap, and web admission
  fail before database network access.
- Symlink, non-regular file, unreadable file, malformed PEM, non-CA
  certificate, empty bundle, or trailing data: fail closed with redacted
  diagnostics.
- URL using the old EFS or `/tmp` bundle path: deployment-contract failure.
- RDS CA identifier unavailable in the selected region: Terraform apply fails;
  the package is not silently downgraded to a provider default.
- Candidate or rollback image missing the immutable asset contract:
  qualification fails before service promotion.
- Read-only root filesystem exposes an unmodelled write: the container fails
  smoke or live qualification; the required write must be assigned to a narrow
  task-local or application-state mount and requalified.

## Rotation and Emergency Update

RDS server-certificate rotation under the same trusted root does not require an
ELSPETH image change. A change to the selected RDS root or the AWS global
bundle does.

For a planned bundle or CA-generation change:

1. retrieve and review the new official bundle outside build and runtime;
2. verify its certificate inventory and AWS certificate catalogue entry;
3. update the vendored bundle, checksum, provenance, application constants,
   OCI labels, Terraform CA identifier if required, and tests in one change;
4. build a new immutable image and record its OCI digest;
5. run offline, container, Terraform, and source-free live AWS qualification;
6. confirm the live RDS CA identifier and authenticated TLS session evidence;
7. move the public RC or release tag only after every gate passes; and
8. retain the prior qualified digest for rollback only if it independently
   satisfies the same trust-root contract.

For an AWS emergency distrust notice, the affected image digest is ineligible
immediately. Operators deploy a newly qualified digest; they do not patch EFS,
exec into a task, or enable a runtime download.

## Verification Strategy

### Unit and static contract tests

Tests must prove:

- source bundle bytes match the committed checksum and application constant;
- every PEM block parses and represents a CA certificate;
- `.dockerignore` admits only the reviewed public bundle;
- the Dockerfile installs the bundle and checksum with root ownership and mode
  `0444`, and publishes matching OCI labels;
- the AWS URL validator accepts only `verify-full` plus the canonical path;
- missing, alternate, repeated, malformed, symlinked, and digest-mismatched
  trust roots fail before database inspection;
- doctor JSON is useful and redacted;
- every generated PostgreSQL URL uses the canonical query contract;
- `ca_cert_identifier` is pinned on the Aurora instance;
- every ELSPETH container has a read-only root filesystem;
- no ECS wrapper or bootstrap source references the public trust-store URL,
  `urllib.request`, the old EFS CA path, or `/tmp/rds-global-bundle.pem`; and
- candidate and rollback task definitions enforce the same image contract.

### Image and Terraform tests

Build the exact lean release image with
`INSTALL_EXTRAS="webui llm aws postgres"` and verify:

- bundle bytes, SHA-256, ownership, modes, and OCI labels from the built image;
- `elspeth doctor aws-ecs` trust-root reporting from the built image;
- web, doctor, and bootstrap entrypoints with `--read-only`;
- only explicitly declared scratch and application-state paths are writable;
  and
- startup succeeds without access to the public RDS trust-store endpoint.

Run Terraform formatting, validation, native mock tests, package contract
tests, and rendered-task-definition assertions from an initialized clean
worktree.

### Live production release qualification

From an exact clean source commit:

1. build and publish by immutable digest;
2. hand the source-free installer only the published deployment artefact,
   immutable candidate digest, required operator inputs, and official AWS
   documentation;
3. provision an empty, uniquely tagged disposable environment in the qualified
   commercial AWS region;
4. confirm the RDS instance reports `rds-ca-rsa2048-g1`;
5. run bootstrap, schema doctor, runtime doctor, web readiness, durable-storage
   checks, and authenticated Composer-to-Bedrock proof;
6. record redacted PostgreSQL `pg_stat_ssl` evidence showing TLS is active for
   both session and Landscape connections;
7. prove the running task definitions use read-only ELSPETH root filesystems
   and the immutable image digest;
8. verify no release step or task downloads the RDS bundle at runtime;
9. destroy every run-owned billable resource and prove teardown; and
10. only then assign the final public 0.7.2 RC tag to that exact digest.

Any failed gate disqualifies the artifact. Fixes produce a new source commit,
new image digest, and a complete qualification run; evidence from the failed
attempt is not spliced into the new release claim.

## Acceptance Criteria

- The reviewed RDS global bundle is committed with provenance and exact
  SHA-256, and is present at the canonical root-owned, mode-`0444` image path.
- Build and runtime require no network access to acquire trust material.
- Aurora explicitly uses `rds-ca-rsa2048-g1`.
- Session, Landscape, schema-owner, and bootstrap URLs require
  `sslmode=verify-full` with the canonical image path.
- Doctor and web startup fail closed on every trust-root integrity or URL
  contract violation before database admission.
- Every ELSPETH ECS container has a read-only root filesystem and only
  justified writable mounts.
- Candidate and rollback images are admitted only when they satisfy the same
  immutable trust-root contract.
- Offline tests, the exact lean image smoke, Terraform native tests, and the
  complete source-free live AWS cold-install and teardown all pass from one
  clean source commit.
- Live evidence confirms the selected RDS CA, authenticated PostgreSQL TLS for
  both logical databases, ECS task-definition immutability, and zero leftover
  run-owned billable resources.
- The existing acceptance-attempt digest is never represented as a production
  release candidate.
- The final public RC tag, if assigned, resolves to the single newly qualified
  digest and no other artifact.
