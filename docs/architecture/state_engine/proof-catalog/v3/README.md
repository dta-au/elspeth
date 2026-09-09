# State-engine proof catalog v3

Catalog v3 uses catalog schema 2 and assessment schema 3. It replaces the v2
applicability profiles with an explicit policy for every
`(leg, case, profile, dimension)` cell. A required cell has a null reason; a
reviewed `not_applicable` cell has a non-empty catalog reason.

The catalog and assessment schemas in this directory are normative. Catalog
v3 preserves every v2 semantic contract outside PB-09. PB-09 cases additionally
carry a live plugin key and a closed provider/authentication variant identity.

Assessment schema 3 records the runner and exact argument vector on every
evidence item. Local provenance binds the captured checkout Python executable
and platform. Protected-live provenance binds the GitHub repository, workflow
path and frozen-workflow digest, frozen baseline SHA, run and attempt, numeric
job identity, runner image, selector lane, authenticated artifact digest, and
scrub-report digest.

Current baselines name every prospective publication file in
`publication_paths`. Validation accepts only the exact docs overlay at the
frozen commit, or a clean single docs-only publication child whose changed
paths equal that list. Directories, globs, undeclared files, later descendants,
and non-document changes are invalid.

Protected-live results enter an assessment only through
`state_engine_assessment.py ingest-live-evidence`. The operation independently
queries the read-only GitHub Actions API and downloads the API-selected archive;
the artifact manifest cannot authenticate itself. The authenticated archive
must byte-match an exact five-file envelope (`manifest.json`, `junit.xml`,
`profile.json`, `nodes.txt`, and `scrub-report.json`) and is materialized under
deterministic per-lane publication names. Raw stdout, stderr, provider values,
and environment values are never accepted.

This directory is a Task 12 input, not the maintained-current pointer. The
repository continues to identify v2 as current until the first full v3
assessment and its documentation pointers are published atomically.
