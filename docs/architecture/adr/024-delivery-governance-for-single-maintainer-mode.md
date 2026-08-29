# ADR-024: Delivery Governance for Single-Maintainer Mode

**Date:** 2026-05-19
**Last amended:** 2026-08-29
**Status:** Accepted
**Deciders:** ELSPETH maintainer
**Tags:** governance, project-control, reporting, cicd, release-management,
branch-protection, elspeth-lints

## Context

ELSPETH started as a public open-source project maintained by one developer. It
is now being built out as government-directed work while still having one
assigned developer. That creates two governance tensions.

The first is assurance:

- government-facing delivery needs explicit control evidence;
- the repository cannot honestly use two-person review while only one developer is assigned;
- self-approval would add ceremony without improving safety;
- automated controls are already load-bearing through CI, `elspeth-lints`, CodeQL, redaction gates, signed container images, and release smoke tests.

Without a recorded decision, a reviewer could misread `required_approving_review_count: 0` as an accidental waiver of review discipline. The real posture is different: ELSPETH is in a deliberate single-maintainer mode with compensating automated controls, and it has a defined step-up path for the moment a second developer is assigned.

The second is project control. As delivery continues, project decision-makers
need a short, reliable answer to four questions:

- how time-and-materials (T&M) effort and expenditure are allocated;
- which risks, issues, assumptions, and dependencies could change the outcome;
- what the next milestones are and when they are forecast to land; and
- which decisions or interventions are needed from authorized decision-makers.

Filigree, Git, CI, architecture decision records, runbooks, and organisational
financial records contain the underlying detail. None of them alone provides a
project-control view, and copying all of them into a large project dossier
would create a second, stale source of truth. The project needs a small reporting
layer over those sources, not a full project-management method.

## Decision

ELSPETH will operate in **single-maintainer mode** until a second developer receives regular write access or participates in release-critical delivery.

In single-maintainer mode:

- human approval count remains zero because self-review is not a meaningful control;
- default-branch and release-critical merges must be protected by mandatory automated gates;
- required evidence comes from CI, policy lints, CodeQL, redaction governance, artifact provenance, signatures, and smoke tests;
- release images must be tied to commits that have passed the required CI gate;
- advisory quality signals must be labelled as advisory rather than presented as enforced thresholds.

### Minimal project-control reporting

ELSPETH will use a lean **Project Control Report (PCR)**. The PCR is a decision
instrument for the maintainer and authorized decision-makers, not a
comprehensive record of project activity. Its purpose is to show resource use,
delivery confidence, material risk, and the decisions needed to keep the
project on course.

The project-control reporting layer consists of four living artifacts:

1. **Project Control Report — one to two pages.** It contains the reporting
   period, report identifier, as-of date, and source-data cut-offs; the
   scope/resource/milestone baseline used; outcome-confidence red-amber-green
   (RAG) status plus Unknown; material change since the previous report; T&M
   position and variance by outcome or workstream; the next milestones and
   forecast ranges; the top risks and issues; and time-bound decisions or asks.
   Activity and ticket counts appear only when they support a progress or
   forecast claim.
2. **T&M register — one to two pages.** Using the granularity available in
   approved source records, it reports time, cost, or both by stable outcome or
   workstream. It states the unit or currency and actuals-through date;
   separates booked actuals, accruals or estimates, and forecast; shows the
   approved resource envelope where one exists, or a clearly labelled planning
   assumption where it does not; reconciles allocated and unallocated
   consumption to the authoritative total; and explains material variance. It
   does not infer rates, reconstruct unsupported allocations, or treat recorded
   hours as individual productivity. Detailed timesheets, rates, invoices, and
   accounting records remain in their authoritative organisational systems.
3. **Risk, assumption, issue, and dependency (RAID) register — one to two
   pages.** It contains only live, material entries. Each entry records a stable
   identifier, type, owner, consequence and exposure, response posture, trend,
   status, and last-reviewed date. Every live entry also has a next action with
   an owner and due date, or an explicit monitoring-only rationale and next
   review date. A risk or assumption includes an observable trigger where one
   materially supports control. An issue includes a target resolution date when
   supportable; otherwise it records Unknown and the action needed to establish
   one. A dependency includes its responsible party and due date where known.
   Detailed delivery defects and tasks remain in Filigree and are linked rather
   than copied.
4. **Milestone and forecast register — one to two pages.** It records the next
   load-bearing milestones, their intended outcomes, accountable owner, original
   and current approved commitment where one exists (otherwise **not set**),
   previous and current forecast range and confidence, critical dependencies,
   status, and change since the previous report. A target, forecast, and
   commitment are labelled separately. Any rebaseline records its reason,
   authority, and date; repeated replanning cannot silently reset variance to
   green. A single date without a confidence basis is not presented as a
   forecast.

No separate decision or change register is introduced. Architecture decisions
remain in ADRs, delivery decisions and scope changes remain in Filigree, policy
overrides remain in their governing control system, and release decisions remain
in release provenance. The PCR carries only the decisions made during the period
and the open asks that need action from an authorized decision-maker.

### Authority, sources, and information handling

The maintainer owns timely assembly and reconciliation of the PCR. Assembly does
not transfer accountability for figures or records supplied from another
authoritative system. Providing project-control visibility does not create an
implied approval gate for ordinary delivery or technical decisions. The PCR
must name the actual accountable authority for any change to scope, resource
tolerance, milestone commitment, material risk acceptance, or public release;
it must not invent a project board or fictional roles.

The control artifacts aggregate, but do not replace, their canonical sources:

- Filigree is authoritative for active scope, ownership, dependencies, critical
  path, acceptance criteria, and delivery status;
- Git, CI, policy gates, and release evidence are authoritative for the state of
  code and enforced controls;
- ADRs, contracts, and release guarantees are authoritative for durable design
  and assurance commitments; and
- the organisation's approved time-recording, financial, procurement, or
  contract system is authoritative for T&M actuals and rates.

ELSPETH is a public repository. Commercially sensitive rates, invoices,
personal time records, internal forecasts, and other controlled project material
must remain in an appropriately controlled system. When controls require that
protection, the full issued PCR and affected registers live in that system and
are authoritative. Any repository version is an optional, clearly labelled
sanitized derivative carrying the same report identifier and as-of date; it may
contain a non-sensitive aggregate or reference, but no sensitive link, rate,
personal time record, or commercial term. Repository publication is not the
price of project visibility.

Each issued PCR is retained through the ordinary version history of its
authoritative system. This preserves previous forecasts, commitments, and
rebaselines without creating signed receipts, hash manifests, or evidence
sidecars for a working report.

### Cadence and management by exception

The maintainer refreshes the three registers before each PCR. The default
reporting cadence is monthly; release-critical periods may use a shorter agreed
cadence. A meeting is not mandatory when the report contains no decision or
exception requiring discussion.

Bad news does not wait for the reporting calendar. The maintainer escalates a
material exception when detected, including:

- T&M consumption or forecast moving outside the agreed tolerance;
- a committed milestone forecast moving outside its agreed range;
- a new critical-path blocker or a material risk trigger firing;
- a scope change that cannot be absorbed without trading time, cost, quality, or
  another committed outcome; or
- a required assurance, release, or operational gate becoming unavailable or
  failing in a way that changes delivery confidence.

Material means likely to breach a recorded resource or milestone tolerance,
change a committed outcome or critical path, or require action beyond delegated
authority. Each exception or decision ask states the decision owner,
recommendation or real options, decision-by date, consequence of delay, and
escalation route. If a decision owner or route has not been established, the PCR
records **not set** and makes establishing it part of the ask rather than
inventing one.

The PCR records tolerances set by the actual budget, contract, or other
accountable authority. Where none has been set, it records **not set** and
raises the gap to that authority as a decision ask; the maintainer does not
invent or self-approve a tolerance.

Status is recorded separately for outcome/scope, T&M, milestone/timing, and
risk/assurance. **Green** means current evidence supports delivery within the
recorded tolerance. **Amber** means the outcome is at risk but remains
recoverable within delegated authority. **Red** means a known tolerance breach
or that recovery requires action beyond delegated authority. **Unknown** means
no current evidence, tolerance, or forecast basis supports a judgement; it is
routed to the responsible source owner or authority and becomes a decision ask
only when an actual decision is required. Unknown is never translated to green.
Overall status follows the worst load-bearing component, so a critical-path red
is never averaged away by unrelated green work. Status measures outcome
confidence, not busyness or effort.

### Deliberate limit on method and paperwork

This decision does not bootstrap PRINCE2 or an equivalent full project-control
method. It does not require a separate project initiation document, business
case pack, stage-plan set, communications plan, stakeholder register, quality
register, lessons log, document-signing package, or standing project board.
Existing useful plans, ADRs, runbooks, Filigree records, and release evidence
continue to do their current jobs.

The maintainer will propose a separate governance decision if the coordination
problem materially changes—for example, multiple delivery teams or suppliers,
contractual reporting obligations, formal stage-gate investment decisions,
delegated budget authority, or repeated failures that the lean control set does
not expose or resolve. Full-method adoption must answer a demonstrated control
need rather than anticipated organisational maturity.

Every process, gate, and document must materially improve at least one of:

- reliability of code or tests;
- integrity of code, tests, runtime data, audit evidence, or documentation; or
- supportability of code, deployments, operations, or user workflows.

Plans, run sheets, test procedures, runbooks, and incident diagnostics pass
this test when they help build, verify, or operate the system. They are ordinary
working documents: update or delete them as the system changes. Do not sign or
seal plans, generate plan hash manifests or review-receipt sidecars, construct
approval chains, or require role handoffs merely to authenticate disposable
working documents. These controls simulate a multi-person organisation without
reducing product risk.

This exclusion does not apply to controls protecting actual system assets:
source commits, releases, images, exports, audit chains, runtime data, deployed
artifacts, and their admission evidence may still require hashes, signatures,
independent automated checks, or fail-closed gates. A gate that no longer
prevents a concrete failure should be removed. If removing a practice is a
marginal call or may discard a real safeguard, the tradeoff must be surfaced to
the maintainer before removal.

When a second developer is assigned, ELSPETH will step up to **two-maintainer mode** by enabling:

- one required approving review;
- stale-review dismissal on new commits;
- last-push approval protection;
- required conversation resolution;
- CODEOWNERS or an equivalent ownership map for security-sensitive paths;
- review requirements for release tags or release branches where GitHub supports them.

## Consequences

### Positive Consequences

- The current zero-reviewer setting is explainable as an honest staffing-mode decision, not an uncontrolled gap.
- Automated controls become more important and must be wired as required checks, not merely present as optional workflows.
- The project can move quickly while there is one maintainer without pretending to have a team process.
- The step-up path is already defined for government review and for future maintainers.
- Decision-makers receive a consistent view of T&M allocation, delivery
  confidence, risks, milestones, and decisions without reading the engineering
  tracker.
- Resource, risk, and schedule exceptions become visible while there is still
  time to change scope, funding, sequencing, or expectations.
- The reporting layer stays small enough to refresh from current evidence rather
  than becoming a parallel project-management system.

### Negative Consequences

- Single-maintainer mode still has concentration risk: one person can author and merge changes if automated checks pass.
- Some controls remain platform configuration rather than repository-tracked code, so periodic ruleset inspection is necessary.
- Review quality depends heavily on CI coverage, policy lints, and disciplined issue/ADR records until a second maintainer exists.
- The maintainer must reconcile information from engineering and organisational
  systems on every reporting cycle.
- T&M and forecast data may require access controls, so the public repository
  cannot always contain the complete controlled record.
- Forecasts and risk scores will become stale unless the registers are actually
  reviewed and updated.

### Neutral Consequences

- Branch protection and repository rulesets are part of the delivery architecture.
- `elspeth-lints` and CodeQL are governance evidence, not just developer convenience tools.
- Mutation testing may remain advisory, but it must not be described as an enforced threshold until it actually fails builds.
- The PCR is not an accounting ledger, a release approval, an authority to
  operate, or an independent assurance assessment.
- Project-control reporting raises the altitude of project visibility; it does not
  transfer ordinary technical or delivery authority by implication.

## Alternatives Considered

### Require one approving review immediately

**Description:** Configure GitHub to require one human approval even while there is only one developer.

**Rejected because:** This would force self-approval or block delivery. Self-approval would be theatre and would teach reviewers to distrust the rest of the control set.

### Leave the posture implicit

**Description:** Keep the current practical setup and explain it verbally when asked.

**Rejected because:** Government-facing delivery needs durable evidence. The important distinction between "no review control" and "single-maintainer mode with compensating automated controls" should be discoverable without oral history.

### Use only manual release discipline

**Description:** Rely on the maintainer to remember which checks to run before release.

**Rejected because:** ELSPETH's safety model already favours mechanical enforcement over memory. Release and merge controls should fail closed wherever the platform can support it.

### Use Filigree and CI without a project-control report

**Description:** Give project stakeholders access to the engineering tracker and
gate outputs and answer questions as they arise.

**Rejected because:** Those sources are authoritative but operate at engineering
altitude. They do not explain T&M allocation, forecast confidence, aggregated
material risk, or the decisions needed by a project-control audience.

### Adopt a full PRINCE2 control set now

**Description:** Establish a project initiation document, business case,
project board, stage plans, strategies, registers, and formal checkpoint and
stage reporting.

**Rejected because:** The current coordination problem needs resource,
risk, milestone, and decision visibility, not a complete delivery method. Full
adoption would create multiple new maintenance surfaces before the project has
demonstrated a need for them. The option remains available if project scale,
contract, or assurance obligations change.

### Maintain one comprehensive project-control dossier

**Description:** Copy scope, plan, risks, costs, decisions, assurance evidence,
and status into one large controlled document.

**Rejected because:** It would duplicate Filigree, Git, CI, ADRs, release
evidence, and financial systems. A large dossier would be difficult to refresh
and would quickly become less reliable than the sources it summarizes.

## Related Decisions

- ADR-023: Custom Python Static Analyzer for ELSPETH-Specific CI Invariants (the `elspeth-lints` Package)

## References

- [Governance](../../../GOVERNANCE.md)
- [Requirements source map](../requirements.md)
- [elspeth-lints rationale](../../elspeth-lints/rationale.md)
- [CI workflow](../../../.github/workflows/ci.yaml)
- [CodeQL workflow](../../../.github/workflows/codeql.yaml)
- [Build and push workflow](../../../.github/workflows/build-push.yaml)
- [Composer redaction gate](../../../.github/workflows/composer-redaction-gate.yml)

## Notes

This ADR does not claim ELSPETH has reached a mature multi-maintainer operating
model or a full project-management maturity level. It records the current
operating mode and the minimum project controls needed to make resource use,
risk, milestones, forecast confidence, and authorized decisions visible. The
2026-08-29 amendment adds the PCR control set without weakening the original
single-maintainer assurance decision.
