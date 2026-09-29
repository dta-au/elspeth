# Security assurance pack

**Product baseline:** reviewed · **Reviewed against:** `release/0.8.1` @
`49c184508` (2026-09-30) · **Deployment template:** ready · **Deployment
record:** unpopulated by design · **Owner:** ELSPETH maintainer

This folder is ELSPETH's public, reusable security-assurance baseline. It
describes the product boundary, data flows, security controls and limitations,
responsibilities, threat and privacy methods, development and supply-chain
practices, testing, incident and risk methods, and the evidence that supports
those claims.

The pack is structured for mapping to the Australian Government Information
Security Manual (ISM), the Protective Security Policy Framework (PSPF), the
Essential Eight and an Information Security Registered Assessors Program
(IRAP) assessment. It remains a product description and assessment template:
it is not a completed assessment, accreditation, authority to operate or claim
that any installation is secure.

## Public baseline and controlled deployment copy

The repository copy contains only facts that are reusable across ELSPETH
installations. For an assessment, copy the folder to the assessment team's
controlled repository and complete every **Deployment record** section there.
Those sections intentionally contain `DEPLOYMENT-TODO:` markers.

Do not complete those records in this repository for the current device,
server or another real installation.

> **Publication boundary.** Keep hostnames, addresses, account identifiers,
> role holders, network placement, actual providers and contracts, open-finding
> detail, penetration-test reports, likelihood ratings, treatment plans and
> risk acceptances in the controlled copy. The public baseline still states
> safe product limitations and non-guarantees so an assessor can understand
> the control boundary.

Start with [00 Assurance-pack lifecycle and completion
plan](00-documentation-plan.md). It defines the two-layer model, completion
criteria, evidence rules, validation checks and the steps for creating a
deployment pack.

## Documents

In this repository, every row has the same deployment state: the reusable
product content and deployment template are ready, while the installation
record is deliberately blank.

| # | Document | Primary question | Product baseline | Deployment template |
|---|---|---|---|---|
| 00 | [Lifecycle and completion plan](00-documentation-plan.md) | How is the pack maintained and instantiated? | Reviewed | Ready |
| 01 | [System overview and boundary](01-system-overview-and-boundary.md) | What product is described, and where does it stop? | Reviewed | Ready |
| 02 | [Data flows and trust boundaries](02-data-flows-and-trust-boundaries.md) | Where can data go, and where does trust change? | Reviewed | Ready |
| 03 | [Shared responsibility matrix](03-shared-responsibility-matrix.md) | What does ELSPETH provide, and what must others provide? | Reviewed | Ready |
| 04 | [Threat model](04-threat-model.md) | What threat classes apply, which controls respond, and what feeds deployment risk? | Reviewed | Ready |
| 05 | [Statement of applicability](05-statement-of-applicability.md) | How does a deployment import, assess and reconcile its ISM control set? | Reviewed | Ready |
| 06 | [Identity and access](06-identity-and-access.md) | Who can do what, and where is that enforced? | Reviewed | Ready |
| 07 | [Secrets and key management](07-secrets-and-key-management.md) | How does the product handle secret references, values and keys? | Reviewed | Ready |
| 08 | [Logging, audit and monitoring](08-logging-audit-and-monitoring.md) | What is recorded, how is it protected, and what signals are available? | Reviewed | Ready |
| 09 | [Secure development lifecycle](09-secure-development-lifecycle.md) | How are changes reviewed, tested and gated before release? | Reviewed | Ready |
| 10 | [Vulnerability and supply-chain management](10-vulnerability-and-supply-chain.md) | How are reports, dependencies and release artifacts managed? | Reviewed | Ready |
| 11 | [AI / LLM risk assessment](11-ai-llm-risk-assessment.md) | What model-specific, responsible-AI and provider risks must be assessed? | Reviewed | Ready |
| 12 | [Privacy impact assessment](12-privacy-impact-assessment.md) | What personal information can the product handle, and how does a deployment complete a PIA? | Reviewed | Ready |
| 13 | [Incident response and continuity](13-incident-response-and-continuity.md) | How should a deployment prepare, respond, preserve evidence and recover? | Reviewed | Ready |
| 14 | [Security testing](14-security-testing.md) | What product testing exists, and what independent testing must a deployment record? | Reviewed | Ready |
| 15 | [Risk register](15-risk-register.md) | How are all risk sources reconciled, treated, accepted and reviewed? | Reviewed | Ready |
| 16 | [Evidence index](16-evidence-index.md) | Where is the proof for the product claims, and what deployment evidence is required? | Reviewed | Ready |

## Reading paths

- **Assessor:** 01 → 02 → 03 → 04 → 05 → 16, then the control-domain
  documents relevant to the selected baseline.
- **Deployment owner:** 00 → 01 → 03, then complete every Deployment record
  before populating 05, 12, 13, 14 and 15.
- **Developer or reviewer:** 04 and 09–11, following evidence IDs into 16.
- **Operator:** 03 and 06–08, then 13 and the linked runbooks.

## Conventions

- **Claim-side evidence:** cite a stable evidence item as `[EV-nnn]` beside the
  control or limitation it supports. Document 16 records provenance, evidence
  strength and the supported section.
- **Measured claims:** derive counts, versions and live configuration from the
  authoritative source, record the command/date/commit and control the
  instrument before trusting it.
- **Explicit limits:** state where ELSPETH supplies no control and allocate the
  responsibility in [03](03-shared-responsibility-matrix.md).
- **Deployment placeholders:** use only `DEPLOYMENT-TODO:` and only under a
  heading containing **Deployment record**. The public baseline contains no
  unclassified placeholders or placeholder risk IDs.
- **Review pins:** document headers identify the product commit reviewed.
  Evidence rows may retain an older commit when they describe a historical
  run or observation; the row must say so.
- **Sensitive findings:** use stable opaque risk references in public control
  documents. Store finding detail, likelihood, treatment and acceptance only
  in the controlled risk register.

## Existing product sources

- [SECURITY.md](../../SECURITY.md) — vulnerability disclosure and scope
- [Platform Architecture](../release/platform-architecture.md) — architecture,
  trust boundaries and operational boundaries
- [Audit and Lineage Guarantees](../release/guarantees.md) — product
  guarantees and explicit non-guarantees
- [Architecture Overview](../../ARCHITECTURE.md) and the
  [ADR index](../architecture/adr/README.md) — design context and decisions
- [Runbook index](../runbooks/index.md) — deployment, audit, recovery, database
  and operational procedures
- [Evidence index](16-evidence-index.md) — evidence catalogue and provenance
