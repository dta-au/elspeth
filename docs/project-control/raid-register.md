# RAID Register — PCR-2026-001

[Return to the Project Control Report](project-control-report.md)

| Field | Value |
| --- | --- |
| Publication class | **Sanitized public derivative** |
| Report ID / as of | `PCR-2026-001` / `2026-08-29T18:14:31+10:00` |
| Intended reader and task | ELSPETH maintainer and authorized decision-makers; review live material exposure and act on exceptions |
| Canonical sources and cut-offs | Filigree critical path captured `2026-08-29T18:14:30+10:00`; selected release-assurance records captured `2026-08-29T18:14:21+10:00` to `18:14:24+10:00`; ADR-024 as amended `2026-08-29`; approved resource records not supplied or checked |
| Authority boundary | This register summarizes governing facts and actions. It does not accept risk, approve release, or replace Filigree and organisational sources |
| Review cadence | Before each monthly PCR; immediately when a material trigger fires |
| Controlled counterpart | **Unknown** — no controlled counterpart was supplied or checked at issue time. Controlled detail, if later issued, remains in its authoritative source |

Exposure is qualitative because approved scoring thresholds and tolerances were
not available. **Material** means the entry can change outcome confidence,
resource visibility, the critical path, or a decision requiring authority.

## Live register

| ID / type | Description and consequence / exposure | Owner | Response, trigger, and next action | Trend / status / last review | Public-safe source and basis |
| --- | --- | --- | --- | --- | --- |
| R-001 Risk | Single-maintainer concentration may constrain continuity or review depth, delaying delivery or assurance. **Exposure: Material**; numeric score **Unknown** | **Unknown**. Action to establish: maintainer asks the accountable authority to confirm the risk owner | **Reduce and monitor.** ADR-024 requires compensating automated controls; their current enforcement was not checked for this report. Trigger: maintainer availability or review capacity becomes a critical-path constraint. Next action: ELSPETH maintainer raises the trigger if observed and establishes the owner; next review at the next PCR, calendar date **Unknown** | Trend: **Not applicable — first cycle**. Status: Open. Reviewed `2026-08-29` | ADR-024 accepted single-maintainer posture and consequences, as of this report |
| A-001 Assumption | The published product purpose can serve as an interim reporting outcome until the accountable authority confirms a baseline. If false, outcome/scope status cannot be assessed. **Exposure: Material**; confidence **Unknown** | **Unknown**. Action to establish: maintainer asks the accountable authority to name the owner | **Validate.** Trigger: authority rejects or changes the proposed baseline. Next action: ASK-001 in the PCR; due **Unknown** because the owner and reporting calendar were unavailable | Trend: **Not applicable — first cycle**. Status: Open / unverified. Reviewed `2026-08-29` | Repository README at Git cut-off and ADR-024 baseline requirement, as of this report |
| I-001 Issue | The authoritative T&M extract, source owner, envelope, tolerance, and public classification were unavailable or not checked. Resource status and variance cannot be assessed. **Exposure: Material** | **Unknown**. Action to establish: maintainer asks the accountable authority to name the source owner | **Resolve.** Obtain the approved controlled aggregate and classification decision. Next action: ASK-002; target resolution date **Unknown** because the owner and reporting calendar were unavailable | Trend: **Not applicable — first cycle**. Status: Open. Reviewed `2026-08-29` | T&M source review for PCR-2026-001 and ADR-024, as of this report |
| I-002 Issue | No approved target, commitment, throughput history, or other forecast basis was available. Decision-makers have sequence but no defensible delivery range. **Exposure: Material** | **Unknown**. Action to establish: maintainer asks the accountable authority to name timing ownership | **Resolve.** Separate target and commitment, select an evidence-based method, and record confidence. Next action: ASK-003; target resolution date **Unknown** because ownership was unavailable | Trend: **Not applicable — first cycle**. Status: Open. Reviewed `2026-08-29` | Milestone source review and Filigree cut-offs above; ADR-024 forecast standard |
| D-001 Dependency | Release readiness requires an operator-owned assurance action after a current key-free handoff, then independent verification. Until complete, release assurance cannot be confirmed. **Exposure: Material**; RAG **Unknown** because no confirmed threshold or baseline was available | Responsible party: `operator` for the operator action; coordination owner **Unknown** and must be established | **Manage.** Trigger: key-free handoff becomes ready or the dependency changes the critical path. Next action: finish the predecessor handoff, complete the operator action, then verify; due **Unknown** because no commitment or forecast basis was available | Trend: **Not applicable — first cycle**. Status: Open; blocked by predecessor. Reviewed `2026-08-29` | Filigree `elspeth-f9c41e6ad9`, `elspeth-97d0c15eb6`, and `elspeth-618b5100b8`, captured `2026-08-29T18:14:21+10:00` to `18:14:24+10:00` |

## Escalation

ADR-024 requires immediate escalation when a critical-path blocker, material
risk trigger, resource exception, commitment exception, or unavailable required
gate changes delivery confidence. The accountable escalation route and numeric
thresholds are **Unknown** because they were not available in the reviewed
sources. ASK-001 to ASK-003 establish the missing authority and control fields;
D-001 follows the existing operator dependency in Filigree.

## Value semantics

**Unknown** means evidence was unavailable or not checked; **not set** requires
confirmation from the accountable authority; **not established** means no
forecast or confidence basis exists; **Not applicable — first cycle** means no
prior-period comparison exists.
