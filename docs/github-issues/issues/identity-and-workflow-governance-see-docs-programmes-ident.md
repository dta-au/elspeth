---
title: Identity and workflow governance
labels: [area/web, type/epic]
---

Tracking issue for the identity and workflow governance programme: who can sign in to ELSPETH's web interface, and what has to happen before a pipeline they author can be run or shared. This is a programme rather than a ticket — the sixteen steps are individually startable, and the place to pick one is the README below.

## Where the detail lives

`docs-archive/2026-09-23-identity-workflow-governance/` in this repository:

- `README.md` — the scope, the documents of record, and all sixteen steps with their acceptance notes.
- `tracker-rows.json` — the original work items, retained for traceability.

Both files carry identifiers from the tracker this project previously used. They are retained deliberately, to tie each closed row to the content that replaced it, and the folder README explains them. They are not links to follow.

## Scope

- **Pluggable SSO** — single sign-on against an external identity provider, with the provider choice left to the deployment rather than hard-coded, plus the identity substrate underneath it.
- **Send for approval** (blocking: the work cannot proceed until someone decides) and **send for review** (non-blocking: a second reader records an opinion).
- A **shared library** of pipelines alongside each person's own list.
- **Per-person quotas** on model-token spend and stored data.
- **Compartment marking** — an operator-set compartment identifier stamped into exported YAML, snapshots, library rows and audit events. It is deliberately marking and recording, not prevention; the programme documents are explicit that content carried by a person legitimately in two compartments cannot be prevented from moving, and that no interface should claim otherwise.
- The **approver audit view**, and a **mailbox**: an inbox of approvals awaiting your decision and review requests addressed to you, and a sent folder tracking what you asked for and what was decided.
- The **operator cutover** — migrating an existing deployment onto the new identity model.

This is product governance: approvals and quotas for the people using ELSPETH. It is not the project's internal code-signing machinery.

## State

Measured on 2026-09-13 and not re-measured since:

| Area | Done |
|---|---|
| Phase 1 | 5 of 6 |
| Phase 2, authentication core | 6 of 6 |
| Phase 3, frontend | 2 of 3 |
| Phase 4, governance backend | 1 of 9 |
| Phase 5, frontend | 0 of 3 |
| Operator cutover | not started |

Approval, review, the shared library, quotas, compartment marking and the admin interface are not built.

## What to do

Do not take this issue as a unit of work. Open the README, pick one of the sixteen steps, and raise a separate issue for it quoting that step's acceptance notes — the steps carry their own scope and are sized to be worked individually.

Two things to check before starting any of them, because the programme documents predate the current tree: re-measure the state table above against the code rather than trusting it, and confirm that the step you picked has not been overtaken by later work. The phases are ordered — Phase 4's governance backend is the largest unstarted block, and Phase 5's frontend depends on it.

## Documents of record

- `docs/specs/2026-09-02-pluggable-sso-design.md`
- `docs-archive/2026-09-19-identity-workflow-finalization.md`
- `docs/plans/2026-09-13-kubernetes-and-identity/`
