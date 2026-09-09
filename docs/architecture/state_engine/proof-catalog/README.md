# State Engine Proof Catalog

The [v3 catalog](v3/catalog.json) is the current finite proof universe for
state-engine assessments: catalog schema 2 with assessment schema 3. It keeps
the 73 stable legs and ten dimensions, replaces shared applicability profiles
with an explicit policy for every case/profile/dimension cell (7,010 required
executable cells), names every PB-09 first-party plugin and provider/auth
variant as its own case, and records per-evidence runner, exact argv, and
provenance. The committed [evidence selector manifest](v3/evidence_selectors.json)
partitions every executable required cell exactly once across the closed lane
inventory. Dated `assessment.json` files bind results to one exact code
baseline; the first full v3 assessment is
[2026-08-15-0537](../assessments/2026-08-15-0537/README.md).

The stable namespaces are `TS-00..19`, `AUX-01..07`, `RC-01..07`,
`PB-01..11`, `RM-01..14`, and `F-01..14`. Run-coordination legs are explicit;
leader-seat and worker-registry changes are not hidden inside a generic fence.

The [v2 catalog](v2/catalog.json) is frozen historical evidence. Its 73-leg
identity and bytes remain available for strict reruns of v2 assessments, and
its execution profiles are byte-equal to v3's. The [v1 catalog](v1/catalog.json)
remains immutable 68-leg history. New assessments must use v3.

## Catalog rules

- Leg IDs are literal and ordered; range expressions are explanatory only.
- v3 applicability is explicit per cell: a required cell carries a null
  reason; a reviewed `not_applicable` cell carries a non-empty catalog
  reason. An assessor cannot mark a dimension N/A ad hoc, and schema 3
  rejects hand-authored `not_applicable` overrides outright.
- `execution_profiles` closes the state-store, deployment, lifecycle,
  state-vocabulary, and first-party plugin inventory. A changed inventory
  requires a catalog revision.
- `evidence_contract` closes behavioral promotion to all-passing executable
  pytest records and reserves documentation records for support only.
- A proof cell is identified by
  `(leg_id, dimension_id, case_id, profile_case)`. The profile case binds one
  supported state store to one deployment and its lifecycle modes; evidence
  for SQLite cannot satisfy PostgreSQL, or vice versa.
- RC-05 and PB-08 are required only for the two SQLite follower deployments;
  their SQLite single-process and PostgreSQL cells are catalog-approved N/A.
  PB-11 is required only for PostgreSQL 16 on the AWS single-leader Landscape
  profile.
- Unless a leg declares named cases, its required case is `leg-contract` for
  every dimension.
- A narrow arm is a case beneath its stable leg, never a new pseudo-leg.
- PB-06 and PB-07 declare the complete sink-effect lifecycle and restart seams
  explicitly because a broad “sink durability” cell would hide material gaps.
- PB-10 declares the complete row-union release matrix: all branches, branch
  loss before or after partial arrival, timeout, late arrival, and restart on
  either side of release.
- PB-09 names every current first-party plugin and every supported
  provider/authentication variant as its own required case, while PB-11 names
  PostgreSQL server-time, row-lock order, isolation, schema
  admission/migration, and ambiguous connection-loss cases.
- `maintenance` is always required.
- Every hard gate declares the dimensions from which its status, affected legs,
  and evidence support are mechanically derived.
- Catalog/assessment versions are dispatched as exact pairs: v1/1 is
  historical, v2 schema 1 uses assessment schema 2, and v3 schema 2 uses
  assessment schema 3. Other combinations are invalid.

## Assessment overlay

Each new assessment must contain all 73 leg IDs. To keep unresolved
assessments readable, a leg may declare `default_status: unknown`; the default
expands to every required dimension/case not named by an override. The derived
verdict remains unresolved until every default is replaced by `pass`, `fail`,
`partial`, or catalog-approved `not_applicable` evidence.

An omitted leg is invalid. An omitted override is not silently passed—it is
explicitly unknown through the default.

## Promotion rules

- `pass` requires executable evidence at the assessment baseline.
- `partial` and `fail` require executable evidence, an exact reason, an
  observable exit gate, and an explicit `owner_issue` key.
- `unknown` requires a reason, exit gate, and explicit owner key; `null` means
  visibly unowned.
- `pytest` is the only behavioral promotion kind. It requires positive passing
  node counts, exact JUnit/count agreement, no failed/error/skipped/xfail/xpass
  result, exact JUnit/index/profile node identity, exact machine-derived
  outcome and warning counts, and matching runtime-profile provenance produced
  at the trusted test/database boundary. Human stdout summaries do not supply
  counts; XFAIL and XPASS remain distinct from skip and pass.
- `documentation` evidence may support a result but cannot independently
  produce behavioral `pass` or `partial`.
- Evidence coverage names exact
  `(leg_id, dimension_id, case_id, profile_case)` tuples and the retained node
  IDs that establish them. One node may span dimensions, but only for one
  leg/case/profile proof subject.
- The assessment records both `establishes` and `does_not_establish`.
- Protected-live results enter an assessment only through
  `ingest-live-evidence`, which independently authenticates workflow
  provenance through the read-only GitHub Actions API.

## Direct validation

Follow [the assessment program](../assessment-program.md). The single
`scripts/state_engine_assessment.py` entry point provides duplicate-key-safe
JSON parsing, exact catalog/manifest checks, static retained-evidence
validation, relative-link checks, and review requirements. Validate the current
contract with:

```bash
PYTHONPATH="$PWD/src" .venv/bin/python scripts/state_engine_assessment.py \
  validate-catalog docs/architecture/state_engine/proof-catalog/v3/catalog.json
```

These are direct assessment operations, not unit tests for the document
package.
