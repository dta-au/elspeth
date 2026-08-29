# Project Control

This folder holds ELSPETH's project-control registers, the lean control set
described in [ADR-024](../architecture/adr/024-delivery-governance-for-single-maintainer-mode.md):

- the Project Control Report (PCR) — current status, exceptions, and asks;
- the T&M register — resource evidence, allocation, and reconciliation gaps;
- the RAID register — live risks, assumptions, issues, and dependencies;
- the milestone and forecast register — commitments, forecasts, and change.

The registers are maintained here, in full and unsanitised, but they are not
published in the repository: `.gitignore` excludes everything in this folder
except this README, so a clone contains only this note.

To read the registers, ask the project maintainer through the channels in
[SUPPORT.md](../../SUPPORT.md).

This is a description of a working arrangement, not an architecture decision
record; it is deliberately not listed in the ADR index.
