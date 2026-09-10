# ELSPETH roadmap

The proposed delivery path is to prepare a controlled pilot, complete the
platform and identity changes needed for durable use, and establish the
evidence required for a 1.0 release. Broader capabilities follow those foundations.

The first decision is whether users can join a named pilot with an agreed
data-disposal date, or need their data retained from the outset. This decision
is required before any onboarding commitment.

**Planning basis:** [Work package inventory, 3 September 2026](docs/project-control/2026-09-03-work-packages.md).
Supplemented by the [web API seam design, 8 September 2026](docs/specs/2026-09-08-application-api-seam-design.md)
the [Composer wires remediation plan, 8 September 2026](docs/plans/2026-09-08-composer-wires-campaign.md)
and the compiler specifications linked below.
Multi-replica implementation remediation also draws on the
[0.8.0 changelog](CHANGELOG.md#080---2026-09-07-unified-lineage-and-production-hardening).
This roadmap summarises their scope and proposed dependencies. It
does not report current completion status; work may have progressed since the
assessment. Delivery dates are not committed. Confirm remaining scope against
the current implementation before scheduling a package.

## Delivery sequence

Stages describe dependency order, not calendar periods. Evaluation, defect
correction, release preparation and environment provisioning can run alongside
other work where they do not change the same components.

Retire bugs throughout delivery: reproduce the defect, correct its cause,
verify the integrated fix and reconcile the issue record with the evidence.
Coordinate Composer wires remediation with guided-mode and defect work
affecting the same components.
Multi-replica pinning (B.5) addresses the implementation gaps recorded in the
changelog and feeds the platform acceptance and release evidence in stages 3 and 4.

| Stage | Main work | Intended outcome and condition for proceeding |
| --- | --- | --- |
| 1. Set the onboarding path | Decide the pilot model, identity model and default authoring mode. Scope the pilot spend ceiling. Begin evaluation implementation and environment access arrangements. | A defined route to first use, with data-retention expectations and cost limits settled before onboarding. |
| 2. Prepare and evaluate a pilot | Resolve preview and sharing defects. Deliver the agreed spend ceiling and assess pilot access controls. Establish comparable guided and freeform measurements. | Named participants can use the agreed pilot scope and provide evidence for authoring improvements. The pilot depends on acceptance of a disposal date. |
| 3. Complete the foundations for durable use | Integrate and verify multi-replica safety before the identity data-store changes. Complete identity, access and administration. Coordinate changes to persisted contracts, including per-user customisation, with the identity cutover. | The planned identity cutover is complete before users rely on retained data. Platform support claims have the required acceptance evidence. |
| 4. Establish release and assurance evidence | Complete engine evidence, security and data-lifecycle work, release checks and operator signing. Run the assurance programme against the agreed deployment profiles. | Release claims are supported by evidence, with any exclusions stated. A 1.0 claim depends on the agreed completeness criteria. |
| 5. Extend capability | Schedule the web API seam, compiler seam, worker affinity, additional plugins, Composer skillpacks and branding according to need and dependencies. Design embedded graphs and iteration last. | Users can build their own web layer against a supported backend contract. New capabilities build on the established execution, authoring and assurance contracts. Scope decisions precede estimates. |

The pilot does not require the full identity or multi-replica packages. It does
require a scoped spend ceiling: if limits must be enforced per person, the
pilot depends on the quota work within A.1 and its timing advantage may be lost.

The compiler and branding packages have no fixed position in the source plan.
Their placement among extensions is indicative; the maintainer must decide
whether to bring them forward. Contributor setup work can also start earlier,
while shared-maintainer operation depends on release and security controls.

The web API seam can proceed independently of the compiler seam. Its initial
contract work precedes handing browser development to another team; placing it
among extensions does not require it to wait for the earlier stages.

## Main work packages

Existing package identifiers retain the source inventory's numbering. A.5,
B.5, C.3, C.4 and E.2 identify additional roadmap packages: user customisation,
multi-replica pinning, the web API seam, Composer wires remediation and
Composer skillpacks. Outcomes below describe the planned work, not capabilities
certified as available.

### A. Readiness for users

| Package | Main scope and intended outcome | Dependency or decision |
| --- | --- | --- |
| **A.1 Identity, access and administration** | Establish stable user identities, single sign-on, roles and administration. Add approval workflows, review records, a shared library and per-person quotas, then complete integration and cutover. | External identity-provider registrations precede dependent implementation. Integrate B.4 before changing the same sessions store. A central organisation-level console requires separate scope. |
| **A.2 Guided mode and first-run experience** | Improve entry, stage order, navigation, decision presentation, explanatory text and accessibility. Compare guided and freeform use and conduct participant testing. | Establish D.2 measurement first. Resolve design rulings and order work against collector remediation. The model continues to author pipelines; the tutorial uses the standard backend. |
| **A.3 Retire bugs** | Correct defects in authoring, execution and output handling, with explicit capacity for reproduction, repair and regression verification. Close issue records when the integrated fix is verified; reconcile already-fixed reports against the current implementation. | Prioritise defects that prevent onboarding or invalidate results. Assign engine and authoring repairs without counting them again in other packages. |
| **A.4 Customer branding and presentation** | Support controlled customer palettes, contrast variants and colour checks while protecting security markings and product vocabulary. | Decide whether the purpose is branding or distinguishing deployments, whether renaming and shared reviews are included, and how supplied palettes pass accessibility checks. |
| **A.5 User customisation** | Consolidate per-person customisation of the authoring experience: the level of detail presented, the default authoring mode, and standing personal preferences such as preferred name, language variant and explanation style, carried into Composer sessions so they need not be restated each time. Record which preferences were in force for a composition. | Preferences are presentational and stylistic. They carry no authority over pipeline structure, validation, custody or required controls, and the controls that enforce this remain server-side. Coordinate the persisted-contract change with the identity data-store work in A.1. Decide whether recurring preferences warrant typed fields rather than free text. |

### B. Platform and deployment

| Package | Main scope and intended outcome | Dependency or decision |
| --- | --- | --- |
| **B.1 Engine completion** | Complete concurrency evidence and consistent follower behaviour for groups of related rows. Consolidate follower controls and test recovery, worker loss and incomplete groups. Provide the common engine support needed by plugin error routing and document reassembly. | Establish evidence for each supported profile. This package precedes worker affinity and embedded graphs. |
| **B.2 Worker affinity** | Direct work to workers with the required hardware, credentials or network access. Cover initial execution, retries and loss of a required worker class. | Follow B.1. Decide whether and how a run can start before a required worker class registers; this affects the execution model and scope. |
| **B.3 Embedded graphs and iteration** | Design reusable nested workflows and iteration, including accounting, recovery and authoring support. | Sequence last. Decide whether an embedded graph belongs to its parent run or has its own run, then choose the node and closure model before estimating. |
| **B.4 Multi-replica safety and Azure deployment** | Integrate process coordination, verify safe rollout overlap and takeover, and complete the selected Azure deployment package, runbooks and live acceptance. | The source orders integration before A.1's data-store changes. Select Azure Container Apps or Kubernetes and arrange live access alongside D.5. Safe temporary overlap does not establish horizontal scaling support. |
| **B.5 Multi-replica pinning and implementation remediation** | Complete the authority checks that bind each mutation to its valid leader or worker membership. Replace remaining plain run-ID write paths in data-flow, execution and scheduler repositories with the required authority, checked in the writing transaction. Pin coverage with regression checks that detect missing enforcement and prove stale owners cannot write. | The 0.8.0 changelog records partial delivery of ADR-048 and targets completion in 0.8.1. Recheck the remaining sites before scheduling. Coordinate with B.1 and B.4; count these implementation repairs here only. Preserve separate leader and worker authority and the database-clock rules. |

The [changelog](CHANGELOG.md) identifies incomplete mutation fencing as a
remaining multi-replica implementation issue. B.5 covers that completion and
its verification; it does not treat the first tranche as a completed safety
model. The changelog also records coordination tables without production
writers, including run-start permits, cross-replica tickets, rate-limit state
and cleanup claims. Reconcile those gaps within B.4 before claiming deployment
acceptance.

### C. Correctness by construction

| Package | Main scope and intended outcome | Dependency or decision |
| --- | --- | --- |
| **C.1 Contract and integration hardening** | Align what components produce, what other components accept and what documentation teaches. Replace duplicated or loosely typed definitions with shared contracts and checks. | Coordinate persisted sharing, failure-code, audit-snapshot and node-type contracts with the identity cutover, before durable use. Sequence remaining Composer work against A.2 and A.3. |
| **C.2 Compiler seam** | Establish the backend boundary between authoring and execution. Compile YAML and Composer definitions into the same pipeline bundle, carrying secret references rather than values, and verify it before execution. | The maintainer must decide whether and when to schedule it. Separate initial compilation and execution from the broader persisted-artefact work. Initial hash checks establish integrity, not operator signing authority. |
| **C.3 Web API seam** | Publish a versioned application programming interface (API) so users can bring their own web layer or other client. Provide a tracked OpenAPI contract, generated client types, compatibility negotiation, response fixtures and documented progress channels. Keep authoring, validation, access control and graph interpretation in the backend. | Can proceed independently of C.2. Establish the contract before a browser-development handoff. Initial delivery supports one contract version; independent releases and separate-origin hosting require later decisions and evidence. |
| **C.4 Composer wires remediation** | Connect each advertised tool setting to its validation model, handler behaviour, redaction rules, planner guidance and user-facing proposal. Repair settings that are dropped or hidden, misleading change summaries and inconsistent response fields. | Derive checks from source, prove they detect deliberate faults, and verify results with live Composer trials. Coordinate with A.2 and A.3. This package gives the Composer wires campaign its own scope within the broader C.1 hardening work; count that work here only. |

The [web API seam design](docs/specs/2026-09-08-application-api-seam-design.md)
defines the boundary between clients and the backend. It enables a replacement
web interface to use documented application services without depending on
internal Python modules. Contract fixtures let client developers test without
a running backend. Building a replacement client is outside this package.

The [compiler architecture](docs/specs/2026-04-15-compiled-pipeline-architecture-design.md)
and [initial compiler seam sketch](docs/specs/2026-08-20-compiler-facade-mvp-sketch.md)
define the boundary inside the backend. Initial delivery checks configuration
integrity before creating plugins, then checks graph integrity before execution.
It retains execution from settings; signing authority and cross-host portability
are outside that initial scope. The compiler consumes and checks what the
planner authored; it does not author pipeline structure.

### D. Assurance and release readiness

| Package | Main scope and intended outcome | Dependency or decision |
| --- | --- | --- |
| **D.1 Release readiness** | Stabilise release checks, resolve the signing backlog, address expiring exemptions and align published guarantees with evidence. Decide whether to publish a measured performance envelope. | Schedule a freeze for operator signing after integration churn settles. Resolve exemption expiry before it blocks release. |
| **D.2 Evaluation and measurement** | Build repeatable operational tests, a standard evaluation driver and scorer, and participant evaluation. Establish a baseline for guided and freeform authoring. | Begin before assessing A.2 improvements. Use pilot evidence where available; retain comparable scenarios across authoring modes. |
| **D.3 Durable-state assurance to 1.0** | Complete the required execution and recovery evidence across supported profiles. Repair verification methods that cannot detect the defects they claim to test. | Requires the environment in D.5 for live evidence, or an explicit amendment that narrows the completeness criteria. Engineering tests alone cannot replace required live evidence. |
| **D.4 Security posture and data lifecycle** | Establish a disclosure channel and system threat model. Address contributor verification, deployment upgrades and supply-chain assurance. | Resolve verification before accepting a second developer's contributions. Set an upgrade approach for retained data; backup and restore do not provide a migration path. |
| **D.5 Test and evaluation environment** | Provide an Azure development environment, access and live-provider testing suitable for deployment acceptance and assurance. | Arrange subscription and enclave access alongside B.4. If unavailable, the owner must decide whether to narrow the 1.0 assurance claim. |

### E. Capability

| Package | Main scope and intended outcome | Dependency or decision |
| --- | --- | --- |
| **E.1 Plugin work** | Prioritise row-failure routing and plugin contracts, then join typing, document reassembly and provider behaviour. Consider knowledge-management and monitoring capabilities after scope review. Keep plugin documentation aligned with the registry. | Row-failure routing and reassembly share engine dependencies owned by B.1. Reassess search designs against existing retrieval support. Decide which plugins users may author through the web before committing to larger extensions. |
| **E.2 Composer skillpacks** | Allow a plugin to ship long-form guidance describing the considerations that govern its correct use, and let the Composer planner retrieve that guidance when it judges it relevant. Advanced statistical and industrial plugins carry assumptions, sequencing requirements and failure semantics that a short hint cannot convey. Record which guidance was available and which was used. | Follows E.1's decision on which plugins users may author through the web. Guidance is explanatory only: it carries no pipeline structure, and the planner continues to author every pipeline. Decide the size limits and the first plugin to document before writing further guidance to fit them. |

The [Composer skillpacks design](docs/specs/2026-09-10-plugin-skillpacks-design.md)
describes guidance that ships with the plugin it documents and reaches the
planner only when the planner asks for it, so its cost falls on the sessions
that need it. The [user customisation design](docs/specs/2026-09-10-user-standing-preferences-design.md)
describes the standing-preferences part of A.5. Both record in the audit trail
what was in force for a composition, and neither authors pipeline structure.
Both apply uniformly to the first-run tutorial, which continues to use the
standard backend; [ADR-049](docs/architecture/adr/049-tutorial-canary-baseline-is-configuration-relative.md)
records what that means for the tutorial's use as a machinery signal. A.5 can
proceed earlier than its placement suggests if its persisted-contract change is
coordinated with A.1.

### F. Continuity

| Package | Main scope and intended outcome | Dependency or decision |
| --- | --- | --- |
| **F.1 Multi-developer readiness** | Provide reproducible setup, installed quality checks, onboarding guidance and access to project knowledge. Establish review controls and responsibility for dependency updates. | Setup can start early. Shared maintenance depends on D.1 and D.4, plus decisions on signing-key custody and how project records are shared. |

## Decisions that set scope and timing

These decisions were open in the source assessment. Confirm their disposition
before treating them as outstanding actions.

| Decision | Required by | Effect on the roadmap |
| --- | --- | --- |
| Named pilot with agreed data disposal, or durable users from the outset | Before an onboarding commitment | The source recommends a pilot, subject to participant acceptance, a scoped spend ceiling and an owner judgement on access controls. Durable use follows the identity cutover. |
| Azure subscription and enclave access, or narrower completeness criteria | Before live deployment acceptance and D.3 assurance can complete | Determines whether to provision D.5 or reduce the supported assurance claim. |
| Key custody and access to project records | Before shared-maintainer operation under F.1 | Determines continuity arrangements and the second maintainer's signing and tooling access. |
| Identity model, control enforcement and public-sharing content | Before the affected A.1 and C.1 implementation | Settles ownership and persisted contracts before users rely on them. |
| Deployment target, worker start conditions, iteration model and organisation-console scope | Before sizing the affected packages | Determines implementation scope for B.4, B.2, B.3 and any console beyond A.1. |
| Web API and compiler seam scheduling, release freeze and treatment of excluded work | Before committing a delivery schedule | Establishes what the schedule includes, when browser development can be handed over and when release signing can converge. |

The source assigns engineering rulings to the maintainer. It records the
accountable authority for scope, milestone commitments and release as not
assigned. It sets the next review at the onboarding decision or 3 October 2026.

Detailed dependencies, risks and assumptions remain in the
[work package inventory](docs/project-control/2026-09-03-work-packages.md) and its
[companion register](docs/project-control/2026-09-03-implementation-raid-register.md).
