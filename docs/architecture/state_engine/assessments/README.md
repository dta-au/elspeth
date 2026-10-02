# State Engine Assessment History

Each dated directory is a baseline-bound evidence record. The currently selected
assessment stays active and is linked from the parent [State Engine hub](../README.md).
Superseded historical packages are preserved in ignored `docs-archive/`; their
links below use published Git history, not a fresh-checkout archive dependency. Git owns
ordinary document history; do not add signatures, seals, plan hashes, review
receipts, or approval chains.

## Assessments

| Assessment | Code baseline | Framework | Verdict | Status |
| --- | --- | --- | --- | --- |
| [2026-08-15 05:37 AEST](2026-08-15-0537/README.md) | `2b4b04a8a` | v3: catalog schema 2 / assessment schema 3; 73 legs, 7,010 required executable cells, per-cell applicability, per-evidence runner/argv/provenance | Not complete | Current |
| [2026-08-12 04:25 AEST](https://github.com/dta-au/elspeth/blob/2308eeccb78f41fcbae3eae3dd7968855f4cedb7/docs/architecture/state_engine/assessments/2026-08-12-0425/README.md) | `9f78d2b2a` | v2 delta: four SQLite single-process Task 5 cells promoted; PostgreSQL 16 AWS and follower profiles remain required | Not complete | Archived; superseded by the first full v3 assessment |
| [2026-08-12 02:39 AEST](https://github.com/dta-au/elspeth/blob/2308eeccb78f41fcbae3eae3dd7968855f4cedb7/docs/architecture/state_engine/assessments/2026-08-12-0239/README.md) | `af79b3404` | v2: 73 legs with explicit semantic/profile cells; PostgreSQL 16 AWS single-leader required | Not complete | Archived; parent of the v2 delta |
| [2026-07-18 18:20 AEST](https://github.com/dta-au/elspeth/blob/2308eeccb78f41fcbae3eae3dd7968855f4cedb7/docs/architecture/state_engine/assessments/2026-07-18-1820/README.md) | `3c782ac3c` | v1: 68 legs × 10 dimensions; TS-02/PB-01 contract refresh | Not complete | Archived; superseded by the v2 pinning baseline |
| [2026-07-18 16:31 AEST](https://github.com/dta-au/elspeth/blob/2308eeccb78f41fcbae3eae3dd7968855f4cedb7/docs/architecture/state_engine/assessments/2026-07-18-1631/README.md) | `422415009` | v1: 68 legs × 10 dimensions | Not complete | Archived; superseded by the 18:20 baseline |
| [2026-07-15 14:59 AEST](https://github.com/dta-au/elspeth/blob/2308eeccb78f41fcbae3eae3dd7968855f4cedb7/docs/architecture/state_engine/assessments/2026-07-15-1459/01-discovery-findings.md) | `0dcd61ac` | Seed: 18 Wave 1 legs | Not complete | Archived; denominator and remediation list superseded |

## Update policy

- Create a new directory for a new code baseline, catalog, evidence run, or
  verdict-changing assessment.
- Correct a factual mistake in an old package only with a visible erratum.
- Never promote a historical remediation checklist to live work authority;
  GitHub Issues owns current status, priority, dependencies, and assignment.
- A historical rerun writes below that assessment's `reruns/` directory and
  records divergence without replacing the original result.
- Keep packages small: assessment result, evidence, and technical review are
  normally sufficient.

## Errata

- 2026-08-30 — machine-local provenance was scrubbed from every package for
  public release: `baseline.repository_root` is now `.`, the
  `worktree_status_at_evidence_capture` dumps are emptied, and absolute
  capture-host paths (Python executable, `PYTHONPATH`, sibling worktrees,
  prose in `evidence.md`) are repository-relative or `~/`-relative. Commits,
  trees, evidence artifacts, and their digests are unchanged; the
  `parent_assessment` digests of 2026-08-12 04:25 and 05:49 were re-pinned to
  the scrubbed parent manifests. The retained `EV-00x.stdout` artifacts of the
  two 2026-07-18 packages still carry a pytest warning that names the capture
  host's venv path; they are digest-frozen evidence and were not edited.
