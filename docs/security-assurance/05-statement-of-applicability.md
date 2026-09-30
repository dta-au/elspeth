# 05 — Statement of applicability

**Status:** deployment template complete — control selection and assessment
unpopulated · **ISM release:** see § 1 Deployment record · **Reviewed
against:** `release/0.8.1` @
`eee4bb9419265e69ac13012a1c5fe421d6ba7240` (2026-10-01) · **Owner:**
ELSPETH maintainer

This template records the complete control population selected for an
ELSPETH installation, the applicability and implementation decision for each
control, its evidence, and the assessor's result. The selected ISM release,
classification, deployment facts and assessment results belong only in the
controlled deployment copy.

## How to populate the controlled copy

1. Fix the assessment boundary and assessed release in
   [01](01-system-overview-and-boundary.md), including the product commit and
   immutable image digest.
2. Obtain the machine-readable or otherwise authoritative control set for the
   selected ISM release from its official source. Preserve the exact source
   export in the controlled repository and record its SHA-256 digest before
   transforming it.
3. Import every source control before making an applicability decision. Do not
   pre-filter guideline sections or delete non-applicable controls. Keep one
   row per source control so a missing row cannot look like an exclusion.
4. Populate the assessment basis (§ 1), the control rows (§ 2), reconciliation
   counts (§ 3), any non-applicable section summary (§ 4), and the Essential
   Eight assessment (§ 5).
5. Link product evidence through
   [16](16-evidence-index.md) and deployment evidence through an immutable
   reference in the controlled evidence store. Name the responsible party
   using the parties in
   [03](03-shared-responsibility-matrix.md).
6. Import every *Partially implemented*, *Not implemented* or deficient
   effectiveness result into the controlled
   [risk register](15-risk-register.md). Record the stable risk ID in the
   control row before sign-off.
7. Run every reconciliation rule in § 3 and record the result. The assessor
   then records findings and sign-off in § 6.

### Closed values and row validity

**Applicability** has two final values: `Yes` and `No`. The deployment
placeholder is allowed only while the controlled copy is being drafted.

**Implementation status** has four final values: `Implemented`,
`Partially implemented`, `Not implemented`, and `Not applicable`.
**Implementation basis** has four final values: `Direct`, `Shared`,
`Inherited`, and `Not applicable`. Inheritance describes who supplies a
control; it never replaces the implementation or effectiveness result.

**Control effectiveness** uses the closed vocabulary and method recorded in
§ 1. Every applicable final row must state an effectiveness result; provider,
implementation or inheritance evidence alone cannot supply one.

Apply these rules to every row:

- `Applicability = No` requires both `Implementation status = Not applicable`
  and `Implementation basis = Not applicable`, plus a specific rationale and
  supporting scope or architecture evidence. Keep the control in the table.
- `Applicability = Yes` must not use `Not applicable` for either field.
- `Implemented` requires implementation evidence and an accountable control
  owner.
- `Implementation basis = Inherited` requires the provider or platform
  control, the inheritance boundary, its control owner, and current evidence
  that the inherited control operates for this deployment. It still requires
  an implementation status and assessment result.
- `Partially implemented` and `Not implemented` require a risk reference in
  [15](15-risk-register.md), a treatment owner and a target date. Put the
  treatment detail in the controlled risk register rather than this public
  template.
- An assessor finding reference does not replace an assessment result. Record
  the result and finding reference in separate fields, using the closed
  assessment-result vocabulary named in § 1. A final row must have an
  assessment result even when there is no finding.
- Do not leave a final row blank, use free-form synonyms for closed values, or
  combine multiple source control IDs into one row.

Assign every preserved source set a stable `source-set-id` in § 1. The
authoritative row identity is the composite key `(source-set-id, control-id)`,
rendered in references and exports as `source-set-id::control-id`. A bare
control ID is not unique when multiple source sets or overlays are imported.
Use the qualified key in findings, evidence, summaries and reconciliation
output.

## 1. Deployment record — assessment basis and source custody

Every `DEPLOYMENT-TODO:` in this document is intentionally unpopulated in the
public repository.

| Field | Deployment value |
|---|---|
| Assessment boundary reference in document 01 | DEPLOYMENT-TODO: |
| Assessed ELSPETH version, commit and container digest | DEPLOYMENT-TODO: |
| Selected ISM release identifier and publication date | DEPLOYMENT-TODO: |
| Official control-set source and retrieval date | DEPLOYMENT-TODO: |
| Preserved source export location in the controlled repository | DEPLOYMENT-TODO: |
| Source export filename, media type, byte size and SHA-256 digest | DEPLOYMENT-TODO: |
| Import date, importer and import method or tool version | DEPLOYMENT-TODO: |
| Classification, protection level and other baseline-selection inputs | DEPLOYMENT-TODO: |
| Agency overlays or additional control sets, with version and source digest | DEPLOYMENT-TODO: |
| Stable `source-set-id` assigned to each preserved source | DEPLOYMENT-TODO: |
| Applicability decision authority and governing policy | DEPLOYMENT-TODO: |
| Assessment-result vocabulary and assessment method | DEPLOYMENT-TODO: |
| Control-effectiveness vocabulary and assessment method | DEPLOYMENT-TODO: |
| Statement-of-applicability owner in the deploying organisation | DEPLOYMENT-TODO: |
| Controlled evidence-store location | DEPLOYMENT-TODO: |

If more than one source control set applies, add one source-custody record for
each set. Preserve source identity on every imported row and reconcile both
the per-source populations and the de-duplicated combined population.

## 2. Deployment record — controls

| Source-set ID | Control ID | Guideline / control title | Applicability | Applicability rationale | Implementation status | Implementation basis | Responsible party | Control owner | Implementation / inheritance summary | Evidence | Control effectiveness | Risk / treatment ref | Assessment result | Finding ref |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| DEPLOYMENT-TODO: | DEPLOYMENT-TODO: import one row per source control | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: optional stable finding ID |

For product evidence, use `[EV-nnn]`. For deployment or inherited evidence,
use the controlled evidence identifier and include its observation date or
validity period. A URL by itself is not evidence that a control operated for
the assessed installation.

## 3. Deployment record — import and outcome reconciliation

Record measured counts from the preserved source and the populated controls
table. Do not enter counts obtained from a prose search of this file.

First reconcile every source independently:

| Source-set ID | Preserved source controls | Imported rows | Unique composite keys | Duplicate composite keys | Omitted composite keys | Instrument / checked by / date |
|---|---|---|---|---|---|---|
| DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: |

Then reconcile the combined population and outcome fields:

| Measure | Count / result | Instrument or query | Checked by / date |
|---|---|---|---|
| Source controls in preserved export | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: |
| Imported control rows | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: |
| Unique `(source-set-id, control-id)` keys | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: |
| Duplicate `(source-set-id, control-id)` keys | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: |
| Omitted `(source-set-id, control-id)` keys | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: |
| Applicability `Yes` | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: |
| Applicability `No` | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: |
| `Implemented` | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: |
| `Partially implemented` | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: |
| `Not implemented` | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: |
| `Not applicable` | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: |
| Implementation basis `Direct` | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: |
| Implementation basis `Shared` | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: |
| Implementation basis `Inherited` | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: |
| Implementation basis `Not applicable` | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: |
| Partial / not-implemented rows with a valid risk reference | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: |
| Inherited rows with provider, owner and current evidence | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: |
| Applicable rows with a final control-effectiveness result | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: |
| Rows with a final assessment result | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: |
| Finding references that resolve to a finding record | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: |
| Deficient effectiveness / assessment results with both finding and risk references | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: |

The import gate passes only when all of these statements are true:

- for each source set, source-control count = imported-row count = unique
  composite-key count, and the de-duplicated combined population reconciles
  separately;
- duplicate count = 0 and omitted count = 0;
- `Yes` + `No` = imported-row count;
- all four implementation-status counts sum to imported-row count, and all
  four implementation-basis counts separately sum to imported-row count;
- `No` count = both `Not applicable` counts, with the same composite-key
  population;
- every partial or not-implemented row resolves to a controlled risk record;
- every inherited row identifies its inheritance evidence and control owner;
- every applicable row has a control-effectiveness result from the declared
  vocabulary, including inherited and shared controls;
- every row has an assessment result from the declared vocabulary; every
  finding reference resolves independently and is not used as that result;
- every result classified as deficient by the declared method has a resolvable
  finding and controlled risk/treatment reference;
- the source digest and import instrument have been independently checked.

If the official source format cannot be imported without loss, stop and
record the exception in the controlled assessment record. Do not make the
counts reconcile by deleting or merging controls.

## 4. Deployment record — non-applicable section summary

This optional summary supports assessor review; it does not remove the
corresponding control rows from § 2. Enumerate the qualified
`source-set-id::control-id` keys and reconcile their count here to the
`Applicability = No` population in § 3.

| Guideline section | Source-set-qualified control IDs | Count | Reason and scope evidence | Decision authority / date |
|---|---|---|---|---|
| DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: |

## 5. Deployment record — Essential Eight

Record applicability explicitly even where a strategy is outside the product
boundary. Use the current Australian Signals Directorate maturity-model
strategy names from the selected assessment source.

| Strategy | Applicability | Target maturity | Current maturity | Responsible party / control owner | Notes, evidence and gap risk ref | Assessment result |
|---|---|---|---|---|---|---|
| Patch applications | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: |
| Patch operating systems | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: |
| Multi-factor authentication | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: |
| Restrict administrative privileges | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: |
| Application control | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: |
| Configure Microsoft Office macro settings | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: |
| User application hardening | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: |
| Regular backups | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: | DEPLOYMENT-TODO: |

## 6. Deployment record — assessment and sign-off

| Field | Deployment value |
|---|---|
| Assessor and independence | DEPLOYMENT-TODO: |
| Assessment dates and assessed evidence cut-off | DEPLOYMENT-TODO: |
| Overall assessment result | DEPLOYMENT-TODO: |
| Finding population and reconciliation to risk IDs | DEPLOYMENT-TODO: |
| Exceptions, qualifications and limitations | DEPLOYMENT-TODO: |
| Deploying-organisation review and approval | DEPLOYMENT-TODO: |
| Next review date or trigger | DEPLOYMENT-TODO: |
