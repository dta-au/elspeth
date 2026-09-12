# ADR-024: Delivery Governance for Single-Maintainer Mode — Retired

**Date:** 2026-05-19
**Retired:** 2026-09-13
**Status:** Retired — not an architecture decision. Superseded by no ADR.
**Deciders:** ELSPETH maintainer
**Tags:** governance, project-control, delivery-posture, retired

## Why this record was retired

ELSPETH's delivery governance and project-control reporting are set by the
maintainer's organisation. They are not a decision this project made, and
they are not an architectural property of the pipeline engine. Recording them
here filed an external mandate as a project decision, which put the ADR
corpus in the position of appearing to own — and to be able to change — a
posture it does not control.

Two symptoms made that visible. The 2026-08-29 amendment specified four
living project-control artefacts, and by 2026-09-11 three of them were
recorded as "Not written" while the artefacts that did exist were not the
four it named. And the ADR's stated reason for existing was that the posture
"should be discoverable without oral history", yet `docs/project-control/` is
excluded from the repository, so a reader of a clone could not verify any of
it. Both symptoms follow from the same misfiling: an ADR cannot be the
authority for something decided elsewhere.

Retiring the record does not retire the practice. Nothing about how the
project is governed, reported, or released changes because of this ADR's
status.

## Where the content now lives

| Content | Home |
| --- | --- |
| Single-maintainer assurance posture — zero required approvals, mandatory automated gates, required evidence, the two-maintainer step-up trigger | [GOVERNANCE.md](../../../GOVERNANCE.md) § Maintainer Continuity |
| Delivery posture test — a process, gate, or document is kept only when it improves reliability, integrity, or supportability; no signing or sealing of disposable working documents | [AGENTS.md](../../../AGENTS.md) § Project delivery posture |
| Project-control report set — the artefacts, their identifiers, and how to obtain them | `docs/project-control/README.md` |
| Scope of audit grade — product surfaces versus the project's own tooling | [ADR-046](046-audit-grade-is-a-product-characteristic.md) |
| Why ELSPETH ships a custom static analyzer as governance evidence | [ADR-023](023-custom-python-ci-analyzer.md) |

Sibling ADRs that cite ADR-024 in their *Related Decisions* sections
([ADR-025](025-multi-source-ingestion.md),
[ADR-026](026-durable-token-scheduler.md),
[ADR-029](029-journal-is-barrier-buffer-truth.md)) cite it only as the
governance context in force at the time. Those references remain accurate as
history; read them against GOVERNANCE.md for the current posture.

## Consequences

- The ADR corpus no longer claims authority over externally mandated
  governance. An ADR states a decision this project made and can revisit;
  this was neither.
- The single-maintainer assurance posture stays publicly discoverable, in the
  governance document that already owned the topic, rather than in an
  architecture record.
- The file is kept rather than deleted so existing references resolve and the
  reasoning above stays readable. It carries no live decision.
