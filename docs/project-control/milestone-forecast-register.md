# Milestone and Forecast Register — PCR-2026-001

[Return to the Project Control Report](project-control-report.md)

| Field | Value |
| --- | --- |
| Publication class | **Sanitized public derivative** |
| Report ID / as of | `PCR-2026-001` / `2026-08-29T18:14:30+10:00` |
| Intended reader and task | Authorized decision-makers and milestone owners; distinguish sequence, target, commitment, and forecast and act on timing gaps |
| Canonical sources and cut-offs | Filigree critical path captured `2026-08-29T18:14:30+10:00`; selected release-assurance records captured `2026-08-29T18:14:21+10:00` to `18:14:24+10:00`; ADR-024 as amended `2026-08-29` |
| Authority boundary | This register reports current source state. It does not create a target, commitment, forecast, confidence level, rebaseline, or release approval |
| Review cadence | Before each monthly PCR and whenever the critical path or a commitment changes |
| Controlled counterpart | **None existed at issue time.** A later controlled register remains authoritative for protected forecast detail |

Filigree establishes the sequence below. The reviewed source set did not
contain an approved schedule commitment or a defensible probabilistic forecast.
Accordingly, missing commitments are **Unknown**, while forecasts and confidence
are **not established**. These values are not interchangeable with **not set**.

## Current milestones

| ID | Intended outcome | Accountable owner | Original / current commitment | Target | Previous / current forecast and confidence | Critical dependencies | Status and period change | Authorized rebaseline | Public-safe source and basis |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| MF-001 | Complete agent-owned release-assurance remediation and publish a current key-free handoff | **Unknown** — no accountable owner for the whole milestone was available; action: maintainer asks authority to establish one | **Unknown / Unknown** — commitment evidence unavailable | **Unknown** | Previous: **Not applicable — first cycle**. Current: **not established**; confidence **not established** because no forecast basis was available | Ordered Filigree path from `elspeth-02cd60d8cd` through `elspeth-f9c41e6ad9` | **In progress.** First path step is in progress; handoff is pending. Change: **Not applicable — first cycle** | **Unknown** — authoritative rebaseline history was not checked | Filigree critical path plus `elspeth-02cd60d8cd` and `elspeth-f9c41e6ad9`, captured at the cut-offs above |
| MF-002 | Complete the operator-owned assurance action on a current handoff | `operator` for the action | **Unknown / Unknown** — commitment evidence unavailable | **Unknown** | Previous: **Not applicable — first cycle**. Current: **not established**; confidence **not established** because no forecast basis was available | MF-001 and its final handoff | **Blocked by MF-001.** Change: **Not applicable — first cycle** | **Unknown** — authoritative rebaseline history was not checked | Filigree `elspeth-97d0c15eb6`, captured `2026-08-29T18:14:21+10:00` to `18:14:24+10:00` |
| MF-003 | Independently verify the operator outputs and complete release preparation | **Unknown** — no accountable owner for the combined milestone was available; action: maintainer asks authority to establish one | **Unknown / Unknown** — commitment evidence unavailable | **Unknown** | Previous: **Not applicable — first cycle**. Current: **not established**; confidence **not established** because no forecast basis was available | MF-002, then post-operator verification before release closeout | **Blocked by MF-002.** Change: **Not applicable — first cycle** | **Unknown** — authoritative rebaseline history was not checked | Filigree `elspeth-618b5100b8` and `elspeth-64c319bf4d`, captured `2026-08-29T18:14:21+10:00` to `18:14:24+10:00` |

## What prevents a forecast

- an approved scope and milestone baseline was not available;
- commitment and target ownership were not established in the reviewed evidence;
- no suitable throughput distribution or approved reference class was supplied;
- no confidence level was selected by the accountable authority; and
- no current approved resource position was available for capacity assumptions.

**Next action:** ASK-003 in the PCR asks the accountable authority to establish
timing ownership, determine whether targets or commitments exist, and select an
evidence-based forecast method and confidence. The decision owner and
decision-by date are **Unknown**; the maintainer must establish them.

## Forecast discipline

A target expresses a desired date. A commitment is an authorized promise. A
forecast is an evidence-based range with a confidence level. Future updates
will keep all three separate and will record the reason, authority, and date for
any rebaseline rather than silently resetting variance.

## Value semantics

**Unknown** means evidence was unavailable or not checked; **not set** requires
confirmation from the accountable authority; **not established** means no
forecast or confidence basis exists; **Not applicable — first cycle** means no
prior-period comparison exists.
