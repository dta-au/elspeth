# Textract Profile-Bound Bucket — Implementation Plan

**Date:** 2026-08-04 · **Branch:** release/0.7.2 · **Tree at review:** `b06fcd5b1`
**Ticket:** `elspeth-cd0f6a6cd9` (P2 bug, battery-2026-08-04)
**Decision record:** [ADR-036](../../architecture/adr/036-textract-profile-bound-bucket.md)
**Status:** BUILT 2026-08-05 (branch `fix/textract-profile-bound-bucket`, four
slice commits per §5; custody NFR verified by
`test_profiled_textract_runtime_uses_private_binding_only_for_aws_calls`).
Step-2/step-4 sequencing deviation: `bucket_field` left the web projection
with the Step-2 resolver rewrite rather than in Step 4, because the new
resolver's lowering injects the profile bucket and a still-authorable
`bucket_field` would have guaranteed an engine mutual-exclusion conflict in
the interim state. Live acceptance rides the round-3 redeploy (operator: new
`ELSPETH_WEB__AWS_TEXTRACT_PROFILES` grant + session-store wipe at epoch 45).

All file:line references were verified against `b06fcd5b1`; the panel read at
`9ef9c894c` and `git diff --stat` confirms none of the cited files changed
between the two.

## 1. Problem

Battery round 2 (release `173a81cbb`, run `e41d0e6b`): asked for a Textract
graph "using the aws_s3 source with the acceptance-docs profile", the
Composer authored an invented CSV manifest of bucket/key rows with
`bucket='acceptance-docs'` — the operator profile alias as a literal bucket
value. `aws_textract_document_analysis` HeadBuckets the alias, receives 404,
and fail-closes every row with `bucket_region_unverified`
(`textract_document_analysis.py:628-634` → `textract_bucket_region.py:264-314`).

The runtime is correct (honest, quarantined, diagnosable). The authoring
surface is the defect, and it is structural, not a prompt-quality issue:

- Config-level custody speaks aliases *by design* — bucket names are
  operator-private (`profiles.py:68-75` `repr=False`; safe config records
  `{profile, key}` only, `aws_s3_source.py:958`).
- The Textract transform needs bucket identity in **row data**
  (`bucket_field`, `textract_document_analysis.py:192`), where no custody
  seam exists.
- Therefore a *perfect* author cannot write a working web-surface Textract
  graph: the only legal value is one the surface deliberately withholds.

## 2. Review provenance

Three independent read-only reviews (2026-08-04), one brief, per-seat
charges; findings below are labelled by seat (SYS / ARCH / PY) and were
spot-verified by the coordinating session before adoption:

- **Systems thinking:** BUILD NOW, conditioned on a single alias axis.
- **Python engineering:** REDESIGN (narrow) — conditions all subsequently
  resolved (alias cardinality = 1 from the deployment ledger; dedicated
  settings table adopted; prefix handling made mandatory), converging to
  build.
- **Solution architecture:** BUILD NOW with the runtime audit projection
  (F1) mandatory in-slice; adjudicated the design forks.

Coordinator verifications that closed the panel's open gaps:

- **No redaction layer** exists between `record_call` and persistence
  (`core/landscape/execution/calls.py:375` serializes payloads as given), so
  F1 stands at full severity and the audit-identity machinery is
  load-bearing, not optional hardening.
- **Deployment ledger** (R3 tracker, 2026-08-04 21:33/22:06 local): task-def
  `web:9`, release `173a81cbb`, `ELSPETH_WEB__AWS_S3_SOURCE_PROFILES`
  carries exactly **one alias, `acceptance-docs` → app bucket + org
  prefix**. Alias cardinality for the demo is 1 and the sole live grant is
  prefix-scoped.
- **Battery round 2 completed** the same day (seven defects filed, this
  ticket among them), so the build lands in the inter-round fix window and
  rides the round-3 redeploy; no mid-battery insertion arises.

## 3. Settled design

See ADR-036 for the decision and rejected alternatives. Summary:

- **Engine:** static `bucket` + `key_prefix` options on
  `AWSTextractDocumentAnalysisConfig`, mutually exclusive with
  `bucket_field`; rows carry keys only in bucket mode; row keys joined and
  path-validated under `key_prefix` (mirror of `profiles.py:709`).
- **Web:** dedicated Textract operator profile table (alias →
  `{bucket, key_prefix}`; region/auth stay deployment-derived), surfaced
  through the existing single `profile` knob, now multi-alias. Resolver
  projection flips denylist → allowlist; `bucket` **and** `bucket_field`
  are both web-private.
- **Audit:** dedicated `TextractProfiledAuditIdentity`
  (`profile_alias`, `binding_fingerprint` over `{bucket, region,
  key_prefix}`, no per-object key), fingerprint-verified at bind
  (`hmac.compare_digest`, as `aws_s3_source.py:949`), and consumed at the
  two persisted call-record sites so profiled runs emit `{profile, key}`
  instead of the literal bucket. Profile `generation` hash covers the
  bucket.
- **Operator:** one explicit kind-qualified Textract grant in the next task
  definition; the alias name `acceptance-docs` may be reused.

## 4. Findings register

Severity is the panel's; every item was verified against the tree.

### Critical — all in-slice, non-negotiable

| # | Finding | Evidence | Seat |
|---|---------|----------|------|
| F1 | Profiled bucket would leak into two **persisted call-record payloads** on every document; no downstream redaction exists. Fix: port the `_audit_object_identity` projection (default: emit literal bucket when unprofiled — correct for CLI). | `textract_bucket_region.py:266` → `:319-327`; `textract_client.py:309-320,378-386`; pattern at `aws_s3_source.py:954-958,1011,1025` | ARCH |
| F2 | Textract resolver projection is a **denylist** — a new engine `bucket` field becomes web-authorable by default, letting a planner author an arbitrary real bucket (worse than the bug: today's alias 404s honestly). Fix: flip to allowlist mirroring `S3_PROFILED_AUTHOR_OPTION_NAMES`; add a negative projection test (none exists today by construction). | `profiles.py:767-775,798-802,863-872`; contrast `:698-701` | PY, ARCH |
| F3 | Making `bucket_field` optional is not enough — it must leave the **web projection** entirely, or the planner can still author a bucket-bearing manifest column and the original defect stays reachable. Both fields private ⇒ mutual exclusion becomes a pure engine concern (no `oneOf` gap in the flat projected schema). | `profiles.py:804-814`; `planner_authoring_aids.py:1208-1212` | ARCH |
| F4 | `declared_input_fields` returns `{bucket_field, key_field}` unconditionally; with `bucket_field=None` every row fails the ADR-013 runtime check and the error path raises `TypeError` sorting `str` against `None`. Guard the contribution. | `textract_document_analysis.py:311-316,543-553`; `declared_required_fields.py:73,84,118-132` | PY, ARCH |

### High

| # | Finding | Evidence | Seat |
|---|---------|----------|------|
| F5 | `S3ProfiledAuditIdentity` cannot be reused: it requires a non-empty `relative_key` and fingerprints `executable_key` — a Textract node has no single key. Reuse forces fabricating a value into an audit type. New sibling type required. Also: `validation.py`'s hard gate restricts audit identities to `source:aws_s3` and must be widened deliberately (branch currently untested). | `contracts/aws_s3.py:41-56,70-71`; `validation.py:526-529` | PY, ARCH |
| F6 | Cross-kind reuse of `aws_s3_source_profiles` silently drops the operator's `prefix` bound — on the live deployment this would widen the sole (org-prefix-scoped) grant to the whole app bucket. Decisive for the dedicated table + mandatory `key_prefix`. | `profiles.py:69,75,709`; deployment ledger | PY, ARCH |
| F7 | Textract `generation` hash covers only `{region, auth_mode}`; with buckets in the profile it must cover them, or rotation is invisible to staleness/availability checks. | `profiles.py:744-755` vs `:880-886` | ARCH |
| F8 | Acceptance harness exercises the raw SDK only (never the transform config or authoring path) — Shape B ships with zero end-to-end coverage unless one profiled-graph exercise is added. | `web/_aws_ecs_acceptance/textract.py:32,75-101` | ARCH |

### Medium / notes

- **F9 (ARCH):** one Shape-A rejection argument was miscalibrated — the
  call-record bucket write is a shared obligation (F1), not a
  discriminator. Conclusion unchanged; the real discriminators are CLI
  parity and audited run config.
- **F10 (ARCH):** the previously agreed authoring-time alias guard becomes
  vacuous on the profiled web path once F3 lands. Disposition below (§8).
- **F11 (ARCH):** S-tier feature slice; ADR + this plan is the right
  artifact weight. A full solution-architecture workspace would be
  gold-plating.
- **F12 (ARCH):** projection flip invalidates stored sessions that
  authored `bucket_field` → session-store epoch bump + wipe per pre-release
  discipline (`auth.db` never).
- **Availability coupling (PY):** the Textract resolver's availability gate
  must extend to its own table, not borrow the S3 source's
  (`profiles.py:936-951`) — otherwise Textract authoring silently vanishes
  for deployments without tabular-S3 config.
- **Fingerprint-shape (PY):** confirmed by ARCH F5 — new bucket-only
  identity, not "mirroring" the source's.
- **`BucketRegionCoordinator` (ARCH flag):** proof cache is keyed by bucket
  (`textract_bucket_region.py:351-397`); the coordinator is reconstructed
  per run (`on_start`), and the binding is frozen for a run's lifetime, so
  profile rotation across runs is safe. Re-check this reasoning during
  implementation.
- **Advisor summary (ARCH):** `_ADVISOR_SUMMARY_VALUE_KEYS` needs
  `"bucket"` (`web/composer/service.py:7386`) or the advisor renders the
  option name-only. Deferrable.

## 5. Slice plan

Four steps; each independently green; riskiest shared-seam work last.
Steps 1 is CLI-only and needs no redeploy; steps 2–4 reach the web surface
and ride the round-3 redeploy.

**Step 1 — Engine only** (`textract_document_analysis.py`, plus
`textract_bucket_region.py` / `textract_client.py` projection capability):
`bucket` + `key_prefix` options; mutual exclusion in `_consistency`
(precedent: the credential-pair logic at `:275-286`); `declared_input_fields`
guard (F4); per-row prefix join + relative-path validation;
call-record projection capability with default None ⇒ literal bucket (F1).
PH3 `source_file_hash` refresh lands here. CLI-testable; zero Composer risk.

**Step 2 — Settings table + resolver rewrite** (`profiles.py`, settings
model): new Textract profile settings (alias, bucket `repr=False`,
`key_prefix`); resolver rewritten multi-alias with allowlist projection
(F2), availability gate on its own table, `generation` covering the bucket
(F7). Six enumerated edit sites inside the one resolver class
(`:779,864-865,874-887,889-890,892-893,880-886`); no seam-signature change.

**Step 3 — Audit-identity plumbing** (`contracts/`, `validation.py`,
`preflight.py`): `TextractProfiledAuditIdentity` + bucket-only fingerprint;
widen the `validation.py:526-529` gate deliberately with its own test;
bind-time fingerprint verification on the transform; wire the F1 projection
to the bound identity. Additive optional field only — this is the sole step
touching structure shared with the LLM / guardrail / S3-source profile
families, which is why it is sequenced late and small.

**Step 4 — Projection flip + coverage + prose:** `bucket` and
`bucket_field` into the private/allowlist set (F3) — only now, after step 2
provides named aliases, is the flip safe (before that it would make Textract
entirely unauthorable on the web, worse than today's honest failure). One
profiled-graph acceptance exercise (F8). Assistance/hints/example text
(`profiles.py:855,1024-1035,1056-1066`;
`textract_document_analysis.py:342-353,981-997`), including the row-vocabulary
lesson: *rows carry keys, never locations*.

## 6. Test plan and blast radius

- **Rewrites:** `tests/unit/web/plugin_policy/test_profiles.py:232-269`
  (single-alias contract assertions: `:253` enum == ["deployment"], `:254`
  bucket_field presence), plus `:659,696,722` constructions.
- **New:** negative projection test — `bucket`/`bucket_field` never appear
  in the Textract public schema (no such coverage exists today);
  `validation.py:526` widened-gate test (branch currently uncovered);
  engine mutual-exclusion + `declared_input_fields` + prefix-join unit
  tests; the F1 assertion (below).
- **Golden:** regenerate
  `tests/golden/web/catalog/knob_schema/transform__aws_textract_document_analysis.json`.
- **Catalog gates:** `test_catalog_reference_content.py` /
  `test_external_catalogue_metadata.py` — light prose churn; whole-tree
  gates, so only the full suite catches drift.
- **Checked and unaffected** (PY seat, verified): `doctor.py:136-148`,
  `audit_readiness/boundary_expectations.py`, `interpretation_state.py`,
  `_aws_ecs_acceptance/textract.py` + its test (raw-SDK probe; F8 is about
  *adding* coverage, nothing existing breaks),
  `test_validation_path_agreement.py`.
- **Probe:** `probe_config` keeps the `bucket_field` shape (probe never
  sees authored config — `infrastructure/base.py:542-561`); a second probe
  shape for bucket mode is a known, accepted coverage hole for this slice.
- **Reconciliation:** full `pytest tests/` (scoped runs miss the whole-tree
  AST/catalog gates), `elspeth-lints check`, wardline gate
  (`scripts/wardline_gate.py`) before handback.

**Custody NFR, falsifiable:** run a profiled Textract graph; assert **zero
persisted call records contain the configured bucket literal** (query
`calls` request payloads). Fails before the change; must pass after.

## 7. Estimate and risk

~2 focused days. Riskiest piece: step 3's shared-seam plumbing (mitigated:
additive optional field, sequenced last). Second: operator misconfiguration
of the new table (mitigated: `key_prefix` first-class, generation-hash
coverage). The engine step is low-risk and additive.

## 8. Open decisions (operator)

1. **F10 — authoring-time alias guard:** becomes vacuous on the profiled
   web path after F3. Recommendation: keep (few lines; still guards
   unprofiled deployments where `bucket_field` remains authorable).
2. **Session epoch:** confirm bump + session-store wipe at the projection
   flip (standard pre-release discipline; `auth.db` never).
3. **Operator grant:** add the Textract-kind `acceptance-docs` grant
   (app bucket + org prefix) to the round-3 task definition alongside the
   existing source grant.

## 9. References

- Filigree `elspeth-cd0f6a6cd9`; battery evidence run `e41d0e6b`
- ADR-036 (this decision); ADR-032 (validate by trust domain)
- `docs/acceptance/2026-08-04-f14-and-battery-handover.md` (battery anchor
  #2, redeploy env additions)
- `docs/acceptance/2026-08-03-r3-rca-remediation-tracker.md` (deployment
  ledger entries 2026-08-04 21:33 / 22:06)
- `docs/superpowers/plans/2026-07-29-amazon-textract-document-analysis-transform.md`
  (original transform design; its custody commitment covered failure
  messages, not call records — the gap F1 closes)
