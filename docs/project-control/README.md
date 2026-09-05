# Project Control

This folder holds ELSPETH's project-control artefacts — the lean control set
described in [ADR-024](../architecture/adr/024-delivery-governance-for-single-maintainer-mode.md).

ADR-024 defines four living artefacts, each one to two pages. One is written:

| Artefact | State |
|---|---|
| Project Control Report (PCR) — current status, exceptions, and asks | Not written |
| T&M register — resource evidence, allocation, and reconciliation gaps | Not written |
| RAID register — live risks, assumptions, issues, and dependencies | **Held here** |
| Milestone and forecast register — commitments, forecasts, and change | Not written |

The folder also holds working documents that are **not** part of the ADR-024
control set and do not substitute for the artefacts above: a program
work-package inventory and a product requirements document, with PDF
renderings of all three documents for briefing. Where an artefact is not
written, ADR-024's requirement for it stands unmet; no other document in this
folder satisfies it.

The current version of each document is the file with the latest date prefix.
A document may carry a delimited section listing work in flight at its
as-at date, which is deleted when the work lands. The PDFs are built by `tools/pdf/build-control-pack.sh --pdf`.

What is here is maintained in full and unsanitised, but it is not published in
the repository: `.gitignore` excludes everything in this folder except this
README, so a clone contains only this note.

To read these documents, ask the project maintainer through the channels in
[SUPPORT.md](../../SUPPORT.md).

This is a description of a working arrangement, not an architecture decision
record; it is deliberately not listed in the ADR index.
