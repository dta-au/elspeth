# Project Control Report — PCR-2026-001

| Field | Value |
| --- | --- |
| Publication class | **Sanitized public derivative** |
| Report ID | `PCR-2026-001` |
| As of | `2026-08-29T18:14:31+10:00` |
| Reporting period | **Unknown** — an approved reporting-period baseline was not available in the reviewed sources |
| Intended reader and task | ELSPETH maintainer and authorized project decision-makers; assess delivery confidence, exceptions, and open decisions |
| Canonical sources and cut-offs | Git delivery branch `feature/unified-lineage` at `29faafe4e8b5f72ebd3d556ccd8411e2a9c1423e` (captured `2026-08-29T18:14:31+10:00`); Filigree critical path (captured `2026-08-29T18:14:30+10:00`) and selected release-assurance records (captured `2026-08-29T18:14:21+10:00` to `18:14:24+10:00`); ADR-024 as accepted and amended `2026-08-29`; approved organisational resource records were not supplied or checked |
| Authority boundary | This report summarizes source evidence. It does not approve scope, expenditure, milestone commitments, risk acceptance, release, or operation |
| Review cadence | Monthly, and immediately when an ADR-024 exception trigger fires |
| Controlled counterpart | **Unknown** — no controlled counterpart was supplied or checked at issue time. If one is produced later, it is authoritative for protected resource detail |

This first public Project Control Report (PCR) is a decision instrument, not an
activity log. ELSPETH's published purpose is to provide high-assurance pipeline
authoring over one validated, auditable runtime. The accountable authority has
not yet confirmed that purpose as the project's current outcome and scope
baseline, so outcome confidence remains Unknown.

## Status at a glance

| Component | Status | Public-safe basis |
| --- | --- | --- |
| Outcome and scope | **Unknown** | No approved outcome, in/out scope, or scope tolerance was available in the reviewed source set. Source: ADR-024 control requirements and reviewed sources, as of this report |
| T&M | **Unknown** | The authoritative resource extract, unit or currency, envelope, tolerance, and source owner were not supplied or checked. Source: [TM-CTRL-001](tm-register.md#control-gaps), as of this report |
| Milestone and timing | **Unknown** | Filigree establishes an ordered release-assurance path, but no evidence basis for a forecast range or confidence was available. Source: [MF-001 to MF-003](milestone-forecast-register.md#current-milestones), Filigree cut-off above |
| Risk and assurance | **Unknown** | An open, material operator-only assurance dependency is on the release path, but no confirmed risk threshold, assurance baseline, or fresh gate result supports a RAG judgement. Source: [D-001](raid-register.md#live-register), Filigree records `elspeth-97d0c15eb6` and `elspeth-618b5100b8`, captured `2026-08-29T18:14:21+10:00` to `18:14:24+10:00` |
| Overall | **Unknown** | ADR-024 requires evidence and recorded tolerances for component judgements and prevents Unknown from becoming Green. No component has enough evidence for a supported RAG judgement. Source: ADR-024 and this report's component evidence, as of this report |

**Change since previous report:** Not applicable — first cycle.

## Progress and constraints

The current Filigree critical path gives a reproducible sequence from
agent-owned remediation through a key-free handoff, an operator-owned assurance
action, post-operator verification, and release preparation. The first step and
the release-preparation task are in progress; the handoff remains open, and the
post-operator verification step is pending and blocked by the operator action.
Release-preparation closure depends on that verification. This is progress
toward release assurance, but it is not evidence that release gates are
currently complete. Source: Filigree critical path and selected records at the
cut-offs above.

The load-bearing constraints are therefore:

- no approved public outcome/scope baseline or tolerance was available;
- no authoritative T&M position was available for reconciliation;
- no defensible milestone forecast basis was available; and
- an operator-only assurance dependency remains on the release path.

## Resource position

All resource values are **Unknown**, not zero. The public register contains no
individual-level or rate-derived figures. It identifies the evidence and
classification decisions needed to establish a position. See
[TM-CTRL-001](tm-register.md#control-gaps).

## Next milestones

| ID | Outcome | Current state | Timing |
| --- | --- | --- | --- |
| [MF-001](milestone-forecast-register.md#current-milestones) | Complete agent-owned remediation and publish a current key-free handoff | In progress; predecessor sequence remains open | Commitment **Unknown**; forecast **not established** |
| [MF-002](milestone-forecast-register.md#current-milestones) | Complete the operator-owned assurance action | Blocked by MF-001 | Commitment **Unknown**; forecast **not established** |
| [MF-003](milestone-forecast-register.md#current-milestones) | Verify operator outputs and complete release preparation | Verification is pending and blocked by MF-002; release preparation is already in progress, with closure dependent on verification | Commitment **Unknown**; forecast **not established** |

## Top exceptions

- **D-001 — operator assurance dependency:** an authorized operator action and
  subsequent verification remain necessary before release readiness can be
  confirmed.
- **I-001 — resource evidence gap:** the report cannot assess T&M consumption,
  forecast, envelope, tolerance, or variance.
- **I-002 — forecast evidence gap:** the report can show sequence and current
  state, but not a defensible completion range or confidence.

See the [RAID register](raid-register.md#live-register) for owners, actions, and
review fields.

## Decisions and asks

| Ask | Decision or options | Recommendation | Decision owner / by | Consequence of delay | Escalation route |
| --- | --- | --- | --- | --- | --- |
| ASK-001 | Confirm the current outcome, in/out scope, and tolerances; either adopt the published product purpose as an interim baseline or provide approved wording | Confirm a concise interim baseline before the next PCR | Owner **Unknown**; date **Unknown**. Action: the maintainer asks the accountable authority to establish both | Outcome/scope status remains Unknown and scope exceptions cannot be assessed | **Unknown**; establish with the decision owner |
| ASK-002 | Name the authoritative resource source owner, provide the approved aggregate extract, and classify which fields may be public | Supply the controlled aggregate first; publish only fields explicitly classified for public release | Owner **Unknown**; date **Unknown**. Action: the maintainer asks the accountable authority to identify the source owner | T&M status and variance remain Unknown | **Unknown**; establish with the decision owner |
| ASK-003 | Establish whether targets or commitments exist and select an evidence-based forecast method and confidence level | Keep target, commitment, and forecast separate; do not issue a date until the basis exists | Owner **Unknown**; date **Unknown**. Action: the maintainer asks the accountable authority to establish ownership and timing governance | Decision-makers have sequence but no defensible timing range | **Unknown**; establish with the decision owner |

## Companion registers

| Register | What it controls |
| --- | --- |
| [T&M Register](tm-register.md) | Resource source evidence, allocation, reconciliation, envelope, tolerance, and variance |
| [RAID Register](raid-register.md) | Live material risks, assumptions, issues, dependencies, and actions |
| [Milestone and Forecast Register](milestone-forecast-register.md) | Target, commitment, forecast, dependencies, status, and change |

## Value semantics

- **Unknown:** authoritative evidence was unavailable or not checked.
- **not set:** the accountable authority confirmed that no value, owner, or date exists.
- **not established:** no evidence basis exists for a forecast or confidence judgement.
- **Not applicable — first cycle:** there is no prior-period comparison.
