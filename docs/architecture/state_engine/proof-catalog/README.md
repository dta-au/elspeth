# State Engine Proof Catalog

The [v2 catalog](v2/catalog.json) is the current finite proof universe for
state-engine assessments. It closes 73 stable legs, ten dimensions, four
required state-store/deployment profile cases, named boundary cases, and ten
hard gates. Dated `assessment.json` files bind results to one exact code
baseline.

The stable namespaces are `TS-00..19`, `AUX-01..07`, `RC-01..07`,
`PB-01..11`, `RM-01..14`, and `F-01..14`. Run-coordination legs are explicit;
leader-seat and worker-registry changes are not hidden inside a generic fence.

The [v1 catalog](v1/catalog.json) is immutable historical evidence. Its 68-leg
identity and bytes remain available for strict reruns of v1 assessments; new
assessments must use v2.

## Catalog rules

- Leg IDs are literal and ordered; range expressions are explanatory only.
- Every leg uses an applicability profile that accounts for all ten dimensions;
  `family_dimension_acceptance` makes each dimension's proof obligation concrete.
- `execution_profiles` closes the state-store, deployment, lifecycle,
  state-vocabulary, and first-party plugin inventory. A changed inventory
  requires a catalog revision.
- `evidence_contract` closes behavioral promotion to all-passing executable
  pytest records and reserves documentation records for support only.
- A v2 proof cell is identified by
  `(leg_id, dimension_id, case_id, profile_case)`. The profile case binds one
  supported state store to one deployment and its lifecycle modes; evidence
  for SQLite cannot satisfy PostgreSQL, or vice versa.
- Applicability is explicit per leg and profile case. RC-05 and PB-08 are
  required only for the two SQLite follower deployments; their SQLite
  single-process and PostgreSQL cells are catalog-approved N/A. PB-11 is
  required only for PostgreSQL 16 on the AWS single-leader Landscape profile.
- The v2 profile treats every dimension as required. Narrowing a dimension to
  N/A requires a catalog revision with a precise reason; an assessor cannot do
  it ad hoc.
- Unless a leg declares dimension-specific cases, its required case is
  `leg-contract` for every dimension.
- A narrow arm is a case beneath its stable leg, never a new pseudo-leg.
- PB-06 and PB-07 declare the complete sink-effect lifecycle and restart seams
  explicitly because a broad “sink durability” cell would hide material gaps.
- PB-10 declares the complete row-union release matrix: all branches, branch
  loss before or after partial arrival, timeout, late arrival, and restart on
  either side of release.
- PB-09 names every current first-party plugin as its own required case, while
  PB-11 names PostgreSQL server-time, row-lock order, isolation, schema
  admission/migration, and ambiguous connection-loss cases.
- `maintenance` is always required.
- Every hard gate declares the dimensions from which its status, affected legs,
  and evidence support are mechanically derived.

## Assessment overlay

Each new assessment must contain all 73 v2 leg IDs. To keep unresolved
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
  result, and matching execution-profile provenance.
- `documentation` evidence may support a result but cannot independently
  produce behavioral `pass` or `partial`.
- Evidence coverage names exact
  `(leg_id, dimension_id, case_id, profile_case)` tuples and the retained node
  IDs that establish them. One node may span dimensions, but only for one
  leg/case/profile proof subject.
- The assessment records both `establishes` and `does_not_establish`.

## Direct validation

Follow [the assessment program](../assessment-program.md). The single
`scripts/state_engine_assessment.py` entry point provides duplicate-key-safe
JSON parsing, exact catalog/manifest checks, executable-node collection,
relative-link checks, and review requirements. Validate the current contract
with:

```bash
PYTHONPATH="$PWD/src" .venv/bin/python scripts/state_engine_assessment.py \
  validate-catalog docs/architecture/state_engine/proof-catalog/v2/catalog.json
```

These are direct assessment operations, not unit tests for the document
package.
