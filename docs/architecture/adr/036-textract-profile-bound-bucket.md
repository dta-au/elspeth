# ADR-036: Textract Document Buckets Bind Through a Dedicated Operator Profile, Not Row Data

**Date:** 2026-08-04
**Status:** Accepted
**Deciders:** ELSPETH maintainers, on a three-seat design review (systems
thinking, solution architecture, Python engineering) of Filigree
`elspeth-cd0f6a6cd9`
**Tags:** operator-profiles, custody, aws, textract, web-composer, audit

## Context

Web operator profiles deliberately hide real S3 bucket names behind opaque
aliases: `AWSS3SourceProfileSettings` marks `bucket` `repr=False`
(`src/elspeth/web/plugin_policy/profiles.py:68-75`), safe config records only
`{profile, key}` (`src/elspeth/plugins/sources/aws_s3_source.py:958`), and a
binding fingerprint (`src/elspeth/contracts/aws_s3.py:41-56`) keeps the
concrete binding out of the audit trail while keeping it verifiable.

The `aws_textract_document_analysis` transform is the one consumer that needs
bucket identity in **row data** (`bucket_field`, per-row values), where no
custody seam exists. Acceptance battery round 2 (2026-08-04, release
`173a81cbb`, run `e41d0e6b`) demonstrated the consequence: asked for a
Textract graph "using the aws_s3 source with the acceptance-docs profile",
the Composer authored an invented CSV manifest with
`bucket='acceptance-docs'` — the profile alias as a literal bucket name. The
transform HeadBuckets the alias, gets 404, and fail-closes every row with
`bucket_region_unverified`. The runtime custody posture is correct; the
authoring surface is the defect. Because real bucket names are
operator-private, **no author can write a working web-surface Textract graph
at all**: the only legal value is information the surface deliberately
withholds.

This is a further instance of the battery round 2 defect theme: the
composer's authoring-time model diverging from what the engine enforces at
run time.

## Decision

Bucket identity moves back to the layer that has a custody seam — config
lowering — and out of the web-authorable surface entirely:

1. **Engine (provider-agnostic, additive).** The transform gains static
   `bucket` and `key_prefix` config options, mutually exclusive with
   `bucket_field`. When `bucket` is set, rows carry keys only; row keys are
   joined and path-validated under `key_prefix`, mirroring the S3 source's
   prefix semantics (`profiles.py:709`). CLI/YAML authors may set a literal
   `bucket` directly; the engine never sees an alias.

2. **Dedicated Textract operator profile table.** Web deployments define
   Textract grants in their own settings table (alias →
   `{bucket, key_prefix}`), *not* by reusing `aws_s3_source_profiles`.
   Region and auth continue to come from the deployment. The grant is
   surfaced through the transform's **existing single `profile` knob**
   (multi-alias); no second alias axis is introduced.

3. **Web projection is allowlist, and both bucket fields are private.** The
   Textract resolver's public projection flips from denylist
   (`_TEXTRACT_PRIVATE_OPTIONS` subtraction, `profiles.py:767-775, 798-802`)
   to a positive allowlist mirroring `S3_PROFILED_AUTHOR_OPTION_NAMES`, and
   both `bucket` and `bucket_field` become web-private. The web surface can
   express neither a literal bucket nor a bucket-bearing row column; the bug
   class becomes structurally inexpressible rather than guarded against.

4. **A dedicated audit identity, consumed at runtime call-record sites.**
   A `TextractProfiledAuditIdentity` (`profile_alias`, `binding_fingerprint`
   over `{bucket, region, key_prefix}`, **no per-object key**) is bound with
   fingerprint verification, and the two persisted call-record payloads that
   today embed the literal bucket (`textract_bucket_region.py:266`,
   `textract_client.py:313`) project `{profile, key}` when a profiled
   identity is bound — the `_audit_object_identity` pattern from
   `aws_s3_source.py:954-958`. Unprofiled (CLI) runs keep emitting the
   literal bucket the author chose. The profile `generation` hash covers the
   bucket so rotation is visible to staleness checks.

5. **Kind-qualified grants stay separate.** An operator wanting the same
   location for both plugins issues two grants; the alias *name* may be
   shared (`source:aws_s3` + `acceptance-docs` and
   `transform:aws_textract_document_analysis` + `acceptance-docs` are
   distinct identities by design).

## Alternatives considered

- **Runtime per-row alias resolution ("Shape A") — rejected.** Row values
  arrive at runtime, past the lowering seam. Resolution there needs either
  web policy callable from a runtime plugin (breaks CLI/web parity) or an
  alias→bucket map in engine config (leaks every private bucket into
  audited run config).
- **Reusing `aws_s3_source_profiles` as the Textract alias table —
  rejected.** The tables have different scope semantics. An S3 source
  profile grants *read one bounded object under a prefix*
  (`profiles.py:709`); Textract implies HeadBucket +
  StartDocumentAnalysis over per-row keys. Reuse silently drops the
  operator's prefix bound — concretely: the sole grant on the live
  acceptance deployment is org-prefix-scoped, and reuse would widen it to
  the whole bucket. It also couples Textract availability to an unrelated
  capability (tabular S3 ingest).
- **A second `bucket_profile` knob — rejected.** The lowering seam is
  single-alias by contract (`OperatorProfileRegistry.lower_options`,
  `profiles.py:1069-1080`; `validation.py:426` pops exactly one
  `"profile"`). A second alias would ride through `safe_options`
  unvalidated, or force a signature change on a seam shared by the LLM,
  Bedrock-guardrail, and S3-source profile families.
- **Growing the `deployment` profile into per-alias variants — rejected.**
  Right authoring shape (one knob), wrong settings home: it conflates the
  deployment runtime binding (region + auth, one per deployment) with the
  bucket grant (potentially many).
- **Authoring aids / wording alone — rejected as a complete fix.** It makes
  the failure honest but leaves the graph unauthorable, and the existing
  assistance text would still direct authors toward a value the surface
  withholds. Retained only as accompanying prose changes.

## Consequences

- The Composer can author a working, custody-clean Textract graph: profile
  alias + per-row keys, no bucket identity anywhere in authored config, row
  data, generated YAML, or persisted call records.
- A web deployment with no Textract profile configured has an empty alias
  enum and the transform is honestly unauthorable there
  (`profile_unavailable`), which is the correct posture: there is no legal
  bucket value on that surface anyway.
- The custody NFR becomes falsifiable: a profiled Textract run must produce
  **zero persisted call records containing the bucket literal**. This
  assertion fails before the change and must pass after it.
- Existing web sessions authored against the old public schema
  (`bucket_field` authorable) are invalidated by the projection flip;
  pre-release discipline applies (session-store epoch bump + wipe, never
  `auth.db`).
- `bucket_field` remains fully supported for CLI/YAML and for unprofiled
  deployments; the engine change is additive and existing pipelines are
  untouched.
- Editing the plugin file obliges the mechanical PH3 `source_file_hash`
  refresh and golden knob-schema regeneration.

## References

- Filigree `elspeth-cd0f6a6cd9` (battery evidence run `e41d0e6b`)
- Implementation plan:
  `docs/superpowers/plans/2026-08-04-textract-profile-bound-bucket.md`
- ADR-032 (validate by trust domain); the kind-qualified profile identity
  note at `src/elspeth/web/composer/planner_authoring_aids.py:1196-1198`
- Prior transform design:
  `docs/superpowers/plans/2026-07-29-amazon-textract-document-analysis-transform.md`
