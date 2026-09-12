# Project control

This folder holds ELSPETH's project-control artefacts. Delivery governance and
project-control reporting for ELSPETH are set by the maintainer's
organisation, not decided by this project; this README is the repository's
pointer to them, and this folder is the only authority for what they are.
(They were previously described in
[ADR-024](../architecture/adr/024-delivery-governance-for-single-maintainer-mode.md),
retired 2026-09-13 as not being an architecture decision. For the project's
own single-maintainer assurance posture — approval counts, required gates,
the two-maintainer step-up trigger — see
[GOVERNANCE.md](../../GOVERNANCE.md) § Maintainer Continuity.)

The reporting set is four living artefacts, each one to two pages:

| Artefact | State |
|---|---|
| Project Control Report (PCR) — current status, exceptions, and asks | Not written |
| T&M register — resource evidence, allocation, and reconciliation gaps | Not written |
| RAID register — live risks, assumptions, issues, and dependencies | **Held here** |
| Milestone and forecast register — commitments, forecasts, and change | Not written |

The folder also holds a program of outstanding work and a product requirements
document. These support scope and onboarding decisions; they do not substitute
for the three missing artefacts above.

The current version of each document is the file with the latest date prefix.
The current drafts use identifiers ELS-POW-1.0.0, ELS-PRD-1.0.0 and
ELS-RAID-1.0.0 in both Markdown and PDF. Each states its evidence date;
historical assessments retain their own dates. Technical tasks and detailed
acceptance evidence remain in the tracker, specifications and runbooks.

Build the PDFs with `tools/pdf/build-control-pack.sh --pdf`, setting
`FORCE_DATE` to the issue date stated in the documents. The compact briefing
layout starts with the document content and carries its identity in the footer.

What is here is maintained in full and unsanitised, but it is not published in
the repository: `.gitignore` excludes everything in this folder except this
README, so a clone contains only this note.

To read these documents, ask the project maintainer through the channels in
[SUPPORT.md](../../SUPPORT.md).
